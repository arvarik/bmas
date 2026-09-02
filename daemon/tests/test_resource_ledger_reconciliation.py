"""The complete resource ledger: estimates, charges, reconciliation.

One run uses every resource class. The ledger keeps estimates and
actual charges separately, a retry creates a new entry, a failed
scorer and an excluded infrastructure result still contribute, a
late charge updates reconciliation without replacing its estimate,
an unknown price stays unknown, a not-billable event keeps its
evidence, totals follow the declared currency policy, cost per
success uses the unconditional denominator, every monetary field is
Money, and a late charge that changes a cost rule creates the next
reconciliation version, reopens settlement, and supersedes gates.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from test_evaluation_contracts import valid_analysis_snapshot
from test_evidence_capture import make_attempts

import database as db
from benchmarks import repository, resource_ledger
from benchmarks.evaluation_contracts import EvaluationContractError
from benchmarks.resource_ledger import (
    REQUIRED_CLASSES,
    ResourceLedgerError,
    actual_from_provider_text,
    apply_late_charge,
    cost_per_success,
    evaluate_cost_rules,
    ledger_entry,
    reconcile_run,
    summarize,
)
from core.money import Money

RUN_ID = "run-evidence"
NOW = "2026-09-01T00:00:00Z"


def _entry(**overrides) -> dict:
    arguments = {
        "run_id": RUN_ID,
        "resource_class": "runtime",
        "provider": "provider-a",
        "service": "chat",
        "region": "us-east",
        "quantity": 1000,
        "unit": "tokens",
        "pricing_version": "pricing-2026-09",
        "estimate": Money("USD", 100_000_000),
        "now": NOW,
    }
    arguments.update(overrides)
    return ledger_entry(**arguments)


# ── Entry construction and the Money contract ────────────────────────


def test_estimate_and_actual_live_in_separate_objects():
    estimate = _entry()
    assert estimate["charge_state"] == "estimated"
    assert "actual" not in estimate
    actual = _entry(
        actual=Money("USD", 110_000_000),
        actual_provider_text="0.11",
        estimate_entry_id=estimate["entry_id"],
    )
    assert actual["charge_state"] == "confirmed"
    assert actual["estimate"]["value"] == {
        "currency": "USD", "amount_nanos": 100_000_000,
    }
    assert actual["actual"]["value"] == {
        "currency": "USD", "amount_nanos": 110_000_000,
    }
    assert actual["actual"]["evidence"]["provider_text"] == "0.11"
    assert actual["estimate_entry_id"] == estimate["entry_id"]


def test_provider_text_parses_once_with_conservative_rounding():
    money = actual_from_provider_text("USD", "0.0000000001")
    assert money.amount_nanos == 1
    assert actual_from_provider_text("USD", "1.25").amount_nanos == (
        1_250_000_000
    )


def test_unknown_price_stays_unknown_and_records_no_amount():
    unknown = _entry(estimate=None, price_unknown=True)
    assert unknown["charge_state"] == "unknown"
    assert "estimate" not in unknown
    assert "actual" not in unknown
    with pytest.raises(ResourceLedgerError, match="no estimate amount"):
        _entry(price_unknown=True)


def test_not_billable_event_keeps_its_evidence():
    entry = _entry(
        estimate=None, not_billable_evidence="included in platform plan",
    )
    assert entry["charge_state"] == "not_billable"
    assert entry["not_billable_evidence"] == "included in platform plan"
    with pytest.raises(ResourceLedgerError, match="no actual charge"):
        _entry(
            not_billable_evidence="plan",
            actual=Money("USD", 1), actual_provider_text="0.000000001",
        )


def test_every_monetary_field_uses_money():
    with pytest.raises(ResourceLedgerError, match="Money"):
        _entry(estimate=0.1)
    with pytest.raises(ResourceLedgerError, match="records an estimate"):
        _entry(estimate=None)
    record = _entry()
    record["quantity"]["cost_usd"] = 0.1
    with pytest.raises(EvaluationContractError):
        from benchmarks.evaluation_contracts import validate_record

        validate_record(record)


def test_unknown_resource_class_rejects():
    with pytest.raises(ResourceLedgerError, match="Unknown resource class"):
        _entry(resource_class="vibes")


# ── Totals ───────────────────────────────────────────────────────────


def _every_class_entries() -> list[dict]:
    entries = []
    for index, resource_class in enumerate(REQUIRED_CLASSES):
        estimate = _entry(
            resource_class=resource_class,
            estimate=Money("USD", 10_000_000 * (index + 1)),
        )
        entries.append(estimate)
        entries.append(_entry(
            resource_class=resource_class,
            estimate=Money("USD", 10_000_000 * (index + 1)),
            actual=Money("USD", 11_000_000 * (index + 1)),
            actual_provider_text=f"0.0{11 * (index + 1)}",
            estimate_entry_id=estimate["entry_id"],
        ))
    return entries


def test_summary_covers_every_class_and_keeps_totals_apart():
    entries = _every_class_entries()
    summary = summarize(entries, currency="USD")
    assert summary["no_use_classes"] == []
    assert set(summary["per_class"]) == set(REQUIRED_CLASSES)
    factor = sum(range(1, len(REQUIRED_CLASSES) + 1))
    # Each class holds one estimate-only entry and one confirmed entry
    # that also carries its estimate, so estimates total twice.
    assert summary["estimate_total"]["amount_nanos"] == (
        2 * 10_000_000 * factor
    )
    assert summary["actual_total"]["amount_nanos"] == 11_000_000 * factor
    assert summary["estimate_error_total"]["amount_nanos"] == (
        1_000_000 * factor
    )
    assert summary["entries_with_both"] == len(REQUIRED_CLASSES)


def test_summary_states_no_use_for_missing_classes():
    summary = summarize([_entry()], currency="USD")
    assert "judge" in summary["no_use_classes"]
    assert "human_review" in summary["no_use_classes"]


def test_retry_failed_scorer_and_excluded_result_still_contribute():
    first = _entry(resource_class="scorer", attempt_id="attempt-1")
    retry = _entry(
        resource_class="scorer", attempt_id="attempt-1",
        retry_of=first["entry_id"],
    )
    failed_scorer = _entry(
        resource_class="scorer",
        actual=Money("USD", 5_000_000), actual_provider_text="0.005",
        scorer_id="scorer-failed",
    )
    excluded = _entry(
        resource_class="runtime",
        actual=Money("USD", 7_000_000), actual_provider_text="0.007",
        attempt_id="attempt-excluded-infrastructure",
    )
    summary = summarize(
        [first, retry, failed_scorer, excluded], currency="USD",
    )
    assert retry["references"]["retry_of"] == first["entry_id"]
    assert summary["per_class"]["scorer"]["entries"] == 3
    assert summary["actual_total"]["amount_nanos"] == 12_000_000


def test_foreign_currency_fails_the_total():
    with pytest.raises(ResourceLedgerError, match="never converts"):
        summarize([_entry(estimate=Money("EUR", 1))], currency="USD")


def test_unknown_and_not_billable_never_become_zero_in_the_total():
    summary = summarize([
        _entry(estimate=None, price_unknown=True),
        _entry(estimate=None, not_billable_evidence="plan"),
        _entry(actual=Money("USD", 3), actual_provider_text="0.000000003"),
    ], currency="USD")
    assert len(summary["unknown_entry_ids"]) == 1
    assert len(summary["not_billable_entry_ids"]) == 1
    assert summary["actual_total"]["amount_nanos"] == 3


def test_cost_per_success_uses_the_unconditional_denominator():
    total = Money("USD", 1_000_000_000)
    assert cost_per_success(total, 4)["amount_nanos"] == 250_000_000
    assert cost_per_success(total, 3)["amount_nanos"] == 333_333_334
    assert cost_per_success(total, 0) is None


def test_cost_rules_fail_closed_on_unknown_amounts():
    rules = [{"metric": "actual_total", "operator": "lte",
              "value": {"currency": "USD", "amount_nanos": 10}}]
    passing = evaluate_cost_rules(rules, summarize([
        _entry(actual=Money("USD", 3), actual_provider_text="0.000000003"),
    ], currency="USD"))
    assert passing[0]["status"] == "passed"
    unknown = evaluate_cost_rules(rules, summarize([
        _entry(actual=Money("USD", 3), actual_provider_text="0.000000003"),
        _entry(estimate=None, price_unknown=True),
    ], currency="USD"))
    assert unknown[0]["status"] == "failed_unknown"


# ── Persistence and reconciliation ───────────────────────────────────


@pytest_asyncio.fixture
async def ledger_db(tmp_path, monkeypatch):
    path = str(tmp_path / "ledger.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    await make_attempts(1)
    monkeypatch.setattr(
        resource_ledger, "_reservations_for_run",
        _synthetic_reservations,
    )
    return path


async def _synthetic_reservations(run_id: str) -> list[dict]:
    return [{
        "reservation_id": "reservation-a",
        "state": "consumed",
        "currency": "USD",
        "requested_amount_nanos": 200_000_000,
        "reserved_amount_nanos": 200_000_000,
        "consumed_amount_nanos": 150_000_000,
        "released_amount_nanos": 50_000_000,
    }]


RULES = [{"metric": "actual_total", "operator": "lte",
          "value": {"currency": "USD", "amount_nanos": 150_000_000}}]


@pytest.mark.asyncio
async def test_entries_persist_immutably_and_reconcile(ledger_db):
    import aiosqlite

    estimate = await resource_ledger.record_event(
        _entry(reservation_id="reservation-a"),
    )
    await resource_ledger.record_event(_entry(
        actual=Money("USD", 120_000_000), actual_provider_text="0.12",
        reservation_id="reservation-a",
        estimate_entry_id=estimate["entry_id"],
    ))
    reconciled = await reconcile_run(
        RUN_ID, currency="USD", cost_rules=RULES,
        unconditional_successes=2, now=NOW,
    )
    record = reconciled["record"]
    assert record["reconciliation_version"] == 1
    assert record["summary"]["actual_total"]["amount_nanos"] == 120_000_000
    assert record["summary"]["estimate_total"]["amount_nanos"] == 200_000_000
    assert record["cost_rules"][0]["status"] == "passed"
    assert record["cost_per_success"]["amount_nanos"] == 60_000_000
    reservation = record["reservations"][0]
    assert reservation["reservation_id"] == "reservation-a"
    assert reservation["ledger_actual"]["amount_nanos"] == 120_000_000
    assert reservation["overshoot"] is False
    assert len(reservation["entry_ids"]) == 2
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE resource_ledger_entries SET charge_state = "
                "'confirmed' WHERE id = ?",
                (estimate["entry_id"],),
            )


@pytest.mark.asyncio
async def test_late_charge_never_replaces_its_estimate(ledger_db):
    from benchmarks import evaluation_records

    estimate = await resource_ledger.record_event(_entry())
    await reconcile_run(RUN_ID, currency="USD", cost_rules=RULES, now=NOW)
    outcome = await apply_late_charge(
        RUN_ID,
        currency="USD",
        entry=_entry(
            actual=Money("USD", 130_000_000), actual_provider_text="0.13",
            estimate_entry_id=estimate["entry_id"],
        ),
        cost_rules=RULES,
        now="2026-09-03T00:00:00Z",
    )
    assert outcome["reconciliation_version"] == 2
    # The estimate entry stays exactly as recorded.
    stored = await evaluation_records.get_record(
        "resource-ledger-entry", estimate["entry_id"],
    )
    assert stored["record"]["charge_state"] == "estimated"
    assert "actual" not in stored["record"]
    entries = await resource_ledger.list_entries(RUN_ID)
    assert [entry["charge_state"] for entry in entries] == [
        "estimated", "confirmed",
    ]
    assert entries[1]["estimate_entry_id"] == estimate["entry_id"]
    # A late charge under the rule limit changes no rule outcome.
    assert outcome["cost_rule_changed"] is False
    assert outcome["superseded_gates"] == 0


@pytest.mark.asyncio
async def test_late_charge_that_changes_a_cost_rule_supersedes(
    ledger_db, monkeypatch,
):
    from benchmarks import facade

    superseded: list[tuple[str, str]] = []

    async def fake_supersede(run_id, *, superseded_by):
        superseded.append((run_id, superseded_by))
        return 1

    status_changes: list[str] = []

    async def fake_status(run_id, target, **kwargs):
        status_changes.append(target)

    monkeypatch.setattr(
        repository, "supersede_gate_evaluations", fake_supersede,
    )
    monkeypatch.setattr(repository, "set_run_cost_status", fake_status)
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE benchmark_runs SET cost_status = 'settled' WHERE id = ?",
            (RUN_ID,),
        )
        await connection.commit()
    snapshot = valid_analysis_snapshot()
    await facade.execute(
        "record_analysis_snapshot",
        {"record": snapshot, "run_id": RUN_ID},
    )

    estimate = await resource_ledger.record_event(_entry())
    first = await reconcile_run(
        RUN_ID, currency="USD", cost_rules=RULES, now=NOW,
    )
    assert first["record"]["cost_rules"][0]["status"] == "passed"
    outcome = await apply_late_charge(
        RUN_ID,
        currency="USD",
        entry=_entry(
            actual=Money("USD", 400_000_000), actual_provider_text="0.40",
            estimate_entry_id=estimate["entry_id"],
        ),
        cost_rules=RULES,
        now="2026-09-03T00:00:00Z",
    )
    assert outcome["cost_rule_changed"] is True
    assert outcome["superseded_gates"] == 1
    assert superseded == [(RUN_ID, outcome["reconciliation_id"])]
    assert status_changes == ["settling"]
    assert outcome["analysis_recompute_required"] is True
    assert outcome["affected_analysis_snapshot_ids"] == [
        snapshot["snapshot_id"],
    ]
    with pytest.raises(ResourceLedgerError, match="confirmed entry"):
        await apply_late_charge(
            RUN_ID, currency="USD", entry=_entry(), cost_rules=RULES,
        )
