"""An off-machine copy of the only thing that cannot be re-fetched.

`data/raw/` is the source of truth: the SQLite store is derived and rebuilds
from it, so losing the store costs an afternoon and losing the raw payloads
costs the history. The upstream archive only reaches back about three months, so
anything older than that exists nowhere else once this directory is gone.

Deliberately not a mirror. Raw payloads are content-addressed and never change,
so the copy only ever grows; syncing deletions would let a damaged source
directory quietly empty the backup too, which is the failure a backup exists to
prevent.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

ICLOUD = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
FOLDER_NAME = "signal-arbitrage"


@dataclass
class BackupReport:
    destination: Path | None
    copied: int = 0
    source_files: int = 0
    destination_files: int = 0
    ok: bool = False
    detail: str = ""


def default_destination() -> Path | None:
    return (ICLOUD / FOLDER_NAME) if ICLOUD.is_dir() else None


def _count(directory: Path) -> int:
    return sum(1 for path in directory.rglob("*") if path.is_file())


def run(destination: Path | None = None) -> BackupReport:
    """Copy the raw payloads and exports off the machine. Never raises."""
    target = destination or default_destination()
    if target is None:
        return BackupReport(
            destination=None,
            detail="iCloud Drive is not set up on this machine. Pass a destination.",
        )
    if shutil.which("rsync") is None:
        return BackupReport(destination=target, detail="rsync is not on the path.")

    source = config.raw_dir()
    if not source.is_dir():
        return BackupReport(destination=target, detail="there is nothing in data/raw yet.")

    report = BackupReport(destination=target, source_files=_count(source))
    try:
        target.mkdir(parents=True, exist_ok=True)
        for name, path in (("raw", source), ("exports", config.EXPORTS_DIR)):
            if not path.is_dir():
                continue
            result = subprocess.run(
                # Archive mode, no deletions, and itemised so the count is real
                # rather than assumed.
                ["rsync", "-a", "--itemize-changes", f"{path}/", str(target / name) + "/"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                report.detail = (result.stderr or result.stdout).strip()[:300]
                return report
            report.copied += sum(
                1 for line in result.stdout.splitlines() if line and not line.startswith("cd")
            )
        report.destination_files = _count(target / "raw") if (target / "raw").is_dir() else 0
        report.ok = report.destination_files >= report.source_files
        if not report.ok:
            report.detail = (
                f"the copy holds {report.destination_files} file(s) against "
                f"{report.source_files} here, so it is not complete."
            )
    except OSError as exc:
        report.detail = str(exc)[:300]
    return report
