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
    monkeypatch.setattr(submit.db, "fail_task", fail_task)
    submit._scheduled_ids.add("task-queued")

    scheduled = await submit.abort_scheduled_task(
        "task-queued", "evaluation_timeout",
    )

    assert scheduled is True
    fail_task.assert_awaited_once_with(
        "task-queued", "Task aborted: evaluation_timeout",
    )


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

    async def process_task(_user_task, task_id, **kwargs):
        if task_id == "task-first":
            first_started.set()
            await asyncio.Event().wait()
        second_completed.set()

    orch = SimpleNamespace(process_task=process_task)
    monkeypatch.setattr(submit, "MAX_ACTIVE_TASKS", 1)
    monkeypatch.setattr(
        submit.db, "get_resumable_tasks", AsyncMock(return_value=[]),
    )
    fail_task = AsyncMock(return_value=True)
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
