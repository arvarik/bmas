"""Contract tests for the classic blackboard evaluation runner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from eval.datasets import EvalItem
from eval.metrics import compute_run_metrics
from eval.runner import BenchmarkRunner
from eval.scorer import ScoredResult


class FakeResponse:
    """Return a fixed HTTP payload to the runner."""

    def __init__(self, payload: dict[str, Any], error: Exception | None = None):
        self.payload = payload
        self.error = error

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error


class FakeHTTPClient:
    """Implement the small HTTP interface that BenchmarkRunner uses."""

    def __init__(
        self,
        poll_payloads: list[dict[str, Any]] | None = None,
        abort_error: Exception | None = None,
        capabilities: dict[str, Any] | None = None,
    ):
        self.poll_payloads = list(poll_payloads or [])
        self.last_poll_payload: dict[str, Any] = {}
        self.abort_error = abort_error
        self.capabilities = capabilities or {
            "api_version": "1",
            "variants": [{
                "id": "classic",
                "available": True,
                "aliases": ["traditional"],
            }],
        }
        self.post_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.closed = False

    async def post(
        self,
        url: str,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeResponse:
        self.post_calls.append({
            "url": url,
            "json": json,
            "timeout": timeout,
            "headers": headers,
        })
        if url.endswith("/submit"):
            return FakeResponse({"task_id": "task-1"})
        if self.abort_error:
            raise self.abort_error
        return FakeResponse({"status": "abort_requested", "task_id": "task-1"})

    async def get(self, url: str) -> FakeResponse:
        self.get_calls.append(url)
        if url.endswith("/capabilities"):
            return FakeResponse(self.capabilities)
        if self.poll_payloads:
            self.last_poll_payload = self.poll_payloads.pop(0)
        return FakeResponse(self.last_poll_payload)

    async def aclose(self) -> None:
        self.closed = True


def _item(item_id: str = "gsm8k-1", answer: str = "42") -> EvalItem:
    return EvalItem(
        id=item_id,
        question="What is 20 plus 22?",
        answer=answer,
        dataset="gsm8k",
    )


def _scored(item: EvalItem) -> ScoredResult:
    return ScoredResult(
        id=item.id,
        question=item.question,
        expected_answer=item.answer,
        actual_response=f"#### {item.answer}",
        dataset=item.dataset,
        subject=item.subject,
        extracted_answer=item.answer,
        correct=True,
        score_method="numeric_match",
        task_id=f"task-{item.id}",
        terminated_by="solution",
        status="completed",
        rounds=2,
    )


async def _runner_with_fake_http(
    fake_http: FakeHTTPClient,
    timeout_per_task_s: float = 60.0,
    api_key: str = "",
) -> BenchmarkRunner:
    runner = BenchmarkRunner(
        daemon_url="http://daemon.test",
        concurrency=2,
        timeout_per_task_s=timeout_per_task_s,
        api_key=api_key,
    )
    await runner.http.aclose()
    runner.http = fake_http
    return runner


@pytest.mark.asyncio
async def test_nested_task_response_maps_classic_lifecycle_fields(monkeypatch):
    """The runner reads the task object from the current daemon response."""
    monkeypatch.setattr("eval.runner.POLL_INTERVAL_S", 0)
    fake_http = FakeHTTPClient(
        poll_payloads=[
            {"task": {"status": "running"}, "sub_tasks": []},
            {
                "task": {
                    "status": "completed",
                    "result_summary": "The result is 42. #### 42",
                    "duration_ms": 8123,
                    "total_cost_usd": 0.041,
                    "total_tokens": 3210,
                    "model_used": "preserved-model-choice",
                    "complexity": "high",
                    "created_at": "2026-08-18T00:00:00Z",
                    "completed_at": "2026-08-18T00:00:08Z",
                    "terminated_by": "max_rounds",
                    "rounds_used": 4,
                    "effective_actions": 17,
                    "variant": "classic",
                    "phase": "completed",
                    "state_revision": 28,
                    "resume_count": 2,
                    "variant_metrics": {"consensus": 0.91},
                },
                "sub_tasks": [{"id": "sub-1"}],
            },
        ]
    )
    runner = await _runner_with_fake_http(fake_http)

    result = await runner._submit_task("What is 20 plus 22?")

    assert result.status == "completed"
    assert result.result_summary.endswith("#### 42")
    assert result.total_cost_usd == 0.041
    assert result.total_tokens == 3210
    assert result.model_used == "preserved-model-choice"
    assert result.terminated_by == "max_rounds"
    assert result.rounds == 4
    assert result.effective_actions == 17
    assert result.variant == "classic"
    assert result.phase == "completed"
    assert result.state_revision == 28
    assert result.recovery_count == 2
    assert result.variant_metrics == {"consensus": 0.91}
    assert fake_http.get_calls == [
        "http://daemon.test/tasks/task-1",
        "http://daemon.test/tasks/task-1",
    ]


@pytest.mark.asyncio
async def test_flat_task_response_remains_compatible(monkeypatch):
    """The runner accepts the old flat task response during upgrades."""
    monkeypatch.setattr("eval.runner.POLL_INTERVAL_S", 0)
    fake_http = FakeHTTPClient(
        poll_payloads=[
            {
                "status": "completed",
                "result_summary": "#### 42",
                "terminated_by": "solution",
                "rounds_used": 1,
            }
        ]
    )
    runner = await _runner_with_fake_http(fake_http)

    result = await runner._submit_task("What is 20 plus 22?")

    assert result.status == "completed"
    assert result.terminated_by == "solution"
    assert result.rounds == 1


@pytest.mark.asyncio
async def test_failed_task_keeps_daemon_failure_fields(monkeypatch):
    """The score record keeps a daemon failure instead of scoring an empty answer."""
    monkeypatch.setattr("eval.runner.POLL_INTERVAL_S", 0)
    fake_http = FakeHTTPClient(
        poll_payloads=[
            {
                "task": {
                    "status": "failed",
                    "result_summary": "Partial synthesis",
                    "duration_ms": 9300,
                    "total_cost_usd": 0.07,
                    "total_tokens": 4500,
                    "model_used": "preserved-model-choice",
                    "error_message": "Reviewer node stopped",
                    "terminated_by": "agent_failure",
                    "rounds_used": 3,
                },
                "sub_tasks": [],
            }
        ]
    )
    runner = await _runner_with_fake_http(fake_http)

    result = await runner._submit_and_score(_item())

    assert result.correct is False
    assert result.score_method == "task_failed"
    assert result.actual_response == "Partial synthesis"
    assert result.status == "failed"
    assert result.error_message == "Reviewer node stopped"
    assert result.terminated_by == "agent_failure"
    assert result.rounds == 3
    assert result.cost_usd == 0.07
    assert result.tokens == 4500


@pytest.mark.asyncio
async def test_timeout_requests_daemon_abort():
    """A harness timeout requests a daemon abort before it returns."""
    fake_http = FakeHTTPClient()
    runner = await _runner_with_fake_http(fake_http, timeout_per_task_s=-1)

    result = await runner._submit_task("A long task")

    assert result.status == "failed"
    assert result.terminated_by == "timeout"
    assert result.error_message == "Timeout after -1s"
    assert fake_http.post_calls == [
        {
            "url": "http://daemon.test/submit",
            "json": {"task": "A long task"},
            "timeout": None,
            "headers": None,
        },
        {
            "url": "http://daemon.test/api/tasks/task-1/abort",
            "json": {"reason": "evaluation_timeout"},
            "timeout": 10.0,
            "headers": None,
        },
    ]


@pytest.mark.asyncio
async def test_timeout_records_abort_request_failure():
    """A failed abort request stays visible in the evaluation result."""
    fake_http = FakeHTTPClient(abort_error=RuntimeError("daemon unavailable"))
    runner = await _runner_with_fake_http(fake_http, timeout_per_task_s=-1)

    result = await runner._submit_task("A long task")

    assert result.terminated_by == "timeout"
    assert result.error_message == "Timeout after -1s; abort request failed"


@pytest.mark.asyncio
async def test_operator_key_authenticates_submit_and_timeout_abort():
    """The runner sends the explicit operator key on both mutations."""
    fake_http = FakeHTTPClient()
    runner = await _runner_with_fake_http(
        fake_http,
        timeout_per_task_s=-1,
        api_key="operator-secret",
    )

    await runner._submit_task("A long task")

    assert [call["headers"] for call in fake_http.post_calls] == [
        {"Authorization": "Bearer operator-secret"},
        {"Authorization": "Bearer operator-secret"},
    ]


@pytest.mark.asyncio
async def test_operator_key_defaults_to_environment(monkeypatch):
    """The runner reads the operator key from the environment by default."""
    monkeypatch.setenv("BMAS_API_KEY", "environment-secret")
    runner = BenchmarkRunner(daemon_url="http://daemon.test")

    try:
        assert runner._mutation_headers() == {
            "Authorization": "Bearer environment-secret",
        }
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_selected_variant_is_submitted_per_task(monkeypatch):
    """One daemon can receive different registered implementations."""
    monkeypatch.setattr("eval.runner.POLL_INTERVAL_S", 0)
    fake_http = FakeHTTPClient(poll_payloads=[{
        "task": {"status": "completed", "result_summary": "#### 42"},
    }])
    runner = BenchmarkRunner(
        daemon_url="http://daemon.test", variant="classic",
    )
    await runner.http.aclose()
    runner.http = fake_http

    await runner._submit_task("A task")

    assert fake_http.post_calls[0]["json"] == {
        "task": "A task", "variant": "classic",
    }


@pytest.mark.asyncio
async def test_verify_daemon_uses_authoritative_capabilities():
    """The preflight reads the versioned capability contract."""
    fake_http = FakeHTTPClient()
    runner = await _runner_with_fake_http(fake_http)

    capabilities = await runner.verify_daemon()

    assert capabilities["api_version"] == "1"
    assert capabilities["variants"][0]["id"] == "classic"
    assert fake_http.get_calls == ["http://daemon.test/capabilities"]


@pytest.mark.asyncio
async def test_verify_daemon_rejects_unknown_contract_version():
    """The harness stops before it uses an incompatible daemon contract."""
    fake_http = FakeHTTPClient(capabilities={
        "api_version": "2", "variants": [],
    })
    runner = await _runner_with_fake_http(fake_http)

    with pytest.raises(ValueError, match="unsupported capability contract"):
        await runner.verify_daemon()


@pytest.mark.asyncio
async def test_classic_result_flows_from_api_to_metrics(monkeypatch):
    """A completed classic task keeps lifecycle fields through aggregation."""
    monkeypatch.setattr("eval.runner.POLL_INTERVAL_S", 0)
    fake_http = FakeHTTPClient(
        poll_payloads=[
            {
                "task": {
                    "status": "completed",
                    "result_summary": "The final answer is 42. #### 42",
                    "duration_ms": 7000,
                    "total_cost_usd": 0.05,
                    "total_tokens": 2500,
                    "model_used": "preserved-model-choice",
                    "terminated_by": "solution",
                    "rounds_used": 5,
                },
                "sub_tasks": [],
            }
        ]
    )
    runner = await _runner_with_fake_http(fake_http)

    scored = await runner._submit_and_score(_item())
    metrics = compute_run_metrics(
        run_id="classic-contract",
        dataset="gsm8k",
        results=[scored],
        run_config={"variant": "traditional"},
    )

    assert scored.correct is True
    assert scored.status == "completed"
    assert scored.model_used == "preserved-model-choice"
    assert scored.rounds == 5
    assert metrics.accuracy == 1.0
    assert metrics.avg_rounds == 5.0
    assert metrics.terminated_by == {"solution": 1}
    assert metrics.total_cost_usd == 0.05
    assert metrics.total_tokens == 2500


@pytest.mark.asyncio
async def test_run_persists_each_result_before_all_tasks_finish(tmp_path: Path):
    """The JSONL file receives each result as soon as that task finishes."""
    runner = BenchmarkRunner(
        daemon_url="http://daemon.test",
        concurrency=2,
    )
    release_slow = asyncio.Event()
    slow_started = asyncio.Event()
    items = [_item("slow"), _item("fast")]

    async def submit_and_score(item: EvalItem) -> ScoredResult:
        if item.id == "slow":
            slow_started.set()
            await release_slow.wait()
        return _scored(item)

    runner._submit_and_score = submit_and_score  # type: ignore[method-assign]
    run_task = asyncio.create_task(
        runner.run(items, run_id="incremental", results_dir=tmp_path)
    )
    await slow_started.wait()
    raw_path = tmp_path / "incremental.jsonl"

    for _ in range(100):
        if raw_path.exists() and len(raw_path.read_text().splitlines()) == 1:
            break
        await asyncio.sleep(0.01)

    partial_lines = raw_path.read_text().splitlines()
    assert len(partial_lines) == 1
    assert json.loads(partial_lines[0])["id"] == "fast"
    assert run_task.done() is False

    release_slow.set()
    results = await run_task
    await runner.close()

    assert [result.id for result in results] == ["slow", "fast"]
    assert len(raw_path.read_text().splitlines()) == 2


@pytest.mark.asyncio
async def test_run_persists_harness_exceptions(tmp_path: Path):
    """The runner converts one harness exception into a durable result."""
    runner = BenchmarkRunner(daemon_url="http://daemon.test")

    async def submit_and_score(_item: EvalItem) -> ScoredResult:
        raise RuntimeError("invalid daemon response")

    runner._submit_and_score = submit_and_score  # type: ignore[method-assign]
    results = await runner.run([_item()], run_id="error", results_dir=tmp_path)
    await runner.close()

    assert len(results) == 1
    assert results[0].status == "harness_error"
    assert results[0].score_method == "harness_error"
    assert results[0].error_message == "invalid daemon response"
    saved = json.loads((tmp_path / "error.jsonl").read_text())
    assert saved["status"] == "harness_error"
    assert saved["error_message"] == "invalid daemon response"
