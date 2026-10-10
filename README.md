# Signal Arbitrage

> ### 🛑 Abandoned
> This was a side project, built over a few weeks in August 2026. **It is no
> longer maintained and will not be worked on again.** The code is published
> as-is, with no support, no fixes and no roadmap. The Cloudflare deployment it
> describes has been taken down, and no data from the feeds it read ships with it.
>
> It never had a paying subscriber and never went looking for one.
>
> **If you think there's something here worth building on, see
> [Interested in it?](#interested-in-it) at the bottom.** I'm open to talking.

A region-agnostic engine that ingests public data feeds, detects threshold
breaches and anomalies, and emits signals carrying provenance, confidence and
disclaimer metadata. Ontario was the first and only node.

What the build does and where it departs from the original pitch is in
[`docs/deviations.md`](docs/deviations.md).

## How far it got

The question it existed to answer: can public feeds yield at least one class of
signal that is materially earlier or sharper than what an interested
professional already knows, at a false-positive rate low enough to stay
subscribed to? It stopped before that was answered, and the record says why.

**The peak line** was the main bet. Each day it estimated the probability that
Ontario demand would set one of the year's five Industrial Conservation
Initiative peak hours, from IESO's own forecast and the measured history of that
forecast's error. Between 19 August and 9 October 2026 it made 174 calls, at
horizons from same day to 14 days ahead, and graded 170 of them against actual
demand before it was shut down.

- Not one day crossed the threshold of 23,833 MW. The closest fell 1,419 MW
  short. The 2026 season's top five hours had all been set in June and July,
  before the engine started.
- A near-zero Brier score on that record proves nothing. Calling "almost
  certainly not" through a stretch with no events is easy. The real test, a
  summer peak season, never arrived.
- Eight of the 5 and 14 day calls put the chance at 10% or more, the highest at
  41%, and every one of those days fell well short. Too few to call it
  miscalibration.

**The market signal line** fired 66 signals, all in one backfill run on
19 August 2026. Its local scheduler stopped soon after and none of them was ever
graded.

**Never resolved:** whether IESO permits redistributing alerts derived from its
data. Nothing IESO-derived could have been sold without that answer, and the
question was never asked.

---

## What it actually does

1. **Ingests** six verified public feeds on their own cadence, storing every
   response verbatim on disk before parsing it.
2. **Normalises** into two tables: numeric `observations` and text `events`,
   both keyed by region so a second node is a config drop.
3. **Detects** with four detectors: rolling z-score, percentage move, absolute
   level, and an isolation forest. Text feeds use keyword novelty.
4. **Emits** signals with provenance, a confidence score, disclaimer flags and a
   licence scope attached to every one.
5. **Grades itself.** `signal review` marks past signals against what the series
   did next, which is the instrument the kill criterion is judged on.

## Quick start

```bash
git clone <repo> && cd signal-arbitrage
./setup.sh                 # venv, pinned deps, installs the 'signal' launcher
signal                     # interactive menu, the front door
```

Then, once:

```bash
signal backfill            # pull dated history so detectors have a baseline
signal update --backfill   # detect across all of it
```

## Where it runs

**The Cloudflare deployment has been taken down.** This section describes how
it ran. The R2 relay was built and tested against the local Workers runtime but
never deployed.

Two halves, and the split is forced rather than chosen.

**The peak line runs on Cloudflare**, on a cron every 5 minutes, with its own
D1 store and a dashboard behind a password login. See [`worker/`](worker/). It moved there
on 2026-08-21 because a laptop is the wrong host for a thing whose whole value
is not missing a morning: the local agent missed 2026-08-20 entirely because the
machine was asleep, and that day's ground truth is gone.

**Collection runs there too, for every feed.** The Worker writes each fetched
payload to R2 verbatim under `raw/<source>/<day>/<stamp>-<hash>.<ext>`, which is
byte for byte the filename `data/raw/` uses plus a `.gz`. `signal pull` brings
them down and gzips them into place, so `signal rebuild` cannot tell a relayed
payload from a locally fetched one. Nothing is compressed on the Worker's side:
gzipping the largest payload would cost roughly a whole invocation's CPU budget
on the free plan.

**The market signal detectors stay here.** They run scikit-learn over three
hundred thousand observations, which is not a shape a Worker can hold. Run
`signal update` when you want them, over whatever the last pull brought home.

`signal peak` still works here, but it computes from the local store and no
longer records anything. Treat it as a spot check, not the record: the two
stores collect independently, so its numbers drift from the dashboard's as soon
as their observation sets differ. Two stores writing the same call would make
"the first call at each horizon stands" mean "whichever database you happened to
ask". Use `--record` only if the Worker is down.

**The record needs pulling back.** It lives in one D1 database and nowhere else,
and unlike a raw payload a call cannot be re-fetched from anywhere: it is a claim
made at a moment. `signal pull` merges it into the local store and writes
`exports/worker-state.json`, which `signal backup` then copies off the machine.
The Worker also snapshots the record into R2 whenever a call is made or settled,
but that is the same Cloudflare account: it protects against losing the database,
not against losing the account. So run `signal pull` when you think of it, and
certainly before touching the D1 database. It never overwrites a local call,
since a conflict means the two stores disagree about a claim, which is something
to look at rather than to resolve silently. Configure it once with
`signal pull --configure`; the address and the API token go in the macOS
Keychain, never the repo. The API token is not the dashboard password: a browser
signs in, a script sends a bearer token, and the two are revoked separately.

## Commands

| Command | Does |
|---|---|
| `signal` | interactive menu exposing everything, prompting for what it needs |
| `signal update` | fetch anything due, run detectors, print new signals |
| `signal backfill` | pull dated archives so the detectors start with history |
| `signal signals` | recent signals; `--json` gives the machine payload |
| `signal sources` | what is configured and whether it is working |
| `signal check --live` | validate configs and call every endpoint |
| `signal peak` | the ICI peak call, recorded and graded as a side effect. Also runs on Cloudflare |
| `signal review` | grade past signals against what happened next |
| `signal schedule` | run unattended, or check that it is |
| `signal pull` | bring the record and the relayed raw payloads back from Cloudflare |
| `signal backup` | copy the raw payloads off this machine |
| `signal export` | write CSV and JSON to `exports/` |
| `signal rebuild` | disaster-recovery drill: rebuild the store from raw alone. `--check` diffs a scratch rebuild instead of replacing anything |

Every command works non-interactively for cron. With no terminal attached, bare
`signal` prints help instead of blocking on a menu.

## Layout

| Path | Holds |
|---|---|
| `sigarb/` | the package. Named `sigarb`, not `signal`, because a package called `signal` shadows the standard library and breaks scikit-learn |
| `sigarb/adapters/` | one module per wire format: rss, json_api, csv_feed, xml_feed |
| `sigarb/detect/` | one module per detector, each a pure function over readings |
| `sources/` | one YAML file per source, the region-agnostic ingestion schema |
| `data/raw/` | every fetched payload, gzipped and content-addressed. The source of truth |
| `data/signals.db` | the derived SQLite store. Rebuildable from `data/raw/` |
| `exports/` | CSV and JSON output |

## The peak call

The one output here with a price attached to it. Ontario's Industrial
Conservation Initiative sets a large consumer's Global Adjustment charge for a
whole year from their demand during the **five highest Ontario Demand hours** of
the May-to-April base period, counting at most one hour per day. Curtailing
during one of those hours is worth a lot; curtailing on a day that turns out not
to matter costs production for nothing.

`signal peak` shows the hours set so far, the hour to beat, and for each coming
day its forecast peak, the margin, and the chance of clearing the threshold.

Be clear about what is and is not ours. IESO publishes the running top hours, its
own demand forecast, and the actual demand that settles it, so **subtracting the
forecast from the threshold is public arithmetic and is not an edge**. What is
not published anywhere is how wrong that forecast usually is at a given horizon.
Every forecast reading is therefore filed by how many hours ahead of its delivery
hour it was issued, the error is measured per horizon against settled actuals,
and the margin becomes a probability. That measurement, and the graded record of
the calls it produces, is the only part of this that is the engine's own.

The probability is a normal approximation on measured error. It is withheld
entirely, and says so, until there are enough matched pairs at that horizon,
drawn from enough distinct delivery days, to mean anything. Days are counted as
well as pairs because the hours inside one day miss together: thirty pairs from
three days is three observations wearing a disguise.

Every call is written down when it is made, keyed so the first call at each
horizon is the one that stands and a later, easier call cannot overwrite it.
Once the delivery day settles, each call is graded against the threshold that
was in force at the time and scored with a Brier score. `signal review` reports
it, and reports plainly when the model is doing worse than always predicting the
base rate.

## Configured sources

Ten sources, each verified against live endpoints before its config was
written. Each records its licence, the terms check, and an honest read on
whether it carries any timing advantage.

| Source | Region | Cadence | Timing edge |
|---|---|---|---|
| IESO Ontario zonal energy price | ON | 5 min | **Yes.** A real clearing price on no calendar |
| IESO demand and operating reserve | ON | 5 min | **Yes.** Reserve shortfalls lead price spikes |
| IESO generator output by unit | ON | 1 hour | **Yes.** A nuclear trip is visible as output collapse |
| IESO ICI peak tracker | ON | 1 hour | **No, and none wanted.** It is the threshold to be measured against |
| IESO demand forecast, today | ON | 1 hour | **No.** Published. The edge is in its measured error |
| IESO demand forecast, day ahead | ON | 1 hour | **No.** Same, one delivery day further out |
| IESO actual hourly demand | ON | 1 hour | **No, and none wanted.** Ground truth. One file holds the year |
| Ontario government news releases | ON | 20 min | Partial. Policy lands here first, advisories are noise |
| Bank of Canada USD/CAD | CA | 6 hours | **No.** Daily average, published on a calendar |
| Canada Gazette Part I | CA | 12 hours | **No.** Weekly, and issue-level only. Context, emits nothing |

## How confidence is computed

It is a documented heuristic, not a probability, and it is never presented as
one:

```
strength     = 0.5 at the detector's threshold, rising to 1.0 at twice it
sufficiency  = readings available / readings the detector wanted
confidence   = strength * (0.6 + 0.4 * sufficiency)
```

A breach on a full history scores its raw strength; the same breach on a quarter
of the history is discounted to 70 percent of it, and picks up a `low_sample`
disclaimer flag.

## Calibration

Thresholds were set against real distributions, not guessed. The evidence is
recorded in comments in each source config. Two examples:

- Ontario five-minute prices are far more volatile than they look. The median
  15-minute move is 12.6 percent, so an obvious-looking 30 percent threshold
  fired 67 times a day. It is now 200 percent, which fires about 2.8 times a day.
- An unscoped z-score on generator output fired 1799 times in 60 days, 827 of
  those on gas peakers and 421 on wind, both of which legitimately swing their
  full range daily. It is now scoped to nuclear and to drops only: 37 fires in
  60 days, and they are real unit trips.

## Operational notes

- **Redistribution is not resolved for the IESO feeds.** Their Terms of Use
  grant a non-sublicensable, non-transferrable licence. Fetching is fine;
  reselling derived alerts is not established. Every signal from those sources
  carries the `redistribution_unresolved` flag. This is the first thing to
  settle before anything is charged for.
- **Data durability.** `data/raw/` is the source of truth but is gitignored: it
  grows by roughly 18 MB a day, mostly generator XML. History older than the
  IESO archive window (about three months) is not recoverable from upstream, so
  once history matters, add an off-repo copy of `data/raw/`.
- **All timestamps are stored UTC with a `Z` suffix.** IESO stamps its files in
  EST year-round, not local Toronto time, and its hours are hour-ending and
  1-indexed. Both are handled in the source configs, and tested.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q     # includes the Worker's own tests, via node
```

Forty-seven tests over the parts where being wrong is expensive: interval timestamp
reconstruction, idempotency, the confidence heuristic, signal determinism, the
YAML trap where a bare `on` is read as the boolean true, and and the peak line's rules that
are easy to get wrong: the ICI top five counts one hour per day, a probability
is withheld until it is calibrated on enough distinct days, a call is recorded
once per horizon and never revised, a probability is never stored as zero or
one, a half-published day is left ungraded, the market day runs 05:00Z to
05:00Z rather than midnight to midnight, and the Worker's copy of the maths has
not drifted from this one.

## Restore

`git clone` → `./setup.sh` → `signal backfill` → `signal update --backfill`.
The only prerequisite is Python 3.11 or newer. One-time `gh auth login` if you
intend to push.

## Interested in it?

There's no license file here, so by default this is all rights reserved. Read it,
learn from it, quote it, but it isn't licensed for you to reuse or ship as-is.

That's not me guarding it. The project is dormant, not defended. If you see
something worth building on, whether that's the ICI peak probability, the
raw-first ingestion schema, or the whole thing, **open an issue and tell me what
you have in mind.** I'd genuinely rather work on it with someone who sees
potential in it, or sort out licensing for a specific use, than leave it sitting
here untouched. A concrete idea is much easier to say yes to than a blank fork.
