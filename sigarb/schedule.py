"""Running unattended, which is what the whole project rests on.

Nothing here is clever. It matters because the peak line records a call only
when someone runs it, the detectors need an unbroken series to judge against,
and the archive upstream only reaches back about three months. A day the engine
does not run is a day of evidence that cannot be recovered later.

launchd rather than cron, because launchd runs a missed job after the machine
wakes and cron simply skips it. A laptop that sleeps every night would otherwise
lose every overnight cycle.
"""

from __future__ import annotations

import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

LABEL = "ca.signalarbitrage.engine"
DEFAULT_INTERVAL_SECONDS = 300           # the fastest source's cadence


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return agents_dir() / f"{LABEL}.plist"


@dataclass
class Status:
    installed: bool
    loaded: bool
    path: Path
    interval: int | None
    log: Path
    detail: str = ""


def _definition(interval: int) -> dict:
    root = project_root()
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", str(root / "bin" / "scheduled.sh")],
        "StartInterval": interval,
        # Run once at load so a fresh install proves itself immediately rather
        # than looking installed and doing nothing for the first interval.
        "RunAtLoad": True,
        "WorkingDirectory": str(root),
        "StandardOutPath": str(root / "data" / "logs" / "launchd.out.log"),
        "StandardErrorPath": str(root / "data" / "logs" / "launchd.err.log"),
        "ProcessType": "Background",
        # Never wake a sleeping machine for this. Public feeds keep an archive;
        # a missed cycle catches up on the next one.
        "LowPriorityIO": True,
    }


def status() -> Status:
    path = plist_path()
    log = project_root() / "data" / "logs" / "scheduled.log"
    if not path.exists():
        return Status(installed=False, loaded=False, path=path, interval=None, log=log)

    try:
        with path.open("rb") as handle:
            interval = int(plistlib.load(handle).get("StartInterval", 0)) or None
    except Exception as exc:
        return Status(installed=True, loaded=False, path=path, interval=None, log=log,
                      detail=f"the plist could not be read: {exc}")

    listed = subprocess.run(
        ["launchctl", "list", LABEL], capture_output=True, text=True, check=False
    )
    return Status(
        installed=True, loaded=listed.returncode == 0, path=path,
        interval=interval, log=log,
    )


def install(interval: int = DEFAULT_INTERVAL_SECONDS) -> Status:
    """Write the agent and load it. Safe to re-run; it replaces cleanly."""
    root = project_root()
    (root / "data" / "logs").mkdir(parents=True, exist_ok=True)
    agents_dir().mkdir(parents=True, exist_ok=True)

    path = plist_path()
    # Write beside and replace, so launchd never reads a half-written plist.
    temp = path.with_suffix(".plist.tmp")
    with temp.open("wb") as handle:
        plistlib.dump(_definition(interval), handle)
    temp.replace(path)

    subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
    loaded = subprocess.run(["launchctl", "load", str(path)], capture_output=True,
                            text=True, check=False)
    result = status()
    if loaded.returncode != 0 and not result.loaded:
        result.detail = (loaded.stderr or loaded.stdout).strip()[:300]
    return result


def remove() -> bool:
    """Unload and delete the agent. True if there was one to remove."""
    path = plist_path()
    if not path.exists():
        return False
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
    path.unlink()
    return True


def recent_log(lines: int = 12) -> list[str]:
    log = project_root() / "data" / "logs" / "scheduled.log"
    if not log.exists():
        return []
    try:
        return log.read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return []
