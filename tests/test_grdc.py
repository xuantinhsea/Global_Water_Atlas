"""Tests for the GRDC station catalogue fetcher.

Only the HTTP call is mocked; the fixture rows are real records from the public
GRDC sample-records endpoint, trimmed to representative daily/monthly cases.
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

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


def _grdc_station_in(country: str) -> str:
    """A GRDC station in ``country`` that a national service publishes too."""
    sites = pd.read_csv(TEST_DATA_DIR.parent.parent / "rivretrieve" / "cached_site_data" / "grdc_sites.csv", dtype=str)
    for gauge_id in sites.loc[sites["country"] == country, "gauge_id"]:
        if GRDCFetcher.national_source(gauge_id):
            return gauge_id
    raise AssertionError(f"No GRDC station in {country} has a national source")


class TestGRDCData(unittest.TestCase):
    """GRDC series come from the national service that runs each station."""

    def setUp(self):
        self.fetcher = GRDCFetcher()

    def test_station_without_national_source_returns_empty_and_says_why(self):
        # 1104150 is in Algeria, which no RivRetrieve fetcher covers.
        self.assertIsNone(GRDCFetcher.national_source("1104150"))
        self.assertIn("GRDC Data Portal", GRDCFetcher.unavailable_reason("1104150"))
        with self.assertLogs("rivretrieve.grdc", level="WARNING") as logged:
            frame = self.fetcher.get_data("1104150", constants.DISCHARGE_DAILY_MEAN, "2000-01-01", "2000-12-31")
        self.assertTrue(frame.empty)
        self.assertEqual(list(frame.columns), [constants.DISCHARGE_DAILY_MEAN])
        self.assertIn("GRDC Data Portal", " ".join(logged.output))

    def test_national_ids_are_matched_against_the_national_catalogue(self):
        # GRDC 4101200 is USGS 15747000; the leading zeros of USGS IDs survive.
        self.assertEqual(GRDCFetcher.national_source("4101200"), ("USAFetcher", "15747000"))
        self.assertIsNone(GRDCFetcher.unavailable_reason("4101200"))
        self.assertEqual(GRDCFetcher.national_source(_grdc_station_in("Canada"))[0], "CanadaFetcher")

    def test_daily_series_comes_from_the_national_fetcher(self):
        daily = pd.DataFrame(
            {constants.DISCHARGE_DAILY_MEAN: [1.0, 2.0, 3.0]},
            index=pd.DatetimeIndex(["2020-01-01", "2020-01-02", "2020-01-03"], name=constants.TIME_INDEX),
        )
        with patch("rivretrieve.USAFetcher.get_data", return_value=daily) as national:
            frame = self.fetcher.get_data("4101200", constants.DISCHARGE_DAILY_MEAN, "2020-01-01", "2020-01-03")

        national.assert_called_once_with(
            gauge_id="15747000",
            variable=constants.DISCHARGE_DAILY_MEAN,
            start_date="2020-01-01",
            end_date="2020-01-03",
        )
        self.assertEqual(list(frame[constants.DISCHARGE_DAILY_MEAN]), [1.0, 2.0, 3.0])
        self.assertEqual(frame.attrs["national_source"], {"fetcher": "USAFetcher", "gauge_id": "15747000"})

    def test_monthly_means_need_most_of_the_month(self):
        days = pd.date_range("2020-01-01", "2020-02-29", freq="D")
        values = pd.Series(1.0, index=days)
        values["2020-01-16":] = 3.0
        values = values.drop(pd.date_range("2020-02-01", "2020-02-10"))  # February is a third empty.
        daily = values.to_frame(constants.DISCHARGE_DAILY_MEAN)
        with patch("rivretrieve.USAFetcher.get_data", return_value=daily):
            frame = self.fetcher.get_data("4101200", constants.DISCHARGE_MONTHLY_MEAN, "2020-01-01", "2020-02-29")

        self.assertEqual(list(frame.index.strftime("%Y-%m")), ["2020-01"])
        self.assertAlmostEqual(frame[constants.DISCHARGE_MONTHLY_MEAN].iloc[0], (15 * 1.0 + 16 * 3.0) / 31)

    def test_bulk_cache_fetchers_are_read_one_station_at_a_time(self):
        station = _grdc_station_in("Canada")
        with patch("rivretrieve.CanadaFetcher.get_data", return_value=pd.DataFrame()):
            self.fetcher.get_data(station, constants.DISCHARGE_DAILY_MEAN, "2020-01-01", "2020-01-31")
        self.assertEqual(self.fetcher._national_fetchers["CanadaFetcher"].source, "api")

    def test_national_fetcher_that_needs_a_key_is_reported(self):
        station = _grdc_station_in("Norway")
        with patch.dict(os.environ, {"NVE_API_KEY": ""}):
            self.assertIn("NVE_API_KEY", GRDCFetcher.unavailable_reason(station))

    def test_unsupported_variable_raises(self):
        with self.assertRaises(ValueError):
            self.fetcher.get_data("1104150", constants.STAGE_DAILY_MEAN, "2000-01-01", "2000-01-31")


if __name__ == "__main__":
    unittest.main()
