"""Decode the HLS v2.0 Fmask quality byte.

Bit layout from the HLS v2.0 User Guide, Table 9 (bit 0 least significant):
  0 cirrus (reserved, unused in v2.0)   1 cloud   2 adjacent to cloud/shadow
  3 cloud shadow   4 snow/ice   5 water   6-7 aerosol level (both set = high)

The HLS VI layers already carry fill for cloud, shadow and adjacency (HLS VI
User Guide v2.0, section 1.2), so this mask mainly adds snow/ice and high
aerosol, which the VI layers leave in. High aerosol is common in post-fire
scenes because of smoke. Re-checking the cloud bits costs nothing and protects
against a product revision. Water is kept: it is a surface, not contamination.
"""

from __future__ import annotations

import numpy as np

FMASK_FILL = 255

CIRRUS = 1 << 0  # reserved, not used in HLS v2.0; never masked
CLOUD = 1 << 1
ADJACENT = 1 << 2
SHADOW = 1 << 3
SNOW_ICE = 1 << 4
WATER = 1 << 5  # kept on purpose
AEROSOL_SHIFT = 6
AEROSOL_HIGH = 0b11

REJECT_BITS = CLOUD | ADJACENT | SHADOW | SNOW_ICE


def clear_mask(fmask: np.ndarray, fill: float = FMASK_FILL) -> np.ndarray:
    """True where a pixel is usable. ``fill`` is the file's declared nodata."""
    fmask = np.asarray(fmask)
    if not np.issubdtype(fmask.dtype, np.integer):
        raise TypeError(f"Fmask must be an integer array, got {fmask.dtype}")
    q = fmask.astype(np.uint16)
    is_fill = (fmask == fill) | (fmask == FMASK_FILL)
    rejected = (q & REJECT_BITS) != 0
    high_aerosol = ((q >> AEROSOL_SHIFT) & 0b11) == AEROSOL_HIGH
    return ~(is_fill | rejected | high_aerosol)
