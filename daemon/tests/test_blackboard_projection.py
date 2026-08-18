"""Incremental and bounded Redis board projection tests."""

from __future__ import annotations

from typing import Any

import pytest

from core.blackboard import Blackboard


class FakePipeline:
    """Execute the Redis commands used by the board projection."""

    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.commands: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def enqueue(*args, **kwargs):
            self.commands.append((name, args, kwargs))
            return self

        return enqueue

    async def execute(self) -> list[Any]:
        self.redis.executions.append(list(self.commands))
        results = []
        for name, args, kwargs in self.commands:
            if name == "hgetall":
                results.append(dict(self.redis.hashes.get(args[0], {})))
            elif name == "hkeys":
                results.append(list(self.redis.hashes.get(args[0], {})))
            elif name == "hget":
                results.append(self.redis.hashes.get(args[0], {}).get(args[1]))
            elif name == "hset":
                target = self.redis.hashes.setdefault(args[0], {})
                target.update(kwargs["mapping"])
                results.append(len(kwargs["mapping"]))
            elif name == "hdel":
                target = self.redis.hashes.setdefault(args[0], {})
                for field in args[1:]:
                    target.pop(field, None)
                results.append(1)
            elif name == "persist":
                results.append(True)
            else:
                raise AssertionError(f"Unexpected pipeline command: {name}")
        return results


class FakeRedis:
    """Keep Redis hashes in memory for exact hydration checks."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.executions: list[list[tuple[str, tuple, dict]]] = []

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))


@pytest.mark.asyncio
async def test_projection_updates_only_changed_entries_and_hydrates_exactly():
    board = Blackboard()
    redis = FakeRedis()
    board.redis = redis  # type: ignore[assignment]
    entries = {
        "e-1": {"id": "e-1", "body": "one"},
        "e-2": {"id": "e-2", "body": "two"},
    }

    await board.save_board_snapshot("task-projection", entries, {"round": 1})
    execution_count = len(redis.executions)
    await board.save_board_snapshot("task-projection", entries, {"round": 1})
    assert len(redis.executions) == execution_count

    await board.save_board_snapshot(
        "task-projection",
        {"e-2": {"id": "e-2", "body": "changed"}},
        {"phase": "review"},
    )
    write_commands = redis.executions[-1]
    entry_hsets = [
        command
        for command in write_commands
        if command[0] == "hset" and command[1][0].endswith(":entries")
    ]
    assert list(entry_hsets[0][2]["mapping"]) == ["e-2"]
    assert any(command[0] == "hdel" and "e-1" in command[1] for command in write_commands)

    snapshot = await board.get_board_snapshot("task-projection")
    assert snapshot == {
        "entries": [{"id": "e-2", "body": "changed", "seq": 2}],
        "meta": {"phase": "review"},
    }


@pytest.mark.asyncio
async def test_projection_cache_evicts_least_recent_task(monkeypatch):
    monkeypatch.setenv("BMAS_BOARD_PROJECTION_CACHE_TASKS", "2")
    board = Blackboard()
    board.redis = FakeRedis()  # type: ignore[assignment]

    for task_id in ("task-1", "task-2", "task-3"):
        await board.save_board_snapshot(
            task_id,
            {"e-1": {"id": "e-1", "body": task_id}},
            {"round": 1},
        )

    assert list(board._projection_revision) == ["task-2", "task-3"]
    assert set(board._projection_digests) == {"task-2", "task-3"}
    assert set(board._projection_meta) == {"task-2", "task-3"}
