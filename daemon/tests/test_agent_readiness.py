"""Tests for capability-aware Hermes node readiness probes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from monitoring.health_loop import _check_agent_health as check_loop_health
from routes.health import _agent_readiness_detail
from routes.health import _check_agent_health as check_route_health


def _response(status_code: int, body: dict) -> SimpleNamespace:
    def raise_for_status() -> None:
        if status_code >= 400:
            raise RuntimeError(f"HTTP {status_code}")

    return SimpleNamespace(
        status_code=status_code,
        headers={"content-type": "application/json"},
        json=lambda: body,
        raise_for_status=raise_for_status,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", [check_route_health, check_loop_health])
async def test_detailed_probe_reports_capability_readiness(probe):
    client = SimpleNamespace(get=AsyncMock(return_value=_response(200, {
        "ready": True,
        "runs_api_ready": True,
        "hermes_status": "ready",
        "hermes_version": "0.20.4",
        "capabilities": {"features": {"run_steer": True}},
        "missing_required_features": [],
        "missing_required_endpoints": [],
    })))

    result = await probe(client, "expert", "http://agent")

    assert result["alive"] is True
    assert result["ready"] is True
    assert result["runs_api_ready"] is True
    assert result["probe"] == "detailed"
    client.get.assert_awaited_once_with(
        "http://agent/health/detailed", headers=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", [check_route_health, check_loop_health])
async def test_legacy_probe_stays_alive_but_not_ready(probe):
    client = SimpleNamespace(get=AsyncMock(side_effect=[
        _response(404, {}),
        _response(200, {"status": "ok"}),
    ]))

    result = await probe(client, "expert", "http://agent")

    assert result["alive"] is True
    assert result["ready"] is False
    assert result["probe"] == "legacy"
    assert client.get.await_count == 2


def test_readiness_detail_names_missing_contract_items():
    detail = _agent_readiness_detail({
        "planner": {
            "ready": False,
            "missing_required_features": ["run_events_sse"],
            "missing_required_endpoints": ["run_stop"],
        },
        "expert": {
            "ready": True,
            "missing_required_features": [],
            "missing_required_endpoints": [],
        },
    })

    assert detail == (
        "1/2 execution endpoints are ready. Missing contract items: "
        "planner: run_events_sse, run_stop."
    )
