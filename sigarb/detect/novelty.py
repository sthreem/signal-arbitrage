"""Event detector: something newly published matches what we care about.

Novelty is the whole signal here. An event is either new to the store or it is
not, so the interesting work is the filter: firing on everything a government
newsroom publishes is noise, not intelligence.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from .. import timeutil

from .base import Detection, clamp


def novelty(
    events: Sequence[Any], options: dict[str, Any], *, source_name: str
) -> list[Detection]:
    match_any = [str(t).lower() for t in (options.get("match_any") or [])]
    match_regex = [re.compile(str(p), re.IGNORECASE) for p in (options.get("match_regex") or [])]
    exclude = [str(t).lower() for t in (options.get("exclude") or [])]
    fire_on_all = bool(options.get("fire_on_all", False))
    max_age_days = options.get("max_age_days")
    base_confidence = float(options.get("confidence", 0.55))

    detections: list[Detection] = []
    cutoff = timeutil.since(int(max_age_days)) if max_age_days else None

    for event in events:
        # A feed carrying its whole archive would otherwise fire on every old
        # item the first time it is ingested.
        published = event["published_at"] or event["first_seen_at"]
        if cutoff and published and published < cutoff:
            continue

        haystack = f"{event['title']} {event['summary'] or ''}".lower()

        if any(term in haystack for term in exclude):
            continue

        hits = [term for term in match_any if term in haystack]
        hits += [p.pattern for p in match_regex if p.search(haystack)]

        if not hits and not fire_on_all:
            continue

        # Several independent matches is weak evidence of relevance, not strong.
        boost = clamp(0.05 * max(0, len(hits) - 1), 0.0, 0.2)

        detections.append(
            Detection(
                detector="novelty",
                series_key=None,
                observed_at=published,
                headline=str(event["title"])[:200],
                plain_english=(
                    f"{source_name} published this"
                    + (f", matching {', '.join(hits[:4])}" if hits else "")
                    + ". "
                    + (str(event["summary"])[:400] if event["summary"] else "No summary was provided.")
                ),
                magnitude=float(len(hits)),
                direction=None,
                confidence=round(clamp(base_confidence + boost, 0.0, 0.95), 3),
                flags=[],
                details={
                    "external_id": event["external_id"],
                    "url": event["url"],
                    "matched": hits,
                    "published_at": event["published_at"],
                },
            )
        )
    return detections
