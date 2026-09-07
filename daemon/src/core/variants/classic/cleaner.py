"""The cleaner policy of the Classic runtime.

The cleaner policy selects the board compaction candidates and keeps
the entries they depend on: it scores every unprotected open entry by
its retention value, estimates the board pressure, and builds the
condense view the cleaner reads. Every function reads the board and the
weights from its arguments and touches no storage.
"""
from __future__ import annotations

from typing import Any

from core.entry import BoardEntry, entry_to_dict

# The rounds a plan or a critique stays protected from condensation.
CLEANER_RECENT_ROUNDS = 2
# The entry types the cleaner never condenses.
ALWAYS_PROTECTED_TYPES = frozenset({"objective", "directive", "ledger", "conflict", "solution"})
RECENTLY_PROTECTED_TYPES = frozenset({"plan", "critique"})
# The entry types every condense view carries in full.
CONDENSE_CONTEXT_TYPES = frozenset({"objective", "directive", "ledger"})
DEFAULT_RETENTION_WEIGHTS = {
    "salience": 2.0, "confidence": 1.0, "recency": 0.1, "size_penalty": 0.01,
}


def _field(entry: Any, name: str, default: Any = None) -> Any:
    """Read one field from a board entry or its dictionary form."""
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


class CleanerPolicy:
    """Select board compaction candidates and preserve dependencies."""

    @staticmethod
    def board_tokens(open_entries: list[Any]) -> int:
        """The token estimate of the open board, four characters per token."""
        return sum(len(str(_field(entry, "body", "") or "")) // 4 for entry in open_entries)

    @staticmethod
    def pressure_rationale(total_tokens: int, token_threshold: int) -> str:
        """The rationale of a forced cleaner activation."""
        return (
            f"Board exceeded token threshold ({total_tokens} > {token_threshold}) "
            "— forced cleaner invocation."
        )

    @staticmethod
    def eviction_candidates(
        snapshot: dict[str, BoardEntry] | dict[str, Any],
        *,
        retention_weights: dict[str, float],
        max_candidates: int = 12,
    ) -> list[BoardEntry]:
        """Calculate Retention Value and return the bottom N eviction candidates."""
        open_entries = []
        for e in snapshot.values():
            if _field(e, "status", "") == "open":
                open_entries.append(e)

        protected_ids = set()

        # 1. Protect critical entries. Structural types stay protected
        # always; plans and critiques stay protected only while recent, so
        # a long run can condense its own history instead of hoarding it.
        latest_round = max(
            (int(_field(e, "round", 0) or 0) for e in open_entries),
            default=0,
        )
        recent_floor = latest_round - CLEANER_RECENT_ROUNDS
        for e in open_entries:
            etype = _field(e, "type", "")
            round_no = int(_field(e, "round", 0) or 0)
            always = etype in ALWAYS_PROTECTED_TYPES
            recent = etype in RECENTLY_PROTECTED_TYPES and round_no >= recent_floor
            if always or recent:
                protected_ids.add(_field(e, "id"))
                refs = _field(e, "refs", [])
                if refs:
                    for ref in refs:
                        protected_ids.add(ref)

        candidates = []
        for e in open_entries:
            if _field(e, "id") in protected_ids:
                continue

            w_sal = retention_weights.get("salience", 2.0)
            w_conf = retention_weights.get("confidence", 1.0)
            w_rec = retention_weights.get("recency", 0.1)
            w_size = retention_weights.get("size_penalty", 0.01)

            body = _field(e, "body", "")
            salience = _field(e, "salience", 0.0)
            confidence = _field(e, "confidence", 0.5)
            round_raw = _field(e, "round", 0)

            body_str = str(body) if body is not None else ""
            sal_val = float(salience) if salience is not None else 0.0
            conf_val = float(confidence) if confidence is not None else 0.5
            round_val = int(round_raw) if round_raw is not None else 0

            # RV = (Salience * W_sal) + (Confidence * W_conf) + (Round * W_rec) - (Tokens * W_size)
            tokens = len(body_str) // 4
            rv = (sal_val * w_sal) + (conf_val * w_conf) + (round_val * w_rec) - (tokens * w_size)

            candidates.append((rv, e))

        # Sort by RV ascending
        candidates.sort(key=lambda x: x[0])
        return [c[1] for c in candidates[:max_candidates]]

    @staticmethod
    def condense_view(board: dict[str, Any], candidates: list[BoardEntry]) -> dict[str, Any]:
        """The board view of a cleaner turn: the protected context and the candidates."""
        protected_context = [
            entry
            for entry in board.values()
            if getattr(entry, "type", None) in CONDENSE_CONTEXT_TYPES
        ]
        subset = [*protected_context, *candidates]
        return {"mode": "condense", "entries": [entry_to_dict(e) for e in subset]}
