"""Coordination variant seam (doc 03 §6).

A coordination paradigm owns scheduling, the agent I/O contract,
and termination.  It never owns the board store, transport, traces,
or UI shell.

This module defines:
  - CoordinationVariant  — the Protocol every variant must satisfy
  - SEAMS_CHECKLIST      — the 8 invariants enforced as a merge gate
  - A simple variant registry for runtime lookup
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# ── The Variant Protocol ─────────────────────────────────────────────

@runtime_checkable
class CoordinationVariant(Protocol):
    """A coordination paradigm.

    Owns scheduling, the agent I/O contract, and termination.
    Never owns the board store, transport, traces, or UI shell.

    Variant implementations:
      - traditional  (doc 05, Phase 3)
      - patchboard   (doc 11, Phase 6)
      - stigmergic   (doc 16, Phase 6)
    """

    name: str  # "traditional" | "patchboard" | "stigmergic"

    async def genesis(self, task: Any) -> None:
        """Initialize a task: triage, generate agents, write objective."""
        ...

    def build_turn_payload(
        self, task: Any, actor: str, board: Any
    ) -> dict:
        """Build the payload dispatched to a KS for this turn."""
        ...

    def parse_agent_response(
        self, task: Any, actor: str, raw: Any
    ) -> list:
        """Parse the agent's response into BoardMutations."""
        ...

    async def apply(
        self, task: Any, mutations: list
    ) -> list:
        """Apply mutations through the Gateway; return BoardEvents."""
        ...

    async def step(self, task: Any, board: Any) -> dict:
        """Run one round: guard checks → CU selection → activations.

        Returns StepResult: { activations, terminal, reason }
        """
        ...

    def is_terminal(self, board: Any) -> tuple[bool, str | None]:
        """Check if the board state is terminal.

        Returns (is_done, reason_or_none).
        """
        ...


# ── Seams Checklist (doc 03 §6) — enforced as merge gate ─────────────
#
# Every core PR must pass this checklist (doc 10 §1).  The checklist
# items are the invariants that guarantee PatchBoard and stigmergic
# variants can plug in without rewriting engine code.

SEAMS_CHECKLIST: list[str] = [
    (
        "1. Coordination lives behind CoordinationVariant — the daemon's "
        "task runner calls variant.step(); it never hardcodes a sequence, "
        "a role name, or 'control unit'."
    ),
    (
        "2. The event log is variant-agnostic — board_events stores "
        "{seq, actor, event_type, payload} with namespaced event types "
        "(entry_added, patch_committed, pheromone_decayed, …)."
    ),
    (
        "3. actor/author are opaque strings everywhere (board, traces, DB, UI) "
        "— never enums. Generated experts (expert.valuation), patchboard "
        "workers (worker.extractor-2), and roleless actors (universal-3) "
        "must all render."
    ),
    (
        "4. Write authorization is capability-based, not role-name-based "
        "— variants assign capabilities to actors however they like."
    ),
    (
        "5. Derived fields are computed in one pluggable hook "
        "(recompute_derived(task) after each commit). Traditional registers "
        "salience; stigmergic registers pressure + decay; patchboard "
        "registers its state hash."
    ),
    (
        "6. Dispatch supports both push and pull (participation_mode per node) "
        "— push now, pull (crons) for stigmergic later."
    ),
    (
        "7. Termination is a variant method (is_terminal), not a task-runner "
        "return."
    ),
    (
        "8. The UI is registry-driven: variant dropdown options come from the "
        "daemon's capabilities endpoint, and each variant registers its "
        "panels/graph adapters instead of being hard-wired into Mission Control."
    ),
]


def verify_seams_checklist() -> list[str]:
    """Return the seams checklist for merge-gate enforcement.

    Every core PR must pass this checklist (doc 10 §1).
    CI / review tooling can call this and assert no violations.
    """
    return list(SEAMS_CHECKLIST)


# ── Variant Registry ─────────────────────────────────────────────────
#
# Each variant module (traditional.py, patchboard.py, stigmergic.py)
# calls register_variant() at import time.  The daemon's task runner
# uses get_variant_class() to instantiate the active variant.

_VARIANTS: dict[str, type] = {}


def register_variant(name: str, cls: type) -> None:
    """Register a CoordinationVariant implementation by name.

    Raises TypeError if cls is not a class.
    Logs a warning if name was already registered (last write wins).
    """
    if not isinstance(cls, type):
        raise TypeError(f"register_variant expects a class, got {type(cls)}")
    if name in _VARIANTS:
        import logging
        logging.getLogger("bmas.variants").warning(
            "Variant '%s' is being re-registered (was %s, now %s). "
            "Last registration wins.",
            name, _VARIANTS[name].__name__, cls.__name__,
        )
    _VARIANTS[name] = cls


def get_variant_class(name: str) -> type | None:
    """Look up a registered variant class by name.  Returns None if unknown."""
    return _VARIANTS.get(name)


def available_variants() -> list[str]:
    """Return the names of all registered variants."""
    return list(_VARIANTS.keys())
