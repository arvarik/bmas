"""Coordination edge cases for the classic blackboard."""

from __future__ import annotations

import time

import pytest

from core.board_store import InMemoryBoardStore
from core.event_emitter import InMemoryEventEmitter
from core.gateway import BoardGateway
from core.variants.traditional import (
    AgentRoster,
    ExpertIdentity,
    TraditionalVariant,
    parse_cu_output,
    sole_evidence_vote,
)


def _variant(
    *,
    config: dict | None = None,
    role_registry: dict | None = None,
    experts: list[ExpertIdentity] | None = None,
):
    store = InMemoryBoardStore()
    gateway = BoardGateway(store, InMemoryEventEmitter())
    variant = TraditionalVariant(
        gateway=gateway,
        board_store=store,
        event_emitter=None,
        triage=None,
        config=config or {},
        litellm_url="",
        litellm_key="",
        node_endpoints=["http://node-a:8000", "http://node-b:8000"],
        role_registry=role_registry or {},
        model_routing={"medium": "test-model", "light": "test-light"},
    )
    variant.roster = AgentRoster(
        constants={
            "planner": "plan",
            "critic": "criticize",
            "conflict_resolver": "resolve",
            "cleaner": "clean",
            "decider": "decide",
        },
        experts=experts or [],
    )
    variant.genesis_time = time.monotonic()
    return variant, store, gateway


def test_duplicate_and_unknown_actor_selections_are_removed():
    selected, rationale = parse_cu_output(
        '{"selected":["planner","planner","ghost","critic","critic"],'
        '"rationale":"test"}',
        ["planner", "critic"],
    )

    assert selected == ["planner", "critic"]
    assert rationale == "test"


@pytest.mark.asyncio
async def test_disabled_actor_is_removed_before_activation():
    variant, store, _gateway = _variant(role_registry={
        "planner": {
            "enabled": False,
            "endpoints": ["http://node-a:8000"],
        },
        "critic": {
            "enabled": True,
            "endpoints": ["http://node-b:8000"],
        },
    })

    async def select(*args, **kwargs):
        return ["planner", "critic"], "selection"

    variant._cu_select = select
    try:
        step = await variant.step(
            {"task_id": "task-disabled", "query": "question"}, {}
        )
    finally:
        await variant.close()

    assert step.selected == ["critic"]
    assert [activation.actor for activation in step.activations] == ["critic"]


@pytest.mark.asyncio
async def test_empty_selection_uses_deterministic_fallback():
    expert = ExpertIdentity(
        name="Expert",
        slug="alpha",
        ability="analyze",
        model="test-model",
    )
    variant, _store, _gateway = _variant(experts=[expert])

    async def select(*args, **kwargs):
        return [], None

    variant._cu_select = select
    try:
        step = await variant.step(
            {"task_id": "task-empty", "query": "question"}, {}
        )
    finally:
        await variant.close()

    assert step.terminal is False
    assert step.selected == ["planner", "expert.alpha"]


@pytest.mark.asyncio
async def test_all_disabled_actors_terminate_without_empty_round_loop():
    disabled = {
        role: {
            "enabled": False,
            "endpoints": ["http://node-a:8000"],
        }
        for role in (
            "planner", "critic", "conflict_resolver", "cleaner", "decider",
        )
    }
    variant, _store, _gateway = _variant(role_registry=disabled)

    async def select(*args, **kwargs):
        return ["planner"], "disabled"

    variant._cu_select = select
    try:
        step = await variant.step(
            {"task_id": "task-all-disabled", "query": "question"}, {}
        )
    finally:
        await variant.close()

    assert step.terminal is True
    assert step.reason == "no_available_agents"
    assert step.activations == []


@pytest.mark.asyncio
async def test_starved_decider_is_forced_after_round_limit():
    variant, store, gateway = _variant(config={
        "max_rounds": 2,
        "stall_rounds": 50,
    })
    await gateway.set_meta("task-starvation", round=2)

    async def select(*args, **kwargs):
        return ["planner"], "repeat planner"

    variant._cu_select = select
    try:
        step = await variant.step(
            {"task_id": "task-starvation", "query": "question"}, {}
        )
    finally:
        await variant.close()

    assert step.selected == ["decider"]
    assert step.activations[0].actor == "decider"
    assert (await store.get_meta("task-starvation"))["terminal_reason"] == "max_rounds"


@pytest.mark.asyncio
async def test_round_plan_is_flat_and_cannot_create_dependency_cycle():
    variant, _store, _gateway = _variant(config={"stall_rounds": 50})

    async def select(*args, **kwargs):
        return ["planner", "critic"], "independent work"

    variant._cu_select = select
    try:
        step = await variant.step(
            {"task_id": "task-flat", "query": "question"}, {}
        )
    finally:
        await variant.close()

    assert [activation.actor for activation in step.activations] == [
        "planner", "critic"
    ]
    assert len({activation.activation_id for activation in step.activations}) == 2
    assert all(not hasattr(activation, "depends_on") for activation in step.activations)


@pytest.mark.asyncio
async def test_repeated_stalls_replan_once_then_force_decider():
    variant, _store, _gateway = _variant(config={
        "stall_rounds": 1,
        "max_replans": 1,
        "max_rounds": 10,
    })
    task = {"task_id": "task-stall", "query": "question"}

    try:
        first = await variant.step(task, {})
        second = await variant.step(task, {})
    finally:
        await variant.close()

    assert first.selection_source == "stall_replan"
    assert first.selected == ["planner"]
    assert second.selected == ["decider"]
    assert "stalled" in (second.rationale or "")


def test_evidence_backed_minority_corrects_a_wrong_majority():
    winner = sole_evidence_vote(
        [
            ("expert.a", "41"),
            ("expert.b", "41"),
            ("expert.c", "42"),
        ],
        [
            ("A checked calculation proves that the final answer is 42.", 1.0, 1.0),
        ],
        "exact",
    )

    assert winner == "42"


@pytest.mark.asyncio
async def test_complete_private_conflict_failure_keeps_public_evidence_open():
    variant, store, gateway = _variant(role_registry={
        "expert": {
            "enabled": True,
            "endpoints": ["http://node-a:8000", "http://node-b:8000"],
        }
    })
    first = (await gateway.append(
        "task-private-failure",
        "expert.alpha",
        ["finding_writer"],
        [{"type": "finding", "body": "alpha"}],
        turn_id="alpha-turn",
    ))[0]
    second = (await gateway.append(
        "task-private-failure",
        "expert.beta",
        ["finding_writer"],
        [{"type": "finding", "body": "beta"}],
        turn_id="beta-turn",
    ))[0]
    conflict = (await gateway.append(
        "task-private-failure",
        "conflict_resolver",
        ["conflict_mediator"],
        [{
            "type": "conflict",
            "body": "alpha conflicts with beta",
            "refs": [first.id, second.id],
        }],
        turn_id="conflict-turn",
    ))[0]

    async def fail_dispatch(**kwargs):
        return {"status": "failed", "result": "all providers unavailable"}

    try:
        published = await variant.handle_conflict_resolution(
            {"task_id": "task-private-failure", "query": "resolve"},
            conflict,
            fail_dispatch,
        )
    finally:
        await variant.close()

    snapshot = await store.get_snapshot("task-private-failure")
    assert published == []
    assert snapshot[first.id].status == "open"
    assert snapshot[second.id].status == "open"
    assert snapshot[conflict.id].status == "open"
    assert await store.get_private_snapshot(
        "task-private-failure", f"private:conflict-{conflict.id}"
    ) == {}
    assert any(
        event["event_type"] == "space_archived"
        for event in await store.get_events("task-private-failure")
    )
