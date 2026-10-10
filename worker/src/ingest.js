/**
 * Fetching, kept deliberately cheap, and parsing only where parsing is owed.
 *
 * The Workers free plan allows 10ms of CPU per invocation, which is the real
 * design constraint here. So nothing builds a DOM: each parser slices to the
 * region it needs and runs one regex over that. Measured on the live payloads,
 * the whole cycle parses in about 2ms. The three market signal feeds are not
 * parsed here at all, only relayed, so the largest payload in the set never
 * becomes a string.
 *
 * The other budget is D1 writes, 100,000 rows a day on the free plan, counted
 * on attempts rather than on changes. That is why the demand archive is read
 * from its tail instead of whole: the file holds a calendar year, and inserting
 * all of it every cycle would spend the day's budget in nine fetches.
 *
 * The third is 50 subrequests per invocation, which is what keeps the source
 * table below honest about cadence: the five-minute feeds are fetched every
 * cycle and everything else on the quarter hour.
 */

import {
  ACTUAL_SOURCE, ACTUAL_SERIES, TRACKER_SOURCE, FORECAST_SOURCES,
  FORECAST_SERIES_STEM, ZONAL_SOURCE, TOTALS_SOURCE, GENERATOR_SOURCE,
} from "./constants.js";
import { hourEndingStart, leadBucket, toMarketTime, fromMarketTime } from "./peak.js";

const BASE = "https://reports-public.ieso.ca/public";
const UA = "signal-arbitrage/1.0 (public data engine; contact via repository)";
const FETCH_TIMEOUT_MS = 12_000;

// How far back into the yearly demand archive to read. Enough to cover a gap of
// a few days and still settle open calls, without re-reading a calendar year on
// every cycle. What actually keeps the write budget in line is the store's
// filter on the newest row already held, since the tail is nearly all duplicates.
const DEMAND_TAIL_BYTES = 12_000;

/**
 * One conditional GET, straight past the edge cache.
 *
 * The conditional headers are a courtesy to IESO and a saving on bandwidth, not
 * the thing correctness rests on: a 304 is believed only because the etag was
 * recorded in the same breath as the object it describes. What actually decides
 * whether a payload is new is its SHA-256, checked by the caller.
 */
export async function sha256Hex(buffer) {
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function get(url, { etag, lastModified } = {}) {
  const headers = { "User-Agent": UA };
  if (etag) headers["If-None-Match"] = etag;
  if (lastModified) headers["If-Modified-Since"] = lastModified;

  const response = await fetch(url, {
    headers,
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    cf: { cacheTtl: 0 },
  });
  if (response.status !== 304 && !response.ok) {
    throw new Error(`HTTP ${response.status} from ${url}`);
  }
  return response;
}

/** `YYYYMMDD` for a market day offset from now, since IESO names files that way. */
function marketFileDate(now, dayOffset = 0) {
  return toMarketTime(now.getTime() + dayOffset * 86_400_000)
    .toISOString().slice(0, 10).replace(/-/g, "");
}

/**
 * The running top Ontario Demand hours of the base period.
 *
 * Six datasets share this file on different measurement bases. Only
 * TOP_ONTARIO_DEMAND is the ICI threshold; the AQEW ones must never be blended
 * in, which is why the slice is taken before the rows are read.
 */
export function parseTracker(xml) {
  const start = xml.indexOf('datapointName="TOP_ONTARIO_DEMAND"');
  if (start < 0) return [];
  const end = xml.indexOf("</dataset>", start);
  const block = xml.slice(start, end < 0 ? undefined : end);

  const rows = [];
  const pattern = /<deliveryDate>(\d{4}-\d{2}-\d{2})<\/deliveryDate>\s*<deliveryHour>(\d+)<\/deliveryHour>\s*<\/datetimeInfo>\s*<value>([\d.]+)<\/value>/g;
  for (const match of block.matchAll(pattern)) {
    rows.push({
      source_id: TRACKER_SOURCE,
      series_key: "top_ontario_demand",
      observed_at: hourEndingStart(match[1], Number(match[2])).toISOString().replace(".000", ""),
      value: Number(match[3]),
    });
  }
  return rows;
}

/**
 * IESO's hour by hour Ontario Demand forecast for one delivery day.
 *
 * Every reading is filed by how far ahead of its delivery hour the report was
 * issued. Without that suffix each revision keys the same row, the earliest one
 * wins, and the forecast error can never be measured.
 */
export function parseForecast(xml, sourceId) {
  const created = /<CreatedAt>(.*?)<\/CreatedAt>/.exec(xml);
  const delivery = /<DeliveryDate>(\d{4}-\d{2}-\d{2})<\/DeliveryDate>/.exec(xml);
  if (!created || !delivery) return [];

  // CreatedAt carries no zone and is market time, like every stamp in these
  // files, so it has to be shifted back to UTC before anything compares it.
  const issuedAtMs = fromMarketTime(Date.parse(`${created[1]}Z`)).getTime();

  const start = xml.indexOf("<ForecastOntDemand>");
  if (start < 0) return [];
  const end = xml.indexOf("</ForecastOntDemand>", start);
  const block = xml.slice(start, end < 0 ? undefined : end);

  const rows = [];
  const pattern = /<DeliveryHour>(\d+)<\/DeliveryHour>\s*<EnergyMW>(-?[\d.]+)<\/EnergyMW>/g;
  for (const match of block.matchAll(pattern)) {
    const observedAt = hourEndingStart(delivery[1], Number(match[1]));
    rows.push({
      source_id: sourceId,
      series_key: FORECAST_SERIES_STEM + leadBucket(observedAt.getTime(), issuedAtMs),
      observed_at: observedAt.toISOString().replace(".000", ""),
      value: Number(match[2]),
    });
  }
  return rows;
}

/** Actual hourly Ontario Demand, read from the tail of the yearly archive. */
export function parseDemand(csv) {
  const tail = csv.length > DEMAND_TAIL_BYTES ? csv.slice(-DEMAND_TAIL_BYTES) : csv;
  const rows = [];
  const pattern = /^(\d{4}-\d{2}-\d{2}),(\d+),(-?\d+),(-?\d+)$/gm;
  for (const match of tail.matchAll(pattern)) {
    rows.push({
      source_id: ACTUAL_SOURCE,
      series_key: ACTUAL_SERIES,
      observed_at: hourEndingStart(match[1], Number(match[2])).toISOString().replace(".000", ""),
      value: Number(match[4]),
    });
  }
  return rows;
}

/**
 * Every source this node collects, and how often.
 *
 * `parse` is what separates the two lines the engine runs. The four that carry
 * one are the peak line's, read into D1 on the quarter hour. The three without
 * one are the market signal feeds: they are relayed to R2 verbatim and parsed on
 * the Mac, because their detectors need a history no Worker can hold.
 *
 * Cadence is the upstream publication rate, not an appetite. The zonal price and
 * the real time totals tick every five minutes and a fifteen minute poll would
 * throw away two intervals in three, which is exactly the intra-hour movement
 * those feeds exist for. Everything else changes hourly at most.
 *
 * Every `id` is also the name of a file in `sources/`, because that config is
 * what parses the payload when it reaches the Mac.
 */
export const SOURCES = [
  {
    id: TRACKER_SOURCE, name: "tracker", ext: "xml", everyMinutes: 15,
    url: () => `${BASE}/ICIPeakTracker/PUB_ICIPeakTracker.xml`,
    parse: parseTracker,
  },
  {
    // Known edge, once a year: between 00:00Z and 05:00Z on 1 January the market
    // day is still 31 December, so this asks for the new year's file and the old
    // year's last hours are missing. Those five hours arrive with the next day's
    // file, so a 31 December call settles a day late rather than never.
    id: ACTUAL_SOURCE, name: "demand", ext: "csv", everyMinutes: 15,
    url: (now) => `${BASE}/Demand/PUB_Demand_${now.getUTCFullYear()}.csv`,
    parse: parseDemand,
  },
  {
    id: FORECAST_SOURCES[0], name: "forecast today", ext: "xml", everyMinutes: 15,
    url: (now) => `${BASE}/Adequacy3/PUB_Adequacy3_${marketFileDate(now, 0)}.xml`,
    parse: (body) => parseForecast(body, FORECAST_SOURCES[0]),
  },
  {
    id: FORECAST_SOURCES[1], name: "forecast ahead", ext: "xml", everyMinutes: 15,
    url: (now) => `${BASE}/Adequacy3/PUB_Adequacy3_${marketFileDate(now, 1)}.xml`,
    parse: (body) => parseForecast(body, FORECAST_SOURCES[1]),
  },
  {
    id: ZONAL_SOURCE, name: "zonal price", ext: "xml", everyMinutes: 5,
    url: () => `${BASE}/RealtimeOntarioZonalPrice/PUB_RealtimeOntarioZonalPrice.xml`,
  },
  {
    id: TOTALS_SOURCE, name: "real time totals", ext: "csv", everyMinutes: 5,
    url: () => `${BASE}/RealtimeTotals/PUB_RealtimeTotals.csv`,
  },
  {
    id: GENERATOR_SOURCE, name: "generator output", ext: "xml", everyMinutes: 15,
    url: () => `${BASE}/GenOutputCapability/PUB_GenOutputCapability.xml`,
  },
];

/** Whether a source is owed a fetch at this minute of the hour. */
export function due(source, now, all = false) {
  return all || now.getUTCMinutes() % source.everyMinutes === 0;
}

/**
 * One fetch pass. Returns an outcome per source, and no side effects.
 *
 * A source that fails is reported and skipped rather than taking the cycle down
 * with it: a missing forecast is a thinner call, a missing cycle is a day of
 * ground truth that cannot be recovered.
 *
 * The body is handed back as bytes. Only a source with a parser is decoded into
 * a string, which is what keeps the 400 kB generator payload off the CPU budget.
 */
export async function fetchAll(now, { state = new Map(), all = false, force = false } = {}) {
  // Two separate things, and conflating them once already meant a hand-run
  // cycle fetched nothing at all. `all` ignores the cadence, which is what a
  // request arriving on an arbitrary minute needs. `force` ignores what the
  // relay has already recorded, which is how a store rebuilt from empty gets
  // its rows back, and is a much bigger hammer.
  const wanted = SOURCES.filter((source) => due(source, now, all || force));

  const settled = await Promise.allSettled(wanted.map(async (source) => {
    const known = force ? {} : (state.get(source.id) || {});
    // Conditional requests are asked for only where a 304 costs nothing. A
    // source with a parser must come back with a body every time, because the
    // peak line's rows are rebuilt from that body and would otherwise depend on
    // the relay's bookkeeping being right about what D1 already holds. That is
    // the behaviour this Worker has always had; the bandwidth saving matters on
    // the 400 kB generator payload, which nothing here parses.
    const conditional = source.parse ? {} : { etag: known.etag, lastModified: known.last_modified };
    const response = await get(source.url(now), conditional);
    if (response.status === 304) {
      return { source, status: "unchanged", reason: "not modified" };
    }

    const body = await response.arrayBuffer();
    const contentHash = await sha256Hex(body);
    const common = {
      source, body, contentHash,
      bytes: body.byteLength,
      etag: response.headers.get("etag"),
      lastModified: response.headers.get("last-modified"),
      contentType: response.headers.get("content-type"),
    };
    return contentHash === known.content_hash
      ? { ...common, status: "unchanged", reason: "identical payload" }
      : { ...common, status: "new" };
  }));

  return settled.map((outcome, index) => (
    outcome.status === "fulfilled"
      ? outcome.value
      : {
        source: wanted[index],
        status: "error",
        reason: String(outcome.reason?.message || outcome.reason).slice(0, 160),
      }
  ));
}

/**
 * Turn one fetched payload into observation rows, if this source carries a
 * parser. Bytes in, rows out; nothing here touches the network or the store.
 *
 * Gated on having a body, not on the payload being new. Parsing an unchanged
 * payload is a no-op against an idempotent store and costs about two
 * milliseconds; skipping it would mean a row missing from D1 for any reason
 * other than the payload itself is never rebuilt, and nothing would say so.
 */
export function parseFetched(outcome) {
  if (!outcome.source.parse || !outcome.body) return [];
  return outcome.source.parse(new TextDecoder().decode(outcome.body));
}
