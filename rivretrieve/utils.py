"""Utility functions for the RivRetrieve package."""

import datetime
import io
import logging
import os
import struct
import zipfile
import zlib
from typing import Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import constants

logger = logging.getLogger(__name__)

_RANGE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RivRetrieve)"}


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


def _get_range(session: requests.Session, url: str, first: int, last: int) -> bytes:
    headers = {**_RANGE_HEADERS, "Range": f"bytes={first}-{last}"}
    with session.get(url, headers=headers, stream=True, timeout=300) as response:
        response.raise_for_status()
        if response.status_code != 206:
            # Never fall back to downloading the whole archive by accident.
            raise IOError(f"{url} ignored the Range header (HTTP {response.status_code})")
        return response.content


def read_zip_member(
    session: requests.Session, url: str, header_offset: int, compress_size: int, compress_type: int
) -> bytes:
    """Downloads and decompresses one member of a remote ZIP archive, in a single request.

    The member's position comes from the archive's central directory, read
    earlier or kept in an index, so nothing else of the archive is fetched.
    """
    # The local header repeats the name and may carry a different extra field,
    # so read a little past it rather than trusting the central directory's lengths.
    blob = _get_range(session, url, header_offset, header_offset + 30 + 1024 + compress_size - 1)
    if blob[:4] != b"PK\x03\x04":
        raise IOError(f"No local file header at offset {header_offset} of {url}")
    name_length, extra_length = struct.unpack("<HH", blob[26:30])
    start = 30 + name_length + extra_length
    raw = blob[start : start + compress_size]
    if compress_type == zipfile.ZIP_STORED:
        return raw
    if compress_type == zipfile.ZIP_DEFLATED:
        return zlib.decompress(raw, -zlib.MAX_WBITS)
    raise IOError(f"Unsupported compression {compress_type} at offset {header_offset} of {url}")


class RemoteZip:
    """Reads single members of a ZIP archive on a server that honours HTTP Range requests.

    Only the central directory (at the end of the file) and the members asked
    for are downloaded, so one table can be read out of a multi-GB archive.
    """

    #: First guess at how many trailing bytes hold the central directory; doubled until they do.
    TAIL_BYTES = 1 << 18
    MAX_TAIL_BYTES = 1 << 26

    def __init__(self, url: str, session: requests.Session):
        self.url = url
        self.session = session
        head = session.head(url, headers=_RANGE_HEADERS, allow_redirects=True, timeout=60)
        head.raise_for_status()
        self.size = int(head.headers["Content-Length"])

        tail_bytes = self.TAIL_BYTES
        while True:
            tail_start = max(0, self.size - tail_bytes)
            tail = _get_range(session, url, tail_start, self.size - 1)
            try:
                with zipfile.ZipFile(_TailFile(tail, tail_start, self.size)) as archive:
                    self.members = {info.filename: info for info in archive.infolist()}
                break
            except OSError:
                # The directory begins before the downloaded tail.
                if tail_start == 0 or tail_bytes >= self.MAX_TAIL_BYTES:
                    raise
                tail_bytes *= 4

    def read(self, name: str) -> bytes:
        """Downloads and decompresses one member, in a single request."""
        info = self.members[name]
        return read_zip_member(self.session, self.url, info.header_offset, info.compress_size, info.compress_type)


def format_start_date(start_date: Optional[str]) -> str:
    """Formats the start date, defaulting to 1900-01-01 if None."""
    if start_date is None:
        return "1900-01-01"
    try:
        datetime.datetime.strptime(start_date, "%Y-%m-%d")
        return start_date
    except ValueError:
        raise ValueError("Incorrect start_date format, should be YYYY-MM-DD")


def format_end_date(end_date: Optional[str]) -> str:
    """Formats the end date, defaulting to today if None."""
    if end_date is None:
        return datetime.date.today().strftime("%Y-%m-%d")
    try:
        datetime.datetime.strptime(end_date, "%Y-%m-%d")
        return end_date
    except ValueError:
        raise ValueError("Incorrect end_date format, should be YYYY-MM-DD")


def requests_retry_session(
    retries=3,
    backoff_factor=0.3,
    status_forcelist=(500, 502, 504),
    session=None,
) -> requests.Session:
    """Creates a requests session with retry logic."""
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def load_cached_metadata_csv(country_code: str) -> pd.DataFrame:
    """Loads site data from a CSV file in the data directory."""
    current_dir = os.path.dirname(__file__)
    file_path = os.path.join(current_dir, "cached_site_data", f"{country_code}_sites.csv")
    try:
        df = pd.read_csv(file_path, dtype={constants.GAUGE_ID: str})
        return df.set_index(constants.GAUGE_ID)
    except FileNotFoundError:
        logger.error(f"Site file not found: {file_path}")
        raise
