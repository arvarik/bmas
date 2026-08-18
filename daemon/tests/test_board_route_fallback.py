"""Authoritative board read tests."""

from unittest.mock import AsyncMock

import pytest

import routes.tasks as tasks_route


@pytest.mark.asyncio
async def test_board_route_reads_sqlite_when_redis_projection_is_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(
        tasks_route.db,
        "get_task",
        AsyncMock(return_value={"id": "task-board", "status": "completed"}),
    )
    monkeypatch.setattr(
        tasks_route.db,
        "get_board_entries",
        AsyncMock(return_value=[
            {"id": "e-10", "body": "later", "salience": 0.4},
            {"id": "e-2", "body": "earlier", "salience": 0.8},
        ]),
    )
    monkeypatch.setattr(
        tasks_route.db,
        "get_board_meta",
        AsyncMock(return_value={"phase": "Solved", "round": 4}),
    )

    response = await tasks_route.get_task_board("task-board")

    assert response == {
        "entries": [
            {"id": "e-2", "body": "earlier", "salience": 0.8, "seq": 2},
            {"id": "e-10", "body": "later", "salience": 0.4, "seq": 10},
        ],
        "meta": {"phase": "Solved", "round": 4},
    }


@pytest.mark.asyncio
async def test_board_route_returns_not_found_before_projection_read(monkeypatch):
    monkeypatch.setattr(tasks_route.db, "get_task", AsyncMock(return_value=None))
    get_entries = AsyncMock()
    monkeypatch.setattr(tasks_route.db, "get_board_entries", get_entries)

    response = await tasks_route.get_task_board("task-missing")

    assert response.status_code == 404
    get_entries.assert_not_awaited()
