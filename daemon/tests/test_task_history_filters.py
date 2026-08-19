"""Task history filter and priority tests."""

import aiosqlite
import pytest
import pytest_asyncio

import database as db


@pytest_asyncio.fixture
async def task_history_db(tmp_path, monkeypatch):
    path = str(tmp_path / "task-history.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    for task_id, label in (
        ("task-completed", "Quarterly report"),
        ("task-running", "Live analysis"),
        ("task-failed", "Broken export"),
        ("task-blocked", "Operator approval"),
    ):
        await db.create_task(task_id, label, f"Full input for {label}")
    async with aiosqlite.connect(path) as connection:
        await connection.execute(
            "UPDATE tasks SET status='completed', total_cost_usd=0.05 "
            "WHERE id='task-completed'"
        )
        await connection.execute(
            "UPDATE tasks SET status='running', run_state='running', "
            "total_cost_usd=0.25 WHERE id='task-running'"
        )
        await connection.execute(
            "UPDATE tasks SET status='failed', run_state='failed', "
            "total_cost_usd=1.25 WHERE id='task-failed'"
        )
        await connection.execute(
            "UPDATE tasks SET status='running', run_state='blocked', "
            "total_cost_usd=0.50 WHERE id='task-blocked'"
        )
        await connection.commit()
    return path


@pytest.mark.asyncio
async def test_task_history_prioritizes_pending_operator_work(task_history_db):
    tasks = await db.list_tasks(limit=20)

    assert [task["id"] for task in tasks] == [
        "task-blocked",
        "task-failed",
        "task-running",
        "task-completed",
    ]


@pytest.mark.asyncio
async def test_task_history_filters_search_status_and_cost(task_history_db):
    searched = await db.list_tasks(limit=20, search="quarterly")
    costly = await db.list_tasks(limit=20, min_cost=0.4, max_cost=1.0)
    failed_count = await db.count_tasks(status="failed")

    assert [task["id"] for task in searched] == ["task-completed"]
    assert [task["id"] for task in costly] == ["task-blocked"]
    assert failed_count == 1
