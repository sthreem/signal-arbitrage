/**
 * The peak call, ported from sigarb/peak.py.
 *
 * Two implementations of one calculation is a real risk, so the constants live
 * in constants.js and a Python test asserts the two files agree. If you change
 * a number here, change it there, and the test will tell you if you forgot.
 *
 * Ontario's Industrial Conservation Initiative sets a large consumer's Global
 * Adjustment charge for a year from their demand during the five highest
 * Ontario Demand hours of the May to April base period, at most one hour per
 * day. What is published is the threshold and the forecast; subtracting one from
 * the other is public arithmetic. What is not published is how wrong the
 * forecast usually is at a given horizon, and that is what turns a margin into
 * a probability.
 */

import {
  MARKET_UTC_OFFSET_HOURS, BASE_PERIOD_FIRST_MONTH, PEAK_RANK,
  MIN_PAIRS_TO_CALIBRATE, MIN_DAYS_TO_CALIBRATE, PROBABILITY_FLOOR,
  PEAK_WINDOW_MARGIN_HOURS, PEAK_WINDOW_CONTENDER_DAYS,
  PEAK_WINDOW_CONTENDER_FLOOR, NOTIFY_ABOVE, LEAD_TIME_BUCKETS,
  ACTUAL_SOURCE, ACTUAL_SERIES, FORECAST_SOURCES, TRACKER_SOURCE,
} from "./constants.js";

const HOUR_MS = 3600_000;
const MARKET_OFFSET_MS = MARKET_UTC_OFFSET_HOURS * HOUR_MS;

// --------------------------------------------------------------------------
// Market time. IESO stamps EST year round, never local Toronto time, so the
// offset is a constant rather than a timezone lookup and there is no DST here.
// --------------------------------------------------------------------------

export function marketDay(iso) {
  return new Date(Date.parse(iso) + MARKET_OFFSET_MS).toISOString().slice(0, 10);
}

export function marketHour(iso) {
  return new Date(Date.parse(iso) + MARKET_OFFSET_MS).getUTCHours();
}

/** UTC instant at which a market day begins. */
export function marketDayStart(day) {
  return new Date(Date.parse(`${day}T00:00:00Z`) - MARKET_OFFSET_MS);
}

/**
 * The UTC start of an hour-ending hour on a market day.
 * Hour 17 is hour-ending, so it runs 16:00 to 17:00 market time.
 */
export function hourEndingStart(day, hourEnding) {
  return new Date(marketDayStart(day).getTime() + (hourEnding - 1) * HOUR_MS);
}

/**
 * The SQL modifier that shifts a stored UTC timestamp into market time.
 *
 * Derived rather than typed, because it appears inside query strings where no
 * test would catch a wrong sign.
 */
export const MARKET_SQL_SHIFT = `${MARKET_UTC_OFFSET_HOURS} hours`;

/** UTC instant for a market-time wall clock, the inverse of marketDay. */
export function fromMarketTime(ms) {
  return new Date(ms - MARKET_OFFSET_MS);
}

/** Market-time wall clock for a UTC instant. */
export function toMarketTime(ms) {
  return new Date(ms + MARKET_OFFSET_MS);
}

export function hourLabel(iso) {
  const local = new Date(Date.parse(iso) + MARKET_OFFSET_MS);
  return `${local.toISOString().slice(0, 10)} HE${local.getUTCHours() + 1}`;
}

export function basePeriod(now = new Date()) {
  const local = new Date(now.getTime() + MARKET_OFFSET_MS);
  const month = local.getUTCMonth() + 1;
  const first = month >= BASE_PERIOD_FIRST_MONTH
    ? local.getUTCFullYear()
    : local.getUTCFullYear() - 1;
  const pad = (n) => String(n).padStart(2, "0");
  return {
    start: new Date(Date.parse(
      `${first}-${pad(BASE_PERIOD_FIRST_MONTH)}-01T00:00:00Z`) - MARKET_OFFSET_MS
    ).toISOString().replace(".000", ""),
    end: new Date(Date.parse(
      `${first + 1}-${pad(BASE_PERIOD_FIRST_MONTH)}-01T00:00:00Z`) - MARKET_OFFSET_MS
    ).toISOString().replace(".000", ""),
    label: `May ${first} to April ${first + 1}`,
  };
}

/**
 * Which horizon band a forecast reading belongs to.
 *
 * A forecast issued at 09:00 for 17:00 and the revision issued at 16:00 for the
 * same hour are different claims. Filing them by horizon keeps both, and horizon
 * is the axis the error distribution has to be measured along anyway.
 */
export function leadBucket(observedAtMs, issuedAtMs) {
  const leadHours = (observedAtMs - issuedAtMs) / HOUR_MS;
  if (leadHours < LEAD_TIME_BUCKETS[0]) return "|leadpast";
  let low = LEAD_TIME_BUCKETS[0];
  for (const bound of LEAD_TIME_BUCKETS) if (leadHours >= bound) low = bound;
  return `|lead${low}h`;
}

// --------------------------------------------------------------------------
// Probability
// --------------------------------------------------------------------------

/** P(Z > z) for a standard normal, via a rational approximation of erfc. */
export function normalTail(z) {
  const sign = z < 0 ? -1 : 1;
  const x = Math.abs(z) / Math.SQRT2;
  // Abramowitz and Stegun 7.1.26. Accurate to about 1.5e-7, which is far below
  // the precision this estimate actually carries.
  const t = 1 / (1 + 0.3275911 * x);
  const y = 1 - ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t
    - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
  return 0.5 * (1 - sign * y);
}

export function isCalibrated(horizon) {
  return horizon.pairs >= MIN_PAIRS_TO_CALIBRATE
    && horizon.days >= MIN_DAYS_TO_CALIBRATE
    && !!horizon.errorSd;
}

export function shortfall(horizon) {
  if (horizon.pairs < MIN_PAIRS_TO_CALIBRATE) {
    return `${horizon.pairs} matched pair(s), ${MIN_PAIRS_TO_CALIBRATE} needed`;
  }
  if (horizon.days < MIN_DAYS_TO_CALIBRATE) {
    return `${horizon.pairs} pair(s) but only from ${horizon.days} delivery day(s), `
      + `${MIN_DAYS_TO_CALIBRATE} needed`;
  }
  return "the error estimate is degenerate";
}

/**
 * Chance the actual beats the threshold, given how wrong this horizon runs.
 *
 * Clamped away from zero and one. A normal approximation has thinner tails than
 * real forecast error on exactly the days that matter, so "impossible" is a
 * claim the model is not entitled to make, and the stored call is the artefact
 * a buyer would be shown.
 */
export function probabilityOfExceeding(forecastMw, threshold, horizon) {
  if (!isCalibrated(horizon)) return null;
  const centre = forecastMw + (horizon.errorBias || 0);
  const raw = normalTail((threshold - centre) / horizon.errorSd);
  return Math.min(Math.max(raw, PROBABILITY_FLOOR), 1 - PROBABILITY_FLOOR);
}

// --------------------------------------------------------------------------
// Reading the store
// --------------------------------------------------------------------------

/** The ranked peak hours so far, at most one per day. Highest first. */
export function rankTopHours(rows, rank = PEAK_RANK) {
  const bestByDay = new Map();
  for (const { observed_at, value } of rows) {          // rows arrive highest first
    const day = marketDay(observed_at);
    if (!bestByDay.has(day)) bestByDay.set(day, { observed_at, value });
  }
  return [...bestByDay.values()].sort((a, b) => b.value - a.value).slice(0, rank);
}

/**
 * The market hours in which a contending day's own maximum has landed.
 *
 * Only the season's highest days count: a mild Sunday can peak at any hour and
 * its peak hour says nothing about when a record is set. Days qualify by height
 * as well as by rank, because early in a base period a count alone lets every
 * day in. And each observed hour is widened by an hour either side rather than
 * spanning lowest to highest, which one overnight outlier would stretch across
 * the whole day.
 */
export function peakHourWindow(rows, contenders = PEAK_WINDOW_CONTENDER_DAYS) {
  const bestByDay = new Map();
  for (const { observed_at, value } of rows) {
    const day = marketDay(observed_at);
    if (!bestByDay.has(day)) bestByDay.set(day, { observed_at, value });
  }
  if (bestByDay.size === 0) return new Set([...Array(24).keys()]);

  const ranked = [...bestByDay.values()].sort((a, b) => b.value - a.value);
  const floor = ranked[0].value * PEAK_WINDOW_CONTENDER_FLOOR;
  const top = ranked.slice(0, contenders).filter((r) => r.value >= floor);
  const window = new Set();
  for (const { observed_at } of (top.length ? top : ranked.slice(0, 1))) {
    const hour = marketHour(observed_at);
    for (let d = -PEAK_WINDOW_MARGIN_HOURS; d <= PEAK_WINDOW_MARGIN_HOURS; d += 1) {
      window.add((hour + d + 24) % 24);
    }
  }
  return window;
}

/**
 * For each coming hour, the most recently issued forecast held.
 *
 * A narrower horizon band means the forecast was issued closer to the hour, so
 * the narrowest band present is the freshest claim about it.
 */
export function freshestForecasts(rows) {
  const band = (seriesKey) => {
    const suffix = seriesKey.split("|").pop();
    const match = /^lead(\d+)h$/.exec(suffix);
    return match ? Number(match[1]) : Number.MAX_SAFE_INTEGER;  // leadpast last
  };
  const best = new Map();
  for (const { observed_at, series_key, value } of rows) {
    const rank = band(series_key);
    const current = best.get(observed_at);
    if (!current || rank < current.rank) {
      best.set(observed_at, {
        rank, value, bucket: `|${series_key.split("|").pop()}`,
      });
    }
  }
  return best;
}

export { NOTIFY_ABOVE, PEAK_RANK, ACTUAL_SOURCE, ACTUAL_SERIES, FORECAST_SOURCES, TRACKER_SOURCE };
