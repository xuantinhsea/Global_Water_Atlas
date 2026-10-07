"""Fetcher for the GRDC station catalogue.

The Global Runoff Data Centre (GRDC) publishes a global station catalogue in
its public data portal:
https://portal.grdc.bafg.de/applications/public.html?publicuser=PublicUser#dataDownload/Stations

The catalogue includes station metadata and availability flags for daily and
monthly discharge records.

.. warning::
    GRDC's public portal currently exposes downloads through an interactive
    export workflow rather than a documented per-station API endpoint.
    ``get_data`` therefore returns an empty frame for now.
"""

import logging
from typing import Any, Optional

import pandas as pd

from . import base, constants, utils

logger = logging.getLogger(__name__)

GRDC_SAMPLE_RECORDS_URL = "https://portal.grdc.bafg.de/grdc/grdc_sample_records.json"
GRDC_PORTAL_URL = (
    "https://portal.grdc.bafg.de/applications/public.html?publicuser=PublicUser#dataDownload/Stations"
)


def _to_number(value: Any) -> float:
    """Converts a cell to a number, returning NaN when conversion fails."""
    return pd.to_numeric(value, errors="coerce")


def _empty(variable: str) -> pd.DataFrame:
    return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)


class GRDCFetcher(base.RiverDataFetcher):
    """Fetches GRDC station metadata and declared discharge availability.

    Data Source: GRDC Data Portal (https://portal.grdc.bafg.de/). No API key is
    required for the station catalogue endpoint used for metadata.

    Supported Variables:
        - ``constants.DISCHARGE_DAILY_MEAN`` (m³/s)
        - ``constants.DISCHARGE_MONTHLY_MEAN`` (m³/s)
    """

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of available GRDC stations from cached CSV."""
        return utils.load_cached_metadata_csv("grdc")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (constants.DISCHARGE_DAILY_MEAN, constants.DISCHARGE_MONTHLY_MEAN)

    def get_metadata(self) -> pd.DataFrame:
        """Downloads the GRDC station catalogue from the public portal."""
        session = utils.requests_retry_session()
        response = session.get(GRDC_SAMPLE_RECORDS_URL, timeout=120)
        response.raise_for_status()
        return self._parse_metadata(response.json())

    def _parse_metadata(self, payload: Any) -> pd.DataFrame:
        if not isinstance(payload, list):
            raise ValueError("Expected a list of station records from GRDC sample records endpoint.")

        records: list[dict[str, Any]] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            gauge_id = entry.get("grdc_no")
            if gauge_id is None:
                continue

            daily_years = _to_number(entry.get("d_yrs"))
            monthly_years = _to_number(entry.get("m_yrs"))

            records.append(
                {
                    constants.GAUGE_ID: str(gauge_id),
                    constants.STATION_NAME: (entry.get("station") or "").strip() or None,
                    constants.RIVER: (entry.get("river") or "").strip() or None,
                    constants.COUNTRY: (entry.get("country") or "").strip() or None,
                    constants.LATITUDE: _to_number(entry.get("lat")),
                    constants.LONGITUDE: _to_number(entry.get("long")),
                    constants.AREA: _to_number(entry.get("area")),
                    constants.ALTITUDE: _to_number(entry.get("altitude")),
                    constants.SOURCE: "GRDC Data Portal",
                    "wmo_reg": entry.get("wmo_reg"),
                    "sub_reg": entry.get("sub_reg"),
                    "subregion_name": entry.get("subregion_name"),
                    "river_basin": entry.get("river_basin"),
                    "provider_id": entry.get("provider_id"),
                    "nat_id": entry.get("nat_id"),
                    "ds_stat_no": entry.get("ds_stat_no"),
                    "timeseries_type": entry.get("timeseries_type"),
                    "d_start": _to_number(entry.get("d_start")),
                    "d_end": _to_number(entry.get("d_end")),
                    "d_yrs": daily_years,
                    "d_miss": _to_number(entry.get("d_miss")),
                    "m_start": _to_number(entry.get("m_start")),
                    "m_end": _to_number(entry.get("m_end")),
                    "m_yrs": monthly_years,
                    "m_miss": _to_number(entry.get("m_miss")),
                    "t_start": _to_number(entry.get("t_start")),
                    "t_end": _to_number(entry.get("t_end")),
                    "t_yrs": _to_number(entry.get("t_yrs")),
                    "lta_discharge": _to_number(entry.get("lta_discharge")),
                    "r_volume_yr": _to_number(entry.get("r_volume_yr")),
                    "r_height_yr": _to_number(entry.get("r_height_yr")),
                    constants.DISCHARGE_DAILY_MEAN: bool(pd.notna(daily_years) and daily_years > 0),
                    constants.DISCHARGE_MONTHLY_MEAN: bool(
                        pd.notna(monthly_years) and monthly_years > 0
                    ),
                }
            )

        frame = pd.DataFrame(records)
        if frame.empty:
            return frame.set_index(constants.GAUGE_ID)
        return frame.drop_duplicates(subset=[constants.GAUGE_ID]).set_index(constants.GAUGE_ID)

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> Any:
        """No direct per-station endpoint is wired yet for automated downloads."""
        return None

    def _parse_data(self, gauge_id: str, raw_data: Any, variable: str) -> pd.DataFrame:
        return _empty(variable)

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        # Keep date validation behaviour consistent with other fetchers.
        utils.format_start_date(start_date)
        utils.format_end_date(end_date)

        logger.warning(
            "GRDC publishes metadata openly, but station downloads currently run through an "
            "interactive export workflow. There is no stable per-station endpoint wired in "
            "RivRetrieve yet, so get_data(%s, %s) returns an empty frame. "
            "Use the GRDC portal for now: %s",
            gauge_id,
            variable,
            GRDC_PORTAL_URL,
        )
        return _empty(variable)
