"""Regenerates the index of GRDC-Caravan, GRDC's openly licensed discharge dataset.

Usage:
    python scripts/refresh_grdc_caravan_index.py

GRDC-Caravan (Färber et al., 2025, https://doi.org/10.5281/zenodo.15349031,
CC BY 4.0) holds GRDC's own daily discharge for the 5,356 stations whose
owners allow open release, 1950-2023, as one CSV per station inside an 8.8 GB
ZIP on Zenodo. ``GRDCFetcher`` reads a single station's CSV with one HTTP range
request; this script records where each one sits in the archive.

Writes ``rivretrieve/cached_site_data/grdc_caravan_index.csv``: GRDC number,
the member's byte offset, compressed size and compression method, and the
catchment area that converts Caravan's mm/day back to m³/s.

The offsets belong to one Zenodo record. When GRDC publishes a new version,
point ``rivretrieve.grdc.GRDC_CARAVAN_URL`` at it and run this again.
"""

import io
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rivretrieve import grdc, utils  # noqa: E402

OUTPUT = Path(__file__).resolve().parent.parent / "rivretrieve" / "cached_site_data" / "grdc_caravan_index.csv"
SERIES_FOLDER = "/timeseries/csv/grdc/"
AREAS = "attributes/grdc/attributes_other_grdc.csv"
CHECKS = "attributes/grdc/attributes_additional_grdc.csv"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    session = utils.requests_retry_session()
    archive = utils.RemoteZip(grdc.GRDC_CARAVAN_URL, session)
    logging.info("%s: %d members", grdc.GRDC_CARAVAN_URL, len(archive.members))

    def member(suffix: str) -> str:
        return next(name for name in archive.members if name.endswith(suffix))

    areas = pd.read_csv(io.BytesIO(archive.read(member(AREAS))), usecols=["gauge_id", "area"])
    areas = areas.set_index(areas["gauge_id"].str.removeprefix("GRDC_"))["area"]

    rows = []
    for name, info in archive.members.items():
        if SERIES_FOLDER not in name or not name.endswith(".csv"):
            continue
        gauge_id = name.rsplit("/", 1)[-1].removeprefix("GRDC_").removesuffix(".csv")
        rows.append(
            {
                "gauge_id": gauge_id,
                "header_offset": info.header_offset,
                "compress_size": info.compress_size,
                "compress_type": info.compress_type,
                "area_km2": areas.get(gauge_id),
            }
        )
    index = pd.DataFrame(rows).sort_values("gauge_id")
    missing = index["area_km2"].isna().sum()
    if missing:
        logging.warning("%d stations have no catchment area and cannot be converted to m³/s", missing)

    # Spot-check the unit conversion against GRDC's own long-term mean discharge.
    checks = pd.read_csv(io.BytesIO(archive.read(member(CHECKS))), usecols=["gauge_id", "lta_discharge"])
    checks = checks.set_index(checks["gauge_id"].str.removeprefix("GRDC_"))["lta_discharge"]
    for row in index.sample(3, random_state=1).itertuples():
        table = pd.read_csv(
            io.BytesIO(
                utils.read_zip_member(
                    session, grdc.GRDC_CARAVAN_URL, row.header_offset, row.compress_size, row.compress_type
                )
            ),
            usecols=["date", "streamflow"],
        )
        mean_m3s = table["streamflow"].mean() * row.area_km2 / grdc.MM_PER_DAY_TO_M3S_PER_KM2
        logging.info(
            "GRDC %s: Caravan mean %.2f m³/s, GRDC long-term mean %.2f m³/s",
            row.gauge_id,
            mean_m3s,
            checks.get(row.gauge_id, float("nan")),
        )

    index.to_csv(OUTPUT, index=False)
    logging.info("Wrote %d stations to %s", len(index), OUTPUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
