"""Generic lifecycle and recovery telemetry tests."""

from __future__ import annotations

import pytest

import database as db
from core.orchestrator import Orchestrator


@pytest.fixture
async def lifecycle_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "lifecycle.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    await db.init_db()
    return db_path


@pytest.mark.asyncio
async def test_resumable_tasks_exclude_blocked_and_retry_clears_block(lifecycle_db):
    await db.create_task("task-ready", "ready", "ready", variant="classic")
    await db.create_task("task-blocked", "blocked", "blocked", variant="future")
    await db.update_run_state("task-blocked", "blocked")

    assert [task["id"] for task in await db.get_resumable_tasks()] == ["task-ready"]
    assert [task["id"] for task in await db.get_blocked_tasks()] == ["task-blocked"]
    assert await db.retry_blocked_task("task-blocked") is True
    assert [task["id"] for task in await db.get_resumable_tasks()] == [
        "task-ready",
        "task-blocked",
    ]


@pytest.mark.asyncio
async def test_unknown_runtime_block_survives_restart_and_can_retry(lifecycle_db):
    await db.create_task(
        "task-runtime-missing",
        "missing",
        "missing",
        variant="plugin-runtime",
    )

    assert await db.block_task_recovery("task-runtime-missing") is True
    assert await db.get_resumable_tasks() == []

    blocked = await db.get_blocked_tasks()
    assert [task["id"] for task in blocked] == ["task-runtime-missing"]
    assert blocked[0]["variant"] == "plugin-runtime"

    assert await db.retry_blocked_task("task-runtime-missing") is True
    resumable = await db.get_resumable_tasks()
    assert [task["id"] for task in resumable] == ["task-runtime-missing"]


@pytest.mark.asyncio
async def test_blocked_task_pages_advance_with_a_stable_cursor(lifecycle_db):
    for task_id in ("task-blocked-a", "task-blocked-b", "task-blocked-c"):
        await db.create_task(task_id, task_id, task_id, variant="future")
        await db.update_run_state(task_id, "blocked")

    first_page = await db.get_blocked_tasks(limit=2)
    cursor = (first_page[-1]["created_at"], first_page[-1]["id"])
    second_page = await db.get_blocked_tasks(limit=2, after=cursor)

    task_ids = [task["id"] for task in [*first_page, *second_page]]
    assert task_ids == [
        "task-blocked-a",
        "task-blocked-b",
        "task-blocked-c",
    ]


@pytest.mark.asyncio
async def test_task_and_recovery_metadata_are_created_atomically(lifecycle_db):
    await db.create_task_with_meta(
        "task-atomic",
        "atomic",
        "atomic",
        "classic",
        {"effective_configuration": {"max_rounds": 4}},
    )

    task = await db.get_task("task-atomic")
    assert task is not None
    assert task["variant"] == "classic"
    assert await db.get_board_meta("task-atomic") == {
        "effective_configuration": {"max_rounds": 4}
    }

    with pytest.raises(TypeError):
        await db.create_task_with_meta(
            "task-rollback",
            "rollback",
            "rollback",
            "classic",
            {"invalid": object()},
        )
    assert await db.get_task("task-rollback") is None


@pytest.mark.asyncio
async def test_create_task_defaults_to_classic(lifecycle_db):
    await db.create_task("task-default", "default", "default")
    task = await db.get_task("task-default")
    assert task is not None
    assert task["variant"] == "classic"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "declined", "failed", "timeout"])
async def test_effective_action_counts_each_terminal_activation_once(
    lifecycle_db, terminal_status
):
    task_id = f"task-{terminal_status}"
    turn_id = f"turn-{terminal_status}"
    await db.create_task(task_id, "test", "test")
    await db.create_turn(
        {
            "id": turn_id,
            "task_id": task_id,
            "role": "expert",
            "status": "running",
        }
    )

    await db.complete_turn(turn_id, terminal_status, 0, 0.0)
    await db.complete_turn(turn_id, terminal_status, 0, 0.0)

    task = await db.get_task(task_id)
    assert task is not None
    assert task["effective_actions"] == 1
    assert task["checkpoint_at"] is None


@pytest.mark.asyncio
async def test_terminal_turn_records_the_endpoint_that_returned(lifecycle_db):
    await db.create_task("task-node", "test", "test")
    await db.create_turn({
        "id": "turn-node",
        "task_id": "task-node",
        "role": "expert",
        "node": "http://node-a:8000",
        "status": "running",
    })

    await db.complete_turn(
        "turn-node",
        "completed",
        0,
        0.0,
        node="http://node-b:8000",
    )

    turns = await db.get_turns("task-node")
    assert turns[0]["node"] == "http://node-b:8000"


@pytest.mark.asyncio
async def test_variant_metrics_are_namespaced_without_replacing_raw_json(lifecycle_db):
    await db.create_task("task-metrics", "test", "test")
    await db.update_task_status("task-metrics", status="running")
    result_json = '{"variant_metrics":{"pressure":3},"answer":"ok"}'
    await db.complete_task("task-metrics", "ok", result_json)

    task = await db.get_task("task-metrics")
    assert task is not None
    assert task["result_json"] == result_json
    assert task["variant_metrics"] == {"pressure": 3}


@pytest.mark.asyncio
async def test_explicit_checkpoint_and_phase_support_lease_fencing(lifecycle_db):
    await db.create_task("task-fenced", "test", "test")
    assert await db.claim_task_lease("task-fenced", "lease-a") is True

    assert await db.update_task_phase("task-fenced", "execution", "wrong") is False
    assert await db.mark_task_checkpoint("task-fenced", "wrong") is False
    assert await db.update_task_phase("task-fenced", "execution", "lease-a") is True
    assert await db.mark_task_checkpoint("task-fenced", "lease-a") is True

    task = await db.get_task("task-fenced")
    assert task is not None
    assert task["phase"] == "execution"
    assert task["checkpoint_at"] is not None


@pytest.mark.asyncio
async def test_failed_task_rolls_up_partial_cost_before_terminal_state(
    lifecycle_db,
):
    await db.create_task("task-failed-cost", "test", "test")
    assert await db.claim_task_lease(
        "task-failed-cost", "lease-failed-cost"
    ) is True
    await db.insert_cost_entry_v2(
        task_id="task-failed-cost",
        model="test-model",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.25,
        phase="execution",
    )
    host = Orchestrator.__new__(Orchestrator)

    assert await host._fail_task_with_cost(
        "task-failed-cost",
        "provider failed",
        "lease-failed-cost",
    ) is True

    task = await db.get_task("task-failed-cost")
    assert task is not None
    assert task["status"] == "failed"
    assert task["total_cost_usd"] == pytest.approx(0.25)
    assert task["total_tokens"] == 15


@pytest.mark.asyncio
async def test_reported_runtime_cost_is_a_floor_not_an_added_cost(lifecycle_db):
    await db.create_task("task-cost-floor", "test", "test")
    assert await db.claim_task_lease(
        "task-cost-floor", "lease-cost-floor"
    ) is True
    await db.insert_cost_entry_v2(
        task_id="task-cost-floor",
        model="test-model",
        input_tokens=4,
        output_tokens=6,
        cost_usd=0.25,
        phase="execution",
    )

    assert await db.update_task_cost_totals(
        "task-cost-floor",
        lease_token="lease-cost-floor",
        reported_cost_usd=0.25,
    ) is True
    task = await db.get_task("task-cost-floor")
    assert task is not None
    assert task["total_cost_usd"] == pytest.approx(0.25)
    assert task["total_tokens"] == 10

    assert await db.update_task_cost_totals(
        "task-cost-floor",
        lease_token="lease-cost-floor",
        reported_cost_usd=0.4,
    ) is True
    task = await db.get_task("task-cost-floor")
    assert task is not None
    assert task["total_cost_usd"] == pytest.approx(0.4)

    assert await db.update_task_cost_totals(
        "task-cost-floor",
        lease_token="lease-cost-floor",
    ) is True
    task = await db.get_task("task-cost-floor")
    assert task is not None
    assert task["total_cost_usd"] == pytest.approx(0.4)
