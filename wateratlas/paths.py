"""Filesystem locations used by the atlas.

Everything the app builds or caches lives under ``wateratlas/catalog_data/``
(gitignored) so a clean checkout never carries derived data.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent

#: True when running as a Vercel Function. The deployment is read-only except
#: for /tmp, and no process lives long enough to run a background download job,
#: so a few features change shape there (see ``registry.Provider.blocked_reason``).
HOSTED = bool(os.environ.get("VERCEL"))

#: For responses whose URL names one exact version (hashed assets, the versioned
#: map payload). ``s-maxage`` is what lets Vercel's CDN keep a function's response.
IMMUTABLE_CACHE = "public, max-age=31536000, s-maxage=31536000, immutable"

# Where rivretrieve keeps its own lazily-downloaded bulk caches (HYDAT, poland.zarr).
RIVRETRIEVE_DIR = REPO_ROOT / "rivretrieve"
RIVRETRIEVE_DATA_DIR = RIVRETRIEVE_DIR / "data"
CACHED_SITE_DATA_DIR = RIVRETRIEVE_DIR / "cached_site_data"

# Derived artefacts produced by `wateratlas build-catalog`.
CATALOG_DIR = Path(os.environ.get("WATERATLAS_CATALOG_DIR", PACKAGE_DIR / "catalog_data"))

STATIONS_PARQUET = CATALOG_DIR / "stations.parquet"
STATIONS_EXTRA_PARQUET = CATALOG_DIR / "stations_extra.parquet"
STATIONS_JSON = CATALOG_DIR / "stations.json"
CATALOG_REPORT_JSON = CATALOG_DIR / "catalog_report.json"

# Runtime state: learned variable availability, download jobs, series cache.
# Hosted, /tmp is the only writable place, and it lasts only as long as the instance.
_DEFAULT_STATE_DIR = Path("/tmp/wateratlas/state") if HOSTED else PACKAGE_DIR / "state"
STATE_DIR = Path(os.environ.get("WATERATLAS_STATE_DIR", _DEFAULT_STATE_DIR))
AVAILABILITY_DB = STATE_DIR / "availability.sqlite3"
JOBS_DIR = STATE_DIR / "jobs"
PREVIEW_CACHE_DIR = STATE_DIR / "preview_cache"

# Built front end, served by FastAPI in production mode.
FRONTEND_DIR = PACKAGE_DIR / "frontend"
FRONTEND_DIST = FRONTEND_DIR / "dist"


def ensure_dirs() -> None:
    """Creates every directory the app writes to.

    Existing directories are left alone rather than re-created: hosted, the
    catalog directory ships inside a read-only bundle.
    """
    for directory in (CATALOG_DIR, STATE_DIR, JOBS_DIR, PREVIEW_CACHE_DIR):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
