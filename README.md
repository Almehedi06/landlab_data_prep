# landlab_data_prep

Builds analysis-ready inputs for Landlab landslide and postfire debris-flow
models. One YAML config in, one common grid out: DEM, burn severity, soil,
landcover and K-factor, plus optional satellite indices and daily weather.

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
