/**
 * Signal Arbitrage on Cloudflare: the peak line, running where a laptop lid
 * cannot close on it, and the collector for everything the Mac detects on.
 *
 * The move off the Mac was not cosmetic. A launchd agent misses its window
 * whenever the machine is asleep, and the ICI peak call is a morning decision
 * against ground truth that passes and does not come back. This runs on a cron
 * trigger in Cloudflare's network instead.
 *
 * Two jobs, one invocation, split by the minute it fires on:
 *
 * The peak line runs on the quarter hour. It parses IESO's tracker, forecast
 * and actual demand into D1, makes the morning call and settles yesterday's.
 *
 * The relay runs every five minutes. It carries raw payloads to R2 verbatim and
 * parses nothing, because the market signal detectors run scikit-learn over
 * three hundred thousand observations and are not a shape a Worker can hold.
 * They stay local and on demand, reading what this collected. See src/relay.js.
 *
 * Access. Everything here quotes IESO values, and their terms grant a licence
 * that is explicitly non-transferrable, so this must not be an open endpoint.
 * The bare address serves a password login and, once signed in, the dashboard;
 * scripts use a bearer token instead, so nothing without a browser is ever given
 * the password. See src/auth.js for why this replaced a token in the URL path.
 */

import { SOURCES } from "./ingest.js";
import {
  writeObservations, buildReport, recordCalls, settleCalls, scoreboard,
  recordCycle, recentCycles, recentCalls, storeCounts, relayHealth, allCalls,
  pruneCycles,
} from "./store.js";
import { marketHour, NOTIFY_ABOVE } from "./peak.js";
import { renderDashboard, renderLogin } from "./dashboard.js";
import * as relay from "./relay.js";
import { RAW_PREFIX } from "./constants.js";
import {
  validSession, signSession, timingSafeEqualStr, sessionCookie, SESSION_SECONDS,
  failureCount, recordFailure, clearFailures, FAIL_LIMIT, WRONG_PASSWORD_DELAY_MS,
} from "./auth.js";

// The call is a morning decision, made before the afternoon it is about. Market
// time, because the delivery day is a market day.
const CALL_WINDOW_START_HOUR = 6;
const CALL_WINDOW_END_HOUR = 10;

// The peak line's rhythm, unchanged by the relay running four times as often.
const PEAK_EVERY_MINUTES = 15;

// Housekeeping, once a day, deliberately on a minute the peak line does not use
// so the two never share an invocation's subrequest budget.
const MAINTENANCE_HOUR_UTC = 8;
const MAINTENANCE_MINUTE_UTC = 35;

// How many objects one listing hands back to `signal pull`.
const LIST_LIMIT = 1000;

const notFound = () => new Response("Not found", {
  status: 404,
  headers: { "content-type": "text/plain; charset=utf-8" },
});

const html = (body, status = 200) => new Response(body, {
  status,
  headers: {
    "content-type": "text/html; charset=utf-8",
    // Nothing between here and the reader may keep a copy of a private page.
    "cache-control": "private, no-store",
    "x-robots-tag": "noindex, nofollow",
    "referrer-policy": "no-referrer",
  },
});

/** A script's credential: `Authorization: Bearer <API_TOKEN>`. */
async function bearerOk(request, env) {
  if (!env.API_TOKEN) return false;
  const header = request.headers.get("authorization") || "";
  if (!header.startsWith("Bearer ")) return false;
  return timingSafeEqualStr(header.slice(7), env.API_TOKEN);
}

/**
 * Once a day: expire one source's old objects and trim the cycle log.
 *
 * One source per pass, rotating, because a whole-bucket sweep would not fit in
 * the 50 subrequests an invocation is allowed alongside everything else.
 */
async function runMaintenance(env, now) {
  const cycles = await pruneCycles(env.DB, now).catch(() => 0);
  if (!env.RAW) return { pruned: 0, prunedFrom: null, cycles };
  const source = SOURCES[Math.floor(now.getTime() / 86_400_000) % SOURCES.length];
  return { pruned: await relay.prune(env.RAW, source.id, now), prunedFrom: source.id, cycles };
}

async function runCycle(env, now, { makeCall, peak, all = false, force = false, maintain = false }) {
  const started = Date.now();
  const ranAt = relay.isoSecond(now);
  // The peak line's problems and the relay's are kept apart on purpose. They
  // fail for different reasons and mean different things, and one array would
  // make a zonal price hiccup report the peak line as broken.
  const problems = [];
  const relayProblems = [];
  let written = 0;
  let calls = 0;
  let settled = 0;
  let fetched = 0;
  let stored = 0;
  let bytes = 0;
  let pruned = 0;
  let prunedFrom = null;
  let cyclesTrimmed = 0;
  let snapshot = null;

  let rows = [];
  try {
    const result = await relay.relayCycle(env, now, ranAt, { all, force });
    rows = result.rows;
    fetched = result.fetched;
    stored = result.stored;
    bytes = result.bytes;
    relayProblems.push(...result.problems);
  } catch (error) {
    relayProblems.push(String(error?.stack || error).slice(0, 400));
  }

  try {
    written = await writeObservations(env.DB, rows);
    if (peak) settled = await settleCalls(env.DB, now);
    if (makeCall) {
      const report = await buildReport(env.DB, now);
      calls = await recordCalls(env.DB, report, ranAt);
    }
  } catch (error) {
    problems.push(String(error?.stack || error).slice(0, 400));
  }

  try {
    if (maintain) {
      ({ pruned, prunedFrom, cycles: cyclesTrimmed } = await runMaintenance(env, now));
    }
    // The record is the one thing here that cannot be re-derived, so a second
    // copy is written whenever it moves, and once a day regardless.
    if (env.RAW && (calls > 0 || settled > 0 || maintain)) {
      [snapshot] = await relay.snapshotCalls(env.RAW, await allCalls(env.DB), now);
    }
  } catch (error) {
    relayProblems.push(String(error?.stack || error).slice(0, 400));
  }

  // A cycle that could not even record itself is not worth failing the run over.
  await recordCycle(env.DB, {
    ranAt, ok: problems.length === 0, ms: Date.now() - started, fetched, written,
    detail: problems.length ? problems.join(" · ").slice(0, 900) : null,
  }).catch(() => {});

  return {
    ok: problems.length === 0 && relayProblems.length === 0,
    written, fetched, calls, settled, stored, bytes,
    pruned, prunedFrom, cyclesTrimmed, snapshot,
    problems, relayProblems,
  };
}

export default {
  async scheduled(controller, env, ctx) {
    const now = new Date(controller.scheduledTime);
    const minute = now.getUTCMinutes();
    const hour = marketHour(now.toISOString());
    const peak = minute % PEAK_EVERY_MINUTES === 0;
    // The record keeps the first call at each horizon, so running the call on
    // every cycle in the window would be harmless but pointless. Restricting it
    // to the window keeps the intent legible and the write budget small.
    const makeCall = peak && hour >= CALL_WINDOW_START_HOUR && hour <= CALL_WINDOW_END_HOUR;
    const maintain = now.getUTCHours() === MAINTENANCE_HOUR_UTC
      && minute === MAINTENANCE_MINUTE_UTC;

    ctx.waitUntil(runCycle(env, now, { makeCall, peak, maintain }).then((result) => {
      if (result.problems.length) console.error("peak:", result.problems.join(" · "));
      if (result.relayProblems.length) console.error("relay:", result.relayProblems.join(" · "));
    }));
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    // The testing endpoint must never be reachable in production.
    if (url.pathname.startsWith("/__scheduled") && env.ENVIRONMENT === "production") {
      return notFound();
    }

    if (request.method === "POST" && url.pathname === "/login") {
      const ip = request.headers.get("cf-connecting-ip") || "unknown";
      if (await failureCount(env.DB, ip) >= FAIL_LIMIT) {
        return html(renderLogin({
          message: "Too many attempts. Try again in 15 minutes.", status: 429,
        }), 429);
      }
      const form = await request.formData();
      const given = String(form.get("password") || "");
      if (env.DASHBOARD_PASSWORD && await timingSafeEqualStr(given, env.DASHBOARD_PASSWORD)) {
        await clearFailures(env.DB, ip);
        const expiry = Math.floor(Date.now() / 1000) + SESSION_SECONDS;
        return new Response(null, {
          status: 303,
          headers: {
            location: "/",
            "set-cookie": sessionCookie(
              await signSession(env.DASHBOARD_PASSWORD, expiry), SESSION_SECONDS,
            ),
          },
        });
      }
      await recordFailure(env.DB, ip);
      // Answer slowly, so guessing costs time as well as attempts.
      await new Promise((resolve) => setTimeout(resolve, WRONG_PASSWORD_DELAY_MS));
      return html(renderLogin({ message: "Wrong password." }), 401);
    }

    if (url.pathname === "/logout") {
      return new Response(null, {
        status: 303,
        headers: { location: "/", "set-cookie": sessionCookie("", 0) },
      });
    }

    // Machines only, and gated exactly as `/run` is. A browser session must not
    // reach the bucket by following a link, and the bearer token must not reach
    // anything in it other than the raw payloads.
    if (url.pathname === "/objects" || url.pathname === "/object") {
      if (!(await bearerOk(request, env))) return notFound();
      if (!env.RAW) return new Response("no bucket bound", { status: 503 });

      if (url.pathname === "/objects") {
        const prefix = url.searchParams.get("prefix") || RAW_PREFIX;
        if (!prefix.startsWith(RAW_PREFIX)) return notFound();
        const listing = await env.RAW.list({
          prefix,
          startAfter: url.searchParams.get("after") || undefined,
          limit: Math.min(Number(url.searchParams.get("limit")) || LIST_LIMIT, LIST_LIMIT),
        });
        return Response.json({
          truncated: listing.truncated,
          objects: listing.objects.map((o) => ({
            key: o.key, size: o.size, uploaded: o.uploaded, sha256: o.customMetadata?.sha256,
          })),
        });
      }

      const key = url.searchParams.get("key") || "";
      if (!key.startsWith(RAW_PREFIX)) return notFound();
      const object = await env.RAW.get(key);
      if (!object) return notFound();
      return new Response(object.body, {
        headers: {
          "content-type": object.httpMetadata?.contentType || "application/octet-stream",
          "content-length": String(object.size),
          "x-sha256": object.customMetadata?.sha256 || "",
        },
      });
    }

    if (url.pathname === "/run") {
      // Machines only. A browser session should not be able to trigger a cycle
      // by following a link.
      if (!(await bearerOk(request, env))) return notFound();
      if (request.method !== "POST") return new Response("POST to run a cycle", { status: 405 });
      const now = new Date();
      const hour = marketHour(now.toISOString());
      // A hand-run cycle arrives on an arbitrary minute, so it ignores the
      // cadence and fetches every source. `force` goes further and ignores the
      // hashes the relay has recorded, which is how a store rebuilt from empty
      // gets its rows back.
      const result = await runCycle(env, now, {
        peak: true,
        all: true,
        force: url.searchParams.get("force") === "1",
        maintain: url.searchParams.get("maintain") === "1",
        makeCall: url.searchParams.get("call") === "1"
          || (hour >= CALL_WINDOW_START_HOUR && hour <= CALL_WINDOW_END_HOUR),
      });
      return Response.json(result, { status: result.ok ? 200 : 500 });
    }

    if (url.pathname !== "/" && url.pathname !== "/api") return notFound();
    const signedIn = await validSession(env.DASHBOARD_PASSWORD, request.headers.get("cookie"));
    if (!signedIn && !(await bearerOk(request, env))) {
      // A browser gets the front door. Anything else gets nothing to work with.
      return url.pathname === "/api"
        ? new Response("unauthorized", { status: 401 })
        : html(renderLogin());
    }

    const now = new Date();
    const [report, board, cycles, calls, counts, relayRows] = await Promise.all([
      buildReport(env.DB, now),
      scoreboard(env.DB),
      recentCycles(env.DB),
      recentCalls(env.DB),
      storeCounts(env.DB),
      relayHealth(env.DB),
    ]);

    if (url.pathname === "/api") {
      return Response.json({
        generatedAt: now.toISOString(), report, board, cycles, calls, counts, relay: relayRows,
      });
    }

    return html(renderDashboard({
      report, board, cycles, calls, counts, now, relay: relayRows, notifyAbove: NOTIFY_ABOVE,
    }));
  },
};
