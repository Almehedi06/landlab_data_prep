"""NASA Earthdata access shared by every source that reads from it (HLS, NISAR, SMAP).

Credentials, CMR granule search, and a file object that reads a remote file by
HTTP range requests, so only the blocks a reader touches travel.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
import io
import netrc
import os
from pathlib import Path
import time
from urllib.parse import urlparse

import requests

EARTHDATA_HOST = "urs.earthdata.nasa.gov"
TOKEN_ENV_VAR = "EARTHDATA_TOKEN"
CMR_GRANULES_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"
DATA_REL = "http://esipfed.org/ns/fedsearch/1.1/data#"
PAGE_SIZE = 2000
# Default block for range reads. Paged files (NISAR) want large blocks; files with
# scattered metadata (SMAP) read fewer bytes with small ones.
BLOCK_SIZE = 4 * 2**20
MAX_CACHED_BLOCKS = 128
RETRIES = 3


class EarthdataAuthError(RuntimeError):
    """No usable Earthdata credentials, or Earthdata refused them."""


class EarthdataError(RuntimeError):
    """A search or read against NASA Earthdata failed."""


def cmr_granules(params: Mapping[str, object]) -> list[dict]:
    """Every CMR granule entry matching ``params``, following CMR's paging."""
    entries: list[dict] = []
    headers: dict[str, str] = {}
    try:
        while True:
            response = requests.get(
                CMR_GRANULES_URL, params={"page_size": PAGE_SIZE, **params}, headers=headers, timeout=60
            )
            response.raise_for_status()
            page = response.json()["feed"]["entry"]
            entries.extend(page)
            after = response.headers.get("CMR-Search-After")
            if len(page) < PAGE_SIZE or not after:
                break
            headers = {"CMR-Search-After": after}
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise EarthdataError(f"NASA CMR search for {params.get('short_name')} failed: {exc}") from None
    return entries


class _EarthdataSession(requests.Session):
    """Sends the netrc login only to the Earthdata login host when ASF redirects there."""

    def __init__(self, login: tuple[str, str]) -> None:
        super().__init__()
        self._login = login

    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        if urlparse(prepared_request.url).hostname == EARTHDATA_HOST:
            prepared_request.prepare_auth(self._login)


def earthdata_session(
    environ: Mapping[str, str] | None = None,
    netrc_path: str | Path | None = None,
) -> tuple[requests.Session, dict[str, str]]:
    """A session and the headers for the first request to each file.

    Same two routes as HLS, in order: ``EARTHDATA_TOKEN``, then a netrc entry for
    urs.earthdata.nasa.gov. Raises before any read if neither is present.
    """
    environ = os.environ if environ is None else environ
    token = (environ.get(TOKEN_ENV_VAR) or "").strip()
    if token:
        return requests.Session(), {"Authorization": f"Bearer {token}"}

    path = Path(netrc_path or environ.get("NETRC") or (Path.home() / ".netrc"))
    if path.exists():
        try:
            entry = netrc.netrc(str(path)).authenticators(EARTHDATA_HOST)
        except (netrc.NetrcParseError, OSError) as exc:
            raise EarthdataAuthError(f"Could not parse netrc file {path}: {exc}") from exc
        if entry:
            login, _, password = entry
            return _EarthdataSession((login, password or "")), {}

    raise EarthdataAuthError(
        "NASA Earthdata reads need credentials, and none were found. Either:\n"
        f"  - export {TOKEN_ENV_VAR}=<token from https://urs.earthdata.nasa.gov/profile>, or\n"
        f"  - add 'machine {EARTHDATA_HOST} login <user> password <pass>' to {path} (chmod 600)."
    )


class RemoteFile(io.RawIOBase):
    """Read-only, seekable view of one remote file, fetched in cached blocks by HTTP range requests.

    The first request follows ASF's login redirects; later reads go straight to the
    signed link they lead to, without credentials, and follow them again if it expires.
    """

    def __init__(
        self,
        url: str,
        session: requests.Session,
        auth_headers: Mapping[str, str] | None = None,
        block_size: int = BLOCK_SIZE,
        max_blocks: int = MAX_CACHED_BLOCKS,
    ) -> None:
        super().__init__()
        self.url = url
        self._session = session
        self._auth = dict(auth_headers or {})
        self._block_size = block_size
        self._max_blocks = max_blocks
        self._blocks: OrderedDict[int, bytes] = OrderedDict()
        self._pos = 0
        self.bytes_fetched = 0
        self.range_requests = 0
        self._href, self._headers, self.size = self._resolve()

    @property
    def name(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def readinto(self, buffer) -> int:
        view = memoryview(buffer).cast("B")
        n = max(0, min(len(view), self.size - self._pos))
        if n == 0:
            return 0
        size = self._block_size
        first, last = self._pos // size, (self._pos + n - 1) // size
        self._fetch([i for i in range(first, last + 1) if i not in self._blocks])
        data = b"".join(self._blocks[i] for i in range(first, last + 1))
        offset = self._pos - first * size
        view[:n] = data[offset:offset + n]
        for i in range(first, last + 1):
            self._blocks.move_to_end(i)
        while len(self._blocks) > self._max_blocks:
            self._blocks.popitem(last=False)
        self._pos += n
        return n

    def _fetch(self, missing: list[int]) -> None:
        """Fetch each run of consecutive missing blocks with one range request."""
        runs: list[list[int]] = []
        for i in missing:
            if runs and runs[-1][-1] == i - 1:
                runs[-1].append(i)
            else:
                runs.append([i])
        size = self._block_size
        for run in runs:
            start, stop = run[0] * size, min((run[-1] + 1) * size, self.size)
            body = self._range(start, stop - 1)
            for k, i in enumerate(run):
                self._blocks[i] = body[k * size:(k + 1) * size]

    def _range(self, start: int, end: int) -> bytes:
        problem = ""
        for attempt in range(RETRIES):
            try:
                response = self._session.get(
                    self._href, headers={**self._headers, "Range": f"bytes={start}-{end}"}, timeout=120
                )
            except requests.RequestException as exc:
                problem = str(exc)
            else:
                if response.status_code == 206 and len(response.content) == end - start + 1:
                    self.range_requests += 1
                    self.bytes_fetched += len(response.content)
                    return response.content
                problem = f"HTTP {response.status_code}, {len(response.content)} bytes"
                if response.status_code in (401, 403):
                    self._href, self._headers, _ = self._resolve()  # the signed link expired
                elif response.status_code < 500 and response.status_code != 206:
                    break
            time.sleep(2 * (attempt + 1))
        raise EarthdataError(f"Could not read bytes {start}-{end} of {self.name}: {problem}")

    def _resolve(self) -> tuple[str, dict[str, str], int]:
        try:
            response = self._session.get(
                self.url, headers={**self._auth, "Range": "bytes=0-0"}, timeout=120, stream=True
            )
        except requests.RequestException as exc:
            raise EarthdataError(f"Could not reach {self.name}: {exc}") from None
        with response:
            final = urlparse(response.url).hostname
            html = "html" in response.headers.get("Content-Type", "")
            if response.status_code in (401, 403) or final == EARTHDATA_HOST or html:
                raise EarthdataAuthError(
                    f"Earthdata refused access to {self.name} (HTTP {response.status_code}). Check the "
                    "credentials, and that the ASF data application is authorized in your profile at "
                    "https://urs.earthdata.nasa.gov (sign in once at https://search.asf.alaska.edu to authorize it)."
                )
            if response.status_code != 206:
                raise EarthdataError(f"{self.name}: expected a partial response to a range request, "
                                 f"got HTTP {response.status_code}")
            try:
                size = int(response.headers["Content-Range"].rsplit("/", 1)[1])
            except (KeyError, ValueError):
                raise EarthdataError(f"{self.name}: no file size in the range response") from None
        same_host = final == urlparse(self.url).hostname
        return response.url, (self._auth if same_host else {}), size
