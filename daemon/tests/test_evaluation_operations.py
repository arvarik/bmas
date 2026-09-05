"""Read routes for the operations screens and the study admission failure.

The study route reports the admission verdict of one run without
raising, the scheduler maps a blocked study to a configuration failure
that names every blocking condition, the reconciliation and score
record listings read the stored rows per run and per attempt, the
evidence section route serves one redacted section by digest, the
evidence record carries a redaction report, and a draft metric
definition revises in place while every other lifecycle state rejects
the revision.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import HTTPException
from test_evaluation_contracts import (
    valid_benchmark_source,
    valid_dataset_version,
    valid_scorer_spec,
)
from test_evidence_capture import make_attempts
from test_frozen_report import publishable_metric_definition
from test_study_publication import _publish

import database as db
from benchmarks import (
    admission,
    evidence_capture,
    facade,
    metric_registry,
    resource_ledger,
    scheduler,
    score_execution,
    study_authoring,
)
from routes import evaluation as routes

RUN_ID = "run-evidence"


@pytest_asyncio.fixture
async def operations_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "operations.db"))
    await db.init_db()
    attempts = await make_attempts(4)
    await facade.execute(
        "register_scorer_version", {"record": valid_scorer_spec()},
    )
    return attempts


@pytest.mark.asyncio
async def test_the_study_route_reports_the_verdict_without_raising(
    operations_db,
):
    published = await _publish()
    await facade.execute(
        "create_run",
        {"run_id": "run-study", "revision_id": published["test_revision_id"],
         "idempotency_key": None},
        generation="legacy",
    )
    blocked = await routes.read_run_study_endpoint("run-study")
    assert blocked["study_id"] == published["study_id"]
    assert blocked["study"]["name"] == "temperature"
    assert blocked["verdict"]["ready"] is False
    assert "source_pinned" in blocked["verdict"]["blocking"]
    assert any(not check["passed"] for check in blocked["verdict"]["checks"])

    await facade.execute("import_source", {"record": valid_benchmark_source()})
    version_record = valid_dataset_version()
    version_record["version_id"] = "version-evidence"
    await facade.execute(
        "record_dataset_version",
        {"record": version_record, "dataset_id": "dataset-evidence",
         "parent_version_id": None},
    )
    ready = await routes.read_run_study_endpoint("run-study")
    assert ready["verdict"]["ready"] is True
    assert ready["verdict"]["blocking"] == []

    plain = await routes.read_run_study_endpoint(RUN_ID)
    assert plain == {"run_id": RUN_ID, "study_id": None, "plan_id": None,
                     "study": None, "verdict": None}
    with pytest.raises(HTTPException) as missing:
        await routes.read_run_study_endpoint("run-missing")
    assert missing.value.status_code == 404

    listing = await routes.list_studies_endpoint()
    assert [entry["study_id"] for entry in listing["studies"]] == [
        published["study_id"],
    ]
    assert listing["studies"][0]["test_revision_id"] == (
        published["test_revision_id"]
    )
    detail = await routes.read_study_endpoint(published["study_id"])
    assert detail["record"]["study_digest"] == published["study"]["study_digest"]
    with pytest.raises(HTTPException):
        await routes.read_study_endpoint("study-missing")


@pytest.mark.asyncio
async def test_a_blocked_study_maps_to_a_configuration_failure(monkeypatch):
    async def blocked(_attempt):
        raise study_authoring.StudyAdmissionError(
            "The study conditions block admission: source_pinned"
        )

    monkeypatch.setattr(admission, "admit_attempt", blocked)
    with pytest.raises(HTTPException) as failure:
        await scheduler._admit({"id": "attempt-a"})  # noqa: SLF001
    assert failure.value.status_code == 422
    assert failure.value.detail["code"] == "benchmark_study_blocked"
    assert "source_pinned" in failure.value.detail["message"]


@pytest.mark.asyncio
async def test_reconciliations_list_every_settlement_version(operations_db):
    assert (await routes.list_reconciliations_endpoint(RUN_ID)) == {
        "run_id": RUN_ID, "reconciliations": [],
    }
    first = await resource_ledger.reconcile_run(
        RUN_ID, currency="USD", now="2026-09-04T00:00:00Z",
    )
    second = await resource_ledger.reconcile_run(
        RUN_ID, currency="USD", reason="operator", now="2026-09-04T01:00:00Z",
    )
    listing = await routes.list_reconciliations_endpoint(RUN_ID)
    versions = listing["reconciliations"]
    assert [entry["id"] for entry in versions] == [
        first["reconciliation_id"], second["reconciliation_id"],
    ]
    assert [entry["record"]["reconciliation_version"] for entry in versions] == [1, 2]
    assert versions[1]["record"]["supersedes_reconciliation"] == (
        first["reconciliation_id"]
    )
    assert versions[1]["record"]["reason"] == "operator"
    with pytest.raises(HTTPException) as missing:
        await routes.list_reconciliations_endpoint("run-missing")
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_score_records_list_per_attempt_with_their_boundary(
    operations_db,
):
    first, second, *_rest = operations_db
    for attempt_id in (first, second):
        await evidence_capture.capture_attempt_evidence(
            attempt_id=attempt_id,
            run_manifest={"run_id": RUN_ID},
            runtime_specification={"runtime": "classic"},
            case={"case_id": "case-0"},
            trace_events=[{"kind": "action", "action": "answer"}],
            final_output="42",
            resources={"cost": None, "tokens": 10, "latency_ms": 5},
            seed_evidence={"requested_seed": 1, "seed_control": "recorded"},
            ledger_references={"reservation_id": "reservation-a"},
        )
    await score_execution.score_attempt(
        attempt_id=first,
        scorer_id="scorer-exact-match",
        scorer_version="2",
        plugin_type="deterministic",
        configuration={"comparison": "exact"},
        extra_evidence={"final_output": "42", "reference_answer": "42"},
    )
    listing = await routes.list_attempt_score_records_endpoint(first)
    assert listing["attempt_id"] == first
    assert len(listing["scores"]) == 1
    stored = listing["scores"][0]
    assert stored["attempt_id"] == first
    assert stored["record"]["sandbox"]["boundary"] == "trusted_service"
    assert stored["record"]["sandbox"]["terminal_class"] == "completed"
    assert stored["record"]["status"] == "scored"
    other = await routes.list_attempt_score_records_endpoint(second)
    assert other["scores"] == []


@pytest.mark.asyncio
async def test_evidence_records_a_redaction_report_and_serves_sections(
    operations_db,
):
    first = operations_db[0]
    captured = await evidence_capture.capture_attempt_evidence(
        attempt_id=first,
        run_manifest={"run_id": RUN_ID},
        runtime_specification={"runtime": "classic"},
        case={"case_id": "case-0"},
        trace_events=[{
            "kind": "tool_call",
            "api_key": "sk-live-secret-value",
            "argument": "safe",
            "reviewer_email": "person@example.com",
        }],
        final_output="Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        resources={"cost": None, "tokens": 10, "latency_ms": 5},
        seed_evidence={"requested_seed": 1, "seed_control": "recorded"},
        ledger_references={"reservation_id": "reservation-a"},
        recovery_events=[{"kind": "checkpoint", "password": "hunter2"}],
    )
    report = captured["record"]["redaction_report"]
    assert "trace[0].api_key" in report["secret"]
    assert "trace[0].reviewer_email" in report["sensitive"]
    assert "recovery_events[0].password" in report["secret"]
    assert report["detectors"]["final_output"] == "bearer_or_basic_header"
    assert "final_output" in report["secret"]
    assert report["policy_digest"] == captured["record"]["redaction_policy_digest"]

    stored = await routes.read_attempt_evidence_endpoint(first)
    assert stored["record"]["redaction_report"] == report

    section = await routes.read_evidence_section_endpoint(
        captured["record"]["trace_digest"],
    )
    assert section["redacted"] is False
    assert section["value"][0]["argument"] == "safe"
    assert "sk-live-secret-value" not in str(section["value"])
    with pytest.raises(HTTPException) as missing:
        await routes.read_evidence_section_endpoint("0" * 64)
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_a_draft_metric_revises_in_place_and_others_reject(
    operations_db,
):
    definition = publishable_metric_definition()
    metric_id = definition["metric_id"]
    await facade.execute(
        "register_metric_definition", {"record": definition},
    )
    before = await routes.get_metric_endpoint(metric_id)
    revised = dict(definition)
    revised["measurement"] = {
        **definition["measurement"],
        "denominator": "Planned cases minus predeclared exclusions.",
    }
    outcome = await facade.execute(
        "revise_metric_definition",
        {"record_id": metric_id, "record": revised},
    )
    after = await routes.get_metric_endpoint(metric_id)
    assert outcome["record_checksum"] == after["record_checksum"]
    assert after["record_checksum"] != before["record_checksum"]
    assert after["record"]["measurement"]["denominator"].startswith("Planned")
    assert after["lifecycle_state"] == "draft"

    wrong_id = {**revised, "metric_id": "metric-other"}
    with pytest.raises(Exception, match="keep its metric id"):
        await facade.execute(
            "revise_metric_definition",
            {"record_id": metric_id, "record": wrong_id},
        )
    promoted = {**revised, "lifecycle_state": "validated"}
    with pytest.raises(Exception, match="draft lifecycle state"):
        await facade.execute(
            "revise_metric_definition",
            {"record_id": metric_id, "record": promoted},
        )

    await metric_registry.advance(
        metric_id, "validated", now="2026-09-03T00:00:00Z",
        validation_evidence={"schema": True, "fixture": True, "evidence": True},
    )
    with pytest.raises(Exception, match="is not a draft"):
        await facade.execute(
            "revise_metric_definition",
            {"record_id": metric_id, "record": revised},
        )


@pytest.mark.asyncio
async def test_invalid_overrides_fail_the_attempt_instead_of_retrying(
    monkeypatch,
):
    from pydantic import ValidationError

    from routes.submit import TaskOverrides

    async def invalid(_attempt):
        TaskOverrides.model_validate({"model_routing": {"medium": "model-a"}})

    monkeypatch.setattr(admission, "admit_attempt", invalid)
    with pytest.raises(HTTPException) as failure:
        await scheduler._admit({"id": "attempt-a"})  # noqa: SLF001
    assert failure.value.status_code == 422
    assert failure.value.detail["code"] == "benchmark_configuration_invalid"
    assert "model_routing" in failure.value.detail["message"]
    with pytest.raises(ValidationError):
        TaskOverrides.model_validate({"model_routing": {}})


@pytest.mark.asyncio
async def test_publication_rejects_overrides_the_dispatch_refuses(
    operations_db,
):
    from benchmarks import runtime
    from core.money import Money

    study = study_authoring.author_study(
        study_type="one_factor_ablation",
        name="routing",
        base_configuration={"model_routing": {"medium": "model-a"}},
        treatment={"path": "model_routing.medium",
                   "values": ["model-a", "model-b"]},
        invariants={
            "dataset_version_id": "version-evidence",
            "case_ids": ["item-0", "item-1"],
            "seed_schedule": {"base_seed": 11},
            "scorers": ["scorer-exact-match-v1"],
            "arm_order": "rotated_interleave",
            "repetitions": 1,
        },
        families={"math": ["item-0", "item-1"]},
        scorer_id="scorer-exact-match-v1",
        master_seed=11,
        comparison_margin=0.05,
        per_attempt_cost=Money("USD", 5_000_000),
        seconds_per_attempt=15,
    )
    with pytest.raises(
        runtime.BenchmarkRuntimeConfigurationError, match="model_routing",
    ):
        await study_authoring.publish_study(
            study,
            runtime_id="classic",
            scorer_versions=[{"id": "scorer-exact-match-v1",
                              "configuration": {}}],
            now="2026-09-04T00:00:00Z",
        )
    accepted = await runtime.prepare_benchmark_arm(
        "classic", {"submission_overrides": {"classic": {"max_rounds": 6}}},
    )
    assert accepted["configuration"]["effective_configuration"]["settings"]["classic"]["max_rounds"] == 6
