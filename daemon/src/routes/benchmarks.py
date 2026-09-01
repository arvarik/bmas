"""Benchmark test authoring and run control endpoints."""

from __future__ import annotations

import csv
import io
import re
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from auth import require_api_key
from benchmarks import records, repository
from benchmarks import scheduler as benchmark_scheduler
from benchmarks.analysis import build_run_report, report_csv_rows, safe_csv_cell
from benchmarks.provenance import content_checksum
from benchmarks.qualification import qualify_runtime
from benchmarks.runtime import BenchmarkRuntimeConfigurationError, prepare_benchmark_arm
from config import BMAS_API_KEY
from core.variants import UnknownVariantError, variant_capabilities
from routes import submit

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])
ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


class BenchmarkArmInput(BaseModel):
    """Define one runtime and configuration for a test revision."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    runtime_id: str = Field(min_length=1, max_length=128)
    configuration: dict[str, Any] = Field(default_factory=dict)


class BenchmarkScorerInput(BaseModel):
    """Select one immutable scorer version."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    configuration: dict[str, Any] = Field(default_factory=dict)
    required: bool = True


class BenchmarkStatisticsInput(BaseModel):
    """Declare the statistical estimand before any admission."""

    model_config = ConfigDict(extra="forbid")
    family_field: str = Field(default="subject", max_length=64)
    family_weights: dict[str, float] = Field(default_factory=dict)
    case_weights: dict[str, float] = Field(default_factory=dict)
    binary_reduction: str = Field(
        default="strict_majority",
        pattern=r"^(strict_majority|all|at_least_k)$",
    )
    at_least_k: int | None = Field(default=None, ge=1, le=20)
    min_family_cases: int = Field(default=5, ge=2, le=100)


class BenchmarkTestInput(BaseModel):
    """Define one complete immutable test revision.

    A monetary limit arrives as one decimal string and parses exactly
    at this trusted boundary. A legacy float still parses, through its
    shortest decimal text, and keeps the conservative rounding.
    """

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    dataset_version_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    repetitions: int = Field(default=1, ge=1, le=20)
    seed: int = Field(default=0, ge=0, le=2_147_483_647)
    max_concurrency: int = Field(default=1, ge=1, le=16)
    timeout_seconds: int = Field(default=3600, ge=30, le=86400)
    cost_limit_usd: str | float | None = Field(default=None)
    attempt_cost_limit_usd: str | float | None = Field(default=None)
    practical_difference: float = Field(default=0.01, ge=0, le=1)
    statistics: BenchmarkStatisticsInput | None = None
    arms: list[BenchmarkArmInput] = Field(min_length=1, max_length=8)
    scorers: list[BenchmarkScorerInput] = Field(min_length=1, max_length=8)


class BenchmarkRunInput(BaseModel):
    """Provide optional operator context for a new run."""

    model_config = ConfigDict(extra="forbid")
    operator_note: str = Field(default="", max_length=2000)
    priority: int = Field(default=0, ge=-100, le=100)


class RegressionRuleInput(BaseModel):
    """Define one exact metric threshold."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    label: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=300)
    operator: Literal["gte", "lte", "max_drop", "max_increase_ratio"]
    value: float
    analysis_method: Literal[
        "point_estimate",
        "lower_confidence_bound",
        "upper_confidence_bound",
        "holm_sign_test",
    ] = "point_estimate"
    direction: Literal["improvement", "reduction"] | None = None
    practical_size: float | None = Field(default=None, ge=0)


class DisplayExceptionInput(BaseModel):
    """Excuse one unavailable secondary display metric, narrowly."""

    model_config = ConfigDict(extra="forbid")
    scope: str = Field(min_length=1, max_length=400)
    author: str = Field(min_length=1, max_length=200)
    expires_at: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)


class BenchmarkBaselineInput(BaseModel):
    """Pin one completed run with immutable regression rules."""

    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    rules: list[RegressionRuleInput] = Field(min_length=1, max_length=100)
    treatment_declaration: list[
        Literal["runtime", "model", "prompt", "configuration"]
    ] = Field(default_factory=list, max_length=4)


class BenchmarkGateInput(BaseModel):
    """Select one candidate run for evaluation."""

    model_config = ConfigDict(extra="forbid")
    candidate_run_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    display_exceptions: list[DisplayExceptionInput] = Field(
        default_factory=list, max_length=20,
    )


class RuntimeQualificationInput(BaseModel):
    """Supply optional completed-run evidence for runtime qualification."""

    model_config = ConfigDict(extra="forbid")
    run_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,128}$")


class HumanReviewInput(BaseModel):
    """Record one immutable human judgment for a completed attempt."""

    model_config = ConfigDict(extra="forbid")
    score: float = Field(ge=0, le=1)
    passed: bool
    note: str = Field(default="", max_length=4000)


def _slug(value: str, fallback: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return clean[:80] or fallback


async def _prepare(payload: BenchmarkTestInput) -> dict[str, Any]:
    scorers = {item["id"]: item for item in await repository.list_scorers()}
    selected_scorer_ids = [item.id for item in payload.scorers]
    if len(selected_scorer_ids) != len(set(selected_scorer_ids)):
        raise HTTPException(status_code=422, detail="Each scorer version must be unique")
    unknown_scorers = [item.id for item in payload.scorers if item.id not in scorers]
    if unknown_scorers:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown scorer versions: {', '.join(unknown_scorers)}",
        )
    prepared_arms: list[dict[str, Any]] = []
    slugs: set[str] = set()
    for index, arm in enumerate(payload.arms):
        slug = _slug(arm.name, f"arm-{index + 1}")
        if slug in slugs:
            raise HTTPException(status_code=422, detail="Each arm name must be unique")
        slugs.add(slug)
        try:
            prepared = await prepare_benchmark_arm(arm.runtime_id, arm.configuration)
        except (BenchmarkRuntimeConfigurationError, UnknownVariantError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        prepared_arms.append({
            "id": f"arm-{uuid.uuid4().hex}",
            "name": arm.name.strip(),
            "slug": slug,
            **prepared,
        })
    from benchmarks import costs
    from core.money import MoneyError

    limits: dict[str, Any] = {}
    for field_name, raw in (
        ("cost_limit_usd", payload.cost_limit_usd),
        ("attempt_cost_limit_usd", payload.attempt_cost_limit_usd),
    ):
        if raw is None:
            continue
        try:
            money, _ = costs.parse_boundary_amount(raw)
        except MoneyError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if money.amount_nanos <= 0:
            raise HTTPException(
                status_code=422,
                detail=f"The {field_name} limit must be positive",
            )
        limits[field_name] = raw
    configuration = {
        "schema_version": "1",
        "repetitions": payload.repetitions,
        "seed": payload.seed,
        "max_concurrency": payload.max_concurrency,
        "timeout_seconds": payload.timeout_seconds,
        "cost_limit_usd": limits.get("cost_limit_usd"),
        "practical_difference": payload.practical_difference,
    }
    if "attempt_cost_limit_usd" in limits:
        configuration["attempt_cost_limit_usd"] = limits[
            "attempt_cost_limit_usd"
        ]
    if payload.statistics is not None:
        configuration["statistics"] = payload.statistics.model_dump(
            exclude_none=True,
        )
    return {
        "configuration": configuration,
        "configuration_checksum": content_checksum(configuration),
        "arms": prepared_arms,
        "scorers": [
            {
                "id": item.id,
                "name": scorers[item.id]["name"],
                "version": scorers[item.id]["version"],
                "configuration": item.configuration,
                "configuration_checksum": content_checksum(item.configuration),
                "required": item.required,
            }
            for item in payload.scorers
        ],
    }


def _repository_error(error: Exception) -> HTTPException:
    if isinstance(error, repository.BenchmarkNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, repository.BenchmarkConflict):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=500, detail="The benchmark request failed")


@router.get("/scorers")
async def list_scorers_endpoint():
    return {"scorers": await repository.list_scorers()}


@router.post("/tests/preflight")
async def preflight_test_endpoint(payload: BenchmarkTestInput):
    prepared = await _prepare(payload)
    item_count = await repository.dataset_version_item_count(payload.dataset_version_id)
    if item_count is None:
        raise HTTPException(status_code=404, detail="The published dataset version does not exist")
    return {
        "valid": True,
        **prepared,
        "dataset_version_id": payload.dataset_version_id,
        "item_count": item_count,
        "total_trials": item_count * len(payload.arms),
        "total_attempts": item_count * len(payload.arms) * payload.repetitions,
    }


@router.get("/tests")
async def list_tests_endpoint(search: str | None = None, limit: int = 50, offset: int = 0):
    tests, total = await repository.list_tests(search=search, limit=limit, offset=offset)
    return {"tests": tests, "total": total, "limit": min(max(limit, 1), 200), "offset": max(offset, 0)}


@router.post("/tests", status_code=201)
async def create_test_endpoint(request: Request, payload: BenchmarkTestInput):
    require_api_key(request, BMAS_API_KEY)
    return await _create_revision(f"test-{uuid.uuid4().hex}", payload)


async def _create_revision(test_id: str, payload: BenchmarkTestInput):
    prepared = await _prepare(payload)
    try:
        return await repository.create_test_revision(
            test_id=test_id,
            revision_id=f"testrev-{uuid.uuid4().hex}",
            name=payload.name.strip(),
            description=payload.description.strip(),
            dataset_version_id=payload.dataset_version_id,
            configuration=prepared["configuration"],
            arms=prepared["arms"],
            scorers=prepared["scorers"],
        )
    except (repository.BenchmarkNotFound, repository.BenchmarkConflict) as error:
        raise _repository_error(error) from error


@router.get("/tests/{test_id}")
async def get_test_endpoint(test_id: str):
    if not ID_PATTERN.fullmatch(test_id):
        raise HTTPException(status_code=422, detail="The test identifier is invalid")
    test = await repository.get_test(test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="The benchmark test does not exist")
    return test


@router.post("/tests/{test_id}/revisions", status_code=201)
async def create_revision_endpoint(request: Request, test_id: str, payload: BenchmarkTestInput):
    require_api_key(request, BMAS_API_KEY)
    if not ID_PATTERN.fullmatch(test_id):
        raise HTTPException(status_code=422, detail="The test identifier is invalid")
    return await _create_revision(test_id, payload)


@router.post("/tests/{test_id}/revisions/{revision_id}/runs", status_code=201)
async def create_run_endpoint(
    request: Request,
    test_id: str,
    revision_id: str,
    payload: BenchmarkRunInput,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
):
    require_api_key(request, BMAS_API_KEY)
    if not ID_PATTERN.fullmatch(test_id) or not ID_PATTERN.fullmatch(revision_id):
        raise HTTPException(status_code=422, detail="The benchmark identifier is invalid")
    try:
        run, created = await repository.create_run(
            run_id=f"run-{uuid.uuid4().hex}",
            revision_id=revision_id,
            test_id=test_id,
            idempotency_key=idempotency_key[:200] if idempotency_key else None,
            operator_note=payload.operator_note,
            priority=payload.priority,
        )
    except (repository.BenchmarkNotFound, repository.BenchmarkConflict) as error:
        raise _repository_error(error) from error
    return {**run, "created": created}


@router.get("/runs")
async def list_runs_endpoint(status: str | None = None, limit: int = 50, offset: int = 0):
    runs, total = await repository.list_runs(status=status, limit=limit, offset=offset)
    return {"runs": runs, "total": total, "limit": min(max(limit, 1), 200), "offset": max(offset, 0)}


@router.get("/capacity")
async def benchmark_capacity_endpoint():
    """Return queue pressure and fenced scheduler ownership."""
    return await benchmark_scheduler.capacity_status()


@router.get("/runs/{run_id}")
async def get_run_endpoint(run_id: str):
    run = await repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="The benchmark run does not exist")
    return run


@router.post("/attempts/{attempt_id}/reviews", status_code=201)
async def create_human_review_endpoint(
    request: Request,
    attempt_id: str,
    payload: HumanReviewInput,
    idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
):
    """Save one retry-safe human review for statistical calibration."""
    require_api_key(request, BMAS_API_KEY)
    if not ID_PATTERN.fullmatch(attempt_id):
        raise HTTPException(status_code=422, detail="The attempt identifier is invalid")
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="X-Idempotency-Key is required")
    try:
        review, created = await repository.create_human_review(
            review_id=f"review-{uuid.uuid4().hex}",
            attempt_id=attempt_id,
            reviewer_id=(operator_id or "operator")[:200],
            score=payload.score,
            passed=payload.passed,
            note=payload.note.strip(),
            idempotency_key=idempotency_key[:200],
        )
        return {**review, "created": created}
    except (repository.BenchmarkNotFound, repository.BenchmarkConflict) as error:
        raise _repository_error(error) from error


def _report_filters(subject: str | None, split: str | None, tag: str | None, scorer_id: str | None) -> dict[str, str]:
    return {
        key: value
        for key, value in {
            "subject": subject,
            "split": split,
            "tag": tag,
            "scorer_id": scorer_id,
        }.items()
        if value
    }


@router.get("/runs/{run_id}/report")
async def get_run_report_endpoint(
    run_id: str,
    subject: str | None = None,
    split: str | None = None,
    tag: str | None = None,
    scorer_id: str | None = None,
):
    run = await repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="The benchmark run does not exist")
    return build_run_report(run, _report_filters(subject, split, tag, scorer_id))


@router.get("/runs/{run_id}/report.csv")
async def export_run_report_endpoint(
    run_id: str,
    subject: str | None = None,
    split: str | None = None,
    tag: str | None = None,
    scorer_id: str | None = None,
):
    run = await repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="The benchmark run does not exist")
    rows = report_csv_rows(run, _report_filters(subject, split, tag, scorer_id))
    output = io.StringIO()
    fieldnames = list(rows[0]) if rows else ["run_id"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(
        {key: safe_csv_cell(value) for key, value in row.items()} for row in rows
    )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="benchmark-{run_id}.csv"'},
    )


@router.get("/baselines")
async def list_baselines_endpoint(test_id: str | None = None):
    return {"baselines": await records.list_baselines(test_id)}


@router.post("/baselines", status_code=201)
async def create_baseline_endpoint(
    request: Request,
    payload: BenchmarkBaselineInput,
    operator_id: str | None = Header(default=None, alias="X-Operator-Id"),
):
    require_api_key(request, BMAS_API_KEY)
    try:
        return await records.create_baseline(
            baseline_id=f"baseline-{uuid.uuid4().hex}",
            run_id=payload.run_id,
            name=payload.name.strip(),
            description=payload.description.strip(),
            rules=[rule.model_dump() for rule in payload.rules],
            created_by=(operator_id or "operator")[:200],
            treatment_declaration=list(payload.treatment_declaration),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (repository.BenchmarkNotFound, repository.BenchmarkConflict) as error:
        raise _repository_error(error) from error


@router.get("/baselines/{baseline_id}")
async def get_baseline_endpoint(baseline_id: str):
    baseline = await records.get_baseline(baseline_id)
    if baseline is None:
        raise HTTPException(status_code=404, detail="The benchmark baseline does not exist")
    return baseline


@router.post("/baselines/{baseline_id}/preview")
async def preview_baseline_endpoint(
    request: Request,
    baseline_id: str,
    payload: BenchmarkGateInput,
):
    """Evaluate one candidate without saving a gate decision."""
    require_api_key(request, BMAS_API_KEY)
    try:
        report = await records.preview_baseline(
            baseline_id,
            payload.candidate_run_id,
            display_exceptions=[
                exception.model_dump()
                for exception in payload.display_exceptions
            ],
        )
        return {"report": report, "saved": False}
    except (repository.BenchmarkNotFound, repository.BenchmarkConflict) as error:
        raise _repository_error(error) from error


@router.post("/baselines/{baseline_id}/evaluate")
async def evaluate_baseline_endpoint(
    request: Request,
    baseline_id: str,
    payload: BenchmarkGateInput,
):
    require_api_key(request, BMAS_API_KEY)
    try:
        evaluation, created = await records.evaluate_baseline(
            baseline_id,
            payload.candidate_run_id,
            display_exceptions=[
                exception.model_dump()
                for exception in payload.display_exceptions
            ],
        )
        return {**evaluation, "created": created}
    except (repository.BenchmarkNotFound, repository.BenchmarkConflict) as error:
        raise _repository_error(error) from error


@router.get("/runtimes")
async def list_runtime_qualifications_endpoint():
    capabilities = variant_capabilities()
    available = {item["id"] for item in capabilities["variants"]}
    return {
        **capabilities,
        "qualifications": await records.list_qualifications(),
        "planned_runtime_ids": [
            runtime_id
            for runtime_id in ("patchboard", "stigmergic")
            if runtime_id not in available
        ],
    }


@router.post("/runtimes/{runtime_id}/qualify")
async def qualify_runtime_endpoint(
    request: Request,
    runtime_id: str,
    payload: RuntimeQualificationInput,
):
    require_api_key(request, BMAS_API_KEY)
    run = None
    if payload.run_id:
        run = await repository.get_run(payload.run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="The qualification run does not exist")
    try:
        report = await qualify_runtime(runtime_id, run)
        qualification, created = await records.save_qualification(report)
        return {**qualification, "created": created}
    except UnknownVariantError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/runs/{run_id}/{action}")
async def update_run_endpoint(
    request: Request,
    run_id: str,
    action: Literal["pause", "resume", "cancel", "retry"],
):
    require_api_key(request, BMAS_API_KEY)
    try:
        if action == "retry":
            count = await repository.retry_failed_attempts(run_id)
            return {"run_id": run_id, "status": "queued", "retried_attempts": count}
        task_ids = await repository.set_run_state(run_id, action)
        if action == "cancel":
            for task_id in task_ids:
                await submit.abort_scheduled_task(task_id, "benchmark_cancelled")
        return {"run_id": run_id, "action": action, "affected_tasks": len(task_ids)}
    except (repository.BenchmarkNotFound, repository.BenchmarkConflict) as error:
        raise _repository_error(error) from error
