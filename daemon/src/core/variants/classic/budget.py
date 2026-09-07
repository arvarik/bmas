"""The budget policy of the Classic runtime.

The budget policy reserves and commits the cost of one task: it keeps
the running spend against the ceiling, splits the remaining budget
across concurrent activations, prices one control-plane completion from
its usage, names each control-plane call, and shapes the budget event.
The durable cost record and the event delivery stay in the engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CostRecord:
    """The priced usage of one control-plane completion."""

    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    price_source: str


@dataclass
class BudgetPolicy:
    """Reserve and commit cost, token, and time use (doc 05 §5)."""

    ceiling: float
    spent: float = 0.0
    # One sequence per task for the synthetic turn ids of control-plane
    # calls, so the cost summary groups control spend by round.
    control_call_sequence: dict[str, int] = field(default_factory=dict)

    def track_cost(self, cost_usd: float) -> None:
        """Update the running budget total."""
        self.spent += cost_usd

    def remaining(self) -> float:
        """The budget still available for new work."""
        return max(0.0, self.ceiling - self.spent)

    def reserve_activation_budgets(self, count: int) -> list[float]:
        """Split the available task budget across concurrent activations.

        Each activation receives an exclusive share instead of seeing the full
        remaining budget. The daemon reconciles actual usage after completion.
        """
        if count <= 0:
            return []
        share = self.remaining() / count
        return [share for _ in range(count)]

    def control_turn_id(self, task_id: str | None, round_no: int | None) -> str | None:
        """One synthetic turn id per control-plane call, keyed by round.

        Actor turns carry their round through the turns table; a
        control-plane call has no turn row, so its id names the round
        and a per-task sequence number, and the cost summary groups
        both kinds of spend by round.
        """
        if task_id is None or round_no is None:
            return None
        self.control_call_sequence[task_id] = self.control_call_sequence.get(task_id, 0) + 1
        return f"control-r{int(round_no)}-{self.control_call_sequence[task_id]}"

    @staticmethod
    def price_usage(
        usage: dict[str, Any] | None,
        model: str,
        model_pricing: dict[str, dict[str, Any]],
    ) -> CostRecord | None:
        """Price the usage of one completion, or None when nothing was used.

        The gateway may report the resolved alias on the response; the
        policy prefers it and falls back to the requested alias. A model
        with no price yields zero cost and the source ``missing``.
        """
        if not usage or not isinstance(usage, dict):
            return None
        resolved_model = usage.get("model") or model
        pricing = (
            model_pricing.get(resolved_model)
            or model_pricing.get(model)
            or {}
        )
        price_model = (
            resolved_model if resolved_model in model_pricing else model
        )

        in_tok = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        out_tok = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
        if in_tok == 0 and out_tok == 0:
            return None

        cost = 0.0
        if pricing:
            cost = round(
                in_tok * float(pricing.get("input_cost_per_token", 0))
                + out_tok * float(pricing.get("output_cost_per_token", 0)),
                8,
            )
        return CostRecord(
            model=price_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            price_source=str(pricing.get("source", "bmas.yaml")) if pricing else "missing",
        )

    def budget_event(self) -> dict[str, Any]:
        """The budget event payload: spend against the ceiling."""
        return {
            "spent": round(self.spent, 6),
            "ceiling": self.ceiling,
            "percentage": round(
                (self.spent / self.ceiling * 100)
                if self.ceiling > 0 else 0.0,
                1,
            ),
        }
