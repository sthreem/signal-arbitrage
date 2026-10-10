"""Detector registry. Numeric detectors read observations; novelty reads events."""

from __future__ import annotations

from .anomaly import isolation_forest
from .base import Detection
from .novelty import novelty
from .statistical import level_threshold, pct_change, zscore

NUMERIC = {
    "zscore": zscore,
    "pct_change": pct_change,
    "level_threshold": level_threshold,
    "isolation_forest": isolation_forest,
}

EVENT = {
    "novelty": novelty,
}

ALL = sorted(set(NUMERIC) | set(EVENT))

__all__ = ["Detection", "NUMERIC", "EVENT", "ALL"]
