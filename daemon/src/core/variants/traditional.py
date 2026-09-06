# /opt/bmas/daemon/src/core/variants/traditional.py
"""Traditional LbMAS variant — the paper's blackboard cycle (doc 05).

Implements CoordinationVariant:
  genesis  → triage → AG experts → objective entry → attach uploads
  step     → deterministic guards → CU LLM selection → activations
  finalize → Decider solution / SolE majority-similarity vote

The CU and AG are control-plane LiteLLM calls, NEVER Hermes runs (doc 05 §7).

Cost rails (doc 05 §5) are integral — budget ceiling, round/duration caps,
concurrency cap, stall breaker, decline gating — all deterministic, all
shipped in this module alongside the loop.

Registered behind `coordination.variant: traditional` (default since Phase 5 cutover).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.capabilities import capabilities_for_role
from core.entry import BoardEntry, entry_to_dict
from core.model_parameters import (
    completion_parameters,
    message_content,
    profile_for_alias,
    retry_budget,
    truncated,
)
from core.response_parser import parse_entries

logger = logging.getLogger("bmas.traditional")


# ── Data Models ──────────────────────────────────────────────────────

@dataclass
class StepResult:
    """Result of one round of the blackboard cycle."""
    terminal: bool
    reason: str | None = None
    activations: list[Activation] = field(default_factory=list)
    # Coordinator (CU) routing decision metadata for this round (doc 05 §1.2).
    # Surfaced to the orchestrator so it can both log WHO was selected and WHY,
    # and persist that rationale/phase on each turn record — which powers the
    # execution-graph handoff/decision visualization on the Graph tab.
    selected: list[str] = field(default_factory=list)
    rationale: str | None = None
    selection_source: str = "heuristic"
    phase: str | None = None


@dataclass
class Activation:
    """A single agent activation for this round."""
    actor: str              # opaque actor id (e.g. "critic", "expert.valuation")
    role: str               # base role for capability lookup
    model: str              # pool-drawn model for this turn
    node_endpoint: str      # target node URL
    profile: str | None = None
    activation_id: str | None = None


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


# ── TraditionalVariant ───────────────────────────────────────────────

class TraditionalVariant:
    """The paper's LbMAS blackboard cycle (doc 05).

    Lifecycle:
      1. genesis()  — called once at task start
      2. step()     — called each round until terminal
      3. finalize() — called after the loop exits
    """

    name = "classic"

    def __init__(
        self,
        gateway: Any,           # BoardGateway
        board_store: Any,       # BoardStore
        event_emitter: Any,     # EventEmitter
        triage: Any,            # TriageRouter
        config: dict[str, Any],
        litellm_url: str,
        litellm_key: str,
        node_endpoints: list[str],
        role_registry: dict[str, dict],
        model_routing: dict[str, str],
        model_pools: dict[str, list[str]] | None = None,
        edge_node_models: list[str] | None = None,
        model_pricing: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.gateway = gateway
        self.store = board_store
        self.emitter = event_emitter
        self.triage = triage

        # Config (doc 05 §3)
        self.max_rounds: int = int(config.get("max_rounds", 4))
        self.max_duration_s: int = int(config.get("max_duration_s", 1800))
        self.budget_ceiling: float = float(config.get("budget_ceiling_usd", 0.50))
        self.max_concurrent: int = int(config.get("max_concurrent_activations", 3))
        self.experts_per_tier: dict[str, int] = config.get(
            "experts_per_tier", {"simple": 0, "light": 1, "medium": 2, "complex": 3}
        )
        self.cleaner_threshold: int = int(config.get("cleaner_entry_threshold", 12))
        self.cleaner_token_threshold: int = int(config.get("cleaner_token_threshold", 8000))
        self.cleaner_retention_weights: dict[str, float] = config.get(
            "cleaner_retention_weights", 
            {"salience": 2.0, "confidence": 1.0, "recency": 0.1, "size_penalty": 0.01}
        )
        self.stall_rounds: int = int(config.get("stall_rounds", 2))
        self.max_replans: int = max(0, int(config.get("max_replans", 2)))
        self.cu_mode: str = str(config.get("cu_mode", "llm"))
        self.round_execution: str = str(config.get("round_execution", "concurrent"))
        self.coordinator_narration: bool = bool(config.get("coordinator_narration", False))
        self.sole_similarity: str = str(config.get("sole_similarity", "auto"))
        self.view_budget_tokens: int = max(
            512, int(config.get("view_budget_tokens", 12000))
        )
        self.grace_verification: bool = bool(config.get("grace_verification", True))
        self.actor_context: str = str(config.get("actor_context", "chained"))
        self.require_evidence: bool = bool(config.get("require_evidence", False))
        self._turn_durations: list[float] = []
        # True while the current step dispatches the closing sequence
        # (forced decider, grace review, grace revision). The orchestrator
        # gives closing turns a full timeout window instead of the clamped
        # remaining wall clock, so slow models can still land the answer.
        self.closing_sequence: bool = False
        # Reserve the tail of the duration budget for the forced decider and
        # the grace verification round, so those turns never dispatch with a
        # guaranteed-timeout window.
        self._duration_reserve_s: int = min(
            180, max(45, int(self.max_duration_s * 0.08))
        )

        # External services
        self.litellm_url = litellm_url
        self.litellm_key = litellm_key
        self.http = httpx.AsyncClient(timeout=60.0)

        # Node topology
        self.node_endpoints = node_endpoints
        self.role_registry = role_registry
        self.model_routing = model_routing
        self.model_pools = model_pools or {}
        if model_pricing is None:
            from config import MODEL_PRICING

            model_pricing = MODEL_PRICING
        self.model_pricing = {
            str(model): dict(pricing)
            for model, pricing in model_pricing.items()
        }

        # Edge inference round-robin state.
        # When model_routing resolves to "local", _resolve_edge_model()
        # cycles through edge_node_models so consecutive LLM calls hit
        # different inference GPUs instead of always targeting edge-node-1.
        self._edge_models: list[str] = edge_node_models or ["edge-node-1"]
        self._edge_rr_counter: int = 0

        # Per-task state (set during genesis)
        self.roster: AgentRoster | None = None
        self.genesis_time: float = 0.0
        self.genesis_started_at: float = 0.0
        self.budget_spent: float = 0.0
        self._stall_counter: int = 0
        self._replan_count: int = 0
        self._round_hashes: list[str] = []
        self._round_token_sets: list[frozenset[str]] = []
        self._tier: str = "medium"

        # Phase 5: stateful turn response IDs (doc 12 §5.2)
        self._response_ids: dict[str, str] = {}
        self._actor_nodes: dict[str, str] = {}

        # Phase 5: HITL pause flag (doc 05 §6)
        self._paused: bool = False
        self._checkpoint_lock = asyncio.Lock()

    # ── Genesis ──────────────────────────────────────────────────────

    @staticmethod
    def genesis_checkpoint_complete(meta: dict[str, Any]) -> bool:
        """Return true when metadata proves that genesis completed."""
        if "genesis_complete" in meta:
            return meta.get("genesis_complete") is True

        # Older classic tasks saved the roster before this marker existed.
        # A saved roster is the legacy completion record for those tasks.
        roster = meta.get("roster")
        if isinstance(roster, str):
            try:
                roster = json.loads(roster)
            except (json.JSONDecodeError, TypeError):
                return False
        return bool(roster) and isinstance(roster, (dict, list))

    async def genesis(self, task: Any) -> None:
        """Initialize: triage → AG experts → objective entry → attachments."""
        self.genesis_time = time.monotonic()
        self.genesis_started_at = time.time()
        task_id = task["task_id"]
        query = task["query"]

        # A failed genesis can save AG cost before it saves the completion
        # marker. Preserve that cost and the original duration boundary.
        get_meta = getattr(self.store, "get_meta", None)
        if get_meta is not None:
            prior_meta = await get_meta(task_id)
            self.budget_spent = max(
                self.budget_spent,
                float(prior_meta.get("budget_spent", 0.0)),
            )
            prior_started_at = prior_meta.get("genesis_started_at")
            if prior_started_at is not None:
                self.genesis_started_at = float(prior_started_at)
                elapsed = max(0.0, time.time() - self.genesis_started_at)
                self.genesis_time = time.monotonic() - elapsed
        await self.gateway.set_meta(
            task_id,
            genesis_started_at=self.genesis_started_at,
            genesis_complete=False,
        )

        # 1. Triage classification (existing triage, now effective)
        triage_result = task.get("triage_result")
        self._tier = triage_result.complexity.value if triage_result else "medium"
        tier_model = self.model_routing.get(self._tier, "medium")

        # 2. AG — generate experts (one LiteLLM call, doc 05 §2.1)
        n_experts = self.experts_per_tier.get(self._tier, 1)
        experts = await self._generate_experts(query, n_experts, self._tier, task_id)

        # 3. Build roster
        self.roster = AgentRoster(
            constants=dict(CONSTANT_ROLE_DESCRIPTIONS),
            experts=experts,
        )

        logger.info(
            "genesis | task=%s tier=%s experts=%d model=%s",
            task_id, self._tier, len(experts), tier_model,
        )

        # 4. Write objective entry via Gateway
        max_body_len = max(1, int(getattr(self.gateway, "_max_body_len", 8000)))
        truncation_marker = (
            "\n\n[Board objective truncated. The turn objective contains the full input.]"
        )
        objective_body = query
        objective_truncated = len(query) > max_body_len
        if objective_truncated:
            marker = truncation_marker[:max_body_len]
            objective_body = (
                query[: max(0, max_body_len - len(marker))]
                + marker
            )
        objective_entry = {
            "type": "objective",
            "title": query[:200],
            "body": objective_body,
            "confidence": 1.0,
            "_mutation_id": "genesis:objective:v1",
        }
        await self.gateway.append(
            task_id, "control_unit", ["decision_writer"],
            [objective_entry], turn_id="genesis", round_no=0,
        )

        # 5. Attach uploads (doc 17 §4)
        await self._attach_uploads(task_id, task)

        # 6. Save the roster and completion marker in one metadata update.
        await self.gateway.set_meta(
            task_id,
            phase="Discovery",
            round=0,
            budget_spent=self.budget_spent,
            budget_reserved=0.0,
            variant="classic",
            decider_state="waiting",
            tier=self._tier,
            genesis_started_at=self.genesis_started_at,
            roster={
                "constants": self.roster.constants,
                "experts": [
                    {
                        "name": expert.name,
                        "slug": expert.slug,
                        "ability": expert.ability,
                        "model": expert.model,
                    }
                    for expert in self.roster.experts
                ],
            },
            response_ids={},
            actor_nodes={},
            stall_counter=0,
            replan_count=0,
            round_hashes=[],
            edge_rr_counter=0,
            progress_ledger=[],
            progress_ledger_archived=0,
            objective_truncated=objective_truncated,
            genesis_complete=True,
        )

    async def resume(self, task: Any) -> None:
        """Restore the control state for a durable classic-board task."""
        task_id = task["task_id"]
        meta = await self.store.get_meta(task_id)
        roster_data = meta.get("roster", {})
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

        self.roster = AgentRoster(constants=constants, experts=experts)
        self._tier = str(meta.get("tier", "medium"))
        self.budget_spent = float(meta.get("budget_spent", 0.0))
        self._response_ids = {
            str(actor): str(response_id)
            for actor, response_id in dict(meta.get("response_ids", {})).items()
        }
        self._actor_nodes = {
            str(actor): str(endpoint)
            for actor, endpoint in dict(meta.get("actor_nodes", {})).items()
            if endpoint
        }
        self._stall_counter = int(meta.get("stall_counter", 0))
        self._replan_count = int(meta.get("replan_count", 0))
        self._round_hashes = [
            str(value) for value in meta.get("round_hashes", [])
        ]
        self._round_token_sets = [
            frozenset(str(token) for token in tokens)
            for tokens in meta.get("round_token_sets", [])
            if isinstance(tokens, (list, tuple, set, frozenset))
        ]
        self._turn_durations = [
            float(value) for value in meta.get("turn_durations", [])
            if isinstance(value, (int, float)) and value > 0
        ]
        self._edge_rr_counter = int(meta.get("edge_rr_counter", 0))
        self.genesis_started_at = float(meta.get("genesis_started_at", time.time()))
        elapsed = max(0.0, time.time() - self.genesis_started_at)
        self.genesis_time = time.monotonic() - elapsed

    async def checkpoint(self, task_id: str) -> None:
        """Persist the control state at a safe round boundary."""
        roster = self.roster or AgentRoster(
            constants=dict(CONSTANT_ROLE_DESCRIPTIONS), experts=[],
        )
        await self.gateway.set_meta(
            task_id,
            tier=self._tier,
            budget_spent=self.budget_spent,
            response_ids=dict(self._response_ids),
            actor_nodes=dict(self._actor_nodes),
            stall_counter=self._stall_counter,
            replan_count=self._replan_count,
            round_hashes=list(self._round_hashes),
            round_token_sets=[
                sorted(tokens) for tokens in self._round_token_sets
            ],
            turn_durations=list(self._turn_durations),
            edge_rr_counter=self._edge_rr_counter,
            genesis_started_at=self.genesis_started_at,
            roster={
                "constants": roster.constants,
                "experts": [
                    {
                        "name": expert.name,
                        "slug": expert.slug,
                        "ability": expert.ability,
                        "model": expert.model,
                    }
                    for expert in roster.experts
                ],
            },
        )

    async def _generate_experts(
        self, query: str, n: int, tier: str, task_id: str | None = None,
    ) -> list[ExpertIdentity]:
        """AG: one LiteLLM call to generate n expert identities (doc 05 §2.1)."""
        if n <= 0:
            return []

        from models.personas import AG_SYSTEM_PROMPT

        ag_model = self._resolve_model(self.model_routing.get(tier, "medium"))
        fallback_reason: str | None = None
        try:
            resp = await self.http.post(
                f"{self.litellm_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.litellm_key}"},
                json={
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
                },
            )
            resp.raise_for_status()
            resp_json = resp.json()
            # Capture control-plane LLM usage/cost (doc 06 §3.1)
            await self._record_llm_cost(
                task_id, resp_json.get("usage"), ag_model, "control_plane:ag",
                round_no=0,
            )
            choice = resp_json["choices"][0]
            finish_reason = choice.get("finish_reason", "stop")
            raw_content = message_content(resp_json)

            # Guard against truncated JSON: if the model hit the token limit
            # the JSON will be incomplete and json.loads will raise.  Detect
            # this early so the except block can log a meaningful reason.
            if finish_reason == "length":
                usage = resp_json.get("usage", {})
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
        except Exception as e:
            fallback_reason = str(e)
            logger.warning("AG call failed (%s), using default experts", e)
            raw_experts = self._default_experts(n)

            # Emit a visible ag_fallback event to the task stream so the operator
            # can diagnose why generic experts appeared in Mission Control.
            if task_id and self.emitter:
                try:
                    await self.emitter.emit(task_id, "ag_fallback", {
                        "model": ag_model,
                        "tier": tier,
                        "error": fallback_reason,
                        "fallback_experts": [ex["slug"] for ex in raw_experts],
                    })
                except Exception as emit_err:
                    logger.debug("Failed to emit ag_fallback event: %s", emit_err)

        # Assign models with pool diversity (doc 05 §2.1)
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

    def _default_experts(self, n: int) -> list[dict]:
        """Fallback expert definitions when AG call fails."""
        defaults = [
            {"name": "Domain Analyst", "slug": "domain_analyst",
             "ability": "Deep analysis of the core domain question"},
            {"name": "Systems Thinker", "slug": "systems_thinker",
             "ability": "Identifies systemic factors and second-order effects"},
            {"name": "Evidence Reviewer", "slug": "evidence_reviewer",
             "ability": "Verifies claims against available evidence and data"},
            {"name": "Root Cause Analyst", "slug": "root_cause_analyst",
             "ability": "Traces failure chains to their underlying structural causes"},
        ]
        return defaults[:n]


    async def _attach_uploads(self, task_id: str, task: dict) -> None:
        """Create attachment entries for uploaded files (doc 17 §4)."""
        attachments = task.get("attachments", [])
        if not attachments:
            return

        for index, att in enumerate(attachments):
            attachment_id = str(att.get("file_id") or index)
            name = str(att.get("name") or "file")
            preview = str(att.get("text_preview") or "")
            body = preview or f"File: {name}"
            max_body_len = max(
                1,
                int(getattr(self.gateway, "_max_body_len", 8000)),
            )
            entry = {
                "type": "attachment",
                "title": f"Uploaded: {name}",
                "body": body[:max_body_len],
                "confidence": 1.0,
                "_mutation_id": f"genesis:attachment:{attachment_id}:v1",
            }
            await self.gateway.append(
                task_id, "control_unit",
                ["post:attachment"],  # direct capability
                [entry], turn_id="genesis", round_no=0,
            )

    # ── Step (one round) ─────────────────────────────────────────────

    async def step(self, task: Any, board: Any) -> StepResult:
        """Run one round: deterministic guards → CU selection → activations."""
        task_id = task["task_id"]
        meta = await self.store.get_meta(task_id)
        current_round = int(meta.get("round", 0)) + 1
        snapshot = await self.store.get_snapshot(task_id)

        # ── 1. Deterministic guards FIRST (no LLM, doc 05 §5) ────────

        # Guard: accepted solution
        reviewed_solution_id = meta.get("solution_reviewed_id")
        solution = self._accepted_solution(
            snapshot,
            current_round,
            reviewed_solution_id=str(reviewed_solution_id)
            if reviewed_solution_id else None,
            require_review=True,
        )
        if solution:
            return StepResult(terminal=True, reason="solution")

        # If decider was forced last round, terminate now — unless the
        # stop rule still owes work: a grace critic review of an unseen
        # answer, or one decider revision after the critic rejected it.
        self.closing_sequence = False
        grace_candidate = None
        grace_revision = False
        if meta.get("decider_forced"):
            critic_enabled = (
                self.role_registry.get("critic", {}).get("enabled") is not False
            )
            if self.grace_verification and critic_enabled:
                open_solutions = sorted(
                    (
                        entry for entry in snapshot.values()
                        if entry.type == "solution" and entry.status == "open"
                    ),
                    key=lambda entry: (entry.round, entry.id),
                    reverse=True,
                )
                latest_solution = open_solutions[0] if open_solutions else None
                elapsed_now = time.monotonic() - self.genesis_time
                overrun_limit = self.max_duration_s + self.closing_turn_timeout_s()
                if (
                    latest_solution is not None
                    and latest_solution.id != meta.get("solution_reviewed_id")
                    and elapsed_now < overrun_limit
                ):
                    if latest_solution.id != meta.get("solution_candidate_id"):
                        # The critic has not seen this answer yet.
                        grace_candidate = latest_solution
                    elif not meta.get("grace_revision_done"):
                        # The critic saw this answer and did not approve it.
                        # If it posted a critique and resources remain, the
                        # decider gets exactly one revision round.
                        rejected = any(
                            entry.type == "critique"
                            and entry.status == "open"
                            and latest_solution.id in (entry.refs or [])
                            for entry in snapshot.values()
                        )
                        if rejected and self._revision_headroom(meta):
                            grace_revision = True
            if grace_candidate is None and not grace_revision:
                return StepResult(
                    terminal=True,
                    reason=meta.get("terminal_reason", "forced_decider_finished")
                )

        force_decider = False
        force_replan = False
        term_reason = None

        # Guard: max rounds
        if current_round > self.max_rounds:
            force_decider = True
            term_reason = "max_rounds"
        
        # Guard: budget ceiling
        self.budget_spent = float(meta.get("budget_spent", 0.0))
        if not force_decider and self.budget_spent >= self.budget_ceiling:
            force_decider = True
            term_reason = "budget"

        # Guard: duration cap. The reserve keeps enough wall clock for the
        # forced decider (and one grace verification round) to actually run,
        # scaled up when observed turns are slow.
        elapsed = time.monotonic() - self.genesis_time
        if not force_decider and elapsed >= self.max_duration_s - self._current_duration_reserve_s():
            force_decider = True
            term_reason = "duration"

        # Guard: stall breaker
        if not force_decider and self._is_stalled(snapshot, current_round):
            logger.info(
                "Stall detected at round %d (stall_counter=%d)",
                current_round, self._stall_counter,
            )
            if self._stall_counter >= self.stall_rounds:
                if self._replan_count < self.max_replans:
                    force_replan = True
                    self._replan_count += 1
                else:
                    force_decider = True
                    term_reason = "stalled"
            # Not yet at threshold — continue but note the stall

        # ── 1.5 Board Pressure Guard (Deterministic Cleaner) ─────────
        open_entries = [e for e in snapshot.values() if e.status == "open"]
        total_tokens = sum(len(e.body) // 4 for e in open_entries)
        solution_candidates = sorted(
            (entry for entry in open_entries if entry.type == "solution"),
            key=lambda entry: (entry.round, entry.id),
            reverse=True,
        )
        unreviewed_solution = (
            solution_candidates[0]
            if solution_candidates
            and solution_candidates[0].id != reviewed_solution_id
            else None
        )
        
        if grace_candidate is not None:
            self.closing_sequence = True
            selected = ["critic"]
            rationale = (
                f"Grace verification: solution {grace_candidate.id} receives "
                "one independent critic review before the task stops."
            )
            source = "grace_verification"
            await self.gateway.set_meta(
                task_id,
                grace_verification_done=True,
                solution_candidate_id=grace_candidate.id,
            )
        elif grace_revision:
            self.closing_sequence = True
            selected = ["decider"]
            rationale = (
                "Grace revision: the critic rejected the answer. The decider "
                "posts one revised solution that resolves the critique, and "
                "then the task stops."
            )
            source = "grace_revision"
            await self.gateway.set_meta(task_id, grace_revision_done=True)
        elif force_decider:
            self.closing_sequence = True
            selected = ["decider"]
            rationale = f"Task termination reached ({term_reason}) — forcing decider to synthesize final solution."
            source = "heuristic"
            await self.gateway.set_meta(task_id, decider_forced=True, terminal_reason=term_reason)
        elif unreviewed_solution is not None:
            selected = ["critic"]
            rationale = (
                f"Solution {unreviewed_solution.id} requires an independent "
                "critic review before completion."
            )
            source = "verification_guard"
            await self.gateway.set_meta(
                task_id,
                solution_candidate_id=unreviewed_solution.id,
            )
        elif force_replan:
            selected = ["planner"]
            rationale = (
                "The board stopped changing. The planner must revise the "
                "work plan before the task can terminate."
            )
            source = "stall_replan"
            await self.gateway.set_meta(
                task_id,
                replan_count=self._replan_count,
            )
        elif total_tokens > self.cleaner_token_threshold:
            selected = ["cleaner"]
            rationale = f"Board exceeded token threshold ({total_tokens} > {self.cleaner_token_threshold}) — forced cleaner invocation."
            source = "heuristic"
        else:
            # ── 2. CU selection (one bare LiteLLM call, doc 05 §1.1) ─────
            rationale = None
            source = "heuristic"
    
            if self.cu_mode == "heuristic_first":
                selected = self._deterministic_fallback(snapshot, current_round)
            else:
                selected, rationale = await self._cu_select(
                    task_id, task["query"], snapshot, current_round, meta,
                )
                source = "llm" if selected else "heuristic"

                # The CU call itself consumes budget. Recheck the ceiling before
                # any worker starts so a control-plane call cannot authorize a
                # new non-terminal activation after it exhausts the task budget.
                if self.budget_spent >= self.budget_ceiling:
                    self.closing_sequence = True
                    selected = ["decider"]
                    rationale = (
                        "The coordinator call exhausted the task budget. "
                        "The decider must now synthesize the final answer."
                    )
                    source = "heuristic"
                    force_decider = True
                    term_reason = "budget"
                    await self.gateway.set_meta(
                        task_id,
                        decider_forced=True,
                        terminal_reason=term_reason,
                    )
    
            if not selected:
                # No agents selected — treat as stall
                self._stall_counter += 1
                selected = self._deterministic_fallback(snapshot, current_round)
                source = "heuristic"
                rationale = None

        selected = self._normalize_selection(selected)
        if not selected:
            await self.gateway.set_meta(
                task_id,
                terminal_reason="no_available_agents",
            )
            return StepResult(
                terminal=True,
                reason="no_available_agents",
                selected=[],
                rationale="No enabled agent can accept the next activation.",
                selection_source="availability_guard",
                phase=self._infer_phase(snapshot, current_round),
            )

        # Clamp to max_concurrent
        selected = selected[:self.max_concurrent]

        # ── Paper §3.2 guard: decider MUST run alone ─────────────────
        # The decider must see ALL board writes (including critiques)
        # before judging. If the CU co-selected decider with other agents,
        # strip it — the next round's CU call will re-select it once the
        # other agents have finished writing.
        if "decider" in selected and len(selected) > 1:
            logger.info(
                "Decider exclusion guard | task=%s round=%d — "
                "CU co-selected decider with %s; deferring decider to next round",
                task_id, current_round,
                [a for a in selected if a != "decider"],
            )
            selected = [a for a in selected if a != "decider"]
            rationale = (
                (rationale or "")
                + " [Decider deferred: must run alone per paper §3.2"
                  " so it can see all prior board writes.]"
            ).strip()

        # Emit coordinator narration event (doc 05 §1.2, doc 13 §3)
        # Gated by flag — when off, no event fires and the UI lane hides entirely.
        # NOTE: this carries the RAW rationale (None on the heuristic path) to
        # preserve the documented narration contract.
        if self.coordinator_narration and self.emitter:
            await self.emitter.emit(task_id, "coordinator_narration", {
                "round": current_round,
                "selected": selected,
                "rationale": rationale,
                "source": source,
            })

        phase = self._infer_phase(snapshot, current_round)
        activations = self._to_activations(selected)
        if not activations:
            await self.gateway.set_meta(
                task_id,
                terminal_reason="no_available_agents",
            )
            return StepResult(
                terminal=True,
                reason="no_available_agents",
                selected=[],
                rationale="No configured endpoint can accept the next activation.",
                selection_source="availability_guard",
                phase=phase,
            )
        for index, activation in enumerate(activations):
            activation.activation_id = self._activation_id(
                task_id, current_round, activation.actor, index,
            )

        # For the persisted turn / execution-graph, always provide a
        # human-readable rationale: fall back to a synthesized one mirroring
        # the deterministic routing rules when the CU gave none. This is kept
        # separate from the narration event above so its contract is untouched.
        display_rationale = rationale or self._fallback_rationale(
            snapshot, current_round, selected,
        )

        await self.gateway.set_meta(
            task_id,
            round=current_round,
            phase=phase,
            actor_nodes=dict(self._actor_nodes),
            round_state={
                "round": current_round,
                "status": "active",
                "rationale": display_rationale,
                "selection_source": source,
                "phase": phase,
                "activations": [
                    {
                        "actor": activation.actor,
                        "role": activation.role,
                        "model": activation.model,
                        "node_endpoint": activation.node_endpoint,
                        "profile": activation.profile,
                        "activation_id": activation.activation_id,
                    }
                    for activation in activations
                ],
                "completed": {},
            },
        )

        logger.info(
            "step | task=%s round=%d selected=%s phase=%s",
            task_id, current_round, [a.actor for a in activations], phase,
        )

        return StepResult(
            terminal=False,
            activations=activations,
            selected=[a.actor for a in activations],
            rationale=display_rationale,
            selection_source=source,
            phase=phase,
        )

    @staticmethod
    def _activation_id(
        task_id: str, round_no: int, actor: str, index: int,
    ) -> str:
        """Build a stable activation identity for retries and restarts."""
        value = f"bmas:{task_id}:{round_no}:{actor}:{index}"
        return f"activation-{uuid.uuid5(uuid.NAMESPACE_URL, value).hex}"

    async def restore_active_round(self, task_id: str) -> StepResult | None:
        """Restore the unfinished activation plan for one classic round."""
        meta = await self.store.get_meta(task_id)
        state = meta.get("round_state")
        if not isinstance(state, dict) or state.get("status") != "active":
            return None
        completed = state.get("completed", {})
        if not isinstance(completed, dict):
            completed = {}
        activations = []
        for raw in state.get("activations", []):
            if not isinstance(raw, dict):
                continue
            activation_id = str(raw.get("activation_id", ""))
            if activation_id and activation_id in completed:
                continue
            activations.append(Activation(
                actor=str(raw.get("actor", "")),
                role=str(raw.get("role", "")),
                model=str(raw.get("model", "")),
                node_endpoint=str(raw.get("node_endpoint", "")),
                profile=raw.get("profile"),
                activation_id=activation_id or None,
            ))
        return StepResult(
            terminal=False,
            activations=activations,
            selected=[activation.actor for activation in activations],
            rationale=str(state.get("rationale", "Recovered round")),
            selection_source=str(state.get("selection_source", "checkpoint")),
            phase=str(state.get("phase", meta.get("phase", "Discovery"))),
        )

    async def mark_activation_complete(
        self,
        task_id: str,
        activation_id: str,
        status: str,
        actor: str | None = None,
        response_id: str | None = None,
        node_endpoint: str | None = None,
    ) -> None:
        """Persist one activation result and its response identity together."""
        if not activation_id:
            return
        async with self._checkpoint_lock:
            meta = await self.store.get_meta(task_id)
            state = dict(meta.get("round_state") or {})
            if state.get("status") != "active":
                return
            completed = dict(state.get("completed") or {})
            completed[activation_id] = status
            state["completed"] = completed
            fields: dict[str, Any] = {"round_state": state}
            if actor:
                if response_id:
                    self._response_ids[actor] = response_id
                fields["response_ids"] = dict(self._response_ids)
                if node_endpoint:
                    self._actor_nodes[actor] = node_endpoint
                fields["actor_nodes"] = dict(self._actor_nodes)
            await self.gateway.set_meta(task_id, **fields)

    async def finish_round(self, task_id: str) -> None:
        """Close a round only after every planned activation has a result."""
        async with self._checkpoint_lock:
            meta = await self.store.get_meta(task_id)
            state = dict(meta.get("round_state") or {})
            if state.get("status") != "active":
                return
            planned = {
                str(raw.get("activation_id", ""))
                for raw in state.get("activations", [])
                if isinstance(raw, dict) and raw.get("activation_id")
            }
            completed = set(dict(state.get("completed") or {}))
            if not planned.issubset(completed):
                missing = sorted(planned - completed)
                raise RuntimeError(
                    f"Round checkpoint has unfinished activations: {missing}"
                )
            state["status"] = "completed"
            snapshot = await self.store.get_snapshot(task_id)
            ledger = list(meta.get("progress_ledger") or [])
            round_no = int(state.get("round", meta.get("round", 0)))
            ledger.append({
                "round": round_no,
                "actors": [
                    str(raw.get("actor", ""))
                    for raw in state.get("activations", [])
                    if isinstance(raw, dict)
                ],
                "activation_statuses": dict(state.get("completed") or {}),
                "entries_added": sum(
                    1 for entry in snapshot.values() if entry.round == round_no
                ),
                "open_entries": sum(
                    1 for entry in snapshot.values() if entry.status == "open"
                ),
                "open_conflicts": sum(
                    1
                    for entry in snapshot.values()
                    if entry.status == "open" and entry.type == "conflict"
                ),
            })
            archived_count = int(meta.get("progress_ledger_archived", 0))
            if len(ledger) > 100:
                archived_count += len(ledger) - 100
                ledger = ledger[-100:]
            await self.gateway.set_meta(
                task_id,
                round_state=state,
                progress_ledger=ledger,
                progress_ledger_archived=archived_count,
            )

    # ── Finalize ─────────────────────────────────────────────────────

    async def finalize(
        self, task: Any, board: Any, reason: str,
    ) -> dict[str, Any]:
        """Extract the final answer (Decider path or SolE, doc 05 §3)."""
        task_id = task["task_id"]
        snapshot = await self.store.get_snapshot(task_id)

        # Decider path: accepted solution on the board
        meta = await self.store.get_meta(task_id)
        reviewed_solution_id = meta.get("solution_reviewed_id")
        solution_entry = self._accepted_solution(
            snapshot,
            reviewed_solution_id=str(reviewed_solution_id)
            if reviewed_solution_id else None,
            require_review=True,
        )
        if solution_entry:
            answer = solution_entry.body
            answer_source = "decider"
            verification_status = "critic_reviewed"
        else:
            open_solutions = sorted(
                (
                    entry for entry in snapshot.values()
                    if entry.type == "solution" and entry.status == "open"
                ),
                key=lambda entry: (entry.round, entry.id),
                reverse=True,
            )
            if open_solutions:
                answer = open_solutions[0].body
                answer_source = "decider_unverified"
            else:
                # SolE provides a fallback answer. Agreement is not verification.
                answer = await self._solution_extraction(task, snapshot)
                answer_source = "sole_unverified"
            verification_status = "unverified"

        # Update board meta
        await self.gateway.set_meta(
            task_id,
            phase="Solved",
            terminated_by=reason,
            answer_source=answer_source,
            verification_status=verification_status,
            final_answer=answer,
        )

        logger.info(
            "finalize | task=%s reason=%s source=%s",
            task_id, reason, answer_source,
        )

        return {
            "answer": answer,
            "terminated_by": reason,
            "answer_source": answer_source,
            "verification_status": verification_status,
            "rounds_completed": int(
                (await self.store.get_meta(task_id)).get("round", 0)
            ),
            "budget_spent": self.budget_spent,
        }

    # ── Build Turn Payload ───────────────────────────────────────────

    def build_turn_payload(
        self, task: Any, actor: str, board: Any,
    ) -> dict:
        """Build the payload dispatched to a KS for this turn (doc 03 §4)."""
        from models.personas import ROLE_PERSONAS, generate_expert_persona

        task_id = task["task_id"]
        query = task["query"]

        # Resolve role prompt
        base_role = actor.split(".")[0] if "." in actor else actor
        if actor.startswith("expert.") and self.roster:
            slug = actor.split(".", 1)[1]
            expert = next(
                (e for e in self.roster.experts if e.slug == slug), None
            )
            if expert:
                role_prompt = generate_expert_persona(
                    expert.name, expert.ability, query,
                )
            else:
                role_prompt = ROLE_PERSONAS.get(base_role, "")
        else:
            role_prompt = ROLE_PERSONAS.get(actor, "")

        # Serialize board for prompt
        if actor == "cleaner":
            eviction_candidates = self._get_eviction_candidates(board)
            protected_context = [
                entry
                for entry in board.values()
                if getattr(entry, "type", None) in {"objective", "directive", "ledger"}
            ]
            subset = [*protected_context, *eviction_candidates]
            board_data = {"mode": "condense", "entries": [entry_to_dict(e) for e in subset]}
        else:
            board_data = self._serialize_board(board, actor=actor)

        payload = {
            "task_id": task_id,
            "turn_id": f"turn-{uuid.uuid4().hex[:8]}",
            "round": board.get("round", 0) if isinstance(board, dict) else 0,
            "role": actor,
            "role_prompt": role_prompt,
            "objective": query,
            "board": board_data,
            "response_contract": "entries_v1",
            "budget_remaining_usd": max(0, self.budget_ceiling - self.budget_spent),
            # Phase 5: stateful turns (doc 12 §5.2). In fresh context mode the
            # bounded board view is the whole memory: the model conversation
            # does not chain, so per-round cost stays flat over long runs.
            "session_id": f"{task_id}:{actor}",
            "previous_response_id": (
                self.get_response_id(actor)
                if self.actor_context == "chained"
                else None
            ),
        }
        if self.require_evidence and base_role in ("expert", "planner"):
            payload["evidence_status"] = (
                "Evidence required: ground every new finding in an external "
                "source. List the URLs or tool citations in the entry's "
                "\"sources\" array. A round of unsourced restatement counts "
                "as a stall."
            )
        if (
            self.budget_ceiling > 0
            and self.budget_spent / self.budget_ceiling >= 0.8
        ):
            payload["budget_status"] = (
                "Over 80% of the task budget is spent. Converge now: "
                "verify or finalize existing work instead of opening new work."
            )
        return payload

    # ── Parse Agent Response ─────────────────────────────────────────

    def parse_agent_response(
        self,
        task: Any,
        actor: str,
        raw: Any,
        known_ids: set[str] | None = None,
    ) -> list[dict]:
        """Parse agent response into proposed board entries."""
        results = []
        action_payload = raw
        if isinstance(raw, dict) and isinstance(raw.get("result"), str):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                decoded = json.loads(raw["result"])
                if isinstance(decoded, dict):
                    action_payload = decoded
        if (
            actor == "critic"
            and isinstance(action_payload, dict)
            and action_payload.get("action") == "approve"
        ):
            refs = action_payload.get("refs", [])
            if not isinstance(refs, list):
                return []
            valid_refs = [
                str(ref) for ref in refs
                if isinstance(ref, str)
                and (known_ids is None or ref in known_ids)
            ]
            return [{"_action": "approve", "refs": valid_refs}]
        # Cleaner / decline short-circuit (preserve existing contract)
        if isinstance(raw, dict):
            if raw.get("action") in ("clean", "condense"):
                results.append({"_action": "clean", "removals": raw.get("removals", [])})
            if raw.get("action") == "decline":
                return []

        parsed = parse_entries(raw, actor, known_ids=known_ids)
        results.extend(parsed)
        return results

    # ── Apply ────────────────────────────────────────────────────────

    async def apply(
        self, task: Any, mutations: list,
    ) -> list:
        """Apply mutations through the Gateway."""
        task_id = task["task_id"]
        events = []
        for mutation in mutations:
            actor = mutation.get("actor", "unknown")
            role = actor.split(".")[0] if "." in actor else actor
            caps = capabilities_for_role(role)
            if not caps and actor.startswith("expert."):
                caps = ["finding_writer"]

            # Handle cleaner removals
            if mutation.get("_action") == "clean":
                removals = mutation.get("removals", [])
                entry_ids = [r.get("entry_id") for r in removals if r.get("entry_id")]
                if entry_ids:
                    removed = await self.gateway.remove(
                        task_id, actor, caps, entry_ids,
                        reason="Cleaner maintenance",
                        turn_id=mutation.get("turn_id"),
                        round_no=int(mutation.get("round", 0)),
                        mutation_id=mutation.get("_mutation_id"),
                    )
                    events.extend(removed)
                continue

            if mutation.get("_action") == "approve":
                snapshot = await self.store.get_snapshot(task_id)
                referenced = [
                    snapshot[entry_id]
                    for entry_id in mutation.get("refs", [])
                    if entry_id in snapshot
                    and snapshot[entry_id].type == "solution"
                    and snapshot[entry_id].status == "open"
                ]
                if len(referenced) != 1:
                    continue
                solution = referenced[0]
                mutation_id = mutation.get("_mutation_id")
                proposed = {
                    "type": "critique",
                    "title": "Verification passed",
                    "body": (
                        "The independent critic found no blocking issue in "
                        f"solution {solution.id}."
                    ),
                    "refs": [solution.id],
                    "confidence": 1.0,
                }
                if mutation_id:
                    proposed["_mutation_id"] = f"{mutation_id}:approval"
                committed = await self.gateway.append(
                    task_id,
                    actor,
                    caps,
                    [proposed],
                    turn_id=mutation.get("turn_id", ""),
                    round_no=mutation.get("round", 0),
                )
                if not committed:
                    continue
                audit_entry = committed[0]
                await self.gateway.set_status(
                    task_id,
                    audit_entry.id,
                    "superseded",
                    actor,
                    mutation_id=(
                        f"{mutation_id}:approval:resolved"
                        if mutation_id else None
                    ),
                )
                await self.gateway.set_meta(
                    task_id,
                    solution_reviewed_id=solution.id,
                )
                events.extend(committed)
                continue

            raw_entries = mutation.get("entries")
            if raw_entries is None:
                proposed_entries = [mutation]
            elif isinstance(raw_entries, list):
                proposed_entries = [
                    entry for entry in raw_entries if isinstance(entry, dict)
                ]
            else:
                continue
            mutation_id = mutation.get("_mutation_id")
            if mutation_id:
                proposed_entries = [
                    {**entry, "_mutation_id": f"{mutation_id}:{index}"}
                    for index, entry in enumerate(proposed_entries)
                ]
            committed = await self.gateway.append(
                task_id, actor, caps, proposed_entries,
                turn_id=mutation.get("turn_id", ""),
                round_no=mutation.get("round", 0),
            )
            events.extend(committed)
            # The newest task ledger replaces every earlier one. A single
            # authoritative ledger keeps the plan from drifting across
            # long runs.
            new_ledgers = [
                entry for entry in committed if entry.type == "ledger"
            ]
            if new_ledgers:
                keep_id = new_ledgers[-1].id
                snapshot = await self.store.get_snapshot(task_id)
                for entry in snapshot.values():
                    if (
                        entry.type == "ledger"
                        and entry.status == "open"
                        and entry.id != keep_id
                    ):
                        await self.gateway.set_status(
                            task_id, entry.id, "superseded", actor,
                        )
        return events

    # ── Is Terminal ──────────────────────────────────────────────────

    def is_terminal(self, board: Any) -> tuple[bool, str | None]:
        """Pure check: is the board in a terminal state?"""
        if isinstance(board, dict):
            snapshot = board
        else:
            # Synchronous check — only works with pre-fetched snapshot
            return (False, None)

        if self._accepted_solution(snapshot):
            return (True, "solution")
        return (False, None)

    # ── CU Selection (doc 05 §1.1) ───────────────────────────────────

    def _cu_prompt(
        self,
        query: str,
        board_text: str,
        roster_text: str,
        current_round: int,
        snapshot: dict[str, BoardEntry],
        meta: dict[str, Any],
    ) -> str:
        """Build the control-unit prompt with an explicit progress block.

        The CU sees how the task is trending — budget pressure, new entries
        last round, unresolved critiques, and the stall state — so it can
        prefer verification and convergence when returns diminish.
        """
        budget_remaining = max(0.0, self.budget_ceiling - self.budget_spent)
        budget_pct = (
            min(100, int(100 * self.budget_spent / self.budget_ceiling))
            if self.budget_ceiling > 0 else 0
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
        if self.require_evidence:
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
            f"- Budget: ${self.budget_spent:.4f} spent of "
            f"${self.budget_ceiling:.2f} ({budget_pct}%)\n"
            f"- Last round added {entries_last_round} entries "
            f"({evidence_last_round} with external sources); "
            f"{open_critiques} unresolved critiques; "
            f"{open_conflicts} open conflicts\n"
            f"- Stall counter: {self._stall_counter}/{self.stall_rounds}; "
            f"replans used: {self._replan_count}/{self.max_replans}\n"
            f"- A round that only restates existing content counts as a stall.\n"
            f"{evidence_line}"
            f"{pressure_line}\n"
            f"## Constraints\n"
            f"- Round: {current_round}/{self.max_rounds}\n"
            f"- Budget remaining: ${budget_remaining:.4f}\n"
            f"- Select 1-{self.max_concurrent} agents\n"
        )

    async def _cu_select(
        self,
        task_id: str,
        query: str,
        snapshot: dict[str, BoardEntry],
        current_round: int,
        meta: dict[str, Any],
    ) -> tuple[list[str], str | None]:
        """One bare LiteLLM call per round for agent selection.

        Returns (selected_actors, rationale).  Rationale may be None if
        the CU response was garbled or missing it — this NEVER blocks
        the loop (doc 05 §1.2).
        """
        if not self.roster:
            return self._deterministic_fallback(snapshot, current_round), None

        from models.personas import CU_SYSTEM_PROMPT

        board_text = self._serialize_board_for_cu(snapshot)
        roster_text = "\n".join(
            f"- {actor}: {desc}"
            for actor, desc in self.roster.all_actors()
        )

        prompt = self._cu_prompt(
            query, board_text, roster_text, current_round, snapshot, meta,
        )

        system = CU_SYSTEM_PROMPT.format(max_concurrent=self.max_concurrent)

        cu_model = self._resolve_model(self.model_routing.get("light", "medium"))
        # The visible reply is a short JSON object; a reasoning model
        # spends completion tokens on reasoning first, so the budget
        # comes from the provider profile and grows once on truncation.
        parameters = completion_parameters(
            profile_for_alias(cu_model), output_tokens=256, temperature=0.2,
            reasoning="low", json_object=True,
        )
        # Try up to 2 times (1 retry on garbled or truncated output)
        for attempt in range(2):
            try:
                resp = await self.http.post(
                    f"{self.litellm_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.litellm_key}"},
                    json={
                        "model": cu_model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        **parameters,
                    },
                    timeout=30.0,
                )
                resp.raise_for_status()
                resp_json = resp.json()
                # Capture control-plane LLM usage/cost (doc 06 §3.1)
                await self._record_llm_cost(
                    task_id, resp_json.get("usage"), cu_model, "control_plane:cu",
                    round_no=current_round,
                )
                cut = truncated(resp_json)
                if cut is not None:
                    logger.warning(
                        "CU reply truncated at %s tokens (%s reasoning); "
                        "retrying with a larger budget",
                        cut["completion_tokens"], cut["reasoning_tokens"],
                    )
                    parameters = retry_budget(parameters)
                    continue
                raw = message_content(resp_json)
                selected, rationale = parse_cu_output(raw, self.roster.actor_names())
                if selected:
                    return selected, rationale
                logger.warning("CU returned empty selection (attempt %d)", attempt + 1)
            except Exception as e:
                logger.warning("CU call failed (attempt %d): %s", attempt + 1, e)

        # Fallback to deterministic table
        logger.info("CU failed after retries, using deterministic fallback")
        return self._deterministic_fallback(snapshot, current_round), None

    def _fallback_rationale(
        self,
        snapshot: dict[str, BoardEntry],
        current_round: int,
        selected: list[str],
    ) -> str:
        """Synthesize a human-readable routing rationale for the graph.

        Used when the CU did not return a usable rationale (deterministic
        fallback or garbled LLM output). Mirrors the decision rules in
        ``_deterministic_fallback`` so the Graph tab can always explain WHY
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

    def _get_eviction_candidates(self, snapshot: dict[str, BoardEntry] | dict[str, Any], max_candidates: int = 12) -> list[BoardEntry]:
        """Calculate Retention Value and return the bottom N eviction candidates."""
        open_entries = []
        for e in snapshot.values():
            status = getattr(e, "status", e.get("status", "")) if isinstance(e, dict) else getattr(e, "status", "")
            if status == "open":
                open_entries.append(e)
                
        protected_ids = set()
        
        # 1. Protect critical entries. Structural types stay protected
        # always; plans and critiques stay protected only while recent, so
        # a long run can condense its own history instead of hoarding it.
        latest_round = max(
            (
                int((getattr(e, "round", e.get("round", 0)) if isinstance(e, dict) else getattr(e, "round", 0)) or 0)
                for e in open_entries
            ),
            default=0,
        )
        recent_floor = latest_round - CLEANER_RECENT_ROUNDS
        for e in open_entries:
            etype = getattr(e, "type", e.get("type", "")) if isinstance(e, dict) else getattr(e, "type", "")
            round_no = int((getattr(e, "round", e.get("round", 0)) if isinstance(e, dict) else getattr(e, "round", 0)) or 0)
            always = etype in ("objective", "directive", "ledger", "conflict", "solution")
            recent = etype in ("plan", "critique") and round_no >= recent_floor
            if always or recent:
                eid = getattr(e, "id", e.get("id")) if isinstance(e, dict) else getattr(e, "id", None)
                protected_ids.add(eid)
                refs = getattr(e, "refs", e.get("refs", [])) if isinstance(e, dict) else getattr(e, "refs", [])
                if refs:
                    for ref in refs:
                        protected_ids.add(ref)
                    
        candidates = []
        for e in open_entries:
            eid = getattr(e, "id", e.get("id")) if isinstance(e, dict) else getattr(e, "id", None)
            if eid in protected_ids:
                continue
                
            w_sal = self.cleaner_retention_weights.get("salience", 2.0)
            w_conf = self.cleaner_retention_weights.get("confidence", 1.0)
            w_rec = self.cleaner_retention_weights.get("recency", 0.1)
            w_size = self.cleaner_retention_weights.get("size_penalty", 0.01)
            
            body = getattr(e, "body", e.get("body", "")) if isinstance(e, dict) else getattr(e, "body", "")
            salience = getattr(e, "salience", e.get("salience", 0.0)) if isinstance(e, dict) else getattr(e, "salience", 0.0)
            confidence = getattr(e, "confidence", e.get("confidence", 0.5)) if isinstance(e, dict) else getattr(e, "confidence", 0.5)
            round_raw = getattr(e, "round", e.get("round", 0)) if isinstance(e, dict) else getattr(e, "round", 0)
            
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

    def _deterministic_fallback(
        self,
        snapshot: dict[str, BoardEntry],
        current_round: int,
    ) -> list[str]:
        """Deterministic fallback policy (doc 05 §1.1).

        Round 1 → planner + all experts
        Open critiques without rebuttals → critiqued authors
        Open conflicts → conflict_resolver
        Entry count > cleaner_threshold → cleaner
        Otherwise → decider
        """
        if not self.roster:
            return ["planner"]

        # Round 1: planner + all experts
        if current_round <= 1:
            selected = ["planner"]
            for expert in self.roster.experts:
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
                return list(critiqued_authors)

        # Open conflicts → conflict_resolver
        conflicts = [
            e for e in open_entries.values() if e.type == "conflict"
        ]
        if conflicts:
            return ["conflict_resolver"]

        # Entry count > threshold → cleaner
        if len(open_entries) > self.cleaner_threshold:
            return ["cleaner"]

        # Default → decider
        return ["decider"]

    # ── SolE (doc 05 §3, path 2) ─────────────────────────────────────

    async def _solution_extraction(
        self, task: dict, snapshot: dict[str, BoardEntry],
    ) -> str:
        """Majority-similarity vote when no accepted solution exists."""
        if not self.roster:
            return self._best_finding(snapshot)

        query = task["query"]
        task_id = task["task_id"]
        board_text = self._serialize_board_for_cu(snapshot)

        # Collect one answer per agent identity (bare LiteLLM calls)
        answers: list[tuple[str, str]] = []
        tasks = []

        for actor, _ in self.roster.all_actors():
            tasks.append(self._sole_answer(actor, query, board_text, task_id))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for (actor, _), result in zip(self.roster.all_actors(), results, strict=False):
            if isinstance(result, str) and result.strip():
                answers.append((actor, result.strip()))
            elif isinstance(result, Exception):
                logger.warning("SolE answer failed for %s: %s", actor, result)

        if not answers:
            return self._best_finding(snapshot)

        evidence = [
            (entry.body, float(entry.confidence), float(entry.salience))
            for entry in snapshot.values()
            if entry.status == "open"
            and entry.type in ("finding", "rebuttal", "artifact")
        ]
        winner = sole_evidence_vote(
            answers,
            evidence,
            self.sole_similarity,
        )
        return winner

    async def _sole_answer(
        self, actor: str, query: str, board_text: str, task_id: str | None = None,
    ) -> str:
        """One bare LiteLLM call per agent for SolE answer collection."""
        from models.personas import SOLE_SYSTEM_PROMPT

        sole_model = self._resolve_model(self.model_routing.get("light", "medium"))
        try:
            resp = await self.http.post(
                f"{self.litellm_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.litellm_key}"},
                json={
                    "model": sole_model,
                    "messages": [
                        {"role": "system", "content": SOLE_SYSTEM_PROMPT},
                        {"role": "user", "content": (
                            f"Objective: {query}\n\n"
                            f"Board state:\n{board_text}\n\n"
                            f"Your role: {actor}\n"
                            f"Provide your answer:"
                        )},
                    ],
                    **completion_parameters(
                        profile_for_alias(sole_model), output_tokens=512,
                        temperature=0.1, reasoning="low",
                    ),
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            resp_json = resp.json()
            # Capture control-plane LLM usage/cost (doc 06 §3.1)
            await self._record_llm_cost(
                task_id, resp_json.get("usage"), sole_model, "control_plane:sole",
            )
            cut = truncated(resp_json)
            if cut is not None:
                logger.warning(
                    "SolE reply for %s truncated at %s tokens (%s reasoning)",
                    actor, cut["completion_tokens"], cut["reasoning_tokens"],
                )
            return message_content(resp_json)
        except Exception as e:
            raise RuntimeError(f"SolE call failed for {actor}: {e}") from e

    def _best_finding(self, snapshot: dict[str, BoardEntry]) -> str:
        """Last-resort: return the highest-salience finding."""
        findings = [
            e for e in snapshot.values()
            if e.type in ("finding", "solution") and e.status == "open"
        ]
        if not findings:
            return "No answer could be determined."
        findings.sort(key=lambda e: e.salience, reverse=True)
        return findings[0].body

    # ── Guard Helpers ────────────────────────────────────────────────

    def _accepted_solution(
        self,
        snapshot: dict[str, BoardEntry],
        current_round: int | None = None,
        reviewed_solution_id: str | None = None,
        require_review: bool = False,
    ) -> BoardEntry | None:
        """Find an accepted solution (survived one round without critique).

        A solution is 'accepted' if no open critique referencing it was
        posted in the same round (doc 05 §3).
        """
        solutions = [
            e for e in snapshot.values()
            if e.type == "solution" and e.status == "open"
        ]
        if not solutions:
            return None

        for sol in sorted(solutions, key=lambda e: e.round, reverse=True):
            if require_review and sol.id != reviewed_solution_id:
                continue
            # Check if any open critique references this solution
            contested = any(
                e.type == "critique"
                and e.status == "open"
                and sol.id in e.refs
                and (current_round is None or e.round >= sol.round)
                for e in snapshot.values()
            )
            if not contested:
                return sol
        return None

    async def mark_solution_reviewed(
        self,
        task_id: str,
        committed_critiques: list[BoardEntry],
    ) -> str | None:
        """Record a review only when a committed critique names the solution."""
        snapshot = await self.store.get_snapshot(task_id)
        candidates = sorted(
            (
                entry for entry in snapshot.values()
                if entry.type == "solution" and entry.status == "open"
            ),
            key=lambda entry: (entry.round, entry.id),
            reverse=True,
        )
        if not candidates:
            return None
        solution_id = candidates[0].id
        if not any(
            entry.type == "critique"
            and entry.status == "superseded"
            and entry.title == "Verification passed"
            and solution_id in entry.refs
            for entry in committed_critiques
        ):
            return None
        await self.gateway.set_meta(
            task_id,
            solution_reviewed_id=solution_id,
        )
        return solution_id

    def _is_stalled(
        self,
        snapshot: dict[str, BoardEntry],
        current_round: int,
    ) -> bool:
        """Check if the board is stalled (doc 05 §5).

        Stall = rounds with no accepted entries, exact-duplicate bodies, or
        paraphrased near-duplicates of a recent round (token-set overlap).
        """
        # Get entries from the previous round
        prev_round = current_round - 1
        prev_entries = [
            e for e in snapshot.values()
            if e.round == prev_round and e.status == "open"
        ]

        if not prev_entries:
            # No entries produced last round
            self._stall_counter += 1
            return self._stall_counter >= self.stall_rounds

        # Exact repetition: normalized hash of the round's bodies.
        round_hash = _entries_hash(prev_entries)
        round_tokens = _round_token_set(prev_entries)
        if round_hash in self._round_hashes:
            self._stall_counter += 1
        elif any(
            _token_jaccard(round_tokens, seen) >= STALL_SIMILARITY
            for seen in self._round_token_sets[-STALL_HISTORY_ROUNDS:]
        ):
            # Paraphrased repetition: the round restates recent content in
            # new words without adding new information.
            self._stall_counter += 1
        elif self.require_evidence and _round_lacks_evidence(prev_entries):
            # Novel words without external grounding: at evidence-gated
            # effort levels an unsourced contribution round is not progress.
            self._stall_counter += 1
            self._round_hashes.append(round_hash)
        else:
            self._stall_counter = 0
            self._round_hashes.append(round_hash)
        if (
            not self._round_token_sets
            or round_tokens != self._round_token_sets[-1]
        ):
            self._round_token_sets.append(round_tokens)
            del self._round_token_sets[:-STALL_HISTORY_ROUNDS]

        return self._stall_counter >= self.stall_rounds

    def _revision_headroom(self, meta: dict[str, Any]) -> bool:
        """True when budget and wall clock allow one revision round."""
        budget_spent = float(meta.get("budget_spent", 0.0))
        if self.budget_ceiling > 0 and budget_spent >= self.budget_ceiling:
            return False
        elapsed = time.monotonic() - self.genesis_time
        return elapsed < self.max_duration_s - 30.0

    def note_turn_duration(self, duration_ms: Any) -> None:
        """Record one completed turn's wall-clock duration."""
        if not isinstance(duration_ms, (int, float)) or duration_ms <= 0:
            return
        self._turn_durations.append(float(duration_ms) / 1000.0)
        del self._turn_durations[:-TURN_DURATION_HISTORY]

    def _avg_turn_s(self) -> float:
        if not self._turn_durations:
            return 0.0
        return sum(self._turn_durations) / len(self._turn_durations)

    def closing_turn_timeout_s(self) -> int:
        """Timeout floor for closing-sequence turns (decider, grace)."""
        return int(min(
            CLOSING_TURN_TIMEOUT_CAP_S,
            max(CLOSING_TURN_TIMEOUT_FLOOR_S, 2.0 * self._avg_turn_s()),
        ))

    def _current_duration_reserve_s(self) -> float:
        """Duration reserve scaled to observed turn latency.

        The static reserve assumes fast API models. On a slow local tier
        one turn can outlast the whole reserve, so the guard must fire
        early enough that the closing sequence starts before the cap.
        """
        adaptive = 2.0 * self._avg_turn_s()
        return min(
            max(self._duration_reserve_s, adaptive),
            0.4 * self.max_duration_s,
        )

    # ── Private Sub-board Conflict Resolution (doc 05 §4) ────────────

    async def handle_conflict_resolution(
        self,
        task: dict,
        conflict_entry: BoardEntry,
        dispatch_fn: Any,
    ) -> list:
        """Run private sub-board conflict resolution.

        When the CU selects conflict_resolver and open conflict entries
        exist, the conflicting agents debate privately for ≤2 rounds,
        then their reconciled positions are posted to the public board.

        The private space is archived after resolution.
        """
        task_id = task["task_id"]
        conflict_id = conflict_entry.id
        space = f"private:conflict-{conflict_id}"

        # 1. Identify conflicting authors from refs
        conflicting_authors: set[str] = set()
        snapshot = await self.store.get_snapshot(task_id)
        for ref_id in conflict_entry.refs:
            ref_entry = snapshot.get(ref_id)
            if ref_entry:
                conflicting_authors.add(ref_entry.author)

        if len(conflicting_authors) < 2:
            logger.warning(
                "Conflict %s has fewer than 2 authors — skipping private resolution",
                conflict_id,
            )
            return []

        logger.info(
            "Private conflict resolution | conflict=%s authors=%s space=%s",
            conflict_id, conflicting_authors, space,
        )

        # 2. Seed the private board with the public conflict context. Private
        # actors otherwise receive an empty board and cannot evaluate the
        # disagreement that selected them.
        context_lines = [
            f"Conflict {conflict_id}: {conflict_entry.body}",
            "",
            "Referenced public entries:",
        ]
        for ref_id in conflict_entry.refs:
            ref_entry = snapshot.get(ref_id)
            if ref_entry:
                context_lines.append(
                    f"[{ref_id}] {ref_entry.author}: {ref_entry.body[:1500]}"
                )
        context_body = "\n".join(context_lines)[:7500]
        seed_turn_id = f"conflict-seed-{conflict_id}"
        await self.gateway.append(
            task_id,
            "control_unit",
            ["post:finding"],
            [{
                "type": "finding",
                "title": "Private conflict context",
                "body": context_body,
                "refs": [conflict_id, *conflict_entry.refs],
                "confidence": 1.0,
                "_mutation_id": f"{seed_turn_id}:0",
            }],
            turn_id=seed_turn_id,
            round_no=int((await self.store.get_meta(task_id)).get("round", 0)),
            space=space,
        )

        # 3. Run ≤2 private rounds
        committed_entries: list = []
        for private_round in range(1, 3):
            for author in sorted(conflicting_authors):
                # Build activation for this author
                base_role = author.split(".")[0] if "." in author else author
                activations = self._to_activations([author])
                if not activations:
                    continue

                activation = activations[0]
                private_turn_id = "activation-" + uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"bmas:{task_id}:{conflict_id}:{private_round}:{author}",
                ).hex
                activation.activation_id = private_turn_id

                # Dispatch turn with private space context
                try:
                    result = await dispatch_fn(
                        variant=self,
                        task=task,
                        activation=activation,
                        round_no=private_round,
                        space=space,
                        apply_to_board=False,
                    )

                    # Parse and apply entries to private space
                    if (
                        isinstance(result, dict)
                        and result.get("status") not in ("failed", "timeout")
                    ):
                        entries = self.parse_agent_response(
                            task, author, result,
                        )
                        for entry_index, entry_data in enumerate(entries):
                            entry_data = dict(entry_data)
                            entry_data["space"] = space
                            entry_data["_mutation_id"] = (
                                f"{private_turn_id}:{entry_index}"
                            )
                            caps = capabilities_for_role(base_role)
                            if not caps and author.startswith("expert."):
                                caps = ["finding_writer"]
                            applied = await self.gateway.append(
                                task_id, author, caps,
                                [entry_data],
                                turn_id=private_turn_id,
                                round_no=private_round,
                                space=space,
                            )
                            committed_entries.extend(applied)
                except Exception as e:
                    logger.warning(
                        "Private turn failed for %s in conflict %s: %s",
                        author, conflict_id, e,
                    )

        # 4. Archive the private space before publication. The public append
        # then persists a snapshot that does not contain private debate entries.
        try:
            await self.gateway.archive_space(
                task_id,
                space,
                mutation_id=f"conflict-archive:{conflict_id}",
            )
        except Exception as e:
            logger.warning(
                "Failed to archive private space %s: %s", space, e,
            )

        # 5. Publish validated private conclusions back to the public board.
        # Private entry ids disappear during archive, so retain only public refs.
        public_entries: list[BoardEntry] = []
        public_snapshot = await self.store.get_snapshot(task_id)
        public_ids = set(public_snapshot)
        public_round = int((await self.store.get_meta(task_id)).get("round", 0))
        for private_entry in committed_entries:
            author = private_entry.author
            base_role = author.split(".")[0] if "." in author else author
            caps = capabilities_for_role(base_role)
            if not caps and author.startswith("expert."):
                caps = ["finding_writer"]

            refs = [ref for ref in private_entry.refs if ref in public_ids]
            if conflict_id not in refs:
                refs.append(conflict_id)
            applied = await self.gateway.append(
                task_id,
                author,
                caps,
                [{
                    "type": private_entry.type,
                    "title": private_entry.title,
                    "body": private_entry.body,
                    "refs": refs,
                    "confidence": private_entry.confidence,
                    "_mutation_id": (
                        f"conflict-public:{conflict_id}:"
                        f"{private_entry.created_by_turn}:{private_entry.id}"
                    ),
                }],
                turn_id=private_entry.created_by_turn
                or f"conflict-public:{conflict_id}",
                round_no=public_round,
                space="public",
            )
            public_entries.extend(applied)

        if not public_entries:
            logger.warning(
                "Conflict %s produced no valid public conclusions; keeping public entries open",
                conflict_id,
            )
            return []

        # 6. Mark original conflicting entries as superseded only after a
        # replacement reached the public board.
        for ref_id in conflict_entry.refs:
            with contextlib.suppress(Exception):
                await self.gateway.set_status(
                    task_id, ref_id, "superseded", "conflict_resolver",
                )

        # 7. Mark the conflict entry itself as superseded.
        with contextlib.suppress(Exception):
            await self.gateway.set_status(
                task_id, conflict_id, "superseded", "conflict_resolver",
            )

        return public_entries

    # ── HITL: Directive Injection (doc 05 §6) ────────────────────────

    async def inject_directives(self, task_id: str) -> int:
        """Inject operator directives as board entries.

        Reads from the Redis hint queue `bmas:public:hints:{task_id}`,
        converts each hint to a `directive` entry (author: "operator"),
        and clears the queue.

        Returns the number of directives injected.
        """
        if not self.emitter:
            return 0

        try:
            # The emitter wraps a Redis client — access it for hint reads
            redis = getattr(self.emitter, '_redis', None)
            if redis is None:
                return 0

            hint_key = f"bmas:public:hints:{task_id}"
            async with redis.pipeline(transaction=True) as pipeline:
                pipeline.lrange(hint_key, 0, -1)
                pipeline.delete(hint_key)
                hints, _ = await pipeline.execute()
            if not hints:
                return 0

            # Inject each hint as a directive entry
            count = 0
            for raw_hint in hints:
                hint_text = raw_hint if isinstance(raw_hint, str) else raw_hint.decode("utf-8")
                entry_data = {
                    "type": "directive",
                    "title": "Operator directive",
                    "body": hint_text,
                    "confidence": 1.0,
                }
                try:
                    await self.gateway.append(
                        task_id, "operator",
                        ["decision_writer"],  # operator has full capabilities
                        [entry_data],
                        turn_id=f"directive-{uuid.uuid4().hex[:8]}",
                        round_no=0,
                    )
                    count += 1
                except Exception as e:
                    logger.warning(
                        "Failed to inject directive for task %s: %s",
                        task_id, e,
                    )

            logger.info(
                "Injected %d operator directives for task %s", count, task_id,
            )
            return count
        except Exception as e:
            logger.warning(
                "Directive injection failed for task %s: %s", task_id, e,
            )
            return 0

    # ── HITL: Pause-at-round-boundary (doc 05 §6) ────────────────────

    async def check_pause(self, task_id: str) -> bool:
        """Check if the operator has paused this task.

        If paused, emits a 'paused' SSE event and waits until the
        flag is cleared (poll every 2s, bounded by max_duration_s).
        Emits 'resumed' when unpaused.

        Returns True if the task was paused (and has now resumed).
        """
        if not self.emitter:
            return False

        try:
            redis = getattr(self.emitter, '_redis', None)
            if redis is None:
                return False

            pause_key = f"bmas:public:pause:{task_id}"
            paused = await redis.get(pause_key)
            if not paused:
                return False

            # Task is paused
            self._paused = True
            await self.emitter.emit(task_id, "paused", {
                "message": "Task paused by operator",
            })
            logger.info("Task %s paused by operator", task_id)

            # Poll until unpaused or timeout
            while True:
                await asyncio.sleep(2.0)
                elapsed = time.monotonic() - self.genesis_time
                if elapsed >= self.max_duration_s:
                    logger.warning(
                        "Task %s hit duration cap while paused — resuming",
                        task_id,
                    )
                    break

                abort_key = f"bmas:public:abort:{task_id}"
                abort_reason = await redis.get(abort_key)
                if abort_reason:
                    await redis.delete(abort_key)
                    raise RuntimeError("Task aborted by operator while paused")

                still_paused = await redis.get(pause_key)
                if not still_paused:
                    break

            self._paused = False
            await self.emitter.emit(task_id, "resumed", {
                "message": "Task resumed",
            })
            logger.info("Task %s resumed", task_id)
            return True

        except RuntimeError:
            self._paused = False
            raise
        except Exception as e:
            logger.warning(
                "Pause check failed for task %s: %s", task_id, e,
            )
            self._paused = False
            return False

    # ── Phase 5: Budget Event Emission ───────────────────────────────

    async def emit_budget_event(self, task_id: str) -> None:
        """Emit a budget SSE event with current spend vs ceiling.

        Called after each round so the frontend budget gauge can update.
        """
        if not self.emitter:
            return
        # Budget events are best-effort
        with contextlib.suppress(Exception):
            await self.emitter.emit(task_id, "budget", {
                "spent": round(self.budget_spent, 6),
                "ceiling": self.budget_ceiling,
                "percentage": round(
                    (self.budget_spent / self.budget_ceiling * 100)
                    if self.budget_ceiling > 0 else 0.0,
                    1,
                ),
            })

    # ── Phase 5: Stateful Turn Helpers (doc 12 §5.2) ─────────────────

    def get_response_id(self, actor: str) -> str | None:
        """Get the last response_id for an actor (cross-round memory)."""
        return self._response_ids.get(actor)

    def set_response_id(self, actor: str, response_id: str) -> None:
        """Store the response_id from an actor's latest turn."""
        self._response_ids[actor] = response_id

    def clear_response_id(self, actor: str) -> None:
        """Drop stateful response context after a safe endpoint failover."""
        self._response_ids.pop(actor, None)

    def set_actor_node(self, actor: str, endpoint: str) -> None:
        """Pin an actor to the endpoint that completed its last turn."""
        if endpoint:
            self._actor_nodes[actor] = endpoint

    # ── Node Assignment ──────────────────────────────────────────────

    def _to_activations(self, selected: list[str]) -> list[Activation]:
        """Assign selected actors to nodes (load-balanced, one-per-host)."""
        activations = []
        used_hosts: set[str] = set()

        for actor in selected:
            base_role = actor.split(".")[0] if "." in actor else actor
            # Look up in registry
            reg = self.role_registry.get(base_role, {})
            if reg.get("enabled") is False:
                logger.info("Actor %s is disabled", actor)
                continue
            profile = reg.get("profile")
            raw_endpoints = reg.get("endpoints", list(self.node_endpoints))
            endpoints: list[str] = [
                str(endpoint) for endpoint in raw_endpoints if endpoint
            ]
            if not endpoints:
                logger.warning("No endpoint is configured for actor %s", actor)
                continue

            # Expert model from roster
            model = self._resolve_model(self.model_routing.get(self._tier, "medium"))
            if actor.startswith("expert.") and self.roster:
                slug = actor.split(".", 1)[1]
                expert = next(
                    (e for e in self.roster.experts if e.slug == slug), None
                )
                if expert:
                    model = self._resolve_model(expert.model)

            # Keep stateful response IDs on the node that created them.
            pinned_endpoint = self._actor_nodes.get(actor)
            endpoint = (
                pinned_endpoint
                if pinned_endpoint in endpoints
                else endpoints[0]
            )
            if pinned_endpoint not in endpoints:
                for ep in endpoints:
                    if ep not in used_hosts:
                        endpoint = ep
                        break
                self._actor_nodes[actor] = endpoint
            used_hosts.add(endpoint)

            activations.append(Activation(
                actor=actor,
                role=base_role,
                model=model,
                node_endpoint=endpoint,
                profile=profile,
            ))

        return activations

    def _normalize_selection(self, selected: list[str]) -> list[str]:
        """Remove unknown, disabled, and duplicate actor selections."""
        valid_names = (
            set(self.roster.actor_names())
            if self.roster
            else set(CONSTANT_ROLE_DESCRIPTIONS)
        )
        normalized: list[str] = []
        seen: set[str] = set()
        for actor in selected:
            if actor in seen or actor not in valid_names:
                continue
            base_role = actor.split(".", 1)[0]
            if self.role_registry.get(base_role, {}).get("enabled") is False:
                continue
            seen.add(actor)
            normalized.append(actor)
        return normalized

    # ── Phase Inference ──────────────────────────────────────────────

    def _infer_phase(
        self, snapshot: dict[str, BoardEntry], current_round: int,
    ) -> str:
        """Infer the board phase from entry composition.

        Phases:
          Discovery   — round 1, board has only objective / plan entries.
          Debate      — at least one open critique has NOT yet been addressed
                        (no other open entry references it).
          Convergence — a solution exists, OR all open critiques have been
                        addressed by at least one referencing entry (rebuttal,
                        finding, or otherwise) — board is ready for the decider.
        """
        open_entries = [e for e in snapshot.values() if e.status == "open"]

        has_solutions = any(e.type == "solution" for e in open_entries)
        if has_solutions:
            return "Convergence"

        critiques = [e for e in open_entries if e.type == "critique"]

        if critiques:
            # Collect all entry IDs that other open entries reference.
            # A critique is "addressed" when at least one non-critique open
            # entry (e.g. rebuttal, finding) lists that critique's id in refs.
            addressed_ids: set[str] = set()
            for e in open_entries:
                if e.type != "critique":
                    addressed_ids.update(e.refs)

            unaddressed = [c for c in critiques if c.id not in addressed_ids]
            if unaddressed:
                return "Debate"
            # All critiques have been responded to — board is converging.
            return "Convergence"

        if current_round <= 1:
            return "Discovery"
        return "Debate"


    # ── Board Serialization ──────────────────────────────────────────

    def _serialize_board(
        self,
        board: dict[str, BoardEntry] | dict[str, Any],
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Build a bounded role-specific view over the classic board."""
        if not board:
            return {
                "mode": "bounded",
                "entries": [],
                "omitted_count": 0,
                "estimated_tokens": 0,
            }

        entries: list[dict[str, Any]] = []
        if isinstance(board, dict):
            for entry in board.values():
                if isinstance(entry, BoardEntry):
                    if entry.status != "removed":
                        entries.append(entry_to_dict(entry))
                elif (
                    isinstance(entry, dict)
                    and entry.get("status") != "removed"
                ):
                    entries.append(dict(entry))

        relevant_types = {
            "planner": {"objective", "directive", "ledger", "plan", "finding", "critique", "conflict"},
            "critic": {"objective", "directive", "ledger", "plan", "finding", "solution", "conflict"},
            "decider": {"objective", "directive", "ledger", "plan", "finding", "critique", "conflict", "solution"},
            "conflict_resolver": {"objective", "directive", "ledger", "finding", "critique", "conflict", "solution"},
        }
        base_role = (actor or "").split(".", 1)[0]
        preferred = relevant_types.get(base_role)

        pinned_types = {"objective", "directive", "ledger"}
        pinned = [entry for entry in entries if entry.get("type") in pinned_types]
        candidates = [entry for entry in entries if entry not in pinned]
        referenced_ids = {
            str(ref)
            for entry in entries
            if entry.get("status", "open") == "open"
            for ref in entry.get("refs", [])
        }

        def _priority(entry: dict[str, Any]) -> tuple:
            entry_type = str(entry.get("type", ""))
            role_relevant = 1 if preferred is None or entry_type in preferred else 0
            referenced = 1 if str(entry.get("id", "")) in referenced_ids else 0
            open_status = 1 if entry.get("status", "open") == "open" else 0
            return (
                open_status,
                referenced,
                role_relevant,
                float(entry.get("salience", 0.0) or 0.0),
                float(entry.get("confidence", 0.0) or 0.0),
                int(entry.get("round", 0) or 0),
                str(entry.get("id", "")),
            )

        candidates.sort(key=_priority, reverse=True)
        pinned.sort(key=lambda entry: (
            entry.get("type") != "directive",
            int(entry.get("round", 0) or 0),
            str(entry.get("id", "")),
        ))

        selected: list[dict[str, Any]] = []
        used_tokens = 0
        budget = self.view_budget_tokens
        index_share = 0.40 if base_role == "decider" else 0.20
        index_budget = max(64, int(budget * index_share))
        entry_budget = max(1, budget - index_budget)
        for entry in [*pinned, *candidates]:
            remaining = entry_budget - used_tokens
            if remaining <= 32:
                break
            item = dict(entry)
            body = str(item.get("body", ""))
            overhead_chars = len(json.dumps({**item, "body": ""}, default=str))
            overhead_tokens = max(1, overhead_chars // 4)
            if overhead_tokens >= remaining:
                if item.get("type") not in pinned_types:
                    continue
                item = {
                    "id": item.get("id"),
                    "type": item.get("type"),
                    "title": str(item.get("title") or "")[:200],
                    "body": "",
                    "status": item.get("status", "open"),
                    "context_truncated": True,
                }
                item_tokens = max(
                    1, (len(json.dumps(item, default=str)) + 3) // 4,
                )
                if item_tokens > remaining:
                    continue
            else:
                body_chars = max(0, (remaining - overhead_tokens) * 4)
                if len(body) > body_chars:
                    item["body"] = body[:body_chars]
                    item["context_truncated"] = True
                item_tokens = overhead_tokens + max(
                    1, (len(str(item.get("body", ""))) + 3) // 4,
                )
                if item_tokens > remaining:
                    continue
            selected.append(item)
            used_tokens += item_tokens

        selected_ids = {str(entry.get("id", "")) for entry in selected}
        omitted_ids = [
            str(entry.get("id", ""))
            for entry in entries
            if str(entry.get("id", "")) not in selected_ids
        ]
        omitted = [
            entry
            for entry in entries
            if str(entry.get("id", "")) not in selected_ids
        ]
        omitted.sort(key=_priority, reverse=True)
        omitted_index: list[dict[str, Any]] = []
        index_tokens = 0
        excerpt_chars = 160 if base_role == "decider" else 80
        available_index_tokens = min(index_budget, budget - used_tokens)
        for entry in omitted:
            compact = {
                "id": entry.get("id"),
                "type": entry.get("type"),
                "title": str(entry.get("title") or "")[:120],
                "author": entry.get("author"),
                "round": int(entry.get("round", 0) or 0),
                "status": entry.get("status", "open"),
                "refs": list(entry.get("refs") or [])[:8],
                "salience": round(float(entry.get("salience", 0.0) or 0.0), 3),
                "body_excerpt": str(entry.get("body") or "")[:excerpt_chars],
            }
            item_tokens = max(
                1, (len(json.dumps(compact, default=str)) + 3) // 4,
            )
            remaining = available_index_tokens - index_tokens
            while item_tokens > remaining and compact["body_excerpt"]:
                compact["body_excerpt"] = compact["body_excerpt"][:
                    len(compact["body_excerpt"]) // 2
                ]
                item_tokens = max(
                    1, (len(json.dumps(compact, default=str)) + 3) // 4,
                )
            while item_tokens > remaining and compact["refs"]:
                compact["refs"] = compact["refs"][:-1]
                item_tokens = max(
                    1, (len(json.dumps(compact, default=str)) + 3) // 4,
                )
            if item_tokens > remaining:
                continue
            omitted_index.append(compact)
            index_tokens += item_tokens

        return {
            "mode": "bounded",
            "entries": selected,
            "omitted_count": len(omitted_ids),
            "omitted_ids": [item["id"] for item in omitted_index],
            "omitted_index": omitted_index,
            "omitted_index_count": len(omitted_index),
            "omitted_index_truncated": len(omitted_index) < len(omitted),
            "estimated_tokens": used_tokens + index_tokens,
            "entry_estimated_tokens": used_tokens,
            "index_estimated_tokens": index_tokens,
            "index_token_budget": index_budget,
            "token_budget": budget,
        }

    def _serialize_board_for_cu(
        self, snapshot: dict[str, BoardEntry],
    ) -> str:
        """Serialize board to a compact text format for the CU prompt."""
        if not snapshot:
            return "(empty board)"

        lines = []
        used_tokens = 0
        cu_budget = min(4000, self.view_budget_tokens)
        for entry in sorted(
            snapshot.values(),
            key=lambda e: (
                e.type in ("objective", "directive", "ledger"),
                e.status == "open",
                e.salience,
                e.round,
                e.id,
            ),
            reverse=True,
        ):
            if entry.status == "removed":
                continue
            refs_str = f" refs=[{','.join(entry.refs)}]" if entry.refs else ""
            conf_str = f" conf={entry.confidence:.1f}" if entry.confidence else ""
            summary = (
                entry.body[:240]
                if entry.type == "directive"
                else entry.title or entry.body[:240]
            )
            line = (
                f"[{entry.id}] ({entry.type}) by {entry.author} "
                f"R{entry.round}{refs_str}{conf_str}: "
                f"{summary}"
            )
            line_tokens = max(1, len(line) // 4)
            if used_tokens + line_tokens > cu_budget:
                continue
            lines.append(line)
            used_tokens += line_tokens
        return "\n".join(lines)

    # ── Edge Model Resolution ────────────────────────────────────────

    def _resolve_model(self, model: str) -> str:
        """Resolve a model alias, distributing 'local' across edge nodes.

        When `model` is the "local" sentinel, picks the next edge-node-N
        alias via round-robin so consecutive LLM calls are spread across
        all inference GPUs.  Non-local aliases pass through unchanged.
        """
        if model == "local":
            return self._resolve_edge_model()
        return model

    def _resolve_edge_model(self) -> str:
        """Round-robin across edge inference node model aliases.

        Returns "edge-node-1", "edge-node-2", ... cycling through all
        available inference nodes.  The counter persists across rounds
        within a single task so distribution is even over the task's
        lifetime, not just within a single round.
        """
        if not self._edge_models:
            return "edge-node-1"  # safety fallback
        model = self._edge_models[self._edge_rr_counter % len(self._edge_models)]
        self._edge_rr_counter += 1
        return model

    # ── Cost Tracking ────────────────────────────────────────────────

    def track_cost(self, cost_usd: float) -> None:
        """Update the running budget total."""
        self.budget_spent += cost_usd

    def reserve_activation_budgets(self, count: int) -> list[float]:
        """Split the available task budget across concurrent activations.

        Each activation receives an exclusive share instead of seeing the full
        remaining budget. The daemon reconciles actual usage after completion.
        """
        if count <= 0:
            return []
        available = max(0.0, self.budget_ceiling - self.budget_spent)
        share = available / count
        return [share for _ in range(count)]

    def _control_turn_id(self, task_id: str | None, round_no: int | None) -> str | None:
        """One synthetic turn id per control-plane call, keyed by round.

        Actor turns carry their round through the turns table; a
        control-plane call has no turn row, so its id names the round
        and a per-task sequence number, and the cost summary groups
        both kinds of spend by round.
        """
        if task_id is None or round_no is None:
            return None
        counters = getattr(self, "_control_call_sequence", None)
        if counters is None:
            counters = {}
            self._control_call_sequence = counters
        counters[task_id] = counters.get(task_id, 0) + 1
        return f"control-r{int(round_no)}-{counters[task_id]}"

    async def _record_llm_cost(
        self,
        task_id: str | None,
        usage: dict | None,
        model: str,
        phase: str,
        *,
        round_no: int | None = None,
    ) -> None:
        """Capture token usage + cost from a control-plane LiteLLM call.

        The CU/AG/SolE calls are real billable LiteLLM completions whose
        `usage` field was previously discarded — the daemon is the sole
        authority on dollar cost (doc 06 §3.1). This records a per-call
        cost entry, accumulates the running budget, and emits a `cost`
        SSE event so the live UI updates. Best-effort: never blocks the
        loop on a pricing miss or DB/SSE failure.
        """
        if not task_id or not usage or not isinstance(usage, dict):
            return

        import database as db

        # LiteLLM may report the resolved alias on the response; prefer it,
        # falling back to the alias we requested (both match MODEL_PRICING).
        resolved_model = usage.get("model") or model
        pricing = (
            self.model_pricing.get(resolved_model)
            or self.model_pricing.get(model)
            or {}
        )
        price_model = (
            resolved_model if resolved_model in self.model_pricing else model
        )

        in_tok = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        out_tok = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
        if in_tok == 0 and out_tok == 0:
            return

        cost = 0.0
        if pricing:
            cost = round(
                in_tok * float(pricing.get("input_cost_per_token", 0))
                + out_tok * float(pricing.get("output_cost_per_token", 0)),
                8,
            )
        self.budget_spent += cost

        # Keep the live checkpoint aligned with control-plane spend. This also
        # preserves AG cost before the first coordination round begins.
        if self.gateway:
            with contextlib.suppress(Exception):
                await self.gateway.set_meta(
                    task_id,
                    budget_spent=self.budget_spent,
                )

        with contextlib.suppress(Exception):
            await db.insert_cost_entry_v2(
                task_id=task_id,
                model=price_model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                phase=phase,
                node_id="control_plane",
                turn_id=self._control_turn_id(task_id, round_no),
                provider=None,
                price_source=str(pricing.get("source", "bmas.yaml")) if pricing else "missing",
                joules_estimate=0.0,
            )

        if self.emitter:
            with contextlib.suppress(Exception):
                await self.emitter.emit(task_id, "cost", {
                    "model": price_model,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "cost_usd": cost,
                    "node_id": "control_plane",
                    "phase": phase,
                    "price_source": str(pricing.get("source", "bmas.yaml")) if pricing else "missing",
                })

    # ── Cleanup ──────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close HTTP client."""
        await self.http.aclose()


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
        import re
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


# ── SolE Majority-Similarity Vote (doc 05 §3) ───────────────────────

def sole_majority_vote(
    answers: list[tuple[str, str]],
    similarity_mode: str = "auto",
) -> str:
    """Majority-similarity vote: V(a_i) = Σ_j sim(a_i, a_j), argmax V.

    Implements tiered similarity:
      - exact: normalized exact match (for short/numeric answers)
      - embedding: cosine similarity (requires LiteLLM embeddings, future)
      - auto: selects tier based on answer length

    For now, implements exact-match similarity with normalized comparison.
    """
    if not answers:
        return "No answer could be determined."

    if len(answers) == 1:
        return answers[0][1]

    # Determine similarity function
    if similarity_mode == "auto":
        avg_len = sum(len(a[1]) for a in answers) / len(answers)
        if avg_len < 100:
            sim_fn = _exact_similarity
        else:
            sim_fn = _fuzzy_similarity
    elif similarity_mode == "exact":
        sim_fn = _exact_similarity
    else:
        sim_fn = _fuzzy_similarity

    # Compute V(a_i) = Σ_j sim(a_i, a_j)
    scores: list[tuple[float, str, str]] = []
    for i, (actor_i, answer_i) in enumerate(answers):
        v = 0.0
        for j, (_actor_j, answer_j) in enumerate(answers):
            if i != j:
                v += sim_fn(answer_i, answer_j)
        scores.append((v, actor_i, answer_i))

    # argmax V
    scores.sort(key=lambda x: x[0], reverse=True)
    winner = scores[0][2]

    logger.info(
        "SolE vote: winner=%s (score=%.2f), %d answers",
        scores[0][1], scores[0][0], len(answers),
    )

    return winner


def sole_evidence_vote(
    answers: list[tuple[str, str]],
    evidence: list[tuple[str, float, float]],
    similarity_mode: str = "auto",
) -> str:
    """Select an answer by peer support and independent board evidence."""
    if not answers:
        return "No answer could be determined."
    if not evidence:
        return sole_majority_vote(answers, similarity_mode)

    if similarity_mode == "exact":
        peer_similarity = _exact_similarity
    else:
        peer_similarity = _fuzzy_similarity

    scored: list[tuple[float, str, str]] = []
    for index, (actor, answer) in enumerate(answers):
        peer_score = sum(
            peer_similarity(answer, other_answer)
            for other_index, (_other_actor, other_answer) in enumerate(answers)
            if index != other_index
        )
        evidence_score = sum(
            _evidence_similarity(answer, body)
            * max(0.0, min(2.0, confidence + salience))
            * 2.0
            for body, confidence, salience in evidence
        )
        scored.append((peer_score + evidence_score, actor, answer))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][2]


def _normalize_answer(text: str) -> str:
    """Normalize an answer for comparison."""
    import re
    # Lowercase, strip whitespace and punctuation
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _exact_similarity(a: str, b: str) -> float:
    """Exact match after normalization."""
    return 1.0 if _normalize_answer(a) == _normalize_answer(b) else 0.0


def _fuzzy_similarity(a: str, b: str) -> float:
    """Token-overlap Jaccard similarity (cheap, no LLM)."""
    tokens_a = set(_normalize_answer(a).split())
    tokens_b = set(_normalize_answer(b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union) if union else 0.0


def _evidence_similarity(answer: str, evidence: str) -> float:
    """Measure whether a concise answer appears in an evidence statement."""
    answer_tokens = set(_normalize_answer(answer).split())
    evidence_tokens = set(_normalize_answer(evidence).split())
    if not answer_tokens or not evidence_tokens:
        return 0.0
    if len(answer_tokens) <= 4 and answer_tokens.issubset(evidence_tokens):
        return 1.0
    return _fuzzy_similarity(answer, evidence)


STALL_SIMILARITY = 0.9
TURN_DURATION_HISTORY = 12
CLOSING_TURN_TIMEOUT_FLOOR_S = 120
CLOSING_TURN_TIMEOUT_CAP_S = 600
STALL_HISTORY_ROUNDS = 6
CLEANER_RECENT_ROUNDS = 2


def _round_token_set(entries: list[BoardEntry]) -> frozenset[str]:
    """Return the normalized word set of one round's open entry bodies."""
    words: set[str] = set()
    for entry in entries:
        body = (entry.body or "").lower()
        words.update(
            token for token in re.findall(r"[a-z0-9]+", body) if len(token) > 2
        )
    return frozenset(words)


def _token_jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard overlap of two word sets; 0.0 when either side is empty."""
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def _entries_hash(entries: list[BoardEntry]) -> str:
    """Hash entry bodies for near-duplicate detection."""
    bodies = sorted(e.body.strip().lower() for e in entries)
    combined = "|".join(bodies)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


_EVIDENCE_TYPES = frozenset({"finding", "rebuttal"})


def _round_lacks_evidence(entries: list[BoardEntry]) -> bool:
    """True when a round contributed findings but none carries a source."""
    contributions = [
        entry for entry in entries
        if getattr(entry, "type", None) in _EVIDENCE_TYPES
    ]
    if not contributions:
        return False
    return not any(getattr(entry, "sources", None) for entry in contributions)
