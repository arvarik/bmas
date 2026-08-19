"""Contracts for the shared coordination runtime and task admission."""

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError

import core.orchestrator as orchestrator_module
import core.variants.classic as classic_module
import routes.submit as submit
import routes.tasks as tasks_route
from core.gateway import LeaseLostError
from core.orchestrator import EndpointOverloadedError, Orchestrator
from core.protocol import LEGACY_EVENT_NAMES, V2_EVENT_NAMES
from core.triage import Complexity, TriageResult
from core.variants import (
    _ALIASES,
    _VARIANTS,
    VariantDescriptor,
    VariantExecutionRequest,
    VariantOutcome,
    canonical_variant_id,
    register_variant,
    variant_capabilities,
)
from core.variants.classic import ClassicVariantRuntime
from core.variants.traditional import TraditionalVariant


def test_capabilities_are_authoritative_and_canonical():
    document = variant_capabilities()

    assert document["api_version"] == "1"
    classic = next(
        item for item in document["variants"] if item["id"] == "classic"
    )
    assert classic["available"] is True
    assert classic["aliases"] == ["traditional"]
    assert classic["configuration_schema_version"] == "1"
    assert classic["supports_recovery"] is True
    assert classic["required_agent_features"]
    assert "abort" in classic["features"]["controls"]
    assert "artifacts" in classic["features"]["panels"]
    assert classic["features"]["events"] == sorted({
        *V2_EVENT_NAMES,
        *LEGACY_EVENT_NAMES,
        "initial_state",
        "error",
        "ag_fallback",
    })


def test_legacy_classic_identifier_resolves_to_canonical_runtime():
    assert canonical_variant_id("traditional") == "classic"


def test_classic_configuration_migrates_legacy_metadata():
    migrated = ClassicVariantRuntime.configuration_from_metadata({
        "effective_task_config": {
            "traditional": {"max_rounds": 7},
            "model_pools": {"medium": ["model-a"]},
        },
        "effective_routing": {"medium": "model-a"},
        "effective_registry": {"planner": {"profile": "planner"}},
    })

    assert migrated is not None
    assert migrated["variant"] == "classic"
    assert migrated["configuration_schema_version"] == "1"
    assert migrated["settings"]["classic"]["max_rounds"] == 7
    assert "traditional" not in migrated["settings"]


def test_classic_configuration_rejects_future_schema():
    with pytest.raises(ValueError, match="Unsupported classic configuration"):
        ClassicVariantRuntime.configuration_from_metadata({
            "effective_configuration": {
                "variant": "classic",
                "configuration_schema_version": "999",
            }
        })


@pytest.mark.asyncio
async def test_classic_configuration_captures_immutable_model_pricing(
    monkeypatch,
):
    pricing = {
        "saved-model": {
            "input_cost_per_token": 0.01,
            "output_cost_per_token": 0.02,
            "source": "saved",
        }
    }
    monkeypatch.setattr(classic_module, "MODEL_PRICING", pricing)

    configuration = await ClassicVariantRuntime.capture_configuration()
    pricing["saved-model"]["input_cost_per_token"] = 9.0

    assert configuration["settings"]["model_pricing"] == {
        "saved-model": {
            "input_cost_per_token": 0.01,
            "output_cost_per_token": 0.02,
            "source": "saved",
        }
    }


@pytest.mark.asyncio
async def test_registered_variant_controls_runtime_execution():
    calls = []

    class FakeRuntime:
        descriptor = VariantDescriptor("test_runtime", "Test runtime", "1")

        @classmethod
        async def capture_configuration(cls, overrides=None):
            return {"configuration_schema_version": "1"}

        @classmethod
        def configuration_from_metadata(cls, metadata):
            return None

        @classmethod
        async def run(cls, host, request):
            calls.append((host, request.task_id))
            return VariantOutcome(
                variant_id=cls.descriptor.id,
                answer="done",
                result={"answer": "done"},
                public_result={
                    "task_id": request.task_id,
                    "variant": cls.descriptor.id,
                },
            )

    register_variant("test_runtime", FakeRuntime)
    host = Orchestrator.__new__(Orchestrator)
    host._complete_variant_task = AsyncMock()
    request = VariantExecutionRequest(
        task_id="task-runtime",
        session_id="session",
        user_task="test",
        triage=None,
    )
    try:
        result = await host._run_variant("test_runtime", request)
    finally:
        _VARIANTS.pop("test_runtime", None)
        _ALIASES.pop("test_runtime", None)

    assert result == {"task_id": "task-runtime", "variant": "test_runtime"}
    assert calls == [(host, "task-runtime")]
    host._complete_variant_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_dispatch_uses_the_stable_activation_as_turn_identity():
    host = Orchestrator.__new__(Orchestrator)
    host._dispatch_turn = AsyncMock(return_value={"status": "completed"})

    result = await host.dispatch_agent(
        task_id="task-stable",
        activation_id="activation-stable",
        role="expert",
        description="question",
        persona="persona",
    )

    assert result == {"status": "completed"}
    host._dispatch_turn.assert_awaited_once_with(
        task_id="task-stable",
        activation_id="activation-stable",
        turn_id="activation-stable",
        role="expert",
        description="question",
        persona="persona",
    )


@pytest.mark.asyncio
async def test_public_dispatch_rejects_an_empty_activation_identity():
    host = Orchestrator.__new__(Orchestrator)
    host._dispatch_turn = AsyncMock()

    with pytest.raises(ValueError, match="activation_id"):
        await host.dispatch_agent(
            task_id="task-stable",
            activation_id=" ",
            role="expert",
        )

    host._dispatch_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_dispatch_rejects_a_stale_lease(monkeypatch):
    host = Orchestrator.__new__(Orchestrator)
    host._task_lock_ids = {"task-stale": "lease-stale"}
    host._lease_lost = {"task-stale": asyncio.Event()}
    host.bb = SimpleNamespace(owns_lock=AsyncMock(return_value=False))
    host.http = SimpleNamespace(post=AsyncMock())
    host._safe_log = AsyncMock()
    monkeypatch.setattr(
        orchestrator_module.db,
        "owns_task_lease",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        orchestrator_module.db, "create_turn", AsyncMock()
    )

    with pytest.raises(LeaseLostError):
        await host._dispatch_turn(
            role="expert",
            task_id="task-stale",
            description="question",
            persona="persona",
            endpoint="http://agent:8000",
            turn_id="activation-stale",
            activation_id="activation-stale",
        )

    host.http.post.assert_not_awaited()
    assert host._lease_lost["task-stale"].is_set()


def test_submission_contract_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        submit.TaskSubmission.model_validate({
            "task": "test",
            "unknown": True,
        })


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("objective", "code"),
    [("   ", "objective_empty"), ("abcd", "objective_too_large")],
)
async def test_submission_rejects_invalid_objectives(
    monkeypatch, objective, code,
):
    monkeypatch.setattr(submit, "MAX_TASK_CHARS", 3)
    with pytest.raises(HTTPException) as exc_info:
        await submit.submit_task(
            submit.TaskSubmission(task=objective),
            cast("Request", None),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == code


@pytest.mark.asyncio
async def test_submission_rejects_unavailable_variant(monkeypatch):
    previous_queue = submit._task_queue
    submit._task_queue = asyncio.Queue(maxsize=1)
    try:
        with pytest.raises(HTTPException) as exc_info:
            await submit.submit_task(
                submit.TaskSubmission(task="test", variant="patchboard"),
                cast("Request", None),
            )
    finally:
        submit._task_queue = previous_queue

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "variant_unavailable"


@pytest.mark.asyncio
async def test_submission_saves_configuration_before_queue_admission(monkeypatch):
    configuration = {
        "variant": "classic",
        "configuration_schema_version": "1",
    }
    capture = AsyncMock(return_value=configuration)
    monkeypatch.setattr(
        ClassicVariantRuntime,
        "capture_configuration",
        capture,
    )
    create_with_meta = AsyncMock()
    monkeypatch.setattr(
        submit.db, "create_task_with_meta", create_with_meta
    )
    monkeypatch.setattr(submit.db, "update_run_state", AsyncMock())
    previous_queue = submit._task_queue
    previous_ids = set(submit._scheduled_ids)
    submit._task_queue = asyncio.Queue(maxsize=1)
    submit._scheduled_ids.clear()
    try:
        response = await submit.submit_task(
            submit.TaskSubmission(task="test", variant="traditional"),
            cast("Request", None),
        )
        item = submit._task_queue.get_nowait()
        submit._task_queue.task_done()
    finally:
        submit._task_queue = previous_queue
        submit._scheduled_ids.clear()
        submit._scheduled_ids.update(previous_ids)

    assert response["variant"] == "classic"
    assert item.variant_id == "classic"
    assert item.effective_configuration == configuration
    create_with_meta.assert_awaited_once()
    assert (
        create_with_meta.await_args.args[4]["effective_configuration"]
        == configuration
    )


@pytest.mark.asyncio
async def test_endpoint_concurrency_fails_fast_and_reports_load(monkeypatch):
    monkeypatch.setattr(
        orchestrator_module, "AGENT_ENDPOINT_MAX_CONCURRENCY", 1
    )
    monkeypatch.setattr(
        orchestrator_module, "AGENT_ENDPOINT_WAIT_TIMEOUT_S", 0.0
    )
    host = Orchestrator.__new__(Orchestrator)
    host._endpoint_slots = {}
    host._endpoint_active = {}
    host._endpoint_waiting = {}
    host._task_lock_ids = {}

    first = await host._acquire_endpoint_slot("http://agent")
    with pytest.raises(EndpointOverloadedError):
        await host._acquire_endpoint_slot("http://agent")

    snapshot = host.runtime_snapshot()
    assert snapshot["endpoint_requests"]["http://agent"] == {
        "active": 1,
        "waiting": 0,
        "limit": 1,
        "circuit": "closed",
        "consecutive_failures": 0,
    }
    host._release_endpoint_slot("http://agent", first)
    assert host._endpoint_slots == {}
    assert host._endpoint_active == {}
    assert host._endpoint_waiting == {}


@pytest.mark.asyncio
async def test_dynamic_endpoint_capacity_state_stays_bounded(monkeypatch):
    monkeypatch.setattr(
        orchestrator_module, "AGENT_ENDPOINT_MAX_CONCURRENCY", 1
    )
    host = Orchestrator.__new__(Orchestrator)
    host._endpoint_slots = {}
    host._endpoint_active = {}
    host._endpoint_waiting = {}

    for index in range(200):
        endpoint = f"http://dynamic-{index}"
        slot = await host._acquire_endpoint_slot(endpoint)
        host._release_endpoint_slot(endpoint, slot)

    assert host._endpoint_slots == {}
    assert host._endpoint_active == {}
    assert host._endpoint_waiting == {}


@pytest.mark.asyncio
async def test_phase_update_uses_the_active_task_lease(monkeypatch):
    update_phase = AsyncMock(return_value=True)
    monkeypatch.setattr(orchestrator_module.db, "update_task_phase", update_phase)
    redis = SimpleNamespace(
        hset=AsyncMock(),
        hlen=AsyncMock(return_value=1),
        hvals=AsyncMock(return_value=[]),
    )
    host = Orchestrator.__new__(Orchestrator)
    host.bb = SimpleNamespace(redis=redis, publish_event=AsyncMock())
    host._task_lock_ids = {"task-phase": "lease-phase"}

    await host._set_phase("execute", 3, task_id="task-phase")

    update_phase.assert_awaited_once_with(
        "task-phase", "execute", lease_token="lease-phase"
    )


@pytest.mark.asyncio
async def test_cost_rollup_failure_does_not_block_task_failure(monkeypatch):
    rollup = AsyncMock(side_effect=RuntimeError("database unavailable"))
    fail_task = AsyncMock(return_value=True)
    monkeypatch.setattr(
        orchestrator_module.db, "update_task_cost_totals", rollup
    )
    monkeypatch.setattr(orchestrator_module.db, "fail_task", fail_task)
    host = Orchestrator.__new__(Orchestrator)

    assert await host._fail_task_with_cost(
        "task-failed", "provider failed", "lease-failed"
    ) is True

    rollup.assert_awaited_once_with(
        "task-failed", lease_token="lease-failed"
    )
    fail_task.assert_awaited_once_with(
        "task-failed", "provider failed", lease_token="lease-failed"
    )


@pytest.mark.asyncio
async def test_shared_completion_reports_variant_cost_to_the_ledger(
    monkeypatch,
):
    rollup = AsyncMock(return_value=True)
    monkeypatch.setattr(
        orchestrator_module.db, "update_task_cost_totals", rollup
    )
    monkeypatch.setattr(
        orchestrator_module.db, "complete_task", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        orchestrator_module.db, "get_task", AsyncMock(return_value=None)
    )
    host = Orchestrator.__new__(Orchestrator)
    host._task_lock_ids = {"task-cost": "lease-cost"}
    host._publish_task_state = AsyncMock()
    host.bb = SimpleNamespace(
        publish_result=AsyncMock(),
        publish_system_event=AsyncMock(),
    )
    request = VariantExecutionRequest(
        task_id="task-cost",
        session_id="session",
        user_task="question",
        triage=None,
    )
    outcome = VariantOutcome(
        variant_id="cost-runtime",
        answer="answer",
        result={"answer": "answer"},
        public_result={"answer": "answer"},
        cost_usd=0.4,
    )

    await host._complete_variant_task(request, outcome)

    rollup.assert_awaited_once_with(
        "task-cost",
        lease_token="lease-cost",
        reported_cost_usd=0.4,
    )


def test_classic_engine_uses_canonical_name():
    assert TraditionalVariant.name == "classic"


@pytest.mark.asyncio
async def test_nonclassic_fake_runs_through_submission_worker_and_detail(
    monkeypatch,
):
    """Run one fake runtime without entering the classic engine."""
    task_rows: dict[str, dict] = {}
    task_meta: dict[str, dict] = {}
    terminal_calls = []

    class FakeRuntime:
        descriptor = VariantDescriptor(
            "lifecycle_fake",
            "Lifecycle fake",
            "1",
            supports_recovery=True,
        )

        @classmethod
        async def capture_configuration(cls, overrides=None):
            return {
                "variant": cls.descriptor.id,
                "configuration_schema_version": "1",
                "model_routing": {"medium": "test-medium"},
            }

        @classmethod
        def configuration_from_metadata(cls, metadata):
            saved = metadata.get("effective_configuration")
            return dict(saved) if isinstance(saved, dict) else None

        @classmethod
        async def run(cls, host, request):
            assert request.effective_configuration["variant"] == cls.descriptor.id
            return VariantOutcome(
                variant_id=cls.descriptor.id,
                answer="fake answer",
                result={
                    "answer": "fake answer",
                    "terminated_by": "fake_complete",
                },
                public_result={
                    "task_id": request.task_id,
                    "variant": cls.descriptor.id,
                    "answer": "fake answer",
                },
            )

    async def create_task(task_id, label, full_input, variant="classic"):
        task_rows[task_id] = {
            "id": task_id,
            "label": label,
            "full_input": full_input,
            "status": "pending",
            "variant": variant,
            "complexity": None,
            "model_used": None,
        }

    async def upsert_meta(task_id, values):
        task_meta.setdefault(task_id, {}).update(values)

    async def create_task_with_meta(
        task_id, label, full_input, variant, metadata,
    ):
        await create_task(task_id, label, full_input, variant)
        await upsert_meta(task_id, metadata)

    async def get_task(task_id):
        row = task_rows.get(task_id)
        return dict(row) if row else None

    async def update_status(task_id, **values):
        task_rows[task_id].update({
            key: value
            for key, value in values.items()
            if key in {"status", "complexity", "model_used", "variant"}
            and value is not None
        })
        return True

    async def complete_task(task_id, result_summary, result_json, **kwargs):
        task_rows[task_id].update({
            "status": "completed",
            "result_summary": result_summary,
            "result_json": result_json,
        })
        terminal_calls.append(task_id)
        return True

    monkeypatch.setattr(
        submit.db, "create_task_with_meta", create_task_with_meta
    )
    monkeypatch.setattr(submit.db, "update_run_state", AsyncMock(return_value=True))
    monkeypatch.setattr(orchestrator_module.db, "create_task", create_task)
    monkeypatch.setattr(orchestrator_module.db, "claim_task_lease", AsyncMock(return_value=True))
    monkeypatch.setattr(orchestrator_module.db, "release_task_lease", AsyncMock(return_value=True))
    monkeypatch.setattr(orchestrator_module.db, "get_task", get_task)
    monkeypatch.setattr(
        orchestrator_module.db,
        "get_board_meta",
        lambda task_id: asyncio.sleep(0, result=dict(task_meta.get(task_id, {}))),
    )
    monkeypatch.setattr(orchestrator_module.db, "upsert_board_meta", upsert_meta)
    monkeypatch.setattr(orchestrator_module.db, "update_task_status", update_status)
    monkeypatch.setattr(orchestrator_module.db, "update_run_state", AsyncMock(return_value=True))
    monkeypatch.setattr(orchestrator_module.db, "update_task_cost_totals", AsyncMock(return_value=True))
    monkeypatch.setattr(orchestrator_module.db, "complete_task", complete_task)
    monkeypatch.setattr(orchestrator_module.db, "fail_task", AsyncMock(return_value=True))
    monkeypatch.setattr(tasks_route.db, "get_task", get_task)
    monkeypatch.setattr(tasks_route.db, "get_sub_tasks", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        tasks_route.db,
        "get_task_files_total_bytes",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        tasks_route.db,
        "get_task_artifacts_total_bytes",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        tasks_route.db,
        "get_event_delivery_health",
        AsyncMock(return_value={"pending": 0}),
    )

    class FakeBlackboard:
        redis = SimpleNamespace(get=AsyncMock(return_value=None))
        acquire_lock = AsyncMock(return_value=(True, "lease-fake"))
        release_lock = AsyncMock(return_value=True)
        publish_system_event = AsyncMock()
        publish_event = AsyncMock()
        publish_result = AsyncMock()

    host = Orchestrator.__new__(Orchestrator)
    host.bb = FakeBlackboard()
    host.triage = SimpleNamespace(classify=AsyncMock(return_value=TriageResult(
        complexity=Complexity.MEDIUM,
        litellm_model="test-medium",
    )))
    host._task_lock_ids = {}
    host._lease_lost = {}
    host._active_gateways = {}
    host._set_phase = AsyncMock()
    host._safe_log = AsyncMock()
    host._publish_task_state = AsyncMock()
    host.run_classic_runtime = AsyncMock(
        side_effect=AssertionError("The classic runtime must not run")
    )

    previous_queue = submit._task_queue
    previous_orchestrator = submit._orchestrator
    previous_ids = set(submit._scheduled_ids)
    worker = None
    register_variant("lifecycle_fake", FakeRuntime)
    try:
        submit._task_queue = asyncio.Queue(maxsize=2)
        submit._orchestrator = host
        submit._scheduled_ids.clear()
        worker = asyncio.create_task(submit._task_worker(0))
        response = await submit.submit_task(
            submit.TaskSubmission(
                task="Run the lifecycle fake",
                variant="lifecycle_fake",
            ),
            cast("Request", None),
        )
        await asyncio.wait_for(submit._task_queue.join(), timeout=1)
        detail = await tasks_route.get_task_detail(response["task_id"])
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        submit._task_queue = previous_queue
        submit._orchestrator = previous_orchestrator
        submit._scheduled_ids.clear()
        submit._scheduled_ids.update(previous_ids)
        _VARIANTS.pop("lifecycle_fake", None)
        _ALIASES.pop("lifecycle_fake", None)

    assert response["variant"] == "lifecycle_fake"
    assert terminal_calls == [response["task_id"]]
    assert detail["task"]["status"] == "completed"
    assert detail["task"]["variant"] == "lifecycle_fake"
    host.run_classic_runtime.assert_not_awaited()
    published_progress = repr(host._publish_task_state.await_args_list).lower()
    for classic_term in (
        "triage classification",
        "plan decomposition",
        "execute sub-tasks",
        "audit and consensus",
        "planner",
        "executor",
        "auditor",
    ):
        assert classic_term not in published_progress


@pytest.mark.asyncio
async def test_nonclassic_fake_restart_restores_variant_and_configuration(
    monkeypatch,
):
    configuration = {
        "variant": "restart_fake",
        "configuration_schema_version": "1",
    }

    class RestartFake:
        descriptor = VariantDescriptor(
            "restart_fake", "Restart fake", "1", supports_recovery=True
        )

        @classmethod
        async def capture_configuration(cls, overrides=None):
            return configuration

        @classmethod
        def configuration_from_metadata(cls, metadata):
            return metadata.get("effective_configuration")

        @classmethod
        async def run(cls, host, request):
            raise AssertionError("Recovery admission does not execute the task")

    previous_queue = submit._task_queue
    previous_ids = set(submit._scheduled_ids)
    register_variant("restart_fake", RestartFake)
    submit._task_queue = asyncio.Queue(maxsize=2)
    submit._scheduled_ids.clear()
    monkeypatch.setattr(submit.db, "get_blocked_tasks", AsyncMock(return_value=[]))
    monkeypatch.setattr(submit.db, "get_resumable_tasks", AsyncMock(return_value=[{
        "id": "task-restart-fake",
        "full_input": "resume fake",
        "status": "running",
        "variant": "restart_fake",
    }]))
    monkeypatch.setattr(submit.db, "get_board_meta", AsyncMock(return_value={
        "effective_configuration": configuration,
    }))

    async def stop_after_scan(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_scan)
    try:
        with pytest.raises(asyncio.CancelledError):
            await submit._recover_unfinished_tasks()
        item = submit._task_queue.get_nowait()
        submit._task_queue.task_done()
    finally:
        submit._task_queue = previous_queue
        submit._scheduled_ids.clear()
        submit._scheduled_ids.update(previous_ids)
        _VARIANTS.pop("restart_fake", None)
        _ALIASES.pop("restart_fake", None)

    assert item.variant_id == "restart_fake"
    assert item.effective_configuration == configuration
    assert item.resume is True


@pytest.mark.asyncio
async def test_unknown_runtime_is_durably_blocked_during_recovery(monkeypatch):
    previous_queue = submit._task_queue
    previous_blocked = dict(submit._recovery_blocked)
    submit._task_queue = asyncio.Queue(maxsize=2)
    submit._recovery_blocked.clear()
    monkeypatch.setattr(submit.db, "get_blocked_tasks", AsyncMock(return_value=[]))
    monkeypatch.setattr(submit.db, "get_resumable_tasks", AsyncMock(return_value=[{
        "id": "task-runtime-unavailable",
        "full_input": "resume unavailable runtime",
        "status": "running",
        "variant": "runtime-not-installed",
    }]))
    monkeypatch.setattr(submit.db, "get_board_meta", AsyncMock(return_value={
        "effective_configuration": {
            "variant": "runtime-not-installed",
            "configuration_schema_version": "1",
        },
    }))
    block_recovery = AsyncMock(return_value=True)
    monkeypatch.setattr(
        submit.db, "block_task_recovery", block_recovery
    )

    async def stop_after_scan(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_scan)
    try:
        with pytest.raises(asyncio.CancelledError):
            await submit._recover_unfinished_tasks()
        blocked_snapshot = dict(submit._recovery_blocked)
    finally:
        submit._task_queue = previous_queue
        submit._recovery_blocked.clear()
        submit._recovery_blocked.update(previous_blocked)

    block_recovery.assert_awaited_once_with("task-runtime-unavailable")
    assert blocked_snapshot == {
        "task-runtime-unavailable": "runtime-not-installed"
    }


@pytest.mark.asyncio
async def test_blocked_configuration_stays_resumable_until_supported(
    monkeypatch,
):
    previous_queue = submit._task_queue
    previous_blocked = dict(submit._recovery_blocked)
    submit._task_queue = asyncio.Queue(maxsize=2)
    submit._recovery_blocked.clear()
    monkeypatch.setattr(submit.db, "get_blocked_tasks", AsyncMock(return_value=[{
        "id": "task-blocked-config",
        "status": "running",
        "run_state": "blocked",
        "variant": "classic",
    }]))
    monkeypatch.setattr(submit.db, "get_resumable_tasks", AsyncMock(return_value=[]))
    monkeypatch.setattr(submit.db, "get_board_meta", AsyncMock(return_value={
        "effective_configuration": {
            "variant": "classic",
            "configuration_schema_version": "999",
        },
    }))
    retry = AsyncMock(return_value=True)
    monkeypatch.setattr(submit.db, "retry_blocked_task", retry)

    async def stop_after_scan(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_scan)
    try:
        with pytest.raises(asyncio.CancelledError):
            await submit._recover_unfinished_tasks()
    finally:
        submit._task_queue = previous_queue
        blocked_snapshot = dict(submit._recovery_blocked)
        submit._recovery_blocked.clear()
        submit._recovery_blocked.update(previous_blocked)

    retry.assert_not_awaited()
    assert blocked_snapshot == {"task-blocked-config": "classic"}


@pytest.mark.asyncio
async def test_blocked_configuration_reenters_recovery_when_supported(
    monkeypatch,
):
    class RestoredRuntime:
        descriptor = VariantDescriptor(
            "restored_runtime", "Restored runtime", "1",
            supports_recovery=True,
        )

        @classmethod
        async def capture_configuration(cls, overrides=None):
            return {}

        @classmethod
        def configuration_from_metadata(cls, metadata):
            return metadata.get("effective_configuration")

        @classmethod
        async def run(cls, host, request):
            raise AssertionError("The recovery scan does not execute tasks")

    previous_queue = submit._task_queue
    previous_blocked = dict(submit._recovery_blocked)
    register_variant("restored_runtime", RestoredRuntime)
    submit._task_queue = asyncio.Queue(maxsize=2)
    submit._recovery_blocked.clear()
    submit._recovery_blocked["task-supported-config"] = "restored_runtime"
    monkeypatch.setattr(submit.db, "get_blocked_tasks", AsyncMock(return_value=[{
        "id": "task-supported-config",
        "status": "running",
        "run_state": "blocked",
        "variant": "restored_runtime",
    }]))
    monkeypatch.setattr(submit.db, "get_resumable_tasks", AsyncMock(return_value=[]))
    monkeypatch.setattr(submit.db, "get_board_meta", AsyncMock(return_value={
        "effective_configuration": {
            "variant": "restored_runtime",
            "configuration_schema_version": "1",
        },
    }))
    retry = AsyncMock(return_value=True)
    monkeypatch.setattr(submit.db, "retry_blocked_task", retry)

    async def stop_after_scan(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_scan)
    try:
        with pytest.raises(asyncio.CancelledError):
            await submit._recover_unfinished_tasks()
        blocked_snapshot = dict(submit._recovery_blocked)
    finally:
        submit._task_queue = previous_queue
        submit._recovery_blocked.clear()
        submit._recovery_blocked.update(previous_blocked)
        _VARIANTS.pop("restored_runtime", None)
        _ALIASES.pop("restored_runtime", None)

    retry.assert_awaited_once_with("task-supported-config")
    assert blocked_snapshot == {}
