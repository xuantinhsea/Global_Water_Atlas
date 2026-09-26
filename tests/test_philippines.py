"""Tests for the DOST-ASTI PhilSensors fetcher.

PhilSensors is the atlas's clearest example of a provider whose station
catalogue is open while its readings are not, so most of what matters here is
that the metadata works without credentials and that a missing token is
reported rather than swallowed.
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rivretrieve import PhilippinesFetcher, constants
from rivretrieve.philippines import STATION_GROUPS

TEST_DATA_DIR = Path(os.path.dirname(__file__)) / "test_data"


def load(name):
    with open(TEST_DATA_DIR / name, encoding="utf-8") as handle:
        return json.load(handle)


class TestPhilippinesMetadata(unittest.TestCase):
    def setUp(self):
        self.fetcher = PhilippinesFetcher(access_token=None)

    def test_metadata_is_gathered_from_every_station_group(self):
        payloads = {group: load(f"philsensors_wfs_{group}.json") for group in STATION_GROUPS}

        def fake_get(url, params=None, **kwargs):
            group = (params or {}).get("typeName", "").split(":")[-1]
            response = MagicMock()
            response.json.return_value = payloads[group]
            response.raise_for_status.return_value = None
            return response

        session = MagicMock()
        session.get.side_effect = fake_get
        with patch("rivretrieve.philippines.utils.requests_retry_session", return_value=session):
            frame = self.fetcher.get_metadata()

        self.assertEqual(session.get.call_count, len(STATION_GROUPS))
        self.assertEqual(set(frame["station_group"]), set(STATION_GROUPS))
        self.assertTrue(frame[constants.LATITUDE].notna().all())
        self.assertTrue((frame[constants.COUNTRY] == "Philippines").all())

    def test_metadata_needs_no_credentials(self):
        """The whole network can be mapped without a token; only readings are gated."""
        self.assertIsNone(self.fetcher.access_token)
        payload = load("philsensors_wfs_wlms.json")
        response = MagicMock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        session = MagicMock()
        session.get.return_value = response
        with patch("rivretrieve.philippines.utils.requests_retry_session", return_value=session):
            frame = self.fetcher.get_metadata()
        self.assertFalse(frame.empty)
        # No token was sent anywhere.
        for call in session.get.call_args_list:
            self.assertNotIn("access-token", (call.kwargs.get("params") or {}))

    def test_per_station_availability_follows_the_group(self):
        frame = PhilippinesFetcher.get_cached_metadata()
        for group, variables in STATION_GROUPS.items():
            rows = frame[frame["station_group"] == group]
            if rows.empty:
                continue
            with self.subTest(group=group):
                self.assertTrue(
                    rows[constants.STAGE_INSTANT].all()
                    == (constants.STAGE_INSTANT in variables)
                )
                self.assertTrue(
                    rows[constants.PRECIPITATION_HOURLY_SUM].all()
                    == (constants.PRECIPITATION_HOURLY_SUM in variables)
                )

    def test_cached_metadata_covers_the_whole_network(self):
        frame = PhilippinesFetcher.get_cached_metadata()
        self.assertGreater(len(frame), 2000)
        self.assertTrue(frame[constants.LATITUDE].notna().all())
        self.assertTrue(frame[constants.STATION_NAME].notna().all())
        # The Philippines spans roughly 4-21 N. The western bound reaches 114 E
        # because a weather station sits on Pag-asa Island in the Kalayaan group,
        # administered as part of Palawan — that coordinate is correct, not an outlier.
        self.assertTrue(frame[constants.LATITUDE].between(4, 22).all())
        self.assertTrue(frame[constants.LONGITUDE].between(114, 127).all())

    def test_station_ids_are_unique_across_groups(self):
        """Why one fetcher serves all four groups: the id spaces do not collide."""
        frame = PhilippinesFetcher.get_cached_metadata()
        self.assertEqual(len(frame.index), len(set(frame.index)))


class TestPhilippinesData(unittest.TestCase):
    def test_no_token_returns_empty_and_never_calls_the_api(self):
        fetcher = PhilippinesFetcher(access_token=None)
        session = MagicMock()
        with patch("rivretrieve.philippines.utils.requests_retry_session", return_value=session):
            result = fetcher.get_data("212", constants.STAGE_INSTANT, "2024-01-01", "2024-01-31")
        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), [constants.STAGE_INSTANT])
        session.get.assert_not_called()

    def test_token_is_sent_when_one_is_configured(self):
        fetcher = PhilippinesFetcher(access_token="test-token")
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "data": [
                {"dateTimeRead": "2024-01-01 00:00:00", "value": 1.25},
                {"dateTimeRead": "2024-01-01 01:00:00", "value": 1.31},
            ]
        }
        response.raise_for_status.return_value = None
        session = MagicMock()
        session.get.return_value = response

        with patch("rivretrieve.philippines.utils.requests_retry_session", return_value=session):
            result = fetcher.get_data("212", constants.STAGE_INSTANT, "2024-01-01", "2024-01-02")

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["access-token"], "test-token")
        self.assertEqual(params["paramNames[0]"], "Water Level")
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(float(result.iloc[0][constants.STAGE_INSTANT]), 1.25)

    def test_a_revoked_token_returns_empty_rather_than_raising(self):
        fetcher = PhilippinesFetcher(access_token="stale-token")
        response = MagicMock()
        response.status_code = 401
        session = MagicMock()
        session.get.return_value = response
        with patch("rivretrieve.philippines.utils.requests_retry_session", return_value=session):
            result = fetcher.get_data("212", constants.STAGE_INSTANT, "2024-01-01", "2024-01-02")
        self.assertTrue(result.empty)

    def test_unsupported_variable_raises(self):
        fetcher = PhilippinesFetcher(access_token="test-token")
        with self.assertRaises(ValueError):
            fetcher.get_data("212", constants.DISCHARGE_DAILY_MEAN, "2024-01-01", "2024-01-02")


if __name__ == "__main__":
    unittest.main()
