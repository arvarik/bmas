"""Task admission through the shared effect ledger, at every crash point.

Every benchmark attempt admits its task through the Foundation effect
ledger under one stable idempotency key. The suite crashes the chain
at each declared boundary and asserts idempotent recovery: one
external task per stable key, the raw admission response stored before
parsing, the original task linked after every recoverable crash, a
lost fence unable to link, an equal key with a different request
digest rejected, and an unknown outcome kept visible with its
pessimistic charge until proof or operator approval resolves it.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

import budget_service as budget
import database as db
import effect_service as effects
from benchmarks import admission, repository
from benchmarks.capacity import CapacityPolicy
from benchmarks.provenance import content_checksum
from core.failpoints import InjectedFaultError, armed
from routes import submit

ITEM_COUNT = 8
WIDE_OPEN = CapacityPolicy(global_limit=500)


@pytest_asyncio.fixture
async def admission_db(tmp_path, monkeypatch):
    path = str(tmp_path / "admission.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    await db.create_dataset_version(
        dataset_id="dataset-admission",
        version_id="version-admission",
        name="Admission data",
        description="",
        source_uri=None,
        license_name=None,
        author=None,
        dataset_metadata={},
        checksum="dataset-admission-checksum",
        schema={"version": "1"},
        source_filename="admission.jsonl",
        source_mime="application/x-ndjson",
        source_checksum="source-admission-checksum",
        source_path="/tmp/admission.jsonl",
        version_metadata={},
        items=[
            {
                "id": f"item-{index}",
                "item_key": f"case-{index}",
                "input": f"Question {index}",
                "expected_output": "Answer",
                "subject": "test",
                "split": "test",
                "tags": [],
                "metadata": {},
            }
            for index in range(ITEM_COUNT)
        ],
    )
    envelope = {
        "runtime_id": "classic",
        "effective_configuration": {"model_routing": {"medium": "model-a"}},
    }
    await repository.create_test_revision(
        test_id="test-admission",
        revision_id="revision-admission",
        name="admission",
        description="",
        dataset_version_id="version-admission",
        configuration={
            "repetitions": 1,
            "seed": 3,
            "max_concurrency": 32,
            "timeout_seconds": 60,
            "practical_difference": 0.01,
            "cost_limit_usd": "4",
        },
        arms=[{
            "id": "arm-admission",
            "name": "Classic",
            "slug": "classic",
            "runtime_id": "classic",
            "configuration": envelope,
            "configuration_checksum": content_checksum(envelope),
        }],
        scorers=[{"id": "scorer-exact-match-v1", "configuration": {}}],
    )
    await repository.create_run(
        run_id="run-admission",
        revision_id="revision-admission",
        idempotency_key=None,
    )
    previous_queue = submit._task_queue
    submit._task_queue = asyncio.Queue(maxsize=100)
    previous_ids = set(submit._scheduled_ids)
    submit._scheduled_ids.clear()
    yield path
    submit._task_queue = previous_queue
    submit._scheduled_ids.clear()
    submit._scheduled_ids.update(previous_ids)


async def _claim() -> dict:
    attempt = await repository.claim_next_attempt(
        "worker-a", capacity_policy=WIDE_OPEN,
    )
    assert attempt is not None
    return attempt


async def _count(sql: str, *parameters) -> int:
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(sql, parameters)
        row = await cursor.fetchone()
    return int(row[0])


async def _ledger_attempts(attempt_id: str) -> list[dict]:
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT attempt.* FROM effect_attempts AS attempt "
            "JOIN effect_operations AS operation "
            "ON operation.effect_operation_id = attempt.effect_operation_id "
            "WHERE operation.idempotency_scope = ? "
            "AND operation.child_idempotency_key = ? "
            "ORDER BY attempt.effect_attempt_number",
            (admission.ADMISSION_SCOPE, attempt_id),
        )
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_the_admission_chain_records_every_declared_state(
    admission_db,
):
    attempt = await _claim()
    response = await admission.admit_attempt(attempt)
    task_id = admission.admission_task_id(str(attempt["id"]))
    assert response["task_id"] == task_id

    task = await db.get_task(task_id)
    assert task is not None
    metadata = await db.get_board_meta(task_id)
    # The task request carried the shared seed, its control label, the
    # stable admission key, and the request digest.
    context = metadata["benchmark"]
    assert context["random_seed"] == int(attempt["random_seed"])
    assert context["seed_control"] == "recorded"
    assert context["admission_key"] == str(attempt["id"])
    assert context["request_digest"] == admission.request_digest_for(attempt)

    ledger = await _ledger_attempts(str(attempt["id"]))
    assert len(ledger) == 1
    record = ledger[0]
    assert record["state"] == "observed"
    assert record["observed_outcome"] == "admitted"
    # The raw admission response persisted before any parsing.
    assert record["raw_response_artifact_digest"]
    reservation = await budget.get_reservation(
        str(record["reservation_id"]),
    )
    assert reservation["state"] == "reserved"

    await admission.record_admission_link(attempt)
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT admission_effect_id, admission_reservation_id, task_id "
            "FROM benchmark_attempts WHERE id = ?",
            (str(attempt["id"]),),
        )
        row = await cursor.fetchone()
    assert row["admission_effect_id"] == record["effect_id"]
    assert row["admission_reservation_id"] == record["reservation_id"]
    assert row["task_id"] == task_id

    run = await repository.get_run("run-admission")
    assert run is not None
    assert run["authority_run_id"] == admission.authority_run_id(
        "run-admission",
    )
    assert run["budget_id"] == "benchmark-budget-run-admission"


@pytest.mark.asyncio
async def test_every_crash_point_recovers_with_one_task(admission_db):
    crash_points = (
        # After lease and before task admission: the intent commit.
        "journal.before_commit",
        # After the reservation request and before the reserve commit.
        "budget.before_commit",
        # After dispatch_claimed and before the task call.
        "benchmark.before_task_call",
        # After task creation and before raw response storage.
        "benchmark.after_task_call",
        # Inside raw response storage, before and after the artifact.
        "effect.before_raw_persist",
        "effect.after_raw_persist",
        # After response parsing and before attempt linking.
        "benchmark.before_attempt_link",
    )
    for point in crash_points:
        attempt = await _claim()
        # The run authority exists after the first pass; creating it
        # first keeps each armed fault on the admission chain itself.
        await admission.ensure_run_authority(attempt)
        with armed(point), pytest.raises(InjectedFaultError):
            await admission.admit_attempt(attempt)
        response = await admission.admit_attempt(attempt)
        task_id = admission.admission_task_id(str(attempt["id"]))
        assert response["task_id"] == task_id, point
        # One stable key, one external task, one linked attempt.
        tasks = await _count(
            "SELECT COUNT(*) FROM tasks WHERE id = ?", task_id,
        )
        assert tasks == 1, point
        linked = await _count(
            "SELECT COUNT(*) FROM benchmark_attempts "
            "WHERE id = ? AND task_id = ?",
            str(attempt["id"]),
            task_id,
        )
        assert linked == 1, point
        operations = await _count(
            "SELECT COUNT(*) FROM effect_operations "
            "WHERE idempotency_scope = ? AND child_idempotency_key = ?",
            admission.ADMISSION_SCOPE,
            str(attempt["id"]),
        )
        assert operations == 1, point


@pytest.mark.asyncio
async def test_an_equal_key_with_a_different_digest_rejects(admission_db):
    attempt = await _claim()
    await admission.admit_attempt(attempt)
    authority = await admission.ensure_run_authority(attempt)
    with pytest.raises(effects.EffectConflictError):
        await effects.create_effect_intent(
            run_id=authority["journal_run"],
            activation_id=authority["activation_id"],
            activation_attempt=1,
            kind="benchmark_admission",
            request_digest="f" * 64,
            idempotency_scope=admission.ADMISSION_SCOPE,
            child_idempotency_key=str(attempt["id"]),
            reservation_id="reservation-unused",
            retry_safety="safe",
            task_fence=authority["fence"],
        )
    # The task service applies the same rejection: an equal task
    # identity with a different request is an identity conflict.
    from fastapi import HTTPException

    submission = admission.build_submission(attempt)
    changed = submission.model_copy(deep=True)
    assert changed.benchmark is not None
    changed.benchmark.request_digest = "f" * 64
    with pytest.raises(HTTPException) as conflict:
        await submit._admit_task(
            changed,
            task_id=admission.admission_task_id(str(attempt["id"])),
        )
    assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_duplicate_admission_delivery_replays_the_stored_task(
    admission_db,
):
    attempt = await _claim()
    first = await admission.admit_attempt(attempt)
    submission = admission.build_submission(attempt)
    replay = await submit._admit_task(
        submission,
        task_id=admission.admission_task_id(str(attempt["id"])),
    )
    assert replay["task_id"] == first["task_id"]
    count = await _count(
        "SELECT COUNT(*) FROM tasks WHERE id = ?", first["task_id"],
    )
    assert count == 1


@pytest.mark.asyncio
async def test_a_lost_fence_cannot_link_or_replace_the_task(admission_db):
    attempt = await _claim()
    await admission.ensure_run_authority(attempt)
    with armed("benchmark.before_attempt_link"), pytest.raises(
        InjectedFaultError,
    ):
        await admission.admit_attempt(attempt)
    # Another worker takes over the expired lease, so the old fence is
    # lost before the link.
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE benchmark_attempts SET lease_expires_at = "
            "'2000-01-01T00:00:00.000Z' WHERE id = ?",
            (str(attempt["id"]),),
        )
        await connection.commit()
    transferred = await repository.claim_expired_attempt("worker-b")
    assert transferred is not None and transferred["id"] == attempt["id"]
    with pytest.raises(repository.BenchmarkConflict):
        await admission.admit_attempt(attempt)
    # The new fence holder links the original task; no second task
    # appears.
    response = await admission.admit_attempt(transferred)
    task_id = admission.admission_task_id(str(attempt["id"]))
    assert response["task_id"] == task_id
    assert await _count(
        "SELECT COUNT(*) FROM tasks WHERE id = ?", task_id,
    ) == 1


@pytest.mark.asyncio
async def test_an_unknown_outcome_charges_pessimistically(admission_db):
    attempt = await _claim()
    authority = await admission.ensure_run_authority(attempt)
    with armed("benchmark.before_task_call"), pytest.raises(
        InjectedFaultError,
    ):
        await admission.admit_attempt(attempt)
    ledger = await _ledger_attempts(str(attempt["id"]))
    record = ledger[-1]
    assert record["state"] == "dispatch_claimed"
    # Without the authoritative lookup, the outcome stays unknown and
    # the operator decision applies the pessimistic reservation. The
    # unknown admission never becomes a zero-cost cancellation.
    await effects.mark_outcome_unknown(
        run_id=authority["journal_run"],
        effect_id=str(record["effect_id"]),
        reason="lookup_unavailable",
        task_fence=authority["fence"],
    )
    await effects.operator_reconcile_unknown(
        run_id=authority["journal_run"],
        effect_id=str(record["effect_id"]),
        operator_id="operator-a",
        reason="No delivery proof exists",
        task_fence=authority["fence"],
    )
    reservation = await budget.get_reservation(
        str(record["reservation_id"]),
    )
    assert reservation["state"] == "consumed"
    assert int(reservation["consumed_amount_nanos"]) == int(
        reservation["reserved_amount_nanos"],
    )
    assert int(reservation["consumed_amount_nanos"]) > 0


@pytest.mark.asyncio
async def test_settlement_reconciles_cost_and_settles_the_run(
    admission_db,
):
    attempt = await _claim()
    await admission.admit_attempt(attempt)
    await admission.record_admission_link(attempt)
    task_id = admission.admission_task_id(str(attempt["id"]))
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE tasks SET status = 'completed', total_cost_usd = 0.25 "
            "WHERE id = ?",
            (task_id,),
        )
        await connection.execute(
            "UPDATE benchmark_attempts SET status = 'completed', "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ?",
            (str(attempt["id"]),),
        )
        await connection.commit()
    await admission.settle_attempt_admission({
        "id": str(attempt["id"]),
        "run_id": "run-admission",
        "total_cost_usd": 0.25,
    })
    ledger = await _ledger_attempts(str(attempt["id"]))
    assert ledger[-1]["state"] == "reconciled"
    reservation = await budget.get_reservation(
        str(ledger[-1]["reservation_id"]),
    )
    assert reservation["state"] == "consumed"
    # The observed charge reconciles exactly and releases the unused
    # reservation remainder.
    assert int(reservation["consumed_amount_nanos"]) == 250_000_000
    charges = await repository.list_cost_charges("run-admission")
    assert [charge["kind"] for charge in charges] == ["charge"]
    assert charges[0]["amount_nanos"] == 250_000_000
    assert charges[0]["source_kind"] == "legacy_float"

    # The other attempts cancel, the run turns terminal, and the cost
    # settles because every admission effect reconciled.
    await repository.set_run_state("run-admission", "cancel")
    await repository.refresh_run_for_attempt(str(attempt["id"]))
    run = await repository.get_run("run-admission")
    assert run is not None
    assert run["cost_status"] == "settling"
    settled = await admission.try_settle_run("run-admission")
    assert settled
    run = await repository.get_run("run-admission")
    assert run is not None
    assert run["cost_status"] == "settled"
    assert run["settled_cost"] is not None
