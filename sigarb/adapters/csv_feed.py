"""CSV adapter. Handles preamble rows, wide and long layouts, and event rows."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..sources import Source
from .. import timeutil
from .base import ParseResult, event_row, fill_template, observation_row, to_float


def parse(source: Source, body: bytes, payload_id: int | None) -> ParseResult:
    result = ParseResult()
    spec = source.parse
    encoding = spec.get("encoding", "utf-8")
    text = body.decode(encoding, errors="replace")
    text_full = text

    # Some agency exports carry title lines above the real header.
    skip_rows = int(spec.get("skip_rows", 0))
    if skip_rows:
        lines = text.splitlines()
        text = "\n".join(lines[skip_rows:])

    reader = csv.DictReader(
        io.StringIO(text),
        delimiter=str(spec.get("delimiter", ",")),
        fieldnames=spec.get("fieldnames"),
    )

    tz = _tz(spec.get("timezone"))
    mode = spec.get("mode", "timeseries")
    rows = list(reader)
    if not rows:
        result.problems.append("CSV had no data rows")
        return result

    if mode == "events":
        _events(source, spec, rows, payload_id, result, tz)
    else:
        anchor = _anchor(text_full, spec, tz)
        _timeseries(source, spec, rows, payload_id, result, tz, anchor)
    return result


def _anchor(raw: str, spec: dict[str, Any], tz: Any) -> datetime | None:
    """Some exports carry the date only in a preamble line, not in a column."""
    pattern = spec.get("date_from_preamble")
    if not pattern:
        return None
    match = re.search(str(pattern), raw)
    if not match:
        return None
    from .. import timeutil

    moment = timeutil.parse(match.group(1), assume_tz=tz)
    if moment is None:
        return None
    return moment.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)


def _timeseries(
    source: Source, spec: dict[str, Any], rows: list[dict[str, Any]],
    payload_id: int | None, result: ParseResult, tz: Any, anchor: datetime | None = None,
) -> None:
    time_column = spec.get("time_column")
    hour_column = spec.get("hour_column")
    interval_column = spec.get("interval_column")
    interval_minutes = int(spec.get("interval_minutes", 5))
    interval_base = int(spec.get("interval_base", 1))
    hour_base = int(spec.get("hour_base", 1))
    value_column = spec.get("value_column")
    value_columns: dict[str, str] = spec.get("value_columns") or {}
    series_column = spec.get("series_column")
    series_literal = spec.get("series_literal") or source.source_id
    unit = spec.get("unit")
    currency = spec.get("currency")

    for row in rows:
        clean = {(k or "").strip(): v for k, v in row.items() if k}
        if time_column:
            observed_at = timeutil.parse_iso(clean.get(time_column), assume_tz=tz)
        else:
            observed_at = _from_interval(
                clean, anchor, hour_column, interval_column,
                interval_minutes, interval_base, hour_base,
                spec.get("date_column"), tz,
            )
        if observed_at is None:
            continue

        if value_columns:
            for series_key, column in value_columns.items():
                value = to_float(clean.get(column))
                if value is not None:
                    result.observations.append(
                        observation_row(
                            source_id=source.source_id, series_key=series_key,
                            observed_at=observed_at, value=value,
                            region_code=source.region_code, payload_id=payload_id,
                            unit=unit, currency=currency,
                        )
                    )
            continue

        value = to_float(clean.get(value_column))
        if value is None:
            continue
        series_key = clean.get(series_column) if series_column else series_literal
        result.observations.append(
            observation_row(
                source_id=source.source_id, series_key=str(series_key or series_literal),
                observed_at=observed_at, value=value,
                region_code=source.region_code, payload_id=payload_id,
                unit=unit, currency=currency,
            )
        )


def _events(
    source: Source, spec: dict[str, Any], rows: list[dict[str, Any]],
    payload_id: int | None, result: ParseResult, tz: Any,
) -> None:
    id_column = spec.get("id_column")
    title_column = spec.get("title_column")
    time_column = spec.get("time_column")
    summary_column = spec.get("summary_column")
    url_template = spec.get("url_template")

    for row in rows:
        clean = {(k or "").strip(): v for k, v in row.items() if k}
        title = clean.get(title_column)
        if not title:
            continue
        external_id = clean.get(id_column) or title
        result.events.append(
            event_row(
                source_id=source.source_id, external_id=str(external_id),
                title=str(title), region_code=source.region_code,
                payload_id=payload_id,
                published_at=timeutil.parse_iso(clean.get(time_column), assume_tz=tz),
                summary=clean.get(summary_column),
                url=fill_template(url_template, clean) if url_template else None,
            )
        )


def _tz(name: Any):
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(str(name))
    except Exception:
        return timezone.utc


def _from_interval(
    row: dict[str, Any], anchor: datetime | None, hour_column: str | None,
    interval_column: str | None, interval_minutes: int, interval_base: int, hour_base: int,
    date_column: str | None = None, tz: Any = None,
) -> str | None:
    """Rebuild a timestamp from an hour-ending column plus an interval index.

    A yearly archive names its date per row rather than in a preamble, so the
    anchor can come from the row itself.
    """
    from .. import timeutil

    if date_column:
        parsed = timeutil.parse(row.get(date_column), assume_tz=tz or timezone.utc)
        if parsed is None:
            return None
        moment = parsed.astimezone(tz or timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif anchor is not None:
        moment = anchor
    else:
        return None

    if hour_column:
        hour = to_float(row.get(hour_column))
        if hour is None:
            return None
        moment = moment + timedelta(hours=int(hour) - hour_base)
    if interval_column:
        interval = to_float(row.get(interval_column))
        if interval is None:
            return None
        moment = moment + timedelta(minutes=(int(interval) - interval_base) * interval_minutes)
    return timeutil.iso(moment)
