"""Contract tests for classic task control routes."""

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

import routes.hitl as hitl
import routes.submit as submit


def _install_app(monkeypatch, orch):
    fake_app = types.ModuleType("app")
    fake_app.app = SimpleNamespace(
        state=SimpleNamespace(orchestrator=orch),
    )
    monkeypatch.setitem(sys.modules, "app", fake_app)


def _install_agent_proxy(monkeypatch, responses):
    posts = []

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            posts.append((url, kwargs))
            return responses.pop(0)

    monkeypatch.setattr(hitl.httpx, "AsyncClient", FakeAsyncClient)
    return posts


class _AgentResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def durable_operator_audit(monkeypatch):
    """Keep route tests focused on action delivery."""
    monkeypatch.setattr(
        hitl,
        "_begin_operator_action",
        AsyncMock(return_value=("action-test", "operator", None)),
    )
    monkeypatch.setattr(hitl, "_finish_operator_action", AsyncMock())
    monkeypatch.setattr(
        hitl.db,
        "request_task_cancellation",
        AsyncMock(return_value=True),
    )


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


@pytest.mark.asyncio
async def test_resume_route_requeues_a_blocked_task(monkeypatch):
    redis = SimpleNamespace(delete=AsyncMock())
    orch = SimpleNamespace(bb=SimpleNamespace(redis=redis))
    _install_app(monkeypatch, orch)
    monkeypatch.setattr(
        hitl.db,
        "get_task",
        AsyncMock(return_value={"id": "task-1", "run_state": "blocked"}),
    )
    resume_blocked = AsyncMock(return_value=True)
    monkeypatch.setattr(submit, "resume_blocked_task", resume_blocked)

    result = await hitl.resume_task("task-1", SimpleNamespace(headers={}))

    assert result == {"status": "recovery_queued", "task_id": "task-1"}
    resume_blocked.assert_awaited_once_with("task-1")
    redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_route_returns_the_incompatible_configuration_cause(monkeypatch):
    redis = SimpleNamespace(delete=AsyncMock())
    orch = SimpleNamespace(bb=SimpleNamespace(redis=redis))
    _install_app(monkeypatch, orch)
    monkeypatch.setattr(
        hitl.db,
        "get_task",
        AsyncMock(return_value={"id": "task-1", "run_state": "blocked"}),
    )
    monkeypatch.setattr(
        submit,
        "resume_blocked_task",
        AsyncMock(side_effect=hitl.VariantConfigurationError("saved model is unavailable")),
    )

    with pytest.raises(hitl.HTTPException) as raised:
        await hitl.resume_task("task-1", SimpleNamespace(headers={}))

    assert raised.value.status_code == 409
    assert "saved model is unavailable" in raised.value.detail


@pytest.mark.parametrize("choice", ["once", "session", "always", "deny"])
def test_approval_request_accepts_each_hermes_choice(choice):
    request = hitl.ApprovalRequest(run_id="run-1", choice=choice)

    assert request.choice == choice


def test_approval_request_maps_legacy_approve_decision_to_once():
    request = hitl.ApprovalRequest(run_id="run-1", decision="approve")

    assert request.choice == "once"


def test_approval_request_rejects_an_unknown_choice():
    with pytest.raises(ValidationError):
        hitl.ApprovalRequest(run_id="run-1", choice="later")


@pytest.mark.asyncio
async def test_approval_forwards_exact_choice_contract(monkeypatch):
    publish_event = AsyncMock()
    orch = SimpleNamespace(bb=SimpleNamespace(publish_event=publish_event))
    _install_app(monkeypatch, orch)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ROLE_REGISTRY", {
        "researcher": {"endpoints": ["http://agent-a", "http://agent-a"]},
        "critic": {"endpoints": ["http://agent-b"]},
    })
    monkeypatch.setattr(config, "AGENT_ENDPOINTS", {
        "researcher": "http://agent-a",
    })
    monkeypatch.setattr(config, "BMAS_EXECUTE_KEY", "execute-secret")
    posts = _install_agent_proxy(monkeypatch, [
        _AgentResponse(404),
        _AgentResponse(200, {"resolved": 1}),
    ])

    result = await hitl.handle_approval(
        "task-1",
        hitl.ApprovalRequest(
            run_id="run-1",
            choice="session",
            reason="Operator approved this session",
        ),
        SimpleNamespace(headers={}),
    )

    assert [post[0] for post in posts] == [
        "http://agent-a/v1/runs/run-1/approval",
        "http://agent-b/v1/runs/run-1/approval",
    ]
    assert posts[1][1]["json"] == {"choice": "session"}
    assert posts[1][1]["headers"] == {
        "Authorization": "Bearer execute-secret",
    }
    assert result == {
        "status": "forwarded",
        "task_id": "task-1",
        "run_id": "run-1",
        "choice": "session",
        "resolved": 1,
    }
    publish_event.assert_awaited_once_with("task-1", "approval_response", {
        "run_id": "run-1",
        "choice": "session",
        "reason": "Operator approved this session",
        "by": "operator",
        "status": "responded",
    })


@pytest.mark.asyncio
async def test_run_steer_preserves_board_steer_and_forwards_live_input(monkeypatch):
    publish_event = AsyncMock()
    orch = SimpleNamespace(bb=SimpleNamespace(publish_event=publish_event))
    _install_app(monkeypatch, orch)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ROLE_REGISTRY", {})
    monkeypatch.setattr(config, "AGENT_ENDPOINTS", {
        "researcher": "http://agent-a",
    })
    monkeypatch.setattr(config, "BMAS_EXECUTE_KEY", "")
    posts = _install_agent_proxy(monkeypatch, [
        _AgentResponse(202, {"accepted": True}),
    ])

    result = await hitl.steer_run(
        "task-1",
        hitl.RunSteerRequest(
            run_id="run-1",
            input="Focus on the audit evidence.",
        ),
        SimpleNamespace(headers={}),
    )

    assert posts == [(
        "http://agent-a/v1/runs/run-1/steer",
        {
            "json": {"input": "Focus on the audit evidence."},
            "headers": None,
        },
    )]
    assert result == {
        "status": "accepted",
        "task_id": "task-1",
        "run_id": "run-1",
        "accepted": True,
    }
    publish_event.assert_awaited_once_with("task-1", "run_steered", {
        "run_id": "run-1",
        "input": "Focus on the audit evidence.",
        "by": "operator",
    })


@pytest.mark.asyncio
async def test_run_action_returns_not_found_when_each_node_returns_404(monkeypatch):
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ROLE_REGISTRY", {})
    monkeypatch.setattr(config, "AGENT_ENDPOINTS", {
        "researcher": "http://agent-a",
        "critic": "http://agent-b",
    })
    _install_agent_proxy(monkeypatch, [
        _AgentResponse(404),
        _AgentResponse(404),
    ])

    with pytest.raises(hitl.HTTPException) as raised:
        await hitl._forward_run_action("run-1", "steer", {"input": "Continue"})

    assert raised.value.status_code == 404
