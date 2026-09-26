"""Fetcher for Philippine river and rainfall data from DOST-ASTI PhilSensors.

The Advanced Science and Technology Institute of the Philippine Department of
Science and Technology operates the country's automated hydromet network and
publishes it at https://philsensors.asti.dost.gov.ph/.

The network splits into four station groups with disjoint id spaces, so one
gauge id is unambiguous across all of them:

=========  ======  =========================================================
Group      Count   What it measures
=========  ======  =========================================================
``wlms``      561  Water level monitoring stations
``arg``      1131  Automated rain gauges
``tandem``    231  Both water level and rainfall
``aws``       209  Automatic weather stations, including rainfall
=========  ======  =========================================================

.. warning::
    **Station metadata is open; readings are not.** The station catalogue comes
    from an unauthenticated GeoServer WFS, which is why all 2,132 stations
    appear on a map without any credentials. The readings API returns HTTP 401
    without an access token, and DOST-ASTI issues those through a data request:
    a signed end-user licence agreement, non-commercial research, academic or
    disaster-management use only, and no redistribution. See
    https://philsensors.asti.dost.gov.ph/datarequest/terms.

    Set ``PHILSENSORS_TOKEN`` in ``rivretrieve/.env`` once you have one, or pass
    ``access_token`` to the constructor. Without it, ``get_data`` returns an
    empty DataFrame and logs what is missing — it never fails silently.
"""

import logging
import os
from typing import Any, Optional

import pandas as pd
from dotenv import load_dotenv

from . import base, constants, utils

logger = logging.getLogger(__name__)

load_dotenv(dotenv_path=os.path.join((os.path.dirname(__file__)), ".env"))

ACCESS_TOKEN = os.environ.get("PHILSENSORS_TOKEN")

BASE_URL = "https://philsensors.asti.dost.gov.ph"
WFS_URL = f"{BASE_URL}/geoserver/philsensors/ows"
DATA_URL = f"{BASE_URL}/api/data"

#: WFS layers that carry stations worth keeping, and what each group measures.
#: ``metbuoy`` is a single marine buoy and is left out.
STATION_GROUPS: dict[str, tuple[str, ...]] = {
    "wlms": (constants.STAGE_INSTANT,),
    "arg": (constants.PRECIPITATION_HOURLY_SUM,),
    "tandem": (constants.STAGE_INSTANT, constants.PRECIPITATION_HOURLY_SUM),
    "aws": (constants.PRECIPITATION_HOURLY_SUM,),
}

#: The provider's own parameter names, as used by the readings API.
PARAM_NAMES = {
    constants.STAGE_INSTANT: "Water Level",
    constants.PRECIPITATION_HOURLY_SUM: "Rainfall Amount",
}


class PhilippinesFetcher(base.RiverDataFetcher):
    """Fetches Philippine water level and rainfall from DOST-ASTI PhilSensors.

    Data Source: DOST-ASTI PhilSensors (https://philsensors.asti.dost.gov.ph/)

    Supported Variables:
        - ``constants.STAGE_INSTANT`` (m)
        - ``constants.PRECIPITATION_HOURLY_SUM`` (mm)

    Which of the two a given station reports depends on its group, and the
    cached metadata records that per station rather than per provider.

    .. warning::
        Readings require an access token from DOST-ASTI. See the module
        docstring. Station metadata needs none.

    Keys in ``.env``: ``PHILSENSORS_TOKEN``
    """

    def __init__(self, access_token: Optional[str] = None):
        super().__init__()
        self.access_token = access_token or ACCESS_TOKEN
        if not self.access_token:
            logger.info(
                "No PhilSensors access token. Station metadata still works; readings need "
                "PHILSENSORS_TOKEN in your .env file, obtained from DOST-ASTI at "
                "%s/datarequest/terms",
                BASE_URL,
            )

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of available Philippine stations.

        This method loads the metadata from a cached CSV file located in
        the ``rivretrieve/cached_site_data/`` directory.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return utils.load_cached_metadata_csv("philippines")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (constants.STAGE_INSTANT, constants.PRECIPITATION_HOURLY_SUM)

    # -- metadata ----------------------------------------------------------------

    def get_metadata(self) -> pd.DataFrame:
        """Downloads the station catalogue from the open GeoServer WFS.

        Needs no credentials — this is why the whole network can be mapped
        even though the readings are gated.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        session = utils.requests_retry_session()
        frames = []
        for group in STATION_GROUPS:
            params = {
                "service": "WFS",
                "version": "1.0.0",
                "request": "GetFeature",
                "typeName": f"philsensors:{group}",
                "maxFeatures": 9000,
                "outputFormat": "application/json",
            }
            try:
                response = session.get(WFS_URL, params=params, timeout=120)
                response.raise_for_status()
                frames.append(self._parse_metadata(response.json(), group))
            except Exception as exc:
                logger.warning("PhilSensors WFS layer %s failed: %s", group, exc)

        frames = [frame for frame in frames if not frame.empty]
        if not frames:
            return pd.DataFrame()
        combined = pd.concat(frames)
        return combined[~combined.index.duplicated(keep="first")]

    def _parse_metadata(self, payload: dict, group: str) -> pd.DataFrame:
        records = []
        for feature in (payload or {}).get("features", []):
            props = feature.get("properties") or {}
            station_id = props.get("station_id")
            if station_id is None:
                continue
            records.append(
                {
                    constants.GAUGE_ID: str(station_id),
                    constants.STATION_NAME: (props.get("full_location") or props.get("location") or "").strip()
                    or None,
                    constants.LATITUDE: pd.to_numeric(props.get("latitude"), errors="coerce"),
                    constants.LONGITUDE: pd.to_numeric(props.get("longitude"), errors="coerce"),
                    constants.COUNTRY: "Philippines",
                    constants.SOURCE: "DOST-ASTI PhilSensors",
                    "station_group": group,
                    "municipality": props.get("municipality"),
                    "province": props.get("province"),
                    "region": props.get("region"),
                    "date_installed": (props.get("date_installed") or "")[:10] or None,
                    # Per-station availability, the same shape Norway publishes.
                    constants.STAGE_INSTANT: constants.STAGE_INSTANT in STATION_GROUPS[group],
                    constants.PRECIPITATION_HOURLY_SUM: (
                        constants.PRECIPITATION_HOURLY_SUM in STATION_GROUPS[group]
                    ),
                }
            )
        frame = pd.DataFrame(records)
        if frame.empty:
            return frame
        return frame.drop_duplicates(subset=[constants.GAUGE_ID]).set_index(constants.GAUGE_ID)

    # -- data --------------------------------------------------------------------

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> Any:
        """Requests readings. Returns None when there is no access token."""
        if not self.access_token:
            return None

        session = utils.requests_retry_session()
        params = {
            "stationId": gauge_id,
            "paramNames[0]": PARAM_NAMES[variable],
            "startDate": start_date,
            "endDate": end_date,
            "access-token": self.access_token,
        }
        try:
            response = session.get(f"{DATA_URL}/parameters", params=params, timeout=120)
            if response.status_code == 401:
                logger.error(
                    "PhilSensors rejected the access token for station %s. "
                    "Tokens are issued by DOST-ASTI and can be revoked.",
                    gauge_id,
                )
                return None
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.warning("PhilSensors request failed for station %s: %s", gauge_id, exc)
            return None

    def _parse_data(self, gauge_id: str, raw_data: Any, variable: str) -> pd.DataFrame:
        empty = pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)
        if not raw_data:
            return empty

        rows = raw_data.get("data") if isinstance(raw_data, dict) else raw_data
        if not isinstance(rows, list) or not rows:
            return empty

        frame = pd.DataFrame(rows)
        time_column = next(
            (c for c in ("dateTimeRead", "datetime_read", "datetime", "date") if c in frame.columns),
            None,
        )
        value_column = next(
            (c for c in ("value", "paramValue", "reading") if c in frame.columns), None
        )
        if time_column is None or value_column is None:
            logger.warning(
                "Unexpected PhilSensors payload for station %s; columns were %s",
                gauge_id,
                list(frame.columns),
            )
            return empty

        frame[constants.TIME_INDEX] = pd.to_datetime(frame[time_column], errors="coerce")
        frame[variable] = pd.to_numeric(frame[value_column], errors="coerce")
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
        """Fetches readings for one Philippine station.

        Args:
            gauge_id: The PhilSensors station id, e.g. ``"212"``.
            variable: One of ``get_available_variables()``.
            start_date: Optional start date in 'YYYY-MM-DD' format.
            end_date: Optional end date in 'YYYY-MM-DD' format.

        Returns:
            pd.DataFrame: A DataFrame indexed by ``constants.TIME_INDEX`` with a
            single column named after the requested ``variable``. **Empty when
            no DOST-ASTI access token is configured** — the station catalogue is
            open but the readings are not.

        Raises:
            ValueError: If the requested ``variable`` is not supported.
        """
        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        start_date = utils.format_start_date(start_date)
        end_date = utils.format_end_date(end_date)

        if not self.access_token:
            logger.warning(
                "Cannot download station %s: PhilSensors readings need an access token. "
                "Request one from DOST-ASTI at %s/datarequest/terms and set "
                "PHILSENSORS_TOKEN in rivretrieve/.env",
                gauge_id,
                BASE_URL,
            )
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(
                constants.TIME_INDEX
            )

        try:
            raw_data = self._download_data(gauge_id, variable, start_date, end_date)
            frame = self._parse_data(gauge_id, raw_data, variable)
        except Exception as exc:
            logger.error("Failed to get PhilSensors data for station %s: %s", gauge_id, exc)
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(
                constants.TIME_INDEX
            )

        if frame.empty:
            return frame
        return frame[
            (frame.index >= pd.Timestamp(start_date))
            & (frame.index <= pd.Timestamp(end_date) + pd.Timedelta(days=1))
        ]
