"""Pure change metrics between two composites.

Inputs are float arrays with NaN as nodata. NaN propagates through every
function here, so a pixel missing in either window is missing in the result.
No I/O, no config: these are the functions the tests pin down.

Sign convention for every metric: post minus pre, the same as every other
change layer (dz, DeltaG). Negative means the index decreased between the two
windows: vegetation, moisture or NBR lost. The NBR change is therefore the
published dNBR (Key & Benson) with its sign flipped, which is why it carries a
different name: high severity is delta_nbr < -660, not dNBR > 660.
"""

from __future__ import annotations

import numpy as np

# Miller & Thode (2007) floor for |NBR_pre| so RdNBR stays finite where the
# pre-window NBR is near zero (bare ground, water edges).
RDNBR_PRE_FLOOR = 0.001


def difference(pre: np.ndarray, post: np.ndarray) -> np.ndarray:
    """Unitless change, post minus pre."""
    pre, post = _as_float_pair(pre, post)
    return post - pre


def delta_nbr(nbr_pre: np.ndarray, nbr_post: np.ndarray) -> np.ndarray:
    """NBR change, post minus pre, in the conventional x1000 scaling: -dNBR (Key & Benson 2006)."""
    return 1000.0 * difference(nbr_pre, nbr_post)


def rdelta_nbr(nbr_pre: np.ndarray, nbr_post: np.ndarray) -> np.ndarray:
    """Relativized NBR change, post minus pre: -RdNBR (Miller & Thode 2007), delta_nbr / sqrt(|NBR_pre|)."""
    pre, post = _as_float_pair(nbr_pre, nbr_post)
    # np.maximum propagates NaN, so missing pre pixels stay missing.
    denom = np.sqrt(np.maximum(np.abs(pre), RDNBR_PRE_FLOOR))
    return delta_nbr(pre, post) / denom


def _as_float_pair(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    if a.shape != b.shape:
        raise ValueError(f"Shape mismatch: pre {a.shape} vs post {b.shape}")
    return a, b
