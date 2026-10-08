"""Fetcher for Brazilian river gauge data from ANA Hidroweb."""

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

import pandas as pd
import requests
from dotenv import load_dotenv

from . import base, constants, utils

logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv(dotenv_path=os.path.join((os.path.dirname(__file__)), ".env"))

# Get credentials from environment variables
USERNAME = os.environ.get("ANA_USERNAME")
PASSWORD = os.environ.get("ANA_PASSWORD")


class BrazilFetcher(base.RiverDataFetcher):
    """Fetches river gauge data from Brazil's National Water and Sanitation Agency (ANA).

    Data Source: ANA Hidroweb (https://www.ana.gov.br/hidroweb/), through one of two services:

    - With credentials, the Hidroweb API v2. Set ``ANA_USERNAME`` and
      ``ANA_PASSWORD`` in a ``.env`` file in the ``rivretrieve`` directory, or
      pass them to the constructor.
    - Without them, ANA's public ``HidroSerieHistorica`` web service
      (https://telemetriaws1.ana.gov.br/ServiceANA.asmx), which serves the same
      daily series and needs no account. Where ANA has published both raw and
      consisted (quality-controlled) values for a month, the consisted ones are used.

    Station metadata (``get_metadata``) still needs credentials.

    Supported Variables:
        - ``constants.DISCHARGE_DAILY_MEAN`` (m³/s)
        - ``constants.STAGE_DAILY_MEAN`` (m)
    """

    BASE_URL = "https://www.ana.gov.br/hidrowebservice/EstacoesTelemetricas"
    AUTH_URL = f"{BASE_URL}/OAUth/v1"
    PUBLIC_SERIES_URL = "https://telemetriaws1.ana.gov.br/ServiceANA.asmx/HidroSerieHistorica"
    #: Years per request to the public service, which answers with one XML document.
    PUBLIC_CHUNK_YEARS = 20

    def __init__(self, username: Optional[str] = None, password: Optional[str] = None):
        super().__init__()
        self.username = username or USERNAME
        self.password = password or PASSWORD
        self._token = None
        self._token_expiry = 0

        if not self.username or not self.password:
            logger.info(
                "No ANA_USERNAME/ANA_PASSWORD set; daily series will come from ANA's public "
                "HidroSerieHistorica service."
            )

    @staticmethod
    def get_cached_metadata() -> pd.DataFrame:
        """Retrieves a DataFrame of available Brazilian gauge IDs and metadata.

        This method loads the metadata from a cached CSV file located in
        the ``rivretrieve/cached_site_data/`` directory.

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        return utils.load_cached_metadata_csv("brazil")

    @staticmethod
    def get_available_variables() -> tuple[str, ...]:
        return (constants.DISCHARGE_DAILY_MEAN, constants.STAGE_DAILY_MEAN)

    def get_metadata(self) -> pd.DataFrame:
        """Fetches station metadata for all Brazilian states from the ANA Hidroweb API.

        Data is fetched from the HidroInventarioEstacoes endpoint:
        ``https://www.ana.gov.br/hidrowebservice/EstacoesTelemetricas/HidroInventarioEstacoes/v1``

        Returns:
            pd.DataFrame: A DataFrame indexed by gauge_id, containing site metadata.
        """
        if not self.username or not self.password:
            logger.error("ANA Username or Password not provided.")
            return pd.DataFrame().set_index(constants.GAUGE_ID)

        token = self._get_token()
        if not token:
            logger.error("Cannot fetch metadata without a token.")
            return pd.DataFrame().set_index(constants.GAUGE_ID)

        states = [
            "AC",
            "AL",
            "AM",
            "AP",
            "BA",
            "CE",
            "DF",
            "ES",
            "GO",
            "MA",
            "MT",
            "MS",
            "MG",
            "PA",
            "PB",
            "PR",
            "PE",
            "PI",
            "RJ",
            "RN",
            "RS",
            "RO",
            "RR",
            "SC",
            "SP",
            "SE",
            "TO",
        ]

        metadata_url = f"{self.BASE_URL}/HidroInventarioEstacoes/v1"
        all_stations = []
        s = utils.requests_retry_session()
        headers = {"Authorization": f"Bearer {token}"}

        for state in states:
            logger.info(f"Fetching metadata for state: {state}")
            params = {"Unidade Federativa": state}
            try:
                response = s.get(metadata_url, params=params, headers=headers)
                response.raise_for_status()
                data = response.json()
                if isinstance(data, list):
                    all_stations.extend(data)
                elif isinstance(data, dict) and data.get("status") == "OK" and data.get("items"):
                    all_stations.extend(data["items"])
                else:
                    logger.warning(f"No stations found for state {state} or unexpected response: {data}")
            except requests.exceptions.RequestException as e:
                logger.error(f"Error fetching metadata for state {state}: {e}")
            except Exception as e:
                logger.error(f"Error processing metadata for state {state}: {e}")
            time.sleep(0.1)

        if not all_stations:
            return pd.DataFrame().set_index(constants.GAUGE_ID)

        df = pd.DataFrame(all_stations)

        rename_map = {
            "codigoestacao": constants.GAUGE_ID,
            "Estacao_Nome": constants.STATION_NAME,
            "Latitude": constants.LATITUDE,
            "Longitude": constants.LONGITUDE,
            "Altitude": constants.ALTITUDE,
            "Area_Drenagem": constants.AREA,
            "Bacia_Nome": constants.RIVER,
        }
        df = df.rename(columns=rename_map)
        df[constants.GAUGE_ID] = df[constants.GAUGE_ID].astype(str)
        return df.set_index(constants.GAUGE_ID)

    def _get_token(self) -> Optional[str]:
        """Gets and caches the authentication token."""
        if not self.username or not self.password:
            return None

        if self._token and time.time() < self._token_expiry:
            return self._token

        logger.info("Fetching new authentication token for Brazil...")
        headers = {"accept": "*/*", "Identificador": self.username, "Senha": self.password}
        s = utils.requests_retry_session()
        try:
            response = s.get(self.AUTH_URL, headers=headers)
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "OK" and data.get("items", {}).get("sucesso"):
                self._token = data["items"]["tokenautenticacao"]
                # Set expiry to 14 minutes (840 seconds) to be safe
                self._token_expiry = time.time() + 840
                logger.info("Successfully obtained new token.")
                return self._token
            else:
                logger.error(f"Authentication failed: {data}")
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching token: {e}")
            return None
        except Exception as e:
            logger.error(f"Error processing token response: {e}")
            return None

    def _download_data(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """Downloads raw data in yearly chunks."""
        if not self.username or not self.password:
            return []

        if variable == constants.DISCHARGE_DAILY_MEAN:
            data_url = f"{self.BASE_URL}/HidroSerieVazao/v1"
        elif variable == constants.STAGE_DAILY_MEAN:
            data_url = f"{self.BASE_URL}/HidroSerieCotas/v1"
        else:
            logger.error(f"Unsupported variable for daily download: {variable}")
            return []

        all_data = []
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        s = utils.requests_retry_session()

        current_year = start_dt.year
        while current_year <= end_dt.year:
            token = self._get_token()
            if not token:
                logger.error("Cannot download data without a token.")
                break

            year_start = datetime(current_year, 1, 1)
            year_end = datetime(current_year, 12, 31)

            req_start_date = max(start_dt, year_start)
            req_end_date = min(end_dt, year_end)

            if req_start_date > req_end_date:
                current_year += 1
                continue

            # Manually build the URL to control encoding
            base_data_url = f"{data_url}?"
            params_list = [
                f"C%C3%B3digo%20da%20Esta%C3%A7%C3%A3o={gauge_id}",
                "Tipo%20Filtro%20Data=DATA_LEITURA",
                f"Data%20Inicial%20(yyyy-MM-dd)={req_start_date.strftime('%Y-%m-%d')}",
                f"Data%20Final%20(yyyy-MM-dd)={req_end_date.strftime('%Y-%m-%d')}",
            ]
            full_url = base_data_url + "&".join(params_list)

            headers = {"accept": "*/*", "Authorization": f"Bearer {token}"}

            logger.debug(
                f"Fetching {variable} for site {gauge_id} from {req_start_date.strftime('%Y-%m-%d')} to "
                f" {req_end_date.strftime('%Y-%m-%d')}"
            )
            logger.debug(f"Request URL: {full_url}")
            try:
                response = s.get(full_url, headers=headers)
                response.raise_for_status()
                data = response.json()
                if isinstance(data, list):
                    all_data.extend(data)
                elif isinstance(data, dict) and data.get("status") == "OK" and data.get("items"):
                    all_data.extend(data["items"])
                elif isinstance(data, dict) and data.get("status") == "OK":
                    logger.info(f"No items returned for {gauge_id} for year {current_year}")
                else:
                    logger.warning(f"API returned unexpected response: {data}")

            except requests.exceptions.RequestException as e:
                logger.error(f"Error downloading data chunk for {gauge_id}: {e}")
            except Exception as e:
                logger.error(f"Error processing data chunk for {gauge_id}: {e}")

            current_year += 1
            time.sleep(0.2)  # Be nice to the API

        return all_data

    def _parse_data(self, gauge_id: str, raw_data: List[Dict[str, Any]], variable: str) -> pd.DataFrame:
        """Parses the raw JSON data from daily endpoints."""
        if not raw_data:
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

        all_dfs = []
        try:
            for month_data in raw_data:
                if not isinstance(month_data, dict):
                    logger.warning(f"Unexpected item format in raw_data: {month_data}")
                    continue

                month_str = month_data.get("Data_Hora_Dado")
                if not month_str:
                    logger.warning(f"Missing 'Data_Hora_Dado' in month_data: {month_data}")
                    continue

                year = int(month_str[:4])
                month = int(month_str[5:7])

                if variable == constants.DISCHARGE_DAILY_MEAN:
                    val_prefix = "Vazao_"
                    unit_conversion = 1.0
                elif variable == constants.STAGE_DAILY_MEAN:
                    val_prefix = "Cota_"
                    unit_conversion = 0.01  # cm to m
                else:
                    return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

                day_values = {}
                for day in range(1, 32):
                    day_str = f"{day:02d}"
                    val_col = f"{val_prefix}{day_str}"
                    if val_col in month_data and month_data[val_col] is not None:
                        try:
                            date = datetime(year, month, day)
                            day_values[date] = pd.to_numeric(month_data[val_col], errors="coerce") * unit_conversion
                        except ValueError:
                            # Handles invalid dates like Feb 30
                            continue

                month_df = pd.DataFrame(list(day_values.items()), columns=[constants.TIME_INDEX, variable])
                if not month_df.empty:
                    all_dfs.append(month_df)

            if not all_dfs:
                return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

            df = pd.concat(all_dfs, ignore_index=True)
            df = df.dropna().sort_values(by=constants.TIME_INDEX).set_index(constants.TIME_INDEX)
            return df
        except Exception as e:
            logger.error(f"Error parsing CSV data for site {gauge_id}: {e}")
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])

    def _download_public(self, gauge_id: str, variable: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """Downloads monthly rows from the public HidroSerieHistorica service, no account needed."""
        kind = {constants.DISCHARGE_DAILY_MEAN: "3", constants.STAGE_DAILY_MEAN: "1"}[variable]
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        s = utils.requests_retry_session()

        rows: List[Dict[str, Any]] = []
        chunk_start = start_dt
        while chunk_start <= end_dt:
            chunk_end = min(end_dt, datetime(chunk_start.year + self.PUBLIC_CHUNK_YEARS - 1, 12, 31))
            params = {
                "codEstacao": gauge_id,
                "dataInicio": chunk_start.strftime("%d/%m/%Y"),
                "dataFim": chunk_end.strftime("%d/%m/%Y"),
                "tipoDados": kind,
                "nivelConsistencia": "",
            }
            response = s.get(self.PUBLIC_SERIES_URL, params=params, timeout=120)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] == "SerieHistorica":
                    rows.append({child.tag.rsplit("}", 1)[-1]: child.text for child in element})
            chunk_start = chunk_end + timedelta(days=1)
            time.sleep(0.2)  # Be nice to the API
        return rows

    def _parse_public(self, gauge_id: str, rows: List[Dict[str, Any]], variable: str) -> pd.DataFrame:
        """One value per day from the service's month rows.

        Each month can come several times: raw (``NivelConsistencia`` 1) and
        consisted (2) versions, and for stage also the individual 07:00 and
        17:00 readings (``MediaDiaria`` 0). Daily means win, then consisted data.
        """
        prefix, scale = {
            constants.DISCHARGE_DAILY_MEAN: ("Vazao", 1.0),
            constants.STAGE_DAILY_MEAN: ("Cota", 0.01),  # cm to m
        }[variable]

        best: Dict[tuple, Dict[str, Any]] = {}
        for row in rows:
            if row.get("MediaDiaria") not in (None, "1"):
                continue
            stamp = (row.get("DataHora") or "")[:7]
            if len(stamp) != 7:
                continue
            month = (int(stamp[:4]), int(stamp[5:7]))
            level = int(row.get("NivelConsistencia") or 0)
            if month not in best or level > int(best[month].get("NivelConsistencia") or 0):
                best[month] = row

        records = []
        for (year, month), row in best.items():
            for day in range(1, 32):
                value = pd.to_numeric(row.get(f"{prefix}{day:02d}"), errors="coerce")
                if pd.isna(value):
                    continue
                try:
                    records.append((datetime(year, month, day), value * scale))
                except ValueError:  # e.g. 30 February
                    continue

        if not records:
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])
        df = pd.DataFrame(records, columns=[constants.TIME_INDEX, variable])
        return df.sort_values(constants.TIME_INDEX).set_index(constants.TIME_INDEX)

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

        if not self.username or not self.password:
            # No account: the public service. Its HTTP errors propagate, so a
            # caller can tell an outage from a station without data.
            rows = self._download_public(gauge_id, variable, start_date, end_date)
            df = self._parse_public(gauge_id, rows, variable)
            if df.empty:
                return df
            return df[(df.index >= pd.to_datetime(start_date)) & (df.index <= pd.to_datetime(end_date))]

        try:
            raw_data = self._download_data(gauge_id, variable, start_date, end_date)
            df = self._parse_data(gauge_id, raw_data, variable)

            if df.empty:
                return df

            start_date_dt = pd.to_datetime(start_date)
            end_date_dt = pd.to_datetime(end_date)
            df = df[(df.index >= start_date_dt) & (df.index <= end_date_dt)]
            return df
        except Exception as e:
            logger.error(f"Failed to get data for site {gauge_id}, variable {variable}: {e}")
            return pd.DataFrame(columns=[constants.TIME_INDEX, variable])
