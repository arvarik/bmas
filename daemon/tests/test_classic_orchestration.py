"""Focused regressions for the classic blackboard orchestration path."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import database as db
from core.board_store import InMemoryBoardStore
from core.entry import BoardEntry
from core.event_emitter import InMemoryEventEmitter
from core.gateway import BoardGateway, LeaseLostError
from core.orchestrator import Orchestrator
from core.variants.traditional import Activation, TraditionalVariant


def _orchestrator_without_clients() -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch._safe_log = AsyncMock()
    orch.bb = SimpleNamespace(publish_event=AsyncMock())
    return orch


def _activation(actor: str = "expert.alpha") -> Activation:
    return Activation(
        actor=actor,
        role="expert",
        model="test-model",
        node_endpoint="http://selected-node:8000",
        profile="expert-profile",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "timeout"])
async def test_failed_turn_never_becomes_board_entry(status):
    orch = _orchestrator_without_clients()
    orch._dispatch_turn = AsyncMock(return_value={
        "status": status,
        "result": "connection refused",
    })

    store = SimpleNamespace(get_snapshot=AsyncMock(return_value={}))
    variant = SimpleNamespace(
        store=store,
        build_turn_payload=MagicMock(return_value={
            "turn_id": "turn-stable",
            "role_prompt": "persona",
            "board": {"entries": []},
            "objective": "question",
            "budget_remaining_usd": 0.25,
            "session_id": "task-1:expert.alpha",
            "previous_response_id": None,
        }),
        parse_agent_response=MagicMock(),
        apply=AsyncMock(),
    )

    response = await orch._dispatch_traditional_turn(
        variant,
        {"task_id": "task-1", "query": "question"},
        _activation(),
        round_no=2,
    )

    assert response["status"] == status
    variant.parse_agent_response.assert_not_called()
    variant.apply.assert_not_awaited()
    end_event = orch.bb.publish_event.await_args_list[-1].args[2]
    assert end_event["entries_added"] == 0


@pytest.mark.asyncio
async def test_dispatch_uses_selected_endpoint_and_one_turn_identity(monkeypatch):
    orch = _orchestrator_without_clients()
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": "completed",
        "result": "ok",
        "turn_id": "node-local-turn",
    }
    orch.http = SimpleNamespace(post=AsyncMock(return_value=response))

    create_turn = AsyncMock()
    complete_turn = AsyncMock()
    monkeypatch.setattr(db, "create_turn", create_turn)
    monkeypatch.setattr(db, "complete_turn", complete_turn)

    result = await orch._dispatch_turn(
        role="expert",
        task_id="task-1",
        description="question",
        persona="persona",
        model="test-model",
        actor="expert.alpha",
        turn_id="turn-stable",
        endpoint="http://selected-node:8000",
        profile="expert-profile",
        session_id="task-1:expert.alpha",
        activation_id="turn-stable",
    )

    assert result["status"] == "completed"
    assert result["turn_id"] == "turn-stable"
    assert result["activation_id"] == "turn-stable"
    assert result["session_id"] == "task-1:expert.alpha"
    post_call = orch.http.post.await_args
    assert post_call.args[0] == "http://selected-node:8000/execute"
    payload = post_call.kwargs["json"]
    assert payload["turn_id"] == "turn-stable"
    assert payload["activation_id"] == "turn-stable"
    assert payload["session_id"] == "task-1:expert.alpha"
    assert payload["profile"] == "expert-profile"
    assert create_turn.await_args.args[0]["id"] == "turn-stable"
    assert complete_turn.await_args.kwargs["turn_id"] == "turn-stable"


@pytest.mark.asyncio
async def test_dispatch_records_usage_before_late_trace_ingestion(monkeypatch):
    orch = _orchestrator_without_clients()
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": "completed",
        "result": "ok",
        "trace_count": 3,
        "usage": {
            "model": "test-model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
        },
    }
    orch.http = SimpleNamespace(post=AsyncMock(return_value=response))
    insert_cost = AsyncMock()
    monkeypatch.setattr(db, "create_turn", AsyncMock())
    monkeypatch.setattr(db, "complete_turn", AsyncMock())
    monkeypatch.setattr(db, "insert_cost_entry_v2", insert_cost)

    await orch._dispatch_turn(
        role="expert",
        task_id="task-1",
        description="question",
        persona="persona",
        model="test-model",
        endpoint="http://node:8000",
    )

    insert_cost.assert_awaited_once()
    assert insert_cost.await_args.kwargs["phase"] == "trace"


@pytest.mark.asyncio
async def test_dispatch_fails_over_without_changing_activation_id(monkeypatch):
    orch = _orchestrator_without_clients()
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": "completed", "result": "ok"}
    calls = []

    async def post(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            raise httpx.ConnectError("offline")
        return response

    orch.http = SimpleNamespace(post=post)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(db, "create_turn", AsyncMock())
    monkeypatch.setattr(db, "complete_turn", AsyncMock())
    monkeypatch.setattr("core.orchestrator.BMAS_EXECUTE_KEY", "execute-secret")

    result = await orch._dispatch_turn(
        role="expert",
        task_id="task-1",
        description="question",
        persona="persona",
        turn_id="activation-stable",
        activation_id="activation-stable",
        endpoint="http://node-a:8000",
        endpoints=["http://node-a:8000", "http://node-b:8000"],
    )

    assert result["status"] == "completed"
    assert [call[0] for call in calls] == [
        "http://node-a:8000/execute",
        "http://node-b:8000/execute",
    ]
    assert all(
        call[1]["json"]["activation_id"] == "activation-stable"
        for call in calls
    )
    assert calls[1][1]["headers"] == {
        "Authorization": "Bearer execute-secret",
    }


@pytest.mark.asyncio
async def test_safe_failover_drops_node_local_response_context(monkeypatch):
    orch = _orchestrator_without_clients()
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": "completed", "result": "ok"}
    payloads = []

    async def post(url, **kwargs):
        payloads.append((url, copy.deepcopy(kwargs["json"])))
        if len(payloads) == 1:
            raise httpx.ConnectError("offline")
        return response

    orch.http = SimpleNamespace(post=post)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(db, "create_turn", AsyncMock())
    monkeypatch.setattr(db, "complete_turn", AsyncMock())

    result = await orch._dispatch_turn(
        role="expert",
        task_id="task-1",
        description="question",
        persona="persona",
        context={"previous_response_id": "node-a-response"},
        endpoints=["http://node-a:8000", "http://node-b:8000"],
    )

    assert payloads[0][1]["context"]["previous_response_id"] == "node-a-response"
    assert payloads[1][1]["context"]["previous_response_id"] is None
    assert result["endpoint"] == "http://node-b:8000"


@pytest.mark.asyncio
async def test_dispatch_retries_ambiguous_read_on_same_endpoint(monkeypatch):
    orch = _orchestrator_without_clients()
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": "completed", "result": "ok"}
    calls = []

    async def post(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise httpx.ReadTimeout("ambiguous")
        return response

    orch.http = SimpleNamespace(post=post)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(db, "create_turn", AsyncMock())
    monkeypatch.setattr(db, "complete_turn", AsyncMock())

    result = await orch._dispatch_turn(
        role="expert",
        task_id="task-1",
        description="question",
        persona="persona",
        turn_id="activation-stable",
        endpoints=["http://node-a:8000", "http://node-b:8000"],
    )

    assert result["status"] == "completed"
    assert calls == [
        "http://node-a:8000/execute",
        "http://node-a:8000/execute",
    ]


@pytest.mark.asyncio
async def test_dispatch_can_reach_fourth_endpoint(monkeypatch):
    orch = _orchestrator_without_clients()
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": "completed", "result": "ok"}
    calls = []

    async def post(url, **kwargs):
        calls.append(url)
        if len(calls) < 4:
            raise httpx.ConnectError("offline")
        return response

    orch.http = SimpleNamespace(post=post)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(db, "create_turn", AsyncMock())
    monkeypatch.setattr(db, "complete_turn", AsyncMock())
    endpoints = [f"http://node-{index}:8000" for index in range(4)]

    result = await orch._dispatch_turn(
        role="expert",
        task_id="task-1",
        description="question",
        persona="persona",
        endpoints=endpoints,
    )

    assert result["status"] == "completed"
    assert calls[-1] == "http://node-3:8000/execute"


@pytest.mark.asyncio
async def test_final_failover_endpoint_keeps_a_bounded_retry_budget(monkeypatch):
    orch = _orchestrator_without_clients()
    calls = []

    async def post(url, **kwargs):
        calls.append(url)
        raise httpx.ConnectError("offline")

    orch.http = SimpleNamespace(post=post)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(db, "create_turn", AsyncMock())
    monkeypatch.setattr(db, "complete_turn", AsyncMock())
    endpoints = [f"http://node-{index}:8000" for index in range(4)]

    result = await orch._dispatch_turn(
        role="expert",
        task_id="task-1",
        description="question",
        persona="persona",
        endpoints=endpoints,
    )

    assert result["status"] == "failed"
    assert len(calls) == 6
    assert calls[-3:] == ["http://node-3:8000/execute"] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_peak"),
    [("sequential", 1), ("concurrent", 2)],
)
async def test_round_execution_mode_controls_overlap(mode, expected_peak):
    orch = _orchestrator_without_clients()
    active = 0
    peak = 0

    async def dispatch(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"status": "completed"}

    orch._dispatch_traditional_turn = dispatch
    gateway = SimpleNamespace(set_meta=AsyncMock())
    variant = SimpleNamespace(
        round_execution=mode,
        gateway=gateway,
        budget_spent=0.0,
        reserve_activation_budgets=MagicMock(return_value=[0.25, 0.25]),
        track_cost=MagicMock(),
        set_response_id=MagicMock(),
    )
    activations = [_activation("expert.alpha"), _activation("expert.beta")]

    results = await orch._dispatch_traditional_group(
        variant,
        {"task_id": "task-1", "query": "question"},
        activations,
        round_no=1,
    )

    assert len(results) == 2
    assert peak == expected_peak
    assert gateway.set_meta.await_args_list[0].kwargs["budget_reserved"] == 0.5
    assert gateway.set_meta.await_args_list[-1].kwargs == {
        "budget_spent": 0.0,
        "budget_reserved": 0.0,
    }


@pytest.mark.asyncio
async def test_completion_survives_a_cost_rollup_error(monkeypatch):
    orch = _orchestrator_without_clients()
    orch._task_lock_ids = {"task-1": "owner"}
    complete_task = AsyncMock(return_value=True)
    monkeypatch.setattr(
        db,
        "update_task_cost_totals",
        AsyncMock(side_effect=RuntimeError("rollup unavailable")),
    )
    monkeypatch.setattr(db, "complete_task", complete_task)

    await orch._complete_traditional_task(
        "task-1",
        "question",
        {"answer": "answer", "rounds_completed": 2},
        0.1,
    )

    complete_task.assert_awaited_once()
    assert complete_task.await_args.kwargs["lease_token"] == "owner"


@pytest.mark.asyncio
async def test_genesis_keeps_configured_round_limit_and_existing_spend():
    gateway = SimpleNamespace(append=AsyncMock(), set_meta=AsyncMock())
    variant = TraditionalVariant(
        gateway=gateway,
        board_store=SimpleNamespace(),
        event_emitter=None,
        triage=None,
        config={
            "max_rounds": 12,
            "experts_per_tier": {
                "simple": 0,
                "light": 0,
                "medium": 0,
                "complex": 0,
            },
        },
        litellm_url="http://litellm.test",
        litellm_key="key",
        node_endpoints=["http://node:8000"],
        role_registry={},
        model_routing={"simple": "test-model"},
    )
    variant.budget_spent = 0.125
    triage_result = SimpleNamespace(complexity=SimpleNamespace(value="simple"))

    try:
        await variant.genesis({
            "task_id": "task-1",
            "query": "question",
            "triage_result": triage_result,
        })
    finally:
        await variant.close()

    assert variant.max_rounds == 12
    meta = gateway.set_meta.await_args.kwargs
    assert meta["budget_spent"] == 0.125
    assert meta["budget_reserved"] == 0.0


@pytest.mark.asyncio
async def test_private_conflict_publishes_results_before_superseding():
    store = InMemoryBoardStore()
    gateway = BoardGateway(store, InMemoryEventEmitter())
    variant = TraditionalVariant(
        gateway=gateway,
        board_store=store,
        event_emitter=None,
        triage=None,
        config={"max_rounds": 8},
        litellm_url="http://litellm.test",
        litellm_key="key",
        node_endpoints=["http://node:8000"],
        role_registry={"expert": {"endpoints": ["http://node:8000"]}},
        model_routing={"medium": "test-model"},
    )

    first = (await gateway.append(
        "task-1",
        "expert.alpha",
        ["finding_writer"],
        [{"type": "finding", "body": "Alpha position"}],
        turn_id="turn-a",
        round_no=1,
    ))[0]
    second = (await gateway.append(
        "task-1",
        "expert.beta",
        ["finding_writer"],
        [{"type": "finding", "body": "Beta position"}],
        turn_id="turn-b",
        round_no=1,
    ))[0]
    conflict = (await gateway.append(
        "task-1",
        "conflict_resolver",
        ["conflict_mediator"],
        [{
            "type": "conflict",
            "body": "The positions conflict",
            "refs": [first.id, second.id],
        }],
        turn_id="turn-c",
        round_no=2,
    ))[0]
    await gateway.set_meta("task-1", round=3)

    saw_seed = False

    async def dispatch_fn(**kwargs):
        nonlocal saw_seed
        private = await store.get_private_snapshot("task-1", kwargs["space"])
        saw_seed = saw_seed or any(
            entry.title == "Private conflict context" for entry in private.values()
        )
        actor = kwargs["activation"].actor
        return {
            "status": "completed",
            "entries": [{
                "type": "finding",
                "title": f"Resolved by {actor}",
                "body": f"{actor} reconciled the evidence",
                "confidence": 0.8,
            }],
        }

    try:
        published = await variant.handle_conflict_resolution(
            {"task_id": "task-1", "query": "resolve"},
            conflict,
            dispatch_fn,
        )
        added_before_replay = sum(
            event["event_type"] == "entry_added"
            for event in await store.get_events("task-1")
        )
        replayed = await variant.handle_conflict_resolution(
            {"task_id": "task-1", "query": "resolve"},
            conflict,
            dispatch_fn,
        )
    finally:
        await variant.close()

    snapshot = await store.get_snapshot("task-1")
    assert saw_seed is True
    assert published
    assert replayed
    assert sum(
        event["event_type"] == "entry_added"
        for event in await store.get_events("task-1")
    ) == added_before_replay
    assert all(entry.space == "public" for entry in published)
    assert all(conflict.id in entry.refs for entry in published)
    assert snapshot[first.id].status == "superseded"
    assert snapshot[second.id].status == "superseded"
    assert snapshot[conflict.id].status == "superseded"
    assert await store.get_private_snapshot(
        "task-1", f"private:conflict-{conflict.id}"
    ) == {}


@pytest.mark.asyncio
async def test_active_round_checkpoint_restores_only_unfinished_work():
    store = InMemoryBoardStore()
    gateway = BoardGateway(store, InMemoryEventEmitter())
    variant = TraditionalVariant(
        gateway=gateway,
        board_store=store,
        event_emitter=None,
        triage=None,
        config={},
        litellm_url="http://litellm.test",
        litellm_key="key",
        node_endpoints=["http://node:8000"],
        role_registry={},
        model_routing={"medium": "test-model"},
    )
    state = {
        "round": 3,
        "status": "active",
        "rationale": "continue",
        "selection_source": "checkpoint",
        "phase": "Debate",
        "activations": [
            {
                "actor": "expert.alpha",
                "role": "expert",
                "model": "test-model",
                "node_endpoint": "http://node:8000",
                "profile": "expert",
                "activation_id": "activation-a",
            },
            {
                "actor": "critic",
                "role": "critic",
                "model": "test-model",
                "node_endpoint": "http://node:8000",
                "profile": "critic",
                "activation_id": "activation-b",
            },
        ],
        "completed": {"activation-a": "completed"},
    }
    await store.set_meta("task-1", round=3, round_state=state)

    try:
        restored = await variant.restore_active_round("task-1")
        assert restored is not None
        assert [item.activation_id for item in restored.activations] == [
            "activation-b",
        ]
        with pytest.raises(RuntimeError, match="unfinished activations"):
            await variant.finish_round("task-1")
        await variant.mark_activation_complete(
            "task-1",
            "activation-b",
            "completed",
            actor="critic",
            response_id="response-2",
        )
        await variant.finish_round("task-1")
    finally:
        await variant.close()

    meta = await store.get_meta("task-1")
    assert meta["round_state"]["status"] == "completed"
    assert meta["progress_ledger"][-1]["round"] == 3
    assert meta["response_ids"] == {"critic": "response-2"}


@pytest.mark.asyncio
async def test_actor_node_pins_survive_changed_selection_order():
    store = InMemoryBoardStore()
    gateway = BoardGateway(store, InMemoryEventEmitter())

    def make_variant():
        return TraditionalVariant(
            gateway=gateway,
            board_store=store,
            event_emitter=None,
            triage=None,
            config={},
            litellm_url="",
            litellm_key="",
            node_endpoints=["http://node-a", "http://node-b"],
            role_registry={},
            model_routing={"medium": "test-model"},
        )

    first = make_variant()
    initial = first._to_activations(["planner", "critic"])
    await first.checkpoint("task-1")
    await first.close()

    resumed = make_variant()
    try:
        await resumed.resume({"task_id": "task-1", "query": "question"})
        reordered = resumed._to_activations(["critic", "planner"])
    finally:
        await resumed.close()

    initial_nodes = {item.actor: item.node_endpoint for item in initial}
    reordered_nodes = {item.actor: item.node_endpoint for item in reordered}
    assert reordered_nodes == initial_nodes


@pytest.mark.asyncio
async def test_large_board_view_stays_inside_token_budget():
    variant = TraditionalVariant(
        gateway=SimpleNamespace(),
        board_store=SimpleNamespace(),
        event_emitter=None,
        triage=None,
        config={"view_budget_tokens": 512},
        litellm_url="http://litellm.test",
        litellm_key="key",
        node_endpoints=["http://node:8000"],
        role_registry={},
        model_routing={"medium": "test-model"},
    )
    board = {
        "objective": {
            "id": "objective",
            "type": "objective",
            "title": "Large objective",
            "body": "objective " * 4000,
            "status": "open",
            "refs": [f"e-{index}" for index in range(500)],
        },
        **{
            f"e-{index}": {
                "id": f"e-{index}",
                "type": "finding",
                "body": "evidence " * 1000,
                "status": "open",
                "salience": index / 100,
                "confidence": 0.8,
                "round": index,
            }
            for index in range(100)
        },
    }

    try:
        view = variant._serialize_board(board, actor="critic")
    finally:
        await variant.close()

    assert view["mode"] == "bounded"
    assert view["estimated_tokens"] <= view["token_budget"] == 512
    assert view["omitted_count"] > 0
    assert view["omitted_index"]
    assert view["index_estimated_tokens"] <= view["index_token_budget"]
    assert set(view["omitted_index"][0]) >= {
        "id", "type", "title", "author", "round", "status", "refs",
        "salience", "body_excerpt",
    }


@pytest.mark.asyncio
async def test_decider_gets_a_larger_omitted_index_share():
    variant = TraditionalVariant(
        gateway=SimpleNamespace(),
        board_store=SimpleNamespace(),
        event_emitter=None,
        triage=None,
        config={"view_budget_tokens": 1000},
        litellm_url="http://litellm.test",
        litellm_key="key",
        node_endpoints=[],
        role_registry={},
        model_routing={},
    )
    board = {
        f"e-{index}": {
            "id": f"e-{index}",
            "type": "finding",
            "title": f"Finding {index}",
            "author": "expert.alpha",
            "body": "evidence " * 400,
            "refs": [],
            "status": "open",
            "round": index,
            "salience": index / 100,
        }
        for index in range(40)
    }

    try:
        critic = variant._serialize_board(board, actor="critic")
        decider = variant._serialize_board(board, actor="decider")
    finally:
        await variant.close()

    assert decider["index_token_budget"] > critic["index_token_budget"]
    assert decider["estimated_tokens"] <= 1000


@pytest.mark.asyncio
async def test_retry_uses_stable_mutation_ids_after_a_rejection():
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
    mutations = [
        {
            "actor": "expert.alpha",
            "turn_id": "turn-stable",
            "_mutation_id": "turn-stable:0",
            "entries": [{"type": "solution", "body": "not allowed"}],
        },
        {
            "actor": "expert.alpha",
            "turn_id": "turn-stable",
            "_mutation_id": "turn-stable:1",
            "entries": [{"type": "finding", "body": "valid evidence"}],
        },
    ]

    try:
        await variant.apply({"task_id": "task-1"}, mutations)
        await variant.apply({"task_id": "task-1"}, mutations)
    finally:
        await variant.close()

    events = await store.get_events("task-1")
    assert sum(event["event_type"] == "entry_rejected" for event in events) == 1
    assert sum(event["event_type"] == "entry_added" for event in events) == 1


@pytest.mark.asyncio
async def test_review_requires_a_committed_critique_reference():
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
        "task-1", "decider", ["decision_writer"],
        [{"type": "solution", "body": "answer"}],
        turn_id="solution-turn",
    ))[0]
    finding = (await gateway.append(
        "task-1", "expert.alpha", ["finding_writer"],
        [{"type": "finding", "body": "evidence"}],
        turn_id="finding-turn",
    ))[0]

    try:
        assert await variant.mark_solution_reviewed("task-1", [finding]) is None
        critique = (await gateway.append(
            "task-1", "critic", ["critique_writer"],
            [{"type": "critique", "body": "review", "refs": [solution.id]}],
            turn_id="critic-turn",
        ))[0]
        assert await variant.mark_solution_reviewed(
            "task-1", [critique],
        ) is None
    finally:
        await variant.close()


@pytest.mark.asyncio
async def test_long_objective_is_truncated_on_board_but_full_in_turn_payload():
    store = InMemoryBoardStore()
    gateway = BoardGateway(
        store,
        InMemoryEventEmitter(),
        max_body_len=8000,
    )
    variant = TraditionalVariant(
        gateway=gateway,
        board_store=store,
        event_emitter=None,
        triage=None,
        config={"experts_per_tier": {"medium": 0}},
        litellm_url="",
        litellm_key="",
        node_endpoints=[],
        role_registry={},
        model_routing={},
    )
    variant._generate_experts = AsyncMock(return_value=[])
    query = "long objective " * 1000
    task = {
        "task_id": "task-long",
        "query": query,
        "triage_result": SimpleNamespace(
            complexity=SimpleNamespace(value="medium"),
        ),
    }

    try:
        await variant.genesis(task)
        snapshot = await store.get_snapshot("task-long")
        objective = next(entry for entry in snapshot.values() if entry.type == "objective")
        payload = variant.build_turn_payload(task, "planner", snapshot)
    finally:
        await variant.close()

    assert len(objective.body) <= 8000
    assert "Board objective truncated" in objective.body
    assert payload["objective"] == query
    assert (await store.get_meta("task-long"))["objective_truncated"] is True


@pytest.mark.asyncio
async def test_private_archive_obeys_the_commit_guard():
    store = InMemoryBoardStore()
    gateway = BoardGateway(
        store,
        InMemoryEventEmitter(),
        commit_guard=AsyncMock(return_value=False),
    )
    private = BoardEntry(
        id="e-1",
        task_id="task-1",
        type="finding",
        author="expert.alpha",
        body="private evidence",
        space="private:conflict-e-9",
    )
    await store.upsert_entry("task-1", private)

    with pytest.raises(LeaseLostError):
        await gateway.archive_space("task-1", "private:conflict-e-9")

    assert "e-1" in await store.get_private_snapshot(
        "task-1", "private:conflict-e-9",
    )


@pytest.mark.asyncio
async def test_operator_steering_uses_durable_gateway_events():
    store = InMemoryBoardStore()
    gateway = BoardGateway(store, InMemoryEventEmitter())
    entry = (await gateway.append(
        "task-1",
        "expert.alpha",
        ["finding_writer"],
        [{"type": "finding", "body": "Evidence"}],
        turn_id="turn-1",
    ))[0]
    await store.set_salience("task-1", entry.id, 0.4)
    orch = _orchestrator_without_clients()
    orch._active_gateways = {"task-1": gateway}

    boosted = await orch.steer_entry("task-1", entry.id, "boost")
    retracted = await orch.steer_entry("task-1", entry.id, "retract")

    assert boosted["salience"] == pytest.approx(0.8)
    assert retracted["status"] == "retracted"
    events = await store.get_events("task-1")
    assert [event["event_type"] for event in events][-2:] == [
        "entry_salience_changed",
        "entry_status_changed",
    ]


@pytest.mark.asyncio
async def test_paused_task_observes_abort_immediately(monkeypatch):
    class FakeRedis:
        async def get(self, key):
            if ":pause:" in key:
                return "1"
            if ":abort:" in key:
                return "operator_request"
            return None

        async def delete(self, key):
            return 1

    emitter = SimpleNamespace(
        _redis=FakeRedis(),
        emit=AsyncMock(),
    )
    variant = TraditionalVariant(
        gateway=SimpleNamespace(),
        board_store=SimpleNamespace(),
        event_emitter=emitter,
        triage=None,
        config={"max_duration_s": 1800},
        litellm_url="http://litellm.test",
        litellm_key="key",
        node_endpoints=["http://node:8000"],
        role_registry={},
        model_routing={"medium": "test-model"},
    )
    variant.genesis_time = asyncio.get_running_loop().time()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    try:
        with pytest.raises(RuntimeError, match="aborted"):
            await variant.check_pause("task-1")
    finally:
        await variant.close()
