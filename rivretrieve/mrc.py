"""Fetcher for Mekong basin telemetry from the Mekong River Commission.

The MRC's near-real-time monitoring service publishes water level and rainfall
from its HYCOS telemetry network across the lower Mekong — Laos, Cambodia,
Viet Nam and Thailand — plus two reporting stations in China. It is the only
openly reachable source of river gauge data for Laos, Cambodia and Viet Nam
found during the Southeast Asia survey, and it includes Tan Chau and My Thuan,
the two Mekong Delta gauges most often cited as hard to obtain.

.. warning::
    **Terms of use are not published for this feed.** These endpoints are
    unauthenticated and serve the MRC's public flood-warning map at
    https://monitoring.mrcmekong.org/, but the MRC's general data policy
    (PDIES) requires a licence agreement and fees for raw data, and the MRC
    has not stated how that applies here. Treat what this fetcher returns as
    the MRC's public near-real-time display, confirm with the MRC Secretariat
    before redistributing it, and use a formal data request for the historical
    archive at https://portal.mrcmekong.org/.
"""

import datetime
import logging
from typing import Any, Optional

import pandas as pd

from . import base, constants, utils

logger = logging.getLogger(__name__)

BASE_URL = "https://api.mrcmekong.org/api/v1/time-series/telemetry/recent"

#: The public feed is a fixed rolling window. Every date parameter tried
#: (startDate/endDate, start/end, from/to, days, limit) is ignored: the service
#: returns the same ~31 days regardless, so a historical request cannot be
#: served and must not be answered with recent data under the wrong dates.
WINDOW_DAYS = 31

#: Readings needed before a day gets a daily value. Telemetry arrives every
#: 5 to 15 minutes, so a full day is well over a hundred points; requiring 48
#: keeps partial days out without discarding a station that reports sparsely.
MIN_READINGS_PER_DAY = 48

#: The MRC's own map sends these, and the API is friendlier when they are present.
HEADERS = {"Referer": "https://monitoring.mrcmekong.org/"}


class MRCFetcher(base.RiverDataFetcher):
    """Fetches Mekong basin water level and rainfall from the MRC's public feed.

    Data Source: Mekong River Commission near-real-time hydrometeorological
    monitoring (https://monitoring.mrcmekong.org/). No API key is required for
    the endpoints used here.

    Supported Variables:
        - ``constants.STAGE_INSTANT`` (m)
        - ``constants.STAGE_DAILY_MEAN`` (m)
        - ``constants.PRECIPITATION_HOURLY_SUM`` (mm)

    .. warning::
        **No history.** The feed is a rolling window of roughly the last
        :data:`WINDOW_DAYS` days at 5 to 15 minute resolution. Requests for
        earlier periods return an empty DataFrame. The MRC's historical archive
        is a separate, licensed service.

    .. note::
        Water level datums are not consistent across the network: the Chinese
        stations report metres above sea level (Jinghong sits near 535 m) while
        the lower-basin gauges report gauge height (Tan Chau near 3 m). Compare
        a station against its own ``floodStage`` and ``alarmStage``, which this
        fetcher keeps in the metadata, rather than against other stations.

    .. note::
        Timestamps are UTC as published, returned timezone-naive. Rainfall is
        the increment accumulated since the previous reading, in mm.
    """

    STATIONS_URL = f"{BASE_URL}/stations"
    STATION_URL = f"{BASE_URL}/station"
    MEASUREMENT_URL = f"{BASE_URL}/measurement"

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of available MRC telemetry stations.

        This method loads the metadata from a cached CSV file located in
        the ``rivretrieve/cached_site_data/`` directory.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return utils.load_cached_metadata_csv("mrc")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (
            constants.STAGE_INSTANT,
            constants.STAGE_DAILY_MEAN,
            constants.PRECIPITATION_HOURLY_SUM,
        )

    # -- metadata ----------------------------------------------------------------

    def get_metadata(self) -> pd.DataFrame:
        """Downloads the current station list from the MRC.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        session = utils.requests_retry_session()
        response = session.get(self.STATIONS_URL, headers=HEADERS, timeout=90)
        response.raise_for_status()
        return self._parse_metadata(response.json())

    def _parse_metadata(self, payload: Any) -> pd.DataFrame:
        records = []
        for entry in payload or []:
            station_id = entry.get("stationId")
            if not station_id:
                continue
            records.append(
                {
                    constants.GAUGE_ID: str(station_id),
                    constants.STATION_NAME: (entry.get("name") or "").strip() or None,
                    constants.LATITUDE: pd.to_numeric(entry.get("latitude"), errors="coerce"),
                    constants.LONGITUDE: pd.to_numeric(entry.get("longitude"), errors="coerce"),
                    constants.RIVER: (entry.get("river") or "").strip() or None,
                    constants.COUNTRY: (entry.get("country") or "").strip() or None,
                    constants.SOURCE: "Mekong River Commission",
                    "station_type": entry.get("stationType"),
                    "flood_stage": entry.get("floodStage"),
                    "alarm_stage": entry.get("alarmStage"),
                    "wl_sensor": entry.get("wlSensor"),
                    "rainfall_sensor": entry.get("rainfallSensor"),
                    "wl_sensor_type": entry.get("wlSensorType"),
                    "last_measurement": entry.get("lastMeasurement"),
                }
            )
        frame = pd.DataFrame(records)
        if frame.empty:
            return frame
        return frame.drop_duplicates(subset=[constants.GAUGE_ID]).set_index(constants.GAUGE_ID)

    # -- data --------------------------------------------------------------------

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> list[dict]:
        """Downloads the rolling measurement window for one station.

        ``start_date`` and ``end_date`` are accepted to satisfy the base class;
        the service ignores them, so only one request is made and the caller
        filters the result.
        """
        session = utils.requests_retry_session()
        try:
            response = session.get(
                f"{self.MEASUREMENT_URL}/{gauge_id}", headers=HEADERS, timeout=90
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("MRC request failed for station %s: %s", gauge_id, exc)
            return []

        if not isinstance(payload, dict):
            logger.warning("Unexpected MRC payload for station %s", gauge_id)
            return []
        return payload.get("measurements") or []

    def _parse_data(self, gauge_id: str, raw_data: list[dict], variable: str) -> pd.DataFrame:
        """Parses the compact ``{d, w, r, b}`` measurement records."""
        empty = pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)
        if not raw_data:
            return empty

        frame = pd.DataFrame(raw_data)
        if "d" not in frame.columns:
            logger.warning("MRC payload for station %s has no timestamps", gauge_id)
            return empty

        # 'w' is water level in metres, 'r' the rainfall increment in mm since
        # the previous reading, 'b' the logger battery voltage (ignored here).
        source_column = "r" if variable.startswith("precipitation") else "w"
        if source_column not in frame.columns:
            logger.warning("MRC payload for station %s has no %r column", gauge_id, source_column)
            return empty

        frame[constants.TIME_INDEX] = pd.to_datetime(
            frame["d"], errors="coerce", utc=True
        ).dt.tz_localize(None)
        frame[variable] = pd.to_numeric(frame[source_column], errors="coerce")
        frame = frame[[constants.TIME_INDEX, variable]].dropna()
        if frame.empty:
            return empty

        frame = frame.set_index(constants.TIME_INDEX).sort_index()
        frame = frame[~frame.index.duplicated(keep="first")]

        if variable == constants.PRECIPITATION_HOURLY_SUM:
            # Increments per reading add up to the hourly total.
            hourly = frame[variable].resample("1h").sum(min_count=1).dropna()
            hourly.index.name = constants.TIME_INDEX
            return hourly.to_frame(name=variable)

        if variable == constants.STAGE_DAILY_MEAN:
            grouped = frame[variable].groupby(frame.index.normalize())
            daily = grouped.mean()[grouped.count() >= MIN_READINGS_PER_DAY]
            daily.index.name = constants.TIME_INDEX
            return daily.to_frame(name=variable)

        return frame

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetches recent telemetry for one MRC station.

        Args:
            gauge_id: The MRC station code, e.g. ``"019803"`` for Tan Chau.
            variable: One of ``get_available_variables()``.
            start_date: Optional start date in 'YYYY-MM-DD' format.
            end_date: Optional end date in 'YYYY-MM-DD' format.

        Returns:
            pd.DataFrame: A DataFrame indexed by ``constants.TIME_INDEX`` with a
            single column named after the requested ``variable``. **Empty unless
            the requested range overlaps the rolling window of roughly the last
            31 days** — the public feed carries no history.

        Raises:
            ValueError: If the requested ``variable`` is not supported.
        """
        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        start_date = utils.format_start_date(start_date)
        end_date = utils.format_end_date(end_date)

        window_start = pd.Timestamp.now("UTC").tz_localize(None).normalize() - pd.Timedelta(
            days=WINDOW_DAYS
        )
        if pd.Timestamp(end_date) < window_start:
            logger.info(
                "The MRC public feed only covers roughly the last %d days (from %s); "
                "the request for %s to %s is outside it. Historical MRC data is a "
                "separate licensed service at https://portal.mrcmekong.org/.",
                WINDOW_DAYS,
                window_start.date(),
                start_date,
                end_date,
            )
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(
                constants.TIME_INDEX
            )

        try:
            raw_data = self._download_data(gauge_id, variable, start_date, end_date)
            frame = self._parse_data(gauge_id, raw_data, variable)
        except Exception as exc:
            logger.error("Failed to get MRC data for station %s: %s", gauge_id, exc)
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(
                constants.TIME_INDEX
            )

        if frame.empty:
            return frame
        return frame[
            (frame.index >= pd.Timestamp(start_date))
            & (frame.index <= pd.Timestamp(end_date) + datetime.timedelta(days=1))
        ]
