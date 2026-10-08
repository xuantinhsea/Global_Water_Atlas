"""Tests for the Singapore NEA rain gauge fetcher.

Only the HTTP call is mocked; the payload is a real day from data.gov.sg
(1 October 2026), trimmed to its wettest gauge (S44), one that reported all day
and stayed dry (S08), and one that sent barely half its readings (S224).
"""

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rivretrieve import SingaporeRainFetcher, constants, singapore
from rivretrieve.singapore import ARCHIVE_START, MAX_DAYS, MIN_READINGS_PER_DAY

TEST_DATA_DIR = Path(os.path.dirname(__file__)) / "test_data"
DAY = "2026-10-01"
WET, DRY, GAPPY = "S44", "S08", "S224"


def load():
    with open(TEST_DATA_DIR / "singapore_rainfall_v1_20261001.json", encoding="utf-8") as handle:
        return json.load(handle)


def mock_session(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    session = MagicMock()
    session.get.return_value = response
    return patch("rivretrieve.singapore.utils.requests_retry_session", return_value=session), session


class TestSingaporeRain(unittest.TestCase):
    def setUp(self):
        self.fetcher = SingaporeRainFetcher()
        self.payload = load()
        self.cache = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(singapore, "CACHE_DIR", Path(self.cache.name)),
            patch.object(singapore, "_pace"),
            patch.object(singapore, "_singapore_today", return_value=datetime.date(2026, 10, 4)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.cache.cleanup()

    def readings(self, station):
        return [
            (item["timestamp"], r["value"])
            for item in self.payload["items"]
            for r in item["readings"]
            if r["station_id"] == station
        ]

    def test_daily_total_matches_the_five_minute_readings(self):
        patcher, _ = mock_session(self.payload)
        with patcher:
            result = self.fetcher.get_data(WET, constants.PRECIPITATION_DAILY_SUM, DAY, DAY)
        total = sum(v for _, v in self.readings(WET))
        self.assertGreater(len(self.readings(WET)), MIN_READINGS_PER_DAY)
        self.assertAlmostEqual(float(result.iloc[0, 0]), round(total, 2), places=2)

    def test_a_dry_gauge_reports_zero_not_nothing(self):
        patcher, _ = mock_session(self.payload)
        with patcher:
            result = self.fetcher.get_data(DRY, constants.PRECIPITATION_DAILY_SUM, DAY, DAY)
        self.assertEqual(float(result.iloc[0, 0]), 0.0)

    def test_hourly_totals_add_up_and_use_singapore_time(self):
        patcher, _ = mock_session(self.payload)
        with patcher:
            result = self.fetcher.get_data(WET, constants.PRECIPITATION_HOURLY_SUM, DAY, DAY)
        self.assertTrue(all(stamp.strftime("%Y-%m-%d") == DAY for stamp in result.index))
        self.assertLessEqual(float(result.iloc[:, 0].sum()), sum(v for _, v in self.readings(WET)) + 1e-6)
        self.assertGreaterEqual(len(result), 23)

    def test_a_day_with_missing_readings_gets_no_total(self):
        """S224 sent 155 of 288 readings that day; a total from them would read as a dry spell."""
        self.assertLess(len(self.readings(GAPPY)), MIN_READINGS_PER_DAY)
        patcher, _ = mock_session(self.payload)
        with patcher:
            result = self.fetcher.get_data(GAPPY, constants.PRECIPITATION_DAILY_SUM, DAY, DAY)
        self.assertTrue(result.empty)

    def test_a_downloaded_day_is_reused_for_other_gauges(self):
        patcher, session = mock_session(self.payload)
        with patcher:
            self.fetcher.get_data(WET, constants.PRECIPITATION_DAILY_SUM, DAY, DAY)
            self.fetcher.get_data(DRY, constants.PRECIPITATION_DAILY_SUM, DAY, DAY)
        self.assertEqual(session.get.call_count, 1)

    def test_long_ranges_are_capped_and_start_at_the_archive(self):
        days = []
        def no_rain(day):
            days.append(day)
            return {"stations": [], "readings": []}

        with patch.object(SingaporeRainFetcher, "_day", side_effect=no_rain):
            self.fetcher.get_data(WET, constants.PRECIPITATION_DAILY_SUM, "2010-01-01", "2017-01-05")
            self.assertEqual(days[0].isoformat(), ARCHIVE_START)
            days.clear()
            self.fetcher.get_data(WET, constants.PRECIPITATION_DAILY_SUM, "2016-12-01", "2026-10-03")
            self.assertEqual(len(days), MAX_DAYS)

    def test_metadata_has_coordinates(self):
        frame = SingaporeRainFetcher.get_cached_metadata()
        self.assertGreater(len(frame), 50)
        self.assertTrue(frame[constants.LATITUDE].between(1.1, 1.5).all())
        self.assertTrue(frame[constants.LONGITUDE].between(103.5, 104.2).all())

    def test_unsupported_variable_raises(self):
        with self.assertRaises(ValueError):
            self.fetcher.get_data(WET, constants.STAGE_DAILY_MEAN, DAY, DAY)


if __name__ == "__main__":
    unittest.main()
