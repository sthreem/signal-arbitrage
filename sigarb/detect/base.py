"""Detector plumbing: the Detection record and the confidence heuristic.

Confidence is a documented heuristic, not a calibrated probability. It is the
product of two readable parts:

    strength     how far past its own threshold the observation sits.
                 0.5 exactly at the threshold, rising to 1.0 at twice it.
    sufficiency  how well established the baseline is: observations available
                 divided by the window the detector wanted.

    confidence = strength * (0.6 + 0.4 * sufficiency)

So a breach on a full history scores its raw strength; the same breach on a
quarter of the history is discounted to 70 percent of it. A detector firing on
a thin baseline also attaches the `low_sample` disclaimer flag. Nothing here
claims to be a likelihood, and the README says so in the same words.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Detection:
    """One detector's finding, before it becomes a signal row."""

    detector: str
    series_key: str | None
    observed_at: str
    headline: str
    plain_english: str
    magnitude: float | None = None
    direction: str | None = None
    confidence: float = 0.5
    flags: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def strength(observed: float, threshold: float) -> float:
    """0.5 at the threshold, 1.0 at twice it, never below 0.5 once fired."""
    if threshold <= 0:
        return 0.5
    excess = (abs(observed) - abs(threshold)) / abs(threshold)
    return clamp(0.5 + 0.5 * clamp(excess), 0.5, 1.0)


def sufficiency(available: int, wanted: int) -> float:
    if wanted <= 0:
        return 1.0
    return clamp(available / wanted)


def confidence(observed: float, threshold: float, available: int, wanted: int) -> float:
    suff = sufficiency(available, wanted)
    return round(strength(observed, threshold) * (0.6 + 0.4 * suff), 3)


def sample_flags(available: int, wanted: int) -> list[str]:
    return ["low_sample"] if sufficiency(available, wanted) < 0.5 else []


def direction_of(value: float) -> str:
    return "up" if value > 0 else "down" if value < 0 else "flat"
