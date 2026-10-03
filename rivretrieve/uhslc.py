"""Fetcher for tide gauge sea level from the University of Hawaii Sea Level Center.

UHSLC assembles hourly and daily sea level from tide gauges worldwide in two
products: *research quality* records, checked and corrected by hand and
reaching back to the 1800s at the longest-running stations, and *fast
delivery* records, given a lighter check and released within one to two
months. Both are served through the centre's open ERDDAP server.

Across Southeast Asia that means quality-controlled hourly records for gauges
such as Manila, Legaspi, Davao, Vung Tau, Qui Nhon, Ko Lak, Ko Taphao Noi,
Langkawi, Tanjong Pagar, Sibolga, Padang, Benoa and Bitung.

Cite: Caldwell, P. C., M. A. Merrifield and P. R. Thompson (2015), Sea level
measured by tide gauges from global oceans — the Joint Archive for Sea Level
holdings, NOAA National Centers for Environmental Information.
"""

import datetime
import io
import logging
import threading
import time
from typing import Optional

import pandas as pd
import requests

from . import base, constants, utils

logger = logging.getLogger(__name__)

ERDDAP_URL = "https://uhslc.soest.hawaii.edu/erddap/tabledap"
#: Station list with each product's date span; one small file, unlike ERDDAP's
#: per-station min/max query, which times out across the whole archive.
META_URL = "https://uhslc.soest.hawaii.edu/data/meta.geojson"

#: variable -> (research quality dataset, fast delivery dataset).
DATASETS: dict[str, tuple[str, str]] = {
    constants.STAGE_HOURLY_MEAN: ("global_hourly_rqds", "global_hourly_fast"),
    constants.STAGE_DAILY_MEAN: ("global_daily_rqds", "global_daily_fast"),
}

MIN_REQUEST_INTERVAL = 0.5

_pace_lock = threading.Lock()
_last_request = 0.0


def _pace() -> None:
    """Blocks until at least ``MIN_REQUEST_INTERVAL`` has passed since the last request."""
    global _last_request
    with _pace_lock:
        wait = _last_request + MIN_REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def _empty(variable: str) -> pd.DataFrame:
    return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)


def _read_erddap_csv(text: str) -> pd.DataFrame:
    """ERDDAP's CSV has a second header row of units, which is skipped."""
    return pd.read_csv(io.StringIO(text), skiprows=[1])


class UHSLCFetcher(base.RiverDataFetcher):
    """Fetches hourly and daily tide gauge sea level from UHSLC.

    Data Source: University of Hawaii Sea Level Center,
    https://uhslc.soest.hawaii.edu/. No API key is required.

    Supported Variables:
        - ``constants.STAGE_HOURLY_MEAN`` (m) — UHSLC's hourly sea level
        - ``constants.STAGE_DAILY_MEAN`` (m) — UHSLC's filtered daily mean

    .. note::
        Research quality values are used wherever they exist and fast delivery
        values fill in after them, so a series is checked data up to each
        station's last research quality release (typically the end of the
        year before last) and lightly checked data from there to one or two
        months ago. ``frame.attrs["sources"]`` says which products contributed.

    .. note::
        Heights are relative to each station's UHSLC zero, not to a common
        datum, so compare a station only with itself. UHSLC's hourly values are
        filtered hourly heights; they are offered as ``STAGE_HOURLY_MEAN``
        because that is the closest standard variable. Timestamps are UTC,
        returned timezone-naive.
    """

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of UHSLC tide gauges.

        This method loads the metadata from a cached CSV file located in
        the ``rivretrieve/cached_site_data/`` directory.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return utils.load_cached_metadata_csv("uhslc")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (constants.STAGE_HOURLY_MEAN, constants.STAGE_DAILY_MEAN)

    # -- metadata ----------------------------------------------------------------

    def _query(self, session: requests.Session, dataset: str, query: str) -> pd.DataFrame:
        _pace()
        response = session.get(f"{ERDDAP_URL}/{dataset}.csv?{query}", timeout=180)
        if response.status_code == 404:
            # ERDDAP's way of saying "no rows match", not a missing dataset.
            return pd.DataFrame()
        response.raise_for_status()
        return _read_erddap_csv(response.text)

    def get_metadata(self) -> pd.DataFrame:
        """Downloads UHSLC's station file, which carries each product's date span.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        session = utils.requests_retry_session()
        response = session.get(META_URL, timeout=120)
        response.raise_for_status()
        return self._parse_metadata(response.json())

    def _parse_metadata(self, payload: dict) -> pd.DataFrame:
        records = []
        for feature in (payload or {}).get("features", []):
            props = feature.get("properties") or {}
            coordinates = (feature.get("geometry") or {}).get("coordinates") or [None, None]
            if props.get("uhslc_id") is None:
                continue
            research = props.get("rq_span") or {}
            fast = props.get("fd_span") or {}
            longitude = pd.to_numeric(coordinates[0], errors="coerce")
            records.append(
                {
                    constants.GAUGE_ID: str(int(props["uhslc_id"])),
                    constants.STATION_NAME: (props.get("name") or "").strip() or None,
                    constants.LATITUDE: pd.to_numeric(coordinates[1], errors="coerce"),
                    # Coordinates come as 0-360 east at some stations; the atlas uses -180..180.
                    constants.LONGITUDE: ((longitude + 180.0) % 360.0) - 180.0 if pd.notna(longitude) else None,
                    constants.COUNTRY: (props.get("country") or "").strip() or None,
                    constants.SOURCE: "University of Hawaii Sea Level Center",
                    "country_code": props.get("country_code_alpha"),
                    "uhslc_code": props.get("uhslc_code"),
                    "gloss_id": props.get("gloss_id"),
                    "timezone": props.get("timezone_name"),
                    "research_quality_from": research.get("oldest"),
                    "research_quality_to": research.get("latest"),
                    "fast_delivery_from": fast.get("oldest"),
                    "fast_delivery_to": fast.get("latest"),
                    "station_url": f"https://uhslc.soest.hawaii.edu/stations/?stn={int(props['uhslc_id']):03d}",
                }
            )
        frame = pd.DataFrame(records)
        if frame.empty:
            return frame
        frame = frame.drop_duplicates(subset=[constants.GAUGE_ID])
        return frame.set_index(constants.GAUGE_ID).sort_index(key=lambda ids: ids.astype(int))

    # -- data --------------------------------------------------------------------

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> dict[str, pd.DataFrame]:
        """Downloads the requested range from both products."""
        session = utils.requests_retry_session()
        query = (
            f"time,sea_level,record_id&uhslc_id={int(gauge_id)}"
            f"&time>={start_date}T00:00:00Z&time<={end_date}T23:59:59Z"
        )
        research, fast = DATASETS[variable]
        return {"research": self._query(session, research, query), "fast": self._query(session, fast, query)}

    def _parse_data(self, gauge_id: str, raw_data: dict[str, pd.DataFrame], variable: str) -> pd.DataFrame:
        """Prefers research quality values and lets fast delivery fill only later times."""
        parts = []
        for product in ("research", "fast"):
            frame = raw_data.get(product)
            if frame is None or frame.empty or not {"time", "sea_level"} <= set(frame.columns):
                continue
            frame = frame.assign(
                time=pd.to_datetime(frame["time"], errors="coerce", utc=True).dt.tz_localize(None),
                sea_level=pd.to_numeric(frame["sea_level"], errors="coerce"),
            ).dropna(subset=["time", "sea_level"])
            if "record_id" in frame.columns:
                # Two sensor records can overlap; the newest record wins.
                frame = frame.sort_values("record_id", ascending=False)
            frame = frame.drop_duplicates(subset="time", keep="first")
            if variable == constants.STAGE_DAILY_MEAN:
                frame = frame.assign(time=frame["time"].dt.normalize())
            parts.append((product, frame.set_index("time")["sea_level"].sort_index() / 1000.0))

        if not parts:
            return _empty(variable)

        series = parts[0][1]
        sources = [parts[0][0]]
        for product, extra in parts[1:]:
            later = extra[extra.index > series.index.max()] if not series.empty else extra
            if not later.empty:
                series = pd.concat([series, later])
                sources.append(product)

        series.index.name = constants.TIME_INDEX
        result = series.to_frame(name=variable)
        result.attrs["sources"] = sources
        result.attrs["datum"] = "UHSLC station zero"
        return result

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetches hourly or daily sea level for one UHSLC station.

        Args:
            gauge_id: The UHSLC station number, e.g. ``"370"`` for Manila.
            variable: One of ``get_available_variables()``.
            start_date: Optional start date in 'YYYY-MM-DD' format.
            end_date: Optional end date in 'YYYY-MM-DD' format.

        Returns:
            pd.DataFrame: A DataFrame indexed by ``constants.TIME_INDEX`` with a
            single column named after the requested ``variable``, in metres.

        Raises:
            ValueError: If the requested ``variable`` is not supported.
            requests.exceptions.RequestException: If UHSLC cannot be reached.
        """
        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        start_date = utils.format_start_date(start_date)
        end_date = utils.format_end_date(end_date)
        raw_data = self._download_data(gauge_id, variable, start_date, end_date)
        frame = self._parse_data(gauge_id, raw_data, variable)
        if frame.empty:
            return frame
        upper = pd.Timestamp(end_date) + datetime.timedelta(days=1)
        clipped = frame[(frame.index >= pd.Timestamp(start_date)) & (frame.index < upper)]
        clipped.attrs.update(frame.attrs)
        return clipped
