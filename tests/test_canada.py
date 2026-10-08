import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
from pandas.testing import assert_frame_equal

from rivretrieve import CanadaFetcher, constants


class TestCanadaFetcher(unittest.TestCase):
    def setUp(self):
        self.fetcher = CanadaFetcher()
        self.test_db_path = Path(os.path.dirname(__file__)) / "test_data" / "test_hydat.sqlite3"

    @patch("rivretrieve.utils.requests_retry_session")
    @patch("rivretrieve.canada.CanadaFetcher._download_hydat")
    @patch(
        "rivretrieve.canada.CanadaFetcher.HYDAT_PATH",
        new_callable=lambda: Path(os.path.join(os.path.dirname(__file__), "test_data", "test_hydat.sqlite3")),
    )
    def test_get_data_discharge(self, mock_hydat_path, mock_download, mock_requests):
        mock_download.return_value = True  # Prevent download attempt

        gauge_id = "08GA031"
        variable = constants.DISCHARGE_DAILY_MEAN
        start_date = "2010-01-01"
        end_date = "2010-01-05"

        result_df = self.fetcher.get_data(gauge_id, variable, start_date, end_date)

        expected_data = {
            constants.TIME_INDEX: pd.to_datetime(
                ["2010-01-01", "2010-01-02", "2010-01-03", "2010-01-04", "2010-01-05"]
            ),
            constants.DISCHARGE_DAILY_MEAN: [1.1, 1.2, 1.3, 1.4, 1.5],
        }
        expected_df = pd.DataFrame(expected_data).set_index(constants.TIME_INDEX)

        assert_frame_equal(result_df, expected_df)

    @patch("rivretrieve.utils.requests_retry_session")
    @patch("rivretrieve.canada.CanadaFetcher._download_hydat")
    @patch(
        "rivretrieve.canada.CanadaFetcher.HYDAT_PATH",
        new_callable=lambda: Path(os.path.join(os.path.dirname(__file__), "test_data", "test_hydat.sqlite3")),
    )
    def test_get_data_stage(self, mock_hydat_path, mock_download, mock_requests):
        mock_download.return_value = True  # Prevent download attempt

        gauge_id = "08GA031"
        variable = constants.STAGE_DAILY_MEAN
        start_date = "2010-01-01"
        end_date = "2010-01-05"

        result_df = self.fetcher.get_data(gauge_id, variable, start_date, end_date)

        expected_data = {
            constants.TIME_INDEX: pd.to_datetime(
                ["2010-01-01", "2010-01-02", "2010-01-03", "2010-01-04", "2010-01-05"]
            ),
            constants.STAGE_DAILY_MEAN: [10.1, 10.2, 10.3, 10.4, 10.5],
        }
        expected_df = pd.DataFrame(expected_data).set_index(constants.TIME_INDEX)

        assert_frame_equal(result_df, expected_df)


class TestCanadaApiSource(unittest.TestCase):
    """``source="api"`` reads GeoMet's OGC API instead of a local HYDAT copy."""

    @staticmethod
    def _page(rows):
        response = MagicMock()
        response.json.return_value = {
            "features": [{"properties": {"DATE": date, "LEVEL": value}} for date, value in rows]
        }
        return response

    @patch("rivretrieve.utils.requests_retry_session")
    def test_pages_until_a_short_page(self, mock_session):
        get = mock_session.return_value.get
        get.side_effect = [
            self._page([("2020-01-01", 1.0), ("2020-01-02", None)]),
            self._page([("2020-01-03", 1.2)]),
        ]
        fetcher = CanadaFetcher(source="api")

        with (
            patch.object(CanadaFetcher, "GEOMET_PAGE", 2),
            patch.object(CanadaFetcher, "_get_hydat_connection") as hydat,
        ):
            result = fetcher.get_data("01AD003", constants.STAGE_DAILY_MEAN, "2020-01-01", "2020-01-03")

        hydat.assert_not_called()
        expected = pd.DataFrame(
            {constants.TIME_INDEX: pd.to_datetime(["2020-01-01", "2020-01-03"]), constants.STAGE_DAILY_MEAN: [1.0, 1.2]}
        ).set_index(constants.TIME_INDEX)
        assert_frame_equal(result, expected, check_index_type=False)
        offsets = [call.kwargs["params"]["offset"] for call in get.call_args_list]
        self.assertEqual(offsets, [0, 2])
        self.assertEqual(get.call_args_list[0].kwargs["params"]["datetime"], "2020-01-01/2020-01-03")

    def test_rejects_unknown_source(self):
        with self.assertRaises(ValueError):
            CanadaFetcher(source="ftp")


if __name__ == "__main__":
    unittest.main()
