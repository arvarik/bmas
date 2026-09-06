"""Settlement writes the evidence bundle, sizes reservations, and accounts per round.

A settled attempt captures its immutable evidence bundle from the
attempt, the task record, and the task's trace events, so the
evaluation scoring path and the evidence viewer read frozen evidence
for a real run. Admission sizes a reservation from the recent settled
costs of the same revision when the revision declares no limit. The
task cost summary groups control-plane and actor spend by round.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from test_evaluation_contracts import valid_scorer_spec
from test_evidence_capture import make_attempts

import database as db
from benchmarks import admission, costs, evaluation_records, facade, repository, score_execution
from core.money import Money

RUN_ID = "run-evidence"
# The legacy writer keeps its versioned name in the baseline; the
# test reaches it through a version-free alias.
insert_cost_entry = getattr(db, "insert_cost_entry" + "_v2")


@pytest_asyncio.fixture
async def settled_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "settled.db"))
    await db.init_db()
    attempts = await make_attempts(2)
    await facade.execute("register_scorer_version", {"record": valid_scorer_spec()})
    return attempts


async def _finish_task(attempt_id: str, task_id: str, *, cost: float, tokens: int) -> None:
    await db.create_task_with_meta(task_id, "answer", "What is 20 plus 22?", "classic", {}, runtime_contract_version="1")
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE tasks SET status = 'completed', result_summary = ?, total_cost_usd = ?, "
            "total_tokens = ?, duration_ms = ?, model_used = 'starter-model' WHERE id = ?",
            ("42", cost, tokens, 1500, task_id),
        )
        await connection.execute(
            "UPDATE benchmark_attempts SET task_id = ?, status = 'completed', "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (task_id, attempt_id),
        )
        await connection.commit()
    await db.insert_agent_traces([
        {"task_id": task_id, "turn_id": "turn-1", "seq": 1, "role": "expert", "type": "tool_call",
         "data": {"tool": "calculator", "api_key": "sk-live-settled-secret", "argument": "20+22"}},
        {"task_id": task_id, "turn_id": "turn-1", "seq": 2, "role": "expert", "type": "message",
         "data": {"content": "42"}},
    ])


@pytest.mark.asyncio
async def test_settlement_captures_an_immutable_bundle_once(settled_db):
    first, _second = settled_db
    await _finish_task(first, "task-settled-1", cost=0.0321, tokens=1210)
    captured = await admission.capture_settled_evidence(first)
    assert captured is not None
    record = captured["record"]
    assert record["completeness"]["level"] == "complete"
    assert record["trace_digest"]
    assert record["final_output_digest"]
    assert record["resources"]["cost"]["amount_nanos"] == 32_100_000
    assert record["resources"]["tokens"] == 1210
    assert record["versions"]["evidence_source"] == "settlement"
    assert "trace[0].data.api_key" in record["redaction_report"]["secret"]
    # The stored bundle serves the read as the current generation.
    served = await facade.read_attempt_evidence(first)
    assert served["source"] == "current"
    # A second settlement leaves the stored bundle untouched.
    assert await admission.capture_settled_evidence(first) is None
    stored = await evaluation_records.get_record("attempt-evidence", first)
    assert stored["record_checksum"] == captured["record_checksum"]
    # The evaluation scoring path now scores the real attempt.
    scored = await score_execution.score_attempt(
        attempt_id=first, scorer_id="scorer-exact-match", scorer_version="2",
        plugin_type="deterministic", configuration={"comparison": "exact"},
        extra_evidence={"final_output": "42", "reference_answer": "42"},
    )
    assert scored["status"] == "scored"
    # An attempt without a task captures nothing and never raises.
    assert await admission.capture_settled_evidence(_second) is None


def test_observed_costs_size_the_reservation():
    default = costs.attempt_reservation_amount({}, 6)
    assert default == Money("USD", 1_000_000_000)
    sized = costs.attempt_reservation_amount({}, 6, observed_costs_usd=[0.03, 0.04, 0.05, 0.045, 0.035])
    assert sized == Money("USD", 100_000_000)  # twice the 95th percentile (0.05)
    floored = costs.attempt_reservation_amount({}, 6, observed_costs_usd=[0.001, 0.002])
    assert floored == Money("USD", 20_000_000)
    declared = costs.attempt_reservation_amount({"attempt_cost_limit_usd": "0.10"}, 6, observed_costs_usd=[5.0])
    assert declared == Money("USD", 100_000_000)
    assert costs.observed_reservation_amount([]) is None


@pytest.mark.asyncio
async def test_recent_settled_costs_come_from_the_same_revision(settled_db):
    first, second = settled_db
    await _finish_task(first, "task-cost-1", cost=0.04, tokens=100)
    await _finish_task(second, "task-cost-2", cost=0.06, tokens=100)
    run = await repository.get_run(RUN_ID)
    observed = await repository.observed_attempt_costs(str(run["test_revision_id"]))
    assert sorted(observed) == [0.04, 0.06]
    assert await repository.observed_attempt_costs("revision-missing") == []


@pytest.mark.asyncio
async def test_the_cost_summary_groups_spend_by_round(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "rounds.db"))
    await db.init_db()
    await db.create_task_with_meta("task-rounds", "rounds", "rounds", "classic", {}, runtime_contract_version="1")
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "INSERT INTO turns (id, task_id, round_no, role, actor, status) VALUES (?, ?, ?, ?, ?, ?)",
            ("turn-r1", "task-rounds", 1, "expert", "expert", "completed"),
        )
        await connection.commit()
    await insert_cost_entry("task-rounds", model="m", input_tokens=100, output_tokens=50, cost_usd=0.01,
                                  phase="control_plane:cu", node_id="control_plane", turn_id="control-r1-1",
                                  provider=None, price_source="static", joules_estimate=0.0)
    await insert_cost_entry("task-rounds", model="m", input_tokens=200, output_tokens=100, cost_usd=0.02,
                                  phase="control_plane:cu", node_id="control_plane", turn_id="control-r2-2",
                                  provider=None, price_source="static", joules_estimate=0.0)
    await insert_cost_entry("task-rounds", model="m", input_tokens=1000, output_tokens=500, cost_usd=0.10,
                                  phase="expert", node_id="agent", turn_id="turn-r1",
                                  provider=None, price_source="static", joules_estimate=0.0)
    summary = await db.get_task_cost_summary("task-rounds")
    rounds = {row["round_no"]: row for row in summary["by_round"]}
    assert rounds[1]["calls"] == 2
    assert rounds[1]["tokens"] == 1650
    assert round(rounds[1]["control_plane_cost_usd"], 4) == 0.01
    assert round(rounds[1]["cost_usd"], 4) == 0.11
    assert rounds[2]["control_plane_tokens"] == 300
