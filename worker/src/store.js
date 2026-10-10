/**
 * D1 access. Everything that touches the database is here.
 *
 * Two budgets shape this module, both on the free plan: 100,000 row writes a
 * day, counted on attempts rather than on changes, and 5 million row reads. So
 * nothing writes a row it can already tell is present, and every filter runs on
 * an indexed column.
 */

import {
  ACTUAL_SOURCE, ACTUAL_SERIES, TRACKER_SOURCE, FORECAST_SOURCES,
  FORECAST_SERIES_STEM, REGION_CODE,
} from "./constants.js";
import {
  basePeriod, marketDay, marketHour, marketDayStart, rankTopHours, peakHourWindow,
  freshestForecasts, isCalibrated, shortfall, probabilityOfExceeding,
  MARKET_SQL_SHIFT,
} from "./peak.js";

const BATCH = 500;   // the free plan allows 1,000 statements per batch

/**
 * Insert observations, skipping anything already stored.
 *
 * `INSERT OR IGNORE` alone would be correct but not cheap: D1 counts the
 * attempt, and the demand archive is almost entirely rows we already hold. So
 * the newest stored timestamp per source is read first and the tail is trimmed
 * against it, which turns roughly 1,500 attempted writes a cycle into a handful.
 */
export async function writeObservations(db, rows) {
  if (rows.length === 0) return 0;

  const sources = [...new Set(rows.map((r) => r.source_id))];
  const newest = new Map();
  const marks = await db.batch(sources.map((id) => db
    .prepare("SELECT MAX(observed_at) AS newest FROM observations WHERE source_id = ?")
    .bind(id)));
  sources.forEach((id, index) => newest.set(id, marks[index].results?.[0]?.newest || ""));

  // The tracker republishes the same handful of hours as the season progresses
  // and the forecast revises hours already stored, so only the demand archive
  // can be trimmed by time. The others are small enough to attempt in full.
  const wanted = rows.filter((r) => r.source_id !== ACTUAL_SOURCE
    || r.observed_at > (newest.get(r.source_id) || ""));
  if (wanted.length === 0) return 0;

  const statement = db.prepare(
    "INSERT OR IGNORE INTO observations (source_id, series_key, observed_at, value) "
    + "VALUES (?, ?, ?, ?)",
  );
  let written = 0;
  for (let i = 0; i < wanted.length; i += BATCH) {
    const chunk = wanted.slice(i, i + BATCH);
    const results = await db.batch(chunk.map((r) => statement
      .bind(r.source_id, r.series_key, r.observed_at, r.value)));
    written += results.reduce((sum, r) => sum + (r.meta?.changes || 0), 0);
  }
  return written;
}

// --------------------------------------------------------------------------
// The call
// --------------------------------------------------------------------------

/**
 * Measured error of forecasts in one horizon band, against settled actuals.
 *
 * Restricted to the hours a daily peak can actually fall in. The forecast is
 * meaningfully wider of the mark on busy afternoons than overnight, so
 * calibrating across all hours would quote a confidence the forecast does not
 * have on exactly the hours that decide the bill.
 *
 * Standard deviation is accumulated in SQL from the count, the sum and the sum
 * of squares, because SQLite has no built-in for it and pulling every pair into
 * the Worker would spend CPU this plan does not have.
 */
export async function horizonError(db, bucket, hours) {
  const placeholders = hours.map(() => "?").join(",");
  const row = await db.prepare(`
    SELECT COUNT(*) AS pairs,
           COUNT(DISTINCT substr(datetime(a.observed_at, ?), 1, 10)) AS days,
           SUM(a.value - f.value) AS total,
           SUM((a.value - f.value) * (a.value - f.value)) AS total_sq
    FROM observations f
    JOIN observations a
      ON a.source_id = ? AND a.series_key = ? AND a.observed_at = f.observed_at
    WHERE f.source_id IN (?, ?) AND f.series_key = ?
      AND CAST(strftime('%H', datetime(f.observed_at, ?)) AS INTEGER)
          IN (${placeholders})
  `).bind(
    MARKET_SQL_SHIFT, ACTUAL_SOURCE, ACTUAL_SERIES,
    FORECAST_SOURCES[0], FORECAST_SOURCES[1], FORECAST_SERIES_STEM + bucket,
    MARKET_SQL_SHIFT, ...hours,
  ).first();

  const horizon = { bucket, pairs: row?.pairs || 0, days: row?.days || 0,
    errorBias: null, errorSd: null };
  if (horizon.pairs >= 2) {
    const mean = row.total / row.pairs;
    const variance = (row.total_sq - row.pairs * mean * mean) / (row.pairs - 1);
    horizon.errorBias = mean;
    horizon.errorSd = Math.sqrt(Math.max(variance, 0));
  }
  return horizon;
}

/** Everything the dashboard and the cron both need. */
export async function buildReport(db, now = new Date(), days = 3) {
  const period = basePeriod(now);
  const nowIso = now.toISOString().replace(".000", "");

  const seasonRows = (await db.prepare(
    "SELECT observed_at, value FROM observations WHERE source_id = ? AND series_key = ? "
    + "AND observed_at >= ? AND observed_at < ? ORDER BY value DESC",
  ).bind(ACTUAL_SOURCE, ACTUAL_SERIES, period.start, period.end).all()).results || [];

  const report = { period, top: [], threshold: null, calls: [], notes: [] };
  report.top = rankTopHours(seasonRows);
  if (report.top.length > 0) {
    report.threshold = report.top[report.top.length - 1].value;
    if (report.top.length < 5) {
      report.notes.push(
        `Only ${report.top.length} peak hour(s) are set so far this period, so the `
        + "threshold is the lowest of those rather than a fifth place. Early in a base "
        + "period almost any hot afternoon clears it.",
      );
    }
  } else {
    report.notes.push("No demand history for this base period yet.");
    return report;
  }

  for (const problem of await disagreements(db, period)) {
    report.notes.push(`Cross-check: ${problem}`);
  }

  const forecastRows = (await db.prepare(
    "SELECT observed_at, series_key, value FROM observations "
    + "WHERE source_id IN (?, ?) AND observed_at >= ?",
  ).bind(FORECAST_SOURCES[0], FORECAST_SOURCES[1], nowIso).all()).results || [];

  if (forecastRows.length === 0) {
    report.notes.push("No forecast hours ahead of now are stored yet.");
    return report;
  }

  const window = [...peakHourWindow(seasonRows)].sort((a, b) => a - b);
  const windowSet = new Set(window);
  const freshest = freshestForecasts(forecastRows);

  const byDay = new Map();
  for (const [observedAt, entry] of freshest) {
    const day = marketDay(observedAt);
    if (!byDay.has(day)) byDay.set(day, []);
    byDay.get(day).push({ observedAt, ...entry });
  }

  const horizons = new Map();
  for (const day of [...byDay.keys()].sort().slice(0, days)) {
    const inWindow = byDay.get(day).filter((r) => windowSet.has(marketHour(r.observedAt)));
    const candidates = inWindow.length ? inWindow : byDay.get(day);
    const best = candidates.reduce((a, b) => (b.value > a.value ? b : a));

    if (!horizons.has(best.bucket)) {
      horizons.set(best.bucket, await horizonError(db, best.bucket, window));
    }
    const horizon = horizons.get(best.bucket);
    const probability = probabilityOfExceeding(best.value, report.threshold, horizon);
    report.calls.push({
      date: day,
      peakHourUtc: best.observedAt,
      forecastMw: best.value,
      marginMw: best.value - report.threshold,
      horizon,
      probability,
      basis: probability === null
        ? `not calibrated at this horizon: ${shortfall(horizon)}`
        : `${horizon.pairs} pairs across ${horizon.days} days at this horizon, `
          + `error ${horizon.errorBias >= 0 ? "+" : ""}${horizon.errorBias.toFixed(0)} MW `
          + `± ${horizon.errorSd.toFixed(0)} MW`,
    });
  }
  return report;
}

/**
 * Where IESO's own tracker and the actual demand series do not line up.
 *
 * Both are IESO's, so this is not a second opinion. It does catch a timestamp or
 * hour-ending mistake on this side, which is the failure that would quietly
 * invalidate everything else.
 */
async function disagreements(db, period, tolerance = 1.0) {
  const rows = (await db.prepare(`
    SELECT t.observed_at, t.value AS tracked, a.value AS actual
    FROM observations t
    LEFT JOIN observations a
      ON a.source_id = ? AND a.series_key = ? AND a.observed_at = t.observed_at
    WHERE t.source_id = ? AND t.observed_at >= ? AND t.observed_at < ?
    ORDER BY t.value DESC LIMIT 5
  `).bind(ACTUAL_SOURCE, ACTUAL_SERIES, TRACKER_SOURCE, period.start, period.end).all())
    .results || [];

  return rows.flatMap((row) => {
    if (row.actual === null || row.actual === undefined) {
      return [`${row.observed_at}: tracker has it, the demand series does not`];
    }
    if (Math.abs(row.actual - row.tracked) > tolerance) {
      return [`${row.observed_at}: tracker ${Math.round(row.tracked)} MW, `
        + `demand series ${Math.round(row.actual)} MW`];
    }
    return [];
  });
}

// --------------------------------------------------------------------------
// The track record
// --------------------------------------------------------------------------

/** Write the calls down. The first call at each horizon is the one that stands. */
export async function recordCalls(db, report, madeAt) {
  if (report.threshold === null || report.calls.length === 0) return 0;
  const statement = db.prepare(`
    INSERT OR IGNORE INTO peak_calls (
      region_code, delivery_date, horizon_bucket, made_at, threshold_mw,
      forecast_mw, peak_hour_utc, probability, pairs, days
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);
  const results = await db.batch(report.calls.map((call) => statement.bind(
    REGION_CODE, call.date, call.horizon.bucket || "|unknown", madeAt,
    report.threshold, call.forecastMw, call.peakHourUtc, call.probability,
    call.horizon.pairs, call.horizon.days,
  )));
  return results.reduce((sum, r) => sum + (r.meta?.changes || 0), 0);
}

/**
 * Grade every open call whose delivery day is over.
 *
 * Against the threshold that was in force when the call was made, not today's,
 * because the threshold moves as peaks land and grading against the current one
 * would mark a correct call wrong. A day is only settled once all 24 hours are
 * present, so a day still being published is left open rather than judged on a
 * partial afternoon.
 */
export async function settleCalls(db, now = new Date()) {
  const today = marketDay(now.toISOString());
  const open = (await db.prepare(
    "SELECT region_code, delivery_date, horizon_bucket, threshold_mw FROM peak_calls "
    + "WHERE settled_at IS NULL AND delivery_date < ?",
  ).bind(today).all()).results || [];
  if (open.length === 0) return 0;

  const settledAt = now.toISOString().replace(".000", "");
  let settled = 0;
  for (const call of open) {
    const start = marketDayStart(call.delivery_date);
    const end = new Date(start.getTime() + 86_400_000);
    const day = await db.prepare(
      "SELECT COUNT(*) AS hours, MAX(value) AS peak FROM observations "
      + "WHERE source_id = ? AND series_key = ? AND observed_at >= ? AND observed_at < ?",
    ).bind(ACTUAL_SOURCE, ACTUAL_SERIES,
      start.toISOString().replace(".000", ""),
      end.toISOString().replace(".000", "")).first();

    if (!day || day.hours < 24) continue;
    await db.prepare(
      "UPDATE peak_calls SET actual_mw = ?, cleared = ?, settled_at = ? "
      + "WHERE region_code = ? AND delivery_date = ? AND horizon_bucket = ?",
    ).bind(day.peak, day.peak > call.threshold_mw ? 1 : 0, settledAt,
      call.region_code, call.delivery_date, call.horizon_bucket).run();
    settled += 1;
  }
  return settled;
}

/**
 * How the recorded calls actually did.
 *
 * Scored with a Brier score, the mean squared error of the probabilities
 * against what happened. Zero is perfect; 0.25 is what saying fifty percent to
 * everything scores. It punishes confident wrongness rather than rewarding
 * hedging, which is the failure a curtailment customer would actually feel.
 */
export async function scoreboard(db) {
  const rows = (await db.prepare(
    "SELECT horizon_bucket, probability, cleared FROM peak_calls WHERE settled_at IS NOT NULL",
  ).all()).results || [];
  const totals = await db.prepare("SELECT COUNT(*) AS recorded FROM peak_calls").first();

  const scored = rows.filter((r) => r.probability !== null && r.cleared !== null);
  const board = {
    recorded: totals?.recorded || 0,
    settled: rows.length,
    scored: scored.length,
    brier: null,
    baseRate: null,
    byHorizon: {},
    verdict: "",
  };
  if (scored.length === 0) {
    board.verdict = "No settled call carries a probability yet. Until one does there is "
      + "no track record, only a working calculation.";
    return board;
  }

  const brier = (set) => set.reduce((sum, r) =>
    sum + (r.probability - r.cleared) ** 2, 0) / set.length;
  board.brier = brier(scored);
  board.baseRate = scored.reduce((sum, r) => sum + r.cleared, 0) / scored.length;
  for (const bucket of [...new Set(scored.map((r) => r.horizon_bucket))].sort()) {
    const subset = scored.filter((r) => r.horizon_bucket === bucket);
    board.byHorizon[bucket] = { n: subset.length, brier: brier(subset) };
  }

  // The score to beat: always predicting the base rate, knowing nothing else.
  const baseline = board.baseRate * (1 - board.baseRate);
  const f = (x) => x.toFixed(3);
  if (scored.length < 20) {
    board.verdict = `${scored.length} scored call(s) is too few to read. A Brier score `
      + "needs a season behind it, not a week.";
  } else if (board.brier < baseline * 0.75) {
    board.verdict = `Brier ${f(board.brier)} against ${f(baseline)} for knowing nothing `
      + "but the base rate. The calls are carrying real information.";
  } else if (board.brier <= baseline) {
    board.verdict = `Brier ${f(board.brier)} against ${f(baseline)} for the base rate `
      + "alone. Barely better than guessing the average, which is not worth paying for.";
  } else {
    board.verdict = `Brier ${f(board.brier)} is worse than ${f(baseline)}, which is what `
      + "always predicting the base rate would score. The model is subtracting value and "
      + "should not be sold in this state.";
  }
  return board;
}

export async function recordCycle(db, entry) {
  await db.prepare(
    "INSERT OR REPLACE INTO cycles (ran_at, ok, ms, fetched, written, detail) "
    + "VALUES (?, ?, ?, ?, ?, ?)",
  ).bind(entry.ranAt, entry.ok ? 1 : 0, entry.ms, entry.fetched, entry.written,
    entry.detail || null).run();
}

// Two hours of ticks at the five minute cadence, so the last-hour count in the
// dashboard is measured inside the window rather than clipped by it.
export async function recentCycles(db, limit = 24) {
  return (await db.prepare(
    "SELECT ran_at, ok, ms, fetched, written, detail FROM cycles ORDER BY ran_at DESC LIMIT ?",
  ).bind(limit).all()).results || [];
}

export async function recentCalls(db, limit = 40) {
  return (await db.prepare(
    "SELECT delivery_date, horizon_bucket, made_at, threshold_mw, forecast_mw, "
    + "probability, pairs, days, actual_mw, cleared, settled_at FROM peak_calls "
    + "ORDER BY delivery_date DESC, horizon_bucket LIMIT ?",
  ).bind(limit).all()).results || [];
}

// --------------------------------------------------------------------------
// The relay
// --------------------------------------------------------------------------

/** Everything the relay knows, keyed by source. One read for the whole cycle. */
export async function relayState(db) {
  const rows = (await db.prepare("SELECT * FROM relay_state").all()).results || [];
  return new Map(rows.map((row) => [row.source_id, row]));
}

/**
 * Record what the relay just did, one statement per source in a single batch.
 *
 * `checked_at` is written even when nothing changed, on purpose. Without it a
 * stale `stored_at` is ambiguous between a relay that has stopped working and a
 * source that has stopped publishing, and those need different responses. Seven
 * rows a cycle is about 2% of the daily write budget.
 */
export async function saveRelayState(db, entries) {
  if (entries.length === 0) return 0;
  const statement = db.prepare(`
    INSERT INTO relay_state (
      source_id, content_hash, object_key, bytes, stored_at, checked_at,
      etag, last_modified, objects, last_error, last_error_at
    ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)
    ON CONFLICT (source_id) DO UPDATE SET
      content_hash  = COALESCE(?2, content_hash),
      object_key    = COALESCE(?3, object_key),
      bytes         = COALESCE(?4, bytes),
      stored_at     = COALESCE(?5, stored_at),
      checked_at    = ?6,
      etag          = COALESCE(?7, etag),
      last_modified = COALESCE(?8, last_modified),
      objects       = objects + ?9,
      last_error    = ?10,
      last_error_at = ?11
  `);
  await db.batch(entries.map((e) => statement.bind(
    e.sourceId, e.contentHash ?? null, e.objectKey ?? null, e.bytes ?? null,
    e.storedAt ?? null, e.checkedAt, e.etag ?? null, e.lastModified ?? null,
    e.stored ? 1 : 0, e.error ?? null, e.error ? e.checkedAt : null,
  )));
  return entries.length;
}

/** The relay's health, for the dashboard, newest activity first. */
export async function relayHealth(db) {
  return (await db.prepare(
    "SELECT source_id, bytes, stored_at, checked_at, objects, last_error, last_error_at "
    + "FROM relay_state ORDER BY source_id",
  ).all()).results || [];
}

/**
 * Drop cycle rows past the window the dashboard reads.
 *
 * Operational telemetry, not the record, and at a five minute cadence it is 288
 * rows a day growing forever. Two weeks is far more than the heartbeat shows and
 * enough to look back over a bad night.
 */
export async function pruneCycles(db, now, days = 14) {
  const cutoff = new Date(now.getTime() - days * 86_400_000).toISOString().replace(/\.\d+Z$/, "Z");
  const result = await db.prepare("DELETE FROM cycles WHERE ran_at < ?").bind(cutoff).run();
  return result.meta?.changes || 0;
}

/** Every call ever made, for the R2 snapshot. */
export async function allCalls(db) {
  return (await db.prepare(
    "SELECT * FROM peak_calls ORDER BY delivery_date, horizon_bucket",
  ).all()).results || [];
}

export async function storeCounts(db) {
  const rows = (await db.prepare(
    "SELECT source_id, COUNT(*) AS n, MAX(observed_at) AS newest FROM observations "
    + "GROUP BY source_id ORDER BY source_id",
  ).all()).results || [];
  return rows;
}
