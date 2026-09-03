"""Model-backed judges and simulators in the run pipeline.

The judge transport pins its model, prompt digest, temperature, and
seed, reports usage into the score record, abstains on an
unparseable reply, and resolves from the scorer configuration inside
``score_attempt``. The simulator asks the model for every turn and
stops when the model stops. Anchor sets calibrate on registration
and then weekly: the loop runs due sets only, advances the schedule,
and stores one calibration record per pass.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from test_evaluation_contracts import valid_scorer_spec
from test_evidence_capture import make_attempts

import database as db
from benchmarks import (
    evaluation_records,
    facade,
    interaction_execution,
    judge_calibration,
    model_backed,
    resource_ledger,
    score_execution,
)
from benchmarks.model_backed import (
    GatewaySettings,
    ModelBackedJudge,
    ModelTransport,
    judge_for,
    model_backed_simulator_version,
    parse_json_reply,
)

SETTINGS = GatewaySettings(base_url="http://gateway.test/v1", api_key="k")


class FakeGateway:
    """Answer chat completions from a script keyed by the system prompt."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, body):
        self.requests.append(body)
        content = self.replies.pop(0) if self.replies else "{}"
        return {
            "model": body["model"],
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5,
                      "total_tokens": 25},
        }


def _judge(replies) -> tuple[ModelBackedJudge, FakeGateway]:
    gateway = FakeGateway(replies)
    transport = ModelTransport(SETTINGS, model="judge-model", seed=3,
                               client=gateway)
    return ModelBackedJudge(transport, judge_id="judge-a", version="2"), gateway


def test_judge_parses_scores_pins_and_reports_usage():
    judge, gateway = _judge([
        '```json\n{"dimensions": [{"name": "accuracy", "value": 0.9}],'
        ' "passed": true, "explanation": "matches", "uncertainty": 0.1}\n```',
    ])
    response = judge({"rubric": {"criteria": ["accuracy"]},
                      "candidates": [{"id": "a", "text": "42"}]})
    assert response["passed"] is True
    assert response["dimensions"] == [
        {"name": "accuracy", "value": 0.9, "category": None},
    ]
    assert response["usage"] == {"prompt_tokens": 20, "completion_tokens": 5,
                                 "total_tokens": 25}
    assert response["model"] == "judge-model"
    body = gateway.requests[0]
    assert body["temperature"] == 0.0 and body["seed"] == 3
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    assert len(judge.prompt_digest) == 64
    assert judge.pins()["model"] == "judge-model"


@pytest.mark.parametrize(
    "reply", ["not json at all", '{"abstain": true, "explanation": "thin"}',
              "", "[1, 2]"],
)
def test_unparseable_or_abstaining_replies_never_pass(reply):
    judge, _gateway = _judge([reply])
    response = judge({"rubric": {}, "candidates": []})
    assert response["passed"] is None
    assert response["dimensions"] == []
    assert response["explanation"].startswith("abstain")


def test_parse_json_reply_finds_the_object_inside_prose():
    assert parse_json_reply('Sure: {"label": "pass"} done') == {"label": "pass"}
    assert parse_json_reply("nothing here") is None


def test_judge_for_reads_the_scorer_configuration():
    gateway = FakeGateway(['{"label": "pass"}'])
    judge = judge_for(
        {"seed": 4, "judge": {"judge_id": "judge-b", "version": "1",
                              "model": "judge-model"}},
        settings=SETTINGS, client=gateway,
    )
    assert isinstance(judge, ModelBackedJudge)
    assert judge.transport.seed == 4
    assert judge_for({"comparison": "exact"}) is None
    label = judge.label({"item_id": "i", "input": "q", "expected_output": "a"},
                        ["pass", "fail"])
    assert label == "pass"
    assert judge.label({"item_id": "i"}, ["pass", "fail"]) == "abstain"
    with pytest.raises(model_backed.ModelBackedError, match="gateway"):
        judge_for({"judge": {"model": "m"}}, settings=None, client=None)


def test_model_backed_simulator_produces_turns_until_the_model_stops():
    gateway = FakeGateway([
        '{"content": "Book a table for two.", "stop": false}',
        '{"content": "Thanks, that is all.", "stop": true}',
    ])
    transport = ModelTransport(SETTINGS, model="sim-model", client=gateway)
    version = model_backed_simulator_version(
        transport, persona="a hungry customer", max_turns=5,
    )
    assert version.implementation_id == "simulator-model-backed"
    assert version.model == "sim-model"
    assert version.random_schedule == "temperature-0.0-seed-0"
    assert len(version.prompt_digest) == 64
    simulator = version.factory()
    simulator.start(["canary-1"])
    first = simulator.next_turn(0, None)
    assert first == {"content": "Book a table for two."}
    second = simulator.next_turn(1, "How many guests?")
    assert second == {"content": "Thanks, that is all.",
                      "stop": "goal_reached"}
    assert simulator.next_turn(2, "Bye") is None
    assert simulator.next_turn(9, "Bye") is None
    history = gateway.requests[1]["messages"]
    assert history[1] == {"role": "user", "content": "Book a table for two."}
    assert history[2] == {"role": "assistant", "content": "How many guests?"}
    assert simulator.received_canaries == ["canary-1"]
    assert len(simulator.usage) == 3


def test_model_backed_simulator_registers_as_a_pinned_version():
    transport = ModelTransport(SETTINGS, model="sim-model",
                               client=FakeGateway([]))
    version = model_backed_simulator_version(transport, persona="p")
    interaction_execution.register_simulator(version)
    resolved = interaction_execution.resolve_simulator("simulator-model-backed")
    assert resolved.pins() == version.pins()


@pytest_asyncio.fixture
async def judge_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "judge.db"))
    await db.init_db()
    attempts = await make_attempts(1)
    await facade.execute(
        "register_scorer_version", {"record": valid_scorer_spec()},
    )
    return attempts[0]


@pytest.mark.asyncio
async def test_score_attempt_resolves_the_model_backed_judge(judge_db, monkeypatch):
    from benchmarks import evidence_capture

    await evidence_capture.capture_attempt_evidence(
        attempt_id=judge_db,
        run_manifest={"run_id": "run-evidence"},
        runtime_specification={"runtime": "classic"},
        case={"case_id": "case-0"},
        trace_events=[{"kind": "action"}],
        final_output="42",
        resources={"cost": None, "tokens": 10, "latency_ms": 5},
        seed_evidence={"requested_seed": 1, "seed_control": "recorded"},
        ledger_references={"reservation_id": "reservation-a"},
    )
    gateway = FakeGateway([
        '{"dimensions": [{"name": "rubric", "value": 1.0}], "passed": true,'
        ' "explanation": "good"}',
    ])
    monkeypatch.setattr(
        model_backed, "gateway_settings_from_environment", lambda: SETTINGS,
    )
    original = model_backed.judge_for
    monkeypatch.setattr(
        model_backed, "judge_for",
        lambda configuration: original(configuration, client=gateway),
    )
    result = await score_execution.score_attempt(
        attempt_id=judge_db,
        scorer_id="scorer-exact-match",
        scorer_version="2",
        plugin_type="rubric_judge",
        configuration={"seed": 1, "judge": {"judge_id": "judge-a",
                                            "version": "2",
                                            "model": "judge-model"}},
        extra_evidence={"rubric": {"criteria": ["correct"]},
                        "candidates": [{"id": "a", "text": "42"}]},
    )
    assert result["status"] == "scored"
    assert result["record"]["passed"] is True
    assert result["record"]["judge"]["model"] == "judge-model"
    assert result["record"]["judge"]["usage"]["total_tokens"] == 25
    entries = await resource_ledger.list_entries("run-evidence")
    judge_entries = [e for e in entries if e["resource_class"] == "judge"]
    assert len(judge_entries) == 1
    assert judge_entries[0]["quantity"] == {"value": 25.0, "unit": "tokens"}
    assert gateway.requests[0]["model"] == "judge-model"


def _anchor(now: str, **overrides) -> dict:
    label_set = judge_calibration.pinned_label_set(
        "version-evidence", "1",
        [{"item_id": "case-0", "label": "pass", "reviewers": ["r-1"]},
         {"item_id": "case-1", "label": "fail", "reviewers": ["r-1"]}],
    )
    arguments = {
        "anchor_id": "anchor-a", "judge_id": "judge-a", "judge_version": "2",
        "judge_model": "judge-model", "prompt_digest": "a" * 64,
        "scorer_id": "scorer-rubric", "scorer_version": "1",
        "label_set": label_set, "candidate_models": ["candidate"],
        "now": now,
    }
    arguments.update(overrides)
    return judge_calibration.anchor_set_record(**arguments)


@pytest_asyncio.fixture
async def anchor_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "anchor.db"))
    await db.init_db()
    await make_attempts(2)


@pytest.mark.asyncio
async def test_anchor_sets_calibrate_when_due_and_advance_weekly(anchor_db):
    registered = await judge_calibration.register_anchor_set(
        _anchor("2026-09-01T00:00:00Z"),
    )
    assert registered["anchor_id"] == "anchor-a"
    listed = await judge_calibration.list_anchor_sets(now="2026-09-01T00:00:00Z")
    assert listed[0]["due"] is True
    assert listed[0]["next_due_at"] == "2026-09-01T00:00:00Z"

    class LabelJudge:
        def __init__(self):
            self.items = []

        def label(self, item, vocabulary):
            self.items.append(item)
            return "pass" if item["item_id"] == "case-0" else "fail"

    judges = []

    def factory(record):
        judge = LabelJudge()
        judges.append(judge)
        return judge

    outcomes = await judge_calibration.run_due_calibrations(
        now="2026-09-01T00:00:00Z", judge_factory=factory,
    )
    assert [o["state"] for o in outcomes] == ["current"]
    assert outcomes[0]["raw_agreement"] == 1.0
    assert outcomes[0]["next_due_at"] == "2026-09-08T00:00:00Z"
    # The judge saw the dataset items joined with the pinned labels.
    assert judges[0].items[0]["input"] == "What is 20 plus 22?"
    assert judges[0].items[0]["expected_output"] == "42"

    stored = await judge_calibration.latest_calibration("judge-a", "2")
    assert stored["calibration_id"] == outcomes[0]["calibration_id"]
    assert stored["dataset"]["item_count"] == 2

    # Nothing is due before the next week.
    assert await judge_calibration.run_due_calibrations(
        now="2026-09-04T00:00:00Z", judge_factory=factory,
    ) == []
    due = await evaluation_records.due_anchor_sets("2026-09-08T00:00:00Z")
    assert [row["id"] for row in due] == ["anchor-a"]
    again = await judge_calibration.run_due_calibrations(
        now="2026-09-08T00:00:00Z", judge_factory=factory,
    )
    assert again[0]["next_due_at"] == "2026-09-15T00:00:00Z"
    listed = await judge_calibration.list_anchor_sets(now="2026-09-08T00:00:00Z")
    assert listed[0]["last_calibrated_at"] == "2026-09-08T00:00:00Z"
    assert listed[0]["due"] is False
    # The stored record never changed.
    assert json.loads(listed[0]["record"]["schedule"]["created_at"] and "1") == 1
    assert listed[0]["record"]["schedule"]["next_due_at"] == "2026-09-01T00:00:00Z"


@pytest.mark.asyncio
async def test_missing_judge_transport_skips_and_the_loop_survives(anchor_db):
    await judge_calibration.register_anchor_set(_anchor("2026-09-01T00:00:00Z"))
    outcomes = await judge_calibration.run_due_calibrations(
        now="2026-09-02T00:00:00Z", judge_factory=lambda record: None,
    )
    assert outcomes == [{"anchor_id": "anchor-a", "state": "skipped",
                         "reason": "no judge transport is configured"}]

    def broken(record):
        raise RuntimeError("gateway down")

    await judge_calibration.calibration_loop(
        interval_seconds=0.0, judge_factory=broken, iterations=1,
    )
    assert judge_calibration.next_due("2026-09-01T00:00:00Z", 7) == (
        "2026-09-08T00:00:00Z"
    )
