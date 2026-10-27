"""A field written from a Landlab grid sits on the analysis grid, the right way up."""

from pathlib import Path

import numpy as np
import pytest
import rasterio

from landlab_data_prep.analysis_grid import GridMismatchError, check_on_grid, snap_grid
from landlab_data_prep.landlab_io import load_grid, write_ascii_field
from landlab_data_prep.reproject_and_resample import convert_to_ascii


def _asc_on_grid(tmp_path: Path, values: np.ndarray, grid) -> Path:
    tif = tmp_path / "z.tif"
    with rasterio.open(tif, "w", **grid.profile(dtype="float64")) as dst:
        dst.write(values, 1)
    return Path(convert_to_ascii(str(tif), str(tmp_path), grid=grid))


def test_written_field_keeps_the_corner_and_the_row_order(tmp_path: Path) -> None:
    grid = snap_grid((500003.0, 5000001.0, 500038.0, 5000029.0), "EPSG:6339", 10.0)  # 4 x 3 cells
    values = np.arange(grid.height * grid.width, dtype="float64").reshape(grid.shape)  # top row first
    src = _asc_on_grid(tmp_path, values, grid)

    out = tmp_path / "out.asc"
    write_ascii_field(str(out), load_grid(str(src), "z"), "z", grid)

    check_on_grid(out, grid)
    assert out.read_text().splitlines()[:6] == src.read_text().splitlines()[:6]
    np.testing.assert_array_equal(np.loadtxt(out, skiprows=6), values)


def test_written_field_refuses_a_grid_of_another_size(tmp_path: Path) -> None:
    grid = snap_grid((500000.0, 5000000.0, 500040.0, 5000030.0), "EPSG:6339", 10.0)
    src = _asc_on_grid(tmp_path, np.zeros(grid.shape), grid)
    other = snap_grid((500000.0, 5000000.0, 500050.0, 5000030.0), "EPSG:6339", 10.0)
    with pytest.raises(GridMismatchError):
        write_ascii_field(str(tmp_path / "out.asc"), load_grid(str(src), "z"), "z", other)
