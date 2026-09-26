"""FastAPI application for the Global Water Atlas.

Run it with::

    python -m wateratlas            # or: wateratlas serve

In development the Vite dev server proxies ``/api`` here. In production the
built front end is served from ``wateratlas/frontend/dist``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, availability, paths
from .api import router
from .catalog import catalog

logger = logging.getLogger(__name__)

# The Vite dev server. Only these origins may call the API in development.
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    paths.ensure_dirs()
    availability.init()
    if catalog.is_built:
        totals = catalog.totals()
        logger.info(
            "Catalog ready: %s stations, %s mappable", totals["stations"], totals["mappable"]
        )
    else:
        logger.warning(
            "No catalog found at %s. Run: python scripts/build_catalog.py", paths.STATIONS_PARQUET
        )
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Global Water Atlas",
        version=__version__,
        description=(
            "Browse and download observed river, rainfall, reservoir and coastal water data "
            "from around the world, via the RivRetrieve library."
        ),
        lifespan=lifespan,
    )

    # The map payload is ~3.5 MB of highly repetitive JSON; gzip takes it under 1 MB.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.get("/healthz")
    def healthz():
        return {
            "status": "ok",
            "version": __version__,
            "catalog_built": catalog.is_built,
        }

    if paths.FRONTEND_DIST.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=paths.FRONTEND_DIST / "assets"),
            name="assets",
        )

        @app.get("/{full_path:path}")
        def serve_spa(full_path: str):
            """Serves the built single-page app, falling back to index.html."""
            candidate = (paths.FRONTEND_DIST / full_path).resolve()
            dist = paths.FRONTEND_DIST.resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(dist):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
    else:

        @app.get("/")
        def no_frontend():
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "The front end has not been built. Run 'npm install && npm run build' in "
                        f"{paths.FRONTEND_DIR}, or use the Vite dev server on port 5173."
                    )
                },
            )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run("wateratlas.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
