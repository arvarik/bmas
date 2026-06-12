# /opt/bmas/daemon/src/core/entry.py
"""Board entry envelope model (doc 04 §1).

Typed envelopes around natural-language bodies.  This is a pure data
model — no I/O, no Redis, no SQLite.  The Board Gateway uses these
types for validation and normalization.

Authors are opaque strings (seam rule 3): "planner", "expert.valuation",
"worker.extractor-2", "universal-3" are all valid.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

# ── Validation Constants ─────────────────────────────────────────────

DEFAULT_MAX_TITLE_LEN: int = 200
DEFAULT_MAX_BODY_LEN: int = 8000
DEFAULT_CONFIDENCE: float = 0.5

# ── Role → Default Entry Type (doc 04 §3) ────────────────────────────
#
# When an agent omits `type`, the gateway infers from the actor id.
# Actor ids are opaque strings; we match prefixes/exact strings.
# Unknown actors default to "finding" (safe fallback).

ROLE_DEFAULT_TYPE: dict[str, str] = {
    "planner": "plan",
    "critic": "critique",
    "conflict_resolver": "conflict",
    "decider": "solution",
    # "expert.*" is handled by prefix match in role_default_type()
    # "cleaner" has no default — cleaner uses the maintenance contract
}

# The prefix that maps to "finding" for generated experts
_EXPERT_PREFIX = "expert."
# Legacy alias: "executor" → "finding" (doc 04 §4 note)
_LEGACY_ALIASES: dict[str, str] = {
    "executor": "finding",
    "auditor": "critique",
}

# Fallback for unknown actors
_FALLBACK_TYPE = "finding"


def role_default_type(actor: str) -> str:
    """Infer the default entry type from an opaque actor id.

    Handles exact matches, prefix matches (expert.*), legacy aliases,
    and falls back to "finding" for unknown actors.
    """
    # Exact match first
    if actor in ROLE_DEFAULT_TYPE:
        return ROLE_DEFAULT_TYPE[actor]
    # Legacy alias
    if actor in _LEGACY_ALIASES:
        return _LEGACY_ALIASES[actor]
    # Prefix match for generated experts
    if actor.startswith(_EXPERT_PREFIX):
        return "finding"
    # Unknown actor → safe default
    return _FALLBACK_TYPE


# ── Data Classes ─────────────────────────────────────────────────────


@dataclass
class ProposedEntry:
    """What an agent submits in its turn response (doc 04 §3).

    These are the fields the agent may set.  The gateway strips
    reserved fields and fills in the rest.
    """
    body: str
    type: str | None = None        # Optional — defaults from actor's role
    title: str | None = None       # Short, indexable — agent-supplied
    refs: list[str] = field(default_factory=list)
    confidence: float | None = None  # 0..1, optional; default 0.5


@dataclass
class BoardEntry:
    """A committed board entry — the canonical shape (doc 04 §1).

    All fields are gateway-assigned except those that come from the
    agent's ProposedEntry.
    """
    id: str                          # stable, gateway-assigned (e.g. "e-14")
    task_id: str
    type: str                        # must be in ENTRY_TYPES
    author: str                      # opaque actor id (seam rule 3)
    body: str                        # natural language / markdown — the point

    # Optional / defaulted
    author_node: str | None = None
    title: str | None = None         # short, indexable
    refs: list[str] = field(default_factory=list)
    confidence: float = DEFAULT_CONFIDENCE
    status: str = "open"             # open | superseded | removed
    salience: float = 0.0            # gateway-computed (§7)
    round: int = 0                   # blackboard-cycle round
    space: str = "public"            # public | private:<topic>
    created_by_turn: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


def entry_to_dict(entry: BoardEntry) -> dict[str, Any]:
    """Serialize a BoardEntry to a JSON-safe dict."""
    d = asdict(entry)
    return d


def entry_from_dict(d: dict[str, Any]) -> BoardEntry:
    """Deserialize a dict to a BoardEntry."""
    # Parse refs from JSON string if needed
    refs = d.get("refs", [])
    if isinstance(refs, str):
        import json
        try:
            refs = json.loads(refs)
        except (json.JSONDecodeError, TypeError):
            refs = []
    return BoardEntry(
        id=d["id"],
        task_id=d["task_id"],
        type=d["type"],
        author=d["author"],
        body=d.get("body", ""),
        author_node=d.get("author_node"),
        title=d.get("title"),
        refs=refs if isinstance(refs, list) else [],
        confidence=float(d.get("confidence", DEFAULT_CONFIDENCE)),
        status=d.get("status", "open"),
        salience=float(d.get("salience", 0.0)),
        round=int(d.get("round", 0)),
        space=d.get("space", "public"),
        created_by_turn=d.get("created_by_turn"),
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
    )


def clamp_confidence(value: float | None) -> float:
    """Clamp confidence to [0, 1], defaulting to 0.5 for None/NaN."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return DEFAULT_CONFIDENCE
    return max(0.0, min(1.0, float(value)))


def envelope_fallback(raw_text: str, actor: str) -> ProposedEntry:
    """Wrap plain-text agent output as a single entry (doc 04 §3).

    Used when the agent's response has no parseable JSON block.
    The turn still contributes; the UI flags it with a "free-text" chip;
    the trace records envelope_fallback: true.
    """
    default_type = role_default_type(actor)
    # Title: first line, truncated to 80 chars
    first_line = raw_text.split("\n", 1)[0].strip()
    title = first_line[:80] if first_line else "Untitled response"
    return ProposedEntry(
        type=default_type,
        title=title,
        body=raw_text,
        refs=[],
        confidence=None,
    )
