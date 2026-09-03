"""Execute scorers against immutable evidence and store score records.

Every execution starts from one stored immutable evidence bundle and
one pinned published scorer specification. Deterministic scorers run
through the documented WASI boundary with every pin recorded, so
equal evidence and configuration produce equal canonical bytes and
digests on every supported host. The stored score record carries the
boundary policy and runtime digests, and the database enforces that
every score references one evidence bundle and one scorer version.
"""

from __future__ import annotations

import hashlib
import inspect
import uuid
from typing import Any

from benchmarks import scorer_plugins, scorer_sandbox
from benchmarks.provenance import content_checksum

# The output schema every boundary scorer result validates against.
SCORER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "dimensions", "passed", "explanation"],
    "properties": {
        "status": {"enum": ["scored", "unavailable", "error"]},
        "dimensions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": ["number", "string", "null"]},
                    "category": {"type": ["string", "null"]},
                },
            },
        },
        "passed": {"type": ["boolean", "null"]},
        "explanation": {"type": "string", "maxLength": 20000},
        # The canonical NaN marker is the one permitted string: NaN
        # payloads canonicalize before serialization, so two payloads
        # always render the same bytes.
        "uncertainty": {
            "anyOf": [
                {"type": ["number", "null"]},
                {"const": "nan:canonical"},
            ],
        },
        "missing_evidence": {
            "type": "array", "items": {"type": "string"},
        },
        "evidence_marks": {"type": "object"},
        "judge": {"type": "object"},
        "error": {"type": ["string", "null"]},
    },
}


class ScoreExecutionError(ValueError):
    """The execution request violates the scoring contract."""


# The boundary each plugin type records. A repository-reviewed plugin
# runs in-process inside the deterministic boundary as one trusted
# service; a component runs inside Wasmtime; a native scorer runs
# inside the pinned microVM.
BOUNDARY_FOR_PLUGIN = {
    "wasi_component": "wasi_component",
    "native_microvm": "native_microvm",
}
DEFAULT_BOUNDARY = "trusted_service"


def boundary_for(plugin: Any) -> str:
    """Name the isolation boundary one plugin executes inside."""
    return BOUNDARY_FOR_PLUGIN.get(
        str(getattr(plugin, "plugin_type", "")), DEFAULT_BOUNDARY,
    )


def _component_for(plugin: Any) -> dict[str, Any]:
    """Pin one reviewed plugin as a boundary component manifest.

    The component digest derives from the exact plugin source, so a
    changed implementation changes every recorded pin.
    """
    source = inspect.getsource(type(plugin))
    return {
        "component_digest": hashlib.sha256(
            source.encode("utf-8"),
        ).hexdigest(),
        "imports": list(scorer_sandbox.GRANTED_INTERFACES),
    }


def boundary_policy_for(
    plugin: Any, configuration: dict[str, Any],
) -> scorer_sandbox.WasiScorerPolicy:
    """Build the pinned boundary policy for one plugin execution."""
    component = _component_for(plugin)
    return scorer_sandbox.WasiScorerPolicy(
        component_digest=component["component_digest"],
        wit_digest=content_checksum({
            "wit": "bmas:scorer", "interfaces":
            list(scorer_sandbox.GRANTED_INTERFACES),
        }),
        wasi_version="preview-2",
        compiler_digest=content_checksum({
            "compiler": "bmas-reviewed-plugin", "generation": 1,
        }),
        dependency_lock_digest=content_checksum({
            "dependencies": [], "lock": "reviewed-in-repository",
        }),
        output_schema=SCORER_OUTPUT_SCHEMA,
        random_seed=int(configuration.get("seed", 0)),
    )


def run_deterministic_in_boundary(
    *,
    plugin: Any,
    evidence: dict[str, Any],
    configuration: dict[str, Any],
    clock: Any = None,
) -> dict[str, Any]:
    """Run one deterministic plugin inside the documented boundary."""
    policy = boundary_policy_for(plugin, configuration)
    component = {
        "component_digest": policy.component_digest,
        "imports": list(scorer_sandbox.GRANTED_INTERFACES),
    }

    def guest(host: Any) -> None:
        bundle = host.read_input()
        host.consume_fuel(len(str(bundle)))
        result = plugin.score(
            bundle["evidence"], bundle["configuration"],
        )
        host.write_result(result)

    return scorer_sandbox.execute_component(
        component=component,
        policy=policy,
        evidence_input={
            "evidence": evidence,
            "configuration": configuration,
        },
        guest=guest,
        clock=clock,
    )


def component_policy_for(
    artifact: Any, configuration: dict[str, Any],
) -> scorer_sandbox.WasiScorerPolicy:
    """Build the pinned policy for one compiled component."""
    from benchmarks import sandbox_backends

    limits = dict(configuration.get("limits") or {})
    return scorer_sandbox.WasiScorerPolicy(
        component_digest=artifact.digest,
        wit_digest=sandbox_backends.wit_digest(),
        wasi_version="preview-2",
        compiler_digest=str(
            configuration.get("compiler_digest")
            or content_checksum({"compiler": "wasm-component-text", "generation": 1})
        ),
        dependency_lock_digest=str(
            configuration.get("dependency_lock_digest")
            or content_checksum({"dependencies": [], "lock": "component-only"})
        ),
        output_schema=SCORER_OUTPUT_SCHEMA,
        fuel_limit=int(limits.get("fuel_limit", 1_000_000)),
        memory_limit_bytes=int(limits.get("memory_limit_bytes", 16_777_216)),
        table_limit_entries=int(limits.get("table_limit_entries", 1_024)),
        output_limit_bytes=int(limits.get("output_limit_bytes", 65_536)),
        wall_time_limit_seconds=float(
            limits.get("wall_time_limit_seconds", 30.0),
        ),
        random_seed=int(configuration.get("seed", 0)),
    )


def run_component_in_wasmtime(
    *,
    plugin: Any,
    evidence: dict[str, Any],
    configuration: dict[str, Any],
) -> dict[str, Any]:
    """Run one compiled component inside the pinned Wasmtime runtime."""
    from benchmarks import sandbox_backends

    policy = component_policy_for(plugin.component, configuration)
    runner = sandbox_backends.WasmtimeComponentRunner()
    return runner.execute(
        artifact=plugin.component,
        policy=policy,
        evidence_input={"evidence": evidence, "configuration": configuration},
    )


def run_native_in_microvm(
    *,
    plugin: Any,
    evidence: dict[str, Any],
    configuration: dict[str, Any],
) -> dict[str, Any]:
    """Run one native scorer inside the pinned microVM boundary."""
    return plugin.runner.execute(
        evidence_input={"evidence": evidence, "configuration": configuration},
        request_token=str(configuration.get("request_token") or ""),
        output_schema=SCORER_OUTPUT_SCHEMA,
    )


def run_in_boundary(
    *,
    plugin: Any,
    evidence: dict[str, Any],
    configuration: dict[str, Any],
    clock: Any = None,
) -> dict[str, Any]:
    """Dispatch one plugin to the boundary its type declares."""
    plugin_type = str(getattr(plugin, "plugin_type", ""))
    if plugin_type == "wasi_component":
        return run_component_in_wasmtime(
            plugin=plugin, evidence=evidence, configuration=configuration,
        )
    if plugin_type == "native_microvm":
        return run_native_in_microvm(
            plugin=plugin, evidence=evidence, configuration=configuration,
        )
    return run_deterministic_in_boundary(
        plugin=plugin, evidence=evidence, configuration=configuration,
        clock=clock,
    )


def _record_from_outcome(
    *,
    score_id: str,
    scorer_id: str,
    scorer_version: str,
    configuration: dict[str, Any],
    attempt_id: str,
    outcome: dict[str, Any],
    boundary: str,
) -> dict[str, Any]:
    import json

    pins = outcome["pins"]
    if outcome["terminal_class"] == "completed":
        result = json.loads(outcome["canonical_output"])
        status = "scored" if result["status"] == "scored" else "error"
        dimensions = [
            {
                "name": str(dimension["name"]),
                "value": (
                    dimension.get("value")
                    if isinstance(dimension.get("value"), (int, float))
                    else None
                ),
                "category": dimension.get("category"),
            }
            for dimension in result["dimensions"]
        ] or [
            {"name": "unavailable", "value": None, "category": None},
        ]
        passed = result["passed"]
        explanation = result["explanation"]
        uncertainty = result.get("uncertainty")
        if not isinstance(uncertainty, (int, float)):
            uncertainty = None
        error = result.get("error") or (
            result["explanation"]
            if result["status"] != "scored" else None
        )
        judge = result.get("judge")
    else:
        status = "error"
        dimensions = [
            {"name": "terminal_class", "value": None,
             "category": outcome["terminal_class"]},
        ]
        passed = None
        explanation = outcome["terminal_class"]
        uncertainty = None
        error = outcome["error"]
        judge = None

    record: dict[str, Any] = {
        "schema_id": "score-record",
        "schema_version": 2,
        "score_id": score_id,
        "scorer": {
            "scorer_id": scorer_id,
            "version": scorer_version,
            "configuration_digest": content_checksum(configuration),
        },
        "evidence_references": [attempt_id],
        "dimensions": dimensions,
        "passed": passed,
        "explanation": str(explanation)[:20_000],
        "uncertainty": uncertainty,
        "sandbox": {
            "boundary": boundary,
            "policy_digest": pins["policy_digest"],
            "runtime_digest": pins["runtime_digest"],
            "component_digest": pins["component_digest"],
            "wit_digest": pins["wit_digest"],
            "compiler_digest": pins["compiler_digest"],
            "dependency_lock_digest": pins["dependency_lock_digest"],
            "output_schema_digest": pins["output_schema_digest"],
            "terminal_class": outcome["terminal_class"],
            "replay_eligible": outcome["replay_eligible"],
            "fuel_used": outcome["resources"]["fuel_used"],
        },
        "status": status,
        "error": str(error)[:20_000] if error else None,
    }
    if judge:
        record["judge"] = judge
    return record


async def score_attempt(
    *,
    attempt_id: str,
    scorer_id: str,
    scorer_version: str,
    plugin_type: str,
    configuration: dict[str, Any] | None = None,
    extra_evidence: dict[str, Any] | None = None,
    judge: Any = None,
    clock: Any = None,
    component: Any = None,
    microvm: Any = None,
) -> dict[str, Any]:
    """Score one stored evidence bundle and persist the record.

    The evidence bundle and the published scorer version must already
    exist; the stored score references both through enforced links.
    """
    from benchmarks import evaluation_records, facade

    stored = await evaluation_records.get_record(
        "attempt-evidence", attempt_id,
    )
    if stored is None:
        raise ScoreExecutionError(
            f"No immutable evidence exists for attempt {attempt_id}"
        )
    scorer_version_id = f"{scorer_id}:{scorer_version}"
    scorer_row = await evaluation_records.get_record(
        "scorer-spec", scorer_version_id,
    )
    if scorer_row is None:
        raise ScoreExecutionError(
            f"No pinned scorer specification exists for "
            f"{scorer_version_id}"
        )

    bundle = stored["record"]
    evidence = {
        "attempt_id": attempt_id,
        "trace_digest": bundle.get("trace_digest"),
        "final_output_digest": bundle.get("final_output_digest"),
        **(extra_evidence or {}),
    }
    configuration = dict(configuration or {})
    if judge is None and plugin_type == "rubric_judge":
        from benchmarks import model_backed

        judge = model_backed.judge_for(configuration)
    plugin = scorer_plugins.plugin_for(
        plugin_type, judge=judge, component=component, microvm=microvm,
    )
    if plugin_type == "rubric_judge":
        # A judge transport may block on the network; the boundary
        # runs on a worker thread so the event loop stays responsive.
        import asyncio

        outcome = await asyncio.to_thread(
            run_in_boundary, plugin=plugin, evidence=evidence,
            configuration=configuration, clock=clock,
        )
    else:
        outcome = run_in_boundary(
            plugin=plugin,
            evidence=evidence,
            configuration=configuration,
            clock=clock,
        )
    record = _record_from_outcome(
        score_id=f"score-{uuid.uuid4().hex}",
        scorer_id=scorer_id,
        scorer_version=scorer_version,
        configuration=configuration,
        attempt_id=attempt_id,
        outcome=outcome,
        boundary=boundary_for(plugin),
    )
    saved = await facade.execute(
        "record_score",
        {
            "record": record,
            "attempt_id": attempt_id,
            "scorer_version_id": scorer_version_id,
        },
    )
    from benchmarks import resource_ledger

    await resource_ledger.emit_scorer_execution(
        attempt_id=attempt_id, scorer_id=scorer_id, outcome=outcome,
        boundary=boundary_for(plugin),
    )
    if record.get("judge"):
        await resource_ledger.emit_judge_usage(
            attempt_id=attempt_id, scorer_id=scorer_id,
            judge=record["judge"],
        )
    return {
        "score_id": record["score_id"],
        "status": record["status"],
        "terminal_class": outcome["terminal_class"],
        "replay_eligible": outcome["replay_eligible"],
        "record_checksum": saved["record_checksum"],
        "record": record,
        "outcome": outcome,
    }
