"""Run durable benchmark attempts through the shared task admission queue."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

import database as db
from benchmarks import repository
from benchmarks.capacity import CapacityPolicy
from benchmarks.provenance import content_checksum

logger = logging.getLogger("bmas.benchmarks")
POLL_SECONDS = 1.0
LEASE_SECONDS = min(max(int(os.getenv("BMAS_BENCHMARK_LEASE_SECONDS", "30")), 10), 300)
CAPACITY_POLICY = CapacityPolicy.from_environment()
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
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
        task_id=(
            "task-benchmark-"
            + uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"bmas:benchmark:{attempt['id']}",
            ).hex
        ),
    )
    metadata = await db.get_board_meta(response["task_id"])
    snapshot = {
        "schema_version": "1",
        "benchmark_plan": attempt.get("execution_snapshot") or {},
        "task_execution": metadata.get("execution_snapshot") or {},
    }
    checksum = content_checksum(snapshot)
    await repository.attach_attempt_task(
        str(attempt["id"]),
        response["task_id"],
        snapshot,
        checksum,
        lease_token=str(attempt["lease_token"]),
    )


async def _reconcile() -> None:
    from routes.submit import abort_scheduled_task

    attempts = await repository.active_attempts(WORKER_ID)
    cancelled_runs: set[str] = set()
    for attempt in attempts:
        lease_token = str(attempt.get("lease_token") or "")
        if not lease_token or not await repository.renew_attempt_lease(
            str(attempt["id"]),
            lease_token,
            lease_seconds=LEASE_SECONDS,
        ):
            continue
        if not attempt.get("task_id"):
            try:
                await _admit(attempt)
            except HTTPException as error:
                message = str(error.detail)
                if error.status_code in {429, 503}:
                    await repository.release_attempt(
                        str(attempt["id"]),
                        message,
                        lease_token,
                    )
                    continue
                await repository.fail_unadmitted_attempt(
                    str(attempt["id"]),
                    "configuration",
                    message,
                    lease_token,
                )
            except repository.BenchmarkConflict:
                logger.info(
                    "A newer scheduler fence owns benchmark attempt %s",
                    attempt["id"],
                )
            except Exception as error:
                logger.exception("Benchmark attempt admission failed")
                await repository.release_attempt(
                    str(attempt["id"]),
                    str(error),
                    lease_token,
                )
            continue
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
            await repository.finish_attempt_from_task(
                str(attempt["id"]),
                lease_token,
            )
            continue
        timeout = _attempt_timeout_seconds(configuration)
        if attempt.get("task_id") and _age_seconds(attempt.get("started_at")) >= timeout:
            await abort_scheduled_task(str(attempt["task_id"]), "benchmark_timeout")
            await repository.fail_active_attempt(
                str(attempt["id"]),
                "timeout",
                f"The attempt exceeded {timeout} seconds",
                lease_token,
            )


def _attempt_timeout_seconds(configuration: dict) -> int:
    """Give an attempt at least the runtime's own duration limit.

    A test can raise an arm's `classic.max_duration_s` or select a long
    effort level. The reaper must not abort an attempt that its runtime
    still allows, so the timeout grows to cover the longest arm plus a
    margin for queueing and finalize work.
    """
    timeout = int(configuration.get("timeout_seconds", 3600))
    from core.variants.effort import CLASSIC_EFFORT_PROFILES

    for arm in configuration.get("arms") or []:
        settings = arm.get("configuration") if isinstance(arm, dict) else None
        if not isinstance(settings, dict):
            continue
        duration = None
        classic = settings.get("classic")
        if isinstance(classic, dict) and isinstance(
            classic.get("max_duration_s"), (int, float)
        ):
            duration = int(classic["max_duration_s"])
        effort = settings.get("effort")
        if duration is None and isinstance(effort, str):
            profile = CLASSIC_EFFORT_PROFILES.get(effort.lower()) or {}
            profile_duration = (profile.get("settings") or {}).get("max_duration_s")
            if isinstance(profile_duration, (int, float)):
                duration = int(profile_duration)
        if duration is not None:
            timeout = max(timeout, duration + 300)
    return timeout


async def _tick() -> None:
    await repository.heartbeat_scheduler_worker(WORKER_ID)
    for _ in range(CAPACITY_POLICY.global_limit):
        recovered = await repository.claim_expired_attempt(
            WORKER_ID,
            lease_seconds=LEASE_SECONDS,
        )
        if recovered is None:
            break
    await _reconcile()
    for _ in range(CAPACITY_POLICY.global_limit):
        attempt = await repository.claim_next_attempt(
            WORKER_ID,
            lease_seconds=LEASE_SECONDS,
            capacity_policy=CAPACITY_POLICY,
        )
        if attempt is None:
            break
        try:
            await _admit(attempt)
        except HTTPException as error:
            message = str(error.detail)
            if error.status_code in {429, 503}:
                await repository.release_attempt(
                    str(attempt["id"]),
                    message,
                    str(attempt["lease_token"]),
                )
                break
            await repository.fail_unadmitted_attempt(
                str(attempt["id"]),
                "configuration",
                message,
                str(attempt["lease_token"]),
            )
        except repository.BenchmarkConflict:
            logger.info(
                "A newer scheduler fence owns benchmark attempt %s",
                attempt["id"],
            )
        except Exception as error:
            logger.exception("Benchmark attempt admission failed")
            await repository.release_attempt(
                str(attempt["id"]),
                str(error),
                str(attempt["lease_token"]),
            )
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
    """Register one replica and start its fenced scheduler loop."""
    global _scheduler_task
    if _scheduler_task is not None:
        return
    await repository.register_scheduler_worker(
        WORKER_ID,
        socket.gethostname(),
        os.getpid(),
    )
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
    await repository.stop_scheduler_worker(WORKER_ID)


async def capacity_status() -> dict[str, Any]:
    """Return the current shared scheduler capacity document."""
    return await repository.benchmark_capacity_snapshot(
        CAPACITY_POLICY,
        stale_after_seconds=LEASE_SECONDS * 3,
    )
