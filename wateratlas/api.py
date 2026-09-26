"""HTTP API for the Global Water Atlas front end."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import availability, catalog_build, jobs, paths, preview, registry
from .catalog import CatalogNotBuilt, StationQuery, catalog, to_geojson

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _json_safe(value: Any) -> Any:
    """Makes parquet-backed values JSON-serialisable.

    ``variables`` round-trips through parquet as a numpy array, and numeric
    columns as numpy scalars; neither is JSON-serialisable on its own.
    """
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return None if math.isnan(number) or math.isinf(number) else number
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


# --------------------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------------------


class DownloadRequest(BaseModel):
    station_keys: list[str] = Field(..., min_length=1)
    variable: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class SelectionRequest(BaseModel):
    station_keys: list[str] = Field(default_factory=list)
    variable: Optional[str] = None


# --------------------------------------------------------------------------------------
# Providers and catalog health
# --------------------------------------------------------------------------------------


@router.get("/providers")
def list_providers() -> dict[str, Any]:
    """Every provider, with what it supports and whether it can be used right now."""
    try:
        counts = catalog.counts_by_country()
        totals = catalog.totals()
        built = True
    except CatalogNotBuilt:
        counts, totals, built = {}, {}, False

    providers = []
    for provider in registry.PROVIDERS:
        missing = provider.missing_credentials()
        providers.append(
            {
                "key": provider.key,
                "label": provider.label,
                "country_name": provider.country_name,
                "source_url": provider.source_url,
                "stations": counts.get(provider.key, 0),
                "variables": list(provider.declared_variables()),
                "per_station_availability": provider.availability_columns,
                "needs_credentials": list(provider.credentials),
                "missing_credentials": missing,
                "usable": not missing,
                "bulk_first_use": provider.bulk_first_use,
                "cache_warm": provider.cache_is_warm(),
                "throttle_note": provider.throttle_note,
                "seconds_per_station": provider.seconds_per_station,
            }
        )

    return {
        "catalog_built": built,
        "totals": totals,
        "variables": list(registry.all_variables()),
        "providers": providers,
        "availability": availability.summary(),
    }


@router.get("/catalog/report")
def catalog_report() -> dict[str, Any]:
    """The build report: rows in, rows out, and every row repaired or dropped."""
    report = catalog.report()
    if not report:
        raise HTTPException(status_code=404, detail="No catalog report. Run scripts/build_catalog.py.")
    return report


@router.post("/catalog/rebuild")
def rebuild_catalog() -> dict[str, Any]:
    """Rebuilds from the cached CSVs and reloads the in-memory catalog."""
    summary = catalog_build.build_catalog(verbose=False)
    catalog.reload()
    return summary


# --------------------------------------------------------------------------------------
# Stations
# --------------------------------------------------------------------------------------


@router.get("/stations/map")
def stations_map_payload() -> FileResponse:
    """The compact payload the map layer loads once.

    Served as a file so the ASGI server can handle conditional requests and
    compression rather than re-serialising 73k rows on every reload.
    """
    if not paths.STATIONS_JSON.exists():
        raise HTTPException(status_code=404, detail="No catalog. Run scripts/build_catalog.py.")
    return FileResponse(
        paths.STATIONS_JSON,
        media_type="application/json",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/stations")
def list_stations(
    bbox: Optional[str] = Query(None, description="west,south,east,north in WGS84 degrees"),
    country: Optional[list[str]] = Query(None),
    variable: Optional[list[str]] = Query(None),
    q: Optional[str] = Query(None, description="Substring of station name, river or gauge ID"),
    min_area: Optional[float] = Query(None),
    max_area: Optional[float] = Query(None),
    confirmed_only: bool = Query(False, description="Only stations with a confirmed download"),
    limit: int = Query(2000, ge=1, le=20000),
    offset: int = Query(0, ge=0),
    format: str = Query("geojson", pattern="^(geojson|table)$"),
) -> Any:
    """Filtered stations, as GeoJSON for the map or flat rows for the table."""
    parsed_bbox = None
    if bbox:
        try:
            west, south, east, north = (float(part) for part in bbox.split(","))
            parsed_bbox = (west, south, east, north)
        except ValueError:
            raise HTTPException(status_code=400, detail="bbox must be 'west,south,east,north'")

    spec = StationQuery(
        bbox=parsed_bbox,
        countries=country,
        variables=variable,
        text=q,
        min_area=min_area,
        max_area=max_area,
        mappable_only=format == "geojson",
        limit=limit,
        offset=offset,
    )
    try:
        page, total = catalog.query(spec)
    except CatalogNotBuilt as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if confirmed_only:
        allowed = availability.confirmed_keys(variable)
        page = page[page[registry.STATION_KEY].isin(allowed)]
        total = len(page)

    if format == "table":
        records = [_json_safe(row) for row in page.to_dict(orient="records")]
        return {"total": total, "returned": len(records), "stations": records}

    payload = to_geojson(page)
    payload["total"] = total
    payload["returned"] = len(payload["features"])
    payload["truncated"] = total > len(payload["features"]) + offset
    return payload


@router.get("/station/preview")
def station_preview(
    key: str = Query(..., description="station_key, e.g. usa:02479500"),
    variable: str = Query(...),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Downsampled hydrograph for the detail panel."""
    if catalog.get(key) is None:
        raise HTTPException(status_code=404, detail=f"Unknown station {key}")
    return preview.get_preview(key, variable, start_date, end_date, use_cache=not refresh)


@router.get("/station")
def get_station(key: str = Query(..., description="station_key, e.g. usa:02479500")) -> dict[str, Any]:
    """Full metadata for one station, including every provider-specific column.

    The key is a query parameter rather than a path segment because Portuguese
    gauge IDs contain slashes (``portugal:04K/04A``).
    """
    station_key = key
    try:
        record = catalog.get(station_key)
    except CatalogNotBuilt as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown station {station_key}")
    record = _json_safe(record)

    provider = registry.PROVIDERS_BY_KEY.get(record[registry.COUNTRY])
    record["provider"] = (
        {
            "key": provider.key,
            "label": provider.label,
            "country_name": provider.country_name,
            "source_url": provider.source_url,
            "missing_credentials": provider.missing_credentials(),
            "bulk_first_use": provider.bulk_first_use,
            "cache_warm": provider.cache_is_warm(),
            "throttle_note": provider.throttle_note,
        }
        if provider
        else None
    )
    record["observed"] = availability.for_station(station_key)
    return record


# --------------------------------------------------------------------------------------
# Selection helpers
# --------------------------------------------------------------------------------------


@router.post("/selection/variables")
def selection_variables(request: SelectionRequest) -> dict[str, Any]:
    """Which variables are worth offering for the current selection."""
    return {"variables": jobs.variable_choices(request.station_keys)}


@router.post("/selection/estimate")
def selection_estimate(request: SelectionRequest) -> dict[str, Any]:
    """Pre-flight: how many stations will actually download, and roughly how long."""
    if not request.variable:
        raise HTTPException(status_code=400, detail="variable is required")
    return jobs.estimate(request.station_keys, request.variable)


# --------------------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------------------


@router.post("/downloads")
def create_download(request: DownloadRequest) -> dict[str, Any]:
    """Starts a download job. Returns immediately; progress arrives over SSE."""
    try:
        job = jobs.manager.create(
            request.station_keys, request.variable, request.start_date, request.end_date
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except CatalogNotBuilt as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return job.to_dict(include_tasks=False)


@router.get("/downloads")
def list_downloads() -> dict[str, Any]:
    return {"jobs": [job.to_dict(include_tasks=False) for job in jobs.manager.list()]}


@router.get("/downloads/{job_id}")
def get_download(job_id: str) -> dict[str, Any]:
    job = jobs.manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    return job.to_dict()


@router.post("/downloads/{job_id}/cancel")
def cancel_download(job_id: str) -> dict[str, Any]:
    job = jobs.manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    job.cancel()
    return job.to_dict(include_tasks=False)


@router.get("/downloads/{job_id}/events")
async def download_events(job_id: str, since: int = 0) -> StreamingResponse:
    """Server-sent events: one message per station completion."""
    job = jobs.manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")

    async def stream():
        cursor = since
        idle = 0.0
        while True:
            events = job.events_since(cursor)
            if events:
                cursor += len(events)
                idle = 0.0
                for event in events:
                    yield f"data: {json.dumps(event)}\n\n"
            else:
                idle += 0.4
                if idle >= 15.0:
                    # Keep intermediaries from closing an idle connection.
                    yield ": keep-alive\n\n"
                    idle = 0.0
            if job.state in {jobs.DONE, jobs.CANCELLED} and not job.events_since(cursor):
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/downloads/{job_id}/archive")
def download_archive(job_id: str) -> FileResponse:
    """The finished ZIP: one CSV per station, plus manifest.csv and ATTRIBUTION.md."""
    job = jobs.manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    if not job.archive_path.exists():
        raise HTTPException(
            status_code=409,
            detail="No archive for this job yet — it is still running, or no station returned data.",
        )
    return FileResponse(
        job.archive_path,
        media_type="application/zip",
        filename=job.archive_path.name,
    )


# --------------------------------------------------------------------------------------
# Bulk cache warming
# --------------------------------------------------------------------------------------


@router.post("/providers/{country}/warm")
def warm_provider(country: str) -> dict[str, Any]:
    """Triggers a provider's one-time bulk download as a tracked job.

    Canada pulls the whole HYDAT database and Poland builds a Zarr store, both
    lazily inside ``get_data()``. Doing that behind a user's first map click
    would look like a hang, so it gets its own visible step.
    """
    try:
        provider = registry.get_provider(country)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not provider.bulk_first_use:
        raise HTTPException(status_code=400, detail=f"{provider.label} has no bulk cache to warm.")
    if provider.cache_is_warm():
        return {"country": country, "state": "already_warm"}

    spec = StationQuery(countries=[country], limit=1)
    page, _ = catalog.query(spec)
    if page.empty:
        raise HTTPException(status_code=400, detail=f"No {country} stations in the catalog.")

    variables = provider.declared_variables()
    if not variables:
        raise HTTPException(status_code=400, detail=f"{provider.label} declares no variables.")

    job = jobs.manager.create(
        [page.iloc[0][registry.STATION_KEY]],
        variables[0],
        start_date="2020-01-01",
        end_date="2020-01-31",
    )
    return {"country": country, "state": "warming", "job": job.to_dict(include_tasks=False)}
