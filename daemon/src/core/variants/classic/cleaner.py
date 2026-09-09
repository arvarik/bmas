"""The cleaner policy of the Classic runtime.

The cleaner policy selects the board compaction candidates and keeps
the entries they depend on: it scores every unprotected open entry by
its retention value, estimates the board pressure, and builds the
condense view the cleaner reads. Every function reads the board and the
weights from its arguments and touches no storage.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from core.capabilities import authorize_post, authorize_remove
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


class CondensationError(ValueError):
    """The complete cleaner proposal fails a board invariant."""


@dataclass(frozen=True)
class CondensationPlan:
    """One summary and the exact requested removal collection."""

    summary: dict[str, Any]
    removals: tuple[dict[str, str], ...]

    @classmethod
    def from_proposal(cls, proposal: dict[str, Any]) -> CondensationPlan:
        """Read a schema-validated proposal without discarding any member."""
        entries = proposal.get("entries")
        removals = proposal.get("removals")
        if proposal.get("action") != "condense" or proposal.get("role") != "cleaner":
            raise CondensationError("The cleaner requires a condense proposal")
        if not isinstance(entries, list) or len(entries) != 1:
            raise CondensationError("Condensation requires exactly one summary")
        if not isinstance(removals, list) or not removals:
            raise CondensationError("Condensation requires explicit removals")
        if any(not isinstance(item, dict) or set(item) != {"entry_id", "reason"}
               or not all(isinstance(value, str) and value.strip() for value in item.values())
               for item in removals):
            raise CondensationError("Every removal requires an entry identifier and a reason")
        return cls(copy.deepcopy(entries[0]), tuple(copy.deepcopy(removals)))

    def validate(self, snapshot: dict[str, BoardEntry], *, capabilities: list[str],
                 max_body: int, max_title: int, max_entries: int, max_tokens: int,
                 recent_rounds: int, claim_ids: set[str], evidence_ids: set[str],
                 tombstone_ids: set[str] | None = None, space: str = "public") -> None:
        """Validate the complete proposed projection before any write."""
        summary = self.summary
        if summary.get("type") != "condensed_finding":
            raise CondensationError("The summary type must be condensed_finding")
        authorize_post(capabilities, "condensed_finding")
        body = summary.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > max_body:
            raise CondensationError("The summary body is empty or exceeds the board limit")
        title = summary.get("title", "")
        if not isinstance(title, str) or len(title) > max_title:
            raise CondensationError("The summary title exceeds the board limit")
        refs, sources = summary.get("refs"), summary.get("sources")
        if any(not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values)
               or len(set(values)) != len(values) for values in (refs, sources)):
            raise CondensationError("Summary references and sources must be explicit unique string lists")
        assert isinstance(refs, list) and isinstance(sources, list)
        removal_ids = {item["entry_id"] for item in self.removals}
        if len(removal_ids) != len(self.removals):
            raise CondensationError("A removal target occurs more than once")
        active = {key: entry for key, entry in snapshot.items() if entry.status == "open"}
        latest = max((entry.round for entry in active.values()), default=0)
        required_refs: set[str] = set(removal_ids)
        required_sources: set[str] = set()
        provenance: set[str] = set()
        for entry_id in removal_ids:
            entry = snapshot.get(entry_id)
            if entry is None:
                raise CondensationError(f"Unknown removal target: {entry_id}")
            if (entry.status != "open" or entry.space != space or entry.type in ALWAYS_PROTECTED_TYPES
                    or (recent_rounds > 0 and entry.type in RECENTLY_PROTECTED_TYPES
                        and entry.round >= latest - recent_rounds)):
                raise CondensationError(f"Protected removal target: {entry_id}")
            authorize_remove(capabilities, entry.type)
            required_refs.update(entry.refs)
            required_sources.update(entry.sources)
            if entry.type == "condensed_finding":
                provenance.update(set(entry.refs).intersection(tombstone_ids or set()))
        if not required_refs.issubset(refs):
            raise CondensationError("The summary drops a required claim or entry link")
        if not required_sources.issubset(sources):
            raise CondensationError("The summary drops a required evidence link")
        for ref in refs:
            if ref not in snapshot and ref not in claim_ids:
                raise CondensationError(f"Unknown claim or entry link: {ref}")
            if ref in snapshot and ref not in removal_ids | provenance and snapshot[ref].status != "open":
                raise CondensationError(f"Inactive summary dependency: {ref}")
        # Existing source citations preserve their original meaning. New evidence
        # references must resolve to a durable decision in this run.
        if any(source not in required_sources and source not in evidence_ids for source in sources):
            raise CondensationError("The summary adds an unresolvable evidence link")
        for entry_id, entry in active.items():
            if entry_id not in removal_ids and removal_ids.intersection(entry.refs):
                raise CondensationError(f"Retained entry loses a required dependency: {entry_id}")
        retained = [entry for key, entry in active.items() if key not in removal_ids]
        if len(retained) + 1 > max_entries:
            raise CondensationError("The complete condensation exceeds the board entry limit")
        tokens = sum((len(entry.body) + 3) // 4 for entry in retained) + (len(body) + 3) // 4
        if tokens > max_tokens:
            raise CondensationError("The complete condensation exceeds the board token limit")


def require_cleaner_dispatch(request: dict[str, Any]) -> None:
    """Reject cleaner requests at the legacy dispatch boundary."""
    if request.get("role") == "cleaner" or (request.get("context") or {}).get("classic_proposal_role") == "cleaner":
        from budget_service import BudgetError

        raise BudgetError("Provider-backed cleaner dispatch requires the strict reservation contract")
