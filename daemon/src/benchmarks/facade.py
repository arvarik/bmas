"""The one version-aware evaluation facade.

Every canonical evaluation mutation, from either contract generation,
routes through one command here to the one writer behind it. Legacy
and current request shapes adapt at this boundary, reads select one
complete record source through dual-read without ever merging
generations, and the facade counts every command, every generation,
and every direct legacy call for the authority metrics.

The legacy ``eval/`` package is a client of the daemon API and this
facade; it never writes a canonical record.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import database as db
from benchmarks import costs, evaluation_migration, evaluation_records, repository
from benchmarks import records as gate_records
from benchmarks.legacy_adapters import (
    attempt_evidence_from_attempt,
    run_plan_from_run,
    scorer_spec_from_scorer,
)

GENERATIONS = ("legacy", "current")


class FacadeCommandError(ValueError):
    """A facade command or its payload is invalid."""


# In-process authority metrics. The durable direct-legacy-call events
# add to these when an undeclared write path is ever observed.
_command_counts: Counter[str] = Counter()
_generation_counts: Counter[str] = Counter()
_fallback_counts: Counter[str] = Counter()


def metrics_snapshot() -> dict[str, Any]:
    """Return the facade authority counters."""
    return {
        "commands": dict(_command_counts),
        "generations": dict(_generation_counts),
        "dual_read_fallbacks": dict(_fallback_counts),
    }


def reset_metrics() -> None:
    """Reset the in-process counters; the durable events remain."""
    _command_counts.clear()
    _generation_counts.clear()
    _fallback_counts.clear()


async def record_direct_legacy_call(entry_point: str) -> None:
    """Record one observed direct legacy write call.

    The declared write paths keep this at zero; the counter exists so
    an undeclared path becomes visible instead of silent.
    """
    await evaluation_migration.record_event(
        "direct_legacy_call", {"entry_point": entry_point},
    )


# ── Commands: one canonical write path per mutation ──────────────────

_LEGACY_COMMANDS = {
    "create_test_revision",
    "create_run",
    "create_human_review",
    "create_baseline",
    "preview_gate",
    "evaluate_gate",
    "save_qualification",
    "set_run_state",
    "retry_failed_attempts",
}

_CURRENT_COMMANDS = {
    "import_source",
    "create_draft",
    "add_draft_case",
    "add_transform_recipe",
    "link_case_asset",
    "publish_draft",
    "register_scorer_version",
    "publish_scorer_version",
    "create_run_plan",
    "publish_run_plan",
    "register_interaction_spec",
    "publish_interaction_spec",
    "register_metric_definition",
    "transition_metric_lifecycle",
    "record_asset_ingestion",
    "record_contamination_rights",
    "transition_asset_state",
    "record_attempt_evidence",
    "record_score",
    "record_analysis_snapshot",
    "record_gate_display_exception",
    "record_cost_settlement_version",
    "record_dispatch_rank_history",
}


async def execute(
    command: str,
    payload: dict[str, Any],
    *,
    generation: str = "current",
) -> Any:
    """Execute one canonical mutation through the one write authority.

    A legacy-shaped request adapts here and reaches the same writer as
    a current-shaped request. No second write path exists.
    """
    if generation not in GENERATIONS:
        raise FacadeCommandError(f"Unknown generation: {generation!r}")
    expected = (
        _LEGACY_COMMANDS if generation == "legacy" else _CURRENT_COMMANDS
    )
    if command not in expected:
        raise FacadeCommandError(
            f"Unknown {generation} facade command: {command!r}"
        )
    _command_counts[command] += 1
    _generation_counts[generation] += 1
    handler = _HANDLERS[command]
    return await handler(dict(payload))


async def _create_test_revision(payload: dict[str, Any]) -> Any:
    return await repository.create_test_revision(**payload)


async def _create_run(payload: dict[str, Any]) -> Any:
    return await repository.create_run(**payload)


async def _create_human_review(payload: dict[str, Any]) -> Any:
    return await repository.create_human_review(**payload)


async def _create_baseline(payload: dict[str, Any]) -> Any:
    return await gate_records.create_baseline(**payload)


async def _preview_gate(payload: dict[str, Any]) -> Any:
    return await gate_records.preview_baseline(
        payload["baseline_id"],
        payload["candidate_run_id"],
        display_exceptions=payload.get("display_exceptions"),
    )


async def _evaluate_gate(payload: dict[str, Any]) -> Any:
    return await gate_records.evaluate_baseline(
        payload["baseline_id"],
        payload["candidate_run_id"],
        display_exceptions=payload.get("display_exceptions"),
    )


async def _save_qualification(payload: dict[str, Any]) -> Any:
    return await gate_records.save_qualification(payload["report"])


async def _set_run_state(payload: dict[str, Any]) -> Any:
    return await repository.set_run_state(
        payload["run_id"],
        payload["action"],
        cancel_reason=payload.get("cancel_reason", "operator_request"),
    )


async def _retry_failed_attempts(payload: dict[str, Any]) -> Any:
    return await repository.retry_failed_attempts(payload["run_id"])


async def _import_source(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_record(payload["record"])


async def _create_draft(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_record(
        payload["record"],
        links={
            "source_id": payload.get("source_id"),
            "parent_version_id": payload.get("parent_version_id"),
        },
    )


async def _add_draft_case(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_record(
        payload["record"], links={"draft_id": payload["draft_id"]},
    )


async def _add_transform_recipe(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_transform_recipe(
        payload["draft_id"], payload["position"], payload["recipe"],
    )


async def _link_case_asset(payload: dict[str, Any]) -> Any:
    return await evaluation_records.link_case_asset(
        payload["draft_id"], payload["case_id"], payload["ingestion_id"],
    )


async def _publish_draft(payload: dict[str, Any]) -> Any:
    return await evaluation_records.publish_draft_with_projection(
        payload["draft_id"],
        dataset_id=payload["dataset_id"],
        version_id=payload["version_id"],
        name=payload["name"],
        description=payload.get("description", ""),
    )


async def _register_scorer_version(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_record(
        payload["record"],
        links={"legacy_scorer_id": payload.get("legacy_scorer_id")},
    )


async def _publish_scorer_version(payload: dict[str, Any]) -> Any:
    await evaluation_records.publish_record(
        "scorer-spec", payload["record_id"],
    )
    return {"record_id": payload["record_id"], "status": "published"}


async def _create_run_plan(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_record(
        payload["record"],
        links={
            "test_revision_id": payload.get("test_revision_id"),
            "run_id": payload.get("run_id"),
        },
    )


async def _publish_run_plan(payload: dict[str, Any]) -> Any:
    await evaluation_records.publish_record(
        "run-plan", payload["record_id"],
    )
    return {"record_id": payload["record_id"], "status": "published"}


async def _register_interaction_spec(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_record(payload["record"])


async def _publish_interaction_spec(payload: dict[str, Any]) -> Any:
    await evaluation_records.publish_record(
        "interaction-spec", payload["record_id"],
    )
    return {"record_id": payload["record_id"], "status": "published"}


async def _register_metric_definition(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_record(payload["record"])


async def _transition_metric_lifecycle(payload: dict[str, Any]) -> Any:
    await evaluation_records.transition_metric_lifecycle(
        payload["record_id"], payload["record"],
    )
    return {"record_id": payload["record_id"]}


async def _record_asset_ingestion(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_record(payload["record"])


async def _record_contamination_rights(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_record(
        payload["record"],
        links={"dataset_version_id": payload["dataset_version_id"]},
    )


async def _transition_asset_state(payload: dict[str, Any]) -> Any:
    await evaluation_records.transition_asset_state(
        payload["record_id"], payload["state"],
    )
    return {"record_id": payload["record_id"], "state": payload["state"]}


async def _record_attempt_evidence(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_record(
        payload["record"], links={"attempt_id": payload["attempt_id"]},
    )


async def _record_score(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_record(
        payload["record"],
        links={
            "attempt_id": payload["attempt_id"],
            "scorer_version_id": payload["scorer_version_id"],
        },
    )


async def _record_analysis_snapshot(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_record(
        payload["record"], links={"run_id": payload["run_id"]},
    )


async def _record_gate_display_exception(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_gate_display_exception(
        payload["gate_evaluation_id"], payload["exception"],
    )


async def _record_cost_settlement_version(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_cost_settlement_version(
        payload["run_id"],
        payload["settlement_version"],
        payload["record"],
    )


async def _record_dispatch_rank_history(payload: dict[str, Any]) -> Any:
    return await evaluation_records.save_dispatch_rank_history(
        payload["attempt_id"],
        payload["eligibility_generation"],
        payload["record"],
    )


_HANDLERS = {
    "create_test_revision": _create_test_revision,
    "create_run": _create_run,
    "create_human_review": _create_human_review,
    "create_baseline": _create_baseline,
    "preview_gate": _preview_gate,
    "evaluate_gate": _evaluate_gate,
    "save_qualification": _save_qualification,
    "set_run_state": _set_run_state,
    "retry_failed_attempts": _retry_failed_attempts,
    "import_source": _import_source,
    "create_draft": _create_draft,
    "add_draft_case": _add_draft_case,
    "add_transform_recipe": _add_transform_recipe,
    "link_case_asset": _link_case_asset,
    "publish_draft": _publish_draft,
    "register_scorer_version": _register_scorer_version,
    "publish_scorer_version": _publish_scorer_version,
    "create_run_plan": _create_run_plan,
    "publish_run_plan": _publish_run_plan,
    "register_interaction_spec": _register_interaction_spec,
    "publish_interaction_spec": _publish_interaction_spec,
    "register_metric_definition": _register_metric_definition,
    "transition_metric_lifecycle": _transition_metric_lifecycle,
    "record_asset_ingestion": _record_asset_ingestion,
    "record_contamination_rights": _record_contamination_rights,
    "transition_asset_state": _transition_asset_state,
    "record_attempt_evidence": _record_attempt_evidence,
    "record_score": _record_score,
    "record_analysis_snapshot": _record_analysis_snapshot,
    "record_gate_display_exception": _record_gate_display_exception,
    "record_cost_settlement_version": _record_cost_settlement_version,
    "record_dispatch_rank_history": _record_dispatch_rank_history,
}

assert set(_HANDLERS) == _LEGACY_COMMANDS | _CURRENT_COMMANDS


# ── Dual reads: one complete source, never a merge ───────────────────


async def _stored_current(
    kind: str, record_id: str,
) -> dict[str, Any] | None:
    return await evaluation_records.get_record(kind, record_id)


async def _dual_read(
    *,
    kind: str,
    record_id: str,
    current_loader: Any,
    legacy_loader: Any,
) -> dict[str, Any] | None:
    """Select one complete record source and record every fallback.

    The current store wins when it holds the record. Otherwise the
    read falls back to one complete legacy record, records the
    fallback durably, and never combines fields from both
    generations.
    """
    current = await current_loader()
    if current is not None:
        return {"source": "current", "record": current}
    legacy = await legacy_loader()
    if legacy is None:
        return None
    _fallback_counts[kind] += 1
    await evaluation_migration.record_fallback(kind, record_id)
    return {"source": "legacy", "record": legacy}


async def read_scorer_spec(
    scorer_id: str, version: str,
) -> dict[str, Any] | None:
    """Dual-read one scorer specification."""

    async def current_loader() -> dict[str, Any] | None:
        stored = await _stored_current(
            "scorer-spec", f"{scorer_id}:{version}",
        )
        return stored["record"] if stored else None

    async def legacy_loader() -> dict[str, Any] | None:
        scorers = {
            scorer["id"]: scorer
            for scorer in await repository.list_scorers()
        }
        row = scorers.get(scorer_id)
        if row is None or str(row.get("version")) != version:
            return None
        return scorer_spec_from_scorer(row)

    return await _dual_read(
        kind="scorer-spec",
        record_id=f"{scorer_id}:{version}",
        current_loader=current_loader,
        legacy_loader=legacy_loader,
    )


async def read_run_plan(run_id: str) -> dict[str, Any] | None:
    """Dual-read the frozen plan of one run."""

    async def current_loader() -> dict[str, Any] | None:
        stored = await _stored_current("run-plan", f"plan-{run_id}")
        return stored["record"] if stored else None

    async def legacy_loader() -> dict[str, Any] | None:
        run = await repository.get_run(run_id)
        if run is None:
            return None
        return run_plan_from_run(run)

    return await _dual_read(
        kind="run-plan",
        record_id=f"plan-{run_id}",
        current_loader=current_loader,
        legacy_loader=legacy_loader,
    )


async def read_attempt_evidence(attempt_id: str) -> dict[str, Any] | None:
    """Dual-read one attempt's evidence bundle."""

    async def current_loader() -> dict[str, Any] | None:
        stored = await _stored_current("attempt-evidence", attempt_id)
        return stored["record"] if stored else None

    async def legacy_loader() -> dict[str, Any] | None:
        row = await _legacy_attempt_row(attempt_id)
        if row is None:
            return None
        return attempt_evidence_from_attempt(
            row,
            run_id=str(row["run_id"]),
            plan_checksum=str(row["execution_plan_checksum"]),
        )

    return await _dual_read(
        kind="attempt-evidence",
        record_id=attempt_id,
        current_loader=current_loader,
        legacy_loader=legacy_loader,
    )


async def _legacy_attempt_row(attempt_id: str) -> dict[str, Any] | None:
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT attempt.*, trial.run_id, trial.dataset_item_id, "
            "item.item_key, arm.runtime_id, "
            "run.execution_plan_checksum, "
            "task.result_summary, task.total_cost_usd, "
            "task.total_tokens, task.duration_ms "
            "FROM benchmark_attempts AS attempt "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "JOIN benchmark_runs AS run ON run.id = trial.run_id "
            "JOIN benchmark_test_arms AS arm ON arm.id = trial.test_arm_id "
            "JOIN dataset_items AS item ON item.id = trial.dataset_item_id "
            "LEFT JOIN tasks AS task ON task.id = attempt.task_id "
            "WHERE attempt.id = ?",
            (attempt_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    result = dict(row)
    result["execution_snapshot"] = json.loads(
        result.get("execution_snapshot") or "{}",
    )
    return result


async def read_run(
    run_id: str, *, generation: str = "current",
) -> dict[str, Any] | None:
    """Read one run through the generation-aware response adapter.

    The legacy response keeps its exact shape. The current response
    annotates the legacy floating-point cost through the compatibility
    adapter as evidence only; the converted value never authorizes a
    reservation or a terminal cost gate.
    """
    if generation not in GENERATIONS:
        raise FacadeCommandError(f"Unknown generation: {generation!r}")
    run = await repository.get_run(run_id)
    if run is None:
        return None
    if generation == "legacy":
        return run
    adapted = dict(run)
    adapted["legacy_cost_evidence"] = costs.legacy_cost_adapter(
        float(run["total_cost_usd"])
        if run.get("total_cost_usd") is not None
        else None,
    )
    return adapted
