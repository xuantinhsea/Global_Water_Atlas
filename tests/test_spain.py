import io
import os
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from rivretrieve import SpainFetcher, constants


class TestSpainFetcher(unittest.TestCase):
    def setUp(self):
        self.fetcher = SpainFetcher()
        self.test_data_dir = Path(os.path.dirname(__file__)) / "test_data"
        self.sample_metadata_zip = self.test_data_dir / "spain-listado-estaciones-aforo-sample.zip"

    def load_sample_zip_content(self):
        with open(self.sample_metadata_zip, "rb") as f:
            return f.read()

    @patch("rivretrieve.utils.requests_retry_session")
    def test_get_metadata(self, mock_requests_session):
        mock_session = MagicMock()
        mock_requests_session.return_value = mock_session

        mock_zip_response = MagicMock()
        mock_zip_response.content = self.load_sample_zip_content()
        mock_zip_response.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_zip_response

        result_df = self.fetcher.get_metadata()

        expected_data = {
            constants.GAUGE_ID: ["1010", "1011"],
            constants.STATION_NAME: ["PUENTE QUEROL", "RUAPETÍN"],
            constants.RIVER: ["Sil", "Sil"],
            "REGIMEN_RIO": [np.nan, np.nan],
            "COD_SAIH": [np.nan, np.nan],
            "COD_DMA": [np.nan, np.nan],
            "COD_MASA_AGUA": ["ES010MSPFES414MAR000770", "ES010MSPFES436MAR001180"],
            "FOTOGRAFIA": ["N.D.jpg", "N.D.jpg"],
            "PLANO": ["N.D.jpg", "N.D.jpg"],
            "SECCION": ["N.D.jpg", "N.D.jpg"],
            "ORGANISMO_CUENCA_VISOR": ["C.H. MIÑO-SIL", "C.H. MIÑO-SIL"],
            "COD_SITUACION_ESTACION": [4, 4],
            "SITUACION_ESTACION": ["RÍO", "RÍO"],
            "ESTADO": ["BAJA", "BAJA"],
            "ANO_INICIO_MEDIDAS": [1913.0, 1912.0],
            "ANO_FIN_MEDIDAS": [1959.0, 1940.0],
            "COD_SAICA": [np.nan, np.nan],
            "COORD_UTMX_H30_ETRS89": [204647, 160356],
            "COORD_UTMY_H30_ETRS89": [4716265, 4701177],
            constants.ALTITUDE: [490.0, 282.0],
            "CUENCA_RECEP": [833, 6346],
            constants.AREA: [7983.0, 7983.0],
            "NUM_CUENCA": [1414.0, 1436.0],
            "HOJA_1_50000": ["PONFERRADA (158)", "BARCO DE VALDEORRAS (190)"],
            "SISTEMA_EXPLO": [np.nan, "SIL INFERIOR"],
            "ESCALA_RC": [np.nan, np.nan],
            "LONG_RC": [np.nan, np.nan],
            "ANCH_RC": [np.nan, np.nan],
            "CASETA_RC": [np.nan, np.nan],
            "PASARELA_RC": [np.nan, np.nan],
            "PROPIETARIO": ["ESTADO", np.nan],
            "SENSOR_1": [np.nan, np.nan],
            "TIPO_CASETA_RC": [np.nan, np.nan],
            "TIPO_ESC_RC": [np.nan, np.nan],
            "TIPO_ESTACION": [np.nan, np.nan],
            "TRANSM_1": [np.nan, np.nan],
            "VERTEDERO_RC": [np.nan, np.nan],
            "TIPO_VERT_RC": [np.nan, np.nan],
            "TERMINO_MUNICIPAL": ["Ponferrada", "Rua"],
            "PROVINCIA": ["León", "Orense"],
            constants.COUNTRY: ["Spain", "Spain"],
            constants.SOURCE: ["ROAN", "ROAN"],
        }
        expected_df = pd.DataFrame(expected_data).set_index(constants.GAUGE_ID)

        # Convert columns to numeric where appropriate, matching get_metadata types
        expected_numeric_columns = [
            constants.ALTITUDE,
            constants.AREA,
            "ANO_INICIO_MEDIDAS",
            "ANO_FIN_MEDIDAS",
            "CUENCA_RECEP",
            "NUM_CUENCA",
        ]
        for col in expected_numeric_columns:
            if col in expected_df.columns:
                expected_df[col] = pd.to_numeric(expected_df[col], errors="coerce")

        assert_frame_equal(result_df, expected_df, check_dtype=False, check_like=True)

    @patch("rivretrieve.utils.requests_retry_session")
    def test_get_data_discharge(self, mock_requests_session):
        archive = _anuario_zip(
            {
                "CANTABRICO/estaf.csv": "indroea;lugar\n1080;ANDOAIN\n1081;OTRA\n",
                "CANTABRICO/afliq.csv": (
                    "indroea;fecha;altura;caudal\n"
                    "1080;31/12/2020;1.70;1.49\n"
                    "1080;01/01/2021;1.78;1.56\n"
                    "1080;02/01/2021;1.79;1.57\n"
                    "1080;03/01/2021;1.80;\n"
                    "10800;01/01/2021;9.99;99.9\n"
                    "1081;01/01/2021;0.50;0.25\n"
                ),
                "EBRO/estaf.csv": "indroea;lugar\n9001;ZARAGOZA\n",
                "EBRO/afliq.csv": "indroea;fecha;altura;caudal\n9001;01/01/2021;2.00;500\n",
            }
        )
        session = _RangeSession(archive)
        mock_requests_session.return_value = session

        result_df = self.fetcher.get_data("1080", constants.DISCHARGE_DAILY_MEAN, "2021-01-01", "2021-01-03")

        expected_df = pd.DataFrame(
            {
                constants.TIME_INDEX: pd.to_datetime(["2021-01-01", "2021-01-02"]),
                constants.DISCHARGE_DAILY_MEAN: [1.56, 1.57],
            }
        ).set_index(constants.TIME_INDEX)
        assert_frame_equal(result_df, expected_df, check_dtype=False, check_index_type=False)

        # Every archive read was a ranged request, and the other basin's table was never fetched.
        self.assertTrue(session.ranges)
        self.assertNotIn("EBRO/afliq.csv", session.members_read(archive))

    @patch("rivretrieve.utils.requests_retry_session")
    def test_unknown_station_returns_empty(self, mock_requests_session):
        archive = _anuario_zip({"EBRO/estaf.csv": "indroea;lugar\n9001;ZARAGOZA\n", "EBRO/afliq.csv": "x\n"})
        mock_requests_session.return_value = _RangeSession(archive)

        result_df = self.fetcher.get_data("1080", constants.DISCHARGE_DAILY_MEAN, "2021-01-01", "2021-01-03")
        self.assertTrue(result_df.empty)

    def tearDown(self):
        # The archive directory and station map are cached on the class.
        SpainFetcher._remote_archive = None
        SpainFetcher._basins = None


def _anuario_zip(members: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            archive.writestr(name, text.encode("latin-1"))
    return buffer.getvalue()


class _RangeResponse:
    def __init__(self, status_code: int, content: bytes = b"", text: str = "", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RangeSession:
    """Serves one ZIP archive the way a web server honouring Range requests would."""

    PAGE = '<a href="/content/dam/x/Anuario-21-22-csv.zip">CSV</a>'

    def __init__(self, archive: bytes):
        self.archive = archive
        self.ranges: list[tuple[int, int]] = []

    def head(self, url, **kwargs):
        return _RangeResponse(200, headers={"Content-Length": str(len(self.archive))})

    def get(self, url, headers=None, **kwargs):
        if url.endswith(".html"):
            return _RangeResponse(200, text=self.PAGE)
        first, last = (int(part) for part in headers["Range"].removeprefix("bytes=").split("-"))
        self.ranges.append((first, last))
        return _RangeResponse(206, content=self.archive[first : last + 1])

    def members_read(self, archive: bytes) -> set:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            starts = {info.header_offset: info.filename for info in zf.infolist()}
        return {starts[first] for first, _ in self.ranges if first in starts}


if __name__ == "__main__":
    unittest.main()
