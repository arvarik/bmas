"""The prompt policy of the Classic runtime.

The prompt policy renders the activation payload of one turn from the
registered persona template or the generated expert definition, the
board view the activation receives, the session fields, and the status
lines of the run. It reads every input from its arguments and touches
no storage or provider.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.variants.classic.roster import AgentRoster

RESPONSE_CONTRACT = "entries_v1"
EVIDENCE_STATUS = (
    "Evidence required: ground every new finding in an external "
    "source. List the URLs or tool citations in the entry's "
    "\"sources\" array. A round of unsourced restatement counts "
    "as a stall."
)
BUDGET_STATUS = (
    "Over 80% of the task budget is spent. Converge now: "
    "verify or finalize existing work instead of opening new work."
)
BUDGET_PRESSURE_SHARE = 0.8


class PromptPolicy:
    """Render the activation prompt from template, definition, and view."""

    @staticmethod
    def role_prompt(actor: str, roster: AgentRoster | None, query: str) -> str:
        """The persona text of one actor: a registered template or a generated expert."""
        from models.personas import ROLE_PERSONAS, generate_expert_persona

        base_role = actor.split(".")[0] if "." in actor else actor
        if actor.startswith("expert.") and roster:
            slug = actor.split(".", 1)[1]
            expert = next(
                (e for e in roster.experts if e.slug == slug), None
            )
            if expert:
                return generate_expert_persona(
                    expert.name, expert.ability, query,
                )
            return ROLE_PERSONAS.get(base_role, "")
        return ROLE_PERSONAS.get(actor, "")

    @staticmethod
    def render(
        *,
        task_id: str,
        query: str,
        actor: str,
        role_prompt: str,
        board_data: dict[str, Any],
        round_no: int,
        session: dict[str, Any],
        budget_remaining_usd: float,
        budget_ceiling: float,
        budget_spent: float,
        require_evidence: bool,
    ) -> dict[str, Any]:
        """Build the payload dispatched to a KS for this turn (doc 03 §4)."""
        base_role = actor.split(".")[0] if "." in actor else actor
        payload = {
            "task_id": task_id,
            "turn_id": f"turn-{uuid.uuid4().hex[:8]}",
            "round": round_no,
            "role": actor,
            "role_prompt": role_prompt,
            "objective": query,
            "board": board_data,
            "response_contract": RESPONSE_CONTRACT,
            "budget_remaining_usd": budget_remaining_usd,
            # Phase 5: stateful turns (doc 12 §5.2).
            **session,
        }
        if require_evidence and base_role in ("expert", "planner"):
            payload["evidence_status"] = EVIDENCE_STATUS
        if (
            budget_ceiling > 0
            and budget_spent / budget_ceiling >= BUDGET_PRESSURE_SHARE
        ):
            payload["budget_status"] = BUDGET_STATUS
        return payload
