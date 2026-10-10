"""Numeric detectors: rolling z-score, percentage move, absolute level."""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from .. import config
from .base import Detection, confidence, direction_of, sample_flags


def _series(rows: Sequence[Any]) -> tuple[list[str], np.ndarray]:
    times = [str(r["observed_at"]) for r in rows]
    values = np.asarray([float(r["value"]) for r in rows], dtype=float)
    return times, values


def zscore(
    rows: Sequence[Any], options: dict[str, Any], *, series_key: str, series_name: str
) -> list[Detection]:
    """Fire where a point sits far from its own trailing mean.

    The window is trailing and strictly excludes the point being judged, so a
    large move cannot inflate the baseline it is measured against.
    """
    window = int(options.get("window", config.DEFAULT_ZSCORE_WINDOW))
    min_periods = int(options.get("min_periods", config.DEFAULT_ZSCORE_MIN_PERIODS))
    threshold = float(options.get("threshold", config.DEFAULT_ZSCORE_THRESHOLD))
    only_last = bool(options.get("only_last", True))
    # Some moves only matter one way round: a baseload unit losing output is an
    # outage, the same unit coming back is not news.
    only_direction = options.get("direction")

    times, values = _series(rows)
    if len(values) <= min_periods:
        return []

    detections: list[Detection] = []
    start = len(values) - 1 if only_last else min_periods
    for index in range(max(start, min_periods), len(values)):
        baseline = values[max(0, index - window):index]
        if len(baseline) < min_periods:
            continue
        mean = float(baseline.mean())
        sigma = float(baseline.std(ddof=1)) if len(baseline) > 1 else 0.0
        if sigma <= 0:
            continue
        z = (float(values[index]) - mean) / sigma
        if abs(z) < threshold:
            continue
        if only_direction and direction_of(z) != only_direction:
            continue

        move = float(values[index]) - mean
        pct = (move / mean * 100.0) if mean else float("nan")
        detections.append(
            Detection(
                detector="zscore",
                series_key=series_key,
                observed_at=times[index],
                headline=(
                    f"{series_name} is {abs(z):.1f} standard deviations "
                    f"{'above' if z > 0 else 'below'} its recent normal"
                ),
                plain_english=(
                    f"{series_name} came in at {values[index]:,.4g}. Over the previous "
                    f"{len(baseline)} readings it averaged {mean:,.4g}, so this is a move of "
                    f"{move:+,.4g}"
                    + (f" ({pct:+.1f}%)" if pct == pct else "")
                    + f", which is {abs(z):.1f} standard deviations out."
                ),
                magnitude=round(z, 4),
                direction=direction_of(z),
                confidence=confidence(z, threshold, len(baseline), window),
                flags=sample_flags(len(baseline), window),
                details={
                    "value": float(values[index]),
                    "baseline_mean": round(mean, 6),
                    "baseline_stdev": round(sigma, 6),
                    "baseline_n": int(len(baseline)),
                    "window": window,
                    "threshold": threshold,
                    "z": round(z, 4),
                    "pct_vs_baseline": round(pct, 3) if pct == pct else None,
                },
            )
        )
    return detections


def pct_change(
    rows: Sequence[Any], options: dict[str, Any], *, series_key: str, series_name: str
) -> list[Detection]:
    """Fire on a percentage move over a fixed number of readings."""
    periods = int(options.get("periods", 1))
    threshold = float(options.get("threshold_pct", 5.0))
    only_last = bool(options.get("only_last", True))
    only_direction = options.get("direction")

    times, values = _series(rows)
    if len(values) <= periods:
        return []

    detections: list[Detection] = []
    indices = [len(values) - 1] if only_last else range(periods, len(values))
    for index in indices:
        previous = float(values[index - periods])
        if previous == 0:
            continue
        change = (float(values[index]) - previous) / abs(previous) * 100.0
        if abs(change) < threshold:
            continue
        if only_direction and direction_of(change) != only_direction:
            continue
        detections.append(
            Detection(
                detector="pct_change",
                series_key=series_key,
                observed_at=times[index],
                headline=f"{series_name} moved {change:+.1f}% over {periods} reading(s)",
                plain_english=(
                    f"{series_name} went from {previous:,.4g} to {values[index]:,.4g} across "
                    f"{periods} reading(s), a move of {change:+.1f}%. The alert threshold is "
                    f"{threshold:.1f}%."
                ),
                magnitude=round(change, 4),
                direction=direction_of(change),
                confidence=confidence(change, threshold, periods, periods),
                flags=[],
                details={
                    "value": float(values[index]),
                    "previous": previous,
                    "periods": periods,
                    "pct_change": round(change, 4),
                    "threshold_pct": threshold,
                },
            )
        )
    return detections


def level_threshold(
    rows: Sequence[Any], options: dict[str, Any], *, series_key: str, series_name: str
) -> list[Detection]:
    """Fire when a value crosses a fixed line. A rule, not an inference."""
    above = options.get("above")
    below = options.get("below")
    if above is None and below is None:
        return []
    fixed_confidence = float(options.get("confidence", 0.6))

    times, values = _series(rows)
    if len(values) == 0:
        return []

    index = len(values) - 1
    value = float(values[index])
    breached: str | None = None
    limit: float | None = None
    if above is not None and value > float(above):
        breached, limit = "above", float(above)
    elif below is not None and value < float(below):
        breached, limit = "below", float(below)
    if breached is None or limit is None:
        return []

    return [
        Detection(
            detector="level_threshold",
            series_key=series_key,
            observed_at=times[index],
            headline=f"{series_name} is {breached} {limit:,.4g}",
            plain_english=(
                f"{series_name} read {value:,.4g}, which is {breached} the configured line of "
                f"{limit:,.4g}. This is a fixed rule, not a statistical finding."
            ),
            magnitude=round(value - limit, 6),
            direction="up" if breached == "above" else "down",
            confidence=fixed_confidence,
            flags=[],
            details={"value": value, "limit": limit, "side": breached},
        )
    ]
