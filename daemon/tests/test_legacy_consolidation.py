"""Legacy consolidation: one authority, measured gates, populated rollback.

The daemon scorer plugins own the ported grade-school numeric scoring
and the soak reliability measures, legacy result summaries migrate
with every unmigrated field marked unavailable, the fallback gate
passes only through a measurement against the declared threshold, the
destructive contract refuses without measured fallback, populated
rollback, and retention evidence, and the removal-gate view reports
that evidence. Rollback runs against populated legacy and current
records, and analysis replay stays separate from execution repetition.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from test_evaluation_contracts import (
    valid_benchmark_source,
    valid_dataset_draft,
    valid_evaluation_case,
)
from test_evidence_capture import make_attempts

import database as db
from benchmarks import (
    evaluation_migration,
    evaluation_records,
    facade,
    legacy_adapters,
    scorer_plugins,
)
from benchmarks.evaluation_migration import (
    DECLARED_FALLBACK_THRESHOLD,
    ContractRefusedError,
    measure_fallback_window,
    record_measured_fallback_gate,
    removal_gate_evidence,
)
from routes import evaluation as evaluation_routes

REPO_ROOT = Path(__file__).resolve().parents[2]
# The handlers accept no request when the API key check is empty.
NO_REQUEST: Any = None


# ── Ported scoring ───────────────────────────────────────────────────


@pytest.mark.parametrize(("response", "expected", "passed"), [
    ("The total is 3 + 4 = 7. #### 1,234", "1234", True),
    ("The answer is 42.0", "42", True),
    ("Answer: -3.50", "-3.5", True),
    ("I computed 12 then 15", "12", False),
    ("", "1", False),
])
def test_last_number_comparison_ports_the_legacy_convention(
    response, expected, passed,
):
    result = scorer_plugins.DeterministicAnswerScorer().score(
        {"final_output": response, "reference_answer": expected},
        {"comparison": "last_number"},
    )
    assert result["passed"] is passed


def test_normalize_number_keeps_large_integers_exact():
    assert scorer_plugins.normalize_number("9,007,199,254,740,993") == (
        "9007199254740993"
    )
    assert scorer_plugins.normalize_number("42.0") == "42"
    assert scorer_plugins.extract_last_number("no digits") is None


def test_reliability_scorer_ports_every_soak_measure():
    outcomes = [
        {"effective_actions": 10, "exact_success": True, "completed": True,
         "retrieval_expected": 2, "retrieval_found": 2,
         "minority_opportunities": 1, "minority_corrections": 1,
         "role_measurements": [{"role": "planner", "cost_usd": 0.2,
                                "latency_ms": 200}]},
        {"effective_actions": 14, "exact_success": False, "completed": True,
         "restart_attempted": True, "restart_recovered": True,
         "external_action_keys": ["send", "send"], "budget_limit_usd": 1.0,
         "budget_spent_usd": 1.5, "stall_count": 1, "replan_count": 2,
         "unresolved_conflicts": 1,
         "role_measurements": [{"role": "planner", "cost_usd": 0.4,
                                "latency_ms": 400}]},
    ]
    result = scorer_plugins.ReliabilityScorer().score(
        {"trial_outcomes": outcomes}, {"baseline_success": 1.0},
    )
    values = {d["name"]: d["value"] for d in result["dimensions"]}
    assert values["exact_task_success"] == 0.5
    assert values["strict_repeated_run_success"] == 0.0
    assert values["false_completion_rate"] == 0.5
    assert values["reliability_decay"] == 0.5
    assert values["restart_recovery_rate"] == 1.0
    assert values["duplicate_external_actions"] == 1.0
    assert values["budget_overshoot_rate"] == 0.5
    assert values["context_retrieval_recall"] == 1.0
    assert values["minority_correction_rate"] == 1.0
    assert values["average_effective_actions"] == 12.0
    planner = result["evidence_marks"]["role_metrics"]["planner"]
    assert planner["activations"] == 2
    assert planner["p95_latency_ms"] == 400.0
    assert result["passed"] is False


def test_reliability_scorer_rejects_inconsistent_outcomes():
    with pytest.raises(scorer_plugins.ScorerPluginError, match="exceeds"):
        scorer_plugins.ReliabilityScorer().score({"trial_outcomes": [
            {"effective_actions": 1, "retrieval_expected": 1,
             "retrieval_found": 2},
        ]}, {})
    assert scorer_plugins.ReliabilityScorer().score({}, {})["status"] == (
        "unavailable"
    )
    assert scorer_plugins.plugin_for("reliability").plugin_type == (
        "reliability"
    )


# ── Legacy result summary migration ──────────────────────────────────


def test_legacy_summary_migrates_with_unavailable_fields():
    migrated = legacy_adapters.migrate_legacy_result_summary({
        "run_id": "bench-gsm8k-1", "dataset": "gsm8k", "dataset_size": 20,
        "accuracy": 0.65, "completed_tasks": 19, "total_cost_usd": 0.42,
        "total_tokens": 1200, "avg_latency_ms": 900.0,
        "terminated_by": {"consensus": 19}, "joules_estimate": None,
        "avg_tokens_per_task": 60.0, "variants": {"classic": 19},
    })
    assert migrated["unconditional_success"] == {
        "rate": 0.65, "successes": 13, "denominator": 20,
        "denominator_statement": "legacy dataset size",
    }
    assert migrated["cost"]["source"] == "legacy_float"
    assert migrated["cost"]["authoritative"] is False
    assert migrated["unavailable_fields"] == [
        "avg_tokens_per_task", "joules_estimate", "variants",
    ]
    assert migrated["migration"]["authority"] == (
        "compatibility_projection_only"
    )
    assert len(migrated["record_digest"]) == 64
    with pytest.raises(ValueError, match="run_id"):
        legacy_adapters.migrate_legacy_result_summary({"dataset_size": 1})


# ── The continuous-integration write guard ───────────────────────────


def test_the_write_guard_passes_on_the_repository():
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/check-legacy-writes.py")],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PASS" in completed.stdout


# ── Measured gates, populated rollback, and retention ────────────────


@pytest_asyncio.fixture
async def consolidation_db(tmp_path, monkeypatch):
    path = str(tmp_path / "consolidation.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    facade.reset_metrics()
    await db.init_db()
    await make_attempts(1)
    return path


@pytest.mark.asyncio
async def test_fallback_gate_passes_only_through_measurement(
    consolidation_db,
):
    await evaluation_migration.record_fallback("run-plan", "run-legacy")
    await facade.record_direct_legacy_call("eval.cli.benchmark")
    measurement = await measure_fallback_window(
        window_start="1970-01-01T00:00:00Z",
    )
    assert measurement["total_legacy_use"] == 2
    assert measurement["threshold"] == DECLARED_FALLBACK_THRESHOLD == 0
    assert measurement["passed"] is False
    recorded = await record_measured_fallback_gate(
        window_start="1970-01-01T00:00:00Z", actor="operator-a",
    )
    assert recorded["passed"] is False
    evidence = await removal_gate_evidence()
    assert evidence["fallback"]["measured"] is True
    assert evidence["fallback"]["passed"] is False
    # A later window with no legacy use passes at the threshold.
    later = await record_measured_fallback_gate(
        window_start="2999-01-01T00:00:00Z", actor="operator-a",
    )
    assert later["passed"] is True
    with_threshold = await measure_fallback_window(
        window_start="1970-01-01T00:00:00Z", threshold=2,
    )
    assert with_threshold["passed"] is True


@pytest.mark.asyncio
async def test_contract_refuses_without_measured_evidence(consolidation_db):
    for name in evaluation_migration.DELETION_GATES:
        await evaluation_migration.record_gate(name, True, actor="operator-a")
    with pytest.raises(ContractRefusedError, match="measured gate evidence"):
        await evaluation_migration.assert_contract_allowed()


@pytest.mark.asyncio
async def test_populated_rollback_records_retention_evidence(
    consolidation_db,
):
    source = await evaluation_records.save_record(valid_benchmark_source())
    draft = await evaluation_records.save_record(
        valid_dataset_draft(),
        links={"source_id": source["id"], "parent_version_id": None},
    )
    await evaluation_records.save_record(
        valid_evaluation_case(), links={"draft_id": draft["id"]},
    )
    export = await evaluation_migration.compatibility_export("run-evidence")
    assert export["verified"] is True
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS legacy FROM benchmark_attempts",
        )
        legacy_attempts = int((await cursor.fetchone())["legacy"])
    outcome = await evaluation_records.downgrade_evaluation_expansion()
    assert outcome["archived_records"] >= 3
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS legacy FROM benchmark_attempts",
        )
        assert int((await cursor.fetchone())["legacy"]) == legacy_attempts
        cursor = await connection.execute(
            "SELECT COUNT(*) AS archived FROM evaluation_readonly_archive",
        )
        archived = int((await cursor.fetchone())["archived"])
    assert archived == outcome["archived_records"]
    await db.init_db()
    await evaluation_migration.record_gate(
        "downgrade_fixtures_passed", True, actor="operator-a",
        evidence={"legacy_records": legacy_attempts,
                  "current_records": 3, "archived_records": archived},
    )
    await evaluation_migration.record_gate(
        "compatibility_export_verified", True, actor="operator-a",
        evidence={"verified_exports": 1},
    )
    evidence = await removal_gate_evidence()
    assert evidence["rollback"]["populated"] is True
    assert evidence["rollback"]["evidence"]["legacy_records"] == legacy_attempts
    assert evidence["retention"]["verified_exports"] >= 1
    assert evidence["retention"]["passed"] is True


@pytest.mark.asyncio
async def test_contract_allows_only_with_every_measured_gate(
    consolidation_db,
):
    await record_measured_fallback_gate(
        window_start="1970-01-01T00:00:00Z", actor="operator-a",
    )
    await evaluation_migration.record_gate(
        "downgrade_fixtures_passed", True, actor="operator-a",
        evidence={"legacy_records": 1, "current_records": 1,
                  "archived_records": 1},
    )
    await evaluation_migration.record_gate(
        "compatibility_export_verified", True, actor="operator-a",
        evidence={"verified_exports": 1},
    )
    for name in ("backfill_without_mismatch", "upgrade_fixtures_passed",
                 "operator_approval"):
        await evaluation_migration.record_gate(name, True, actor="operator-a")
    await evaluation_migration.assert_contract_allowed()


@pytest.mark.asyncio
async def test_direct_legacy_calls_record_through_the_facade(
    consolidation_db,
):
    no_request = NO_REQUEST
    recorded = await evaluation_routes.record_legacy_fallback_endpoint(
        no_request,
        evaluation_routes.LegacyFallbackInput(entry_point="eval.cli.ab"),
    )
    assert recorded["recorded"] is True
    snapshot = await evaluation_migration.authority_snapshot()
    assert snapshot["direct_legacy_call_events"] == 1
    preview = await evaluation_routes.score_preview_endpoint(
        no_request,
        evaluation_routes.ScorePreviewInput(
            plugin_type="deterministic",
            evidence={"final_output": "#### 7", "reference_answer": "7"},
            configuration={"comparison": "last_number"},
        ),
    )
    assert preview["persisted"] is False
    assert preview["result"]["passed"] is True
    migrated = await evaluation_routes.migrate_legacy_result_endpoint(
        no_request,
        evaluation_routes.LegacySummaryInput(summary={
            "run_id": "bench-1", "dataset_size": 4, "accuracy": 0.5,
            "joules_estimate": None,
        }),
    )
    assert migrated["unavailable_fields"] == ["joules_estimate"]
    events = await evaluation_migration.list_events("backfill_run")
    assert events[-1]["payload"]["target"] == "legacy_result_files"
