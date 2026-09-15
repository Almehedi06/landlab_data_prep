# Changelog

## 0.2.0 - 2026-09-15

First version prepared for use by other people. Configs written for 0.1.0 need
the changes under "Migrating from 0.1.0".

### Added

- One analysis grid for every command, built from `aoi.aoi` and
  `raster.target_res`, with a single warp and a grid check on every output.
- Strict config validation. Unknown keys and wrong types stop a run, and every
  problem is reported at once.
- A persistent download cache under `paths.cache_dir`, written atomically, so
  national soil and landcover files download once per machine.
- Installable package `landslide_data_prep` with named commands, a Linux
  lockfile and a CI workflow.
- Standalone HLS vegetation-index products: `landslide-prep-hls`.
- Standalone DEM download and alignment: `scripts/download_dem.py`.

### Fixed

- Scaling turned soil nodata into plausible numbers, conductivity overflowed to
  infinity, and GeoTIFF export crashed. Soil-derived layers are now `-9999`
  wherever an input is missing, and non-finite layers stop the run.
- Non-UTM AOI files were rewritten in place, and southern-hemisphere AOIs got a
  northern UTM zone.
- A failed source only logged a warning. The pipeline now stops and lists every
  failure.
- An unknown resampling name silently became nearest.
- The DEM was interpolated twice, and intermediates were written into input
  folders.
- A DEM download could silently use the public demo key or reuse an empty
  cached file.

### Migrating from 0.1.0

- Add `paths.cache_dir` when any source downloads, and move `dem.cache_dir`
  there.
- Remove `inputs`, `target`, `paths.input_dir`, `fire.state`, `fire.year` and
  `dem.output_format`.
- `python src/run_pipeline.py` is now `landslide-prep-pipeline`, or
  `python scripts/run_pipeline.py` from a clone.
- Soil commands no longer take `--aoi`, `--template` or `--target-res`, and
  the export command no longer takes `--aoi` or `--crs`. The config defines
  the grid.
- Outputs made by 0.1.0 hold source fill codes in edge and outside cells.
  Regenerate them.
