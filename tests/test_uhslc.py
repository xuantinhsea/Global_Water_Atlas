"""Tests for the University of Hawaii Sea Level Center fetcher.

Only the HTTP call is mocked; the payloads are real responses captured from
UHSLC's station file and ERDDAP server, chosen to straddle the end of Manila's
research quality record so the two products overlap.
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from rivretrieve import UHSLCFetcher, constants

TEST_DATA_DIR = Path(os.path.dirname(__file__)) / "test_data"


def text(name):
    return (TEST_DATA_DIR / name).read_text(encoding="utf-8")


def response(body="", status=200, payload=None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def erddap_session(research, fast):
    def get(url, **_):
        if "rqds" in url:
            return research
        return fast

    session = MagicMock()
    session.get.side_effect = get
    return patch("rivretrieve.uhslc.utils.requests_retry_session", return_value=session), session


class TestUHSLCMetadata(unittest.TestCase):
    def setUp(self):
        session = MagicMock()
        session.get.return_value = response(payload=json.loads(text("uhslc_meta_sample.json")))
        with patch("rivretrieve.uhslc.utils.requests_retry_session", return_value=session):
            self.frame = UHSLCFetcher().get_metadata()

    def test_manila_is_mapped_with_both_products(self):
        manila = self.frame.loc["370"]
        self.assertEqual(manila[constants.STATION_NAME], "Manila")
        self.assertEqual(manila[constants.COUNTRY], "Philippines")
        self.assertTrue(manila["research_quality_to"] < manila["fast_delivery_to"])

    def test_east_longitudes_are_wrapped(self):
        """The station file gives some longitudes as 0-360 east."""
        self.assertTrue(self.frame[constants.LONGITUDE].between(-180, 180).all())
        self.assertAlmostEqual(float(self.frame.loc["3", constants.LONGITUDE]), 269.71447 - 360, places=4)

    def test_cached_list_covers_southeast_asia(self):
        frame = UHSLCFetcher.get_cached_metadata()
        for station in ["370", "383", "699", "148"]:  # Manila, Vung Tau, Tanjong Pagar, Ko Taphao Noi
            self.assertIn(station, frame.index)


class TestUHSLCData(unittest.TestCase):
    def setUp(self):
        self.fetcher = UHSLCFetcher()
        self.pace = patch("rivretrieve.uhslc._pace")
        self.pace.start()

    def tearDown(self):
        self.pace.stop()

    def test_research_quality_wins_and_fast_delivery_fills_after_it(self):
        patcher, _ = erddap_session(
            response(text("uhslc_370_global_hourly_rqds.csv")), response(text("uhslc_370_global_hourly_fast.csv"))
        )
        with patcher:
            result = self.fetcher.get_data("370", constants.STAGE_HOURLY_MEAN, "2024-12-31", "2025-01-01")

        research = pd.read_csv(TEST_DATA_DIR / "uhslc_370_global_hourly_rqds.csv", skiprows=[1])
        fast = pd.read_csv(TEST_DATA_DIR / "uhslc_370_global_hourly_fast.csv", skiprows=[1])
        self.assertEqual(result.attrs["sources"], ["research", "fast"])
        self.assertEqual(len(result), 48)
        # 31 Dec 2024 comes from the research quality record, in metres.
        stamp = pd.Timestamp(research["time"].iloc[5]).tz_localize(None)
        self.assertAlmostEqual(float(result.loc[stamp].iloc[0]), research["sea_level"].iloc[5] / 1000)
        # 1 Jan 2025 only exists in fast delivery.
        last = pd.Timestamp(fast["time"].iloc[-1]).tz_localize(None)
        self.assertAlmostEqual(float(result.loc[last].iloc[0]), fast["sea_level"].iloc[-1] / 1000)

    def test_no_matching_rows_is_an_empty_frame(self):
        """ERDDAP answers 'no rows' with HTTP 404, which is not an error here."""
        patcher, _ = erddap_session(response("", status=404), response("", status=404))
        with patcher:
            result = self.fetcher.get_data("370", constants.STAGE_HOURLY_MEAN, "2026-09-01", "2026-09-02")
        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), [constants.STAGE_HOURLY_MEAN])

    def test_unsupported_variable_raises(self):
        with self.assertRaises(ValueError):
            self.fetcher.get_data("370", constants.STAGE_INSTANT, "2024-01-01", "2024-01-02")


if __name__ == "__main__":
    unittest.main()
