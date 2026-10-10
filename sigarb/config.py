"""Paths, constants and runtime settings.

Everything resolves from the package location, so the project works wherever it
is cloned. Nothing here reads the network or the environment except for optional
secrets, which come from the launcher, never from a file in the repo.
"""

from __future__ import annotations

import os
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SOURCES_DIR = PROJECT_ROOT / "sources"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
EXPORTS_DIR = PROJECT_ROOT / "exports"
DB_PATH = DATA_DIR / "signals.db"

UTC = timezone.utc
DISPLAY_TZ = ZoneInfo("America/Toronto")

# Network manners. The engine is a guest on every host it touches.
USER_AGENT = "signal-arbitrage/0.1 (public-data signal engine; contact via repo)"
REQUEST_TIMEOUT = 30
MIN_SECONDS_BETWEEN_REQUESTS_PER_HOST = 1.0
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

# Determinism: every stochastic component pins this.
RANDOM_SEED = 20260818

# Detector defaults, overridable per source config.
DEFAULT_ZSCORE_WINDOW = 60
DEFAULT_ZSCORE_MIN_PERIODS = 20
DEFAULT_ZSCORE_THRESHOLD = 3.0

DISCLAIMER_TEXT = {
    "model_derived": (
        "This signal was produced by an automated detector, not by a human "
        "analyst."
    ),
    "source_aggregated": (
        "Derived from publicly available sources and algorithmically "
        "synthesized. No warranty of completeness or primary research."
    ),
    "not_financial_advice": (
        "Information only. This is not financial, investment, legal or tax "
        "advice, and is not a recommendation about any security."
    ),
    "scheduled_release": (
        "The underlying source publishes on a known calendar, so this signal "
        "carries no timing advantage over the publication itself."
    ),
    "low_sample": (
        "Fired on a short history. The baseline is not yet well established and "
        "the false-positive rate is correspondingly higher."
    ),
    "unit_unverified": (
        "Units or currency for this series were not independently confirmed."
    ),
    "redistribution_unresolved": (
        "The source licence does not clearly permit redistributing this content to "
        "third parties. Internal use only until the terms are resolved in writing."
    ),
}


def ensure_dirs() -> None:
    """Create the directories the engine writes to. Safe to call repeatedly."""
    for path in (DATA_DIR, RAW_DIR, EXPORTS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def db_path() -> Path:
    """Allow an override for tests without threading a parameter everywhere."""
    override = os.environ.get("SIGNAL_DB_PATH")
    return Path(override) if override else DB_PATH


def raw_dir() -> Path:
    override = os.environ.get("SIGNAL_RAW_DIR")
    return Path(override) if override else RAW_DIR


def sources_dir() -> Path:
    override = os.environ.get("SIGNAL_SOURCES_DIR")
    return Path(override) if override else SOURCES_DIR
