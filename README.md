# landslide-data-prep

Preprocessing pipeline for AOI-based geospatial data retrieval, harmonization, and Landlab-ready raster export.

## Scope

This repo keeps the data-prep side only:

- AOI validation
- DEM retrieval or local DEM ingest
- burn severity retrieval with `remote_then_local` fallback
- soil, landcover, vegetation, climate, and topographic layer processing
- aligned raster stack generation
- Landlab-ready `.asc` export
- final GeoTIFF export

This repo does not include ML training, tuning, or prediction workflows.

## Install

```bash
conda env create -f environment.yml
conda activate landslide-data-prep
```

If you already have a working geospatial environment on this machine, you can also use it directly.

## Quick Start

```bash
git clone <repo-url>
cd landslide-data-prep
conda env create -f environment.yml
conda activate landslide-data-prep
cp config/base.example.yaml config/base.yaml
```

`config/base.yaml` is the one working config, for one event at a time. For a
new event, edit its values rather than adding config files. At minimum set:

- `aoi.aoi` and `paths.output_dir`
- `raster.target_res`
- the DEM, burn severity, soil and landcover sources, each a local path or a remote URL
- `fire.name` and `fire.id`, only if burn severity or dNBR is downloaded

Continuous dNBR is optional. Keep `dnbr.enabled: false` to skip it. Set it to
`true` only after configuring a valid local file or remote BAER post-image date.

## Run

Raster-only debug run:

```bash
python src/run_pipeline.py --config config/base.yaml --raster-only --keep-intermediates
```

Full preprocessing run with final GeoTIFF export:

```bash
python src/run_pipeline.py --config config/base.yaml --export-final-tifs
```

## Analysis grid

Every command builds the same grid from the config, and nothing else chooses a
CRS, origin or pixel size:

- **CRS:** the AOI's own CRS when it is UTM, otherwise the UTM zone of the AOI
  centre. The AOI file is only read, never rewritten.
- **Extent:** the AOI bounds, snapped outward to multiples of `raster.target_res`.
- **Pixels:** square, `raster.target_res` metres.

Each layer is warped onto that grid once, with its configured resampling (the
DEM always uses cubic), and cells outside the AOI are set to `-9999`. Every
output is checked against the grid, and a mismatch stops the run. There are no
per-command resolution, CRS, AOI or template flags: change the config instead.

The pipeline stops if any configured source fails, listing every failure.
Soil-derived layers are `-9999` wherever any soil input is missing, and a layer
containing infinite or NaN values stops the run instead of being written.
Downloads and aligned intermediates live in `_downloads/` and `_aligned/`
inside the output directory; cleanup removes only those two folders.

## Soil CLI

Standalone soil workflow. All three commands read the AOI and grid from
`--config` (default `config/base.yaml`), so soil layers land on the same grid
as the pipeline:

```bash
python scripts/soil_fetch.py
python scripts/soil_harmonize.py
python scripts/soil_run.py
```

## Remote sensing: HLS vegetation indices

Optional. Builds NBR, NDVI and NDMI composites for a pre window and a post window
from NASA's Harmonized Landsat Sentinel-2 vegetation-index products
(HLSL30_VI and HLSS30_VI v2.0, 30 m), plus their change. Enable it by adding a
`remote_sensing` block to the config. It runs as its own command, separate
from the Landlab pipeline, which ignores this block. The commented example in
`config/base.example.yaml` is a starting point.

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

Every key except `resampling` is required, and unknown keys are an error. Keep
both windows in the same season, otherwise the change layers measure phenology
rather than disturbance. HLS starts 2013-04-11 for Landsat and 2015-11-28 for
Sentinel-2.

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
# GeoTIFFs on the analysis grid + manifest in <paths.output_dir>/remote_sensing
python scripts/remote_sensing_run.py --config config/base.yaml
```

**Products**

| File | Meaning | Units |
|---|---|---|
| `hls_<index>_pre.tif`, `hls_<index>_post.tif` | Median of clear observations in the window | unitless |
| `hls_dndvi.tif`, `hls_dndmi.tif` | Pre minus post | unitless |
| `hls_dnbr.tif` | 1000 x (NBR pre - NBR post) | x1000 |
| `hls_rdnbr.tif` | dNBR / sqrt(max(abs(NBR pre), 0.001)) | x1000 |
| `hls_clear_count_pre.tif`, `hls_clear_count_post.tif` | Clear solar days behind each pixel | days |

Change metrics are always pre minus post, so positive means the index dropped.
Missing values are `-9999`. Use the clear-count layers to decide how many
observations a pixel needs before you trust it.

**Behaviour worth knowing**

- Per pixel, Fmask cloud, cloud shadow, adjacent-to-cloud, snow/ice and high
  aerosol are masked. Water is kept. A pixel-day counts only when every
  requested index is valid, so all indices come from the same observations.
- Tiles from one satellite pass share a solar day and are averaged before the
  median, so overlapping tiles are not counted twice.
- Composites are made on the native 30 m HLS grid and kept in
  `remote_sensing/native/`. Each product is then put on the analysis grid by
  the same warp every other layer uses: counts with nearest, everything else
  with `remote_sensing.resampling` or `raster.resampling_method`. Native
  resolution is recorded in each GeoTIFF's tags and in
  `remote_sensing_manifest.json`, which also lists every scene ID used.
- The run stops rather than writing partial output on an invalid config, an
  empty window, missing credentials, an unreadable scene, a composite with no
  clear pixel, or a stack larger than 8 GiB.

## Tests

```bash
python -m pytest -q tests
```

## Notes

- `config/base.example.yaml` is the tracked template. `config/base.yaml` is your
  git-ignored working config with the same structure. These are the only configs.
- `--raster-only` stops before Landlab-style final layer generation.
- Use the full run if you want the final `.asc` and `.tif` layer set.

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
