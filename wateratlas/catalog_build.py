"""Builds the unified station catalog from every provider's cached CSV.

This is M0 — the step everything else depends on. It runs once (and again
whenever the cached CSVs change) and writes four artefacts into
``wateratlas/catalog_data/``:

``stations.parquet``
    One normalised row per station: core columns only, fast to load.
``stations_extra.parquet``
    ``station_key`` plus every provider-specific column as JSON, loaded lazily
    for the detail panel so the 15 MB of UK-EA blobs never touch the map path.
``stations.json``
    A compact positional-array payload for the map layer.
``catalog_report.json``
    What went in, what came out, and every row that was dropped or repaired.

The raw CSVs are never read at request time.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import pandas as pd

from . import paths, registry
from .registry import (  # noqa: F401  (ALTITUDE is part of the canonical column set)
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
    Provider,
)

logger = logging.getLogger(__name__)

# Columns that are enormous, provider-internal and useless in the UI. Dropping
# them keeps stations_extra.parquet to a sane size (uk_ea's `measures` alone is
# most of that file's 15.2 MB).
DROPPED_EXTRA_COLUMNS: dict[str, tuple[str, ...]] = {
    "uk_ea": ("measures", "observedProperty", "type"),
    "norway": ("seriesList",),
}


@dataclass
class ProviderReport:
    """What happened to one provider's CSV during the build."""

    key: str
    label: str
    rows_in: int = 0
    rows_out: int = 0
    duplicates_dropped: int = 0
    missing_coordinates: int = 0
    reprojected: int = 0
    with_station_name: int = 0
    with_river: int = 0
    with_area: int = 0
    declared_variables: list[str] = field(default_factory=list)
    per_station_availability: bool = False
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None


# Placeholders that mean "no value". Portugal writes "-" for a missing
# coordinate; pandas stringifies its own NA as "<NA>".
#
# "nan" is deliberately NOT in this set. Real missing values are caught by the
# pd.isna check below, and Nan is a Thai province, city and major Chao Phraya
# tributary: two rain gauges there are named "Nan" and "NaN" (the latter a
# romanisation slip upstream). Treating the text as null discarded both.
_NULL_TEXT = {"nat", "none", "null", "na", "n/a", "n.d.", "-", "--", "<na>", ""}


def _clean_text(value: Any) -> Optional[str]:
    """Normalises a cell to a non-empty string, or None."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        # pd.isna raises on list-likes; those are never text fields here.
        pass
    text = str(value).strip()
    if text.lower() in _NULL_TEXT:
        return None
    return text


def _clean_number(value: Any) -> Optional[float]:
    """Normalises a cell to a finite float, or None."""
    number = pd.to_numeric(value, errors="coerce")
    try:
        number = float(number)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _rounded(value: Any, digits: int) -> Optional[float]:
    """A finite rounded float, or None. Never NaN — see :func:`build_map_payload`."""
    number = _clean_number(value)
    return None if number is None else round(number, digits)


def _reproject(frame: pd.DataFrame, provider: Provider) -> tuple[pd.Series, pd.Series, int]:
    """Projects a provider's native coordinates into WGS84.

    Spain is the only provider in the set whose cached CSV has no latitude or
    longitude column at all — all 1,491 rows carry UTM 30N / ETRS89 easting and
    northing instead, which would leave every Spanish station off the map.
    """
    from pyproj import Transformer

    x_column, y_column = provider.coord_columns
    missing = [c for c in (x_column, y_column) if c not in frame.columns]
    if missing:
        raise KeyError(f"{provider.key}: expected coordinate columns {missing} in the cached CSV")

    x_values = pd.to_numeric(frame[x_column], errors="coerce")
    y_values = pd.to_numeric(frame[y_column], errors="coerce")

    transformer = Transformer.from_crs(provider.source_crs, "EPSG:4326", always_xy=True)
    longitudes, latitudes = transformer.transform(x_values.to_numpy(), y_values.to_numpy())

    longitude = pd.Series(longitudes, index=frame.index)
    latitude = pd.Series(latitudes, index=frame.index)
    converted = int((latitude.notna() & longitude.notna()).sum())
    return latitude, longitude, converted


def _station_variables(row: pd.Series, provider: Provider, declared: tuple[str, ...]) -> list[str]:
    """Variables for one station.

    Some cached CSVs carry one boolean column per variable, so availability can
    be known per station rather than assumed per provider. Everywhere else, the
    provider's declared set is the best we have until a download confirms
    otherwise.
    """
    if not provider.availability_columns:
        return list(declared)

    available = []
    for variable in declared:
        flag = row.get(variable)
        if isinstance(flag, str):
            if flag.strip().lower() == "true":
                available.append(variable)
        elif bool(flag):
            available.append(variable)
    return available


def build_provider(provider: Provider) -> tuple[pd.DataFrame, pd.DataFrame, ProviderReport]:
    """Normalises one provider's cached CSV into core + extra frames."""
    report = ProviderReport(key=provider.key, label=provider.label)

    fetcher_class = provider.fetcher_class()
    raw = fetcher_class.get_cached_metadata()
    frame = raw.reset_index()
    report.rows_in = len(frame)

    # 1. Canonical column names. Seven CSVs say `station_name`, three say
    #    `gauge_name`; Poland alone says `gauge_altitude`.
    aliases = {**registry.COMMON_ALIASES, **provider.aliases}
    renames = {source: target for source, target in aliases.items() if source in frame.columns}
    # germany_berlin's CSV leads with an unnamed pandas index column.
    for column in frame.columns:
        if isinstance(column, str) and column.startswith("Unnamed:"):
            renames[column] = "_row_index"
    frame = frame.rename(columns=renames)
    # A rename can collide with a column that already exists; keep the first.
    frame = frame.loc[:, ~frame.columns.duplicated()]

    if GAUGE_ID not in frame.columns:
        raise KeyError(f"{provider.key}: cached CSV has no {GAUGE_ID} column")

    # 2. Coordinates.
    if provider.coord_columns:
        latitude, longitude, converted = _reproject(frame, provider)
        frame[LATITUDE] = latitude
        frame[LONGITUDE] = longitude
        report.reprojected = converted
        report.warnings.append(
            f"No latitude/longitude in the cached CSV; reprojected {converted} rows "
            f"from {provider.source_crs} via {'/'.join(provider.coord_columns)}."
        )
    for column in (LATITUDE, LONGITUDE):
        if column not in frame.columns:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    # 3. Optional descriptive columns.
    for column in (STATION_NAME, RIVER, ALTITUDE, AREA):
        if column not in frame.columns:
            frame[column] = pd.NA

    declared = provider.declared_variables()
    report.declared_variables = list(declared)
    report.per_station_availability = provider.availability_columns

    # 4. Deduplicate. Australia's CSV has 125 repeated gauge_ids; first row wins
    #    and the count is reported rather than silently swallowed.
    frame[GAUGE_ID] = frame[GAUGE_ID].astype(str).str.strip()
    before = len(frame)
    frame = frame.drop_duplicates(subset=[GAUGE_ID], keep="first")
    report.duplicates_dropped = before - len(frame)
    if report.duplicates_dropped:
        report.warnings.append(
            f"Dropped {report.duplicates_dropped} rows with a duplicate {GAUGE_ID} (kept the first of each)."
        )

    core_rows: list[dict[str, Any]] = []
    extra_rows: list[dict[str, Any]] = []
    dropped_extra = set(DROPPED_EXTRA_COLUMNS.get(provider.key, ()))

    for _, row in frame.iterrows():
        gauge_id = str(row[GAUGE_ID])
        key = registry.station_key(provider.key, gauge_id)

        latitude = _clean_number(row.get(LATITUDE))
        longitude = _clean_number(row.get(LONGITUDE))
        # A coordinate outside these bounds is corrupt, not merely absent.
        valid = (
            latitude is not None
            and longitude is not None
            and -90.0 <= latitude <= 90.0
            and -180.0 <= longitude <= 180.0
        )
        if not valid:
            latitude = longitude = None

        core_rows.append(
            {
                STATION_KEY: key,
                COUNTRY: provider.key,
                GAUGE_ID: gauge_id,
                STATION_NAME: _clean_text(row.get(STATION_NAME)),
                RIVER: _clean_text(row.get(RIVER)),
                LATITUDE: latitude,
                LONGITUDE: longitude,
                HAS_COORDS: valid,
                ALTITUDE: _clean_number(row.get(ALTITUDE)),
                AREA: _clean_number(row.get(AREA)),
                VARIABLES: _station_variables(row, provider, declared),
            }
        )

        extra: dict[str, Any] = {}
        for column, value in row.items():
            if not isinstance(column, str) or column in dropped_extra or column.startswith("_row_index"):
                continue
            cleaned = _clean_text(value)
            if cleaned is not None:
                extra[column] = cleaned
        extra_rows.append({STATION_KEY: key, "extra": json.dumps(extra, ensure_ascii=False)})

    core = pd.DataFrame(core_rows, columns=list(registry.CORE_COLUMNS))
    # Without an explicit cast an empty provider yields an object-dtype column,
    # and `~core[HAS_COORDS]` then inverts Python bools rather than the mask.
    core[HAS_COORDS] = core[HAS_COORDS].astype(bool)
    extras = pd.DataFrame(extra_rows, columns=[STATION_KEY, "extra"])

    report.rows_out = len(core)
    report.missing_coordinates = int((~core[HAS_COORDS]).sum())
    report.with_station_name = int(core[STATION_NAME].notna().sum())
    report.with_river = int(core[RIVER].notna().sum())
    report.with_area = int(core[AREA].notna().sum())
    if report.missing_coordinates and not provider.coord_columns:
        report.warnings.append(f"{report.missing_coordinates} rows have no usable coordinates and are off-map.")
    if report.with_station_name == 0:
        report.warnings.append("Cached CSV carries no station names; the map can only show gauge IDs.")

    return core, extras, report


def build_map_payload(catalog: pd.DataFrame) -> dict[str, Any]:
    """Packs the mappable stations into a compact positional-array payload.

    Objects-per-station would roughly triple the transfer size for 73k rows, so
    each station is a fixed-order array and the country and variable vocabularies
    are sent once at the top.
    """
    countries = [provider.key for provider in registry.PROVIDERS]
    country_index = {key: position for position, key in enumerate(countries)}
    variables = list(registry.all_variables())

    mappable = catalog[catalog[HAS_COORDS]]
    stations = [
        [
            row[GAUGE_ID],
            # 5 decimal places is ~1 m at the equator and saves a third of the file.
            round(float(row[LATITUDE]), 5),
            round(float(row[LONGITUDE]), 5),
            country_index[row[COUNTRY]],
            registry.variable_mask(row[VARIABLES]),
            # A missing name arrives as NaN once it has been through parquet, and
            # json.dumps writes that as a bare NaN, which no browser will parse.
            _clean_text(row[STATION_NAME]),
            _rounded(row[AREA], 1),
        ]
        for row in mappable.to_dict(orient="records")
    ]

    return {
        "version": 1,
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "countries": countries,
        "variables": variables,
        "fields": ["gauge_id", "lat", "lon", "country", "variable_mask", "station_name", "area"],
        "station_count": len(stations),
        "off_map_count": int((~catalog[HAS_COORDS]).sum()),
        "stations": stations,
    }


def build_catalog(verbose: bool = True) -> dict[str, Any]:
    """Runs the full build and writes every artefact. Returns the report."""
    paths.ensure_dirs()

    cores: list[pd.DataFrame] = []
    extras: list[pd.DataFrame] = []
    reports: list[ProviderReport] = []

    for provider in registry.PROVIDERS:
        try:
            core, extra, report = build_provider(provider)
            cores.append(core)
            extras.append(extra)
        except Exception as exc:
            report = ProviderReport(key=provider.key, label=provider.label, error=str(exc))
            logger.error("Failed to build %s: %s", provider.key, exc)
        reports.append(report)
        if verbose:
            if report.error:
                print(f"  {provider.key:16s} FAILED — {report.error}")
            else:
                named = f"{report.with_station_name}/{report.rows_out} named"
                print(
                    f"  {provider.key:16s} {report.rows_out:6d} stations  "
                    f"{report.missing_coordinates:4d} off-map  {named}"
                )
                for warning in report.warnings:
                    print(f"      ! {warning}")

    if not cores:
        raise RuntimeError("No provider produced any stations; nothing to write.")

    catalog = pd.concat(cores, ignore_index=True)
    extra_table = pd.concat(extras, ignore_index=True)

    catalog.to_parquet(paths.STATIONS_PARQUET, index=False)
    extra_table.to_parquet(paths.STATIONS_EXTRA_PARQUET, index=False)

    payload = build_map_payload(catalog)
    # separators without spaces shaves ~8% off a payload this repetitive.
    # allow_nan=False matters: Python happily writes a bare NaN, which is not
    # valid JSON and leaves the map stuck on "Loading the station catalog".
    # Failing the build here is far better than shipping a page that cannot parse.
    paths.STATIONS_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )

    summary = {
        "built_at": payload["built_at"],
        "providers": [asdict(report) for report in reports],
        "totals": {
            "providers_built": sum(1 for r in reports if not r.error),
            "providers_failed": sum(1 for r in reports if r.error),
            "rows_in": sum(r.rows_in for r in reports),
            "stations": len(catalog),
            "mappable": int(catalog[HAS_COORDS].sum()),
            "off_map": int((~catalog[HAS_COORDS]).sum()),
            "duplicates_dropped": sum(r.duplicates_dropped for r in reports),
            "reprojected": sum(r.reprojected for r in reports),
            "with_station_name": int(catalog[STATION_NAME].notna().sum()),
            "without_station_name": int(catalog[STATION_NAME].isna().sum()),
        },
        "artefacts": {
            "stations_parquet": str(paths.STATIONS_PARQUET),
            "stations_extra_parquet": str(paths.STATIONS_EXTRA_PARQUET),
            "stations_json": str(paths.STATIONS_JSON),
            "stations_json_bytes": paths.STATIONS_JSON.stat().st_size,
        },
    }
    paths.CATALOG_REPORT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    if verbose:
        totals = summary["totals"]
        size_mb = summary["artefacts"]["stations_json_bytes"] / 1_048_576
        print()
        print(f"  {totals['stations']:,} stations from {totals['providers_built']} providers")
        print(f"  {totals['mappable']:,} mappable, {totals['off_map']:,} without coordinates")
        print(f"  {totals['reprojected']:,} reprojected, {totals['duplicates_dropped']:,} duplicates dropped")
        print(f"  {totals['without_station_name']:,} stations still have no name")
        print(f"  map payload {size_mb:.1f} MB uncompressed -> {paths.STATIONS_JSON}")

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    build_catalog()
