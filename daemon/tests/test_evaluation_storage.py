"""Additive evaluation storage: authority, keys, triggers, downgrade.

The expansion adds beside the current schema without deleting,
renaming, or reinterpreting a column. One module is the canonical
write authority, every stored record validates first, required
foreign keys enforce, immutable publication triggers protect
published and historical records, a populated legacy database
upgrades without silent loss, and the supported predestructive
downgrade preserves every record.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
import pytest_asyncio
from test_evaluation_contracts import (
    valid_asset_ingestion,
    valid_attempt_evidence,
    valid_benchmark_source,
    valid_dataset_draft,
    valid_evaluation_case,
    valid_metric_definition,
    valid_scorer_spec,
)

import database as db
from benchmarks import evaluation_records, repository
from benchmarks.evaluation_contracts import EvaluationContractError
from benchmarks.provenance import content_checksum


@pytest_asyncio.fixture
async def storage_db(tmp_path, monkeypatch):
    path = str(tmp_path / "storage.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    # Populated legacy content the expansion must never disturb: one
    # dataset, one revision, one run with attempts.
    await db.create_dataset_version(
        dataset_id="dataset-legacy",
        version_id="version-legacy",
        name="Legacy data",
        description="",
        source_uri=None,
        license_name=None,
        author=None,
        dataset_metadata={},
        checksum="legacy-checksum",
        schema={"version": "1"},
        source_filename="legacy.jsonl",
        source_mime="application/x-ndjson",
        source_checksum="legacy-source-checksum",
        source_path="/tmp/legacy.jsonl",
        version_metadata={},
        items=[{
            "id": "item-legacy",
            "item_key": "one",
            "input": "What is 20 plus 22?",
            "expected_output": "42",
            "subject": "math",
            "split": "test",
            "tags": [],
            "metadata": {},
        }],
    )
    envelope = {
        "runtime_id": "classic",
        "effective_configuration": {"model_routing": {"medium": "model-a"}},
    }
    await repository.create_test_revision(
        test_id="test-legacy",
        revision_id="revision-legacy",
        name="legacy",
        description="",
        dataset_version_id="version-legacy",
        configuration={"repetitions": 1, "seed": 1},
        arms=[{
            "id": "arm-legacy",
            "name": "Classic",
            "slug": "classic",
            "runtime_id": "classic",
            "configuration": envelope,
            "configuration_checksum": content_checksum(envelope),
        }],
        scorers=[{"id": "scorer-exact-match-v1", "configuration": {}}],
    )
    await repository.create_run(
        run_id="run-legacy",
        revision_id="revision-legacy",
        idempotency_key=None,
    )
    return path


async def _one_attempt_id() -> str:
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT id FROM benchmark_attempts LIMIT 1",
        )
        row = await cursor.fetchone()
    return str(row["id"])


async def _table_names() -> set[str]:
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )
    return {str(row["name"]) for row in rows}


async def _legacy_snapshot() -> dict[str, list[tuple]]:
    """Read a byte-stable snapshot of representative legacy tables."""
    snapshot: dict[str, list[tuple]] = {}
    async with db._connect() as connection:  # noqa: SLF001
        for table in ("tasks", "datasets", "dataset_versions",
                      "dataset_items", "benchmark_tests",
                      "benchmark_test_revisions", "benchmark_runs",
                      "benchmark_trials", "benchmark_attempts",
                      "benchmark_scorers"):
            rows = await connection.execute_fetchall(
                f"SELECT * FROM {table} ORDER BY 1",
            )
            snapshot[table] = [tuple(row) for row in rows]
    return snapshot


@pytest.mark.asyncio
async def test_the_expansion_adds_every_table_without_touching_legacy(
    storage_db,
):
    before = await _legacy_snapshot()
    # Re-running the additive migration changes nothing: it deletes,
    # renames, and reinterprets no existing column.
    async with db._connect() as connection:  # noqa: SLF001
        await db._migrate_add_evaluation_contract_storage(  # noqa: SLF001
            connection,
        )
    assert await _legacy_snapshot() == before
    tables = await _table_names()
    for table, _kind in evaluation_records.EXPANSION_TABLES:
        assert table in tables, table
    assert "evaluation_readonly_archive" in tables
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS rows_present "
            "FROM evaluation_readonly_archive",
        )
        row = await cursor.fetchone()
    assert int(row["rows_present"]) == 0


@pytest.mark.asyncio
async def test_records_validate_before_any_write(storage_db):
    record = valid_benchmark_source()
    record["surprise_field"] = "unexpected"
    with pytest.raises(EvaluationContractError):
        await evaluation_records.save_record(record)
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS rows_present FROM benchmark_sources",
        )
        row = await cursor.fetchone()
    assert int(row["rows_present"]) == 0


@pytest.mark.asyncio
async def test_saved_records_keep_canonical_json_and_checksums(
    storage_db,
):
    saved = await evaluation_records.save_record(valid_benchmark_source())
    stored = await evaluation_records.get_record(
        "benchmark-source", saved["id"],
    )
    assert stored is not None
    assert stored["record"]["source_id"] == "source-gsm8k"
    assert stored["record_checksum"] == content_checksum(
        valid_benchmark_source(),
    )
    assert stored["schema_version"] == 2


@pytest.mark.asyncio
async def test_required_foreign_keys_enforce(storage_db):
    evidence = valid_attempt_evidence()
    evidence["attempt_id"] = "attempt-missing"
    with pytest.raises(aiosqlite.IntegrityError):
        await evaluation_records.save_record(
            evidence, links={"attempt_id": "attempt-missing"},
        )
    with pytest.raises(
        evaluation_records.EvaluationStorageError, match="requires",
    ):
        await evaluation_records.save_record(valid_attempt_evidence())
    attempt_id = await _one_attempt_id()
    evidence = valid_attempt_evidence()
    evidence["attempt_id"] = attempt_id
    saved = await evaluation_records.save_record(
        evidence, links={"attempt_id": attempt_id},
    )
    assert saved["table"] == "attempt_evidence_bundles"


@pytest.mark.asyncio
async def test_immutable_records_reject_updates_and_deletes(storage_db):
    await evaluation_records.save_record(valid_benchmark_source())
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE benchmark_sources SET record = '{}'",
            )
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute("DELETE FROM benchmark_sources")


@pytest.mark.asyncio
async def test_publication_freezes_a_scorer_version(storage_db):
    saved = await evaluation_records.save_record(valid_scorer_spec())
    await evaluation_records.publish_record("scorer-spec", saved["id"])
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE scorer_versions SET record = '{}' WHERE id = ?",
                (saved["id"],),
            )
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "DELETE FROM scorer_versions WHERE id = ?",
                (saved["id"],),
            )
    with pytest.raises(
        evaluation_records.EvaluationStorageError, match="draft",
    ):
        await evaluation_records.publish_record("scorer-spec", saved["id"])


@pytest.mark.asyncio
async def test_a_published_draft_freezes_its_cases(storage_db):
    source = await evaluation_records.save_record(valid_benchmark_source())
    draft = await evaluation_records.save_record(
        valid_dataset_draft(),
        links={"source_id": source["id"], "parent_version_id": None},
    )
    case = await evaluation_records.save_record(
        valid_evaluation_case(), links={"draft_id": draft["id"]},
    )
    await evaluation_records.publish_record("dataset-draft", draft["id"])
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE dataset_draft_cases SET record = '{}' "
                "WHERE id = ?",
                (case["id"],),
            )
        await connection.rollback()
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "DELETE FROM dataset_draft_cases WHERE id = ?",
                (case["id"],),
            )
        await connection.rollback()
    second = valid_evaluation_case()
    second["case_id"] = "example-002"
    with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
        await evaluation_records.save_record(
            second, links={"draft_id": draft["id"]},
        )


@pytest.mark.asyncio
async def test_asset_ingestion_follows_its_declared_state_machine(
    storage_db,
):
    saved = await evaluation_records.save_record(valid_asset_ingestion())
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="content"):
            await connection.execute(
                "UPDATE asset_ingestion_records SET record = '{}'",
            )
    await evaluation_records.transition_asset_state(
        saved["id"], "accepted",
    )
    await evaluation_records.transition_asset_state(saved["id"], "deleted")
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(
            aiosqlite.IntegrityError, match="undeclared",
        ):
            await connection.execute(
                "UPDATE asset_ingestion_records SET state = 'quarantined' "
                "WHERE id = ?",
                (saved["id"],),
            )


@pytest.mark.asyncio
async def test_metric_definitions_follow_their_lifecycle(storage_db):
    record = valid_metric_definition()
    saved = await evaluation_records.save_record(record)
    published = valid_metric_definition()
    published["lifecycle_state"] = "published"
    await evaluation_records.transition_metric_lifecycle(
        saved["id"], published,
    )
    # A published definition is immutable; only deprecation or
    # withdrawal can follow.
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE metric_definitions SET record = '{}' "
                "WHERE id = ? AND lifecycle_state = 'published'",
                (saved["id"],),
            )
        with pytest.raises(aiosqlite.IntegrityError, match="draft"):
            await connection.execute(
                "DELETE FROM metric_definitions WHERE id = ?",
                (saved["id"],),
            )
    deprecated = valid_metric_definition()
    deprecated["lifecycle_state"] = "deprecated"
    await evaluation_records.transition_metric_lifecycle(
        saved["id"], deprecated,
    )
    stored = await evaluation_records.get_record(
        "metric-definition", saved["id"],
    )
    assert stored is not None
    assert stored["lifecycle_state"] == "deprecated"
    # A deprecated definition stays readable and unchanged.
    withdrawn = valid_metric_definition()
    withdrawn["lifecycle_state"] = "withdrawn"
    with pytest.raises(aiosqlite.IntegrityError, match="readable"):
        await evaluation_records.transition_metric_lifecycle(
            saved["id"], withdrawn,
        )


@pytest.mark.asyncio
async def test_history_rows_are_unique_and_immutable(storage_db):
    attempt_id = await _one_attempt_id()
    await evaluation_records.save_dispatch_rank_history(
        attempt_id, 1, {"ticket": 1, "priority_band": "standard"},
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await evaluation_records.save_dispatch_rank_history(
            attempt_id, 1, {"ticket": 2, "priority_band": "standard"},
        )
    await evaluation_records.save_cost_settlement_version(
        "run-legacy", 1, {"settled_total": {"currency": "USD",
                                            "amount_nanos": 0}},
    )
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE dispatch_rank_history SET record = '{}'",
            )
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE cost_settlement_versions SET record = '{}'",
            )


@pytest.mark.asyncio
async def test_model_only_kinds_validate_without_storage(storage_db):
    from test_evaluation_contracts import valid_score_record

    with pytest.raises(
        evaluation_records.EvaluationStorageError, match="no table",
    ):
        await evaluation_records.save_record(valid_score_record())


@pytest.mark.asyncio
async def test_the_predestructive_downgrade_preserves_every_record(
    storage_db,
):
    source = await evaluation_records.save_record(valid_benchmark_source())
    draft = await evaluation_records.save_record(
        valid_dataset_draft(),
        links={"source_id": source["id"], "parent_version_id": None},
    )
    await evaluation_records.save_record(
        valid_evaluation_case(), links={"draft_id": draft["id"]},
    )
    before = await _legacy_snapshot()

    outcome = await evaluation_records.downgrade_evaluation_expansion()
    # Three evaluation records plus the single migration-state row.
    assert outcome["archived_records"] == 4
    assert outcome["schema_version"] == (
        evaluation_records.EXPANSION_BASE_VERSION
    )

    # Every legacy-compatible record stays byte-identical, the expansion
    # tables are gone, and the recorded version lowered.
    assert await _legacy_snapshot() == before
    tables = await _table_names()
    for table, _kind in evaluation_records.EXPANSION_TABLES:
        assert table not in tables, table
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT MAX(version) AS version FROM schema_version",
        )
        row = await cursor.fetchone()
        assert int(row["version"]) == (
            evaluation_records.EXPANSION_BASE_VERSION
        )
        archived_rows = await connection.execute_fetchall(
            "SELECT * FROM evaluation_readonly_archive ORDER BY id",
        )
    # The expansion-only records survive as explicit read-only records; no
    # expansion-only field silently discards.
    archived = {row["id"]: dict(row) for row in archived_rows}
    assert len(archived) == 4
    draft_row = archived[f"dataset_drafts:{draft['id']}"]
    preserved = json.loads(draft_row["record"])
    assert json.loads(preserved["record"])["draft_id"] == "draft-alpha"
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(aiosqlite.IntegrityError, match="read-only"):
            await connection.execute(
                "UPDATE evaluation_readonly_archive SET record = '{}'",
            )

    # A later upgrade recreates the empty expansion beside the
    # preserved archive.
    await db.init_db()
    tables = await _table_names()
    for table, _kind in evaluation_records.EXPANSION_TABLES:
        assert table in tables, table
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS rows_present FROM dataset_drafts",
        )
        row = await cursor.fetchone()
        assert int(row["rows_present"]) == 0
        cursor = await connection.execute(
            "SELECT COUNT(*) AS rows_present "
            "FROM evaluation_readonly_archive",
        )
        row = await cursor.fetchone()
        assert int(row["rows_present"]) == 4
    assert await _legacy_snapshot() == before
