"""In-memory station catalog.

Loads the artefacts built by :mod:`wateratlas.catalog_build` once and answers
spatial and attribute queries against them. The raw provider CSVs are never
touched at request time.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

from . import paths, registry
from .registry import (
    ALTITUDE,
    AREA,
    COUNTRY,
    GAUGE_ID,
    HAS_COORDS,
    LATITUDE,
    LONGITUDE,
    RIVER,
    STATION_KEY,
    STATION_NAME,
    VARIABLES,
)


class CatalogNotBuilt(RuntimeError):
    """Raised when the app starts before ``build_catalog`` has ever run."""


@dataclass
class StationQuery:
    """Filters accepted by :meth:`Catalog.query`."""

    bbox: Optional[tuple[float, float, float, float]] = None
    """``(west, south, east, north)`` in WGS84 degrees."""

    countries: Optional[Iterable[str]] = None
    variables: Optional[Iterable[str]] = None
    text: Optional[str] = None
    """Case-insensitive substring match against station name, river and gauge ID."""

    min_area: Optional[float] = None
    max_area: Optional[float] = None
    mappable_only: bool = False
    limit: int = 2000
    offset: int = 0


class Catalog:
    """Thread-safe, lazily loaded view of the built catalog."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stations: Optional[pd.DataFrame] = None
        self._extra: Optional[pd.DataFrame] = None
        self._search_blob: Optional[pd.Series] = None
        self._masks: Optional[np.ndarray] = None

    # -- loading -------------------------------------------------------------------

    @property
    def is_built(self) -> bool:
        return paths.STATIONS_PARQUET.exists()

    def _load(self) -> pd.DataFrame:
        if self._stations is not None:
            return self._stations
        with self._lock:
            if self._stations is not None:
                return self._stations
            if not self.is_built:
                raise CatalogNotBuilt(
                    f"No catalog at {paths.STATIONS_PARQUET}. Run: python scripts/build_catalog.py"
                )
            stations = pd.read_parquet(paths.STATIONS_PARQUET)

            # Precompute what every query needs, once.
            self._masks = np.array([registry.variable_mask(v) for v in stations[VARIABLES]], dtype=np.int64)
            self._search_blob = (
                stations[GAUGE_ID].fillna("")
                + "␟"
                + stations[STATION_NAME].fillna("")
                + "␟"
                + stations[RIVER].fillna("")
            ).str.casefold()

            self._stations = stations
            return self._stations

    def _load_extra(self) -> pd.DataFrame:
        if self._extra is None:
            with self._lock:
                if self._extra is None:
                    if paths.STATIONS_EXTRA_PARQUET.exists():
                        self._extra = pd.read_parquet(paths.STATIONS_EXTRA_PARQUET).set_index(STATION_KEY)
                    else:
                        self._extra = pd.DataFrame(columns=["extra"])
        return self._extra

    def reload(self) -> None:
        """Drops the cached frames so the next request re-reads a fresh build."""
        with self._lock:
            self._stations = None
            self._extra = None
            self._search_blob = None
            self._masks = None

    # -- queries -------------------------------------------------------------------

    def query(self, spec: StationQuery) -> tuple[pd.DataFrame, int]:
        """Applies every filter and returns ``(page, total_matched)``."""
        stations = self._load()
        keep = np.ones(len(stations), dtype=bool)

        if spec.mappable_only or spec.bbox is not None:
            keep &= stations[HAS_COORDS].to_numpy(dtype=bool)

        if spec.bbox is not None:
            west, south, east, north = spec.bbox
            latitudes = stations[LATITUDE].to_numpy(dtype="float64", na_value=np.nan)
            longitudes = stations[LONGITUDE].to_numpy(dtype="float64", na_value=np.nan)
            keep &= (latitudes >= south) & (latitudes <= north)
            if west <= east:
                keep &= (longitudes >= west) & (longitudes <= east)
            else:
                # The viewport straddles the antimeridian.
                keep &= (longitudes >= west) | (longitudes <= east)

        if spec.countries:
            keep &= stations[COUNTRY].isin(list(spec.countries)).to_numpy(dtype=bool)

        if spec.variables:
            wanted = registry.variable_mask(spec.variables)
            # A station matches if it declares *any* of the requested variables.
            keep &= (self._masks & wanted) != 0

        if spec.text:
            needle = spec.text.strip().casefold()
            if needle:
                keep &= self._search_blob.str.contains(needle, regex=False, na=False).to_numpy(dtype=bool)

        if spec.min_area is not None or spec.max_area is not None:
            areas = stations[AREA].to_numpy(dtype="float64", na_value=np.nan)
            if spec.min_area is not None:
                keep &= areas >= spec.min_area
            if spec.max_area is not None:
                keep &= areas <= spec.max_area

        matched = stations[keep]
        total = len(matched)
        page = matched.iloc[spec.offset : spec.offset + spec.limit]
        return page, total

    def get(self, key: str) -> Optional[dict[str, Any]]:
        """Full record for one station, including provider-specific columns."""
        stations = self._load()
        hit = stations[stations[STATION_KEY] == key]
        if hit.empty:
            return None
        record = hit.iloc[0].to_dict()
        record[VARIABLES] = list(record[VARIABLES])

        extra_table = self._load_extra()
        raw_extra = extra_table["extra"].get(key)
        record["extra"] = json.loads(raw_extra) if isinstance(raw_extra, str) else {}
        return record

    def keys_exist(self, keys: Iterable[str]) -> tuple[list[str], list[str]]:
        """Splits requested keys into ``(known, unknown)``."""
        stations = self._load()
        known_set = set(stations[STATION_KEY])
        known, unknown = [], []
        for key in keys:
            (known if key in known_set else unknown).append(key)
        return known, unknown

    def rows_for_keys(self, keys: Iterable[str]) -> pd.DataFrame:
        """Catalog rows for a selection, in catalog order."""
        stations = self._load()
        wanted = set(keys)
        return stations[stations[STATION_KEY].isin(wanted)]

    # -- summaries -----------------------------------------------------------------

    def counts_by_country(self) -> dict[str, int]:
        stations = self._load()
        return {str(k): int(v) for k, v in stations[COUNTRY].value_counts().items()}

    def totals(self) -> dict[str, int]:
        stations = self._load()
        return {
            "stations": len(stations),
            "mappable": int(stations[HAS_COORDS].sum()),
            "off_map": int((~stations[HAS_COORDS]).sum()),
            "named": int(stations[STATION_NAME].notna().sum()),
        }

    def report(self) -> dict[str, Any]:
        """The build report written alongside the catalog, if present."""
        if not paths.CATALOG_REPORT_JSON.exists():
            return {}
        return json.loads(paths.CATALOG_REPORT_JSON.read_text(encoding="utf-8"))


def to_geojson(frame: pd.DataFrame) -> dict[str, Any]:
    """Converts catalog rows into a GeoJSON FeatureCollection.

    Only the properties the map and result table actually render are included;
    everything else is one request away at ``/api/stations/{key}``.
    """
    features = []
    for row in frame.to_dict(orient="records"):
        if not row[HAS_COORDS]:
            continue
        features.append(
            {
                "type": "Feature",
                "id": row[STATION_KEY],
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(row[LONGITUDE]), float(row[LATITUDE])],
                },
                "properties": {
                    "station_key": row[STATION_KEY],
                    "country": row[COUNTRY],
                    "gauge_id": row[GAUGE_ID],
                    "station_name": row[STATION_NAME],
                    "river": row[RIVER],
                    "area": row[AREA],
                    "altitude": row[ALTITUDE],
                    "variables": list(row[VARIABLES]),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


#: Process-wide catalog. The FastAPI app shares one instance.
catalog = Catalog()
