"""Fetcher for the GRDC station catalogue.

The Global Runoff Data Centre (GRDC) publishes a global station catalogue in
its public data portal:
https://portal.grdc.bafg.de/applications/public.html?publicuser=PublicUser#dataDownload/Stations

The catalogue includes station metadata and availability flags for daily and
monthly discharge records.

GRDC offers no API for its time series, by design: as custodian of data from
national services under WMO data-exchange rules, it releases most of them only
through its Data Portal, where each request goes through a form, the terms of
use are accepted and a download link is e-mailed within a day
(https://grdc.bafg.de/help/faq/). RivRetrieve does not automate that workflow.

``get_data`` reads a station from the first of these that has it:

1. **GRDC-Caravan**, GRDC's own openly licensed dataset (CC BY 4.0, daily,
   1950-2023), for the 5,356 stations whose owners allow open release. One
   station's file is read out of the 8.8 GB archive on Zenodo with a single
   HTTP range request, located by ``cached_site_data/grdc_caravan_index.csv``.
2. The **national service** that runs the gauge, where RivRetrieve supports it
   and its catalogue lists the GRDC station's national ID — the same
   observations, from the agency that made them. This also covers years after
   GRDC-Caravan ends.

For other stations it returns an empty frame, and
:meth:`GRDCFetcher.unavailable_reason` says why.
"""

import importlib
import io
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

#: GRDC-Caravan v0.6 (Färber et al., 2025), GRDC's openly licensed (CC BY 4.0) daily
#: discharge for 5,356 stations, 1950-2023: one CSV per station in this ZIP.
GRDC_CARAVAN_URL = "https://zenodo.org/api/records/15349031/files/GRDC_Caravan_extension_csv.zip/content"
GRDC_CARAVAN_DOI = "https://doi.org/10.5281/zenodo.15349031"
#: Caravan gives streamflow as runoff depth: mm/day × km² / 86.4 = m³/s.
MM_PER_DAY_TO_M3S_PER_KM2 = 86.4


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
def _caravan_index() -> pd.DataFrame:
    """Where each GRDC-Caravan station's CSV sits in the archive, and its catchment area."""
    path = os.path.join(os.path.dirname(__file__), "cached_site_data", "grdc_caravan_index.csv")
    return pd.read_csv(path, dtype={constants.GAUGE_ID: str}).set_index(constants.GAUGE_ID)


@lru_cache(maxsize=8)
def _caravan_daily(gauge_id: str) -> pd.Series:
    """A GRDC-Caravan station's whole daily record in m³/s.

    Cached because a preview and a download of the same station read the same
    file; callers must not modify the result.
    """
    row = _caravan_index().loc[gauge_id]
    raw = utils.read_zip_member(
        utils.requests_retry_session(),
        GRDC_CARAVAN_URL,
        int(row["header_offset"]),
        int(row["compress_size"]),
        int(row["compress_type"]),
    )
    table = pd.read_csv(io.BytesIO(raw), usecols=["date", "streamflow"])
    runoff_mm_per_day = pd.to_numeric(table["streamflow"], errors="coerce").to_numpy()
    series = pd.Series(
        runoff_mm_per_day * float(row["area_km2"]) / MM_PER_DAY_TO_M3S_PER_KM2,
        index=pd.DatetimeIndex(pd.to_datetime(table["date"]), name=constants.TIME_INDEX),
    )
    return series.dropna()


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
    """Fetches GRDC station metadata and discharge series.

    Data Source: GRDC Data Portal (https://portal.grdc.bafg.de/). No API key is
    required for the station catalogue endpoint used for metadata.

    Series come from GRDC's open GRDC-Caravan dataset where it has the station,
    and otherwise from the national service that runs the gauge, where
    RivRetrieve supports it (see the module notes and :data:`NATIONAL_SOURCES`).
    The frame's ``attrs`` say which: ``"grdc_caravan"`` (the dataset's DOI) or
    ``"national_source"`` (the fetcher and national station ID).

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

    @staticmethod
    def in_caravan(gauge_id: str) -> bool:
        """Whether GRDC's open GRDC-Caravan dataset carries this station."""
        return str(gauge_id).strip() in _caravan_index().index

    @classmethod
    def download_source(cls, gauge_id: str) -> Optional[dict[str, str]]:
        """Where ``get_data`` reads this station from first, or None if nowhere.

        ``{"kind": "grdc_caravan", "doi": ...}`` or
        ``{"kind": "national", "fetcher": ..., "gauge_id": ...}``.
        """
        if cls.in_caravan(gauge_id):
            return {"kind": "grdc_caravan", "doi": GRDC_CARAVAN_DOI}
        match = cls.national_source(gauge_id)
        if match:
            return {"kind": "national", "fetcher": match[0], "gauge_id": match[1]}
        return None

    @classmethod
    def unavailable_reason(cls, gauge_id: str) -> Optional[str]:
        """Why ``get_data`` cannot return this station's series, or None if it can."""
        if cls.in_caravan(gauge_id):
            return None
        match = cls.national_source(gauge_id)
        if match is None:
            return (
                f"GRDC station {gauge_id} is not in GRDC's open dataset (GRDC-Caravan), and no national "
                "service RivRetrieve supports publishes it. GRDC releases it only on request through the "
                f"GRDC Data Portal: request it there (the link arrives by e-mail): {GRDC_PORTAL_URL}"
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
        """The daily series in m³/s and the ``attrs`` naming its source; None when nowhere has it.

        GRDC-Caravan first. The national service is asked when the station is not
        in GRDC-Caravan, or GRDC-Caravan has nothing in the range (it ends in 2023).
        """
        gauge_id = str(gauge_id).strip()
        coverage = None
        if self.in_caravan(gauge_id):
            record = _caravan_daily(gauge_id)
            daily = record[(record.index >= pd.Timestamp(start_date)) & (record.index <= pd.Timestamp(end_date))]
            if not daily.empty:
                logger.info(f"GRDC station {gauge_id}: read from GRDC-Caravan")
                return daily, {"grdc_caravan": GRDC_CARAVAN_DOI}
            if not record.empty:
                # GRDC's catalogue often runs years past its open snapshot; say what there is.
                coverage = (
                    f"GRDC-Caravan has this station from {record.index.min().date()} to "
                    f"{record.index.max().date()}; later years are released only on request "
                    "through the GRDC Data Portal."
                )

        match = self.national_source(gauge_id)
        if match is None:
            return None if coverage is None else (None, {"coverage_note": coverage})
        fetcher_name, national_id = match
        fetcher = self._national_fetcher(fetcher_name)
        logger.info(f"GRDC station {gauge_id}: reading national station {national_id} with {fetcher_name}")
        frame = fetcher.get_data(
            gauge_id=national_id,
            variable=constants.DISCHARGE_DAILY_MEAN,
            start_date=start_date,
            end_date=end_date,
        )
        if frame is None or frame.empty:
            return None
        series = pd.to_numeric(frame[constants.DISCHARGE_DAILY_MEAN], errors="coerce")
        series.index = pd.to_datetime(series.index)
        if series.index.tz is not None:
            series.index = series.index.tz_localize(None)
        return series, {"national_source": {"fetcher": fetcher_name, "gauge_id": national_id}}

    def _parse_data(self, gauge_id: str, raw_data: Any, variable: str) -> pd.DataFrame:
        if raw_data is None:
            return _empty(variable)
        series, source = raw_data
        if series is None:
            # Nothing in range; ``attrs["coverage_note"]`` says what the station does have.
            empty = _empty(variable)
            empty.attrs.update(source)
            return empty
        series = series.dropna().sort_index()

        if variable == constants.DISCHARGE_MONTHLY_MEAN:
            months = series.resample("MS")
            coverage = months.count() / months.count().index.days_in_month
            series = months.mean()[coverage >= MONTHLY_COVERAGE]

        result = series.to_frame(name=variable)
        result.index.name = constants.TIME_INDEX
        result.attrs.update(source)
        return result

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetches a GRDC station's discharge from GRDC-Caravan or its national service.

        Returns an empty frame when neither has the station; :meth:`unavailable_reason`
        says why. Errors from the source propagate as it raises them.
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
