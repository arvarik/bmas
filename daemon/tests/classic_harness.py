"""Deterministic lifecycle harness for the classic blackboard variant."""

from __future__ import annotations

import copy
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from core.board_store import InMemoryBoardStore, fold_events_to_snapshot
from core.event_emitter import InMemoryEventEmitter
from core.gateway import BoardGateway
from core.orchestrator import Orchestrator
from core.variants.traditional import ExpertIdentity, TraditionalVariant

if TYPE_CHECKING:
    from core.entry import BoardEntry

TASK_ID = "task-golden-classic"
OBJECTIVE = "Resolve the two constraints and return the verified conclusion."
TERMINAL_ACTIVATION_STATES = {"completed", "declined", "failed", "timeout"}


class RecordingBlackboard:
    """Capture the lifecycle events that the orchestrator publishes."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def publish_event(
        self, task_id: str, event_type: str, data: dict[str, Any],
    ) -> None:
        self.events.append((task_id, event_type, copy.deepcopy(data)))


class InvariantCheckingGateway(BoardGateway):
    """Check board invariants after each complete gateway mutation."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.mutation_checks = 0

    async def _check(self, task_id: str) -> None:
        await assert_board_invariants(self.store, task_id)
        self.mutation_checks += 1

    async def _commit(
        self,
        task_id: str,
        entry: BoardEntry,
        actor: str,
        turn_id: str,
        round_no: int,
        mutation_id: str | None = None,
    ) -> None:
        await super()._commit(
            task_id, entry, actor, turn_id, round_no, mutation_id,
        )
        await self._check(task_id)

    async def _log_rejection(
        self,
        task_id: str,
        raw: dict[str, Any],
        actor: str,
        reason: str,
        turn_id: str | None = None,
        round_no: int = 0,
        mutation_id: str | None = None,
    ) -> None:
        await super()._log_rejection(
            task_id,
            raw,
            actor,
            reason,
            turn_id,
            round_no,
            mutation_id,
        )
        await self._check(task_id)

    async def remove(self, *args: Any, **kwargs: Any) -> list[str]:
        removed = await super().remove(*args, **kwargs)
        if removed:
            await self._check(str(args[0] if args else kwargs["task_id"]))
        return removed

    async def set_status(self, *args: Any, **kwargs: Any) -> None:
        await super().set_status(*args, **kwargs)
        await self._check(str(args[0] if args else kwargs["task_id"]))

    async def archive_space(
        self, *args: Any, **kwargs: Any,
    ) -> list[dict[str, Any]]:
        archived = await super().archive_space(*args, **kwargs)
        await self._check(str(args[0] if args else kwargs["task_id"]))
        return archived


@dataclass
class WorkerCall:
    actor: str
    role: str
    model: str
    endpoint: str
    profile: str | None
    turn_id: str
    activation_id: str
    session_id: str
    round_no: int
    board: dict[str, Any]
    status: str
    cost_usd: float

    @property
    def private(self) -> bool:
        entries = self.board.get("entries", [])
        return any(str(entry.get("space", "public")).startswith("private:") for entry in entries)


@dataclass
class LifecycleRun:
    mode: str
    result: dict[str, Any]
    calls: list[WorkerCall]
    events: list[dict[str, Any]]
    snapshot: dict[str, BoardEntry]
    meta: dict[str, Any]
    external_actions: Counter[str]
    mutation_checks: int


class DeterministicWorker:
    """Return role-specific responses without network or model variance."""

    def __init__(self) -> None:
        self.calls: list[WorkerCall] = []
        self.external_actions: Counter[str] = Counter()
        self._results: dict[str, dict[str, Any]] = {}
        self._actor_calls: defaultdict[str, int] = defaultdict(int)

    @staticmethod
    def _entry(
        board: dict[str, Any],
        *,
        entry_type: str | None = None,
        author: str | None = None,
        title: str | None = None,
        status: str = "open",
    ) -> dict[str, Any] | None:
        for entry in reversed(board.get("entries", [])):
            if entry_type is not None and entry.get("type") != entry_type:
                continue
            if author is not None and entry.get("author") != author:
                continue
            if title is not None and entry.get("title") != title:
                continue
            if entry.get("status", "open") != status:
                continue
            return entry
        return None

    def _response(
        self, actor: str, board: dict[str, Any], private: bool,
    ) -> dict[str, Any]:
        self._actor_calls[actor] += 1
        if actor == "planner":
            objective = self._entry(board, entry_type="objective")
            payload = {"entries": [{
                "type": "plan",
                "title": "Resolve both constraints",
                "body": "Compare both claims, resolve conflicts, and verify the result.",
                "refs": [objective["id"]] if objective else [],
                "confidence": 0.9,
            }]}
        elif actor == "expert.alpha":
            payload = {"entries": [{
                "type": "finding",
                "title": "Alpha constraint",
                "body": (
                    "Private review confirms that alpha remains required."
                    if private else
                    "Alpha requires the final value to equal forty-two."
                ),
                "confidence": 0.92,
            }]}
        elif actor == "expert.beta":
            if private:
                payload = {"entries": [{
                    "type": "finding",
                    "title": "Beta reconciliation",
                    "body": "Beta accepts forty-two after reviewing alpha evidence.",
                    "confidence": 0.94,
                }]}
            elif self._actor_calls[actor] == 1:
                payload = {"entries": [{
                    "type": "finding",
                    "title": "Beta constraint",
                    "body": "Beta initially claims that the value equals forty-one.",
                    "confidence": 0.72,
                }]}
            else:
                critique = self._entry(board, entry_type="critique")
                payload = {"entries": [{
                    "type": "rebuttal",
                    "title": "Beta correction",
                    "body": "The critique is correct. Beta withdraws forty-one.",
                    "refs": [critique["id"]] if critique else [],
                    "confidence": 0.96,
                }]}
        elif actor == "critic":
            solution = self._entry(board, entry_type="solution")
            if solution:
                payload = {"action": "approve", "refs": [solution["id"]]}
            else:
                beta = self._entry(
                    board, entry_type="finding", author="expert.beta",
                    title="Beta constraint",
                )
                payload = {"entries": [{
                    "type": "critique",
                    "title": "Beta lacks evidence",
                    "body": "The value forty-one conflicts with the stronger alpha constraint.",
                    "refs": [beta["id"]] if beta else [],
                    "confidence": 0.97,
                }]}
        elif actor == "conflict_resolver":
            alpha = self._entry(
                board, entry_type="finding", author="expert.alpha",
                title="Alpha constraint",
            )
            beta = self._entry(
                board, entry_type="finding", author="expert.beta",
                title="Beta constraint",
            )
            payload = {"entries": [{
                "type": "conflict",
                "title": "Conflicting values",
                "body": "Alpha states forty-two while beta states forty-one.",
                "refs": [entry["id"] for entry in (alpha, beta) if entry],
                "confidence": 0.99,
            }]}
        elif actor == "cleaner":
            stale_finding = self._entry(board, entry_type="finding")
            payload = {
                "action": "clean",
                "removals": ([{
                    "entry_id": stale_finding["id"],
                    "reason": "The final synthesis replaces this duplicate finding.",
                }] if stale_finding else []),
            }
        elif actor == "decider":
            payload = {"entries": [{
                "type": "solution",
                "title": "Verified conclusion",
                "body": "The final value is 42.",
                "confidence": 0.99,
            }]}
        else:
            payload = {"action": "decline"}
        return {
            "status": "completed",
            "result": json.dumps(payload),
            "usage": {
                "model": "filled-by-dispatch",
                "prompt_tokens": 100,
                "completion_tokens": 25,
            },
            "response_id": f"response-{actor}-{self._actor_calls[actor]}",
            "node_id": "deterministic-node",
            "duration_ms": 5,
        }

    async def dispatch(self, **kwargs: Any) -> dict[str, Any]:
        """Execute one idempotent fake external action."""
        actor = str(kwargs.get("actor") or kwargs["role"])
        activation_id = str(kwargs.get("activation_id") or kwargs["turn_id"])
        context = dict(kwargs.get("context") or {})
        board = copy.deepcopy(context.get("board") or {"entries": []})
        private = any(
            str(entry.get("space", "public")).startswith("private:")
            for entry in board.get("entries", [])
        )
        if activation_id not in self._results:
            self.external_actions[activation_id] += 1
            response = self._response(actor, board, private)
            response["usage"]["model"] = kwargs["model"]
            response["turn_id"] = kwargs["turn_id"]
            response["activation_id"] = activation_id
            response["session_id"] = kwargs["session_id"]
            response["endpoint"] = kwargs["endpoint"]
            self._results[activation_id] = copy.deepcopy(response)
        response = copy.deepcopy(self._results[activation_id])
        cost = (response["usage"]["prompt_tokens"] + response["usage"]["completion_tokens"]) / 1_000_000
        self.calls.append(WorkerCall(
            actor=actor,
            role=str(kwargs["role"]),
            model=str(kwargs["model"]),
            endpoint=str(kwargs["endpoint"]),
            profile=kwargs.get("profile"),
            turn_id=str(kwargs["turn_id"]),
            activation_id=activation_id,
            session_id=str(kwargs["session_id"]),
            round_no=int(kwargs["round_no"]),
            board=board,
            status=str(response["status"]),
            cost_usd=cost,
        ))
        return response


class ClassicLifecycleHarness:
    """Run one complete classic lifecycle through the real variant and gateway."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.store = InMemoryBoardStore()
        self.emitter = InMemoryEventEmitter()
        self.gateway = InvariantCheckingGateway(self.store, self.emitter)
        self.worker = DeterministicWorker()
        self.blackboard = RecordingBlackboard()
        self.orchestrator = object.__new__(Orchestrator)
        self.orchestrator.bb = self.blackboard
        self.orchestrator._safe_log = self._safe_log
        self.orchestrator._dispatch_turn = self.worker.dispatch
        self.orchestrator._compute_cost = lambda usage, pricing: (
            int(usage.get("prompt_tokens", 0))
            + int(usage.get("completion_tokens", 0))
        ) / 1_000_000
        self.variant = TraditionalVariant(
            gateway=self.gateway,
            board_store=self.store,
            event_emitter=self.emitter,
            triage=None,
            config={
                "max_rounds": 10,
                "max_duration_s": 600,
                "budget_ceiling_usd": 1.0,
                "max_concurrent_activations": 3,
                "experts_per_tier": {
                    "simple": 0,
                    "light": 1,
                    "medium": 2,
                    "complex": 2,
                },
                "cleaner_entry_threshold": 100,
                "cleaner_token_threshold": 100000,
                "stall_rounds": 50,
                "cu_mode": "llm",
                "round_execution": mode,
            },
            litellm_url="http://unused",
            litellm_key="unused",
            node_endpoints=[
                "http://node-a:8000",
                "http://node-b:8000",
                "http://node-c:8000",
            ],
            role_registry={
                role: {
                    "profile": f"{role}-profile",
                    "endpoints": [
                        "http://node-a:8000",
                        "http://node-b:8000",
                        "http://node-c:8000",
                    ],
                    "enabled": True,
                }
                for role in (
                    "planner", "expert", "critic", "conflict_resolver",
                    "cleaner", "decider",
                )
            },
            model_routing={
                "light": "control-model",
                "medium": "fixed-role-model",
            },
            model_pools={
                "medium": ["expert-model-alpha", "expert-model-beta"],
            },
        )
        self.variant._generate_experts = self._generate_experts
        self.variant._cu_select = self._select

    async def _safe_log(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def _generate_experts(
        self, query: str, count: int, tier: str, task_id: str | None = None,
    ) -> list[ExpertIdentity]:
        assert query == OBJECTIVE
        assert count == 2
        assert tier == "medium"
        return [
            ExpertIdentity(
                name="Alpha Analyst",
                slug="alpha",
                ability="Apply the alpha constraint.",
                model="expert-model-alpha",
            ),
            ExpertIdentity(
                name="Beta Analyst",
                slug="beta",
                ability="Test the beta constraint.",
                model="expert-model-beta",
            ),
        ]

    async def _select(
        self,
        task_id: str,
        query: str,
        snapshot: dict[str, BoardEntry],
        current_round: int,
        meta: dict[str, Any],
    ) -> tuple[list[str], str]:
        selections = {
            1: ["planner", "expert.alpha", "expert.beta"],
            2: ["critic"],
            3: ["expert.beta"],
            4: ["conflict_resolver"],
            5: ["conflict_resolver"],
            6: ["cleaner"],
            7: ["decider"],
            8: ["critic"],
        }
        return selections.get(current_round, ["decider"]), "golden schedule"

    async def run(self) -> LifecycleRun:
        task = {
            "task_id": TASK_ID,
            "query": OBJECTIVE,
            "triage_result": SimpleNamespace(
                complexity=SimpleNamespace(value="medium")
            ),
        }
        await self.variant.genesis(task)
        terminal_reason = "max_rounds"
        try:
            for _ in range(1, 12):
                board = await self.store.get_snapshot(TASK_ID)
                step = await self.variant.step(task, board)
                if step.terminal:
                    terminal_reason = step.reason or "unknown"
                    break

                conflicts = [
                    entry for entry in board.values()
                    if entry.type == "conflict" and entry.status == "open"
                ]
                conflict_activations = [
                    activation for activation in step.activations
                    if activation.actor == "conflict_resolver"
                ]
                if conflicts and conflict_activations:
                    await self.variant.handle_conflict_resolution(
                        task,
                        conflicts[0],
                        self.orchestrator._dispatch_traditional_turn,
                    )
                    await self.variant.mark_activation_complete(
                        TASK_ID,
                        conflict_activations[0].activation_id or "",
                        "completed",
                        actor="conflict_resolver",
                        node_endpoint=conflict_activations[0].node_endpoint,
                    )
                    step.activations = [
                        activation for activation in step.activations
                        if activation.actor != "conflict_resolver"
                    ]

                non_decider = [
                    activation for activation in step.activations
                    if activation.actor != "decider"
                ]
                decider = [
                    activation for activation in step.activations
                    if activation.actor == "decider"
                ]
                if non_decider:
                    await self.orchestrator._dispatch_traditional_group(
                        self.variant,
                        task,
                        non_decider,
                        int((await self.store.get_meta(TASK_ID))["round"]),
                        rationale=step.rationale,
                        phase=step.phase,
                    )
                if decider:
                    await self.orchestrator._dispatch_traditional_group(
                        self.variant,
                        task,
                        decider,
                        int((await self.store.get_meta(TASK_ID))["round"]),
                        rationale=step.rationale,
                        phase=step.phase,
                    )
                await self.variant.finish_round(TASK_ID)
                await self.variant.checkpoint(TASK_ID)
                await assert_state_invariants(self.store, TASK_ID)
            else:
                raise AssertionError("The golden lifecycle did not terminate")

            snapshot = await self.store.get_snapshot(TASK_ID)
            result = await self.variant.finalize(task, snapshot, terminal_reason)
            await self.variant.checkpoint(TASK_ID)
            events = await self.store.get_events(TASK_ID)
            meta = await self.store.get_meta(TASK_ID)
            await assert_state_invariants(self.store, TASK_ID)
            return LifecycleRun(
                mode=self.mode,
                result=result,
                calls=list(self.worker.calls),
                events=events,
                snapshot=snapshot,
                meta=meta,
                external_actions=Counter(self.worker.external_actions),
                mutation_checks=self.gateway.mutation_checks,
            )
        finally:
            await self.variant.close()


def _entry_signature(entry: BoardEntry) -> tuple[Any, ...]:
    return (
        entry.id,
        entry.type,
        entry.author,
        entry.title,
        entry.body,
        tuple(entry.refs),
        entry.status,
        round(entry.salience, 8),
        entry.round,
        entry.space,
        entry.created_by_turn,
    )


async def assert_state_invariants(
    store: InMemoryBoardStore, task_id: str,
) -> None:
    """Assert the durable board and activation invariants."""
    await assert_board_invariants(store, task_id)

    meta = await store.get_meta(task_id)
    for round_record in meta.get("progress_ledger", []):
        statuses = round_record.get("activation_statuses", {})
        assert statuses
        assert all(status in TERMINAL_ACTIVATION_STATES for status in statuses.values())


async def assert_board_invariants(
    store: InMemoryBoardStore, task_id: str,
) -> None:
    """Assert sequence, identity, and replay rules for one board mutation."""
    events = await store.get_events(task_id)
    sequences = [int(event["seq"]) for event in events]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert all(
        next_seq > current
        for current, next_seq in zip(sequences, sequences[1:], strict=False)
    )
    event_ids = [(event["task_id"], event["seq"]) for event in events]
    assert len(event_ids) == len(set(event_ids))

    live = await store.get_snapshot(task_id)
    replayed = fold_events_to_snapshot(events)
    live_signature = {
        entry_id: _entry_signature(entry) for entry_id, entry in live.items()
    }
    replay_signature = {
        entry_id: _entry_signature(entry) for entry_id, entry in replayed.items()
    }
    assert live_signature == replay_signature, {
        "live": live_signature,
        "replayed": replay_signature,
    }
