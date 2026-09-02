"""Attempt evidence capture: complete, redacted, immutable bundles.

The capture connects the run manifest, runtime specification, case,
trace, final output, final state, tool calls, artifacts, claims and
verification, resources, seed evidence, versions with locale and
time zone, and the ledger references. Large sections persist as
content-addressed artifacts with typed digest references, foundation
redaction applies before any byte persists, and the stored bundle
never changes.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

import database as db
from benchmarks import evaluation_records, evidence_capture, repository
from benchmarks.provenance import content_checksum

RUN_MANIFEST = {"run_id": "run-alpha", "plan": "plan-alpha"}
RUNTIME_SPECIFICATION = {"runtime": "classic", "model": "model-a"}
CASE = {
    "case_id": "example-001",
    "task": {"instructions": "Add 20 and 22.", "assets": ["asset-a"]},
}
RESOURCES = {
    "cost": {"currency": "USD", "amount_nanos": 250_000_000},
    "tokens": 1200,
    "latency_ms": 4200,
}
SEED_EVIDENCE = {
    "requested_seed": 7001,
    "seed_control": "recorded",
    "applied_seed": None,
}
LEDGER_REFERENCES = {
    "admission_effect_id": "effect-a",
    "reservation_id": "reservation-a",
    "resource_ledger_ids": ["ledger-a"],
}


async def make_attempts(count: int = 1) -> list[str]:
    """Create one real run and return its materialized attempt ids."""
    await db.create_dataset_version(
        dataset_id="dataset-evidence",
        version_id="version-evidence",
        name="Evidence data",
        description="",
        source_uri=None,
        license_name=None,
        author=None,
        dataset_metadata={},
        checksum="evidence-checksum",
        schema={"version": "1"},
        source_filename="evidence.jsonl",
        source_mime="application/x-ndjson",
        source_checksum="evidence-source-checksum",
        source_path="/tmp/evidence.jsonl",
        version_metadata={},
        items=[{
            "id": f"item-{index}",
            "item_key": f"case-{index}",
            "input": "What is 20 plus 22?",
            "expected_output": "42",
            "subject": "math",
            "split": "test",
            "tags": [],
            "metadata": {},
        } for index in range(count)],
    )
    envelope = {
        "runtime_id": "classic",
        "effective_configuration": {"model_routing": {"medium": "model-a"}},
    }
    await repository.create_test_revision(
        test_id="test-evidence",
        revision_id="revision-evidence",
        name="evidence",
        description="",
        dataset_version_id="version-evidence",
        configuration={"repetitions": 1, "seed": 1},
        arms=[{
            "id": "arm-evidence",
            "name": "Classic",
            "slug": "classic",
            "runtime_id": "classic",
            "configuration": envelope,
            "configuration_checksum": content_checksum(envelope),
        }],
        scorers=[{"id": "scorer-exact-match-v1", "configuration": {}}],
    )
    await repository.create_run(
        run_id="run-evidence",
        revision_id="revision-evidence",
        idempotency_key=None,
    )
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT id FROM benchmark_attempts ORDER BY id",
        )
    return [str(row["id"]) for row in rows]


@pytest_asyncio.fixture
async def evidence_db(tmp_path, monkeypatch):
    path = str(tmp_path / "evidence.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    attempts = await make_attempts(3)
    return attempts


async def _capture(attempt_id: str, **overrides):
    arguments = {
        "attempt_id": attempt_id,
        "run_manifest": RUN_MANIFEST,
        "runtime_specification": RUNTIME_SPECIFICATION,
        "case": CASE,
        "trace_events": [{"kind": "action", "action": "add"}],
        "final_output": "42",
        "final_state": {"state": {"answer": "42"}},
        "board_state_reference": "board-alpha",
        "tool_calls": [{"tool": "calculator", "observation": "42"}],
        "artifacts": {"report.txt": b"final report"},
        "claims": [{"claim": "answer computed"}],
        "verification_decisions": [{"decision": "verified"}],
        "resources": RESOURCES,
        "seed_evidence": SEED_EVIDENCE,
        "ledger_references": LEDGER_REFERENCES,
        "recovery_events": [{"kind": "checkpoint", "step": 1}],
        "versions": {"runtime": "classic/1"},
    }
    arguments.update(overrides)
    return await evidence_capture.capture_attempt_evidence(**arguments)


@pytest.mark.asyncio
async def test_complete_bundle_captures_every_section(evidence_db):
    attempt_id = evidence_db[0]
    captured = await _capture(attempt_id)
    record = captured["record"]
    assert captured["completeness"]["level"] == "complete"
    assert captured["completeness"]["unavailable_sections"] == []
    for digest_field in (
        "run_manifest_digest",
        "runtime_specification_digest",
        "trace_digest",
        "final_output_digest",
        "final_state_digest",
        "tool_calls_digest",
        "verification_decisions_digest",
    ):
        assert len(record[digest_field]) == 64, digest_field
    assert record["case_reference"] == {
        "case_id": "example-001", "asset_ids": ["asset-a"],
    }
    assert record["board_state_reference"] == "board-alpha"
    assert len(record["artifacts"]) == 1
    assert record["ledger_references"] == LEDGER_REFERENCES
    # The stored bundle is readable through the write authority.
    stored = await evaluation_records.get_record(
        "attempt-evidence", attempt_id,
    )
    assert stored["record"]["attempt_id"] == attempt_id


@pytest.mark.asyncio
async def test_versions_record_locale_and_time_zone(evidence_db):
    captured = await _capture(evidence_db[0])
    versions = captured["record"]["versions"]
    assert "locale" in versions
    assert "time_zone" in versions
    assert versions["runtime"] == "classic/1"


@pytest.mark.asyncio
async def test_large_sections_store_content_addressed(evidence_db):
    captured = await _capture(evidence_db[0])
    record = captured["record"]
    trace = evidence_capture.read_evidence_section(record["trace_digest"])
    assert trace["redacted"] is False
    assert trace["value"] == [{"kind": "action", "action": "add"}]
    output = evidence_capture.read_evidence_section(
        record["final_output_digest"],
    )
    assert output["value"] == "42"


@pytest.mark.asyncio
async def test_redaction_applies_before_persistence(evidence_db):
    captured = await _capture(
        evidence_db[0],
        trace_events=[{
            "kind": "tool_call",
            "api_key": "sk-live-secret-value",
            "argument": "safe",
        }],
    )
    section = evidence_capture.read_evidence_section(
        captured["record"]["trace_digest"],
    )
    rendered = str(section["value"])
    assert "sk-live-secret-value" not in rendered
    assert "[redacted]" in rendered
    assert "safe" in rendered


@pytest.mark.asyncio
async def test_missing_core_sections_downgrade_completeness(evidence_db):
    captured = await _capture(
        evidence_db[1],
        trace_events=None,
        final_output=None,
        final_state=None,
        board_state_reference=None,
        tool_calls=None,
        claims=None,
        verification_decisions=None,
    )
    completeness = captured["completeness"]
    assert completeness["level"] == "partial_legacy"
    assert "trace" in completeness["unavailable_sections"]
    assert "final_output" in completeness["unavailable_sections"]
    assert captured["record"]["trace_digest"] is None


@pytest.mark.asyncio
async def test_optional_sections_stay_complete(evidence_db):
    captured = await _capture(
        evidence_db[2],
        final_state=None,
        board_state_reference=None,
        tool_calls=None,
        claims=None,
        verification_decisions=None,
        artifacts=None,
        recovery_events=None,
    )
    completeness = captured["completeness"]
    assert completeness["level"] == "complete"
    assert completeness["unavailable_sections"] == []
    assert "final_state" in captured["absent_sections"]


@pytest.mark.asyncio
async def test_stored_evidence_is_immutable(evidence_db):
    import aiosqlite

    attempt_id = evidence_db[0]
    await _capture(attempt_id)
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE attempt_evidence_bundles SET record = '{}' "
                "WHERE id = ?",
                (attempt_id,),
            )
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "DELETE FROM attempt_evidence_bundles WHERE id = ?",
                (attempt_id,),
            )


@pytest.mark.asyncio
async def test_capture_requires_an_attempt_identifier(evidence_db):
    with pytest.raises(
        evidence_capture.EvidenceCaptureError, match="names its attempt",
    ):
        await _capture("")
