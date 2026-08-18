"""Golden lifecycle and state invariant tests for classic coordination."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from classic_harness import (
    TASK_ID,
    ClassicLifecycleHarness,
    assert_state_invariants,
)

from core.board_store import InMemoryBoardStore
from core.entry import entry_to_dict
from core.event_emitter import InMemoryEventEmitter
from core.gateway import BoardGateway, LeaseLostError
from core.orchestrator import Orchestrator
from core.variants.traditional import Activation, TraditionalVariant


def _stable_board(run) -> list[tuple]:
    return sorted(
        (
            entry.type,
            entry.author,
            entry.title,
            entry.body,
            entry.status,
            entry.space,
        )
        for entry in run.snapshot.values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sequential", "concurrent"])
async def test_golden_classic_lifecycle(mode):
    run = await ClassicLifecycleHarness(mode).run()

    assert run.result == {
        "answer": "The final value is 42.",
        "terminated_by": "solution",
        "answer_source": "decider",
        "verification_status": "critic_reviewed",
        "rounds_completed": 8,
        "budget_spent": pytest.approx(len(run.calls) * 0.000125),
    }
    actors = [call.actor for call in run.calls]
    assert set(actors) == {
        "planner",
        "expert.alpha",
        "expert.beta",
        "critic",
        "conflict_resolver",
        "cleaner",
        "decider",
    }
    assert Counter(actors)["critic"] == 2
    assert Counter(actors)["expert.alpha"] == 3
    assert Counter(actors)["expert.beta"] == 4

    models = {call.actor: call.model for call in run.calls if not call.private}
    assert models["expert.alpha"] == "expert-model-alpha"
    assert models["expert.beta"] == "expert-model-beta"
    assert models["planner"] == "fixed-role-model"
    assert models["critic"] == "fixed-role-model"
    assert models["decider"] == "fixed-role-model"
    assert all(call.profile == f"{call.role}-profile" for call in run.calls)
    assert all(call.turn_id == call.activation_id for call in run.calls)
    assert all(call.session_id == f"{TASK_ID}:{call.actor}" for call in run.calls)
    assert all(count == 1 for count in run.external_actions.values())
    assert run.mutation_checks >= 10

    sequences = [event["seq"] for event in run.events]
    assert sequences == list(range(1, len(sequences) + 1))
    assert any(event["event_type"] == "entry_removed" for event in run.events)
    assert any(event["event_type"] == "space_archived" for event in run.events)
    assert any(
        event["event_type"] == "entry_status_changed"
        and event["payload"].get("status") == "superseded"
        for event in run.events
    )

    ledger = run.meta["progress_ledger"]
    assert [record["round"] for record in ledger] == list(range(1, 9))
    assert all(record["activation_statuses"] for record in ledger)
    assert run.meta["solution_reviewed_id"]
    assert run.meta["verification_status"] == "critic_reviewed"

    public_calls = [call for call in run.calls if not call.private]
    assert all(
        not any(
            str(entry.get("space", "public")).startswith("private:")
            for entry in call.board.get("entries", [])
        )
        for call in public_calls
    )
    private_calls = [call for call in run.calls if call.private]
    assert private_calls
    assert {call.actor for call in private_calls} == {
        "expert.alpha", "expert.beta",
    }

    removal = next(
        event for event in run.events if event["event_type"] == "entry_removed"
    )
    assert removal["actor"] == "cleaner"
    assert removal["turn_id"]
    assert removal["entry_id"]
    assert removal["payload"]["reason"] == "Cleaner maintenance"
    assert removal["payload"]["_mutation_id"]


@pytest.mark.asyncio
async def test_sequential_and_concurrent_modes_produce_the_same_result():
    sequential = await ClassicLifecycleHarness("sequential").run()
    concurrent = await ClassicLifecycleHarness("concurrent").run()

    assert sequential.result == concurrent.result
    assert _stable_board(sequential) == _stable_board(concurrent)
    assert [call.actor for call in sequential.calls] == [
        call.actor for call in concurrent.calls
    ]
    assert [call.model for call in sequential.calls] == [
        call.model for call in concurrent.calls
    ]
    assert [event["event_type"] for event in sequential.events] == [
        event["event_type"] for event in concurrent.events
    ]


@pytest.mark.asyncio
async def test_failed_activation_creates_no_knowledge_entry():
    orchestrator = object.__new__(Orchestrator)
    orchestrator._safe_log = AsyncMock()
    orchestrator.bb = SimpleNamespace(publish_event=AsyncMock())
    orchestrator._dispatch_turn = AsyncMock(return_value={
        "status": "failed",
        "result": "provider rate limit",
    })
    store = InMemoryBoardStore()
    gateway = BoardGateway(store, InMemoryEventEmitter())
    variant = TraditionalVariant(
        gateway=gateway,
        board_store=store,
        event_emitter=None,
        triage=None,
        config={},
        litellm_url="",
        litellm_key="",
        node_endpoints=["http://node:8000"],
        role_registry={"expert": {"endpoints": ["http://node:8000"]}},
        model_routing={"medium": "test-model"},
    )
    activation = Activation(
        actor="expert.alpha",
        role="expert",
        model="test-model",
        node_endpoint="http://node:8000",
        activation_id="failed-activation",
    )

    try:
        response = await orchestrator._dispatch_traditional_turn(
            variant,
            {"task_id": "task-failed", "query": "question"},
            activation,
            1,
        )
    finally:
        await variant.close()

    assert response["status"] == "failed"
    assert await store.get_snapshot("task-failed") == {}
    assert await store.get_events("task-failed") == []


@pytest.mark.asyncio
async def test_failed_verification_cannot_report_verified_success():
    store = InMemoryBoardStore()
    gateway = BoardGateway(store, InMemoryEventEmitter())
    variant = TraditionalVariant(
        gateway=gateway,
        board_store=store,
        event_emitter=None,
        triage=None,
        config={"max_rounds": 5},
        litellm_url="",
        litellm_key="",
        node_endpoints=["http://node:8000"],
        role_registry={
            "critic": {"endpoints": ["http://node:8000"]},
            "decider": {"endpoints": ["http://node:8000"]},
        },
        model_routing={"medium": "test-model"},
    )
    solution = (await gateway.append(
        "task-verification",
        "decider",
        ["decision_writer"],
        [{"type": "solution", "body": "incorrect answer"}],
        turn_id="solution-turn",
        round_no=1,
    ))[0]
    critique = (await gateway.append(
        "task-verification",
        "critic",
        ["critique_writer"],
        [{
            "type": "critique",
            "title": "Verification failed",
            "body": "The answer conflicts with the evidence.",
            "refs": [solution.id],
        }],
        turn_id="critic-turn",
        round_no=2,
    ))[0]
    variant.roster = SimpleNamespace(
        all_actors=lambda: [],
        actor_names=lambda: ["critic", "decider"],
    )

    try:
        assert await variant.mark_solution_reviewed(
            "task-verification", [critique],
        ) is None
        result = await variant.finalize(
            {"task_id": "task-verification", "query": "question"},
            await store.get_snapshot("task-verification"),
            "max_rounds",
        )
    finally:
        await variant.close()

    assert result["answer_source"] == "decider_unverified"
    assert result["verification_status"] == "unverified"


@pytest.mark.asyncio
async def test_stale_lease_rejects_every_board_mutation():
    allowed = True

    async def guard(task_id: str) -> bool:
        return allowed

    store = InMemoryBoardStore()
    gateway = BoardGateway(
        store,
        InMemoryEventEmitter(),
        commit_guard=guard,
    )
    await gateway.append(
        "task-lease",
        "expert.alpha",
        ["finding_writer"],
        [{"type": "finding", "body": "owned entry"}],
        turn_id="turn-owned",
    )
    allowed = False

    with pytest.raises(LeaseLostError):
        await gateway.append(
            "task-lease",
            "expert.alpha",
            ["finding_writer"],
            [{"type": "finding", "body": "stale entry"}],
            turn_id="turn-stale",
        )
    with pytest.raises(LeaseLostError):
        await gateway.set_meta("task-lease", round=99)

    snapshot = await store.get_snapshot("task-lease")
    assert [entry.body for entry in snapshot.values()] == ["owned entry"]
    await assert_state_invariants(store, "task-lease")


@pytest.mark.asyncio
async def test_approved_solution_replay_is_idempotent():
    store = InMemoryBoardStore()
    gateway = BoardGateway(store, InMemoryEventEmitter())
    variant = TraditionalVariant(
        gateway=gateway,
        board_store=store,
        event_emitter=None,
        triage=None,
        config={},
        litellm_url="",
        litellm_key="",
        node_endpoints=[],
        role_registry={},
        model_routing={},
    )
    solution = (await gateway.append(
        "task-approve", "decider", ["decision_writer"],
        [{"type": "solution", "body": "answer"}],
        turn_id="solution-turn",
    ))[0]
    mutation = {
        "actor": "critic",
        "turn_id": "approve-turn",
        "round": 2,
        "_mutation_id": "approve-turn:0",
        "_action": "approve",
        "refs": [solution.id],
    }

    try:
        first = await variant.apply({"task_id": "task-approve"}, [mutation])
        second = await variant.apply({"task_id": "task-approve"}, [mutation])
    finally:
        await variant.close()

    assert [entry_to_dict(entry) for entry in first] == [
        entry_to_dict(entry) for entry in second
    ]
    events = await store.get_events("task-approve")
    assert sum(event["event_type"] == "entry_added" for event in events) == 2
    assert sum(
        event["event_type"] == "entry_status_changed" for event in events
    ) == 1
    await assert_state_invariants(store, "task-approve")
