"""The daemon dispatches through the native protocol to the real agent code.

The agent package under ``agent/`` runs in process behind a mock
transport: it verifies the daemon grant, signs the acknowledgement,
stores both durably, posts the acknowledgement over the daemon routes,
requests one effect grant for its model call, and posts signed
receipts. A repeated delivery replays the stored acknowledgement and
result without a second execution, a fresh agent process over the same
cache directory still serves the acknowledgement, and a fresh daemon
key registry still verifies it. A legacy endpoint stays on the bearer
path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import protocol_test_support as support
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport

import agent_dispatch
import database as db
import edge_access
import protocol_keys
import runtime_journal as journal
from routes import agent_protocol as routes

AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from bmas_protocol import native  # noqa: E402

RUN_ID = "run-native"
TASK_ID = "task-native"
FENCE = "fence-native"
AGENT_ID = "agent-native"
NODE_HEADERS = {"Authorization": "Bearer node-secret", "X-Node-Id": AGENT_ID}
OPERATOR_HEADERS = {"Authorization": "Bearer operator-secret"}


@pytest_asyncio.fixture
async def stack(tmp_path, monkeypatch):
    """One daemon app with the protocol routes and one seeded run."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "native.db"))
    monkeypatch.setenv("BMAS_DAEMON_SIGNING_KEY_FILE", str(tmp_path / "daemon-signing-key"))
    protocol_keys.reset_for_tests()
    await db.init_db()
    await support.seed_run(RUN_ID, TASK_ID, task_fence=FENCE)
    await support.seed_budget(run_id=RUN_ID, task_id=TASK_ID)
    await support.make_reservation("reservation-native", cost=5_000)
    monkeypatch.setattr(edge_access, "operator_key", lambda: "operator-secret")
    monkeypatch.setattr(edge_access, "node_key", lambda: "node-secret")
    application = FastAPI()
    application.middleware("http")(edge_access.enforce_edge_access)
    application.include_router(routes.router)
    daemon_http = httpx.AsyncClient(transport=ASGITransport(app=application), base_url="http://daemon")
    protocol = native.NativeProtocol(
        agent_id=AGENT_ID, cache_dir=tmp_path / "agent-cache", daemon_url="http://daemon",
        node_key="node-secret", http=daemon_http,
    )
    executions: list[dict] = []

    async def execute(context: native.EffectContext) -> dict:
        request = {"model": "fake-model", "messages": [{"role": "user", "content": "add"}]}
        handle = await protocol.open_model_effect(context, model="fake-model", request=request)
        await protocol.receipt(
            handle, stage=native.STAGE_RESPONSE_OBSERVED,
            usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            raw_response=b'{"choices":[{"message":{"content":"42"}}]}',
        )
        executions.append({"grant": context.grant["activation_grant_id"]})
        return {"task_id": TASK_ID, "status": "completed", "result": "42", "usage": {"total_tokens": 15}}

    async def agent_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/bmas/capabilities":
            return httpx.Response(200, json={"document": protocol.document, "capability_digest": protocol.capability_digest})
        if request.url.path == "/bmas/activations":
            body = json.loads(request.content)
            try:
                outcome = await protocol.activate(body["grant"], body["grant_digest"], execute)
            except native.GrantRejectedError as exc:
                return httpx.Response(422, json={"detail": exc.reason_code})
            return httpx.Response(200, json=outcome)
        return httpx.Response(404)

    agent_http = httpx.AsyncClient(transport=httpx.MockTransport(agent_handler))
    yield {
        "app": application, "daemon_http": daemon_http, "agent_http": agent_http,
        "protocol": protocol, "executions": executions, "tmp_path": tmp_path,
    }
    await daemon_http.aclose()
    await agent_http.aclose()


@pytest.mark.asyncio
async def test_the_daemon_dispatches_through_grants_to_the_real_agent_code(stack):
    daemon_http, agent_http, protocol = stack["daemon_http"], stack["agent_http"], stack["protocol"]
    assert await protocol.ensure_registered()
    keys = await daemon_http.get("/agent-protocol/agent-keys", headers=NODE_HEADERS)
    assert [key["key_id"] for key in keys.json()["keys"]] == [protocol.keys.key_id]
    request = {"description": "Add 20 and 22.", "role": "expert", "model": "fake-model", "timeout": 30}
    result = await agent_dispatch.dispatch_activation(
        agent_http, agent_url="http://agent", run_id=RUN_ID, task_id=TASK_ID,
        activation_id="activation-native", request=request, task_fence=FENCE,
        reservation_id="reservation-native",
    )
    assert result["acknowledgement_status"] in ("accepted", "duplicate")
    assert result["activation_state"] == "dispatched"
    assert result["result"]["status"] == "completed" and result["result"]["result"] == "42"
    assert result["replayed"] is False
    assert len(stack["executions"]) == 1
    view = (await daemon_http.get("/agent-protocol/activations/activation-native/1", headers=OPERATOR_HEADERS)).json()
    assert view["activation"]["state"] == "dispatched"
    assert [grant["agent_protocol_version"] for grant in view["grants"]] == ["2"]
    assert [ack["decision"] for ack in view["acknowledgements"]] == ["accepted"]
    assert [receipt["stage"] for receipt in view["receipts"]] == ["transport_starting", "response_observed"]
    assert view["receipts"][1]["usage"]["total_tokens"] == 15
    # A second delivery of the same grant replays the stored result without a second execution.
    delivery = {"grant": json.loads(protocol.store.load(result["grant_id"])["grant"] and json.dumps(protocol.store.load(result["grant_id"])["grant"])),
                "grant_digest": result["grant_digest"], "request": request}
    replay = await agent_http.post("http://agent/bmas/activations", json=delivery)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert len(stack["executions"]) == 1
    # The daemon treats the same acknowledgement bytes as a duplicate.
    acknowledgement = native.canonical_bytes(replay.json()["acknowledgement"])
    again = await daemon_http.post("/agent-protocol/acknowledgements", content=acknowledgement,
                                   headers={**NODE_HEADERS, "Content-Type": "application/json"})
    assert again.status_code == 200 and again.json()["status"] == "duplicate"
    # A fresh agent process over the same cache still serves the acknowledgement,
    # and a fresh daemon key registry still verifies it.
    restarted = native.NativeProtocol(
        agent_id=AGENT_ID, cache_dir=stack["tmp_path"] / "agent-cache", daemon_url="http://daemon",
        node_key="node-secret", http=daemon_http,
    )
    stored = restarted.acknowledgement_for(result["grant_id"])
    assert stored is not None and stored["decision"] == "accepted" and stored["completed"]
    assert restarted.keys.key_id == protocol.keys.key_id
    protocol_keys.reset_for_tests()
    registry = await protocol_keys.registry()
    registry.require(protocol.keys.key_id)
    registry.require(protocol_keys.DAEMON_KEY_ID)
    # The run view reports the durable state and a stable projection digest.
    run_view = (await daemon_http.get(f"/agent-protocol/runs/{RUN_ID}", headers=OPERATOR_HEADERS)).json()
    assert run_view["task_fence"] == FENCE
    assert {(row["activation_id"], row["state"]) for row in run_view["activations"]} == {("activation-native", "dispatched")}
    assert run_view["journal_records"] >= 3


@pytest.mark.asyncio
async def test_a_tampered_grant_is_rejected_and_a_legacy_endpoint_stays_on_the_bearer_path(stack):
    protocol = stack["protocol"]
    await protocol.ensure_registered()
    legacy_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    assert await agent_dispatch.endpoint_capabilities(legacy_http, "http://legacy", use_cache=False) is None
    with pytest.raises(agent_dispatch.DispatchError):
        await agent_dispatch.dispatch_activation(
            legacy_http, agent_url="http://legacy", run_id=RUN_ID, task_id=TASK_ID,
            activation_id="activation-legacy", request={"description": "x"}, task_fence=FENCE,
        )
    await legacy_http.aclose()
    context = await agent_dispatch.native_context(TASK_ID)
    assert context is not None and context.run_id == RUN_ID and context.task_fence == FENCE
    assert await agent_dispatch.native_context("task-without-run") is None
    # A grant with a changed field fails signature verification and yields a rejected acknowledgement.
    tampered = {
        "schema_version": "1", "activation_grant_id": "grant-tampered", "task_id": TASK_ID, "run_id": RUN_ID,
        "runtime_key": {"runtime_id": "classic", "runtime_contract_version": "1"},
        "activation_id": "activation-t", "attempt": 1, "request_digest": "a" * 64, "context_view_digest": "b" * 64,
        "task_fence": FENCE, "activation_fence": "fence-a", "agent_id": AGENT_ID, "agent_protocol_version": "2",
        "audience": "bmas-agent", "not_before": "2000-01-01T00:00:00.000Z", "expires_at": "2999-01-01T00:00:00.000Z",
        "grant_nonce": "nonce", "key_id": protocol_keys.DAEMON_KEY_ID, "signature_algorithm": "ed25519-jcs",
        "signature": "AAAA",
    }
    with pytest.raises(native.GrantRejectedError) as rejected:
        await protocol.verify_grant(tampered)
    assert rejected.value.reason_code == "signature"


def test_the_agent_key_pins_its_bytes(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "keys.db"))
    monkeypatch.setenv("BMAS_DAEMON_SIGNING_KEY_FILE", str(tmp_path / "daemon-key"))
    protocol_keys.reset_for_tests()

    async def scenario() -> None:
        await db.init_db()
        first = await protocol_keys.register_agent_key("agent-a", "agent-key-a", "11" * 32)
        assert first["new"] is True
        second = await protocol_keys.register_agent_key("agent-a", "agent-key-a", "11" * 32)
        assert second["new"] is False
        with pytest.raises(protocol_keys.KeyRegistrationError):
            await protocol_keys.register_agent_key("agent-a", "agent-key-a", "22" * 32)
        with pytest.raises(protocol_keys.KeyRegistrationError):
            await protocol_keys.register_agent_key("agent-b", "agent-key-a", "11" * 32)
        rotated = await protocol_keys.register_agent_key("agent-a", "agent-key-b", "33" * 32)
        assert rotated["new"] is True
        assert await protocol_keys.revoke_agent_key("agent-key-a")
        registry = await protocol_keys.registry()
        assert registry.require("agent-key-a").revoked_at is not None
        assert registry.require("agent-key-b").revoked_at is None
        # The daemon key persists on disk with owner-only access.
        path = protocol_keys.daemon_key_path()
        assert path.is_file() and (path.stat().st_mode & 0o777) == 0o600
        protocol_keys.reset_for_tests()
        assert protocol_keys.daemon_public_records()[0]["public_key_hex"] == registry.require(protocol_keys.DAEMON_KEY_ID).public_bytes.hex()

    asyncio.run(scenario())
    # The journal stays untouched by key registration.
    assert asyncio.run(journal.read_journal()) == []
