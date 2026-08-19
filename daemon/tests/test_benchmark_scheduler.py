"""Tests for benchmark fencing, capacity, priority, and human review."""

import asyncio

import aiosqlite
import pytest
import pytest_asyncio

import database as db
from benchmarks import repository
from benchmarks.capacity import CapacityPolicy
from benchmarks.provenance import content_checksum


@pytest_asyncio.fixture
async def scheduler_db(tmp_path, monkeypatch):
    path = str(tmp_path / "scheduler.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    await db.create_dataset_version(
        dataset_id="dataset-scheduler",
        version_id="version-scheduler",
        name="Scheduler data",
        description="",
        source_uri=None,
        license_name=None,
        author=None,
        dataset_metadata={},
        checksum="dataset-scheduler-checksum",
        schema={"version": "1"},
        source_filename="scheduler.jsonl",
        source_mime="application/x-ndjson",
        source_checksum="source-scheduler-checksum",
        source_path="/tmp/scheduler.jsonl",
        version_metadata={},
        items=[{
            "id": "scheduler-item",
            "item_key": "one",
            "input": "Question",
            "expected_output": "Answer",
            "subject": "test",
            "split": "test",
            "tags": [],
            "metadata": {},
        }],
    )
    return path


async def _revision(identifier: str, *, concurrency: int = 4):
    envelope = {
        "runtime_id": "classic",
        "effective_configuration": {
            "model_routing": {"medium": "model-a"},
        },
    }
    return await repository.create_test_revision(
        test_id=f"test-{identifier}",
        revision_id=f"revision-{identifier}",
        name=identifier,
        description="",
        dataset_version_id="version-scheduler",
        configuration={
            "repetitions": 1,
            "seed": 1,
            "max_concurrency": concurrency,
            "timeout_seconds": 60,
            "practical_difference": 0.01,
        },
        arms=[{
            "id": f"arm-{identifier}",
            "name": "Classic",
            "slug": "classic",
            "runtime_id": "classic",
            "configuration": envelope,
            "configuration_checksum": content_checksum(envelope),
        }],
        scorers=[{"id": "scorer-exact-match-v1", "configuration": {}}],
    )


@pytest.mark.asyncio
async def test_atomic_claim_assigns_one_fenced_owner(scheduler_db):
    await _revision("atomic", concurrency=1)
    await repository.create_run(
        run_id="run-atomic",
        revision_id="revision-atomic",
        idempotency_key=None,
    )
    policy = CapacityPolicy(global_limit=4)

    claims = await asyncio.gather(
        repository.claim_next_attempt("worker-one", capacity_policy=policy),
        repository.claim_next_attempt("worker-two", capacity_policy=policy),
    )

    owned = [claim for claim in claims if claim is not None]
    assert len(owned) == 1
    assert owned[0]["lease_owner"] in {"worker-one", "worker-two"}
    assert owned[0]["lease_fence"] == 1


@pytest.mark.asyncio
async def test_expired_lease_transfer_rejects_stale_writer(scheduler_db):
    await _revision("transfer")
    await repository.create_run(
        run_id="run-transfer",
        revision_id="revision-transfer",
        idempotency_key=None,
    )
    first = await repository.claim_next_attempt("worker-one", lease_seconds=10)
    assert first is not None
    async with aiosqlite.connect(scheduler_db) as connection:
        await connection.execute(
            "UPDATE benchmark_attempts SET lease_expires_at = '2000-01-01T00:00:00.000Z' "
            "WHERE id = ?",
            (first["id"],),
        )
        await connection.commit()

    second = await repository.claim_expired_attempt("worker-two", lease_seconds=10)

    assert second is not None
    assert second["id"] == first["id"]
    assert second["lease_fence"] == 2
    assert await repository.release_attempt(
        first["id"],
        lease_token=first["lease_token"],
    ) is False
    assert await repository.release_attempt(
        second["id"],
        lease_token=second["lease_token"],
    ) is True


@pytest.mark.asyncio
async def test_priority_and_runtime_capacity_control_admission(scheduler_db):
    await _revision("low")
    await _revision("high")
    await repository.create_run(
        run_id="run-low",
        revision_id="revision-low",
        idempotency_key=None,
        priority=-50,
    )
    await repository.create_run(
        run_id="run-high",
        revision_id="revision-high",
        idempotency_key=None,
        priority=50,
    )
    policy = CapacityPolicy(global_limit=4, runtime_limits={"classic": 1})

    first = await repository.claim_next_attempt("worker", capacity_policy=policy)
    second = await repository.claim_next_attempt("worker", capacity_policy=policy)

    assert first is not None
    assert first["run_id"] == "run-high"
    assert second is None


def test_capacity_policy_validates_and_applies_model_provider_limits(monkeypatch):
    monkeypatch.setenv("BMAS_BENCHMARK_MAX_ACTIVE", "7")
    monkeypatch.setenv("BMAS_BENCHMARK_RUNTIME_LIMITS", '{"patchboard": 3}')
    monkeypatch.setenv("BMAS_BENCHMARK_MODEL_LIMITS", '{"model-a": 2}')
    monkeypatch.setenv("BMAS_BENCHMARK_PROVIDER_LIMITS", '{"provider-a": 1}')
    monkeypatch.setenv(
        "BMAS_BENCHMARK_MODEL_PROVIDERS",
        '{"model-a": "provider-a"}',
    )

    policy = CapacityPolicy.from_environment()
    candidate = {
        "runtime_id": "patchboard",
        "arm_configuration": {
            "effective_configuration": {
                "model_routing": {"medium": "model-a"},
            },
        },
    }

    assert policy.global_limit == 7
    assert policy.claims(candidate) == {
        "runtime:patchboard",
        "model:model-a",
        "provider:provider-a",
    }
    assert policy.allows(candidate, [candidate]) is False


def test_capacity_policy_rejects_invalid_json(monkeypatch):
    monkeypatch.setenv("BMAS_BENCHMARK_RUNTIME_LIMITS", "not-json")

    with pytest.raises(ValueError, match="valid JSON"):
        CapacityPolicy.from_environment()


@pytest.mark.asyncio
async def test_capacity_snapshot_and_immutable_human_review(scheduler_db):
    await _revision("review")
    await repository.create_run(
        run_id="run-review",
        revision_id="revision-review",
        idempotency_key=None,
    )
    claim = await repository.claim_next_attempt("worker-review")
    assert claim is not None
    async with aiosqlite.connect(scheduler_db) as connection:
        await connection.execute(
            "UPDATE benchmark_attempts SET status = 'completed' WHERE id = ?",
            (claim["id"],),
        )
        await connection.commit()
    await repository.register_scheduler_worker("worker-review", "host", 7)
    snapshot = await repository.benchmark_capacity_snapshot(
        CapacityPolicy(global_limit=3, runtime_limits={"classic": 2})
    )
    review, created = await repository.create_human_review(
        review_id="review-one",
        attempt_id=claim["id"],
        reviewer_id="tester",
        score=1.0,
        passed=True,
        note="Correct",
        idempotency_key="review-request-one",
    )
    replay, replay_created = await repository.create_human_review(
        review_id="review-two",
        attempt_id=claim["id"],
        reviewer_id="tester",
        score=1.0,
        passed=True,
        note="Correct",
        idempotency_key="review-request-one",
    )

    assert snapshot["global"] == {"active": 0, "limit": 3, "available": 3}
    assert snapshot["workers"][0]["worker_id"] == "worker-review"
    assert created is True
    assert replay_created is False
    assert replay["id"] == review["id"]
    async with aiosqlite.connect(scheduler_db) as connection:
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE benchmark_human_reviews SET score = 0 WHERE id = 'review-one'"
            )


@pytest.mark.asyncio
async def test_worker_registration_prunes_old_inactive_records(scheduler_db):
    await repository.register_scheduler_worker("worker-old", "old-host", 1)
    async with aiosqlite.connect(scheduler_db) as connection:
        await connection.execute(
            "UPDATE benchmark_scheduler_workers SET status = 'stopped', "
            "last_seen_at = '2000-01-01T00:00:00.000Z' WHERE worker_id = 'worker-old'"
        )
        await connection.commit()

    await repository.register_scheduler_worker("worker-new", "new-host", 2)

    async with aiosqlite.connect(scheduler_db) as connection:
        cursor = await connection.execute(
            "SELECT worker_id FROM benchmark_scheduler_workers ORDER BY worker_id"
        )
        worker_ids = [row[0] for row in await cursor.fetchall()]
    assert worker_ids == ["worker-new"]
