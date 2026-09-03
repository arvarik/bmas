"""Study authoring: frozen treatments, invariants, estimands, and gates."""

from __future__ import annotations

import pytest

from benchmarks.study_authoring import (
    MAX_GENERATED_ARMS,
    StudyAuthoringError,
    author_study,
    expand_arms,
)
from core.money import Money

BASE = {"model": {"name": "model-a", "temperature": 0.0}, "tools": ["calc"]}
INVARIANTS = {
    "dataset_version_id": "version-alpha",
    "case_ids": ["c0", "c1"],
    "seed_schedule": {"base_seed": 7, "scope": "item-repetition"},
    "scorers": ["exact"],
    "arm_order": "rotated_interleave",
    "repetitions": 2,
}


def _author(**overrides):
    arguments = {
        "study_type": "one_factor_ablation",
        "name": "temperature",
        "base_configuration": BASE,
        "treatment": {"path": "model.temperature", "values": [0.0, 0.7]},
        "invariants": INVARIANTS,
        "families": {"math": [f"c{i}" for i in range(6)]},
        "scorer_id": "exact",
        "master_seed": 7,
        "comparison_margin": 0.05,
        "per_attempt_cost": Money("USD", 5_000_000),
        "seconds_per_attempt": 30,
        "max_concurrency": 4,
        "resample_count": 9,
    }
    arguments.update(overrides)
    return author_study(**arguments)


def test_every_study_type_expands_bounded_immutable_arms():
    ablation = expand_arms("one_factor_ablation", base_configuration=BASE,
                           treatment={"path": "model.temperature",
                                      "values": [0.0, 0.5]})
    assert [arm["slug"] for arm in ablation] == [
        "temperature-0.0", "temperature-0.5",
    ]
    grid = expand_arms("parameter_grid", base_configuration=BASE,
                       treatment={"factors": {"model.temperature": [0, 1],
                                              "model.name": ["a", "b"]}})
    assert len(grid) == 4
    for study_type, path in (
        ("preset_comparison", "preset"),
        ("runtime_comparison", "runtime_id"),
        ("model_family_comparison", "model.name"),
    ):
        arms = expand_arms(study_type, base_configuration=BASE,
                           treatment={"path": path, "values": ["x", "y"]})
        assert len(arms) == 2
    with pytest.raises(StudyAuthoringError, match="limit"):
        expand_arms("parameter_grid", base_configuration=BASE,
                    treatment={"factors": {"a": list(range(4)),
                                           "b": list(range(4))}})
    assert MAX_GENERATED_ARMS == 12
    with pytest.raises(StudyAuthoringError, match="Unknown study type"):
        expand_arms("vibes", base_configuration=BASE, treatment={})


def test_study_freezes_treatments_invariants_estimand_and_gates():
    study = _author()
    assert [arm["slug"] for arm in study["arms"]] == [
        "temperature-0.0", "temperature-0.7",
    ]
    assert study["invariants"]["dataset_version_id"] == "version-alpha"
    assert study["estimand"]["primary_estimand"] == (
        "family-balanced-unconditional-task-success"
    )
    gate = study["gates"]["comparison_family"]["comparisons"][0]
    assert gate["non_inferiority_margin"] == 0.05
    assert gate["baseline_arm"] == "temperature-0.0"
    assert study["gates"]["predeclared"] is True
    assert len(study["study_digest"]) == 64
    assert study["treatment_paths"] == ["model.temperature"]


def test_estimates_show_before_publication():
    study = _author()
    estimates = study["estimates"]
    # 6 cases x 2 repetitions x 2 arms = 24 attempts.
    assert study["sample_plan"]["attempts"] == 24
    assert estimates["cost"] == {"currency": "USD",
                                 "amount_nanos": 120_000_000}
    assert estimates["duration_seconds"] == 180
    assert estimates["pricing_basis"] == "per_attempt_reservation"


def test_missing_invariants_and_changed_invariants_reject():
    with pytest.raises(StudyAuthoringError, match="invariant repetitions"):
        _author(invariants={k: v for k, v in INVARIANTS.items()
                            if k != "repetitions"})
    with pytest.raises(StudyAuthoringError, match="one path"):
        _author(treatment={"path": "", "values": []})
