"""Pulling the record and the raw payloads back from Cloudflare.

Two things come home here, for two different reasons.

The peak line runs on a Worker now, which means the record of every call it has
made lives in a D1 database and nowhere else. That is a worse exposure than the
one `signal backup` was written for: raw payloads can at least be re-fetched
from the upstream archive for about three months, whereas a call is a claim made
at a moment and there is no archive of it anywhere. Deleting the database, or
losing the account, would destroy the only copy of the thing the product is.

So this is the reverse of `worker/make-seed.py`: Cloudflare to here, on demand,
into the same local tables. From there the ordinary backup carries it off the
machine.

The raw payloads are the other half. The Worker collects the market signal feeds
into R2 and never parses them, because their detectors run scikit-learn over
three hundred thousand observations and that is not a shape a Worker can hold.
So collection is remote and detection is local, and this is the seam. R2 keeps a
bounded window; `data/raw/` here is the durable copy, and `signal backup` is what
carries it off the machine.

Nothing is compressed on the Worker's side, because gzipping the largest payload
would cost roughly a whole invocation's CPU budget on the free plan. It is
gzipped here instead, into exactly the layout `sigarb/fetch.py` writes, so
`signal rebuild` cannot tell a pulled payload from a locally fetched one.

Credentials never touch the repo. The Worker's address and API token live in the
macOS Keychain and are read at runtime.

The token is not the dashboard password. A browser signs in with a password and
gets a session cookie; a script sends `Authorization: Bearer <token>` instead, so
nothing without a browser is ever handed the password and the two can be revoked
separately.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from . import config, fetch, timeutil

KEYCHAIN_SERVICE = "signal-arbitrage"
ACCOUNT_BASE_URL = "worker-url"
ACCOUNT_TOKEN = "worker-api-token"

# How many payloads are downloaded at once. The Worker streams each object
# straight out of R2, so the limit is round trips rather than anything on either
# end, and a day of the five minute feeds is several hundred small objects.
DOWNLOAD_WORKERS = 6

# One listing page. Matches the Worker's own cap.
LIST_PAGE = 1000

# Extension back to a media type, so `fetch.store_raw` rebuilds the same
# filename the Worker's key already carries.
MEDIA_TYPES = {
    "xml": "text/xml",
    "csv": "text/csv",
    "json": "application/json",
    "html": "text/html",
    "txt": "text/plain",
}

# The Worker's own tables, in the order a restore would need them.
PEAK_CALL_COLUMNS = (
    "region_code", "delivery_date", "horizon_bucket", "made_at", "threshold_mw",
    "forecast_mw", "peak_hour_utc", "probability", "pairs", "days",
    "actual_mw", "cleared", "settled_at",
)


class RemoteError(RuntimeError):
    """A pull that failed in a way the caller should report, not crash on."""


@dataclass
class PullReport:
    calls_seen: int = 0
    calls_new: int = 0
    snapshot: str | None = None
    payloads: int = 0
    payload_bytes: int = 0
    sources: int = 0
    problems: list[str] = field(default_factory=list)


def keychain_get(account: str) -> str | None:
    """Read one secret. Returns None rather than raising when it is not set."""
    if shutil.which("security") is None:
        return None
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def keychain_set(account: str, secret: str) -> bool:
    """Write one secret, replacing any existing entry."""
    if shutil.which("security") is None:
        return False
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
         "-a", account, "-w", secret],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def configured() -> bool:
    return bool(keychain_get(ACCOUNT_BASE_URL) and keychain_get(ACCOUNT_TOKEN))


def fetch_state(timeout: int = 30) -> dict:
    """The Worker's whole state, as JSON."""
    base, token = _creds()
    try:
        response = requests.get(
            f"{base}/api",
            headers={
                "User-Agent": config.USER_AGENT,
                "Authorization": f"Bearer {token}",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RemoteError(str(exc)) from exc

    if response.status_code == 401:
        raise RemoteError(
            "401 from the Worker: the API token is wrong or has been rotated. "
            "Run `signal pull --configure` with the current one."
        )
    if not response.ok:
        raise RemoteError(f"HTTP {response.status_code} from the Worker")
    try:
        return response.json()
    except ValueError as exc:
        raise RemoteError(f"the Worker did not return JSON: {exc}") from exc


def _merge_calls(conn: sqlite3.Connection, calls: list[dict]) -> tuple[int, int]:
    """Insert remote calls locally, never overwriting one already here.

    A call is immutable once made, so a conflict means the two stores disagree
    about a claim, which is a problem to look at rather than to resolve by
    overwriting. Settlement is the one field allowed to arrive later, and only
    onto a call that has none.
    """
    rows = [
        tuple(call.get(column) for column in PEAK_CALL_COLUMNS)
        for call in calls
        if call.get("delivery_date") and call.get("horizon_bucket")
    ]
    if not rows:
        return 0, 0

    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO peak_calls ({', '.join(PEAK_CALL_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(PEAK_CALL_COLUMNS))})",
        rows,
    )
    new = conn.total_changes - before

    conn.executemany(
        "UPDATE peak_calls SET actual_mw = ?, cleared = ?, settled_at = ? "
        "WHERE region_code = ? AND delivery_date = ? AND horizon_bucket = ? "
        "AND settled_at IS NULL",
        [
            (call.get("actual_mw"), call.get("cleared"), call.get("settled_at"),
             call.get("region_code", "on"), call["delivery_date"], call["horizon_bucket"])
            for call in calls
            if call.get("settled_at") and call.get("delivery_date")
        ],
    )
    conn.commit()
    return len(rows), new


# --------------------------------------------------------------------------
# The raw payloads
# --------------------------------------------------------------------------

_local = threading.local()
_credentials: tuple[str, str] | None = None


def _creds() -> tuple[str, str]:
    """Base URL and token, read from the Keychain once per process.

    Every `keychain_get` shells out to `security`, and a pull makes one request
    per payload, so reading them per call would cost more than the downloads.
    """
    global _credentials
    if _credentials is None:
        base = keychain_get(ACCOUNT_BASE_URL)
        token = keychain_get(ACCOUNT_TOKEN)
        if not base or not token:
            raise RemoteError(
                "the Worker's address and API token are not in the Keychain. "
                "Run `signal pull --configure` once."
            )
        _credentials = (base.rstrip("/"), token)
    return _credentials


def _session() -> requests.Session:
    """One HTTP session per thread. Sessions are not safe to share."""
    existing = getattr(_local, "session", None)
    if existing is None:
        existing = requests.Session()
        _local.session = existing
    return existing


def _authed(path: str, params: dict | None = None, timeout: int = 60) -> requests.Response:
    base, token = _creds()
    response = _session().get(
        f"{base}{path}",
        params=params,
        headers={"User-Agent": config.USER_AGENT, "Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    if not response.ok:
        raise RemoteError(f"HTTP {response.status_code} from {path}")
    return response


def newest_local_key(source_id: str) -> str | None:
    """The R2 key of the newest payload already here, or None.

    The Worker names objects `raw/<source>/<day>/<stamp>-<hash12>.<ext>` and the
    local file is that tail plus `.gz`, so the newest local filename converts
    straight into a `startAfter` and the sync resumes from itself. That is why
    there is no cursor file: delete `data/raw/` and the next pull refills it.
    """
    directory = config.raw_dir() / source_id
    if not directory.is_dir():
        return None
    newest = None
    for blob in directory.glob("*/*.gz"):
        candidate = (blob.parent.name, blob.name)
        if newest is None or candidate > newest:
            newest = candidate
    if newest is None:
        return None
    day, name = newest
    return f"raw/{source_id}/{day}/{name[:-3]}"


def _fetched_at(key: str) -> str:
    """The fetch time the Worker stamped into the key."""
    stamp = key.rsplit("/", 1)[-1].split("-")[0]
    moment = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return timeutil.iso(moment)


def list_objects(source_id: str, after: str | None) -> list[dict]:
    """Every object for one source newer than `after`, paged."""
    found: list[dict] = []
    cursor = after
    while True:
        params = {"prefix": f"raw/{source_id}/", "limit": LIST_PAGE}
        if cursor:
            params["after"] = cursor
        page = _authed("/objects", params).json()
        objects = page.get("objects") or []
        found.extend(objects)
        if not objects or not page.get("truncated"):
            return found
        cursor = objects[-1]["key"]


def _download(entry: dict) -> tuple[int, str | None]:
    """Fetch one payload and write it into `data/raw/`. (bytes, problem)."""
    key = entry["key"]
    source_id = key.split("/")[1]
    extension = key.rsplit(".", 1)[-1]
    try:
        body = _authed("/object", {"key": key}).content
    except RemoteError as exc:
        return 0, f"{key}: {exc}"

    # The key carries the first twelve characters of the payload's SHA-256, so
    # the download checks itself. A mismatch means the bytes changed in transit
    # or the key lies, and either way this must not enter the raw store, which
    # everything else is derived from.
    digest = hashlib.sha256(body).hexdigest()
    expected = key.rsplit("-", 1)[-1].split(".")[0]
    if not digest.startswith(expected):
        return 0, f"{key}: content hash does not match the key"

    path, _, _ = fetch.store_raw(
        source_id, body, MEDIA_TYPES.get(extension), _fetched_at(key)
    )
    if path.name[:-3] != key.rsplit("/", 1)[-1]:
        return 0, f"{key}: landed as {path.name}, which is not the key's own name"
    return len(body), None


def sync_raw(sources: list[str], report: PullReport, *, full: bool = False) -> None:
    """Bring every payload the Worker holds and this machine does not.

    Incremental by construction: each source resumes from the newest file
    already here, so a pull after a pull is a listing and nothing else. That
    resume point is a high-water mark, though, so a payload missed while older
    ones arrived would never be asked for again. `full` walks the whole window
    instead and is the repair for exactly that.
    """
    wanted: list[dict] = []
    for source_id in sources:
        try:
            objects = list_objects(source_id, None if full else newest_local_key(source_id))
        except RemoteError as exc:
            report.problems.append(f"{source_id}: {exc}")
            continue
        fresh = [o for o in objects if not _already_here(o["key"])]
        if fresh:
            report.sources += 1
        wanted.extend(fresh)

    if not wanted:
        return
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        for size, problem in pool.map(_download, wanted):
            if problem:
                report.problems.append(problem)
            else:
                report.payloads += 1
                report.payload_bytes += size


def _already_here(key: str) -> bool:
    """True if this exact payload is already in the raw store."""
    _, source_id, day, name = key.split("/", 3)
    return (config.raw_dir() / source_id / day / f"{name}.gz").exists()


# --------------------------------------------------------------------------
# The whole pull
# --------------------------------------------------------------------------

def pull(conn: sqlite3.Connection, *, raw: bool = True, full: bool = False) -> PullReport:
    """Bring the remote record home, and leave a dated snapshot beside it."""
    report = PullReport()
    state = fetch_state()

    calls = state.get("calls") or []
    report.calls_seen, report.calls_new = _merge_calls(conn, calls)

    if raw:
        relayed = [row["source_id"] for row in (state.get("relay") or []) if row.get("source_id")]
        if relayed:
            sync_raw(relayed, report, full=full)
        else:
            report.problems.append(
                "the Worker reported no relayed sources, so no payloads were pulled"
            )

    # The snapshot is the belt to the database's braces: plain JSON, in the
    # exports directory, which `signal backup` copies off the machine and which
    # git already versions. One stable filename rather than a dated pile, so the
    # history is a readable diff instead of a directory that only grows.
    config.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.EXPORTS_DIR / "worker-state.json"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True))
    temp.replace(path)
    report.snapshot = str(path)

    for cycle in (state.get("cycles") or [])[:12]:
        if not cycle.get("ok"):
            report.problems.append(f"{cycle.get('ran_at')}: {cycle.get('detail')}")
    return report
