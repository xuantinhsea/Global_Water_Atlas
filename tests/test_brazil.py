import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
from pandas.testing import assert_frame_equal

from rivretrieve import BrazilFetcher, constants


class TestBrazilFetcher(unittest.TestCase):
    def setUp(self):
        self.fetcher = BrazilFetcher(username="testuser", password="testpass")

    @patch("rivretrieve.brazil.BrazilFetcher._get_token")
    @patch("rivretrieve.utils.requests_retry_session")
    def test_get_data_discharge(self, mock_session, mock_get_token):
        mock_get_token.return_value = "fake_token"
        mock_response = MagicMock()
        mock_json = [
            {
                "Data_Hora_Dado": "2024-01-01 00:00:00.0",
                "Vazao_01": "10.0",
                "Vazao_02": "11.0",
                "Vazao_03": "12.0",
                # ... add other days up to 31, some can be None
                "Vazao_31": None,
            }
        ]
        mock_response.json.return_value = mock_json
        mock_response.raise_for_status = MagicMock()
        mock_session.return_value.get.return_value = mock_response

        gauge_id = "12345678"
        variable = constants.DISCHARGE_DAILY_MEAN
        start_date = "2024-01-01"
        end_date = "2024-01-03"

        result_df = self.fetcher.get_data(gauge_id, variable, start_date, end_date)

        expected_data = {
            constants.TIME_INDEX: pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
            constants.DISCHARGE_DAILY_MEAN: [10.0, 11.0, 12.0],
        }
        expected_df = pd.DataFrame(expected_data).set_index(constants.TIME_INDEX)

        assert_frame_equal(result_df, expected_df)
        mock_session.return_value.get.assert_called_once()

    @patch("rivretrieve.brazil.BrazilFetcher._get_token")
    @patch("rivretrieve.utils.requests_retry_session")
    def test_get_data_stage(self, mock_session, mock_get_token):
        mock_get_token.return_value = "fake_token"
        mock_response = MagicMock()
        mock_json = [
            {
                "Data_Hora_Dado": "2024-02-01 00:00:00.0",
                "Cota_01": "150",
                "Cota_02": "155",
                # ... add other days up to 29
                "Cota_29": "160",
            }
        ]
        mock_response.json.return_value = mock_json
        mock_response.raise_for_status = MagicMock()
        mock_session.return_value.get.return_value = mock_response

        gauge_id = "12345678"
        variable = constants.STAGE_DAILY_MEAN
        start_date = "2024-02-01"
        end_date = "2024-02-02"

        result_df = self.fetcher.get_data(gauge_id, variable, start_date, end_date)

        expected_data = {
            constants.TIME_INDEX: pd.to_datetime(["2024-02-01", "2024-02-02"]),
            constants.STAGE_DAILY_MEAN: [1.50, 1.55],  # Converted to meters
        }
        expected_df = pd.DataFrame(expected_data).set_index(constants.TIME_INDEX)

        assert_frame_equal(result_df, expected_df)
        mock_session.return_value.get.assert_called_once()


def _public_series(*rows: str) -> bytes:
    """A HidroSerieHistorica reply, trimmed to the fields the parser reads."""
    body = "".join(f"<SerieHistorica>{row}</SerieHistorica>" for row in rows)
    return (
        '<?xml version="1.0" encoding="utf-8"?><DataTable xmlns="http://MRCS/">'
        '<diffgr:diffgram xmlns:diffgr="urn:schemas-microsoft-com:xml-diffgram-v1">'
        f"<DocumentElement xmlns=\"\">{body}</DocumentElement></diffgr:diffgram></DataTable>"
    ).encode("utf-8")


class TestBrazilPublicService(unittest.TestCase):
    """Without credentials the fetcher uses ANA's public HidroSerieHistorica service."""

    def setUp(self):
        with patch("rivretrieve.brazil.USERNAME", None), patch("rivretrieve.brazil.PASSWORD", None):
            self.fetcher = BrazilFetcher()

    @patch("rivretrieve.utils.requests_retry_session")
    def test_stage_prefers_consisted_daily_means(self, mock_session):
        def month(time: str, level: int, mean: int, day1: str, day15: str) -> str:
            return (
                f"<DataHora>2020-12-01 {time}</DataHora><NivelConsistencia>{level}</NivelConsistencia>"
                f"<MediaDiaria>{mean}</MediaDiaria><Cota01>{day1}</Cota01><Cota15>{day15}</Cota15>"
            )

        reply = _public_series(
            month("00:00:00", level=1, mean=1, day1="747", day15="691.5"),
            month("00:00:00", level=2, mean=1, day1="747", day15="692"),
            # A single 07:00 reading, not a daily mean.
            month("07:00:00", level=1, mean=0, day1="750", day15="694"),
        )
        response = MagicMock(content=reply)
        mock_session.return_value.get.return_value = response

        result = self.fetcher.get_data("14515000", constants.STAGE_DAILY_MEAN, "2020-12-01", "2020-12-31")

        expected = pd.DataFrame(
            {
                constants.TIME_INDEX: pd.to_datetime(["2020-12-01", "2020-12-15"]),
                constants.STAGE_DAILY_MEAN: [7.47, 6.92],
            }
        ).set_index(constants.TIME_INDEX)
        assert_frame_equal(result, expected, check_index_type=False)
        params = mock_session.return_value.get.call_args.kwargs["params"]
        self.assertEqual(params["tipoDados"], "1")
        self.assertEqual(params["dataInicio"], "01/12/2020")

    @patch("rivretrieve.utils.requests_retry_session")
    def test_discharge_skips_invalid_days(self, mock_session):
        reply = _public_series(
            "<DataHora>2021-02-01 00:00:00</DataHora><NivelConsistencia>2</NivelConsistencia>"
            "<MediaDiaria>1</MediaDiaria><Vazao01>1373.89</Vazao01><Vazao28>12.5</Vazao28>"
            "<Vazao30>99</Vazao30><Vazao31></Vazao31>"
        )
        mock_session.return_value.get.return_value = MagicMock(content=reply)

        result = self.fetcher.get_data("14515000", constants.DISCHARGE_DAILY_MEAN, "2021-02-01", "2021-02-28")

        self.assertEqual(list(result.index.strftime("%Y-%m-%d")), ["2021-02-01", "2021-02-28"])
        self.assertEqual(list(result[constants.DISCHARGE_DAILY_MEAN]), [1373.89, 12.5])


if __name__ == "__main__":
    unittest.main()
