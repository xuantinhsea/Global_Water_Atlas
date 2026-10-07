"""Tests for the GRDC station catalogue fetcher.

Only the HTTP call is mocked; the fixture rows are real records from the public
GRDC sample-records endpoint, trimmed to representative daily/monthly cases.
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rivretrieve import GRDCFetcher, constants
from rivretrieve.grdc import GRDC_SAMPLE_RECORDS_URL

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
    return patch("rivretrieve.grdc.utils.requests_retry_session", return_value=session), session


class TestGRDCMetadata(unittest.TestCase):
    def setUp(self):
        self.fetcher = GRDCFetcher()
        self.payload = load("grdc_sample_records_subset.json")

    def test_available_variables(self):
        self.assertEqual(
            GRDCFetcher.get_available_variables(),
            (constants.DISCHARGE_DAILY_MEAN, constants.DISCHARGE_MONTHLY_MEAN),
        )

    def test_parse_metadata_maps_core_columns_and_availability(self):
        frame = self.fetcher._parse_metadata(self.payload)

        # One malformed record without grdc_no is skipped.
        self.assertEqual(len(frame), 3)
        self.assertEqual(frame.index.name, constants.GAUGE_ID)

        both = frame.loc["1104150"]
        self.assertEqual(both[constants.STATION_NAME], "SIDI BELATAR")
        self.assertEqual(both[constants.COUNTRY], "Algeria")
        self.assertAlmostEqual(float(both[constants.LATITUDE]), 36.02, places=3)
        self.assertTrue(bool(both[constants.DISCHARGE_DAILY_MEAN]))
        self.assertTrue(bool(both[constants.DISCHARGE_MONTHLY_MEAN]))

        monthly_only = frame.loc["1104200"]
        self.assertFalse(bool(monthly_only[constants.DISCHARGE_DAILY_MEAN]))
        self.assertTrue(bool(monthly_only[constants.DISCHARGE_MONTHLY_MEAN]))

        daily_only = frame.loc["1357600"]
        self.assertTrue(bool(daily_only[constants.DISCHARGE_DAILY_MEAN]))
        self.assertFalse(bool(daily_only[constants.DISCHARGE_MONTHLY_MEAN]))

    def test_get_metadata_reads_the_public_endpoint(self):
        patcher, session = mock_session(self.payload)
        with patcher:
            frame = self.fetcher.get_metadata()

        session.get.assert_called_once_with(GRDC_SAMPLE_RECORDS_URL, timeout=120)
        self.assertIn("1104150", frame.index)
        self.assertIn(constants.DISCHARGE_DAILY_MEAN, frame.columns)

    def test_cached_metadata_is_shipped(self):
        frame = GRDCFetcher.get_cached_metadata()
        self.assertGreater(len(frame), 11000)
        self.assertIn(constants.LATITUDE, frame.columns)
        self.assertIn(constants.DISCHARGE_DAILY_MEAN, frame.columns)


class TestGRDCData(unittest.TestCase):
    def setUp(self):
        self.fetcher = GRDCFetcher()

    def test_get_data_returns_empty_frame_and_warns(self):
        with self.assertLogs("rivretrieve.grdc", level="WARNING") as logged:
            frame = self.fetcher.get_data(
                "1104150", constants.DISCHARGE_DAILY_MEAN, "2000-01-01", "2000-12-31"
            )
        self.assertTrue(frame.empty)
        self.assertEqual(list(frame.columns), [constants.DISCHARGE_DAILY_MEAN])
        self.assertIn("interactive export workflow", " ".join(logged.output))

    def test_unsupported_variable_raises(self):
        with self.assertRaises(ValueError):
            self.fetcher.get_data("1104150", constants.STAGE_DAILY_MEAN, "2000-01-01", "2000-01-31")


if __name__ == "__main__":
    unittest.main()
