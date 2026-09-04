"""Benchmark runner — submits labeled datasets through bMAS and captures results.

Submits each question via POST /submit, polls GET /tasks/{id} until terminal,
captures the full response including cost/token/latency metadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from eval.datasets import EvalItem
from eval.scored_result import ScoredResult

logger = logging.getLogger("bmas.eval.runner")

# Polling configuration
POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 600.0  # 10 minutes max per task


@dataclass
class TaskResult:
    """Raw result from a single bMAS task submission."""

    task_id: str
    status: str  # "completed" | "failed"
    result_summary: str
    duration_ms: int | None
    total_cost_usd: float | None
    total_tokens: int | None
    model_used: str | None
    complexity: str | None
    created_at: str | None
    completed_at: str | None
    error_message: str | None = None
    terminated_by: str | None = None
    rounds: int | None = None
    effective_actions: int | None = None
    variant: str | None = None
    phase: str | None = None
    state_revision: int | None = None
    recovery_count: int | None = None
    variant_metrics: dict[str, object] | None = None


class BenchmarkRunner:
    """Submits a dataset through the bMAS daemon and scores accuracy.

    Usage:
        runner = BenchmarkRunner(daemon_url="http://192.168.4.240:9000")
        results = await runner.run(items, run_id="eval-001")
    """

    def __init__(
        self,
        daemon_url: str,
        concurrency: int = 1,
        timeout_per_task_s: float = POLL_TIMEOUT_S,
        api_key: str | None = None,
        variant: str | None = None,
        scorer: Any = None,
    ):
        self.daemon_url = daemon_url.rstrip("/")
        self.concurrency = concurrency
        self.timeout_per_task_s = timeout_per_task_s
        self.api_key = api_key if api_key is not None else os.getenv("BMAS_API_KEY", "")
        self.variant = variant
        self.http = httpx.AsyncClient(timeout=30.0)
        # ``scorer(dataset, expected, response)`` returns the scored
        # triple; the default scores through the daemon scorer plugins.
        self._scorer = scorer

    async def _score(
        self, dataset: str, expected: str, response: str,
    ) -> tuple[str | None, bool, str]:
        if self._scorer is None:
            from eval.client import EvaluationClient

            self._scorer = EvaluationClient(
                self.daemon_url, api_key=self.api_key,
            ).score_answer
        return await asyncio.to_thread(self._scorer, dataset, expected, response)

    def _mutation_headers(self) -> dict[str, str] | None:
        """Return operator authentication for daemon mutation requests."""
        if not self.api_key:
            return None
        return {"Authorization": f"Bearer {self.api_key}"}

    async def run(
        self,
        items: list[EvalItem],
        run_id: str | None = None,
        results_dir: str | Path = "eval/results",
    ) -> list[ScoredResult]:
        """Run the full benchmark: submit → poll → score.

        Returns a list of ScoredResult, one per item.
        Writes raw results to {results_dir}/{run_id}.jsonl.
        """
        run_id = run_id or f"eval-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        out_dir = Path(results_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_path = out_dir / f"{run_id}.jsonl"

        logger.info(
            "Starting benchmark run %s: %d items, concurrency=%d",
            run_id, len(items), self.concurrency,
        )

        scored: list[ScoredResult | None] = [None] * len(items)
        semaphore = asyncio.Semaphore(self.concurrency)

        async def process_one(index: int, item: EvalItem) -> tuple[int, ScoredResult]:
            async with semaphore:
                try:
                    result = await self._submit_and_score(item)
                except Exception as exc:
                    logger.error("Item %s failed: %s", item.id, exc)
                    result = ScoredResult(
                        id=item.id,
                        question=item.question,
                        expected_answer=item.answer,
                        actual_response=f"ERROR: {exc}",
                        dataset=item.dataset,
                        subject=item.subject,
                        extracted_answer=None,
                        correct=False,
                        score_method="harness_error",
                        status="harness_error",
                        error_message=str(exc),
                    )
                return index, result

        tasks = [
            asyncio.create_task(process_one(index, item))
            for index, item in enumerate(items)
        ]

        # Persist each completed item immediately. A long benchmark can then
        # retain completed work after interruption or process failure.
        with open(raw_path, "w") as f:
            for completed in asyncio.as_completed(tasks):
                index, result = await completed
                scored[index] = result
                f.write(json.dumps(result.to_dict(), default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())

        ordered_scored = [result for result in scored if result is not None]

        logger.info(
            "Benchmark run %s complete: %d/%d correct (%.1f%%)",
            run_id,
            sum(1 for s in ordered_scored if s.correct),
            len(ordered_scored),
            (
                sum(1 for s in ordered_scored if s.correct)
                / len(ordered_scored) * 100
            ) if ordered_scored else 0,
        )
        return ordered_scored

    async def _submit_and_score(self, item: EvalItem) -> ScoredResult:
        """Submit a single item, poll until done, score the result."""
        # Submit
        task_result = await self._submit_task(item.question)

        if task_result.status != "completed":
            return ScoredResult(
                id=item.id,
                question=item.question,
                expected_answer=item.answer,
                actual_response=task_result.result_summary,
                dataset=item.dataset,
                subject=item.subject,
                extracted_answer=None,
                correct=False,
                score_method="task_failed",
                task_id=task_result.task_id,
                duration_ms=task_result.duration_ms,
                cost_usd=task_result.total_cost_usd,
                tokens=task_result.total_tokens,
                model_used=task_result.model_used,
                terminated_by=task_result.terminated_by,
                status=task_result.status,
                error_message=task_result.error_message,
                rounds=task_result.rounds,
                effective_actions=task_result.effective_actions,
                variant=task_result.variant,
                phase=task_result.phase,
                state_revision=task_result.state_revision,
                recovery_count=task_result.recovery_count,
                variant_metrics=task_result.variant_metrics,
            )

        # Score through the daemon scorer plugins.
        extracted, correct, method = await self._score(
            item.dataset, item.answer, task_result.result_summary,
        )

        return ScoredResult(
            id=item.id,
            question=item.question,
            expected_answer=item.answer,
            actual_response=task_result.result_summary,
            dataset=item.dataset,
            subject=item.subject,
            extracted_answer=extracted,
            correct=correct,
            score_method=method,
            task_id=task_result.task_id,
            duration_ms=task_result.duration_ms,
            cost_usd=task_result.total_cost_usd,
            tokens=task_result.total_tokens,
            model_used=task_result.model_used,
            terminated_by=task_result.terminated_by or "solution",
            status=task_result.status,
            error_message=task_result.error_message,
            rounds=task_result.rounds,
            effective_actions=task_result.effective_actions,
            variant=task_result.variant,
            phase=task_result.phase,
            state_revision=task_result.state_revision,
            recovery_count=task_result.recovery_count,
            variant_metrics=task_result.variant_metrics,
        )

    async def _submit_task(self, question: str) -> TaskResult:
        """Submit a question and poll until terminal state."""
        # POST /submit
        submission = {"task": question}
        if self.variant:
            submission["variant"] = self.variant
        resp = await self.http.post(
            f"{self.daemon_url}/submit",
            json=submission,
            headers=self._mutation_headers(),
        )
        resp.raise_for_status()
        task_id = resp.json()["task_id"]
        logger.debug("Submitted task %s", task_id)

        # Poll GET /tasks/{id}
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if elapsed > self.timeout_per_task_s:
                abort_requested = await self._abort_task(task_id)
                abort_note = "" if abort_requested else "; abort request failed"
                return TaskResult(
                    task_id=task_id,
                    status="failed",
                    result_summary="",
                    duration_ms=int(elapsed * 1000),
                    total_cost_usd=None,
                    total_tokens=None,
                    model_used=None,
                    complexity=None,
                    created_at=None,
                    completed_at=None,
                    error_message=(
                        f"Timeout after {self.timeout_per_task_s}s{abort_note}"
                    ),
                    terminated_by="timeout",
                )

            await asyncio.sleep(POLL_INTERVAL_S)

            try:
                poll = await self.http.get(f"{self.daemon_url}/tasks/{task_id}")
                poll.raise_for_status()
                payload = poll.json()
                # GET /tasks/{id} returns {"task": {...}, "sub_tasks": [...]}.
                # Accept the former flat shape for compatibility with older daemons.
                task_data = payload.get("task", payload)
                if not isinstance(task_data, dict):
                    raise ValueError("Task detail response does not contain a task object")
            except Exception as e:
                logger.warning("Poll error for %s: %s", task_id, e)
                continue

            status = task_data.get("status", "pending")
            if status in ("completed", "failed"):
                return TaskResult(
                    task_id=task_id,
                    status=status,
                    result_summary=task_data.get("result_summary", ""),
                    duration_ms=task_data.get("duration_ms"),
                    total_cost_usd=task_data.get("total_cost_usd"),
                    total_tokens=task_data.get("total_tokens"),
                    model_used=task_data.get("model_used"),
                    complexity=task_data.get("complexity"),
                    created_at=task_data.get("created_at"),
                    completed_at=task_data.get("completed_at"),
                    error_message=task_data.get("error_message"),
                    terminated_by=task_data.get("terminated_by"),
                    rounds=task_data.get("rounds_used"),
                    effective_actions=task_data.get("effective_actions"),
                    variant=task_data.get("variant"),
                    phase=task_data.get("phase"),
                    state_revision=task_data.get("state_revision"),
                    recovery_count=task_data.get("resume_count"),
                    variant_metrics=(
                        task_data.get("variant_metrics")
                        if isinstance(task_data.get("variant_metrics"), dict)
                        else None
                    ),
                )

    async def _abort_task(self, task_id: str) -> bool:
        """Request task termination after an evaluation timeout.

        The request is best-effort because the daemon can already be unavailable.
        The endpoint must set the task abort flag and return a successful status.
        """
        try:
            response = await self.http.post(
                f"{self.daemon_url}/api/tasks/{task_id}/abort",
                json={"reason": "evaluation_timeout"},
                headers=self._mutation_headers(),
                timeout=10.0,
            )
            response.raise_for_status()
            logger.warning("Requested abort for timed-out task %s", task_id)
            return True
        except Exception as exc:
            logger.error("Abort request failed for %s: %s", task_id, exc)
            return False

    async def verify_daemon(self) -> dict:
        """Return the authoritative coordination capability document."""
        resp = await self.http.get(f"{self.daemon_url}/capabilities")
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or payload.get("api_version") != "1":
            raise ValueError("Daemon returned an unsupported capability contract")
        variants = payload.get("variants")
        if not isinstance(variants, list):
            raise TypeError("Daemon capability document has no variant list")
        return payload

    async def close(self):
        await self.http.aclose()
