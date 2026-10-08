"""Fetcher for Chilean river gauge data."""

import io
import json
import logging
import re
import time
from typing import Optional
from urllib.parse import urljoin

import pandas as pd
import requests

from . import base, constants, utils

logger = logging.getLogger(__name__)


class ChileFetcher(base.RiverDataFetcher):
    """Fetches river gauge data from Chile's CR2 explorador.

    Data Source: CR2 explorador (https://explorador.cr2.cl/)

    Supported Variables:
        - ``constants.DISCHARGE_DAILY_MEAN`` (m³/s)
    """

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of available Chilean gauge IDs and metadata.

        This method loads the metadata from a cached CSV file located in
        the ``rivretrieve/cached_site_data/`` directory.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return utils.load_cached_metadata_csv("chile")

    BASE_URL = "https://explorador.cr2.cl/"
    REQUEST_URL = BASE_URL + "request.php"

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (constants.DISCHARGE_DAILY_MEAN,)

    @classmethod
    def _export_url(cls, body: str) -> Optional[str]:
        """Finds the exported CSV's address in the explorer's reply.

        The explorer used to embed an absolute URL in its reply; since 2026 it
        answers with JSON naming a path relative to the site,
        ``{"export": {"series": {"url": "tmp/map_xxxx/EC_series.csv"}}}``.
        """
        try:
            relative = json.loads(body)["export"]["series"]["url"]
            if relative:
                return urljoin(cls.BASE_URL, relative)
        except (ValueError, KeyError, TypeError):
            pass
        match = re.search(r"https://(?:www\.)?explorador\.cr2\.cl/tmp/[^/]+/[^\"]+\.csv", body)
        return match.group(0) if match else None

    def _download_data(
        self,
        gauge_id: str,
        variable: str,
        start_date: str,
        end_date: str,
    ) -> Optional[pd.DataFrame]:
        if variable != constants.DISCHARGE_DAILY_MEAN:
            logger.warning(f"ChileFetcher only supports variable='{constants.DISCHARGE_DAILY_MEAN}'")
            return None

        # The request the explorer's own "export series" button sends, as first
        # extracted from the R package. The time window always spans the whole
        # record; get_data() clips it afterwards.
        options = {
            "variable": {
                "id": "qflxDaily",
                "var": "caudal",
                "intv": "daily",
                "season": "year",
                "stat": "mean",
                "minFrac": 80,
            },
            "time": {"start": -946771200, "end": int(time.time()), "months": "Año completo"},
            "anomaly": {
                "enabled": False,
                "type": "dif",
                "rank": "no",
                "start_year": 1980,
                "end_year": 2010,
                "minFrac": 70,
            },
            "map": {
                "stat": "mean",
                "minFrac": 10,
                "borderColor": "7F7F7F",
                "colorRamp": "Jet",
                "showNaN": False,
                "limits": {"range": [5, 95], "size": [4, 12], "type": "prc"},
            },
            "series": {"sites": [gauge_id], "start": None, "end": None},
            "export": {
                "map": "Shapefile",
                "series": "CSV",
                "view": {
                    "frame": "Vista Actual",
                    "map": "roadmap",
                    "clat": -18.0036,
                    "clon": -69.6331,
                    "zoom": 5,
                    "width": 461,
                    "height": 2207,
                },
            },
            "action": ["export_series"],
        }

        s = utils.requests_retry_session()
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            time.sleep(0.3)  # Be nice to the server
            response = s.get(
                self.REQUEST_URL,
                params={"options": json.dumps(options, separators=(",", ":"))},
                headers=headers,
            )
            response.raise_for_status()

            csv_url = self._export_url(response.text)
            if not csv_url:
                logger.error(f"Could not find download link in response for site {gauge_id}")
                return None
            logger.info(f"Found CSV URL: {csv_url}")

            time.sleep(0.3)
            csv_response = s.get(csv_url, headers=headers)
            csv_response.raise_for_status()

            df = pd.read_csv(io.StringIO(csv_response.text))
            return df

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching data for site {gauge_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error processing data for site {gauge_id}: {e}")
            return None

    def _parse_data(
        self,
        gauge_id: str,
        raw_df: Optional[pd.DataFrame],
        variable: str,
    ) -> pd.DataFrame:
        """Parses the raw DataFrame."""
        if raw_df is None or raw_df.empty:
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

        try:
            # Clean column names (remove leading/trailing spaces)
            raw_df.columns = raw_df.columns.str.strip()

            if not all(col in raw_df.columns for col in ["agno", "mes", "dia", "valor"]):
                logger.warning(f"Missing expected columns for site {gauge_id}")
                return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

            df = raw_df.copy()
            df[constants.TIME_INDEX] = pd.to_datetime(
                df[["agno", "mes", "dia"]].rename(columns={"agno": "year", "mes": "month", "dia": "day"}),
                errors="coerce",
            )
            df = df.dropna(subset=[constants.TIME_INDEX])
            df[variable] = pd.to_numeric(df["valor"], errors="coerce")
            # Unit is already m3/s according to CR2 metadata

            return (
                df[[constants.TIME_INDEX, variable]]
                .dropna()
                .sort_values(by=constants.TIME_INDEX)
                .set_index(constants.TIME_INDEX)
            )
        except Exception as e:
            logger.error(f"Error parsing data for site {gauge_id}: {e}")
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

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
        if variable != constants.DISCHARGE_DAILY_MEAN:
            logger.warning(f"ChileFetcher only supports variable='{constants.DISCHARGE_DAILY_MEAN}'")
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

        start_date = utils.format_start_date(start_date)
        end_date = utils.format_end_date(end_date)

        if variable not in self.get_available_variables():
            raise ValueError(f"Unsupported variable: {variable}")

        try:
            raw_data = self._download_data(gauge_id, variable, start_date, end_date)
            df = self._parse_data(gauge_id, raw_data, variable)
            if df.empty:
                return df

            # Filter by date range
            start_date_dt = pd.to_datetime(start_date)
            end_date_dt = pd.to_datetime(end_date)
            df = df[(df.index >= start_date_dt) & (df.index <= end_date_dt)]
            return df
        except Exception as e:
            logger.error(f"Failed to get data for site {gauge_id}, variable {variable}: {e}")
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])
