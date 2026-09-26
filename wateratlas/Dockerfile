# Global Water Atlas
#
# Build from the repository root:
#     docker build -f wateratlas/Dockerfile -t global-water-atlas .
#     docker run -p 8000:8000 \
#         -v atlas-state:/data \
#         -v atlas-bulk:/app/rivretrieve/data \
#         --env-file rivretrieve/.env \
#         gauge-atlas
#
# Before running this anywhere shared, read each provider's terms of use. A
# hosted instance concentrates every user's requests on one IP address, and
# several providers throttle by IP or restrict redistribution.

# ---- build the front end -------------------------------------------------
FROM node:24-slim AS frontend

WORKDIR /build
COPY wateratlas/frontend/package*.json ./
RUN npm ci
COPY wateratlas/frontend/ ./
RUN npm run build

# ---- runtime -------------------------------------------------------------
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Jobs, learned availability and the preview cache belong on a volume.
    # The catalog does not: it is derived from CSVs that ship in the image, so
    # it is baked in below and stays in sync with them.
    WATERATLAS_STATE_DIR=/data/state

WORKDIR /app

COPY requirements.txt setup.py README.md ./
COPY rivretrieve/ ./rivretrieve/
COPY wateratlas/ ./wateratlas/
COPY scripts/ ./scripts/

RUN pip install --no-cache-dir -e ".[app]"

COPY --from=frontend /build/dist ./wateratlas/frontend/dist

RUN python scripts/build_catalog.py

# /data              — jobs, availability database, preview cache
# rivretrieve/data   — the HYDAT database and Poland's Zarr store, so a restart
#                      does not re-download hundreds of megabytes
VOLUME ["/data", "/app/rivretrieve/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["python", "-m", "uvicorn", "wateratlas.main:app", "--host", "0.0.0.0", "--port", "8000"]
