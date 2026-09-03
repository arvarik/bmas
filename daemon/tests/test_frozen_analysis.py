"""Frozen analysis: specification first, clustered statistics second.

The suite freezes every input and estimand before calculation,
normalizes family and case weights separately with zero-sum
rejection, reduces slots before weights, removes paired slots on
infrastructure missingness, renormalizes weights over usable cases,
enforces the missing-weight limits, marks small clusters
insufficient, reproduces every oracle draw and family aggregate,
applies each case weight once and never as a draw probability,
decides predeclared non-inferiority gates with Holm correction, pins
every snapshot digest, replays stored evidence to equal checksums,
separates analysis replay from execution provenance, and validates
study conditions.
"""

from __future__ import annotations

import json
import locale
import os
import time
from fractions import Fraction
from pathlib import Path

import pytest
import pytest_asyncio
from test_evaluation_contracts import valid_run_plan
from test_evidence_capture import make_attempts

import database as db
from benchmarks import frozen_analysis as fa
from benchmarks.frozen_analysis import (
    FrozenAnalysisError,
    bootstrap,
    compute_report,
    execution_provenance,
    freeze_input,
    freeze_specification,
    gate_decision,
    holm_adjust,
    pair_cases,
    replay,
    validate_study,
    weighted_estimate,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "analysis_rng.json").read_text(),
)
ORACLE = FIXTURES["weighted_bootstrap_oracle"]


# ── Helpers ──────────────────────────────────────────────────────────


def run_from_slots(
    slots_by_case: dict[str, dict[str, list]],
) -> tuple[dict, dict[str, list[str]]]:
    """Build one run from ``{family: {case_id: [(left, right), ...]}}``.

    ``None`` marks an infrastructure failure that never seals.
    """
    attempts, scores = [], []
    families: dict[str, list[str]] = {}
    for family, cases in slots_by_case.items():
        for case_id, slots in cases.items():
            families.setdefault(family, []).append(case_id)
            for repeat, (left, right) in enumerate(slots, start=1):
                for arm, value in (("left", left), ("right", right)):
                    attempt_id = f"{arm}-{case_id}-{repeat}"
                    if value is None:
                        attempts.append({
                            "id": attempt_id, "arm_id": arm,
                            "dataset_item_id": case_id,
                            "repeat_index": repeat, "retry_index": 0,
                            "status": "failed",
                            "failure_category": "infrastructure",
                        })
                        continue
                    attempts.append({
                        "id": attempt_id, "arm_id": arm,
                        "dataset_item_id": case_id,
                        "repeat_index": repeat, "retry_index": 0,
                        "status": "completed",
                    })
                    scores.append({
                        "attempt_id": attempt_id, "scorer_id": "exact",
                        "status": "scored", "score": float(value),
                        "passed": value == 1,
                    })
    return {"id": "run-frozen", "attempts": attempts, "scores": scores}, (
        families
    )


def oracle_run() -> tuple[dict, dict[str, list[str]]]:
    return run_from_slots({
        family: {
            case_id: [tuple(pair) for pair in slots]
            for case_id, slots in entry["slots"].items()
        }
        for family, entry in ORACLE["cases"].items()
    })


def comparison(**overrides) -> dict:
    entry = {
        "comparison_id": "primary", "baseline_arm": "left",
        "candidate_arm": "right", "non_inferiority_margin": 0.1,
    }
    entry.update(overrides)
    return entry


def spec_for(families, *, resample_count=6, **overrides) -> dict:
    arguments = {
        "families": families,
        "scorer_id": "exact",
        "master_seed": 7,
        "comparison_family": {
            "family_id": "primary", "comparisons": [comparison()],
        },
        "resample_count": resample_count,
    }
    arguments.update(overrides)
    return freeze_specification(**arguments)


def oracle_spec(**overrides) -> tuple[dict, dict, dict]:
    run, families = oracle_run()
    spec = spec_for(
        families,
        family_weights={"algebra": 2, "geometry": 1},
        case_weights={
            case_id: weight
            for entry in ORACLE["cases"].values()
            for case_id, weight in entry["weights"].items()
        },
        **overrides,
    )
    frozen_input = freeze_input(run, spec, planned_repetitions=2)
    return run, spec, frozen_input


def _fraction(text: str) -> float:
    return float(Fraction(text))


# ── Freezing before calculation ──────────────────────────────────────


def test_specification_freezes_before_any_data():
    families = {"algebra": ["a", "b"], "geometry": ["c"]}
    spec = spec_for(families, family_weights={"algebra": 3, "geometry": 1})
    assert spec["family_weights"] == {"algebra": 0.75, "geometry": 0.25}
    assert spec["case_weights"]["algebra"] == {"a": 0.5, "b": 0.5}
    assert spec["cluster_order"] == ["algebra", "geometry"]
    assert spec["resampling"]["algorithm"] == "bmas-analysis-rng"
    assert spec["resampling"]["algorithm_version"] == 1
    assert spec["reduction"]["order"] == "slots_before_weights"
    assert spec["comparison_family"]["multiplicity"] == "holm"
    assert len(spec["specification_digest"]) == 64
    # Equal declarations freeze to the same digest, whatever the data.
    assert spec["specification_digest"] == spec_for(
        families, family_weights={"algebra": 3, "geometry": 1},
    )["specification_digest"]


def test_zero_sum_weight_vectors_reject_before_admission():
    with pytest.raises(FrozenAnalysisError, match="family weight vector"):
        spec_for({"a": ["x"], "b": ["y"]}, family_weights={"a": 0, "b": 0})
    with pytest.raises(FrozenAnalysisError, match="included family a"):
        spec_for({"a": ["x", "y"]}, case_weights={"x": 0, "y": 0})
    with pytest.raises(FrozenAnalysisError, match="negative"):
        spec_for({"a": ["x"]}, family_weights={"a": -1})


def test_predeclared_margin_and_reduction_are_required():
    with pytest.raises(FrozenAnalysisError, match="margin"):
        spec_for({"a": ["x"]}, comparison_family={
            "family_id": "primary",
            "comparisons": [comparison(non_inferiority_margin=None)],
        })
    with pytest.raises(FrozenAnalysisError, match="binary reduction"):
        spec_for({"a": ["x"]}, binary_reduction="vibes")
    with pytest.raises(FrozenAnalysisError, match="at_least_k"):
        spec_for({"a": ["x"]}, binary_reduction="at_least_k")
    with pytest.raises(FrozenAnalysisError, match="at least one comparison"):
        spec_for({"a": ["x"]}, comparison_family={"family_id": "primary",
                                                  "comparisons": []})


# ── Slot reduction, pairing, and missingness ─────────────────────────


def test_frozen_input_classifies_every_planned_slot():
    run, families = run_from_slots({
        "f": {"c1": [(1, 1), (None, 0)], "c2": [(0, 1)]},
    })
    spec = spec_for(families)
    frozen = freeze_input(run, spec, planned_repetitions=2)
    left = frozen["slots"]["left"]
    assert left["c1"]["1"]["state"] == "observed"
    assert left["c1"]["2"]["state"] == "infrastructure_missing"
    assert left["c2"]["2"]["state"] == "unplanned_missing"
    assert frozen["counts"]["left"] == {
        "planned": 4, "admitted": 3, "failed": 0, "retried": 0,
        "missing": 2, "excluded": 1, "observed": 2,
    }
    assert len(frozen["input_digest"]) == 64


def test_substantive_failure_counts_zero_and_infra_stays_missing():
    run, families = run_from_slots({"f": {"c1": [(1, 1)]}})
    run["attempts"].append({
        "id": "left-c1-2", "arm_id": "left", "dataset_item_id": "c1",
        "repeat_index": 2, "retry_index": 0, "status": "failed",
        "failure_category": "timeout",
    })
    run["attempts"].append({
        "id": "right-c1-2", "arm_id": "right", "dataset_item_id": "c1",
        "repeat_index": 2, "retry_index": 0, "status": "completed",
    })
    run["scores"].append({
        "attempt_id": "right-c1-2", "scorer_id": "exact",
        "status": "scored", "score": 1.0, "passed": True,
    })
    spec = spec_for(families)
    frozen = freeze_input(run, spec, planned_repetitions=2)
    assert frozen["slots"]["left"]["c1"]["2"] == {
        "value": 0.0, "passed": False, "state": "failed_zero",
        "retry_attempts": 0,
    }


def test_paired_slot_removal_and_case_reduction():
    _, spec, frozen = oracle_spec()
    paired = pair_cases(spec, frozen, baseline_arm="left",
                        candidate_arm="right")
    algebra = paired["families"]["algebra"]
    # a3 loses its second slot on both arms; one usable slot remains.
    assert len(algebra["usable"]["a3"]["slots"]) == 1
    assert algebra["removed_slots"] == 1
    assert paired["missing_cases"] == 0
    for family, expected in ORACLE["reduced_case_deltas"].items():
        for case_id, entry in expected.items():
            case = paired["families"][family]["usable"][case_id]
            assert case["delta"] == pytest.approx(_fraction(entry["delta"]))
            assert len(case["slots"]) == entry["usable_slots"]


def test_missing_cases_renormalize_weights_and_report_removed_weight():
    run, families = run_from_slots({
        "f": {
            "c1": [(1, 1)], "c2": [(0, 1)], "c3": [(None, 1)],
            "c4": [(1, 0)], "c5": [(0, 0)],
        },
    })
    spec = spec_for(families)
    frozen = freeze_input(run, spec, planned_repetitions=1)
    paired = pair_cases(spec, frozen, baseline_arm="left",
                        candidate_arm="right")
    family = paired["families"]["f"]
    assert family["missing_case_ids"] == ["c3"]
    assert family["removed_weight"] == pytest.approx(0.2)
    assert family["renormalized_weights"] == {
        "c1": 0.25, "c2": 0.25, "c4": 0.25, "c5": 0.25,
    }
    # The removed weight is exactly the family limit, so the family
    # limit holds, but the total limit of 0.05 fails.
    assert paired["limit_failures"] == ["max_missing_total_weight"]
    assert paired["primary_valid"] is False


def test_missing_weight_inside_the_limits_keeps_the_primary_valid():
    cases = {f"c{index}": [(1, 1)] for index in range(40)}
    cases["c0"] = [(None, 1)]
    run, families = run_from_slots({"f": cases})
    spec = spec_for(families)
    frozen = freeze_input(run, spec, planned_repetitions=1)
    paired = pair_cases(spec, frozen, baseline_arm="left",
                        candidate_arm="right")
    assert paired["total_missing_weight"] == pytest.approx(0.025)
    assert paired["primary_valid"] is True


def test_binary_reductions_follow_the_frozen_rule():
    run, families = run_from_slots({
        "f": {"c1": [(1, 1), (0, 1), (0, 0)]},
    })
    for reduction, extra, expected in (
        ("strict_majority", {}, (False, True)),
        ("all", {}, (False, False)),
        ("at_least_k", {"at_least_k": 1}, (True, True)),
    ):
        spec = spec_for(families, binary_reduction=reduction, **extra)
        frozen = freeze_input(run, spec, planned_repetitions=3)
        paired = pair_cases(spec, frozen, baseline_arm="left",
                            candidate_arm="right")
        case = paired["families"]["f"]["usable"]["c1"]
        assert (case["baseline_binary"], case["candidate_binary"]) == (
            expected
        )
        assert case["delta"] == pytest.approx(1 / 3)


# ── The weighted cluster bootstrap oracle ────────────────────────────


def test_point_estimate_matches_the_oracle():
    _, spec, frozen = oracle_spec()
    paired = pair_cases(spec, frozen, baseline_arm="left",
                        candidate_arm="right")
    estimate = weighted_estimate(paired)
    assert estimate["estimate"] == pytest.approx(
        _fraction(ORACLE["point_estimate"]),
    )
    for family, expected in ORACLE["point_family_aggregates"].items():
        assert estimate["family_aggregates"][family] == pytest.approx(
            _fraction(expected),
        )


def test_every_replicate_draw_and_aggregate_matches_the_oracle():
    _, spec, frozen = oracle_spec()
    paired = pair_cases(spec, frozen, baseline_arm="left",
                        candidate_arm="right")
    result = bootstrap(
        spec, paired, FIXTURES["input_digest"], record_draws=True,
    )
    assert result["algorithm"] == "bmas-analysis-rng"
    assert len(result["replicates"]) == ORACLE["replicates"]
    for record, expected in zip(
        result["replicates"], ORACLE["replicate_records"], strict=True,
    ):
        assert record["draws"] == expected["draws"]
        for family, aggregate in expected["family_aggregates"].items():
            assert record["family_aggregates"][family] == pytest.approx(
                _fraction(aggregate),
            )
        assert record["combined"] == pytest.approx(
            _fraction(expected["combined"]),
        )


def test_case_weights_apply_once_and_never_as_draw_probabilities():
    cases = {f"c{index}": [(0, 1 if index % 2 else 0)] for index in range(8)}
    run, families = run_from_slots({"f": cases})
    heavy = {f"c{index}": 1 for index in range(8)}
    heavy["c0"] = 50
    spec = spec_for(families, case_weights=heavy, resample_count=400)
    frozen = freeze_input(run, spec, planned_repetitions=1)
    paired = pair_cases(spec, frozen, baseline_arm="left",
                        candidate_arm="right")
    result = bootstrap(spec, paired, frozen["input_digest"],
                       record_draws=True)
    drawn = [
        case_id
        for record in result["replicates"]
        for case_id in record["draws"]["f"]
    ]
    frequency = drawn.count("c0") / len(drawn)
    # Uniform draws: the heavy case appears near one eighth of the
    # time, never near its fifty-out-of-fifty-seven weight share.
    assert abs(frequency - 1 / 8) < 0.05
    estimate = weighted_estimate(paired)["estimate"]
    manual = sum(
        paired["families"]["f"]["renormalized_weights"][case_id]
        * paired["families"]["f"]["usable"][case_id]["delta"]
        for case_id in paired["families"]["f"]["usable"]
    )
    assert estimate == pytest.approx(manual)


def test_small_family_marks_the_comparison_insufficient():
    run, families = run_from_slots({
        "f": {f"c{index}": [(0, 1)] for index in range(4)},
    })
    spec = spec_for(families)
    frozen = freeze_input(run, spec, planned_repetitions=1)
    report = compute_report(spec, frozen)
    result = report["comparisons"][0]
    assert result["interval"]["status"] == "insufficient"
    assert result["small_families"] == ["f"]
    # Four paired cases still allow the exact paired sign-flip test.
    assert result["test"]["mode"] == "exact_enumeration"
    assert result["gate"]["status"] == "indeterminate"
    assert "insufficient_family_cluster" in result["gate"]["reasons"]


# ── Gates, multiplicity, and cross-host determinism ──────────────────


def _gate_run(candidate_success: int) -> tuple[dict, dict[str, list[str]]]:
    cases = {}
    for index in range(10):
        left = 1 if index < 6 else 0
        right = 1 if index < candidate_success else 0
        cases[f"c{index}"] = [(left, right)]
    return run_from_slots({"f": cases})


def test_non_inferiority_gate_uses_the_predeclared_margin():
    run, families = _gate_run(6)
    spec = spec_for(families, resample_count=199)
    frozen = freeze_input(run, spec, planned_repetitions=1)
    report = compute_report(spec, frozen)
    result = report["comparisons"][0]
    assert result["estimate"] == pytest.approx(0.0)
    assert result["gate"]["status"] == "passed"
    assert result["gate"]["margin"] == 0.1
    assert result["multiplicity_family"] == "primary"

    regressed, families = _gate_run(1)
    frozen = freeze_input(regressed, spec, planned_repetitions=1)
    result = compute_report(spec, frozen)["comparisons"][0]
    assert result["estimate"] == pytest.approx(-0.5)
    assert result["gate"]["status"] == "failed"


def test_gate_stays_indeterminate_below_the_predeclared_sample_size():
    run, families = _gate_run(6)
    spec = spec_for(families, comparison_family={
        "family_id": "primary",
        "comparisons": [comparison(minimum_usable_cases=20)],
    }, resample_count=99)
    frozen = freeze_input(run, spec, planned_repetitions=1)
    result = compute_report(spec, frozen)["comparisons"][0]
    assert result["gate"]["status"] == "indeterminate"
    assert "below_predeclared_sample_size" in result["gate"]["reasons"]


def test_missingness_limit_makes_the_gate_indeterminate():
    run, families = _gate_run(6)
    run["attempts"] = [
        attempt for attempt in run["attempts"]
        if attempt["id"] != "left-c0-1"
    ]
    spec = spec_for(families, resample_count=99)
    frozen = freeze_input(run, spec, planned_repetitions=1)
    result = compute_report(spec, frozen)["comparisons"][0]
    assert result["limit_failures"] == ["max_missing_total_weight"]
    assert result["gate"]["status"] == "indeterminate"
    assert result["gate"]["reasons"][0] == "missingness_limit_exceeded"


def test_superiority_gate_needs_holm_significance_and_interval():
    cases = {f"c{index}": [(0, 1)] for index in range(14)}
    run, families = run_from_slots({"f": cases})
    spec = spec_for(families, resample_count=199, comparison_family={
        "family_id": "primary",
        "comparisons": [
            comparison(comparison_id="superior", hypothesis="superiority",
                       non_inferiority_margin=None),
            comparison(comparison_id="reverse", hypothesis="superiority",
                       non_inferiority_margin=None,
                       baseline_arm="right", candidate_arm="left"),
        ],
    })
    frozen = freeze_input(run, spec, planned_repetitions=1)
    report = compute_report(spec, frozen)
    superior, reverse = report["comparisons"]
    assert superior["test"]["mode"] == "monte_carlo"
    assert superior["p_value_adjusted"] is not None
    assert superior["gate"]["status"] == "passed"
    assert reverse["gate"]["status"] == "failed"
    assert superior["p_value_adjusted"] >= superior["test"]["p_value"]


def test_holm_adjustment_inside_one_family():
    assert holm_adjust([0.01, 0.04, 0.03]) == pytest.approx(
        [0.03, 0.06, 0.06],
    )
    assert holm_adjust([None, 0.2]) == [None, 0.2]


def test_report_digest_is_locale_and_time_zone_independent(monkeypatch):
    _, spec, frozen = oracle_spec()
    baseline = compute_report(spec, frozen)["results_digest"]
    original = locale.setlocale(locale.LC_ALL)
    try:
        for name in ("C", "de_DE.UTF-8", "en_US.UTF-8"):
            try:
                locale.setlocale(locale.LC_ALL, name)
            except locale.Error:
                continue
            monkeypatch.setenv("TZ", "Asia/Kolkata")
            time.tzset()
            assert compute_report(spec, frozen)["results_digest"] == baseline
    finally:
        locale.setlocale(locale.LC_ALL, original)
        os.environ.pop("TZ", None)
        time.tzset()


def test_gate_decision_never_passes_without_an_interval():
    decision = gate_decision({
        "primary_valid": True,
        "counts": {"paired_cases": 10},
        "minimum_usable_cases": 5,
        "small_families": [],
        "interval": {"status": "no_data", "low": None, "high": None},
        "direction": "higher_is_better",
        "hypothesis": "non_inferiority",
        "non_inferiority_margin": 0.1,
    }, alpha=0.05)
    assert decision["status"] == "indeterminate"
    assert decision["reasons"] == ["no_comparative_interval"]


# ── Snapshots, replay, and provenance ────────────────────────────────


def test_execution_provenance_completes_only_with_every_field():
    bundle = {
        "attempt_id": "attempt-a",
        "run_manifest_digest": "a" * 64,
        "runtime_specification_digest": "a" * 64,
        "trace_digest": "a" * 64,
        "final_output_digest": "a" * 64,
        "seed_evidence": {"requested_seed": 1, "seed_control": "applied",
                          "applied_seed": 1},
        "versions": {"runtime": "classic/1"},
        "ledger_references": {"reservation_id": "r"},
        "resources": {"cost": {"currency": "USD", "amount_nanos": 1}},
    }
    complete = execution_provenance([bundle])
    assert complete["execution_provenance_complete"] is True
    assert complete["execution_seed_requested"] is True
    assert complete["execution_seed_confirmed"] is True
    partial = execution_provenance([{**bundle, "trace_digest": None}])
    assert partial["execution_provenance_complete"] is False
    assert partial["missing_provenance_fields"] == ["attempt-a:trace_digest"]
    assert execution_provenance([])["execution_provenance_complete"] is False


@pytest_asyncio.fixture
async def snapshot_db(tmp_path, monkeypatch):
    path = str(tmp_path / "snapshot.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    return await make_attempts(2)


def _stored_spec() -> dict:
    return spec_for(
        {"math": ["item-0", "item-1"]},
        resample_count=9,
        comparison_family={
            "family_id": "primary",
            "comparisons": [comparison(
                baseline_arm="arm-evidence", candidate_arm="arm-evidence",
            )],
        },
    )


@pytest.mark.asyncio
async def test_snapshot_pins_every_digest_and_replays(snapshot_db):
    from benchmarks import evaluation_records, evidence_capture, repository

    spec = _stored_spec()
    stored = await fa.freeze_and_store(
        "run-evidence", specification=spec, planned_repetitions=1,
    )
    record = stored["record"]
    for field in ("source_digest", "build_digest",
                  "dependency_lock_digest", "runtime_digest"):
        assert len(record["engine"][field]) == 64, field
    assert record["engine"]["toolchain_versions"]["python"]
    random_source = record["random_source"]
    assert random_source["algorithm"] == "bmas-analysis-rng"
    assert random_source["algorithm_version"] == 1
    assert random_source["master_seed"] == 7
    assert random_source["derivation_schedule"]
    assert record["io_checksums"]["input"] == (
        stored["frozen_input"]["input_digest"]
    )
    assert record["replay"]["claim"] == "analysis_replayable"
    # No evidence bundle exists yet, so provenance is incomplete.
    assert record["replay"]["execution_provenance_complete"] is False
    persisted = await evaluation_records.get_record(
        "analysis-snapshot", stored["snapshot_id"],
    )
    assert persisted["run_id"] == "run-evidence"

    # A fresh replay from stored evidence reproduces equal checksums.
    run = await repository.get_run("run-evidence")
    replayed = replay(spec, run, record, planned_repetitions=1)
    assert replayed["analysis_replayable"] is True
    assert replayed["results_digest"] == record["results_digest"]
    assert "never proves" in replayed["execution_claim"]

    for attempt_id in snapshot_db:
        await evidence_capture.capture_attempt_evidence(
            attempt_id=attempt_id,
            run_manifest={"run_id": "run-evidence"},
            runtime_specification={"runtime": "classic"},
            case={"case_id": "case-0"},
            trace_events=[{"kind": "action"}],
            final_output="42",
            resources={"cost": {"currency": "USD", "amount_nanos": 5},
                       "tokens": 1, "latency_ms": 1},
            seed_evidence={"requested_seed": 1, "seed_control": "recorded",
                           "applied_seed": None},
            ledger_references={"reservation_id": "reservation-a"},
        )
    second = await fa.freeze_and_store(
        "run-evidence", specification=spec, planned_repetitions=1,
    )
    assert second["record"]["replay"]["execution_provenance_complete"] is True
    assert second["record"]["replay"]["execution_seed_requested"] is True
    assert second["record"]["replay"]["execution_seed_confirmed"] is False
    assert second["record"]["evidence_checksum"] != record["evidence_checksum"]


@pytest.mark.asyncio
async def test_freeze_rejects_a_missing_run(snapshot_db):
    with pytest.raises(FrozenAnalysisError, match="does not exist"):
        await fa.freeze_and_store(
            "run-missing", specification=_stored_spec(),
            planned_repetitions=1,
        )


# ── Evaluation study validation ──────────────────────────────────────


def test_study_validation_blocks_on_each_failed_condition():
    plan = valid_run_plan()
    plan["estimand"]["direction"] = "higher_is_better"
    _, spec, frozen = oracle_spec()
    report = compute_report(spec, frozen)
    ready = validate_study(
        run_plan=plan,
        source={"pinned_revision": "abc", "license": {"name": "MIT"}},
        holdout_hidden=True,
        report=report,
        cost_includes_retries_and_control_plane=True,
    )
    assert ready["ready"] is True
    assert ready["blocking"] == []
    blocked = validate_study(
        run_plan={**plan, "arm_order": {"strategy": "sequential"}},
        source={"pinned_revision": "", "license": {}},
        holdout_hidden=False,
        report=None,
        cost_includes_retries_and_control_plane=False,
    )
    assert blocked["ready"] is False
    assert set(blocked["blocking"]) >= {
        "source_pinned", "holdout_hidden", "interleaved_arms",
        "report_shows_failures_and_missingness",
        "cost_includes_retries_and_control_plane",
    }
