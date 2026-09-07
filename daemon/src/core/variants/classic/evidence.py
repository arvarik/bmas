"""The evidence policy of the Classic runtime.

The evidence policy validates evidence records: it normalizes the
sources an agent supplies with an entry, and it decides whether a round
of contributions carries any external grounding. Every function reads
its input from its arguments and touches no storage.
"""
from __future__ import annotations

from typing import Any

MAX_SOURCES_PER_ENTRY = 8
MAX_SOURCE_CHARS = 500

# The entry types that count as contributions under the evidence gate.
EVIDENCE_TYPES = frozenset({"finding", "rebuttal"})


class EvidencePolicy:
    """Validate evidence records and claim support."""

    @staticmethod
    def normalize_sources(value: Any) -> list[str]:
        """Return a clean list of source strings from agent-supplied data."""
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if not text:
                continue
            cleaned.append(text[:MAX_SOURCE_CHARS])
            if len(cleaned) >= MAX_SOURCES_PER_ENTRY:
                break
        return cleaned

    @staticmethod
    def round_lacks_evidence(entries: list[Any]) -> bool:
        """True when a round contributed findings but none carries a source."""
        contributions = [
            entry for entry in entries
            if getattr(entry, "type", None) in EVIDENCE_TYPES
        ]
        if not contributions:
            return False
        return not any(getattr(entry, "sources", None) for entry in contributions)
