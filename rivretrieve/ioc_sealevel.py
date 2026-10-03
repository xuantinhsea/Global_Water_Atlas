"""Fetcher for sea level from the IOC Sea Level Station Monitoring Facility.

The facility, run by the Flanders Marine Institute (VLIZ) for UNESCO's
Intergovernmental Oceanographic Commission, gathers real-time readings from
about 1,700 tide gauges operated by national agencies worldwide and republishes
them unprocessed through one open web service. That includes 255 gauges across
Southeast Asia, from the Andaman Sea to Papua, most of which publish nowhere
else in machine-readable form.

Cite the facility when using its data: Flanders Marine Institute (VLIZ);
Intergovernmental Oceanographic Commission (IOC). Sea level station monitoring
facility. https://www.ioc-sealevelmonitoring.org
"""

import datetime
import logging
import threading
import time
from functools import lru_cache
from typing import Any, Optional

import pandas as pd

from . import base, constants, utils

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ioc-sealevelmonitoring.org/service.php"

#: The service answers at most 31 days per request and silently drops the rest,
#: so ranges are fetched in windows comfortably inside that.
CHUNK_DAYS = 30

#: Most recent days of a request that are actually fetched. Raw readings run to
#: about 4 MB per station-month, and the facility is a monitoring service rather
#: than an archive: a decade-long request would be over a hundred large calls.
#: UHSLC (:class:`rivretrieve.UHSLCFetcher`) serves long, quality-controlled records.
MAX_DAYS = 92

#: Seconds between requests. The facility is a shared, volunteer-funded service.
MIN_REQUEST_INTERVAL = 1.0

#: Water level sensor codes, preferred first: radar, then pressure, then float or
#: encoder, then bubbler and the rest. Everything else in the feed is something
#: else — battery voltage (``bat``), air pressure (``atm``), switch states
#: (``sw1``, ``sw2``) — and ``prt`` is a DART tsunameter's deep-ocean water column,
#: thousands of metres, not a coastal level, so stations with only ``prt`` are left out.
SENSORS = (
    "rad", "ra2", "ra3", "ras",
    "prs", "pr1", "pr2", "pwl", "bwl",
    "enc", "en2", "flt",
    "bub", "bub1", "aqu", "wls", "ecs", "stp",
)

FEET_TO_METRES = 0.3048

#: Share of a station's usual readings per hour needed before an hour gets a mean.
MIN_HOUR_COVERAGE = 0.5

#: Hourly means needed before a day gets a daily mean.
MIN_HOURS_PER_DAY = 18

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


def _number(value: Any) -> Optional[float]:
    number = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(number) else float(number)


@lru_cache(maxsize=1)
def _station_units() -> dict[str, dict[str, str]]:
    """``{code: {sensor: units}}`` from the cached station list, loaded once per process."""
    try:
        frame = IOCSeaLevelFetcher.get_cached_metadata()
    except Exception:
        return {}
    units: dict[str, dict[str, str]] = {}
    for code, text in frame.get("sensor_units", pd.Series(dtype=str)).dropna().items():
        units[str(code)] = dict(part.split(":", 1) for part in str(text).split(";") if ":" in part)
    return units


class IOCSeaLevelFetcher(base.RiverDataFetcher):
    """Fetches tide gauge sea level from the IOC Sea Level Station Monitoring Facility.

    Data Source: Flanders Marine Institute (VLIZ) and UNESCO-IOC,
    https://www.ioc-sealevelmonitoring.org/. No API key is required.

    Supported Variables:
        - ``constants.STAGE_INSTANT`` (m) — every reading, typically one a minute
        - ``constants.STAGE_HOURLY_MEAN`` (m)
        - ``constants.STAGE_DAILY_MEAN`` (m)

    .. warning::
        **Raw, unchecked data.** The facility republishes what each gauge
        transmits, spikes, flat lines and datum jumps included. Values are
        heights above the sensor's own zero, which is not tied to any common
        datum, so compare a station only with itself.

    .. warning::
        **Recent data only.** One call fetches at most the most recent
        :data:`MAX_DAYS` days of the requested range; anything earlier is
        skipped with a log message. Use :class:`rivretrieve.UHSLCFetcher` for
        long, quality-controlled tide gauge records.

    .. note::
        Many stations carry several water level sensors. Each call uses the
        station's preferred sensor (radar first, see :data:`SENSORS`), falling
        back to the next one when it has no readings; the sensor used is in
        ``frame.attrs["sensor"]``. Timestamps are UTC, returned timezone-naive.
    """

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of IOC tide gauges.

        This method loads the metadata from a cached CSV file located in
        the ``rivretrieve/cached_site_data/`` directory.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return utils.load_cached_metadata_csv("ioc_sealevel")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (constants.STAGE_INSTANT, constants.STAGE_HOURLY_MEAN, constants.STAGE_DAILY_MEAN)

    # -- metadata ----------------------------------------------------------------

    def get_metadata(self) -> pd.DataFrame:
        """Downloads the facility's station list.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        session = utils.requests_retry_session()
        response = session.get(BASE_URL, params={"query": "stationlist", "showall": "all"}, timeout=180)
        response.raise_for_status()
        return self._parse_metadata(response.json())

    def _parse_metadata(self, payload: Any) -> pd.DataFrame:
        """Collapses the one-row-per-sensor list into one row per station."""
        rows_by_code: dict[str, list[dict]] = {}
        for row in payload or []:
            code = str(row.get("Code") or "").strip()
            if code:
                rows_by_code.setdefault(code, []).append(row)

        records = []
        for code, rows in rows_by_code.items():
            units: dict[str, str] = {}
            for row in rows:
                sensor = str(row.get("sensor") or "").strip()
                if sensor in SENSORS and sensor not in units:
                    units[sensor] = str(row.get("units") or "M").strip().upper()
            if not units:
                continue
            sensors = sorted(units, key=SENSORS.index)
            first = rows[0]
            latitude = _number(first.get("Lat")) if first.get("Lat") is not None else _number(first.get("lat"))
            longitude = _number(first.get("Lon")) if first.get("Lon") is not None else _number(first.get("lon"))
            records.append(
                {
                    constants.GAUGE_ID: code,
                    constants.STATION_NAME: str(first.get("Location") or "").strip() or None,
                    constants.LATITUDE: latitude,
                    constants.LONGITUDE: longitude,
                    constants.COUNTRY: str(first.get("countryname") or first.get("country") or "").strip() or None,
                    constants.SOURCE: "IOC Sea Level Station Monitoring Facility",
                    "country_code": first.get("country"),
                    "gloss_id": first.get("GlossID"),
                    "station_type": first.get("type"),
                    "status": first.get("status"),
                    "sensors": ",".join(sensors),
                    "primary_sensor": sensors[0],
                    "sensor_units": ";".join(f"{s}:{units[s]}" for s in sensors),
                    "registered": str(first.get("date_created") or "")[:10] or None,
                    "station_url": f"https://www.ioc-sealevelmonitoring.org/station.php?code={code}",
                }
            )

        frame = pd.DataFrame(records)
        if frame.empty:
            return frame
        return frame.set_index(constants.GAUGE_ID).sort_index()

    # -- data --------------------------------------------------------------------

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> list[dict]:
        """Downloads every sensor's readings for ``[start_date, end_date]``, one window at a time."""
        session = utils.requests_retry_session()
        points: list[dict] = []
        for window_start, window_end in _windows(pd.Timestamp(start_date), pd.Timestamp(end_date), CHUNK_DAYS):
            _pace()
            params = {
                "query": "data",
                "code": gauge_id,
                "timestart": f"{window_start:%Y-%m-%d}T00:00:00",
                "timestop": f"{window_end + pd.Timedelta(days=1):%Y-%m-%d}T00:00:00",
                "format": "json",
            }
            response = session.get(BASE_URL, params=params, timeout=180)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                points.extend(payload)
            else:
                logger.warning("IOC returned %r for station %s", str(payload)[:120], gauge_id)
        return points

    def _parse_data(self, gauge_id: str, raw_data: list[dict], variable: str) -> pd.DataFrame:
        """Keeps one water level sensor and derives the requested aggregate."""
        if not raw_data:
            return _empty(variable)

        frame = pd.DataFrame(raw_data)
        if not {"stime", "slevel", "sensor"} <= set(frame.columns):
            logger.warning("Unexpected IOC payload for station %s", gauge_id)
            return _empty(variable)

        frame = frame[frame["sensor"].isin(SENSORS)]
        frame = frame.assign(
            time=pd.to_datetime(frame["stime"], errors="coerce"),
            value=pd.to_numeric(frame["slevel"], errors="coerce"),
        ).dropna(subset=["time", "value"])
        if frame.empty:
            return _empty(variable)

        sensor = min(frame["sensor"].unique(), key=SENSORS.index)
        series = frame.loc[frame["sensor"] == sensor].set_index("time")["value"].sort_index()
        series = series[~series.index.duplicated(keep="first")]
        if _station_units().get(gauge_id, {}).get(sensor) == "F":
            series = series * FEET_TO_METRES
        series.index.name = constants.TIME_INDEX

        if variable == constants.STAGE_INSTANT:
            result = series.to_frame(name=variable)
        else:
            hourly = self._hourly_means(series)
            if variable == constants.STAGE_HOURLY_MEAN:
                result = hourly.to_frame(name=variable)
            else:
                grouped = hourly.groupby(hourly.index.normalize())
                daily = grouped.mean()[grouped.count() >= MIN_HOURS_PER_DAY]
                daily.index.name = constants.TIME_INDEX
                result = daily.to_frame(name=variable)

        result.attrs["sensor"] = sensor
        result.attrs["datum"] = "sensor zero (not levelled to a common datum)"
        return result

    @staticmethod
    def _hourly_means(series: pd.Series) -> pd.Series:
        """Hourly means, kept only where at least half the usual readings arrived."""
        if len(series) < 2:
            return series.iloc[0:0]
        step_minutes = series.index.to_series().diff().dt.total_seconds().div(60).median()
        expected = max(1.0, 60.0 / step_minutes) if step_minutes and step_minutes > 0 else 1.0
        grouped = series.resample("1h")
        hourly = grouped.mean()[grouped.count() >= MIN_HOUR_COVERAGE * expected].dropna()
        hourly.index.name = constants.TIME_INDEX
        return hourly

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetches sea level for one IOC tide gauge.

        Args:
            gauge_id: The IOC station code, e.g. ``"vung"`` for Vung Tau.
            variable: One of ``get_available_variables()``.
            start_date: Optional start date in 'YYYY-MM-DD' format.
            end_date: Optional end date in 'YYYY-MM-DD' format.

        Returns:
            pd.DataFrame: A DataFrame indexed by ``constants.TIME_INDEX`` with a
            single column named after the requested ``variable``, in metres.
            Covers at most the last :data:`MAX_DAYS` days of the range.

        Raises:
            ValueError: If the requested ``variable`` is not supported.
            requests.exceptions.RequestException: If the facility cannot be reached.
        """
        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        start_date = utils.format_start_date(start_date)
        end_date = utils.format_end_date(end_date)
        earliest = (pd.Timestamp(end_date) - pd.Timedelta(days=MAX_DAYS - 1)).strftime("%Y-%m-%d")
        if start_date < earliest:
            logger.info(
                "IOC sea level: fetching only the last %d days of the request (%s to %s). "
                "Use UHSLCFetcher for long, quality-controlled records.",
                MAX_DAYS,
                earliest,
                end_date,
            )
            start_date = earliest
        if start_date > end_date:
            return _empty(variable)

        raw_data = self._download_data(gauge_id, variable, start_date, end_date)
        frame = self._parse_data(gauge_id, raw_data, variable)
        if frame.empty:
            return frame
        upper = pd.Timestamp(end_date) + datetime.timedelta(days=1)
        clipped = frame[(frame.index >= pd.Timestamp(start_date)) & (frame.index < upper)]
        clipped.attrs.update(frame.attrs)
        return clipped
