"""Downloads into a persistent cache, written atomically.

A cached file only ever appears after a complete download, so reusing it is
safe. Delete the cache directory to force a fresh download.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import time
import zipfile

import requests

CHUNK_BYTES = 16 * 1024 * 1024
_EXTRACTED_MARKER = ".extracted"


def cached_download(
    url: str,
    cache_dir: str | Path,
    *,
    timeout: float = 60,
    retries: int = 3,
    retry_delay: float = 2.0,
    filename: str | None = None,
) -> Path:
    """Return the cached copy of ``url``, downloading it first if needed.

    ``filename`` names the cached file; by default it is derived from the URL.
    Client errors such as 404 fail at once; server and network errors retry.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if filename is None:
        name = Path(url.split("?", 1)[0]).name or "download"
        filename = f"{hashlib.sha1(url.encode()).hexdigest()[:12]}_{name}"
    target = cache_dir / filename
    if target.is_file() and target.stat().st_size > 0:
        return target

    partial = target.with_name(target.name + ".part")
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            _stream_to(url, partial, timeout)
            partial.replace(target)
            return target
        except requests.HTTPError as exc:
            partial.unlink(missing_ok=True)
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500 and status != 429:
                raise RuntimeError(f"Download failed: {url}: {exc}") from None
            last_error = exc
        except (requests.RequestException, OSError) as exc:
            partial.unlink(missing_ok=True)
            last_error = exc
        if attempt < retries:
            time.sleep(retry_delay * attempt)
    raise RuntimeError(f"Download failed after {retries} attempts: {url}: {last_error}")


def _stream_to(url: str, destination: Path, timeout: float) -> None:
    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        expected = response.headers.get("Content-Length")
        encoded = response.headers.get("Content-Encoding", "identity") not in ("identity", "")
        written = 0
        with open(destination, "wb") as f:
            for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
    if written == 0:
        raise OSError("empty download")
    if expected is not None and not encoded and written != int(expected):
        raise OSError(f"incomplete download: {written} of {expected} bytes")


def extract_tifs(zip_path: str | Path, dest_dir: str | Path) -> list[Path]:
    """Extract a zip once and list its GeoTIFFs; a marker records a complete extraction."""
    zip_path, dest = Path(zip_path), Path(dest_dir)
    if not (dest / _EXTRACTED_MARKER).is_file():
        staging = dest.with_name(dest.name + ".part")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(staging)
        (staging / _EXTRACTED_MARKER).write_text(str(zip_path))
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(staging, dest)

    tifs = sorted(p for p in dest.rglob("*") if p.is_file() and p.suffix.lower() in (".tif", ".tiff"))
    if not tifs:
        raise FileNotFoundError(f"No .tif found inside {zip_path}")
    return tifs


def extract_first_tif(zip_path: str | Path, dest_dir: str | Path) -> Path:
    return extract_tifs(zip_path, dest_dir)[0]


def extract_tif_by_suffix(zip_path: str | Path, dest_dir: str | Path, suffix: str) -> Path:
    """BAER bundles hold several GeoTIFFs; pick the product by its filename suffix."""
    tifs = extract_tifs(zip_path, dest_dir)
    matches = [p for p in tifs if p.name.lower().endswith(suffix.lower())]
    if not matches:
        raise FileNotFoundError(
            f"No .tif ending in {suffix!r} inside {zip_path}. Found: {[p.name for p in tifs]}"
        )
    return matches[0]
