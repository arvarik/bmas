"""The scheduling policy of the Classic runtime.

The scheduling policy turns a selected actor list into an activation
plan: it applies the decider-alone guard, clamps the plan to the
concurrency limit, assigns every actor to an endpoint and a model, and
names each activation. It also infers the board phase and rebuilds an
interrupted plan from the saved round state. The policy keeps the
actor-to-endpoint pins as its state, and the engine checkpoints them.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.entry import BoardEntry
    from core.variants.classic.roster import AgentRoster

logger = logging.getLogger("bmas.classic.scheduling")


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


DECIDER_DEFERRAL_NOTE = (
    " [Decider deferred: must run alone per paper §3.2"
    " so it can see all prior board writes.]"
)


def activation_identity(task_id: str, round_no: int, actor: str, index: int) -> str:
    """Build a stable activation identity for retries and restarts."""
    value = f"bmas:{task_id}:{round_no}:{actor}:{index}"
    return f"activation-{uuid.uuid5(uuid.NAMESPACE_URL, value).hex}"


@dataclass
class SchedulingPolicy:
    """Order dependent work and group independent work.

    The policy reads the registry, the endpoints, and the limits from the
    arguments of each call, so a value the engine changes after
    construction reaches the policy at once. Its only state is the
    actor-to-endpoint pin table.
    """

    # Keep stateful response IDs on the node that created them: each
    # actor pins to the endpoint that completed its last turn.
    actor_nodes: dict[str, str] = field(default_factory=dict)

    def pin_actor(self, actor: str, endpoint: str) -> None:
        """Pin an actor to the endpoint that completed its last turn."""
        if endpoint:
            self.actor_nodes[actor] = endpoint

    def isolate_decider(self, selected: list[str]) -> tuple[list[str], bool]:
        """Apply the paper §3.2 guard: the decider must run alone.

        The decider must see every board write (including critiques)
        before judging. When the selection co-selects the decider with
        other agents, the guard drops the decider; the next round
        re-selects it once the other agents have written. The guard runs
        before the concurrency clamp, so the slot the decider held goes
        to the next selected agent.
        """
        if "decider" in selected and len(selected) > 1:
            return [actor for actor in selected if actor != "decider"], True
        return list(selected), False

    @staticmethod
    def clamp(selected: list[str], max_concurrent: int) -> list[str]:
        """Clamp the selection to the concurrency limit."""
        return list(selected[:max_concurrent])

    def to_activations(
        self,
        selected: list[str],
        *,
        roster: AgentRoster | None,
        tier: str,
        role_registry: dict[str, dict[str, Any]],
        node_endpoints: list[str],
        model_routing: dict[str, str],
        resolve_model: Callable[[str], str],
    ) -> list[Activation]:
        """Assign selected actors to nodes (load-balanced, one-per-host)."""
        activations = []
        used_hosts: set[str] = set()

        for actor in selected:
            base_role = actor.split(".")[0] if "." in actor else actor
            # Look up in registry
            reg = role_registry.get(base_role, {})
            if reg.get("enabled") is False:
                logger.info("Actor %s is disabled", actor)
                continue
            profile = reg.get("profile")
            raw_endpoints = reg.get("endpoints", list(node_endpoints))
            endpoints: list[str] = [
                str(endpoint) for endpoint in raw_endpoints if endpoint
            ]
            if not endpoints:
                logger.warning("No endpoint is configured for actor %s", actor)
                continue

            # Expert model from roster
            model = resolve_model(model_routing.get(tier, "medium"))
            if actor.startswith("expert.") and roster:
                slug = actor.split(".", 1)[1]
                expert = next(
                    (e for e in roster.experts if e.slug == slug), None
                )
                if expert:
                    model = resolve_model(expert.model)

            # Keep stateful response IDs on the node that created them.
            pinned_endpoint = self.actor_nodes.get(actor)
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
                self.actor_nodes[actor] = endpoint
            used_hosts.add(endpoint)

            activations.append(Activation(
                actor=actor,
                role=base_role,
                model=model,
                node_endpoint=endpoint,
                profile=profile,
            ))

        return activations

    @staticmethod
    def infer_phase(snapshot: dict[str, BoardEntry], current_round: int) -> str:
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

    @staticmethod
    def plan_from_state(state: Any, meta: dict[str, Any]) -> StepResult | None:
        """Rebuild the unfinished activation plan from one saved round state."""
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
