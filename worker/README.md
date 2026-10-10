# The peak line and the collector, on Cloudflare

The ICI peak call runs here rather than on a laptop. A launchd agent misses its
window whenever the machine sleeps, and the call is a morning decision against
ground truth that passes and does not come back: on 2026-08-20 the local agent
missed its morning entirely for exactly that reason, and that day is gone.

**The detectors did not move.** The market signal detectors run scikit-learn over
three hundred thousand observations and are not a shape a Worker can hold. They
stay in `sigarb/` and run on demand. But removing the local agent also stopped
the only thing *collecting* the feeds they read, and that history cannot be
backfilled past the upstream archive window of roughly three months. So
collection moved here even though detection could not: Cloudflare relays raw
payloads to R2 and the Mac pulls them down and detects on them.

## What runs

| Piece | Where |
|---|---|
| Cron, every 5 minutes | `src/index.js` `scheduled()` |
| The source table, fetching, and the four parsers | `src/ingest.js` |
| Raw payloads to R2, pruning, the record snapshot | `src/relay.js` |
| D1 reads and writes | `src/store.js` |
| The peak maths, ported from `sigarb/peak.py` | `src/peak.js` |
| Numbers shared with the Python | `src/constants.js` |
| The dashboard and the login page | `src/dashboard.js` |
| Sessions, password check, rate limiting | `src/auth.js` |

One trigger, two jobs, split by the minute it fires on. The relay runs every
cycle; the peak line runs on the quarter hour, so making the relay four times as
frequent left its cost untouched. Housekeeping runs at 08:35 UTC, deliberately a
minute the peak line does not use, so the two never share an invocation's
subrequest budget.

The call is made once a market morning, between 06:00 and 10:00 EST. Calls key
on `(region, delivery day, horizon)` and insert-or-ignore, so the first call at
each horizon is the one that stands. Cron delivery is at-least-once, which makes
that load bearing rather than tidy.

## The relay

Seven sources are fetched here. Four carry a parser and feed the peak line's D1
tables; three are the market signal feeds and are never parsed here at all. All
seven are written to R2 verbatim, which is what hard invariant 1 asks for and
what the first port of this Worker did not do: it parsed in memory and kept only
the rows it wanted, so the peak line's own inputs existed nowhere durable.

Keys are `raw/<source>/<day>/<stamp>-<hash12>.<ext>`, which is exactly the
filename `sigarb/fetch.py` writes plus a `.gz`. The stamp leads so keys sort
chronologically, which is what lets `signal pull` resume from its own newest
local file rather than keeping a cursor. Delete `data/raw/` and the next pull
refills it.

**Nothing is compressed on this side.** Gzipping the 400 kB generator payload
would cost roughly a whole invocation's 10 ms CPU budget, and that is the one
payload the relay cannot afford to drop. Uncompressed the relay writes about
24 MB a day; `RELAY_RETENTION_DAYS` bounds that well inside R2's free 10 GB. The
Mac compresses on arrival, where CPU is free, at roughly eleven to one.

**Dedup is by content hash, never by the 304.** Conditional requests save
bandwidth when the edge passes them through to IESO, but nothing depends on that
firing: a 200 carrying bytes already stored is recognised by its SHA-256 and
dropped. The etag and the hash are recorded only *after* the object is safely in
R2, so a failed put cannot leave the relay believing it holds a payload it does
not and answering 304 forever.

R2 is a relay, not an archive. The durable copies are `data/raw/` on the Mac and
the iCloud copy `signal backup` makes. One source is pruned per day, rotating, so
a sweep never crowds out the cycle's own work.

The track record is snapshotted to `state/peak-calls/` whenever a call is made
or settled, and once a day regardless. Be precise about what that buys: an object
in R2 protects the record against losing the database, **not** against losing the
account. The Mac is still the only second copy that survives Cloudflare.

## Access

A password login with a single password field.

- `GET /` with a valid session shows the dashboard; without one it shows a login
  page with a single password field.
- `POST /login` checks `DASHBOARD_PASSWORD` and sets a 30 day session cookie.
  Eight failures per address per 15 minutes, then 429, and a wrong password
  always answers a second slow so guessing costs time as well as attempts.
- `GET /logout` ends the session.
- Scripts send `Authorization: Bearer <API_TOKEN>` instead and never see the
  password. That is what `signal pull` uses.

**The session cookie is an HMAC-signed expiry, and the signing key is derived
from the password itself.** Nothing stores sessions, and changing the password
ends every one of them everywhere. That is what makes `./set-password.sh` a real
revocation rather than a rename.

This replaced a secret token in the URL path. The token worked, but it is the
weakest access control there is: it cannot be revoked for one device, it leaks
through browser history and through any link ever shared, and it made the bare
address useless. `preview_urls` is off, so there is exactly one address.

None of this is decoration. Every page here quotes IESO values under a licence
that is explicitly non-transferrable, and the address is public and guessable.

    ./set-password.sh                        # change the password, ends all sessions
    wrangler secret put API_TOKEN            # rotate the script credential

## Routes

| Route | Does |
|---|---|
| `/` | the dashboard, or the login page |
| `POST /login`, `/logout` | the session |
| `/api` | the same data as JSON; session cookie or bearer token |
| `POST /run` | run a cycle now, bearer token only; `?call=1` forces the call outside the morning window, `?force=1` ignores the recorded hashes, `?maintain=1` prunes and snapshots |
| `/objects` | list relayed payloads, bearer token only; `?prefix=`, `?after=`, `?limit=` |
| `/object` | one payload's bytes, bearer token only; `?key=` |

`/run`, `/objects` and `/object` deliberately refuse a session cookie. A browser
should not be able to trigger a cycle by following a link, and the bucket routes
are gated to the `raw/` prefix so the script credential cannot read the call
record out of `state/`.

`?force=1` is the bigger hammer of the two overrides: it ignores what the relay
has recorded and re-parses every payload, which is how a D1 store rebuilt from
empty gets its rows back. A plain `/run` already ignores the cadence, because a
request arrives on an arbitrary minute.

## The three budgets

All are free-plan limits and all shaped the code.

- **10 ms CPU per invocation.** Nothing builds a DOM. Each parser slices to the
  region it needs and runs one regex over it; the whole cycle parses in about
  2 ms against the live payloads.
- **100,000 D1 row writes a day, counted on attempts.** The demand archive holds
  a calendar year, so inserting it whole each cycle would spend the day's budget
  in nine fetches. `writeObservations` reads the newest stored timestamp first
  and trims the tail against it. Note that a write to this schema costs two rows
  written, the table and its index.

- **50 subrequests per invocation.** Seven fetches, up to seven R2 puts, a
  batched D1 write and the peak line's own queries fit; a whole-bucket prune
  would not, which is why housekeeping takes one source a day and runs on a
  minute the peak line skips.

Also relevant: 5 cron triggers per account, so this spends exactly one and
branches on the clock inside the handler. R2 is generous by comparison: puts and
lists are Class A operations against a million a month, and this spends roughly
33,000.

## Setting it up from nothing

```bash
cd worker
wrangler r2 bucket create signal-arbitrage-raw   # needs R2 enabled on the account
wrangler d1 create signal-arbitrage          # put the id in wrangler.jsonc
wrangler d1 execute signal-arbitrage --remote --file=schema.sql
python3 make-seed.py                         # history, from the local store
for f in seed/*.sql; do wrangler d1 execute signal-arbitrage --remote --file="$f"; done
./set-password.sh                            # the dashboard password
wrangler secret put API_TOKEN                # a long random string, for scripts
wrangler deploy
```

The seed matters. The Worker computes its own calibration from history it holds,
so without it the engine would sit uncalibrated for a month waiting for an
archive it could have read on day one.

## Market time lives in one place

`MARKET_UTC_OFFSET_HOURS` in `constants.js`, and everything else imports
`toMarketTime`, `fromMarketTime`, `marketDayStart` or `MARKET_SQL_SHIFT` from
`peak.js`. Retyping `5 * 3600_000` or `'-5 hours'` anywhere else is a test
failure, deliberately: all six original sites were correct, which is exactly why
a wrong sign in one would never have been noticed. It would shift which hour a
forecast is matched against and corrupt the calibration rather than crash.

Once a year, between 00:00Z and 05:00Z on 1 January, the demand fetch asks for
the new year's file while the market day is still 31 December. Those five hours
arrive with the next day's file, so a 31 December call settles a day late rather
than never.

## Keeping the two implementations honest

The peak maths exists twice, in Python and in JavaScript. That is a real risk, so
`tests/test_engine.py::test_the_worker_constants_match_the_python_ones` reads
`src/constants.js` and fails if any value has drifted from its Python
counterpart, including the horizon buckets in the source YAML. That catches a
number changed on one side; `test.mjs` catches behaviour that drifted without
any constant moving, which is the likelier failure. Both run under `pytest`, so
one command remains the gate.

Verified on 2026-08-21: for the same three delivery days both produced the same
forecast peaks, the same pair and day counts, the same error bias and standard
deviation, and the same decision to withhold the day-ahead probability.
