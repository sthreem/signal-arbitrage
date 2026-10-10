-- The peak line's store, on D1.
--
-- Only what the peak call needs. The market signal detectors stay on the Mac:
-- they run scikit-learn over three hundred thousand observations, which is not
-- a shape a Worker can hold.
--
-- Idempotency works exactly as it does locally: observations key on
-- (source, series, time) and calls key on (region, day, horizon), so a repeated
-- cron cycle is a no-op rather than a duplicate. Cron delivery is at-least-once,
-- so this is load-bearing, not tidiness.

CREATE TABLE IF NOT EXISTS observations (
    source_id   TEXT NOT NULL,
    series_key  TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    value       REAL NOT NULL,
    PRIMARY KEY (source_id, series_key, observed_at)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_obs_time ON observations (observed_at);

-- One row per call, written when the call is made and never revised. The first
-- call at each horizon is the one that stands: a later, easier call is a
-- different horizon and gets its own row.
CREATE TABLE IF NOT EXISTS peak_calls (
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
    PRIMARY KEY (region_code, delivery_date, horizon_bucket)
);

CREATE INDEX IF NOT EXISTS idx_calls_open ON peak_calls (settled_at, delivery_date);

-- Every cycle, so the dashboard can show that the engine is alive rather than
-- merely showing stale numbers that look fine.
CREATE TABLE IF NOT EXISTS cycles (
    ran_at   TEXT PRIMARY KEY,
    ok       INTEGER NOT NULL,
    ms       INTEGER,
    fetched  INTEGER NOT NULL DEFAULT 0,
    written  INTEGER NOT NULL DEFAULT 0,
    detail   TEXT
);

-- Failed logins per address, so guessing the password is rate limited. Rows
-- carry their own expiry rather than being swept: the table is read on every
-- attempt and an expired row is treated as absent.
CREATE TABLE IF NOT EXISTS login_failures (
    ip         TEXT PRIMARY KEY,
    n          INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

-- What the relay knows about each source it carries to R2.
--
-- One row per source, and the only state the relay keeps. It holds the content
-- hash of the last payload stored, which is what decides whether a fetched
-- payload is new, and the etag beside it, recorded in the same breath as the
-- object it describes so a 304 can be believed. `checked_at` moves every cycle
-- and `stored_at` only when bytes changed, which is how the dashboard tells a
-- broken relay from a source that simply has not published.
CREATE TABLE IF NOT EXISTS relay_state (
    source_id     TEXT PRIMARY KEY,
    content_hash  TEXT,
    object_key    TEXT,
    bytes         INTEGER,
    stored_at     TEXT,
    checked_at    TEXT,
    etag          TEXT,
    last_modified TEXT,
    objects       INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    last_error_at TEXT
);
