"""Fetchers for Philippine data published by PAGASA.

The Philippine Atmospheric, Geophysical and Astronomical Services Administration
publishes two things this library can use, and they behave very differently:

:class:`PagasaDamFetcher`
    A daily bulletin for the nine major regulated reservoirs of Luzon —
    including Angat, which supplies most of Metro Manila's water, and Magat and
    Pantabangan, the largest irrigation and power reservoirs in the country.
    Open, no credentials, but only today and yesterday.

:class:`PagasaStationFetcher`
    PAGASA's own inventory of 199 hydrometeorological stations: synoptic,
    synoptic-radar, hydromet and telemetered rainfall and water level. The
    inventory is open; the observations behind it are sold through PAGASA's
    data request portal.

Together they complement :mod:`rivretrieve.philippines`: DOST-ASTI's PhilSensors
network covers river water level and rainfall but publishes nothing about the
dams, and its readings need a token of a different kind.

.. warning::
    **The dam bulletin covers two days only.** It is a live HTML table carrying
    today's and yesterday's 08:00 observation. PAGASA keeps no public archive of
    it — the former ``damstat.csv`` feed stopped updating in April 2016 — and
    historical hydrometeorological data is issued through a formal request to
    PAGASA's Hydrometeorology Division. Requests for earlier periods return an
    empty DataFrame rather than yesterday's reading under the wrong date.
"""

import datetime
import io
import logging
import re
from typing import Any, Optional

import pandas as pd

from . import base, constants, utils

logger = logging.getLogger(__name__)

BULLETIN_URL = "https://pagasa.dost.gov.ph/flood"

#: PAGASA's published station inventory, exported from the Google map it links
#: from its data request portal. The map id is PAGASA's own and stable.
STATION_MAP_ID = "1YOIcQSw7nMNUl8JBvLRpZ94e1zeCXw0"
STATION_KML_URL = f"https://www.google.com/maps/d/kml?mid={STATION_MAP_ID}&forcekml=1"
DATA_REQUEST_URL = "https://sites.google.com/view/hydromet-data-request-portal-1"

#: How far back the bulletin reaches: today and yesterday.
WINDOW_DAYS = 2

#: The nine dams in the bulletin, with coordinates taken from PAGASA's own
#: placemark file at pubfiles.pagasa.dost.gov.ph/hmd/Previous_Dam_Status/.
#: Hard-coded rather than parsed at runtime: it is nine rows that have not moved
#: in a decade, against a 1.1 MB KML whose structure is not part of any contract.
DAMS: dict[str, dict[str, Any]] = {
    "angat": {"name": "Angat", "lat": 14.91139, "lon": 121.16500, "river": "Angat"},
    "ipo": {"name": "Ipo", "lat": 14.87500, "lon": 121.06222, "river": "Angat"},
    "la_mesa": {"name": "La Mesa", "lat": 14.71372, "lon": 121.07316, "river": "Tullahan"},
    "ambuklao": {"name": "Ambuklao", "lat": 16.46111, "lon": 120.74389, "river": "Agno"},
    "binga": {"name": "Binga", "lat": 16.39611, "lon": 120.72667, "river": "Agno"},
    "san_roque": {"name": "San Roque", "lat": 16.14600, "lon": 120.68400, "river": "Agno"},
    "pantabangan": {"name": "Pantabangan", "lat": 15.81833, "lon": 121.10944, "river": "Pampanga"},
    "magat": {"name": "Magat", "lat": 16.83333, "lon": 121.45056, "river": "Magat"},
    "caliraya": {"name": "Caliraya", "lat": 14.28830, "lon": 121.50140, "river": "Caliraya"},
}

#: Bulletin spellings that differ from the canonical name.
_NAME_ALIASES = {"magat dam": "magat", "la mesa": "la_mesa", "san roque": "san_roque"}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _slug(dam_name: Any) -> Optional[str]:
    """Maps a bulletin dam name onto a gauge id."""
    if not isinstance(dam_name, str):
        return None
    cleaned = dam_name.strip().lower()
    if not cleaned or cleaned == "dam name":
        return None
    cleaned = _NAME_ALIASES.get(cleaned, cleaned)
    slug = cleaned.replace(" ", "_")
    return slug if slug in DAMS else None


def _parse_bulletin_date(text: Any, today: datetime.date) -> Optional[datetime.date]:
    """Reads a ``Sep-15`` style cell into a date.

    The bulletin omits the year. Rows only ever cover today and yesterday, so
    the year is inferred from ``today`` — and rolled back when that would place
    the row in the future, which is what happens across New Year.
    """
    if not isinstance(text, str):
        return None
    match = re.match(r"([A-Za-z]{3})[-\s]+(\d{1,2})$", text.strip())
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if not month:
        return None
    day = int(match.group(2))
    try:
        candidate = datetime.date(today.year, month, day)
    except ValueError:
        return None
    if candidate > today:
        try:
            candidate = datetime.date(today.year - 1, month, day)
        except ValueError:
            return None
    return candidate


def _parse_time(text: Any) -> Optional[datetime.time]:
    """Reads an ``08:00 AM`` cell, in either case."""
    if not isinstance(text, str):
        return None
    match = re.match(r"(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]", text.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if match.group(3).lower() == "p" and hour != 12:
        hour += 12
    if match.group(3).lower() == "a" and hour == 12:
        hour = 0
    return datetime.time(hour, minute) if 0 <= hour < 24 else None


def _today() -> datetime.date:
    """Today's date, as a single seam the tests can pin.

    The bulletin never writes the year, so every timestamp is resolved relative
    to this.
    """
    return datetime.date.today()


def _reference_level(value: Any) -> Optional[float]:
    """Reads a reference elevation, treating 0 as "not defined".

    The bulletin writes 0.00 where a dam has no normal high water level or rule
    curve — Caliraya and Ipo, for instance. No Philippine reservoir sits at
    exactly sea level, so a literal zero here always means absent.
    """
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number) or float(number) == 0.0:
        return None
    return float(number)


def _flatten(columns) -> list[str]:
    """Collapses the bulletin's two-row header into single labels."""
    flat = []
    for column in columns:
        if isinstance(column, tuple):
            parts = [str(p).strip() for p in column if str(p) != "nan"]
            # The header repeats the group name on single-level columns.
            unique = list(dict.fromkeys(parts))
            flat.append(" ".join(unique))
        else:
            flat.append(str(column).strip())
    return flat


class PagasaDamFetcher(base.RiverDataFetcher):
    """Fetches Philippine reservoir levels and outflow from PAGASA's dam bulletin.

    Data Source: PAGASA flood information (https://pagasa.dost.gov.ph/flood).
    No API key is required.

    Supported Variables:
        - ``constants.STAGE_INSTANT`` (m) — reservoir water level
        - ``constants.DISCHARGE_INSTANT`` (m³/s) — estimated outflow

    .. warning::
        **Two days only**, and no archive. See the module docstring.

    .. note::
        Reservoir water level is an **elevation above datum**, not gauge height
        above a local zero: Ambuklao reads near 752 m and La Mesa near 78 m
        because that is where they sit, not because one is deeper. Compare each
        dam against its own normal high water level, which
        :meth:`get_cached_metadata` carries as ``normal_high_water_level``.

    .. note::
        Outflow is published only while the spillway gates are open, so that
        series is sparse by design — an absent value means closed gates, not a
        failed reading. Inflow appears in the bulletin's header but PAGASA does
        not currently populate it, so it is not offered here.
    """

    #: The bulletin's own column labels, used to find the table and its fields.
    _LEVEL_COLUMN = "Reservoir Water Level (RWL) (m)"
    _OUTFLOW_COLUMN = "Estimated (cms) Outflow"
    _NAME_COLUMN = "Dam Name"
    _WHEN_COLUMN = "Observation Time & Date"

    _SOURCE_COLUMN = {
        constants.STAGE_INSTANT: _LEVEL_COLUMN,
        constants.DISCHARGE_INSTANT: _OUTFLOW_COLUMN,
    }

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of the dams in PAGASA's bulletin.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return utils.load_cached_metadata_csv("pagasa_dams")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (constants.STAGE_INSTANT, constants.DISCHARGE_INSTANT)

    # -- metadata ----------------------------------------------------------------

    def get_metadata(self) -> pd.DataFrame:
        """Builds the dam list, enriched with today's reference levels.

        The dams and their coordinates are fixed; the normal high water level
        and rule curve elevation are read from the live bulletin so they stay
        current.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        reference: dict[str, dict[str, Any]] = {}
        try:
            table = self._download_bulletin()
            if table is not None:
                for _, row in table.iterrows():
                    slug = _slug(row.get(self._NAME_COLUMN))
                    if slug and slug not in reference:
                        reference[slug] = {
                            "normal_high_water_level": _reference_level(
                                row.get("Normal High Water Level (NHWL) (m)")
                            ),
                            "rule_curve_elevation": _reference_level(
                                row.get("Rule Curve Elevation (m)")
                            ),
                        }
        except Exception as exc:
            logger.warning("Could not read reference levels from the bulletin: %s", exc)

        records = []
        for slug, dam in DAMS.items():
            record = {
                constants.GAUGE_ID: slug,
                constants.STATION_NAME: f"{dam['name']} Dam",
                constants.LATITUDE: dam["lat"],
                constants.LONGITUDE: dam["lon"],
                constants.RIVER: dam["river"],
                constants.COUNTRY: "Philippines",
                constants.SOURCE: "PAGASA dam bulletin",
                "station_type": "reservoir",
            }
            record.update(reference.get(slug, {}))
            records.append(record)
        return pd.DataFrame(records).set_index(constants.GAUGE_ID)

    # -- data --------------------------------------------------------------------

    def _download_bulletin(self) -> Optional[pd.DataFrame]:
        """Fetches the bulletin page and returns the dam table.

        The table is located by its column signature rather than by position,
        so an extra table elsewhere on the page does not silently shift it.
        """
        session = utils.requests_retry_session()
        response = session.get(BULLETIN_URL, timeout=90)
        response.raise_for_status()

        try:
            tables = pd.read_html(io.StringIO(response.text))
        except ValueError:
            logger.warning("No tables found on the PAGASA flood page")
            return None

        for table in tables:
            columns = _flatten(table.columns)
            if any(self._LEVEL_COLUMN in column for column in columns):
                table = table.copy()
                table.columns = columns
                return table

        logger.warning(
            "The PAGASA flood page no longer has a table containing %r; "
            "the bulletin layout may have changed.",
            self._LEVEL_COLUMN,
        )
        return None

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> Any:
        """Downloads the bulletin. Dates are ignored — it only ever holds two days."""
        try:
            return self._download_bulletin()
        except Exception as exc:
            logger.warning("PAGASA bulletin request failed: %s", exc)
            return None

    def _parse_data(self, gauge_id: str, raw_data: Any, variable: str) -> pd.DataFrame:
        """Pairs each dam's time row with its date row into timestamped readings.

        Every observation occupies two rows: one carrying the time, the next
        carrying the date, with the values repeated on both. Reading either row
        alone loses half the timestamp.
        """
        empty = pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)
        if raw_data is None or getattr(raw_data, "empty", True):
            return empty

        source_column = self._SOURCE_COLUMN[variable]
        if source_column not in raw_data.columns:
            logger.warning("PAGASA bulletin has no %r column", source_column)
            return empty

        rows = raw_data[raw_data[self._NAME_COLUMN].map(_slug) == gauge_id]
        if rows.empty:
            logger.info("Dam %s is not in the current PAGASA bulletin", gauge_id)
            return empty

        today = _today()
        records = []
        pending_time: Optional[datetime.time] = None
        pending_value: Any = None

        for _, row in rows.iterrows():
            when = row.get(self._WHEN_COLUMN)
            value = row.get(source_column)

            parsed_time = _parse_time(when)
            if parsed_time is not None:
                pending_time = parsed_time
                pending_value = value
                continue

            parsed_date = _parse_bulletin_date(when, today)
            if parsed_date is None or pending_time is None:
                continue

            # The date row repeats the value; fall back to it if the time row was blank.
            number = pd.to_numeric(pending_value, errors="coerce")
            if pd.isna(number):
                number = pd.to_numeric(value, errors="coerce")
            if not pd.isna(number):
                records.append(
                    {
                        constants.TIME_INDEX: datetime.datetime.combine(parsed_date, pending_time),
                        variable: float(number),
                    }
                )
            pending_time, pending_value = None, None

        if not records:
            return empty

        frame = pd.DataFrame(records).set_index(constants.TIME_INDEX).sort_index()
        return frame[~frame.index.duplicated(keep="first")]

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetches recent reservoir observations for one dam.

        Args:
            gauge_id: A dam id from :data:`DAMS`, e.g. ``"angat"``.
            variable: One of ``get_available_variables()``.
            start_date: Optional start date in 'YYYY-MM-DD' format.
            end_date: Optional end date in 'YYYY-MM-DD' format.

        Returns:
            pd.DataFrame: A DataFrame indexed by ``constants.TIME_INDEX`` with a
            single column named after the requested ``variable``. **Empty unless
            the requested range overlaps today or yesterday** — PAGASA publishes
            no archive of this bulletin.

        Raises:
            ValueError: If the requested ``variable`` is not supported, or the
                dam is unknown.
        """
        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")
        if gauge_id not in DAMS:
            raise ValueError(f"Unknown dam {gauge_id!r}. Known dams: {', '.join(DAMS)}")

        start_date = utils.format_start_date(start_date)
        end_date = utils.format_end_date(end_date)

        # The same "today" the bulletin's dates are resolved against.
        window_start = pd.Timestamp(_today()) - pd.Timedelta(days=WINDOW_DAYS)
        if pd.Timestamp(end_date) < window_start:
            logger.info(
                "The PAGASA dam bulletin only covers today and yesterday (from %s); "
                "the request for %s to %s is outside it. Historical PAGASA data is "
                "issued through a data request to its Hydrometeorology Division.",
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
            logger.error("Failed to get PAGASA data for dam %s: %s", gauge_id, exc)
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(
                constants.TIME_INDEX
            )

        if frame.empty:
            return frame
        return frame[
            (frame.index >= pd.Timestamp(start_date))
            & (frame.index <= pd.Timestamp(end_date) + pd.Timedelta(days=1))
        ]


# --------------------------------------------------------------------------------------
# Station inventory
# --------------------------------------------------------------------------------------

#: Which variables each group of stations records.
#: Synoptic, synoptic-radar and hydromet stations are the sources behind
#: PAGASA's rainfall intensity-duration-frequency curves, so they carry daily
#: rainfall; telemetered stations declare their own parameters.
_GROUP_VARIABLES = {
    "synoptic": (constants.PRECIPITATION_DAILY_SUM,),
    "synoptic-radar": (constants.PRECIPITATION_DAILY_SUM,),
    "hydromet": (constants.PRECIPITATION_DAILY_SUM,),
}

_FOLDER_GROUPS = {
    "Synoptic Stations": "synoptic",
    "Synoptic-Radar Stations": "synoptic-radar",
    "Hydromet Stations": "hydromet",
    "Telemetered Stations": "telemetered",
}

_PLACEMARK_RE = re.compile(r"<Placemark>(.*?)</Placemark>", re.S)
_FOLDER_RE = re.compile(r"<Folder>(.*?)</Folder>", re.S)
_DATA_RE = re.compile(r'<Data name="([^"]+)">\s*<value>(.*?)</value>', re.S)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


class PagasaStationFetcher(base.RiverDataFetcher):
    """PAGASA's hydrometeorological station inventory.

    Data Source: PAGASA Hydromet Data Request Portal
    (https://sites.google.com/view/hydromet-data-request-portal-1)

    Covers 199 stations in four groups: 47 synoptic, 10 synoptic-radar, 23
    hydromet and 119 telemetered rainfall and water level stations across seven
    river basins.

    Supported Variables:
        - ``constants.PRECIPITATION_DAILY_SUM`` (mm)
        - ``constants.PRECIPITATION_HOURLY_SUM`` (mm)
        - ``constants.STAGE_INSTANT`` (m)

    Which variables a station records is recorded per station, from PAGASA's own
    ``Available Parameters`` field, rather than assumed for the whole network.

    .. warning::
        **Inventory only.** PAGASA sells its observations: requests go through
        its data request portal, with a requirements checklist, applicable fees
        and online payment. There is no open readings endpoint, so
        ``get_data`` always returns an empty DataFrame and logs where to apply.
        The station list is published openly, which is why all 199 can be mapped.

        For Philippine data you can actually download today, see
        :class:`PagasaDamFetcher` for the nine Luzon reservoirs, and
        :mod:`rivretrieve.philippines` for DOST-ASTI's 2,132-station network.
    """

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of PAGASA's hydrometeorological stations.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return utils.load_cached_metadata_csv("pagasa_stations")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (
            constants.PRECIPITATION_DAILY_SUM,
            constants.PRECIPITATION_HOURLY_SUM,
            constants.STAGE_INSTANT,
        )

    # -- metadata ----------------------------------------------------------------

    def get_metadata(self) -> pd.DataFrame:
        """Downloads and parses PAGASA's published station map.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        session = utils.requests_retry_session()
        response = session.get(STATION_KML_URL, timeout=120)
        response.raise_for_status()
        return self._parse_metadata(response.text)

    def _parse_metadata(self, kml: str) -> pd.DataFrame:
        records = []
        seen: set[str] = set()

        for folder in _FOLDER_RE.findall(kml):
            name_match = re.search(r"<name>(.*?)</name>", folder, re.S)
            folder_name = _strip_tags(name_match.group(1)) if name_match else ""
            group = _FOLDER_GROUPS.get(folder_name)
            if group is None:
                # "RBs with Data" holds river basin outlines, not stations.
                continue

            for placemark in _PLACEMARK_RE.findall(folder):
                if "<Point>" not in placemark:
                    continue
                record = self._parse_placemark(placemark, group, seen)
                if record is not None:
                    records.append(record)

        frame = pd.DataFrame(records)
        if frame.empty:
            return frame
        return frame.set_index(constants.GAUGE_ID)

    def _parse_placemark(self, placemark: str, group: str, seen: set) -> Optional[dict]:
        attributes = {key: _strip_tags(value) for key, value in _DATA_RE.findall(placemark)}
        name_match = re.search(r"<name>(.*?)</name>", placemark, re.S)
        label = _strip_tags(name_match.group(1)) if name_match else ""

        coord_match = re.search(r"<coordinates>\s*(.*?)\s*</coordinates>", placemark, re.S)
        if not coord_match:
            return None
        parts = coord_match.group(1).strip().split(",")
        if len(parts) < 2:
            return None
        try:
            longitude, latitude = float(parts[0]), float(parts[1])
        except ValueError:
            return None

        # Build a stable id: PAGASA's own numeric id or station code where it
        # exists, otherwise the basin and name, which together are unique.
        station_id = attributes.get("Station ID")
        station_code = attributes.get("Station Code")
        basin = attributes.get("River Basin")
        if station_id:
            gauge_id = f"{group}-{station_id}"
        elif station_code:
            gauge_id = f"{group}-{station_code.lower()}"
        else:
            stem = _slugify(f"{basin}-{label}") or f"{latitude:.5f}-{longitude:.5f}"
            gauge_id = f"{group}-{stem}"
        # Two telemetered placemarks have no name at all.
        if gauge_id in seen:
            gauge_id = f"{gauge_id}-{latitude:.5f}-{longitude:.5f}"
        seen.add(gauge_id)

        if group == "telemetered":
            declared = (attributes.get("Available Parameters") or "").lower()
            variables = []
            if "rainfall" in declared:
                variables.append(constants.PRECIPITATION_HOURLY_SUM)
            if "water level" in declared:
                variables.append(constants.STAGE_INSTANT)
        else:
            variables = list(_GROUP_VARIABLES[group])

        record = {
            constants.GAUGE_ID: gauge_id,
            constants.STATION_NAME: label or None,
            constants.LATITUDE: latitude,
            constants.LONGITUDE: longitude,
            constants.RIVER: basin or None,
            constants.COUNTRY: "Philippines",
            constants.SOURCE: "PAGASA",
            "station_group": group,
            "station_code": station_code or None,
            "pagasa_station_id": station_id or None,
            "province": attributes.get("Province") or None,
            "river_basin": basin or None,
            "available_parameters": attributes.get("Available Parameters") or None,
        }
        # Per-station availability, the same shape Norway and PhilSensors use.
        for variable in self.get_available_variables():
            record[variable] = variable in variables
        return record

    # -- data --------------------------------------------------------------------

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> Any:
        """PAGASA publishes no open readings endpoint for these stations."""
        return None

    def _parse_data(self, gauge_id: str, raw_data: Any, variable: str) -> pd.DataFrame:
        return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Always returns an empty DataFrame, and says where the data is sold.

        Args:
            gauge_id: A station id from :meth:`get_cached_metadata`.
            variable: One of ``get_available_variables()``.
            start_date: Accepted and ignored.
            end_date: Accepted and ignored.

        Returns:
            pd.DataFrame: Always empty. PAGASA issues these observations through
            a paid data request rather than an API.

        Raises:
            ValueError: If the requested ``variable`` is not supported.
        """
        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        logger.warning(
            "PAGASA does not publish readings for station %s. Its observations are issued "
            "through the Hydromet Data Request Portal, which has a requirements checklist, "
            "applicable fees and online payment: %s. For Philippine data available now, use "
            "PagasaDamFetcher (9 Luzon reservoirs) or PhilippinesFetcher (DOST-ASTI).",
            gauge_id,
            DATA_REQUEST_URL,
        )
        return pd.DataFrame(columns=[constants.TIME_INDEX, variable]).set_index(constants.TIME_INDEX)
