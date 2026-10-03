"""RivRetrieve: A Python package for retrieving global river gauge data."""

from .australia import AustraliaFetcher
from .base import RiverDataFetcher
from .brazil import BrazilFetcher
from .canada import CanadaFetcher
from .chile import ChileFetcher
from .czech import CzechFetcher
from .france import FranceFetcher
from .germany_berlin import GermanyBerlinFetcher
from .ioc_sealevel import IOCSeaLevelFetcher
from .japan import JapanFetcher
from .lithuania import LithuaniaFetcher
from .mrc import MRCFetcher
from .noaa_tides import NOAATidesFetcher
from .norway import NorwayFetcher
from .pagasa import PagasaDamFetcher, PagasaStationFetcher
from .philippines import PhilippinesFetcher
from .poland import PolandFetcher
from .portugal import PortugalFetcher
from .singapore import SingaporeRainFetcher
from .slovenia import SloveniaFetcher
from .southafrica import SouthAfricaFetcher
from .spain import SpainFetcher
from .thailand import ThailandFetcher, ThailandRainFetcher
from .uhslc import UHSLCFetcher
from .uk_ea import UKEAFetcher
from .uk_nrfa import UKNRFAFetcher
from .usa import USAFetcher

__version__ = "0.1.0"

