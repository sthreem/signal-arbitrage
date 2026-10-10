"""Presentation. Plain English first, technical depth behind --details."""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import config, timeutil

console = Console()

CONFIDENCE_BANDS = (
    (0.80, "strong", "bold green"),
    (0.65, "moderate", "green"),
    (0.50, "weak", "yellow"),
    (0.00, "very weak", "dim yellow"),
)


def band(confidence: float) -> tuple[str, str]:
    for floor, label, style in CONFIDENCE_BANDS:
        if confidence >= floor:
            return label, style
    return "very weak", "dim"


def banner(subtitle: str = "") -> None:
    console.print(
        Panel(
            Text.assemble(
                ("Signal Arbitrage", "bold cyan"),
                ("\npublic data in, decision triggers out", "dim"),
                (f"\n{subtitle}", "dim italic") if subtitle else ("", ""),
            ),
            border_style="cyan",
            padding=(0, 2),
        )
    )


def signals_table(rows: Sequence[Any], *, details: bool = False, title: str = "Signals") -> None:
    if not rows:
        console.print("[dim]No signals. A quiet feed is a real answer, not a failure.[/dim]")
        return

    table = Table(title=title, header_style="bold cyan", expand=True, title_justify="left")
    table.add_column("When", style="dim", no_wrap=True)
    table.add_column("Region", no_wrap=True)
    table.add_column("What happened", overflow="fold")
    table.add_column("Confidence", no_wrap=True)

    for row in rows:
        payload = row if isinstance(row, dict) else _row_to_payload(row)
        label, style = band(float(payload["confidence_score"]))
        table.add_row(
            timeutil.to_display(payload["observed_at"]),
            str(payload["region_code"]).upper(),
            payload["headline"],
            Text(f"{payload['confidence_score']:.2f} {label}", style=style),
        )
    console.print(table)

    if details:
        for row in rows:
            payload = row if isinstance(row, dict) else _row_to_payload(row)
            _detail_panel(payload)


def _detail_panel(payload: dict[str, Any]) -> None:
    body = Text()
    body.append(payload["explanation"] + "\n\n")
    body.append("signal_id  ", style="dim")
    body.append(payload["signal_id"] + "\n")
    body.append("detector   ", style="dim")
    body.append(f"{payload['detector']}  ·  series {payload['series_key'] or 'n/a'}\n")
    body.append("observed   ", style="dim")
    body.append(f"{payload['observed_at']}  ·  processed {payload['processing_timestamp']}\n")
    body.append("provenance ", style="dim")
    for entry in payload["provenance"]:
        body.append(f"{entry['source_name']} ({entry['licence']}, {entry['licence_scope']})\n           ")
    body.append("\ndetails    ", style="dim")
    body.append(json.dumps(payload["details"], indent=2, sort_keys=True) + "\n")
    for text in payload["disclaimers"]:
        body.append(f"\n{text}", style="dim italic")
    console.print(Panel(body, title=payload["headline"][:80], border_style="dim", padding=(0, 2)))


def _row_to_payload(row: Any) -> dict[str, Any]:
    from .signals import as_payload

    return as_payload(row)


def sources_table(rows: Iterable[Any], *, details: bool = False) -> None:
    table = Table(title="Sources", header_style="bold cyan", expand=True, title_justify="left")
    table.add_column("Source", overflow="fold")
    table.add_column("Region", no_wrap=True)
    table.add_column("Every", no_wrap=True)
    table.add_column("Last success", no_wrap=True)
    table.add_column("State", no_wrap=True)
    if details:
        table.add_column("Licence", overflow="fold")

    for row in rows:
        errors = int(row["consecutive_errors"] or 0)
        if errors >= 3:
            state = Text("failing", style="bold red")
        elif errors:
            state = Text("wobbly", style="yellow")
        elif row["last_success_at"]:
            state = Text("healthy", style="green")
        else:
            state = Text("never run", style="dim")

        cells = [
            row["name"],
            str(row["region_code"]).upper(),
            _cadence(int(row["update_frequency_s"])),
            timeutil.humanise_seconds(timeutil.age_seconds(row["last_success_at"])),
            state,
        ]
        if details:
            cells.append(f"{row['licence']} ({row['licence_scope']})")
        table.add_row(*cells)
    console.print(table)


def _cadence(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def run_summary(report: Any) -> None:
    fetched = sum(1 for o in report.outcomes if o.status == "fetched")
    unchanged = sum(1 for o in report.outcomes if o.status == "unchanged")
    skipped = sum(1 for o in report.outcomes if o.status == "skipped")
    errors = report.errors

    console.print(
        f"\n[bold]{report.new_signals}[/bold] new signal(s) · "
        f"[bold]{report.new_observations}[/bold] new reading(s) · "
        f"[bold]{report.new_events}[/bold] new item(s)"
    )
    console.print(
        f"[dim]{fetched} source(s) fetched, {unchanged} unchanged, {skipped} not due, "
        f"{len(errors)} failed[/dim]"
    )
    for outcome in errors:
        console.print(f"[red]  {outcome.source_id}: {outcome.message}[/red]")
    for outcome in report.outcomes:
        for problem in outcome.problems:
            console.print(f"[yellow]  {outcome.source_id}: {problem}[/yellow]")


def counts_panel(counts: dict[str, int]) -> None:
    table = Table.grid(padding=(0, 3))
    table.add_column(style="dim")
    table.add_column(justify="right", style="bold")
    labels = {
        "sources": "sources configured",
        "raw_payloads": "raw payloads kept",
        "observations": "numeric readings",
        "events": "text items",
        "signals": "signals emitted",
    }
    for key, label in labels.items():
        table.add_row(label, f"{counts.get(key, 0):,}")
    console.print(Panel(table, title="Store", border_style="cyan", padding=(0, 2)))


def disclaimer_footer() -> None:
    console.print(
        f"\n[dim italic]{config.DISCLAIMER_TEXT['source_aggregated']} "
        f"{config.DISCLAIMER_TEXT['not_financial_advice']}[/dim italic]"
    )
