"""The peak line: how close a coming hour is to setting an ICI peak.

Ontario's Industrial Conservation Initiative sets a Class A consumer's Global
Adjustment charge for a whole year from their own demand during the five highest
Ontario Demand hours of the May-to-April base period. Curtailing during one of
those hours is worth a great deal; curtailing on a day that turns out not to
matter costs production for nothing. The decision therefore needs a probability,
not a point forecast.

What is public and what is not, stated plainly because the distinction is the
whole product:

- IESO publishes the running top hours (`on_ieso_ici_peak_tracker`), its own
  hourly demand forecast (`on_ieso_demand_forecast`), and the actual demand that
  settles it (`on_ieso_demand_actual`). Subtracting a forecast from a threshold
  is public arithmetic and is not an edge.
- What is not published anywhere is how wrong that forecast usually is at a
  given horizon. Measure it, and the same subtraction becomes a probability.
  That measurement, and the graded record of the calls it produces, is the only
  thing here that is ours.

One rule matters more than any other and is easy to get wrong. **The top five
counts at most one hour per day.** Verified 2026-08-19: the five highest single
hours of the 2026 season were all consecutive hours on 14 July, but IESO's
tracker lists 14 July, 2 July, 13 July, 3 July and 1 July, so it is ranking each
day's own peak hour. Ranking raw hours would put four phantom peaks in the list
and understate the threshold badly.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import timeutil

MARKET_TZ = ZoneInfo("EST")          # IESO stamps EST year round, not local Toronto.
BASE_PERIOD_FIRST_MONTH = 5          # The ICI base period runs 1 May to 30 April.
PEAK_RANK = 5                        # Charges are set by the top five.

TRACKER_SOURCE = "on_ieso_ici_peak_tracker"
# Two sources read the same adequacy report a day apart. They are kept separate
# because a forecast issued the same morning and one issued the evening before
# are different claims, and are read together here because they answer the same
# question about the same hours.
FORECAST_SOURCES = ("on_ieso_demand_forecast", "on_ieso_demand_forecast_ahead")
ACTUAL_SOURCE = "on_ieso_demand_actual"
ACTUAL_SERIES = "ontario_demand"

# Below this many matched forecast/actual pairs at a horizon, the error estimate
# is not worth dressing up as a probability and the report says so instead.
MIN_PAIRS_TO_CALIBRATE = 30

# And below this many distinct delivery days behind those pairs. Hours inside one
# day miss together: the weather was wrong for the whole afternoon, not for 17:00
# in particular. Thirty pairs drawn from three days is three observations wearing
# a disguise, so days are the count that actually bounds the estimate.
MIN_DAYS_TO_CALIBRATE = 10

# Hours either side of the window where daily maxima actually land, kept so the
# window does not clamp shut on a single hour early in a base period.
PEAK_WINDOW_MARGIN_HOURS = 1

# How many of the season's highest days define the window. The question is not
# when demand peaks on a quiet Sunday, it is when it peaks on a day that could
# actually reach the threshold.
PEAK_WINDOW_CONTENDER_DAYS = 20

# And a contender has to be within this much of the season's highest day. The
# count alone is not enough early in a base period, when there are fewer than
# twenty days and every mild Tuesday qualifies by default.
PEAK_WINDOW_CONTENDER_FLOOR = 0.85


@dataclass
class BasePeriod:
    start: str
    end: str
    label: str


@dataclass
class Horizon:
    """A forecast band, and how wrong forecasts in it have historically been."""
    bucket: str
    lead_hours_low: int
    error_sd: float | None = None
    error_bias: float | None = None
    pairs: int = 0
    days: int = 0

    @property
    def calibrated(self) -> bool:
        return (
            self.pairs >= MIN_PAIRS_TO_CALIBRATE
            and self.days >= MIN_DAYS_TO_CALIBRATE
            and bool(self.error_sd)
        )

    @property
    def shortfall(self) -> str:
        """Why it is not calibrated, in the reader's terms."""
        if self.pairs < MIN_PAIRS_TO_CALIBRATE:
            return f"{self.pairs} matched pair(s), {MIN_PAIRS_TO_CALIBRATE} needed"
        if self.days < MIN_DAYS_TO_CALIBRATE:
            return (
                f"{self.pairs} pair(s) but only from {self.days} delivery day(s), "
                f"{MIN_DAYS_TO_CALIBRATE} needed"
            )
        return "the error estimate is degenerate"


@dataclass
class DayCall:
    """One delivery day's call: its forecast peak hour against the threshold."""
    date: str
    peak_hour_utc: str
    peak_hour_label: str
    forecast_mw: float
    margin_mw: float
    horizon: Horizon
    probability: float | None = None
    basis: str = ""


# A call at or above this chance is worth interrupting someone for; below it,
# the engine stays quiet. Set where it is because the cost of a missed peak is
# an order of magnitude above the cost of a wasted look.
NOTIFY_ABOVE = 0.15

# A probability is never stated as certainty in either direction. A normal
# approximation has thinner tails than real forecast error on exactly the days
# that matter, so "impossible" is a claim the model is not entitled to make.
# Clamped where the number is produced rather than where it is printed, because
# the recorded call is the artefact a buyer would be shown.
PROBABILITY_FLOOR = 0.001


@dataclass
class PeakReport:
    period: BasePeriod
    top: list[tuple[str, float]] = field(default_factory=list)
    threshold: float | None = None
    calls: list[DayCall] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Base period
# --------------------------------------------------------------------------

def base_period(now: datetime | None = None) -> BasePeriod:
    """The 1 May to 30 April window whose peaks are currently being set."""
    moment = (now or timeutil.now_utc()).astimezone(MARKET_TZ)
    first_year = moment.year if moment.month >= BASE_PERIOD_FIRST_MONTH else moment.year - 1
    start = datetime(first_year, BASE_PERIOD_FIRST_MONTH, 1, tzinfo=MARKET_TZ)
    end = datetime(first_year + 1, BASE_PERIOD_FIRST_MONTH, 1, tzinfo=MARKET_TZ)
    return BasePeriod(
        start=timeutil.iso(start),
        end=timeutil.iso(end),
        label=f"May {first_year} to April {first_year + 1}",
    )


def _market_day(observed_at: str) -> str:
    moment = timeutil.parse(observed_at)
    return moment.astimezone(MARKET_TZ).strftime("%Y-%m-%d") if moment else observed_at[:10]


def market_day_bounds(delivery_date: str) -> tuple[str, str]:
    """The UTC half-open window covering one market day."""
    day = datetime.strptime(delivery_date, "%Y-%m-%d").replace(tzinfo=MARKET_TZ)
    return timeutil.iso(day), timeutil.iso(day + timedelta(days=1))


def _market_hour(observed_at: str) -> int:
    """The market-local hour a reading starts in."""
    moment = timeutil.parse(observed_at)
    return moment.astimezone(MARKET_TZ).hour if moment else 0


def _hour_label(observed_at: str) -> str:
    """IESO speaks in hour-ending, so 16:00 EST is hour 17."""
    moment = timeutil.parse(observed_at)
    if moment is None:
        return observed_at
    local = moment.astimezone(MARKET_TZ)
    return f"{local:%Y-%m-%d} HE{local.hour + 1}"


# --------------------------------------------------------------------------
# Where the threshold stands
# --------------------------------------------------------------------------

def top_hours(conn: sqlite3.Connection, period: BasePeriod, rank: int = PEAK_RANK
              ) -> list[tuple[str, float]]:
    """The ranked peak hours so far, at most one per day. Highest first.

    Read from actual demand rather than from the tracker, because the actual
    series is complete and independently rankable, while the tracker publishes
    only its own top handful. The two are cross-checked in `disagreements`.
    """
    rows = conn.execute(
        """
        SELECT observed_at, value FROM observations
        WHERE source_id = ? AND series_key = ?
          AND observed_at >= ? AND observed_at < ?
        ORDER BY value DESC
        """,
        (ACTUAL_SOURCE, ACTUAL_SERIES, period.start, period.end),
    ).fetchall()

    best_by_day: dict[str, tuple[str, float]] = {}
    for observed_at, value in rows:
        day = _market_day(observed_at)
        if day not in best_by_day:            # rows arrive highest first
            best_by_day[day] = (observed_at, float(value))
    ranked = sorted(best_by_day.values(), key=lambda pair: -pair[1])
    return ranked[:rank]


def disagreements(conn: sqlite3.Connection, period: BasePeriod, tolerance: float = 1.0
                  ) -> list[str]:
    """Where IESO's own tracker and the actual demand series do not line up.

    Both are IESO's, so this is not a second opinion. It does catch a timestamp
    or hour-ending mistake on our side, which is the failure that would quietly
    invalidate everything else here.
    """
    problems: list[str] = []
    rows = conn.execute(
        """
        SELECT observed_at, value FROM observations
        WHERE source_id = ? AND observed_at >= ? AND observed_at < ?
        ORDER BY value DESC LIMIT ?
        """,
        (TRACKER_SOURCE, period.start, period.end, PEAK_RANK),
    ).fetchall()
    for observed_at, value in rows:
        actual = conn.execute(
            "SELECT value FROM observations WHERE source_id = ? AND series_key = ? "
            "AND observed_at = ?",
            (ACTUAL_SOURCE, ACTUAL_SERIES, observed_at),
        ).fetchone()
        if actual is None:
            problems.append(f"{_hour_label(observed_at)}: tracker has it, the demand series does not")
        elif abs(float(actual[0]) - float(value)) > tolerance:
            problems.append(
                f"{_hour_label(observed_at)}: tracker {float(value):.0f} MW, "
                f"demand series {float(actual[0]):.0f} MW"
            )
    return problems


# --------------------------------------------------------------------------
# How wrong the forecast usually is
# --------------------------------------------------------------------------

def peak_hour_window(conn: sqlite3.Connection, period: BasePeriod,
                     contenders: int = PEAK_WINDOW_CONTENDER_DAYS) -> set[int]:
    """The market hours in which a *contending* day's own maximum has landed.

    Ontario peaks in the late afternoon in summer and twice a day in winter, so
    the window is read off the data rather than asserted.

    Three details that a first attempt got wrong. It looks only at the season's
    highest days, because a mild Sunday can peak at any hour and its peak hour
    tells you nothing about when a record is set. It qualifies those days by
    height as well as by rank, since early in a base period a count alone lets
    every day in. And it widens each observed hour by an hour either side rather
    than spanning the lowest to the highest, which one overnight outlier would
    stretch across the entire day.

    It lags the season by construction: in January the window is still
    summer-shaped until winter maxima accumulate, which widens it rather than
    narrowing it, so the failure is on the conservative side.
    """
    rows = conn.execute(
        """
        SELECT observed_at, value FROM observations
        WHERE source_id = ? AND series_key = ?
          AND observed_at >= ? AND observed_at < ?
        ORDER BY value DESC
        """,
        (ACTUAL_SOURCE, ACTUAL_SERIES, period.start, period.end),
    ).fetchall()
    best_by_day: dict[str, tuple[str, float]] = {}
    for observed_at, value in rows:
        best_by_day.setdefault(_market_day(observed_at), (observed_at, float(value)))
    if not best_by_day:
        return set(range(24))

    ranked = sorted(best_by_day.values(), key=lambda pair: -pair[1])
    floor = ranked[0][1] * PEAK_WINDOW_CONTENDER_FLOOR
    top = [pair for pair in ranked[:contenders] if pair[1] >= floor] or ranked[:1]
    margin = range(-PEAK_WINDOW_MARGIN_HOURS, PEAK_WINDOW_MARGIN_HOURS + 1)
    return {(_market_hour(observed_at) + offset) % 24
            for observed_at, _ in top for offset in margin}


def horizon_error(conn: sqlite3.Connection, bucket: str, hours: set[int] | None = None
                  ) -> Horizon:
    """Measured error of forecasts in one horizon band, against settled actuals.

    Sign convention: positive means the actual came in above the forecast, which
    is the direction that matters, because that is how a day nobody expected to
    matter turns into a peak.

    Restricted to the hours a daily peak can actually fall in. Measured
    2026-08-19, the error on peak hours is consistently wider than the error
    across all hours (631 MW against 499 MW at a six hour horizon, 1,960 against
    1,636 at five days), because overnight demand is easy to forecast and busy
    afternoons are not. Calibrating on every hour would therefore quote a
    confidence the forecast does not have on exactly the hours that decide the
    bill.
    """
    low = 0
    if bucket.startswith("|lead") and bucket.endswith("h"):
        try:
            low = int(bucket[len("|lead"):-1])
        except ValueError:
            low = 0
    horizon = Horizon(bucket=bucket, lead_hours_low=low)

    rows = conn.execute(
        """
        SELECT f.observed_at, a.value - f.value
        FROM observations f
        JOIN observations a
          ON a.source_id = ? AND a.series_key = ? AND a.observed_at = f.observed_at
        WHERE f.source_id IN (?, ?) AND f.series_key = ?
        """,
        (ACTUAL_SOURCE, ACTUAL_SERIES, *FORECAST_SOURCES,
         f"forecast_ontario_demand{bucket}"),
    ).fetchall()
    kept = [
        (observed_at, float(error)) for observed_at, error in rows
        if hours is None or _market_hour(observed_at) in hours
    ]
    errors = [error for _, error in kept]
    horizon.pairs = len(errors)
    horizon.days = len({_market_day(observed_at) for observed_at, _ in kept})
    if len(errors) >= 2:
        mean = sum(errors) / len(errors)
        variance = sum((e - mean) ** 2 for e in errors) / (len(errors) - 1)
        horizon.error_bias = mean
        horizon.error_sd = math.sqrt(variance)
    return horizon


def _normal_tail(z: float) -> float:
    """P(Z > z) for a standard normal, via the error function."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def probability_of_exceeding(forecast_mw: float, threshold: float, horizon: Horizon
                             ) -> float | None:
    """Chance the actual beats the threshold, given how wrong this horizon runs.

    A normal approximation on the measured error. It is a stated model, not a
    law: forecast errors have fatter tails than a normal on the handful of days
    that matter most, so treat a small probability as a floor rather than a
    dismissal. Clamped away from zero and one for that reason.
    """
    if not horizon.calibrated or not horizon.error_sd:
        return None
    centre = forecast_mw + (horizon.error_bias or 0.0)
    raw = _normal_tail((threshold - centre) / horizon.error_sd)
    return min(max(raw, PROBABILITY_FLOOR), 1.0 - PROBABILITY_FLOOR)


# --------------------------------------------------------------------------
# The call
# --------------------------------------------------------------------------

def latest_forecast_by_hour(conn: sqlite3.Connection, from_utc: str
                            ) -> dict[str, tuple[float, str]]:
    """For each coming hour, the most recently issued forecast we hold.

    Readings are filed by horizon, and a smaller horizon band means the forecast
    was issued closer to the hour, so the narrowest band present is the freshest
    claim about it.
    """
    rows = conn.execute(
        """
        SELECT observed_at, series_key, value FROM observations
        WHERE source_id IN (?, ?) AND observed_at >= ?
        """,
        (*FORECAST_SOURCES, from_utc),
    ).fetchall()

    def band(series_key: str) -> int:
        suffix = series_key.split("|", 1)[-1]
        if suffix.startswith("lead") and suffix.endswith("h"):
            try:
                return int(suffix[4:-1])
            except ValueError:
                return 10 ** 6
        return 10 ** 6                       # `leadpast` and anything unexpected

    best: dict[str, tuple[int, float, str]] = {}
    for observed_at, series_key, value in rows:
        rank = band(series_key)
        current = best.get(observed_at)
        if current is None or rank < current[0]:
            bucket = "|" + series_key.split("|", 1)[-1] if "|" in series_key else ""
            best[observed_at] = (rank, float(value), bucket)
    return {ts: (value, bucket) for ts, (_, value, bucket) in best.items()}


def report(conn: sqlite3.Connection, *, days: int = 2, now: datetime | None = None) -> PeakReport:
    """Everything `signal peak` prints, assembled and gradeable."""
    moment = now or timeutil.now_utc()
    period = base_period(moment)
    result = PeakReport(period=period)
    result.top = top_hours(conn, period)
    if len(result.top) >= PEAK_RANK:
        result.threshold = result.top[-1][1]
    elif result.top:
        result.threshold = result.top[-1][1]
        result.notes.append(
            f"Only {len(result.top)} peak hour(s) are set so far this period, so the "
            f"threshold is the lowest of those rather than a fifth place. Early in a "
            f"base period almost any hot afternoon clears it."
        )

    for problem in disagreements(conn, period):
        result.notes.append(f"Cross-check: {problem}")

    forecasts = latest_forecast_by_hour(conn, timeutil.iso(moment))
    if not forecasts:
        result.notes.append(
            "No forecast hours ahead of now are stored. Run `signal update` first."
        )
        return result

    window = peak_hour_window(conn, period)
    by_day: dict[str, list[tuple[str, float, str]]] = {}
    horizon_cache: dict[str, Horizon] = {}
    for observed_at, (value, bucket) in forecasts.items():
        by_day.setdefault(_market_day(observed_at), []).append((observed_at, value, bucket))

    for day in sorted(by_day)[:days]:
        candidates = [row for row in by_day[day] if _market_hour(row[0]) in window] or by_day[day]
        observed_at, value, bucket = max(candidates, key=lambda row: row[1])
        if bucket not in horizon_cache:
            horizon_cache[bucket] = horizon_error(conn, bucket, window)
        horizon = horizon_cache[bucket]
        threshold = result.threshold if result.threshold is not None else value
        call = DayCall(
            date=day,
            peak_hour_utc=observed_at,
            peak_hour_label=_hour_label(observed_at),
            forecast_mw=value,
            margin_mw=value - threshold,
            horizon=horizon,
            probability=probability_of_exceeding(value, threshold, horizon),
        )
        if call.probability is None:
            call.basis = f"not calibrated at this horizon: {horizon.shortfall}"
        else:
            call.basis = (
                f"{horizon.pairs} pairs across {horizon.days} days at this horizon, "
                f"error {horizon.error_bias:+.0f} MW ± {horizon.error_sd:.0f} MW"
            )
        result.calls.append(call)
    return result


# --------------------------------------------------------------------------
# The track record
# --------------------------------------------------------------------------

def worth_raising(result: PeakReport) -> str | None:
    """One line if a call deserves interrupting someone, otherwise nothing."""
    urgent = [
        call for call in result.calls
        if call.probability is not None and call.probability >= NOTIFY_ABOVE
    ]
    if not urgent:
        return None
    worst = max(urgent, key=lambda call: call.probability or 0.0)
    verb = "Curtail" if (worst.probability or 0) >= 0.5 else "Watch"
    return (
        f"{verb}: {worst.date} {worst.peak_hour_label.split()[-1]} at "
        f"{worst.forecast_mw:,.0f} MW, {worst.probability:.0%} chance of setting an "
        f"ICI peak (threshold {result.threshold:,.0f} MW)."
    )


def record_calls(conn: sqlite3.Connection, result: PeakReport, region_code: str = "on",
                 made_at: str | None = None) -> int:
    """Write today's calls down. Returns how many were new.

    Keyed on `(region, delivery date, horizon)` and inserted with a conflict
    ignore, so the first call made at a horizon is the one that stands. Running
    the report again an hour later records a *different* horizon's call, never a
    revision of an existing one. Anything else and the record grades a decision
    nobody actually had to make.
    """
    if result.threshold is None:
        return 0
    stamp = made_at or timeutil.now_iso()
    rows = [
        {
            "region_code": region_code,
            "delivery_date": call.date,
            "horizon_bucket": call.horizon.bucket or "|unknown",
            "made_at": stamp,
            "threshold_mw": result.threshold,
            "forecast_mw": call.forecast_mw,
            "peak_hour_utc": call.peak_hour_utc,
            "probability": call.probability,
            "pairs": call.horizon.pairs,
            "days": call.horizon.days,
        }
        for call in result.calls
    ]
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO peak_calls (
            region_code, delivery_date, horizon_bucket, made_at, threshold_mw,
            forecast_mw, peak_hour_utc, probability, pairs, days
        ) VALUES (
            :region_code, :delivery_date, :horizon_bucket, :made_at, :threshold_mw,
            :forecast_mw, :peak_hour_utc, :probability, :pairs, :days
        )
        """,
        rows,
    )
    conn.commit()
    return conn.total_changes - before


def settle_calls(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Grade every open call whose delivery day is over. Returns how many settled.

    Graded against the threshold that was in force when the call was made, not
    the current one, because the threshold moves as peaks land and grading
    against today's would mark a correct call wrong.
    """
    moment = now or timeutil.now_utc()
    today = moment.astimezone(MARKET_TZ).strftime("%Y-%m-%d")
    open_calls = conn.execute(
        "SELECT call_id, delivery_date, threshold_mw FROM peak_calls "
        "WHERE settled_at IS NULL AND delivery_date < ?",
        (today,),
    ).fetchall()

    settled = 0
    for call_id, delivery_date, threshold_mw in open_calls:
        day_max = _day_max_actual(conn, delivery_date)
        if day_max is None:
            continue                     # actuals have not arrived yet
        conn.execute(
            "UPDATE peak_calls SET actual_mw = ?, cleared = ?, settled_at = ? "
            "WHERE call_id = ?",
            (day_max, 1 if day_max > float(threshold_mw) else 0, timeutil.iso(moment), call_id),
        )
        settled += 1
    conn.commit()
    return settled


def _day_max_actual(conn: sqlite3.Connection, delivery_date: str) -> float | None:
    """The highest actual Ontario Demand hour of one market day, if it is complete.

    Requires a full 24 hours before settling, so a day still being published is
    left open rather than graded against a partial afternoon.
    """
    # A market day is not a UTC day. EST runs five hours behind, so the day that
    # IESO calls 20 August starts at 05:00Z on the 20th and ends at 05:00Z on the
    # 21st. Querying the UTC day instead loses five hours at one end and gains
    # five at the other, and no day ever reaches twenty four hours.
    start, end = market_day_bounds(delivery_date)
    rows = conn.execute(
        "SELECT observed_at, value FROM observations WHERE source_id = ? AND series_key = ? "
        "AND observed_at >= ? AND observed_at < ?",
        (ACTUAL_SOURCE, ACTUAL_SERIES, start, end),
    ).fetchall()
    hours = [(observed_at, float(value)) for observed_at, value in rows
             if _market_day(observed_at) == delivery_date]
    if len(hours) < 24:
        return None
    return max(value for _, value in hours)


def scoreboard(conn: sqlite3.Connection) -> dict[str, Any]:
    """How the recorded calls actually did.

    Scored with a Brier score, the mean squared error of the probabilities
    against what happened. Zero is perfect; 0.25 is what you get by saying fifty
    percent to everything. It is the right scoring rule here because it punishes
    confident wrongness rather than rewarding hedging, which is the failure a
    curtailment customer would actually feel.
    """
    rows = conn.execute(
        "SELECT horizon_bucket, probability, cleared FROM peak_calls "
        "WHERE settled_at IS NOT NULL"
    ).fetchall()
    scored = [(b, float(p), int(c)) for b, p, c in rows if p is not None and c is not None]

    out: dict[str, Any] = {
        "recorded": conn.execute("SELECT COUNT(*) FROM peak_calls").fetchone()[0],
        "settled": len(rows),
        "scored": len(scored),
        "brier": None,
        "base_rate": None,
        "by_horizon": {},
        "verdict": "",
    }
    if not scored:
        out["verdict"] = (
            "No settled call carries a probability yet. Until one does there is no "
            "track record, only a working calculation."
        )
        return out

    out["brier"] = sum((p - c) ** 2 for _, p, c in scored) / len(scored)
    out["base_rate"] = sum(c for _, _, c in scored) / len(scored)
    for bucket in sorted({b for b, _, _ in scored}):
        subset = [(p, c) for b, p, c in scored if b == bucket]
        out["by_horizon"][bucket] = {
            "n": len(subset),
            "brier": sum((p - c) ** 2 for p, c in subset) / len(subset),
        }

    # The score to beat: always predicting the base rate, knowing nothing else.
    baseline = out["base_rate"] * (1 - out["base_rate"])
    if len(scored) < 20:
        out["verdict"] = (
            f"{len(scored)} scored call(s) is too few to read. A Brier score needs "
            f"a season behind it, not a week."
        )
    elif out["brier"] < baseline * 0.75:
        out["verdict"] = (
            f"Brier {out['brier']:.3f} against {baseline:.3f} for knowing nothing but "
            f"the base rate. The calls are carrying real information."
        )
    elif out["brier"] <= baseline:
        out["verdict"] = (
            f"Brier {out['brier']:.3f} against {baseline:.3f} for the base rate alone. "
            f"Barely better than guessing the average, which is not worth paying for."
        )
    else:
        out["verdict"] = (
            f"Brier {out['brier']:.3f} is worse than {baseline:.3f}, which is what "
            f"always predicting the base rate would score. The model is subtracting "
            f"value and should not be sold in this state."
        )
    return out
