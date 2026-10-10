"""Self-grading: let the engine mark its own past work.

The kill criterion asks whether the signals are worth staying subscribed to.
That needs a number, not an opinion, so this module grades every numeric signal
against what the series actually did next.

The grade is deliberately simple and stated plainly:

    blip       the very next reading returned inside one standard deviation of
               the pre-signal baseline. The move did not survive one step, which
               is what a noisy print or a data-entry artefact looks like.
    persisted  the next reading stayed at least one standard deviation out, in
               the same direction. The move was real enough to last a step.
    reverted   the next reading crossed back past the baseline mean in the
               opposite direction: a genuine swing, but a round trip.
    pending    not enough later readings yet to judge.

The blip rate is the closest honest proxy for a false-positive rate. It is not
a claim about whether a subscriber would have made money.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any

from . import db


def grade_signal(conn: sqlite3.Connection, row: Any) -> tuple[str, str]:
    """Return (verdict, note) for one signal."""
    if row["detector"] not in ("zscore", "pct_change", "isolation_forest"):
        return "not_applicable", "only numeric detectors are graded"

    details = json.loads(row["details_json"])
    mean = details.get("baseline_mean")
    sigma = details.get("baseline_stdev")
    if mean is None or sigma in (None, 0):
        return "not_applicable", "no baseline recorded for this detector"

    later = conn.execute(
        "SELECT observed_at, value FROM observations "
        "WHERE source_id = ? AND series_key = ? AND observed_at > ? "
        "ORDER BY observed_at ASC LIMIT 1",
        (row["source_id"], row["series_key"], row["observed_at"]),
    ).fetchone()
    if later is None:
        return "pending", "no later reading yet"

    next_value = float(later["value"])
    next_z = (next_value - float(mean)) / float(sigma)
    fired_z = float(details.get("z") or row["magnitude"] or 0.0)

    if abs(next_z) < 1.0:
        return "blip", f"next reading fell back to {next_z:+.2f} sigma"
    if fired_z * next_z < 0:
        return "reverted", f"next reading swung to {next_z:+.2f} sigma, opposite side"
    return "persisted", f"next reading held at {next_z:+.2f} sigma"


def review_all(conn: sqlite3.Connection, *, limit: int = 5000) -> dict[str, Any]:
    """Grade everything gradeable and store the verdicts. Idempotent."""
    rows = list(
        conn.execute(
            "SELECT * FROM signals ORDER BY fired_at DESC LIMIT ?", (limit,)
        )
    )
    tally: Counter[str] = Counter()
    by_source: dict[str, Counter[str]] = {}

    for row in rows:
        verdict, note = grade_signal(conn, row)
        tally[verdict] += 1
        by_source.setdefault(row["source_id"], Counter())[verdict] += 1
        conn.execute(
            "UPDATE signals SET review_verdict = ?, review_note = ? WHERE signal_id = ?",
            (verdict, note, row["signal_id"]),
        )
    conn.commit()

    judged = tally["blip"] + tally["persisted"] + tally["reverted"]
    blip_rate = (tally["blip"] / judged) if judged else None

    return {
        "total_signals": len(rows),
        "judged": judged,
        "verdicts": dict(tally),
        "blip_rate": blip_rate,
        "by_source": {k: dict(v) for k, v in by_source.items()},
        "verdict": _verdict_sentence(blip_rate, judged),
    }


def _verdict_sentence(blip_rate: float | None, judged: int) -> str:
    if judged < 20:
        return (
            f"Only {judged} signal(s) have a later reading to judge against. That is too few "
            "to say anything honest about the false-positive rate. Keep the engine running."
        )
    if blip_rate is None:
        return "Nothing gradeable yet."
    percent = blip_rate * 100
    if percent >= 50:
        return (
            f"{percent:.0f}% of judged signals did not survive a single further reading. On the "
            "kill criterion as written, this is a no: the detectors are firing on noise and a "
            "reader would mute these inside two weeks. Retune thresholds or drop the source."
        )
    if percent >= 25:
        return (
            f"{percent:.0f}% of judged signals were one-reading blips. Usable but noisy. Raise "
            "thresholds on the worst sources before putting this in front of anyone."
        )
    return (
        f"{percent:.0f}% of judged signals were one-reading blips, so most fired moves lasted at "
        "least a step. That clears the noise half of the kill criterion. It says nothing yet "
        "about whether the signals are earlier than what a professional already sees."
    )


def source_scoreboard(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.source_id,
               COUNT(*) AS fired,
               AVG(s.confidence) AS avg_confidence,
               SUM(CASE WHEN s.review_verdict = 'blip' THEN 1 ELSE 0 END) AS blips,
               SUM(CASE WHEN s.review_verdict IN ('blip','persisted','reverted') THEN 1 ELSE 0 END) AS judged
        FROM signals s GROUP BY s.source_id ORDER BY fired DESC
        """
    )
    out = []
    for row in rows:
        judged = int(row["judged"] or 0)
        out.append(
            {
                "source_id": row["source_id"],
                "fired": int(row["fired"]),
                "avg_confidence": round(float(row["avg_confidence"] or 0), 3),
                "judged": judged,
                "blip_rate": round(int(row["blips"]) / judged, 3) if judged else None,
            }
        )
    return out
