"""Tests for the Global Water Atlas catalog builder.

These exercise the normalisation rules that let two dozen inconsistent CSVs
become one station index — the column aliases, the Spain reprojection, the
Australia deduplication and the null-placeholder handling. They use small
in-memory frames rather than the shipped CSVs so they stay fast and keep
working when a provider's cached file is refreshed.
"""

import json
import unittest
from unittest.mock import patch

import pandas as pd

from wateratlas import catalog_build, registry


def _fake_metadata(frame: pd.DataFrame):
    """Mimics ``get_cached_metadata()``, which returns a gauge_id-indexed frame."""
    return frame.set_index(registry.GAUGE_ID)


class TestCleanHelpers(unittest.TestCase):
    def test_null_placeholders_become_none(self):
        # Portugal writes "-" for a missing coordinate; pandas stringifies its
        # own NA as "<NA>"; Spain's photo columns use "N.D.".
        for value in ["-", "--", "N/A", "n.d.", "", "   ", pd.NA, None, float("nan")]:
            self.assertIsNone(catalog_build._clean_text(value), f"{value!r} should be None")

    def test_real_text_survives(self):
        self.assertEqual(catalog_build._clean_text("  Praha-Výtoň "), "Praha-Výtoň")
        # A gauge ID that looks numeric is still text.
        self.assertEqual(catalog_build._clean_text(601), "601")

    def test_nan_is_a_thai_place_name_not_a_null_marker(self):
        """Nan is a province, a city and a Chao Phraya tributary.

        Two ThaiWater rain gauges there are named "Nan" and "NaN"; treating the
        text as a null marker silently discarded both.
        """
        self.assertEqual(catalog_build._clean_text("Nan"), "Nan")
        self.assertEqual(catalog_build._clean_text("NaN"), "NaN")
        # A genuine missing value is still caught, by pd.isna rather than by text.
        self.assertIsNone(catalog_build._clean_text(float("nan")))
        self.assertIsNone(catalog_build._clean_text(pd.NA))

    def test_clean_number(self):
        self.assertEqual(catalog_build._clean_number("42.5"), 42.5)
        self.assertIsNone(catalog_build._clean_number("not a number"))
        self.assertIsNone(catalog_build._clean_number(float("inf")))
        self.assertIsNone(catalog_build._clean_number(pd.NA))

    def test_rounded_never_returns_nan(self):
        self.assertEqual(catalog_build._rounded("1234.56", 1), 1234.6)
        self.assertIsNone(catalog_build._rounded(float("nan"), 1))
        self.assertIsNone(catalog_build._rounded(None, 1))


class TestBuildProvider(unittest.TestCase):
    def test_gauge_name_is_aliased_to_station_name(self):
        """Poland, Slovenia and the NRFA spell it `gauge_name`."""
        provider = registry.get_provider("uk_nrfa")
        frame = pd.DataFrame(
            {
                "gauge_id": ["1001", "1002"],
                "gauge_name": ["Wick at Tarroul", "Thurso at Halkirk"],
                "river": ["Wick", "Thurso"],
                "latitude": [58.45, 58.50],
                "longitude": [-3.20, -3.50],
                "area": [161.9, 412.9],
            }
        )
        with patch.object(provider.fetcher_class(), "get_cached_metadata", return_value=_fake_metadata(frame)):
            core, _, report = catalog_build.build_provider(provider)

        self.assertEqual(list(core[registry.STATION_NAME]), ["Wick at Tarroul", "Thurso at Halkirk"])
        self.assertEqual(report.with_station_name, 2)
        self.assertEqual(list(core[registry.STATION_KEY]), ["uk_nrfa:1001", "uk_nrfa:1002"])

    def test_poland_gauge_altitude_is_aliased(self):
        provider = registry.get_provider("poland")
        frame = pd.DataFrame(
            {
                "gauge_id": ["149180010"],
                "gauge_name": ["KRZYŻANOWICE"],
                "river": ["Odra (1)"],
                "area": [5876.08],
                "gauge_altitude": [184.806],
                "latitude": [49.99362],
                "longitude": [18.28731],
            }
        )
        with patch.object(provider.fetcher_class(), "get_cached_metadata", return_value=_fake_metadata(frame)):
            core, _, _ = catalog_build.build_provider(provider)

        self.assertAlmostEqual(core.iloc[0][registry.ALTITUDE], 184.806)
        self.assertEqual(core.iloc[0][registry.STATION_NAME], "KRZYŻANOWICE")

    def test_spain_is_reprojected_from_utm(self):
        """Spain's cached CSV has no latitude/longitude at all — only UTM 30N."""
        provider = registry.get_provider("spain")
        self.assertEqual(provider.source_crs, "EPSG:25830")

        frame = pd.DataFrame(
            {
                "gauge_id": ["1010", "1011"],
                "station_name": ["PUENTE QUEROL", "RUAPETÍN"],
                "COORD_UTMX_H30_ETRS89": [204647, 160356],
                "COORD_UTMY_H30_ETRS89": [4716265, 4701177],
            }
        )
        with patch.object(provider.fetcher_class(), "get_cached_metadata", return_value=_fake_metadata(frame)):
            core, _, report = catalog_build.build_provider(provider)

        self.assertEqual(report.reprojected, 2)
        self.assertTrue(core[registry.HAS_COORDS].all())
        # Both gauges are in León province, north-west Spain.
        for _, row in core.iterrows():
            self.assertTrue(41.0 < row[registry.LATITUDE] < 44.0, row[registry.LATITUDE])
            self.assertTrue(-8.0 < row[registry.LONGITUDE] < -5.0, row[registry.LONGITUDE])

    def test_duplicate_gauge_ids_are_dropped_and_counted(self):
        """Australia's cached CSV repeats 125 gauge IDs."""
        provider = registry.get_provider("australia")
        frame = pd.DataFrame(
            {
                "gauge_id": ["410730", "410730", "410731"],
                "latitude": [-35.1, -35.1, -35.2],
                "longitude": [148.9, 148.9, 149.0],
            }
        )
        with patch.object(provider.fetcher_class(), "get_cached_metadata", return_value=_fake_metadata(frame)):
            core, _, report = catalog_build.build_provider(provider)

        self.assertEqual(report.duplicates_dropped, 1)
        self.assertEqual(len(core), 2)
        self.assertEqual(len(set(core[registry.STATION_KEY])), 2)

    def test_missing_and_out_of_range_coordinates_are_flagged_not_dropped(self):
        provider = registry.get_provider("slovenia")
        frame = pd.DataFrame(
            {
                "gauge_id": ["1060", "1070", "1080"],
                "gauge_name": ["Kokra", "Sava", "Drava"],
                "latitude": [46.2, None, 999.0],
                "longitude": [14.4, None, 15.0],
            }
        )
        with patch.object(provider.fetcher_class(), "get_cached_metadata", return_value=_fake_metadata(frame)):
            core, _, report = catalog_build.build_provider(provider)

        # Every station is kept so it stays searchable; only the map excludes them.
        self.assertEqual(len(core), 3)
        self.assertEqual(report.missing_coordinates, 2)
        self.assertEqual(list(core[registry.HAS_COORDS]), [True, False, False])

    def test_norway_availability_is_per_station(self):
        """Norway's CSV knows which series each gauge has."""
        provider = registry.get_provider("norway")
        self.assertTrue(provider.availability_columns)

        frame = pd.DataFrame(
            {
                "gauge_id": ["1.10.0", "1.15.0"],
                "station_name": ["Skotberg bru", "Ørje"],
                "latitude": [59.21, 59.48],
                "longitude": [11.69, 11.64],
                "stage_daily_mean": ["True", "True"],
                "discharge_daily_mean": ["False", "True"],
                "stage_hourly_mean": ["False", "True"],
            }
        )
        with patch.object(provider.fetcher_class(), "get_cached_metadata", return_value=_fake_metadata(frame)):
            core, _, _ = catalog_build.build_provider(provider)

        self.assertEqual(core.iloc[0][registry.VARIABLES], ["stage_daily_mean"])
        self.assertEqual(
            sorted(core.iloc[1][registry.VARIABLES]),
            ["discharge_daily_mean", "stage_daily_mean", "stage_hourly_mean"],
        )

    def test_unnamed_index_column_is_not_kept_as_metadata(self):
        """germany_berlin's CSV leads with an unnamed pandas index column."""
        provider = registry.get_provider("germany_berlin")
        frame = pd.DataFrame(
            {
                "Unnamed: 0": [0],
                "gauge_id": ["601"],
                "station_name": ["MPS Berlin-Spandauer-Schifffahrtskanal"],
                "latitude": [52.53854],
                "longitude": [13.34503],
            }
        )
        with patch.object(provider.fetcher_class(), "get_cached_metadata", return_value=_fake_metadata(frame)):
            _, extras, _ = catalog_build.build_provider(provider)

        extra = json.loads(extras.iloc[0]["extra"])
        self.assertNotIn("Unnamed: 0", extra)
        self.assertNotIn("_row_index", extra)


class TestMapPayload(unittest.TestCase):
    @staticmethod
    def _row(**overrides):
        row = {
            registry.STATION_KEY: "usa:1",
            registry.COUNTRY: "usa",
            registry.GAUGE_ID: "1",
            registry.STATION_NAME: None,
            registry.RIVER: None,
            registry.LATITUDE: 40.0,
            registry.LONGITUDE: -100.0,
            registry.HAS_COORDS: True,
            registry.ALTITUDE: None,
            registry.AREA: 250.0,
            registry.VARIABLES: ["discharge_daily_mean"],
        }
        row.update(overrides)
        return row

    def test_payload_excludes_stations_without_coordinates(self):
        catalog = pd.DataFrame(
            [
                self._row(),
                self._row(
                    **{
                        registry.STATION_KEY: "slovenia:2",
                        registry.COUNTRY: "slovenia",
                        registry.GAUGE_ID: "2",
                        registry.STATION_NAME: "Sava",
                        registry.LATITUDE: None,
                        registry.LONGITUDE: None,
                        registry.HAS_COORDS: False,
                        registry.AREA: None,
                    }
                ),
            ]
        )
        payload = catalog_build.build_map_payload(catalog)

        self.assertEqual(payload["station_count"], 1)
        self.assertEqual(payload["off_map_count"], 1)
        row = payload["stations"][0]
        self.assertEqual(row[0], "1")
        self.assertEqual(row[6], 250.0)
        # The bit position must match the server's variable order.
        self.assertEqual(row[4], registry.variable_mask(["discharge_daily_mean"]))

    def test_payload_is_strict_json_even_when_names_are_missing(self):
        """A missing name arrives as NaN from parquet, and bare NaN is not JSON.

        json.dumps writes it happily; no browser will parse it, and the map is
        left stuck on "Loading the station catalog".
        """
        catalog = pd.DataFrame(
            [self._row(**{registry.STATION_NAME: float("nan"), registry.AREA: float("nan")})]
        )
        payload = catalog_build.build_map_payload(catalog)
        row = payload["stations"][0]
        self.assertIsNone(row[5], "station_name must be null, not NaN")
        self.assertIsNone(row[6], "area must be null, not NaN")

        encoded = json.dumps(payload, allow_nan=False)
        self.assertNotIn("NaN", encoded)
        self.assertIsNone(json.loads(encoded)["stations"][0][5])

    def test_variable_mask_round_trips(self):
        variables = ["discharge_daily_mean", "stage_instantaneous"]
        mask = registry.variable_mask(variables)
        decoded = [
            name for position, name in enumerate(registry.all_variables()) if mask & (1 << position)
        ]
        self.assertEqual(sorted(decoded), sorted(variables))


class TestRegistry(unittest.TestCase):
    def test_every_provider_maps_to_a_real_fetcher(self):
        for provider in registry.PROVIDERS:
            with self.subTest(provider=provider.key):
                self.assertTrue(callable(provider.fetcher_class()))
                self.assertTrue(provider.declared_variables(), f"{provider.key} declares no variables")

    def test_station_keys_survive_gauge_ids_containing_separators(self):
        # Portuguese gauge IDs look like 04K/04A.
        key = registry.station_key("portugal", "04K/04A")
        self.assertEqual(registry.split_station_key(key), ("portugal", "04K/04A"))

    def test_credentials_are_declared_for_every_provider_that_needs_them(self):
        needing = {p.key: p.credentials for p in registry.PROVIDERS if p.credentials}
        self.assertEqual(
            needing,
            {
                # Brazil's credentials are optional: without them it uses ANA's public service.
                "norway": ("NVE_API_KEY",),
                # Station metadata is open; only the readings need a token.
                "philippines": ("PHILSENSORS_TOKEN",),
            },
        )


if __name__ == "__main__":
    unittest.main()
