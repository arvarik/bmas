"""Native proposal schemas and the complete activation receipt chain."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from test_classic_journal_projection import TASK_ID, make_gateway
from test_classic_journal_projection import native_run as _native_run

import agent_dispatch
import edge_access
import protocol_keys
import runtime_journal as journal
from core.variants.classic.activations import reserve_call, seal_response
from core.variants.classic.proposals import parse_proposal, parser_fixtures
from execution_envelope import ModelProposalError
from routes import agent_protocol as routes

native_run = _native_run

AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"
sys.path.insert(0, str(AGENT_DIR))
from bmas_protocol import native  # noqa: E402


@pytest.mark.parametrize("case", parser_fixtures())
def test_generated_parser_cases(case):
    payload = case["payload"]
    if case["valid"]:
        assert parse_proposal(json.dumps(payload).encode(), role=payload["role"]).content == payload
    else:
        with pytest.raises(ModelProposalError):
            parse_proposal(json.dumps(payload).encode(), role=payload["role"])


@pytest.mark.parametrize("raw", [b'[]', b'{} {}', b'{"role":"expert","role":"critic"}', b'```json\n{}\n```'])
def test_parser_rejects_ambiguous_or_multiple_proposals(raw):
    with pytest.raises(ModelProposalError):
        parse_proposal(raw, role="expert")


async def execute_proposal(run, raw, monkeypatch, *, returned_raw=None, duplicate_delivery=False, dispatch_route=False, role="expert"):
    protocol_keys.reset_for_tests()
    monkeypatch.setattr(edge_access, "operator_key", lambda: "operator-key")
    monkeypatch.setattr(edge_access, "node_key", lambda: "node-key")
    app = FastAPI()
    app.middleware("http")(edge_access.enforce_edge_access)
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://daemon") as daemon_http:
        agent = native.NativeProtocol(agent_id="proposal-agent", cache_dir=run["root"] / "agent-cache",
            daemon_url="http://daemon", node_key="node-key", http=daemon_http)

        async def execute(context):
            effect = await agent.open_model_effect(context, model="test-light", request={"model": "test-light", "messages": [], "max_completion_tokens": 4096})
            await agent.receipt(effect, stage=native.STAGE_RESPONSE_OBSERVED, raw_response=raw,
                                usage={"prompt_tokens": 10, "completion_tokens": 20})
            return {"result": (returned_raw or raw).decode(), "entries": [{"body": "forged agent entries"}]}

        async def transport(request):
            if request.method == "GET":
                return httpx.Response(200, json={"document": agent.document})
            delivery = json.loads(request.content)
            if duplicate_delivery:
                first, second = await asyncio.gather(
                    agent.activate(delivery["grant"], delivery["grant_digest"], execute),
                    agent.activate(delivery["grant"], delivery["grant_digest"], execute),
                )
                assert first["result"] == second["result"]
                assert first["replayed"] is False and second["replayed"] is True
                body = first
            else:
                body = await agent.activate(delivery["grant"], delivery["grant_digest"], execute)
            return httpx.Response(200, json=body)

        request = {"model": "test-light", "context": {"board": []}, "role": role}
        reservation = None if dispatch_route else await reserve_call(run["run_id"], "activation-proposal", 1, request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            if dispatch_route:
                app.state.health_client = http
                response = await daemon_http.post("/agent-protocol/dispatch", headers={"Authorization": "Bearer operator-key"},
                    json={"agent_url": "http://agent", "run_id": run["run_id"], "task_id": TASK_ID,
                          "activation_id": "activation-proposal", "request": request})
                assert response.status_code == 200, response.text
                return response.json()["result"]
            outcome = await agent_dispatch.dispatch_activation(http, agent_url="http://agent",
                run_id=run["run_id"], task_id=TASK_ID, activation_id="activation-proposal", request=request,
                task_fence=run["context"].task_fence, reservation_id=reservation,
                document=agent_dispatch.document_from_dict(agent.document))
        return await seal_response(run_id=run["run_id"], activation_id="activation-proposal", attempt=1,
                                   result=outcome["result"], role=role)


@pytest.mark.asyncio
async def test_native_proposal_and_all_board_rows_commit_together(native_run, monkeypatch):
    raw = json.dumps({"schema_version": "classic-proposal/1", "role": "expert", "action": "contribute",
        "entries": [{"type": "finding", "body": "First observation"},
                    {"type": "finding", "body": "Second observation"}]}).encode()
    response = await execute_proposal(native_run, raw, monkeypatch, duplicate_delivery=True)
    execution = response["native_execution"]
    assert len(execution["envelope"]["receipt_digests"]) == 2
    assert execution["envelope"]["proposal_ref"]
    gateway, store, _ = make_gateway(native_run["binding"])
    committed = await gateway.apply_proposal(task_id=TASK_ID, actor="expert", capabilities=["finding_writer"],
        proposed=execution["proposal"]["entries"], turn_id="activation-proposal", attempt=1, round_no=1)
    assert [entry.body for entry in committed] == ["First observation", "Second observation"]
    records = [r for r in await journal.read_journal(run_id=native_run["run_id"])
               if r.operation_type == "proposal_decision"]
    assert len(records) == 1
    assert len(records[0].payload["board"]["entries"]) == 2
    assert records[0].payload["execution_envelope_digest"] == execution["envelope_digest"]
    assert records[0].authority_type == "runtime"
    observations = [row for row in await journal.read_journal(run_id=native_run["run_id"])
                    if row.payload.get("evidence", {}).get("execution_envelope_artifact_digest")]
    assert len(observations) == 1
    envelope_artifact = protocol_keys.artifact_store().read_object(
        observations[0].payload["evidence"]["execution_envelope_artifact_digest"])
    assert json.loads(bytes(envelope_artifact["payload"])) == execution["envelope"]
    assert len(await store.get_snapshot(TASK_ID)) == 2
    await journal.verify_durable_projections()


@pytest.mark.asyncio
async def test_a_rejected_entry_rejects_the_whole_proposal(native_run, monkeypatch):
    raw = json.dumps({"schema_version": "classic-proposal/1", "role": "expert", "action": "contribute",
        "entries": [{"type": "finding", "body": "Allowed"}, {"type": "solution", "body": "Denied"}]}).encode()
    response = await execute_proposal(native_run, raw, monkeypatch)
    gateway, store, _ = make_gateway(native_run["binding"])
    assert await gateway.apply_proposal(task_id=TASK_ID, actor="expert", capabilities=["finding_writer"],
        proposed=response["native_execution"]["proposal"]["entries"],
        turn_id="activation-proposal", attempt=1, round_no=1) == []
    assert await store.get_snapshot(TASK_ID) == {}
    records = await journal.read_journal(run_id=native_run["run_id"])
    decisions = [r for r in records if r.operation_type == "proposal_decision"]
    assert len(decisions) == 1 and decisions[0].payload["decision"] == "rejected"


@pytest.mark.asyncio
async def test_invalid_raw_response_stays_available_with_a_parse_failure(native_run, monkeypatch):
    response = await execute_proposal(native_run, b'{"status":"completed"}', monkeypatch, dispatch_route=True)
    envelope = response["native_execution"]["envelope"]
    assert envelope["parse_failure_ref"] and envelope["proposal_ref"] is None
    raw = protocol_keys.artifact_store().read_object(envelope["raw_response_artifact_digest"])
    assert bytes(raw["payload"]) == b'{"status":"completed"}'


@pytest.mark.asyncio
async def test_forged_result_does_not_match_the_signed_receipt(native_run, monkeypatch):
    response = await execute_proposal(native_run, b'{}', monkeypatch, returned_raw=b'{"forged":true}')
    envelope = response["native_execution"]["envelope"]
    assert envelope["trusted_status"] == "failed"
    assert envelope["no_proposal_reason"] == "receipt_verification_failed"
    assert envelope["proposal_ref"] is None and envelope["usage"] is None


def test_generated_model_and_parser_fixtures_are_current():
    import subprocess

    root = Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable, str(root / "scripts/generate-classic-proposals.py"), "--check"],
                   cwd=root, check=True, capture_output=True, text=True)


@pytest.mark.asyncio
async def test_board_write_failure_rolls_back_the_proposal_decision(native_run, monkeypatch):
    import activation_service
    import database as db

    raw = json.dumps({"schema_version": "classic-proposal/1", "role": "expert", "action": "contribute",
                      "entries": [{"type": "finding", "body": "Atomic finding"}]}).encode()
    response = await execute_proposal(native_run, raw, monkeypatch)
    binding = native_run["binding"]
    original = binding._projection_writer

    def failing_writer(section):
        async def write(connection, cursor, now):
            await original(section)(connection, cursor, now)
            raise RuntimeError("Injected projection failure")
        return write

    gateway, store, _ = make_gateway(binding)
    arguments = dict(task_id=TASK_ID, actor="expert", capabilities=["finding_writer"],
        proposed=response["native_execution"]["proposal"]["entries"],
        turn_id="activation-proposal", attempt=1, round_no=1)
    monkeypatch.setattr(binding, "_projection_writer", failing_writer)
    with pytest.raises(RuntimeError, match="Injected projection failure"):
        await gateway.apply_proposal(**arguments)
    assert await store.get_snapshot(TASK_ID) == {}
    assert await db.get_classic_board_projection(native_run["run_id"]) == []
    assert not [row for row in await journal.read_journal(run_id=native_run["run_id"])
                if row.operation_type == "proposal_decision"]
    assert (await activation_service.get_activation("activation-proposal", 1))["state"] == "proposal_recorded"
    monkeypatch.setattr(binding, "_projection_writer", original)
    assert len(await gateway.apply_proposal(**arguments)) == 1


@pytest.mark.asyncio
async def test_cancellation_after_preflight_rejects_the_board_transaction(native_run, monkeypatch):
    import activation_service
    import database as db

    raw = json.dumps({"schema_version": "classic-proposal/1", "role": "expert", "action": "contribute",
                      "entries": [{"type": "finding", "body": "Late finding"}]}).encode()
    response = await execute_proposal(native_run, raw, monkeypatch)
    original = activation_service.validate_proposal_eligibility

    async def cancel_after_preflight(**kwargs):
        await original(**kwargs)
        await db.request_run_cancellation_control(native_run["run_id"])

    monkeypatch.setattr(activation_service, "validate_proposal_eligibility", cancel_after_preflight)
    gateway, store, _ = make_gateway(native_run["binding"])
    with pytest.raises(activation_service.ProposalEligibilityError, match="live_authority"):
        await gateway.apply_proposal(task_id=TASK_ID, actor="expert", capabilities=["finding_writer"],
            proposed=response["native_execution"]["proposal"]["entries"],
            turn_id="activation-proposal", attempt=1, round_no=1)
    assert await store.get_snapshot(TASK_ID) == {}
    assert await db.get_classic_board_projection(native_run["run_id"]) == []
