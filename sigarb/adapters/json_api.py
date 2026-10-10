"""JSON REST adapter: time series (wide or long) and event lists."""

from __future__ import annotations

import json
from typing import Any

from ..sources import Source
from .base import ParseResult, dig, event_row, fill_template, observation_row, to_float
from .. import timeutil


def parse(source: Source, body: bytes, payload_id: int | None) -> ParseResult:
    result = ParseResult()
    try:
        # Some feeds embed raw control characters in description strings.
        document = json.loads(body.decode("utf-8", errors="replace"), strict=False)
    except json.JSONDecodeError as exc:
        result.problems.append(f"invalid JSON: {exc}")
        return result

    spec = source.parse
    records = dig(document, spec.get("records"), [])
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        result.problems.append(
            f"records path {spec.get('records')!r} did not yield a list"
        )
        return result

    mode = spec.get("mode", "timeseries")
    if mode == "timeseries":
        _timeseries(source, spec, records, payload_id, result)
    elif mode == "events":
        _events(source, spec, records, payload_id, result)
    else:
        result.problems.append(f"unknown parse.mode {mode!r}")
    return result


def _timeseries(
    source: Source, spec: dict[str, Any], records: list[Any], payload_id: int | None, result: ParseResult
) -> None:
    time_field = spec.get("time_field")
    assume_tz = spec.get("timezone")
    unit = spec.get("unit")
    currency = spec.get("currency")
    # Wide format: one record holds several named series.
    value_fields: dict[str, str] = spec.get("value_fields") or {}
    # Long format: one record is one point, series named by a field or a literal.
    value_field = spec.get("value_field")
    series_field = spec.get("series_field")
    series_literal = spec.get("series_literal")

    for record in records:
        if not isinstance(record, dict):
            continue
        observed_at = timeutil.parse_iso(dig(record, time_field), assume_tz=_tz(assume_tz))
        if observed_at is None:
            result.problems.append(f"unparseable timestamp in field {time_field!r}")
            continue

        if value_fields:
            for series_key, path in value_fields.items():
                value = to_float(dig(record, path))
                if value is None:
                    continue
                result.observations.append(
                    observation_row(
                        source_id=source.source_id, series_key=series_key,
                        observed_at=observed_at, value=value,
                        region_code=source.region_code, payload_id=payload_id,
                        unit=unit, currency=currency,
                    )
                )
            continue

        value = to_float(dig(record, value_field))
        if value is None:
            continue
        series_key = str(dig(record, series_field) or series_literal or source.source_id)
        result.observations.append(
            observation_row(
                source_id=source.source_id, series_key=series_key,
                observed_at=observed_at, value=value,
                region_code=source.region_code, payload_id=payload_id,
                unit=unit, currency=currency,
            )
        )


def _events(
    source: Source, spec: dict[str, Any], records: list[Any], payload_id: int | None, result: ParseResult
) -> None:
    id_field = spec.get("id_field", "id")
    title_field = spec.get("title_field", "title")
    time_field = spec.get("time_field")
    summary_field = spec.get("summary_field")
    url_field = spec.get("url_field")
    url_template = spec.get("url_template")
    assume_tz = _tz(spec.get("timezone"))

    for record in records:
        if not isinstance(record, dict):
            continue
        external_id = dig(record, id_field)
        title = dig(record, title_field)
        if external_id in (None, "") or title in (None, ""):
            continue
        url = fill_template(url_template, record) if url_template else dig(record, url_field)
        result.events.append(
            event_row(
                source_id=source.source_id, external_id=str(external_id),
                title=str(title), region_code=source.region_code,
                payload_id=payload_id,
                published_at=timeutil.parse_iso(dig(record, time_field), assume_tz=assume_tz),
                summary=_stringify(dig(record, summary_field)),
                url=url,
            )
        )


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _tz(name: Any):
    from datetime import timezone
    from zoneinfo import ZoneInfo

    if not name:
        return timezone.utc
    try:
        return ZoneInfo(str(name))
    except Exception:
        return timezone.utc
