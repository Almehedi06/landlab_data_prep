from __future__ import annotations

import os
from collections.abc import Callable

import numpy as np
from landlab.io import esri_ascii

from landlab_data_prep.analysis_grid import NODATA, Grid, GridMismatchError, check_on_grid


def read_nodata_value(ascii_path: str, default: float = -9999.0) -> float:
    nodata_val = None
    with open(ascii_path) as f:
        for line in f:
            if line.strip().upper().startswith("NODATA_VALUE"):
                nodata_val = float(line.split()[1])
                break
    return default if nodata_val is None else nodata_val


def load_grid(ascii_path: str, field_name: str):
    with open(ascii_path) as f:
        return esri_ascii.load(f, name=field_name)


def add_ascii_field(
    master_grid,
    ascii_path: str,
    field_name: str,
    scale: float = 1.0,
    offset: float = 0.0,
    close_nodata: bool = True,
    extra_close_values: list[float] | None = None,
    rename_file: bool = True,
    transform: Callable | None = None,
):
    nodata_val = read_nodata_value(ascii_path)

    with open(ascii_path) as f:
        tmp = esri_ascii.load(f, name=field_name)

    raw_vals = tmp.at_node[field_name].copy()
    if transform is not None:
        raw_vals = transform(raw_vals)

    # Scaling must not turn nodata into a plausible number (-9999 * 0.01 = -99.99).
    vals = np.where(raw_vals == nodata_val, nodata_val, raw_vals * scale + offset)
    master_grid.add_field(field_name, vals, at="node", clobber=True)

    if close_nodata:
        master_grid.set_nodata_nodes_to_closed(raw_vals, nodata_val)

    if extra_close_values:
        for v in extra_close_values:
            master_grid.set_nodata_nodes_to_closed(raw_vals, v)

    if rename_file:
        new_path = os.path.join(os.path.dirname(ascii_path), f"{field_name}.asc")
        if ascii_path != new_path:
            os.rename(ascii_path, new_path)

    return master_grid


def write_ascii_field(path: str, grid, field_name: str, analysis_grid: Grid) -> None:
    """Write a node field as ESRI ASCII on ``analysis_grid``, where the DEM and every aligned layer sit.

    The header comes from the analysis grid, not from Landlab: Landlab's ESRI
    ASCII conventions have changed between versions, and a mix of them once put
    every field half a cell north-east of the DEM. Landlab nodes run from the
    bottom row up; ASCII rows run from the top down.
    """
    if tuple(grid.shape) != analysis_grid.shape:
        raise GridMismatchError(
            f"{field_name}: Landlab grid {tuple(grid.shape)} is not the analysis grid {analysis_grid.shape}"
        )
    values = np.asarray(grid.at_node[field_name], dtype=float).reshape(grid.shape)
    left, bottom, _, _ = analysis_grid.bounds
    with open(path, "w") as f:
        f.write(f"ncols         {analysis_grid.width}\n")
        f.write(f"nrows         {analysis_grid.height}\n")
        f.write(f"xllcorner     {left}\n")
        f.write(f"yllcorner     {bottom}\n")
        f.write(f"cellsize      {analysis_grid.resolution}\n")
        f.write(f"NODATA_value  {NODATA}\n")
        np.savetxt(f, np.flipud(values), fmt="%.9g")
    check_on_grid(path, analysis_grid)
