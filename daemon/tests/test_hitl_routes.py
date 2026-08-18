"""Contract tests for classic task control routes."""

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import routes.hitl as hitl
import routes.submit as submit


def _install_app(monkeypatch, orch):
    fake_app = types.ModuleType("app")
    fake_app.app = SimpleNamespace(
        state=SimpleNamespace(orchestrator=orch),
    )
    monkeypatch.setitem(sys.modules, "app", fake_app)


@pytest.mark.asyncio
async def test_abort_route_cancels_scheduler_and_remote_agents(monkeypatch):
    redis = SimpleNamespace(set=AsyncMock())
    orch = SimpleNamespace(
        bb=SimpleNamespace(redis=redis),
        cancel_remote_task=AsyncMock(return_value=2),
    )
    _install_app(monkeypatch, orch)
    abort_scheduled = AsyncMock(return_value=True)
    monkeypatch.setattr(submit, "abort_scheduled_task", abort_scheduled)

    result = await hitl.abort_task(
        "task-1", hitl.AbortRequest(reason="evaluation_timeout"),
        SimpleNamespace(headers={}),
    )

    assert result == {
        "status": "abort_requested",
        "task_id": "task-1",
        "scheduled": True,
        "remote_cancelled": 2,
    }
    redis.set.assert_awaited_once_with(
        "bmas:public:abort:task-1", "evaluation_timeout", ex=3600,
    )
    abort_scheduled.assert_awaited_once_with(
        "task-1", "evaluation_timeout",
    )
    orch.cancel_remote_task.assert_awaited_once_with("task-1")


@pytest.mark.asyncio
async def test_abort_continues_when_redis_is_unavailable(monkeypatch):
    redis = SimpleNamespace(set=AsyncMock(side_effect=RuntimeError("offline")))
    orch = SimpleNamespace(
        bb=SimpleNamespace(redis=redis),
        cancel_remote_task=AsyncMock(return_value=1),
    )
    _install_app(monkeypatch, orch)
    abort_scheduled = AsyncMock(return_value=True)
    monkeypatch.setattr(submit, "abort_scheduled_task", abort_scheduled)

    result = await hitl.abort_task(
        "task-1", hitl.AbortRequest(reason="operator"),
        SimpleNamespace(headers={}),
    )

    assert result["status"] == "abort_requested"
    assert result["scheduled"] is True
    assert result["remote_cancelled"] == 1
    abort_scheduled.assert_awaited_once_with("task-1", "operator")


@pytest.mark.asyncio
async def test_steer_route_uses_orchestrator_gateway(monkeypatch):
    orch = SimpleNamespace(steer_entry=AsyncMock(return_value={
        "status": "boosted",
        "entry_id": "e-1",
        "salience": 1.0,
    }))
    _install_app(monkeypatch, orch)

    result = await hitl.steer_entry(
        "task-1", hitl.SteerRequest(action="boost", entry_id="e-1"),
        SimpleNamespace(headers={}),
    )

    assert result["status"] == "boosted"
    orch.steer_entry.assert_awaited_once_with("task-1", "e-1", "boost")
