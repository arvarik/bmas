"""Tests for benchmark authoring, materialization, retries, and scoring."""

import aiosqlite
import pytest
import pytest_asyncio

import database as db
from benchmarks import repository
from benchmarks.provenance import content_checksum
from benchmarks.scoring import score_output


@pytest_asyncio.fixture
async def execution_db(tmp_path, monkeypatch):
    database_path = str(tmp_path / "execution.db")
    monkeypatch.setattr(db, "DB_PATH", database_path)
    await db.init_db()
    await db.create_dataset_version(
        dataset_id="dataset-one",
        version_id="version-one",
        name="Dataset one",
        description="",
        source_uri=None,
        license_name=None,
        author=None,
        dataset_metadata={},
        checksum="dataset-checksum",
        schema={"version": "1"},
        source_filename="one.jsonl",
        source_mime="application/x-ndjson",
        source_checksum="source-checksum",
        source_path="/tmp/one.jsonl",
        version_metadata={},
        items=[{
            "id": "item-one",
            "item_key": "one",
            "input": "What is 20 plus 22?",
            "expected_output": "42",
            "subject": "math",
            "split": "test",
            "tags": [],
            "metadata": {},
        }],
    )
    return database_path


async def _test_revision():
    envelope = {
        "schema_version": "1",
        "runtime_id": "classic",
        "submission_overrides": {},
        "effective_configuration": {"max_rounds": 2},
    }
    return await repository.create_test_revision(
        test_id="test-one",
        revision_id="revision-one",
        name="Test one",
        description="",
        dataset_version_id="version-one",
        configuration={
            "schema_version": "1",
            "repetitions": 2,
            "seed": 50,
            "max_concurrency": 1,
            "timeout_seconds": 60,
            "cost_limit_usd": None,
        },
        arms=[{
            "id": "arm-one",
            "name": "Classic",
            "slug": "classic",
            "runtime_id": "classic",
            "configuration": envelope,
            "configuration_checksum": content_checksum(envelope),
        }],
        scorers=[{"id": "scorer-gsm8k-numeric-v1", "configuration": {}}],
    )


@pytest.mark.asyncio
async def test_published_revision_and_scorer_links_are_immutable(execution_db):
    await _test_revision()
    async with aiosqlite.connect(execution_db) as connection:
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE benchmark_test_revision_scorers SET sort_order = 2 "
                "WHERE test_revision_id = 'revision-one'"
            )


@pytest.mark.asyncio
async def test_run_materialization_is_idempotent_and_repeated(execution_db):
    await _test_revision()
    first, created = await repository.create_run(
        run_id="run-one",
        revision_id="revision-one",
        idempotency_key="request-one",
    )
    replay, replay_created = await repository.create_run(
        run_id="run-two",
        revision_id="revision-one",
        idempotency_key="request-one",
    )

    assert created is True
    assert replay_created is False
    assert replay["id"] == first["id"] == "run-one"
    assert [(item["repeat_index"], item["retry_index"]) for item in first["attempts"]] == [
        (1, 0),
        (2, 0),
    ]
    assert [item["random_seed"] for item in first["attempts"]] == [51, 52]


@pytest.mark.asyncio
async def test_claim_respects_run_concurrency(execution_db):
    await _test_revision()
    await repository.create_run(
        run_id="run-one",
        revision_id="revision-one",
        idempotency_key=None,
    )
    first = await repository.claim_next_attempt()
    second = await repository.claim_next_attempt()
    active = await repository.active_attempts()

    assert first is not None
    assert second is None
    assert active[0]["id"] == first["id"]
    assert active[0]["run_cost_usd"] == 0


@pytest.mark.asyncio
async def test_cancel_excludes_queued_attempts_and_retry_preserves_history(execution_db):
    await _test_revision()
    await repository.create_run(
        run_id="run-one",
        revision_id="revision-one",
        idempotency_key=None,
    )

    task_ids = await repository.set_run_state("run-one", "cancel")
    cancelled = await repository.get_run("run-one")
    retry_count = await repository.retry_failed_attempts("run-one")
    retried = await repository.get_run("run-one")

    assert task_ids == []
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert {score["status"] for score in cancelled["scores"]} == {"excluded"}
    assert retry_count == 2
    assert retried is not None
    assert retried["status"] == "queued"
    assert [(item["repeat_index"], item["retry_index"]) for item in retried["attempts"]] == [
        (1, 0),
        (1, 1),
        (2, 0),
        (2, 1),
    ]


def test_versioned_scorers_extract_numeric_letter_and_text_answers():
    numeric = score_output(
        scorer={"kind": "numeric_match", "version": "1"},
        expected_output="42",
        actual_output="The result is \\boxed{42}.",
    )
    letter = score_output(
        scorer={"kind": "letter_match", "version": "1"},
        expected_output="B",
        actual_output="Answer: B",
    )
    exact = score_output(
        scorer={"kind": "exact_match", "version": "1"},
        expected_output="Ready",
        actual_output=" Ready ",
    )

    assert numeric["passed"] is True
    assert letter["passed"] is True
    assert exact["passed"] is True
