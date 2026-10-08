"""Fetcher for Spanish river gauge data from ROAN."""

import io
import logging
import re
import struct
import zipfile
import zlib
from typing import Optional
from urllib.parse import urljoin

import pandas as pd
import requests

from . import base, constants, utils

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RivRetrieve)"}


class _TailFile(io.RawIOBase):
    """A read-only view of a remote file of which only the last bytes were downloaded.

    Enough for :mod:`zipfile` to read an archive's central directory, which sits
    at the end of the file.
    """

    def __init__(self, tail: bytes, start: int, size: int):
        self._tail = tail
        self._start = start
        self._size = size
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = base + offset
        return self._pos

    def readinto(self, buffer) -> int:
        if self._pos < self._start:
            raise OSError("Read before the downloaded tail of the archive")
        chunk = self._tail[self._pos - self._start : self._pos - self._start + len(buffer)]
        buffer[: len(chunk)] = chunk
        self._pos += len(chunk)
        return len(chunk)


class _RemoteZip:
    """Reads single members of a ZIP archive on a server that honours HTTP Range requests."""

    #: The archive's central directory has to fit in this many trailing bytes.
    TAIL_BYTES = 1 << 18

    def __init__(self, url: str, session: requests.Session):
        self.url = url
        self.session = session
        head = session.head(url, headers=_HEADERS, allow_redirects=True, timeout=60)
        head.raise_for_status()
        self.size = int(head.headers["Content-Length"])
        tail_start = max(0, self.size - self.TAIL_BYTES)
        tail = self._get(tail_start, self.size - 1)
        with zipfile.ZipFile(_TailFile(tail, tail_start, self.size)) as archive:
            self.members = {info.filename: info for info in archive.infolist()}

    def _get(self, first: int, last: int) -> bytes:
        headers = {**_HEADERS, "Range": f"bytes={first}-{last}"}
        with self.session.get(self.url, headers=headers, stream=True, timeout=300) as response:
            response.raise_for_status()
            if response.status_code != 206:
                # Never fall back to downloading the whole archive by accident.
                raise IOError(f"{self.url} ignored the Range header (HTTP {response.status_code})")
            return response.content

    def read(self, name: str) -> bytes:
        """Downloads and decompresses one member, in a single request."""
        info = self.members[name]
        # The local header repeats the name and may carry a different extra field,
        # so read a little past it rather than trusting the central directory's lengths.
        blob = self._get(info.header_offset, info.header_offset + 30 + 1024 + info.compress_size - 1)
        if blob[:4] != b"PK\x03\x04":
            raise IOError(f"No local file header for {name} in {self.url}")
        name_length, extra_length = struct.unpack("<HH", blob[26:30])
        start = 30 + name_length + extra_length
        raw = blob[start : start + info.compress_size]
        if info.compress_type == zipfile.ZIP_STORED:
            return raw
        if info.compress_type == zipfile.ZIP_DEFLATED:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
        raise IOError(f"Unsupported compression {info.compress_type} for {name}")


class SpainFetcher(base.RiverDataFetcher):
    """Fetches river gauge data from Spain's National Hydrological Data System (ROAN).

    Data Source: MITECO's Anuario de Aforos, the national gauging yearbook
    (https://www.miteco.gob.es/es/agua/temas/evaluacion-de-los-recursos-hidricos/sistema-informacion-anuario-aforos.html).

    Supported Variables:
        - ``constants.DISCHARGE_DAILY_MEAN (m³/s)``

    .. note::
        MITECO retired the ROAN web service this fetcher used to query in 2026.
        Daily records now come from the yearbook's CSV archive, which holds one
        table per river basin. Only the table for the requested station's basin
        is downloaded, using HTTP range requests. The yearbook runs to the end
        of its latest hydrological year (30 September 2022 for the 2021-22
        edition), so recent days are not available.
    """

    METADATA_ZIP_URL = "https://www.miteco.gob.es/content/dam/miteco/es/agua/temas/evaluacion-de-los-recursos-hidricos/sistema-informacion-anuario-aforos/listado-estaciones-aforo.zip"
    ANUARIO_PAGE_URL = "https://www.miteco.gob.es/es/agua/temas/evaluacion-de-los-recursos-hidricos/sistema-informacion-anuario-aforos.html"
    ANUARIO_CSV_URL = "https://www.miteco.gob.es/content/dam/miteco/es/agua/temas/evaluacion-de-los-recursos-hidricos/sistema-informacion-anuario-aforos/Anuario-21-22-csv.zip"

    # Shared by every instance: the archive's directory and the station -> basin map.
    _remote_archive: Optional[_RemoteZip] = None
    _basins: Optional[dict] = None

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of available Spanish gauge IDs and metadata.

        This method loads the metadata from a cached CSV file located in
        the ``rivretrieve/cached_site_data/`` directory.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return utils.load_cached_metadata_csv("spain")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (constants.DISCHARGE_DAILY_MEAN,)

    def get_metadata(self) -> pd.DataFrame:
        """Downloads and returns ROAN station metadata from MITECO.

        Columns are translated from Spanish to English based on the mapping
        provided in issue #28.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        logger.info(f"Downloading stations metadata from {self.METADATA_ZIP_URL}")
        try:
            resp = utils.requests_retry_session().get(self.METADATA_ZIP_URL)
            resp.raise_for_status()

            with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                target_file = [f for f in z.namelist() if "Situac" in f and "Rio" in f]
                if not target_file:
                    raise FileNotFoundError("Could not find 'Situac...Rio.csv' in ZIP.")
                csv_name = target_file[0]
                logger.info(f"Found metadata file in ZIP: {csv_name}")

                with z.open(csv_name) as f:
                    df = pd.read_csv(f, encoding="latin1", sep=";", low_memory=False)

            df.columns = [c.strip() for c in df.columns]

            rename_map = {
                "COD_HIDRO": constants.GAUGE_ID,
                "NOM_ANUARIO": constants.STATION_NAME,
                "RIO": constants.RIVER,
                "COTA_Z": constants.ALTITUDE,
                "CUENCA_TOTAL": constants.AREA,
            }
            df = df.rename(columns=rename_map)
            df[constants.COUNTRY] = "Spain"
            df[constants.SOURCE] = "ROAN"

            # The metadata contains a few unnamed and unused columns. We drop them.
            drop_cols = [x for x in df.columns if x.startswith("Unnamed: ")]
            df = df.drop(columns=drop_cols)

            if constants.GAUGE_ID in df.columns:
                df[constants.GAUGE_ID] = df[constants.GAUGE_ID].astype(str)
                df = df.set_index(constants.GAUGE_ID)
            else:
                logger.error("GAUGE_ID column not found after renaming.")
                return pd.DataFrame()

            # Convert types
            for col in [constants.ALTITUDE, constants.AREA]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            logger.info(f"Loaded metadata with {len(df)} stations.")
            return df

        except requests.exceptions.RequestException as e:
            logger.error(f"Error downloading metadata ZIP: {e}")
            raise
        except Exception as e:
            logger.error(f"Error processing metadata: {e}")
            raise

    @classmethod
    def _archive(cls) -> "_RemoteZip":
        """The Anuario's CSV archive, opened once per process.

        MITECO renames the file for each new edition of the yearbook
        (``Anuario-21-22-csv.zip``), so the current name is read from the
        Anuario page, falling back to the last one known.
        """
        if cls._remote_archive is None:
            session = utils.requests_retry_session()
            url = cls.ANUARIO_CSV_URL
            try:
                page = session.get(cls.ANUARIO_PAGE_URL, headers=_HEADERS, timeout=60)
                page.raise_for_status()
                links = re.findall(r'href="([^"]*Anuario-\d{2}-\d{2}-csv\.zip)"', page.text)
                if links:
                    url = urljoin(cls.ANUARIO_PAGE_URL, max(links))
            except requests.exceptions.RequestException as e:
                logger.warning(f"Could not read the Anuario page, using {url}: {e}")
            cls._remote_archive = _RemoteZip(url, session)
        return cls._remote_archive

    @classmethod
    def _basin_of(cls, gauge_id: str) -> Optional[str]:
        """The archive folder (one per river basin) that holds a station's records."""
        if cls._basins is None:
            archive = cls._archive()
            basins: dict[str, str] = {}
            for name in archive.members:
                folder, _, leaf = name.rpartition("/")
                if leaf != "estaf.csv":
                    continue
                text = archive.read(name).decode("latin-1")
                for line in text.splitlines()[1:]:
                    station = line.split(";", 1)[0].strip()
                    if station:
                        basins.setdefault(station, folder)
            cls._basins = basins
        return cls._basins.get(str(gauge_id).strip())

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> Optional[bytes]:
        """Returns the station's rows of its basin's daily table (``afliq.csv``).

        Only that one table is downloaded — 4 to 18 MB compressed, against
        136 MB for the whole archive — using HTTP range requests.
        """
        if variable != constants.DISCHARGE_DAILY_MEAN:
            logger.error(f"Unsupported variable: {variable} for SpainFetcher")
            return None

        basin = self._basin_of(gauge_id)
        if basin is None:
            logger.warning(f"Station {gauge_id} is not in the Anuario de Aforos archive")
            return None

        table = self._archive().read(f"{basin}/afliq.csv")
        prefix = re.escape(str(gauge_id).strip().encode("ascii"))
        rows = re.findall(rb"^" + prefix + rb";[^\r\n]*", table, re.M)
        logger.info(f"Found {len(rows)} daily rows for {gauge_id} in {basin}/afliq.csv")
        return b"\n".join(rows)

    def _parse_data(self, gauge_id: str, raw_data: Optional[bytes], variable: str) -> pd.DataFrame:
        """Parses ``indroea;fecha;altura;caudal`` rows: dates dd/mm/yyyy, stage m, discharge m³/s."""
        if not raw_data:
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

        try:
            df = pd.read_csv(
                io.BytesIO(raw_data),
                sep=";",
                header=None,
                names=["indroea", "fecha", "altura", "caudal"],
                dtype={"indroea": str},
            )
            df[constants.TIME_INDEX] = pd.to_datetime(df["fecha"], format="%d/%m/%Y", errors="coerce")
            df[variable] = pd.to_numeric(df["caudal"], errors="coerce")
            return (
                df[[constants.TIME_INDEX, variable]]
                .dropna()
                .sort_values(constants.TIME_INDEX)
                .set_index(constants.TIME_INDEX)
            )
        except Exception as e:
            logger.error(f"Error parsing data for gauge {gauge_id}: {e}")
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

    def get_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Fetches and parses time series data for a specific gauge and variable.

        Args:
            gauge_id: The site-specific identifier for the gauge.
            variable: The variable to fetch.
            start_date: Optional start date in 'YYYY-MM-DD' format.
            end_date: Optional end date in 'YYYY-MM-DD' format.

        Returns:
            pd.DataFrame: A pandas DataFrame indexed by time.
        """
        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        start_date = utils.format_start_date(start_date)
        end_date = utils.format_end_date(end_date)

        try:
            raw_data = self._download_data(gauge_id, variable, start_date, end_date)
            df = self._parse_data(gauge_id, raw_data, variable)

            if not df.empty:
                start_date_dt = pd.to_datetime(start_date)
                end_date_dt = pd.to_datetime(end_date)
                df = df[(df.index >= start_date_dt) & (df.index <= end_date_dt)]
            return df
        except Exception as e:
            logger.error(f"Failed to get data for site {gauge_id}, variable {variable}: {e}")
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])
