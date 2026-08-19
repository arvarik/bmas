"""Run durable benchmark attempts through the shared task admission queue."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

import database as db
from benchmarks import repository
from benchmarks.provenance import content_checksum

logger = logging.getLogger("bmas.benchmarks")
POLL_SECONDS = 1.0
GLOBAL_ACTIVE_LIMIT = min(max(int(os.getenv("BMAS_BENCHMARK_MAX_ACTIVE", "4")), 1), 32)
_scheduler_task: asyncio.Task[None] | None = None


def _age_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        started = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now(UTC) - started).total_seconds())


async def _admit(attempt: dict[str, Any]) -> None:
    from routes.submit import BenchmarkContext, TaskOverrides, TaskSubmission, _admit_task

    arm = attempt.get("arm_configuration") or {}
    overrides = arm.get("submission_overrides") or {}
    request = TaskSubmission(
        task=str(attempt["input"]),
        variant=str(attempt["runtime_id"]),
        overrides=TaskOverrides.model_validate(overrides) if overrides else None,
        benchmark=BenchmarkContext(
            run_id=str(attempt["run_id"]),
            trial_id=str(attempt["trial_id"]),
            attempt_id=str(attempt["id"]),
        ),
    )
    response = await _admit_task(
        request,
        captured_configuration=arm.get("effective_configuration"),
    )
    metadata = await db.get_board_meta(response["task_id"])
    snapshot = {
        "schema_version": "1",
        "benchmark_plan": attempt.get("execution_snapshot") or {},
        "task_execution": metadata.get("execution_snapshot") or {},
    }
    checksum = content_checksum(snapshot)
    await repository.attach_attempt_task(
        str(attempt["id"]), response["task_id"], snapshot, checksum
    )


async def _reconcile() -> None:
    from routes.submit import abort_scheduled_task

    attempts = await repository.active_attempts()
    cancelled_runs: set[str] = set()
    for attempt in attempts:
        run_id = str(attempt["run_id"])
        configuration = attempt.get("test_configuration") or {}
        cost_limit = configuration.get("cost_limit_usd")
        if (
            run_id not in cancelled_runs
            and attempt.get("run_status") in {"queued", "running", "paused"}
            and cost_limit is not None
            and float(attempt.get("run_cost_usd") or 0) >= float(cost_limit)
        ):
            task_ids = await repository.set_run_state(
                run_id, "cancel", cancel_reason="cost_limit"
            )
            for task_id in task_ids:
                await abort_scheduled_task(task_id, "benchmark_cost_limit")
            cancelled_runs.add(run_id)
            continue
        if attempt.get("task_status") in {"completed", "failed"}:
            await repository.finish_attempt_from_task(str(attempt["id"]))
            continue
        timeout = int(configuration.get("timeout_seconds", 3600))
        if attempt.get("task_id") and _age_seconds(attempt.get("started_at")) >= timeout:
            await abort_scheduled_task(str(attempt["task_id"]), "benchmark_timeout")
            await repository.fail_active_attempt(
                str(attempt["id"]), "timeout", f"The attempt exceeded {timeout} seconds"
            )


async def _tick() -> None:
    await _reconcile()
    available = GLOBAL_ACTIVE_LIMIT - await repository.count_active_attempts()
    for _ in range(max(0, available)):
        attempt = await repository.claim_next_attempt()
        if attempt is None:
            break
        try:
            await _admit(attempt)
        except HTTPException as error:
            message = str(error.detail)
            if error.status_code in {429, 503}:
                await repository.release_attempt(str(attempt["id"]), message)
                break
            await repository.fail_unadmitted_attempt(
                str(attempt["id"]), "configuration", message
            )
        except Exception as error:
            logger.exception("Benchmark attempt admission failed")
            await repository.release_attempt(str(attempt["id"]), str(error))
            break


async def _run() -> None:
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Benchmark scheduler tick failed")
        await asyncio.sleep(POLL_SECONDS)


async def start_scheduler() -> None:
    """Recover orphan claims and start one local scheduler loop."""
    global _scheduler_task
    if _scheduler_task is not None:
        return
    recovered = await repository.recover_orphan_attempts()
    if recovered:
        logger.info("Recovered %s benchmark attempt claims", recovered)
    _scheduler_task = asyncio.create_task(_run(), name="benchmark-scheduler")


async def stop_scheduler() -> None:
    """Stop the scheduler without changing durable run state."""
    global _scheduler_task
    if _scheduler_task is None:
        return
    _scheduler_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _scheduler_task
    _scheduler_task = None
