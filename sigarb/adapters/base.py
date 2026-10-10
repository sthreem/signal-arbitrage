"""Shared adapter plumbing: the result type and path/value helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .. import timeutil


@dataclass
class ParseResult:
    """What one payload yielded. Adapters never write; the pipeline does."""

    observations: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.observations) + len(self.events)


_INDEX = re.compile(r"^(.*?)\[(\d+)\]$")


def dig(data: Any, path: str | None, default: Any = None) -> Any:
    """Dotted-path lookup with optional list indexing: 'a.b[0].c'.

    An empty path returns the object itself, which is how a top-level JSON list
    is addressed.
    """
    if not path:
        return data
    current = data
    for part in str(path).split("."):
        if current is None:
            return default
        match = _INDEX.match(part)
        index: int | None = None
        if match:
            part, index = match.group(1), int(match.group(2))
        if part:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return default
        if index is not None:
            if isinstance(current, (list, tuple)) and -len(current) <= index < len(current):
                current = current[index]
            else:
                return default
    return current if current is not None else default


def to_float(value: Any) -> float | None:
    """Numbers arrive as strings, with commas, percent signs and blanks."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "").replace("$", "")
    if text in ("", "-", "n/a", "N/A", ".", "null", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fill_template(template: str, record: dict[str, Any]) -> str:
    """'https://x/{name}' against a record dict, leaving unknown keys blank."""

    def sub(match: re.Match[str]) -> str:
        value = dig(record, match.group(1))
        return "" if value is None else str(value)

    return re.sub(r"\{([^}]+)\}", sub, template)


def observation_row(
    *,
    source_id: str,
    series_key: str,
    observed_at: str,
    value: float,
    region_code: str,
    payload_id: int | None,
    unit: str | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "series_key": series_key,
        "observed_at": observed_at,
        "value": value,
        "unit": unit,
        "currency": currency,
        "region_code": region_code,
        "payload_id": payload_id,
        "ingested_at": timeutil.now_iso(),
    }


def event_row(
    *,
    source_id: str,
    external_id: str,
    title: str,
    region_code: str,
    payload_id: int | None,
    published_at: str | None = None,
    summary: str | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "external_id": external_id,
        "published_at": published_at,
        "first_seen_at": timeutil.now_iso(),
        "title": title.strip()[:500],
        "summary": (summary or "").strip()[:4000] or None,
        "url": url,
        "region_code": region_code,
        "payload_id": payload_id,
    }
