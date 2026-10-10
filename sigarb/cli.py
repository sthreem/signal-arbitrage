"""Command line interface.

Running `signal` with no arguments opens the menu, which exposes every
operation and prompts for what it needs. Named subcommands do the same work for
scripting and cron. With no terminal attached, the menu is replaced by help.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, IntPrompt, Prompt

from . import backfill, backup as backup_mod, config, db, digest, export, notify, peak, remote, pipeline, review, schedule as schedule_mod, sources as sources_mod
from .digest import console


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

def _load_sources(only: str | None = None) -> list[sources_mod.Source]:
    try:
        loaded = sources_mod.load_all()
    except sources_mod.ConfigError as exc:
        console.print(f"[red]Source config problem: {exc}[/red]")
        console.print("[dim]Fix the file in sources/ and try again. Nothing was fetched.[/dim]")
        raise SystemExit(2)
    if only:
        loaded = [s for s in loaded if s.source_id == only]
        if not loaded:
            console.print(f"[red]No source with id {only!r}.[/red]")
            raise SystemExit(2)
    return loaded


def cmd_update(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    sources = _load_sources(getattr(args, "source", None))
    if not sources:
        console.print(
            "[yellow]No sources configured yet.[/yellow] Add a YAML file to sources/ "
            "and run this again."
        )
        return 1

    digest.banner("checking sources for anything new")
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), transient=True, console=console,
    ) as bar:
        task = bar.add_task("starting", total=len(sources) * 2)

        def tick(message: str) -> None:
            bar.update(task, description=message, advance=1)

        with db.session() as conn:
            report = pipeline.run(
                conn, sources,
                force=getattr(args, "force", False),
                backfill=getattr(args, "backfill", False),
                detect_only=getattr(args, "detect_only", False),
                progress=tick,
            )

    digest.run_summary(report)
    if report.signals:
        console.print()
        digest.signals_table(report.signals, details=getattr(args, "details", False), title="New signals")
    digest.disclaimer_footer()
    return 0


def cmd_signals(args: argparse.Namespace) -> int:
    with db.session() as conn:
        rows = db.recent_signals(
            conn,
            limit=getattr(args, "limit", 25),
            region=getattr(args, "region", None),
            min_confidence=getattr(args, "min_confidence", 0.0),
        )
        if getattr(args, "json", False):
            from .signals import as_payload

            print(json.dumps([as_payload(r) for r in rows], indent=2, ensure_ascii=False))
            return 0
        digest.banner("most recent signals")
        digest.signals_table(rows, details=getattr(args, "details", False))
        digest.disclaimer_footer()
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    loaded = _load_sources()
    with db.session() as conn:
        pipeline.sync_sources(conn, loaded)
        rows = list(conn.execute("SELECT * FROM sources ORDER BY region_code, source_id"))
        digest.banner(f"{len(rows)} source(s) configured")
        digest.sources_table(rows, details=getattr(args, "details", False))
        if getattr(args, "details", False):
            for source in loaded:
                console.print(
                    f"[dim]{source.source_id}: {source.signal_potential}[/dim]"
                )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Validate configs and, optionally, prove each endpoint answers."""
    config.ensure_dirs()
    loaded = _load_sources(getattr(args, "source", None))
    digest.banner("validating source configs")
    console.print(f"[green]{len(loaded)} config(s) parsed and valid.[/green]")

    for source in loaded:
        console.print(
            f"  [bold]{source.source_id}[/bold] · {source.region_code.upper()} · "
            f"{source.adapter} · every {source.update_frequency_s}s\n"
            f"    [dim]licence: {source.licence} ({source.licence_scope})[/dim]\n"
            f"    [dim]terms:   {source.terms_check}[/dim]"
        )

    if not getattr(args, "live", False):
        console.print("\n[dim]Pass --live to actually call each endpoint.[/dim]")
        return 0

    from . import adapters, fetch

    failures = 0
    for source in loaded:
        try:
            result = fetch.fetch(source)
            parsed = adapters.get(source.adapter)(source, result.body, None)
            console.print(
                f"  [green]ok[/green] {source.source_id}: HTTP {result.status}, "
                f"{len(parsed.observations)} reading(s), {len(parsed.events)} item(s)"
            )
            for problem in parsed.problems:
                console.print(f"       [yellow]{problem}[/yellow]")
        except Exception as exc:
            failures += 1
            console.print(f"  [red]fail[/red] {source.source_id}: {exc}")
    return 1 if failures else 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """Pull dated history so the detectors have a baseline on day one."""
    config.ensure_dirs()
    sources = _load_sources(getattr(args, "source", None))
    doable = [s for s in sources if backfill.supported(s)]
    if not doable:
        console.print(
            "[yellow]None of the configured sources declare a backfill archive.[/yellow]\n"
            "[dim]Add a 'backfill' block with index_url and pattern to a source config.[/dim]"
        )
        return 1

    digest.banner("pulling history from the source archives")
    console.print(
        "[dim]This walks a public directory listing one file at a time, one request "
        "per second. It is not fast, and it only needs running once.[/dim]\n"
    )

    with db.session() as conn:
        pipeline.sync_sources(conn, sources)
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), transient=True, console=console,
        ) as bar:
            task = bar.add_task("starting", total=None)
            for source in doable:
                report = backfill.run(
                    conn, source,
                    max_files=getattr(args, "files", None),
                    progress=lambda m: bar.update(task, description=m),
                )
                bar.console.print(
                    f"  [green]{source.source_id}[/green]: {report.fetched} file(s) fetched, "
                    f"{report.skipped_existing} already held, "
                    f"{report.new_observations:,} reading(s), {report.new_events} item(s)"
                )
                for problem in (report.problems or [])[:3]:
                    bar.console.print(f"    [yellow]{problem}[/yellow]")

    console.print(
        "\n[dim]History is in. Run 'update --backfill' to detect across all of it.[/dim]"
    )
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    with db.session() as conn:
        summary = review.review_all(conn)
        digest.banner("how the engine's own past signals held up")
        console.print(
            f"[bold]{summary['total_signals']}[/bold] signal(s) on file, "
            f"[bold]{summary['judged']}[/bold] with a later reading to judge against."
        )
        for verdict, count in sorted(summary["verdicts"].items()):
            console.print(f"  {verdict:<16} {count}")
        console.print(f"\n[bold]{summary['verdict']}[/bold]")

        board = peak.scoreboard(conn)
        if board["recorded"]:
            console.print(
                f"\nPeak calls: [bold]{board['recorded']}[/bold] on file, "
                f"[bold]{board['settled']}[/bold] settled, "
                f"[bold]{board['scored']}[/bold] carrying a probability."
            )
            console.print(f"[bold]{board['verdict']}[/bold]")
            for bucket, entry in board["by_horizon"].items():
                console.print(
                    f"  {bucket.lstrip('|'):<10} {entry['n']:>4} call(s), "
                    f"Brier {entry['brier']:.3f}"
                )

        if getattr(args, "details", False):
            from rich.table import Table

            table = Table(title="By source", header_style="bold cyan", title_justify="left")
            table.add_column("Source")
            table.add_column("Fired", justify="right")
            table.add_column("Judged", justify="right")
            table.add_column("Blip rate", justify="right")
            table.add_column("Avg confidence", justify="right")
            for entry in review.source_scoreboard(conn):
                rate = entry["blip_rate"]
                table.add_row(
                    entry["source_id"], str(entry["fired"]), str(entry["judged"]),
                    "n/a" if rate is None else f"{rate:.0%}",
                    f"{entry['avg_confidence']:.2f}",
                )
            console.print(table)
    return 0


def cmd_peak(args: argparse.Namespace) -> int:
    """The ICI peak call: how close a coming hour is to setting the threshold."""
    from rich.table import Table

    # Recording is Cloudflare's job now. Both stores key a call on
    # (region, delivery day, horizon) and both insert-or-ignore, so a local run
    # that also recorded would make "the first call at each horizon stands" mean
    # "whichever database you happened to ask". This prints; --record is the
    # escape hatch for when the Worker is down.
    record = bool(getattr(args, "record", False))
    with db.session() as conn:
        settled = peak.settle_calls(conn)
        result = peak.report(conn, days=int(getattr(args, "days", 2) or 2))
        recorded = peak.record_calls(conn, result) if record else 0
        board = peak.scoreboard(conn)

    digest.banner("how close the next hours are to setting an ICI peak")
    console.print(
        f"Base period [bold]{result.period.label}[/bold]. Under the Industrial "
        f"Conservation Initiative a Class A consumer's Global Adjustment for the "
        f"following year is set by their demand in this period's five highest "
        f"Ontario Demand hours, counting at most one hour per day."
    )

    if result.threshold is None:
        console.print("\n[yellow]No demand history for this base period yet. "
                      "Run `signal update` and try again.[/yellow]")
        return 0

    table = Table(title="Peak hours set so far", header_style="bold cyan", title_justify="left")
    table.add_column("#", justify="right")
    table.add_column("Hour")
    table.add_column("Ontario demand", justify="right")
    for index, (observed_at, value) in enumerate(result.top, start=1):
        style = "bold" if index == len(result.top) else ""
        table.add_row(str(index), peak._hour_label(observed_at), f"{value:,.0f} MW", style=style)
    console.print()
    console.print(table)
    console.print(
        f"The hour to beat is [bold]{result.threshold:,.0f} MW[/bold]. "
        f"Anything above it displaces the bottom of that list."
    )

    console.print()
    for call in result.calls:
        if call.probability is None:
            verdict, colour = "not yet callable", "yellow"
        elif call.probability >= 0.5:
            verdict, colour = "curtail", "red"
        elif call.probability >= 0.15:
            verdict, colour = "watch", "yellow"
        else:
            verdict, colour = "stand down", "green"

        if call.probability is None:
            chance = "unknown"
        elif call.probability < 0.001:
            # Never print a bare 0%. The model has thin tails and the reader
            # should not take "impossible" from a normal approximation.
            chance = "under 0.1%"
        elif call.probability < 0.01:
            chance = f"{call.probability:.1%}"
        else:
            chance = f"{call.probability:.0%}"
        console.print(
            f"[bold]{call.date}[/bold]  peak hour [bold]{call.peak_hour_label.split()[-1]}[/bold] "
            f"at {call.forecast_mw:,.0f} MW, [bold]{call.margin_mw:+,.0f} MW[/bold] against "
            f"the threshold"
        )
        console.print(
            f"  chance of setting a peak [bold {colour}]{chance}[/bold {colour}]  "
            f"[{colour}]{verdict}[/{colour}]"
        )
        console.print(f"  [dim]{call.basis}[/dim]")

    console.print()
    console.print(
        f"[dim]Local track record: {board['recorded']} call(s) on file, "
        f"{board['settled']} settled"
        + (f", {recorded} recorded just now" if recorded else "")
        + (f", {settled} graded just now" if settled else "")
        + f". {board['verdict']}[/dim]"
    )
    if not record:
        console.print(
            "[dim]This call was not recorded. The record lives on Cloudflare, which "
            "makes the call each market morning; `signal pull` brings it back here. "
            "Use --record only if the Worker is down.[/dim]"
        )

    if getattr(args, "notify", False):
        headline = peak.worth_raising(result)
        if headline:
            notify.desktop("Signal Arbitrage", headline)

    if result.notes:
        console.print()
        for note in result.notes:
            console.print(f"[yellow]·[/yellow] {note}")

    console.print(
        "\n[dim]The threshold and the forecast are both published by IESO; subtracting "
        "one from the other is public arithmetic. The probability is this engine's own, "
        "from a normal approximation on measured forecast error, and forecast errors have "
        "fatter tails than a normal on exactly the days that matter. Read a small "
        "probability as a floor, not a dismissal.[/dim]"
    )
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    """Install, remove or inspect the unattended run."""
    action = getattr(args, "action", None) or "status"

    if action == "remove":
        removed = schedule_mod.remove()
        console.print("[green]Removed.[/green]" if removed
                      else "[dim]Nothing was installed.[/dim]")
        return 0

    if action == "install":
        state = schedule_mod.install(int(getattr(args, "interval", 0)
                                        or schedule_mod.DEFAULT_INTERVAL_SECONDS))
        if state.loaded:
            console.print(
                f"[green]Running unattended every {state.interval}s.[/green]\n"
                f"  agent  {state.path}\n  log    {state.log}"
            )
            console.print(
                "[dim]It fetches whatever is due and makes the peak call each morning. "
                "Failures are logged and raised as a desktop notification. "
                "`signal schedule --remove` undoes all of it.[/dim]"
            )
        else:
            console.print(f"[red]Installed but not loaded.[/red] {state.detail}")
        return 0

    state = schedule_mod.status()
    if not state.installed:
        console.print(
            "[yellow]Not scheduled.[/yellow] The engine only runs when you run it, so "
            "no track record accumulates and gaps in the series cannot be recovered "
            "later.\n[dim]signal schedule --install[/dim]"
        )
        return 0
    console.print(
        f"{'[green]Loaded[/green]' if state.loaded else '[red]Installed but not loaded[/red]'}"
        f" · every {state.interval}s · {state.path}"
    )
    if state.detail:
        console.print(f"[yellow]{state.detail}[/yellow]")
    recent = schedule_mod.recent_log()
    if recent:
        console.print("\n[dim]Recent cycles:[/dim]")
        for line in recent:
            style = "red" if "FAILED" in line else "dim"
            console.print(f"  [{style}]{line}[/{style}]")
    else:
        console.print("[dim]No cycle has logged anything yet.[/dim]")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Copy the raw payloads off the machine, where they cannot be re-fetched from."""
    destination = getattr(args, "destination", None)
    report = backup_mod.run(Path(destination) if destination else None)

    if report.destination is None or (not report.ok and report.detail and not report.copied):
        console.print(f"[yellow]Not backed up:[/yellow] {report.detail}")
        return 1
    if report.ok:
        console.print(
            f"[green]Backed up.[/green] {report.destination_files} file(s) at "
            f"{report.destination}"
            + (f", {report.copied} copied this run." if report.copied else ", nothing new.")
        )
    else:
        console.print(f"[yellow]Incomplete:[/yellow] {report.detail}")
    console.print(
        "[dim]data/raw is the source of truth and the upstream archive only reaches "
        "back about three months, so anything older lives here and nowhere else.[/dim]"
    )
    return 0 if report.ok else 1


def cmd_pull(args: argparse.Namespace) -> int:
    """Bring the track record back from Cloudflare, where it is the only copy."""
    if getattr(args, "configure", False):
        base = Prompt.ask("Worker base URL", default=remote.keychain_get(remote.ACCOUNT_BASE_URL) or "")
        token = Prompt.ask("API token (not the dashboard password)", password=True)
        ok = (remote.keychain_set(remote.ACCOUNT_BASE_URL, base.strip())
              and remote.keychain_set(remote.ACCOUNT_TOKEN, token.strip()))
        console.print("[green]Stored in the Keychain.[/green]" if ok
                      else "[red]Could not write to the Keychain.[/red]")
        return 0 if ok else 1

    if not remote.configured():
        console.print(
            "[yellow]The Worker's address and API token are not set.[/yellow]\n"
            "[dim]signal pull --configure[/dim]"
        )
        return 1

    config.ensure_dirs()
    raw = not getattr(args, "calls_only", False)
    try:
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            transient=True, console=console,
        ) as bar:
            bar.add_task("pulling from Cloudflare", total=None)
            with db.session() as conn:
                report = remote.pull(conn, raw=raw, full=getattr(args, "full", False))
    except remote.RemoteError as exc:
        console.print(f"[red]Could not reach the Worker:[/red] {exc}")
        return 1

    console.print(
        f"[green]Pulled.[/green] {report.calls_seen} call(s) on the Worker, "
        f"{report.calls_new} new here."
    )
    if raw:
        console.print(
            f"  payloads  {report.payloads:,} new from {report.sources} feed(s), "
            f"{report.payload_bytes / 1_048_576:.1f} MB"
            if report.payloads else "  payloads  nothing new"
        )
    console.print(f"  snapshot  {report.snapshot}")
    console.print(
        "[dim]The Worker's D1 store is the only copy of the record until this runs, "
        "and a call cannot be re-derived the way a raw payload can. Run "
        "`signal backup` afterwards to put the snapshot off the machine.[/dim]"
    )
    for problem in report.problems:
        console.print(f"[yellow]·[/yellow] {problem}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    with db.session() as conn:
        paths = export.write_all(conn)
    console.print("[green]Exports written:[/green]")
    for path in paths:
        console.print(f"  {path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    with db.session() as conn:
        digest.banner("what the store holds")
        digest.counts_panel(db.counts(conn))
        last = conn.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if last:
            from . import timeutil

            console.print(
                f"[dim]Last run: {timeutil.to_display(last['started_at'])} · "
                f"{last['kind']} · {last['new_signals']} new signal(s), "
                f"{last['sources_fail']} source failure(s)[/dim]"
            )
        else:
            console.print("[dim]No runs recorded yet. Try 'update'.[/dim]")
    return 0


def _series_counts(path: Path) -> dict[tuple[str, str], int]:
    """Readings per (source, series), which is the granularity a gap hides in."""
    conn = db.connect(path)
    try:
        return {
            (row["source_id"], row["series_key"]): row["n"]
            for row in conn.execute(
                "SELECT source_id, series_key, COUNT(*) AS n FROM observations "
                "GROUP BY source_id, series_key"
            )
        }
    finally:
        conn.close()


def cmd_rebuild(args: argparse.Namespace) -> int:
    """Disaster-recovery drill: rebuild the store from raw payloads alone."""
    config.ensure_dirs()
    loaded = _load_sources()

    if getattr(args, "check", False):
        return _rebuild_check(loaded)

    with db.session() as conn:
        before = db.counts(conn)

    console.print(
        "[yellow]This rebuilds the SQLite store from the raw payloads on disk.[/yellow]\n"
        "[dim]Raw payloads are untouched. The rebuilt store replaces the current one "
        "atomically.[/dim]"
    )
    if sys.stdin.isatty() and not getattr(args, "yes", False):
        if not Confirm.ask("Rebuild now?", default=False):
            console.print("[dim]Left alone.[/dim]")
            return 0

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        transient=True, console=console,
    ) as bar:
        task = bar.add_task("rebuilding", total=None)
        totals = pipeline.rebuild_from_raw(
            loaded, progress=lambda m: bar.update(task, description=m)
        )

    with db.session() as conn:
        after = db.counts(conn)

    console.print(
        f"[green]Rebuilt from {totals['payloads']} raw payload(s).[/green]\n"
        f"  observations {before['observations']:,} -> {after['observations']:,}\n"
        f"  events       {before['events']:,} -> {after['events']:,}"
    )
    if after["observations"] < before["observations"] or after["events"] < before["events"]:
        console.print(
            "[red]The rebuild produced less data than the store held. That means raw is not "
            "a complete record. Investigate before trusting a restore.[/red]"
        )
        return 1
    console.print("[dim]Signals are not rebuilt; run 'update --backfill' to re-detect.[/dim]")
    return 0


def _rebuild_check(loaded) -> int:
    """Rebuild into a scratch store and diff it, without touching the live one.

    "It ran without error" is not the test that matters: an empty rebuild passes
    that. What matters is whether every series comes back at the same count, so
    this compares per source and series and names whatever is short.
    """
    scratch = config.DATA_DIR / "rebuild-check.db"
    scratch.unlink(missing_ok=True)
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        transient=True, console=console,
    ) as bar:
        task = bar.add_task("rebuilding into a scratch store", total=None)
        totals = pipeline.rebuild_from_raw(
            loaded, target=scratch, progress=lambda m: bar.update(task, description=m)
        )

    live = _series_counts(config.db_path())
    rebuilt = _series_counts(scratch)
    scratch.unlink(missing_ok=True)

    short = {key: (live[key], rebuilt.get(key, 0)) for key in live
             if rebuilt.get(key, 0) < live[key]}
    extra = sorted(set(rebuilt) - set(live))

    console.print(
        f"[green]Rebuilt {totals['payloads']} raw payload(s) into a scratch store.[/green]\n"
        f"  series      {len(live)} live, {len(rebuilt)} rebuilt\n"
        f"  readings    {sum(live.values()):,} live, {sum(rebuilt.values()):,} rebuilt"
    )
    if extra:
        console.print(
            f"[dim]{len(extra)} series exist only in the rebuild. That is raw holding "
            "more than the store, which is the right way round.[/dim]"
        )
    if short:
        console.print("[red]Raw does not hold everything the store does:[/red]")
        for (source_id, series_key), (was, now) in sorted(short.items())[:12]:
            console.print(f"  {source_id} · {series_key}: {was:,} live, {now:,} rebuilt")
        if len(short) > 12:
            console.print(f"  [dim]and {len(short) - 12} more[/dim]")
        return 1
    console.print("[green]Every series rebuilds to at least its live count.[/green]")
    return 0


# --------------------------------------------------------------------------
# Interactive menu
# --------------------------------------------------------------------------

MENU: list[tuple[str, str, Callable[[argparse.Namespace], int]]] = [
    ("Check for new signals", "fetch anything due, then run the detectors", cmd_update),
    ("Show recent signals", "what has fired lately", cmd_signals),
    ("Source health", "what is configured and whether it is working", cmd_sources),
    ("Validate sources", "parse the configs, optionally call each endpoint", cmd_check),
    ("Backfill history", "pull dated archives so detectors have a baseline", cmd_backfill),
    ("Peak call", "how close the next hours are to setting an ICI peak", cmd_peak),
    ("Grade past signals", "how the engine's own calls held up", cmd_review),
    ("Scheduling", "run unattended, which is what the track record needs", cmd_schedule),
    ("Pull the record", "bring the calls back from Cloudflare", cmd_pull),
    ("Back up", "copy the raw payloads off this machine", cmd_backup),
    ("Export", "write CSV and JSON to exports/", cmd_export),
    ("Store status", "counts and the last run", cmd_status),
    ("Rebuild from raw", "disaster-recovery drill", cmd_rebuild),
]


def menu() -> int:
    digest.banner("interactive menu · everything the engine can do")
    while True:
        console.print()
        for index, (label, blurb, _) in enumerate(MENU, start=1):
            console.print(f"  [bold cyan]{index}[/bold cyan]  {label}  [dim]{blurb}[/dim]")
        console.print("  [bold cyan]q[/bold cyan]  quit")

        choice = Prompt.ask("\nWhat would you like to do", default="1").strip().lower()
        if choice in ("q", "quit", "exit"):
            console.print("[dim]Bye.[/dim]")
            return 0
        if not choice.isdigit() or not 1 <= int(choice) <= len(MENU):
            console.print("[yellow]Pick a number from the list, or q to quit.[/yellow]")
            continue

        label, _, handler = MENU[int(choice) - 1]
        args = argparse.Namespace(details=False)
        try:
            _prompt_for(handler, args)
            handler(args)
        except SystemExit as exc:
            if exc.code:
                console.print(f"[red]That did not complete (exit {exc.code}).[/red]")
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped.[/dim]")
        except Exception as exc:
            console.print(f"[red]Something went wrong: {exc}[/red]")


def _prompt_for(handler: Callable, args: argparse.Namespace) -> None:
    """Ask only for what the chosen operation actually needs."""
    if handler is cmd_update:
        args.force = Confirm.ask("Fetch even if nothing is due yet?", default=False)
        args.backfill = Confirm.ask("Re-run detectors over the full history?", default=False)
        args.detect_only = False
        args.source = None
        args.details = Confirm.ask("Show the technical detail for each signal?", default=False)
    elif handler is cmd_signals:
        args.limit = IntPrompt.ask("How many?", default=20)
        region = Prompt.ask("Region filter (blank for all)", default="").strip()
        args.region = region.lower() or None
        args.min_confidence = float(
            Prompt.ask("Minimum confidence, 0 to 1", default="0")
        )
        args.details = Confirm.ask("Show the technical detail?", default=False)
        args.json = False
    elif handler is cmd_backfill:
        args.source = None
        args.files = IntPrompt.ask("How many archive files per source?", default=72)
    elif handler is cmd_check:
        args.source = None
        args.live = Confirm.ask("Call each endpoint for real?", default=True)
    elif handler in (cmd_sources, cmd_review):
        args.details = Confirm.ask("Show the detailed breakdown?", default=True)
    elif handler is cmd_rebuild:
        args.yes = False


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signal",
        description=(
            "Signal Arbitrage: ingest public data feeds, detect what moved, and emit "
            "signals with provenance. Run with no arguments for the menu."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("update", help="fetch what is due and run the detectors")
    p.add_argument("--force", action="store_true", help="fetch even if nothing is due")
    p.add_argument("--backfill", action="store_true", help="detect over the full history")
    p.add_argument("--detect-only", action="store_true", help="skip fetching")
    p.add_argument("--source", help="limit to one source_id")
    p.add_argument("--details", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("signals", help="show recent signals")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--region", help="e.g. on")
    p.add_argument("--min-confidence", type=float, default=0.0, dest="min_confidence")
    p.add_argument("--details", action="store_true")
    p.add_argument("--json", action="store_true", help="machine-readable payloads")
    p.set_defaults(func=cmd_signals)

    p = sub.add_parser("sources", help="what is configured and how it is doing")
    p.add_argument("--details", action="store_true")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("check", help="validate source configs")
    p.add_argument("--live", action="store_true", help="also call each endpoint")
    p.add_argument("--source")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("backfill", help="pull dated history from source archives")
    p.add_argument("--source", help="limit to one source_id")
    p.add_argument("--files", type=int, help="max archive files per source")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("peak", help="how close the next hours are to setting an ICI peak")
    p.add_argument("--days", type=int, default=2, help="how many delivery days ahead to call")
    p.add_argument("--notify", action="store_true",
                   help="raise a desktop notification when a call warrants one")
    p.add_argument("--record", action="store_true",
                   help="also write the call locally (normally Cloudflare's job)")
    p.set_defaults(func=cmd_peak)

    p = sub.add_parser("schedule", help="run unattended, or check that it is")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--install", dest="action", action="store_const", const="install",
                       help="install and load the launchd agent")
    group.add_argument("--remove", dest="action", action="store_const", const="remove",
                       help="unload and delete it")
    p.add_argument("--interval", type=int, default=schedule_mod.DEFAULT_INTERVAL_SECONDS,
                   help="seconds between cycles")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("pull", help="bring the record and the raw payloads back from Cloudflare")
    p.add_argument("--calls-only", action="store_true",
                   help="skip the raw payloads and pull only the track record")
    p.add_argument("--full", action="store_true",
                   help="walk the whole retention window instead of resuming, to fill a gap")
    p.add_argument("--configure", action="store_true",
                   help="store the Worker address and token in the Keychain")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("backup", help="copy the raw payloads off this machine")
    p.add_argument("--destination", help="where to copy to (defaults to iCloud Drive)")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("review", help="grade the engine's own past signals")
    p.add_argument("--details", action="store_true")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("export", help="write CSV and JSON exports")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("status", help="store counts and last run")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("rebuild", help="rebuild the store from raw payloads")
    p.add_argument("--yes", action="store_true", help="skip the confirmation")
    p.add_argument("--check", action="store_true",
                   help="rebuild into a scratch store and diff it, leaving the live one alone")
    p.set_defaults(func=cmd_rebuild)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) is None:
        # Invariant of the house style: the menu is the front door, but only
        # when there is a human at the other end.
        if sys.stdin.isatty() and sys.stdout.isatty():
            try:
                return menu()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Bye.[/dim]")
                return 0
        parser.print_help()
        return 0

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
