"""HTTP fetching: polite, schedule-aware, and lossless.

Three rules shape this module.

1. Nothing is fetched when nothing new is due, unless forced.
2. Every response body is written to disk verbatim before anything parses it.
3. The engine is a guest: one request per second per host, conditional requests
   where the server supports them, and robots.txt honoured where it applies.
"""

from __future__ import annotations

import gzip
import hashlib
import time
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import config, timeutil
from .sources import Source


class FetchError(RuntimeError):
    """A fetch that failed in a way the caller should record, not crash on."""


@dataclass
class FetchResult:
    source_id: str
    url: str
    status: int
    body: bytes
    media_type: str | None
    content_hash: str
    path: Path
    fetched_at: str
    is_new_content: bool
    from_cache: bool = False
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None


_last_request_at: dict[str, float] = {}
_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def _throttle(host: str) -> None:
    """One request per host per MIN_SECONDS_BETWEEN_REQUESTS_PER_HOST."""
    last = _last_request_at.get(host)
    if last is not None:
        wait = config.MIN_SECONDS_BETWEEN_REQUESTS_PER_HOST - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_request_at[host] = time.monotonic()


def _robots_allows(url: str, user_agent: str) -> bool:
    """True if robots.txt permits, or if it cannot be read (fail open, politely)."""
    parts = urlparse(url)
    host_key = f"{parts.scheme}://{parts.netloc}"
    if host_key not in _robots_cache:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{host_key}/robots.txt")
        try:
            _throttle(parts.netloc)
            response = requests.get(
                f"{host_key}/robots.txt",
                timeout=10,
                headers={"User-Agent": user_agent},
            )
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
                _robots_cache[host_key] = parser
            else:
                _robots_cache[host_key] = None
        except requests.RequestException:
            _robots_cache[host_key] = None
    parser = _robots_cache[host_key]
    return True if parser is None else parser.can_fetch(user_agent, url)


def is_due(source: Source, last_success_at: str | None, *, force: bool = False) -> bool:
    """No needless work: skip when the cadence says nothing new exists yet."""
    if force or last_success_at is None:
        return True
    age = timeutil.age_seconds(last_success_at)
    return age is None or age >= source.update_frequency_s


def _payload_path(source_id: str, fetched_at: str, content_hash: str, media_type: str | None) -> Path:
    ext = "bin"
    if media_type:
        lowered = media_type.lower()
        for needle, candidate in (
            ("json", "json"), ("xml", "xml"), ("csv", "csv"),
            ("html", "html"), ("plain", "txt"),
        ):
            if needle in lowered:
                ext = candidate
                break
    day = fetched_at[:10].replace("-", "")
    directory = config.raw_dir() / source_id / day
    directory.mkdir(parents=True, exist_ok=True)
    stamp = fetched_at.replace(":", "").replace("-", "")
    return directory / f"{stamp}-{content_hash[:12]}.{ext}.gz"


def store_raw(source_id: str, body: bytes, media_type: str | None, fetched_at: str) -> tuple[Path, str, bool]:
    """Write the payload verbatim, gzipped, content-addressed. (path, hash, is_new)."""
    content_hash = hashlib.sha256(body).hexdigest()
    path = _payload_path(source_id, fetched_at, content_hash, media_type)
    if path.exists():
        return path, content_hash, False
    # Atomic: write beside, then replace, so a reader never sees a half file.
    temp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temp, "wb") as handle:
        handle.write(body)
    temp.replace(path)
    return path, content_hash, True


def read_raw(path: Path | str) -> bytes:
    """Read a stored payload back, so the store can be rebuilt from raw."""
    with gzip.open(Path(path), "rb") as handle:
        return handle.read()


def fetch(
    source: Source,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    session: requests.Session | None = None,
) -> FetchResult:
    """Fetch one source. Raises FetchError; never lets an exception escape raw."""
    now = timeutil.now_utc()
    url = source.resolved_url(now)
    params = source.resolved_params(now)
    method = str(source.request.get("method", "GET")).upper()
    body_spec = source.request.get("json")
    respect_robots = bool(source.request.get("respect_robots", source.adapter in {"csv_feed", "xml_feed"}))

    headers = {"User-Agent": config.USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    headers.update({str(k): str(v) for k, v in (source.request.get("headers") or {}).items()})
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    if respect_robots and not _robots_allows(url, config.USER_AGENT):
        raise FetchError(f"robots.txt disallows {url}")

    http = session or requests.Session()
    host = urlparse(url).netloc
    last_error: Exception | None = None

    for attempt in range(config.MAX_RETRIES):
        try:
            _throttle(host)
            response = http.request(
                method,
                url,
                params=params or None,
                json=body_spec,
                headers=headers,
                timeout=config.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(config.RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue

        fetched_at = timeutil.now_iso()

        if response.status_code == 304:
            return FetchResult(
                source_id=source.source_id, url=url, status=304, body=b"",
                media_type=None, content_hash="", path=Path(),
                fetched_at=fetched_at, is_new_content=False, not_modified=True,
                etag=etag, last_modified=last_modified,
            )

        if response.status_code in (429, 500, 502, 503, 504):
            last_error = FetchError(f"HTTP {response.status_code} from {url}")
            time.sleep(config.RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue

        if not response.ok:
            raise FetchError(f"HTTP {response.status_code} from {url}")

        media_type = response.headers.get("Content-Type")
        path, content_hash, is_new = store_raw(
            source.source_id, response.content, media_type, fetched_at
        )
        return FetchResult(
            source_id=source.source_id, url=response.url, status=response.status_code,
            body=response.content, media_type=media_type, content_hash=content_hash,
            path=path, fetched_at=fetched_at, is_new_content=is_new,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )

    raise FetchError(f"giving up on {url} after {config.MAX_RETRIES} attempts: {last_error}")
