"""Backfill history from a dated file archive.

The real-time IESO reports overwrite one rolling file, so a fresh install has
no baseline and every statistical detector sits idle until a day of polling has
gone by. The same reports are also published as dated hourly files in an open
directory listing, which is where the history comes from.

The filename grammar is `PUB_<Report>_<YYYYMMDDHH>[_v<N>].<ext>`. A file with no
version suffix is the final settled version for that hour and wins over any
`_vN` revision; otherwise the highest version wins.

A forecast archive is the exception. There the discarded revisions are the whole
point: the final settled file for a past day was issued after the day was over,
so it records no forecast error at all, while `_v1` for the same day was issued
weeks ahead and `_v90` an hour ahead. A source can set `versions_per_period` to
keep several revisions spread across that range, which is what turns an archive
into a measured error distribution instead of a month of waiting.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Sequence
from urllib.parse import urljoin

import requests

from . import adapters, config, db, fetch, timeutil
from .sources import Source

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    return None


@dataclass
class BackfillReport:
    source_id: str
    considered: int = 0
    fetched: int = 0
    skipped_existing: int = 0
    new_observations: int = 0
    new_events: int = 0
    problems: list[str] | None = None


def supported(source: Source) -> bool:
    spec = source.raw.get("backfill")
    return bool(spec and spec.get("index_url") and spec.get("pattern"))


def _by_period(names: Sequence[str], pattern: str) -> dict[str, list[tuple[int, str]]]:
    """Group matching filenames by period, each as (version rank, filename)."""
    compiled = re.compile(pattern)
    grouped: dict[str, list[tuple[int, str]]] = {}
    for name in names:
        match = compiled.fullmatch(name)
        if not match:
            continue
        period = match.group(1)
        version_text = match.group(2) if match.lastindex and match.lastindex >= 2 else None
        # No version suffix means the final settled file: rank it above all revisions.
        rank = 10**9 if version_text is None else int(version_text)
        grouped.setdefault(period, []).append((rank, name))
    return grouped


def _spread(ranked: list[tuple[int, str]], count: int) -> list[str]:
    """Up to `count` revisions across the version range, weighted to the late ones.

    Version number is not a proxy for time, and assuming it was produced a
    misleading calibration once already. Checked 2026-08-19 on one delivery date
    with 122 revisions: v1 was issued 34 days ahead, v25 twenty-two days, v49 ten
    days, and then v74 to v122 were all issued during the delivery day itself. So
    the archive spends a third of its version numbers on a month of daily
    republication and two thirds on one day of intraday revisions.

    An even spread therefore lands almost entirely on horizons of days to weeks,
    which is not where a curtailment is decided. The square-root weighting pulls
    most picks into the upper range where the same-day revisions live, which is
    where the 0 to 24 hour horizons come from.
    """
    ordered = [name for _, name in sorted(ranked)]
    if count <= 1:
        return ordered[-1:]
    if len(ordered) <= count:
        return ordered
    last = len(ordered) - 1
    picked = {ordered[round(last * ((i / (count - 1)) ** 0.5))] for i in range(count)}
    return sorted(picked, key=ordered.index)


def _pick_best(names: Sequence[str], pattern: str, versions_per_period: int = 1
               ) -> list[tuple[str, str]]:
    """Return [(period_key, filename)], newest period first."""
    grouped = _by_period(names, pattern)
    out: list[tuple[str, str]] = []
    for period in sorted(grouped, reverse=True):
        for name in _spread(grouped[period], versions_per_period):
            out.append((period, name))
    return out


def run(
    conn: sqlite3.Connection,
    source: Source,
    *,
    max_files: int | None = None,
    progress: Progress = _noop,
    http: requests.Session | None = None,
) -> BackfillReport:
    report = BackfillReport(source_id=source.source_id, problems=[])
    spec = source.raw.get("backfill") or {}
    index_url = str(spec["index_url"])
    limit = int(max_files or spec.get("max_files", 48))

    session = http or requests.Session()
    headers = {"User-Agent": config.USER_AGENT}

    progress(f"listing {source.name} archive")
    try:
        listing = session.get(index_url, headers=headers, timeout=config.REQUEST_TIMEOUT)
        listing.raise_for_status()
    except requests.RequestException as exc:
        report.problems.append(f"could not read the archive index: {exc}")
        return report

    names = re.findall(r'href="([^"]+)"', listing.text)
    per_period = int(spec.get("versions_per_period", 1))
    candidates = _pick_best(names, str(spec["pattern"]), per_period)[:limit]
    report.considered = len(candidates)

    known = {
        row["request_url"]
        for row in conn.execute(
            "SELECT request_url FROM raw_payloads WHERE source_id = ?", (source.source_id,)
        )
    }

    for period, name in candidates:
        url = urljoin(index_url, name)
        if url in known:
            report.skipped_existing += 1
            continue

        progress(f"{source.name} {period}")
        try:
            fetch._throttle(url.split("/")[2])
            response = session.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as exc:
            report.problems.append(f"{name}: {exc}")
            continue

        fetched_at = timeutil.now_iso()
        path, content_hash, _ = fetch.store_raw(
            source.source_id, response.content, response.headers.get("Content-Type"), fetched_at
        )
        payload_id, is_new = db.record_payload(
            conn,
            source_id=source.source_id,
            content_hash=content_hash,
            fetched_at=fetched_at,
            media_type=response.headers.get("Content-Type"),
            byte_len=len(response.content),
            path=str(path),
            http_status=response.status_code,
            request_url=url,
        )
        report.fetched += 1
        if not is_new:
            continue

        parsed = adapters.get(source.adapter)(source, response.content, payload_id)
        for row in parsed.observations:
            row["payload_id"] = payload_id
        for row in parsed.events:
            row["payload_id"] = payload_id
        report.new_observations += db.insert_observations(conn, parsed.observations)
        report.new_events += db.insert_events(conn, parsed.events)
        report.problems.extend(parsed.problems)
        conn.commit()

    conn.commit()
    if http is None:
        session.close()
    return report
