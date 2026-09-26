"""Regenerates the cached NOAA Tides & Currents station list.

Usage:
    python scripts/refresh_noaa_tides_sites.py

Writes ``rivretrieve/cached_site_data/noaa_tides_sites.csv``: every NOAA CO-OPS
station that has recorded water levels, active or historic, plus the water
temperature stations. Three requests to the open Metadata API; no key needed.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rivretrieve import NOAATidesFetcher, constants  # noqa: E402

OUTPUT = Path(__file__).resolve().parent.parent / "rivretrieve" / "cached_site_data" / "noaa_tides_sites.csv"


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    print("NOAATidesFetcher: downloading station lists ...")
    frame = NOAATidesFetcher().get_metadata()
    if frame is None or frame.empty:
        print("NOAA returned no stations; leaving the cached file alone.")
        return 1

    frame.to_csv(OUTPUT, encoding="utf-8")
    located = frame[constants.LATITUDE].notna() & frame[constants.LONGITUDE].notna()
    print(f"{len(frame):,} stations -> {OUTPUT.name}")
    print(f"  with coordinates: {int(located.sum()):,}")
    print("  by status:        " + ", ".join(f"{k} {v}" for k, v in frame["status"].value_counts().items()))
    print("  default datum:    " + ", ".join(f"{k} {v}" for k, v in frame["default_datum"].value_counts().items()))
    for variable in NOAATidesFetcher.get_available_variables():
        print(f"  {variable:32s} {int(frame[variable].sum()):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
