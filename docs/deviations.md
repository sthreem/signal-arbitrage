# What was built, and where it departs from the pitch

Written 2026-08-19, after the first working version of the engine, and revised
the same day after the project changed direction. The redirection is section 2.0
and is the most important thing in this document.

The original pitch is not part of this repository; it is quoted below where it
matters. `docs/potential-sources.md` (the candidate source list) is, unedited.
This document records what the build actually does, every place it diverges
from the pitch and that list, and why.

Claims below are marked where it matters: **[verified]** means an endpoint was
called or a number measured from the engine's own store; **[recalled]** means it
rests on general knowledge and has not been checked against a primary source.

---

## Part 1 · Summary of what exists

A Python CLI, `signal`, installed on the path, backed by SQLite and a directory
of raw payloads. It:

- fetches ten public feeds on their own cadence, storing every response verbatim
  before parsing;
- normalises into numeric `observations` and text `events`, both keyed by
  region, so a second region is a config drop;
- runs four numeric detectors (rolling z-score, percentage move, absolute
  level, isolation forest) plus keyword novelty for text feeds, all
  deterministic and seeded;
- emits signals carrying provenance, a confidence score, disclaimer flags and a
  licence scope, exactly the JSON shape the pitch specified;
- grades its own past signals against what the series did next;
- makes a daily **peak call** (section 2.0), records it when it is made, and
  scores the record with a Brier score once the day settles;
- runs unattended on a launchd agent, copies its raw payloads to iCloud daily,
  and raises a desktop notification when a call is worth acting on.

Measured state, re-checked 2026-08-19: 271,569 observations and 441 events in
the store, 67 signals fired; the store rebuilds from raw payloads alone with
identical counts; 16 tests pass. **[verified: counted from `data/signals.db`
and `pytest` run on the day of writing. The observation count grows with every
fetch, so treat it as an as-of figure, not a constant.]**

The shape of the pitch survived. The scale, the stack and the revenue mechanics
did not, for reasons that are findings rather than shortcuts.

---

## Part 2 · The deviations that change decisions

### 2.0 The largest deviation: the money angle is not in the concept at all

**The documents say.** Sell a cross-domain signal feed by subscription, at 49 to
799 CAD a month, to anyone who wants earlier information. The value is framed as
latency: get the same public facts sooner than the mainstream feeds.

**What the build found.** Latency alone does not survive contact with Canadian
public data (section 2.2), and a subscription to "earlier facts" has no
identifiable buyer attached to a number. So the project went looking for a use
of the same engine where a buyer already exists, already has a budget, and can
compute the value themselves. It is in the same Ontario electricity data the
engine was already reading.

Ontario's **Industrial Conservation Initiative** sets a large consumer's Global
Adjustment charge for a whole year from their own demand during the five highest
Ontario Demand hours of the May to April base period, counting at most one hour
per day. Curtailing during one of those hours is worth a great deal; curtailing
on a day that turns out not to matter costs production for nothing. The decision
is taken on a forecast, and the forecast is uncertain.

**Why this is a better product than the one in the concept:**

- **The ground truth is unambiguous.** At the end of the base period you know
  exactly which five hours were the peaks. The engine can be graded against
  reality rather than against a proxy, which is the difference between a track
  record and a claim.
- **The buyer does the valuation.** No persuasion is needed about whether the
  signal is worth anything; the customer already knows what a peak hour costs
  them.
- **The honest edge is real and narrow.** IESO publishes the threshold, the
  demand forecast, and the actual demand that settles it, so subtracting one from
  the other is public arithmetic and is **not** an edge. What is published
  nowhere is how wrong that forecast usually is at a given horizon. Measured
  2026-08-19, the error on the hours a peak can fall in is wider than the error
  across all hours (631 MW against 499 MW at a six hour horizon, 1,960 against
  1,636 at five days), because overnight demand is easy to forecast and busy
  afternoons are not. Turning that measured distribution into a probability, and
  grading the resulting calls, is the only part of this that is ours.

**What it cost to be honest about it.** The probability is withheld entirely, and
says so, until there are enough matched forecast and actual pairs at that
horizon drawn from enough distinct delivery days. Days are counted as well as
pairs because the hours inside one day miss together, so thirty pairs from three
days is three observations wearing a disguise. That gate is not decorative: as of
2026-08-19 it withholds the day-ahead horizon, where 24 pairs come from only 3
delivery days and had produced a bias estimate of -738 MW against -323 MW at the
neighbouring horizon. The same-morning call, which is the one a curtailment is
actually decided on, is calibrated on 93 pairs across 33 days.

**Commercial timing, stated plainly.** The current base period's top hours were
all set in late June and July. Roughly six weeks of live value remain this
season, plus any winter peak. So **this season builds the track record and the
sale is for the period opening May 2027.** Every day the engine does not run is
a day of ground truth that passes and does not come back, which is why
scheduling it came before adding any new data source.

**This is a deviation, not a discovery in the concept.** Nothing in
the pitch or `docs/potential-sources.md` mentions electricity peak
demand, the Industrial Conservation Initiative, or a customer whose bill is set
by five hours a year. The concept's contribution was the region-agnostic schema
and the provenance metadata, both of which held. Its commercial thesis did not,
and was replaced rather than patched.

### 2.1 The legal premise in the pitch does not hold as written

**The documents say.** The pitch positions the whole product in a
"legally defensible grey zone", and `docs/potential-sources.md` states that
"Canadian courts recognize *transformative compilations* as fair dealing/fair
use when raw data is repackaged with original analysis".

**What is true.** Canada has no US style fair use. Fair dealing under the
Copyright Act is limited to a closed list of statutory purposes (research,
private study, education, parody, satire, criticism, review, news reporting),
and a commercial subscription alerting product does not obviously sit in any of
them. **[recalled]** More importantly, the question that actually binds here is
not copyright at all: it is the contractual terms of each source. A licence that
says "non-sublicensable, non-transferrable" is a contract term, and no
transformation argument overrides it.

**What the build does instead.** The engine does not rely on the grey zone. Each
source config records its licence, its licence scope and a terms check, and hard
invariant 6 refuses to fetch any source without one. Where the terms are not
clear, that fact is carried in the data rather than argued away: IESO's Terms of
Use grant a non-sublicensable, non-transferrable licence, so every signal
derived from IESO carries a `redistribution_unresolved` disclaimer flag.
**[verified: the flag is in `sigarb/config.py` and set on those sources]**

**Why this is the load-bearing item.** The three IESO feeds are the only sources
with a genuine timing edge (see 2.2). They are also the three whose
redistribution rights are unresolved. The engine can watch them; whether it can
sell alerts derived from them is an open question, and it is open on the exact
sources the product depends on.

**To close it.** One email to IESO asking, in writing, whether derived alerts
may be redistributed to paying subscribers. Until that answer exists, nothing
IESO-derived can be charged for. The email was never sent.

### 2.2 Almost no public Canadian feed is early. That reshaped the source list

**The documents say.** Both assume a broad menu of feeds with latency edges:
five categories, roughly twenty sources, "signal velocity" as the moat, Ontario
signals delivered "in under 5 minutes from source".

**What verification found.** Several dozen candidate sources were checked
against live endpoints on 2026-08-18, and nearly all of them responded. (The
exact tally is not recoverable: the verification run's output was never written
to the repo, and two working documents disagreed on it. Only the six survivors are on record, in `sources/`. Recording the tally is
a lesson, not a correction: a verification sweep should leave a committed
artefact.) The finding was not about availability: **almost every Canadian
public statistical feed publishes on a fixed, pre-announced calendar.** StatCan
releases at 08:30 ET on dates published a year ahead. The Bank of Canada posts its rate decision at a scheduled minute. CMHC,
CBSA and Transport Canada publish monthly. Detecting "StatCan published CPI
today" has a latency edge of zero, because every interested professional already
has it in a calendar. **[verified: release calendars are themselves published]**

Of everything examined, only real-time electricity market data moves
unpredictably. A nuclear unit tripping, a reserve shortfall, a price spike: none
of those are on anyone's calendar.

**What the build does instead.** Ten sources, none of them chosen for breadth,
and each config carries a `signal_potential` note stating honestly whether it has
an edge. Three carry one (the IESO real-time feeds). One is partial (Ontario news
releases: policy lands there first, but most items are routine). The rest carry
none **and are not meant to**: the peak tracker, the two demand forecasts and the
actual demand series exist to be measured against each other, not to be early.
Bank of Canada FX and the Canada Gazette are context only.

The Canada Gazette source emits nothing at all. Its RSS entries are whole
issues, not individual notices, so keyword detection on it is structurally
impossible rather than merely tuned badly. The config says so, with
`detectors: []` and the reason in a comment.

**Why this matters more than the source count.** The pitch's moat was breadth
plus velocity. Breadth is available but mostly worthless, because breadth of
calendared feeds is breadth of things everyone already knows. The real moat, if
there is one, is narrow: operational real-time feeds that no one is watching
continuously.

**To close the gap honestly.** The remaining candidates worth onboarding are the
ones that are event-driven rather than calendared: tender postings, building
permits, GDELT. None of them was onboarded before the project stopped.

### 2.3 Several sources in the candidate list cannot be used as listed

Checked 2026-08-19. **[verified: DNS and HTTP]**

| Listed | Result |
|---|---|
| `api2.statscan.gc.ca/rest/v1` | Host does not exist (NXDOMAIN). The real service is the Web Data Service at `www150.statcan.gc.ca/t1/wds/rest`, which returns 200 |
| `www.ontla.on.ca` | Host does not exist (NXDOMAIN). The Legislative Assembly moved to `ola.org`, which returns 200 |
| `eprocure.ontario.ca` | Host does not exist (NXDOMAIN). Ontario tenders are on `ontariotenders.app.jaggaer.com`; federal notices are on `canadabuys.canada.ca`. Both return 200 |
| `api.gdeltproject.org` | Alive, but rate limited (HTTP 429) from this address on first contact. Usable, with backoff |
| Kijiji and Indeed listing scraping | **Rejected.** Both prohibit automated collection in their terms. Hard invariant 6 forbids fetching a source whose terms forbid it, and "fair use for derivative signals" is not a defence that exists in Canadian law |

None of this is a criticism of the candidate list, which is a brainstorm and was
useful as one. It is the reason the project's rule is that no source is real
until its config exists and its endpoint has answered.

### 2.4 Nothing is delivered to anyone yet

**The pitch says.** Dashboard with WebSocket or SSE live updates, cron email
digests, a REST API, a Next.js frontend, "fully faceless and passive after v1".

**What exists.** A local CLI with an interactive menu, CSV and JSON export, and
a digest module. Signals do not leave the machine unless someone runs a command.
There is also no scheduler installed, so the engine is currently neither faceless
nor passive: it runs when invoked. **[verified: no launchd job, no crontab
entry]**

**Update, later the same day.** Both of those are now closed. The engine ran
unattended on a launchd agent, made the peak call once each market morning,
copied its raw payloads to iCloud daily, and raised a desktop notification above
a 15 percent chance.

**Update, 2026-08-21: it does not run on the Mac any more.** Two days of
operation produced the argument against a laptop. On 2026-08-20 the agent missed
its morning call entirely, because the machine was asleep during the 06:00 to
10:00 window and launchd's catch-up ran outside it. That is not a bug in the
gating; it is what a laptop is. For a product whose whole value is not missing a
morning, and whose ground truth passes and does not come back, the host was
wrong.

The peak line now runs on a Cloudflare Worker on a cron trigger, with a D1 store
and a token-gated dashboard, at zero cost on the free plan. It was verified
against the Python for the same three delivery days: identical forecast peaks,
identical pair and day counts, identical error bias and standard deviation, and
the same decision to withhold the day-ahead probability. `worker/README.md`
records the design and the two free-plan budgets that shaped it.

**What the move cost, stated rather than buried.** Only the peak line went.
Detection uses scikit-learn over three hundred thousand observations and does not
fit a Worker, so the market signal feeds were no longer being collected at all,
and the thirty day measurement was paused rather than running. Every day until
that is fixed is a gap in a series that cannot be backfilled past the upstream
archive window of roughly three months. The trade was taken deliberately: the
peak line is the half with a buyer.

**Update, 2026-08-23: collection and detection are now separate machines.** This
is the architectural deviation worth naming, because the concept assumed one
system that ingests, detects and delivers. It is two. Cloudflare fetches every
feed and writes the raw payload to R2 verbatim, parsing only the four the peak
line needs; the Mac pulls those payloads down with `signal pull`, gzips them into
`data/raw/` and runs the detectors there. The seam is the object key, which is
byte for byte the filename the local fetcher already wrote, so `signal rebuild`
cannot tell a relayed payload from a locally fetched one.

The split is forced by arithmetic, not preference. Generator output alone is
about 2,200 readings an hour, which at two D1 row writes each would spend the
free plan's entire daily allowance; and the detectors are a scikit-learn shape a
10 ms CPU budget cannot hold. What that buys is that the collection half, the
half whose loss is permanent, no longer depends on a laptop being awake.

It also closed something the first Cloudflare port had quietly broken. Hard
invariant 1 says raw payloads are the source of truth and everything else is
derived from them. That port parsed in memory and kept only the rows it wanted,
so between 2026-08-21 and 2026-08-23 the peak line's own inputs existed nowhere
durable. They do again.

The paragraphs below record why delivery was built after signal quality, which
still stands.

**Why.** Delivery polish on an unproven signal is the classic way to spend a
month on the wrong thing. The kill criterion is about signal quality, and signal
quality is measurable from a local store. Building a dashboard first would have
produced a prettier way to look at signals that might turn out not to be worth
looking at.

**Why it cannot stay this way.** The kill criterion is judged on 30 days of
continuous running, and that clock has not started. Scheduling the engine is now
the highest priority item in the queue, ahead of every new source, with
delivery close behind, because a signal nobody receives cannot be graded
by a reader and cannot be sold.

### 2.5 The stack is local, not cloud

**The pitch says.** Kafka or Redpanda, TimescaleDB or InfluxDB, Postgres,
FastAPI workers, Next.js, 25 to 60 CAD per month on a VPS.

**What exists.** SQLite, gzipped files on disk, a Python CLI, and since
2026-08-21 a Cloudflare Worker with D1 for the peak line. Running cost: still
zero, and the concept's own 25 to 60 CAD a month turns out to be avoidable
rather than merely deferred.

**Why.** Every one of those components is a two-way door, and none of them
changes whether the signals are any good. SQLite handles 271,553 rows without
noticing. The concept's own scaling argument ("10k+ users") is a problem that
does not exist and may never.

**To close it.** Only when there are users. The ingestion and detection layers
are storage-agnostic pure functions over readings, so the swap is mechanical.

### 2.6 The correlation matrix, the multi-region nodes and the tiers are unbuilt

**The pitch says.** A cross-signal correlation graph as the compounding moat,
Ontario then Canada-wide then US nodes on a 90 day path, and four pricing tiers
from free to 799 CAD per month.

**What exists.** None of it. One region, no correlation layer, no pricing.

**Why.** Correlation between signals needs more than one genuinely independent
live series to correlate. Right now three of the six sources are the same market
(Ontario electricity) and two emit nothing. Correlating three views of one
system produces confident nonsense. The correlation layer is queued as `D1`,
deliberately behind the new event-driven sources, because it needs them as
inputs.

The region-agnostic claim is real in the schema (`region_code` is a parameter
everywhere, and hard invariant 7 forbids hardcoding it) but is **untested**
until a non-Ontario node runs. Queued as `D2`. Treat it as designed-for, not
proven.

Pricing is unbuilt because of 2.1: pricing a product on sources whose
redistribution rights are unresolved would be building the till before the shop.

---

## Part 3 · Where the build went further than the pitch

Not everything diverged downward.

- **Sources are verified, not assumed.** The pitch named categories; the build
  called every endpoint and recorded the licence, the terms check and an honest
  edge assessment per source. Two traps were caught this way that would have
  poisoned the data silently: a federal recalls API that returns HTTP 200 with
  well-formed JSON frozen since 2021, and an IESO host that returns HTTP 200
  with a login page instead of a report.
- **Raw payloads are the source of truth.** The pitch mentioned raw and
  processed separation; the build made it an invariant and then proved it, with
  a disaster-recovery drill that rebuilds the entire store from raw blobs alone
  and matches counts exactly. **[verified]**
- **The engine grades itself.** Nothing in the pitch does this. `signal review`
  marks every past signal as blip, persisted, reverted or pending, which turns
  the kill criterion from an opinion into a number.
- **Thresholds are calibrated against measured distributions, not guessed.** The
  first pass at generator anomalies fired 1,799 times in 60 days, mostly gas
  peakers and wind doing exactly what they are built to do. Scoped to nuclear
  and to drops only, it fires 37 times, and those are unit trips. Similarly, an
  obvious-looking 30 percent price threshold fired 67 times a day, because the
  median 15-minute Ontario price move is 12.6 percent. **[verified: measured on
  709 observations]**
- **The disclaimer schema was implemented as specified.** This is the one part
  of the pitch adopted essentially verbatim, including the flag-to-text mapping
  that removes per-signal legal review.

---

## Part 4 · The pitch's numbers against reality

| The pitch said | Reality |
|---|---|
| Pipeline stable in 7 to 10 days | Working in one day, because the scope shrank to what is real |
| 25 to 60 CAD per month infrastructure | 0 CAD. Nothing is hosted |
| False positive rate under 8 percent by day 30 | Not measurable yet. The nearest proxy is the blip rate: 2 of 50 judged signals, 4 percent, did not persist past one reading. **[verified: `signal review`, 2026-08-19]** Two cautions. It is not the same quantity as a false positive rate and must not be reported as one, and 50 judged signals is a thin sample drawn from backfilled history rather than live operation |
| 3 to 5 high-signal sources in days 1 to 2 | 6 configured, of which 3 carry a real edge and 1 emits nothing |
| Pro tier open by day 14 | Not opened. Blocked on 2.1 |
| Faceless and passive after v1 | True for the peak line, which now runs on Cloudflare with no machine of ours involved. Not true for the market signal line, whose collection is paused. Not true commercially, since there is still no customer |
| Dashboard + threshold alerts + API | Two of the three exist for the peak line: a private dashboard and the same data as JSON. Alerts regressed to nothing when the local agent went, since the desktop notification went with it |
| Nothing about peak demand or the Industrial Conservation Initiative | The largest single deviation, and the only part with a buyer attached. See 2.0 |

---

## Part 5 · The honest read

The pitch's engineering thesis held up: a region-agnostic schema, deterministic
detection, provenance and disclaimer metadata in the data model, raw payloads
kept lossless. All of that was buildable in a day and is built.

The pitch's commercial thesis did not hold, and was replaced rather than
patched. Two findings stand against it and one question stands over what
replaced it.

The finding: public data is mostly not early. The breadth the pitch treats as an
asset is largely breadth of calendared releases, which carry no latency
advantage by construction. What is left after that filter is narrow, operational
and real, but it is three feeds, not twenty categories.

The question: those three feeds are exactly the ones whose redistribution rights
are unresolved.

The replacement: the peak line, which has a named buyer, an unambiguous ground
truth, and an edge that is narrow but real and measured rather than asserted.
What it does not yet have is a track record, because a track record cannot be
backdated. The engine now runs unattended so one accumulates, and the honest
position is that the product is not sellable until it does.

None of this was visible from the pitch, and none of it is fatal. The licence
question is answerable by an email. The rest is answerable by the season.
