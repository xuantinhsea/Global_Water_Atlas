# RivRetrieve (Python)

A Python package for unified access to observed water data worldwide: river
discharge and water level, rainfall, reservoir levels, water temperature, and
coastal and Great Lakes water levels. It comes with the **Global Water Atlas**,
a local map app for browsing and downloading all of it.

> [!NOTE]
> RivRetrieve-Python is currently under active development and has not yet reached a stable version. APIs and functionality are subject to change.

Welcome to RivRetrieve-Python! We're actively developing this library to make global water data more accessible. As an early-stage project, it's not yet stable, and we appreciate your understanding as things evolve. Contributions and feedback are welcome! Please check out the active development under [Issues](https://github.com/kratzert/RivRetrieve-Python/issues) or start posting under [Discussions](https://github.com/kratzert/RivRetrieve-Python/discussions).

- Documentation: [rivretrieve-python.readthedocs.io](https://rivretrieve-python.readthedocs.io/en/latest/) (Work in Progress)

## Data coverage

29 providers and about 98,000 stations. Every fetcher returns the same shape (a
DataFrame indexed by `time`, one column named after the variable) in SI units:
discharge in m³/s, water level in m, water temperature in °C and rainfall in mm.

| Provider | Fetcher | Coverage | Stations | Data |
| --- | --- | --- | ---: | --- |
| BoM Water Data Online | `AustraliaFetcher` | Australia | 6,646 | discharge, water level |
| ANA Hidroweb ¹ | `BrazilFetcher` | Brazil | 4,610 | discharge, water level |
| ECCC HYDAT | `CanadaFetcher` | Canada | 7,954 | discharge, water level |
| CR2 Explorador | `ChileFetcher` | Chile | 548 | discharge |
| CHMI Open Data | `CzechFetcher` | Czechia | 826 | discharge, water level, water temperature |
| Hub'Eau | `FranceFetcher` | France | 5,678 | discharge, water level |
| GRDC Data Portal | `GRDCFetcher` | Worldwide river discharge stations | 11,910 | station catalogue only (daily/monthly availability flags) |
| Wasserportal Berlin | `GermanyBerlinFetcher` | Germany (Berlin) | 189 | discharge, water level, water temperature |
| IOC Sea Level Monitoring | `IOCSeaLevelFetcher` | Worldwide coastal tide gauges (101 in SE Asia) | 1,390 | sea level, raw, real time |
| MLIT Water Information System | `JapanFetcher` | Japan | 1,029 | discharge, water level |
| Meteo.lt | `LithuaniaFetcher` | Lithuania | 97 | discharge, water level |
| Mekong River Commission | `MRCFetcher` | Mekong basin (LA, KH, VN, TH, CN) | 79 | water level, rainfall |
| NOAA Tides & Currents | `NOAATidesFetcher` | US coasts, estuaries and Great Lakes | 2,932 | coastal / lake water level, water temperature |
| NVE HydAPI ¹ | `NorwayFetcher` | Norway | 4,876 | discharge, water level, water temperature |
| PAGASA dam bulletin | `PagasaDamFetcher` | Philippines | 9 | reservoir level, outflow |
| PAGASA station inventory | `PagasaStationFetcher` | Philippines | 199 | station list only |
| DOST-ASTI PhilSensors ¹ | `PhilippinesFetcher` | Philippines | 2,132 | water level, rainfall |
| IMGW Public Data | `PolandFetcher` | Poland | 1,301 | discharge, water level, water temperature |
| SNIRH | `PortugalFetcher` | Portugal | 721 | discharge, water level |
| NEA rain gauges (data.gov.sg) | `SingaporeRainFetcher` | Singapore | 112 | rainfall |
| ARSO | `SloveniaFetcher` | Slovenia | 739 | discharge, water level |
| DWS Hydrology Services | `SouthAfricaFetcher` | South Africa | 1,294 | discharge, water level |
| MITECO Anuario de Aforos | `SpainFetcher` | Spain | 1,491 | discharge |
| ThaiWater (HII) | `ThailandFetcher` | Thailand | 1,121 | discharge, water level |
| ThaiWater rain gauges (HII) | `ThailandRainFetcher` | Thailand | 4,485 | rainfall |
| UHSLC | `UHSLCFetcher` | Worldwide coastal tide gauges (79 in SE Asia) | 598 | sea level, quality-controlled |
| Environment Agency Hydrology | `UKEAFetcher` | England | 9,194 | discharge, water level |
| NRFA | `UKNRFAFetcher` | United Kingdom | 1,601 | discharge, catchment rainfall |
| USGS NWIS | `USAFetcher` | United States | 24,527 | discharge, water level |

¹ Needs credentials in `rivretrieve/.env`: `ANA_USERNAME` and `ANA_PASSWORD`
(Brazil), `NVE_API_KEY` (Norway), `PHILSENSORS_TOKEN` (Philippines).

Things worth knowing before comparing stations:

- **Water level means different things at different stations.** River stage is
  a height above each gauge's own zero. NOAA's coastal and Great Lakes levels
  are heights above a tidal or lake datum (MLLW, IGLD 1985 or the station
  datum), reported in `frame.attrs["datum"]`. IOC sea levels are raw heights
  above each sensor's own zero, and UHSLC's are above each station's UHSLC zero.
- **Some providers have no history.** The MRC, ThaiWater rain gauges and the
  PAGASA dam bulletin only publish a rolling recent window; their fetchers
  return nothing for older dates rather than recent data under the wrong ones.
- **One provider is metadata-only for now.** `GRDCFetcher` maps the global GRDC
  station catalogue and per-station daily/monthly availability flags; GRDC's
  automated station downloads are not yet wired into RivRetrieve.
- **Two providers fetch only recent data per call.** IOC sea level and Singapore
  rainfall cover at most the last 92 days of a requested range, because their
  services hand out raw five-minute or one-minute data in bulk. For long tide
  gauge records use UHSLC.

Each fetcher's docstring (and the [API docs](https://rivretrieve-python.readthedocs.io/en/latest/))
lists its variables, time steps and caveats.

## Background

This package originated as a Python translation of the [RivRetrieve R package](https://github.com/Ryan-Riggs/RivRetrieve). The initial translation was performed by Gemini, with a few manual adjustments for API changes.

Since then, the package has evolved significantly and is under heavy development. See [Issues](https://github.com/kratzert/RivRetrieve-Python/issues) for more details.

Initially, I ([@kratzert](https://github.com/kratzert)) used this project to experiment with the Gemini-CLI. I was surprised by its effectiveness for this purpose. So far, all code, tests, and everything you see is the output of the Gemini-CLI, including this README.md. Using the Gemini-CLI allows for rapid iteration and integration of new fetchers. I may write a blog post later with more details on my custom `Gemini.md` instruction file. For now, I am focusing on prompting it to resolve all open [Issues](https://github.com/kratzert/RivRetrieve-Python/issues).

## Disclaimer

The purpose of this package is to simplify access to water data. All data rights remain with the original providers. Users are responsible for reviewing and adhering to the license terms of each data provider, which can be found on their respective homepages. The MIT license in the LICENSE file applies only to the code of this package, not to any data downloaded through it.

## Installation

1. Clone or download the repository to your computer.

2. Set up your environment, for example, using the following command from within the RivRetrieve-Python directory:

    ```bash
    # Creates a virtual Python environment within the directory.
    python3 -m venv .venv
    ```

3. Activate the virtual environment:

    ```bash
    source .venv/bin/activate
    ```

4. Install the package and all requirements:

    ```bash
    # The -e makes the installed version editable, in case you want to change some code.
    pip install -e .
    ```

5. Test the installation:

    ```bash
    # Downloads data for one gauge from the US and saves a plot with the discharge data.
    python examples/test_usa_fetcher.py
    ```

## Example Usage

```python
from rivretrieve import NOAATidesFetcher, USAFetcher, constants

# Every fetcher ships a cached station list, indexed by gauge_id.
sites = USAFetcher.get_cached_metadata()
print(sites.head())

# And declares the variables it can return.
print(USAFetcher.get_available_variables())

# River discharge: the Mississippi River at Baton Rouge (USGS 07374000), in m³/s.
discharge = USAFetcher().get_data(
    gauge_id="07374000",
    variable=constants.DISCHARGE_DAILY_MEAN,
    start_date="2023-01-01",
    end_date="2023-01-31",
)
print(discharge.head())

# Coastal water level: The Battery, New York Harbor (NOAA 8518750), 6-minute values in m.
water_level = NOAATidesFetcher().get_data(
    gauge_id="8518750",
    variable=constants.STAGE_INSTANT,
    start_date="2023-01-01",
    end_date="2023-01-02",
)
print(water_level.attrs["datum"])  # MLLW: metres above Mean Lower Low Water
print(water_level.head())
```

## Global Water Atlas (map app)

`wateratlas/` is a web app built on this library. It shows every station from
all 29 providers on one map (rivers, rain gauges, reservoirs and coastal tide
gauges), with filtering, box and lasso selection, series previews, and batch
CSV download.

**Live: <https://global-water-atlas.vercel.app>** (hosted on Vercel; Canada and
Poland are map-only there, see [wateratlas/README.md](wateratlas/README.md#hosting-on-vercel)).
To run it locally:

```bash
pip install -e ".[app]"
python scripts/build_catalog.py
cd wateratlas/frontend && npm install && npm run build && cd ../..
wateratlas
```

That opens <http://127.0.0.1:8000>. See [wateratlas/README.md](wateratlas/README.md)
for the full guide, including how the catalog is built from the cached site
CSVs, per-provider caveats, and how to set credentials.

The Global Water Atlas is developed by **Nguyen Xuan Tinh (Ph.D.)**, Water
Resources & Energy Department, Nippon Koei Co., Ltd., 〒102-8539 5-4 Kojimachi,
Chiyoda-ku, Tokyo, Japan. Contact: <xuantinhsea@gmail.com>.

## Community Contributions

Community-maintained packages that extend RivRetrieve:

### [watershed-retrieve](https://github.com/CooperBigFoot/watershed-retrieve)

Pre-delineated [MERIT-Hydro](https://www.reachhydro.org/home/params/merit-hydro) watershed boundaries and river networks for ~60,000 RivRetrieve gauging stations across 16 countries. Data is served as GeoParquet from a public CDN and cached locally — no configuration required.

- **Author**: [Nicolas Lazaro](https://github.com/CooperBigFoot)
- **Install**: `pip install watershed-retrieve`
- **Links**: [PyPI](https://pypi.org/project/watershed-retrieve/) · [GitHub](https://github.com/CooperBigFoot/watershed-retrieve)

```python
import watershed_retrieve as wr

# Get the watershed boundary for a Portuguese gauging station
watershed = wr.get_watershed("portugal", "04K/04A")

# Watershed + river network
watershed, rivers = wr.get_watershed_with_rivers("portugal", "04K/04A")
```

See [Issue #87](https://github.com/kratzert/RivRetrieve-Python/issues/87) for the original proposal.
