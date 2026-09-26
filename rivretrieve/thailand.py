"""Fetchers for Thai river gauge and rain gauge data from the ThaiWater Open API.

Thailand's Hydro-Informatics Institute (HII) runs the National Hydroinformatics
Data Center, which aggregates telemetry from more than a dozen Thai agencies —
the Royal Irrigation Department, the Department of Water Resources, the Thai
Meteorological Department, EGAT and others — and republishes it through one
unauthenticated JSON API.

Two networks are published, with **separate and overlapping station id spaces**
(475 ids appear in both), so they are exposed here as two fetchers rather than
one:

``ThailandFetcher``
    1,119 telemetry water level stations, a subset of which also report
    discharge. Historical queries work.
``ThailandRainFetcher``
    4,485 rain gauges. The provider's graph endpoint ignores date parameters
    and always returns a rolling window of roughly the last 41 hours, so this
    fetcher cannot serve history. See its docstring.
"""

import datetime
import logging
import os
import time
from typing import Any, Optional

import pandas as pd

from . import base, constants, utils

logger = logging.getLogger(__name__)

BASE_URL = "https://api-v3.thaiwater.net/api/v1/thaiwater30/public"

#: Hours that must carry a reading before a day is given a daily value.
#: Thai telemetry is patchy — some months have three readings in twenty-four
#: hours — and averaging those into a "daily mean" would invent a number that
#: was never measured.
MIN_HOURS_PER_DAY = 18

#: Politeness delay between the chunks of one station's request.
CHUNK_DELAY_SECONDS = 0.2

#: Days per request.
#:
#: The provider caps a response at roughly 8,784 hourly rows — 366 days — and
#: when a wider range is asked for it does not error: it silently returns only
#: the most recent year. A three-year request comes back looking healthy while
#: two thirds of the period is missing. Half a year stays comfortably clear of
#: that cap, and :func:`_covers` checks every response anyway.
CHUNK_DAYS = 183

#: Earliest date worth requesting. The telemetry archive begins around 2020 —
#: every month before it returns a full grid of nulls — so the library's 1900
#: default would spend 1,400 requests to learn nothing. Raising this costs
#: nothing; lowering it only adds empty requests.
ARCHIVE_START = "2020-01-01"


#: Blank cells still mean "missing", but the literal text "NaN" does not.
#: A Thai Meteorological Department rain gauge in Nan province is romanised
#: "NaN" upstream (Thai น่าน = Nan, and the provider's own province field says
#: "Nan"), so pandas' default parsing would silently drop a real station name.
_NA_VALUES = ["", "#N/A", "N/A", "NA", "NULL", "null", "None", "n/a"]


def _load_thai_sites(country_code: str) -> pd.DataFrame:
    """Loads a cached Thai site file without mistaking the name "NaN" for a null."""
    path = os.path.join(os.path.dirname(__file__), "cached_site_data", f"{country_code}_sites.csv")
    frame = pd.read_csv(
        path,
        dtype={constants.GAUGE_ID: str},
        keep_default_na=False,
        na_values=_NA_VALUES,
    )
    return frame.set_index(constants.GAUGE_ID)


def _localised(value: Any, prefer: str = "en") -> Optional[str]:
    """Picks a name out of the API's ``{"en": ..., "th": ...}`` objects.

    Only 424 of 1,119 water level stations carry an English name, so Thai is
    the fallback rather than an error.
    """
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if not isinstance(value, dict):
        return None
    for key in (prefer, "en", "th"):
        text = (value.get(key) or "").strip()
        if text:
            return text
    return None


def _date_ranges(start_date: str, end_date: str, chunk_days: int = CHUNK_DAYS) -> list[tuple[str, str]]:
    """Splits a date range into chunks the provider will answer in full."""
    start = datetime.date.fromisoformat(start_date)
    end = datetime.date.fromisoformat(end_date)
    if end < start:
        return []

    chunks = []
    cursor = start
    step = datetime.timedelta(days=chunk_days - 1)
    while cursor <= end:
        chunk_end = min(cursor + step, end)
        chunks.append((cursor.isoformat(), chunk_end.isoformat()))
        cursor = chunk_end + datetime.timedelta(days=1)
    return chunks


def _covers(points: list[dict], stamp_key: str, chunk_start: str) -> bool:
    """Whether a response actually reaches back to the start of its chunk.

    The provider truncates over-wide requests silently, so a short response is
    the only signal that data was dropped.
    """
    if not points:
        return True
    first = str(points[0].get(stamp_key) or "")[:10]
    return bool(first) and first <= chunk_start


class ThailandFetcher(base.RiverDataFetcher):
    """Fetches Thai river water level and discharge from the ThaiWater Open API.

    Data Source: Hydro-Informatics Institute / National Hydroinformatics Data
    Center (https://www.thaiwater.net/). No API key is required.

    Supported Variables:
        - ``constants.STAGE_HOURLY_MEAN`` (m)
        - ``constants.DISCHARGE_HOURLY_MEAN`` (m³/s)
        - ``constants.STAGE_DAILY_MEAN`` (m)
        - ``constants.DISCHARGE_DAILY_MEAN`` (m³/s)

    .. note::
        The provider publishes hourly telemetry only. The daily variables are
        aggregated here from those hourly readings, and a day is left out
        unless at least :data:`MIN_HOURS_PER_DAY` of its hours carry a value.

    .. warning::
        This is a telemetry archive, not a long historical record. Readings
        begin around 2020 and only become dense from 2023; requests for earlier
        years return a full grid of empty rows, which this fetcher discards. A
        returned DataFrame is routinely much shorter than the requested range.

    .. note::
        Water levels are metres above mean sea level as published by the
        provider, not gauge height above a local datum. Timestamps are local
        Thai time (UTC+7) and are returned timezone-naive, as the API gives them.
    """

    STATION_TYPE = "tele_waterlevel"
    CATALOGUE_URL = f"{BASE_URL}/waterlevel"
    GRAPH_URL = f"{BASE_URL}/waterlevel_graph"

    #: Column in the provider's graph payload for each supported variable.
    _SOURCE_COLUMN = {
        constants.STAGE_HOURLY_MEAN: "value",
        constants.STAGE_DAILY_MEAN: "value",
        constants.DISCHARGE_HOURLY_MEAN: "discharge",
        constants.DISCHARGE_DAILY_MEAN: "discharge",
    }

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of available Thai water level gauges.

        This method loads the metadata from a cached CSV file located in
        the ``rivretrieve/cached_site_data/`` directory.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return _load_thai_sites("thailand")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (
            constants.DISCHARGE_DAILY_MEAN,
            constants.DISCHARGE_HOURLY_MEAN,
            constants.STAGE_DAILY_MEAN,
            constants.STAGE_HOURLY_MEAN,
        )

    # -- metadata ----------------------------------------------------------------

    def get_metadata(self) -> pd.DataFrame:
        """Downloads the current station list from the provider.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        session = utils.requests_retry_session()
        response = session.get(self.CATALOGUE_URL, timeout=120)
        response.raise_for_status()
        return self._parse_metadata(response.json())

    def _parse_metadata(self, payload: dict) -> pd.DataFrame:
        records = []
        for entry in payload.get("data", []):
            station = entry.get("station") or {}
            if station.get("id") is None:
                continue
            geocode = entry.get("geocode") or {}
            records.append(
                {
                    constants.GAUGE_ID: str(station["id"]),
                    constants.STATION_NAME: _localised(station.get("tele_station_name")),
                    constants.LATITUDE: pd.to_numeric(station.get("tele_station_lat"), errors="coerce"),
                    constants.LONGITUDE: pd.to_numeric(station.get("tele_station_long"), errors="coerce"),
                    constants.RIVER: _localised((entry.get("basin") or {}).get("basin_name")),
                    constants.COUNTRY: "Thailand",
                    constants.SOURCE: "ThaiWater (HII)",
                    "station_code": station.get("tele_station_oldcode"),
                    "agency": _localised((entry.get("agency") or {}).get("agency_shortname")),
                    "province": _localised(geocode.get("province_name")),
                    "basin_code": (entry.get("basin") or {}).get("basin_code"),
                    "ground_level": station.get("ground_level"),
                    "left_bank": station.get("left_bank"),
                    "right_bank": station.get("right_bank"),
                }
            )
        frame = pd.DataFrame(records)
        if frame.empty:
            return frame
        return frame.drop_duplicates(subset=[constants.GAUGE_ID]).set_index(constants.GAUGE_ID)

    # -- data --------------------------------------------------------------------

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> list[dict]:
        """Downloads the hourly graph payload in chunks the provider answers in full."""
        session = utils.requests_retry_session()
        points: list[dict] = []

        for index, (chunk_start, chunk_end) in enumerate(_date_ranges(start_date, end_date)):
            if index:
                time.sleep(CHUNK_DELAY_SECONDS)
            params = {
                "station_id": gauge_id,
                # Without station_type the service raises a 500 rather than
                # reporting a bad request, so it is never omitted.
                "station_type": self.STATION_TYPE,
                "start_date": chunk_start,
                "end_date": chunk_end,
            }
            try:
                response = session.get(self.GRAPH_URL, params=params, timeout=90)
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                logger.warning(
                    "ThaiWater request failed for station %s, %s to %s: %s",
                    gauge_id,
                    chunk_start,
                    chunk_end,
                    exc,
                )
                continue

            data = payload.get("data")
            if isinstance(data, dict):
                chunk = data.get("graph_data") or []
                if not _covers(chunk, "datetime", chunk_start):
                    logger.warning(
                        "ThaiWater truncated the response for station %s: asked from %s, "
                        "got from %s. Some readings in this chunk are missing.",
                        gauge_id,
                        chunk_start,
                        str(chunk[0].get("datetime"))[:10],
                    )
                points.extend(chunk)
            elif isinstance(data, str):
                # The API reports "422: No station id" and friends in the data field.
                logger.warning("ThaiWater rejected station %s: %s", gauge_id, data)

        return points

    def _parse_data(self, gauge_id: str, raw_data: list[dict], variable: str) -> pd.DataFrame:
        """Parses the hourly graph payload, discarding the empty padding rows.

        A request for a period the telemetry never covered still returns a full
        hourly grid with null values, so dropping nulls is what separates "no
        data" from "a thousand rows of nothing".
        """
        source_column = self._SOURCE_COLUMN[variable]
        empty = pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)

        if not raw_data:
            return empty

        frame = pd.DataFrame(raw_data)
        if "datetime" not in frame.columns or source_column not in frame.columns:
            logger.warning("ThaiWater payload for station %s has no %s column", gauge_id, source_column)
            return empty

        frame[constants.TIME_INDEX] = pd.to_datetime(frame["datetime"], errors="coerce")
        frame[variable] = pd.to_numeric(frame[source_column], errors="coerce")
        frame = frame[[constants.TIME_INDEX, variable]].dropna()
        if frame.empty:
            return empty

        frame = frame.set_index(constants.TIME_INDEX).sort_index()
        frame = frame[~frame.index.duplicated(keep="first")]

        if constants.DAILY in variable:
            frame = self._to_daily(frame, variable)

        return frame

    @staticmethod
    def _to_daily(frame: pd.DataFrame, variable: str) -> pd.DataFrame:
        """Averages hourly readings into daily means, dropping thin days."""
        grouped = frame[variable].groupby(frame.index.normalize())
        daily = grouped.mean()
        counts = grouped.count()
        daily = daily[counts >= MIN_HOURS_PER_DAY]
        daily.index.name = constants.TIME_INDEX
        return daily.to_frame(name=variable)

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetches and parses time series data for a specific gauge and variable.

        Args:
            gauge_id: The ThaiWater telemetry station id, e.g. ``"2752"``.
            variable: One of ``get_available_variables()``.
            start_date: Optional start date in 'YYYY-MM-DD' format.
            end_date: Optional end date in 'YYYY-MM-DD' format.

        Returns:
            pd.DataFrame: A DataFrame indexed by ``constants.TIME_INDEX`` with a
            single column named after the requested ``variable``. Empty if the
            telemetry does not cover the requested period.

        Raises:
            ValueError: If the requested ``variable`` is not supported.
        """
        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        start_date = utils.format_start_date(start_date or ARCHIVE_START)
        end_date = utils.format_end_date(end_date)

        # Clamp to the start of the archive. A caller asking for 1950 is not
        # wrong to ask, but every month before ARCHIVE_START is a round trip
        # that returns nothing but padding.
        if start_date < ARCHIVE_START:
            logger.info(
                "ThaiWater telemetry starts around %s; requesting from there rather than %s.",
                ARCHIVE_START,
                start_date,
            )
            start_date = ARCHIVE_START

        try:
            raw_data = self._download_data(gauge_id, variable, start_date, end_date)
            frame = self._parse_data(gauge_id, raw_data, variable)
        except Exception as exc:
            logger.error("Failed to get data for station %s, variable %s: %s", gauge_id, variable, exc)
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)

        if frame.empty:
            return frame
        return frame[
            (frame.index >= pd.Timestamp(start_date))
            & (frame.index <= pd.Timestamp(end_date) + pd.Timedelta(days=1))
        ]


class ThailandRainFetcher(base.RiverDataFetcher):
    """Fetches Thai rain gauge data from the ThaiWater Open API.

    Data Source: Hydro-Informatics Institute / National Hydroinformatics Data
    Center (https://www.thaiwater.net/). No API key is required.

    Covers 4,485 rain gauges operated by the Department of Water Resources, HII,
    the Royal Irrigation Department, the Thai Meteorological Department, EGAT,
    the Bangkok Metropolitan Administration and others.

    Supported Variables:
        - ``constants.PRECIPITATION_HOURLY_SUM`` (mm)

    .. warning::
        **No history.** The provider's rainfall graph endpoint accepts
        ``start_date`` and ``end_date`` but ignores them, always returning a
        rolling window of roughly the last 41 hours. This was verified against
        several date ranges and station types. Requests for earlier periods
        return an empty DataFrame rather than silently handing back the last
        two days under the wrong dates.

    .. note::
        Rainfall is millimetres accumulated in the hour ending at the
        timestamp. Timestamps are local Thai time (UTC+7), timezone-naive.
    """

    STATION_TYPE = "rainfall_24h"
    CATALOGUE_URL = f"{BASE_URL}/rain_24h"
    GRAPH_URL = f"{BASE_URL}/rain_24h_graph"

    #: How far back the provider's rolling window actually reaches.
    WINDOW_HOURS = 48

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of available Thai rain gauges.

        This method loads the metadata from a cached CSV file located in
        the ``rivretrieve/cached_site_data/`` directory.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return _load_thai_sites("thailand_rain")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (constants.PRECIPITATION_HOURLY_SUM,)

    # -- metadata ----------------------------------------------------------------

    def get_metadata(self) -> pd.DataFrame:
        """Downloads the current rain gauge list from the provider.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        session = utils.requests_retry_session()
        response = session.get(self.CATALOGUE_URL, timeout=180)
        response.raise_for_status()
        return self._parse_metadata(response.json())

    def _parse_metadata(self, payload: dict) -> pd.DataFrame:
        records = []
        for entry in payload.get("data", []):
            station = entry.get("station") or {}
            if station.get("id") is None:
                continue
            geocode = entry.get("geocode") or {}
            records.append(
                {
                    constants.GAUGE_ID: str(station["id"]),
                    constants.STATION_NAME: _localised(station.get("tele_station_name")),
                    constants.LATITUDE: pd.to_numeric(station.get("tele_station_lat"), errors="coerce"),
                    constants.LONGITUDE: pd.to_numeric(station.get("tele_station_long"), errors="coerce"),
                    constants.COUNTRY: "Thailand",
                    constants.SOURCE: "ThaiWater (HII)",
                    "station_code": station.get("tele_station_oldcode"),
                    "agency": _localised((entry.get("agency") or {}).get("agency_shortname")),
                    "province": _localised(geocode.get("province_name")),
                    "basin": _localised((entry.get("basin") or {}).get("basin_name")),
                }
            )
        frame = pd.DataFrame(records)
        if frame.empty:
            return frame
        return frame.drop_duplicates(subset=[constants.GAUGE_ID]).set_index(constants.GAUGE_ID)

    # -- data --------------------------------------------------------------------

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> list[dict]:
        """Downloads the rolling rainfall window.

        ``start_date`` and ``end_date`` are accepted to satisfy the base class
        but the provider ignores them, so only one request is ever made.
        """
        session = utils.requests_retry_session()
        params = {"station_id": gauge_id, "station_type": self.STATION_TYPE}
        try:
            response = session.get(self.GRAPH_URL, params=params, timeout=60)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("ThaiWater rainfall request failed for station %s: %s", gauge_id, exc)
            return []

        data = payload.get("data")
        if isinstance(data, list):
            return data
        logger.warning("ThaiWater rejected rain station %s: %s", gauge_id, data)
        return []

    def _parse_data(self, gauge_id: str, raw_data: list[dict], variable: str) -> pd.DataFrame:
        empty = pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)
        if not raw_data:
            return empty

        frame = pd.DataFrame(raw_data)
        if "rainfall_datetime" not in frame.columns or "rainfall_value" not in frame.columns:
            logger.warning("Unexpected rainfall payload for station %s", gauge_id)
            return empty

        frame[constants.TIME_INDEX] = pd.to_datetime(frame["rainfall_datetime"], errors="coerce")
        frame[variable] = pd.to_numeric(frame["rainfall_value"], errors="coerce")
        frame = frame[[constants.TIME_INDEX, variable]].dropna()
        if frame.empty:
            return empty

        frame = frame.set_index(constants.TIME_INDEX).sort_index()
        return frame[~frame.index.duplicated(keep="first")]

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetches recent hourly rainfall for one gauge.

        Args:
            gauge_id: The ThaiWater rain gauge id, e.g. ``"2005"``.
            variable: Must be ``constants.PRECIPITATION_HOURLY_SUM``.
            start_date: Optional start date in 'YYYY-MM-DD' format.
            end_date: Optional end date in 'YYYY-MM-DD' format.

        Returns:
            pd.DataFrame: A DataFrame indexed by ``constants.TIME_INDEX`` with a
            single ``precipitation_hourly_sum`` column in mm. **Empty unless the
            requested range overlaps the provider's rolling window of roughly
            the last 41 hours** — the provider publishes no rainfall history.

        Raises:
            ValueError: If the requested ``variable`` is not supported.
        """
        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        start_date = utils.format_start_date(start_date)
        end_date = utils.format_end_date(end_date)

        # Fail fast rather than spend a request on a range the provider cannot serve.
        window_start = pd.Timestamp.now().normalize() - pd.Timedelta(hours=self.WINDOW_HOURS)
        if pd.Timestamp(end_date) < window_start:
            logger.info(
                "ThaiWater publishes no rainfall before roughly %s; "
                "the request for %s to %s is outside that window.",
                window_start.date(),
                start_date,
                end_date,
            )
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)

        try:
            raw_data = self._download_data(gauge_id, variable, start_date, end_date)
            frame = self._parse_data(gauge_id, raw_data, variable)
        except Exception as exc:
            logger.error("Failed to get rainfall for station %s: %s", gauge_id, exc)
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)

        if frame.empty:
            return frame
        return frame[
            (frame.index >= pd.Timestamp(start_date))
            & (frame.index <= pd.Timestamp(end_date) + pd.Timedelta(days=1))
        ]
