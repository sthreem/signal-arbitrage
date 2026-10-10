"""Time handling. Invariant 3: everything is stored UTC ISO 8601 with a Z."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from dateutil import parser as date_parser

from . import config


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """UTC ISO 8601, second precision, always with a trailing Z."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    return iso(now_utc())


def parse(value: Any, *, assume_tz: timezone | Any = timezone.utc) -> datetime | None:
    """Best-effort parse of a wire timestamp into aware UTC. None on failure."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = date_parser.parse(str(value))
        except (ValueError, OverflowError, TypeError):
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=assume_tz)
    return moment.astimezone(timezone.utc)


def parse_iso(value: Any, *, assume_tz: timezone | Any = timezone.utc) -> str | None:
    moment = parse(value, assume_tz=assume_tz)
    return iso(moment) if moment else None


def to_display(value: str | datetime) -> str:
    """Render a stored UTC timestamp in Toronto local time, for humans only."""
    moment = value if isinstance(value, datetime) else parse(value)
    if moment is None:
        return "unknown time"
    local = moment.astimezone(config.DISPLAY_TZ)
    return local.strftime("%a %d %b %Y, %H:%M %Z")


def age_seconds(value: str | datetime | None, *, reference: datetime | None = None) -> float | None:
    moment = value if isinstance(value, datetime) else parse(value)
    if moment is None:
        return None
    return ((reference or now_utc()) - moment).total_seconds()


def humanise_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def since(days: int) -> str:
    return iso(now_utc() - timedelta(days=days))
