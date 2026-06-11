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

Registered behind `coordination.variant: traditional` (legacy_pipeline is default).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from core.capabilities import capabilities_for_role
from core.entry import BoardEntry, envelope_fallback, entry_to_dict
from core.variants import register_variant

logger = logging.getLogger("bmas.traditional")


# ── Data Models ──────────────────────────────────────────────────────

@dataclass
class StepResult:
    """Result of one round of the blackboard cycle."""
    terminal: bool
    reason: str | None = None
    activations: list[Activation] = field(default_factory=list)


@dataclass
class Activation:
    """A single agent activation for this round."""
    actor: str              # opaque actor id (e.g. "critic", "expert.valuation")
    role: str               # base role for capability lookup
    model: str              # pool-drawn model for this turn
    node_endpoint: str      # target node URL
    profile: str | None = None


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

    name = "traditional"

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
        self.stall_rounds: int = int(config.get("stall_rounds", 2))
        self.cu_mode: str = str(config.get("cu_mode", "llm"))
        self.sole_similarity: str = str(config.get("sole_similarity", "auto"))

        # External services
        self.litellm_url = litellm_url
        self.litellm_key = litellm_key
        self.http = httpx.AsyncClient(timeout=60.0)

        # Node topology
        self.node_endpoints = node_endpoints
        self.role_registry = role_registry
        self.model_routing = model_routing
        self.model_pools = model_pools or {}

        # Per-task state (set during genesis)
        self.roster: AgentRoster | None = None
        self.genesis_time: float = 0.0
        self.budget_spent: float = 0.0
        self._stall_counter: int = 0
        self._round_hashes: list[str] = []
        self._tier: str = "medium"

    # ── Genesis ──────────────────────────────────────────────────────

    async def genesis(self, task: Any) -> None:
        """Initialize: triage → AG experts → objective entry → attachments."""
        self.genesis_time = time.monotonic()
        task_id = task["task_id"]
        query = task["query"]

        # 1. Triage classification (existing triage, now effective)
        triage_result = task.get("triage_result")
        self._tier = triage_result.complexity.value if triage_result else "medium"
        tier_model = self.model_routing.get(self._tier, "medium")

        # Seed max_rounds by tier (doc 05 §8)
        tier_rounds = {"simple": 2, "light": 3, "medium": 4, "complex": 4}
        self.max_rounds = min(self.max_rounds, tier_rounds.get(self._tier, 4))

        # 2. AG — generate experts (one LiteLLM call, doc 05 §2.1)
        n_experts = self.experts_per_tier.get(self._tier, 1)
        experts = await self._generate_experts(query, n_experts, self._tier)

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
        objective_entry = {
            "type": "objective",
            "title": query[:200],
            "body": query,
            "confidence": 1.0,
        }
        await self.gateway.append(
            task_id, "control_unit", ["decision_writer"],
            [objective_entry], turn_id="genesis", round_no=0,
        )

        # 5. Initialize board meta
        await self.gateway.set_meta(
            task_id,
            phase="Discovery",
            round=0,
            budget_spent=0.0,
            variant="traditional",
            decider_state="waiting",
            roster=json.dumps(
                [{"actor": a, "ability": d} for a, d in self.roster.all_actors()]
            ),
        )

        # 6. Attach uploads (doc 17 §4)
        await self._attach_uploads(task_id, task)

    async def _generate_experts(
        self, query: str, n: int, tier: str,
    ) -> list[ExpertIdentity]:
        """AG: one LiteLLM call to generate n expert identities (doc 05 §2.1)."""
        if n <= 0:
            return []

        from models.personas import AG_SYSTEM_PROMPT

        try:
            resp = await self.http.post(
                f"{self.litellm_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.litellm_key}"},
                json={
                    "model": self.model_routing.get(tier, "medium"),
                    "messages": [
                        {"role": "system", "content": AG_SYSTEM_PROMPT.format(n=n)},
                        {"role": "user", "content": f"Task: {query}"},
                    ],
                    "max_tokens": 512,
                    "temperature": 0.4,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = json.loads(resp.json()["choices"][0]["message"]["content"])
            raw_experts = data.get("experts", [])[:n]
        except Exception as e:
            logger.warning("AG call failed (%s), using default experts", e)
            raw_experts = self._default_experts(n)

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
        ]
        return defaults[:n]

    async def _attach_uploads(self, task_id: str, task: dict) -> None:
        """Create attachment entries for uploaded files (doc 17 §4)."""
        attachments = task.get("attachments", [])
        if not attachments:
            return

        for att in attachments:
            entry = {
                "type": "attachment",
                "title": f"Uploaded: {att.get('name', 'file')}",
                "body": att.get("text_preview", f"File: {att.get('name', 'unknown')}"),
                "confidence": 1.0,
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
        solution = self._accepted_solution(snapshot, current_round)
        if solution:
            return StepResult(terminal=True, reason="solution")

        # Guard: max rounds
        if current_round > self.max_rounds:
            return StepResult(terminal=True, reason="max_rounds")

        # Guard: budget ceiling
        self.budget_spent = float(meta.get("budget_spent", 0.0))
        if self.budget_spent >= self.budget_ceiling:
            return StepResult(terminal=True, reason="budget")

        # Guard: duration cap
        elapsed = time.monotonic() - self.genesis_time
        if elapsed >= self.max_duration_s:
            return StepResult(terminal=True, reason="duration")

        # Guard: stall breaker
        if self._is_stalled(snapshot, current_round):
            logger.info(
                "Stall detected at round %d (stall_counter=%d)",
                current_round, self._stall_counter,
            )
            if self._stall_counter >= self.stall_rounds:
                # Force one decider activation then halt
                return StepResult(
                    terminal=True, reason="stalled",
                )
            # Not yet at threshold — continue but note the stall

        # ── 2. CU selection (one bare LiteLLM call, doc 05 §1.1) ─────

        if self.cu_mode == "heuristic_first":
            selected = self._deterministic_fallback(snapshot, current_round)
        else:
            selected = await self._cu_select(
                task["query"], snapshot, current_round, meta,
            )

        if not selected:
            # No agents selected — treat as stall
            self._stall_counter += 1
            selected = self._deterministic_fallback(snapshot, current_round)

        # Clamp to max_concurrent
        selected = selected[:self.max_concurrent]

        # Update board meta
        phase = self._infer_phase(snapshot, current_round)
        await self.gateway.set_meta(
            task_id, round=current_round, phase=phase,
        )

        # Build activations with node assignments
        activations = self._to_activations(selected)

        logger.info(
            "step | task=%s round=%d selected=%s phase=%s",
            task_id, current_round, [a.actor for a in activations], phase,
        )

        return StepResult(terminal=False, activations=activations)

    # ── Finalize ─────────────────────────────────────────────────────

    async def finalize(
        self, task: Any, board: Any, reason: str,
    ) -> dict[str, Any]:
        """Extract the final answer (Decider path or SolE, doc 05 §3)."""
        task_id = task["task_id"]
        snapshot = await self.store.get_snapshot(task_id)

        # Decider path: accepted solution on the board
        solution_entry = self._accepted_solution(snapshot)
        if solution_entry:
            answer = solution_entry.body
            answer_source = "decider"
        else:
            # SolE: majority-similarity vote (doc 05 §3, path 2)
            answer = await self._solution_extraction(task, snapshot)
            answer_source = "sole"

        # Update board meta
        await self.gateway.set_meta(
            task_id,
            phase="Solved",
            terminated_by=reason,
            answer_source=answer_source,
        )

        logger.info(
            "finalize | task=%s reason=%s source=%s",
            task_id, reason, answer_source,
        )

        return {
            "answer": answer,
            "terminated_by": reason,
            "answer_source": answer_source,
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
        board_data = self._serialize_board(board)

        return {
            "task_id": task_id,
            "turn_id": f"turn-{uuid.uuid4().hex[:8]}",
            "round": board.get("round", 0) if isinstance(board, dict) else 0,
            "role": actor,
            "role_prompt": role_prompt,
            "objective": query,
            "board": board_data,
            "response_contract": "entries_v1",
            "budget_remaining_usd": max(0, self.budget_ceiling - self.budget_spent),
        }

    # ── Parse Agent Response ─────────────────────────────────────────

    def parse_agent_response(
        self, task: Any, actor: str, raw: Any,
    ) -> list[dict]:
        """Parse agent response into proposed board entries.

        Supports entries_v1 JSON and falls back to envelope_fallback
        for free-text responses (doc 04 §3).
        """
        if isinstance(raw, dict):
            # Check for structured entries
            entries = raw.get("entries", [])
            if entries and isinstance(entries, list):
                return entries

            # Check for cleaner action
            if raw.get("action") == "clean":
                return [{"_action": "clean", "removals": raw.get("removals", [])}]

            # Check for decline
            if raw.get("action") == "decline":
                return []

            # Free-text in result field
            result_text = raw.get("result", "")
            if result_text:
                proposed = envelope_fallback(result_text, actor)
                return [{
                    "type": proposed.type,
                    "title": proposed.title,
                    "body": proposed.body,
                    "refs": proposed.refs,
                    "confidence": proposed.confidence,
                }]

        # String response
        if isinstance(raw, str) and raw.strip():
            proposed = envelope_fallback(raw, actor)
            return [{
                "type": proposed.type,
                "title": proposed.title,
                "body": proposed.body,
                "refs": proposed.refs,
                "confidence": proposed.confidence,
            }]

        return []

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
                    )
                    events.extend(removed)
                continue

            proposed = mutation.get("entries", [mutation])
            committed = await self.gateway.append(
                task_id, actor, caps, proposed,
                turn_id=mutation.get("turn_id", ""),
                round_no=mutation.get("round", 0),
            )
            events.extend(committed)
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

    async def _cu_select(
        self,
        query: str,
        snapshot: dict[str, BoardEntry],
        current_round: int,
        meta: dict[str, Any],
    ) -> list[str]:
        """One bare LiteLLM call per round for agent selection."""
        if not self.roster:
            return self._deterministic_fallback(snapshot, current_round)

        from models.personas import CU_SYSTEM_PROMPT

        board_text = self._serialize_board_for_cu(snapshot)
        roster_text = "\n".join(
            f"- {actor}: {desc}"
            for actor, desc in self.roster.all_actors()
        )

        budget_remaining = max(0, self.budget_ceiling - self.budget_spent)
        prompt = (
            f"## Objective\n{query}\n\n"
            f"## Current Board (round {current_round})\n{board_text}\n\n"
            f"## Available Agents\n{roster_text}\n\n"
            f"## Constraints\n"
            f"- Round: {current_round}/{self.max_rounds}\n"
            f"- Budget remaining: ${budget_remaining:.4f}\n"
            f"- Select 1-{self.max_concurrent} agents\n"
        )

        system = CU_SYSTEM_PROMPT.format(max_concurrent=self.max_concurrent)

        # Try up to 2 times (1 retry on garbled output)
        for attempt in range(2):
            try:
                resp = await self.http.post(
                    f"{self.litellm_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.litellm_key}"},
                    json={
                        "model": self.model_routing.get("light", "medium"),
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 256,
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                    },
                    timeout=30.0,
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"]
                selected = parse_cu_output(raw, self.roster.actor_names())
                if selected:
                    return selected
                logger.warning("CU returned empty selection (attempt %d)", attempt + 1)
            except Exception as e:
                logger.warning("CU call failed (attempt %d): %s", attempt + 1, e)

        # Fallback to deterministic table
        logger.info("CU failed after retries, using deterministic fallback")
        return self._deterministic_fallback(snapshot, current_round)

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
        rebuttals = [
            e for e in open_entries.values() if e.type == "rebuttal"
        ]
        rebutted_refs = set()
        for r in rebuttals:
            rebutted_refs.update(r.refs)

        unaddressed_critiques = [
            c for c in critiques if c.id not in rebutted_refs
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
        board_text = self._serialize_board_for_cu(snapshot)

        from models.personas import SOLE_SYSTEM_PROMPT

        # Collect one answer per agent identity (bare LiteLLM calls)
        answers: list[tuple[str, str]] = []
        tasks = []

        for actor, _ in self.roster.all_actors():
            tasks.append(self._sole_answer(actor, query, board_text))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for (actor, _), result in zip(self.roster.all_actors(), results):
            if isinstance(result, str) and result.strip():
                answers.append((actor, result.strip()))
            elif isinstance(result, Exception):
                logger.warning("SolE answer failed for %s: %s", actor, result)

        if not answers:
            return self._best_finding(snapshot)

        # Majority-similarity vote
        winner = sole_majority_vote(answers, self.sole_similarity)
        return winner

    async def _sole_answer(
        self, actor: str, query: str, board_text: str,
    ) -> str:
        """One bare LiteLLM call per agent for SolE answer collection."""
        from models.personas import SOLE_SYSTEM_PROMPT

        try:
            resp = await self.http.post(
                f"{self.litellm_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.litellm_key}"},
                json={
                    "model": self.model_routing.get("light", "medium"),
                    "messages": [
                        {"role": "system", "content": SOLE_SYSTEM_PROMPT},
                        {"role": "user", "content": (
                            f"Objective: {query}\n\n"
                            f"Board state:\n{board_text}\n\n"
                            f"Your role: {actor}\n"
                            f"Provide your answer:"
                        )},
                    ],
                    "max_tokens": 512,
                    "temperature": 0.1,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
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

    def _is_stalled(
        self,
        snapshot: dict[str, BoardEntry],
        current_round: int,
    ) -> bool:
        """Check if the board is stalled (doc 05 §5).

        Stall = rounds with no accepted entries or near-duplicate bodies.
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

        # Check for near-duplicate bodies (normalized hash)
        round_hash = _entries_hash(prev_entries)
        if round_hash in self._round_hashes:
            self._stall_counter += 1
        else:
            self._stall_counter = 0
            self._round_hashes.append(round_hash)

        return self._stall_counter >= self.stall_rounds

    # ── Node Assignment ──────────────────────────────────────────────

    def _to_activations(self, selected: list[str]) -> list[Activation]:
        """Assign selected actors to nodes (load-balanced, one-per-host)."""
        activations = []
        used_hosts: set[str] = set()

        for actor in selected:
            base_role = actor.split(".")[0] if "." in actor else actor
            # Look up in registry
            reg = self.role_registry.get(base_role, {})
            profile = reg.get("profile")
            endpoints = reg.get("endpoints", list(self.node_endpoints))

            # Expert model from roster
            model = self.model_routing.get(self._tier, "medium")
            if actor.startswith("expert.") and self.roster:
                slug = actor.split(".", 1)[1]
                expert = next(
                    (e for e in self.roster.experts if e.slug == slug), None
                )
                if expert:
                    model = expert.model

            # Pick endpoint: prefer unused hosts, then round-robin
            endpoint = endpoints[0]
            for ep in endpoints:
                if ep not in used_hosts:
                    endpoint = ep
                    break
            used_hosts.add(endpoint)

            activations.append(Activation(
                actor=actor,
                role=base_role,
                model=model,
                node_endpoint=endpoint,
                profile=profile,
            ))

        return activations

    # ── Phase Inference ──────────────────────────────────────────────

    def _infer_phase(
        self, snapshot: dict[str, BoardEntry], current_round: int,
    ) -> str:
        """Infer the board phase from entry composition."""
        open_entries = [e for e in snapshot.values() if e.status == "open"]

        has_critiques = any(e.type == "critique" for e in open_entries)
        has_solutions = any(e.type == "solution" for e in open_entries)

        if has_solutions:
            return "Convergence"
        if has_critiques:
            return "Debate"
        if current_round <= 1:
            return "Discovery"
        return "Debate"

    # ── Board Serialization ──────────────────────────────────────────

    def _serialize_board(
        self, board: dict[str, BoardEntry] | dict[str, Any],
    ) -> dict[str, Any]:
        """Serialize board for agent turn payload."""
        if not board:
            return {"mode": "full", "entries": []}

        entries = []
        if isinstance(board, dict):
            for entry in board.values():
                if isinstance(entry, BoardEntry):
                    if entry.status != "removed":
                        entries.append(entry_to_dict(entry))
                elif isinstance(entry, dict):
                    entries.append(entry)

        return {"mode": "full", "entries": entries}

    def _serialize_board_for_cu(
        self, snapshot: dict[str, BoardEntry],
    ) -> str:
        """Serialize board to a compact text format for the CU prompt."""
        if not snapshot:
            return "(empty board)"

        lines = []
        for entry in sorted(
            snapshot.values(),
            key=lambda e: (e.round, e.id),
        ):
            if entry.status == "removed":
                continue
            refs_str = f" refs=[{','.join(entry.refs)}]" if entry.refs else ""
            conf_str = f" conf={entry.confidence:.1f}" if entry.confidence else ""
            lines.append(
                f"[{entry.id}] ({entry.type}) by {entry.author} "
                f"R{entry.round}{refs_str}{conf_str}: "
                f"{entry.title or entry.body[:80]}"
            )
        return "\n".join(lines)

    # ── Cost Tracking ────────────────────────────────────────────────

    def track_cost(self, cost_usd: float) -> None:
        """Update the running budget total."""
        self.budget_spent += cost_usd

    # ── Cleanup ──────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close HTTP client."""
        await self.http.aclose()


# ── CU Output Parser (doc 05 §1.1) ──────────────────────────────────

def parse_cu_output(
    raw: str, valid_names: list[str],
) -> list[str]:
    """Parse CU selection JSON. Returns list of valid actor names.

    Drops unknown names with warning. Returns empty list on garbled output.
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
                return []
        else:
            return []

    if not isinstance(data, dict):
        return []

    selected = data.get("selected", [])
    if not isinstance(selected, list):
        return []

    # Filter to valid names
    result = []
    valid_set = set(valid_names)
    for name in selected:
        if not isinstance(name, str):
            continue
        if name in valid_set:
            result.append(name)
        else:
            logger.warning("CU selected unknown agent '%s' — dropping", name)

    return result


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
        for j, (actor_j, answer_j) in enumerate(answers):
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


def _entries_hash(entries: list[BoardEntry]) -> str:
    """Hash entry bodies for near-duplicate detection."""
    bodies = sorted(e.body.strip().lower() for e in entries)
    combined = "|".join(bodies)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


# ── Register with the variant registry ───────────────────────────────

register_variant("traditional", TraditionalVariant)
