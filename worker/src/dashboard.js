/**
 * The dashboard. One page, server-rendered, no client framework.
 *
 * It is read the way a control room display is read, not the way a report is
 * read: the verdict first and large, then the ladder that explains it, then the
 * record that says whether the verdict is worth anything, then the heartbeat
 * that says the engine is actually alive. Anything that needs attention has to
 * be legible without reading a number.
 */

import { hourLabel, toMarketTime } from "./peak.js";
import { RELAY_RETENTION_DAYS } from "./constants.js";

const esc = (value) => String(value ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

const mw = (value) => (value === null || value === undefined
  ? "n/a" : Math.round(value).toLocaleString("en-CA"));

function chance(probability) {
  if (probability === null || probability === undefined) return "unknown";
  if (probability < 0.01) return `${(probability * 100).toFixed(1)}%`;
  return `${Math.round(probability * 100)}%`;
}

function verdictOf(probability, notifyAbove) {
  if (probability === null || probability === undefined) {
    return { word: "not yet callable", tone: "unknown" };
  }
  if (probability >= 0.5) return { word: "curtail", tone: "critical" };
  if (probability >= notifyAbove) return { word: "watch", tone: "watch" };
  return { word: "stand down", tone: "clear" };
}

function hourEndingLabel(iso) {
  return `HE${toMarketTime(Date.parse(iso)).getUTCHours() + 1}`;
}

function ago(iso, now) {
  if (!iso) return "never";
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return "unknown";
  const seconds = Math.max(0, (now.getTime() - parsed) / 1000);
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

// --------------------------------------------------------------------------

function callPanel(call, threshold, notifyAbove) {
  const verdict = verdictOf(call.probability, notifyAbove);
  const day = new Date(Date.parse(`${call.date}T12:00:00Z`))
    .toLocaleDateString("en-CA", { weekday: "short", month: "short", day: "numeric" });
  return `
  <article class="call call--${verdict.tone}">
    <header class="call__head">
      <span class="call__day">${esc(day)}</span>
      <span class="call__hour">peak hour ${esc(hourEndingLabel(call.peakHourUtc))}</span>
    </header>
    <p class="call__verdict">${esc(verdict.word)}</p>
    <dl class="call__figures">
      <div><dt>Chance of a peak</dt><dd class="big">${esc(chance(call.probability))}</dd></div>
      <div><dt>Forecast peak</dt><dd>${mw(call.forecastMw)} <span class="unit">MW</span></dd></div>
      <div><dt>Against the line</dt><dd>${call.marginMw >= 0 ? "+" : ""}${mw(call.marginMw)} <span class="unit">MW</span></dd></div>
    </dl>
    <p class="call__basis">${esc(call.basis)}</p>
  </article>`;
}

function ladder(top, threshold) {
  if (top.length === 0) return "<p class=\"empty\">No demand history for this base period yet.</p>";
  const ceiling = top[0].value * 1.02;
  const floor = Math.min(...top.map((r) => r.value)) * 0.94;
  const span = Math.max(ceiling - floor, 1);
  const pct = (value) => `${((value - floor) / span) * 100}%`;

  return `
  <ol class="ladder">
    ${top.map((row, index) => {
      const label = hourLabel(row.observed_at);
      const last = index === top.length - 1;
      return `<li class="rung${last ? " rung--line" : ""}">
        <span class="rung__rank">${index + 1}</span>
        <span class="rung__label">${esc(label)}</span>
        <span class="rung__bar"><i style="width:${pct(row.value)}"></i></span>
        <span class="rung__value">${mw(row.value)}<span class="unit">MW</span></span>
      </li>`;
    }).join("")}
  </ol>
  <p class="ladder__note">The hour to beat is <strong>${mw(threshold)} MW</strong>.
  Anything above it displaces the bottom of this list and resets what every
  coming afternoon is measured against.</p>`;
}

/**
 * How a settled call turned out, judged as a call.
 *
 * A probability is right when it leaned the way the day went, so the split is at
 * a half. A call made without a probability is not gradeable and says so rather
 * than borrowing a verdict it never earned.
 */
function outcomeOf(call) {
  if (!call.settled_at) return { word: "open", tone: "open" };
  const peak = call.cleared === 1;
  if (call.probability === null || call.probability === undefined) {
    return { word: peak ? "peak, no call" : "no peak, no call", tone: "ungraded" };
  }
  const leanedThatWay = (call.probability >= 0.5) === peak;
  return {
    word: leanedThatWay ? (peak ? "called the peak" : "correctly quiet") : (peak ? "missed a peak" : "false alarm"),
    tone: leanedThatWay ? "right" : "wrong",
  };
}

function recordPanel(board, calls) {
  const rows = calls.slice(0, 14).map((call) => {
    // Colour the call, not the weather. Whether a peak happened is the grid's
    // business; what this table is for is whether the call was any good, and a
    // one-in-a-thousand call on a day that set a peak is a bad call however
    // ordinary the peak was. Undecided when no probability was quoted.
    const state = outcomeOf(call);
    return `<tr>
      <td>${esc(call.delivery_date)}</td>
      <td class="dim">${esc(call.horizon_bucket.replace("|", ""))}</td>
      <td class="num">${mw(call.forecast_mw)}</td>
      <td class="num">${mw(call.threshold_mw)}</td>
      <td class="num">${esc(chance(call.probability))}</td>
      <td class="num">${call.actual_mw === null ? "&mdash;" : mw(call.actual_mw)}</td>
      <td><span class="state state--${state.tone}">${esc(state.word)}</span></td>
    </tr>`;
  }).join("");

  return `
  <p class="verdict-line">${esc(board.verdict)}</p>
  <dl class="tallies">
    <div><dt>Calls recorded</dt><dd>${board.recorded}</dd></div>
    <div><dt>Settled</dt><dd>${board.settled}</dd></div>
    <div><dt>Carrying a probability</dt><dd>${board.scored}</dd></div>
    <div><dt>Brier score</dt><dd>${board.brier === null ? "&mdash;" : board.brier.toFixed(3)}</dd></div>
  </dl>
  ${rows ? `<div class="scroll"><table>
    <thead><tr>
      <th>Delivery day</th><th>Horizon</th><th class="num">Forecast</th>
      <th class="num">Line</th><th class="num">Chance</th><th class="num">Actual</th><th>How the call did</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>` : "<p class=\"empty\">No calls recorded yet.</p>"}`;
}

// How often the cron is meant to fire, so a gap can be counted rather than
// guessed at. Keep in step with the schedule in wrangler.jsonc.
const CYCLE_MINUTES = 5;

/**
 * Cycles seen against cycles due in the last hour.
 *
 * This exists because the failure that matters most cannot report itself. The
 * free plan allows 10 ms of CPU per invocation, and an overrun is killed rather
 * than caught, so it leaves no row and no error, only an absence. Counting what
 * should be there turns that absence into a number.
 */
function cadence(cycles, now) {
  const windowMs = 3600_000;
  const since = now.getTime() - windowMs;
  const seen = cycles.filter((c) => Date.parse(c.ran_at) >= since).length;
  const due = Math.floor(windowMs / (CYCLE_MINUTES * 60_000));
  return { seen, due, healthy: seen >= due - 1 };
}

/**
 * What the relay has carried to R2, per source.
 *
 * Two timestamps rather than one, because they answer different questions. A
 * stale `stored_at` on its own is ambiguous: the relay may be broken, or the
 * source may simply not have published anything new. `checked_at` moving proves
 * the first is not it.
 */
function collection(relay, now) {
  if (!relay || relay.length === 0) {
    return "<p class=\"empty\">Nothing relayed yet. The next cycle will collect.</p>";
  }
  const bytes = relay.reduce((sum, row) => sum + (row.bytes || 0), 0);
  return `
  <div class="scroll"><table class="counts">
    <thead><tr><th>Feed</th><th class="num">Payloads</th><th>Last stored</th><th>Last checked</th></tr></thead>
    <tbody>${relay.map((row) => `<tr${row.last_error ? ' class="row--bad"' : ""}>
      <td>${esc(row.source_id)}</td>
      <td class="num">${(row.objects || 0).toLocaleString("en-CA")}</td>
      <td class="dim">${esc(ago(row.stored_at, now))}</td>
      <td class="dim">${esc(ago(row.checked_at, now))}</td>
    </tr>${row.last_error ? `<tr><td colspan="4" class="dim">${esc(row.last_error)}</td></tr>` : ""}`).join("")}</tbody>
  </table></div>
  <p class="dim">Latest payload of each feed totals ${(bytes / 1024).toFixed(0)} kB.
  Payloads are relayed verbatim and never parsed here: the market signal detectors
  run on the Mac, which pulls this down with <code>signal pull</code>. R2 keeps
  ${RELAY_RETENTION_DAYS} days, so the durable copy is the one on that machine.</p>`;
}

function heartbeat(cycles, counts, now) {
  const ticks = cycles.slice().reverse().map((cycle) => `<i class="tick tick--${cycle.ok ? "ok" : "bad"}" title="${esc(cycle.ran_at)}${cycle.detail ? ` · ${esc(cycle.detail)}` : ""}"></i>`).join("");
  const latest = cycles[0];
  const failures = cycles.filter((c) => !c.ok);
  const beat = cadence(cycles, now);
  return `
  <div class="beat">
    <div class="beat__ticks">${ticks || "<span class=\"empty\">no cycles yet</span>"}</div>
    <p class="beat__status">${latest
      ? `Last cycle ${esc(ago(latest.ran_at, now))}, ${latest.written} row(s) written in ${latest.ms} ms.`
      : `The cron has not run yet. It fires every ${CYCLE_MINUTES} minutes.`}</p>
  </div>
  <p class="beat__cadence${beat.healthy ? "" : " beat__cadence--gap"}">
    ${beat.seen} of ${beat.due} cycles in the last hour.${beat.healthy ? ""
      : " A missing cycle leaves no error behind, because an over-budget run is killed"
        + " rather than caught. Check the Worker's logs rather than this page."}</p>
  ${failures.length ? `<ul class="failures">${failures.slice(0, 3)
    .map((f) => `<li><span class="dim">${esc(f.ran_at)}</span> ${esc(f.detail || "failed")}</li>`).join("")}</ul>` : ""}
  <div class="scroll"><table class="counts">
    <thead><tr><th>Source</th><th class="num">Readings</th><th>Newest</th></tr></thead>
    <tbody>${counts.map((row) => `<tr>
      <td>${esc(row.source_id)}</td>
      <td class="num">${row.n.toLocaleString("en-CA")}</td>
      <td class="dim">${esc(row.newest || "")}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

// --------------------------------------------------------------------------

/**
 * The login page.
 *
 * Deliberately plain, and deliberately a real form: one password field, named
 * and typed so a password manager recognises it, with the same ground and type
 * as the dashboard behind it so it does not read as a different site.
 */
export function renderLogin({ message = "", status = 200 } = {}) {
  return `<!doctype html>
<html lang="en-CA">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Ontario Peak Watch</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Condensed:wght@600;700&family=IBM+Plex+Sans:wght@400;500&display=swap">
<style>
:root { --ground:#eef1ef; --plate:#ffffff; --line:#ccd5d1; --ink:#101a17; --muted:#5b6b66; --accent:#b07000; --critical:#a5311f; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --ground:#0b1310; --plate:#121d19; --line:#24332d; --ink:#e4ece8; --muted:#8ba098; --accent:#e0a020; --critical:#e0705c; } }
:root[data-theme="dark"] { --ground:#0b1310; --plate:#121d19; --line:#24332d; --ink:#e4ece8; --muted:#8ba098; --accent:#e0a020; --critical:#e0705c; }
*, *::before, *::after { box-sizing: border-box; }
body { margin:0; min-height:100vh; display:grid; place-items:center; padding:1.5rem;
  background:var(--ground); color:var(--ink);
  font:400 16px/1.6 "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif; }
main { background:var(--plate); border:1px solid var(--line); padding:1.75rem;
  width:min(23rem, 100%); display:flex; flex-direction:column; gap:1rem; }
h1 { margin:0; font-family:"IBM Plex Sans Condensed", sans-serif; font-weight:700;
  font-size:1.35rem; letter-spacing:-0.01em; }
p { margin:0; font-size:0.85rem; color:var(--muted); }
form { display:flex; flex-direction:column; gap:0.6rem; }
label { font-size:0.72rem; text-transform:uppercase; letter-spacing:0.08em; color:var(--muted); }
input { font:inherit; padding:0.6rem 0.7rem; background:var(--ground); color:var(--ink);
  border:1px solid var(--line); }
input:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
button { font:500 1rem/1 "IBM Plex Sans", sans-serif; padding:0.65rem; cursor:pointer;
  background:var(--ink); color:var(--ground); border:1px solid var(--ink); }
button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.error { color:var(--critical); font-size:0.85rem; }
</style>
</head>
<body>
<main>
  <h1>Ontario Peak Watch</h1>
  ${message ? `<p class="error">${esc(message)}</p>` : ""}
  <form method="post" action="/login">
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password"
           required autofocus>
    <button type="submit">Sign in</button>
  </form>
  <p>Private. The data behind this page is licensed on terms that do not permit
  passing it on.</p>
</main>
</body>
</html>`;
}

export function renderDashboard({ report, board, cycles, calls, counts, now, relay, notifyAbove }) {
  const lead = report.calls[0];
  const leadVerdict = lead ? verdictOf(lead.probability, notifyAbove) : { tone: "unknown" };

  return `<!doctype html>
<html lang="en-CA" data-tone="${leadVerdict.tone}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Ontario Peak Watch</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@500;600;700&family=IBM+Plex+Sans:ital,wght@0,400;0,500;1,400&display=swap">
<style>
:root {
  --ground: #eef1ef;
  --plate: #ffffff;
  --line: #ccd5d1;
  --line-strong: #9fada8;
  --ink: #101a17;
  --muted: #5b6b66;
  --accent: #b07000;
  --clear: #26714a;
  --watch: #9a6a06;
  --critical: #a5311f;
  --unknown: #5b6b66;
  --plate-shadow: 0 1px 0 rgba(16, 26, 23, 0.04);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #0b1310;
    --plate: #121d19;
    --line: #24332d;
    --line-strong: #3d5148;
    --ink: #e4ece8;
    --muted: #8ba098;
    --accent: #e0a020;
    --clear: #57b585;
    --watch: #d9a63a;
    --critical: #e0705c;
    --unknown: #8ba098;
    --plate-shadow: none;
  }
}
:root[data-theme="dark"] {
  --ground: #0b1310;
  --plate: #121d19;
  --line: #24332d;
  --line-strong: #3d5148;
  --ink: #e4ece8;
  --muted: #8ba098;
  --accent: #e0a020;
  --clear: #57b585;
  --watch: #d9a63a;
  --critical: #e0705c;
  --unknown: #8ba098;
  --plate-shadow: none;
}

*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font: 400 16px/1.6 "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
  font-variant-numeric: tabular-nums;
}
.wrap { max-width: 68rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; display: flex; flex-direction: column; gap: 1.5rem; }

.masthead { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.5rem 1.25rem; padding-bottom: 1rem; border-bottom: 2px solid var(--line-strong); }
.masthead h1 {
  margin: 0; font-family: "IBM Plex Sans Condensed", "IBM Plex Sans", sans-serif;
  font-weight: 700; font-size: clamp(1.6rem, 4vw, 2.2rem); letter-spacing: -0.01em; text-wrap: balance;
}
.masthead__period { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
.masthead__lede { flex-basis: 100%; margin: 0.35rem 0 0; max-width: 62ch; color: var(--muted); font-size: 0.95rem; }

.plate { background: var(--plate); border: 1px solid var(--line); box-shadow: var(--plate-shadow); }
.plate > h2 {
  margin: 0; padding: 0.7rem 1.1rem;
  border-bottom: 1px solid var(--line);
  font-family: "IBM Plex Sans Condensed", sans-serif; font-weight: 600;
  font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.11em; color: var(--muted);
}
.plate__body { padding: 1.1rem; display: flex; flex-direction: column; gap: 1rem; }

.calls { display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); gap: 0; }
.call { padding: 1.1rem; border-right: 1px solid var(--line); display: flex; flex-direction: column; gap: 0.7rem; border-top: 3px solid var(--tone); }
.call:last-child { border-right: 0; }
.call--clear { --tone: var(--clear); }
.call--watch { --tone: var(--watch); }
.call--critical { --tone: var(--critical); }
.call--unknown { --tone: var(--line-strong); }
.call__head { display: flex; justify-content: space-between; align-items: baseline; gap: 0.5rem; font-family: "IBM Plex Mono", monospace; font-size: 0.75rem; color: var(--muted); }
.call__day { font-weight: 600; color: var(--ink); letter-spacing: 0.02em; }
.call__verdict {
  margin: 0; font-family: "IBM Plex Sans Condensed", sans-serif; font-weight: 700;
  font-size: clamp(1.5rem, 3.4vw, 2rem); line-height: 1.05; color: var(--tone); text-transform: lowercase;
}
.call__figures { margin: 0; display: flex; flex-direction: column; gap: 0.45rem; }
.call__figures > div { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; border-bottom: 1px dotted var(--line); padding-bottom: 0.3rem; }
.call__figures dt { font-size: 0.8rem; color: var(--muted); }
.call__figures dd { margin: 0; font-family: "IBM Plex Mono", monospace; font-weight: 500; }
.call__figures dd.big { font-size: 1.35rem; font-weight: 600; color: var(--tone); }
.unit { font-size: 0.72em; color: var(--muted); margin-left: 0.25em; }
.call__basis { margin: 0; font-size: 0.78rem; line-height: 1.5; color: var(--muted); }

.ladder { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.35rem; }
.rung { display: grid; grid-template-columns: 1.4rem 9.5rem 1fr auto; align-items: center; gap: 0.75rem; font-size: 0.85rem; }
.rung__rank { font-family: "IBM Plex Mono", monospace; color: var(--muted); font-size: 0.78rem; }
.rung__label { font-family: "IBM Plex Mono", monospace; font-size: 0.78rem; color: var(--muted); white-space: nowrap; }
.rung__bar { height: 0.7rem; background: color-mix(in srgb, var(--line) 60%, transparent); position: relative; }
.rung__bar i { display: block; height: 100%; background: var(--line-strong); }
.rung--line .rung__bar i { background: var(--accent); }
.rung--line .rung__value, .rung--line .rung__label { color: var(--ink); font-weight: 600; }
.rung__value { font-family: "IBM Plex Mono", monospace; font-weight: 500; white-space: nowrap; }
.ladder__note { margin: 0; font-size: 0.85rem; color: var(--muted); max-width: 62ch; }

.verdict-line { margin: 0; font-size: 1rem; line-height: 1.55; max-width: 62ch; }
.tallies { margin: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr)); gap: 0.75rem; }
.tallies > div { border-left: 2px solid var(--line); padding-left: 0.6rem; }
.tallies dt { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted); }
.tallies dd { margin: 0.1rem 0 0; font-family: "IBM Plex Mono", monospace; font-size: 1.15rem; font-weight: 600; }

.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
th, td { text-align: left; padding: 0.4rem 0.6rem 0.4rem 0; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { font-family: "IBM Plex Sans Condensed", sans-serif; font-weight: 600; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted); }
td { font-family: "IBM Plex Mono", monospace; }
.num { text-align: right; padding-right: 0.9rem; }
th.num { text-align: right; }
.dim { color: var(--muted); }

.state { font-family: "IBM Plex Sans Condensed", sans-serif; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; padding: 0.1rem 0.45rem; border: 1px solid currentColor; }
.state--right { color: var(--clear); }
.state--wrong { color: var(--critical); }
.state--ungraded { color: var(--muted); border-style: dashed; }
.state--open { color: var(--muted); }

.beat { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem 1rem; }
.beat__ticks { display: flex; gap: 3px; }
.tick { width: 8px; height: 20px; display: block; }
.tick--ok { background: var(--clear); }
.tick--bad { background: var(--critical); }
.beat__status { margin: 0; font-size: 0.85rem; color: var(--muted); }
.beat__cadence { margin: 0; font-size: 0.85rem; color: var(--muted); max-width: 62ch; }
.beat__cadence--gap { color: var(--critical); }
.failures { margin: 0; padding-left: 1.1rem; font-size: 0.8rem; color: var(--critical); display: flex; flex-direction: column; gap: 0.25rem; }
.failures .dim { color: var(--muted); }
.row--bad td:first-child { color: var(--critical); }

.notes { margin: 0; padding-left: 1.1rem; display: flex; flex-direction: column; gap: 0.4rem; font-size: 0.87rem; color: var(--muted); }
.empty { margin: 0; color: var(--muted); font-size: 0.87rem; }

.footnote { font-size: 0.8rem; line-height: 1.6; color: var(--muted); max-width: 68ch; display: flex; flex-direction: column; gap: 0.7rem; border-top: 1px solid var(--line); padding-top: 1.1rem; }
.footnote strong { color: var(--ink); font-weight: 500; }
@media (max-width: 40rem) {
  .rung { grid-template-columns: 1.2rem 1fr auto; }
  .rung__bar { display: none; }
  .call { border-right: 0; border-bottom: 1px solid var(--line); }
}
</style>
</head>
<body>
<div class="wrap">

  <header class="masthead">
    <h1>Ontario Peak Watch</h1>
    <span class="masthead__period">base period ${esc(report.period.label)}</span>
    <p class="masthead__lede">Ontario sets a large consumer's Global Adjustment charge for a
    whole year from their demand during the five highest Ontario Demand hours of the base
    period, at most one hour per day. This watches for those hours.</p>
  </header>

  <section class="plate">
    <h2>The call</h2>
    ${report.calls.length
      ? `<div class="calls">${report.calls.map((call) => callPanel(call, report.threshold, notifyAbove)).join("")}</div>`
      : "<div class=\"plate__body\"><p class=\"empty\">No forecast hours ahead of now are stored yet. The next cycle will collect them.</p></div>"}
  </section>

  <section class="plate">
    <h2>Peak hours set so far</h2>
    <div class="plate__body">${ladder(report.top, report.threshold)}
    ${report.notes.length ? `<ul class="notes">${report.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}</div>
  </section>

  <section class="plate">
    <h2>Track record</h2>
    <div class="plate__body">${recordPanel(board, calls)}</div>
  </section>

  <section class="plate">
    <h2>Engine</h2>
    <div class="plate__body">${heartbeat(cycles, counts, now)}</div>
  </section>

  <section class="plate">
    <h2>Collection</h2>
    <div class="plate__body">${collection(relay, now)}</div>
  </section>

  <footer class="footnote">
    <p>The threshold and the forecast are both published by the IESO, so subtracting one
    from the other is public arithmetic and is not an edge. <strong>The probability is this
    engine's own</strong>, from a normal approximation on forecast error measured per
    horizon against settled actuals. Forecast errors have fatter tails than a normal on
    exactly the days that matter, so read a small probability as a floor rather than a
    dismissal.</p>
    <p><strong>Not for redistribution.</strong> The IESO licence is non-transferrable and
    that question is unresolved, so this page is reachable only through its unlisted
    address and must not be shared.</p>
    <p>Derived from publicly available sources and algorithmically synthesized. No warranty
    of completeness or primary research. Information only. This is not financial,
    investment, legal or tax advice.</p>
    <p class="dim">Rendered ${esc(now.toISOString().slice(0, 19).replace("T", " "))}Z ·
    <a href="/api" style="color:var(--accent)">JSON</a> ·
    <a href="/logout" style="color:var(--accent)">sign out</a></p>
  </footer>

</div>
</body>
</html>`;
}
