"""Isolation forest over a short feature window.

The concept called for lightweight ML, and this is where it earns its place:
a multivariate view (level, first difference, three-step difference) catches
shapes a single z-score misses. It stays deterministic through a pinned
random_state, and it degrades to nothing if scikit-learn is absent.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from .. import config
from .base import Detection, clamp, direction_of


def isolation_forest(
    rows: Sequence[Any], options: dict[str, Any], *, series_key: str, series_name: str
) -> list[Detection]:
    try:
        from sklearn.ensemble import IsolationForest
    except ImportError:
        return []

    window = int(options.get("window", 200))
    min_periods = int(options.get("min_periods", 40))
    contamination = float(options.get("contamination", 0.02))
    only_last = bool(options.get("only_last", True))

    values = np.asarray([float(r["value"]) for r in rows], dtype=float)
    times = [str(r["observed_at"]) for r in rows]
    if len(values) < min_periods:
        return []

    values = values[-window:]
    times = times[-window:]

    diff1 = np.diff(values, prepend=values[0])
    diff3 = values - np.concatenate([np.repeat(values[0], min(3, len(values))), values[:-3]])[: len(values)]
    features = np.column_stack([values, diff1, diff3])

    model = IsolationForest(
        n_estimators=int(options.get("n_estimators", 200)),
        contamination=contamination,
        random_state=config.RANDOM_SEED,
        n_jobs=1,
    )
    model.fit(features)
    scores = model.decision_function(features)
    flags = model.predict(features)

    indices = [len(values) - 1] if only_last else range(len(values))
    detections: list[Detection] = []
    for index in indices:
        if flags[index] != -1:
            continue
        # decision_function is negative for outliers; map depth to 0.5 .. 0.9.
        depth = clamp(abs(float(scores[index])) / 0.25)
        detections.append(
            Detection(
                detector="isolation_forest",
                series_key=series_key,
                observed_at=times[index],
                headline=f"{series_name} does not fit its recent pattern",
                plain_english=(
                    f"An isolation forest over the last {len(values)} readings of {series_name} "
                    f"flagged the value {values[index]:,.4g} as an outlier on level and rate of "
                    "change together. This detector sees shape, not just distance from average."
                ),
                magnitude=round(float(scores[index]), 5),
                direction=direction_of(float(diff1[index])),
                confidence=round(0.5 + 0.4 * depth, 3),
                flags=["model_derived"],
                details={
                    "value": float(values[index]),
                    "decision_score": round(float(scores[index]), 6),
                    "window": int(len(values)),
                    "contamination": contamination,
                    "random_state": config.RANDOM_SEED,
                },
            )
        )
    return detections
