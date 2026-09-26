"""Hydrograph previews for the station detail panel.

A full daily record can run to 40,000 points, which is both slow to send and
impossible to read in a 300 px panel. Previews are downsampled to buckets that
keep each interval's minimum, mean and maximum, so flood peaks survive the
reduction instead of being averaged away.

Responses are cached on disk, so opening the same station twice is free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import Any, Optional

import numpy as np
import pandas as pd

from . import availability, paths, registry
from .jobs import manager

logger = logging.getLogger(__name__)

TARGET_BUCKETS = 600


def _cache_path(station_key: str, variable: str, start: Optional[str], end: Optional[str]):
    digest = hashlib.sha1(
        "|".join([station_key, variable, start or "", end or ""]).encode("utf-8")
    ).hexdigest()[:16]
    return paths.PREVIEW_CACHE_DIR / f"{digest}.json"


def _downsample(frame: pd.DataFrame, variable: str) -> dict[str, list]:
    """Reduces a series to at most ``TARGET_BUCKETS`` min/mean/max buckets."""
    series = pd.to_numeric(frame[variable], errors="coerce").dropna()
    if series.empty:
        return {"t": [], "mean": [], "min": [], "max": []}

    index = pd.to_datetime(series.index, errors="coerce")
    series = series[index.notna()]
    index = index[index.notna()]
    series.index = index
    series = series.sort_index()

    if len(series) <= TARGET_BUCKETS:
        values = [round(float(v), 4) for v in series.to_numpy()]
        stamps = [d.strftime("%Y-%m-%d") for d in series.index]
        return {"t": stamps, "mean": values, "min": values, "max": values}

    bucket_size = math.ceil(len(series) / TARGET_BUCKETS)
    groups = np.arange(len(series)) // bucket_size
    grouped = series.groupby(groups)

    means = grouped.mean()
    minima = grouped.min()
    maxima = grouped.max()
    stamps = grouped.apply(lambda chunk: chunk.index[0])

    return {
        "t": [pd.Timestamp(d).strftime("%Y-%m-%d") for d in stamps],
        "mean": [round(float(v), 4) for v in means],
        "min": [round(float(v), 4) for v in minima],
        "max": [round(float(v), 4) for v in maxima],
        "bucket_days": bucket_size,
    }


def get_preview(
    station_key: str,
    variable: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Fetches, downsamples and caches one station's series."""
    paths.ensure_dirs()
    cache_file = _cache_path(station_key, variable, start_date, end_date)
    if use_cache and cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache_file.unlink(missing_ok=True)

    country, gauge_id = registry.split_station_key(station_key)
    provider = registry.get_provider(country)

    reason = provider.blocked_reason()
    if reason:
        return {
            "station_key": station_key,
            "variable": variable,
            "status": "blocked",
            "message": reason,
            "series": {"t": [], "mean": [], "min": [], "max": []},
        }
    if variable not in provider.declared_variables():
        return {
            "station_key": station_key,
            "variable": variable,
            "status": "unsupported",
            "message": f"{provider.label} does not publish {variable}.",
            "series": {"t": [], "mean": [], "min": [], "max": []},
        }

    fetcher = manager.fetcher(country)
    try:
        frame = fetcher.get_data(
            gauge_id=gauge_id, variable=variable, start_date=start_date, end_date=end_date
        )
    except Exception as exc:
        logger.warning("Preview failed for %s/%s: %s", station_key, variable, exc)
        availability.record(station_key, variable, availability.FAILED, note=str(exc)[:400])
        return {
            "station_key": station_key,
            "variable": variable,
            "status": "failed",
            "message": str(exc)[:400],
            "series": {"t": [], "mean": [], "min": [], "max": []},
        }

    if frame is None or frame.empty:
        availability.record(station_key, variable, availability.ABSENT)
        payload = {
            "station_key": station_key,
            "variable": variable,
            "status": "empty",
            "message": "The provider returned no rows for this range.",
            "series": {"t": [], "mean": [], "min": [], "max": []},
        }
        cache_file.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    if variable not in frame.columns:
        # Fetchers name the single value column after the variable; be forgiving.
        frame = frame.rename(columns={frame.columns[0]: variable})

    series = _downsample(frame, variable)
    values = pd.to_numeric(frame[variable], errors="coerce").dropna()
    index = pd.to_datetime(frame.index, errors="coerce")

    first_date = str(index.min().date()) if index.notna().any() else None
    last_date = str(index.max().date()) if index.notna().any() else None
    availability.record(
        station_key,
        variable,
        availability.CONFIRMED,
        rows=len(values),
        first_date=first_date,
        last_date=last_date,
    )

    payload = {
        "station_key": station_key,
        "variable": variable,
        "status": "ok",
        "rows": int(len(values)),
        "first_date": first_date,
        "last_date": last_date,
        "stats": {
            "min": round(float(values.min()), 4),
            "max": round(float(values.max()), 4),
            "mean": round(float(values.mean()), 4),
        },
        "series": series,
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    return payload
