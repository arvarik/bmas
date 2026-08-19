"""Tests for the benchmark identity and audit foundation."""

import json

import aiosqlite
import pytest
import pytest_asyncio

import database as db
from benchmarks.provenance import build_execution_snapshot


@pytest_asyncio.fixture
async def benchmark_db(tmp_path, monkeypatch):
    database_path = str(tmp_path / "benchmark.db")
    monkeypatch.setattr(db, "DB_PATH", database_path)
    await db.init_db()
    return database_path


def _item(item_id: str, item_key: str) -> dict:
    return {
        "id": item_id,
        "item_key": item_key,
        "input": f"Question {item_key}",
        "expected_output": f"Answer {item_key}",
        "subject": "math",
        "split": "test",
        "tags": ["smoke"],
        "metadata": {"difficulty": "easy"},
    }


@pytest.mark.asyncio
async def test_v8_creates_benchmark_tables_and_scorers(benchmark_db):
    async with aiosqlite.connect(benchmark_db) as connection:
        table_rows = await connection.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        tables = {row[0] for row in table_rows}
        scorer_rows = await connection.execute_fetchall(
            "SELECT id FROM benchmark_scorers ORDER BY id"
        )

    assert {
        "datasets",
        "dataset_versions",
        "dataset_items",
        "benchmark_tests",
        "benchmark_test_revisions",
        "benchmark_test_arms",
        "benchmark_runs",
        "benchmark_trials",
        "benchmark_attempts",
        "benchmark_scores",
        "benchmark_artifacts",
        "operator_actions",
    } <= tables
    assert [row[0] for row in scorer_rows] == [
        "scorer-gsm8k-numeric-v1",
        "scorer-mmlu-letter-v1",
    ]


@pytest.mark.asyncio
async def test_published_dataset_version_is_immutable(benchmark_db):
    await db.create_dataset_version(
        dataset_id="dataset-one",
        version_id="version-one",
        name="Dataset one",
        description="A stable dataset",
        source_uri=None,
        license_name="MIT",
        author="Test",
        dataset_metadata={},
        checksum="canonical-checksum",
        schema={"version": "1"},
        source_filename="one.csv",
        source_mime="text/csv",
        source_checksum="source-checksum",
        source_path="/tmp/one.csv",
        version_metadata={},
        items=[_item("item-one", "one")],
    )

    dataset = await db.get_dataset("dataset-one")
    items, total = await db.list_dataset_items("version-one")

    assert dataset is not None
    assert dataset["versions"][0]["status"] == "published"
    assert dataset["versions"][0]["source_checksum"] == "source-checksum"
    assert total == 1
    assert items[0]["tags"] == ["smoke"]

    async with aiosqlite.connect(benchmark_db) as connection:
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE dataset_items SET input = 'changed' WHERE id = 'item-one'"
            )


@pytest.mark.asyncio
async def test_dataset_versions_reject_duplicate_content(benchmark_db):
    arguments = {
        "dataset_id": "dataset-one",
        "name": "Dataset one",
        "description": "",
        "source_uri": None,
        "license_name": None,
        "author": None,
        "dataset_metadata": {},
        "checksum": "same-checksum",
        "schema": {"version": "1"},
        "source_filename": "one.jsonl",
        "source_mime": "application/x-ndjson",
        "source_checksum": "source-checksum",
        "source_path": "/tmp/one.jsonl",
        "version_metadata": {},
        "items": [_item("item-one", "one")],
    }
    await db.create_dataset_version(version_id="version-one", **arguments)

    with pytest.raises(db.DatasetVersionConflict):
        await db.create_dataset_version(
            version_id="version-two",
            items=[_item("item-two", "two")],
            **{key: value for key, value in arguments.items() if key != "items"},
        )


@pytest.mark.asyncio
async def test_new_version_preserves_existing_dataset_metadata(benchmark_db):
    common = {
        "dataset_id": "dataset-one",
        "name": "Dataset one",
        "dataset_metadata": {},
        "schema": {"version": "1"},
        "source_mime": "text/csv",
        "version_metadata": {},
    }
    await db.create_dataset_version(
        version_id="version-one",
        description="Original description",
        source_uri="https://example.test/source",
        license_name="MIT",
        author="Original author",
        checksum="checksum-one",
        source_filename="one.csv",
        source_checksum="source-one",
        source_path="/tmp/one.csv",
        items=[_item("item-one", "one")],
        **common,
    )
    await db.create_dataset_version(
        version_id="version-two",
        description="",
        source_uri=None,
        license_name=None,
        author=None,
        checksum="checksum-two",
        source_filename="two.csv",
        source_checksum="source-two",
        source_path="/tmp/two.csv",
        items=[_item("item-two", "two")],
        **common,
    )

    dataset = await db.get_dataset("dataset-one")

    assert dataset is not None
    assert dataset["description"] == "Original description"
    assert dataset["source_uri"] == "https://example.test/source"
    assert dataset["license"] == "MIT"
    assert dataset["author"] == "Original author"
    assert [version["version"] for version in dataset["versions"]] == [2, 1]


@pytest.mark.asyncio
async def test_operator_action_idempotency_preserves_one_outcome(benchmark_db):
    await db.create_task("task-one", "Task one", "Run a task")

    requested, created = await db.claim_operator_action(
        action_id="action-one",
        task_id="task-one",
        action="pause",
        actor="operator-a",
        detail={"reason": "inspect"},
    )
    replayed, replay_created = await db.claim_operator_action(
        action_id="action-one",
        task_id="task-one",
        action="pause",
        actor="operator-a",
        detail={"reason": "inspect"},
    )
    completed = await db.finish_operator_action(
        action_id="action-one",
        status="accepted",
        detail={"state": "pause_requested"},
    )

    assert created is True
    assert replay_created is False
    assert replayed["action_id"] == requested["action_id"]
    assert completed["status"] == "accepted"

    with pytest.raises(db.EventIdempotencyConflict):
        await db.finish_operator_action(
            action_id="action-one",
            status="failed",
            detail={"error": "late failure"},
        )


@pytest.mark.asyncio
async def test_cancellation_has_a_distinct_terminal_identity(benchmark_db):
    await db.create_task("task-one", "Task one", "Run a task")

    assert await db.request_task_cancellation("task-one") is True
    cancelling = await db.get_task("task-one")
    assert cancelling is not None
    assert cancelling["run_state"] == "cancelling"
    assert cancelling["cancel_requested_at"] is not None

    assert await db.cancel_task("task-one", "operator") is True
    cancelled = await db.get_task("task-one")
    assert cancelled is not None
    assert cancelled["status"] == "failed"
    assert cancelled["terminal_kind"] == "cancelled"
    assert cancelled["failure_category"] == "cancelled"


def test_execution_snapshot_is_stable_and_secret_free(monkeypatch):
    monkeypatch.setenv("BMAS_BUILD_REVISION", "revision-one")
    configuration = {
        "models": {"researcher": "model-a"},
        "persona": "researcher",
        "api_key": "secret-value",
    }

    first, first_checksum = build_execution_snapshot(
        runtime_id="classic",
        effective_configuration=configuration,
        submission_overrides={"rounds": 4},
    )
    second, second_checksum = build_execution_snapshot(
        runtime_id="classic",
        effective_configuration=json.loads(json.dumps(configuration)),
        submission_overrides={"rounds": 4},
    )

    assert first == second
    assert first_checksum == second_checksum
    assert first["runtime"]["configuration"]["api_key"] == "[redacted]"
    assert first["runtime"]["id"] == "classic"
