"""Benchmark test authoring and run control endpoints."""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from auth import require_api_key
from benchmarks import repository
from benchmarks.provenance import content_checksum
from benchmarks.runtime import BenchmarkRuntimeConfigurationError, prepare_benchmark_arm
from config import BMAS_API_KEY
from core.variants import UnknownVariantError
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


class BenchmarkTestInput(BaseModel):
    """Define one complete immutable test revision."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    dataset_version_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    repetitions: int = Field(default=1, ge=1, le=20)
    seed: int = Field(default=0, ge=0, le=2_147_483_647)
    max_concurrency: int = Field(default=1, ge=1, le=16)
    timeout_seconds: int = Field(default=3600, ge=30, le=86400)
    cost_limit_usd: float | None = Field(default=None, gt=0)
    arms: list[BenchmarkArmInput] = Field(min_length=1, max_length=8)
    scorers: list[BenchmarkScorerInput] = Field(min_length=1, max_length=8)


class BenchmarkRunInput(BaseModel):
    """Provide optional operator context for a new run."""

    model_config = ConfigDict(extra="forbid")
    operator_note: str = Field(default="", max_length=2000)


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
    configuration = {
        "schema_version": "1",
        "repetitions": payload.repetitions,
        "seed": payload.seed,
        "max_concurrency": payload.max_concurrency,
        "timeout_seconds": payload.timeout_seconds,
        "cost_limit_usd": payload.cost_limit_usd,
    }
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
        )
    except (repository.BenchmarkNotFound, repository.BenchmarkConflict) as error:
        raise _repository_error(error) from error
    return {**run, "created": created}


@router.get("/runs")
async def list_runs_endpoint(status: str | None = None, limit: int = 50, offset: int = 0):
    runs, total = await repository.list_runs(status=status, limit=limit, offset=offset)
    return {"runs": runs, "total": total, "limit": min(max(limit, 1), 200), "offset": max(offset, 0)}


@router.get("/runs/{run_id}")
async def get_run_endpoint(run_id: str):
    run = await repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="The benchmark run does not exist")
    return run


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
