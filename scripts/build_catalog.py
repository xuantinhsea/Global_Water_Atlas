"""Builds the unified station catalog used by the Global Water Atlas front end.

Usage:
    python scripts/build_catalog.py

The work lives in ``wateratlas.catalog_build``; this is the command-line entry point.
"""

import logging
import sys
from pathlib import Path

# Run from a plain checkout without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wateratlas.catalog_build import build_catalog  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    print("Building station catalog from rivretrieve/cached_site_data/ ...")
    summary = build_catalog(verbose=True)
    return 0 if summary["totals"]["providers_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
