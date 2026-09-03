"""The resource ledger fills itself from every execution path.

Runtime settlement, scorer execution, judge use, evidence storage,
human review, and run-scoped ingestion each record one ledger entry
through the one facade without any caller posting to the ledger
API. Every entry id derives from its source event, so a repeated
path never doubles an entry, and the ledger summary reports every
required class from observed use instead of "no use".
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from test_evaluation_contracts import valid_scorer_spec
from test_evidence_capture import make_attempts

import database as db
from benchmarks import (
    asset_ingestion,
    evidence_capture,
    facade,
    resource_ledger,
    score_execution,
)

RUN_ID = "run-evidence"


@pytest_asyncio.fixture
async def emission_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "emission.db"))
    await db.init_db()
    attempts = await make_attempts(2)
    await facade.execute(
        "register_scorer_version", {"record": valid_scorer_spec()},
    )
    return attempts


async def _capture(attempt_id: str) -> None:
    await evidence_capture.capture_attempt_evidence(
        attempt_id=attempt_id,
        run_manifest={"run_id": RUN_ID},
        runtime_specification={"runtime": "classic"},
        case={"case_id": "case-0"},
        trace_events=[{"kind": "action", "action": "answer"}],
        final_output="42",
        resources={"cost": None, "tokens": 10, "latency_ms": 5},
        seed_evidence={"requested_seed": 1, "seed_control": "recorded"},
        ledger_references={"reservation_id": "reservation-a"},
    )


@pytest.mark.asyncio
async def test_storage_scorer_and_judge_paths_emit_entries(emission_db):
    first, _second = emission_db
    await _capture(first)
    entries = await resource_ledger.list_entries(RUN_ID)
    storage = [e for e in entries if e["resource_class"] == "storage"]
    assert len(storage) == 1
    assert storage[0]["charge_state"] == "not_billable"
    assert storage[0]["quantity"]["unit"] == "bytes"
    assert storage[0]["quantity"]["value"] > 0
    assert storage[0]["references"]["attempt_id"] == first

    await score_execution.score_attempt(
        attempt_id=first,
        scorer_id="scorer-exact-match",
        scorer_version="2",
        plugin_type="deterministic",
        configuration={"comparison": "exact"},
        extra_evidence={"final_output": "42", "reference_answer": "42"},
    )

    def judge(request):
        return {
            "dimensions": [{"name": "rubric", "value": 1.0}],
            "passed": True,
            "explanation": "clear",
            "usage": {"total_tokens": 321, "cost": 0.0021},
            "model": "judge-model",
        }

    await score_execution.score_attempt(
        attempt_id=first,
        scorer_id="scorer-exact-match",
        scorer_version="2",
        plugin_type="rubric_judge",
        configuration={"seed": 1},
        extra_evidence={
            "rubric": {"criteria": ["correct"]},
            "candidates": [{"id": "a", "text": "42"}],
        },
        judge=judge,
    )
    entries = await resource_ledger.list_entries(RUN_ID)
    classes = sorted(entry["resource_class"] for entry in entries)
    assert classes == ["judge", "scorer", "scorer", "storage"]
    judge_entry = next(e for e in entries if e["resource_class"] == "judge")
    assert judge_entry["charge_state"] == "confirmed"
    assert judge_entry["quantity"] == {"value": 321.0, "unit": "tokens"}
    assert judge_entry["service"] == "judge-model"
    assert judge_entry["actual"]["value"]["amount_nanos"] == 2_100_000
    scorer_entries = [e for e in entries if e["resource_class"] == "scorer"]
    assert all(e["charge_state"] == "not_billable" for e in scorer_entries)
    assert all(e["references"]["scorer_id"] == "scorer-exact-match"
               for e in scorer_entries)


@pytest.mark.asyncio
async def test_runtime_review_and_import_paths_emit_entries(emission_db):
    first, second = emission_db
    attempt = {"id": first, "run_id": RUN_ID, "total_tokens": 1500,
               "total_cost_usd": 0.0123, "model_used": "model-a"}
    stored = await resource_ledger.emit_runtime_usage(attempt, now="2026-09-03T00:00:00Z")
    assert stored is not None
    record = stored["record"]
    assert record["entry_id"] == f"ledger-runtime-{first}"
    assert record["charge_state"] == "confirmed"
    assert record["actual"]["value"]["amount_nanos"] == 12_300_000
    assert record["actual"]["evidence"]["provider_text"] == "0.012300000"
    assert record["quantity"] == {"value": 1500.0, "unit": "tokens"}
    assert record["reservation_id"] == f"benchmark-reservation-{first}"
    # A repeated settlement never doubles the entry.
    assert await resource_ledger.emit_runtime_usage(attempt) is None
    unknown = await resource_ledger.emit_runtime_usage(
        {"id": second, "run_id": RUN_ID, "total_cost_usd": None},
    )
    assert unknown["record"]["charge_state"] == "unknown"
    assert unknown["record"]["quantity"]["unit"] == "attempts"

    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE benchmark_attempts SET status = 'completed' WHERE id = ?",
            (first,),
        )
        await connection.commit()
    review, created = await facade.execute(
        "create_human_review",
        {
            "review_id": "review-1", "attempt_id": first,
            "reviewer_id": "reviewer-a", "score": 1.0, "passed": True,
            "note": "fine", "idempotency_key": "review-key-1",
        },
        generation="legacy",
    )
    assert created is True
    await facade.execute(
        "create_human_review",
        {
            "review_id": "review-1", "attempt_id": first,
            "reviewer_id": "reviewer-a", "score": 1.0, "passed": True,
            "note": "fine", "idempotency_key": "review-key-1",
        },
        generation="legacy",
    )

    outcome = asset_ingestion.ingest_asset(
        original_name="note.txt", declared_media_type="text/plain",
        content=b"hello evidence",
    )
    await asset_ingestion.store_ingestion(outcome, run_id=RUN_ID)

    entries = await resource_ledger.list_entries(RUN_ID)
    classes = sorted(entry["resource_class"] for entry in entries)
    assert classes == ["human_review", "import", "runtime", "runtime"]
    review_entry = next(e for e in entries if e["resource_class"] == "human_review")
    assert review_entry["charge_state"] == "unknown"
    assert review_entry["entry_id"] == "ledger-review-review-1"
    import_entry = next(e for e in entries if e["resource_class"] == "import")
    assert import_entry["charge_state"] == "not_billable"
    assert import_entry["quantity"]["value"] == float(len(b"hello evidence"))
    assert import_entry["references"]["import_id"]

    summary = resource_ledger.summarize(entries, currency="USD")
    assert summary["per_class"]["runtime"]["entries"] == 2
    assert summary["no_use_classes"] == ["environment", "judge", "scorer",
                                         "storage"]
    assert summary["unknown_entry_ids"]
