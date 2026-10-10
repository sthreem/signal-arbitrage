/**
 * Tests for the ported maths.
 *
 * The peak calculation exists twice, in Python and here. `test_engine.py` checks
 * that the constants have not drifted; this checks that the behaviour has not.
 * Run directly with `node worker/test.mjs`, or through `pytest`, which shells
 * out to it so one command remains the gate.
 */

import assert from "node:assert/strict";
import {
  basePeriod, marketDay, marketHour, hourEndingStart, leadBucket, normalTail,
  probabilityOfExceeding, rankTopHours, peakHourWindow, freshestForecasts,
  isCalibrated, shortfall,
} from "./src/peak.js";
import { parseTracker, parseForecast, parseDemand } from "./src/ingest.js";
import { MIN_PAIRS_TO_CALIBRATE, MIN_DAYS_TO_CALIBRATE, PROBABILITY_FLOOR } from "./src/constants.js";

// Cases are collected, then run once the whole module has been evaluated.
//
// Two things this avoids. Running a case inline would return a promise nobody
// awaits, so a failure inside an async one would surface as an unhandled
// rejection while the suite reported success. And scheduling the run for after
// the module body means a case appended to the end of this file is picked up
// wherever it sits, rather than being silently skipped because it was written
// below the runner.
const cases = [];
const test = (name, fn) => cases.push([name, fn]);

queueMicrotask(async () => {
  let run = 0;
  for (const [name, fn] of cases) {
    try {
      await fn();
    } catch (error) {
      console.error(`FAILED: ${name}`);
      console.error(error);
      process.exitCode = 1;
      return;
    }
    run += 1;
  }
  console.log(`${run} worker test(s) passed`);
});

const calibrated = (over) => ({
  bucket: "|lead6h", pairs: MIN_PAIRS_TO_CALIBRATE, days: MIN_DAYS_TO_CALIBRATE,
  errorSd: 400, errorBias: 0, ...over,
});

test("the base period runs May to April", () => {
  assert.equal(basePeriod(new Date("2026-08-19T12:00:00Z")).label, "May 2026 to April 2027");
  assert.equal(basePeriod(new Date("2027-04-30T12:00:00Z")).label, "May 2026 to April 2027");
  assert.equal(basePeriod(new Date("2027-05-01T12:00:00Z")).label, "May 2027 to April 2028");
});

test("hours are hour-ending in market time", () => {
  // HE17 on 14 July runs 16:00 to 17:00 EST, so it starts at 21:00 UTC.
  assert.equal(hourEndingStart("2026-07-14", 17).toISOString(), "2026-07-14T21:00:00.000Z");
  assert.equal(marketHour("2026-07-14T21:00:00Z"), 16);
  assert.equal(marketDay("2026-07-15T04:00:00Z"), "2026-07-14");
});

test("the top five counts at most one hour per day", () => {
  // Verified against IESO's own tracker: the five highest single hours of the
  // 2026 season were consecutive hours on one July evening, but the tracker
  // lists five different days.
  const rows = [
    { observed_at: "2026-07-14T21:00:00Z", value: 25646 },
    { observed_at: "2026-07-14T22:00:00Z", value: 25517 },
    { observed_at: "2026-07-14T20:00:00Z", value: 25384 },
    { observed_at: "2026-07-02T22:00:00Z", value: 24734 },
    { observed_at: "2026-07-13T22:00:00Z", value: 23982 },
  ];
  const top = rankTopHours(rows);
  assert.deepEqual(top.map((r) => Math.round(r.value)), [25646, 24734, 23982]);
  assert.equal(new Set(top.map((r) => marketDay(r.observed_at))).size, 3);
});

test("one overnight day does not stretch the peak window", () => {
  const rows = [];
  for (let day = 1; day <= 10; day += 1) {
    for (let hour = 0; hour < 24; hour += 1) {
      rows.push({
        observed_at: new Date(hourEndingStart(`2026-07-${String(day).padStart(2, "0")}`, 1)
          .getTime() + hour * 3600_000).toISOString(),
        value: 20000 + (hour === 17 ? 600 : 0),
      });
    }
  }
  for (let hour = 0; hour < 24; hour += 1) {
    rows.push({
      observed_at: new Date(hourEndingStart("2026-11-05", 1).getTime() + hour * 3600_000)
        .toISOString(),
      value: 15000 + (hour === 3 ? 100 : 0),
    });
  }
  rows.sort((a, b) => b.value - a.value);
  const window = peakHourWindow(rows);
  assert.deepEqual([...window].sort((a, b) => a - b), [16, 17, 18]);
});

test("a probability is withheld until it is calibrated on enough distinct days", () => {
  assert.equal(isCalibrated(calibrated({ pairs: 5 })), false);
  const crowded = calibrated({ pairs: 90, days: 3 });
  assert.equal(isCalibrated(crowded), false);
  assert.match(shortfall(crowded), /3 delivery day\(s\)/);
  assert.equal(probabilityOfExceeding(23800, 23833, crowded), null);
});

test("the probability is a half on the line and never zero or one", () => {
  const h = calibrated();
  assert.ok(Math.abs(probabilityOfExceeding(23833, 23833, h) - 0.5) < 1e-6);
  assert.ok(Math.abs(probabilityOfExceeding(23433, 23833, h) - 0.1587) < 2e-3);
  assert.equal(probabilityOfExceeding(15000, 23833, h), PROBABILITY_FLOOR);
  assert.equal(probabilityOfExceeding(30000, 23833, h), 1 - PROBABILITY_FLOOR);
});

test("a forecast that runs low raises the probability", () => {
  assert.ok(probabilityOfExceeding(23600, 23833, calibrated({ errorBias: 300 }))
    > probabilityOfExceeding(23600, 23833, calibrated()));
});

test("the normal tail agrees with the values the Python is checked against", () => {
  assert.ok(Math.abs(normalTail(0) - 0.5) < 1e-6);
  assert.ok(Math.abs(normalTail(1) - 0.158655) < 1e-4);
  assert.ok(Math.abs(normalTail(-1) - 0.841345) < 1e-4);
});

test("forecasts are filed by horizon and the freshest wins", () => {
  const issued = Date.parse("2026-08-19T13:00:00Z");       // 08:00 EST
  assert.equal(leadBucket(Date.parse("2026-08-19T14:00:00Z"), issued), "|lead0h");
  assert.equal(leadBucket(Date.parse("2026-08-19T22:00:00Z"), issued), "|lead6h");
  assert.equal(leadBucket(Date.parse("2026-08-20T04:00:00Z"), issued), "|lead12h");
  assert.equal(leadBucket(Date.parse("2026-08-19T12:00:00Z"), issued), "|leadpast");

  const best = freshestForecasts([
    { observed_at: "2026-08-20T21:00:00Z", series_key: "forecast_ontario_demand|lead24h", value: 22000 },
    { observed_at: "2026-08-20T21:00:00Z", series_key: "forecast_ontario_demand|lead6h", value: 24100 },
    { observed_at: "2026-08-20T21:00:00Z", series_key: "forecast_ontario_demand|leadpast", value: 19000 },
  ]);
  assert.deepEqual(best.get("2026-08-20T21:00:00Z"), { rank: 6, value: 24100, bucket: "|lead6h" });
});

test("only the demand dataset is read out of the tracker file", () => {
  // Six datasets share that file on different measurement bases. Blending the
  // AQEW ones in would quietly move the threshold everything is judged against.
  const xml = `<Document><xmldata>
    <dataset datapointName="TOP_ONTARIO_DEMAND">
    <datapoint><datetimeInfo><deliveryDate>2026-07-14</deliveryDate>
    <deliveryHour>17</deliveryHour></datetimeInfo><value>25646.25</value></datapoint>
    </dataset>
    <dataset datapointName="TOP_AQEW_VALUES">
    <datapoint><datetimeInfo><deliveryDate>2026-07-14</deliveryDate>
    <deliveryHour>17</deliveryHour></datetimeInfo><value>24865.78</value></datapoint>
    </dataset></xmldata></Document>`;
  const rows = parseTracker(xml);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].value, 25646.25);
  assert.equal(rows[0].observed_at, "2026-07-14T21:00:00Z");
});

test("a forecast payload files each hour by its horizon", () => {
  const xml = `<Document><DocHeader><CreatedAt>2026-08-19T08:00:00</CreatedAt></DocHeader>
    <DocBody><DeliveryDate>2026-08-19</DeliveryDate><OntarioDemand><ForecastOntDemand>
    <Demand><DeliveryHour>10</DeliveryHour><EnergyMW>19000</EnergyMW></Demand>
    <Demand><DeliveryHour>18</DeliveryHour><EnergyMW>23000</EnergyMW></Demand>
    <Demand><DeliveryHour>24</DeliveryHour><EnergyMW>20000</EnergyMW></Demand>
    </ForecastOntDemand></OntarioDemand></DocBody></Document>`;
  const keys = Object.fromEntries(parseForecast(xml, "src").map((r) => [r.observed_at, r.series_key]));
  assert.equal(keys["2026-08-19T14:00:00Z"], "forecast_ontario_demand|lead0h");
  assert.equal(keys["2026-08-19T22:00:00Z"], "forecast_ontario_demand|lead6h");
  assert.equal(keys["2026-08-20T04:00:00Z"], "forecast_ontario_demand|lead12h");
});

test("the demand archive yields Ontario demand, not market demand", () => {
  const csv = `\\\\Hourly Demand Report,,,
\\\\Created at 2026-08-19 07:30:09,,,
\\\\For 2026,,,
Date,Hour,Market Demand,Ontario Demand
2026-08-19,1,19489,16526
2026-08-19,17,25000,23833
`;
  const rows = parseDemand(csv);
  assert.equal(rows.length, 2);
  assert.equal(rows[1].value, 23833);                       // the fourth column
  assert.equal(rows[1].observed_at, "2026-08-19T21:00:00Z");
});


test("the market offset is defined once and never retyped", async () => {
  // All six sites were correct when this was written, which is exactly the
  // problem: a wrong sign in one would shift which hour a forecast is matched
  // against, corrupting the calibration rather than crashing. The constants
  // drift test reads constants.js only, so it would pass straight through it.
  const { readFileSync, readdirSync } = await import("node:fs");
  const dir = new URL("./src/", import.meta.url);
  for (const name of readdirSync(dir)) {
    if (!name.endsWith(".js") || name === "peak.js" || name === "constants.js") continue;
    const body = readFileSync(new URL(name, dir), "utf8");
    assert.equal(/5\s*\*\s*3600_?000|['"`]-5 hours/.test(body), false,
      `${name} hardcodes the market offset instead of importing it`);
  }
});

test("the SQL shift is derived from the constant", async () => {
  const { MARKET_SQL_SHIFT, marketDayStart, toMarketTime } = await import("./src/peak.js");
  assert.equal(MARKET_SQL_SHIFT, "-5 hours");
  assert.equal(marketDayStart("2026-08-20").toISOString(), "2026-08-20T05:00:00.000Z");
  assert.equal(toMarketTime(Date.parse("2026-08-20T05:00:00Z")).toISOString(),
    "2026-08-20T00:00:00.000Z");
});


test("a session cookie is signed, expiring, and tied to the password", async () => {
  const { signSession, validSession } = await import("./src/auth.js");
  const future = Math.floor(Date.now() / 1000) + 3600;
  const cookie = await signSession("correct horse battery", future);

  assert.equal(await validSession("correct horse battery", `sigarb_session=${cookie}`), true);
  // Changing the password ends every session, because the signing key is
  // derived from it. That is what makes set-password.sh a real revocation.
  assert.equal(await validSession("a different password", `sigarb_session=${cookie}`), false);
  // An expiry can be read and edited by anyone holding the cookie, so it has to
  // be covered by the signature rather than merely trusted.
  const forged = await signSession("correct horse battery", future);
  const tampered = forged.replace(/^\d+/, String(future + 86400));
  assert.equal(await validSession("correct horse battery", `sigarb_session=${tampered}`), false);
  assert.equal(await validSession("correct horse battery", "sigarb_session=nonsense"), false);
  assert.equal(await validSession("correct horse battery", ""), false);
});

test("an expired session is refused without checking the signature", async () => {
  const { signSession, validSession } = await import("./src/auth.js");
  const past = Math.floor(Date.now() / 1000) - 60;
  const cookie = await signSession("pw", past);
  assert.equal(await validSession("pw", `sigarb_session=${cookie}`), false);
});

test("the session cookie is host-only, http-only and secure", async () => {
  const { sessionCookie } = await import("./src/auth.js");
  const header = sessionCookie("value", 100);
  assert.match(header, /HttpOnly/);
  assert.match(header, /Secure/);
  assert.match(header, /SameSite=Lax/);
  assert.match(header, /Path=\//);
  // No Domain attribute: on workers.dev a shared parent would be the wrong
  // scope for a cookie that belongs to this subdomain alone.
  assert.equal(/Domain=/i.test(header), false);
});

test("no route falls back to serving the page without a check", async () => {
  const { readFileSync } = await import("node:fs");
  const source = readFileSync(new URL("./src/index.js", import.meta.url), "utf8");
  // renderDashboard must be reached only after signedIn or a bearer token has
  // been established, never from an earlier branch.
  const guard = source.indexOf("if (!signedIn && !(await bearerOk(request, env)))");
  const render = source.indexOf("renderDashboard({");
  assert.ok(guard > 0 && render > guard, "the dashboard is rendered before the access check");
  assert.match(source, /__scheduled/, "the test endpoint is not blocked in production");
});

// --------------------------------------------------------------------------
// The relay
// --------------------------------------------------------------------------

// `timingSafeEqual` is a Workers extension to WebCrypto that node does not
// have, and the routes below cannot be reached without it. The shim is a plain
// byte comparison: it stands in for the guard, it does not stand in for the
// timing property, which is exercised against the deployed Worker.
if (!globalThis.crypto.subtle.timingSafeEqual) {
  globalThis.crypto.subtle.timingSafeEqual = (a, b) => {
    const x = new Uint8Array(a);
    const y = new Uint8Array(b);
    return x.length === y.length && x.every((byte, i) => byte === y[i]);
  };
}

/** An R2 bucket, in memory, with only the surface the relay actually uses. */
function fakeBucket() {
  const objects = new Map();
  return {
    objects,
    async put(key, body, options) { objects.set(key, { body, options }); return { key }; },
    async get(key) {
      const held = objects.get(key);
      return held ? { body: held.body, size: 0, customMetadata: held.options?.customMetadata } : null;
    },
    async list({ prefix, limit = 1000, startAfter }) {
      const keys = [...objects.keys()]
        .filter((key) => key.startsWith(prefix) && (!startAfter || key > startAfter)).sort();
      return { objects: keys.slice(0, limit).map((key) => ({ key })), truncated: false };
    },
    async delete(keys) { for (const key of [].concat(keys)) objects.delete(key); },
  };
}

/** A D1 handle that answers the two statements the relay makes of it. */
function fakeDb(relayRows = []) {
  const written = [];
  const make = (sql, args = []) => ({
    sql,
    args,
    bind: (...next) => make(sql, next),
    async all() { return { results: sql.includes("relay_state") ? relayRows : [] }; },
    async run() { return { meta: { changes: 0 } }; },
  });
  return {
    written,
    prepare: (sql) => make(sql),
    async batch(statements) {
      written.push(...statements.map((s) => s.args));
      return statements.map(() => ({ meta: { changes: 1 } }));
    },
  };
}

/** Answer every IESO fetch with the same body, and count the calls. */
function stubFetch(body, { status = 200, headers = {} } = {}) {
  const seen = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push({ url, headers: init?.headers || {} });
    return new Response(status === 304 ? null : body, {
      status,
      headers: { "content-type": "text/xml", etag: '"abc"', ...headers },
    });
  };
  return { seen, restore: () => { globalThis.fetch = original; } };
}

test("the object stamp is the local filename's, floored to the second", async () => {
  const { stamp, objectKey } = await import("./src/relay.js");
  // sigarb/fetch.py names files from timeutil.iso, which is second precision.
  // A fraction leaking through makes strptime("%Y%m%dT%H%M%SZ") fail on the Mac,
  // and rebuild_from_raw then silently dates the payload from the file's mtime.
  assert.equal(stamp(new Date("2026-08-23T16:02:17.481Z")), "20260823T160217Z");
  assert.equal(stamp(new Date("2026-08-23T16:02:17Z")), "20260823T160217Z");
  assert.match(stamp(new Date()), /^\d{8}T\d{6}Z$/);

  const key = objectKey("on_ieso_zonal_price", new Date("2026-08-23T16:02:17.481Z"), "abcdef0123456789", "xml");
  assert.equal(key, "raw/on_ieso_zonal_price/20260823/20260823T160217Z-abcdef012345.xml");
  // The tail after the last slash is the local filename minus the .gz.
  assert.equal(key.split("/").pop(), "20260823T160217Z-abcdef012345.xml");
});

test("keys sort chronologically inside a source, so pull can resume from its own newest file", async () => {
  const { objectKey } = await import("./src/relay.js");
  const keys = [
    objectKey("s", new Date("2026-08-23T16:02:17Z"), "ffffffffffff", "xml"),
    objectKey("s", new Date("2026-08-23T15:57:00Z"), "000000000000", "xml"),
    objectKey("s", new Date("2026-09-01T00:00:00Z"), "888888888888", "xml"),
  ];
  assert.deepEqual([...keys].sort(), [keys[1], keys[0], keys[2]]);
});

test("an unchanged payload is stored once, not once per cycle", async () => {
  const { relayCycle } = await import("./src/relay.js");
  const stub = stubFetch("<xml>same</xml>");
  try {
    const env = { RAW: fakeBucket(), DB: fakeDb() };
    const first = await relayCycle(env, new Date("2026-08-23T16:00:00Z"), "2026-08-23T16:00:00Z", { all: true });
    assert.equal(first.stored, 7, "the first pass stores every source");

    // Second pass: the recorded hashes come back, and nothing new lands. The
    // 304 is deliberately not exercised here, because dedup must not depend on
    // it: a 200 carrying bytes already held has to be recognised on its own.
    const rows = [...env.RAW.objects.keys()].map((key) => ({
      source_id: key.split("/")[1],
      content_hash: null,
    }));
    const held = new Map();
    for (const [key, value] of env.RAW.objects) held.set(key.split("/")[1], value.options.customMetadata.sha256);
    for (const row of rows) row.content_hash = held.get(row.source_id);

    const env2 = { RAW: env.RAW, DB: fakeDb(rows) };
    const second = await relayCycle(env2, new Date("2026-08-23T16:05:00Z"), "2026-08-23T16:05:00Z", { all: true });
    assert.equal(second.stored, 0, "an identical payload was stored a second time");
    assert.equal(env.RAW.objects.size, 7);
  } finally { stub.restore(); }
});

test("a failed put leaves the hash and etag unrecorded, so the next cycle retries", async () => {
  const { relayCycle } = await import("./src/relay.js");
  const stub = stubFetch("<xml>body</xml>");
  try {
    const bucket = fakeBucket();
    bucket.put = async () => { throw new Error("R2 is down"); };
    const db = fakeDb();
    const result = await relayCycle({ RAW: bucket, DB: db }, new Date("2026-08-23T16:00:00Z"), "2026-08-23T16:00:00Z", { all: true });
    assert.equal(result.stored, 0);
    assert.equal(result.problems.length, 7);
    // saveRelayState binds content_hash at position 2 and etag at 7. Both must
    // be null, or a later conditional request answers 304 for a payload that
    // was never stored and the gap is permanent.
    for (const args of db.written) {
      assert.equal(args[1], null, "a content hash was recorded for a payload that failed to store");
      assert.equal(args[6], null, "an etag was recorded for a payload that failed to store");
    }
  } finally { stub.restore(); }
});

test("a broken relay does not report the peak line as broken", async () => {
  const worker = (await import("./src/index.js")).default;
  const stub = stubFetch("nope", { status: 500 });
  try {
    const db = fakeDb();
    const recorded = [];
    db.prepare = (sql) => {
      const make = (args = []) => ({
        sql,
        args,
        bind: (...next) => make(next),
        async all() { return { results: [] }; },
        async run() { if (sql.includes("INTO cycles")) recorded.push(args); return { meta: { changes: 1 } }; },
      });
      return make();
    };
    const request = new Request("https://example.com/run", {
      method: "POST", headers: { authorization: "Bearer t" },
    });
    const response = await worker.fetch(request, { DB: db, RAW: fakeBucket(), API_TOKEN: "t" }, {});
    const body = await response.json();
    // Every source, not merely some: a request arrives on an arbitrary minute
    // and must not be silently filtered down to nothing by the cadence gate.
    assert.equal(body.relayProblems.length, 7, "a hand-run cycle fetches every source");
    assert.deepEqual(body.problems, [], "the peak line has nothing wrong with it");
    // ok = 1 in the cycles row: the column the dashboard's heartbeat reads.
    assert.equal(recorded.at(-1)?.[1], 1, "an upstream feed failing marked the cycle unhealthy");
  } finally { stub.restore(); }
});

test("the bucket is reachable only with a bearer token, and only under raw/", async () => {
  const worker = (await import("./src/index.js")).default;
  const bucket = fakeBucket();
  await bucket.put("raw/on_ieso_zonal_price/20260823/20260823T160000Z-aaaaaaaaaaaa.xml", "payload", {});
  await bucket.put("state/peak-calls/latest.json", "the record", {});
  const env = { RAW: bucket, DB: fakeDb(), API_TOKEN: "t" };
  const auth = { authorization: "Bearer t" };
  const call = (path, headers = auth) => worker.fetch(new Request(`https://example.com${path}`, { headers }), env, {});

  assert.equal((await call("/objects", {})).status, 404, "listing without a token");
  assert.equal((await call("/object?key=raw/x", {})).status, 404, "reading without a token");
  assert.equal((await call("/objects?prefix=state/")).status, 404, "listing outside raw/");
  assert.equal((await call("/object?key=state/peak-calls/latest.json")).status, 404, "reading outside raw/");

  const listing = await (await call("/objects")).json();
  assert.equal(listing.objects.length, 1);
  assert.equal(await (await call(`/object?key=${listing.objects[0].key}`)).text(), "payload");
});

test("pruning deletes past the retention window and nothing inside it", async () => {
  const { prune, objectKey, PRUNE_WINDOW_DAYS } = await import("./src/relay.js");
  const { RELAY_RETENTION_DAYS } = await import("./src/constants.js");
  const bucket = fakeBucket();
  const now = new Date("2026-08-23T08:35:00Z");
  const day = (back) => new Date(now.getTime() - back * 86_400_000);
  const keep = objectKey("s", day(RELAY_RETENTION_DAYS - 1), "aaaaaaaaaaaa", "xml");
  const drop = objectKey("s", day(RELAY_RETENTION_DAYS + 1), "bbbbbbbbbbbb", "xml");
  const other = objectKey("t", day(RELAY_RETENTION_DAYS + 1), "cccccccccccc", "xml");
  // Older than the window a single pass looks at. One source is pruned per day,
  // so the window has to outlast a full rotation; anything beyond it waits.
  const ancient = objectKey("s", day(RELAY_RETENTION_DAYS + PRUNE_WINDOW_DAYS + 1), "dddddddddddd", "xml");
  for (const key of [keep, drop, other, ancient]) await bucket.put(key, "x", {});

  assert.equal(await prune(bucket, "s", now), 1);
  assert.deepEqual([...bucket.objects.keys()].sort(), [ancient, keep, other].sort());
});

test("the call record is snapshotted to the bucket under a dated and a stable key", async () => {
  const { snapshotCalls } = await import("./src/relay.js");
  const bucket = fakeBucket();
  const calls = [{ delivery_date: "2026-08-23", probability: 0.4 }];
  await snapshotCalls(bucket, calls, new Date("2026-08-23T08:35:00Z"));
  assert.deepEqual([...bucket.objects.keys()].sort(),
    ["state/peak-calls/2026-08-23.json", "state/peak-calls/latest.json"]);
  assert.deepEqual(JSON.parse(bucket.objects.get("state/peak-calls/latest.json").body).calls, calls);
});

test("the five minute feeds are fetched every cycle and the rest on the quarter hour", async () => {
  const { SOURCES, due } = await import("./src/ingest.js");
  const onFive = new Date("2026-08-23T16:05:00Z");
  const onQuarter = new Date("2026-08-23T16:15:00Z");
  const fast = SOURCES.filter((s) => s.everyMinutes === 5).map((s) => s.id);
  assert.equal(fast.length, 2, "the zonal price and the real time totals tick every five minutes");
  assert.deepEqual(SOURCES.filter((s) => due(s, onFive)).map((s) => s.id), fast);
  assert.equal(SOURCES.filter((s) => due(s, onQuarter)).length, SOURCES.length);
  // A hand-run cycle is not on a minute boundary and must still fetch.
  assert.equal(SOURCES.filter((s) => due(s, new Date("2026-08-23T16:07:41Z"), true)).length, SOURCES.length);
});

test("an unchanged peak payload is still parsed, so a missing row can come back", async () => {
  const { fetchAll, parseFetched, SOURCES } = await import("./src/ingest.js");
  const tracker = SOURCES.find((s) => s.parse);
  const body = '<dataset datapointName="TOP_ONTARIO_DEMAND">'
    + "<datetimeInfo><deliveryDate>2026-07-01</deliveryDate><deliveryHour>14</deliveryHour>"
    + "</datetimeInfo><value>23833</value></dataset>";
  const stub = stubFetch(body);
  try {
    const { sha256Hex } = await import("./src/ingest.js");
    const hash = await sha256Hex(new TextEncoder().encode(body).buffer);
    const state = new Map(SOURCES.map((s) => [s.id, { content_hash: hash, etag: '"abc"' }]));
    const outcomes = await fetchAll(new Date("2026-08-23T16:00:00Z"), { state, all: true });

    const seen = outcomes.find((o) => o.source.id === tracker.id);
    assert.equal(seen.status, "unchanged", "the hash matched, so nothing is stored");
    // ...but the rows still come out. Gating parsing on the relay's bookkeeping
    // would mean a row missing from D1 for any other reason is never rebuilt.
    assert.equal(parseFetched(seen).length, 1);
  } finally { stub.restore(); }
});

test("a parsed source is never asked conditionally, so a 304 cannot starve the peak line", async () => {
  const { fetchAll, SOURCES } = await import("./src/ingest.js");
  const stub = stubFetch("<xml/>");
  try {
    const state = new Map(SOURCES.map((s) => [s.id, { etag: '"abc"', last_modified: "yesterday" }]));
    await fetchAll(new Date("2026-08-23T16:00:00Z"), { state, all: true });
    for (const call of stub.seen) {
      const source = SOURCES.find((s) => call.url === s.url(new Date("2026-08-23T16:00:00Z")));
      const conditional = "If-None-Match" in call.headers;
      assert.equal(conditional, !source.parse, `${source.id} asked conditionally: ${conditional}`);
    }
  } finally { stub.restore(); }
});

test("the prune window outlasts a full rotation of the source list", async () => {
  const { PRUNE_WINDOW_DAYS } = await import("./src/relay.js");
  const { SOURCES } = await import("./src/ingest.js");
  // One source is pruned per day, so a day-prefix is revisited every
  // SOURCES.length days and nothing ever goes back for one the window missed.
  assert.ok(PRUNE_WINDOW_DAYS > SOURCES.length,
    "adding a source without widening the window would leak objects permanently");
});
