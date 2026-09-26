"""Vercel build step for the hosted Global Water Atlas.

Vercel installs the dependencies listed in pyproject.toml and then runs this
script (see ``[tool.vercel.scripts]`` there). It builds the two things the
function needs that are deliberately not in git: the front-end bundle and the
station catalog.

Run it locally the same way to check a deployment will build:

    python scripts/vercel_build.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "wateratlas" / "frontend"


def run(*command: str, cwd: Path) -> None:
    print(f"$ {' '.join(command)}  (in {cwd.relative_to(ROOT) or '.'})", flush=True)
    # npm is npm.cmd on Windows, which subprocess only finds through its full path.
    executable = shutil.which(command[0]) or command[0]
    subprocess.run([executable, *command[1:]], cwd=cwd, check=True)


def main() -> int:
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
