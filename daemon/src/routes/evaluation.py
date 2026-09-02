"""Versioned evaluation resources behind the one facade.

Every resource routes through the evaluation facade: sources, drafts,
cases, transformations, publishing, interactions, metrics, scorers,
run plans, datasets, runs, scores, evidence, analyses, gates, and
exports. Reads use dual-read where two generations exist, and the
authority endpoint reports the migration phase, the facade counters,
and the durable fallback evidence.
"""

from __future__ import annotations

from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from auth import require_api_key
from benchmarks import evaluation_migration, evaluation_records, facade, repository
from benchmarks.evaluation_contracts import EvaluationContractError
from config import BMAS_API_KEY

router = APIRouter(prefix="/api/evaluation", tags=["evaluation"])

ID_PATTERN = r"^[a-zA-Z0-9_.:@-]{1,200}$"


class RecordEnvelope(BaseModel):
    """One evaluation record with its optional storage links."""

    model_config = ConfigDict(extra="forbid")
    record: dict[str, Any]
    links: dict[str, str | None] = Field(default_factory=dict)


class DraftPublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(pattern=ID_PATTERN)
    version_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)


class TransformRecipeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: int = Field(ge=0, le=10_000)
    recipe: dict[str, Any]


class MetricTransitionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record: dict[str, Any]


def _facade_error(error: Exception) -> HTTPException:
    if isinstance(error, EvaluationContractError):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, aiosqlite.IntegrityError):
        # A trigger or constraint rejection is a state conflict, for
        # example a write into a frozen published draft.
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, evaluation_records.EvaluationStorageError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, evaluation_migration.MigrationPhaseError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, facade.FacadeCommandError):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(status_code=500, detail="Evaluation storage failed")


async def _run_command(command: str, payload: dict[str, Any]) -> Any:
    try:
        return await facade.execute(command, payload)
    except (
        EvaluationContractError,
        evaluation_records.EvaluationStorageError,
        evaluation_migration.MigrationPhaseError,
        facade.FacadeCommandError,
        aiosqlite.IntegrityError,
    ) as error:
        raise _facade_error(error) from error


# ── Sources ──────────────────────────────────────────────────────────


@router.post("/sources", status_code=201)
async def create_source_endpoint(request: Request, payload: RecordEnvelope):
    require_api_key(request, BMAS_API_KEY)
    return await _run_command("import_source", {"record": payload.record})


@router.get("/sources/{source_id}")
async def get_source_endpoint(source_id: str):
    stored = await evaluation_records.get_record(
        "benchmark-source", source_id,
    )
    if stored is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return stored


# ── Drafts, cases, transformations, and publishing ───────────────────


@router.post("/drafts", status_code=201)
async def create_draft_endpoint(request: Request, payload: RecordEnvelope):
    require_api_key(request, BMAS_API_KEY)
    return await _run_command(
        "create_draft",
        {
            "record": payload.record,
            "source_id": payload.links.get("source_id"),
            "parent_version_id": payload.links.get("parent_version_id"),
        },
    )


@router.get("/drafts/{draft_id}")
async def get_draft_endpoint(draft_id: str):
    stored = await evaluation_records.get_record("dataset-draft", draft_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return stored


@router.post("/drafts/{draft_id}/cases", status_code=201)
async def add_draft_case_endpoint(
    request: Request, draft_id: str, payload: RecordEnvelope,
):
    require_api_key(request, BMAS_API_KEY)
    return await _run_command(
        "add_draft_case",
        {"draft_id": draft_id, "record": payload.record},
    )


@router.post("/drafts/{draft_id}/transformations", status_code=201)
async def add_transformation_endpoint(
    request: Request, draft_id: str, payload: TransformRecipeInput,
):
    require_api_key(request, BMAS_API_KEY)
    recipe_id = await _run_command(
        "add_transform_recipe",
        {
            "draft_id": draft_id,
            "position": payload.position,
            "recipe": payload.recipe,
        },
    )
    return {"id": recipe_id, "draft_id": draft_id}


@router.post("/drafts/{draft_id}/publish")
async def publish_draft_endpoint(
    request: Request, draft_id: str, payload: DraftPublishInput,
):
    require_api_key(request, BMAS_API_KEY)
    return await _run_command(
        "publish_draft",
        {
            "draft_id": draft_id,
            "dataset_id": payload.dataset_id,
            "version_id": payload.version_id,
            "name": payload.name,
            "description": payload.description,
        },
    )


# ── Interactions, metrics, and scorers ───────────────────────────────


@router.post("/interactions", status_code=201)
async def create_interaction_endpoint(
    request: Request, payload: RecordEnvelope,
):
    require_api_key(request, BMAS_API_KEY)
    return await _run_command(
        "register_interaction_spec", {"record": payload.record},
    )


@router.post("/interactions/{spec_id}/publish")
async def publish_interaction_endpoint(request: Request, spec_id: str):
    require_api_key(request, BMAS_API_KEY)
    return await _run_command(
        "publish_interaction_spec", {"record_id": spec_id},
    )


@router.post("/metrics", status_code=201)
async def create_metric_endpoint(request: Request, payload: RecordEnvelope):
    require_api_key(request, BMAS_API_KEY)
    return await _run_command(
        "register_metric_definition", {"record": payload.record},
    )


@router.post("/metrics/{metric_id}/lifecycle")
async def transition_metric_endpoint(
    request: Request, metric_id: str, payload: MetricTransitionInput,
):
    require_api_key(request, BMAS_API_KEY)
    return await _run_command(
        "transition_metric_lifecycle",
        {"record_id": metric_id, "record": payload.record},
    )


@router.get("/metrics/{metric_id}")
async def get_metric_endpoint(metric_id: str):
    stored = await evaluation_records.get_record(
        "metric-definition", metric_id,
    )
    if stored is None:
        raise HTTPException(status_code=404, detail="Metric not found")
    return stored


@router.post("/scorers", status_code=201)
async def create_scorer_endpoint(request: Request, payload: RecordEnvelope):
    require_api_key(request, BMAS_API_KEY)
    return await _run_command(
        "register_scorer_version",
        {
            "record": payload.record,
            "legacy_scorer_id": payload.links.get("legacy_scorer_id"),
        },
    )


@router.post("/scorers/{record_id}/publish")
async def publish_scorer_endpoint(request: Request, record_id: str):
    require_api_key(request, BMAS_API_KEY)
    return await _run_command(
        "publish_scorer_version", {"record_id": record_id},
    )


@router.get("/scorers/{scorer_id}/versions/{version}")
async def read_scorer_endpoint(scorer_id: str, version: str):
    result = await facade.read_scorer_spec(scorer_id, version)
    if result is None:
        raise HTTPException(status_code=404, detail="Scorer not found")
    return result


# ── Run plans, datasets, runs, scores, and evidence ──────────────────


@router.post("/run-plans", status_code=201)
async def create_run_plan_endpoint(
    request: Request, payload: RecordEnvelope,
):
    require_api_key(request, BMAS_API_KEY)
    return await _run_command(
        "create_run_plan",
        {
            "record": payload.record,
            "test_revision_id": payload.links.get("test_revision_id"),
            "run_id": payload.links.get("run_id"),
        },
    )


@router.post("/run-plans/{record_id}/publish")
async def publish_run_plan_endpoint(request: Request, record_id: str):
    require_api_key(request, BMAS_API_KEY)
    return await _run_command("publish_run_plan", {"record_id": record_id})


@router.get("/runs/{run_id}/plan")
async def read_run_plan_endpoint(run_id: str):
    result = await facade.read_run_plan(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return result


@router.get("/datasets/{dataset_id}")
async def read_dataset_endpoint(dataset_id: str, version_id: str | None = None):
    import database as db

    dataset = await db.get_dataset(
        dataset_id, distribution_version_id=version_id,
    )
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return {"source": "legacy", "record": dataset}


@router.get("/runs/{run_id}")
async def read_run_endpoint(run_id: str, generation: str = "current"):
    try:
        run = await facade.read_run(run_id, generation=generation)
    except facade.FacadeCommandError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/runs/{run_id}/scores")
async def read_run_scores_endpoint(run_id: str):
    run = await repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"source": "legacy", "scores": run.get("scores") or []}


@router.get("/attempts/{attempt_id}/evidence")
async def read_attempt_evidence_endpoint(attempt_id: str):
    result = await facade.read_attempt_evidence(attempt_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Attempt not found")
    return result


# ── Analyses, gates, exports, and the authority view ─────────────────


@router.post("/runs/{run_id}/analyses", status_code=201)
async def freeze_analysis_endpoint(request: Request, run_id: str, payload: RecordEnvelope):
    require_api_key(request, BMAS_API_KEY)
    return await _run_command(
        "record_analysis_snapshot",
        {"record": payload.record, "run_id": run_id},
    )


@router.get("/runs/{run_id}/analyses")
async def list_analyses_endpoint(run_id: str):
    import database as db

    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT id, record_checksum, created_at "
            "FROM analysis_snapshots WHERE run_id = ? "
            "ORDER BY created_at, id",
            (run_id,),
        )
    return {"run_id": run_id, "snapshots": [dict(row) for row in rows]}


@router.get("/gates/{gate_evaluation_id}/display-exceptions")
async def read_gate_exceptions_endpoint(gate_evaluation_id: str):
    import database as db

    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT * FROM gate_display_exceptions "
            "WHERE gate_evaluation_id = ? ORDER BY id",
            (gate_evaluation_id,),
        )
        if rows:
            return {
                "source": "current",
                "exceptions": [dict(row) for row in rows],
            }
        cursor = await connection.execute(
            "SELECT display_exceptions FROM benchmark_gate_evaluations "
            "WHERE id = ?",
            (gate_evaluation_id,),
        )
        legacy = await cursor.fetchone()
    if legacy is None:
        raise HTTPException(status_code=404, detail="Gate not found")
    import json

    await evaluation_migration.record_fallback(
        "gate-display-exceptions", gate_evaluation_id,
    )
    return {
        "source": "legacy",
        "exceptions": json.loads(legacy["display_exceptions"] or "[]"),
    }


@router.get("/exports/{run_id}")
async def export_run_endpoint(request: Request, run_id: str):
    require_api_key(request, BMAS_API_KEY)
    try:
        return await evaluation_migration.compatibility_export(run_id)
    except evaluation_migration.MigrationPhaseError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/authority")
async def authority_endpoint():
    snapshot = await evaluation_migration.authority_snapshot()
    return {**snapshot, "facade": facade.metrics_snapshot()}
