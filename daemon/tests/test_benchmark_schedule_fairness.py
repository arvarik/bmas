"""Paired seeds, stored arm order, dispatch ranks, and fair turns.

The suite verifies the paired-schedule contract: every arm shares the
same item and repetition seed, the stored arm-order schedule decides
dispatch order inside a run, each dispatch stores one immutable rank,
requeue preserves earlier ranks under a new eligibility generation,
and the weighted round-robin gives no run a second equal-priority turn
while another eligible run waits. Crossing the frozen starvation limit
promotes the waiting run and records a scheduler event.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio

import database as db
from benchmarks import admission, repository
from benchmarks.capacity import CapacityPolicy
from benchmarks.provenance import content_checksum

ITEM_COUNT = 4
WIDE_OPEN = CapacityPolicy(global_limit=500)


@pytest_asyncio.fixture
async def fairness_db(tmp_path, monkeypatch):
    path = str(tmp_path / "fairness.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    await db.init_db()
    await db.create_dataset_version(
        dataset_id="dataset-fairness",
        version_id="version-fairness",
        name="Fairness data",
        description="",
        source_uri=None,
        license_name=None,
        author=None,
        dataset_metadata={},
        checksum="dataset-fairness-checksum",
        schema={"version": "1"},
        source_filename="fairness.jsonl",
        source_mime="application/x-ndjson",
        source_checksum="source-fairness-checksum",
        source_path="/tmp/fairness.jsonl",
        version_metadata={},
        items=[
            {
                "id": f"item-{index}",
                "item_key": f"case-{index}",
                "input": f"Question {index}",
                "expected_output": "Answer",
                "subject": "algebra" if index % 2 else "geometry",
                "split": "test",
                "tags": [],
                "metadata": {},
            }
            for index in range(ITEM_COUNT)
        ],
    )
    return path


def _arm(identifier: str, slug: str) -> dict:
    envelope = {
        "runtime_id": "classic",
        "effective_configuration": {"model_routing": {"medium": "model-a"}},
    }
    return {
        "id": identifier,
        "name": slug.title(),
        "slug": slug,
        "runtime_id": "classic",
        "configuration": envelope,
        "configuration_checksum": content_checksum(envelope),
    }


async def _revision(
    identifier: str,
    *,
    arms: int = 2,
    repetitions: int = 2,
    seed: int = 7,
) -> None:
    await repository.create_test_revision(
        test_id=f"test-{identifier}",
        revision_id=f"revision-{identifier}",
        name=identifier,
        description="",
        dataset_version_id="version-fairness",
        configuration={
            "repetitions": repetitions,
            "seed": seed,
            "max_concurrency": 32,
            "timeout_seconds": 60,
            "practical_difference": 0.01,
        },
        arms=[
            _arm(f"arm-{identifier}-{index}", f"arm-{index}")
            for index in range(arms)
        ],
        scorers=[{"id": "scorer-exact-match-v1", "configuration": {}}],
    )


async def _attempt_rows(run_id: str) -> list[dict]:
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT attempt.*, trial.dataset_item_id, trial.test_arm_id "
            "FROM benchmark_attempts AS attempt "
            "JOIN benchmark_trials AS trial ON trial.id = attempt.trial_id "
            "WHERE trial.run_id = ? "
            "ORDER BY attempt.schedule_rank",
            (run_id,),
        )
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_every_arm_shares_the_item_and_repetition_seed(fairness_db):
    await _revision("seeds")
    run, _ = await repository.create_run(
        run_id="run-seeds",
        revision_id="revision-seeds",
        idempotency_key=None,
    )
    rows = await _attempt_rows("run-seeds")
    by_slot: dict[tuple[str, int], set[int]] = {}
    for row in rows:
        key = (str(row["dataset_item_id"]), int(row["repeat_index"]))
        by_slot.setdefault(key, set()).add(int(row["random_seed"]))
    # Two arms, four items, two repetitions: eight slots, one shared
    # seed per slot across both arms.
    assert len(by_slot) == ITEM_COUNT * 2
    assert all(len(seeds) == 1 for seeds in by_slot.values())
    seed = next(
        seeds for key, seeds in by_slot.items() if key == ("item-0", 1)
    )
    assert seeds_value(seed) == 7 + 0 * 1_000 + 1
    assert run["execution_plan"]["seed_scope"] == "item-repetition"
    for row in rows:
        assert row["seed_control"] == "recorded"
        snapshot = row["execution_snapshot"]
        assert '"seed_control":"recorded"' in snapshot
        assert '"seed_scope":"item-repetition"' in snapshot


def seeds_value(seeds: set[int]) -> int:
    assert len(seeds) == 1
    return next(iter(seeds))


@pytest.mark.asyncio
async def test_submission_carries_the_seed_and_admission_identity(
    fairness_db,
):
    await _revision("submission")
    await repository.create_run(
        run_id="run-submission",
        revision_id="revision-submission",
        idempotency_key=None,
    )
    attempt = await repository.claim_next_attempt(
        "worker-a", capacity_policy=WIDE_OPEN,
    )
    assert attempt is not None
    submission = admission.build_submission(attempt)
    context = submission.benchmark
    assert context is not None
    assert context.random_seed == int(attempt["random_seed"])
    assert context.seed_control == "recorded"
    assert context.admission_key == str(attempt["id"])
    assert context.request_digest == admission.request_digest_for(attempt)
    assert len(context.request_digest) == 64


@pytest.mark.asyncio
async def test_dispatch_order_follows_the_stored_arm_order(fairness_db):
    await _revision("order")
    await repository.create_run(
        run_id="run-order",
        revision_id="revision-order",
        idempotency_key=None,
    )
    claimed: list[dict] = []
    while True:
        attempt = await repository.claim_next_attempt(
            "worker-a", capacity_policy=WIDE_OPEN,
        )
        if attempt is None:
            break
        claimed.append(attempt)
    assert len(claimed) == ITEM_COUNT * 2 * 2
    ranks = [int(attempt["schedule_rank"]) for attempt in claimed]
    # The dispatch order equals the stored schedule order.
    assert ranks == sorted(ranks)
    # Arms interleave: each slot dispatches both arms back to back,
    # and the slot rotation alternates which arm starts.
    positions = [rank % 2 for rank in ranks]
    assert positions == [0, 1] * (ITEM_COUNT * 2)
    records = await repository.run_dispatch_records("run-order")
    assert [int(row["ticket"]) for row in records["ranks"]] == list(
        range(1, len(claimed) + 1),
    )
    assert [int(row["arm_position"]) for row in records["ranks"]] == positions


@pytest.mark.asyncio
async def test_requeue_keeps_the_old_rank_and_opens_a_new_generation(
    fairness_db,
):
    await _revision("requeue", arms=1, repetitions=1)
    await repository.create_run(
        run_id="run-requeue",
        revision_id="revision-requeue",
        idempotency_key=None,
    )
    first = await repository.claim_next_attempt(
        "worker-a", capacity_policy=WIDE_OPEN,
    )
    assert first is not None
    released = await repository.release_attempt(
        str(first["id"]), "capacity", str(first["lease_token"]),
    )
    assert released
    second = await repository.claim_next_attempt(
        "worker-a", capacity_policy=WIDE_OPEN,
    )
    assert second is not None and second["id"] == first["id"]
    assert int(second["eligibility_generation"]) == 2
    records = await repository.run_dispatch_records("run-requeue")
    generations = [
        int(row["eligibility_generation"])
        for row in records["ranks"]
        if row["attempt_id"] == first["id"]
    ]
    # The earlier rank survives the requeue; the new claim creates the
    # next generation's rank.
    assert generations == [1, 2]


@pytest.mark.asyncio
async def test_a_stored_dispatch_rank_is_immutable(fairness_db):
    await _revision("immutable", arms=1, repetitions=1)
    await repository.create_run(
        run_id="run-immutable",
        revision_id="revision-immutable",
        idempotency_key=None,
    )
    claimed = await repository.claim_next_attempt(
        "worker-a", capacity_policy=WIDE_OPEN,
    )
    assert claimed is not None
    async with aiosqlite.connect(fairness_db) as connection:
        with pytest.raises(aiosqlite.IntegrityError, match="immutable"):
            await connection.execute(
                "UPDATE benchmark_dispatch_ranks SET ticket = 99",
            )


@pytest.mark.asyncio
async def test_no_second_equal_priority_turn_while_a_peer_waits(
    fairness_db,
):
    await _revision("peers")
    for name in ("run-peer-one", "run-peer-two"):
        await repository.create_run(
            run_id=name,
            revision_id="revision-peers",
            idempotency_key=None,
        )
    turns = []
    for _ in range(6):
        attempt = await repository.claim_next_attempt(
            "worker-a", capacity_policy=WIDE_OPEN,
        )
        assert attempt is not None
        turns.append(str(attempt["run_id"]))
    # Equal priority, equal weight: the turns alternate strictly, so
    # one run can never take two turns while the other waits.
    for index in range(1, len(turns)):
        assert turns[index] != turns[index - 1]


@pytest.mark.asyncio
async def test_weighted_bands_share_turns_without_starvation(fairness_db):
    await _revision("bands")
    band_priorities = {
        "run-band-expedited": 20,
        "run-band-standard": 0,
        "run-band-deferred": -5,
    }
    for name, priority in band_priorities.items():
        await repository.create_run(
            run_id=name,
            revision_id="revision-bands",
            idempotency_key=None,
            priority=priority,
        )
    turns: dict[str, int] = dict.fromkeys(band_priorities, 0)
    for _ in range(14):
        attempt = await repository.claim_next_attempt(
            "worker-a", capacity_policy=WIDE_OPEN,
        )
        assert attempt is not None
        turns[str(attempt["run_id"])] += 1
    # Weighted behavior: 4:2:1 turn shares, and even the deferred band
    # receives turns, so no band starves.
    assert turns["run-band-expedited"] == 8
    assert turns["run-band-standard"] == 4
    assert turns["run-band-deferred"] == 2


@pytest.mark.asyncio
async def test_twenty_runs_across_three_bands_stay_bounded(fairness_db):
    await _revision("spread", arms=1, repetitions=1)
    priorities = [20] * 7 + [0] * 7 + [-5] * 6
    for index, priority in enumerate(priorities):
        await repository.create_run(
            run_id=f"run-spread-{index:02d}",
            revision_id="revision-spread",
            idempotency_key=None,
            priority=priority,
        )
    served: list[str] = []
    for _ in range(20 * ITEM_COUNT):
        attempt = await repository.claim_next_attempt(
            "worker-a", capacity_policy=WIDE_OPEN,
        )
        if attempt is None:
            break
        served.append(str(attempt["run_id"]))
    # Every run's schedule drains completely: weighted turns delay the
    # deferred band, they never starve it.
    assert sorted(set(served)) == sorted(
        f"run-spread-{index:02d}" for index in range(20)
    )
    assert len(served) == 20 * ITEM_COUNT


@pytest.mark.asyncio
async def test_crossing_the_starvation_limit_promotes_the_band(
    fairness_db, monkeypatch,
):
    monkeypatch.setattr(repository, "STARVATION_PROMOTION_LIMIT", 3)
    await _revision("starve")
    await repository.create_run(
        run_id="run-starve-heavy",
        revision_id="revision-starve",
        idempotency_key=None,
        priority=20,
    )
    await repository.create_run(
        run_id="run-starve-waiting",
        revision_id="revision-starve",
        idempotency_key=None,
        priority=-5,
    )
    for _ in range(4):
        attempt = await repository.claim_next_attempt(
            "worker-a", capacity_policy=WIDE_OPEN,
        )
        assert attempt is not None
    records = await repository.run_dispatch_records("run-starve-waiting")
    promotions = [
        event
        for event in records["events"]
        if event["event_type"] == "priority_promotion"
    ]
    assert promotions, "Crossing the limit records one promotion event"
    payload = promotions[0]["payload"]
    assert payload["old_band"] == "deferred"
    assert payload["new_band"] == "standard"
    assert payload["starvation_limit"] == 3
    # The promotion bounds the next dispatch: the waiting run now wins
    # a turn ahead of the heavy run's next turn.
    followers = []
    for _ in range(3):
        attempt = await repository.claim_next_attempt(
            "worker-a", capacity_policy=WIDE_OPEN,
        )
        assert attempt is not None
        followers.append(str(attempt["run_id"]))
    assert "run-starve-waiting" in followers
