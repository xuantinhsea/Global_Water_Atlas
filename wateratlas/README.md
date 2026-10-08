# Global Water Atlas

A local-first map of observed water data from around the world, built on the
RivRetrieve library. It puts every station from all 29 providers on a Leaflet
basemap: river gauges, rain gauges, reservoirs, and coastal and Great Lakes tide
gauges worldwide. You can filter and select stations, preview a series, and download
discharge, water level, water temperature and rainfall as CSV.

`wateratlas` is a *consumer* of `rivretrieve`. It never modifies the library:
every station list and every time series still comes from the fetchers in
`rivretrieve/`.

## Quick start

```bash
# 1. Install the library plus the app's extra dependencies.
pip install -e ".[app]"

# 2. Build the station catalog from the cached provider CSVs (once, ~15 s).
python scripts/build_catalog.py

# 3. Build the front end.
cd wateratlas/frontend && npm install && npm run build && cd ../..

# 4. Run it.
wateratlas
```

That opens <http://127.0.0.1:8000>. `wateratlas doctor` reports anything missing.
`python -m wateratlas` does the same without the installed command.

For front-end development, run the API and the Vite dev server side by side:

```bash
wateratlas serve --no-open              # API on :8000
cd wateratlas/frontend && npm run dev   # UI on :5173, proxies /api to :8000
```

## What's on the map

**98,288 stations from 29 providers**, of which 98,096 are mappable.

| Data | Examples |
| --- | --- |
| River discharge | USGS, ECCC HYDAT, Environment Agency, BoM, Hub'Eau, ANA, … |
| Water level | River stage almost everywhere; reservoir levels (PAGASA dams); coastal, estuarine and Great Lakes levels (NOAA Tides & Currents); sea level at tide gauges worldwide (IOC, UHSLC) |
| Water temperature | NOAA, NVE, CHMI, IMGW, Wasserportal Berlin |
| Rainfall | ThaiWater, MRC, PhilSensors, PAGASA, NEA Singapore; catchment averages from NRFA |

Units are SI throughout: discharge in m³/s, water level in m, water temperature
in °C, rainfall in mm. River stage is a height above each gauge's own zero;
coastal and Great Lakes levels are heights above a tidal or lake datum (see
[NOAA Tides & Currents](#noaa-tides--currents)). Don't compare levels across
stations without checking the datum.

## What the catalog build does

The cached CSVs in `rivretrieve/cached_site_data/` mostly predate the column
standard in `docs/design_docs/data_fetcher.md` §5, so they disagree with each
other in ways that would keep many stations off a map. `scripts/build_catalog.py`
reconciles them into one index and reports every row it repaired or dropped:

| Problem | What the build does |
| --- | --- |
| Spain has no `latitude`/`longitude` at all — only UTM 30N | Reprojects EPSG:25830 → EPSG:4326 (1,491 stations) |
| `station_name` vs `gauge_name`, `altitude` vs `gauge_altitude` | Per-provider alias map |
| `germany_berlin` leads with an unnamed index column | Dropped |
| Australia repeats 125 `gauge_id` values | First row wins, count reported |
| `gauge_id` is only unique per provider | Every record keyed `<provider>:<gauge_id>` |
| Portugal writes `-` for a missing coordinate | Treated as null, station kept but flagged off-map |
| `uk_ea_sites.csv` embeds 15 MB of JSON blobs | Excluded from the shipped metadata |

Current result: **98,288 stations, 98,096 mappable, 192 without coordinates.**
48,399 still have no station name, because several providers' cached CSVs carry
only `gauge_id, latitude, longitude`. Those show their gauge ID on the map.

## Southeast Asia

Nine dedicated providers cover the region: 8,317 stations, all geolocated, including 180
tide gauge entries from the two worldwide networks (many gauges appear in both). The **SE Asia** button on the map toolbar frames the region, and
<https://global-water-atlas.vercel.app/#sea> opens the atlas there, which is the
link to share. Refresh the station lists with:

```bash
python scripts/refresh_sea_sites.py              # the national networks
python scripts/refresh_sea_sites.py mrc          # or just one
python scripts/refresh_sealevel_sites.py         # the two worldwide tide gauge networks
```

| Provider | Stations in the region | Data | History |
| --- | --- | --- | --- |
| `thailand` | 1,121 | stage, discharge | hourly, from ~2020 |
| `thailand_rain` | 4,485 | rainfall | **none**: rolling ~41 h |
| `mrc` | 79 | stage, rainfall | **none**: rolling ~31 days |
| `philippines` | 2,132 | stage, rainfall | needs a DOST-ASTI token |
| `pagasa_dams` | 9 | reservoir level, outflow | **none**: today's and yesterday's bulletin |
| `pagasa_stations` | 199 | inventory only | readings are sold, not published |
| `singapore_rain` | 112 | rainfall, 5-minute | from Dec 2016; last 92 days per request |
| `ioc_sealevel` | 101 of 1,390 | sea level, about 1-minute, raw | real time; last 92 days per request |
| `uhslc` | 79 of 598 | sea level, hourly and daily, checked | decades; to one or two months ago |

The tide gauges run from Sittwe and Moulmein in Myanmar through Ko Lak, Ko
Taphao Noi and Ko Miang in Thailand, Langkawi, Penang and Kudat in Malaysia,
Tanjong Pagar in Singapore and Vung Tau and Qui Nhon in Viet Nam, to Manila,
Legaspi, Davao and Subic Bay in the Philippines and Sabang, Sibolga, Padang,
Benoa, Bitung and Ambon in Indonesia.

### Networks without open access

The survey for this region also checked these, and none can be added yet:

| Network | What blocks it |
| --- | --- |
| Malaysia, JPS Public InfoBanjir | Rainfall tables are public but carry no station coordinates and only the last six days; the water level tables come back empty. |
| Viet Nam, VNDMS (vndms.gov.vn) | No documented data service, and none found in the monitoring map's scripts. |
| Indonesia, PUPR SIHKA | Answers 403 to automated requests. |
| Jakarta, Dinas SDA flood gauges | Server-rendered pages only, with no data service. |
| Indonesia, BIG tide gauges | No data service of its own, but most of its gauges reach the IOC facility and are mapped through `ioc_sealevel`. |

### Mekong River Commission

79 telemetry stations across Laos (23), Cambodia (21), Viet Nam (17), Thailand
(16) and China (2), including Tan Chau and My Thuan in the Mekong Delta. This is
the only openly reachable gauge data for Laos, Cambodia and Viet Nam.

The feed is a rolling window of roughly the last 31 days at 5–15 minute
resolution; every date parameter the API accepts is ignored. **Redistribution
terms for this feed are not published.** It is unauthenticated and serves the
MRC's public flood-warning map, but the MRC's general data policy requires a
licence and fees for raw data. Confirm with the MRC Secretariat before
republishing anything from it, and use a formal data request at
<https://portal.mrcmekong.org/> for the historical archive.

Note that all 79 stations carry `country = "mrc"` in the catalog, because the
atlas keys country by provider. Each station's real country is in its provider
record, visible in the detail panel.

### Philippines

2,132 DOST-ASTI PhilSensors stations — 561 water level, 1,131 rain gauges, 231
tandem (both) and 209 weather stations. The station catalogue comes from an open
GeoServer WFS, which is why every station maps without credentials.

The readings API needs an access token that DOST-ASTI issues through a data
request: non-commercial research, academic or disaster-management use only, no
redistribution. Set `PHILSENSORS_TOKEN` in `rivretrieve/.env` once you have one.
Without it `get_data` returns an empty frame and says why.

### Thailand

Two things to know before relying on the ThaiWater providers:

- **The water level archive starts around 2020** and only becomes dense from
  2023. Earlier requests return a grid of nulls, which the fetcher discards, so
  a DataFrame is routinely much shorter than the range asked for.
- **There is no rainfall history.** The provider's rainfall endpoint accepts
  date parameters and ignores them, always returning a rolling window of about
  the last 41 hours. `ThailandRainFetcher` returns an empty frame for anything
  older rather than handing back recent data under the wrong dates.

## Sea level at tide gauges worldwide

Two open networks add tide gauges on every coast, and they complement each other:

| | `ioc_sealevel` | `uhslc` |
| --- | --- | --- |
| Operator | IOC Sea Level Station Monitoring Facility (VLIZ for UNESCO-IOC) | University of Hawaii Sea Level Center |
| Stations | 1,390 | 598 |
| Variables | `stage_instantaneous`, `stage_hourly_mean`, `stage_daily_mean` | `stage_hourly_mean`, `stage_daily_mean` |
| Checked? | No: raw transmissions, spikes and all | Yes: research quality, then fast delivery |
| Latest data | minutes ago | one to two months ago |
| History per request | the last 92 days of the range | the whole range, back to the 1800s at some stations |
| Heights relative to | each sensor's own zero | each station's UHSLC zero |

Use IOC for an event that is happening or just happened, and UHSLC for anything
longer or anything you will publish. Neither network's heights are tied to a
common datum, so compare a station only with itself.

Things to know:

- **IOC stations often carry several sensors.** The fetcher uses one per
  request, radar first, and says which in `frame.attrs["sensor"]`. Battery and
  switch channels are dropped, and so are DART tsunameters, which measure the
  deep-ocean water column rather than a coastal level. The handful of stations
  that report in feet are converted to metres.
- **IOC data is fetched in 30-day windows one second apart**, because the
  service truncates longer requests and is a shared monitoring facility.
- **UHSLC's latest weeks are not there yet.** Its fast delivery product runs to
  one or two months ago; research quality data to the end of the year before
  last. A series uses research quality wherever it exists, and
  `frame.attrs["sources"]` says which products contributed.
- **Cite them.** IOC asks for "Flanders Marine Institute (VLIZ); Intergovernmental
  Oceanographic Commission (IOC): Sea level station monitoring facility"; UHSLC
  for Caldwell, Merrifield and Thompson (2015), NOAA NCEI.

## NOAA Tides & Currents

`noaa_tides` adds the 2,932 NOAA CO-OPS stations behind
<https://tidesandcurrents.noaa.gov/map/>: 309 active tide and Great Lakes
gauges plus 2,623 historic ones, including tidal river stations such as
Carrollton on the Mississippi at New Orleans. Refresh the list with:

```bash
python scripts/refresh_noaa_tides_sites.py
```

| Variable | Source | Notes |
| --- | --- | --- |
| `stage_instantaneous` | 6-minute water level | from ~1995; preliminary for the latest month or so |
| `stage_daily_mean` | mean of 24 verified hourly heights | complete UTC days only |
| `stage_monthly_mean` | NOAA monthly mean sea level (MSL) | one request for the whole record |
| `water-temperature_instantaneous` | 6-minute water temperature | 240 stations |

Things to know:

- **Stage is a height above a datum, and the datum depends on the station.**
  Tide gauges use MLLW, Great Lakes gauges IGLD 1985, and the dozen non-tidal
  coastal and river stations their station datum (STND). The station's
  `default_datum` is in the detail panel. A station without MLLW, such as
  Augusta on the Kennebec River, falls back to STND with a warning. The
  downloaded CSV does not record the datum; the library returns it in
  `frame.attrs["datum"]`.
- **Historic stations are mapped too.** Most are short 1970s deployments, so a
  default ten-year range returns nothing for them. The detail panel shows
  `status`, `established` and `removed`, and requests past a station's removal
  date are never sent.
- **NOAA rate-limits bursts.** It answers with HTTP 403 for several minutes, so
  the fetcher paces every request 0.5 s apart and reports a 403 as a failure,
  not as an empty series.

## Catalog artefacts

The build writes these to `wateratlas/catalog_data/` (gitignored):

- `stations.parquet`: normalised core columns, what the API queries
- `stations_extra.parquet`: every provider-specific column as JSON, read only for the detail panel
- `stations.json`: compact positional-array payload for the map (~5.4 MB, ~1.6 MB gzipped)
- `catalog_report.json`: rows in, rows out, and why each row was dropped

## Credentials

Two providers need secrets, set in `rivretrieve/.env`:

```
NVE_API_KEY=...         # Norway, NVE HydAPI
PHILSENSORS_TOKEN=...   # Philippines, DOST-ASTI PhilSensors
```

Without them those providers are marked unavailable in the UI and their jobs
report `blocked` rather than silently returning an empty series. Brazil needs
none: without `ANA_USERNAME` / `ANA_PASSWORD` it reads ANA's public
HidroSerieHistorica service, and with them the Hidroweb API v2.

GRDC stations download from the national service that runs them (USGS, ECCC,
BoM and so on) where RivRetrieve supports it: 5,516 of 11,910. GRDC releases its
own copies only through its Data Portal, so the others say that, with a link,
instead of downloading.

## Bulk caches

Canada downloads the entire HYDAT SQLite database and Poland builds a local Zarr
store, both lazily inside `get_data()`. Warm them up front rather than behind a
user's first click:

```bash
wateratlas warm                      # both
wateratlas warm --provider canada    # just one
```

## How the map handles 98,000 points

Leaflet markers are one DOM node each, and Leaflet.markercluster struggles well
below this count. Instead:

- `src/cluster.worker.ts` owns the whole station index and runs
  [supercluster](https://github.com/mapbox/supercluster) off the main thread. It
  also owns filtering, so panning and zooming never round-trip to the server.
- `src/lib/canvasLayer.ts` paints clusters and stations into a single canvas
  with its own hit testing — one DOM node in total.

Supercluster measures its radius in tile units with `extent` per tile (512 by
default) while Leaflet paints 256 px tiles, so the radius here is 120 for the
~60 px grouping the map wants.

## API

Everything the UI does is available directly.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/providers` | Every provider, with variables, credential and cache state |
| `GET /api/stations` | Filtered stations as GeoJSON (`format=table` for flat rows) |
| `GET /api/stations/map` | The compact map payload |
| `GET /api/station?key=` | Full metadata, including every provider-specific column |
| `GET /api/station/preview?key=&variable=` | Downsampled series for the detail panel |
| `POST /api/selection/variables` | Variables worth offering for a selection |
| `POST /api/selection/estimate` | Pre-flight: what will download, and roughly how long |
| `POST /api/downloads` | Start a job; returns immediately |
| `GET /api/downloads/{id}` | Per-station status |
| `GET /api/downloads/{id}/events` | Server-sent progress events |
| `GET /api/downloads/{id}/archive` | ZIP: CSVs + `manifest.csv` + `ATTRIBUTION.md` |
| `POST /api/providers/{country}/warm` | Trigger a bulk cache download as a job |

Station keys are query parameters, not path segments, because Portuguese gauge
IDs contain slashes (`portugal:04K/04A`).

## Learned availability

`get_available_variables()` is static per fetcher, so for most providers the
catalog can only say a station *may* publish stage. Five providers know better
and carry a boolean per variable per station in their cached CSV: Norway,
GRDC, PhilSensors, the PAGASA station inventory, and NOAA Tides & Currents.

Every download and preview therefore records what it found in
`wateratlas/state/availability.sqlite3`: `confirmed` (rows came back), `absent`
(nothing for that range) or `failed`. A confirmed observation is never
downgraded by a later empty range — a gauge with a 1990s record still has it
when a query for 2024 comes back empty. The detail panel shows these as chips,
and the atlas gets more accurate the more it is used.

Two environment variables move the app's files elsewhere:
`WATERATLAS_CATALOG_DIR` for the catalog and `WATERATLAS_STATE_DIR` for this
state.

## Hosting on Vercel

Live at **<https://global-water-atlas.vercel.app>**. The atlas runs there as a
single Vercel Function, and every push to `main` redeploys it.

- `pyproject.toml` points Vercel at `wateratlas.main:app` and runs
  `scripts/vercel_build.py`, which builds the front end and the station catalog
  (neither is in git).
- The `[project]` dependencies in `pyproject.toml` are exactly what the function
  installs; development and docs tools are the `dev` extra
  (`pip install -e ".[dev]"`). `[tool.uv] package = false` stops Vercel
  installing a copy of the project that would shadow the built catalog.
- `vercel.json` allows 5 minutes per request (the Hobby maximum) and keeps
  tests, docs and `node_modules` out of the bundle.

Run `python scripts/vercel_build.py` locally to check that a deployment will build.

A hosted function has no long-lived process and only `/tmp` to write to, so
a few things work differently there (the app detects Vercel via `VERCEL=1`):

- **Downloads run in your browser.** Instead of a server job, the page fetches
  each station's CSV from `GET /api/station/data`, one station at a time per
  provider, and builds the same ZIP (CSVs, `manifest.csv`, `ATTRIBUTION.md`)
  itself. Keep the tab open until it finishes.
- **Canada (HYDAT) and Poland (IMGW) read one station at a time.** Locally their
  fetchers build a multi-GB archive once, which a function cannot store, so the
  hosted atlas uses `CanadaFetcher(source="api")` (ECCC's GeoMet OGC API, same
  HYDAT daily means) and `PolandFetcher(source="direct")` (only the IMGW
  archives covering the range, about 2 MB a year, kept in `/tmp`).
- **The station catalog is cached at the edge.** `/api/providers` reports a
  `catalog_version` hash; the page asks for `/api/stations/map?v=<hash>`, which
  browsers and Vercel's CDN keep for a year because a rebuilt catalog gets a new
  hash. Vite's hashed `/assets/*` files are cached the same way.
- **One station must finish within 5 minutes.** Long ranges of 6-minute data or
  slow scraped providers can exceed that; the manifest then says so, and a
  shorter date range fixes it.

Learned availability and preview caches live in `/tmp` and reset whenever
Vercel starts a new instance.

## Data rights

All data rights remain with the original providers. The MIT licence of this
repository covers the code only. Every download archive ships an
`ATTRIBUTION.md` naming each provider that contributed to it, and
`manifest.csv` records the outcome for every station requested, including the
ones that returned nothing.

Before deploying this anywhere shared, read each provider's terms: several
throttle by IP and some restrict redistribution, and a hosted instance
concentrates all traffic on one address.

## Author and contact

© 2026 Nguyen Xuan Tinh (Ph.D.)
Nippon Koei Co., Ltd., Water Resources & Energy Department
〒102-8539 5-4 Kojimachi, Chiyoda-ku, Tokyo, Japan
E-mail: <xuantinhsea@gmail.com>

The same details are in the app: click the ⓘ next to the title, or open
<http://127.0.0.1:8000/#about>.
