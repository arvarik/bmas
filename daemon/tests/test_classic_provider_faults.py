"""Provider fault and endpoint circuit tests for classic dispatch."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import database as db
from core.circuit_breaker import EndpointCircuitBreaker
from core.orchestrator import Orchestrator


def _orchestrator(post) -> Orchestrator:
    orchestrator = object.__new__(Orchestrator)
    orchestrator.http = SimpleNamespace(post=post)
    orchestrator._safe_log = AsyncMock()
    orchestrator._assert_dispatch_lease = AsyncMock()
    orchestrator._agent_circuits = EndpointCircuitBreaker(
        failure_threshold=3,
        recovery_timeout_s=30,
    )
    return orchestrator


def _response(payload: dict, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


@pytest.fixture(autouse=True)
def _no_database_writes(monkeypatch):
    monkeypatch.setattr(db, "create_turn", AsyncMock())
    monkeypatch.setattr(db, "complete_turn", AsyncMock())
    monkeypatch.setattr(db, "insert_cost_entry_v2", AsyncMock())
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadError("truncated stream"),
        httpx.ReadTimeout("slow stream"),
        json.JSONDecodeError("malformed", "{", 1),
        ValueError("empty response"),
    ],
    ids=[
        "connection-failure",
        "truncated-stream",
        "slow-stream",
        "malformed-json",
        "empty-response",
    ],
)
async def test_provider_faults_return_failed_without_finding_text(failure):
    calls = 0

    async def post(url, **kwargs):
        nonlocal calls
        calls += 1
        if isinstance(failure, json.JSONDecodeError):
            response = _response({})
            response.json.side_effect = failure
            return response
        if isinstance(failure, ValueError):
            return _response({})
        raise failure

    orchestrator = _orchestrator(post)
    result = await orchestrator._dispatch_turn(
        role="expert",
        task_id="task-provider-fault",
        description="question",
        persona="persona",
        actor="expert.alpha",
        model="test-model",
        endpoint="http://node-a:8000",
        turn_id="activation-provider-fault",
        activation_id="activation-provider-fault",
    )

    assert result["status"] == "failed"
    assert result["result"]
    assert "entries" not in result
    assert calls == 3


@pytest.mark.asyncio
async def test_invalid_or_empty_agent_status_is_a_protocol_failure():
    responses = [
        _response({"result": "failure text"}),
        _response({"status": "unknown", "result": "failure text"}),
        _response({}),
    ]

    async def post(url, **kwargs):
        return responses.pop(0)

    orchestrator = _orchestrator(post)
    result = await orchestrator._dispatch_turn(
        role="expert",
        task_id="task-invalid-status",
        description="question",
        persona="persona",
        endpoint="http://node-a:8000",
    )

    assert result["status"] == "failed"
    assert "invalid response" in result["result"]
    assert orchestrator._agent_circuits.status("http://node-a:8000") == "open"


@pytest.mark.asyncio
async def test_rate_limit_opens_circuit_and_next_activation_uses_fallback():
    calls: list[str] = []
    request = httpx.Request("POST", "http://node-a:8000/execute")
    rate_limited = httpx.Response(429, request=request)

    async def post(url, **kwargs):
        calls.append(url)
        if url.startswith("http://node-a"):
            raise httpx.HTTPStatusError(
                "rate limited",
                request=request,
                response=rate_limited,
            )
        return _response({"status": "completed", "result": "valid answer"})

    orchestrator = _orchestrator(post)
    first = await orchestrator._dispatch_turn(
        role="expert",
        task_id="task-rate-limit",
        description="question",
        persona="persona",
        endpoint="http://node-a:8000",
        endpoints=["http://node-a:8000", "http://node-b:8000"],
        turn_id="activation-rate-limit-a",
    )
    second = await orchestrator._dispatch_turn(
        role="expert",
        task_id="task-rate-limit",
        description="question",
        persona="persona",
        endpoint="http://node-a:8000",
        endpoints=["http://node-a:8000", "http://node-b:8000"],
        turn_id="activation-rate-limit-b",
    )

    assert first["status"] == "failed"
    assert second["status"] == "completed"
    assert second["endpoint"] == "http://node-b:8000"
    assert calls[:4] == ["http://node-a:8000/execute"] * 4
    assert calls[-1] == "http://node-b:8000/execute"


@pytest.mark.asyncio
async def test_connection_failure_uses_fallback_without_opening_both_circuits():
    calls: list[str] = []

    async def post(url, **kwargs):
        calls.append(url)
        if url.startswith("http://node-a"):
            raise httpx.ConnectError("offline")
        return _response({"status": "completed", "result": "valid answer"})

    orchestrator = _orchestrator(post)
    result = await orchestrator._dispatch_turn(
        role="expert",
        task_id="task-fallback",
        description="question",
        persona="persona",
        endpoints=["http://node-a:8000", "http://node-b:8000"],
    )

    assert result["status"] == "completed"
    assert result["endpoint"] == "http://node-b:8000"
    assert orchestrator._agent_circuits.failures("http://node-a:8000") == 1
    assert orchestrator._agent_circuits.status("http://node-b:8000") == "closed"


@pytest.mark.asyncio
async def test_cancellation_propagates_without_poisoning_the_circuit():
    async def post(url, **kwargs):
        raise asyncio.CancelledError()

    orchestrator = _orchestrator(post)
    with pytest.raises(asyncio.CancelledError):
        await orchestrator._dispatch_turn(
            role="expert",
            task_id="task-cancel",
            description="question",
            persona="persona",
            endpoint="http://node-a:8000",
        )

    assert orchestrator._agent_circuits.failures("http://node-a:8000") == 0


def test_half_open_probe_closes_circuit_after_success():
    now = 0.0

    def clock() -> float:
        return now

    breaker = EndpointCircuitBreaker(
        failure_threshold=2,
        recovery_timeout_s=10,
        clock=clock,
    )
    breaker.record_failure("node-a")
    breaker.record_failure("node-a")
    assert breaker.status("node-a") == "open"
    assert breaker.allow("node-a") is False

    now = 10.0
    assert breaker.status("node-a") == "half_open"
    assert breaker.allow("node-a") is True
    assert breaker.allow("node-a") is False
    breaker.record_success("node-a")
    assert breaker.status("node-a") == "closed"
    assert breaker.allow("node-a") is True
