# landlab_data_prep

Builds analysis-ready inputs for Landlab landslide and postfire debris-flow
models. One YAML config in, one common grid out: DEM, burn severity, soil,
landcover and K-factor, plus optional HLS vegetation indices, PRISM and Synoptic
rainfall, NISAR radar and soil moisture, and SMAP soil moisture.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install landlab_data_prep@git+https://github.com/gaia-hazlab/landlab_data_prep
```

Python 3.10 to 3.13. To reproduce the exact tested environment instead:

```bash
conda env create -f environment.lock.yml   # environment.yml off Linux
conda activate landlab_data_prep
pip install --no-deps -e .
```

## Quick start

```bash
landlab-prep-init-config                   # writes config/base.yaml
# edit config/base.yaml for your event
landlab-prep-pipeline --config config/base.yaml --export-final-tifs
```

The config is never tracked by git, and holds one event at a time.

## Commands

| Command | Without installing | Purpose |
|---|---|---|
| `landlab-prep-init-config` | `scripts/init_config.py` | Write a starting config |
| `landlab-prep-pipeline` | `scripts/run_pipeline.py` | All layers, then Landlab fields |
| `landlab-prep-soil` | `scripts/soil_run.py` | Fetch and align soil layers |
| `landlab-prep-soil-fetch` | `scripts/soil_fetch.py` | Fetch and crop soil only |
| `landlab-prep-soil-align` | `scripts/soil_harmonize.py` | Align fetched soil layers |
| `landlab-prep-hls` | `scripts/remote_sensing_run.py` | HLS vegetation indices |
| `landlab-prep-prism` | `scripts/prism_run.py` | PRISM daily rain and temperature |
| `landlab-prep-synoptic` | `scripts/synoptic_run.py` | Synoptic station rainfall |
| `landlab-prep-nisar` | `scripts/nisar_run.py` | NISAR backscatter and soil moisture |
| `landlab-prep-smap` | `scripts/smap_run.py` | SMAP L4 soil moisture, every 3 hours |
| `landlab-prep-dem-difference` | `scripts/dem_difference.py` | Post minus pre DEM |
| `landlab-prep-export-tifs` | `scripts/export_tifs.py` | ASCII grids to GeoTIFF |

Every command takes `--config`. Add `--help` for its options.

## Config

- One file per event. The template documents every key.
- Bad configs stop the run: unknown keys, wrong types and missing values are
  all reported at once, before any work starts.
- Downloads are cached in `paths.cache_dir` and reused.
- DEM downloads need an OpenTopography key, read from `USGS_TOPO_API_KEY`,
  then `OPENTOPOGRAPHY_API_KEY`, then `dem.api_key`.

## Outputs

- **Grid.** One analysis grid for every layer: the AOI's UTM zone, AOI bounds
  snapped to multiples of `raster.target_res`.
- **Files.** ESRI ASCII for Landlab in `paths.output_dir`; GeoTIFFs too with
  `--export-final-tifs`.
- **Nodata.** `-9999` outside the AOI, except burn severity.
- **Burn severity.** Classes 2 to 4 are kept. Everything else, including
  missing data and cells outside the AOI, becomes 1, unburned.
- **Soil.** Where a soil input is missing, derived layers are `-9999` and the
  Landlab node is closed. Water (landcover 11) is closed too.
- **K-factor.** `kffact` is the STATSGO erodibility factor from USGS. It is
  mapped coarsely, so small areas show few distinct values.
- **Missing-data codes.** A raster source may list `missing_values`, codes
  meaning missing beyond its declared nodata. They become `-9999` and need
  `nearest` or `mode` resampling. STATSGO's -0.1 water code uses this.

## HLS vegetation indices (optional)

Median NBR, NDVI and NDMI for a pre-event and post-event window, and their
change, from NASA HLS v2.0 at 30 m.

1. Fill in the `remote_sensing` block, both windows in the same season.
2. Set `EARTHDATA_TOKEN`, or add `urs.earthdata.nasa.gov` to `~/.netrc`.
3. Run `landlab-prep-hls --config config/base.yaml`.

Written to `<output_dir>/remote_sensing`:

| File | Content |
|---|---|
| `hls_<index>_pre.tif`, `hls_<index>_post.tif` | Median of cloud-free observations |
| `hls_dndvi.tif`, `hls_dndmi.tif` | Pre minus post |
| `hls_dnbr.tif`, `hls_rdnbr.tif` | dNBR and relative dNBR, scaled by 1000 |
| `hls_clear_count_pre.tif`, `hls_clear_count_post.tif` | Cloud-free days per pixel |

Cloud, shadow, snow and high-aerosol pixels are masked. HLS starts in 2013
(Landsat) and late 2015 (Sentinel-2).

## PRISM daily forcing (optional)

Daily rain (mm) and minimum and maximum temperature (°C) at 800 m or 4 km.

1. Fill in the `prism` block: `start`, `end`, `variables`, `resolution`.
2. Run `landlab-prep-prism --config config/base.yaml`.

Written to `<output_dir>/prism_forcing`: a grid per day and variable in
`aligned_tif/` and `asc/`, plus `forcing_daily_prism.csv` of AOI means, in the
layout `landlab_debrisflow` reads. Grids are cached by PRISM release, so
revised data are fetched again and unchanged data are reused.

PRISM covers the conterminous US only. Cite it as PRISM Group, Oregon State
University, https://prism.oregonstate.edu.

## Synoptic station rainfall (optional)

Station precipitation from the [Synoptic API](https://docs.synopticdata.com/services/precipitation),
the gauge network behind [USGS FlowAlert](https://www.usgs.gov/programs/landslide-hazards/flowalert).
It gives rainfall forcing, not FlowAlert's intensities, thresholds or alerts.

1. Fill in the `synoptic` block: `start`, `end`, `interval_hours` (1, 2, 3, 4, 6,
   8, 12 or 24), `buffer_km` and `max_distance_km`.
2. Set `SYNOPTIC_TOKEN`, or put `SYNOPTIC_TOKEN=...` in the project's `.env`
   (git-ignored). Get a public API token under Data Access Credentials at
   <https://customer.synopticdata.com/>; the account needs precipitation and
   historical-data access.
3. Run `landlab-prep-synoptic --config config/base.yaml`.

Dates are inclusive UTC days, up to 366 per run. Stations within `buffer_km` of
the AOI are used. Each cell gets the total of the nearest valid station within
`max_distance_km`, otherwise NoData, never zero; a total is used only when it is
finite, non-negative, from at least one report, and covers exactly the requested
interval. The grid shows nearest-station assignment, not gauge resolution, so
check coverage before using AOI means. Original JSON responses are cached under
`<cache_dir>/synoptic/`, so a cached run needs no token; `--refresh` fetches
them again.

Written to `<output_dir>/synoptic_forcing`: a grid per interval in
`aligned_tif/ppt/` and `asc/ppt/` (mm), `station_index/` and
`station_distance_km/` (the gauge that filled each cell, and how far away),
`forcing_synoptic.csv` (AOI mean, coverage and station count per interval),
`rainfall.csv` (every returned station interval and whether it was used),
`stations.csv`, `stations.gpkg` and `synoptic_manifest.json`.

## NISAR backscatter and soil moisture (optional)

Two NISAR L-band products, for the AOI and a date window:

| Product | Layers | Pixels | Written to |
|---|---|---|---|
| `GCOV` radar backscatter | `HHHH`, `HVHV` (and other terms the mode acquired), linear gamma0 | 10-20 m, UTM | `nisar_gcov/` |
| `SME2` soil moisture (m³/m³) | `combined`, `DSG`, `PMI`, `TSR`, `recommended` | 200 m, EASE-Grid 2.0 | `nisar_sme2/` |

`combined` is NISAR's own estimate, the average of its three algorithms: `DSG`
(disaggregation), `PMI` (physical model inversion) and `TSR` (time-series ratio).
`recommended` is 1 where NISAR recommends the retrieval and 0 where it does not.

1. Fill in the `nisar` block: `start`, `end`, and the `layers` of each product you want.
2. Set `EARTHDATA_TOKEN`, or add `urs.earthdata.nasa.gov` to `~/.netrc`, as for HLS.
3. Run `landlab-prep-nisar --config config/base.yaml`.

Scenes are found in NASA CMR, from October 2025 on. Calibrated (Provisional)
products are used where they exist, pre-calibration (Beta) products otherwise;
the `release` column of each index says which. Each NISAR file is large, so only
the window around the AOI is read, by HTTP range requests, and cached at NISAR's
own pixels and values under `<cache_dir>/nisar/`. Pixels NISAR marks invalid
become NoData before resampling. Backscatter is averaged onto the grid by area;
soil moisture is copied onto it by nearest cell, never smoothed.

Each product folder holds a grid per scene and layer in `aligned_tif/<layer>/`
and `asc/<layer>/`, `nisar_<product>_index.csv` (date, release, track, ascending
or descending pass, and AOI coverage and mean of every layer) and
`nisar_manifest.json`. Compare scenes of the same track: ascending and
descending passes see slopes from opposite sides. A scene without a layer, or
without data over the AOI, stays in the index with a note.

## SMAP L4 soil moisture (optional)

Surface (`sm_surface`, 0-5 cm), root-zone (`sm_rootzone`, 0-100 cm) and profile
(`sm_profile`) soil moisture in m³/m³, every 3 hours on a 9 km grid, from
2015-03-31 on. SMAP L4 (`SPL4SMGP` v008) is a land model that assimilates SMAP
observations, so it has values under snow and frozen ground, where radar
retrievals such as NISAR's do not.

1. Fill in the `smap` block: `start`, `end`, `layers`.
2. Set `EARTHDATA_TOKEN`, or add `urs.earthdata.nasa.gov` to `~/.netrc`, as for HLS.
3. Run `landlab-prep-smap --config config/base.yaml`.

Each file is about 140 MB, but a layer is read as one compressed block of about
4 MB, so a file costs about 11 MB. The window around the AOI is cached at SMAP's
own 9 km cells under `<cache_dir>/smap/`, and copied onto the grid by nearest
cell: a 9 km cell is larger than most AOIs, so expect one or a few values per step.

Written to `<output_dir>/smap_l4`: a GeoTIFF per 3-hour step and layer in
`aligned_tif/<layer>/`, `smap_l4_index.csv` (AOI mean and coverage per step;
`datetime` is the centre of the 3-hour average, UTC), `smap_l4_daily.csv` (mean
of the steps in each UTC day) and `smap_manifest.json`. ASCII grids are not
written: they would repeat a handful of values over every cell for every step.

## Standalone DEM download

`scripts/download_dem.py` downloads an OpenTopography DEM onto any grid. It
does not import this package, so it can be copied elsewhere.

```bash
python scripts/download_dem.py --reference grid.tif --output dem.tif
python scripts/download_dem.py --aoi aoi.shp --crs EPSG:32610 --resolution 10 --output dem.tif
```

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q tests
ruff check .
```

Changes are recorded in `CHANGELOG.md`. Two more scripts live in `scripts/`:
`run_landlab_batch.py` runs Landlab simulations on a prepared DEM, and
`smoke_test_soil_cli.py` checks the soil commands end to end.

## Citation and license

Cite with the metadata in `CITATION.cff`. MIT license, see `LICENSE`.

## Funding acknowledgement

This work was supported by the U.S. National Science Foundation under the
following awards:

- [2530591](https://www.nsf.gov/awardsearch/showAward?AWD_ID=2530591),
  CAIG: Framework for Artificial Intelligence-Enhanced Modeling of Wildfire
  Geohazards (FAIM-WG).
- [2303870](https://www.nsf.gov/awardsearch/showAward?AWD_ID=2303870),
  EAR RAPID: Monitoring postfire geomorphic response on humid slopes of the
  North Cascade Range, Washington.
- [2103632](https://www.nsf.gov/awardsearch/showAward?AWD_ID=2103632),
  OAC Frameworks: OpenEarthscape, transformative cyberinfrastructure for
  modeling and simulation in the Earth-surface science communities.

Any opinions, findings, and conclusions or recommendations expressed in this
material are those of the authors and do not necessarily reflect the views of
the National Science Foundation.
