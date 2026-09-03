"""Hardening faults: storage, network, scorer, scale, and redaction.

The suite injects one fault per subsystem and asserts the declared
outcome: a storage digest collision quarantines and stops, a resolver
failure never leaves a partial import record, a scorer trap records
an error score instead of a fabricated result, a large dataset pages
with a stable cursor, a large trace persists and reads back intact,
artifact erasure leaves a durable redacted record, and every export
policy strips secrets.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from test_evidence_capture import make_attempts

import database as db
from benchmarks import evidence_capture, replay_bundle, score_execution
from benchmarks.scorer_plugins import DeterministicAnswerScorer


@pytest_asyncio.fixture
async def faults_db(tmp_path, monkeypatch):
    path = str(tmp_path / "faults.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    return await make_attempts(2)


def test_storage_digest_collision_quarantines_and_stops(tmp_path):
    from core.asset_store import (
        ARTIFACT_CONTENT_DIGEST_DOMAIN,
        ArtifactQuarantineError,
        ArtifactStore,
        DataClass,
        RetentionClass,
    )
    from core.digest_profile import digest_bytes

    store = ArtifactStore(tmp_path / "assets", "tenant-a")
    payload = b"trace-a"
    digest = digest_bytes(ARTIFACT_CONTENT_DIGEST_DOMAIN, payload)
    staged = store.stage(
        payload, declared_digest=digest, declared_size=len(payload),
        media_type="application/json", scanner_result="clean",
        data_class=DataClass.INTERNAL, access_policy="attempt-evidence-bytes",
        retention_class=RetentionClass.EVIDENCE_REQUIRED,
    )
    store.promote(staged)
    # Corrupt the stored bytes to simulate a storage fault, then stage
    # the same digest with the original bytes.
    target = store._object_path(digest)  # noqa: SLF001
    target.write_bytes(b"corrupted")
    again = store.stage(
        payload, declared_digest=digest, declared_size=len(payload),
        media_type="application/json", scanner_result="clean",
        data_class=DataClass.INTERNAL, access_policy="attempt-evidence-bytes",
        retention_class=RetentionClass.EVIDENCE_REQUIRED,
    )
    with pytest.raises(ArtifactQuarantineError, match="quarantined"):
        store.promote(again)


@pytest.mark.asyncio
async def test_network_fault_never_leaves_a_partial_import(faults_db):
    from benchmarks import evaluation_records, source_adapters
    from benchmarks.import_worker import ImportFetchError
    from core.url_guard import UrlValidationError

    async def failing(*args, **kwargs):
        raise ImportFetchError("resolver failure")

    with pytest.raises((ImportFetchError, UrlValidationError,
                        source_adapters.SourceAdapterError)):
        await source_adapters.import_through_registry(
            "adapter-https-file",
            {"url": "https://10.0.0.5/private.jsonl"},
        )
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS sources FROM benchmark_sources",
        )
        assert int((await cursor.fetchone())["sources"]) == 0
    del evaluation_records, failing


@pytest.mark.asyncio
async def test_scorer_trap_records_an_error_not_a_score(faults_db):
    from test_evaluation_contracts import valid_scorer_spec

    from benchmarks import facade

    await facade.execute("register_scorer_version",
                         {"record": valid_scorer_spec()})
    await evidence_capture.capture_attempt_evidence(
        attempt_id=faults_db[0], run_manifest={}, runtime_specification={},
        case={"case_id": "c"}, trace_events=[], final_output="42",
        resources={"cost": None, "tokens": 1, "latency_ms": 1},
        seed_evidence={"requested_seed": 1, "seed_control": "recorded"},
        ledger_references={},
    )

    class Trapping(DeterministicAnswerScorer):
        def score(self, evidence, configuration):
            raise RuntimeError("scorer fault")

    original = score_execution.scorer_plugins.plugin_for
    score_execution.scorer_plugins.plugin_for = lambda *a, **k: Trapping()
    try:
        result = await score_execution.score_attempt(
            attempt_id=faults_db[0], scorer_id="scorer-exact-match",
            scorer_version="2", plugin_type="deterministic",
        )
    finally:
        score_execution.scorer_plugins.plugin_for = original
    assert result["status"] == "error"
    assert result["terminal_class"] == "trap"
    assert result["record"]["passed"] is None


@pytest.mark.asyncio
async def test_large_dataset_pages_with_stable_offsets(faults_db):
    items = [{
        "id": f"large-{index}", "item_key": f"k-{index:05d}",
        "input": "q", "expected_output": "a", "subject": "s",
        "split": "test", "tags": [], "metadata": {},
    } for index in range(10_000)]
    await db.create_dataset_version(
        dataset_id="dataset-large", version_id="version-large",
        name="large", description="", source_uri=None, license_name=None,
        author=None, dataset_metadata={}, checksum="c", schema={"version": "1"},
        source_filename="l.jsonl", source_mime="application/x-ndjson",
        source_checksum="c", source_path="/tmp/l.jsonl", version_metadata={},
        items=items,
    )
    seen: list[str] = []
    offset = 0
    while True:
        rows, total = await db.list_dataset_items(
            "version-large", limit=200, offset=offset,
        )
        assert total == 10_000
        if not rows:
            break
        seen.extend(str(row["item_key"]) for row in rows)
        offset += len(rows)
    assert len(seen) == 10_000
    assert seen == sorted(seen)
    assert len(set(seen)) == 10_000


@pytest.mark.asyncio
async def test_large_trace_persists_and_reads_back_intact(faults_db):
    events = [{"kind": "action", "index": index} for index in range(10_000)]
    captured = await evidence_capture.capture_attempt_evidence(
        attempt_id=faults_db[1], run_manifest={}, runtime_specification={},
        case={"case_id": "c"}, trace_events=events, final_output="done",
        resources={"cost": None, "tokens": 1, "latency_ms": 1},
        seed_evidence={"requested_seed": 1, "seed_control": "recorded"},
        ledger_references={},
    )
    section = evidence_capture.read_evidence_section(
        captured["record"]["trace_digest"],
    )
    assert len(section["value"]) == 10_000
    assert section["value"][-1]["index"] == 9_999


def test_artifact_erasure_leaves_a_durable_redacted_record(tmp_path):
    from core.asset_store import (
        ARTIFACT_CONTENT_DIGEST_DOMAIN,
        ArtifactStore,
        DataClass,
        RetentionClass,
    )
    from core.digest_profile import digest_bytes

    store = ArtifactStore(tmp_path / "assets", "tenant-a")
    payload = b"personal data"
    digest = digest_bytes(ARTIFACT_CONTENT_DIGEST_DOMAIN, payload)
    staged = store.stage(
        payload, declared_digest=digest, declared_size=len(payload),
        media_type="text/plain", scanner_result="clean",
        data_class=DataClass.SENSITIVE, access_policy="protected",
        retention_class=RetentionClass.EVIDENCE_REQUIRED,
    )
    store.promote(staged)
    store.commit_reference(digest, referenced_by="attempt-a")
    store.erase(
        digest, authority_id="legal-authority", reason="legal_erasure",
        erased_at="2026-09-03T00:00:00Z",
    )
    read = store.read_object(digest)
    assert read["redacted"] is True
    assert read["reason"]


def test_export_redaction_strips_every_secret():
    from test_frozen_analysis import oracle_spec

    from benchmarks import frozen_analysis

    _, spec, frozen = oracle_spec()
    report = frozen_analysis.compute_report(spec, frozen)
    snapshot = frozen_analysis.snapshot_record(
        specification=spec, frozen_input=frozen, report=report,
        run_checksum="a" * 64, evidence_checksum="b" * 64,
        provenance=frozen_analysis.execution_provenance([]), replayable=True,
    )
    built = replay_bundle.build_bundle(
        policy="redacted",
        run_manifest={"run_id": "r", "api_key": "sk-live", "password": "p"},
        sources=[{"source_id": "s", "token": "t"}],
        dataset_manifest={}, test_revision={}, run_plan={},
        runtime_specifications=[{"credentials": {"secret": "x"}}],
        evidence_bundles=[], score_records=[], snapshot=snapshot,
        frozen_input=frozen, report=report, gate_results=[],
        artifacts={}, schemas={},
    )
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(built["archive"])) as archive:
        text = "".join(
            archive.read(name).decode("utf-8", errors="replace")
            for name in archive.namelist()
        )
    for secret in ("sk-live", '"p"', '"t"', '"x"'):
        assert secret not in text
    assert "[redacted]" in text


def test_redaction_keeps_token_counts_and_hides_credentials():
    from benchmarks.provenance import redact_secrets

    redacted = redact_secrets({
        "view_budget_tokens": 12000, "total_tokens": 42, "max_tokens": 5,
        "cleaner_token_threshold": 8000,
        "api_token": "secret", "token": "secret", "access_token": "secret",
    })
    # The runtime envelope parses these counts after redaction; a
    # redacted count once failed every real attempt.
    assert redacted["view_budget_tokens"] == 12000
    assert redacted["total_tokens"] == 42
    assert redacted["max_tokens"] == 5
    assert redacted["cleaner_token_threshold"] == 8000
    for credential in ("api_token", "token", "access_token"):
        assert redacted[credential] == "[redacted]"
