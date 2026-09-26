"""Vercel build step for the hosted Global Water Atlas.

Vercel installs the dependencies listed in pyproject.toml and then runs this
script (see ``[tool.vercel.scripts]`` there). It builds the two things the
function needs that are deliberately not in git: the front-end bundle and the
station catalog.

Run it locally the same way to check a deployment will build:

    python scripts/vercel_build.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "wateratlas" / "frontend"

#: Where Vercel's build creates the virtual environment it installs dependencies into.
VERCEL_VENV_PYTHON = ROOT / ".vercel" / "python" / ".venv" / "bin" / "python"


def ensure_dependencies() -> None:
    """Re-runs this script with Vercel's virtual environment if the current Python lacks the dependencies.

    The catalog build imports pandas and the fetchers, which Vercel installs into
    its own virtual environment; the Build Command is not guaranteed to run there.
    """
    try:
        import pandas  # noqa: F401
    except ImportError:
        if VERCEL_VENV_PYTHON.exists() and Path(sys.executable).resolve() != VERCEL_VENV_PYTHON.resolve():
            print(f"Dependencies not importable from {sys.executable}; re-running with {VERCEL_VENV_PYTHON}", flush=True)
            os.execv(str(VERCEL_VENV_PYTHON), [str(VERCEL_VENV_PYTHON), __file__, *sys.argv[1:]])
        raise


def run(*command: str, cwd: Path) -> None:
    print(f"$ {' '.join(command)}  (in {cwd.relative_to(ROOT) or '.'})", flush=True)
    # npm is npm.cmd on Windows, which subprocess only finds through its full path.
    executable = shutil.which(command[0]) or command[0]
    subprocess.run([executable, *command[1:]], cwd=cwd, check=True)


def main() -> int:
    ensure_dependencies()
    print(f"Python {sys.version.split()[0]} at {sys.executable}", flush=True)
    run("npm", "ci", "--no-audit", "--no-fund", cwd=FRONTEND)
    run("npm", "run", "build", cwd=FRONTEND)

    sys.path.insert(0, str(ROOT))
    from wateratlas.catalog_build import build_catalog  # noqa: E402

    print("Building the station catalog ...", flush=True)
    summary = build_catalog(verbose=True)
    failed = [p["key"] for p in summary["providers"] if p["error"]]
    if failed:
        print(f"Catalog build failed for: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
