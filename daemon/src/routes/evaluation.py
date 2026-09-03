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
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from auth import require_api_key
from benchmarks import (
    asset_ingestion,
    draft_editor,
    evaluation_migration,
    evaluation_records,
    facade,
    repository,
    rights_screening,
    source_adapters,
)
from benchmarks.evaluation_contracts import EvaluationContractError
from benchmarks.import_worker import ImportFetchError
from config import BMAS_API_KEY
from core.url_guard import UrlValidationError

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
    if isinstance(error, source_adapters.TrustPolicyError):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, rights_screening.RightsPolicyError):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, asset_ingestion.AssetIngestionError):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, draft_editor.DraftEditorError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, (
        source_adapters.SourceAdapterError,
        ImportFetchError,
        UrlValidationError,
    )):
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


# ── Draft editor: edits, undo, previews, and governed publishing ─────


class CaseEditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case: dict[str, Any]


class CaseDuplicateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_case_id: str = Field(pattern=ID_PATTERN)


class TransformPreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe: dict[str, Any]
    limit: int = Field(default=10, ge=1, le=100)


class GovernedPublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(pattern=ID_PATTERN)
    version_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    recipe: dict[str, Any] | None = None
    screening_corpus: dict[str, Any] | None = None
    operator_decisions: dict[str, str] = Field(default_factory=dict)
    approved_promotions: list[str] = Field(default_factory=list)


class ScreeningInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[dict[str, Any]]
    corpus: dict[str, Any]


async def _editor_call(operation: Any) -> Any:
    try:
        return await operation
    except (
        draft_editor.DraftEditorError,
        rights_screening.RightsPolicyError,
        EvaluationContractError,
        evaluation_records.EvaluationStorageError,
        facade.FacadeCommandError,
        aiosqlite.IntegrityError,
    ) as error:
        raise _facade_error(error) from error


@router.put("/drafts/{draft_id}/editor/cases")
async def edit_case_endpoint(
    request: Request, draft_id: str, payload: CaseEditInput,
):
    require_api_key(request, BMAS_API_KEY)
    return await _editor_call(
        draft_editor.edit_case(draft_id, payload.case),
    )


@router.delete("/drafts/{draft_id}/editor/cases/{case_id}")
async def delete_case_endpoint(
    request: Request, draft_id: str, case_id: str,
):
    require_api_key(request, BMAS_API_KEY)
    return await _editor_call(
        draft_editor.delete_case(draft_id, case_id),
    )


@router.post("/drafts/{draft_id}/editor/cases/{case_id}/duplicate")
async def duplicate_case_endpoint(
    request: Request, draft_id: str, case_id: str,
    payload: CaseDuplicateInput,
):
    require_api_key(request, BMAS_API_KEY)
    return await _editor_call(
        draft_editor.duplicate_case(
            draft_id, case_id, payload.new_case_id,
        ),
    )


@router.post("/drafts/{draft_id}/editor/undo")
async def undo_endpoint(request: Request, draft_id: str):
    require_api_key(request, BMAS_API_KEY)
    return await _editor_call(draft_editor.undo(draft_id))


@router.post("/drafts/{draft_id}/editor/redo")
async def redo_endpoint(request: Request, draft_id: str):
    require_api_key(request, BMAS_API_KEY)
    return await _editor_call(draft_editor.redo(draft_id))


@router.get("/drafts/{draft_id}/validation")
async def validation_endpoint(draft_id: str):
    issues = await _editor_call(
        draft_editor.validation_issues(draft_id),
    )
    return {"draft_id": draft_id, "issues": issues}


@router.post("/drafts/{draft_id}/preview/transform")
async def transform_preview_endpoint(
    request: Request, draft_id: str, payload: TransformPreviewInput,
):
    require_api_key(request, BMAS_API_KEY)
    try:
        return await draft_editor.transform_preview(
            draft_id, payload.recipe, limit=payload.limit,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=422, detail=str(error),
        ) from error


@router.get("/drafts/{draft_id}/preview/distributions")
async def distribution_preview_endpoint(draft_id: str):
    return await _editor_call(
        draft_editor.distribution_preview(draft_id),
    )


@router.get("/drafts/{draft_id}/difference/{version_id}")
async def version_difference_endpoint(draft_id: str, version_id: str):
    return await _editor_call(
        draft_editor.version_difference(draft_id, version_id),
    )


@router.get("/drafts/{draft_id}/publish-confirmation")
async def publish_confirmation_endpoint(draft_id: str):
    return await _editor_call(
        draft_editor.publish_confirmation(draft_id),
    )


@router.post("/drafts/{draft_id}/publish-governed")
async def publish_governed_endpoint(
    request: Request, draft_id: str, payload: GovernedPublishInput,
):
    require_api_key(request, BMAS_API_KEY)
    return await _editor_call(
        draft_editor.publish_governed(
            draft_id,
            dataset_id=payload.dataset_id,
            version_id=payload.version_id,
            name=payload.name,
            description=payload.description,
            recipe=payload.recipe,
            screening_corpus=payload.screening_corpus,
            operator_decisions=payload.operator_decisions,
            approved_promotions=set(payload.approved_promotions),
        ),
    )


# ── Screening and asset ingestion ────────────────────────────────────


class AssetUploadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_name: str = Field(min_length=1, max_length=200)
    declared_media_type: str = Field(min_length=3, max_length=100)
    content_base64: str = Field(min_length=1, max_length=70_000_000)


@router.post("/screening")
async def screening_endpoint(request: Request, payload: ScreeningInput):
    require_api_key(request, BMAS_API_KEY)
    return rights_screening.screen_cases(payload.cases, payload.corpus)


@router.post("/assets", status_code=201)
async def ingest_asset_endpoint(
    request: Request, payload: AssetUploadInput,
):
    require_api_key(request, BMAS_API_KEY)
    import base64
    import binascii

    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(
            status_code=422, detail="Invalid base64 content",
        ) from error
    outcome = asset_ingestion.ingest_asset(
        original_name=payload.original_name,
        declared_media_type=payload.declared_media_type,
        content=content,
    )
    return await _editor_call(asset_ingestion.store_ingestion(outcome))


@router.get("/assets/{ingestion_id}")
async def get_asset_endpoint(ingestion_id: str):
    stored = await evaluation_records.get_record(
        "asset-ingestion-record", ingestion_id,
    )
    if stored is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return stored


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


# ── Evidence capture, scoring, and environment drivers ───────────────


class EvidenceCaptureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_manifest: dict[str, Any]
    runtime_specification: dict[str, Any]
    case: dict[str, Any]
    trace_events: list[dict[str, Any]] | None = None
    final_output: str | None = None
    final_state: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    resources: dict[str, Any]
    seed_evidence: dict[str, Any]
    ledger_references: dict[str, Any] = Field(default_factory=dict)
    failure_classification: str | None = None
    versions: dict[str, str] = Field(default_factory=dict)


class ScoreAttemptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scorer_id: str = Field(pattern=ID_PATTERN)
    scorer_version: str = Field(min_length=1, max_length=100)
    plugin_type: str = Field(min_length=1, max_length=100)
    configuration: dict[str, Any] = Field(default_factory=dict)
    extra_evidence: dict[str, Any] = Field(default_factory=dict)


@router.post("/attempts/{attempt_id}/evidence", status_code=201)
async def capture_evidence_endpoint(
    request: Request, attempt_id: str, payload: EvidenceCaptureInput,
):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import evidence_capture

    try:
        return await evidence_capture.capture_attempt_evidence(
            attempt_id=attempt_id,
            run_manifest=payload.run_manifest,
            runtime_specification=payload.runtime_specification,
            case=payload.case,
            trace_events=payload.trace_events,
            final_output=payload.final_output,
            final_state=payload.final_state,
            tool_calls=payload.tool_calls,
            resources=payload.resources,
            seed_evidence=payload.seed_evidence,
            ledger_references=payload.ledger_references,
            failure_classification=payload.failure_classification,
            versions=payload.versions,
        )
    except (
        evidence_capture.EvidenceCaptureError,
        EvaluationContractError,
        evaluation_records.EvaluationStorageError,
        facade.FacadeCommandError,
        aiosqlite.IntegrityError,
    ) as error:
        raise _facade_error(error) from error


@router.post("/attempts/{attempt_id}/scores", status_code=201)
async def score_attempt_endpoint(
    request: Request, attempt_id: str, payload: ScoreAttemptInput,
):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import score_execution, scorer_plugins

    try:
        result = await score_execution.score_attempt(
            attempt_id=attempt_id,
            scorer_id=payload.scorer_id,
            scorer_version=payload.scorer_version,
            plugin_type=payload.plugin_type,
            configuration=payload.configuration,
            extra_evidence=payload.extra_evidence,
        )
    except (
        score_execution.ScoreExecutionError,
        scorer_plugins.ScorerPluginError,
    ) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (
        EvaluationContractError,
        evaluation_records.EvaluationStorageError,
        facade.FacadeCommandError,
        aiosqlite.IntegrityError,
    ) as error:
        raise _facade_error(error) from error
    return {key: result[key] for key in (
        "score_id", "status", "terminal_class", "replay_eligible",
        "record_checksum",
    )}


@router.get("/scores/{score_id}")
async def read_score_endpoint(score_id: str):
    stored = await evaluation_records.get_record("score-record", score_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Score not found")
    return stored


@router.get("/environment-drivers")
async def list_environment_drivers_endpoint():
    from benchmarks import environment_drivers

    return {"drivers": environment_drivers.list_drivers()}


# ── Judge calibration, failure taxonomy, and the resource ledger ─────


class CalibrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    judge_id: str = Field(pattern=ID_PATTERN)
    judge_version: str = Field(min_length=1, max_length=100)
    judge_model: str = Field(min_length=1, max_length=200)
    prompt_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    scorer_id: str = Field(pattern=ID_PATTERN)
    scorer_version: str = Field(min_length=1, max_length=100)
    label_set: dict[str, Any]
    judge_outputs: dict[str, Any]
    candidate_models: list[str] = Field(default_factory=list)
    threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    calibrated_at: str = "1970-01-01T00:00:00Z"


class ClassificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    classes: list[dict[str, Any]]
    classifier: str = Field(min_length=1, max_length=200)
    evidence_references: list[str] = Field(default_factory=list)
    classified_at: str = "1970-01-01T00:00:00Z"


class CorrectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    classes: list[dict[str, Any]]
    reviewer: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=4000)
    classified_at: str = "1970-01-01T00:00:00Z"


class ResourceEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record: dict[str, Any]


class ReconciliationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = Field(min_length=3, max_length=3)
    cost_rules: list[dict[str, Any]] = Field(default_factory=list)
    unconditional_successes: int | None = Field(default=None, ge=0)
    late_charge: dict[str, Any] | None = None
    reconciled_at: str = "1970-01-01T00:00:00Z"


async def _governed_call(
    operation: Any, *error_types: type[Exception],
) -> Any:
    try:
        return await operation
    except error_types as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (
        EvaluationContractError,
        evaluation_records.EvaluationStorageError,
        facade.FacadeCommandError,
        aiosqlite.IntegrityError,
    ) as error:
        raise _facade_error(error) from error


@router.post("/judges/calibrations", status_code=201)
async def calibrate_judge_endpoint(
    request: Request, payload: CalibrationInput,
):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import judge_calibration

    try:
        previous = await judge_calibration.latest_calibration(
            payload.judge_id, payload.judge_version,
        )
        record = judge_calibration.calibrate(
            judge_id=payload.judge_id,
            judge_version=payload.judge_version,
            judge_model=payload.judge_model,
            prompt_digest=payload.prompt_digest,
            scorer_id=payload.scorer_id,
            scorer_version=payload.scorer_version,
            label_set=judge_calibration.pinned_label_set(
                str(payload.label_set.get("dataset_id") or "labels"),
                str(payload.label_set.get("version") or "1"),
                list(payload.label_set.get("items") or []),
            ),
            judge_outputs=payload.judge_outputs,
            candidate_models=payload.candidate_models,
            previous=previous,
            threshold=payload.threshold,
            now=payload.calibrated_at,
        )
    except (judge_calibration.JudgeCalibrationError, KeyError,
            EvaluationContractError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return await _governed_call(
        judge_calibration.store_calibration(record),
        judge_calibration.JudgeCalibrationError,
    )


@router.get("/judges/{judge_id}/versions/{judge_version}/calibration")
async def read_calibration_endpoint(judge_id: str, judge_version: str):
    from benchmarks import judge_calibration

    record = await judge_calibration.latest_calibration(
        judge_id, judge_version,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Calibration not found")
    return record


@router.get("/scores/{score_id}/judge-view")
async def judge_view_endpoint(score_id: str):
    from benchmarks import judge_calibration

    stored = await evaluation_records.get_record("score-record", score_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Score not found")
    record = stored["record"]
    calibration = None
    version = record.get("calibration_version")
    if version:
        calibration = await judge_calibration.latest_calibration(
            str(record["scorer"]["scorer_id"]), str(version),
        )
    return judge_calibration.judge_result_view(record, calibration)


@router.get("/failure-taxonomy")
async def failure_taxonomy_endpoint():
    from benchmarks import failure_taxonomy

    return failure_taxonomy.taxonomy_document()


@router.post("/attempts/{attempt_id}/failure-classifications",
             status_code=201)
async def classify_failure_endpoint(
    request: Request, attempt_id: str, payload: ClassificationInput,
):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import failure_taxonomy

    try:
        record = failure_taxonomy.classification_record(
            attempt_id=attempt_id,
            classes=payload.classes,
            source="automatic",
            classifier=payload.classifier,
            evidence_references=payload.evidence_references,
            now=payload.classified_at,
        )
    except (failure_taxonomy.FailureTaxonomyError,
            EvaluationContractError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return await _governed_call(
        failure_taxonomy.record_classification(record),
        failure_taxonomy.FailureTaxonomyError,
    )


@router.post("/failure-classifications/{classification_id}/corrections",
             status_code=201)
async def correct_failure_endpoint(
    request: Request, classification_id: str, payload: CorrectionInput,
):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import failure_taxonomy

    return await _governed_call(
        failure_taxonomy.correct_classification(
            classification_id,
            classes=payload.classes,
            reviewer=payload.reviewer,
            reason=payload.reason,
            now=payload.classified_at,
        ),
        failure_taxonomy.FailureTaxonomyError,
    )


@router.get("/attempts/{attempt_id}/failure-classifications")
async def failure_history_endpoint(attempt_id: str):
    from benchmarks import failure_taxonomy

    return await failure_taxonomy.classification_history(attempt_id)


@router.post("/runs/{run_id}/resource-events", status_code=201)
async def record_resource_event_endpoint(
    request: Request, run_id: str, payload: ResourceEventInput,
):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import resource_ledger

    record = dict(payload.record)
    if (record.get("references") or {}).get("run_id") != run_id:
        raise HTTPException(
            status_code=422,
            detail="The entry references the run in its path",
        )
    try:
        from benchmarks.evaluation_contracts import validate_record

        validate_record(record)
    except EvaluationContractError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return await _governed_call(
        resource_ledger.record_event(record),
        resource_ledger.ResourceLedgerError,
    )


@router.get("/runs/{run_id}/resource-ledger")
async def read_resource_ledger_endpoint(run_id: str, currency: str = "USD"):
    from benchmarks import resource_ledger

    entries = await resource_ledger.list_entries(run_id)
    try:
        summary = resource_ledger.summarize(entries, currency=currency)
    except resource_ledger.ResourceLedgerError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"run_id": run_id, "entries": entries, "summary": summary}


@router.post("/runs/{run_id}/reconciliations", status_code=201)
async def reconcile_run_endpoint(
    request: Request, run_id: str, payload: ReconciliationInput,
):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import resource_ledger

    if payload.late_charge is not None:
        operation = resource_ledger.apply_late_charge(
            run_id,
            currency=payload.currency,
            entry=payload.late_charge,
            cost_rules=payload.cost_rules,
            unconditional_successes=payload.unconditional_successes,
            now=payload.reconciled_at,
        )
    else:
        operation = resource_ledger.reconcile_run(
            run_id,
            currency=payload.currency,
            cost_rules=payload.cost_rules,
            unconditional_successes=payload.unconditional_successes,
            now=payload.reconciled_at,
        )
    return await _governed_call(
        operation, resource_ledger.ResourceLedgerError,
    )


# ── Frozen analysis snapshots, replay, overview, and study checks ───


class FreezeAnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    families: dict[str, list[str]]
    scorer_id: str = Field(pattern=ID_PATTERN)
    master_seed: int = Field(ge=0, le=2**64 - 1)
    comparison_family: dict[str, Any]
    planned_repetitions: int = Field(default=1, ge=1, le=1000)
    family_weights: dict[str, float] = Field(default_factory=dict)
    case_weights: dict[str, float] = Field(default_factory=dict)
    binary_reduction: str = "strict_majority"
    at_least_k: int | None = None
    resample_count: int = Field(default=999, ge=1, le=100_000)
    confidence_level: float = Field(default=0.95, gt=0.0, lt=1.0)


class StudyValidationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_plan: dict[str, Any]
    source: dict[str, Any] | None = None
    holdout_hidden: bool = False
    report: dict[str, Any] | None = None
    cost_includes_retries_and_control_plane: bool = False


@router.post("/runs/{run_id}/analyses/freeze", status_code=201)
async def freeze_frozen_analysis_endpoint(
    request: Request, run_id: str, payload: FreezeAnalysisInput,
):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import frozen_analysis

    try:
        specification = frozen_analysis.freeze_specification(
            families=payload.families,
            scorer_id=payload.scorer_id,
            master_seed=payload.master_seed,
            comparison_family=payload.comparison_family,
            family_weights=payload.family_weights,
            case_weights=payload.case_weights,
            binary_reduction=payload.binary_reduction,
            at_least_k=payload.at_least_k,
            resample_count=payload.resample_count,
            confidence_level=payload.confidence_level,
        )
        stored = await frozen_analysis.freeze_and_store(
            run_id,
            specification=specification,
            planned_repetitions=payload.planned_repetitions,
        )
    except (frozen_analysis.FrozenAnalysisError, KeyError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (
        EvaluationContractError,
        evaluation_records.EvaluationStorageError,
        facade.FacadeCommandError,
        aiosqlite.IntegrityError,
    ) as error:
        raise _facade_error(error) from error
    return {
        "snapshot_id": stored["snapshot_id"],
        "results_digest": stored["record"]["results_digest"],
        "replay": stored["record"]["replay"],
        "report": stored["report"],
    }


@router.post("/runs/{run_id}/analyses/{snapshot_id}/replay")
async def replay_analysis_endpoint(
    request: Request, run_id: str, snapshot_id: str,
):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import frozen_analysis

    stored = await evaluation_records.get_record(
        "analysis-snapshot", snapshot_id,
    )
    if stored is None or str(stored["run_id"]) != run_id:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    run = await repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    record = stored["record"]
    return frozen_analysis.replay(
        record["estimand"], run, record,
        planned_repetitions=int(
            (record.get("filters") or {}).get("planned_repetitions") or 1,
        ),
    )


@router.get("/runs/{run_id}/analyses/{snapshot_id}/overview")
async def analysis_overview_endpoint(run_id: str, snapshot_id: str):
    from benchmarks import analytics_views, frozen_analysis

    stored = await evaluation_records.get_record(
        "analysis-snapshot", snapshot_id,
    )
    if stored is None or str(stored["run_id"]) != run_id:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    run = await repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    record = stored["record"]
    specification = record["estimand"]
    frozen_input = frozen_analysis.freeze_input(
        run, specification, planned_repetitions=1,
    )
    report = frozen_analysis.compute_report(specification, frozen_input)
    return analytics_views.overview(
        report, frozen_input=frozen_input, replay=record.get("replay"),
    )


@router.post("/studies/validate")
async def validate_study_endpoint(
    request: Request, payload: StudyValidationInput,
):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import frozen_analysis

    return frozen_analysis.validate_study(
        run_plan=payload.run_plan,
        source=payload.source,
        holdout_hidden=payload.holdout_hidden,
        report=payload.report,
        cost_includes_retries_and_control_plane=(
            payload.cost_includes_retries_and_control_plane
        ),
    )


# ── Studies, metric lifecycle, interactions, and replay bundles ─────


class StudyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    study_type: str
    name: str = Field(min_length=1, max_length=200)
    base_configuration: dict[str, Any]
    treatment: dict[str, Any]
    invariants: dict[str, Any]
    families: dict[str, list[str]]
    scorer_id: str = Field(pattern=ID_PATTERN)
    master_seed: int = Field(ge=0, le=2**64 - 1)
    comparison_margin: float = Field(ge=0.0)
    per_attempt_cost: dict[str, Any]
    seconds_per_attempt: int = Field(ge=1)
    max_concurrency: int = Field(default=4, ge=1)
    hypothesis: str = "non_inferiority"


class MetricAdvanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str
    now: str = "1970-01-01T00:00:00Z"
    validation_evidence: dict[str, bool] = Field(default_factory=dict)
    reason: str = ""


class InteractionRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scripted_turns: list[dict[str, Any]]
    agent_replies: list[str]
    canaries: list[str] = Field(default_factory=list)


class BundleImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archive_base64: str = Field(min_length=1, max_length=70_000_000)
    approval: dict[str, str] | None = None


@router.post("/studies", status_code=201)
async def author_study_endpoint(request: Request, payload: StudyInput):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import study_authoring
    from benchmarks.costs import money_from_json
    from benchmarks.frozen_analysis import FrozenAnalysisError

    try:
        return study_authoring.author_study(
            study_type=payload.study_type,
            name=payload.name,
            base_configuration=payload.base_configuration,
            treatment=payload.treatment,
            invariants=payload.invariants,
            families=payload.families,
            scorer_id=payload.scorer_id,
            master_seed=payload.master_seed,
            comparison_margin=payload.comparison_margin,
            per_attempt_cost=money_from_json(payload.per_attempt_cost),
            seconds_per_attempt=payload.seconds_per_attempt,
            max_concurrency=payload.max_concurrency,
            hypothesis=payload.hypothesis,
        )
    except (study_authoring.StudyAuthoringError, FrozenAnalysisError,
            ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/metrics/{metric_id}/advance")
async def advance_metric_endpoint(
    request: Request, metric_id: str, payload: MetricAdvanceInput,
):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import metric_registry

    try:
        return await metric_registry.advance(
            metric_id, payload.target, now=payload.now,
            validation_evidence=payload.validation_evidence,
            reason=payload.reason,
        )
    except metric_registry.MetricRegistryError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (
        EvaluationContractError,
        evaluation_records.EvaluationStorageError,
        facade.FacadeCommandError,
        aiosqlite.IntegrityError,
    ) as error:
        raise _facade_error(error) from error


@router.get("/metrics/privacy-definitions")
async def privacy_definitions_endpoint(
    scorer_version: str = "1",
    configuration_digest: str = "0" * 64,
    calibrated_at: str = "2026-01-01T00:00:00Z",
):
    from benchmarks import metric_registry

    try:
        return {"definitions": metric_registry.privacy_metric_definitions(
            scorer_version=scorer_version,
            configuration_digest=configuration_digest,
            calibrated_at=calibrated_at,
        )}
    except (EvaluationContractError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/interactions/{spec_id}/execute")
async def execute_interaction_endpoint(
    request: Request, spec_id: str, payload: InteractionRunInput,
):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import interaction_execution

    stored = await evaluation_records.get_record("interaction-spec", spec_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Interaction not found")
    version = interaction_execution.scripted_simulator_version(
        payload.scripted_turns,
    )
    interaction_execution.register_simulator(version)
    spec = dict(stored["record"])
    pins = version.pins()
    spec["simulator"] = {
        key: pins[key]
        for key in ("implementation_id", "prompt_digest", "model",
                    "image_digest", "dependency_digest")
    }
    replies = iter(payload.agent_replies)

    def scripted_agent(message: Any, allowed: Any) -> dict[str, Any]:
        return {"content": next(replies, "")}

    try:
        return interaction_execution.execute_interaction(
            spec, agent=scripted_agent, canaries=payload.canaries,
        )
    except (interaction_execution.InteractionError,
            EvaluationContractError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/runs/{run_id}/replay-bundles")
async def export_replay_bundle_endpoint(
    request: Request, run_id: str, policy: str = "redacted",
    snapshot_id: str | None = None,
):
    require_api_key(request, BMAS_API_KEY)
    import base64

    from benchmarks import replay_bundle

    try:
        built = await replay_bundle.export_run_bundle(
            run_id, policy=policy, snapshot_id=snapshot_id,
        )
    except replay_bundle.ReplayBundleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "manifest": built["manifest"],
        "manifest_digest": built["manifest_digest"],
        "bundle_digest": built["bundle_digest"],
        "member_count": built["member_count"],
        "archive_base64": base64.b64encode(built["archive"]).decode("ascii"),
    }


@router.post("/replay-bundles/import")
async def import_replay_bundle_endpoint(
    request: Request, payload: BundleImportInput,
):
    require_api_key(request, BMAS_API_KEY)
    import base64
    import binascii

    from benchmarks import replay_bundle

    try:
        archive = base64.b64decode(payload.archive_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(
            status_code=422, detail="Invalid base64 archive",
        ) from error
    try:
        imported = replay_bundle.import_bundle(archive)
        result: dict[str, Any] = {
            "import_id": imported.import_id,
            "ingestion_state": imported.ingestion_state,
            "quarantined_members": imported.quarantined_members,
            "stripped_fields": imported.stripped_fields,
            "readable_members": sorted(imported.records),
            "replay_approved": False,
            "execution_repeat": replay_bundle.execution_repeat_requirements(
                imported,
            ),
        }
        if payload.approval:
            replay_bundle.approve_replay(
                imported,
                actor=str(payload.approval.get("actor") or ""),
                policy_version=str(payload.approval.get("policy_version")
                                   or ""),
            )
            replayed = replay_bundle.replay_from_bundle(imported)
            result["replay_approved"] = True
            result["replay"] = {
                key: replayed[key] for key in (
                    "analysis_replayable", "claim", "results_digest",
                    "expected_results_digest", "execution_repeat",
                )
            }
    except replay_bundle.ReplayBundleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return result


# ── Legacy consolidation: client fallbacks, previews, gates ─────────


class LegacyFallbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_point: str = Field(min_length=1, max_length=200)


class ScorePreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_type: str = Field(min_length=1, max_length=100)
    evidence: dict[str, Any]
    configuration: dict[str, Any] = Field(default_factory=dict)


class LegacySummaryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: dict[str, Any]


class FallbackGateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_start: str = Field(min_length=1, max_length=64)
    actor: str = Field(min_length=1, max_length=200)
    threshold: int = Field(default=0, ge=0)


@router.post("/legacy-fallbacks", status_code=201)
async def record_legacy_fallback_endpoint(
    request: Request, payload: LegacyFallbackInput,
):
    require_api_key(request, BMAS_API_KEY)
    await facade.record_direct_legacy_call(payload.entry_point)
    return {"entry_point": payload.entry_point, "recorded": True}


@router.post("/scorers/preview")
async def score_preview_endpoint(request: Request, payload: ScorePreviewInput):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import score_execution, scorer_plugins

    try:
        plugin = scorer_plugins.plugin_for(payload.plugin_type)
        outcome = score_execution.run_deterministic_in_boundary(
            plugin=plugin, evidence=payload.evidence,
            configuration=payload.configuration,
        )
    except scorer_plugins.ScorerPluginError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    import json as json_module

    result = (
        json_module.loads(outcome["canonical_output"])
        if outcome["canonical_output"] else None
    )
    return {
        "terminal_class": outcome["terminal_class"],
        "result": result,
        "result_digest": outcome["result_digest"],
        "persisted": False,
    }


@router.post("/legacy-results")
async def migrate_legacy_result_endpoint(
    request: Request, payload: LegacySummaryInput,
):
    require_api_key(request, BMAS_API_KEY)
    from benchmarks import legacy_adapters

    try:
        migrated = legacy_adapters.migrate_legacy_result_summary(
            payload.summary,
        )
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await evaluation_migration.record_event(
        "backfill_run",
        {"target": "legacy_result_files",
         "legacy_run_id": migrated["legacy_run_id"],
         "record_digest": migrated["record_digest"],
         "unavailable_fields": migrated["unavailable_fields"]},
    )
    return migrated


@router.get("/removal-gates")
async def removal_gates_endpoint():
    return await evaluation_migration.removal_gate_evidence()


@router.post("/removal-gates/fallback")
async def measure_fallback_gate_endpoint(
    request: Request, payload: FallbackGateInput,
):
    require_api_key(request, BMAS_API_KEY)
    return await evaluation_migration.record_measured_fallback_gate(
        window_start=payload.window_start, actor=payload.actor,
        threshold=payload.threshold,
    )


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


# ── Source adapters ──────────────────────────────────────────────────


class AdapterRequest(BaseModel):
    """One adapter operation request with its selection controls."""

    model_config = ConfigDict(extra="forbid")
    request: dict[str, Any]
    configuration: str | None = None
    split: str | None = None
    limit: int = Field(default=10, ge=1, le=100)
    row_limit: int | None = Field(default=None, ge=1, le=100_000)


class CapabilityPromotionInput(BaseModel):
    """One operator decision that lifts one reviewable restriction."""

    model_config = ConfigDict(extra="forbid")
    trust_level: str
    restriction_name: str
    evidence: str = Field(min_length=1, max_length=4000)


def _resolution_view(resolution: Any) -> dict[str, Any]:
    """Return one resolution without its transport payloads."""
    return {
        "adapter_id": resolution.adapter_id,
        "adapter_version": resolution.adapter_version,
        "source_type": resolution.source_type,
        "locator": resolution.locator,
        "pinned_revision": resolution.pinned_revision,
        "trust_level": resolution.trust_level,
        "trust_policy_version": resolution.trust_policy_version,
    }


async def _adapter_call(operation: Any) -> Any:
    try:
        return await operation
    except (
        source_adapters.SourceAdapterError,
        ImportFetchError,
        UrlValidationError,
        EvaluationContractError,
        evaluation_records.EvaluationStorageError,
    ) as error:
        raise _facade_error(error) from error


@router.get("/adapters")
async def list_adapters_endpoint():
    return {"adapters": source_adapters.list_adapters()}


@router.post("/adapters/{adapter_id}/resolve")
async def resolve_source_endpoint(
    request: Request, adapter_id: str, payload: AdapterRequest,
):
    require_api_key(request, BMAS_API_KEY)
    try:
        adapter = source_adapters.get_adapter(adapter_id)
    except source_adapters.SourceAdapterError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    resolution = await _adapter_call(adapter.resolve(payload.request))
    return _resolution_view(resolution)


@router.post("/adapters/{adapter_id}/preview")
async def preview_source_endpoint(
    request: Request, adapter_id: str, payload: AdapterRequest,
):
    require_api_key(request, BMAS_API_KEY)
    try:
        adapter = source_adapters.get_adapter(adapter_id)
    except source_adapters.SourceAdapterError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    resolution = await _adapter_call(adapter.resolve(payload.request))
    options = await _adapter_call(adapter.list_options(resolution))
    preview = await _adapter_call(
        adapter.preview(
            resolution,
            configuration=payload.configuration,
            split=payload.split,
            limit=payload.limit,
        ),
    )
    return {
        "resolution": _resolution_view(resolution),
        "options": options,
        "preview": preview,
    }


@router.post("/adapters/{adapter_id}/import", status_code=201)
async def import_source_endpoint(
    request: Request,
    adapter_id: str,
    payload: AdapterRequest,
    operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
):
    require_api_key(request, BMAS_API_KEY)
    return await _adapter_call(
        source_adapters.import_through_registry(
            adapter_id,
            payload.request,
            configuration=payload.configuration,
            split=payload.split,
            row_limit=payload.row_limit,
            imported_by=(operator_id or "operator")[:200],
        ),
    )


@router.post("/capability-promotions")
async def promote_capability_endpoint(
    request: Request,
    payload: CapabilityPromotionInput,
    operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
):
    """Lift one reviewable restriction through one operator action.

    The authenticated operator identity is the authority. Dataset
    text or an agent request never reaches this decision, a hard
    restriction never lifts, and the promoted profile applies only to
    a new version.
    """
    require_api_key(request, BMAS_API_KEY)
    try:
        profile = source_adapters.capability_profile_for(
            payload.trust_level,
        )
        promoted = source_adapters.authorize_capability_increase(
            profile,
            payload.restriction_name,
            operator_id=(operator_id or "").strip(),
            evidence=payload.evidence,
        )
    except source_adapters.TrustPolicyError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    from benchmarks.provenance import content_checksum

    decision = {
        "trust_level": payload.trust_level,
        "restriction_name": payload.restriction_name,
        "actor": operator_id,
        "evidence": payload.evidence,
        "prior_profile": profile,
        "promoted_profile": promoted,
    }
    return {**decision, "decision_digest": content_checksum(decision)}
