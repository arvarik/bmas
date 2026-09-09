"""The control policy of the Classic runtime.

The control policy produces the eligible agents of one round and ranks
them: it builds the control-unit prompt, reads the control-unit reply,
falls back to the deterministic routing table, explains a heuristic
selection, and normalizes a selection against the roster and the role
registry. The provider call stays outside the policy: the engine passes
one completion callable, and the policy shapes the request and reads
the reply.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from budget_service import BudgetError
from core.model_parameters import (
    completion_parameters,
    message_content,
    profile_for_alias,
    retry_budget,
    truncated,
)
from core.variants.classic.roster import CONSTANT_ROLE_DESCRIPTIONS

if TYPE_CHECKING:
    from core.entry import BoardEntry
    from core.variants.classic.roster import AgentRoster

logger = logging.getLogger("bmas.classic.control")

# One chat completion: the callable posts the request body and returns
# the response JSON. The engine owns the transport and the cost record.
CompletionCall = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ControlLimits:
    """The live limits the control policy reads for one decision.

    The engine builds one instance per call from its current settings,
    so a limit an operator or a test changes after construction reaches
    the policy at once.
    """

    max_rounds: int
    max_concurrent: int
    stall_rounds: int
    max_replans: int
    budget_ceiling: float
    require_evidence: bool
    cleaner_threshold: int


@dataclass(frozen=True)
class ControlProgress:
    """The dynamic control state one decision reads."""

    budget_spent: float
    stall_counter: int
    replan_count: int


class ControlPolicy:
    """Produce eligible agents and rank their expected value (doc 05 §1.1).

    Every method reads its inputs from the arguments, so the policy holds
    no state and no storage or provider handle.
    """

    def cu_prompt(
        self,
        query: str,
        board_text: str,
        roster_text: str,
        current_round: int,
        snapshot: dict[str, BoardEntry],
        meta: dict[str, Any],
        *,
        limits: ControlLimits,
        progress: ControlProgress,
    ) -> str:
        """Build the control-unit prompt with an explicit progress block.

        The CU sees how the task is trending — budget pressure, new entries
        last round, unresolved critiques, and the stall state — so it can
        prefer verification and convergence when returns diminish.
        """
        budget_spent = progress.budget_spent
        budget_remaining = max(0.0, limits.budget_ceiling - budget_spent)
        budget_pct = (
            min(100, int(100 * budget_spent / limits.budget_ceiling))
            if limits.budget_ceiling > 0 else 0
        )
        ledger_rows = list(meta.get("progress_ledger") or [])
        last = ledger_rows[-1] if ledger_rows else {}
        entries_last_round = int(last.get("entries_added", 0) or 0)
        open_critiques = sum(
            1 for entry in snapshot.values()
            if entry.status == "open" and entry.type == "critique"
        )
        open_conflicts = sum(
            1 for entry in snapshot.values()
            if entry.status == "open" and entry.type == "conflict"
        )
        evidence_last_round = sum(
            1 for entry in snapshot.values()
            if entry.status == "open"
            and entry.round == current_round - 1
            and getattr(entry, "sources", None)
        )
        pressure_line = ""
        if budget_pct >= 80:
            pressure_line = (
                "- BUDGET PRESSURE: over 80% of the budget is spent. "
                "Select agents that converge (critic, decider). "
                "Do not open new lines of work.\n"
            )
        evidence_line = ""
        if limits.require_evidence:
            evidence_line = (
                "- EVIDENCE REQUIRED: this effort level treats a round of "
                "unsourced findings as a stall. Prefer agents that can cite "
                "tool or web sources.\n"
            )
        return (
            f"## Objective\n{query}\n\n"
            f"## Current Board (round {current_round})\n{board_text}\n\n"
            f"## Available Agents\n{roster_text}\n\n"
            f"## Progress\n"
            f"- Budget: ${budget_spent:.4f} spent of "
            f"${limits.budget_ceiling:.2f} ({budget_pct}%)\n"
            f"- Last round added {entries_last_round} entries "
            f"({evidence_last_round} with external sources); "
            f"{open_critiques} unresolved critiques; "
            f"{open_conflicts} open conflicts\n"
            f"- Stall counter: {progress.stall_counter}/{limits.stall_rounds}; "
            f"replans used: {progress.replan_count}/{limits.max_replans}\n"
            f"- A round that only restates existing content counts as a stall.\n"
            f"{evidence_line}"
            f"{pressure_line}\n"
            f"## Constraints\n"
            f"- Round: {current_round}/{limits.max_rounds}\n"
            f"- Budget remaining: ${budget_remaining:.4f}\n"
            f"- Select 1-{limits.max_concurrent} agents\n"
        )

    async def select(
        self,
        query: str,
        snapshot: dict[str, BoardEntry],
        current_round: int,
        meta: dict[str, Any],
        *,
        roster: AgentRoster,
        board_text: str,
        model: str,
        limits: ControlLimits,
        progress: ControlProgress,
        complete: CompletionCall,
    ) -> tuple[list[str], str | None]:
        """One control-unit completion per round for agent selection.

        Returns (selected_actors, rationale). Rationale may be None if the
        CU response was garbled or missing it — this NEVER blocks the
        loop (doc 05 §1.2). A failed or empty reply after one retry
        falls back to the deterministic table.
        """
        from models.personas import CU_SYSTEM_PROMPT

        roster_text = "\n".join(
            f"- {actor}: {desc}"
            for actor, desc in roster.all_actors()
        )
        prompt = self.cu_prompt(
            query, board_text, roster_text, current_round, snapshot, meta,
            limits=limits, progress=progress,
        )
        system = CU_SYSTEM_PROMPT.format(max_concurrent=limits.max_concurrent)

        # The visible reply is a short JSON object; a reasoning model
        # spends completion tokens on reasoning first, so the budget
        # comes from the provider profile and grows once on truncation.
        parameters = completion_parameters(
            profile_for_alias(model), output_tokens=256, temperature=0.2,
            reasoning="low", json_object=True,
        )
        # Try up to 2 times (1 retry on garbled or truncated output)
        for attempt in range(2):
            try:
                response = await complete({
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    **parameters,
                })
                cut = truncated(response)
                if cut is not None:
                    logger.warning(
                        "CU reply truncated at %s tokens (%s reasoning); "
                        "retrying with a larger budget",
                        cut["completion_tokens"], cut["reasoning_tokens"],
                    )
                    parameters = retry_budget(parameters)
                    continue
                raw = message_content(response)
                selected, rationale = parse_cu_output(raw, roster.actor_names())
                if selected:
                    return selected, rationale
                logger.warning("CU returned empty selection (attempt %d)", attempt + 1)
            except BudgetError:
                raise
            except Exception as e:
                logger.warning("CU call failed (attempt %d): %s", attempt + 1, e)

        # Fallback to deterministic table
        logger.info("CU failed after retries, using deterministic fallback")
        return self.deterministic_fallback(
            snapshot, current_round, roster, cleaner_threshold=limits.cleaner_threshold,
        ), None

    def fallback_rationale(
        self,
        snapshot: dict[str, BoardEntry],
        current_round: int,
        selected: list[str],
    ) -> str:
        """Synthesize a human-readable routing rationale for the graph.

        Used when the CU did not return a usable rationale (deterministic
        fallback or garbled LLM output). Mirrors the decision rules in
        ``deterministic_fallback`` so the Graph tab can always explain WHY
        a handoff happened, even on replayed/completed tasks (doc 05 §1.2).
        """
        names = ", ".join(selected) if selected else "no agents"
        if current_round <= 1:
            return (
                f"Discovery round: seeded the board by activating the planner "
                f"and all domain experts ({names})."
            )

        open_entries = [e for e in snapshot.values() if e.status == "open"]
        addressed_refs = {ref for e in open_entries if e.type != "critique" for ref in e.refs}
        has_unaddressed_critique = any(
            e.type == "critique" and e.id not in addressed_refs
            for e in open_entries
        )
        has_conflict = any(e.type == "conflict" for e in open_entries)

        if "conflict_resolver" in selected and has_conflict:
            return (
                "Open conflict detected between board entries — routed to the "
                "conflict_resolver to mediate."
            )
        if "cleaner" in selected:
            return (
                f"Board grew past the cleaner threshold "
                f"({len(open_entries)} open entries) — routed to the cleaner to prune."
            )
        if "decider" in selected and len(selected) == 1:
            return (
                "No open critiques or conflicts remain — routed to the decider "
                "to judge sufficiency and post a solution."
            )
        if has_unaddressed_critique:
            return (
                f"Unaddressed critiques on the board — routed back to the critiqued "
                f"authors ({names}) to rebut or revise."
            )
        return f"Heuristic routing for round {current_round}: activated {names}."

    def deterministic_fallback(
        self,
        snapshot: dict[str, BoardEntry],
        current_round: int,
        roster: AgentRoster | None,
        *,
        cleaner_threshold: int,
    ) -> list[str]:
        """Deterministic fallback policy (doc 05 §1.1).

        Round 1 → planner + all experts
        Open critiques without rebuttals → critiqued authors
        Open conflicts → conflict_resolver
        Entry count > cleaner_threshold → cleaner
        Otherwise → decider
        """
        if not roster:
            return ["planner"]

        # Round 1: planner + all experts
        if current_round <= 1:
            selected = ["planner"]
            for expert in roster.experts:
                selected.append(f"expert.{expert.slug}")
            return selected

        # Open critiques without rebuttals → critiqued authors
        open_entries = {
            eid: e for eid, e in snapshot.items()
            if e.status == "open"
        }
        critiques = [
            e for e in open_entries.values() if e.type == "critique"
        ]
        addressed_refs = set()
        for e in open_entries.values():
            if e.type != "critique":
                addressed_refs.update(e.refs)

        unaddressed_critiques = [
            c for c in critiques if c.id not in addressed_refs
        ]
        if unaddressed_critiques:
            # Find the authors of the critiqued entries
            critiqued_authors = set()
            for c in unaddressed_critiques:
                for ref_id in c.refs:
                    ref_entry = snapshot.get(ref_id)
                    if ref_entry:
                        critiqued_authors.add(ref_entry.author)
            if critiqued_authors:
                # A sorted list keeps the selection order independent of
                # the set iteration order and the hash seed.
                return sorted(critiqued_authors)

        # Open conflicts → conflict_resolver
        conflicts = [
            e for e in open_entries.values() if e.type == "conflict"
        ]
        if conflicts:
            return ["conflict_resolver"]

        # Entry count > threshold → cleaner
        if len(open_entries) > cleaner_threshold:
            return ["cleaner"]

        # Default → decider
        return ["decider"]

    @staticmethod
    def normalize_selection(
        selected: list[str],
        roster: AgentRoster | None,
        role_registry: dict[str, dict[str, Any]],
    ) -> list[str]:
        """Remove unknown, disabled, and duplicate actor selections."""
        valid_names = (
            set(roster.actor_names())
            if roster
            else set(CONSTANT_ROLE_DESCRIPTIONS)
        )
        normalized: list[str] = []
        seen: set[str] = set()
        for actor in selected:
            if actor in seen or actor not in valid_names:
                continue
            base_role = actor.split(".", 1)[0]
            if role_registry.get(base_role, {}).get("enabled") is False:
                continue
            seen.add(actor)
            normalized.append(actor)
        return normalized


# ── CU Output Parser (doc 05 §1.1) ──────────────────────────────────

def parse_cu_output(
    raw: str, valid_names: list[str],
) -> tuple[list[str], str | None]:
    """Parse CU selection JSON.  Returns (valid_actor_names, rationale).

    Drops unknown names with warning.  Returns ([], None) on garbled output.
    A malformed or missing rationale is returned as None — it NEVER raises
    or blocks the loop (doc 05 §1.2).
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Try to extract JSON from markdown code blocks
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
            except (json.JSONDecodeError, TypeError):
                return [], None
        else:
            return [], None

    if not isinstance(data, dict):
        return [], None

    selected = data.get("selected", [])
    if not isinstance(selected, list):
        return [], None

    # Extract rationale — must be a non-empty string, else None.
    # A malformed rationale never blocks the loop.
    raw_rationale = data.get("rationale")
    rationale: str | None = (
        str(raw_rationale).strip() or None
    ) if isinstance(raw_rationale, str) else None

    # Filter to valid names
    result = []
    seen: set[str] = set()
    valid_set = set(valid_names)
    for name in selected:
        if not isinstance(name, str):
            continue
        if name in valid_set and name not in seen:
            result.append(name)
            seen.add(name)
        else:
            logger.warning("CU selected unknown agent '%s' — dropping", name)

    return result, rationale
