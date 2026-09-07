"""The roster policy of the Classic runtime.

The roster policy creates the roles, the generated experts, and the
model assignment of one task. The engine delegates the expert
generation, the fallback roster, the model resolution, and the roster
metadata to this module. The provider call stays outside the policy: the
engine passes one completion callable, and the policy shapes the request
and reads the response.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from core.model_parameters import (
    completion_parameters,
    message_content,
    profile_for_alias,
)

logger = logging.getLogger("bmas.classic.roster")


@dataclass
class ExpertIdentity:
    """An AG-generated expert."""
    name: str               # display name (e.g. "Valuation Analyst")
    slug: str               # actor id suffix (e.g. "valuation_analyst")
    ability: str            # one-line ability description D_i
    model: str              # pool-drawn model for this expert


@dataclass
class AgentRoster:
    """The complete agent group for a task."""
    constants: dict[str, str]    # role → ability description
    experts: list[ExpertIdentity]

    def all_actors(self) -> list[tuple[str, str]]:
        """Return [(actor_id, ability_description)] for all agents."""
        result = [(role, desc) for role, desc in self.constants.items()]
        for expert in self.experts:
            result.append((f"expert.{expert.slug}", expert.ability))
        return result

    def actor_names(self) -> list[str]:
        """Return all actor names."""
        return [a[0] for a in self.all_actors()]


# ── Constant Role Descriptions (for CU roster) ──────────────────────

CONSTANT_ROLE_DESCRIPTIONS: dict[str, str] = {
    "planner": "Decomposes the objective into actionable sub-goals and plans.",
    "critic": "Identifies errors, hallucinations, and weak reasoning in findings.",
    "conflict_resolver": "Detects contradictions between entries and mediates resolution.",
    "cleaner": "Removes redundant or obsolete entries to keep the board focused.",
    "decider": "Judges whether the board is sufficient and posts the final solution.",
}

# ── Fallback experts ─────────────────────────────────────────────────
#
# The deterministic roster the engine uses when the agent generator
# fails. The list holds the largest expert count the settings accept
# (twelve per tier), in a fixed order.

FALLBACK_EXPERTS: tuple[dict[str, str], ...] = (
    {"name": "Domain Analyst", "slug": "domain_analyst",
     "ability": "Deep analysis of the core domain question"},
    {"name": "Systems Thinker", "slug": "systems_thinker",
     "ability": "Identifies systemic factors and second-order effects"},
    {"name": "Evidence Reviewer", "slug": "evidence_reviewer",
     "ability": "Verifies claims against available evidence and data"},
    {"name": "Root Cause Analyst", "slug": "root_cause_analyst",
     "ability": "Traces failure chains to their underlying structural causes"},
    {"name": "Constraint Mapper", "slug": "constraint_mapper",
     "ability": "Lists the hard constraints and checks each candidate against them"},
    {"name": "Counterexample Hunter", "slug": "counterexample_hunter",
     "ability": "Searches for cases that break a proposed answer"},
    {"name": "Quantitative Modeler", "slug": "quantitative_modeler",
     "ability": "Builds the numeric model behind an estimate and states its assumptions"},
    {"name": "Historical Precedent Analyst", "slug": "historical_precedent_analyst",
     "ability": "Finds prior cases and reports what happened and why"},
    {"name": "Stakeholder Analyst", "slug": "stakeholder_analyst",
     "ability": "Identifies who is affected and what each party needs"},
    {"name": "Risk Assessor", "slug": "risk_assessor",
     "ability": "Ranks the failure modes by likelihood and impact"},
    {"name": "Implementation Planner", "slug": "implementation_planner",
     "ability": "Turns a conclusion into ordered steps with owners and checks"},
    {"name": "Synthesis Editor", "slug": "synthesis_editor",
     "ability": "Merges the findings into one consistent account and flags gaps"},
)

# One chat completion: the callable posts the request body and returns
# the response JSON. The engine owns the transport and the cost record.
CompletionCall = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
# One fallback notice: (model, error, fallback experts).
FallbackNotice = Callable[[str, str, list[dict[str, Any]]], Awaitable[None]]


@dataclass
class RosterPolicy:
    """Create roles, experts, and model assignments (doc 05 §2.1)."""

    model_routing: dict[str, str]
    model_pools: dict[str, list[str]] = field(default_factory=dict)
    # Edge inference round-robin state. When a model alias resolves to
    # "local", the policy cycles through the edge models so consecutive
    # calls hit different inference nodes. The engine checkpoints the
    # counter, so the distribution stays even across a restart.
    edge_models: list[str] = field(default_factory=lambda: ["edge-node-1"])
    edge_rotation: int = 0

    # ── Model resolution ──────────────────────────────────────────────

    def resolve_model(self, model: str) -> str:
        """Resolve a model alias, distributing 'local' across edge nodes."""
        if model == "local":
            return self.resolve_edge_model()
        return model

    def resolve_edge_model(self) -> str:
        """Round-robin across edge inference node model aliases."""
        if not self.edge_models:
            return "edge-node-1"  # safety fallback
        model = self.edge_models[self.edge_rotation % len(self.edge_models)]
        self.edge_rotation += 1
        return model

    # ── Experts ───────────────────────────────────────────────────────

    def default_experts(self, n: int) -> list[dict[str, str]]:
        """The deterministic fallback roster for ``n`` experts."""
        return [dict(expert) for expert in FALLBACK_EXPERTS[:max(0, n)]]

    def expert_request(self, query: str, n: int, tier: str) -> tuple[str, dict[str, Any]]:
        """The model alias and the request body of one AG call."""
        from models.personas import AG_SYSTEM_PROMPT

        ag_model = self.resolve_model(self.model_routing.get(tier, "medium"))
        body = {
            "model": ag_model,
            "messages": [
                {"role": "system", "content": AG_SYSTEM_PROMPT.format(n=n)},
                {"role": "user", "content": f"Task: {query}"},
            ],
            # 4 experts x ~200 tokens each plus the JSON wrapper; the
            # provider profile adds the reasoning headroom a thinking
            # model needs before it writes the visible JSON.
            **completion_parameters(
                profile_for_alias(ag_model), output_tokens=1024,
                temperature=0.4, reasoning="low", json_object=True,
            ),
        }
        return ag_model, body

    @staticmethod
    def parse_expert_response(response: dict[str, Any], n: int) -> list[dict[str, Any]]:
        """Read the generated experts, or raise on a truncated or empty reply."""
        choice = response["choices"][0]
        finish_reason = choice.get("finish_reason", "stop")
        raw_content = message_content(response)

        # Guard against truncated JSON: if the model hit the token limit
        # the JSON will be incomplete and json.loads will raise.  Detect
        # this early so the caller can log a meaningful reason.
        if finish_reason == "length":
            usage = response.get("usage", {})
            raise ValueError(
                f"AG response truncated (finish_reason=length): "
                f"completion_tokens={usage.get('completion_tokens')}, "
                f"reasoning_tokens={usage.get('completion_tokens_details', {}).get('reasoning_tokens')}. "
                f"Increase max_tokens or switch to a non-thinking model for the AG call."
            )

        data = json.loads(raw_content)
        raw_experts = data.get("experts", [])[:n]
        if not raw_experts:
            raise ValueError(
                f"AG returned empty experts list. "
                f"Raw content preview: {raw_content[:200]!r}"
            )
        return list(raw_experts)

    def assign_models(self, raw_experts: list[dict[str, Any]], tier: str) -> list[ExpertIdentity]:
        """Assign pool models with diversity and build the identities."""
        experts = []
        pool = self.model_pools.get(tier) or [self.model_routing.get(tier, "medium")]
        for i, ex in enumerate(raw_experts):
            model = pool[i % len(pool)] if pool else self.model_routing.get(tier, "medium")
            slug = str(ex.get("slug", f"expert_{i}")).replace(" ", "_").lower()
            # Sanitize slug: only alphanumeric and underscores
            slug = "".join(c for c in slug if c.isalnum() or c == "_")
            experts.append(ExpertIdentity(
                name=str(ex.get("name", f"Expert {i+1}")),
                slug=slug,
                ability=str(ex.get("ability", "Domain expert")),
                model=model,
            ))
        return experts

    async def generate_experts(
        self,
        query: str,
        n: int,
        tier: str,
        *,
        complete: CompletionCall,
        on_fallback: FallbackNotice | None = None,
    ) -> list[ExpertIdentity]:
        """AG: one completion call to generate ``n`` expert identities.

        ``complete`` performs the provider call and returns the response
        JSON. A failed or unusable reply falls back to the deterministic
        roster and reports the reason through ``on_fallback``.
        """
        if n <= 0:
            return []
        ag_model, body = self.expert_request(query, n, tier)
        try:
            response = await complete(body)
            raw_experts = self.parse_expert_response(response, n)
        except Exception as e:
            logger.warning("AG call failed (%s), using default experts", e)
            raw_experts = self.default_experts(n)
            if on_fallback is not None:
                await on_fallback(ag_model, str(e), raw_experts)
        return self.assign_models(raw_experts, tier)

    # ── Roster ────────────────────────────────────────────────────────

    @staticmethod
    def build_roster(experts: list[ExpertIdentity]) -> AgentRoster:
        """The roster of the constant roles plus the generated experts."""
        return AgentRoster(
            constants=dict(CONSTANT_ROLE_DESCRIPTIONS),
            experts=list(experts),
        )

    def roster_from_metadata(self, roster_data: Any) -> AgentRoster:
        """Read the roster the checkpoint saved, in either stored shape."""
        if isinstance(roster_data, str):
            try:
                roster_data = json.loads(roster_data)
            except (json.JSONDecodeError, TypeError):
                roster_data = {}

        constants = dict(CONSTANT_ROLE_DESCRIPTIONS)
        experts: list[ExpertIdentity] = []
        if isinstance(roster_data, dict):
            raw_constants = roster_data.get("constants")
            if isinstance(raw_constants, dict):
                constants = {
                    str(role): str(description)
                    for role, description in raw_constants.items()
                }
            for raw in roster_data.get("experts", []):
                if not isinstance(raw, dict):
                    continue
                experts.append(ExpertIdentity(
                    name=str(raw.get("name", "Expert")),
                    slug=str(raw.get("slug", "expert")),
                    ability=str(raw.get("ability", "Domain expert")),
                    model=str(raw.get("model", self.model_routing.get("medium", "medium"))),
                ))
        elif isinstance(roster_data, list):
            # Read legacy metadata written before the durable roster format.
            for raw in roster_data:
                if not isinstance(raw, dict):
                    continue
                actor = str(raw.get("actor", ""))
                if actor.startswith("expert."):
                    slug = actor.split(".", 1)[1]
                    experts.append(ExpertIdentity(
                        name=slug.replace("_", " ").title(),
                        slug=slug,
                        ability=str(raw.get("ability", "Domain expert")),
                        model=self.model_routing.get("medium", "medium"),
                    ))
        return AgentRoster(constants=constants, experts=experts)

    @staticmethod
    def roster_to_metadata(roster: AgentRoster | None) -> dict[str, Any]:
        """The durable shape of one roster."""
        current = roster or AgentRoster(constants=dict(CONSTANT_ROLE_DESCRIPTIONS), experts=[])
        return {
            "constants": current.constants,
            "experts": [
                {
                    "name": expert.name,
                    "slug": expert.slug,
                    "ability": expert.ability,
                    "model": expert.model,
                }
                for expert in current.experts
            ],
        }
