"""Evaluation record contracts: schemas, metadata, and canonical money.

Every documented evaluation record validates against its published
JSON Schema. The contract generation travels in record metadata, an
unknown field rejects at the controlled boundary, and every monetary
field uses the canonical ``Money(currency, amount_nanos)`` shape: a
minor-unit integer, an authoritative decimal string, or a binary
floating-point amount fails validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.evaluation_contracts import (
    EVALUATION_CONTRACT_GENERATION,
    RECORD_SCHEMAS,
    EvaluationContractError,
    validate_record,
)

DIGEST = "a" * 64
WHEN = "2026-09-01T00:00:00Z"


def _envelope(schema_id: str) -> dict:
    return {
        "schema_id": schema_id,
        "schema_version": EVALUATION_CONTRACT_GENERATION,
    }


def valid_benchmark_source() -> dict:
    return {
        **_envelope("benchmark-source"),
        "source_id": "source-gsm8k",
        "source_type": "hugging_face",
        "locator": "hf://datasets/openai/gsm8k",
        "pinned_revision": "e53f048856ff4f594e959d75785d2c2d37b678ee",
        "content_checksum": DIGEST,
        "license": {"name": "MIT", "citation": "Cobbe et al."},
        "adapter": {"id": "adapter-hugging-face", "version": "1"},
        "fetched_at": WHEN,
        "imported_by": "operator-a",
        "configuration": {
            "selected_configuration": "main",
            "selected_splits": ["test"],
        },
        "documentation_digest": DIGEST,
        "trust": {"level": "public_untrusted", "policy_version": "1"},
        "execution_restrictions": [
            {"name": "deny_network", "behavior": "hard"},
            {"name": "deny_secrets", "behavior": "hard"},
        ],
    }


def valid_evaluation_case() -> dict:
    return {
        **_envelope("evaluation-case"),
        "case_id": "example-001",
        "task": {"instructions": "Add 20 and 22.", "messages": [],
                 "assets": []},
        "expected": {"reference_answer": "42", "final_state": None,
                     "rubric_id": None},
        "environment": None,
        "tools": [],
        "limits": {"max_tokens": 2048},
        "classification": {
            "task_family": "arithmetic",
            "split": "test",
            "tags": ["smoke"],
            "intrinsic_horizon": None,
            "human_minutes": 1.5,
        },
        "contamination": {},
        "metadata": {},
    }


def valid_dataset_draft() -> dict:
    return {
        **_envelope("dataset-draft"),
        "draft_id": "draft-alpha",
        "created_from": {"kind": "source_import",
                         "reference": "source-gsm8k"},
        "source_ids": ["source-gsm8k"],
        "parent_version_id": None,
        "trust_inputs": [
            {"level": "public_untrusted", "policy_version": "1"},
        ],
        "asset_policy": {},
        "effective_restrictions": [
            {"name": "deny_network", "behavior": "hard"},
        ],
        "validation_issues": [],
        "metadata": {},
    }


def valid_dataset_version() -> dict:
    return {
        **_envelope("dataset-version"),
        "version_id": "version-alpha",
        "parent_version_id": None,
        "canonical_schema_version": "2",
        "source_lineage": ["source-gsm8k"],
        "trust_inputs": [
            {"level": "public_untrusted", "policy_version": "1"},
        ],
        "effective_restrictions": [
            {"name": "deny_network", "behavior": "hard"},
        ],
        "policy_digest": DIGEST,
        "case_manifest_digest": DIGEST,
        "transformation_recipe_digest": DIGEST,
        "split_manifest": {"test": ["example-001"]},
        "asset_digests": [],
        "content_digest": DIGEST,
        "validation_report_digest": DIGEST,
        "contamination_record_digest": DIGEST,
        "attribution_bundle_digest": DIGEST,
    }


def valid_scorer_spec() -> dict:
    return {
        **_envelope("scorer-spec"),
        "scorer_id": "scorer-exact-match",
        "version": "2",
        "implementation_digest": DIGEST,
        "description": "Compare the answer with the reference.",
        "input_evidence_contract": ["final_output"],
        "configuration_schema": {"type": "object"},
        "output_dimensions": [
            {"name": "accuracy", "scale": "unit_interval",
             "direction": "higher_is_better"},
        ],
        "scale": "unit_interval",
        "direction": "higher_is_better",
        "determinism": "deterministic",
        "required_evidence": ["final_output"],
        "trust_class": "built_in",
        "sandbox": {"policy_version": "1", "policy_digest": DIGEST},
        "execution_digests": {
            "artifact": DIGEST,
            "runtime": DIGEST,
            "dependencies": DIGEST,
        },
    }


def valid_run_plan() -> dict:
    return {
        **_envelope("run-plan"),
        "plan_id": "plan-alpha",
        "digests": {
            "dataset": DIGEST,
            "runtime": DIGEST,
            "scorers": DIGEST,
            "schema": DIGEST,
        },
        "outcome_mapping_set_digest": DIGEST,
        "arm_mappings": [{
            "runtime_id": "classic",
            "runtime_contract_version": "1",
            "mapping_id": "outcome-mapping-classic-1",
            "mapping_digest": DIGEST,
        }],
        "case_ids": ["example-001"],
        "seed_schedule": {"base_seed": 7, "scope": "item-repetition"},
        "arm_order": {"strategy": "rotated_interleave"},
        "repetitions": 2,
        "limits": {
            "max_concurrency": 4,
            "timeout_seconds": 600,
            "run_cost": {"currency": "USD",
                         "amount_nanos": 4_000_000_000},
        },
        "retry_rules": {
            "infrastructure_exclusions": ["infrastructure"],
            "reason": "provider outage window",
        },
        "unit_hierarchy": ["family", "case", "repetition"],
        "estimand": {"primary_estimand":
                     "paired-difference-in-weighted-case-means"},
        "trust_policy": {
            "trust": {"level": "public_untrusted", "policy_version": "1"},
            "capabilities": [],
        },
        "resource_estimate": {
            "expected_cost": {"currency": "USD",
                              "amount_nanos": 2_000_000_000},
            "pricing_version": "pricing-2026-09",
        },
        "settlement": {"policy": "strict", "maximum_wait_seconds": 3600},
        "dispatch": {"fairness_policy": "weighted_round_robin",
                     "starvation_limit": 25},
    }


def valid_attempt_evidence() -> dict:
    return {
        **_envelope("attempt-evidence"),
        "attempt_id": "attempt-alpha",
        "run_manifest_digest": DIGEST,
        "runtime_specification_digest": DIGEST,
        "case_reference": {"case_id": "example-001", "asset_ids": []},
        "trace_digest": DIGEST,
        "final_output_digest": DIGEST,
        "resources": {
            "cost": {"currency": "USD", "amount_nanos": 250_000_000},
            "tokens": 1200,
            "latency_ms": 4200,
        },
        "completeness": {"level": "complete",
                         "unavailable_sections": []},
        "seed_evidence": {
            "requested_seed": 7001,
            "seed_control": "recorded",
            "applied_seed": None,
        },
        "failure_classification": None,
        "versions": {"runtime": "classic/1"},
        "ledger_references": {
            "admission_effect_id": "effect-a",
            "reservation_id": "reservation-a",
        },
    }


def valid_score_record() -> dict:
    return {
        **_envelope("score-record"),
        "score_id": "score-alpha",
        "scorer": {
            "scorer_id": "scorer-exact-match",
            "version": "2",
            "configuration_digest": DIGEST,
        },
        "evidence_references": ["attempt-alpha"],
        "dimensions": [
            {"name": "accuracy", "value": 1.0, "category": None},
        ],
        "passed": True,
        "explanation": "exact_match",
        "uncertainty": None,
        "status": "scored",
        "error": None,
    }


def valid_analysis_snapshot() -> dict:
    return {
        **_envelope("analysis-snapshot"),
        "snapshot_id": "snapshot-alpha",
        "run_checksum": DIGEST,
        "evidence_checksum": DIGEST,
        "filters": {},
        "missingness_policy": "predeclared_infrastructure_exclusions",
        "estimand": {"primary_estimand":
                     "paired-difference-in-weighted-case-means"},
        "unit_hierarchy": ["family", "case", "repetition"],
        "resampling": {
            "cluster_order": ["algebra", "geometry"],
            "small_cluster_policy": "flagged_not_dropped",
            "resample_count": 999,
        },
        "methods": {
            "estimator": "family_stratified_weighted_mean",
            "interval_method":
                "family_stratified_weighted_case_bootstrap",
            "confidence_level": 0.95,
        },
        "multiplicity_groups": ["comparison.left.right"],
        "results_digest": DIGEST,
        "report_checksum": DIGEST,
        "engine": {
            "source_digest": DIGEST,
            "build_digest": DIGEST,
            "dependency_lock_digest": DIGEST,
            "toolchain_versions": {"python": "3.13"},
        },
        "random_source": {
            "algorithm": "bmas-analysis-rng",
            "implementation_digest": DIGEST,
            "master_seed": 7,
            "derivation_schedule": ["bootstrap", "sign-flip"],
        },
        "io_checksums": {"input": DIGEST, "output": DIGEST},
    }


def valid_gate_evaluation() -> dict:
    return {
        **_envelope("gate-evaluation"),
        "evaluation_id": "gate-alpha",
        "baseline_analysis_checksum": DIGEST,
        "candidate_analysis_checksum": DIGEST,
        "invariant_digest": DIGEST,
        "treatment": {
            "declaration": [{
                "field_path": "arms.classic.model",
                "baseline_digest": DIGEST,
                "candidate_digest": DIGEST,
                "reason": "The candidate changes the model treatment.",
            }],
            "digest": DIGEST,
        },
        "rules": [{
            "id": "primary-floor",
            "metric": "arm.classic.score.exact",
            "operator": "gte",
            "value": 0.8,
            "direction": "improvement",
            "practical_size": 0.01,
        }],
        "display_exceptions": [],
    }


def valid_interaction_spec() -> dict:
    return {
        **_envelope("interaction-spec"),
        "spec_id": "interaction-alpha",
        "protocol": "turn-based",
        "participants": [
            {"role": "agent", "channel": "primary"},
            {"role": "simulated-user", "channel": "primary"},
        ],
        "simulator": {
            "implementation_id": "simulator-standard",
            "prompt_digest": DIGEST,
            "model": "claude-sonnet-5",
            "image_digest": DIGEST,
            "dependency_digest": DIGEST,
        },
        "initial_messages": [{"role": "user", "content": "Hello"}],
        "observation_rules": [],
        "limits": {"max_turns": 10,
                   "max_cost": {"currency": "USD",
                                "amount_nanos": 1_000_000_000}},
        "allowed": {"tools": [], "capabilities": [],
                    "environment_operations": []},
        "stop_conditions": ["goal_reached", "turn_limit"],
        "invalid_transition_behavior": "fail_attempt",
        "secrets": {"classes": [], "canary_references": []},
        "trajectory_assertions": [{
            "verifier_id": "verifier-no-secret-leak",
            "event_selector": "assistant_messages",
            "quantifier": "none",
            "predicate": "contains_canary",
            "severity": "blocking",
            "missing_evidence_result": "unknown",
        }],
        "recovery_rules": {"retry": "infrastructure_only",
                           "timeout": "fail_attempt",
                           "missing_turn": "fail_attempt"},
    }


def valid_contamination_record() -> dict:
    return {
        **_envelope("contamination-rights-record"),
        "record_id": "contamination-alpha",
        "dataset_version_id": "version-alpha",
        "split_rules": {"development": "none", "validation": "none",
                        "hidden_test": "all"},
        "screening": {
            "implementation": "exact-and-overlap",
            "corpus": "public-pretraining-index",
            "thresholds": {"overlap": 0.8},
            "version": "1",
            "result": "screened",
        },
        "matches": [],
        "holdout_access": [{"actor": "operator-a",
                            "accessed_at": WHEN}],
        "canaries": {"identifiers": ["canary-a"],
                     "rotation_history": []},
        "license_decisions": [
            {"subject": "source-gsm8k", "decision": "approved"},
        ],
        "attribution": {"text": "GSM8K by OpenAI.",
                        "links": ["https://example.com"]},
        "use_decisions": {
            "allowed_use": "evaluation_only",
            "redistribution": "denied",
            "modification": "allowed",
            "export": "denied",
        },
    }


def valid_metric_definition() -> dict:
    return {
        **_envelope("metric-definition"),
        "metric_id": "metric-task-success",
        "lifecycle_state": "draft",
        "calibration": {"state": "due", "method": "deterministic",
                        "version": "1"},
        "population": {
            "target": "declared dataset cases",
            "inclusion_rule": "Every planned non-excluded slot counts.",
        },
        "measurement": {
            "numerator": "Cases with a passing binary reduction.",
            "denominator": "Unconditional planned cases.",
            "unit": "proportion",
            "range": {"minimum": 0, "maximum": 1},
            "direction": "higher_is_better",
            "aggregation": "family_stratified_weighted_mean",
        },
        "labels": {"source": "scorer",
                   "evidence_contract": ["final_output"]},
        "scorer": {
            "scorer_id": "scorer-exact-match",
            "version": "2",
            "configuration_digest": DIGEST,
        },
        "missingness": "predeclared_infrastructure_exclusions",
        "exclusions": [],
        "uncertainty_method":
            "family_stratified_weighted_case_bootstrap",
    }


def valid_asset_ingestion() -> dict:
    return {
        **_envelope("asset-ingestion-record"),
        "ingestion_id": "ingestion-alpha",
        "original_name": "images.zip",
        "declared_media_type": "application/zip",
        "detected_media_type": "application/zip",
        "size_bytes": 1024,
        "digest": DIGEST,
        "scanner": {"engine": "clamav", "signature_version": "27000",
                    "result": "clean", "completed_at": WHEN},
        "archive": {"entry_count": 3, "depth": 1,
                    "expanded_bytes": 4096, "compression_ratio": 4.0},
        "state": "quarantined",
    }


VALID_RECORDS = {
    "benchmark-source": valid_benchmark_source,
    "evaluation-case": valid_evaluation_case,
    "dataset-draft": valid_dataset_draft,
    "dataset-version": valid_dataset_version,
    "scorer-spec": valid_scorer_spec,
    "run-plan": valid_run_plan,
    "attempt-evidence": valid_attempt_evidence,
    "score-record": valid_score_record,
    "analysis-snapshot": valid_analysis_snapshot,
    "gate-evaluation": valid_gate_evaluation,
    "interaction-spec": valid_interaction_spec,
    "contamination-rights-record": valid_contamination_record,
    "metric-definition": valid_metric_definition,
    "asset-ingestion-record": valid_asset_ingestion,
}


def test_every_documented_record_has_one_schema_and_fixture():
    assert sorted(VALID_RECORDS) == sorted(RECORD_SCHEMAS)


@pytest.mark.parametrize("schema_id", sorted(RECORD_SCHEMAS))
def test_each_valid_record_validates_with_its_checksum(schema_id):
    record = VALID_RECORDS[schema_id]()
    summary = validate_record(record)
    assert summary["schema_id"] == schema_id
    # The contract generation lives in record metadata, never inside
    # an identifier.
    assert summary["schema_version"] == EVALUATION_CONTRACT_GENERATION
    assert len(summary["record_checksum"]) == 64


@pytest.mark.parametrize("schema_id", sorted(RECORD_SCHEMAS))
def test_an_unknown_controlled_field_rejects(schema_id):
    record = VALID_RECORDS[schema_id]()
    record["surprise_field"] = "unexpected"
    with pytest.raises(EvaluationContractError, match="surprise_field"):
        validate_record(record)


def test_a_wrong_contract_generation_rejects():
    record = valid_evaluation_case()
    record["schema_version"] = 1
    with pytest.raises(EvaluationContractError, match="schema_version"):
        validate_record(record)
    with pytest.raises(EvaluationContractError, match="Unknown"):
        validate_record({"schema_id": "mystery-record"})
    with pytest.raises(EvaluationContractError):
        validate_record("not a record")


def test_a_rubric_only_case_needs_no_reference_answer():
    record = valid_evaluation_case()
    record["expected"] = {"reference_answer": None, "final_state": None,
                          "rubric_id": "rubric-1"}
    validate_record(record)


def test_an_environment_case_can_require_a_final_state():
    record = valid_evaluation_case()
    record["expected"] = {
        "reference_answer": None,
        "final_state": {"files": {"result.txt": DIGEST}},
        "rubric_id": None,
    }
    record["environment"] = {"image_digest": DIGEST}
    validate_record(record)


def test_every_published_source_pins_an_exact_revision():
    record = valid_benchmark_source()
    del record["pinned_revision"]
    with pytest.raises(EvaluationContractError, match="pinned_revision"):
        validate_record(record)


def test_every_scorer_declares_direction_and_scale():
    record = valid_scorer_spec()
    del record["direction"]
    with pytest.raises(EvaluationContractError, match="direction"):
        validate_record(record)
    record = valid_scorer_spec()
    del record["scale"]
    with pytest.raises(EvaluationContractError, match="scale"):
        validate_record(record)


def test_every_run_plan_pins_cases_and_the_mapping_set():
    record = valid_run_plan()
    record["case_ids"] = []
    with pytest.raises(EvaluationContractError, match="case_ids"):
        validate_record(record)
    record = valid_run_plan()
    del record["outcome_mapping_set_digest"]
    with pytest.raises(
        EvaluationContractError, match="outcome_mapping_set_digest",
    ):
        validate_record(record)


def test_every_analysis_names_its_unit_and_estimand():
    record = valid_analysis_snapshot()
    del record["unit_hierarchy"]
    with pytest.raises(EvaluationContractError, match="unit_hierarchy"):
        validate_record(record)
    record = valid_analysis_snapshot()
    del record["estimand"]
    with pytest.raises(EvaluationContractError, match="estimand"):
        validate_record(record)


def test_a_gate_separates_invariant_and_treatment():
    record = valid_gate_evaluation()
    for field in ("invariant_digest", "treatment"):
        broken = valid_gate_evaluation()
        del broken[field]
        with pytest.raises(EvaluationContractError, match=field):
            validate_record(broken)
    # A wildcard treatment path never validates.
    record["treatment"]["declaration"][0]["field_path"] = "arms.*"
    with pytest.raises(EvaluationContractError, match="field_path"):
        validate_record(record)


def test_a_score_record_stores_named_dimensions_only():
    record = valid_score_record()
    record["dimensions"] = []
    with pytest.raises(EvaluationContractError, match="dimensions"):
        validate_record(record)


# ── Canonical money at the controlled boundary ───────────────────────


def test_a_binary_floating_point_amount_rejects():
    record = valid_run_plan()
    record["limits"]["run_cost"]["amount_nanos"] = 2.0
    with pytest.raises(EvaluationContractError, match="exact integer"):
        validate_record(record)
    record = valid_attempt_evidence()
    record["resources"]["cost"]["amount_nanos"] = True
    with pytest.raises(EvaluationContractError, match="integer|valid"):
        validate_record(record)


def test_an_authoritative_decimal_string_rejects():
    record = valid_run_plan()
    record["limits"]["run_cost"]["amount_nanos"] = "2.50"
    with pytest.raises(EvaluationContractError, match="integer"):
        validate_record(record)
    record = valid_run_plan()
    record["estimand"]["budget"] = "2.50"
    with pytest.raises(EvaluationContractError, match="Money"):
        validate_record(record)


def test_a_minor_unit_integer_field_rejects():
    record = valid_run_plan()
    record["estimand"]["amount_cents"] = 250
    with pytest.raises(EvaluationContractError, match="minor-unit"):
        validate_record(record)
    record = valid_run_plan()
    record["estimand"]["total_cost_usd"] = 2.5
    with pytest.raises(EvaluationContractError, match="Money"):
        validate_record(record)


def test_amount_nanos_lives_only_inside_money():
    record = valid_run_plan()
    record["estimand"]["amount_nanos"] = 100
    with pytest.raises(EvaluationContractError, match="canonical Money"):
        validate_record(record)
    # A Money-shaped object with an extra field is not Money.
    record = valid_run_plan()
    record["limits"]["run_cost"] = {
        "currency": "USD",
        "amount_nanos": 100,
        "amount_text": "0.0000001",
    }
    with pytest.raises(EvaluationContractError):
        validate_record(record)


def test_canonical_money_validates_and_bounds():
    record = valid_run_plan()
    record["limits"]["run_cost"] = {"currency": "EUR",
                                    "amount_nanos": 1}
    validate_record(record)
    record["limits"]["run_cost"] = {"currency": "usd",
                                    "amount_nanos": 1}
    with pytest.raises(EvaluationContractError):
        validate_record(record)


# ── Published schema files ───────────────────────────────────────────


def _published_directory() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "docs/reference/evaluation-contracts"
    )


def test_the_published_schemas_match_the_definitions():
    directory = _published_directory()
    published = {
        path.name: path.read_text()
        for path in directory.glob("*.schema.json")
    }
    assert sorted(published) == sorted(
        f"{schema_id}.schema.json" for schema_id in RECORD_SCHEMAS
    )
    for schema_id, schema in RECORD_SCHEMAS.items():
        rendered = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        assert published[f"{schema_id}.schema.json"] == rendered, (
            f"The published {schema_id} schema is stale; run "
            "scripts/generate-evaluation-contract-schemas.py"
        )


def test_every_schema_forbids_unknown_fields_and_references_money():
    for schema_id, schema in RECORD_SCHEMAS.items():
        assert schema["additionalProperties"] is False, schema_id
        assert schema["properties"]["schema_id"] == {
            "const": schema_id,
        }
        assert schema["properties"]["schema_version"] == {
            "const": EVALUATION_CONTRACT_GENERATION,
        }
        # Every schema carries the one canonical Money definition.
        assert schema["$defs"]["money"]["required"] == [
            "currency", "amount_nanos",
        ]
        assert schema["$defs"]["money"]["additionalProperties"] is False
