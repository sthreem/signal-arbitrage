"""XML adapter, built for interval reports.

Operational market feeds put the date (and often the hour) in a header and give
each row an interval index rather than a timestamp. Some nest a series identity
one level above the readings: a generator name, then that generator's hours.
This adapter handles all three shapes.

Modes
    timeseries          flat records under records_path
    timeseries_nested   groups under group_path, each naming a series, with
                        their own records underneath
    events              flat records treated as discrete items

Two options exist for the reports that do not fit the header-anchored shape.
`date_path` lets a record carry its own delivery date, which the peak trackers
do because their rows are scattered across months. `lead_time_path` points at
the moment the report was issued, and buckets each reading by how far ahead of
its delivery hour that was, so a forecast series can be graded by horizon
instead of collapsing every revision into one line.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from ..sources import Source
from .. import timeutil
from .base import ParseResult, event_row, observation_row, to_float


def parse(source: Source, body: bytes, payload_id: int | None) -> ParseResult:
    result = ParseResult()
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        result.problems.append(f"invalid XML: {exc}")
        return result

    spec = source.parse
    namespaces: dict[str, str] = dict(spec.get("namespaces") or {})
    # A default namespace on the root would make every path fail silently.
    if not namespaces and root.tag.startswith("{"):
        namespaces = {"d": root.tag[1:].split("}")[0]}

    tz = _tz(spec.get("timezone"))
    anchor = _anchor(root, spec, namespaces, tz)
    issued_at = _issued_at(root, spec, namespaces, tz)
    mode = spec.get("mode", "timeseries")

    if mode == "timeseries_nested":
        _nested(source, spec, root, namespaces, payload_id, result, anchor, tz, issued_at)
    elif mode == "events":
        _events(source, spec, root, namespaces, payload_id, result, tz)
    else:
        records = root.findall(spec.get("records_path", "."), namespaces)
        if not records:
            result.problems.append(f"records_path {spec.get('records_path')!r} matched nothing")
            return result
        _flat(source, spec, records, namespaces, payload_id, result, anchor, tz, issued_at,
              series_key=str(spec.get("series_literal") or source.source_id))
    return result


# --------------------------------------------------------------------------

def _flat(
    source: Source, spec: dict[str, Any], records: list[Any], namespaces: dict[str, str],
    payload_id: int | None, result: ParseResult, anchor: datetime | None, tz: Any,
    issued_at: datetime | None = None, *, series_key: str,
) -> None:
    interval_minutes = int(spec.get("interval_minutes", 5))
    interval_base = int(spec.get("interval_base", 1))
    hour_base = int(spec.get("hour_base", 1))
    unit = spec.get("unit")
    currency = spec.get("currency")

    # A record can carry several measures at once (price, congestion, loss).
    value_paths: dict[str, str] = spec.get("value_paths") or {}
    single_path = spec.get("value_path")

    for index, record in enumerate(records):
        observed_at = _timestamp(
            record, spec, namespaces, anchor, tz, interval_minutes, interval_base,
            hour_base, index,
        )
        if observed_at is None:
            result.problems.append("could not build a timestamp for a record")
            continue

        lead = _lead_suffix(observed_at, issued_at, spec)

        if value_paths:
            for suffix, path in value_paths.items():
                value = to_float(_text(record, path, namespaces))
                if value is None:
                    continue
                result.observations.append(
                    observation_row(
                        source_id=source.source_id, series_key=f"{suffix}{lead}",
                        observed_at=observed_at, value=value,
                        region_code=source.region_code, payload_id=payload_id,
                        unit=unit, currency=currency,
                    )
                )
            continue

        value = to_float(_text(record, single_path, namespaces))
        if value is None:
            # Unsettled intervals arrive as empty elements. Skipping is correct;
            # reading them as zero would invent a price crash every hour.
            continue
        key = _text(record, spec.get("series_path"), namespaces) or series_key
        result.observations.append(
            observation_row(
                source_id=source.source_id, series_key=f"{key}{lead}",
                observed_at=observed_at, value=value,
                region_code=source.region_code, payload_id=payload_id,
                unit=unit, currency=currency,
            )
        )


def _nested(
    source: Source, spec: dict[str, Any], root: Any, namespaces: dict[str, str],
    payload_id: int | None, result: ParseResult, anchor: datetime | None, tz: Any,
    issued_at: datetime | None = None,
) -> None:
    groups = root.findall(spec.get("group_path", "."), namespaces)
    if not groups:
        result.problems.append(f"group_path {spec.get('group_path')!r} matched nothing")
        return

    series_path = spec.get("group_series_path")
    label_paths = spec.get("group_label_paths") or []
    records_path = spec.get("records_path", ".")

    for group in groups:
        name = _text(group, series_path, namespaces)
        if not name:
            continue
        labels = [_text(group, p, namespaces) for p in label_paths]
        series_key = "|".join([name] + [str(v) for v in labels if v])

        records = group.findall(records_path, namespaces)
        if not records:
            continue
        _flat(
            source, spec, records, namespaces, payload_id, result, anchor, tz, issued_at,
            series_key=series_key,
        )


def _events(
    source: Source, spec: dict[str, Any], root: Any, namespaces: dict[str, str],
    payload_id: int | None, result: ParseResult, tz: Any,
) -> None:
    records = root.findall(spec.get("records_path", "."), namespaces)
    if not records:
        result.problems.append(f"records_path {spec.get('records_path')!r} matched nothing")
        return
    for record in records:
        title_parts = [
            _text(record, path, namespaces)
            for path in (spec.get("title_paths") or [spec.get("title_path")])
        ]
        title = " · ".join(str(p) for p in title_parts if p)
        if not title:
            continue
        external_id = _text(record, spec.get("id_path"), namespaces) or title
        result.events.append(
            event_row(
                source_id=source.source_id, external_id=str(external_id),
                title=title, region_code=source.region_code, payload_id=payload_id,
                published_at=timeutil.parse_iso(
                    _text(record, spec.get("time_path"), namespaces), assume_tz=tz
                ),
                summary=_text(record, spec.get("summary_path"), namespaces),
                url=_text(record, spec.get("url_path"), namespaces),
            )
        )


# --------------------------------------------------------------------------

def _timestamp(
    record: Any, spec: dict[str, Any], namespaces: dict[str, str], anchor: datetime | None,
    tz: Any, interval_minutes: int, interval_base: int, hour_base: int, index: int,
) -> str | None:
    own_time = _text(record, spec.get("time_path"), namespaces)
    if own_time:
        return timeutil.parse_iso(own_time, assume_tz=tz)

    # A row can name its own delivery date. The peak trackers do, because their
    # rows are the highest hours of a whole season and share no report date.
    own_date = _text(record, spec.get("date_path"), namespaces)
    if own_date:
        parsed = timeutil.parse(own_date, assume_tz=tz)
        if parsed is None:
            return None
        moment = parsed.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    elif anchor is not None:
        moment = anchor
    else:
        return None

    hour_text = _text(record, spec.get("hour_path"), namespaces)
    if hour_text is not None:
        hour = to_float(hour_text)
        if hour is None:
            return None
        # Market hours are hour-ending and 1-indexed: hour 1 covers 00:00 to 01:00.
        moment = moment + timedelta(hours=int(hour) - hour_base)

    interval_path = spec.get("interval_path")
    if interval_path:
        interval = to_float(_text(record, interval_path, namespaces))
        if interval is None:
            return None
        moment = moment + timedelta(minutes=(int(interval) - interval_base) * interval_minutes)

    return timeutil.iso(moment)


def _issued_at(root: Any, spec: dict[str, Any], namespaces: dict[str, str], tz: Any) -> datetime | None:
    """When the report was published, for grading a forecast by how far ahead it was."""
    path = spec.get("lead_time_path")
    if not path:
        return None
    return timeutil.parse(_text(root, path, namespaces), assume_tz=tz)


# Lower bounds, in hours, of the horizon bands a forecast reading is filed under.
DEFAULT_LEAD_BUCKETS = (0, 3, 6, 12, 24, 48)


def _lead_suffix(observed_at: str, issued_at: datetime | None, spec: dict[str, Any]) -> str:
    """`|lead6h` and so on, or empty when the source is not a forecast.

    A forecast issued at 09:00 for 17:00 and the revision issued at 16:00 for the
    same hour are different claims, and collapsing them onto one series key would
    keep whichever arrived first. Filing them by horizon keeps both, and horizon
    is the axis the error distribution has to be measured along anyway.
    """
    if issued_at is None:
        return ""
    moment = timeutil.parse(observed_at)
    if moment is None:
        return ""
    lead_hours = (moment - issued_at).total_seconds() / 3600.0
    buckets = [int(b) for b in (spec.get("lead_time_buckets") or DEFAULT_LEAD_BUCKETS)]
    if lead_hours < buckets[0]:
        return "|leadpast"
    low = buckets[0]
    for bound in buckets:
        if lead_hours >= bound:
            low = bound
    return f"|lead{low}h"


def _anchor(root: Any, spec: dict[str, Any], namespaces: dict[str, str], tz: Any) -> datetime | None:
    """Local midnight of the report date, plus any header-level hour offset."""
    path = spec.get("base_date_path")
    if not path:
        return None
    text = _text(root, path, namespaces)
    if not text:
        return None
    moment = timeutil.parse(text, assume_tz=tz)
    if moment is None:
        return None
    local = moment.astimezone(tz)
    anchor = local.replace(hour=0, minute=0, second=0, microsecond=0)

    base_hour_path = spec.get("base_hour_path")
    if base_hour_path:
        hour = to_float(_text(root, base_hour_path, namespaces))
        if hour is None:
            return None
        anchor = anchor + timedelta(hours=int(hour) - int(spec.get("hour_base", 1)))
    return anchor


def _text(node: Any, path: str | None, namespaces: dict[str, str]) -> str | None:
    if not path:
        return None
    found = node.find(path, namespaces)
    if found is None:
        return None
    return (found.text or "").strip() or None


def _tz(name: Any):
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(str(name))
    except Exception:
        return timezone.utc
