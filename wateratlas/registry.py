"""Provider registry.

The single place that knows how each RivRetrieve fetcher maps onto the atlas'
unified station model: which cached CSV columns mean what, which providers need
credentials, and which do an expensive bulk download on first use.

Everything here is derived from the fetcher sources in ``rivretrieve/`` — if a
fetcher changes, this file is what needs updating.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from . import paths

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Canonical column names for the unified station record.
# These follow rivretrieve.constants wherever the library already names a field.
# --------------------------------------------------------------------------------------

STATION_KEY = "station_key"
COUNTRY = "country"
GAUGE_ID = "gauge_id"
STATION_NAME = "station_name"
RIVER = "river"
LATITUDE = "latitude"
LONGITUDE = "longitude"
ALTITUDE = "altitude"
AREA = "area"
HAS_COORDS = "has_coords"
VARIABLES = "variables"

CORE_COLUMNS = (
    STATION_KEY,
    COUNTRY,
    GAUGE_ID,
    STATION_NAME,
    RIVER,
    LATITUDE,
    LONGITUDE,
    HAS_COORDS,
    ALTITUDE,
    AREA,
    VARIABLES,
)

# Column spellings seen across the cached CSVs that mean a canonical field.
# The design doc (docs/design_docs/data_fetcher.md §5) names the standard, but
# several CSVs predate it.
COMMON_ALIASES: dict[str, str] = {
    "gauge_name": STATION_NAME,
    "gauge_altitude": ALTITUDE,
}


@dataclass(frozen=True)
class Provider:
    """One RivRetrieve data provider, as the atlas sees it."""

    key: str
    """Short code used in URLs and as the station_key prefix, e.g. ``"usa"``."""

    fetcher: str
    """Class name exported from the ``rivretrieve`` package."""

    label: str
    """Provider name as it should appear in the UI, e.g. ``"USGS NWIS"``."""

    country_name: str
    """Human-readable territory, e.g. ``"United States"``."""

    source_url: str
    """Provider homepage. Shown in the detail panel and written into ATTRIBUTION.md."""

    credentials: tuple[str, ...] = ()
    """Environment variable names that must be set for this fetcher to work."""

    bulk_first_use: Optional[str] = None
    """Human description of the one-time download this fetcher does on first call."""

    cache_glob: Optional[str] = None
    """Glob under ``rivretrieve/data/`` that exists once the bulk cache is warm."""

    source_crs: Optional[str] = None
    """EPSG code of ``coord_columns`` when the CSV has no lat/lon of its own."""

    coord_columns: Optional[tuple[str, str]] = None
    """``(x_column, y_column)`` to reproject from ``source_crs`` into WGS84."""

    aliases: dict[str, str] = field(default_factory=dict)
    """Provider-specific column renames applied on top of ``COMMON_ALIASES``."""

    availability_columns: bool = False
    """True when the CSV carries one boolean column per variable."""

    throttle_note: Optional[str] = None
    """Why bulk downloads from this provider are slow. Shown before a job starts."""

    seconds_per_station: float = 2.0
    """Rough wall-clock cost of one get_data() call, used for job estimates."""

    hosted_fetcher_kwargs: dict[str, object] = field(default_factory=dict)
    """Constructor arguments for the hosted atlas, which cannot keep a bulk cache.

    Set for the bulk-cache providers whose fetcher can instead read one station
    at a time, e.g. ``{"source": "api"}``. Their stations then download hosted too.
    """

    hosted_seconds_per_station: Optional[float] = None
    """``seconds_per_station`` when built with ``hosted_fetcher_kwargs``."""

    # -- derived -------------------------------------------------------------------

    def fetcher_class(self):
        """Imports and returns the fetcher class. Raises ImportError if deps are missing."""
        module = importlib.import_module("rivretrieve")
        return getattr(module, self.fetcher)

    def build_fetcher(self):
        """Instantiates the fetcher.

        Norway reads its credentials from ``rivretrieve/.env`` at construction
        time, so a fetcher built without them will refuse to fetch.
        """
        kwargs = self.hosted_fetcher_kwargs if paths.HOSTED else {}
        return self.fetcher_class()(**kwargs)

    def uses_bulk_cache(self) -> bool:
        """Whether downloads here go through the provider's one-time bulk download."""
        return bool(self.bulk_first_use) and not (paths.HOSTED and self.hosted_fetcher_kwargs)

    @property
    def bulk_note(self) -> Optional[str]:
        """``bulk_first_use``, but only where this deployment actually does the bulk download."""
        return self.bulk_first_use if self.uses_bulk_cache() else None

    def station_seconds(self) -> float:
        """Rough cost of one station in this deployment, for job estimates."""
        if paths.HOSTED and self.hosted_fetcher_kwargs and self.hosted_seconds_per_station is not None:
            return self.hosted_seconds_per_station
        return self.seconds_per_station

    def declared_variables(self) -> tuple[str, ...]:
        """Variables this provider says it supports, via ``get_available_variables()``."""
        try:
            return tuple(self.fetcher_class().get_available_variables())
        except Exception as exc:  # pragma: no cover - depends on optional deps
            logger.warning("Could not read variables for %s: %s", self.key, exc)
            return ()

    def missing_credentials(self) -> list[str]:
        """Environment variables this provider needs that are not currently set."""
        return [name for name in self.credentials if not os.environ.get(name)]

    def cache_is_warm(self) -> Optional[bool]:
        """True/False for providers with a bulk cache, None for those without one."""
        if self.cache_glob is None or not self.uses_bulk_cache():
            return None
        return any(paths.RIVRETRIEVE_DATA_DIR.glob(self.cache_glob))

    def blocked_kind(self) -> Optional[str]:
        """Why downloads are blocked, as a code the UI words: credentials or hosted."""
        if self.missing_credentials():
            return "credentials"
        if paths.HOSTED and self.uses_bulk_cache():
            return "hosted"
        return None

    def blocked_reason(self) -> Optional[str]:
        """Why a download from this provider cannot succeed right now, or None if it can."""
        kind = self.blocked_kind()
        if kind == "credentials":
            return f"{self.label} needs {' and '.join(self.missing_credentials())} in your .env file."
        if kind == "hosted":
            # HYDAT alone is 1.2 GB; a hosted function has neither the disk nor
            # the time to build it, and nothing would keep it between requests.
            return (
                f"{self.label} needs a one-time bulk download of several GB, which the hosted "
                "atlas cannot store. Use the local app for these stations."
            )
        return None

    def is_usable(self) -> bool:
        """Whether a download request to this provider can succeed right now."""
        return self.blocked_reason() is None


# --------------------------------------------------------------------------------------
# The providers, in the order the map payload indexes them.
# --------------------------------------------------------------------------------------

PROVIDERS: tuple[Provider, ...] = (
    Provider(
        key="australia",
        fetcher="AustraliaFetcher",
        label="BoM Water Data Online",
        country_name="Australia",
        source_url="http://www.bom.gov.au/waterdata/",
        seconds_per_station=3.0,
    ),
    Provider(
        key="brazil",
        fetcher="BrazilFetcher",
        label="ANA Hidroweb",
        country_name="Brazil",
        source_url="https://www.ana.gov.br/hidroweb/",
        # ANA_USERNAME / ANA_PASSWORD are optional: without them the fetcher
        # uses ANA's public HidroSerieHistorica service.
        throttle_note=(
            "Daily series from ANA's public HidroSerieHistorica service. Where ANA has published "
            "both raw and consisted (quality-controlled) values for a month, the consisted ones are used."
        ),
        seconds_per_station=4.0,
    ),
    Provider(
        key="canada",
        fetcher="CanadaFetcher",
        label="ECCC HYDAT",
        country_name="Canada",
        source_url="https://collaboration.cmc.ec.gc.ca/cmc/hydrometrics/www/",
        bulk_first_use=(
            "Downloads the complete HYDAT SQLite database (several hundred MB) "
            "the first time any Canadian station is requested."
        ),
        cache_glob="Hydat*.sqlite3",
        # Reads from a local SQLite file once warm, so it is by far the fastest provider.
        seconds_per_station=0.2,
        # Hosted, the same HYDAT daily means come from ECCC's GeoMet API, a station at a time.
        hosted_fetcher_kwargs={"source": "api"},
        hosted_seconds_per_station=3.0,
    ),
    Provider(
        key="chile",
        fetcher="ChileFetcher",
        label="CR2 Explorador",
        country_name="Chile",
        source_url="https://explorador.cr2.cl/",
        throttle_note="CR2's compiled daily discharge archive, which ends in mid-2020.",
        seconds_per_station=4.0,
    ),
    Provider(
        key="czech",
        fetcher="CzechFetcher",
        label="CHMI Open Data",
        country_name="Czechia",
        source_url="https://opendata.chmi.cz/",
        throttle_note=(
            "One request per station per year of record. CHMI's archive runs to the end of last "
            "year; the current year is published only as the latest reading."
        ),
        seconds_per_station=6.0,
    ),
    Provider(
        key="france",
        fetcher="FranceFetcher",
        label="Hub'Eau",
        country_name="France",
        source_url="https://hubeau.eaufrance.fr/",
        seconds_per_station=2.0,
    ),
    Provider(
        key="grdc",
        fetcher="GRDCFetcher",
        label="GRDC Data Portal",
        country_name="Worldwide river discharge stations",
        source_url="https://portal.grdc.bafg.de/applications/public.html?publicuser=PublicUser#dataDownload/Stations",
        availability_columns=True,
        throttle_note=(
            "GRDC releases its own copies of these series only through its Data Portal (a request "
            "form; the link arrives by e-mail). Where the national service that runs a station "
            "publishes it too — about 5,500 stations in 16 countries — the series is downloaded "
            "from that service instead, and the download names it."
        ),
        seconds_per_station=3.0,
    ),
    Provider(
        key="germany_berlin",
        fetcher="GermanyBerlinFetcher",
        label="Wasserportal Berlin",
        country_name="Germany (Berlin)",
        source_url="https://wasserportal.berlin.de/",
        # The CSV was written with a pandas index column that has no header.
        aliases={"Unnamed: 0": "_row_index"},
        seconds_per_station=3.0,
    ),
    Provider(
        key="ioc_sealevel",
        fetcher="IOCSeaLevelFetcher",
        label="IOC Sea Level Monitoring",
        country_name="Worldwide coastal tide gauges",
        source_url="https://www.ioc-sealevelmonitoring.org/",
        throttle_note=(
            "Raw real-time tide gauge readings, about one a minute, unchecked and relative to each "
            "sensor's own zero. Each request covers only the last 92 days of the range, fetched in "
            "30-day windows one second apart; use UHSLC for long, quality-controlled records."
        ),
        seconds_per_station=20.0,
    ),
    Provider(
        key="japan",
        fetcher="JapanFetcher",
        label="MLIT Water Information System",
        country_name="Japan",
        source_url="http://www1.river.go.jp/",
        throttle_note="Scrapes one page per month; records before ~1980 are sparse.",
        seconds_per_station=25.0,
    ),
    Provider(
        key="lithuania",
        fetcher="LithuaniaFetcher",
        label="Meteo.lt",
        country_name="Lithuania",
        source_url="https://api.meteo.lt/v1/",
        throttle_note="Backs off on HTTP 429; one request per station per month.",
        seconds_per_station=10.0,
    ),
    Provider(
        key="mrc",
        fetcher="MRCFetcher",
        label="Mekong River Commission",
        country_name="Mekong basin (LA · KH · VN · TH · CN)",
        source_url="https://monitoring.mrcmekong.org/",
        throttle_note=(
            "Public near-real-time feed: a rolling window of roughly the last 31 days at 5–15 "
            "minute resolution. Earlier periods return nothing — the MRC's historical archive is "
            "a separate licensed service. Redistribution terms for this feed are unconfirmed."
        ),
        seconds_per_station=1.5,
    ),
    Provider(
        key="noaa_tides",
        fetcher="NOAATidesFetcher",
        label="NOAA Tides & Currents",
        country_name="United States (coasts, estuaries & Great Lakes)",
        source_url="https://tidesandcurrents.noaa.gov/",
        # The cached CSV flags 6-minute stage and water temperature per station.
        availability_columns=True,
        throttle_note=(
            "One request per 31 days of 6-minute data or per year of daily means, paced 0.5 s "
            "apart — NOAA answers bursts with HTTP 403 for several minutes. Water levels are "
            "heights above each station's own datum: MLLW at tide gauges, IGLD on the Great Lakes."
        ),
        seconds_per_station=10.0,
    ),
    Provider(
        key="norway",
        fetcher="NorwayFetcher",
        label="NVE HydAPI",
        country_name="Norway",
        source_url="https://hydapi.nve.no/",
        credentials=("NVE_API_KEY",),
        availability_columns=True,
        seconds_per_station=2.0,
    ),
    Provider(
        key="pagasa_dams",
        fetcher="PagasaDamFetcher",
        label="PAGASA dam bulletin",
        country_name="Philippines",
        source_url="https://pagasa.dost.gov.ph/flood",
        throttle_note=(
            "The bulletin carries today's and yesterday's 08:00 reading only — PAGASA keeps no "
            "public archive. Outflow appears only while the spillway gates are open."
        ),
        seconds_per_station=2.0,
    ),
    Provider(
        key="pagasa_stations",
        fetcher="PagasaStationFetcher",
        label="PAGASA station inventory",
        country_name="Philippines",
        source_url="https://sites.google.com/view/hydromet-data-request-portal-1",
        availability_columns=True,
        throttle_note=(
            "Inventory only. PAGASA sells these observations through its Hydromet Data Request "
            "Portal — requirements checklist, applicable fees, online payment — so downloads "
            "return nothing. The station list itself is open, which is why all 199 are mapped."
        ),
        seconds_per_station=0.1,
    ),
    Provider(
        key="philippines",
        fetcher="PhilippinesFetcher",
        label="DOST-ASTI PhilSensors",
        country_name="Philippines",
        source_url="https://philsensors.asti.dost.gov.ph/",
        credentials=("PHILSENSORS_TOKEN",),
        availability_columns=True,
        throttle_note=(
            "Station metadata is open, readings are not. DOST-ASTI issues access tokens through "
            "a data request: non-commercial research, academic or disaster-management use, no "
            "redistribution. See https://philsensors.asti.dost.gov.ph/datarequest/terms"
        ),
        seconds_per_station=2.0,
    ),
    Provider(
        key="poland",
        fetcher="PolandFetcher",
        label="IMGW Public Data",
        country_name="Poland",
        source_url="https://danepubliczne.imgw.pl/",
        bulk_first_use=(
            "Downloads every historical archive and builds a local Zarr store "
            "the first time any Polish station is requested."
        ),
        cache_glob="poland.zarr",
        # Served from the local Zarr store once warm.
        seconds_per_station=0.2,
        # Hosted, only the archives covering the range are downloaded (about 2 MB a year),
        # and kept in /tmp for the next station while the instance lives.
        hosted_fetcher_kwargs={"source": "direct"},
        hosted_seconds_per_station=20.0,
    ),
    Provider(
        key="portugal",
        fetcher="PortugalFetcher",
        label="SNIRH",
        country_name="Portugal",
        source_url="https://snirh.apambiente.pt/",
        throttle_note=(
            "In October 2026 SNIRH refused every request from the atlas — hosted in the US and "
            "tested from Japan — with HTTP 403 Forbidden, which looks like a block on foreign "
            "networks. Downloads fail until that changes; SNIRH's own site is the fallback."
        ),
        seconds_per_station=4.0,
    ),
    Provider(
        key="singapore_rain",
        fetcher="SingaporeRainFetcher",
        label="NEA rain gauges (data.gov.sg)",
        country_name="Singapore",
        source_url="https://data.gov.sg/",
        throttle_note=(
            "Five-minute rainfall, served one whole day at a time for every gauge, back to "
            "December 2016. Each request covers only the last 92 days of the range; days already "
            "downloaded are reused for the other gauges."
        ),
        seconds_per_station=30.0,
    ),
    Provider(
        key="slovenia",
        fetcher="SloveniaFetcher",
        label="ARSO",
        country_name="Slovenia",
        source_url="https://vode.arso.gov.si/",
        seconds_per_station=4.0,
    ),
    Provider(
        key="southafrica",
        fetcher="SouthAfricaFetcher",
        label="DWS Hydrology Services",
        country_name="South Africa",
        source_url="https://www.dws.gov.za/Hydrology/",
        throttle_note=(
            "Scrapes one HTML page per station per year. In October 2026 the DWS site refused "
            "every request from the atlas — hosted in the US and tested from Japan — with HTTP 403 "
            "Forbidden, so downloads fail until that changes."
        ),
        seconds_per_station=12.0,
    ),
    Provider(
        key="spain",
        fetcher="SpainFetcher",
        label="MITECO Anuario de Aforos",
        country_name="Spain",
        source_url="https://www.miteco.gob.es/es/agua/temas/evaluacion-de-los-recursos-hidricos/sistema-informacion-anuario-aforos/",
        # The cached CSV carries no latitude/longitude at all — only UTM 30N / ETRS89.
        source_crs="EPSG:25830",
        coord_columns=("COORD_UTMX_H30_ETRS89", "COORD_UTMY_H30_ETRS89"),
        throttle_note=(
            "From the Anuario de Aforos yearbook, which ends with its latest hydrological year "
            "(30 September 2022 for the 2021-22 edition). Each request downloads the station's "
            "river-basin table, 4–18 MB."
        ),
        seconds_per_station=6.0,
    ),
    Provider(
        key="thailand",
        fetcher="ThailandFetcher",
        label="ThaiWater (HII)",
        country_name="Thailand",
        source_url="https://www.thaiwater.net/",
        throttle_note=(
            "Telemetry archive: readings begin around 2020 and only become dense from 2023. "
            "Requests before 2020 return nothing, and wide ranges are fetched in half-year "
            "chunks because the provider silently truncates anything longer than a year."
        ),
        seconds_per_station=3.0,
    ),
    Provider(
        key="thailand_rain",
        fetcher="ThailandRainFetcher",
        label="ThaiWater rain gauges (HII)",
        country_name="Thailand",
        source_url="https://www.thaiwater.net/",
        throttle_note=(
            "The provider publishes no rainfall history — only a rolling window of roughly "
            "the last 41 hours. Requests for earlier periods return nothing."
        ),
        seconds_per_station=1.5,
    ),
    Provider(
        key="uhslc",
        fetcher="UHSLCFetcher",
        label="UHSLC tide gauges",
        country_name="Worldwide coastal tide gauges",
        source_url="https://uhslc.soest.hawaii.edu/",
        throttle_note=(
            "Quality-controlled hourly and daily sea level, some records reaching back to the "
            "1800s. Research quality data runs to the end of the year before last and fast delivery "
            "data to one or two months ago, so the latest weeks are not here yet; IOC Sea Level "
            "Monitoring has them raw. Heights are relative to each station's UHSLC zero."
        ),
        seconds_per_station=4.0,
    ),
    Provider(
        key="uk_ea",
        fetcher="UKEAFetcher",
        label="Environment Agency Hydrology",
        country_name="United Kingdom (England)",
        source_url="https://environment.data.gov.uk/hydrology/",
        seconds_per_station=3.0,
    ),
    Provider(
        key="uk_nrfa",
        fetcher="UKNRFAFetcher",
        label="NRFA",
        country_name="United Kingdom",
        source_url="https://nrfaapps.ceh.ac.uk/nrfa/ws",
        seconds_per_station=2.0,
    ),
    Provider(
        key="usa",
        fetcher="USAFetcher",
        label="USGS NWIS",
        country_name="United States",
        source_url="https://waterservices.usgs.gov/",
        seconds_per_station=2.0,
    ),
)

PROVIDERS_BY_KEY: dict[str, Provider] = {p.key: p for p in PROVIDERS}


def get_provider(key: str) -> Provider:
    """Looks up a provider, raising a clear error for an unknown key."""
    try:
        return PROVIDERS_BY_KEY[key]
    except KeyError:
        raise KeyError(f"Unknown provider {key!r}. Known providers: {', '.join(PROVIDERS_BY_KEY)}")


def provider_for_fetcher(fetcher_name: str) -> Optional[Provider]:
    """The provider served by a fetcher class, e.g. ``"USAFetcher"`` -> USGS NWIS."""
    return next((provider for provider in PROVIDERS if provider.fetcher == fetcher_name), None)


def station_download_note(fetcher, gauge_id: str) -> Optional[str]:
    """Why this one station cannot be downloaded although its provider can, if it cannot.

    Fetchers that serve some stations only (GRDC) say so through ``unavailable_reason``.
    """
    explain = getattr(fetcher, "unavailable_reason", None)
    return explain(gauge_id) if explain is not None else None


def national_source(fetcher, gauge_id: str) -> Optional[dict]:
    """The national provider and station a fetcher reads this station from, if it delegates.

    GRDC stations are downloaded from the national service that runs them.
    """
    find = getattr(fetcher, "national_source", None)
    match = find(gauge_id) if find is not None else None
    if not match:
        return None
    fetcher_name, national_id = match
    provider = provider_for_fetcher(fetcher_name)
    return {
        "provider_key": provider.key if provider else None,
        "provider_label": provider.label if provider else fetcher_name,
        "gauge_id": national_id,
        "station_key": station_key(provider.key, national_id) if provider else None,
    }


def station_key(country: str, gauge_id: str) -> str:
    """Builds the globally unique station key.

    ``gauge_id`` is only unique within a provider — and in Australia's cached CSV
    it is not even unique within the provider — so every record is keyed by both.
    """
    return f"{country}:{gauge_id}"


def split_station_key(key: str) -> tuple[str, str]:
    """Inverse of :func:`station_key`. Gauge IDs may themselves contain ``:``."""
    country, _, gauge_id = key.partition(":")
    if not gauge_id:
        raise ValueError(f"Malformed station key {key!r}; expected '<country>:<gauge_id>'")
    return country, gauge_id


@lru_cache(maxsize=1)
def all_variables() -> tuple[str, ...]:
    """Every variable declared by any provider, in a stable order.

    The order defines the bit positions used by the map payload's variable mask,
    so it must stay deterministic across builds.
    """
    seen: list[str] = []
    for provider in PROVIDERS:
        for variable in provider.declared_variables():
            if variable not in seen:
                seen.append(variable)
    return tuple(sorted(seen))


def variable_mask(variables) -> int:
    """Packs a list of variable names into an integer bitmask."""
    index = {name: position for position, name in enumerate(all_variables())}
    mask = 0
    for name in variables:
        if name in index:
            mask |= 1 << index[name]
    return mask
