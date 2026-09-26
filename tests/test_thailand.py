"""Tests for the Thai fetchers.

Only the HTTP call to ThaiWater is mocked. Every payload below is a real
response captured from the provider, including the null-padded rows it returns
for periods its telemetry never covered.
"""

import datetime
import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from rivretrieve import ThailandFetcher, ThailandRainFetcher, constants
from rivretrieve.thailand import (
    ARCHIVE_START,
    CHUNK_DAYS,
    MIN_HOURS_PER_DAY,
    _covers,
    _date_ranges,
    _localised,
)

TEST_DATA_DIR = Path(os.path.dirname(__file__)) / "test_data"


def load(name):
    with open(TEST_DATA_DIR / name, encoding="utf-8") as handle:
        return json.load(handle)


def mock_session(payload):
    """Patches utils.requests_retry_session to return `payload` for any GET."""
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    session = MagicMock()
    session.get.return_value = response
    return patch("rivretrieve.thailand.utils.requests_retry_session", return_value=session), session


class TestHelpers(unittest.TestCase):
    def test_localised_prefers_english_but_falls_back_to_thai(self):
        self.assertEqual(_localised({"en": "Mun Basin", "th": "ลุ่มน้ำมูล"}), "Mun Basin")
        # Only 424 of 1,119 water level stations carry an English name.
        self.assertEqual(_localised({"en": "", "th": "สะพานเสรีประชาธิปไตย"}), "สะพานเสรีประชาธิปไตย")
        self.assertIsNone(_localised({"en": "", "th": ""}))
        self.assertIsNone(_localised(None))

    def test_date_ranges_are_contiguous_and_within_the_provider_cap(self):
        chunks = _date_ranges("2020-01-01", "2026-09-15")
        self.assertGreater(len(chunks), 1)
        # Contiguous: each chunk starts the day after the previous one ended.
        for (_, previous_end), (next_start, _) in zip(chunks, chunks[1:]):
            self.assertEqual(
                datetime.date.fromisoformat(next_start) - datetime.date.fromisoformat(previous_end),
                datetime.timedelta(days=1),
            )
        # The provider silently truncates anything past ~366 days.
        for chunk_start, chunk_end in chunks:
            span = (
                datetime.date.fromisoformat(chunk_end) - datetime.date.fromisoformat(chunk_start)
            ).days + 1
            self.assertLessEqual(span, CHUNK_DAYS)
            self.assertLess(span, 366)
        self.assertEqual(chunks[0][0], "2020-01-01")
        self.assertEqual(chunks[-1][1], "2026-09-15")

    def test_date_ranges_handles_short_and_reversed_ranges(self):
        self.assertEqual(_date_ranges("2024-06-01", "2024-06-30"), [("2024-06-01", "2024-06-30")])
        self.assertEqual(_date_ranges("2024-06-10", "2024-06-01"), [])

    def test_covers_detects_a_silently_truncated_response(self):
        """A three-year request comes back looking healthy with two years missing."""
        full = [{"datetime": "2024-01-01 00:00"}, {"datetime": "2024-06-30 23:00"}]
        self.assertTrue(_covers(full, "datetime", "2024-01-01"))
        truncated = [{"datetime": "2025-01-01 00:00"}, {"datetime": "2025-12-31 23:00"}]
        self.assertFalse(_covers(truncated, "datetime", "2023-01-01"))
        # An empty chunk is a genuine gap, not a truncation.
        self.assertTrue(_covers([], "datetime", "2023-01-01"))


class TestThailandFetcher(unittest.TestCase):
    def setUp(self):
        self.fetcher = ThailandFetcher()

    def test_get_data_discharge_hourly(self):
        payload = load("thailand_waterlevel_graph_2752_202506.json")
        patcher, session = mock_session(payload)
        with patcher:
            result = self.fetcher.get_data(
                "2752", constants.DISCHARGE_HOURLY_MEAN, "2025-06-01", "2025-06-03"
            )

        expected_rows = [
            point
            for point in payload["data"]["graph_data"]
            if point.get("discharge") is not None
        ]
        self.assertEqual(len(result), len(expected_rows))
        self.assertEqual(list(result.columns), [constants.DISCHARGE_HOURLY_MEAN])
        self.assertEqual(result.index.name, constants.TIME_INDEX)
        self.assertAlmostEqual(
            float(result.iloc[0][constants.DISCHARGE_HOURLY_MEAN]),
            float(expected_rows[0]["discharge"]),
        )

        # station_type is mandatory: without it the provider returns HTTP 500.
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"]["station_type"], "tele_waterlevel")
        self.assertEqual(kwargs["params"]["station_id"], "2752")
        self.assertEqual(kwargs["params"]["start_date"], "2025-06-01")

    def test_stage_reads_the_value_column_not_discharge(self):
        payload = load("thailand_waterlevel_graph_2752_202506.json")
        patcher, _ = mock_session(payload)
        with patcher:
            stage = self.fetcher.get_data(
                "2752", constants.STAGE_HOURLY_MEAN, "2025-06-01", "2025-06-03"
            )
            discharge = self.fetcher.get_data(
                "2752", constants.DISCHARGE_HOURLY_MEAN, "2025-06-01", "2025-06-03"
            )

        first = next(p for p in payload["data"]["graph_data"] if p.get("value") is not None)
        self.assertAlmostEqual(float(stage.iloc[0][constants.STAGE_HOURLY_MEAN]), float(first["value"]))
        # Water level is ~110 m MSL, discharge ~1500 m³/s: never the same column.
        self.assertNotAlmostEqual(
            float(stage.iloc[0][constants.STAGE_HOURLY_MEAN]),
            float(discharge.iloc[0][constants.DISCHARGE_HOURLY_MEAN]),
        )

    def test_null_padded_months_return_empty_not_thousands_of_rows(self):
        """A period the telemetry never covered still returns a full hourly grid."""
        payload = load("thailand_waterlevel_graph_2752_empty.json")
        self.assertEqual(len(payload["data"]["graph_data"]), 120)
        self.assertTrue(all(p["value"] is None for p in payload["data"]["graph_data"]))

        patcher, _ = mock_session(payload)
        with patcher:
            result = self.fetcher.get_data(
                "2752", constants.DISCHARGE_DAILY_MEAN, "2012-06-01", "2012-06-05"
            )
        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), [constants.DISCHARGE_DAILY_MEAN])

    def test_daily_mean_drops_days_with_too_few_hours(self):
        """A 'daily mean' from three readings would be a number nobody measured."""
        dense_day = [
            {"datetime": f"2025-06-01 {hour:02d}:00", "value": 100.0 + hour, "discharge": 10.0 * hour}
            for hour in range(24)
        ]
        thin_day = [
            {"datetime": f"2025-06-02 {hour:02d}:00", "value": 200.0, "discharge": 500.0}
            for hour in range(MIN_HOURS_PER_DAY - 1)
        ]
        payload = {"result": "OK", "data": {"graph_data": dense_day + thin_day}}

        patcher, _ = mock_session(payload)
        with patcher:
            result = self.fetcher.get_data(
                "2752", constants.DISCHARGE_DAILY_MEAN, "2025-06-01", "2025-06-02"
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result.index[0], pd.Timestamp("2025-06-01"))
        expected_mean = sum(10.0 * hour for hour in range(24)) / 24
        self.assertAlmostEqual(float(result.iloc[0][constants.DISCHARGE_DAILY_MEAN]), expected_mean)

    def test_a_wide_range_is_chunked_rather_than_truncated(self):
        payload = {"result": "OK", "data": {"graph_data": []}}
        patcher, session = mock_session(payload)
        with patcher:
            self.fetcher.get_data("2752", constants.STAGE_HOURLY_MEAN, "2023-01-01", "2025-12-31")
        # Three years in one call would silently come back as one.
        self.assertEqual(session.get.call_count, len(_date_ranges("2023-01-01", "2025-12-31")))
        self.assertGreater(session.get.call_count, 3)

    def test_requests_before_the_archive_starts_are_not_sent(self):
        """The library's 1900 default would be hundreds of round trips returning padding."""
        payload = {"result": "OK", "data": {"graph_data": []}}
        patcher, session = mock_session(payload)
        with patcher:
            self.fetcher.get_data("2752", constants.STAGE_HOURLY_MEAN, "1950-01-01", "2020-03-31")
        self.assertEqual(session.get.call_count, 1)
        first_call = session.get.call_args_list[0][1]["params"]
        self.assertEqual(first_call["start_date"], ARCHIVE_START)

    def test_unsupported_variable_raises(self):
        with self.assertRaises(ValueError):
            self.fetcher.get_data("2752", constants.WATER_TEMPERATURE_DAILY_MEAN, "2025-06-01", "2025-06-02")

    def test_request_failure_returns_an_empty_frame(self):
        session = MagicMock()
        session.get.side_effect = RuntimeError("connection reset")
        with patch("rivretrieve.thailand.utils.requests_retry_session", return_value=session):
            result = self.fetcher.get_data(
                "2752", constants.DISCHARGE_DAILY_MEAN, "2025-06-01", "2025-06-02"
            )
        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), [constants.DISCHARGE_DAILY_MEAN])

    def test_get_metadata_maps_to_standard_columns(self):
        payload = load("thailand_waterlevel_metadata_sample.json")
        patcher, _ = mock_session(payload)
        with patcher:
            frame = self.fetcher.get_metadata()

        self.assertIn("2752", frame.index)
        row = frame.loc["2752"]
        self.assertEqual(row["station_code"], "M.7")
        self.assertEqual(row["agency"], "RID")
        self.assertEqual(row[constants.COUNTRY], "Thailand")
        # M.7 sits on the Mun at Ubon Ratchathani, north-east Thailand.
        self.assertAlmostEqual(float(row[constants.LATITUDE]), 15.22263, places=4)
        self.assertAlmostEqual(float(row[constants.LONGITUDE]), 104.859131, places=4)

    def test_cached_metadata_is_complete(self):
        frame = ThailandFetcher.get_cached_metadata()
        self.assertGreater(len(frame), 1000)
        self.assertTrue(frame[constants.LATITUDE].notna().all())
        self.assertTrue(frame[constants.LONGITUDE].notna().all())
        self.assertTrue(frame[constants.STATION_NAME].notna().all())
        # Thailand spans roughly 5-21 N, 97-106 E.
        self.assertTrue(frame[constants.LATITUDE].between(5, 21).all())
        self.assertTrue(frame[constants.LONGITUDE].between(97, 106).all())


class TestThailandRainFetcher(unittest.TestCase):
    def setUp(self):
        self.fetcher = ThailandRainFetcher()

    def test_get_data_returns_recent_hourly_rainfall(self):
        payload = load("thailand_rain_graph_2005.json")
        stamps = [p["rainfall_datetime"] for p in payload["data"]]
        start, end = stamps[0][:10], stamps[-1][:10]

        patcher, session = mock_session(payload)
        with patcher:
            result = self.fetcher.get_data("2005", constants.PRECIPITATION_HOURLY_SUM, start, end)

        self.assertFalse(result.empty)
        self.assertEqual(list(result.columns), [constants.PRECIPITATION_HOURLY_SUM])
        self.assertTrue((result[constants.PRECIPITATION_HOURLY_SUM] >= 0).all())
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"]["station_type"], "rainfall_24h")

    def test_historical_request_returns_empty_without_calling_the_api(self):
        """The provider ignores date parameters, so history must not be faked."""
        patcher, session = mock_session(load("thailand_rain_graph_2005.json"))
        with patcher:
            result = self.fetcher.get_data(
                "2005", constants.PRECIPITATION_HOURLY_SUM, "2024-06-01", "2024-06-30"
            )
        self.assertTrue(result.empty)
        # Nothing is requested for a period the provider cannot serve.
        session.get.assert_not_called()

    def test_only_point_precipitation_is_offered(self):
        variables = ThailandRainFetcher.get_available_variables()
        self.assertEqual(variables, (constants.PRECIPITATION_HOURLY_SUM,))
        # Gauge rainfall is not a catchment average, and must not borrow its name.
        self.assertNotIn(constants.CATCHMENT_PRECIPITATION_DAILY_SUM, variables)

    def test_get_metadata_maps_to_standard_columns(self):
        payload = load("thailand_rain_metadata_sample.json")
        patcher, _ = mock_session(payload)
        with patcher:
            frame = self.fetcher.get_metadata()
        self.assertIn("2005", frame.index)
        self.assertEqual(frame.loc["2005"][constants.COUNTRY], "Thailand")
        self.assertTrue(frame[constants.LATITUDE].notna().all())

    def test_cached_metadata_is_complete(self):
        frame = ThailandRainFetcher.get_cached_metadata()
        self.assertGreater(len(frame), 4000)
        self.assertTrue(frame[constants.LATITUDE].notna().all())
        self.assertTrue(frame[constants.STATION_NAME].notna().all())

    def test_a_station_literally_named_nan_survives_the_csv_round_trip(self):
        """TMD's gauge in Nan province is romanised "NaN" by the provider.

        Thai น่าน is Nan, and the provider's own province field says "Nan", so
        this is an upstream romanisation slip — but it is the station's name,
        and pandas' default CSV parsing would turn it into a missing value.
        """
        frame = ThailandRainFetcher.get_cached_metadata()
        self.assertEqual(frame.loc["3647"][constants.STATION_NAME], "NaN")
        self.assertEqual(frame.loc["3647"]["province"], "Nan")

    def test_the_two_networks_have_overlapping_ids(self):
        """Why these are two fetchers and not one: 475 ids appear in both."""
        water = set(ThailandFetcher.get_cached_metadata().index)
        rain = set(ThailandRainFetcher.get_cached_metadata().index)
        self.assertTrue(water & rain, "expected the id spaces to collide")


if __name__ == "__main__":
    unittest.main()
