"""Source configs: the region-agnostic ingestion schema.

One YAML file per source in `sources/`. Adding a region means dropping in new
files, never touching the pipeline. A config missing its licence or terms check
is refused at load time rather than quietly fetched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import yaml

from . import config

VALID_DATA_TYPES = {"timeseries_numeric", "event_text", "dataset_metadata"}
VALID_ADAPTERS = {"rss", "json_api", "csv_feed", "xml_feed"}
VALID_LICENCE_SCOPES = {
    "commercial_use_permitted",
    "attribution_required",
    "non_commercial_only",
    "unknown",
}

_DURATION = re.compile(r"^(\d+)\s*([smhdw])$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class ConfigError(ValueError):
    """A source config that cannot be trusted to run."""


def parse_duration(text: str | int) -> int:
    """'5m' -> 300. Plain integers are already seconds."""
    if isinstance(text, int):
        return text
    match = _DURATION.match(str(text).strip().lower())
    if not match:
        raise ConfigError(f"cannot read update_frequency {text!r}, expected e.g. '5m' or '1d'")
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


@dataclass(frozen=True)
class DetectorSpec:
    type: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Source:
    source_id: str
    name: str
    region_code: str
    data_type: str
    adapter: str
    update_frequency_s: int
    licence: str
    licence_scope: str
    terms_check: str
    signal_potential: str
    enabled: bool
    request: dict[str, Any]
    parse: dict[str, Any]
    detectors: tuple[DetectorSpec, ...]
    raw: dict[str, Any]

    def _request_now(self, now: datetime) -> datetime:
        """`now` in the zone the source names its files by.

        A market that stamps its files EST rolls its date over at 19:00 or 20:00
        UTC, so resolving a calendar-named URL against UTC asks for tomorrow's
        file all evening and gets a 404.
        """
        name = self.request.get("date_timezone")
        if not name:
            return now
        try:
            return now.astimezone(ZoneInfo(str(name)))
        except Exception:
            return now

    def resolved_url(self, now: datetime) -> str:
        """Fill date placeholders such as {date:%Y%m%d} for calendar-named files."""
        url = str(self.request["url"])
        return _fill_dates(url, self._request_now(now))

    def resolved_params(self, now: datetime) -> dict[str, Any]:
        params = self.request.get("params") or {}
        moment = self._request_now(now)
        return {k: _fill_dates(str(v), moment) for k, v in params.items()}

    def as_db_row(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "region_code": self.region_code,
            "data_type": self.data_type,
            "adapter": self.adapter,
            "update_frequency_s": self.update_frequency_s,
            "licence": self.licence,
            "licence_scope": self.licence_scope,
            "terms_check": self.terms_check,
            "signal_potential": self.signal_potential,
            "enabled": 1 if self.enabled else 0,
            "config_json": _dump(self.raw),
        }


_DATE_TOKEN = re.compile(r"\{date(?::([^}]+))?\}")
_DATE_OFFSET_TOKEN = re.compile(r"\{date([+-]\d+)d(?::([^}]+))?\}")


def _fill_dates(text: str, now: datetime) -> str:
    def offset_sub(match: re.Match[str]) -> str:
        days = int(match.group(1))
        fmt = match.group(2) or "%Y-%m-%d"
        return (now + timedelta(days=days)).strftime(fmt)

    def plain_sub(match: re.Match[str]) -> str:
        fmt = match.group(1) or "%Y-%m-%d"
        return now.strftime(fmt)

    text = _DATE_OFFSET_TOKEN.sub(offset_sub, text)
    return _DATE_TOKEN.sub(plain_sub, text)


def _dump(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _require(data: dict[str, Any], key: str, path: Path) -> Any:
    if key not in data or data[key] in (None, ""):
        raise ConfigError(f"{path.name}: missing required field '{key}'")
    value = data[key]
    # YAML 1.1 reads bare on/off/yes/no as booleans, which silently turns
    # region_code: on into True. Invariant 7 says region is a parameter, so this
    # has to fail loudly rather than produce a region called "true".
    if isinstance(value, bool) and key not in ("enabled",):
        raise ConfigError(
            f"{path.name}: field '{key}' was read as the boolean {value}. YAML treats "
            f"bare on/off/yes/no as booleans. Quote it, for example: {key}: \"on\""
        )
    return value


def load_source(path: Path) -> Source:
    """Read and validate one config. Raises ConfigError rather than guessing."""
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: top level must be a mapping")

    source_id = str(_require(data, "source_id", path))
    data_type = str(_require(data, "data_type", path))
    adapter = str(_require(data, "adapter", path))
    licence_scope = str(_require(data, "licence_scope", path))

    if data_type not in VALID_DATA_TYPES:
        raise ConfigError(f"{path.name}: data_type {data_type!r} not in {sorted(VALID_DATA_TYPES)}")
    if adapter not in VALID_ADAPTERS:
        raise ConfigError(f"{path.name}: adapter {adapter!r} not in {sorted(VALID_ADAPTERS)}")
    if licence_scope not in VALID_LICENCE_SCOPES:
        raise ConfigError(
            f"{path.name}: licence_scope {licence_scope!r} not in {sorted(VALID_LICENCE_SCOPES)}"
        )

    # Invariant 6: no source runs without a recorded terms check.
    terms_check = str(_require(data, "terms_check", path))
    licence = str(_require(data, "licence", path))

    request = data.get("request") or {}
    if not isinstance(request, dict) or not request.get("url"):
        raise ConfigError(f"{path.name}: request.url is required")

    detectors = tuple(
        DetectorSpec(type=str(d["type"]), options={k: v for k, v in d.items() if k != "type"})
        for d in (data.get("detectors") or [])
        if isinstance(d, dict) and d.get("type")
    )

    return Source(
        source_id=source_id,
        name=str(_require(data, "name", path)),
        region_code=str(_require(data, "region_code", path)).lower(),
        data_type=data_type,
        adapter=adapter,
        update_frequency_s=parse_duration(_require(data, "update_frequency", path)),
        licence=licence,
        licence_scope=licence_scope,
        terms_check=terms_check,
        signal_potential=str(data.get("signal_potential") or "unassessed"),
        enabled=bool(data.get("enabled", True)),
        request=request,
        parse=data.get("parse") or {},
        detectors=detectors,
        raw=data,
    )


def load_all(directory: Path | None = None) -> list[Source]:
    """Load every config, sorted by id. Invalid configs raise, never skip silently."""
    target = Path(directory) if directory else config.sources_dir()
    if not target.exists():
        return []
    sources = [load_source(p) for p in sorted(target.glob("*.yaml")) if not p.name.startswith("_")]
    seen: set[str] = set()
    for source in sources:
        if source.source_id in seen:
            raise ConfigError(f"duplicate source_id {source.source_id!r}")
        seen.add(source.source_id)
    return sources
