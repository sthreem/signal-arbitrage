"""SQLite storage: the canonical derived store.

Raw payloads on disk are the source of truth; every table here can be rebuilt
from them. All writes are idempotent, keyed on natural unique keys, so a
re-run never duplicates.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL
);

-- Registry mirror of sources/*.yaml, plus mutable fetch state.
CREATE TABLE IF NOT EXISTS sources (
    source_id           TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    region_code         TEXT NOT NULL,
    data_type           TEXT NOT NULL,
    adapter             TEXT NOT NULL,
    update_frequency_s  INTEGER NOT NULL,
    licence             TEXT,
    licence_scope       TEXT,
    terms_check         TEXT,
    signal_potential    TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1,
    config_json         TEXT NOT NULL,
    last_attempt_at     TEXT,
    last_success_at     TEXT,
    last_error          TEXT,
    etag                TEXT,
    last_modified       TEXT,
    consecutive_errors  INTEGER NOT NULL DEFAULT 0
);

-- One row per distinct fetched payload. Content-addressed, so an unchanged
-- feed re-fetched costs one row lookup and no new blob.
CREATE TABLE IF NOT EXISTS raw_payloads (
    payload_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL REFERENCES sources(source_id),
    content_hash    TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    media_type      TEXT,
    byte_len        INTEGER NOT NULL,
    path            TEXT NOT NULL,
    http_status     INTEGER,
    request_url     TEXT NOT NULL,
    UNIQUE (source_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_raw_source_time
    ON raw_payloads (source_id, fetched_at);

-- Numeric time series. One row per (series, timestamp).
CREATE TABLE IF NOT EXISTS observations (
    obs_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    TEXT NOT NULL REFERENCES sources(source_id),
    series_key   TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    value        REAL NOT NULL,
    unit         TEXT,
    currency     TEXT,
    region_code  TEXT NOT NULL,
    payload_id   INTEGER REFERENCES raw_payloads(payload_id),
    ingested_at  TEXT NOT NULL,
    UNIQUE (source_id, series_key, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_obs_series_time
    ON observations (source_id, series_key, observed_at);

-- Discrete text events: announcements, filings, recalls, dataset updates.
CREATE TABLE IF NOT EXISTS events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     TEXT NOT NULL REFERENCES sources(source_id),
    external_id   TEXT NOT NULL,
    published_at  TEXT,
    first_seen_at TEXT NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT,
    url           TEXT,
    region_code   TEXT NOT NULL,
    payload_id    INTEGER REFERENCES raw_payloads(payload_id),
    UNIQUE (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_events_seen
    ON events (source_id, first_seen_at);

-- The product. Every row carries provenance, confidence and disclaimers.
CREATE TABLE IF NOT EXISTS signals (
    signal_id         TEXT PRIMARY KEY,
    source_id         TEXT NOT NULL REFERENCES sources(source_id),
    detector          TEXT NOT NULL,
    series_key        TEXT,
    region_code       TEXT NOT NULL,
    observed_at       TEXT NOT NULL,
    fired_at          TEXT NOT NULL,
    headline          TEXT NOT NULL,
    plain_english     TEXT NOT NULL,
    magnitude         REAL,
    direction         TEXT,
    confidence        REAL NOT NULL,
    provenance_json   TEXT NOT NULL,
    disclaimer_json   TEXT NOT NULL,
    licence_scope     TEXT NOT NULL,
    details_json      TEXT NOT NULL,
    delivered_at      TEXT,
    review_verdict    TEXT,
    review_note       TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_fired
    ON signals (fired_at);
CREATE INDEX IF NOT EXISTS idx_signals_region
    ON signals (region_code, fired_at);

-- Audit trail, so a quiet night is distinguishable from a broken engine.
-- Peak calls, written when the call is made and graded later.
--
-- Keyed so the FIRST call at each horizon is what survives. A call made a day
-- ahead and the easier one made an hour ahead are separate rows, and neither can
-- overwrite the other. A record that could be improved after the fact would not
-- be a track record.
CREATE TABLE IF NOT EXISTS peak_calls (
    call_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    region_code    TEXT NOT NULL,
    delivery_date  TEXT NOT NULL,
    horizon_bucket TEXT NOT NULL,
    made_at        TEXT NOT NULL,
    threshold_mw   REAL NOT NULL,
    forecast_mw    REAL NOT NULL,
    peak_hour_utc  TEXT NOT NULL,
    probability    REAL,
    pairs          INTEGER NOT NULL DEFAULT 0,
    days           INTEGER NOT NULL DEFAULT 0,
    actual_mw      REAL,
    cleared        INTEGER,
    settled_at     TEXT,
    UNIQUE (region_code, delivery_date, horizon_bucket)
);

CREATE INDEX IF NOT EXISTS idx_peak_calls_open
    ON peak_calls (settled_at, delivery_date);

CREATE TABLE IF NOT EXISTS runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    kind         TEXT NOT NULL,
    sources_ok   INTEGER NOT NULL DEFAULT 0,
    sources_fail INTEGER NOT NULL DEFAULT 0,
    new_obs      INTEGER NOT NULL DEFAULT 0,
    new_events   INTEGER NOT NULL DEFAULT 0,
    new_signals  INTEGER NOT NULL DEFAULT 0,
    notes        TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the store, creating it if absent."""
    target = Path(path) if path else config.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    """Create the schema. Idempotent."""
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open, initialise, commit on success, always close."""
    conn = connect(path)
    try:
        init(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Idempotent writes
# --------------------------------------------------------------------------

def upsert_source(conn: sqlite3.Connection, spec: dict[str, Any]) -> None:
    """Write the config-derived fields, leaving mutable fetch state alone."""
    conn.execute(
        """
        INSERT INTO sources (
            source_id, name, region_code, data_type, adapter,
            update_frequency_s, licence, licence_scope, terms_check,
            signal_potential, enabled, config_json
        ) VALUES (
            :source_id, :name, :region_code, :data_type, :adapter,
            :update_frequency_s, :licence, :licence_scope, :terms_check,
            :signal_potential, :enabled, :config_json
        )
        ON CONFLICT (source_id) DO UPDATE SET
            name              = excluded.name,
            region_code       = excluded.region_code,
            data_type         = excluded.data_type,
            adapter           = excluded.adapter,
            update_frequency_s= excluded.update_frequency_s,
            licence           = excluded.licence,
            licence_scope     = excluded.licence_scope,
            terms_check       = excluded.terms_check,
            signal_potential  = excluded.signal_potential,
            enabled           = excluded.enabled,
            config_json       = excluded.config_json
        """,
        spec,
    )


def record_payload(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    content_hash: str,
    fetched_at: str,
    media_type: str | None,
    byte_len: int,
    path: str,
    http_status: int | None,
    request_url: str,
) -> tuple[int, bool]:
    """Return (payload_id, is_new). Content-addressed, so unchanged is cheap."""
    existing = conn.execute(
        "SELECT payload_id FROM raw_payloads WHERE source_id = ? AND content_hash = ?",
        (source_id, content_hash),
    ).fetchone()
    if existing:
        return int(existing["payload_id"]), False
    cur = conn.execute(
        """
        INSERT INTO raw_payloads (
            source_id, content_hash, fetched_at, media_type, byte_len,
            path, http_status, request_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id, content_hash, fetched_at, media_type, byte_len,
            path, http_status, request_url,
        ),
    )
    return int(cur.lastrowid), True


def insert_observations(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    """Insert observations, ignoring ones already present. Returns new count."""
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO observations (
            source_id, series_key, observed_at, value, unit, currency,
            region_code, payload_id, ingested_at
        ) VALUES (
            :source_id, :series_key, :observed_at, :value, :unit, :currency,
            :region_code, :payload_id, :ingested_at
        )
        """,
        rows,
    )
    return conn.total_changes - before


def insert_events(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    """Insert events, ignoring ones already seen. Returns new count."""
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO events (
            source_id, external_id, published_at, first_seen_at, title,
            summary, url, region_code, payload_id
        ) VALUES (
            :source_id, :external_id, :published_at, :first_seen_at, :title,
            :summary, :url, :region_code, :payload_id
        )
        """,
        rows,
    )
    return conn.total_changes - before


def insert_signals(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    """Insert signals. The deterministic signal_id makes re-detection a no-op."""
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO signals (
            signal_id, source_id, detector, series_key, region_code,
            observed_at, fired_at, headline, plain_english, magnitude,
            direction, confidence, provenance_json, disclaimer_json,
            licence_scope, details_json
        ) VALUES (
            :signal_id, :source_id, :detector, :series_key, :region_code,
            :observed_at, :fired_at, :headline, :plain_english, :magnitude,
            :direction, :confidence, :provenance_json, :disclaimer_json,
            :licence_scope, :details_json
        )
        """,
        rows,
    )
    return conn.total_changes - before


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def series_values(
    conn: sqlite3.Connection, source_id: str, series_key: str, limit: int | None = None
) -> list[sqlite3.Row]:
    """Observations for one series, oldest first."""
    sql = (
        "SELECT observed_at, value, unit, currency, region_code "
        "FROM observations WHERE source_id = ? AND series_key = ? "
        "ORDER BY observed_at ASC"
    )
    params: list[Any] = [source_id, series_key]
    if limit:
        sql = (
            "SELECT * FROM (" + sql.replace("ASC", "DESC") + " LIMIT ?) "
            "ORDER BY observed_at ASC"
        )
        params.append(limit)
    return list(conn.execute(sql, params))


def distinct_series(conn: sqlite3.Connection, source_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT series_key FROM observations WHERE source_id = ? "
        "ORDER BY series_key",
        (source_id,),
    )
    return [r["series_key"] for r in rows]


def enabled_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM sources WHERE enabled = 1 ORDER BY source_id")
    )


def recent_signals(
    conn: sqlite3.Connection,
    *,
    limit: int = 25,
    region: str | None = None,
    min_confidence: float = 0.0,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM signals WHERE confidence >= ?"
    params: list[Any] = [min_confidence]
    if region:
        sql += " AND region_code = ?"
        params.append(region)
    sql += " ORDER BY fired_at DESC, signal_id LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in ("sources", "raw_payloads", "observations", "events", "signals"):
        out[table] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
    return out


def start_run(conn: sqlite3.Connection, kind: str, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, kind) VALUES (?, ?)", (started_at, kind)
    )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, finished_at: str, **totals: Any) -> None:
    fields = ", ".join(f"{k} = ?" for k in totals)
    params = list(totals.values()) + [finished_at, run_id]
    conn.execute(
        f"UPDATE runs SET {fields}, finished_at = ? WHERE run_id = ?", params
    )


def json_dumps(value: Any) -> str:
    """Stable JSON, so identical content hashes identically."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
