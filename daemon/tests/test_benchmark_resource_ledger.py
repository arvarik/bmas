"""Exact run cost: Money contract, reservations, states, and gates.

Every authoritative monetary field uses the Foundation
``Money(currency, amount_nanos)`` contract. Reservations stop
concurrent overshoot before any cost-bearing work, unknown prices stay
unknown instead of becoming zero, run cost moves through provisional,
settling, and settled, a cost-sensitive final gate waits for
settlement, and a late charge supersedes the stored gate and opens the
next evaluation version.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest
import pytest_asyncio

import budget_service as budget
import database as db
from benchmarks import admission, costs, records, repository
from benchmarks.gates import GateSettlementError, evaluate_gate, validate_rules
from benchmarks.provenance import content_checksum
from core.money import (
    CurrencyMismatchError,
    FloatAmountError,
    Money,
    MoneyError,
    UnknownExchangeRateError,
    convert,
)

# ── The Money contract at the trusted boundary ───────────────────────


def test_every_monetary_field_uses_currency_and_nanos():
    money = Money("USD", 1_250_000_000)
    encoded = costs.money_to_json(money)
    assert encoded == {"currency": "USD", "amount_nanos": 1_250_000_000}
    assert costs.money_from_json(encoded) == money


def test_binary_floating_point_amounts_fail_validation():
    with pytest.raises(FloatAmountError):
        Money("USD", 1.5)  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        costs.money_from_json({"currency": "USD", "amount_nanos": "10"})
    with pytest.raises(MoneyError):
        costs.money_from_json("1.50")


def test_decimal_strings_parse_once_with_conservative_rounding():
    money, source = costs.parse_boundary_amount("0.1234567891")
    assert source == "decimal_string"
    # More than nine fractional digits rounds up to the next nano, so
    # the reservation never understates the price.
    assert money.amount_nanos == 123_456_790


def test_the_legacy_float_adapter_marks_its_source():
    adapted = costs.legacy_cost_adapter(0.25)
    assert adapted is not None
    assert adapted["money"] == {"currency": "USD",
                                "amount_nanos": 250_000_000}
    assert adapted["source"] == "legacy_float"
    assert adapted["source_text"] == "0.25"
    # The legacy value renders and reconciles; it never authorizes a
    # reservation or a terminal cost gate.
    assert adapted["authoritative"] is False
    assert costs.legacy_cost_adapter(None) is None


def test_cross_currency_comparison_needs_a_versioned_rate():
    dollars = Money("USD", 10)
    euros = Money("EUR", 10)
    with pytest.raises(CurrencyMismatchError):
        dollars.compare(euros)
    with pytest.raises(UnknownExchangeRateError):
        convert(dollars, "EUR", rate=None)


def test_cost_state_transitions_follow_the_declared_machine():
    costs.validate_cost_transition("provisional", "settling")
    costs.validate_cost_transition("settling", "settled")
    costs.validate_cost_transition("settled", "settling")
    with pytest.raises(costs.RunCostStateError):
        costs.validate_cost_transition("provisional", "settled")
    with pytest.raises(costs.RunCostStateError):
        costs.validate_cost_transition("settled", "provisional")


def test_charge_summary_keeps_estimates_and_unknowns_apart():
    charges = [
        {"id": "c1", "kind": "charge", "currency": "USD",
         "amount_nanos": 100, "evidence": {}},
        {"id": "c2", "kind": "estimate", "currency": "USD",
         "amount_nanos": 900, "evidence": {}},
        {"id": "c3", "kind": "not_billable", "currency": "USD",
         "amount_nanos": None,
         "evidence": {"proof": "included in subscription"}},
        {"id": "c4", "kind": "unknown", "currency": "USD",
         "amount_nanos": None, "evidence": {}},
    ]
    summary = costs.summarize_charges(charges)
    # Estimates stay separate, the not-billable event adds zero with
    # its evidence kept, and the unknown price never becomes zero: it
    # stays visible as an unbounded unknown.
    assert summary["settled_total"]["amount_nanos"] == 100
    assert summary["unknown_charge_ids"] == ["c4"]
    assert summary["unbounded_unknown"] is True
    charges[3]["evidence"]["accepted_bound"] = {
        "currency": "USD", "amount_nanos": 50,
    }
    bounded = costs.summarize_charges(charges)
    assert bounded["unbounded_unknown"] is False
    assert bounded["settled_total"]["amount_nanos"] == 150


def test_a_foreign_currency_charge_fails_the_total():
    with pytest.raises(CurrencyMismatchError):
        costs.summarize_charges([
            {"id": "c1", "kind": "charge", "currency": "EUR",
             "amount_nanos": 100, "evidence": {}},
        ])


def test_wilson_and_raw_p_value_rules_never_enter_a_gate():
    with pytest.raises(ValueError, match="Wilson"):
        validate_rules([{
            "id": "wilson-rule",
            "metric": "arm.classic.score.exact.wilson_unclustered_diagnostic",
            "operator": "gte",
            "value": 0.5,
        }])


# ── Database-backed reservations, settlement, and gates ──────────────


@pytest_asyncio.fixture
async def ledger_db(tmp_path, monkeypatch):
    path = str(tmp_path / "ledger.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    await db.create_dataset_version(
        dataset_id="dataset-ledger",
        version_id="version-ledger",
        name="Ledger data",
        description="",
        source_uri=None,
        license_name=None,
        author=None,
        dataset_metadata={},
        checksum="dataset-ledger-checksum",
        schema={"version": "1"},
        source_filename="ledger.jsonl",
        source_mime="application/x-ndjson",
        source_checksum="source-ledger-checksum",
        source_path="/tmp/ledger.jsonl",
        version_metadata={},
        items=[{
            "id": "item-ledger",
            "item_key": "one",
            "input": "What is 20 plus 22?",
            "expected_output": "42",
            "subject": "math",
            "split": "test",
            "tags": [],
            "metadata": {},
        }],
    )
    envelope = {
        "runtime_id": "classic",
        "effective_configuration": {"model_routing": {"medium": "model-a"}},
    }
    await repository.create_test_revision(
        test_id="test-ledger",
        revision_id="revision-ledger",
        name="ledger",
        description="",
        dataset_version_id="version-ledger",
        configuration={
            "repetitions": 1,
            "seed": 1,
            "max_concurrency": 4,
            "timeout_seconds": 60,
            "practical_difference": 0.01,
            "cost_limit_usd": "1",
        },
        arms=[{
            "id": "arm-ledger",
            "name": "Classic",
            "slug": "classic",
            "runtime_id": "classic",
            "configuration": envelope,
            "configuration_checksum": content_checksum(envelope),
        }],
        scorers=[{"id": "scorer-exact-match-v1", "configuration": {}}],
    )
    return path


async def _budget(budget_id: str, limit_nanos: int) -> str:
    async with db._connect() as connection:  # noqa: SLF001
        await budget.create_run_budget(
            connection,
            budget_id=budget_id,
            run_id=f"run-{budget_id}",
            task_id=f"task-{budget_id}",
            currency="USD",
            limits=(
                budget.LimitSpec(
                    "run",
                    f"run-{budget_id}",
                    "provider_cost",
                    limit_nanos,
                    currency="USD",
                ),
            ),
        )
        await connection.commit()
    return budget_id


@pytest.mark.asyncio
async def test_reservations_stop_concurrent_overshoot(ledger_db):
    await _budget("budget-concurrent", 1_000_000_000)
    for name in ("reservation-a", "reservation-b"):
        await budget.request_reservation(
            reservation_id=name,
            budget_id="budget-concurrent",
            resources={"provider_cost": 600_000_000},
        )
    outcomes = await asyncio.gather(
        budget.reserve("reservation-a"),
        budget.reserve("reservation-b"),
    )
    # Two 0.6 reservations against a 1.0 limit: exactly one fits, so
    # concurrent work can never exceed the budget.
    assert sorted(outcomes) == [False, True]


async def _complete_run(database_path: str, run_id: str) -> None:
    async with aiosqlite.connect(database_path) as connection:
        connection.row_factory = aiosqlite.Row
        rows = await connection.execute_fetchall(
            "SELECT attempt.id FROM benchmark_attempts AS attempt "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "WHERE trial.run_id = ?",
            (run_id,),
        )
        for index, row in enumerate(rows):
            task_id = f"task-{run_id}-{index}"
            await connection.execute(
                "INSERT INTO tasks (id, label, full_input, status, "
                "terminal_kind, result_summary, total_cost_usd, "
                "total_tokens, duration_ms) VALUES (?, 'Benchmark', "
                "'Question', 'completed', 'completed', '42', 0.01, 100, "
                "1000)",
                (task_id,),
            )
            await connection.execute(
                "UPDATE benchmark_attempts SET status = 'completed', "
                "task_id = ?, completed_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (task_id, str(row["id"])),
            )
            await connection.execute(
                "INSERT INTO benchmark_scores (id, attempt_id, scorer_id, "
                "status, score, passed, evidence) VALUES (?, ?, "
                "'scorer-exact-match-v1', 'scored', 1.0, 1, '{}')",
                (f"score-{run_id}-{index}", str(row["id"])),
            )
        await connection.commit()
        first = str(rows[0]["id"])
    await repository.refresh_run_for_attempt(first)


@pytest.mark.asyncio
async def test_a_terminal_run_enters_settling(ledger_db):
    await repository.create_run(
        run_id="run-settling",
        revision_id="revision-ledger",
        idempotency_key=None,
    )
    run = await repository.get_run("run-settling")
    assert run is not None and run["cost_status"] == "provisional"
    await _complete_run(ledger_db, "run-settling")
    run = await repository.get_run("run-settling")
    assert run is not None
    assert run["status"] == "completed"
    assert run["cost_status"] == "settling"


@pytest.mark.asyncio
async def test_an_unknown_charge_blocks_settlement_until_accepted(
    ledger_db,
):
    await repository.create_run(
        run_id="run-unknown",
        revision_id="revision-ledger",
        idempotency_key=None,
    )
    await _complete_run(ledger_db, "run-unknown")
    charge_id = await repository.record_cost_charge(
        "run-unknown",
        kind="unknown",
        currency="USD",
        amount_nanos=None,
        provider="task-service",
        source_kind="none",
        evidence={"reason": "The provider reported no final price"},
    )
    assert not await admission.try_settle_run("run-unknown")
    await repository.accept_unknown_charge(
        charge_id,
        operator_id="operator-a",
        bound={"currency": "USD", "amount_nanos": 20_000_000},
    )
    assert await admission.try_settle_run("run-unknown")
    run = await repository.get_run("run-unknown")
    assert run is not None
    assert run["cost_status"] == "settled"
    # The accepted unknown stays visible and nonzero inside the bound.
    settled = costs.money_from_json(run["settled_cost"])
    assert settled.amount_nanos == 20_000_000


@pytest.mark.asyncio
async def test_a_cost_gate_waits_for_settlement_and_supersedes(ledger_db):
    for run_id in ("run-base", "run-candidate"):
        await repository.create_run(
            run_id=run_id,
            revision_id="revision-ledger",
            idempotency_key=None,
        )
        await _complete_run(ledger_db, run_id)
    await records.create_baseline(
        baseline_id="baseline-cost",
        run_id="run-base",
        name="Cost baseline",
        description="",
        rules=[{
            "id": "cost-ceiling",
            "metric": "arm.classic.cost_usd.total",
            "operator": "lte",
            "value": 1.0,
        }],
        created_by="operator",
    )
    # The candidate run cost is settling, so the cost-sensitive final
    # gate blocks until settlement completes.
    with pytest.raises(repository.BenchmarkConflict, match="settlement"):
        await records.evaluate_baseline("baseline-cost", "run-candidate")
    assert await admission.try_settle_run("run-candidate")
    stored, created = await records.evaluate_baseline(
        "baseline-cost", "run-candidate",
    )
    assert created
    assert stored["status"] == "passed"
    assert int(stored["evaluation_version"]) == 1

    # A late charge supersedes the stored gate and reopens settlement.
    charge_id = await repository.record_late_charge(
        "run-candidate",
        currency="USD",
        source_text="0.075",
        provider="task-service",
        evidence={"invoice": "late-invoice-77"},
    )
    run = await repository.get_run("run-candidate")
    assert run is not None and run["cost_status"] == "settling"
    charges = await repository.list_cost_charges("run-candidate")
    late = [c for c in charges if c["kind"] == "late_charge"]
    # The charge evidence preserves the original provider string.
    assert late[0]["source_text"] == "0.075"
    assert late[0]["amount_nanos"] == 75_000_000

    baseline = await records.get_baseline("baseline-cost")
    assert baseline is not None
    superseded = baseline["evaluations"][0]
    assert superseded["superseded_at"] is not None
    assert superseded["superseded_by"] == charge_id

    # The new reconciliation settles again, and the next evaluation
    # creates a superseding gate version while the earlier decision
    # stays readable.
    assert await admission.try_settle_run("run-candidate")
    replacement, created = await records.evaluate_baseline(
        "baseline-cost", "run-candidate",
    )
    assert created
    assert int(replacement["evaluation_version"]) == 2
    baseline = await records.get_baseline("baseline-cost")
    assert baseline is not None
    versions = sorted(
        int(item["evaluation_version"]) for item in baseline["evaluations"]
    )
    assert versions == [1, 2]


def test_an_unbounded_unknown_fails_a_passing_cost_rule():
    run = {
        "id": "run-synthetic",
        "status": "completed",
        "test_id": "test-synthetic",
        "test_revision_id": "revision-synthetic",
        "test_configuration_checksum": "checksum",
        "test_configuration": {"practical_difference": 0.01},
        "dataset_id": "dataset-synthetic",
        "dataset_checksum": "dataset-checksum",
        "execution_plan_checksum": "plan-checksum",
        "revision_scorers": [{"id": "exact", "required": True,
                              "sort_order": 0,
                              "configuration_checksum": ""}],
        "arms": [],
        "attempts": [{
            "id": "attempt-a",
            "trial_id": "trial-a",
            "arm_id": "arm-a",
            "arm_name": "Classic",
            "arm_slug": "classic",
            "runtime_id": "classic",
            "dataset_item_id": "item-a",
            "item_key": "one",
            "repeat_index": 1,
            "retry_index": 0,
            "status": "completed",
            "subject": "math",
            "split": "test",
            "tags": [],
            "total_cost_usd": 0.01,
            "duration_ms": 10,
            "total_tokens": 5,
        }],
        "scores": [{
            "id": "score-a",
            "attempt_id": "attempt-a",
            "scorer_id": "exact",
            "scorer_name": "Exact",
            "scorer_version": "1",
            "status": "scored",
            "score": 1.0,
            "passed": 1,
        }],
        "human_reviews": [],
    }
    rules = [{
        "id": "cost-ceiling",
        "metric": "arm.classic.cost_usd.total",
        "operator": "lte",
        "value": 1.0,
    }]
    passing = evaluate_gate(
        run,
        {**run, "id": "candidate"},
        rules,
        cost_evidence={"cost_status": "settled",
                       "unbounded_unknown": False},
    )
    assert passing["status"] == "passed"
    with pytest.raises(GateSettlementError):
        evaluate_gate(
            run,
            {**run, "id": "candidate"},
            rules,
            cost_evidence={"cost_status": "settling",
                           "unbounded_unknown": False},
        )
    blocked = evaluate_gate(
        run,
        {**run, "id": "candidate"},
        rules,
        cost_evidence={"cost_status": "settled",
                       "unbounded_unknown": True},
    )
    # The rule threshold passes, but an unbounded unknown amount can
    # never pass a cost rule.
    assert blocked["status"] == "failed"
    assert "unknown" in str(blocked["rules"][0]["direction_guard"])
