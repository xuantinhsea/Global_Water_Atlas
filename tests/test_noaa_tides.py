"""Tests for the NOAA Tides & Currents (CO-OPS) fetcher.

Only the HTTP call is mocked; the payloads are real responses captured from the
CO-OPS Data and Metadata APIs.
"""

import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import requests
from pandas.testing import assert_frame_equal

from rivretrieve import NOAATidesFetcher, constants, noaa_tides

TEST_DATA_DIR = Path(os.path.dirname(__file__)) / "test_data"


def load(name):
    with open(TEST_DATA_DIR / name, encoding="utf-8") as handle:
        return json.load(handle)


def response(payload, status=200):
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = load(payload) if isinstance(payload, str) else payload
    if status >= 400:
        mock.raise_for_status.side_effect = requests.exceptions.HTTPError(f"HTTP {status}")
    else:
        mock.raise_for_status.return_value = None
    return mock


def mock_session(*responses):
    session = MagicMock()
    session.get.side_effect = list(responses)
    return patch("rivretrieve.noaa_tides.utils.requests_retry_session", return_value=session), session


def params_of(call):
    return call.kwargs["params"]


class NOAATestCase(unittest.TestCase):
    def setUp(self):
        # Pacing is exercised in its own test; everywhere else it would only slow the suite.
        pacing = patch.object(noaa_tides, "MIN_REQUEST_INTERVAL", 0)
        pacing.start()
        self.addCleanup(pacing.stop)
        self.fetcher = NOAATidesFetcher()


class TestNOAATidesMetadata(NOAATestCase):
    def test_get_metadata_merges_active_historic_and_water_temperature(self):
        payloads = {
            "historicwl": response("noaa_tides_stations_historic_sample.json"),
            "waterlevels": response("noaa_tides_stations_active_sample.json"),
            "watertemp": response("noaa_tides_stations_watertemp_sample.json"),
        }
        session = MagicMock()
        session.get.side_effect = lambda url, params, timeout: payloads[params["type"]]
        with patch("rivretrieve.noaa_tides.utils.requests_retry_session", return_value=session):
            frame = self.fetcher.get_metadata()

        self.assertEqual(frame.index.name, constants.GAUGE_ID)
        self.assertEqual(set(frame.index), {"8518750", "9063063", "8761955", "1495000", "8720245"})

        battery = frame.loc["8518750"]
        self.assertEqual(battery[constants.STATION_NAME], "The Battery")
        self.assertAlmostEqual(battery[constants.LATITUDE], 40.7006, places=3)
        self.assertAlmostEqual(battery[constants.LONGITUDE], -74.0142, places=3)
        self.assertEqual(battery["status"], "active")
        self.assertEqual(battery["default_datum"], "MLLW")
        self.assertEqual(battery["established"], "1920-05-24")
        self.assertTrue(pd.isna(battery["removed"]))
        self.assertTrue(battery[constants.STAGE_INSTANT])
        self.assertTrue(battery[constants.WATER_TEMPERATURE_INSTANT])

        self.assertEqual(frame.loc["9063063", "default_datum"], "IGLD")  # Cleveland, Lake Erie
        self.assertEqual(frame.loc["8761955", "default_datum"], "STND")  # Carrollton, Mississippi River

        esperanza = frame.loc["1495000"]
        self.assertEqual(esperanza["status"], "historic")
        self.assertEqual(esperanza["removed"], "1999-02-26")
        self.assertTrue(esperanza[constants.STAGE_INSTANT])  # removed after 6-minute data began
        self.assertFalse(esperanza[constants.WATER_TEMPERATURE_INSTANT])

        # A water temperature station with no water level record offers no stage.
        temperature_only = frame.loc["8720245"]
        self.assertFalse(temperature_only[constants.STAGE_DAILY_MEAN])
        self.assertTrue(temperature_only[constants.WATER_TEMPERATURE_INSTANT])
        self.assertTrue(pd.isna(temperature_only["default_datum"]))

    def test_cached_metadata_covers_the_network(self):
        frame = NOAATidesFetcher.get_cached_metadata()
        for gauge_id in ["8518750", "9414290", "9063063", "8761955"]:
            self.assertIn(gauge_id, frame.index)
        self.assertTrue(frame[constants.LATITUDE].notna().all())
        self.assertTrue(frame[constants.STATION_NAME].notna().all())
        self.assertGreater((frame["status"] == "active").sum(), 250)
        for variable in NOAATidesFetcher.get_available_variables():
            self.assertIn(variable, frame.columns)


class TestNOAATidesData(NOAATestCase):
    def test_stage_instant(self):
        patcher, session = mock_session(response("noaa_tides_8518750_water_level_20240101.json"))
        with patcher:
            result = self.fetcher.get_data("8518750", constants.STAGE_INSTANT, "2024-01-01", "2024-01-01")

        expected_head = pd.DataFrame(
            {
                constants.TIME_INDEX: pd.to_datetime(["2024-01-01 00:00", "2024-01-01 00:06", "2024-01-01 00:12"]),
                constants.STAGE_INSTANT: [0.589, 0.617, 0.64],
            }
        ).set_index(constants.TIME_INDEX)
        assert_frame_equal(result.head(3), expected_head, check_dtype=False)
        # Ten readings an hour, and the whole of the end day is kept.
        self.assertEqual(len(result), 240)
        self.assertEqual(result.index[-1], pd.Timestamp("2024-01-01 23:54"))
        self.assertIsNone(result.index.tz)
        self.assertEqual(result.attrs["datum"], "MLLW")

        session.get.assert_called_once()
        params = params_of(session.get.call_args)
        self.assertEqual(session.get.call_args.args[0], noaa_tides.DATA_URL)
        self.assertEqual(params["station"], "8518750")
        self.assertEqual(params["product"], "water_level")
        self.assertEqual(params["datum"], "MLLW")
        self.assertEqual((params["begin_date"], params["end_date"]), ("20240101", "20240101"))
        self.assertEqual((params["units"], params["time_zone"]), ("metric", "gmt"))

    def test_daily_mean_is_the_mean_of_24_hourly_heights(self):
        payload = load("noaa_tides_8518750_hourly_height_20230101.json")
        patcher, session = mock_session(response(payload))
        with patcher:
            result = self.fetcher.get_data("8518750", constants.STAGE_DAILY_MEAN, "2023-01-01", "2023-01-03")

        by_day = {}
        for reading in payload["data"]:
            by_day.setdefault(reading["t"][:10], []).append(float(reading["v"]))
        expected = pd.DataFrame(
            {
                constants.TIME_INDEX: pd.to_datetime(sorted(by_day)),
                constants.STAGE_DAILY_MEAN: [sum(by_day[d]) / len(by_day[d]) for d in sorted(by_day)],
            }
        ).set_index(constants.TIME_INDEX)
        self.assertEqual([len(v) for v in by_day.values()], [24, 24, 24])
        assert_frame_equal(result, expected, check_dtype=False, check_freq=False)
        self.assertEqual(params_of(session.get.call_args)["product"], "hourly_height")

    def test_daily_mean_drops_days_missing_an_hour(self):
        """A partial day would average an arbitrary part of the tidal cycle."""
        payload = load("noaa_tides_8518750_hourly_height_20230101.json")
        payload["data"] = [r for r in payload["data"] if r["t"] != "2023-01-02 13:00"]
        patcher, _ = mock_session(response(payload))
        with patcher:
            result = self.fetcher.get_data("8518750", constants.STAGE_DAILY_MEAN, "2023-01-01", "2023-01-03")
        self.assertEqual(list(result.index), list(pd.to_datetime(["2023-01-01", "2023-01-03"])))

    def test_monthly_mean_uses_msl_and_whole_months(self):
        payload = load("noaa_tides_8518750_monthly_mean_2023.json")
        patcher, session = mock_session(response(payload))
        with patcher:
            result = self.fetcher.get_data("8518750", constants.STAGE_MONTHLY_MEAN, "2023-03-15", "2023-05-10")

        msl = {int(r["month"]): float(r["MSL"]) for r in payload["data"]}
        expected = pd.DataFrame(
            {
                constants.TIME_INDEX: pd.to_datetime(["2023-03-01", "2023-04-01", "2023-05-01"]),
                constants.STAGE_MONTHLY_MEAN: [msl[3], msl[4], msl[5]],
            }
        ).set_index(constants.TIME_INDEX)
        assert_frame_equal(result, expected, check_dtype=False)

        params = params_of(session.get.call_args)
        self.assertEqual(params["product"], "monthly_mean")
        self.assertEqual((params["begin_date"], params["end_date"]), ("20230301", "20230531"))

    def test_water_temperature_sends_no_datum(self):
        patcher, session = mock_session(response("noaa_tides_8518750_water_temperature_20240101.json"))
        with patcher:
            result = self.fetcher.get_data("8518750", constants.WATER_TEMPERATURE_INSTANT, "2024-01-01", "2024-01-01")
        self.assertEqual(len(result), 240)
        self.assertEqual(result.iloc[0, 0], 7.3)
        params = params_of(session.get.call_args)
        self.assertEqual(params["product"], "water_temperature")
        self.assertNotIn("datum", params)

    def test_great_lakes_station_uses_igld(self):
        patcher, session = mock_session(response("noaa_tides_9063063_water_level_20240101.json"))
        with patcher:
            result = self.fetcher.get_data("9063063", constants.STAGE_INSTANT, "2024-01-01", "2024-01-01")
        self.assertEqual(params_of(session.get.call_args)["datum"], "IGLD")
        self.assertEqual(result.attrs["datum"], "IGLD")
        # Lake Erie sits about 174 m above the IGLD 1985 zero.
        self.assertTrue(result[constants.STAGE_INSTANT].between(170, 180).all())

    def test_missing_default_datum_falls_back_to_station_datum(self):
        """Augusta on the tidal Kennebec River is charted without MLLW."""
        patcher, session = mock_session(
            response("noaa_tides_8417144_error_unsupported_datum.json", status=400),
            response("noaa_tides_8417144_water_level_stnd_20150622.json"),
        )
        with patcher, self.assertLogs("rivretrieve.noaa_tides", level="WARNING"):
            result = self.fetcher.get_data("8417144", constants.STAGE_INSTANT, "2015-06-22", "2015-06-22")

        self.assertEqual(len(result), 240)
        self.assertEqual(result.attrs["datum"], "STND")
        self.assertEqual([params_of(c)["datum"] for c in session.get.call_args_list], ["MLLW", "STND"])

    def test_the_other_missing_datum_wording_also_falls_back(self):
        patcher, session = mock_session(
            response("noaa_tides_1495000_error_no_mllw.json", status=400),
            response("noaa_tides_8518750_error_no_data_19500101.json"),
        )
        with patcher, self.assertLogs("rivretrieve.noaa_tides", level="WARNING"):
            self.fetcher.get_data("1495000", constants.STAGE_INSTANT, "1997-06-01", "1997-06-01")
        self.assertEqual([params_of(c)["datum"] for c in session.get.call_args_list], ["MLLW", "STND"])

    def test_an_explicit_datum_is_never_swapped(self):
        fetcher = NOAATidesFetcher(datum="mllw")
        patcher, session = mock_session(response("noaa_tides_1495000_error_no_mllw.json", status=400))
        with patcher, self.assertLogs("rivretrieve.noaa_tides", level="ERROR"):
            result = fetcher.get_data("1495000", constants.STAGE_INSTANT, "1997-06-01", "1997-06-01")
        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), [constants.STAGE_INSTANT])
        session.get.assert_called_once()

    def test_no_data_returns_an_empty_frame(self):
        patcher, _ = mock_session(response("noaa_tides_8518750_error_no_data_19500101.json"))
        with patcher:
            result = self.fetcher.get_data("8518750", constants.STAGE_DAILY_MEAN, "1950-01-01", "1950-01-01")
        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), [constants.STAGE_DAILY_MEAN])
        self.assertEqual(result.index.name, constants.TIME_INDEX)

    def test_long_ranges_are_split_into_31_day_requests(self):
        no_data = "noaa_tides_8518750_error_no_data_19500101.json"
        patcher, session = mock_session(response(no_data), response(no_data), response(no_data))
        with patcher:
            self.fetcher.get_data("8518750", constants.STAGE_INSTANT, "2024-01-01", "2024-03-05")
        windows = [(params_of(c)["begin_date"], params_of(c)["end_date"]) for c in session.get.call_args_list]
        self.assertEqual(
            windows,
            [("20240101", "20240131"), ("20240201", "20240302"), ("20240303", "20240305")],
        )

    def test_no_six_minute_request_before_the_record_starts(self):
        patcher, session = mock_session()
        with patcher:
            result = self.fetcher.get_data("8518750", constants.STAGE_INSTANT, "1980-01-01", "1980-12-31")
        self.assertTrue(result.empty)
        session.get.assert_not_called()

    def test_no_request_after_a_historic_station_was_removed(self):
        patcher, session = mock_session()
        with patcher:
            result = self.fetcher.get_data("1495000", constants.STAGE_DAILY_MEAN, "2020-01-01", "2020-12-31")
        self.assertTrue(result.empty)
        session.get.assert_not_called()

    def test_throttling_raises_instead_of_looking_empty(self):
        # NOAA's gateway body when it turns a client away; small enough to inline.
        patcher, _ = mock_session(response({"message": "Forbidden"}, status=403))
        with patcher, self.assertRaises(requests.exceptions.HTTPError):
            self.fetcher.get_data("8518750", constants.STAGE_INSTANT, "2024-01-01", "2024-01-01")

    def test_unsupported_variable_raises(self):
        with self.assertRaises(ValueError):
            self.fetcher.get_data("8518750", constants.DISCHARGE_DAILY_MEAN, "2024-01-01", "2024-01-02")

    def test_unknown_datum_raises(self):
        with self.assertRaises(ValueError):
            NOAATidesFetcher(datum="WGS84")


class TestNOAATidesPacing(unittest.TestCase):
    def test_requests_are_spaced_out(self):
        with patch.object(noaa_tides, "MIN_REQUEST_INTERVAL", 0.05):
            noaa_tides._pace()
            started = time.monotonic()
            noaa_tides._pace()
            noaa_tides._pace()
            self.assertGreaterEqual(time.monotonic() - started, 0.09)


if __name__ == "__main__":
    unittest.main()
