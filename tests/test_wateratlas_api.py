"""Tests for the Global Water Atlas HTTP API.

The catalog is built once into a temporary directory from small synthetic
provider frames, so these never touch the network and never depend on the
shipped CSVs. Only the fetchers' ``get_data`` calls are mocked — the API,
catalog queries, job runner and packaging are all exercised for real.
"""

import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from wateratlas import availability, catalog_build, jobs, paths, registry
from wateratlas.catalog import catalog
from wateratlas.main import app

USA_FRAME = pd.DataFrame(
    {
        "gauge_id": ["02479500", "07374000"],
        "latitude": [30.8624092, 29.9],
        "longitude": [-88.417792, -91.2],
    }
).set_index("gauge_id")

CZECH_FRAME = pd.DataFrame(
    {
        "gauge_id": ["0-203-1-200500"],
        "station_name": ["Průhonice"],
        "river": ["Botič"],
        "latitude": [50.0],
        "longitude": [14.55],
        "area": [95.0],
        "altitude": [280.0],
    }
).set_index("gauge_id")

PORTUGAL_FRAME = pd.DataFrame(
    {"gauge_id": ["04K/04A"], "latitude": [41.5], "longitude": [-8.3]}
).set_index("gauge_id")

# Spain's cached CSV has no latitude/longitude columns at all, so its fixture
# must carry UTM 30N instead — the builder is meant to fail loudly if they go
# missing, and this keeps that guarantee under test.
SPAIN_FRAME = pd.DataFrame(
    {
        "gauge_id": ["1010"],
        "station_name": ["PUENTE QUEROL"],
        "COORD_UTMX_H30_ETRS89": [204647],
        "COORD_UTMY_H30_ETRS89": [4716265],
    }
).set_index("gauge_id")

FRAMES = {
    "usa": USA_FRAME,
    "czech": CZECH_FRAME,
    "portugal": PORTUGAL_FRAME,
    "spain": SPAIN_FRAME,
}


def _series(rows: int, variable: str) -> pd.DataFrame:
    index = pd.date_range("2023-01-01", periods=rows, freq="D", name="time")
    return pd.DataFrame({variable: [10.0 + i for i in range(rows)]}, index=index)


class RiverAppAPITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Redirect every write to a temp tree. The paths module reads its
        # environment at import time, which is too late once another test file
        # has already imported wateratlas, so patch the resolved values directly
        # — that keeps this independent of test collection order and cannot
        # overwrite a developer's real catalog.
        cls._temp = Path(tempfile.mkdtemp(prefix="wateratlas-test-"))
        cls._patches = [
            patch.object(paths, "CATALOG_DIR", cls._temp / "catalog"),
            patch.object(paths, "STATIONS_PARQUET", cls._temp / "catalog" / "stations.parquet"),
            patch.object(
                paths, "STATIONS_EXTRA_PARQUET", cls._temp / "catalog" / "stations_extra.parquet"
            ),
            patch.object(paths, "STATIONS_JSON", cls._temp / "catalog" / "stations.json"),
            patch.object(
                paths, "CATALOG_REPORT_JSON", cls._temp / "catalog" / "catalog_report.json"
            ),
            patch.object(paths, "STATE_DIR", cls._temp / "state"),
            patch.object(paths, "AVAILABILITY_DB", cls._temp / "state" / "availability.sqlite3"),
            patch.object(paths, "JOBS_DIR", cls._temp / "state" / "jobs"),
            patch.object(paths, "PREVIEW_CACHE_DIR", cls._temp / "state" / "preview_cache"),
        ]

        empty = pd.DataFrame({"gauge_id": []}).set_index("gauge_id")

        def fake_metadata(provider_key):
            return staticmethod(lambda: FRAMES.get(provider_key, empty))

        for provider in registry.PROVIDERS:
            cls._patches.append(
                patch.object(
                    provider.fetcher_class(),
                    "get_cached_metadata",
                    fake_metadata(provider.key),
                )
            )
        for item in cls._patches:
            item.start()

        paths.ensure_dirs()
        catalog_build.build_catalog(verbose=False)
        catalog.reload()
        availability.init()

    @classmethod
    def tearDownClass(cls):
        for item in cls._patches:
            item.stop()
        # The next test file must not see this run's five synthetic stations.
        catalog.reload()
        shutil.rmtree(cls._temp, ignore_errors=True)

    def setUp(self):
        self.client = TestClient(app)

    # -- catalog ---------------------------------------------------------------

    def test_providers_lists_every_registered_provider_with_credential_state(self):
        payload = self.client.get("/api/providers").json()
        self.assertTrue(payload["catalog_built"])
        self.assertEqual(len(payload["providers"]), len(registry.PROVIDERS))

        by_key = {p["key"]: p for p in payload["providers"]}
        self.assertEqual(by_key["usa"]["stations"], 2)
        self.assertEqual(by_key["usa"]["label"], "USGS NWIS")
        self.assertTrue(by_key["norway"]["per_station_availability"])
        # Norway and Brazil are the only providers that need secrets.
        self.assertEqual(by_key["norway"]["needs_credentials"], ["NVE_API_KEY"])
        self.assertIn("ANA_USERNAME", by_key["brazil"]["needs_credentials"])
        self.assertIsNotNone(by_key["canada"]["bulk_first_use"])

    def test_bbox_query_returns_geojson(self):
        response = self.client.get("/api/stations", params={"bbox": "-89,30,-88,31"})
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        feature = payload["features"][0]
        self.assertEqual(feature["properties"]["station_key"], "usa:02479500")
        self.assertEqual(feature["geometry"]["coordinates"], [-88.417792, 30.8624092])

    def test_text_search_matches_name_and_river(self):
        for needle in ["Průhonice", "Botič", "0-203-1-200500"]:
            with self.subTest(needle=needle):
                payload = self.client.get(
                    "/api/stations", params={"q": needle, "format": "table"}
                ).json()
                self.assertEqual(payload["total"], 1, needle)
                self.assertEqual(payload["stations"][0]["country"], "czech")

    def test_variable_filter_matches_any_requested_variable(self):
        # The Czech fetcher publishes water temperature; USGS does not.
        payload = self.client.get(
            "/api/stations",
            params={"variable": ["water-temperature_daily_mean"], "format": "table"},
        ).json()
        self.assertEqual({row["country"] for row in payload["stations"]}, {"czech"})

    def test_area_filter_excludes_stations_without_an_area(self):
        payload = self.client.get(
            "/api/stations", params={"min_area": 1, "format": "table"}
        ).json()
        # Only the Czech station has a catchment area in these fixtures.
        self.assertEqual(payload["total"], 1)

    def test_station_key_with_a_slash_is_addressable(self):
        """Portuguese gauge IDs contain '/', which is why the key is a query param."""
        response = self.client.get("/api/station", params={"key": "portugal:04K/04A"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["gauge_id"], "04K/04A")

    def test_unknown_station_is_a_404(self):
        response = self.client.get("/api/station", params={"key": "usa:does-not-exist"})
        self.assertEqual(response.status_code, 404)

    def test_map_payload_is_positional_and_self_describing(self):
        payload = self.client.get("/api/stations/map").json()
        self.assertEqual(
            payload["fields"],
            ["gauge_id", "lat", "lon", "country", "variable_mask", "station_name", "area"],
        )
        self.assertEqual(len(payload["countries"]), len(registry.PROVIDERS))
        self.assertEqual(payload["station_count"], len(payload["stations"]))

    # -- selection -------------------------------------------------------------

    def test_estimate_separates_unsupported_from_blocked(self):
        response = self.client.post(
            "/api/selection/estimate",
            json={
                "station_keys": ["usa:02479500", "czech:0-203-1-200500"],
                "variable": "water-temperature_daily_mean",
            },
        )
        payload = response.json()
        # USGS does not publish water temperature; the Czech station does.
        self.assertEqual(payload["unsupported"], 1)
        self.assertEqual(payload["downloadable"], 1)
        self.assertEqual(payload["blocked"], 0)

    def test_variable_choices_count_declaring_stations(self):
        payload = self.client.post(
            "/api/selection/variables",
            json={"station_keys": ["usa:02479500", "usa:07374000", "czech:0-203-1-200500"]},
        ).json()
        counts = {row["variable"]: row["stations"] for row in payload["variables"]}
        self.assertEqual(counts["discharge_daily_mean"], 3)
        self.assertEqual(counts["stage_daily_max"], 2)

    # -- downloads -------------------------------------------------------------

    def test_download_job_packages_csvs_manifest_and_attribution(self):
        def fake_get_data(self_, gauge_id, variable, start_date=None, end_date=None):
            # One station returns nothing, so the manifest has to record both outcomes.
            if gauge_id == "07374000":
                return pd.DataFrame(columns=["time", variable]).set_index("time")
            return _series(5, variable)

        with patch.object(registry.get_provider("usa").fetcher_class(), "get_data", fake_get_data):
            response = self.client.post(
                "/api/downloads",
                json={
                    "station_keys": ["usa:02479500", "usa:07374000", "usa:not-a-station"],
                    "variable": "discharge_daily_mean",
                    "start_date": "2023-01-01",
                    "end_date": "2023-01-05",
                },
            )
            job_id = response.json()["id"]
            self.assertEqual(response.json()["unknown_keys"], ["usa:not-a-station"])

            job = jobs.manager.get(job_id)
            for _ in range(200):
                if job.state in (jobs.DONE, jobs.CANCELLED):
                    break
                import time

                time.sleep(0.05)

        detail = self.client.get(f"/api/downloads/{job_id}").json()
        self.assertEqual(detail["state"], "done")
        states = {task["station_key"]: task["state"] for task in detail["tasks"]}
        self.assertEqual(states["usa:02479500"], "done")
        self.assertEqual(states["usa:07374000"], "empty")

        archive = self.client.get(f"/api/downloads/{job_id}/archive")
        self.assertEqual(archive.status_code, 200)

        with zipfile.ZipFile(job.archive_path) as bundle:
            names = bundle.namelist()
            self.assertIn("manifest.csv", names)
            self.assertIn("ATTRIBUTION.md", names)
            self.assertIn("data/usa/02479500_discharge_daily_mean.csv", names)
            # The empty station gets no CSV but still appears in the manifest.
            self.assertNotIn("data/usa/07374000_discharge_daily_mean.csv", names)

            manifest = bundle.read("manifest.csv").decode("utf-8")
            self.assertIn("usa:07374000", manifest)
            self.assertIn("empty", manifest)
            # A station with no name must not be written as the string "nan".
            self.assertNotIn(",nan,", manifest)

            attribution = bundle.read("ATTRIBUTION.md").decode("utf-8")
            self.assertIn("USGS NWIS", attribution)
            self.assertIn("rights remain with the original providers", attribution.lower())

    def test_download_records_learned_availability(self):
        observed = availability.for_station("usa:02479500")
        self.assertEqual(observed["discharge_daily_mean"]["state"], availability.CONFIRMED)
        self.assertEqual(observed["discharge_daily_mean"]["rows"], 5)

        absent = availability.for_station("usa:07374000")
        self.assertEqual(absent["discharge_daily_mean"]["state"], availability.ABSENT)

    def test_confirmed_does_not_get_overwritten_by_a_later_empty_range(self):
        """A gauge with 1990s data still has it when a 2024 query comes back empty."""
        availability.record("test:1", "discharge_daily_mean", availability.CONFIRMED, rows=100)
        availability.record("test:1", "discharge_daily_mean", availability.ABSENT)
        observed = availability.for_station("test:1")
        self.assertEqual(observed["discharge_daily_mean"]["state"], availability.CONFIRMED)

    def test_blocked_provider_reports_which_credential_is_missing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NVE_API_KEY", None)
            payload = self.client.post(
                "/api/selection/estimate",
                json={"station_keys": ["usa:02479500"], "variable": "discharge_daily_mean"},
            ).json()
        self.assertEqual(payload["blocked"], 0)

        # And directly, through the registry, for a provider that does need one.
        norway = registry.get_provider("norway")
        os.environ.pop("NVE_API_KEY", None)
        self.assertEqual(norway.missing_credentials(), ["NVE_API_KEY"])

    def test_archive_before_completion_is_a_conflict_not_a_500(self):
        response = self.client.get("/api/downloads/nonexistent/archive")
        self.assertEqual(response.status_code, 404)

    # -- report ----------------------------------------------------------------

    def test_catalog_report_accounts_for_every_row(self):
        report = self.client.get("/api/catalog/report").json()
        totals = report["totals"]
        failed = [p["key"] for p in report["providers"] if p["error"]]
        self.assertEqual(totals["providers_failed"], 0, f"providers failed: {failed}")
        self.assertEqual(totals["stations"], totals["mappable"] + totals["off_map"])
        self.assertEqual(json.loads(json.dumps(totals)), totals)


if __name__ == "__main__":
    unittest.main()
