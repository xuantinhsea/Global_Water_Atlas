"""Tests for the Mekong River Commission fetcher.

Only the HTTP call is mocked; the payloads are real responses captured from the
MRC's public near-real-time feed.
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from rivretrieve import MRCFetcher, constants
from rivretrieve.mrc import MIN_READINGS_PER_DAY, WINDOW_DAYS

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
    return patch("rivretrieve.mrc.utils.requests_retry_session", return_value=session), session


class TestMRCMetadata(unittest.TestCase):
    def setUp(self):
        self.fetcher = MRCFetcher()

    def test_get_metadata_maps_to_standard_columns(self):
        patcher, _ = mock_session(load("mrc_stations_sample.json"))
        with patcher:
            frame = self.fetcher.get_metadata()

        self.assertIn("019803", frame.index)
        tan_chau = frame.loc["019803"]
        self.assertEqual(tan_chau[constants.STATION_NAME], "Tan Chau")
        self.assertEqual(tan_chau[constants.COUNTRY], "Viet Nam")
        self.assertEqual(tan_chau[constants.RIVER], "Mekong")
        self.assertAlmostEqual(float(tan_chau[constants.LATITUDE]), 10.80062, places=4)
        # Flood and alarm stages are the only sane reference for these levels.
        self.assertGreater(float(tan_chau["flood_stage"]), 0)

    def test_cached_metadata_spans_the_member_countries(self):
        frame = MRCFetcher.get_cached_metadata()
        countries = set(frame[constants.COUNTRY].dropna())
        for expected in ["Viet Nam", "Lao PDR", "Cambodia", "Thailand"]:
            self.assertIn(expected, countries)
        self.assertTrue(frame[constants.LATITUDE].notna().all())
        self.assertTrue(frame[constants.STATION_NAME].notna().all())

    def test_the_delta_gauges_are_present(self):
        """Tan Chau and My Thuan are the Mekong Delta gauges cited as hard to obtain."""
        frame = MRCFetcher.get_cached_metadata()
        names = set(frame[constants.STATION_NAME])
        self.assertIn("Tan Chau", names)
        self.assertIn("My Thuan", names)


class TestMRCData(unittest.TestCase):
    def setUp(self):
        self.fetcher = MRCFetcher()
        self.payload = load("mrc_measurement_019803.json")
        stamps = [m["d"][:10] for m in self.payload["measurements"]]
        self.start, self.end = min(stamps), max(stamps)

    def test_stage_instant_reads_the_w_column(self):
        patcher, _ = mock_session(self.payload)
        with patcher:
            result = self.fetcher.get_data("019803", constants.STAGE_INSTANT, self.start, self.end)

        expected = [m for m in self.payload["measurements"] if m.get("w") is not None]
        self.assertEqual(len(result), len(expected))
        self.assertEqual(list(result.columns), [constants.STAGE_INSTANT])
        self.assertEqual(result.index.name, constants.TIME_INDEX)
        # Tan Chau reads around 3 m against a 4.5 m flood stage, never hundreds.
        self.assertTrue((result[constants.STAGE_INSTANT] < 20).all())

    def test_rainfall_increments_are_summed_into_hours(self):
        patcher, _ = mock_session(self.payload)
        with patcher:
            result = self.fetcher.get_data(
                "019803", constants.PRECIPITATION_HOURLY_SUM, self.start, self.end
            )

        self.assertEqual(list(result.columns), [constants.PRECIPITATION_HOURLY_SUM])
        # Hourly buckets, so far fewer rows than the 5-15 minute readings.
        self.assertLess(len(result), len(self.payload["measurements"]))
        self.assertTrue((result[constants.PRECIPITATION_HOURLY_SUM] >= 0).all())
        # The hourly totals must add up to the same rain that went in.
        raw_total = sum(m["r"] for m in self.payload["measurements"] if m.get("r"))
        self.assertAlmostEqual(
            float(result[constants.PRECIPITATION_HOURLY_SUM].sum()), raw_total, places=4
        )

    def test_daily_mean_drops_partial_days(self):
        """The fixture's first and last days are partial and must not become means."""
        patcher, _ = mock_session(self.payload)
        with patcher:
            result = self.fetcher.get_data(
                "019803", constants.STAGE_DAILY_MEAN, self.start, self.end
            )

        counts = pd.Series(
            [m["d"][:10] for m in self.payload["measurements"] if m.get("w") is not None]
        ).value_counts()
        expected_days = {day for day, n in counts.items() if n >= MIN_READINGS_PER_DAY}
        self.assertEqual({str(d.date()) for d in result.index}, expected_days)

    def test_timestamps_are_naive_utc(self):
        patcher, _ = mock_session(self.payload)
        with patcher:
            result = self.fetcher.get_data("019803", constants.STAGE_INSTANT, self.start, self.end)
        self.assertIsNone(result.index.tz)
        first = self.payload["measurements"][0]["d"]
        self.assertTrue(first.endswith("Z"), "fixture should be UTC-stamped")

    def test_historical_request_returns_empty_without_calling_the_api(self):
        """The public feed has no history, so old ranges must not be answered."""
        patcher, session = mock_session(self.payload)
        with patcher:
            result = self.fetcher.get_data(
                "019803", constants.STAGE_INSTANT, "2015-01-01", "2015-01-31"
            )
        self.assertTrue(result.empty)
        session.get.assert_not_called()

    def test_window_is_declared_honestly(self):
        self.assertGreaterEqual(WINDOW_DAYS, 28)
        self.assertLessEqual(WINDOW_DAYS, 40)

    def test_unsupported_variable_raises(self):
        with self.assertRaises(ValueError):
            self.fetcher.get_data("019803", constants.DISCHARGE_DAILY_MEAN, "2026-09-01", "2026-09-02")

    def test_request_failure_returns_an_empty_frame(self):
        session = MagicMock()
        session.get.side_effect = RuntimeError("connection reset")
        with patch("rivretrieve.mrc.utils.requests_retry_session", return_value=session):
            result = self.fetcher.get_data(
                "019803", constants.STAGE_INSTANT, self.start, self.end
            )
        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), [constants.STAGE_INSTANT])


if __name__ == "__main__":
    unittest.main()
