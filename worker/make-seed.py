#!/usr/bin/env python3
"""Export the peak line's history from the local store into D1 seed files.

The Worker computes its own calibration from history it holds, so it needs that
history before its first call means anything. Without a seed it would sit
uncalibrated for a month, and the archive it would have to wait for is exactly
the ground truth that cannot be recovered later.

Only the peak line moves. The market signal series stay local: they are two
orders of magnitude larger and nothing on Cloudflare reads them.

Re-runnable. Every statement is INSERT OR IGNORE, so applying a seed twice is a
no-op rather than a duplicate, which matters because D1 counts attempted writes
against a daily budget.

    python3 worker/make-seed.py
    for f in worker/seed/*.sql; do wrangler d1 execute signal-arbitrage --remote --file="$f"; done
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "worker" / "seed"

SOURCES = (
    "on_ieso_demand_actual",
    "on_ieso_demand_forecast",
    "on_ieso_demand_forecast_ahead",
    "on_ieso_ici_peak_tracker",
)

ROWS_PER_STATEMENT = 100      # keeps each statement well under any parser limit
ROWS_PER_FILE = 4000          # and each file small enough to send in one call


def quote(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def statements(table: str, columns: tuple[str, ...], rows) -> list[str]:
    out = []
    for start in range(0, len(rows), ROWS_PER_STATEMENT):
        chunk = rows[start:start + ROWS_PER_STATEMENT]
        values = ",".join("(" + ",".join(quote(v) for v in row) + ")" for row in chunk)
        out.append(
            f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) VALUES {values};"
        )
    return out


def main() -> int:
    db = ROOT / "data" / "signals.db"
    if not db.exists():
        print(f"no local store at {db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    placeholders = ",".join("?" * len(SOURCES))
    observations = conn.execute(
        f"SELECT source_id, series_key, observed_at, value FROM observations "
        f"WHERE source_id IN ({placeholders}) ORDER BY source_id, observed_at",
        SOURCES,
    ).fetchall()
    calls = conn.execute(
        "SELECT region_code, delivery_date, horizon_bucket, made_at, threshold_mw, "
        "forecast_mw, peak_hour_utc, probability, pairs, days, actual_mw, cleared, "
        "settled_at FROM peak_calls ORDER BY delivery_date"
    ).fetchall()
    conn.close()

    lines = statements(
        "observations", ("source_id", "series_key", "observed_at", "value"), observations,
    ) + statements(
        "peak_calls",
        ("region_code", "delivery_date", "horizon_bucket", "made_at", "threshold_mw",
         "forecast_mw", "peak_hour_utc", "probability", "pairs", "days", "actual_mw",
         "cleared", "settled_at"),
        calls,
    )

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    per_file = max(1, ROWS_PER_FILE // ROWS_PER_STATEMENT)
    written = 0
    for index in range(0, len(lines), per_file):
        path = OUT / f"{index // per_file + 1:03d}.sql"
        path.write_text("\n".join(lines[index:index + per_file]) + "\n")
        written += 1

    print(f"{len(observations)} observation(s) and {len(calls)} call(s) "
          f"in {written} file(s) under {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
