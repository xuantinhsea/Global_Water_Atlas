"""Fetcher for Polish river gauge data from IMGW."""

import io
import logging
import os
import re
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd
import requests
import xarray as xr

from . import base, constants, utils

logger = logging.getLogger(__name__)


class PolandFetcher(base.RiverDataFetcher):
    """Fetches river gauge data from Poland's Institute of Meteorology and Water Management (IMGW).

    Data Source: IMGW Public Data (https://danepubliczne.imgw.pl/)

    IMGW publishes daily data as ZIP archives covering every station: one per
    month until 2022, one per hydrological year (November to October) since.
    Two ways to read them, chosen with ``source``:

    - ``"cache"`` (default): downloads all historical data on first use and
      caches it in a Zarr store in ``rivretrieve/data/poland.zarr``.
    - ``"direct"``: downloads only the archives that cover the requested range
      and keeps this station's rows. Archives are kept in the system temporary
      directory, so further stations over the same range reuse them. No bulk
      download, so it suits short-lived or read-only environments.

    Supported Variables:
        - ``constants.DISCHARGE_DAILY_MEAN`` (m³/s)
        - ``constants.STAGE_DAILY_MEAN`` (m)
        - ``constants.WATER_TEMPERATURE_DAILY_MEAN`` (°C)
    """

    BASE_URL = "https://danepubliczne.imgw.pl/data/dane_pomiarowo_obserwacyjne/dane_hydrologiczne/"
    CACHE_FILE = Path(os.path.dirname(__file__)) / "data" / "poland.zarr"
    #: Where ``source="direct"`` keeps downloaded archives.
    ARCHIVE_DIR = Path(tempfile.gettempdir()) / "rivretrieve" / "imgw"
    #: Monthly archives until 2022 (``codz_2022_01.zip``), one per year since (``codz_2023.zip``).
    ARCHIVE_LINK = re.compile(r'href="(codz_\d{4}(?:_\d{2})?\.zip)"')
    FIRST_YEAR = 1951
    SOURCES = ("cache", "direct")

    def __init__(self, source: str = "cache"):
        super().__init__()
        if source not in self.SOURCES:
            raise ValueError(f"source must be one of {self.SOURCES}, not {source!r}")
        self.source = source
        self._meta_headers: Optional[List[str]] = None
    METADATA_URL = (
        "https://danepubliczne.imgw.pl/data/dane_pomiarowo_obserwacyjne/dane_hydrologiczne/lista_stacji_hydro.csv"
    )
    METADATA_CSV = Path(os.path.dirname(__file__)) / "cached_site_data" / "poland_sites.csv"

    @staticmethod
    def get_metadata():
        """Downloads the metadata CSV file from IMGW and converts it into a pandas DataFrame.

        Data is fetched from:
        ``https://danepubliczne.imgw.pl/data/dane_pomiarowo_obserwacyjne/dane_hydrologiczne/lista_stacji_hydro.csv``

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        logger.info(f"Downloading metadata from {PolandFetcher.METADATA_URL}")
        try:
            r = utils.requests_retry_session().get(PolandFetcher.METADATA_URL)
            r.raise_for_status()

            # The file is encoded in cp1250
            df = pd.read_csv(io.StringIO(r.content.decode("cp1250")), header=None, dtype=str)

            logger.info("Successfully downloaded and read metadata.")

            # Assign column names based on manual inspection of data on the https:// server
            col_names = [
                constants.GAUGE_ID,
                constants.STATION_NAME,
                constants.RIVER,
                "Kod Hydro",  # Seems to be an alternativ station id.
            ]
            df.columns = col_names

            # Strip potential whitespace from gauge IDs
            df[constants.GAUGE_ID] = df[constants.GAUGE_ID].str.strip()

            return df.set_index(constants.GAUGE_ID)

        except requests.exceptions.RequestException as e:
            logger.error(f"Error downloading metadata: {e}")
            raise
        except Exception as e:
            logger.error(f"Error processing or saving metadata: {e}")
            raise

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of available Polish gauge IDs and metadata.

        This method loads the metadata from a cached CSV file located in
        the ``rivretrieve/cached_site_data/`` directory.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return utils.load_cached_metadata_csv("poland")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (constants.DISCHARGE_DAILY_MEAN, constants.STAGE_DAILY_MEAN, constants.WATER_TEMPERATURE_DAILY_MEAN)

    #: The field list of the daily tables. IMGW replaced ``codz_info.txt`` in 2026.
    HEADER_FILES = ("dobowe/CODZ_publiczne_format.txt", "dobowe/codz_info.txt")

    def _get_metadata_headers(self):
        """Fetches and cleans metadata headers."""
        s = utils.requests_retry_session()
        error: Optional[Exception] = None
        for name in self.HEADER_FILES:
            try:
                response = s.get(self.BASE_URL + name)
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                error = e
                continue
            lines = response.content.decode("cp1250", errors="ignore").splitlines()[2:12]  # 10 fields
            cleaned = [re.sub(r"\s+", " ", re.sub(r"[?'^]", "", line)).strip() for line in lines]
            # The current file prefixes each name with its database field: "PSKDSZS - Kod stacji".
            return [re.sub(r"^[A-Z]+ - ", "", line) for line in cleaned]
        logger.error(f"Error fetching metadata headers: {error}")
        raise error

    def _download_all_data(self, start_year: int, end_year: int) -> List[pd.DataFrame]:
        """Downloads raw data from IMGW for the specified year range."""
        s = utils.requests_retry_session()
        all_data = []
        meta_headers = self._get_metadata_headers()

        for year in range(start_year, end_year + 1):
            year_url = f"{self.BASE_URL}dobowe/{year}/"
            try:
                response = s.get(year_url)
                response.raise_for_status()
                zip_files = self.ARCHIVE_LINK.findall(response.text)
                logger.info(f"Found {len(zip_files)} zip files for year {year}")

                for i, fname in enumerate(zip_files):
                    logger.info(f"Downloading and processing {fname} ({i + 1}/{len(zip_files)})")
                    resp = s.get(f"{year_url}{fname}")
                    resp.raise_for_status()
                    all_data.extend(self._read_archive(resp.content, meta_headers, fname))

            except requests.exceptions.RequestException as e:
                logger.error(f"Error fetching data for year {year}: {e}")
            except Exception as e:
                logger.error(f"Error processing data for year {year}: {e}")

        return all_data

    @staticmethod
    def _read_archive(content: bytes, meta_headers: List[str], fname: str) -> List[pd.DataFrame]:
        """The CSV tables in one downloaded archive, with IMGW's column names."""
        frames = []
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for member in zf.namelist():
                df = _imgw_read(zf.read(member))
                if df.empty:
                    continue
                if df.shape[1] == len(meta_headers):
                    df.columns = meta_headers
                    frames.append(df)
                elif df.shape[1] == 9:  # Special case for current year format
                    df["flow"] = None
                    df = df.iloc[:, list(range(7)) + [9, 7, 8]]
                    df.columns = meta_headers
                    frames.append(df)
                else:
                    logger.warning(f"Column mismatch in {fname}")
        return frames

    def _archive_urls(self, first_year: int, last_year: int) -> List[tuple]:
        """``(url, cache name)`` for every daily archive of these hydrological years."""
        s = utils.requests_retry_session()
        found = []
        for year in range(first_year, last_year + 1):
            year_url = f"{self.BASE_URL}dobowe/{year}/"
            response = s.get(year_url, timeout=60)
            if response.status_code == 404:  # A hydrological year not published yet.
                continue
            response.raise_for_status()
            for match in self.ARCHIVE_LINK.finditer(response.text):
                fname = match.group(1)
                # IMGW revises the latest yearly archive in place, so the listing's
                # modification time is part of the cache name.
                modified = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", response.text[match.end() : match.end() + 200])
                stamp = re.sub(r"\D", "", modified.group(0)) if modified else "undated"
                found.append((f"{year_url}{fname}", f"{fname[:-4]}_{stamp}.zip"))
        return found

    def _fetch_archive(self, url: str, cache_name: str) -> bytes:
        path = self.ARCHIVE_DIR / cache_name
        if path.exists():
            return path.read_bytes()
        response = utils.requests_retry_session().get(url, timeout=120)
        response.raise_for_status()
        try:
            self.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".part")
            partial.write_bytes(response.content)
            partial.replace(path)
        except OSError as e:  # A read-only disk only costs the reuse.
            logger.warning(f"Could not keep {cache_name}: {e}")
        return response.content

    def _get_direct_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Reads one station from just the archives covering the range. HTTP errors propagate."""
        start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
        # Hydrological year N runs from November of N-1 to October of N.
        first_year = max(self.FIRST_YEAR, start.year + (1 if start.month >= 11 else 0))
        last_year = min(datetime.now().year + 1, end.year + (1 if end.month >= 11 else 0))

        if self._meta_headers is None:
            self._meta_headers = self._get_metadata_headers()
        meta_headers = self._meta_headers
        archives = self._archive_urls(first_year, last_year)
        logger.info(f"Reading {gauge_id} from {len(archives)} IMGW archives, {first_year}-{last_year}")

        frames = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            contents = pool.map(lambda item: (item[1], self._fetch_archive(*item)), archives)
            for name, content in contents:
                for df in self._read_archive(content, meta_headers, name):
                    station = df.iloc[:, 0].astype(str).str.strip()
                    picked = df[station == str(gauge_id).strip()]
                    if not picked.empty:
                        frames.append(picked)

        parsed = self._parse_all_data(frames)
        if parsed.empty or variable not in parsed.columns:
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])
        series = parsed.set_index(constants.TIME_INDEX)[[variable]].dropna().sort_index()
        series = series[~series.index.duplicated(keep="last")]
        return series[(series.index >= start) & (series.index <= end)].astype(float)

    def _parse_all_data(self, raw_data_list: List[pd.DataFrame]) -> pd.DataFrame:
        """Parses the raw dataframes into a single standardized format."""
        if not raw_data_list:
            return pd.DataFrame()

        try:
            full_df = pd.concat(raw_data_list, ignore_index=True)
            if full_df.empty:
                return pd.DataFrame()

            # Rename columns
            full_df = full_df.rename(
                columns={
                    "Kod stacji": constants.GAUGE_ID,
                    "Przepływ [m3/s]": constants.DISCHARGE_DAILY_MEAN,
                    "Stan wody [cm]": constants.STAGE_DAILY_MEAN,
                    "Temperatura wody [st. C]": constants.WATER_TEMPERATURE_DAILY_MEAN,
                }
            )

            # Build Date column
            date_cols = ["Rok hydrologiczny", "Miesiąc kalendarzowy", "Dzień"]
            full_df = full_df.dropna(subset=date_cols)
            df_dates = full_df[date_cols].astype(int)
            df_dates.columns = ["hyy", "mm", "dd"]
            df_dates["yy"] = df_dates["hyy"] - (df_dates["mm"] >= 11).astype(int)
            full_df[constants.TIME_INDEX] = pd.to_datetime(
                dict(year=df_dates["yy"], month=df_dates["mm"], day=df_dates["dd"]),
                errors="coerce",
            )
            full_df = full_df.dropna(subset=[constants.TIME_INDEX])
            full_df[constants.GAUGE_ID] = full_df[constants.GAUGE_ID].astype(str)

            # Select and convert variables
            var_cols = [
                constants.DISCHARGE_DAILY_MEAN,
                constants.STAGE_DAILY_MEAN,
                constants.WATER_TEMPERATURE_DAILY_MEAN,
            ]
            for var in var_cols:
                if var in full_df.columns:
                    full_df[var] = pd.to_numeric(full_df[var], errors="coerce")

            if constants.STAGE_DAILY_MEAN in full_df.columns:
                full_df[constants.STAGE_DAILY_MEAN] = full_df[constants.STAGE_DAILY_MEAN] / 100.0  # cm to m

            # Clean placeholder values
            full_df.replace({9999: None, 99999.999: None, 99.9: None, 999: None}, inplace=True)

            result_df = full_df[[constants.GAUGE_ID, constants.TIME_INDEX] + var_cols].dropna(
                how="all", subset=var_cols
            )
            return result_df

        except Exception as e:
            logger.error(f"Error parsing all data: {e}")
            return pd.DataFrame()

    def _create_cache(self):
        """Downloads all data, processes it, and saves it to a zarr cache."""
        logger.info(f"Creating cache file at {self.CACHE_FILE}")
        start_year = 1951
        end_year = datetime.now().year
        raw_data_list = self._download_all_data(start_year, end_year)
        if not raw_data_list:
            logger.error("No data downloaded, cache creation failed.")
            return

        df = self._parse_all_data(raw_data_list)
        if df.empty:
            logger.error("No data parsed, cache creation failed.")
            return

        # Convert to xarray Dataset
        df = df.set_index([constants.GAUGE_ID, constants.TIME_INDEX]).sort_index()
        ds = df.to_xarray()

        # Save to zarr
        try:
            self.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            ds.to_zarr(self.CACHE_FILE, mode="w")
            logger.info(f"Successfully created cache file at {self.CACHE_FILE}")
        except Exception as e:
            logger.error(f"Error saving cache to zarr: {e}")

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetches and parses time series data for a specific gauge and variable.

        This method retrieves the requested data from the provider's API or data source,
        parses it, and returns it in a standardized pandas DataFrame format.

        Args:
            gauge_id: The site-specific identifier for the gauge.
            variable: The variable to fetch. Must be one of the strings listed
                in the fetcher's ``get_available_variables()`` output.
                These are typically defined in ``rivretrieve.constants``.
            start_date: Optional start date for the data retrieval in 'YYYY-MM-DD' format.
                If None, data is fetched from the earliest available date.
            end_date: Optional end date for the data retrieval in 'YYYY-MM-DD' format.
                If None, data is fetched up to the latest available date.

        Returns:
            pd.DataFrame: A pandas DataFrame indexed by datetime objects (``constants.TIME_INDEX``)
            with a single column named after the requested ``variable``. The DataFrame
            will be empty if no data is found for the given parameters.

        Raises:
            ValueError: If the requested ``variable`` is not supported by this fetcher.
            requests.exceptions.RequestException: If a network error occurs during data download.
            Exception: For other unexpected errors during data fetching or parsing.
        """
        start_date = utils.format_start_date(start_date)
        end_date = utils.format_end_date(end_date)

        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        if self.source == "direct":
            return self._get_direct_data(gauge_id, variable, start_date, end_date)

        if not self.CACHE_FILE.exists():
            self._create_cache()

        if not self.CACHE_FILE.exists():
            logger.error("Cache file not found after creation attempt.")
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

        try:
            ds = xr.open_zarr(self.CACHE_FILE)
            if variable not in ds:
                logger.warning(f"Variable {variable} not found in cache.")
                return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

            data_array = ds[variable].sel(gauge_id=gauge_id, time=slice(start_date, end_date))
            df = data_array.to_pandas().dropna().reset_index().rename(columns={variable: variable})
            return df.set_index(constants.TIME_INDEX)[[variable]]

        except KeyError:
            logger.info(f"No data found for gauge {gauge_id} in the selected date range.")
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

            logger.error("Error reading from cache")
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> any:
        """Not used for PolandFetcher, cache is created from all data."""
        raise NotImplementedError("This method is not used in PolandFetcher.")

    def _parse_data(self, gauge_id: str, raw_data: any, variable: str) -> pd.DataFrame:
        """Not used for PolandFetcher, cache is created from all data."""
        raise NotImplementedError("This method is not used in PolandFetcher.")


def _imgw_read(raw: bytes) -> pd.DataFrame:
    """Reads one IMGW CSV file, whatever its encoding, separator or quoting.

    Files since 2023 wrap each whole line in quotes and double the quotes
    inside it, e.g. ``"149180020,CHAŁUPKI,Odra (1),""2024"",""01"",..."``,
    which a CSV reader sees as a single column.
    """
    try:
        text = raw.decode("cp1250")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")

    def parse(body: str, sep: str) -> pd.DataFrame:
        try:
            return pd.read_csv(io.StringIO(body), header=None, sep=sep, low_memory=False)
        except Exception:
            return pd.DataFrame()

    data = parse(text, ",")
    if data.shape[1] <= 1:
        unwrapped = "\n".join(
            line[1:-1].replace('""', '"') if len(line) > 1 and line[0] == line[-1] == '"' else line
            for line in text.splitlines()
        )
        data = parse(unwrapped, ",")
    if data.shape[1] <= 1:
        data = parse(text, ";")
    return data
