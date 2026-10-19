# What the Landlab debris-flow workflow needs

The contract between data staging and `landlab_data_prep`. A staging tool that
delivers these inputs, and a converter that honours the grid and nodata rules,
can replace the download code here without changing any model result.

Reference implementation: this package. Reference event: Pioneer Fire, WA
(`WA4818312053120240608`), 10 m, with mapped initiation points and a lidar dDEM
for validation.

## 1. One grid, decided once

Every layer is put on a single analysis grid, derived from the AOI alone:

| Rule | Value |
|---|---|
| CRS | the AOI's own CRS if it is UTM, otherwise the UTM zone of the AOI centroid |
| Pixel | square, `target_res` metres (10 m for Pioneer) |
| Extent | AOI bounds snapped **outward** to multiples of `target_res` |
| Nodata | `-9999` (float32), including every cell outside the AOI, with one exception below |
| Files | ESRI ASCII for Landlab, GeoTIFF alongside |

Nothing else chooses a CRS, origin or pixel size, and nothing else resamples.
Every output must pass the same check: same CRS, transform, shape and nodata as
the grid. A layer half a cell off is the most common silent error, so the
check is a test, not a convention.

## 2. Inputs the model reads (required)

| Dataset | Source | Native | Resampling | Rule on the grid |
|---|---|---|---|---|
| DEM | USGS 3DEP 10 m (today via OpenTopography; Planetary Computer `3dep-seamless` would do) | 10 m | cubic | `-9999` outside AOI |
| Burn severity, SBS | BAER / MTBS | 20–30 m | nearest | classes 2–4 kept; everything else, missing data and outside the AOI included, becomes 1 (unburned). The one layer with no nodata |
| Soil, 7 layers | SOLUS100 (`cec7`, `anylithicdpt`, `claytotal`, `ph1to1h2o`, `sandtotal`, `silttotal`, `dbovendry`, all 0 cm) | 100 m | bilinear | where any one is missing, every derived soil field is `-9999` and the Landlab node is closed |
| Landcover | NLCD Annual 2021 | 30 m | nearest | water (class 11) closes the node |
| K-factor | STATSGO KFFACT, USGS ScienceBase | 30 m | nearest | `-0.1` (large water bodies) is a code, not a value: becomes `-9999` |

Copernicus 30 m is not a substitute for the DEM: the terrain features (slope,
specific catchment area, ΔG, curvature) are computed at 10 m on a lidar
footprint, and lose their meaning at 30 m.

## 3. Inputs used by the evidence and ML tracks (optional, same grid)

| Dataset | Source | Native | Resampling | Note |
|---|---|---|---|---|
| dNBR | BAER preliminary bundle | 30 m | bilinear | continuous severity |
| HLS NBR, NDVI, NDMI | NASA HLS v2.0, pre and post windows | 30 m | bilinear | median of cloud-free scenes per window; pre − post and relative dNBR |
| PRISM daily ppt, tmin, tmax | PRISM, by release | 800 m / 4 km | ppt nearest, temperature bilinear | one grid per day; PRISM's day ends 12:00 UTC |
| Synoptic station precipitation | Synoptic API | stations | nearest station within `max_distance_km`, else nodata | hourly to daily totals; FlowAlert's source |
| NISAR GCOV, SME2 | NASA Earthdata | 20–30 m | GCOV average of linear power; SME2 nearest | soil moisture flags withhold most of the AOI in winter |
| SMAP L4 soil moisture | NASA Earthdata | 9 km | nearest | coarse; context only |

## 4. What comes out: Landlab node fields

24 ASCII grids, one per field, all on the grid of section 1:

`topographic__elevation`, `burn__severity`, `landcover`, `kffact`,
`clay__total`, `sand__total`, `silt__total`, `pH`, `cation__exchange_capacity`,
`dry__bulk_density`, `soil__density`, `soil__thickness`, `soil__texture`,
`porosity`, `field__capacity`, `wilting__point`, `saturated__water_content`,
`soil__saturated_hydraulic_conductivity`, `soil__transmissivity`,
`soil__internal_friction_angle`, `soil__minimum_total_cohesion`,
`soil__mode_total_cohesion`, `soil__maximum_total_cohesion`,
`vegetation__plant_functional_type`.

The soil-derived fields are functions of the seven SOLUS layers and landcover;
the conversion from source rasters to these fields is the part of this package
that is Landlab-specific and should stay in one place.

## 5. Acceptance

A staged event is correct when:

1. every layer passes the grid check (`landlab_data_prep.analysis_grid.check_on_grid`);
2. nodata is `-9999` everywhere outside the AOI, except `burn__severity`, which is 1;
3. the Pioneer reference run reproduces its stored outputs within float32 tolerance;
4. `landlab-grid-validation` scores the model layers against the 89 mapped
   initiation points and reproduces the stored AUCs (0.57 / 0.66 / 0.68 for
   landslide, channel initiation and combined hazard, all failure types).

Items 3 and 4 are what make a refactor safe: they fail on a plausible wrong
number, not only on a crash.
