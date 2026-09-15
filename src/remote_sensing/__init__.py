"""HLS vegetation-index products: search, composite, and write on the native grid."""

from .config import RemoteSensingConfig, RemoteSensingConfigError, Window, parse_remote_sensing_config
from .core import (
    MANIFEST_NAME,
    REMOTE_SENSING_SUBDIR,
    EmptyWindowError,
    Product,
    ProductSpec,
    build_hls_products,
    product_specs,
)

__all__ = [
    "MANIFEST_NAME",
    "REMOTE_SENSING_SUBDIR",
    "EmptyWindowError",
    "Product",
    "ProductSpec",
    "RemoteSensingConfig",
    "RemoteSensingConfigError",
    "Window",
    "build_hls_products",
    "parse_remote_sensing_config",
    "product_specs",
]
