"""Tests for the IOC Sea Level Station Monitoring Facility fetcher.

Only the HTTP call is mocked; the payloads are real responses captured from the
facility's open web service, battery and switch channels included.
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from rivretrieve import IOCSeaLevelFetcher, constants
from rivretrieve.ioc_sealevel import CHUNK_DAYS, FEET_TO_METRES, MAX_DAYS, SENSORS

TEST_DATA_DIR = Path(os.path.dirname(__file__)) / "test_data"


def load(name):
    with open(TEST_DATA_DIR / name, encoding="utf-8") as handle:
        return json.load(handle)


def mock_session(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    session = MagicMock()
    session.get.return_value = response
    return patch("rivretrieve.ioc_sealevel.utils.requests_retry_session", return_value=session), session


class TestIOCMetadata(unittest.TestCase):
    def setUp(self):
        patcher, _ = mock_session(load("ioc_stationlist_sample.json"))
        with patcher:
            self.frame = IOCSeaLevelFetcher().get_metadata()

    def test_one_row_per_station_not_per_sensor(self):
        self.assertEqual(len(self.frame.index), len(set(self.frame.index)))
        self.assertIn("vung", self.frame.index)
        vung = self.frame.loc["vung"]
        self.assertEqual(vung[constants.STATION_NAME], "Vung Tau")
        self.assertAlmostEqual(float(vung[constants.LATITUDE]), 10.34, places=1)

    def test_preferred_sensor_is_radar(self):
        self.assertEqual(self.frame.loc["lank", "primary_sensor"], "rad")
        for sensors in self.frame["sensors"]:
            self.assertTrue(all(s in SENSORS for s in sensors.split(",")))

    def test_deep_ocean_buoys_and_sensorless_rows_are_left_out(self):
        """A DART tsunameter reads thousands of metres of water column, not a coastal level."""
        self.assertNotIn("dacp", self.frame.index)
        self.assertNotIn("agua2", self.frame.index)

    def test_units_are_kept_per_sensor(self):
        self.assertIn(":F", self.frame.loc["abas", "sensor_units"])

    def test_cached_list_covers_southeast_asia(self):
        frame = IOCSeaLevelFetcher.get_cached_metadata()
        in_region = frame[constants.LATITUDE].between(-11.5, 24.5) & frame[constants.LONGITUDE].between(92, 142)
        self.assertGreater(int(in_region.sum()), 80)
        for code in ["vung", "quin", "lank"]:
            self.assertIn(code, frame.index)


class TestIOCData(unittest.TestCase):
    def setUp(self):
        self.fetcher = IOCSeaLevelFetcher()
        self.payload = load("ioc_data_vung_20260925.json")
        self.pace = patch("rivretrieve.ioc_sealevel._pace")
        self.pace.start()
        self.units = patch("rivretrieve.ioc_sealevel._station_units", return_value={})
        self.units.start()

    def tearDown(self):
        self.pace.stop()
        self.units.stop()

    def test_instant_uses_one_sensor_only(self):
        """Vung Tau carries ra2, ra3 and enc plus battery and switch channels."""
        patcher, _ = mock_session(self.payload)
        with patcher:
            result = self.fetcher.get_data("vung", constants.STAGE_INSTANT, "2026-09-25", "2026-09-25")

        self.assertEqual(result.attrs["sensor"], "ra2")
        expected = {p["stime"]: p["slevel"] for p in self.payload if p["sensor"] == "ra2"}
        self.assertEqual(len(result), len(expected))
        first = result.index[0].strftime("%Y-%m-%d %H:%M:%S")
        self.assertAlmostEqual(float(result.iloc[0, 0]), float(expected[first]))
        # Battery voltage (~13 V) must never leak in as a water level.
        self.assertTrue(result[constants.STAGE_INSTANT].between(-2, 6).all())

    def test_hourly_means_need_half_the_usual_readings(self):
        patcher, _ = mock_session(self.payload)
        with patcher:
            result = self.fetcher.get_data("vung", constants.STAGE_HOURLY_MEAN, "2026-09-25", "2026-09-25")

        readings = pd.Series(
            {pd.Timestamp(p["stime"]): p["slevel"] for p in self.payload if p["sensor"] == "ra2"}
        ).sort_index()
        counts = readings.groupby(readings.index.floor("1h")).count()
        self.assertEqual(set(result.index), set(counts[counts >= 30].index))
        hour = result.index[0]
        expected = readings[readings.index.floor("1h") == hour].mean()
        self.assertAlmostEqual(float(result.loc[hour].iloc[0]), float(expected), places=6)

    def test_feet_are_converted_to_metres(self):
        patcher, _ = mock_session(self.payload)
        with patcher, patch("rivretrieve.ioc_sealevel._station_units", return_value={"vung": {"ra2": "F"}}):
            feet = self.fetcher.get_data("vung", constants.STAGE_INSTANT, "2026-09-25", "2026-09-25")
        patcher, _ = mock_session(self.payload)
        with patcher:
            metres = self.fetcher.get_data("vung", constants.STAGE_INSTANT, "2026-09-25", "2026-09-25")
        self.assertAlmostEqual(float(feet.iloc[0, 0]), float(metres.iloc[0, 0]) * FEET_TO_METRES)

    def test_long_ranges_fetch_only_the_most_recent_days_in_windows(self):
        patcher, session = mock_session([])
        with patcher:
            self.fetcher.get_data("vung", constants.STAGE_DAILY_MEAN, "2016-01-01", "2026-10-03")

        calls = [kwargs["params"] for _, kwargs in session.get.call_args_list]
        self.assertEqual(len(calls), -(-MAX_DAYS // CHUNK_DAYS))
        first_start = pd.Timestamp(calls[0]["timestart"])
        self.assertEqual(first_start, pd.Timestamp("2026-10-03") - pd.Timedelta(days=MAX_DAYS - 1))
        for params in calls:
            span = pd.Timestamp(params["timestop"]) - pd.Timestamp(params["timestart"])
            self.assertLessEqual(span.days, CHUNK_DAYS)

    def test_unsupported_variable_raises(self):
        with self.assertRaises(ValueError):
            self.fetcher.get_data("vung", constants.DISCHARGE_DAILY_MEAN, "2026-09-25", "2026-09-26")


if __name__ == "__main__":
    unittest.main()
