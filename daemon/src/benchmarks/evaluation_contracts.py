"""Versioned evaluation record contracts and their validation.

One documented JSON Schema exists for every external evaluation
record. The contract generation travels in record metadata: every
record carries its ``schema_id`` and ``schema_version`` fields, and
identifiers stay free of version tokens. A controlled boundary
rejects unknown fields, and every monetary field uses the Foundation
``Money(currency, amount_nanos)`` shape: a minor-unit integer, an
authoritative decimal string, or a binary floating-point amount fails
validation.

The published schema files live under
``docs/reference/evaluation-contracts/``. The generator script writes
them from the definitions in this module, and a test keeps the
published files equal to the definitions.
"""

from __future__ import annotations

import json
import re
from functools import cache
from typing import Any

from benchmarks.provenance import content_checksum
from core.money import MAX_AMOUNT_NANOS, MIN_AMOUNT_NANOS, Money, MoneyError

# The evaluation contract generation. It lives in record metadata and
# in schema constants, never inside an identifier.
EVALUATION_CONTRACT_GENERATION = 2

_JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

_DIGEST = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
_IDENTIFIER = {"type": "string", "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_.:@/-]{0,199}$"}
_NAME = {"type": "string", "minLength": 1, "maxLength": 500}
_TEXT = {"type": "string", "maxLength": 20_000}
_TIMESTAMP = {
    "type": "string",
    "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$",
}
_COUNT = {"type": "integer", "minimum": 0}
_OPEN_MAP = {"type": "object"}
_STRING_LIST = {"type": "array", "items": _NAME}
_IDENTIFIER_LIST = {"type": "array", "items": _IDENTIFIER}

# The one canonical monetary shape. Every monetary configuration and
# result field references this definition.
MONEY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["currency", "amount_nanos"],
    "properties": {
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "amount_nanos": {
            "type": "integer",
            "minimum": MIN_AMOUNT_NANOS,
            "maximum": MAX_AMOUNT_NANOS,
        },
    },
}
_MONEY_REF = {"$ref": "#/$defs/money"}

_RESTRICTION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "behavior"],
    "properties": {
        "name": _NAME,
        "behavior": {"enum": ["hard", "reviewable"]},
    },
}
_RESTRICTION_LIST = {"type": "array", "items": _RESTRICTION}

TRUST_LEVELS = (
    "built_in_verified",
    "publisher_verified",
    "owner_uploaded",
    "public_untrusted",
    "unknown",
)

_TRUST = {
    "type": "object",
    "additionalProperties": False,
    "required": ["level", "policy_version"],
    "properties": {
        "level": {"enum": list(TRUST_LEVELS)},
        "policy_version": _NAME,
    },
}

_SCALE = {"enum": ["unit_interval", "count", "ratio", "categorical"]}
_DIRECTION = {"enum": ["higher_is_better", "lower_is_better", "target"]}

_MAPPING_MEMBER = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "runtime_id",
        "runtime_contract_version",
        "mapping_id",
        "mapping_digest",
    ],
    "properties": {
        "runtime_id": _IDENTIFIER,
        "runtime_contract_version": _NAME,
        "mapping_id": _IDENTIFIER,
        "mapping_digest": _DIGEST,
    },
}


def _record_schema(
    schema_id: str,
    *,
    title: str,
    required: list[str],
    properties: dict[str, Any],
) -> dict[str, Any]:
    """Build one complete record schema with the shared envelope."""
    return {
        "$schema": _JSON_SCHEMA_DIALECT,
        "$id": f"bmas/{schema_id}",
        "title": title,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_id", "schema_version", *required],
        "properties": {
            "schema_id": {"const": schema_id},
            "schema_version": {"const": EVALUATION_CONTRACT_GENERATION},
            **properties,
        },
        "$defs": {"money": MONEY_SCHEMA},
    }


RECORD_SCHEMAS: dict[str, dict[str, Any]] = {
    "benchmark-source": _record_schema(
        "benchmark-source",
        title="The origin record for imported evaluation content",
        required=[
            "source_id",
            "source_type",
            "locator",
            "pinned_revision",
            "content_checksum",
            "license",
            "adapter",
            "fetched_at",
            "imported_by",
            "configuration",
            "documentation_digest",
            "trust",
            "execution_restrictions",
        ],
        properties={
            "source_id": _IDENTIFIER,
            "source_type": {
                "enum": [
                    "local_upload",
                    "hugging_face",
                    "https_file",
                    "built_in_catalog",
                    "git_repository",
                    "test_package",
                ],
            },
            "locator": _NAME,
            # A mutable branch never imports; the locator resolves to
            # one exact revision before import.
            "pinned_revision": _NAME,
            "content_checksum": _DIGEST,
            "license": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": _NAME,
                    "url": _NAME,
                    "citation": _TEXT,
                },
            },
            "adapter": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "version"],
                "properties": {"id": _IDENTIFIER, "version": _NAME},
            },
            "fetched_at": _TIMESTAMP,
            "imported_by": _NAME,
            "configuration": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "selected_configuration": _NAME,
                    "selected_splits": _STRING_LIST,
                },
            },
            "documentation_digest": _DIGEST,
            "trust": _TRUST,
            "execution_restrictions": _RESTRICTION_LIST,
        },
    ),
    "evaluation-case": _record_schema(
        "evaluation-case",
        title="One versioned evaluation case envelope",
        required=["case_id", "task", "expected", "classification"],
        properties={
            "case_id": _IDENTIFIER,
            "task": {
                "type": "object",
                "additionalProperties": False,
                "required": ["instructions"],
                "properties": {
                    "instructions": _TEXT,
                    "messages": {"type": "array", "items": _OPEN_MAP},
                    "assets": _IDENTIFIER_LIST,
                },
            },
            # A rubric or final-state verifier can hold the authority,
            # so no single expected field is required.
            "expected": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "reference_answer": {"type": ["string", "null"]},
                    "final_state": {"type": ["object", "null"]},
                    "rubric_id": {
                        "anyOf": [_IDENTIFIER, {"type": "null"}],
                    },
                },
            },
            "environment": {"type": ["object", "null"]},
            "tools": _IDENTIFIER_LIST,
            "limits": _OPEN_MAP,
            "classification": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_family", "split"],
                "properties": {
                    "task_family": _NAME,
                    "split": _NAME,
                    "tags": _STRING_LIST,
                    "intrinsic_horizon": {
                        "anyOf": [_NAME, {"type": "null"}],
                    },
                    "human_minutes": {
                        "type": ["number", "null"],
                        "minimum": 0,
                    },
                },
            },
            "contamination": _OPEN_MAP,
            "metadata": _OPEN_MAP,
        },
    ),
    "dataset-draft": _record_schema(
        "dataset-draft",
        title="One editable dataset draft with its trust inputs",
        required=[
            "draft_id",
            "created_from",
            "source_ids",
            "trust_inputs",
            "effective_restrictions",
            "validation_issues",
        ],
        properties={
            "draft_id": _IDENTIFIER,
            "created_from": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind"],
                "properties": {
                    "kind": {
                        "enum": [
                            "source_import",
                            "published_version",
                            "empty_template",
                            "test_package",
                        ],
                    },
                    "reference": _IDENTIFIER,
                },
            },
            "source_ids": _IDENTIFIER_LIST,
            "parent_version_id": {
                "anyOf": [_IDENTIFIER, {"type": "null"}],
            },
            "trust_inputs": {"type": "array", "items": _TRUST},
            "asset_policy": _OPEN_MAP,
            # The compiled effective execution restrictions. Each one
            # declares hard or reviewable override behavior.
            "effective_restrictions": _RESTRICTION_LIST,
            "validation_issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "message"],
                    "properties": {
                        "code": _IDENTIFIER,
                        "message": _TEXT,
                        "case_id": _IDENTIFIER,
                    },
                },
            },
            "metadata": _OPEN_MAP,
        },
    ),
    "dataset-version": _record_schema(
        "dataset-version",
        title="The frozen publication record of one dataset version",
        required=[
            "version_id",
            "canonical_schema_version",
            "source_lineage",
            "trust_inputs",
            "effective_restrictions",
            "policy_digest",
            "case_manifest_digest",
            "transformation_recipe_digest",
            "split_manifest",
            "asset_digests",
            "content_digest",
            "validation_report_digest",
            "contamination_record_digest",
            "attribution_bundle_digest",
        ],
        properties={
            "version_id": _IDENTIFIER,
            "parent_version_id": {
                "anyOf": [_IDENTIFIER, {"type": "null"}],
            },
            "canonical_schema_version": _NAME,
            "source_lineage": _IDENTIFIER_LIST,
            "trust_inputs": {"type": "array", "items": _TRUST},
            "effective_restrictions": _RESTRICTION_LIST,
            "policy_digest": _DIGEST,
            "case_manifest_digest": _DIGEST,
            "transformation_recipe_digest": _DIGEST,
            "split_manifest": {
                "type": "object",
                "additionalProperties": _IDENTIFIER_LIST,
            },
            "asset_digests": {"type": "array", "items": _DIGEST},
            "content_digest": _DIGEST,
            "validation_report_digest": _DIGEST,
            "contamination_record_digest": _DIGEST,
            "attribution_bundle_digest": _DIGEST,
        },
    ),
    "scorer-spec": _record_schema(
        "scorer-spec",
        title="One complete scorer declaration",
        required=[
            "scorer_id",
            "version",
            "implementation_digest",
            "description",
            "input_evidence_contract",
            "configuration_schema",
            "output_dimensions",
            "scale",
            "direction",
            "determinism",
            "required_evidence",
            "trust_class",
            "sandbox",
            "execution_digests",
        ],
        properties={
            "scorer_id": _IDENTIFIER,
            "version": _NAME,
            "implementation_digest": _DIGEST,
            "description": _TEXT,
            "input_evidence_contract": _STRING_LIST,
            "configuration_schema": _OPEN_MAP,
            "output_dimensions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "scale", "direction"],
                    "properties": {
                        "name": _IDENTIFIER,
                        "scale": _SCALE,
                        "direction": _DIRECTION,
                    },
                },
            },
            "scale": _SCALE,
            "direction": _DIRECTION,
            "determinism": {
                "enum": ["deterministic", "seeded", "nondeterministic"],
            },
            "required_evidence": _STRING_LIST,
            "judge": {
                "type": "object",
                "additionalProperties": False,
                "required": ["model"],
                "properties": {
                    "model": _NAME,
                    "calibration_reference": _IDENTIFIER,
                },
            },
            "trust_class": {
                "enum": ["built_in", "reviewed", "sandboxed_untrusted"],
            },
            "sandbox": {
                "type": "object",
                "additionalProperties": False,
                "required": ["policy_version", "policy_digest"],
                "properties": {
                    "policy_version": _NAME,
                    "policy_digest": _DIGEST,
                },
            },
            "execution_digests": {
                "type": "object",
                "additionalProperties": False,
                "required": ["artifact", "runtime", "dependencies"],
                "properties": {
                    "artifact": _DIGEST,
                    "runtime": _DIGEST,
                    "dependencies": _DIGEST,
                    "locale": _DIGEST,
                    "time_source": _DIGEST,
                    "random_source": _DIGEST,
                },
            },
        },
    ),
    "run-plan": _record_schema(
        "run-plan",
        title="The frozen experimental design of one run",
        required=[
            "plan_id",
            "digests",
            "outcome_mapping_set_digest",
            "arm_mappings",
            "case_ids",
            "seed_schedule",
            "arm_order",
            "repetitions",
            "limits",
            "retry_rules",
            "unit_hierarchy",
            "estimand",
            "trust_policy",
            "resource_estimate",
            "settlement",
            "dispatch",
        ],
        properties={
            "plan_id": _IDENTIFIER,
            "digests": {
                "type": "object",
                "additionalProperties": False,
                "required": ["dataset", "runtime", "scorers", "schema"],
                "properties": {
                    "dataset": _DIGEST,
                    "runtime": _DIGEST,
                    "scorers": _DIGEST,
                    "schema": _DIGEST,
                    "interaction": _DIGEST,
                    "metric_definitions": _DIGEST,
                    "contamination_policy": _DIGEST,
                },
            },
            "outcome_mapping_set_digest": _DIGEST,
            "arm_mappings": {
                "type": "array",
                "minItems": 1,
                "items": _MAPPING_MEMBER,
            },
            # The exact sampled case identifiers.
            "case_ids": {
                "type": "array",
                "minItems": 1,
                "items": _IDENTIFIER,
            },
            "seed_schedule": {
                "type": "object",
                "additionalProperties": False,
                "required": ["base_seed", "scope"],
                "properties": {
                    "base_seed": _COUNT,
                    "scope": {"const": "item-repetition"},
                },
            },
            "arm_order": {
                "type": "object",
                "additionalProperties": False,
                "required": ["strategy"],
                "properties": {
                    "strategy": {
                        "enum": ["rotated_interleave", "randomized"],
                    },
                    "rotation": _NAME,
                },
            },
            "repetitions": {"type": "integer", "minimum": 1},
            "limits": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_concurrency": {"type": "integer", "minimum": 1},
                    "timeout_seconds": {"type": "integer", "minimum": 1},
                    "run_cost": _MONEY_REF,
                    "attempt_cost": _MONEY_REF,
                },
            },
            "retry_rules": {
                "type": "object",
                "additionalProperties": False,
                "required": ["infrastructure_exclusions"],
                "properties": {
                    "infrastructure_exclusions": _STRING_LIST,
                    "reason": _TEXT,
                },
            },
            "unit_hierarchy": {
                "type": "array",
                "minItems": 1,
                "items": {"enum": ["family", "case", "repetition"]},
            },
            "estimand": _OPEN_MAP,
            "trust_policy": {
                "type": "object",
                "additionalProperties": False,
                "required": ["trust", "capabilities"],
                "properties": {
                    "trust": _TRUST,
                    "capabilities": _STRING_LIST,
                },
            },
            "resource_estimate": {
                "type": "object",
                "additionalProperties": False,
                "required": ["expected_cost", "pricing_version"],
                "properties": {
                    "expected_cost": _MONEY_REF,
                    "pricing_version": _NAME,
                },
            },
            "settlement": {
                "type": "object",
                "additionalProperties": False,
                "required": ["policy", "maximum_wait_seconds"],
                "properties": {
                    "policy": {"enum": ["strict", "operator_bounded"]},
                    "maximum_wait_seconds": {
                        "type": "integer",
                        "minimum": 0,
                    },
                },
            },
            "dispatch": {
                "type": "object",
                "additionalProperties": False,
                "required": ["fairness_policy", "starvation_limit"],
                "properties": {
                    "fairness_policy": {
                        "const": "weighted_round_robin",
                    },
                    "starvation_limit": {
                        "type": "integer",
                        "minimum": 1,
                    },
                },
            },
        },
    ),
    "attempt-evidence": _record_schema(
        "attempt-evidence",
        title="One immutable evidence bundle for one attempt",
        required=[
            "attempt_id",
            "run_manifest_digest",
            "runtime_specification_digest",
            "case_reference",
            "trace_digest",
            "final_output_digest",
            "resources",
            "completeness",
            "seed_evidence",
            "versions",
            "ledger_references",
        ],
        properties={
            "attempt_id": _IDENTIFIER,
            "run_manifest_digest": _DIGEST,
            "runtime_specification_digest": _DIGEST,
            "case_reference": {
                "type": "object",
                "additionalProperties": False,
                "required": ["case_id"],
                "properties": {
                    "case_id": _IDENTIFIER,
                    "asset_ids": _IDENTIFIER_LIST,
                },
            },
            # A legacy adapter marks an unavailable section
            # explicitly instead of fabricating a digest.
            "trace_digest": {"anyOf": [_DIGEST, {"type": "null"}]},
            "final_output_digest": {
                "anyOf": [_DIGEST, {"type": "null"}],
            },
            "final_state_digest": _DIGEST,
            "board_state_reference": _IDENTIFIER,
            "tool_calls_digest": _DIGEST,
            "artifacts": {"type": "array", "items": _DIGEST},
            "verification_decisions_digest": _DIGEST,
            "resources": {
                "type": "object",
                "additionalProperties": False,
                "required": ["cost", "tokens", "latency_ms"],
                "properties": {
                    # An unknown legacy cost stays null; it never
                    # becomes a zero amount.
                    "cost": {"anyOf": [_MONEY_REF, {"type": "null"}]},
                    "tokens": _COUNT,
                    "latency_ms": _COUNT,
                },
            },
            "completeness": {
                "type": "object",
                "additionalProperties": False,
                "required": ["level", "unavailable_sections"],
                "properties": {
                    "level": {"enum": ["complete", "partial_legacy"]},
                    "unavailable_sections": _STRING_LIST,
                },
            },
            "recovery_events": {"type": "array", "items": _OPEN_MAP},
            "seed_evidence": {
                "type": "object",
                "additionalProperties": False,
                "required": ["requested_seed", "seed_control"],
                "properties": {
                    "requested_seed": {"type": "integer"},
                    "seed_control": {
                        "enum": ["recorded", "best_effort", "applied"],
                    },
                    "applied_seed": {"type": ["integer", "null"]},
                },
            },
            "failure_classification": {
                "anyOf": [_NAME, {"type": "null"}],
            },
            "versions": {
                "type": "object",
                "additionalProperties": _NAME,
            },
            "ledger_references": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "admission_effect_id": _IDENTIFIER,
                    "reservation_id": _IDENTIFIER,
                    "resource_ledger_ids": _IDENTIFIER_LIST,
                },
            },
        },
    ),
    "score-record": _record_schema(
        "score-record",
        title="One named-dimension score record",
        required=[
            "score_id",
            "scorer",
            "evidence_references",
            "dimensions",
            "explanation",
            "status",
        ],
        properties={
            "score_id": _IDENTIFIER,
            "scorer": {
                "type": "object",
                "additionalProperties": False,
                "required": ["scorer_id", "version",
                             "configuration_digest"],
                "properties": {
                    "scorer_id": _IDENTIFIER,
                    "version": _NAME,
                    "configuration_digest": _DIGEST,
                },
            },
            "evidence_references": _IDENTIFIER_LIST,
            # Named dimensions only; one unexplained average never
            # stores.
            "dimensions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name"],
                    "properties": {
                        "name": _IDENTIFIER,
                        "value": {"type": ["number", "null"]},
                        "category": {"anyOf": [_NAME, {"type": "null"}]},
                    },
                },
            },
            "passed": {"type": ["boolean", "null"]},
            "explanation": _TEXT,
            "uncertainty": {"type": ["number", "null"]},
            "judge": {
                "type": "object",
                "additionalProperties": False,
                "required": ["request_digest", "response_digest"],
                "properties": {
                    "request_digest": _DIGEST,
                    "response_digest": _DIGEST,
                },
            },
            "calibration_version": _NAME,
            # A sandboxed execution records its boundary policy and
            # every pinned runtime digest, so the score stays
            # replayable on a qualified host.
            "sandbox": {
                "type": "object",
                "additionalProperties": False,
                "required": ["boundary", "policy_digest",
                             "runtime_digest"],
                "properties": {
                    "boundary": {
                        "enum": ["trusted_service", "wasi_component",
                                 "native_microvm"],
                    },
                    "policy_digest": _DIGEST,
                    "runtime_digest": _DIGEST,
                    "component_digest": _DIGEST,
                    "wit_digest": _DIGEST,
                    "compiler_digest": _DIGEST,
                    "dependency_lock_digest": _DIGEST,
                    "output_schema_digest": _DIGEST,
                    "terminal_class": _NAME,
                    "replay_eligible": {"type": "boolean"},
                    "fuel_used": _COUNT,
                },
            },
            "status": {"enum": ["scored", "error", "excluded"]},
            "error": {"anyOf": [_TEXT, {"type": "null"}]},
        },
    ),
    "analysis-snapshot": _record_schema(
        "analysis-snapshot",
        title="One frozen report computation",
        required=[
            "snapshot_id",
            "run_checksum",
            "evidence_checksum",
            "filters",
            "missingness_policy",
            "estimand",
            "unit_hierarchy",
            "resampling",
            "methods",
            "multiplicity_groups",
            "results_digest",
            "report_checksum",
            "engine",
            "random_source",
            "io_checksums",
        ],
        properties={
            "snapshot_id": _IDENTIFIER,
            "run_checksum": _DIGEST,
            "evidence_checksum": _DIGEST,
            "filters": _OPEN_MAP,
            "missingness_policy": _NAME,
            "estimand": _OPEN_MAP,
            "unit_hierarchy": {
                "type": "array",
                "items": {"enum": ["family", "case", "repetition"]},
            },
            "resampling": {
                "type": "object",
                "additionalProperties": False,
                "required": ["cluster_order", "small_cluster_policy",
                             "resample_count"],
                "properties": {
                    "cluster_order": _STRING_LIST,
                    "small_cluster_policy": _NAME,
                    "resample_count": _COUNT,
                },
            },
            "methods": {
                "type": "object",
                "additionalProperties": False,
                "required": ["estimator", "interval_method",
                             "confidence_level"],
                "properties": {
                    "estimator": _NAME,
                    "interval_method": _NAME,
                    "confidence_level": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "exclusiveMaximum": 1,
                    },
                },
            },
            "multiplicity_groups": _STRING_LIST,
            "results_digest": _DIGEST,
            "report_checksum": _DIGEST,
            "engine": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_digest", "build_digest",
                             "dependency_lock_digest",
                             "toolchain_versions"],
                "properties": {
                    "source_digest": _DIGEST,
                    "build_digest": _DIGEST,
                    "dependency_lock_digest": _DIGEST,
                    "runtime_digest": _DIGEST,
                    "toolchain_versions": {
                        "type": "object",
                        "additionalProperties": _NAME,
                    },
                },
            },
            "random_source": {
                "type": "object",
                "additionalProperties": False,
                "required": ["algorithm", "implementation_digest",
                             "master_seed", "derivation_schedule"],
                "properties": {
                    "algorithm": _NAME,
                    # The algorithm version is metadata, never part
                    # of the algorithm identifier.
                    "algorithm_version": _COUNT,
                    "implementation": _NAME,
                    "implementation_digest": _DIGEST,
                    "master_seed": _COUNT,
                    "derivation_schedule": _STRING_LIST,
                },
            },
            "io_checksums": {
                "type": "object",
                "additionalProperties": False,
                "required": ["input", "output"],
                "properties": {"input": _DIGEST, "output": _DIGEST},
            },
            # The replay claim separates deterministic analysis replay
            # from external execution repeatability; a model run is
            # never reproducible because its stored analysis replays.
            "replay": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim", "execution_provenance_complete"],
                "properties": {
                    "claim": {
                        "enum": ["analysis_replayable",
                                 "analysis_not_replayable"],
                    },
                    "execution_provenance_complete": {"type": "boolean"},
                    "execution_seed_requested": {"type": "boolean"},
                    "execution_seed_confirmed": {"type": "boolean"},
                    "missing_provenance_fields": _STRING_LIST,
                },
            },
        },
    ),
    "gate-evaluation": _record_schema(
        "gate-evaluation",
        title="One final terminal gate decision",
        required=[
            "evaluation_id",
            "baseline_analysis_checksum",
            "candidate_analysis_checksum",
            "invariant_digest",
            "treatment",
            "rules",
            "display_exceptions",
        ],
        properties={
            "evaluation_id": _IDENTIFIER,
            "baseline_analysis_checksum": _DIGEST,
            "candidate_analysis_checksum": _DIGEST,
            "invariant_digest": _DIGEST,
            "treatment": {
                "type": "object",
                "additionalProperties": False,
                "required": ["declaration", "digest"],
                "properties": {
                    # One exact field path per entry; a wildcard path
                    # never validates.
                    "declaration": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["field_path", "baseline_digest",
                                         "candidate_digest", "reason"],
                            "properties": {
                                "field_path": {
                                    "type": "string",
                                    "pattern": (
                                        r"^[a-zA-Z0-9_.-]+"
                                        r"(\.[a-zA-Z0-9_.-]+)*$"
                                    ),
                                },
                                "baseline_digest": _DIGEST,
                                "candidate_digest": _DIGEST,
                                "reason": _TEXT,
                            },
                        },
                    },
                    "digest": _DIGEST,
                },
            },
            "rules": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "metric", "operator", "value",
                                 "direction"],
                    "properties": {
                        "id": _IDENTIFIER,
                        "metric": _NAME,
                        "operator": {
                            "enum": ["gte", "lte", "max_drop",
                                     "max_increase_ratio"],
                        },
                        "value": {"type": "number"},
                        "direction": {
                            "enum": ["improvement", "reduction"],
                        },
                        "practical_size": {
                            "type": ["number", "null"],
                            "minimum": 0,
                        },
                        "analysis_method": _NAME,
                    },
                },
            },
            "display_exceptions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["author", "scope", "expires_at",
                                 "reason"],
                    "properties": {
                        "author": _NAME,
                        "scope": _NAME,
                        "expires_at": _TIMESTAMP,
                        "reason": _TEXT,
                    },
                },
            },
        },
    ),
    "interaction-spec": _record_schema(
        "interaction-spec",
        title="One versioned multi-turn interaction specification",
        required=[
            "spec_id",
            "protocol",
            "participants",
            "simulator",
            "initial_messages",
            "limits",
            "allowed",
            "stop_conditions",
            "secrets",
            "trajectory_assertions",
            "recovery_rules",
        ],
        properties={
            "spec_id": _IDENTIFIER,
            "protocol": _IDENTIFIER,
            "participants": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["role", "channel"],
                    "properties": {"role": _NAME, "channel": _NAME},
                },
            },
            # An imported case references a registered simulator
            # version; it never provides executable simulator code.
            "simulator": {
                "type": "object",
                "additionalProperties": False,
                "required": ["implementation_id", "prompt_digest",
                             "model", "image_digest",
                             "dependency_digest"],
                "properties": {
                    "implementation_id": _IDENTIFIER,
                    "prompt_digest": _DIGEST,
                    "model": _NAME,
                    "image_digest": _DIGEST,
                    "dependency_digest": _DIGEST,
                },
            },
            "initial_messages": {"type": "array", "items": _OPEN_MAP},
            "observation_rules": {"type": "array", "items": _OPEN_MAP},
            "limits": {
                "type": "object",
                "additionalProperties": False,
                "required": ["max_turns"],
                "properties": {
                    "max_turns": {"type": "integer", "minimum": 1},
                    "max_actions": {"type": "integer", "minimum": 1},
                    "max_tokens": {"type": "integer", "minimum": 1},
                    "max_seconds": {"type": "integer", "minimum": 1},
                    "max_cost": _MONEY_REF,
                },
            },
            "allowed": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tools": _IDENTIFIER_LIST,
                    "capabilities": _STRING_LIST,
                    "environment_operations": _STRING_LIST,
                },
            },
            "stop_conditions": _STRING_LIST,
            "invalid_transition_behavior": _NAME,
            "secrets": {
                "type": "object",
                "additionalProperties": False,
                "required": ["classes", "canary_references"],
                "properties": {
                    "classes": _STRING_LIST,
                    # A simulator receives synthetic canaries instead
                    # of production secrets.
                    "canary_references": _IDENTIFIER_LIST,
                },
            },
            "trajectory_assertions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["verifier_id", "event_selector",
                                 "quantifier", "predicate", "severity",
                                 "missing_evidence_result"],
                    "properties": {
                        "verifier_id": _IDENTIFIER,
                        "event_selector": _NAME,
                        "quantifier": {
                            "enum": ["all", "any", "none", "at_least"],
                        },
                        "predicate": _NAME,
                        "severity": {"enum": ["blocking", "warning"]},
                        "missing_evidence_result": {
                            "enum": ["fail", "warn", "unknown"],
                        },
                    },
                },
            },
            "recovery_rules": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "retry": _NAME,
                    "timeout": _NAME,
                    "missing_turn": _NAME,
                },
            },
        },
    ),
    "contamination-rights-record": _record_schema(
        "contamination-rights-record",
        title="The contamination and rights record of one version",
        required=[
            "record_id",
            "dataset_version_id",
            "split_rules",
            "screening",
            "matches",
            "holdout_access",
            "canaries",
            "license_decisions",
            "attribution",
            "use_decisions",
        ],
        properties={
            "record_id": _IDENTIFIER,
            "dataset_version_id": _IDENTIFIER,
            "split_rules": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "development": _NAME,
                    "validation": _NAME,
                    "hidden_test": _NAME,
                },
            },
            "screening": {
                "type": "object",
                "additionalProperties": False,
                "required": ["implementation", "corpus", "thresholds",
                             "version", "result"],
                "properties": {
                    "implementation": _NAME,
                    "corpus": _NAME,
                    "thresholds": _OPEN_MAP,
                    "version": _NAME,
                    # Screening never proves that a model missed an
                    # item; the label stays one of these states.
                    "result": {
                        "enum": ["screened", "suspected", "confirmed",
                                 "unknown"],
                    },
                },
            },
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["case_id", "kind", "decision"],
                    "properties": {
                        "case_id": _IDENTIFIER,
                        "kind": {
                            "enum": ["content_hash", "exact_match",
                                     "approximate_overlap"],
                        },
                        "decision": _NAME,
                        "reviewer": _NAME,
                    },
                },
            },
            "holdout_access": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["actor", "accessed_at"],
                    "properties": {
                        "actor": _NAME,
                        "accessed_at": _TIMESTAMP,
                    },
                },
            },
            "canaries": {
                "type": "object",
                "additionalProperties": False,
                "required": ["identifiers"],
                "properties": {
                    "identifiers": _IDENTIFIER_LIST,
                    "rotation_history": {
                        "type": "array",
                        "items": _OPEN_MAP,
                    },
                },
            },
            "license_decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["subject", "decision"],
                    "properties": {
                        "subject": _NAME,
                        "decision": {
                            "enum": ["approved", "restricted",
                                     "unresolved"],
                        },
                        "note": _TEXT,
                    },
                },
            },
            "attribution": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {
                    "text": _TEXT,
                    "links": _STRING_LIST,
                },
            },
            "use_decisions": {
                "type": "object",
                "additionalProperties": False,
                "required": ["allowed_use", "redistribution",
                             "modification", "export"],
                "properties": {
                    "allowed_use": _NAME,
                    "redistribution": {
                        "enum": ["allowed", "denied"],
                    },
                    "modification": {"enum": ["allowed", "denied"]},
                    "export": {"enum": ["allowed", "denied"]},
                },
            },
        },
    ),
    "metric-definition": _record_schema(
        "metric-definition",
        title="One complete immutable metric definition",
        required=[
            "metric_id",
            "lifecycle_state",
            "calibration",
            "population",
            "measurement",
            "labels",
            "scorer",
            "missingness",
            "uncertainty_method",
        ],
        properties={
            "metric_id": _IDENTIFIER,
            "lifecycle_state": {
                "enum": ["draft", "validated", "published",
                         "deprecated", "withdrawn"],
            },
            "calibration": {
                "type": "object",
                "additionalProperties": False,
                "required": ["state", "method", "version"],
                "properties": {
                    "state": {
                        "enum": ["current", "due", "expired", "failed"],
                    },
                    "dataset": _IDENTIFIER,
                    "method": _NAME,
                    "result": _OPEN_MAP,
                    "version": _NAME,
                    "calibrated_at": _TIMESTAMP,
                    "expires_at": _TIMESTAMP,
                    "drift_policy": _NAME,
                },
            },
            "population": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target", "inclusion_rule"],
                "properties": {
                    "target": _NAME,
                    "inclusion_rule": _TEXT,
                },
            },
            "measurement": {
                "type": "object",
                "additionalProperties": False,
                "required": ["numerator", "denominator", "unit",
                             "range", "direction", "aggregation"],
                "properties": {
                    "numerator": _TEXT,
                    "denominator": _TEXT,
                    "unit": _NAME,
                    "range": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["minimum", "maximum"],
                        "properties": {
                            "minimum": {"type": "number"},
                            "maximum": {"type": "number"},
                        },
                    },
                    "direction": _DIRECTION,
                    "aggregation": _NAME,
                },
            },
            "labels": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "evidence_contract"],
                "properties": {
                    "source": _NAME,
                    "evidence_contract": _STRING_LIST,
                },
            },
            "scorer": {
                "type": "object",
                "additionalProperties": False,
                "required": ["scorer_id", "version",
                             "configuration_digest"],
                "properties": {
                    "scorer_id": _IDENTIFIER,
                    "version": _NAME,
                    "configuration_digest": _DIGEST,
                },
            },
            "missingness": _NAME,
            "exclusions": _STRING_LIST,
            "uncertainty_method": _NAME,
        },
    ),
    "asset-ingestion-record": _record_schema(
        "asset-ingestion-record",
        title="The quarantine and acceptance record of one asset",
        required=[
            "ingestion_id",
            "original_name",
            "declared_media_type",
            "detected_media_type",
            "size_bytes",
            "digest",
            "scanner",
            "state",
        ],
        properties={
            "ingestion_id": _IDENTIFIER,
            "original_name": _NAME,
            "declared_media_type": _NAME,
            "detected_media_type": _NAME,
            "size_bytes": _COUNT,
            "digest": _DIGEST,
            "scanner": {
                "type": "object",
                "additionalProperties": False,
                "required": ["engine", "signature_version", "result",
                             "completed_at"],
                "properties": {
                    "engine": _NAME,
                    "signature_version": _NAME,
                    "result": {"enum": ["clean", "flagged", "failed"]},
                    "completed_at": _TIMESTAMP,
                },
            },
            "archive": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entry_count", "depth", "expanded_bytes",
                             "compression_ratio"],
                "properties": {
                    "entry_count": _COUNT,
                    "depth": _COUNT,
                    "expanded_bytes": _COUNT,
                    "compression_ratio": {
                        "type": "number",
                        "minimum": 0,
                    },
                },
            },
            "extraction": {
                "type": "object",
                "additionalProperties": False,
                "required": ["image_digest", "policy_digest",
                             "output_manifest_digest"],
                "properties": {
                    "image_digest": _DIGEST,
                    "policy_digest": _DIGEST,
                    "output_manifest_digest": _DIGEST,
                },
            },
            "state": {
                "enum": ["quarantined", "accepted", "rejected",
                         "deleted"],
            },
        },
    ),
    "judge-calibration-record": _record_schema(
        "judge-calibration-record",
        title="One judge calibration against pinned human labels",
        required=[
            "calibration_id",
            "judge",
            "scorer",
            "dataset",
            "independence",
            "agreement",
            "disagreement",
            "invalid_output",
            "abstention",
            "drift",
            "state",
            "calibrated_at",
        ],
        properties={
            "calibration_id": _IDENTIFIER,
            "judge": {
                "type": "object",
                "additionalProperties": False,
                "required": ["judge_id", "version", "model",
                             "prompt_digest"],
                "properties": {
                    "judge_id": _IDENTIFIER,
                    "version": _NAME,
                    "model": _NAME,
                    "prompt_digest": _DIGEST,
                },
            },
            "scorer": {
                "type": "object",
                "additionalProperties": False,
                "required": ["scorer_id", "version"],
                "properties": {
                    "scorer_id": _IDENTIFIER,
                    "version": _NAME,
                },
            },
            "dataset": {
                "type": "object",
                "additionalProperties": False,
                "required": ["dataset_id", "version", "label_digest",
                             "item_count"],
                "properties": {
                    "dataset_id": _IDENTIFIER,
                    "version": _NAME,
                    "label_digest": _DIGEST,
                    "item_count": _COUNT,
                },
            },
            # The judge is independent only when no candidate shares
            # its model and no candidate content shaped its prompt.
            "independence": {
                "type": "object",
                "additionalProperties": False,
                "required": ["independent", "candidate_models",
                             "reason"],
                "properties": {
                    "independent": {"type": "boolean"},
                    "candidate_models": _STRING_LIST,
                    "reason": _TEXT,
                },
            },
            "agreement": {
                "type": "object",
                "additionalProperties": False,
                "required": ["raw", "kappa", "kappa_defined",
                             "interval"],
                "properties": {
                    "raw": {"type": "number", "minimum": 0,
                            "maximum": 1},
                    # Kappa reports only when its calculation defines
                    # it; otherwise it stays null with the flag false.
                    "kappa": {"type": ["number", "null"]},
                    "kappa_defined": {"type": "boolean"},
                    "interval": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["low", "high", "method"],
                        "properties": {
                            "low": {"type": "number"},
                            "high": {"type": "number"},
                            "method": _NAME,
                        },
                    },
                },
            },
            "disagreement": {
                "type": "object",
                "additionalProperties": False,
                "required": ["count", "item_ids"],
                "properties": {
                    "count": _COUNT,
                    "item_ids": _IDENTIFIER_LIST,
                },
            },
            "invalid_output": {
                "type": "object",
                "additionalProperties": False,
                "required": ["count", "rate"],
                "properties": {
                    "count": _COUNT,
                    "rate": {"type": "number", "minimum": 0,
                             "maximum": 1},
                },
            },
            "abstention": {
                "type": "object",
                "additionalProperties": False,
                "required": ["count", "rate"],
                "properties": {
                    "count": _COUNT,
                    "rate": {"type": "number", "minimum": 0,
                             "maximum": 1},
                },
            },
            "drift": {
                "type": "object",
                "additionalProperties": False,
                "required": ["previous_version", "raw_agreement_delta"],
                "properties": {
                    "previous_version": {
                        "anyOf": [_NAME, {"type": "null"}],
                    },
                    "raw_agreement_delta": {"type": ["number", "null"]},
                    "exceeds_policy": {"type": "boolean"},
                },
            },
            "state": {"enum": ["current", "failed"]},
            "threshold": {"type": "number", "minimum": 0,
                          "maximum": 1},
            "calibrated_at": _TIMESTAMP,
        },
    ),
    "failure-classification-record": _record_schema(
        "failure-classification-record",
        title="One multi-label failure classification of one attempt",
        required=[
            "classification_id",
            "attempt_id",
            "classes",
            "source",
            "classifier",
            "evidence_references",
            "supersedes",
            "classified_at",
        ],
        properties={
            "classification_id": _IDENTIFIER,
            "attempt_id": _IDENTIFIER,
            # Multiple classes apply at once; each names its family
            # from the documented taxonomy.
            "classes": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["family", "name"],
                    "properties": {
                        "family": _NAME,
                        "name": _NAME,
                        "confidence": {
                            "type": ["number", "null"],
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                },
            },
            "source": {"enum": ["automatic", "human"]},
            "classifier": _NAME,
            "evidence_references": _IDENTIFIER_LIST,
            # A human correction supersedes the prior record and keeps
            # it readable; the prior never changes.
            "supersedes": {"anyOf": [_IDENTIFIER, {"type": "null"}]},
            "correction": {
                "type": "object",
                "additionalProperties": False,
                "required": ["reviewer", "reason"],
                "properties": {
                    "reviewer": _NAME,
                    "reason": _TEXT,
                },
            },
            "classified_at": _TIMESTAMP,
        },
    ),
    "resource-ledger-entry": _record_schema(
        "resource-ledger-entry",
        title="One immutable resource ledger entry",
        required=[
            "entry_id",
            "resource_class",
            "provider",
            "service",
            "region",
            "quantity",
            "pricing_version",
            "charge_state",
            "references",
            "reservation_id",
            "reconciliation_id",
            "estimate_entry_id",
            "recorded_at",
        ],
        properties={
            "entry_id": _IDENTIFIER,
            "resource_class": {
                "enum": [
                    "runtime", "control_plane", "scorer", "judge",
                    "environment", "external_tool", "import",
                    "transformation", "storage", "human_review",
                ],
            },
            "provider": _NAME,
            "service": _NAME,
            "region": _NAME,
            "quantity": {
                "type": "object",
                "additionalProperties": False,
                "required": ["value", "unit"],
                "properties": {
                    "value": {"type": "number", "minimum": 0},
                    "unit": _NAME,
                },
            },
            "pricing_version": _NAME,
            # An estimate and an actual charge live in separate
            # objects and never replace each other.
            "estimate": {
                "type": "object",
                "additionalProperties": False,
                "required": ["value", "method", "estimated_at"],
                "properties": {
                    "value": _MONEY_REF,
                    "method": _NAME,
                    "estimated_at": _TIMESTAMP,
                },
            },
            "actual": {
                "type": "object",
                "additionalProperties": False,
                "required": ["value", "evidence", "charged_at"],
                "properties": {
                    "value": _MONEY_REF,
                    "evidence": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["provider_text", "source"],
                        "properties": {
                            # The original provider string stays as
                            # evidence; the Money value is the
                            # authority.
                            "provider_text": _NAME,
                            "source": _NAME,
                            "invoice_reference": _NAME,
                        },
                    },
                    "charged_at": _TIMESTAMP,
                },
            },
            "charge_state": {
                "enum": ["estimated", "confirmed", "unknown",
                         "not_billable"],
            },
            "not_billable_evidence": _TEXT,
            "references": {
                "type": "object",
                "additionalProperties": False,
                "required": ["run_id"],
                "properties": {
                    "run_id": _IDENTIFIER,
                    "attempt_id": _IDENTIFIER,
                    "activation_id": _IDENTIFIER,
                    "scorer_id": _IDENTIFIER,
                    "import_id": _IDENTIFIER,
                    "retry_of": _IDENTIFIER,
                },
            },
            "reservation_id": {"anyOf": [_IDENTIFIER, {"type": "null"}]},
            "reconciliation_id": {
                "anyOf": [_IDENTIFIER, {"type": "null"}],
            },
            "estimate_entry_id": {
                "anyOf": [_IDENTIFIER, {"type": "null"}],
            },
            "recorded_at": _TIMESTAMP,
        },
    ),
}


class EvaluationContractError(ValueError):
    """A record violates its documented evaluation contract."""


@cache
def _validator(schema_id: str) -> Any:
    import jsonschema

    schema = RECORD_SCHEMAS[schema_id]
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


# Monetary scalars are prohibited outside the canonical Money shape.
# A minor-unit integer key, a currency-suffixed scalar, or a bare
# monetary scalar rejects at the controlled boundary.
_MINOR_UNIT_SUFFIXES = ("_cents", "_minor_units", "_minor")
_CURRENCY_SUFFIXES = (
    "_usd", "_eur", "_gbp", "_jpy", "_cny", "_chf", "_cad", "_aud",
)
_MONETARY_KEY_PATTERN = re.compile(
    r"(^|_)(cost|price|amount|budget)s?$"
)


def _is_money_shape(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {
        "currency", "amount_nanos",
    }


def _validate_money(path: str, value: dict[str, Any]) -> None:
    """Validate one canonical monetary object exactly."""
    amount = value.get("amount_nanos")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise EvaluationContractError(
            f"{path}: amount_nanos must be one exact integer; a binary "
            "floating-point or string amount never validates"
        )
    try:
        Money(currency=value.get("currency"), amount_nanos=amount)
    except MoneyError as error:
        raise EvaluationContractError(f"{path}: {error}") from error


def _walk_money_rules(value: Any, path: str) -> None:
    if isinstance(value, dict):
        if _is_money_shape(value):
            _validate_money(path, value)
            return
        for key, item in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            lowered = str(key).lower()
            if lowered.endswith(_MINOR_UNIT_SUFFIXES):
                raise EvaluationContractError(
                    f"{key_path}: a minor-unit monetary field never "
                    "validates; use Money(currency, amount_nanos)"
                )
            if lowered == "amount_nanos":
                raise EvaluationContractError(
                    f"{key_path}: amount_nanos appears only inside one "
                    "canonical Money object"
                )
            monetary_name = lowered.endswith(_CURRENCY_SUFFIXES) or (
                _MONETARY_KEY_PATTERN.search(lowered) is not None
            )
            if monetary_name and isinstance(
                item, (int, float, str),
            ) and not isinstance(item, bool):
                raise EvaluationContractError(
                    f"{key_path}: a monetary field uses "
                    "Money(currency, amount_nanos); a minor-unit "
                    "integer, an authoritative decimal string, or a "
                    "float never validates"
                )
            _walk_money_rules(item, key_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_money_rules(item, f"{path}[{index}]")


def validate_record(record: Any) -> dict[str, Any]:
    """Validate one evaluation record at the controlled boundary.

    The record must name a known ``schema_id``, carry the current
    ``schema_version`` in its metadata, match its documented JSON
    Schema with unknown fields rejected, and use the canonical Money
    shape for every monetary field. The result reports the record's
    canonical checksum.
    """
    if not isinstance(record, dict):
        raise EvaluationContractError(
            "An evaluation record is one JSON object"
        )
    schema_id = record.get("schema_id")
    if schema_id not in RECORD_SCHEMAS:
        raise EvaluationContractError(
            f"Unknown evaluation record schema: {schema_id!r}"
        )
    errors = sorted(
        _validator(str(schema_id)).iter_errors(record),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path)
        raise EvaluationContractError(
            f"The {schema_id} record is invalid at "
            f"{location or 'the record root'}: {first.message}"
        )
    _walk_money_rules(record, "")
    if schema_id == "attempt-evidence":
        _validate_evidence_completeness(record)
    return {
        "schema_id": str(schema_id),
        "schema_version": int(record["schema_version"]),
        "record_checksum": content_checksum(record),
    }


def _validate_evidence_completeness(record: dict[str, Any]) -> None:
    """Require an explicit mark for every unavailable evidence section.

    A complete bundle carries every digest. A partial legacy bundle
    names each unavailable section instead of fabricating a digest,
    and it never claims completeness.
    """
    unavailable = []
    if record.get("trace_digest") is None:
        unavailable.append("trace")
    if record.get("final_output_digest") is None:
        unavailable.append("final_output")
    if (record.get("resources") or {}).get("cost") is None:
        unavailable.append("resources.cost")
    completeness = record.get("completeness") or {}
    declared = set(completeness.get("unavailable_sections") or [])
    if unavailable and completeness.get("level") != "partial_legacy":
        raise EvaluationContractError(
            "An evidence bundle with a missing section declares the "
            "partial_legacy level"
        )
    missing = [name for name in unavailable if name not in declared]
    if missing:
        raise EvaluationContractError(
            "Every unavailable evidence section is marked explicitly; "
            f"unmarked: {sorted(missing)}"
        )
    if completeness.get("level") == "complete" and declared:
        raise EvaluationContractError(
            "A complete evidence bundle declares no unavailable section"
        )


def canonical_record_json(record: dict[str, Any]) -> str:
    """Return the canonical stored JSON for one validated record."""
    return json.dumps(record, separators=(",", ":"), sort_keys=True)
