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
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx

from budget_service import BudgetError
from config_schema import DEFAULT_CONSENSUS_STRATEGY, resolve_consensus_strategy
from core.capabilities import capabilities_for_role
from core.model_parameters import message_content, truncated
from core.response_parser import parse_entries
from core.variants.classic.board_views import BoardViewPolicy
from core.variants.classic.budget import BudgetPolicy
from core.variants.classic.cleaner import CleanerPolicy
from core.variants.classic.consensus import (
    ConsensusPolicy,
    evidence_similarity,
    exact_similarity,
    fuzzy_similarity,
    normalize_answer,
    sole_evidence_vote,
    sole_majority_vote,
)
from core.variants.classic.control import (
    ControlLimits,
    ControlPolicy,
    ControlProgress,
    parse_cu_output,
)
from core.variants.classic.evidence import EvidencePolicy
from core.variants.classic.memory import ContextSessionPolicy
from core.variants.classic.prompts import PromptPolicy
from core.variants.classic.roster import (
    CONSTANT_ROLE_DESCRIPTIONS,
    FALLBACK_EXPERTS,
    AgentRoster,
    ExpertIdentity,
    RosterPolicy,
)
from core.variants.classic.scheduling import (
    DECIDER_DEFERRAL_NOTE,
    Activation,
    SchedulingPolicy,
    StepResult,
    activation_identity,
)
from core.variants.classic.termination import (
    CLOSING_TURN_TIMEOUT_CAP_S,
    CLOSING_TURN_TIMEOUT_FLOOR_S,
    STALL_HISTORY_ROUNDS,
    STALL_SIMILARITY,
    TURN_DURATION_HISTORY,
    TerminationPolicy,
    entries_hash,
    round_token_set,
    token_jaccard,
)
from core.variants.classic.verification import VerificationPolicy

if TYPE_CHECKING:
    from core.entry import BoardEntry

# The engine re-exports the data models and the parser it once defined,
# so every existing import path stays valid after the extraction.
__all__ = [
    "CONSTANT_ROLE_DESCRIPTIONS",
    "FALLBACK_EXPERTS",
    "Activation",
    "AgentRoster",
    "ExpertIdentity",
    "StepResult",
    "CLOSING_TURN_TIMEOUT_CAP_S",
    "CLOSING_TURN_TIMEOUT_FLOOR_S",
    "STALL_HISTORY_ROUNDS",
    "STALL_SIMILARITY",
    "TURN_DURATION_HISTORY",
    "TraditionalVariant",
    "parse_cu_output",
    "sole_evidence_vote",
    "sole_majority_vote",
]

logger = logging.getLogger("bmas.traditional")


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
        self.budget_policy = BudgetPolicy(
            ceiling=float(config.get("budget_ceiling_usd", 0.50)),
        )
        self.max_concurrent: int = int(config.get("max_concurrent_activations", 3))
        self.experts_per_tier: dict[str, int] = config.get(
            "experts_per_tier", {"simple": 0, "light": 1, "medium": 2, "complex": 4}
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
        # The strategy resolves through the schema vocabulary: the legacy
        # alias ``auto`` names the token-similarity strategy, and an
        # unregistered strategy fails closed instead of degrading silently.
        self.sole_similarity: str = resolve_consensus_strategy(
            config.get("sole_similarity", DEFAULT_CONSENSUS_STRATEGY)
        )
        self.view_budget_tokens: int = max(
            512, int(config.get("view_budget_tokens", 12000))
        )
        self.grace_verification: bool = bool(config.get("grace_verification", True))
        self.actor_context: str = str(config.get("actor_context", "chained"))
        self.require_evidence: bool = bool(config.get("require_evidence", False))
        self.termination_policy = TerminationPolicy()
        self.consensus_policy = ConsensusPolicy()
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

        # The policies the engine delegates to. The roster policy keeps
        # the edge round-robin state and the scheduling policy keeps the
        # actor-to-endpoint pins; the engine checkpoints both.
        self.roster_policy = RosterPolicy(
            model_routing=self.model_routing,
            model_pools=self.model_pools,
            edge_models=edge_node_models or ["edge-node-1"],
        )
        self.control_policy = ControlPolicy()
        self.scheduling_policy = SchedulingPolicy()
        self.board_view_policy = BoardViewPolicy()
        self.session_policy = ContextSessionPolicy()
        self.prompt_policy = PromptPolicy()
        self.cleaner_policy = CleanerPolicy()
        self.evidence_policy = EvidencePolicy()
        self.verification_policy = VerificationPolicy()

        # Per-task state (set during genesis)
        self.roster: AgentRoster | None = None
        self.genesis_time: float = 0.0
        self.genesis_started_at: float = 0.0
        self._replan_count: int = 0
        self._tier: str = "medium"


        # Phase 5: HITL pause flag (doc 05 §6)
        self._paused: bool = False
        self._checkpoint_lock = asyncio.Lock()

    # ── Policy state under the engine's historical names ─────────────

    @property
    def _actor_nodes(self) -> dict[str, str]:
        return self.scheduling_policy.actor_nodes

    @_actor_nodes.setter
    def _actor_nodes(self, value: dict[str, str]) -> None:
        self.scheduling_policy.actor_nodes = dict(value)

    @property
    def budget_spent(self) -> float:
        return self.budget_policy.spent

    @budget_spent.setter
    def budget_spent(self, value: float) -> None:
        self.budget_policy.spent = float(value)

    @property
    def budget_ceiling(self) -> float:
        return self.budget_policy.ceiling

    @budget_ceiling.setter
    def budget_ceiling(self, value: float) -> None:
        self.budget_policy.ceiling = float(value)

    @property
    def _stall_counter(self) -> int:
        return self.termination_policy.stall_counter

    @_stall_counter.setter
    def _stall_counter(self, value: int) -> None:
        self.termination_policy.stall_counter = int(value)

    @property
    def _round_hashes(self) -> list[str]:
        return self.termination_policy.round_hashes

    @_round_hashes.setter
    def _round_hashes(self, value: list[str]) -> None:
        self.termination_policy.round_hashes = list(value)

    @property
    def _round_token_sets(self) -> list[frozenset[str]]:
        return self.termination_policy.round_token_sets

    @_round_token_sets.setter
    def _round_token_sets(self, value: list[frozenset[str]]) -> None:
        self.termination_policy.round_token_sets = list(value)

    @property
    def _turn_durations(self) -> list[float]:
        return self.termination_policy.turn_durations

    @_turn_durations.setter
    def _turn_durations(self, value: list[float]) -> None:
        self.termination_policy.turn_durations = list(value)

    @property
    def _response_ids(self) -> dict[str, str]:
        return self.session_policy.response_ids

    @_response_ids.setter
    def _response_ids(self, value: dict[str, str]) -> None:
        self.session_policy.response_ids = dict(value)

    @property
    def _edge_models(self) -> list[str]:
        return self.roster_policy.edge_models

    @_edge_models.setter
    def _edge_models(self, value: list[str]) -> None:
        self.roster_policy.edge_models = list(value)

    @property
    def _edge_rr_counter(self) -> int:
        return self.roster_policy.edge_rotation

    @_edge_rr_counter.setter
    def _edge_rr_counter(self, value: int) -> None:
        self.roster_policy.edge_rotation = int(value)

    def _control_limits(self) -> ControlLimits:
        """The live limits of this engine for one control decision."""
        return ControlLimits(
            max_rounds=self.max_rounds,
            max_concurrent=self.max_concurrent,
            stall_rounds=self.stall_rounds,
            max_replans=self.max_replans,
            budget_ceiling=self.budget_ceiling,
            require_evidence=self.require_evidence,
            cleaner_threshold=self.cleaner_threshold,
        )

    def _control_progress(self) -> ControlProgress:
        """The live control state of this engine for one decision."""
        return ControlProgress(
            budget_spent=self.budget_spent,
            stall_counter=self._stall_counter,
            replan_count=self._replan_count,
        )

    async def _post_control_completion(
        self,
        task_id: str | None,
        body: dict[str, Any],
        phase: str,
        *,
        round_no: int | None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Post one control-plane completion and record its cost."""
        request: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {self.litellm_key}"},
            "json": body,
        }
        if timeout is not None:
            request["timeout"] = timeout
        from core.variants.classic.effects import post_completion

        resp = await post_completion(self.http, f"{self.litellm_url}/chat/completions",
                                     task_id=task_id, phase=phase, **request)
        resp.raise_for_status()
        resp_json = resp.json()
        # Capture control-plane LLM usage/cost (doc 06 §3.1)
        await self._record_llm_cost(
            task_id, resp_json.get("usage"), str(body.get("model")), phase,
            round_no=round_no,
        )
        return resp_json

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
        self.roster = self.roster_policy.build_roster(experts)

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
            roster=self.roster_policy.roster_to_metadata(self.roster),
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
        self.roster = self.roster_policy.roster_from_metadata(meta.get("roster", {}))
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
            roster=self.roster_policy.roster_to_metadata(self.roster),
        )

    async def _generate_experts(
        self, query: str, n: int, tier: str, task_id: str | None = None,
    ) -> list[ExpertIdentity]:
        """AG: one LiteLLM call to generate n expert identities (doc 05 §2.1)."""

        async def complete(body: dict[str, Any]) -> dict[str, Any]:
            return await self._post_control_completion(
                task_id, body, "control_plane:ag", round_no=0,
            )

        async def on_fallback(
            model: str, error: str, fallback_experts: list[dict[str, Any]],
        ) -> None:
            # Emit a visible ag_fallback event to the task stream so the
            # operator can diagnose why generic experts appeared in
            # Mission Control.
            if task_id and self.emitter:
                try:
                    await self.emitter.emit(task_id, "ag_fallback", {
                        "model": model,
                        "tier": tier,
                        "error": error,
                        "fallback_experts": [ex["slug"] for ex in fallback_experts],
                    })
                except Exception as emit_err:
                    logger.debug("Failed to emit ag_fallback event: %s", emit_err)

        return await self.roster_policy.generate_experts(
            query, n, tier, complete=complete, on_fallback=on_fallback,
        )

    def _default_experts(self, n: int) -> list[dict]:
        """Fallback expert definitions when the AG call fails."""
        return self.roster_policy.default_experts(n)


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
        grace = self.verification_policy.grace_plan(
            snapshot, meta,
            grace_verification=self.grace_verification,
            critic_enabled=(
                self.role_registry.get("critic", {}).get("enabled") is not False
            ),
            within_overrun=lambda: (
                time.monotonic() - self.genesis_time
                < self.max_duration_s + self.closing_turn_timeout_s()
            ),
            revision_headroom=lambda: self._revision_headroom(meta),
        )
        grace_candidate = grace.candidate
        grace_revision = grace.revision
        if meta.get("decider_forced") and grace_candidate is None and not grace_revision:
            return StepResult(
                terminal=True,
                reason=meta.get("terminal_reason", "forced_decider_finished")
            )

        # The termination policy runs the hard limits in their fixed
        # order: rounds, budget, duration, then the stall breaker. The
        # duration reserve keeps enough wall clock for the forced decider
        # and one grace verification round, scaled up when turns are slow.
        self.budget_spent = float(meta.get("budget_spent", 0.0))
        verdict = self.termination_policy.limit_verdict(
            current_round=current_round,
            max_rounds=self.max_rounds,
            budget_spent=self.budget_spent,
            budget_ceiling=self.budget_ceiling,
            elapsed_s=time.monotonic() - self.genesis_time,
            duration_limit_s=self.max_duration_s - self._current_duration_reserve_s(),
            stalled=lambda: self._is_stalled(snapshot, current_round),
            stall_counter=lambda: self._stall_counter,
            stall_rounds=self.stall_rounds,
            replan_count=self._replan_count,
            max_replans=self.max_replans,
        )
        force_decider = verdict.force_decider
        force_replan = verdict.force_replan
        term_reason = verdict.term_reason
        if force_replan:
            self._replan_count += 1

        # ── 1.5 Board Pressure Guard (Deterministic Cleaner) ─────────
        open_entries = [e for e in snapshot.values() if e.status == "open"]
        total_tokens = self.cleaner_policy.board_tokens(open_entries)
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
            rationale = self.cleaner_policy.pressure_rationale(
                total_tokens, self.cleaner_token_threshold,
            )
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

        # The scheduling policy applies the paper §3.2 guard (the decider
        # runs alone) before it clamps the plan to the concurrency limit.
        selected, decider_deferred = self.scheduling_policy.isolate_decider(selected)
        if decider_deferred:
            logger.info(
                "Decider exclusion guard | task=%s round=%d — "
                "CU co-selected decider with %s; deferring decider to next round",
                task_id, current_round, selected,
            )
            rationale = ((rationale or "") + DECIDER_DEFERRAL_NOTE).strip()
        selected = self.scheduling_policy.clamp(selected, self.max_concurrent)

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
        return activation_identity(task_id, round_no, actor, index)

    async def restore_active_round(self, task_id: str) -> StepResult | None:
        """Restore the unfinished activation plan for one classic round."""
        meta = await self.store.get_meta(task_id)
        return SchedulingPolicy.plan_from_state(meta.get("round_state"), meta)

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
        resolved = self.verification_policy.resolve_answer(
            snapshot,
            str(reviewed_solution_id) if reviewed_solution_id else None,
        )
        if resolved is not None:
            answer = resolved.answer
            answer_source = resolved.answer_source
            verification_status = resolved.verification_status
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
        task_id = task["task_id"]
        query = task["query"]

        role_prompt = self.prompt_policy.role_prompt(actor, self.roster, query)

        # Serialize board for prompt
        if actor == "cleaner":
            board_data = self.cleaner_policy.condense_view(
                board, self._get_eviction_candidates(board),
            )
        else:
            board_data = self._serialize_board(board, actor=actor)

        return self.prompt_policy.render(
            task_id=task_id,
            query=query,
            actor=actor,
            role_prompt=role_prompt,
            board_data=board_data,
            round_no=board.get("round", 0) if isinstance(board, dict) else 0,
            session=self.session_policy.session_fields(
                task_id, actor, actor_context=self.actor_context,
            ),
            budget_remaining_usd=max(0, self.budget_ceiling - self.budget_spent),
            budget_ceiling=self.budget_ceiling,
            budget_spent=self.budget_spent,
            require_evidence=self.require_evidence,
        )

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
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("```") and text.endswith("```"):
                text = "\n".join(text.splitlines()[1:-1])
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                action_payload = json.loads(text)
        if isinstance(raw, dict) and isinstance(raw.get("result"), str):
            text = raw["result"].strip()
            if text.startswith("```") and text.endswith("```"):
                text = "\n".join(text.splitlines()[1:-1])
            action_payload = None
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                decoded = json.loads(text)
                if isinstance(decoded, dict):
                    action_payload = decoded
        # The legacy pair never applies the native condensation contract.
        if any(isinstance(value, dict) and value.get("action") == "condense"
               for value in (raw, action_payload)):
            return []
        if actor.split(".", 1)[0] == "cleaner":
            # Only the frozen clean action reaches the legacy removal path.
            # Nested or contradictory wrappers cannot introduce condensation.
            if not isinstance(action_payload, dict) or action_payload.get("action") != "clean":
                return []
            if isinstance(raw, dict) and raw.get("action") not in (None, "clean"):
                return []
            if action_payload.get("entries"):
                return []
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
                proposed = self.verification_policy.approval_entry(
                    solution.id, mutation_id,
                )
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
        return self.termination_policy.is_terminal(board, self._accepted_solution)

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
        """Build the control-unit prompt through the control policy."""
        return self.control_policy.cu_prompt(
            query, board_text, roster_text, current_round, snapshot, meta,
            limits=self._control_limits(), progress=self._control_progress(),
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

        board_text = self._serialize_board_for_cu(snapshot)
        cu_model = self._resolve_model(self.model_routing.get("light", "medium"))

        async def complete(body: dict[str, Any]) -> dict[str, Any]:
            return await self._post_control_completion(
                task_id, body, "control_plane:cu", round_no=current_round, timeout=30.0,
            )

        return await self.control_policy.select(
            query, snapshot, current_round, meta,
            roster=self.roster, board_text=board_text, model=cu_model,
            limits=self._control_limits(), progress=self._control_progress(),
            complete=complete,
        )

    def _fallback_rationale(
        self,
        snapshot: dict[str, BoardEntry],
        current_round: int,
        selected: list[str],
    ) -> str:
        """Explain a heuristic selection through the control policy."""
        return self.control_policy.fallback_rationale(snapshot, current_round, selected)

    def _get_eviction_candidates(
        self,
        snapshot: dict[str, BoardEntry] | dict[str, Any],
        max_candidates: int = 12,
    ) -> list[BoardEntry]:
        """Return the bottom N eviction candidates through the cleaner policy."""
        return self.cleaner_policy.eviction_candidates(
            snapshot,
            retention_weights=self.cleaner_retention_weights,
            max_candidates=max_candidates,
        )

    def _deterministic_fallback(
        self,
        snapshot: dict[str, BoardEntry],
        current_round: int,
    ) -> list[str]:
        """Deterministic fallback policy (doc 05 §1.1) through the control policy."""
        return self.control_policy.deterministic_fallback(
            snapshot, current_round, self.roster,
            cleaner_threshold=self.cleaner_threshold,
        )

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

        async def answer(actor: str) -> str:
            return await self._sole_answer(actor, query, board_text, task_id)

        return await self.termination_policy.solution_extraction(
            snapshot,
            roster=self.roster,
            strategy=self.sole_similarity,
            answer=answer,
        )

    async def _sole_answer(
        self, actor: str, query: str, board_text: str, task_id: str | None = None,
    ) -> str:
        """One bare LiteLLM call per agent for SolE answer collection."""
        sole_model = self._resolve_model(self.model_routing.get("light", "medium"))
        body = self.termination_policy.sole_request(actor, query, board_text, sole_model)
        try:
            resp_json = await self._post_control_completion(
                task_id, body, "control_plane:sole", round_no=None, timeout=30.0,
            )
            cut = truncated(resp_json)
            if cut is not None:
                logger.warning(
                    "SolE reply for %s truncated at %s tokens (%s reasoning)",
                    actor, cut["completion_tokens"], cut["reasoning_tokens"],
                )
            return message_content(resp_json)
        except BudgetError:
            raise
        except Exception as e:
            raise RuntimeError(f"SolE call failed for {actor}: {e}") from e

    def _best_finding(self, snapshot: dict[str, BoardEntry]) -> str:
        """Last-resort: return the highest-salience finding."""
        return self.termination_policy.best_finding(snapshot)

    # ── Guard Helpers ────────────────────────────────────────────────

    def _accepted_solution(
        self,
        snapshot: dict[str, BoardEntry],
        current_round: int | None = None,
        reviewed_solution_id: str | None = None,
        require_review: bool = False,
    ) -> BoardEntry | None:
        """Find an accepted solution through the verification policy."""
        return self.verification_policy.accepted_solution(
            snapshot, current_round, reviewed_solution_id, require_review,
        )

    async def mark_solution_reviewed(
        self,
        task_id: str,
        committed_critiques: list[BoardEntry],
    ) -> str | None:
        """Record a review only when a committed critique names the solution."""
        snapshot = await self.store.get_snapshot(task_id)
        solution_id = self.verification_policy.reviewed_solution(
            snapshot, committed_critiques,
        )
        if solution_id is None:
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
        """Check if the board is stalled through the termination policy."""
        return self.termination_policy.is_stalled(
            snapshot, current_round,
            stall_rounds=self.stall_rounds,
            require_evidence=self.require_evidence,
            round_lacks_evidence=self.evidence_policy.round_lacks_evidence,
        )

    def _revision_headroom(self, meta: dict[str, Any]) -> bool:
        """True when budget and wall clock allow one revision round."""
        return self.termination_policy.revision_headroom(
            meta,
            budget_ceiling=self.budget_ceiling,
            elapsed_s=time.monotonic() - self.genesis_time,
            max_duration_s=self.max_duration_s,
        )

    def note_turn_duration(self, duration_ms: Any) -> None:
        """Record one completed turn's wall-clock duration."""
        self.termination_policy.note_turn_duration(duration_ms)

    def _avg_turn_s(self) -> float:
        return self.termination_policy.average_turn_s()

    def closing_turn_timeout_s(self) -> int:
        """Timeout floor for closing-sequence turns (decider, grace)."""
        return self.termination_policy.closing_turn_timeout_s()

    def _current_duration_reserve_s(self) -> float:
        """Duration reserve scaled to observed turn latency."""
        return self.termination_policy.duration_reserve_s(
            self._duration_reserve_s, self.max_duration_s,
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
                except BudgetError:
                    raise
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
                    "sources": list(private_entry.sources),
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
            await self.emitter.emit(task_id, "budget", self.budget_policy.budget_event())

    # ── Phase 5: Stateful Turn Helpers (doc 12 §5.2) ─────────────────

    def get_response_id(self, actor: str) -> str | None:
        """Get the last response_id for an actor (cross-round memory)."""
        return self.session_policy.get_response_id(actor)

    def set_response_id(self, actor: str, response_id: str) -> None:
        """Store the response_id from an actor's latest turn."""
        self.session_policy.set_response_id(actor, response_id)

    def clear_response_id(self, actor: str) -> None:
        """Drop stateful response context after a safe endpoint failover."""
        self.session_policy.clear_response_id(actor)

    def set_actor_node(self, actor: str, endpoint: str) -> None:
        """Pin an actor to the endpoint that completed its last turn."""
        self.scheduling_policy.pin_actor(actor, endpoint)

    # ── Node Assignment ──────────────────────────────────────────────

    def _to_activations(self, selected: list[str]) -> list[Activation]:
        """Assign selected actors to nodes through the scheduling policy."""
        return self.scheduling_policy.to_activations(
            selected,
            roster=self.roster,
            tier=self._tier,
            role_registry=self.role_registry,
            node_endpoints=self.node_endpoints,
            model_routing=self.model_routing,
            resolve_model=self._resolve_model,
        )

    def _normalize_selection(self, selected: list[str]) -> list[str]:
        """Remove unknown, disabled, and duplicate actor selections."""
        return self.control_policy.normalize_selection(
            selected, self.roster, self.role_registry,
        )

    # ── Phase Inference ──────────────────────────────────────────────

    def _infer_phase(
        self, snapshot: dict[str, BoardEntry], current_round: int,
    ) -> str:
        """Infer the board phase through the scheduling policy."""
        return self.scheduling_policy.infer_phase(snapshot, current_round)


    # ── Board Serialization ──────────────────────────────────────────

    def _serialize_board(
        self,
        board: dict[str, BoardEntry] | dict[str, Any],
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Build a bounded role-specific view through the board view policy."""
        return self.board_view_policy.serialize_board(
            board, actor, view_budget_tokens=self.view_budget_tokens,
        )

    def _serialize_board_for_cu(
        self, snapshot: dict[str, BoardEntry],
    ) -> str:
        """Serialize the board for the CU prompt through the board view policy."""
        return self.board_view_policy.serialize_for_cu(
            snapshot, view_budget_tokens=self.view_budget_tokens,
        )

    # ── Edge Model Resolution ────────────────────────────────────────

    def _resolve_model(self, model: str) -> str:
        """Resolve a model alias through the roster policy."""
        return self.roster_policy.resolve_model(model)

    def _resolve_edge_model(self) -> str:
        """Round-robin across edge inference node model aliases."""
        return self.roster_policy.resolve_edge_model()

    # ── Cost Tracking ────────────────────────────────────────────────

    def track_cost(self, cost_usd: float) -> None:
        """Update the running budget total."""
        self.budget_policy.track_cost(cost_usd)

    def reserve_activation_budgets(self, count: int) -> list[float]:
        """Split the available task budget across concurrent activations."""
        return self.budget_policy.reserve_activation_budgets(count)

    def _control_turn_id(self, task_id: str | None, round_no: int | None) -> str | None:
        """One synthetic turn id per control-plane call, keyed by round."""
        return self.budget_policy.control_turn_id(task_id, round_no)

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
        authority on dollar cost (doc 06 §3.1). The budget policy prices
        the usage; this method records the cost entry, accumulates the
        running budget, and emits a `cost` SSE event so the live UI
        updates. Best-effort: never blocks the loop on a pricing miss or
        DB/SSE failure.
        """
        if not task_id:
            return
        record = self.budget_policy.price_usage(usage, model, self.model_pricing)
        if record is None:
            return

        import database as db

        self.budget_policy.track_cost(record.cost_usd)

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
                model=record.model,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                cost_usd=record.cost_usd,
                phase=phase,
                node_id="control_plane",
                turn_id=self._control_turn_id(task_id, round_no),
                provider=None,
                price_source=record.price_source,
                joules_estimate=0.0,
            )

        if self.emitter:
            with contextlib.suppress(Exception):
                await self.emitter.emit(task_id, "cost", {
                    "model": record.model,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                    "cost_usd": record.cost_usd,
                    "node_id": "control_plane",
                    "phase": phase,
                    "price_source": record.price_source,
                })

    # ── Cleanup ──────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close HTTP client."""
        await self.http.aclose()


# ── The helpers the engine once defined, under their historical names ─

_normalize_answer = normalize_answer
_exact_similarity = exact_similarity
_fuzzy_similarity = fuzzy_similarity
_evidence_similarity = evidence_similarity
_round_token_set = round_token_set
_token_jaccard = token_jaccard
_entries_hash = entries_hash

# The evidence rule the engine once defined, under its historical name.
_round_lacks_evidence = EvidencePolicy.round_lacks_evidence
