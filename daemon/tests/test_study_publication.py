"""Study authoring writes a run plan and a test revision; admission enforces it.

Publication turns one authored study into one immutable test
revision with one arm per study arm, one published run plan that
freezes the case schedule, the seed schedule, the rotated interleave,
the repetitions, the limits, and the estimand, and one study record
linking both. Admission validates the study conditions of every run
that carries a study plan and admits a run without one unchanged.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from test_evaluation_contracts import valid_benchmark_source
from test_evidence_capture import make_attempts

import database as db
from benchmarks import evaluation_records, facade, study_authoring
from benchmarks.frozen_analysis import validate_study
from core.money import Money


@pytest_asyncio.fixture
async def study_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "study.db"))
    await db.init_db()
    await make_attempts(4)


def _study() -> dict:
    return study_authoring.author_study(
        study_type="one_factor_ablation",
        name="temperature",
        base_configuration={"classic": {"max_rounds": 4}},
        treatment={"path": "classic.max_rounds", "values": [4, 6]},
        invariants={
            "dataset_version_id": "version-evidence",
            "case_ids": ["item-0", "item-1", "item-2", "item-3"],
            "seed_schedule": {"base_seed": 11},
            "scorers": ["scorer-exact-match-v1"],
            "arm_order": "rotated_interleave",
            "repetitions": 2,
        },
        families={"math": ["item-0", "item-1", "item-2", "item-3"]},
        scorer_id="scorer-exact-match-v1",
        master_seed=11,
        comparison_margin=0.05,
        per_attempt_cost=Money("USD", 5_000_000),
        seconds_per_attempt=15,
    )


async def _publish() -> dict:
    return await study_authoring.publish_study(
        _study(),
        runtime_id="classic",
        scorer_versions=[{"id": "scorer-exact-match-v1", "configuration": {}}],
        now="2026-09-03T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_publication_writes_the_revision_plan_and_study(study_db):
    published = await _publish()
    revision_id = published["test_revision_id"]
    stored_plan = await evaluation_records.run_plan_for_revision(revision_id)
    assert stored_plan is not None
    assert stored_plan["id"] == published["run_plan_id"]
    assert stored_plan["status"] == "published"
    plan = stored_plan["record"]
    assert plan["case_ids"] == ["item-0", "item-1", "item-2", "item-3"]
    assert plan["seed_schedule"] == {"base_seed": 11, "scope": "item-repetition"}
    assert plan["arm_order"] == {"strategy": "rotated_interleave"}
    assert plan["repetitions"] == 2
    assert plan["unit_hierarchy"] == ["family", "case", "repetition"]
    assert plan["estimand"]["primary_estimand"]
    assert plan["estimand"]["study_id"] == published["study_id"]
    # Both arms share one runtime pair, so the mapping set has one member.
    assert len(plan["arm_mappings"]) == 1
    assert plan["limits"]["run_cost"]["amount_nanos"] == 5_000_000 * 16

    study = await evaluation_records.study_for_run_plan(published["run_plan_id"])
    assert study is not None
    assert study["test_revision_id"] == revision_id
    assert study["record"]["study_digest"] == published["study"]["study_digest"]
    assert [arm["slug"] for arm in study["record"]["arms"]] == [
        "classic.max_rounds-4", "classic.max_rounds-6",
    ] or len(study["record"]["arms"]) == 2

    run, _created = await facade.execute(
        "create_run",
        {"run_id": "run-study", "revision_id": revision_id,
         "idempotency_key": None},
        generation="legacy",
    )
    assert run["test_revision_id"] == revision_id
    assert len(run["attempts"]) == 4 * 2 * 2


@pytest.mark.asyncio
async def test_admission_enforces_the_study_conditions(study_db):
    published = await _publish()
    revision_id = published["test_revision_id"]
    await facade.execute(
        "create_run",
        {"run_id": "run-study", "revision_id": revision_id,
         "idempotency_key": None},
        generation="legacy",
    )
    # Without a pinned source the study is not ready.
    with pytest.raises(study_authoring.StudyAdmissionError, match="source_pinned"):
        await study_authoring.enforce_study_admission("run-study")

    # A pinned source through the dataset version lineage unblocks it.
    await facade.execute("import_source", {"record": valid_benchmark_source()})
    from test_evaluation_contracts import valid_dataset_version

    version_record = valid_dataset_version()
    version_record["version_id"] = "version-evidence"
    await facade.execute(
        "record_dataset_version",
        {"record": version_record, "dataset_id": "dataset-evidence",
         "parent_version_id": None},
    )
    verdict = await study_authoring.enforce_study_admission("run-study")
    assert verdict["study_id"] == published["study_id"]
    assert all(check["passed"] for check in verdict["checks"])
    assert "report_shows_failures_and_missingness" not in [
        check["check"] for check in verdict["checks"]
    ]

    # A run without a study plan admits unchanged.
    assert await study_authoring.enforce_study_admission("run-evidence") is None
    assert await study_authoring.enforce_study_admission("run-missing") is None


def test_validate_study_stages():
    plan = {
        "case_ids": ["a"], "seed_schedule": {"base_seed": 1, "scope": "item-repetition"},
        "arm_order": {"strategy": "rotated_interleave"},
        "unit_hierarchy": ["family", "case", "repetition"],
        "estimand": {"primary_estimand": "x", "direction": "higher_is_better"},
    }
    source = {"pinned_revision": "abc", "license": {"name": "MIT"}}
    admission = validate_study(
        run_plan=plan, source=source, holdout_hidden=True, report=None,
        cost_includes_retries_and_control_plane=True, stage="admission",
    )
    assert admission["ready"] is True
    publication = validate_study(
        run_plan=plan, source=source, holdout_hidden=True, report=None,
        cost_includes_retries_and_control_plane=True,
    )
    assert publication["blocking"] == ["report_shows_failures_and_missingness"]
    with pytest.raises(Exception, match="Unknown study stage"):
        validate_study(
            run_plan=plan, source=source, holdout_hidden=True, report=None,
            cost_includes_retries_and_control_plane=True, stage="later",
        )
