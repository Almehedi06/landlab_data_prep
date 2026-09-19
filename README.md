# landlab_data_prep

Prepares analysis-ready inputs for Landlab landslide and postfire debris-flow
models. From one YAML config, it builds DEM, burn severity, soil and landcover
layers on a single common grid, with optional HLS vegetation indices.

## Install

On Linux, recreate the exact tested environment:

```bash
conda env create -f environment.lock.yml
conda activate landlab_data_prep
pip install --no-deps -e .
```

On other platforms, use `environment.yml` in place of the lockfile.

## Quick start

```bash
cp config/base.example.yaml config/base.yaml   # then edit it for your event
landlab-prep-pipeline --config config/base.yaml --export-final-tifs
```

`config/base.yaml` holds one event and is not tracked by git.

## Commands

| Command | Script, without installing | Purpose |
|---|---|---|
| `landlab-prep-pipeline` | `scripts/run_pipeline.py` | All layers on the grid, then Landlab fields |
| `landlab-prep-soil` | `scripts/soil_run.py` | Fetch and align soil layers |
| `landlab-prep-soil-fetch` | `scripts/soil_fetch.py` | Fetch and crop soil layers only |
| `landlab-prep-soil-align` | `scripts/soil_harmonize.py` | Align already fetched soil layers |
| `landlab-prep-hls` | `scripts/remote_sensing_run.py` | HLS vegetation indices |
| `landlab-prep-dem-difference` | `scripts/dem_difference.py` | Post-event minus pre-event DEM |
| `landlab-prep-export-tifs` | `scripts/export_tifs.py` | ASCII grids to GeoTIFF |

Every command takes `--config`. Run any command with `--help` for its options.

## Configuration

- One YAML file per event. `config/base.example.yaml` documents every key.
- The config is checked before anything runs. Unknown keys, wrong types and
  missing values stop the command, and all problems are reported together.
- Remote inputs are downloaded once into `paths.cache_dir` and reused.
- DEM downloads need an OpenTopography API key, read from
  `USGS_TOPO_API_KEY`, then `OPENTOPOGRAPHY_API_KEY`, then `dem.api_key`.

## Outputs

- **Grid.** Every layer is placed on one analysis grid: the AOI's UTM zone,
  with the AOI bounds snapped to multiples of `raster.target_res`.
- **Files.** ESRI ASCII grids for Landlab in `paths.output_dir`, and GeoTIFFs
  as well with `--export-final-tifs`.
- **Nodata.** Cells outside the AOI are `-9999`, except for burn severity.
- **Burn severity.** Classes 2 to 4 are kept. All other values, including
  missing data and cells outside the AOI, become 1, unburned.
- **Soil.** Where any soil input is missing, the derived soil layers are
  `-9999` and the Landlab node is closed. Water, landcover class 11, is also
  closed.

## HLS vegetation indices (optional)

Median NBR, NDVI and NDMI for a pre-event and a post-event window, and their
change, from NASA HLS v2.0 at 30 m, aligned to the analysis grid.

1. Uncomment the `remote_sensing` block in the config and set both windows in
   the same season.
2. Provide NASA Earthdata credentials, either `export EARTHDATA_TOKEN=<token>`
   or a `~/.netrc` entry for `urs.earthdata.nasa.gov`.
3. Run `landlab-prep-hls --config config/base.yaml`.

Outputs are written to `<output_dir>/remote_sensing`:

| File | Content |
|---|---|
| `hls_<index>_pre.tif`, `hls_<index>_post.tif` | Median of cloud-free observations |
| `hls_dndvi.tif`, `hls_dndmi.tif` | Pre minus post |
| `hls_dnbr.tif`, `hls_rdnbr.tif` | dNBR and relative dNBR, scaled by 1000 |
| `hls_clear_count_pre.tif`, `hls_clear_count_post.tif` | Cloud-free days per pixel |

Cloud, shadow, snow and high-aerosol pixels are masked. HLS data begin in 2013
for Landsat and late 2015 for Sentinel-2.

## Standalone DEM download

`scripts/download_dem.py` downloads an OpenTopography DEM onto a grid you
choose. It does not import this package, so it can be copied into other
projects.

```bash
# Match an existing raster's grid exactly
python scripts/download_dem.py --reference grid.tif --output dem.tif

# Or define the grid
python scripts/download_dem.py --aoi aoi.shp --crs EPSG:32610 --resolution 10 --output dem.tif
```

It reads the API key from the same environment variables, or from
`dem.api_key` in `config/base.yaml`. Cells outside the AOI are `-9999`, and
elevations keep the source's vertical datum. Run it with `--help` for all
options.

## Other scripts

- `scripts/run_landlab_batch.py` runs Landlab landscape-evolution simulations
  on a prepared DEM.
- `scripts/smoke_test_soil_cli.py` checks the soil commands end to end.

## Development

Run the tests with `python -m pytest -q tests`. Changes are recorded in
`CHANGELOG.md`.

## License

MIT. See `LICENSE`.

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
