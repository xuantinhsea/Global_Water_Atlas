"""Regenerates the cached station lists for the global tide gauge providers.

Usage:
    python scripts/refresh_sealevel_sites.py [ioc_sealevel|uhslc ...]

Writes into ``rivretrieve/cached_site_data/``:

``ioc_sealevel_sites.csv``  IOC Sea Level Station Monitoring Facility (real time, raw)
``uhslc_sites.csv``         University of Hawaii Sea Level Center (hourly/daily, checked)

Both catalogues are open; no key is needed.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rivretrieve import IOCSeaLevelFetcher, UHSLCFetcher, constants  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "rivretrieve" / "cached_site_data"

TARGETS = {
    "ioc_sealevel": (IOCSeaLevelFetcher, "ioc_sealevel_sites.csv"),
    "uhslc": (UHSLCFetcher, "uhslc_sites.csv"),
}

#: Rough box around Southeast Asia, for the summary line only.
SEA_BOX = {"lat": (-11.5, 24.5), "lon": (92.0, 142.0)}


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

    path = OUTPUT_DIR / filename
    frame.to_csv(path, encoding="utf-8")
    lat, lon = frame[constants.LATITUDE], frame[constants.LONGITUDE]
    located = lat.notna() & lon.notna()
    in_sea = lat.between(*SEA_BOX["lat"]) & lon.between(*SEA_BOX["lon"])
    print(f"{label}: {len(frame):,} stations -> {path.name}")
    print(f"  with coordinates: {int(located.sum()):,}")
    print(f"  in Southeast Asia: {int(in_sea.sum()):,}")
    return len(frame)


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    wanted = sys.argv[1:] or list(TARGETS)
    unknown = [name for name in wanted if name not in TARGETS]
    if unknown:
        print(f"Unknown target(s): {', '.join(unknown)}. Choose from: {', '.join(TARGETS)}")
        return 2
    total = sum(refresh(*TARGETS[name]) for name in wanted)
    print(f"{total:,} stations cached.")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
