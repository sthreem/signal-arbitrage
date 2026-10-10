"""Wire-format adapters. One module per format, all returning ParseResult."""

from __future__ import annotations

from typing import Callable

from .base import ParseResult
from . import csv_feed, json_api, rss, xml_feed

REGISTRY: dict[str, Callable] = {
    "rss": rss.parse,
    "json_api": json_api.parse,
    "csv_feed": csv_feed.parse,
    "xml_feed": xml_feed.parse,
}


def get(adapter: str) -> Callable:
    try:
        return REGISTRY[adapter]
    except KeyError:
        raise ValueError(f"unknown adapter {adapter!r}, expected one of {sorted(REGISTRY)}")


__all__ = ["ParseResult", "REGISTRY", "get"]
