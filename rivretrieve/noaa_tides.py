"""Fetcher for coastal, estuarine and Great Lakes water levels from NOAA CO-OPS.

NOAA's Center for Operational Oceanographic Products and Services (CO-OPS) runs
the National Water Level Observation Network: the tide gauges along the US
coasts, estuaries and tidal rivers, and the Great Lakes water level gauges. The
stations are the ones shown on https://tidesandcurrents.noaa.gov/map/.

Two services are used, neither of which needs a key:

- the Metadata API (``mdapi``) for the station lists, and
- the Data API (``datagetter``) for the observations.

Only observations are exposed. Tide *predictions*, currents and meteorological
products are left out: predictions are a harmonic model rather than a
measurement, and the others are not water level or water temperature.
"""

import functools
import logging
import re
import threading
import time
from typing import Any, Optional

import pandas as pd
import requests

from . import base, constants, utils

logger = logging.getLogger(__name__)

DATA_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
STATIONS_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"

#: NOAA asks callers to identify themselves with this parameter.
APPLICATION = "RivRetrieve"

REQUEST_TIMEOUT = 90

#: Minimum gap, in seconds, between two requests to the Data API from this
#: process. NOAA's gateway answers a burst with HTTP 403 "Forbidden" and keeps
#: refusing for several minutes — ten parallel clients were enough to trigger
#: it — so every request, from every fetcher instance and thread, is paced here.
MIN_REQUEST_INTERVAL = 0.5

#: Status codes NOAA's gateway uses to turn a client away for sending too much.
THROTTLE_STATUSES = (403, 429)

#: 6-minute water level and water temperature are archived from the mid-1990s:
#: across long-record stations (The Battery, San Francisco, Seattle, Key West,
#: Nawiliwili) the earliest 6-minute data found was 1995. Earlier ranges are not
#: requested at all, which saves one empty request per month. The margin below
#: 1995 is deliberate.
SIX_MINUTE_RECORD_START = pd.Timestamp("1990-01-01")

#: A daily mean of a tide gauge is only meaningful over a whole day: a partial
#: day samples an arbitrary part of the tidal cycle. NOAA derives its own daily
#: means from all 24 hourly heights, and so does this fetcher.
HOURS_PER_DAY = 24

#: Vertical datums the Data API accepts.
DATUMS = ("CRD", "IGLD", "LWD", "MHHW", "MHW", "MTL", "MSL", "MLW", "MLLW", "NAVD", "STND")

#: Station datum. Every station with water level data has it, so it is the fallback.
STATION_DATUM = "STND"

_NO_DATA = re.compile(r"no data was found", re.IGNORECASE)
#: NOAA words a missing datum two ways: "There is no MLLW for the station: 1495000"
#: and, at some stations, "The supported Datum values are: MHHW, MHW, NAVD, HWI".
_MISSING_DATUM = re.compile(r"there is no \w+ for the station|supported datum values are", re.IGNORECASE)

#: variable -> (CO-OPS product, maximum days per request, is 6-minute data).
#: The day limits are the Data API's own; a longer range is rejected outright.
_PRODUCTS: dict[str, tuple[str, int, bool]] = {
    constants.STAGE_INSTANT: ("water_level", 31, True),
    constants.STAGE_DAILY_MEAN: ("hourly_height", 365, False),
    constants.STAGE_MONTHLY_MEAN: ("monthly_mean", 200 * 365, False),
    constants.WATER_TEMPERATURE_INSTANT: ("water_temperature", 31, True),
}

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


def _windows(start: pd.Timestamp, end: pd.Timestamp, days: int):
    """Splits ``[start, end]`` into consecutive whole-day windows of at most ``days`` days."""
    current = start
    while current <= end:
        stop = min(current + pd.Timedelta(days=days - 1), end)
        yield current, stop
        current = stop + pd.Timedelta(days=1)


def _empty(variable: str) -> pd.DataFrame:
    return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)


def _date_text(value: Any) -> Optional[str]:
    """``"1920-05-24 00:00:00"`` -> ``"1920-05-24"``; blanks become None."""
    text = str(value or "").strip()
    return text[:10] if text else None


def _default_datum(tidal: bool, greatlakes: bool) -> str:
    """The datum a station's water levels are most usefully reported against.

    Tide gauges are charted against Mean Lower Low Water and the Great Lakes
    against the International Great Lakes Datum; neither exists at the handful
    of non-tidal coastal and river stations, which only have their station datum.
    """
    if greatlakes:
        return "IGLD"
    if tidal:
        return "MLLW"
    return STATION_DATUM


@functools.lru_cache(maxsize=1)
def _sites() -> pd.DataFrame:
    """The cached station list, loaded once per process for datum and date lookups."""
    try:
        return utils.load_cached_metadata_csv("noaa_tides")
    except FileNotFoundError:
        return pd.DataFrame()


def _site(gauge_id: str) -> Optional[pd.Series]:
    sites = _sites()
    if gauge_id in sites.index:
        return sites.loc[gauge_id]
    return None


class NOAATidesFetcher(base.RiverDataFetcher):
    """Fetches water level and water temperature from NOAA Tides & Currents.

    Data Source: NOAA CO-OPS Data API (https://api.tidesandcurrents.noaa.gov/api/prod/),
    the service behind https://tidesandcurrents.noaa.gov/map/. No API key is required.

    Supported Variables:
        - ``constants.STAGE_INSTANT`` (m) — 6-minute water level, verified where
          NOAA has verified it and preliminary otherwise.
        - ``constants.STAGE_DAILY_MEAN`` (m) — mean of the 24 verified hourly
          heights in each UTC day. Days missing any hour are dropped.
        - ``constants.STAGE_MONTHLY_MEAN`` (m) — NOAA's monthly mean sea level
          (``MSL``), stamped on the first day of the month.
        - ``constants.WATER_TEMPERATURE_INSTANT`` (°C) — 6-minute water temperature.

    .. note::
        **Water levels are heights above a vertical datum, and the datum differs
        by station.** By default each station uses the datum NOAA publishes it
        against: MLLW (Mean Lower Low Water) at tide gauges, IGLD (International
        Great Lakes Datum 1985) on the Great Lakes, and the station datum (STND)
        at the few non-tidal coastal and river stations. A tidal station that has
        no MLLW falls back to STND with a warning. Pass ``datum`` to use one
        datum everywhere, e.g. ``NOAATidesFetcher(datum="NAVD")``; stations
        without it then return nothing rather than switching silently. The datum
        each result is relative to is in ``frame.attrs["datum"]``.

    .. note::
        Daily and monthly means come from NOAA's *verified* record, which runs a
        month or two behind real time. The most recent data is in
        ``STAGE_INSTANT``, as preliminary 6-minute values.

    .. note::
        The Data API limits each request to 31 days of 6-minute data and 365
        days of hourly data, so long ranges take many requests, paced at
        :data:`MIN_REQUEST_INTERVAL`. 6-minute data starts in the mid-1990s and
        is never requested before :data:`SIX_MINUTE_RECORD_START`. If NOAA turns
        the client away for sending too much (HTTP 403 or 429), ``get_data``
        raises rather than returning a series that looks empty.

    Timestamps are UTC, returned timezone-naive.
    """

    def __init__(self, datum: Optional[str] = None):
        """Initializes the fetcher.

        Args:
            datum: Optional vertical datum for every water level request, one of
                :data:`DATUMS`. When omitted, each station uses its own default
                (see the class docstring).
        """
        super().__init__()
        if datum is not None and datum.upper() not in DATUMS:
            raise ValueError(f"Unsupported datum {datum!r}. Choose from: {', '.join(DATUMS)}")
        self.datum = datum.upper() if datum else None

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of NOAA CO-OPS water level stations, active and historic.

        This method loads the metadata from a cached CSV file located in
        the ``rivretrieve/cached_site_data/`` directory.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return utils.load_cached_metadata_csv("noaa_tides")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return tuple(_PRODUCTS)

    # -- metadata ----------------------------------------------------------------

    def get_metadata(self) -> pd.DataFrame:
        """Downloads the current station lists from the NOAA CO-OPS Metadata API.

        Combines every station that has ever recorded water levels (NOAA's
        ``historicwl`` list, which includes most active stations), the active
        water level network, and the water temperature stations.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata,
            including one boolean column per supported variable.
        """
        session = utils.requests_retry_session()
        lists = {}
        for kind in ("historicwl", "waterlevels", "watertemp"):
            response = session.get(STATIONS_URL, params={"type": kind, "expand": "details"}, timeout=300)
            response.raise_for_status()
            lists[kind] = response.json().get("stations") or []
        return self._parse_metadata(lists["historicwl"], lists["waterlevels"], lists["watertemp"])

    def _parse_metadata(self, historic: list[dict], active: list[dict], water_temperature: list[dict]) -> pd.DataFrame:
        def ids(entries):
            return {str(entry.get("id") or "").strip() for entry in entries} - {""}

        active_ids = ids(active)
        water_level_ids = ids(historic) | active_ids
        water_temperature_ids = ids(water_temperature)

        # Active records are the freshest description of a station, so they go last and win.
        merged: dict[str, dict] = {}
        for entry in [*historic, *water_temperature, *active]:
            station_id = str(entry.get("id") or "").strip()
            if station_id:
                merged[station_id] = entry

        records = []
        for station_id, entry in merged.items():
            details = entry.get("details") or {}
            is_active = station_id in active_ids or station_id in water_temperature_ids
            removed = None if is_active else _date_text(details.get("removed"))
            six_minute_era = removed is None or pd.Timestamp(removed) >= SIX_MINUTE_RECORD_START
            has_water_level = station_id in water_level_ids
            tidal = bool(entry.get("tidal"))
            greatlakes = bool(entry.get("greatlakes"))
            records.append(
                {
                    constants.GAUGE_ID: station_id,
                    constants.STATION_NAME: (entry.get("name") or "").strip() or None,
                    constants.LATITUDE: pd.to_numeric(entry.get("lat"), errors="coerce"),
                    constants.LONGITUDE: pd.to_numeric(entry.get("lng"), errors="coerce"),
                    constants.SOURCE: "NOAA CO-OPS",
                    "state": (entry.get("state") or "").strip() or None,
                    "status": "active" if is_active else "historic",
                    "established": _date_text(details.get("established")),
                    "removed": removed,
                    "tidal": tidal,
                    "greatlakes": greatlakes,
                    "default_datum": _default_datum(tidal, greatlakes) if has_water_level else None,
                    "affiliations": (entry.get("affiliations") or "").strip() or None,
                    "tide_type": (entry.get("tideType") or "").strip() or None,
                    "shef_code": (entry.get("shefcode") or "").strip() or None,
                    constants.STAGE_INSTANT: has_water_level and six_minute_era,
                    constants.STAGE_DAILY_MEAN: has_water_level,
                    constants.STAGE_MONTHLY_MEAN: has_water_level,
                    constants.WATER_TEMPERATURE_INSTANT: station_id in water_temperature_ids and six_minute_era,
                }
            )

        frame = pd.DataFrame(records)
        if frame.empty:
            return frame
        return frame.set_index(constants.GAUGE_ID).sort_index()

    # -- data --------------------------------------------------------------------

    def _datum_candidates(self, gauge_id: str) -> list[str]:
        """Datums to try, in order. Only an automatic choice may fall back."""
        if self.datum:
            return [self.datum]
        site = _site(gauge_id)
        preferred = site.get("default_datum") if site is not None else None
        preferred = preferred if isinstance(preferred, str) and preferred else "MLLW"
        return [preferred] if preferred == STATION_DATUM else [preferred, STATION_DATUM]

    def _request(self, session: requests.Session, params: dict) -> tuple[list[dict], Optional[str]]:
        """Makes one Data API call. Returns ``(records, None)`` or ``([], NOAA's error message)``."""
        _pace()
        response = session.get(DATA_URL, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code in THROTTLE_STATUSES:
            raise requests.exceptions.HTTPError(
                f"NOAA CO-OPS refused the request for station {params.get('station')} with HTTP "
                f"{response.status_code}, which is how its gateway turns away a client that sent too "
                "many requests. Wait a few minutes before trying again.",
                response=response,
            )
        try:
            payload = response.json()
        except ValueError:
            response.raise_for_status()
            raise
        # NOAA reports "no data" with HTTP 200 and a missing datum with HTTP 400,
        # both as {"error": {"message": ...}}.
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            return [], str(payload["error"].get("message") or "").strip()
        response.raise_for_status()
        return (payload.get("data") or []) if isinstance(payload, dict) else [], None

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> dict:
        """Downloads one product in as many requests as the Data API's range limits need.

        Returns:
            ``{"datum": <datum used, or None>, "records": [<raw NOAA records>]}``.
        """
        product, window_days, _ = _PRODUCTS[variable]
        datums = [None] if product == "water_temperature" else self._datum_candidates(gauge_id)
        datum_index = 0
        records: list[dict] = []
        session = utils.requests_retry_session()

        for begin, finish in _windows(pd.Timestamp(start_date), pd.Timestamp(end_date), window_days):
            while True:
                datum = datums[datum_index]
                params = {
                    "station": gauge_id,
                    "product": product,
                    "begin_date": begin.strftime("%Y%m%d"),
                    "end_date": finish.strftime("%Y%m%d"),
                    "units": "metric",
                    "time_zone": "gmt",
                    "format": "json",
                    "application": APPLICATION,
                }
                if datum:
                    params["datum"] = datum
                rows, error = self._request(session, params)
                if error is None:
                    records.extend(rows)
                    break
                if _NO_DATA.search(error):
                    break
                if _MISSING_DATUM.search(error) and datum_index + 1 < len(datums):
                    logger.warning(
                        "NOAA station %s has no %s datum; returning %s relative to %s (station datum) instead.",
                        gauge_id,
                        datum,
                        variable,
                        datums[datum_index + 1],
                    )
                    datum_index += 1
                    continue
                logger.error("NOAA CO-OPS rejected the %s request for station %s: %s", product, gauge_id, error)
                return {"datum": datum, "records": []}

        return {"datum": datums[datum_index], "records": records}

    def _parse_data(self, gauge_id: str, raw_data: dict, variable: str) -> pd.DataFrame:
        """Parses NOAA's ``{"t", "v"}`` records, or ``{"year", "month", "MSL"}`` for monthly means."""
        records = (raw_data or {}).get("records") or []
        if not records:
            return _empty(variable)

        frame = pd.DataFrame(records)
        if variable == constants.STAGE_MONTHLY_MEAN:
            if not {"year", "month", "MSL"} <= set(frame.columns):
                logger.warning("NOAA monthly means for station %s have no MSL column", gauge_id)
                return _empty(variable)
            months = pd.DataFrame(
                {
                    "year": pd.to_numeric(frame["year"], errors="coerce"),
                    "month": pd.to_numeric(frame["month"], errors="coerce"),
                    "day": 1,
                }
            )
            times = pd.to_datetime(months, errors="coerce")
            values = pd.to_numeric(frame["MSL"], errors="coerce")
        else:
            if not {"t", "v"} <= set(frame.columns):
                logger.warning("NOAA payload for station %s has no t/v columns", gauge_id)
                return _empty(variable)
            times = pd.to_datetime(frame["t"], format="%Y-%m-%d %H:%M", errors="coerce")
            # Missing readings arrive as empty strings.
            values = pd.to_numeric(frame["v"], errors="coerce")

        result = pd.DataFrame({constants.TIME_INDEX: times, variable: values}).dropna()
        if result.empty:
            return _empty(variable)
        result = result.set_index(constants.TIME_INDEX).sort_index()
        result = result[~result.index.duplicated(keep="first")]

        if variable == constants.STAGE_DAILY_MEAN:
            grouped = result[variable].groupby(result.index.normalize())
            daily = grouped.mean()[grouped.count() >= HOURS_PER_DAY]
            daily.index.name = constants.TIME_INDEX
            result = daily.to_frame(name=variable)

        result.attrs["datum"] = raw_data.get("datum")
        return result

    def _request_range(
        self, gauge_id: str, variable: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> tuple[pd.Timestamp, pd.Timestamp]:
        """Narrows the requested range to where data can exist, so no request is wasted."""
        first, last = start, end
        last = min(last, pd.Timestamp.now("UTC").tz_localize(None).normalize())

        site = _site(gauge_id)
        removed = site.get("removed") if site is not None else None
        if isinstance(removed, str) and removed:
            last = min(last, pd.Timestamp(removed))

        if _PRODUCTS[variable][2]:
            first = max(first, SIX_MINUTE_RECORD_START)
        if variable == constants.STAGE_MONTHLY_MEAN:
            # Whole months, so a mid-month start still gets that month's mean.
            first = first.to_period("M").start_time
            last = last.to_period("M").end_time.normalize()
        return first, last

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetches water level or water temperature for one NOAA CO-OPS station.

        Args:
            gauge_id: The 7-digit NOAA station ID, e.g. ``"8518750"`` for The Battery, New York.
            variable: One of ``get_available_variables()``.
            start_date: Optional start date in 'YYYY-MM-DD' format.
            end_date: Optional end date in 'YYYY-MM-DD' format. The whole day is included.

        Returns:
            pd.DataFrame: A DataFrame indexed by ``constants.TIME_INDEX`` (UTC,
            timezone-naive) with a single column named after the requested
            ``variable``. ``frame.attrs["datum"]`` names the vertical datum of
            water levels. Empty if the station has no data for the range.

        Raises:
            ValueError: If the requested ``variable`` is not supported.
            requests.exceptions.RequestException: If NOAA cannot be reached or
                rate-limits the client (HTTP 403 or 429).
        """
        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        start_date = utils.format_start_date(start_date)
        end_date = utils.format_end_date(end_date)
        start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)

        first, last = self._request_range(gauge_id, variable, start, end)
        if first > last:
            logger.info(
                "NOAA station %s has no %s between %s and %s (6-minute data starts %s; historic "
                "stations stop at their removal date).",
                gauge_id,
                variable,
                start_date,
                end_date,
                SIX_MINUTE_RECORD_START.date(),
            )
            return _empty(variable)

        try:
            raw_data = self._download_data(gauge_id, variable, first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d"))
            frame = self._parse_data(gauge_id, raw_data, variable)
        except requests.exceptions.RequestException:
            # A failed or throttled download is not "no data"; let the caller see it.
            raise
        except Exception as exc:
            logger.error("Failed to get NOAA data for station %s, variable %s: %s", gauge_id, variable, exc)
            return _empty(variable)

        if frame.empty:
            return frame
        lower = start.to_period("M").start_time if variable == constants.STAGE_MONTHLY_MEAN else start
        return frame[(frame.index >= lower) & (frame.index < end + pd.Timedelta(days=1))]
