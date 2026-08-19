"""Reliability tests for bounded classic-task admission and recovery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

import routes.submit as submit
from core.gateway import LeaseLostError
from core.orchestrator import LeaseBusyError


@pytest_asyncio.fixture(autouse=True)
async def reset_queue_state():
    await submit.stop_task_workers()
    submit._cancel_reasons.clear()
    yield
    await submit.stop_task_workers()
    submit._cancel_reasons.clear()


@pytest.mark.asyncio
async def test_workers_enforce_global_concurrency_limit(monkeypatch):
    active = 0
    peak = 0

    async def process_task(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    orch = SimpleNamespace(process_task=process_task)
    monkeypatch.setattr(submit, "MAX_ACTIVE_TASKS", 2)
    monkeypatch.setattr(submit.db, "get_resumable_tasks", AsyncMock(return_value=[]))
    monkeypatch.setattr(submit.db, "update_run_state", AsyncMock())

    await submit.start_task_workers(orch)
    assert submit._task_queue is not None
    for index in range(6):
        task_id = f"task-{index}"
        submit._scheduled_ids.add(task_id)
        submit._task_queue.put_nowait(
            submit.TaskWorkItem(task_id, "question"),
        )
    await submit._task_queue.join()

    assert peak == 2


@pytest.mark.asyncio
async def test_queued_abort_reaches_terminal_database_state(monkeypatch):
    fail_task = AsyncMock()
    rollup = AsyncMock()
    monkeypatch.setattr(submit.db, "fail_task", fail_task)
    monkeypatch.setattr(submit.db, "update_task_cost_totals", rollup)
    submit._scheduled_ids.add("task-queued")

    scheduled = await submit.abort_scheduled_task(
        "task-queued", "evaluation_timeout",
    )

    assert scheduled is True
    fail_task.assert_awaited_once_with(
        "task-queued", "Task aborted: evaluation_timeout",
    )
    rollup.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_abort_removes_the_queue_item(monkeypatch):
    submit._task_queue = asyncio.Queue(maxsize=1)
    submit._scheduled_ids.add("task-queued")
    submit._task_queue.put_nowait(
        submit.TaskWorkItem("task-queued", "question"),
    )
    monkeypatch.setattr(submit.db, "fail_task", AsyncMock(return_value=True))

    assert await submit.abort_scheduled_task("task-queued", "operator")

    assert submit._task_queue.empty()
    assert "task-queued" not in submit._scheduled_ids
    assert "task-queued" not in submit._cancel_reasons
    await asyncio.wait_for(submit._task_queue.join(), timeout=1)


@pytest.mark.asyncio
async def test_unknown_abort_does_not_leak_a_cancel_reason(monkeypatch):
    fail_task = AsyncMock()
    monkeypatch.setattr(submit.db, "fail_task", fail_task)

    assert not await submit.abort_scheduled_task("task-unknown", "operator")

    assert "task-unknown" not in submit._cancel_reasons
    fail_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_survives_an_active_task_abort(monkeypatch):
    first_started = asyncio.Event()
    second_completed = asyncio.Event()
    terminal_steps = []

    async def process_task(_user_task, task_id, **kwargs):
        if task_id == "task-first":
            first_started.set()
            await asyncio.Event().wait()
        second_completed.set()

    async def rollup_task_cost(task_id, lease_token):
        terminal_steps.append(("rollup", task_id, lease_token))
        return True

    def task_lease_token(task_id):
        assert task_id == "task-first"
        return "lease-first"

    orch = SimpleNamespace(
        process_task=process_task,
        rollup_task_cost=rollup_task_cost,
        task_lease_token=task_lease_token,
    )
    monkeypatch.setattr(submit, "MAX_ACTIVE_TASKS", 1)
    monkeypatch.setattr(
        submit.db, "get_resumable_tasks", AsyncMock(return_value=[]),
    )
    async def fail_task(task_id, message):
        terminal_steps.append(("fail", task_id, message))
        return True

    fail_task = AsyncMock(side_effect=fail_task)
    monkeypatch.setattr(submit.db, "fail_task", fail_task)

    await submit.start_task_workers(orch)
    assert submit._task_queue is not None
    submit._scheduled_ids.add("task-first")
    submit._task_queue.put_nowait(
        submit.TaskWorkItem("task-first", "first"),
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    assert await submit.abort_scheduled_task("task-first", "operator")
    submit._scheduled_ids.add("task-second")
    submit._task_queue.put_nowait(
        submit.TaskWorkItem("task-second", "second"),
    )

    await asyncio.wait_for(second_completed.wait(), timeout=1)
    await asyncio.wait_for(submit._task_queue.join(), timeout=1)

    assert submit._workers[0].done() is False
    fail_task.assert_awaited_once_with(
        "task-first", "Task aborted: operator",
    )
    assert terminal_steps == [
        ("rollup", "task-first", "lease-first"),
        ("fail", "task-first", "Task aborted: operator"),
    ]


@pytest.mark.asyncio
async def test_blocked_recovery_rotates_past_an_incompatible_full_page(
    monkeypatch,
):
    first_page = [
        {
            "id": f"task-missing-{index:03d}",
            "variant": "missing-runtime",
            "created_at": f"2026-01-01T00:00:{index:03d}Z",
        }
        for index in range(100)
    ]
    compatible_task = {
        "id": "task-compatible",
        "variant": "compatible-runtime",
        "created_at": "2026-01-02T00:00:00Z",
    }
    observed_cursors = []

    async def load_blocked(*, limit, after):
        assert limit == 100
        observed_cursors.append(after)
        if after is None:
            return first_page
        return [compatible_task]

    class CompatibleRuntime:
        @classmethod
        def configuration_from_metadata(cls, metadata):
            return metadata["effective_configuration"]

    def require_runtime(variant_id):
        if variant_id == "compatible-runtime":
            return CompatibleRuntime
        raise submit.UnknownVariantError(variant_id)

    retry = AsyncMock(return_value=True)
    monkeypatch.setattr(submit.db, "get_blocked_tasks", load_blocked)
    monkeypatch.setattr(
        submit.db,
        "get_board_meta",
        AsyncMock(return_value={"effective_configuration": {"version": "1"}}),
    )
    monkeypatch.setattr(submit.db, "retry_blocked_task", retry)
    monkeypatch.setattr(submit, "require_variant_class", require_runtime)

    await submit._retry_compatible_blocked_tasks()
    await submit._retry_compatible_blocked_tasks()

    assert observed_cursors == [
        None,
        (
            first_page[-1]["created_at"],
            first_page[-1]["id"],
        ),
    ]
    retry.assert_awaited_once_with("task-compatible")


@pytest.mark.asyncio
async def test_operator_resume_enqueues_one_compatible_blocked_task(monkeypatch):
    submit._task_queue = asyncio.Queue(maxsize=2)

    class CompatibleRuntime:
        @classmethod
        def configuration_from_metadata(cls, metadata):
            return metadata["effective_configuration"]

    monkeypatch.setattr(
        submit.db,
        "get_task",
        AsyncMock(return_value={
            "id": "task-blocked",
            "variant": "classic",
            "status": "running",
            "full_input": "Continue this task",
        }),
    )
    monkeypatch.setattr(
        submit.db,
        "get_board_meta",
        AsyncMock(return_value={
            "effective_configuration": {"version": "1"},
            "submission_overrides": {"routing": {"medium": "model-a"}},
        }),
    )
    retry = AsyncMock(return_value=True)
    monkeypatch.setattr(submit.db, "retry_blocked_task", retry)
    monkeypatch.setattr(submit, "require_variant_class", lambda _variant: CompatibleRuntime)

    assert await submit.resume_blocked_task("task-blocked") is True

    item = submit._task_queue.get_nowait()
    submit._task_queue.task_done()
    assert item.task_id == "task-blocked"
    assert item.resume is True
    assert item.effective_configuration == {"version": "1"}
    assert item.overrides == {"routing": {"medium": "model-a"}}
    retry.assert_awaited_once_with("task-blocked")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [LeaseBusyError("busy"), LeaseLostError("lost")],
)
async def test_lease_contention_never_marks_task_failed(monkeypatch, error):
    orch = SimpleNamespace(
        process_task=AsyncMock(side_effect=error),
        bb=SimpleNamespace(publish_event=AsyncMock()),
    )
    fail_task = AsyncMock()
    monkeypatch.setattr(submit.db, "fail_task", fail_task)

    await submit._run_task_safe(orch, "task-lease", "question", resume=True)

    fail_task.assert_not_awaited()
    orch.bb.publish_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_restores_persisted_task_overrides(monkeypatch):
    submit._task_queue = asyncio.Queue(maxsize=10)
    monkeypatch.setattr(submit.db, "get_resumable_tasks", AsyncMock(return_value=[{
        "id": "task-resume",
        "full_input": "question",
        "status": "running",
    }]))
    monkeypatch.setattr(submit.db, "get_blocked_tasks", AsyncMock(return_value=[]))
    monkeypatch.setattr(submit.db, "get_board_meta", AsyncMock(return_value={
        "submission_overrides": {
            "routing": {"complex": "chosen-model"},
        },
    }))

    async def stop_after_scan(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_scan)
    with pytest.raises(asyncio.CancelledError):
        await submit._recover_unfinished_tasks()

    item = submit._task_queue.get_nowait()
    assert item.resume is True
    assert item.overrides == {
        "routing": {"complex": "chosen-model"},
    }
