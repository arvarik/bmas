# /opt/bmas/daemon/src/core/log_levels.py
"""Canonical log-level normalization (dependency-free).

Lives in its own module so it can be imported anywhere — including tests
and lightweight tooling — without pulling in Redis or other heavy deps.

The UI renders the canonical words in full (INFO / WARNING / ERROR / DEBUG);
abbreviations are normalized away here at the source so no downstream
consumer has to guess.
"""
from __future__ import annotations

LEVEL_ALIASES: dict[str, str] = {
    "inf": "info",
    "info": "info",
    "information": "info",
    "wrn": "warning",
    "warn": "warning",
    "warning": "warning",
    "err": "error",
    "error": "error",
    "err!": "error",
    "fatal": "error",
    "critical": "error",
    "crit": "error",
    "dbg": "debug",
    "debug": "debug",
    "trace": "debug",
}


def normalize_level(level: str | None) -> str:
    """Map any level spelling/abbreviation to a canonical level word."""
    if not level:
        return "info"
    key = str(level).strip().lower()
    return LEVEL_ALIASES.get(key, key)
