"""Benchmark runner — submits labeled datasets through bMAS and captures results.

Submits each question via POST /submit, polls GET /tasks/{id} until terminal,
captures the full response including cost/token/latency metadata.

See docs/proposals/10-migration-and-rollout.md Phase E bullet 1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from eval.datasets import EvalItem
from eval.scorer import ScoredResult, score_gsm8k, score_mmlu

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
    termination_reason: str | None = None


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
    ):
        self.daemon_url = daemon_url.rstrip("/")
        self.concurrency = concurrency
        self.timeout_per_task_s = timeout_per_task_s
        self.http = httpx.AsyncClient(timeout=30.0)

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

        scored: list[ScoredResult] = []
        semaphore = asyncio.Semaphore(self.concurrency)

        async def process_one(item: EvalItem) -> ScoredResult:
            async with semaphore:
                return await self._submit_and_score(item)

        tasks = [process_one(item) for item in items]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        with open(raw_path, "w") as f:
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error("Item %s failed: %s", items[i].id, result)
                    sr = ScoredResult(
                        id=items[i].id,
                        question=items[i].question,
                        expected_answer=items[i].answer,
                        actual_response=f"ERROR: {result}",
                        dataset=items[i].dataset,
                        subject=items[i].subject,
                        extracted_answer=None,
                        correct=False,
                        score_method="error",
                    )
                else:
                    sr = result
                scored.append(sr)
                f.write(json.dumps(sr.to_dict(), default=str) + "\n")

        logger.info(
            "Benchmark run %s complete: %d/%d correct (%.1f%%)",
            run_id,
            sum(1 for s in scored if s.correct),
            len(scored),
            (sum(1 for s in scored if s.correct) / len(scored) * 100) if scored else 0,
        )
        return scored

    async def _submit_and_score(self, item: EvalItem) -> ScoredResult:
        """Submit a single item, poll until done, score the result."""
        # Submit
        task_result = await self._submit_task(item.question)

        # Score
        if item.dataset == "gsm8k":
            extracted, correct, method = score_gsm8k(
                item.answer, task_result.result_summary
            )
        elif item.dataset == "mmlu":
            extracted, correct, method = score_mmlu(
                item.answer, task_result.result_summary
            )
        else:
            extracted, correct, method = None, False, "unknown_dataset"

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
            terminated_by=task_result.termination_reason or "solution",
        )

    async def _submit_task(self, question: str) -> TaskResult:
        """Submit a question and poll until terminal state."""
        # POST /submit
        resp = await self.http.post(
            f"{self.daemon_url}/submit",
            json={"task": question},
        )
        resp.raise_for_status()
        task_id = resp.json()["task_id"]
        logger.debug("Submitted task %s", task_id)

        # Poll GET /tasks/{id}
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if elapsed > self.timeout_per_task_s:
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
                    error_message=f"Timeout after {self.timeout_per_task_s}s",
                    termination_reason="timeout",
                )

            await asyncio.sleep(POLL_INTERVAL_S)

            try:
                poll = await self.http.get(f"{self.daemon_url}/tasks/{task_id}")
                poll.raise_for_status()
                task_data = poll.json()
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
                    termination_reason=task_data.get("termination_reason", "solution"),
                )

    async def verify_daemon(self) -> dict:
        """Check that the daemon is reachable and return its active config."""
        resp = await self.http.get(f"{self.daemon_url}/config/active")
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        await self.http.aclose()
