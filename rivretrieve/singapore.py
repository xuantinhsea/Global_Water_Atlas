"""Fetcher for Singapore's rain gauge network from the National Environment Agency.

The NEA publishes five-minute rainfall from roughly 60 automatic gauges across
Singapore through the government's open data platform, data.gov.sg, with an
archive back to December 2016. No key is needed.

The service answers by day, for every gauge at once, so a day downloaded for
one gauge is kept on disk and reused for the others.
"""

import datetime
import gzip
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from . import base, constants, utils

logger = logging.getLogger(__name__)

V1_URL = "https://api.data.gov.sg/v1/environment/rainfall"
V2_URL = "https://api-open.data.gov.sg/v2/real-time/api/rainfall"

#: First day the archive answers.
ARCHIVE_START = "2016-12-01"

#: Most recent days of a request that are fetched. Each day is one ~0.8 MB
#: download covering every gauge; a decade would be thousands of them.
MAX_DAYS = 92

#: Five-minute readings needed for an hourly total (of 12) and a daily total (of 288).
MIN_READINGS_PER_HOUR = 11
MIN_READINGS_PER_DAY = 276

MIN_REQUEST_INTERVAL = 0.5

#: Past days are cached here; today's partial day never is.
CACHE_DIR = Path(
    os.environ.get("RIVRETRIEVE_SINGAPORE_CACHE", Path(tempfile.gettempdir()) / "rivretrieve_singapore_rain")
)

_pace_lock = threading.Lock()
_last_request = 0.0


def _pace() -> None:
    global _last_request
    with _pace_lock:
        wait = _last_request + MIN_REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def _empty(variable: str) -> pd.DataFrame:
    return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)


def _singapore_today() -> datetime.date:
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).date()


class SingaporeRainFetcher(base.RiverDataFetcher):
    """Fetches rain gauge totals for Singapore from the NEA via data.gov.sg.

    Data Source: National Environment Agency, Singapore,
    https://data.gov.sg/. No API key is required.

    Supported Variables:
        - ``constants.PRECIPITATION_HOURLY_SUM`` (mm)
        - ``constants.PRECIPITATION_DAILY_SUM`` (mm)

    .. note::
        Totals are sums of five-minute readings, kept only where at least
        :data:`MIN_READINGS_PER_HOUR` of 12 (hourly) or
        :data:`MIN_READINGS_PER_DAY` of 288 (daily) readings arrived, so a gap
        never shows up as a dry spell. Hours are labelled by their start and
        days are Singapore calendar days; timestamps are Singapore time
        (UTC+8), returned timezone-naive.

    .. warning::
        One call fetches at most the most recent :data:`MAX_DAYS` days of the
        requested range, and nothing before :data:`ARCHIVE_START`.
    """

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of Singapore rain gauges.

        This method loads the metadata from a cached CSV file located in
        the ``rivretrieve/cached_site_data/`` directory.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return utils.load_cached_metadata_csv("singapore_rain")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (constants.PRECIPITATION_HOURLY_SUM, constants.PRECIPITATION_DAILY_SUM)

    # -- the daily payload, shared by every gauge ----------------------------------

    def _day(self, day: datetime.date) -> dict[str, Any]:
        """``{"stations": [...], "readings": [(timestamp, {station: mm})]}`` for one day."""
        cache_file = CACHE_DIR / f"{day.isoformat()}.json.gz"
        if day < _singapore_today() and cache_file.exists():
            try:
                with gzip.open(cache_file, "rt", encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, ValueError):
                cache_file.unlink(missing_ok=True)

        try:
            payload = self._download_v1(day)
        except Exception as exc:
            logger.info("data.gov.sg v1 failed for %s (%s); trying v2", day, exc)
            payload = self._download_v2(day)

        if day < _singapore_today() and payload["readings"]:
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                with gzip.open(cache_file, "wt", encoding="utf-8") as handle:
                    json.dump(payload, handle)
            except OSError as exc:
                logger.debug("Could not cache %s: %s", cache_file, exc)
        return payload

    def _download_v1(self, day: datetime.date) -> dict[str, Any]:
        session = utils.requests_retry_session()
        _pace()
        response = session.get(V1_URL, params={"date": day.isoformat()}, timeout=120)
        response.raise_for_status()
        body = response.json()
        if "items" not in body:
            raise ValueError(f"unexpected v1 payload: {str(body)[:120]}")
        stations = [
            {"id": s["id"], "name": s.get("name"), **(s.get("location") or {})}
            for s in (body.get("metadata") or {}).get("stations", [])
        ]
        readings = [
            (item["timestamp"], {r["station_id"]: r["value"] for r in item.get("readings", [])})
            for item in body["items"]
        ]
        return {"stations": stations, "readings": readings}

    def _download_v2(self, day: datetime.date) -> dict[str, Any]:
        session = utils.requests_retry_session()
        stations: dict[str, dict] = {}
        readings = []
        token = None
        while True:
            _pace()
            params = {"date": day.isoformat(), **({"paginationToken": token} if token else {})}
            response = session.get(V2_URL, params=params, timeout=120)
            response.raise_for_status()
            data = response.json().get("data") or {}
            for s in data.get("stations", []):
                stations.setdefault(s["id"], {"id": s["id"], "name": s.get("name"), **(s.get("location") or {})})
            readings.extend(
                (item["timestamp"], {r["stationId"]: r["value"] for r in item.get("data", [])})
                for item in data.get("readings", [])
            )
            token = data.get("paginationToken")
            if not token:
                break
        return {"stations": list(stations.values()), "readings": readings}

    # -- metadata ----------------------------------------------------------------

    def get_metadata(self) -> pd.DataFrame:
        """Collects the gauge list from one day a year, so retired gauges are kept too.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        today = _singapore_today()
        days = [datetime.date(year, 1, 15) for year in range(2017, today.year + 1)]
        days.append(today - datetime.timedelta(days=1))
        seen: dict[str, dict] = {}
        for day in days:
            try:
                payload = self._day(day)
            except Exception as exc:
                logger.warning("Could not read the gauge list for %s: %s", day, exc)
                continue
            for station in payload["stations"]:
                record = seen.setdefault(station["id"], {**station, "first_seen": day.isoformat()})
                record.update({k: v for k, v in station.items() if v is not None})
                record["last_seen"] = day.isoformat()
        return self._parse_metadata(list(seen.values()))

    def _parse_metadata(self, stations: list[dict]) -> pd.DataFrame:
        records = [
            {
                constants.GAUGE_ID: s["id"],
                constants.STATION_NAME: (s.get("name") or "").strip() or None,
                constants.LATITUDE: pd.to_numeric(s.get("latitude"), errors="coerce"),
                constants.LONGITUDE: pd.to_numeric(s.get("longitude"), errors="coerce"),
                constants.COUNTRY: "Singapore",
                constants.SOURCE: "National Environment Agency (data.gov.sg)",
                "first_seen": s.get("first_seen"),
                "last_seen": s.get("last_seen"),
            }
            for s in stations
        ]
        frame = pd.DataFrame(records)
        if frame.empty:
            return frame
        return frame.set_index(constants.GAUGE_ID).sort_index()

    # -- data --------------------------------------------------------------------

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> list[tuple]:
        """Five-minute readings for one gauge, as ``(timestamp, mm)`` pairs."""
        points = []
        day = datetime.date.fromisoformat(start_date)
        last = datetime.date.fromisoformat(end_date)
        while day <= last:
            payload = self._day(day)
            points.extend((stamp, values[gauge_id]) for stamp, values in payload["readings"] if gauge_id in values)
            day += datetime.timedelta(days=1)
        return points

    def _parse_data(self, gauge_id: str, raw_data: list[tuple], variable: str) -> pd.DataFrame:
        if not raw_data:
            return _empty(variable)
        frame = pd.DataFrame(raw_data, columns=["stamp", "value"])
        # "2026-09-25T00:05:00+08:00": kept in Singapore time. A reading closes its
        # five minutes, so it is shifted back a second before bucketing.
        stamps = pd.to_datetime(frame["stamp"].str[:19], errors="coerce") - pd.Timedelta(seconds=1)
        values = pd.to_numeric(frame["value"], errors="coerce")
        series = pd.Series(values.to_numpy(), index=stamps).dropna()
        series = series[series >= 0]
        series = series[~series.index.duplicated(keep="first")]
        if series.empty:
            return _empty(variable)

        if variable == constants.PRECIPITATION_HOURLY_SUM:
            grouped = series.groupby(series.index.floor("1h"))
            totals = grouped.sum()[grouped.count() >= MIN_READINGS_PER_HOUR]
        else:
            grouped = series.groupby(series.index.normalize())
            totals = grouped.sum()[grouped.count() >= MIN_READINGS_PER_DAY]
        totals.index.name = constants.TIME_INDEX
        return totals.round(2).to_frame(name=variable)

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetches hourly or daily rainfall for one Singapore gauge.

        Args:
            gauge_id: The NEA station id, e.g. ``"S24"``.
            variable: One of ``get_available_variables()``.
            start_date: Optional start date in 'YYYY-MM-DD' format.
            end_date: Optional end date in 'YYYY-MM-DD' format.

        Returns:
            pd.DataFrame: A DataFrame indexed by ``constants.TIME_INDEX`` with a
            single column named after the requested ``variable``, in mm. Covers
            at most the last :data:`MAX_DAYS` days of the range.

        Raises:
            ValueError: If the requested ``variable`` is not supported.
            requests.exceptions.RequestException: If data.gov.sg cannot be reached.
        """
        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        start_date = utils.format_start_date(start_date)
        end_date = min(utils.format_end_date(end_date), _singapore_today().isoformat())
        earliest = max(
            ARCHIVE_START,
            (datetime.date.fromisoformat(end_date) - datetime.timedelta(days=MAX_DAYS - 1)).isoformat(),
        )
        if start_date < earliest:
            logger.info(
                "Singapore rainfall: fetching %s to %s (at most the last %d days).", earliest, end_date, MAX_DAYS
            )
            start_date = earliest
        if start_date > end_date:
            return _empty(variable)

        raw_data = self._download_data(gauge_id, variable, start_date, end_date)
        frame = self._parse_data(gauge_id, raw_data, variable)
        if frame.empty:
            return frame
        upper = pd.Timestamp(end_date) + datetime.timedelta(days=1)
        return frame[(frame.index >= pd.Timestamp(start_date)) & (frame.index < upper)]
