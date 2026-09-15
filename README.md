# landslide-data-prep

Prepares aligned geospatial inputs for postfire debris-flow and landslide
modelling. From one YAML config it builds DEM, burn severity, soil and
landcover layers for Landlab, plus optional HLS vegetation indices, all on the
same analysis grid.

## Install

On Linux, recreate the exact tested environment:

```bash
conda env create -f environment.lock.yml
conda activate landslide-data-prep
pip install --no-deps -e .
```

On other platforms, use `environment.yml` instead of the lockfile.

## Quick start

```bash
cp config/base.example.yaml config/base.yaml
# edit config/base.yaml for your event, then:
landslide-prep-pipeline --config config/base.yaml --export-final-tifs
```

`config/base.yaml` is git-ignored and holds one event at a time. For another
event, edit its values rather than adding config files.

## Commands

| Command | From a clone, without installing | What it does |
|---|---|---|
| `landslide-prep-pipeline` | `python scripts/run_pipeline.py` | DEM, burn severity, soil and landcover on the grid, then Landlab fields |
| `landslide-prep-soil` | `python scripts/soil_run.py` | Fetch soil layers and align them to the grid |
| `landslide-prep-soil-fetch` | `python scripts/soil_fetch.py` | Fetch and crop soil layers only |
| `landslide-prep-soil-align` | `python scripts/soil_harmonize.py` | Align already fetched soil layers |
| `landslide-prep-hls` | `python scripts/remote_sensing_run.py` | HLS vegetation indices, described below |
| `landslide-prep-dem-difference` | `python scripts/dem_difference.py` | Post-event minus pre-event DEM on the grid |
| `landslide-prep-export-tifs` | `python scripts/export_tifs.py` | Convert a folder of ASCII grids to GeoTIFF |

Every command takes `--config`; `--help` lists the other options. The pipeline
also takes `--raster-only` to stop before Landlab fields, `--export-final-tifs`
to write GeoTIFFs next to the ASCII grids, and `--keep-intermediates` to keep
`_downloads/` and `_aligned/` in the output folder.

## Config

One YAML file, validated before anything runs. Unknown keys, wrong types and
missing keys stop the command, and every problem is listed at once. Each
command requires only the sections it uses, and checks any other section that
is present. `config/base.example.yaml` documents every key.

| Section | Keys | Notes |
|---|---|---|
| `aoi` | `aoi` | Polygon vector file. Only read, never modified. |
| `paths` | `output_dir`, `cache_dir` | `cache_dir` is required when any source downloads. |
| `fire` | `name`, `id`, `post_image_date` | Only used to build BAER download names. |
| `raster` | `target_res`, `resampling_method` | Defines the analysis grid. |
| `dem` | `source`, then `path` or `dem_type` and `buffer_deg`, `api_key` | `source` is `local` or `bmi-topography`. |
| `burn_severity` | `source`, `local`, `remote` | `source` is `local`, `remote`, `remote_then_local` or `auto`. |
| `dnbr` | `enabled`, `source`, `local`, `remote` | Continuous dNBR, skipped unless enabled. |
| `feature_sources` | `rasters`, `landcover` | The seven SoLUS soil layers and one landcover layer. Each `url` is a local path or a URL. |
| `remote_sensing` | see below | Only used by `landslide-prep-hls`. |

**OpenTopography key.** Downloading a DEM needs an OpenTopography API key,
looked up in this order: `USGS_TOPO_API_KEY`, `OPENTOPOGRAPHY_API_KEY`, then
`dem.api_key`. A missing key stops the run; the public demo key is never used.

**Downloads.** Remote files are cached in `paths.cache_dir` and reused across
runs and events, so the national soil and landcover files download once per
machine. A cached file only appears after a complete download. Delete the
folder to force a refresh.

## Analysis grid

Every command builds the same grid from the config, and nothing else chooses a
CRS, origin or pixel size:

- **CRS:** the AOI's own CRS when it is UTM, otherwise the UTM zone of the AOI
  centre.
- **Extent:** the AOI bounds, snapped outward to multiples of `raster.target_res`.
- **Pixels:** square, `raster.target_res` metres.

Each layer is warped onto that grid once with its configured resampling; the
DEM always uses cubic. Every output is checked against the grid, and a mismatch
stops the run. There are no per-command resolution, CRS, AOI or template
options: change the config instead. The pipeline stops if any configured source
fails, listing every failure.

## Modelling rules

These choices shape the Landlab inputs. Change them deliberately, in code.

- **Outside the AOI**, every layer is `-9999` and the Landlab nodes are closed.
- **Burn severity** keeps classes 2, 3 and 4. Every other value, including
  nodata inside the AOI, becomes 1, unburned.
- **Missing soil data** closes the node, and every soil-derived layer is
  `-9999` wherever any of the seven soil inputs is missing.
- **Water**, landcover class 11, closes the node.
- **Conductivity, transmissivity, porosity and friction angle** come from the
  pedotransfer rules in `soil_features.py`. **Root cohesion** and plant type
  come from landcover classes in `vegetation_features.py`.
- A layer containing infinite or NaN values stops the run instead of being
  written.

## Remote sensing: HLS vegetation indices

Builds NBR, NDVI and NDMI composites for a pre window and a post window from
NASA's Harmonized Landsat Sentinel-2 vegetation-index products (HLSL30_VI and
HLSS30_VI v2.0, 30 m), plus their change. It runs separately from the Landlab
pipeline, which ignores the `remote_sensing` block. Uncomment the example at
the bottom of `config/base.example.yaml` to start.

```yaml
remote_sensing:
  windows:
    pre:  {start: 2017-06-01, end: 2017-09-30}
    post: {start: 2018-06-01, end: 2018-09-30}
  indices: [NBR, NDVI, NDMI]
  cloud_max: 60          # drop whole scenes above this cloud cover percent
  compositing: median
  resampling: bilinear   # optional; default is raster.resampling_method
```

Every key except `resampling` is required. Keep both windows in the same
season, otherwise the change layers measure phenology rather than disturbance.
HLS starts 2013-04-11 for Landsat and 2015-11-28 for Sentinel-2.

**Credentials.** Catalog search is public. Downloads need a free NASA Earthdata
account, supplied one of two ways:

```bash
export EARTHDATA_TOKEN=<token from https://urs.earthdata.nasa.gov/profile>
# or
echo "machine urs.earthdata.nasa.gov login <user> password <pass>" >> ~/.netrc
chmod 600 ~/.netrc
```

**Run.**

```bash
landslide-prep-hls --config config/base.yaml
```

GeoTIFFs on the analysis grid and a manifest are written to
`<paths.output_dir>/remote_sensing`.

| File | Meaning | Units |
|---|---|---|
| `hls_<index>_pre.tif`, `hls_<index>_post.tif` | Median of clear observations in the window | unitless |
| `hls_dndvi.tif`, `hls_dndmi.tif` | Pre minus post | unitless |
| `hls_dnbr.tif` | 1000 x (NBR pre - NBR post) | x1000 |
| `hls_rdnbr.tif` | dNBR / sqrt(max(abs(NBR pre), 0.001)) | x1000 |
| `hls_clear_count_pre.tif`, `hls_clear_count_post.tif` | Clear solar days behind each pixel | days |

Change metrics are pre minus post, so positive means the index dropped. Missing
values are `-9999`. Use the clear-count layers to decide how many observations
a pixel needs before you trust it.

- Per pixel, Fmask cloud, cloud shadow, adjacent-to-cloud, snow/ice and high
  aerosol are masked; water is kept. A pixel-day counts only when every
  requested index is valid, so all indices come from the same observations.
- Tiles from one satellite pass share a solar day and are averaged before the
  median, so overlapping tiles are not counted twice.
- Composites are made on the native 30 m HLS grid and kept in
  `remote_sensing/native/`, then aligned to the analysis grid: counts with
  nearest, everything else with `remote_sensing.resampling` or
  `raster.resampling_method`. The manifest lists every scene ID used.
- The run stops rather than writing partial output on an invalid config, an
  empty window, missing credentials, an unreadable scene, a composite with no
  clear pixel, or a stack larger than 8 GiB.

## Standalone DEM download and alignment

`scripts/download_dem.py` is self-contained and can be copied elsewhere. It
requires `bmi-topography`, `geopandas`, `rasterio`, `shapely`, `numpy`, and `pyyaml`.
It automatically reads `dem.api_key` from `config/base.yaml` relative to the
script location. Use `--config /path/to/config.yaml` to select another file.
Only the key is read; AOI and grid settings come from command-line arguments.
`USGS_TOPO_API_KEY` or `OPENTOPOGRAPHY_API_KEY` overrides the configured key.

Match an existing raster exactly (its footprint is the default AOI):

```bash
python scripts/download_dem.py \
  --reference /path/to/analysis_raster.tif \
  --output /path/to/aligned_dem.tif
```

Use a shapefile or another polygon vector to limit the domain while retaining
that reference's full dimensions:

```bash
python scripts/download_dem.py \
  --aoi /path/to/aoi.shp \
  --reference /path/to/analysis_raster.tif \
  --mask-reference \
  --output /path/to/aligned_dem.tif
```

Without a reference, specify a CRS and square pixel size:

```bash
python scripts/download_dem.py \
  --aoi /path/to/aoi.shp \
  --crs EPSG:32610 --resolution 10 \
  --output /path/to/dem.tif
```

- `--aoi` also accepts a raster, using its full footprint.
- Reference mode copies CRS, affine transform (including rotation), width and
  height exactly. It rejects CRS/resolution overrides. `--mask-reference`
  additionally excludes invalid cells in reference band 1.
- Explicit-grid mode snaps AOI bounds outward to multiples of the resolution,
  which is in target CRS units: metres for a metre-based projected CRS, degrees
  for a geographic CRS.
- The default source is `USGS10m`; `--dem-type` selects another product supported
  by the installed `bmi-topography`. Product coverage and API limits still apply.
- Resampling defaults to cubic. Finer output pixels do not increase source detail.
- Outside-AOI and missing elevations are `-9999`. The JSON printed on completion
  reports grid geometry, valid pixels, and missing pixels within the domain.
- Downloads are cached in `dem_cache/` beside the output, or `--cache-dir`.
  `--source-dem /path/to/local.tif` bypasses downloading and credentials.
- Existing outputs require `--overwrite`. Input files cannot be used as output.
- Elevations retain the source's vertical units and datum; only the horizontal
  grid is harmonized. No vertical datum conversion is performed.

## Other scripts

- `scripts/run_landlab_batch.py` runs Landlab landscape-evolution simulations
  on a prepared DEM, in parallel. It is a modelling tool, not part of data
  preparation.
- `scripts/smoke_test_soil_cli.py` runs the soil commands end to end on
  synthetic data.

## Tests

```bash
python -m pytest -q tests
```

## Changes

See `CHANGELOG.md`.
