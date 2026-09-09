"""Local calls retain reservations, transport receipts, and uncertain outcomes."""
from __future__ import annotations

import httpx
import pytest
from test_classic_journal_projection import TASK_ID
from test_classic_journal_projection import native_run as _native_run

import database as db
import effect_service
import protocol_keys
import recovery_center
from access_control import Principal
from core.variants.classic.effects import post_completion

native_run = _native_run


@pytest.mark.asyncio
async def test_local_completion_has_its_own_reservation_acknowledgement_and_receipts(native_run):
    protocol_keys.reset_for_tests()
    calls = []

    async def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "42"}}],
                                         "usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        for phase in ("triage", "control_unit", "expert_generation", "verifier", "judge", "sole"):
            result = await post_completion(http, "http://provider/chat/completions", task_id=TASK_ID,
                phase=phase, json={"model": "test-light", "messages": []})
            assert result.status_code == 200
    assert len(calls) == 6
    async with db._connect() as connection:  # noqa: SLF001
        for table, expected in (("activations", 6), ("activation_leases", 6), ("activation_grants", 6),
                                ("activation_acknowledgements", 6), ("effect_operations", 6),
                                ("effect_attempts", 6), ("attempt_receipts", 12)):
            row = await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            assert row[0] == expected, table
        rows = await connection.execute_fetchall("SELECT * FROM activations")
        assert len({row["reservation_id"] for row in rows}) == 6
        assert all(row["execution_envelope_digest"] and row["raw_result_artifact_digest"] for row in rows)
        charges = await connection.execute_fetchall(
            "SELECT consumed_amount_nanos, consumption_kind FROM budget_reservations WHERE activation_id IS NOT NULL")
        assert all(row["consumption_kind"] == "actual" and row["consumed_amount_nanos"] == 2400 for row in charges)


@pytest.mark.asyncio
async def test_cancellation_at_transport_boundary_invokes_no_provider(native_run, monkeypatch):
    protocol_keys.reset_for_tests()
    invocations = []
    original = effect_service.record_transport_start

    async def cancel_before_start(**kwargs):
        await db.request_run_cancellation_control(native_run["run_id"])
        return await original(**kwargs)

    monkeypatch.setattr(effect_service, "record_transport_start", cancel_before_start)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: invocations.append(r))) as http:
        with pytest.raises(effect_service.EffectDispatchError):
            await post_completion(http, "http://provider/chat/completions", task_id=TASK_ID,
                                  json={"model": "test-light", "messages": []})
    assert invocations == []
    async with db._connect() as connection:  # noqa: SLF001
        row = await (await connection.execute("SELECT state FROM effect_attempts")).fetchone()
        assert row["state"] == "cancelled"


@pytest.mark.asyncio
async def test_uncertain_transport_enters_recovery(native_run):
    protocol_keys.reset_for_tests()

    async def uncertain(request):
        raise httpx.ReadTimeout("The response was lost", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(uncertain)) as http:
        with pytest.raises(httpx.ReadTimeout):
            await post_completion(http, "http://provider/chat/completions", task_id=TASK_ID,
                                  json={"model": "test-light", "messages": []})
    async with db._connect() as connection:  # noqa: SLF001
        row = await (await connection.execute("SELECT state FROM effect_attempts")).fetchone()
        assert row["state"] == "outcome_unknown"
        activation = await (await connection.execute("SELECT * FROM activations")).fetchone()
        assert activation["state"] == "suspended" and activation["execution_envelope_digest"]
    queue = await recovery_center.list_queue("unknown_effects", principal=Principal(
        principal_id="operator", tenant_id="tenant-default", roles=("operator",)))
    assert len(queue) == 1 and queue[0]["run_id"] == native_run["run_id"]


@pytest.mark.asyncio
async def test_legacy_control_call_observes_without_a_reservation(native_run):
    protocol_keys.reset_for_tests()
    await db.create_task_with_meta("legacy-observation", "legacy", "Add 20 and 22.", "classic", {},
                                   runtime_contract_version="1", run_state="staging")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 1}}),
    )) as http:
        await post_completion(http, "http://provider/chat/completions", task_id="legacy-observation",
                              json={"model": "test-light", "messages": []})
    async with db._connect() as connection:  # noqa: SLF001
        attempts = await connection.execute_fetchall("SELECT * FROM effect_attempts WHERE task_id = 'legacy-observation'")
        receipts = await connection.execute_fetchall("SELECT * FROM attempt_receipts WHERE effect_id = ?", (attempts[0]["effect_id"],))
        assert len(attempts) == 1 and attempts[0]["reservation_id"] == ""
        assert len(receipts) == 2
        row = await (await connection.execute("SELECT dispatch_policy FROM effect_dispatch_outbox WHERE effect_id = ?",
                                             (attempts[0]["effect_id"],))).fetchone()
        assert row["dispatch_policy"] == "observe_only"


@pytest.mark.asyncio
async def test_a_missing_ledger_never_invokes_the_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "missing-schema.db"))
    invocations = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: invocations.append(request))) as http:
        with pytest.raises(effect_service.EffectServiceError, match="cannot read its effect ledger"):
            await post_completion(http, "http://provider/chat/completions", json={"model": "test-light"})
    assert invocations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_run_cancellation_cancels_approved_effects(native_run, queued):
    import activation_service
    from core.variants.classic.activations import reserve_call

    reservation = await reserve_call(native_run["run_id"], "pending-effect", 1, {"model": "test-light"})
    await activation_service.create_activation(run_id=native_run["run_id"], activation_id="pending-effect",
        reservation_id=reservation, request_digest="d" * 64, context_view_digest="e" * 64)
    effect = await effect_service.create_effect_intent(run_id=native_run["run_id"], activation_id="pending-effect",
        activation_attempt=1, kind="provider", request_digest="d" * 64,
        idempotency_scope="pending-effect", child_idempotency_key="pending-effect",
        reservation_id=reservation, retry_safety="unsafe")
    await effect_service.approve_effect(run_id=native_run["run_id"], effect_id=effect["effect_id"])
    if queued:
        await effect_service.queue_effect_dispatch(run_id=native_run["run_id"], effect_id=effect["effect_id"], target="provider")
    assert await db.request_run_cancellation_control(native_run["run_id"])
    assert not await db.request_run_cancellation_control(native_run["run_id"])
    assert (await effect_service.get_attempt(effect["effect_id"]))["state"] == "cancelled"
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall("SELECT dispatch_state FROM effect_dispatch_outbox")
        assert all(row["dispatch_state"] == "cancelled" for row in rows)


@pytest.mark.asyncio
async def test_concurrent_calls_each_own_their_reservation(native_run):
    import asyncio

    from core.variants.classic.activations import reserve_call

    reservations = await asyncio.gather(*(reserve_call(native_run["run_id"], f"independent-{name}", 1,
        {"model": "test-light"}) for name in ("first", "second", "third")))
    assert len(set(reservations)) == 3
