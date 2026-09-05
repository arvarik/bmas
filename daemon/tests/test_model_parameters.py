"""Provider-aware completion parameters and the real-model repairs.

A reasoning model spends completion tokens on reasoning before it
writes a structured reply, so every control-plane call sizes its budget
from the provider profile, asks for a low effort where the gateway maps
it, omits the sampling parameters a provider rejects, and retries once
when the reply truncates. Anchor items carry or resolve the content the
judge reads, runtime ledger entries carry the admission estimate, a
terminal gate checks the calibration of every referenced metric, and
the analytics overview reads its inputs from storage.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from test_evaluation_contracts import valid_scorer_spec
from test_evidence_capture import make_attempts
from test_frozen_report import publishable_metric_definition

import database as db
from benchmarks import (
    analytics_views,
    evaluation_records,
    facade,
    judge_calibration,
    metric_registry,
    model_backed,
    resource_ledger,
)
from core.model_parameters import (
    ModelProfile,
    completion_parameters,
    message_content,
    profile_from_configuration,
    retry_budget,
    truncated,
)
from core.money import Money


def profile(provider: str, model: str, reasoning: str | None = None) -> ModelProfile:
    return profile_from_configuration(
        "alias", {"provider": provider, "model": model, "reasoning": reasoning},
    )


def test_reasoning_models_get_headroom_and_a_low_effort():
    gemini = completion_parameters(
        profile("gemini", "gemini-3.5-flash"), output_tokens=256,
        temperature=0.2, reasoning="low", json_object=True,
    )
    assert gemini == {
        "max_tokens": 2048,
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
    }
    openai = completion_parameters(
        profile("openai", "gpt-5-mini"), output_tokens=1024, temperature=0.4,
    )
    assert openai == {"max_tokens": 4096, "reasoning_effort": "low"}
    older = completion_parameters(
        profile("gemini", "gemini-2.5-flash"), output_tokens=16,
        temperature=0.1, reasoning="minimal",
    )
    # Gemini 2.5 reasons by default and still accepts temperature.
    assert older == {"max_tokens": 2048, "temperature": 0.1,
                     "reasoning_effort": "minimal"}


def test_plain_models_keep_their_sampling_parameters():
    for provider, model in (
        ("openai", "gpt-4o"), ("anthropic", "claude-sonnet-4-20250514"),
        ("ollama", "llama3"), ("unknown", "starter-model"),
    ):
        parameters = completion_parameters(
            profile(provider, model), output_tokens=256, temperature=0.2,
            reasoning="low", json_object=True,
        )
        assert parameters == {
            "max_tokens": 256,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }, (provider, model)


def test_the_operator_setting_overrides_the_automatic_effort():
    forced = completion_parameters(
        profile("anthropic", "claude-sonnet-4-6", "medium"), output_tokens=100,
        temperature=0.0,
    )
    assert forced == {"max_tokens": 2048, "temperature": 0.0,
                      "reasoning_effort": "medium"}
    # "off" never sends an effort; the model still reasons by default,
    # so the budget keeps its headroom and the temperature stays omitted.
    off = completion_parameters(
        profile("gemini", "gemini-3.5-flash", "off"), output_tokens=100,
        temperature=0.0,
    )
    assert off == {"max_tokens": 2048}
    plain_off = completion_parameters(
        profile("openai", "gpt-4o", "off"), output_tokens=100, temperature=0.3,
    )
    assert plain_off == {"max_tokens": 100, "temperature": 0.3}
    with pytest.raises(ValueError, match="reasoning must be one of"):
        profile("gemini", "gemini-3.5-flash", "maximum")


def test_truncation_detection_and_the_retry_budget():
    cut = {
        "choices": [{"finish_reason": "length", "message": {"content": "Here is"}}],
        "usage": {"completion_tokens": 252,
                  "completion_tokens_details": {"reasoning_tokens": 246, "text_tokens": 6}},
    }
    assert truncated(cut) == {"completion_tokens": 252, "reasoning_tokens": 246,
                              "text_tokens": 6}
    assert truncated({"choices": [{"finish_reason": "stop", "message": {}}]}) is None
    assert truncated({}) is None
    assert retry_budget({"max_tokens": 2048, "temperature": 0.2}) == {
        "max_tokens": 8192, "temperature": 0.2,
    }
    assert message_content({"choices": [{"message": {"content": None}}]}) == ""
    assert message_content({"choices": [{"message": {"content": [
        {"type": "text", "text": "a"}, {"type": "text", "text": "b"},
    ]}}]}) == "ab"


def test_the_judge_transport_uses_the_provider_profile():
    settings = model_backed.GatewaySettings(base_url="http://gateway/v1", api_key="k")
    seen: list[dict] = []

    def client(body):
        seen.append(body)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "{\"label\": \"pass\"}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, "model": "judge"}

    transport = model_backed.ModelTransport(
        settings, model="judge", temperature=0.0, max_tokens=1024, client=client,
        profile=profile("gemini", "gemini-3.5-flash"),
    )
    transport.complete([{"role": "user", "content": "{}"}])
    assert seen[0]["max_tokens"] == 4096
    assert seen[0]["reasoning_effort"] == "low"
    assert "temperature" not in seen[0]
    assert seen[0]["response_format"] == {"type": "json_object"}
    pins = transport.pins()
    assert pins["provider"] == "gemini"
    assert pins["effective_parameters"]["max_tokens"] == 4096
    plain = model_backed.ModelTransport(
        settings, model="judge", temperature=0.0, max_tokens=1024, client=client,
        profile=profile("openai", "gpt-4o"),
    )
    plain.complete([{"role": "user", "content": "{}"}])
    assert seen[1]["temperature"] == 0.0
    assert seen[1]["max_tokens"] == 1024
    assert "reasoning_effort" not in seen[1]


@pytest_asyncio.fixture
async def repairs_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "repairs.db"))
    await db.init_db()
    attempts = await make_attempts(2)
    await facade.execute(
        "register_scorer_version", {"record": valid_scorer_spec()},
    )
    return attempts


@pytest.mark.asyncio
async def test_anchor_items_resolve_from_the_dataset_or_inline_content(repairs_db):
    # The label set names the dataset and the version number, not the
    # version id, and one item carries inline content.
    label_set = judge_calibration.pinned_label_set(
        "dataset-evidence", "1",
        [
            {"item_id": "case-0", "label": "pass", "reviewers": ["r-1"]},
            {"item_id": "case-9", "label": "fail", "reviewers": ["r-1"],
             "input": "What is 7 plus 8?", "expected_output": "15",
             "candidate": "16"},
        ],
    )
    assert label_set["items"][1]["candidate"] == "16"
    items = await judge_calibration._anchor_items(label_set)  # noqa: SLF001
    by_id = {item["item_id"]: item for item in items}
    assert by_id["case-0"]["input"] == "What is 20 plus 22?"
    assert by_id["case-0"]["expected_output"] == "42"
    assert by_id["case-0"]["candidate"] is None
    assert by_id["case-9"] == {
        "item_id": "case-9", "label": "fail", "input": "What is 7 plus 8?",
        "expected_output": "15", "candidate": "16",
    }
    # A version id still resolves directly.
    direct = await judge_calibration._anchor_items(  # noqa: SLF001
        judge_calibration.pinned_label_set(
            "version-evidence", "1",
            [{"item_id": "case-1", "label": "pass", "reviewers": []}],
        ),
    )
    assert direct[0]["input"]
    # The record with inline content validates against the contract.
    record = judge_calibration.anchor_set_record(
        anchor_id="anchor-inline", judge_id="judge-a", judge_version="1",
        judge_model="judge-model", prompt_digest="a" * 64,
        scorer_id="scorer-exact-match", scorer_version="2",
        label_set=label_set, candidate_models=[], now="2026-09-05T00:00:00Z",
    )
    stored = await judge_calibration.register_anchor_set(record)
    assert stored["record"]["label_set"]["items"][1]["input"] == "What is 7 plus 8?"

    class Judge:
        def __init__(self):
            self.items = []

        def label(self, item, vocabulary):
            self.items.append(item)
            return "pass" if item.get("candidate") is None else "fail"

    judge = Judge()
    outcome = await judge_calibration.calibrate_anchor_set(
        stored, judge=judge, now="2026-09-05T00:00:00Z",
    )
    assert outcome["raw_agreement"] == 1.0
    assert judge.items[1]["candidate"] == "16"


@pytest.mark.asyncio
async def test_runtime_ledger_entries_carry_the_admission_estimate(repairs_db):
    first = repairs_db[0]
    import budget_service

    async def reservation(reservation_id):
        assert reservation_id == f"benchmark-reservation-{first}"
        return {"currency": "USD", "requested_amount_nanos": 50_000_000,
                "reserved_amount_nanos": 40_000_000}

    original = budget_service.get_reservation
    budget_service.get_reservation = reservation
    try:
        entry = await resource_ledger.emit_runtime_usage({
            "id": first, "run_id": "run-evidence", "total_tokens": 100,
            "total_cost_usd": 0.0321, "model_used": "starter-model",
        })
    finally:
        budget_service.get_reservation = original
    record = entry["record"]
    assert record["charge_state"] == "confirmed"
    assert record["estimate"]["value"] == {"currency": "USD", "amount_nanos": 40_000_000}
    assert record["estimate"]["method"] == "admission_reservation"
    assert record["actual"]["value"]["amount_nanos"] == 32_100_000
    summary = resource_ledger.summarize(
        await resource_ledger.list_entries("run-evidence"), currency="USD",
    )
    assert summary["entries_with_both"] == 1
    assert summary["estimate_total"]["amount_nanos"] == 40_000_000


@pytest.mark.asyncio
async def test_a_terminal_gate_blocks_on_expired_calibration(repairs_db, monkeypatch):
    from benchmarks import frozen_analysis, records, repository

    definition = publishable_metric_definition()
    # A semantic calibration expires on the clock; a deterministic one
    # expires only when a pinned digest changes.
    definition["calibration"]["method"] = sorted(metric_registry.SEMANTIC_METHODS)[0]
    metric_id = definition["metric_id"]
    await facade.execute("register_metric_definition", {"record": definition})
    await metric_registry.advance(
        metric_id, "validated", now="2026-09-03T00:00:00Z",
        validation_evidence={"schema": True, "fixture": True, "evidence": True},
    )
    await metric_registry.advance(metric_id, "published", now="2026-09-03T00:00:00Z")

    async def snapshot(run_id):
        return {"record": {"estimand": {"metric_ids": [metric_id]}}}

    monkeypatch.setattr(frozen_analysis, "current_snapshot", snapshot)
    # Inside the calibration window the gate proceeds.
    await records._assert_gate_calibration(  # noqa: SLF001
        "run-evidence", now="2026-09-05T00:00:00Z",
    )
    # After the calibration expires the gate blocks.
    with pytest.raises(repository.BenchmarkConflict, match="blocked by calibration"):
        await records._assert_gate_calibration(  # noqa: SLF001
            "run-evidence", now="2028-01-01T00:00:00Z",
        )

    async def none(run_id):
        return None

    monkeypatch.setattr(frozen_analysis, "current_snapshot", none)
    await records._assert_gate_calibration("run-evidence", now="2028-01-01T00:00:00Z")  # noqa: SLF001


@pytest.mark.asyncio
async def test_the_overview_inputs_come_from_storage(repairs_db):
    first = repairs_db[0]
    await resource_ledger.record_event(resource_ledger.ledger_entry(
        run_id="run-evidence", resource_class="runtime", provider="p",
        service="s", region="r", quantity=10, unit="tokens",
        pricing_version="v", actual=Money("USD", 30_000_000),
        actual_provider_text="0.03", attempt_id=first, now="2026-09-05T00:00:00Z",
    ))
    run = await repository_run()
    inputs = await analytics_views.overview_inputs(run)
    arm = next(a for a in run["attempts"] if str(a["id"]) == first)
    arm_id = str(arm.get("arm_id") or arm.get("arm_name"))
    assert inputs["cost_by_arm"] == {arm_id: {"currency": "USD", "amount_nanos": 30_000_000}}
    assert inputs["classifications"] == []
    assert inputs["trajectory_results"] == []
    assert inputs["calibrations"] == []
    assert inputs["panels"] == []
    assert isinstance(inputs["horizon_by_case"], dict)
    stored = await evaluation_records.list_records("judge-calibration-record")
    assert stored == []
    assert json.dumps(inputs)  # serializable


async def repository_run():
    from benchmarks import repository

    return await repository.get_run("run-evidence")
