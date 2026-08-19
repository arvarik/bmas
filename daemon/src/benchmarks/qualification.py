"""Qualify registered runtimes without inventing unavailable variants."""

from __future__ import annotations

from typing import Any

from benchmarks.provenance import content_checksum
from benchmarks.runtime import prepare_benchmark_arm
from core.variants import require_variant_class


def _latest_runtime_attempts(run: dict[str, Any], runtime_id: str) -> list[dict[str, Any]]:
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for attempt in run.get("attempts") or []:
        if attempt.get("runtime_id") != runtime_id:
            continue
        key = (str(attempt["trial_id"]), int(attempt.get("repeat_index") or 1))
        previous = latest.get(key)
        if previous is None or int(attempt.get("retry_index") or 0) > int(previous.get("retry_index") or 0):
            latest[key] = attempt
    return list(latest.values())


async def qualify_runtime(runtime_id: str, run: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check one registered runtime contract and optional completed run."""
    runtime = require_variant_class(runtime_id)
    descriptor = runtime.descriptor
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "status": "passed" if passed else "failed", "detail": detail})

    check("benchmark_support", descriptor.benchmark.supported, "The runtime declares benchmark support")
    schema = descriptor.benchmark.configuration_schema
    check("configuration_schema", isinstance(schema, dict) and schema.get("type") == "object", "The runtime publishes an object configuration schema")
    check("seed_strategy", descriptor.benchmark.seed_strategy in {"recorded", "applied"}, "The runtime declares how it uses attempt seeds")
    first = await prepare_benchmark_arm(runtime_id, {})
    second = await prepare_benchmark_arm(runtime_id, {})
    check("stable_preflight", first["configuration_checksum"] == second["configuration_checksum"], "Two preflight captures produce the same checksum")

    evidence_status = "not_run"
    if run is not None:
        attempts = _latest_runtime_attempts(run, descriptor.id)
        evidence_status = "passed"
        check("run_terminal", run.get("status") == "completed", "The qualification run completed")
        check("run_attempts", bool(attempts), "The run contains attempts for this runtime")
        check("attempt_terminal", bool(attempts) and all(attempt.get("status") == "completed" for attempt in attempts), "Every latest runtime attempt completed")
        missing_fields: set[str] = set()
        for attempt in attempts:
            snapshot = attempt.get("execution_snapshot") or {}
            plan = snapshot.get("benchmark_plan", snapshot) if isinstance(snapshot, dict) else {}
            for field_name in descriptor.benchmark.required_snapshot_fields:
                if not isinstance(plan, dict) or field_name not in plan:
                    missing_fields.add(field_name)
        check("snapshot_contract", not missing_fields, "All required snapshot fields are present" if not missing_fields else f"Missing snapshot fields: {', '.join(sorted(missing_fields))}")
        if any(item["status"] == "failed" for item in checks[-4:]):
            evidence_status = "failed"

    if any(item["status"] == "failed" for item in checks):
        status = "failed"
    elif run is None:
        status = "provisional"
    else:
        status = "passed"
    report = {
        "schema_version": "1",
        "runtime_id": descriptor.id,
        "runtime_label": descriptor.label,
        "contract_version": descriptor.contract_version,
        "configuration_schema_version": descriptor.configuration_schema_version,
        "status": status,
        "evidence_status": evidence_status,
        "run_id": run.get("id") if run else None,
        "checks": checks,
    }
    return {**report, "report_checksum": content_checksum(report)}
