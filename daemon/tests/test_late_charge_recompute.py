"""A late charge that changes a cost rule recomputes every analysis.

The recomputation freezes the same specification and planned
repetitions over the stored evidence with the new ledger summary,
stores the result as a new immutable snapshot, and records one
supersession from the old snapshot to the new one. The old snapshot
never changes, the listing shows which snapshot is current, and a
snapshot that cannot rebuild stays flagged without failing the
charge.
"""

from __future__ import annotations

import pytest
from test_frozen_analysis import _stored_spec
from test_resource_ledger_reconciliation import (  # noqa: F401 - fixtures
    NOW,
    RULES,
    RUN_ID,
    _entry,
    ledger_db,
)

from benchmarks import evaluation_records, frozen_analysis, resource_ledger
from core.money import Money


@pytest.mark.asyncio
async def test_late_charge_recomputes_and_supersedes_the_snapshot(ledger_db):  # noqa: F811
    stored = await frozen_analysis.freeze_and_store(
        RUN_ID, specification=_stored_spec(), planned_repetitions=1,
    )
    original_id = stored["snapshot_id"]
    original = await evaluation_records.get_record(
        "analysis-snapshot", original_id,
    )
    assert original["record"]["resampling"]["planned_repetitions"] == 1

    estimate = await resource_ledger.record_event(_entry())
    await resource_ledger.reconcile_run(
        RUN_ID, currency="USD", cost_rules=RULES, now=NOW,
    )
    outcome = await resource_ledger.apply_late_charge(
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
    assert outcome["recompute_failures"] == []
    assert outcome["affected_analysis_snapshot_ids"] == [original_id]
    (recomputed,) = outcome["recomputed_analysis_snapshots"]
    assert recomputed["superseded_snapshot_id"] == original_id
    assert recomputed["snapshot_id"] != original_id
    assert recomputed["resources"]["available"] is True
    assert recomputed["resources"]["actual_total"]["amount_nanos"] == (
        400_000_000
    )

    replacement = await evaluation_records.get_record(
        "analysis-snapshot", recomputed["snapshot_id"],
    )
    assert replacement["run_id"] == RUN_ID
    assert replacement["record"]["estimand"] == original["record"]["estimand"]
    # The old snapshot stays exactly as stored.
    again = await evaluation_records.get_record(
        "analysis-snapshot", original_id,
    )
    assert again["record"] == original["record"]

    supersessions = await evaluation_records.list_snapshot_supersessions(
        RUN_ID,
    )
    assert [(row["snapshot_id"], row["superseded_by"]) for row in supersessions] == [
        (original_id, recomputed["snapshot_id"]),
    ]
    assert supersessions[0]["reason"] == "late_charge_changed_cost_rule"
    assert supersessions[0]["reconciliation_id"] == outcome["reconciliation_id"]

    # A second late charge recomputes only the current snapshot.
    second = await resource_ledger.apply_late_charge(
        RUN_ID,
        currency="USD",
        entry=_entry(
            actual=Money("USD", 10_000_000), actual_provider_text="0.01",
        ),
        cost_rules=[{"metric": "actual_total", "operator": "lte",
                     "value": {"currency": "USD",
                               "amount_nanos": 1_000_000_000}}],
        now="2026-09-04T00:00:00Z",
    )
    assert second["cost_rule_changed"] is True
    assert second["affected_analysis_snapshot_ids"] == [
        recomputed["snapshot_id"],
    ]
    assert len(await evaluation_records.list_snapshot_supersessions(RUN_ID)) == 2


@pytest.mark.asyncio
async def test_a_snapshot_that_cannot_rebuild_stays_flagged(ledger_db):  # noqa: F811
    from test_evaluation_contracts import valid_analysis_snapshot

    from benchmarks import facade

    snapshot = valid_analysis_snapshot()
    await facade.execute(
        "record_analysis_snapshot", {"record": snapshot, "run_id": RUN_ID},
    )
    estimate = await resource_ledger.record_event(_entry())
    await resource_ledger.reconcile_run(
        RUN_ID, currency="USD", cost_rules=RULES, now=NOW,
    )
    outcome = await resource_ledger.apply_late_charge(
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
    assert outcome["recomputed_analysis_snapshots"] == []
    assert [entry["superseded_snapshot_id"] for entry in outcome["recompute_failures"]] == [
        snapshot["snapshot_id"],
    ]
    assert outcome["analysis_recompute_required"] is True


@pytest.mark.asyncio
async def test_listing_marks_superseded_snapshots(ledger_db):  # noqa: F811
    from routes import evaluation as evaluation_routes

    stored = await frozen_analysis.freeze_and_store(
        RUN_ID, specification=_stored_spec(), planned_repetitions=1,
    )
    recomputed = await frozen_analysis.recompute_snapshot(
        stored["snapshot_id"], ledger_summary=None, reason="manual",
    )
    listing = await evaluation_routes.list_analyses_endpoint(RUN_ID)
    listed = {row["id"]: row for row in listing["snapshots"]}
    assert listed[stored["snapshot_id"]]["current"] is False
    assert listed[stored["snapshot_id"]]["superseded_by"] == (
        recomputed["snapshot_id"]
    )
    assert listed[recomputed["snapshot_id"]]["current"] is True
