"""Turning detections into signals.

Invariant 4: no signal leaves this module without provenance, a confidence
score, disclaimer flags and a licence scope. The disclaimer flags are the
mechanism from the concept: the schema carries flags, and one table maps flag
to text, so no signal ever needs individual legal review.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

from . import config, db, timeutil
from .detect import Detection
from .sources import Source

# Detectors that infer rather than simply check a rule.
MODEL_DETECTORS = {"zscore", "pct_change", "isolation_forest"}

_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(text: str | None, limit: int = 28) -> str:
    if not text:
        return "all"
    return _SLUG.sub("_", str(text).lower()).strip("_")[:limit] or "all"


def signal_id(source: Source, detection: Detection) -> str:
    """Deterministic and readable, so re-detection is an idempotent no-op.

    The trailing hash covers everything that distinguishes one finding from
    another, including the magnitude, so a corrected value produces a new
    signal rather than silently overwriting the old one.
    """
    stamp = re.sub(r"[^0-9TZ]", "", detection.observed_at or timeutil.now_iso())
    fingerprint = db.json_dumps(
        {
            "source": source.source_id,
            "detector": detection.detector,
            "series": detection.series_key,
            "observed_at": detection.observed_at,
            "magnitude": None if detection.magnitude is None else round(detection.magnitude, 6),
        }
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:8]
    return "_".join(
        [
            source.region_code,
            _slug(source.source_id, 32),
            _slug(detection.detector, 20),
            _slug(detection.series_key, 24),
            stamp,
            digest,
        ]
    )


def disclaimer_flags(source: Source, detection: Detection) -> list[str]:
    """Flags are additive and deduplicated; text lives in config.DISCLAIMER_TEXT."""
    flags = {"source_aggregated", "not_financial_advice"}
    if detection.detector in MODEL_DETECTORS:
        flags.add("model_derived")
    if bool(source.raw.get("scheduled_release", False)):
        flags.add("scheduled_release")
    if source.licence_scope in ("unknown", "non_commercial_only"):
        flags.add("redistribution_unresolved")
    flags.update(detection.flags)
    return sorted(flags)


def build_row(source: Source, detection: Detection) -> dict[str, Any]:
    """One signal, ready for the store."""
    provenance = [
        {
            "source_id": source.source_id,
            "source_name": source.name,
            "licence": source.licence,
            "licence_scope": source.licence_scope,
            "region_code": source.region_code,
            "url": source.request.get("url"),
        }
    ]
    return {
        "signal_id": signal_id(source, detection),
        "source_id": source.source_id,
        "detector": detection.detector,
        "series_key": detection.series_key,
        "region_code": source.region_code,
        "observed_at": detection.observed_at,
        "fired_at": timeutil.now_iso(),
        "headline": detection.headline,
        "plain_english": detection.plain_english,
        "magnitude": detection.magnitude,
        "direction": detection.direction,
        "confidence": float(detection.confidence),
        "provenance_json": db.json_dumps(provenance),
        "disclaimer_json": db.json_dumps(disclaimer_flags(source, detection)),
        "licence_scope": source.licence_scope,
        "details_json": db.json_dumps(detection.details),
    }


def build_rows(source: Source, detections: Iterable[Detection]) -> list[dict[str, Any]]:
    return [build_row(source, d) for d in detections]


def as_payload(row: Any) -> dict[str, Any]:
    """The public shape: what an API client or an export would receive."""
    import json

    return {
        "signal_id": row["signal_id"],
        "region_code": row["region_code"],
        "detector": row["detector"],
        "series_key": row["series_key"],
        "headline": row["headline"],
        "explanation": row["plain_english"],
        "magnitude": row["magnitude"],
        "direction": row["direction"],
        "confidence_score": row["confidence"],
        "observed_at": row["observed_at"],
        "processing_timestamp": row["fired_at"],
        "provenance": json.loads(row["provenance_json"]),
        "disclaimer_flags": json.loads(row["disclaimer_json"]),
        "disclaimers": [
            config.DISCLAIMER_TEXT.get(flag, flag)
            for flag in json.loads(row["disclaimer_json"])
        ],
        "license_scope": row["licence_scope"],
        "details": json.loads(row["details_json"]),
    }
