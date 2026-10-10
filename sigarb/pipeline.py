"""The orchestrator: fetch, parse, store, detect, emit.

Every stage is idempotent. Running the whole pipeline twice in a row produces
the same store and no duplicate signals, which is what makes it safe to put on
a cron and forget.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import requests

from . import adapters, db, detect, fetch, signals, timeutil
from .detect import Detection
from .sources import Source

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    return None


@dataclass
class SourceOutcome:
    source_id: str
    status: str                       # fetched | unchanged | skipped | error
    new_observations: int = 0
    new_events: int = 0
    problems: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class RunReport:
    outcomes: list[SourceOutcome] = field(default_factory=list)
    new_signals: int = 0
    signals: list[dict[str, Any]] = field(default_factory=list)

    @property
    def new_observations(self) -> int:
        return sum(o.new_observations for o in self.outcomes)

    @property
    def new_events(self) -> int:
        return sum(o.new_events for o in self.outcomes)

    @property
    def errors(self) -> list[SourceOutcome]:
        return [o for o in self.outcomes if o.status == "error"]


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def sync_sources(conn: sqlite3.Connection, sources: Sequence[Source]) -> None:
    """Mirror the YAML configs into the store, preserving fetch state."""
    for source in sources:
        db.upsert_source(conn, source.as_db_row())
    conn.commit()


def _state(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT last_success_at, etag, last_modified, consecutive_errors "
        "FROM sources WHERE source_id = ?",
        (source_id,),
    ).fetchone()


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------

def ingest_source(
    conn: sqlite3.Connection,
    source: Source,
    *,
    force: bool = False,
    http: requests.Session | None = None,
) -> SourceOutcome:
    state = _state(conn, source.source_id)
    last_success = state["last_success_at"] if state else None

    if not fetch.is_due(source, last_success, force=force):
        return SourceOutcome(source.source_id, "skipped", message="nothing new is due yet")

    attempt_at = timeutil.now_iso()
    conn.execute(
        "UPDATE sources SET last_attempt_at = ? WHERE source_id = ?",
        (attempt_at, source.source_id),
    )

    try:
        result = fetch.fetch(
            source,
            etag=state["etag"] if state else None,
            last_modified=state["last_modified"] if state else None,
            session=http,
        )
    except Exception as exc:  # a bad source must never stop the run
        conn.execute(
            "UPDATE sources SET last_error = ?, consecutive_errors = consecutive_errors + 1 "
            "WHERE source_id = ?",
            (str(exc)[:500], source.source_id),
        )
        conn.commit()
        return SourceOutcome(source.source_id, "error", message=str(exc)[:300])

    if result.not_modified:
        conn.execute(
            "UPDATE sources SET last_success_at = ?, last_error = NULL, "
            "consecutive_errors = 0 WHERE source_id = ?",
            (result.fetched_at, source.source_id),
        )
        conn.commit()
        return SourceOutcome(source.source_id, "unchanged", message="server reported not modified")

    payload_id, payload_is_new = db.record_payload(
        conn,
        source_id=source.source_id,
        content_hash=result.content_hash,
        fetched_at=result.fetched_at,
        media_type=result.media_type,
        byte_len=len(result.body),
        path=str(result.path),
        http_status=result.status,
        request_url=result.url,
    )

    conn.execute(
        "UPDATE sources SET last_success_at = ?, last_error = NULL, consecutive_errors = 0, "
        "etag = ?, last_modified = ? WHERE source_id = ?",
        (result.fetched_at, result.etag, result.last_modified, source.source_id),
    )

    if not payload_is_new:
        conn.commit()
        return SourceOutcome(
            source.source_id, "unchanged", message="identical payload already ingested"
        )

    parsed = adapters.get(source.adapter)(source, result.body, payload_id)
    for row in parsed.observations:
        row["payload_id"] = payload_id
    for row in parsed.events:
        row["payload_id"] = payload_id

    new_obs = db.insert_observations(conn, parsed.observations)
    new_events = db.insert_events(conn, parsed.events)
    conn.commit()

    return SourceOutcome(
        source.source_id,
        "fetched",
        new_observations=new_obs,
        new_events=new_events,
        problems=parsed.problems,
    )


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def detect_source(
    conn: sqlite3.Connection, source: Source, *, backfill: bool = False, event_days: int = 14
) -> list[dict[str, Any]]:
    """Run this source's configured detectors and return signal rows."""
    detections: list[Detection] = []

    for spec in source.detectors:
        options = dict(spec.options)
        if backfill:
            options["only_last"] = False

        if spec.type in detect.NUMERIC:
            detections.extend(_numeric(conn, source, spec.type, options))
        elif spec.type in detect.EVENT:
            detections.extend(_events(conn, source, spec.type, options, event_days))

    return signals.build_rows(source, detections)


def _numeric(
    conn: sqlite3.Connection, source: Source, kind: str, options: dict[str, Any]
) -> list[Detection]:
    function = detect.NUMERIC[kind]
    wanted = options.get("series", "*")
    available = db.distinct_series(conn, source.source_id)
    keys = available if wanted in ("*", None) else [
        key for key in available if key in (wanted if isinstance(wanted, list) else [wanted])
    ]

    # A source can carry hundreds of series of very different character. Scoping
    # by pattern is what keeps a detector aimed at the ones where a move means
    # something, instead of alerting on every peaking unit doing its job.
    pattern = options.get("series_pattern")
    if pattern:
        matcher = re.compile(str(pattern))
        keys = [key for key in keys if matcher.search(key)]

    found: list[Detection] = []
    history = int(options.get("history", 1000))
    for series_key in keys:
        rows = db.series_values(conn, source.source_id, series_key, limit=history)
        if not rows:
            continue
        label = options.get("label") or f"{source.name} · {series_key}"
        found.extend(function(rows, options, series_key=series_key, series_name=label))
    return found


def _events(
    conn: sqlite3.Connection, source: Source, kind: str, options: dict[str, Any], event_days: int
) -> list[Detection]:
    function = detect.EVENT[kind]
    since = timeutil.since(int(options.get("lookback_days", event_days)))
    rows = list(
        conn.execute(
            "SELECT * FROM events WHERE source_id = ? AND first_seen_at >= ? "
            "ORDER BY first_seen_at DESC LIMIT 500",
            (source.source_id, since),
        )
    )
    return function(rows, options, source_name=source.name)


# --------------------------------------------------------------------------
# Whole run
# --------------------------------------------------------------------------

def run(
    conn: sqlite3.Connection,
    sources: Sequence[Source],
    *,
    force: bool = False,
    backfill: bool = False,
    detect_only: bool = False,
    progress: Progress = _noop,
) -> RunReport:
    report = RunReport()
    started = timeutil.now_iso()
    run_id = db.start_run(conn, "backfill" if backfill else "update", started)

    sync_sources(conn, sources)
    http = requests.Session()

    try:
        for source in sources:
            if not source.enabled:
                continue
            if detect_only:
                report.outcomes.append(SourceOutcome(source.source_id, "skipped", message="detect only"))
            else:
                progress(f"fetching {source.name}")
                report.outcomes.append(ingest_source(conn, source, force=force, http=http))

            progress(f"detecting on {source.name}")
            rows = detect_source(conn, source, backfill=backfill)
            inserted = db.insert_signals(conn, rows)
            report.new_signals += inserted
            if inserted:
                ids = [r["signal_id"] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                fresh = conn.execute(
                    f"SELECT * FROM signals WHERE signal_id IN ({placeholders}) "
                    "ORDER BY confidence DESC",
                    ids,
                ).fetchall()
                report.signals.extend(signals.as_payload(row) for row in fresh)
            conn.commit()
    finally:
        http.close()
        db.finish_run(
            conn,
            run_id,
            timeutil.now_iso(),
            sources_ok=sum(1 for o in report.outcomes if o.status in ("fetched", "unchanged")),
            sources_fail=len(report.errors),
            new_obs=report.new_observations,
            new_events=report.new_events,
            new_signals=report.new_signals,
        )
        conn.commit()

    return report


# --------------------------------------------------------------------------
# Disaster recovery: rebuild the derived store from raw payloads alone
# --------------------------------------------------------------------------

def rebuild_from_raw(
    sources: Sequence[Source], *, target: Any = None, progress: Progress = _noop
) -> dict[str, Any]:
    """Rebuild the SQLite store using only the raw blobs on disk.

    This is the drill the baseline asks for: it never reads the existing
    database, so it proves the raw payloads really are the source of truth. The
    new store is built beside the old one and swapped in atomically.
    """
    import os
    import tempfile
    from pathlib import Path

    from . import config as _config

    by_id = {s.source_id: s for s in sources}
    raw_root = _config.raw_dir()
    live = Path(target) if target else _config.db_path()

    handle = tempfile.NamedTemporaryFile(
        suffix=".db", dir=live.parent, delete=False
    )
    handle.close()
    scratch = Path(handle.name)
    scratch.unlink(missing_ok=True)

    totals = {"payloads": 0, "observations": 0, "events": 0, "skipped_sources": 0}

    conn = db.connect(scratch)
    try:
        db.init(conn)
        sync_sources(conn, sources)

        blobs = sorted(raw_root.glob("*/*/*.gz")) if raw_root.exists() else []
        for blob in blobs:
            source_id = blob.parent.parent.name
            source = by_id.get(source_id)
            if source is None:
                totals["skipped_sources"] += 1
                continue

            body = fetch.read_raw(blob)
            content_hash = hashlib.sha256(body).hexdigest()
            fetched_at = _fetched_at_from_blob(blob)
            payload_id, is_new = db.record_payload(
                conn,
                source_id=source_id,
                content_hash=content_hash,
                fetched_at=fetched_at,
                media_type=None,
                byte_len=len(body),
                path=str(blob),
                http_status=None,
                request_url=str(source.request.get("url")),
            )
            if not is_new:
                continue
            totals["payloads"] += 1

            parsed = adapters.get(source.adapter)(source, body, payload_id)
            for row in parsed.observations:
                row["payload_id"] = payload_id
            for row in parsed.events:
                row["payload_id"] = payload_id
            totals["observations"] += db.insert_observations(conn, parsed.observations)
            totals["events"] += db.insert_events(conn, parsed.events)
            progress(f"rebuilt {blob.name}")
        conn.commit()
    finally:
        conn.close()

    os.replace(scratch, live)
    return totals


def _fetched_at_from_blob(blob: Any) -> str:
    """Recover the fetch time from the filename we wrote, or fall back to mtime."""
    stamp = blob.name.split("-")[0]
    try:
        moment = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return timeutil.iso(moment)
    except ValueError:
        return timeutil.iso(datetime.fromtimestamp(blob.stat().st_mtime, tz=timezone.utc))
