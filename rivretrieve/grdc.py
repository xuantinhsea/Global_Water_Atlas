"""Fetcher for the GRDC station catalogue.

The Global Runoff Data Centre (GRDC) publishes a global station catalogue in
its public data portal:
https://portal.grdc.bafg.de/applications/public.html?publicuser=PublicUser#dataDownload/Stations

The catalogue includes station metadata and availability flags for daily and
monthly discharge records.

GRDC itself offers no API for its time series, by design: as custodian of data
from national services under WMO data-exchange rules, it releases them only
through its Data Portal, where each request goes through a form, the terms of
use are accepted and a download link is e-mailed within a day
(https://grdc.bafg.de/help/faq/). RivRetrieve does not automate that workflow.

Many GRDC stations are, however, gauges that a national service runs and
publishes itself, and the catalogue gives their national ID. Where that
service is one RivRetrieve supports and lists the ID, ``get_data`` downloads
the series from it — the same observations, from the agency that made them.
For other stations it returns an empty frame, and
:meth:`GRDCFetcher.unavailable_reason` says why.
"""

import importlib
import logging
import os
from functools import lru_cache
from typing import Any, Callable, Optional

import pandas as pd

from . import base, constants, utils

logger = logging.getLogger(__name__)

GRDC_SAMPLE_RECORDS_URL = "https://portal.grdc.bafg.de/grdc/grdc_sample_records.json"
GRDC_PORTAL_URL = (
    "https://portal.grdc.bafg.de/applications/public.html?publicuser=PublicUser#dataDownload/Stations"
)


def _same(national_id: str) -> list[str]:
    return [national_id]


#: GRDC country -> (RivRetrieve fetcher, ways its gauge ID may be written, env var it needs).
NATIONAL_SOURCES: dict[str, tuple[str, Callable[[str], list[str]], Optional[str]]] = {
    "United States": ("USAFetcher", lambda n: [n, n.zfill(8)], None),
    "Canada": ("CanadaFetcher", _same, None),
    "Brazil": ("BrazilFetcher", _same, None),
    "Australia": ("AustraliaFetcher", _same, None),
    "France": ("FranceFetcher", _same, None),
    # GRDC's UK IDs are NRFA station numbers.
    "United Kingdom": ("UKNRFAFetcher", _same, None),
    "Japan": ("JapanFetcher", _same, None),
    "Spain": ("SpainFetcher", _same, None),
    "Norway": ("NorwayFetcher", _same, "NVE_API_KEY"),
    "Poland": ("PolandFetcher", _same, None),
    "Lithuania": ("LithuaniaFetcher", _same, None),
    "South Africa": ("SouthAfricaFetcher", _same, None),
    "Chile": ("ChileFetcher", _same, None),
    "Portugal": ("PortugalFetcher", _same, None),
    "Slovenia": ("SloveniaFetcher", _same, None),
    # CHMI's object IDs carry a prefix GRDC leaves out.
    "Czech Republic": ("CzechFetcher", lambda n: [n, f"0-203-1-{n}"], None),
    "Germany": ("GermanyBerlinFetcher", _same, None),
}

#: Constructor arguments for national fetchers whose default is a multi-GB bulk
#: download: one GRDC station should never cost a whole national archive.
DEFAULT_NATIONAL_OPTIONS: dict[str, dict[str, Any]] = {
    "CanadaFetcher": {"source": "api"},
    "PolandFetcher": {"source": "direct"},
}

#: A month's mean is reported only when at least this share of its days has data.
MONTHLY_COVERAGE = 0.9


@lru_cache(maxsize=None)
def _national_ids(fetcher_name: str) -> frozenset:
    """Gauge IDs in a national fetcher's own cached catalogue."""
    fetcher_class = getattr(importlib.import_module("rivretrieve"), fetcher_name)
    return frozenset(fetcher_class.get_cached_metadata().index.astype(str).str.strip())


@lru_cache(maxsize=1)
def _grdc_sites() -> pd.DataFrame:
    """The cached catalogue, with national IDs kept as text (USGS IDs have leading zeros)."""
    path = os.path.join(os.path.dirname(__file__), "cached_site_data", "grdc_sites.csv")
    sites = pd.read_csv(
        path,
        dtype={constants.GAUGE_ID: str, "nat_id": str, constants.COUNTRY: str},
        usecols=[constants.GAUGE_ID, constants.COUNTRY, "nat_id"],
    )
    sites[constants.GAUGE_ID] = sites[constants.GAUGE_ID].str.strip()
    return sites.set_index(constants.GAUGE_ID)


def _to_number(value: Any) -> float:
    """Converts a cell to a number, returning NaN when conversion fails."""
    return pd.to_numeric(value, errors="coerce")


def _empty(variable: str) -> pd.DataFrame:
    return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)


class GRDCFetcher(base.RiverDataFetcher):
    """Fetches GRDC station metadata, and series from each station's national service.

    Data Source: GRDC Data Portal (https://portal.grdc.bafg.de/). No API key is
    required for the station catalogue endpoint used for metadata.

    Series come from the national service that runs the gauge, where RivRetrieve
    supports it (see the module notes and :data:`NATIONAL_SOURCES`). The frame's
    ``attrs["national_source"]`` names the fetcher and station used.

    Supported Variables:
        - ``constants.DISCHARGE_DAILY_MEAN`` (m³/s)
        - ``constants.DISCHARGE_MONTHLY_MEAN`` (m³/s) — calendar-month means of
          the daily series, for months with at least 90 % of days present.

    Args:
        national_options: Constructor arguments per national fetcher class name,
            merged over :data:`DEFAULT_NATIONAL_OPTIONS`.
    """

    def __init__(self, national_options: Optional[dict[str, dict[str, Any]]] = None):
        super().__init__()
        self.national_options = {**DEFAULT_NATIONAL_OPTIONS, **(national_options or {})}
        self._national_fetchers: dict[str, base.RiverDataFetcher] = {}

    @staticmethod
    def national_source(gauge_id: str) -> Optional[tuple[str, str]]:
        """``(fetcher class name, national gauge ID)`` for a GRDC station, or None.

        Only IDs that the national fetcher's own catalogue lists count as a match.
        """
        sites = _grdc_sites()
        gauge_id = str(gauge_id).strip()
        if gauge_id not in sites.index:
            return None
        site = sites.loc[gauge_id]
        if isinstance(site, pd.DataFrame):
            site = site.iloc[0]
        source = NATIONAL_SOURCES.get(str(site.get(constants.COUNTRY) or ""))
        national_id = site.get("nat_id")
        if source is None or not isinstance(national_id, str) or not national_id.strip():
            return None
        fetcher_name, spellings, _ = source
        known = _national_ids(fetcher_name)
        for candidate in spellings(national_id.strip()):
            if candidate in known:
                return fetcher_name, candidate
        return None

    @classmethod
    def unavailable_reason(cls, gauge_id: str) -> Optional[str]:
        """Why ``get_data`` cannot return this station's series, or None if it can."""
        match = cls.national_source(gauge_id)
        if match is None:
            return (
                f"GRDC station {gauge_id} is not published by a national service RivRetrieve "
                "supports, and GRDC releases its own copies only through the GRDC Data Portal: "
                f"request it there (the link arrives by e-mail): {GRDC_PORTAL_URL}"
            )
        fetcher_name, national_id = match
        needed = NATIONAL_SOURCES[_country_of(gauge_id)][2]
        if needed:
            importlib.import_module("rivretrieve")  # Loads rivretrieve/.env, as the fetchers do.
            if not os.environ.get(needed):
                return (
                    f"GRDC station {gauge_id} is national station {national_id}, which "
                    f"{fetcher_name} can only download with {needed} set."
                )
        return None

    def _national_fetcher(self, fetcher_name: str) -> base.RiverDataFetcher:
        if fetcher_name not in self._national_fetchers:
            fetcher_class = getattr(importlib.import_module("rivretrieve"), fetcher_name)
            self._national_fetchers[fetcher_name] = fetcher_class(**self.national_options.get(fetcher_name, {}))
        return self._national_fetchers[fetcher_name]

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
        """The daily series from the station's national service; None when there is none."""
        match = self.national_source(gauge_id)
        if match is None:
            return None
        fetcher_name, national_id = match
        fetcher = self._national_fetcher(fetcher_name)
        logger.info(f"GRDC station {gauge_id}: reading national station {national_id} with {fetcher_name}")
        frame = fetcher.get_data(
            gauge_id=national_id,
            variable=constants.DISCHARGE_DAILY_MEAN,
            start_date=start_date,
            end_date=end_date,
        )
        return frame, fetcher_name, national_id

    def _parse_data(self, gauge_id: str, raw_data: Any, variable: str) -> pd.DataFrame:
        if raw_data is None:
            return _empty(variable)
        daily, fetcher_name, national_id = raw_data
        if daily is None or daily.empty:
            return _empty(variable)

        series = pd.to_numeric(daily[constants.DISCHARGE_DAILY_MEAN], errors="coerce")
        series.index = pd.to_datetime(series.index)
        if series.index.tz is not None:
            series.index = series.index.tz_localize(None)
        series = series.dropna().sort_index()

        if variable == constants.DISCHARGE_MONTHLY_MEAN:
            months = series.resample("MS")
            coverage = months.count() / months.count().index.days_in_month
            series = months.mean()[coverage >= MONTHLY_COVERAGE]

        result = series.to_frame(name=variable)
        result.index.name = constants.TIME_INDEX
        result.attrs["national_source"] = {"fetcher": fetcher_name, "gauge_id": national_id}
        return result

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetches a GRDC station's discharge from the national service that runs it.

        Returns an empty frame when no supported national service publishes the
        station; :meth:`unavailable_reason` says why. Errors from the national
        service propagate as that fetcher raises them.
        """
        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        start_date = utils.format_start_date(start_date)
        end_date = utils.format_end_date(end_date)

        reason = self.unavailable_reason(gauge_id)
        if reason:
            logger.warning(reason)
            return _empty(variable)
        return self._parse_data(gauge_id, self._download_data(gauge_id, variable, start_date, end_date), variable)


def _country_of(gauge_id: str) -> str:
    site = _grdc_sites().loc[str(gauge_id).strip()]
    if isinstance(site, pd.DataFrame):
        site = site.iloc[0]
    return str(site.get(constants.COUNTRY) or "")
