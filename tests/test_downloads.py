from __future__ import annotations

from pathlib import Path
import sys
import zipfile

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from landlab_data_prep import downloads
from landlab_data_prep.downloads import cached_download, extract_first_tif, extract_tif_by_suffix, extract_tifs


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, length: int | None = None) -> None:
        self.body = body
        self.status_code = status
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def iter_content(self, chunk_size: int):
        for i in range(0, len(self.body), 4):
            yield self.body[i : i + 4]


def test_download_is_cached_and_reused(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_get(url, stream, timeout):
        calls.append(url)
        return FakeResponse(b"GeoTIFF-bytes", length=13)

    monkeypatch.setattr(downloads.requests, "get", fake_get)
    first = cached_download("https://example.org/data/soil.tif", tmp_path)
    second = cached_download("https://example.org/data/soil.tif", tmp_path)
    assert first == second and first.read_bytes() == b"GeoTIFF-bytes"
    assert first.name.endswith("_soil.tif")
    assert len(calls) == 1
    assert not list(tmp_path.glob("*.part"))


def test_truncated_download_leaves_nothing_behind(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(downloads.requests, "get", lambda url, stream, timeout: FakeResponse(b"short", length=100))
    monkeypatch.setattr(downloads.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="incomplete download"):
        cached_download("https://example.org/big.zip", tmp_path, retries=2)
    assert list(tmp_path.iterdir()) == []


def test_client_errors_fail_fast_without_retrying(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_get(url, stream, timeout):
        calls.append(url)
        return FakeResponse(b"", status=404)

    monkeypatch.setattr(downloads.requests, "get", fake_get)
    with pytest.raises(RuntimeError, match="404"):
        cached_download("https://example.org/missing_sbs.zip", tmp_path, retries=3)
    assert len(calls) == 1


def test_zip_is_extracted_once(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("inner/b_dnbr.tif", b"x")
        zf.writestr("inner/a_sbs.tif", b"y")
        zf.writestr("readme.txt", b"z")
    dest = tmp_path / "bundle_extracted"

    assert [p.name for p in extract_tifs(archive, dest)] == ["a_sbs.tif", "b_dnbr.tif"]
    monkeypatch.setattr(downloads.zipfile, "ZipFile", lambda *a, **k: pytest.fail("extracted twice"))
    assert extract_first_tif(archive, dest).name == "a_sbs.tif"
    assert extract_tif_by_suffix(archive, dest, "_dnbr.tif").name == "b_dnbr.tif"
    with pytest.raises(FileNotFoundError, match="_missing.tif"):
        extract_tif_by_suffix(archive, dest, "_missing.tif")
