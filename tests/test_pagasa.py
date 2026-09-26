"""Tests for the PAGASA fetchers.

The bulletin fixture is a real capture of https://pagasa.dost.gov.ph/flood, so
the awkward parts of it — a two-row header, each observation split across a time
row and a date row, a year that is never written down — are exercised exactly as
they appear in production.
"""

import datetime
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from rivretrieve import PagasaDamFetcher, PagasaStationFetcher, constants
from rivretrieve.pagasa import (
    DAMS,
    WINDOW_DAYS,
    _parse_bulletin_date,
    _parse_time,
    _reference_level,
    _slug,
)

TEST_DATA_DIR = Path(os.path.dirname(__file__)) / "test_data"
FIXTURE = TEST_DATA_DIR / "pagasa_flood_page.html"

# The captured bulletin carries these two dates.
CAPTURED_TODAY = datetime.date(2026, 9, 15)


def mock_page():
    html = FIXTURE.read_text(encoding="utf-8")
    response = MagicMock()
    response.text = html
    response.raise_for_status.return_value = None
    session = MagicMock()
    session.get.return_value = response
    return patch("rivretrieve.pagasa.utils.requests_retry_session", return_value=session), session


class TestHelpers(unittest.TestCase):
    def test_slug_maps_bulletin_spellings(self):
        self.assertEqual(_slug("Angat"), "angat")
        self.assertEqual(_slug("Magat Dam"), "magat")
        self.assertEqual(_slug("La Mesa"), "la_mesa")
        self.assertEqual(_slug("San Roque"), "san_roque")
        # The repeated header row must not be read as a dam.
        self.assertIsNone(_slug("Dam Name"))
        self.assertIsNone(_slug(float("nan")))

    def test_parse_time_handles_either_case(self):
        self.assertEqual(_parse_time("08:00 AM"), datetime.time(8, 0))
        self.assertEqual(_parse_time("08:00 am"), datetime.time(8, 0))
        self.assertEqual(_parse_time("12:00 AM"), datetime.time(0, 0))
        self.assertEqual(_parse_time("01:30 PM"), datetime.time(13, 30))
        self.assertIsNone(_parse_time("Sep-15"))

    def test_bulletin_date_infers_the_year(self):
        today = datetime.date(2026, 9, 15)
        self.assertEqual(_parse_bulletin_date("Sep-15", today), datetime.date(2026, 9, 15))
        self.assertEqual(_parse_bulletin_date("Sep-14", today), datetime.date(2026, 9, 14))

    def test_bulletin_date_rolls_back_across_new_year(self):
        """On 1 January, yesterday's 'Dec-31' belongs to the previous year."""
        today = datetime.date(2026, 1, 1)
        self.assertEqual(_parse_bulletin_date("Dec-31", today), datetime.date(2025, 12, 31))
        self.assertEqual(_parse_bulletin_date("Jan-01", today), datetime.date(2026, 1, 1))

    def test_zero_reference_level_means_absent(self):
        """Caliraya and Ipo are published with 0.00 where no level is defined."""
        self.assertIsNone(_reference_level(0.0))
        self.assertIsNone(_reference_level("0.00"))
        self.assertIsNone(_reference_level("-"))
        self.assertEqual(_reference_level("210.00"), 210.0)


class TestPagasaData(unittest.TestCase):
    def setUp(self):
        self.fetcher = PagasaDamFetcher()
        self.start = str(CAPTURED_TODAY - datetime.timedelta(days=1))
        self.end = str(CAPTURED_TODAY)

    def _get(self, dam, variable):
        # The bulletin never writes the year, so the fixture only resolves
        # correctly against the day it was captured.
        patcher, _ = mock_page()
        with patcher, patch("rivretrieve.pagasa._today", return_value=CAPTURED_TODAY):
            return self.fetcher.get_data(dam, variable, self.start, self.end)

    def test_every_dam_returns_two_days_of_levels(self):
        for dam in DAMS:
            with self.subTest(dam=dam):
                result = self._get(dam, constants.STAGE_INSTANT)
                self.assertEqual(len(result), 2, f"{dam} should have today and yesterday")
                self.assertEqual(list(result.columns), [constants.STAGE_INSTANT])
                # Observations are taken at 08:00.
                self.assertTrue(all(t.hour == 8 for t in result.index))

    def test_levels_match_the_bulletin(self):
        angat = self._get("angat", constants.STAGE_INSTANT)
        values = {str(i.date()): round(float(v), 2) for i, v in angat[constants.STAGE_INSTANT].items()}
        self.assertEqual(values, {"2026-09-14": 208.13, "2026-09-15": 208.16})

    def test_reservoir_levels_are_elevations_not_gauge_heights(self):
        """Ambuklao sits near 752 m and La Mesa near 78 m; both are correct."""
        ambuklao = self._get("ambuklao", constants.STAGE_INSTANT)
        la_mesa = self._get("la_mesa", constants.STAGE_INSTANT)
        self.assertGreater(float(ambuklao[constants.STAGE_INSTANT].max()), 700)
        self.assertLess(float(la_mesa[constants.STAGE_INSTANT].max()), 100)

    def test_outflow_is_published_only_while_gates_are_open(self):
        # Ambuklao and Binga were spilling when the fixture was captured.
        for dam in ("ambuklao", "binga"):
            with self.subTest(dam=dam):
                flow = self._get(dam, constants.DISCHARGE_INSTANT)
                self.assertEqual(len(flow), 2)
                self.assertTrue((flow[constants.DISCHARGE_INSTANT] > 0).all())
        # Angat's gates were closed, so it has levels but no outflow.
        self.assertTrue(self._get("angat", constants.DISCHARGE_INSTANT).empty)
        self.assertFalse(self._get("angat", constants.STAGE_INSTANT).empty)

    def test_historical_request_returns_empty_without_fetching(self):
        patcher, session = mock_page()
        with patcher:
            result = self.fetcher.get_data(
                "angat", constants.STAGE_INSTANT, "2020-01-01", "2020-01-31"
            )
        self.assertTrue(result.empty)
        session.get.assert_not_called()

    def test_unknown_dam_and_variable_raise(self):
        with self.assertRaises(ValueError):
            self.fetcher.get_data("not_a_dam", constants.STAGE_INSTANT)
        with self.assertRaises(ValueError):
            self.fetcher.get_data("angat", constants.DISCHARGE_DAILY_MEAN)

    def test_table_is_found_by_signature_not_position(self):
        """An extra table on the page must not shift the parser onto the wrong one."""
        html = FIXTURE.read_text(encoding="utf-8")
        injected = html.replace("<body", "<body><table><tr><td>decoy</td></tr></table>", 1)
        response = MagicMock()
        response.text = injected
        response.raise_for_status.return_value = None
        session = MagicMock()
        session.get.return_value = response
        with patch("rivretrieve.pagasa.utils.requests_retry_session", return_value=session):
            table = self.fetcher._download_bulletin()
        self.assertIsNotNone(table)
        self.assertIn(PagasaDamFetcher._LEVEL_COLUMN, table.columns)

    def test_a_changed_layout_is_reported_not_guessed(self):
        response = MagicMock()
        response.text = "<html><body><table><tr><td>nothing useful</td></tr></table></body></html>"
        response.raise_for_status.return_value = None
        session = MagicMock()
        session.get.return_value = response
        with patch("rivretrieve.pagasa.utils.requests_retry_session", return_value=session):
            self.assertIsNone(self.fetcher._download_bulletin())

    def test_request_failure_returns_an_empty_frame(self):
        session = MagicMock()
        session.get.side_effect = RuntimeError("connection reset")
        with patch("rivretrieve.pagasa.utils.requests_retry_session", return_value=session):
            result = self.fetcher.get_data(
                "angat", constants.STAGE_INSTANT, self.start, self.end
            )
        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), [constants.STAGE_INSTANT])

    def test_window_is_declared_honestly(self):
        self.assertEqual(WINDOW_DAYS, 2)


class TestPagasaMetadata(unittest.TestCase):
    def test_cached_metadata_has_all_nine_dams_located(self):
        frame = PagasaDamFetcher.get_cached_metadata()
        self.assertEqual(len(frame), len(DAMS))
        self.assertTrue(frame[constants.LATITUDE].notna().all())
        self.assertTrue(frame[constants.LONGITUDE].notna().all())
        # All nine sit on Luzon.
        self.assertTrue(frame[constants.LATITUDE].between(13, 18).all())
        self.assertTrue(frame[constants.LONGITUDE].between(119, 123).all())

    def test_undefined_reference_levels_are_null_not_zero(self):
        frame = PagasaDamFetcher.get_cached_metadata()
        self.assertAlmostEqual(float(frame.loc["angat"]["normal_high_water_level"]), 210.0)
        # Caliraya has no normal high water level in the bulletin.
        self.assertTrue(pd.isna(frame.loc["caliraya"]["normal_high_water_level"]))

    def test_get_metadata_survives_an_unreachable_bulletin(self):
        """Coordinates are fixed, so the dam list works even when PAGASA is down."""
        session = MagicMock()
        session.get.side_effect = RuntimeError("timeout")
        with patch("rivretrieve.pagasa.utils.requests_retry_session", return_value=session):
            frame = PagasaDamFetcher().get_metadata()
        self.assertEqual(len(frame), len(DAMS))
        self.assertTrue(frame[constants.LATITUDE].notna().all())


class TestPagasaStationInventory(unittest.TestCase):
    def setUp(self):
        self.fetcher = PagasaStationFetcher()
        self.frame = PagasaStationFetcher.get_cached_metadata()

    def test_all_four_station_groups_are_present(self):
        counts = self.frame["station_group"].value_counts().to_dict()
        self.assertEqual(counts.get("synoptic"), 47)
        self.assertEqual(counts.get("synoptic-radar"), 10)
        self.assertEqual(counts.get("hydromet"), 23)
        self.assertEqual(counts.get("telemetered"), 119)
        self.assertEqual(len(self.frame), 199)

    def test_river_basin_outlines_are_not_treated_as_stations(self):
        """The map's fifth folder holds basin polygons, which have no Point."""
        self.assertNotIn("RBs with Data", set(self.frame["station_group"]))
        self.assertTrue(self.frame[constants.LATITUDE].notna().all())

    def test_every_station_is_located_in_the_philippines(self):
        # 114 E rather than 116 because PAGASA runs synoptic station PAG on
        # Pag-asa Island in the Kalayaan group — the same westward outlier that
        # appears in DOST-ASTI's network, and correct in both.
        self.assertTrue(self.frame[constants.LATITUDE].between(4, 22).all())
        self.assertTrue(self.frame[constants.LONGITUDE].between(114, 128).all())

    def test_telemetered_availability_follows_pagasas_own_field(self):
        telemetered = self.frame[self.frame["station_group"] == "telemetered"]
        for _, row in telemetered.iterrows():
            declared = (row["available_parameters"] or "").lower()
            with self.subTest(station=row.name):
                self.assertEqual(
                    bool(row[constants.PRECIPITATION_HOURLY_SUM]), "rainfall" in declared
                )
                self.assertEqual(bool(row[constants.STAGE_INSTANT]), "water level" in declared)
        # PAGASA's map shows 53 rainfall, 31 water level and 35 carrying both.
        self.assertEqual(int(telemetered[constants.PRECIPITATION_HOURLY_SUM].sum()), 88)
        self.assertEqual(int(telemetered[constants.STAGE_INSTANT].sum()), 66)

    def test_station_ids_are_unique_even_for_unnamed_placemarks(self):
        """Two telemetered placemarks carry no name at all."""
        self.assertEqual(len(self.frame.index), len(set(self.frame.index)))
        self.assertEqual(int(self.frame[constants.STATION_NAME].isna().sum()), 2)

    def test_get_data_returns_empty_and_points_at_the_request_portal(self):
        with self.assertLogs("rivretrieve.pagasa", level="WARNING") as logged:
            result = self.fetcher.get_data(
                self.frame.index[0], constants.PRECIPITATION_DAILY_SUM, "2024-01-01", "2024-01-31"
            )
        self.assertTrue(result.empty)
        self.assertIn("hydromet-data-request-portal", " ".join(logged.output))

    def test_unsupported_variable_raises(self):
        with self.assertRaises(ValueError):
            self.fetcher.get_data(self.frame.index[0], constants.DISCHARGE_DAILY_MEAN)

    def test_the_two_pagasa_providers_do_not_overlap(self):
        """The dams and the inventory are separate networks, not duplicates."""
        dams = set(PagasaDamFetcher.get_cached_metadata().index)
        stations = set(self.frame.index)
        self.assertFalse(dams & stations)


if __name__ == "__main__":
    unittest.main()

