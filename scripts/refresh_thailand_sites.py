"""Regenerates the cached Thai station lists from the ThaiWater Open API.

Usage:
    python scripts/refresh_thailand_sites.py

Writes ``rivretrieve/cached_site_data/thailand_sites.csv`` (water level and
discharge telemetry) and ``thailand_rain_sites.csv`` (rain gauges).

The two networks share an id space — 475 station ids appear in both — which is
why they are two separate files and two separate fetchers.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rivretrieve import ThailandFetcher, ThailandRainFetcher, constants  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "rivretrieve" / "cached_site_data"


def refresh(fetcher, filename: str) -> int:
    label = type(fetcher).__name__
    print(f"{label}: downloading station list ...")
    frame = fetcher.get_metadata()
    if frame.empty:
        print(f"{label}: the provider returned no stations; leaving the cached file alone.")
        return 0

    located = frame[constants.LATITUDE].notna() & frame[constants.LONGITUDE].notna()
    named = frame[constants.STATION_NAME].notna()
    path = OUTPUT_DIR / filename
    frame.to_csv(path, encoding="utf-8")

    print(f"{label}: {len(frame):,} stations -> {path.name}")
    print(f"  with coordinates: {int(located.sum()):,}")
    print(f"  with a name:      {int(named.sum()):,}")
    return len(frame)


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    total = refresh(ThailandFetcher(), "thailand_sites.csv")
    total += refresh(ThailandRainFetcher(), "thailand_rain_sites.csv")
    print(f"\n{total:,} Thai stations cached.")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
