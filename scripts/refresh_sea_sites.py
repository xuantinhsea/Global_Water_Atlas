"""Regenerates the cached station lists for the Southeast Asian providers.

Usage:
    python scripts/refresh_sea_sites.py [thailand|mrc|philippines ...]

Writes into ``rivretrieve/cached_site_data/``:

``thailand_sites.csv``       ThaiWater telemetry water level gauges
``thailand_rain_sites.csv``  ThaiWater rain gauges
``mrc_sites.csv``            Mekong River Commission telemetry network
``philippines_sites.csv``    DOST-ASTI PhilSensors, all four station groups

None of these needs credentials: every station catalogue used here is open,
even where the readings behind it are not.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rivretrieve import (  # noqa: E402
    MRCFetcher,
    PagasaDamFetcher,
    PagasaStationFetcher,
    PhilippinesFetcher,
    ThailandFetcher,
    ThailandRainFetcher,
    constants,
)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "rivretrieve" / "cached_site_data"

TARGETS = {
    "thailand": (ThailandFetcher, "thailand_sites.csv"),
    "thailand_rain": (ThailandRainFetcher, "thailand_rain_sites.csv"),
    "mrc": (MRCFetcher, "mrc_sites.csv"),
    "philippines": (PhilippinesFetcher, "philippines_sites.csv"),
    "pagasa_dams": (PagasaDamFetcher, "pagasa_dams_sites.csv"),
    "pagasa_stations": (PagasaStationFetcher, "pagasa_stations_sites.csv"),
}


def refresh(fetcher_class, filename: str) -> int:
    label = fetcher_class.__name__
    print(f"{label}: downloading station list ...")
    try:
        frame = fetcher_class().get_metadata()
    except Exception as exc:
        print(f"{label}: FAILED — {exc}")
        return 0

    if frame is None or frame.empty:
        print(f"{label}: the provider returned no stations; leaving the cached file alone.")
        return 0

    located = frame[constants.LATITUDE].notna() & frame[constants.LONGITUDE].notna()
    named = frame[constants.STATION_NAME].notna()
    path = OUTPUT_DIR / filename
    frame.to_csv(path, encoding="utf-8")

    print(f"{label}: {len(frame):,} stations -> {path.name}")
    print(f"  with coordinates: {int(located.sum()):,}")
    print(f"  with a name:      {int(named.sum()):,}")
    if constants.COUNTRY in frame.columns:
        counts = frame[constants.COUNTRY].value_counts()
        if len(counts) > 1:
            print("  by country:       " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return len(frame)


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    wanted = sys.argv[1:] or list(TARGETS)
    unknown = [name for name in wanted if name not in TARGETS]
    if unknown:
        print(f"Unknown target(s): {', '.join(unknown)}. Choose from: {', '.join(TARGETS)}")
        return 2

    total = 0
    for name in wanted:
        fetcher_class, filename = TARGETS[name]
        total += refresh(fetcher_class, filename)
        print()
    print(f"{total:,} stations cached.")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())

