"""Map legacy benchmark records into the current evaluation contracts.

Each adapter builds one complete, validating current-generation record
from one complete legacy record. Derived digests hash the exact legacy
values they stand for, an unavailable evidence section marks itself
explicitly instead of fabricating a digest, and a legacy
floating-point cost converts through the compatibility adapter with
its legacy source kept as evidence only.
"""

from __future__ import annotations

from typing import Any

from benchmarks import costs, outcome_mappings
from benchmarks.evaluation_contracts import (
    EVALUATION_CONTRACT_GENERATION,
    validate_record,
)
from benchmarks.provenance import content_checksum
from benchmarks.repository import STARVATION_PROMOTION_LIMIT

_LEGACY_TRUST = {"level": "owner_uploaded", "policy_version": "legacy"}


def _derived_digest(domain: str, value: Any) -> str:
    """Digest the exact legacy value one derived field stands for."""
    return content_checksum({"legacy_source": domain, "value": value})


def _envelope(schema_id: str) -> dict[str, Any]:
    return {
        "schema_id": schema_id,
        "schema_version": EVALUATION_CONTRACT_GENERATION,
    }


def evaluation_case_from_item(item: dict[str, Any]) -> dict[str, Any]:
    """Map one legacy CSV or JSONL dataset item into one case."""
    record = {
        **_envelope("evaluation-case"),
        "case_id": str(item.get("item_key") or item["id"]),
        "task": {
            "instructions": str(item.get("input") or ""),
            "messages": [],
            "assets": [],
        },
        "expected": {
            "reference_answer": (
                str(item["expected_output"])
                if item.get("expected_output") is not None
                else None
            ),
            "final_state": None,
            "rubric_id": None,
        },
        "environment": None,
        "tools": [],
        "limits": {},
        "classification": {
            "task_family": str(item.get("subject") or "default"),
            "split": str(item.get("split") or "test"),
            "tags": [str(tag) for tag in item.get("tags") or []],
            "intrinsic_horizon": None,
            "human_minutes": None,
        },
        "contamination": {},
        "metadata": {"legacy_item_id": str(item["id"])},
    }
    validate_record(record)
    return record


def scorer_spec_from_scorer(scorer: dict[str, Any]) -> dict[str, Any]:
    """Map one legacy scorer row into one scorer specification."""
    kind = str(scorer.get("kind") or "")
    schema = scorer.get("configuration_schema") or {}
    record = {
        **_envelope("scorer-spec"),
        "scorer_id": str(scorer["id"]),
        "version": str(scorer.get("version") or "1"),
        "implementation_digest": _derived_digest(
            "builtin-scorer-kind", kind,
        ),
        "description": str(scorer.get("description") or kind),
        "input_evidence_contract": ["final_output"],
        "configuration_schema": schema if isinstance(schema, dict) else {},
        "output_dimensions": [{
            "name": "score",
            "scale": "unit_interval",
            "direction": "higher_is_better",
        }],
        "scale": str(scorer.get("scale") or "unit_interval"),
        "direction": str(scorer.get("direction") or "higher_is_better"),
        "determinism": "deterministic",
        "required_evidence": ["final_output"],
        "trust_class": "built_in",
        "sandbox": {
            "policy_version": "legacy",
            "policy_digest": _derived_digest(
                "in-process-builtin-scorer", kind,
            ),
        },
        "execution_digests": {
            "artifact": _derived_digest("scorer-row", {
                "id": str(scorer["id"]),
                "kind": kind,
                "version": str(scorer.get("version") or "1"),
            }),
            "runtime": _derived_digest("daemon-python-runtime", "in-process"),
            "dependencies": _derived_digest(
                "daemon-requirements", "in-process",
            ),
        },
    }
    validate_record(record)
    return record


def run_plan_from_run(run: dict[str, Any]) -> dict[str, Any]:
    """Map one legacy run's frozen execution plan into one run plan."""
    plan = run.get("execution_plan") or {}
    configuration = run.get("test_configuration") or {}
    estimand = dict(plan.get("estimand") or {})
    mapping_set = plan.get("outcome_mapping_set")
    if isinstance(mapping_set, dict) and mapping_set.get("members"):
        set_digest = str(mapping_set["digest"])
        members = [dict(member) for member in mapping_set["members"]]
    else:
        # A run from before the pinned mapping sets derives the
        # current registry members; the provenance note stays with the
        # open estimand map.
        derived = outcome_mappings.mapping_set_for_arms(
            [dict(arm) for arm in plan.get("arms") or []],
        )
        set_digest = str(derived["digest"])
        members = [dict(member) for member in derived["members"]]
        estimand["mapping_set_provenance"] = "derived_from_registry"
    case_ids = sorted({
        case_id
        for keys in (estimand.get("families") or {}).values()
        for case_id in keys
    })
    if not case_ids:
        case_ids = ["unknown-case"]
        estimand["case_manifest_provenance"] = "legacy_plan_without_cases"

    limits: dict[str, Any] = {}
    if configuration.get("max_concurrency") is not None:
        limits["max_concurrency"] = int(configuration["max_concurrency"])
    if configuration.get("timeout_seconds") is not None:
        limits["timeout_seconds"] = int(configuration["timeout_seconds"])
    run_limit = costs.run_cost_limit(configuration)
    if run_limit is not None:
        limits["run_cost"] = costs.money_to_json(run_limit)
    attempts = max(int(run.get("total_attempts") or 1), 1)
    expected = costs.attempt_reservation_amount(
        configuration, attempts,
    ).scale_ratio(attempts, 1)

    exclusions = configuration.get("infrastructure_exclusions") or {}
    record = {
        **_envelope("run-plan"),
        "plan_id": f"plan-{run['id']}",
        "digests": {
            "dataset": _coerce_digest(plan.get("dataset_checksum")),
            "runtime": _derived_digest("plan-arms", plan.get("arms") or []),
            "scorers": _derived_digest(
                "plan-scorers", plan.get("scorers") or [],
            ),
            "schema": _derived_digest(
                "plan-schema-version", plan.get("schema_version"),
            ),
        },
        "outcome_mapping_set_digest": set_digest,
        "arm_mappings": members,
        "case_ids": case_ids,
        "seed_schedule": {
            "base_seed": int(plan.get("seed") or 0),
            "scope": "item-repetition",
        },
        "arm_order": {"strategy": "rotated_interleave"},
        "repetitions": int(plan.get("repetitions") or 1),
        "limits": limits,
        "retry_rules": {
            "infrastructure_exclusions": [
                str(category)
                for category in exclusions.get("categories") or []
            ],
        },
        "unit_hierarchy": ["family", "case", "repetition"],
        "estimand": estimand,
        "trust_policy": {"trust": dict(_LEGACY_TRUST), "capabilities": []},
        "resource_estimate": {
            "expected_cost": costs.money_to_json(expected),
            "pricing_version": "legacy-configuration",
        },
        "settlement": {"policy": "strict", "maximum_wait_seconds": 3600},
        "dispatch": {
            "fairness_policy": "weighted_round_robin",
            "starvation_limit": STARVATION_PROMOTION_LIMIT,
        },
    }
    validate_record(record)
    return record


def _coerce_digest(value: Any) -> str:
    """Keep a real digest; derive one from any other legacy checksum."""
    text = str(value or "")
    if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
        return text
    return _derived_digest("legacy-checksum", text)


def attempt_evidence_from_attempt(
    attempt: dict[str, Any],
    *,
    run_id: str,
    plan_checksum: str,
) -> dict[str, Any]:
    """Map one terminal legacy attempt into one partial bundle.

    The bundle keeps every value the legacy attempt records. Every
    section the legacy schema cannot represent marks itself as
    unavailable, and the legacy floating-point cost converts through
    the compatibility adapter as evidence only.
    """
    snapshot = attempt.get("execution_snapshot") or {}
    unavailable = ["trace"]
    result_summary = attempt.get("result_summary")
    final_output_digest = (
        _derived_digest("task-result-summary", str(result_summary))
        if result_summary is not None
        else None
    )
    if final_output_digest is None:
        unavailable.append("final_output")
    raw_cost = attempt.get("total_cost_usd")
    adapted = costs.legacy_cost_adapter(
        float(raw_cost) if raw_cost is not None else None,
    )
    cost = adapted["money"] if adapted is not None else None
    if cost is None:
        unavailable.append("resources.cost")
    versions = {"runtime": str(attempt.get("runtime_id") or "classic")}
    if adapted is not None:
        versions["cost_source"] = "legacy_float"
    ledger: dict[str, Any] = {}
    if attempt.get("admission_effect_id"):
        ledger["admission_effect_id"] = str(attempt["admission_effect_id"])
    if attempt.get("admission_reservation_id"):
        ledger["reservation_id"] = str(
            attempt["admission_reservation_id"],
        )
    record = {
        **_envelope("attempt-evidence"),
        "attempt_id": str(attempt["id"]),
        "run_manifest_digest": _derived_digest(
            "run-plan-checksum",
            {"run_id": run_id, "plan_checksum": plan_checksum},
        ),
        "runtime_specification_digest": _derived_digest(
            "attempt-snapshot", snapshot,
        ),
        "case_reference": {
            "case_id": str(
                attempt.get("item_key")
                or attempt.get("dataset_item_id")
                or "unknown-case",
            ),
            "asset_ids": [],
        },
        "trace_digest": None,
        "final_output_digest": final_output_digest,
        "resources": {
            "cost": cost,
            "tokens": int(attempt.get("total_tokens") or 0),
            "latency_ms": int(attempt.get("duration_ms") or 0),
        },
        "completeness": {
            "level": "partial_legacy",
            "unavailable_sections": sorted(unavailable),
        },
        "seed_evidence": {
            "requested_seed": int(attempt.get("random_seed") or 0),
            "seed_control": str(
                attempt.get("seed_control") or "recorded",
            ),
            "applied_seed": None,
        },
        "failure_classification": (
            str(attempt["failure_category"])
            if attempt.get("failure_category")
            else None
        ),
        "versions": versions,
        "ledger_references": ledger,
    }
    validate_record(record)
    return record


def legacy_item_from_case(case: dict[str, Any]) -> dict[str, Any]:
    """Project one current case back into the legacy item shape.

    The compatibility projection carries every representable field; a
    reader of the legacy shape sees one complete record from one
    generation.
    """
    classification = case.get("classification") or {}
    expected = case.get("expected") or {}
    return {
        "item_key": str(case["case_id"]),
        "input": str((case.get("task") or {}).get("instructions") or ""),
        "expected_output": str(expected.get("reference_answer") or ""),
        "subject": str(classification.get("task_family") or "default"),
        "split": str(classification.get("split") or "test"),
        "tags": [str(tag) for tag in classification.get("tags") or []],
        "metadata": dict(case.get("metadata") or {}),
    }
