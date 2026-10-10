/**
 * The relay: raw payloads to R2, verbatim and unparsed.
 *
 * Hard invariant 1 says the raw payload is the source of truth and everything
 * else is derived from it. The first Cloudflare port did not honour that: it
 * parsed in memory and kept only the rows it wanted, so the peak line's own
 * inputs existed nowhere durable. This module closes that, and in the same pass
 * carries the three market signal feeds, which are relayed and never parsed
 * here at all. The detectors that read them run scikit-learn over three hundred
 * thousand observations, which is not a shape a Worker can hold, so Cloudflare
 * collects and the Mac detects.
 *
 * Two deliberate choices, both forced by the free plan:
 *
 * Nothing is compressed. `data/raw/` stores gzip and so does the backup, but
 * gzipping the 400 kB generator payload would cost roughly the whole 10 ms CPU
 * budget of the invocation that carries it, and that is the one payload the
 * relay cannot afford to drop. Uncompressed the relay writes about 24 MB a day,
 * which the retention window below bounds well inside R2's free 10 GB. The Mac
 * compresses on arrival, where CPU is free.
 *
 * Dedup is by content hash, never by the 304. A conditional request saves
 * bandwidth when the edge passes it through to IESO, but nothing depends on it
 * firing: a 200 carrying bytes already stored is recognised by its SHA-256 and
 * dropped. If conditional requests silently stopped working the relay would
 * cost more bandwidth and store exactly the same objects.
 */

import { RAW_PREFIX, STATE_PREFIX, RELAY_RETENTION_DAYS } from "./constants.js";
import { fetchAll, parseFetched, SOURCES } from "./ingest.js";
import { relayState, saveRelayState } from "./store.js";

// R2 deletes up to 1,000 keys per call, and the free plan allows 50 subrequests
// per invocation, so a prune pass is one list per day-prefix and one delete.
const DELETE_BATCH = 1000;

// How many day-prefixes a prune pass looks at. One source is pruned per pass and
// passes run once a day, so the window has to outlast a full rotation of the
// source list. Derived rather than typed: nothing revisits a day the window
// skipped, so an eighth source added without widening this would leak objects
// permanently and invisibly.
const PRUNE_SLACK_DAYS = 5;
export const PRUNE_WINDOW_DAYS = SOURCES.length + PRUNE_SLACK_DAYS;

const HOUR_MS = 3600_000;
const DAY_MS = 24 * HOUR_MS;

/**
 * `YYYYMMDDTHHMMSSZ`, whole seconds.
 *
 * This is `sigarb/fetch.py`'s filename stamp with the punctuation removed, and
 * the Mac parses it back with `strptime("%Y%m%dT%H%M%SZ")`. A stray fraction of
 * a second makes that parse fail, `rebuild_from_raw` falls back to the file's
 * mtime, and the provenance is quietly wrong rather than loudly broken. So the
 * flooring is explicit and a test pins it.
 */
export function stamp(date) {
  return new Date(Math.floor(date.getTime() / 1000) * 1000)
    .toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
}

/** The same instant as an ISO 8601 UTC string at second precision. */
export function isoSecond(date) {
  return new Date(Math.floor(date.getTime() / 1000) * 1000)
    .toISOString().replace(/\.\d+Z$/, "Z");
}

/**
 * `raw/<source>/<day>/<stamp>-<hash12>.<ext>`
 *
 * The stamp leads so keys sort chronologically inside a source, which is what
 * lets `signal pull` resume from its own newest local file with `startAfter`
 * instead of keeping a cursor. The tail after the last slash is exactly the
 * local filename minus `.gz`.
 */
export function objectKey(sourceId, at, contentHash, ext) {
  const marked = stamp(at);
  return `${RAW_PREFIX}${sourceId}/${marked.slice(0, 8)}/${marked}-${contentHash.slice(0, 12)}.${ext}`;
}

/** Write one payload. Returns the key it landed on. */
export async function store(bucket, { sourceId, at, body, contentHash, ext, contentType }) {
  const key = objectKey(sourceId, at, contentHash, ext);
  await bucket.put(key, body, {
    httpMetadata: contentType ? { contentType } : undefined,
    customMetadata: { sourceId, sha256: contentHash, fetchedAt: isoSecond(at) },
  });
  return key;
}

/** `YYYYMMDD` for a whole number of days before `now`. */
function dayKey(now, daysBack) {
  return new Date(now.getTime() - daysBack * DAY_MS).toISOString().slice(0, 10).replace(/-/g, "");
}

/**
 * Delete one source's objects that are past the retention window.
 *
 * R2 here is a relay, not an archive: the durable copies are `data/raw/` on the
 * Mac and the iCloud copy `signal backup` makes. The window is generous enough
 * that a Mac switched off for a month still loses nothing, and short enough that
 * the bucket cannot grow into the free tier's ceiling.
 *
 * One source per pass, because the free plan allows 50 subrequests per
 * invocation and a whole-bucket sweep would not fit beside the cycle's own work.
 */
export async function prune(bucket, sourceId, now, retentionDays = RELAY_RETENTION_DAYS) {
  const doomed = [];
  for (let back = retentionDays; back < retentionDays + PRUNE_WINDOW_DAYS; back += 1) {
    const listing = await bucket.list({
      prefix: `${RAW_PREFIX}${sourceId}/${dayKey(now, back)}/`,
      limit: DELETE_BATCH,
    });
    doomed.push(...listing.objects.map((o) => o.key));
    if (doomed.length >= DELETE_BATCH) break;
  }
  if (doomed.length === 0) return 0;
  await bucket.delete(doomed.slice(0, DELETE_BATCH));
  return Math.min(doomed.length, DELETE_BATCH);
}

/**
 * The track record, as JSON, beside the payloads.
 *
 * A call is a claim made at a moment and there is no archive of it anywhere, so
 * D1 holding the only copy is the sharpest single point of failure the project
 * has. Be precise about what this fixes: an object in R2 protects the record
 * against losing the database, not against losing the account. The Mac plus the
 * iCloud copy is still the only second copy that survives Cloudflare.
 */
export async function snapshotCalls(bucket, calls, now) {
  const body = JSON.stringify({ snapshotAt: isoSecond(now), calls }, null, 1);
  const day = now.toISOString().slice(0, 10);
  const options = { httpMetadata: { contentType: "application/json" } };
  await bucket.put(`${STATE_PREFIX}peak-calls/latest.json`, body, options);
  await bucket.put(`${STATE_PREFIX}peak-calls/${day}.json`, body, options);
  return [`${STATE_PREFIX}peak-calls/latest.json`, `${STATE_PREFIX}peak-calls/${day}.json`];
}

/**
 * Fetch every due source, put what is new into R2, and hand back the rows the
 * peak line's parsers produced.
 *
 * The etag and the content hash are recorded only after the object is safely in
 * R2. Recording them earlier would mean a failed put leaves the relay believing
 * it holds a payload it does not, and the next conditional request would answer
 * 304 forever.
 */
export async function relayCycle(env, now, ranAt, { all = false, force = false } = {}) {
  const problems = [];
  const state = env.DB ? await relayState(env.DB) : new Map();
  const outcomes = await fetchAll(now, { state, all, force });

  const entries = [];
  const rows = [];
  let fetched = 0;
  let stored = 0;
  let bytes = 0;

  for (const outcome of outcomes) {
    const entry = { sourceId: outcome.source.id, checkedAt: ranAt };

    if (outcome.status === "error") {
      entry.error = outcome.reason;
      problems.push(`${outcome.source.name}: ${outcome.reason}`);
      entries.push(entry);
      continue;
    }

    fetched += 1;
    rows.push(...parseFetched(outcome));

    if (outcome.status === "new") {
      try {
        if (!env.RAW) throw new Error("no R2 binding");
        entry.objectKey = await store(env.RAW, {
          sourceId: outcome.source.id, at: now, body: outcome.body,
          contentHash: outcome.contentHash, ext: outcome.source.ext,
          contentType: outcome.contentType,
        });
        entry.stored = true;
        entry.storedAt = ranAt;
        entry.contentHash = outcome.contentHash;
        entry.bytes = outcome.bytes;
        entry.etag = outcome.etag;
        entry.lastModified = outcome.lastModified;
        stored += 1;
        bytes += outcome.bytes;
      } catch (error) {
        entry.error = String(error?.message || error).slice(0, 160);
        problems.push(`${outcome.source.name}: ${entry.error}`);
      }
    }
    entries.push(entry);
  }

  if (env.DB) await saveRelayState(env.DB, entries).catch(() => {});
  return { rows, problems, fetched, stored, bytes };
}
