"""RSS and Atom adapter. Every entry is an event; novelty is the signal."""

from __future__ import annotations

import hashlib

import feedparser

from ..sources import Source
from .. import timeutil
from .base import ParseResult, event_row


def parse(source: Source, body: bytes, payload_id: int | None) -> ParseResult:
    result = ParseResult()
    feed = feedparser.parse(body)
    if getattr(feed, "bozo", 0) and not feed.entries:
        result.problems.append(f"feed did not parse: {getattr(feed, 'bozo_exception', 'unknown')}")
        return result

    spec = source.parse
    max_entries = int(spec.get("max_entries", 200))

    for entry in feed.entries[:max_entries]:
        title = entry.get("title") or entry.get("summary") or ""
        if not title:
            continue
        link = entry.get("link")
        external_id = entry.get("id") or entry.get("guid") or link
        if not external_id:
            # Some feeds give neither; hash the stable parts instead of skipping.
            external_id = hashlib.sha256(
                f"{title}|{entry.get('published', '')}".encode("utf-8")
            ).hexdigest()[:32]

        published = (
            entry.get("published")
            or entry.get("updated")
            or entry.get("created")
        )
        summary = entry.get("summary") or entry.get("description")
        result.events.append(
            event_row(
                source_id=source.source_id,
                external_id=str(external_id),
                title=str(title),
                region_code=source.region_code,
                payload_id=payload_id,
                published_at=timeutil.parse_iso(published),
                summary=_strip_html(summary) if summary else None,
                url=link,
            )
        )
    return result


def _strip_html(text: str) -> str:
    import re

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
