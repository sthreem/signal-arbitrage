"""Exports: portable, diff-friendly, and rebuilt atomically.

CSV is the committed record because it diffs; JSON is the machine shape and
matches exactly what an API client would receive.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Sequence

from . import config, db
from .signals import as_payload

SIGNAL_COLUMNS = [
    "signal_id", "region_code", "detector", "source_id", "series_key",
    "observed_at", "fired_at", "headline", "magnitude", "direction",
    "confidence", "licence_scope", "disclaimer_flags", "plain_english",
]


def _atomic_write(path: Path, write) -> Path:
    """Write beside the target, then os.replace, so readers never see a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            write(handle)
        os.replace(handle.name, path)
    except Exception:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return path


def signals_csv(conn: sqlite3.Connection, path: Path | None = None, *, limit: int = 10000) -> Path:
    target = Path(path) if path else config.EXPORTS_DIR / "signals.csv"
    rows = list(
        conn.execute(
            "SELECT * FROM signals ORDER BY fired_at DESC, signal_id LIMIT ?", (limit,)
        )
    )

    def write(handle: Any) -> None:
        writer = csv.DictWriter(handle, fieldnames=SIGNAL_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            record = {key: row[key] for key in row.keys() if key in SIGNAL_COLUMNS}
            record["disclaimer_flags"] = "|".join(json.loads(row["disclaimer_json"]))
            writer.writerow(record)

    return _atomic_write(target, write)


def signals_json(conn: sqlite3.Connection, path: Path | None = None, *, limit: int = 1000) -> Path:
    target = Path(path) if path else config.EXPORTS_DIR / "signals.json"
    rows = list(
        conn.execute(
            "SELECT * FROM signals ORDER BY fired_at DESC, signal_id LIMIT ?", (limit,)
        )
    )
    payloads = [as_payload(row) for row in rows]

    def write(handle: Any) -> None:
        json.dump(
            {"version": "v1", "count": len(payloads), "signals": payloads},
            handle,
            indent=2,
            ensure_ascii=False,
        )

    return _atomic_write(target, write)


def observations_csv(conn: sqlite3.Connection, path: Path | None = None, *, limit: int = 200000) -> Path:
    target = Path(path) if path else config.EXPORTS_DIR / "observations.csv"
    rows = list(
        conn.execute(
            "SELECT source_id, series_key, observed_at, value, unit, currency, region_code "
            "FROM observations ORDER BY source_id, series_key, observed_at LIMIT ?",
            (limit,),
        )
    )

    def write(handle: Any) -> None:
        writer = csv.writer(handle)
        writer.writerow(
            ["source_id", "series_key", "observed_at", "value", "unit", "currency", "region_code"]
        )
        writer.writerows([tuple(row) for row in rows])

    return _atomic_write(target, write)


def write_all(conn: sqlite3.Connection) -> list[Path]:
    return [signals_csv(conn), signals_json(conn), observations_csv(conn)]
