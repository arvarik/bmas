"""Strict native dispatch bounds, monetary input, and receipt accounting."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from test_classic_journal_projection import TASK_ID
from test_classic_journal_projection import native_run as _native_run

import budget_service as budget
import database as db
import protocol_keys
import runtime_journal as journal
from core.variants.classic.activations import reserve_call, validate_call_reservation
from core.variants.classic.effects import post_completion
from core.variants.classic.spec import MoneyValue, TaskOverrideSet

native_run = _native_run


@pytest.mark.parametrize("amount", [0.5, 1.0, True])
def test_authoritative_money_rejects_coercion(amount):
    with pytest.raises(ValidationError):
        MoneyValue(currency="USD", amount_nanos=amount)


def test_task_money_requires_decimal_text():
    with pytest.raises(ValidationError, match="binary floating point"):
        TaskOverrideSet(classic={"budget_ceiling_usd": 0.5})
    assert TaskOverrideSet(classic={"budget_ceiling_usd": "0.5"})


def test_price_override_requires_provenance():
    with pytest.raises(ValidationError, match="provenance"):
        TaskOverrideSet(price_overrides={"model": {"input_cost_per_token": "0.1", "output_cost_per_token": "0.2"}})
    assert TaskOverrideSet(price_overrides={"model": {
        "input_cost_per_token": "0.1", "output_cost_per_token": "0.2", "source": "operator quote quote-reference",
    }})


@pytest.mark.asyncio
async def test_unknown_price_rejects_before_releasing_admission(native_run):
    admission = await db.get_runtime_admission(native_run["run_id"])
    with pytest.raises(budget.UnknownPriceError):
        await reserve_call(native_run["run_id"], "unknown-model", 1, {"model": "not-priced"})
    assert (await budget.get_reservation(admission["initial_reservation_id"]))["state"] == "reserved"


@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling", [0, -1, 1.5, True, "42"])
async def test_invalid_output_ceiling_never_reserves(native_run, ceiling):
    with pytest.raises(budget.BudgetError, match="positive integer"):
        await reserve_call(native_run["run_id"], "invalid-limit", 1,
                           {"model": "test-light", "max_completion_tokens": ceiling})


@pytest.mark.asyncio
async def test_concurrent_calls_compete_for_all_resource_limits(native_run):
    admission = await db.get_runtime_admission(native_run["run_id"])
    async with db._connect() as connection:
        await connection.execute("UPDATE budget_limits SET limit_amount = 2 WHERE budget_id = ? AND resource = 'model_calls'",
                                 (admission["run_budget_id"],))
        await connection.commit()
    outcomes = await asyncio.gather(*(reserve_call(native_run["run_id"], f"call-{name}", 1,
        {"model": "test-light"}) for name in ("alpha", "beta", "gamma")), return_exceptions=True)
    assert sum(isinstance(item, str) for item in outcomes) == 2
    assert sum(isinstance(item, budget.BudgetError) for item in outcomes) == 1
    limits = await budget.get_limits(admission["run_budget_id"])
    assert {row["resource"] for row in limits} == {"provider_cost", "input_tokens", "output_tokens", "model_calls"}
    assert all(row["reserved_amount"] + row["consumed_amount"] <= row["limit_amount"] for row in limits)


@pytest.mark.asyncio
async def test_released_reservation_never_authorizes_dispatch(native_run):
    request = {"model": "test-light"}
    reservation = await reserve_call(native_run["run_id"], "released-call", 1, request)
    await validate_call_reservation(reservation, native_run["run_id"], "released-call", request)
    await budget.release(reservation)
    with pytest.raises(budget.BudgetError):
        await validate_call_reservation(reservation, native_run["run_id"], "released-call", request)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["worker", "control_unit", "triage", "expert_generation", "decider", "verifier", "judge", "sole"])
async def test_each_phase_reserves_and_records_partial_output(native_run, phase):
    protocol_keys.reset_for_tests()
    async def provider(request):
        body = json.loads(request.content)
        assert body["max_completion_tokens"] == 42
        async with db._connect() as connection:
            rows = await connection.execute_fetchall("SELECT resources FROM budget_reservations WHERE state = 'reserved'")
        assert len(rows) == 1
        assert json.loads(rows[0]["resources"])["output_tokens"] == 42
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "partial"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 42}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        await post_completion(http, "http://provider/chat/completions", task_id=TASK_ID, phase=phase,
                              json={"model": "test-light", "messages": [], "max_completion_tokens": 42})
    records = await journal.read_journal(run_id=native_run["run_id"])
    assert any(record.operation_type == "budget_reconciliation" and record.authority_type == "runtime" for record in records)
    async with db._connect() as connection:
        rows = await connection.execute_fetchall("SELECT transport_observation FROM attempt_receipts WHERE stage = 'response_observed'")
    assert json.loads(rows[0]["transport_observation"])["truncated"] is True


@pytest.mark.asyncio
async def test_unknown_usage_consumes_the_complete_reservation(native_run):
    protocol_keys.reset_for_tests()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": []}))) as http:
        await post_completion(http, "http://provider/chat/completions", task_id=TASK_ID,
                              json={"model": "test-light", "messages": []})
    async with db._connect() as connection:
        rows = await connection.execute_fetchall("SELECT * FROM budget_reservations WHERE activation_id IS NOT NULL")
    assert rows[0]["consumption_kind"] == "estimated"
    assert rows[0]["consumed_amount_nanos"] == rows[0]["reserved_amount_nanos"] > 0


@pytest.mark.asyncio
async def test_late_usage_replaces_estimate_without_double_counting(native_run):
    import effect_service

    protocol_keys.reset_for_tests()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))) as http:
        await post_completion(http, "http://provider/chat/completions", task_id=TASK_ID,
                              json={"model": "test-light", "messages": []})
    async with db._connect() as connection:
        rows = await connection.execute_fetchall("SELECT effect_id FROM effect_attempts")
    usage = {"provider_cost": 500, "input_tokens": 1, "output_tokens": 1, "model_calls": 1}
    first = await effect_service.record_late_usage(effect_id=rows[0]["effect_id"], usage=usage)
    again = await effect_service.record_late_usage(effect_id=rows[0]["effect_id"], usage=usage)
    assert first == again
    state = journal.empty_projection_state()
    for record in await journal.read_journal(run_id=native_run["run_id"]):
        state = journal.apply_record_to_state(state, record)
    assert state["budgets"][native_run["run_id"]]["consumed"] == 1


def test_missing_prices_produce_an_unknown_estimate():
    from test_classic_spec_compiler import golden_deployment

    from core.variants.classic.compiler import compile_specification
    from core.variants.classic.spec import ClassicSpecInput

    deployment = golden_deployment().model_copy(update={"model_pricing": {}})
    specification = compile_specification(ClassicSpecInput(fidelity="production_safe", effort="quick", deployment=deployment))
    assert specification.estimate.cost_minimum is None
    assert specification.estimate.maximum_in_flight_allowance.amount_nanos == 0


@pytest.mark.asyncio
async def test_native_configuration_rejects_binary_prices(native_run, monkeypatch):
    import config
    from core.variants import VariantConfigurationError
    from core.variants.classic.runtime import deployment_snapshot

    monkeypatch.setattr(config, "MODEL_PRICING", {"model": {"input_cost_per_token": 0.1, "output_cost_per_token": 0.2}})
    with pytest.raises(VariantConfigurationError, match="binary floating point"):
        await deployment_snapshot()


def test_old_specifications_remain_readable_but_new_calls_require_strict_pricing():
    from test_classic_spec_compiler import compile_pair

    from core.variants.classic.compiler import ClassicSpecError
    from core.variants.classic.spec import ClassicSpec

    stored = compile_pair("paper_aligned", "quick").model_dump()
    stored["limits"]["strict_pricing"] = False
    assert ClassicSpec.model_validate(stored).limits.strict_pricing is False
    with pytest.raises(ClassicSpecError, match="strict pricing"):
        compile_pair("paper_aligned", "quick", classic={"limits.strict_pricing": False})


@pytest.mark.asyncio
async def test_native_endpoint_change_never_uses_legacy_transport():
    from core.orchestrator import Orchestrator

    orchestrator = object.__new__(Orchestrator)
    response = await orchestrator._post_activation("http://replacement-agent",
        {"role": "expert", "timeout": 10}, None, {"required": True, "url": "http://qualified-agent"})
    assert response.json()["status"] == "failed"
    assert "qualified dispatch plan" in response.json()["result"]


@pytest.mark.asyncio
async def test_missing_resource_limits_reject_native_dispatch(native_run):
    admission = await db.get_runtime_admission(native_run["run_id"])
    async with db._connect() as connection:
        await connection.execute("DELETE FROM budget_limits WHERE budget_id = ? AND resource = 'output_tokens'",
                                 (admission["run_budget_id"],))
        await connection.commit()
    with pytest.raises(budget.BudgetError, match="all four resource limits"):
        await reserve_call(native_run["run_id"], "incomplete-budget", 1, {"model": "test-light"})
