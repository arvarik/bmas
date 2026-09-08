"""The journal-backed board of the Classic native pair.

The native gateway commits every board mutation as one
``proposal_decision`` through the unit of work, with the projection rows
in the same transaction. A replay from cursor zero rebuilds the board
projection digest, the durable projection check compares the rows with
the replay, the checkpoint is a verified snapshot that a restart resumes
under the same fence, and a stale fence, an expired lease, a
cancellation, and a deadline each reject the mutation.
"""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest
import pytest_asyncio

import config
import database as db
import interactive_admission as admission
import runtime_journal as journal
import settings_store
from core import foundation_gates
from core.board_store import InMemoryBoardStore
from core.event_emitter import InMemoryEventEmitter
from core.gateway import LeaseLostError
from core.variants import RuntimeKey, VariantConfigurationError, VariantExecutionRequest
from core.variants.classic import ClassicRuntime
from core.variants.classic import projection as proj
from core.variants.classic.runtime import NativeRunBinding

NATIVE = RuntimeKey("classic", "2")
TASK_ID = "task-native-board"
LOCK_ID = "lock-native-board"
LONG_AGO = "2000-01-01T00:00:00.000Z"


@pytest_asyncio.fixture
async def native_run(tmp_path, monkeypatch):
    """One admitted native run with its context, services, and lease."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "native.db"))
    monkeypatch.setattr(config, "FOUNDATION_GATES", {name: True for name in foundation_gates.PLANNED_WRITER_GATES}, raising=False)
    monkeypatch.setattr(config, "STORAGE_OPERATOR_CONFIRMED", True, raising=False)
    monkeypatch.setattr(config, "REQUIRE_PROVIDER_QUALIFICATION", False, raising=False)
    monkeypatch.setattr(config, "ADMIT_TEST_ONLY_RUNTIMES", True, raising=False)
    monkeypatch.setattr(config, "ROLE_REGISTRY", {
        "planner": {"profile": "planner", "endpoints": ["http://agent.test"]},
        "critic": {"profile": "critic", "endpoints": ["http://agent.test"]},
        "decider": {"profile": "decider", "endpoints": ["http://agent.test"]},
    }, raising=False)
    monkeypatch.setattr(config, "MODEL_PRICING", {
        "test-light": {"input_cost_per_token": 1e-07, "output_cost_per_token": 4e-07, "source": "test"},
    }, raising=False)
    monkeypatch.setattr(settings_store, "_store", None)
    admission.reset_for_tests()
    await db.init_db()
    envelope = await ClassicRuntime.capture_configuration({"effort": "quick"})
    await db.create_task_with_meta(
        TASK_ID, "native board", "Add 20 and 22.", "classic",
        {"effective_configuration": envelope}, runtime_contract_version="2", run_state="staging",
    )
    admitted = await admission.admit_task_run(task_id=TASK_ID, runtime_key=NATIVE, effective_configuration=envelope)
    assert admitted is not None and admitted["new"] is True
    run_id = admitted["run_id"]
    context = await admission.run_context_for(run_id, lease_ref=LOCK_ID)
    services = await admission.runtime_services_for(
        context, lease_owner="orchestrator", lease_fence=LOCK_ID, lease_ttl_seconds=60.0,
        artifact_root=tmp_path / "classic-board",
    )
    assert await services.task_leases.acquire() is True
    binding = ClassicRuntime.bind_run(VariantExecutionRequest(
        task_id=TASK_ID, session_id="s", user_task="Add 20 and 22.", triage=None,
        run_context=context, runtime_services=services,
    ))
    assert binding is not None
    yield {"run_id": run_id, "context": context, "services": services, "binding": binding, "root": tmp_path}
    monkeypatch.setattr(settings_store, "_store", None)


def make_gateway(binding: NativeRunBinding) -> tuple[proj.NativeBoardGateway, InMemoryBoardStore, InMemoryEventEmitter]:
    store = InMemoryBoardStore()
    emitter = InMemoryEventEmitter()
    return proj.NativeBoardGateway(store, emitter, committer=binding), store, emitter


async def runtime_records(run_id: str, operation_type: str) -> list[journal.JournalRecord]:
    return [
        record for record in await journal.read_journal(run_id=run_id)
        if record.operation_type == operation_type
    ]


async def seed_board(gateway: proj.NativeBoardGateway, task_id: str) -> list:
    committed = await gateway.append(
        task_id, "control_unit", ["decision_writer"],
        [{"type": "objective", "title": "Objective", "body": "Add 20 and 22.", "confidence": 1.0,
          "_mutation_id": "genesis:objective:v1"}],
        turn_id="genesis", round_no=0,
    )
    committed += await gateway.append(
        task_id, "planner", ["plan_writer"],
        [{"type": "plan", "title": "Plan", "body": "Add the operands.", "refs": ["e-1"], "confidence": 0.8,
          "_mutation_id": "activation-plan:0"}],
        turn_id="activation-plan", round_no=1,
    )
    committed += await gateway.append(
        task_id, "expert.arithmetic", ["finding_writer"],
        [{"type": "finding", "title": "Sum", "body": "The sum is 42.", "refs": ["e-2"], "confidence": 0.9}],
        turn_id="activation-expert", round_no=1,
    )
    return committed


# ── The unit of work ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_gate_check_runs_before_the_first_native_write(native_run, monkeypatch):
    request = VariantExecutionRequest(
        task_id=TASK_ID, session_id="s", user_task="Add 20 and 22.", triage=None,
        run_context=native_run["context"], runtime_services=native_run["services"],
    )
    assert ClassicRuntime.bind_run(request) is not None
    monkeypatch.setattr(config, "FOUNDATION_GATES", {"runtime_unit_of_work": True, "trace_envelope": False}, raising=False)
    with pytest.raises(VariantConfigurationError, match="trace_envelope"):
        ClassicRuntime.bind_run(request)
    # A request without a run context keeps the delegated path with no native write.
    assert ClassicRuntime.bind_run(dataclasses.replace(request, run_context=None, runtime_services=None)) is None


@pytest.mark.asyncio
async def test_an_append_commits_one_proposal_decision_with_its_projection_rows(native_run):
    binding = native_run["binding"]
    gateway, store, emitter = make_gateway(binding)
    committed = await seed_board(gateway, TASK_ID)
    assert [entry.id for entry in committed] == ["e-1", "e-2", "e-3"]

    decisions = await runtime_records(native_run["run_id"], "proposal_decision")
    assert len(decisions) == 3
    for record in decisions:
        assert record.authority_type == "runtime"
        assert record.producer == "classic-runtime"
        assert record.payload["decision"] == "accepted"
        assert record.payload["policy_set_digest"] == native_run["context"].policy_set_digest
        assert record.payload["board"]["kind"] == "append"
        assert all("body" not in entry for entry in record.payload["board"]["entries"])
    # The mutation identifier is the idempotency token when the engine supplies one.
    assert decisions[0].payload["board"]["mutation_id"] == "genesis:objective:v1"
    assert decisions[2].payload["board"]["mutation_id"] is None

    rows = await db.get_classic_board_projection(native_run["run_id"])
    assert [row["entry_id"] for row in rows] == ["e-1", "e-2", "e-3"]
    assert rows[1]["refs"] == ["e-1"] and rows[1]["activation_id"] == "activation-plan"
    assert rows[2]["author"] == "expert.arithmetic" and rows[2]["confidence"] == "0.9"
    assert rows[0]["journal_cursor"] == decisions[0].journal_cursor
    assert rows[0]["created_at"] == decisions[0].recorded_at
    # The body lives as a promoted artifact and the row holds its digest.
    assert proj.read_text(binding.artifacts, rows[2]["body_digest"]) == "The sum is 42."
    # The hot store carries the transaction time of the journal record.
    assert (await store.get_entry(TASK_ID, "e-3")).created_at == decisions[2].recorded_at
    assert [event[1] for event in emitter.events].count("board_entry") == 3

    # A replay from cursor zero rebuilds the board projection digest.
    board, _run_state, _cursor = await proj.replay_run_board(native_run["run_id"])
    assert proj.board_projection_digest(board) == decisions[-1].payload["checkpoint_digest"]
    assert proj.board_projection_digest(board) == binding.board_digest()
    await journal.verify_durable_projections()


@pytest.mark.asyncio
async def test_a_rejected_proposal_commits_a_rejected_decision_and_no_row(native_run):
    binding = native_run["binding"]
    gateway, store, _emitter = make_gateway(binding)
    committed = await gateway.append(
        TASK_ID, "planner", ["plan_writer"],
        [{"type": "not-a-type", "title": "Bad", "body": "x", "_mutation_id": "activation-bad:0"}],
        turn_id="activation-bad", round_no=1,
    )
    assert committed == []
    decisions = await runtime_records(native_run["run_id"], "proposal_decision")
    assert len(decisions) == 1
    assert decisions[0].payload["decision"] == "rejected"
    assert "Unknown entry type" in decisions[0].payload["rejection"]["reason"]
    assert "board" not in decisions[0].payload
    assert await db.get_classic_board_projection(native_run["run_id"]) == []
    events = await store.get_events(TASK_ID)
    assert [event["event_type"] for event in events] == ["entry_rejected"]
    # The rejection replays from the store without a second journal record.
    again = await gateway.append(
        TASK_ID, "planner", ["plan_writer"],
        [{"type": "not-a-type", "title": "Bad", "body": "x", "_mutation_id": "activation-bad:0"}],
        turn_id="activation-bad", round_no=1,
    )
    assert again == []
    assert len(await runtime_records(native_run["run_id"], "proposal_decision")) == 1
    await journal.verify_durable_projections()


@pytest.mark.asyncio
async def test_a_replayed_mutation_commits_no_second_record(native_run):
    binding = native_run["binding"]
    gateway, _store, _emitter = make_gateway(binding)
    first = await seed_board(gateway, TASK_ID)
    # The same genesis mutation replays from the store and the journal.
    again = await gateway.append(
        TASK_ID, "control_unit", ["decision_writer"],
        [{"type": "objective", "title": "Objective", "body": "Add 20 and 22.", "confidence": 1.0,
          "_mutation_id": "genesis:objective:v1"}],
        turn_id="genesis", round_no=0,
    )
    assert [entry.id for entry in again] == [first[0].id]
    assert len(await runtime_records(native_run["run_id"], "proposal_decision")) == 3
    # The same accepted section commits idempotently against the journal.
    section = copy.deepcopy((await runtime_records(native_run["run_id"], "proposal_decision"))[1].payload["board"])
    mutation = proj.BoardMutation(
        kind="append", decision="accepted", task_id=TASK_ID, actor="planner",
        activation_id="activation-plan", round=1, token="activation-plan:0",
        proposal={"entry": {"type": "plan", "title": "Plan", "body": "Add the operands.", "refs": ["e-1"], "confidence": 0.8, "_mutation_id": "activation-plan:0"},
                  "mutation_id": "activation-plan:0", "entry_id": "e-2"},
        section=section, bodies={"e-2": "Add the operands."},
    )
    board_before = copy.deepcopy(binding.board)
    binding.board = {"entries": {"e-1": board_before["entries"]["e-1"]}, "tombstones": {}}
    record = await binding.commit(mutation)
    assert record.journal_cursor == (await runtime_records(native_run["run_id"], "proposal_decision"))[1].journal_cursor
    binding.board = board_before
    await journal.verify_durable_projections()


@pytest.mark.asyncio
async def test_removals_status_changes_and_archives_write_tombstones(native_run):
    binding = native_run["binding"]
    gateway, store, _emitter = make_gateway(binding)
    await seed_board(gateway, TASK_ID)
    removed = await gateway.remove(
        TASK_ID, "cleaner", ["board_maintenance"], ["e-3"], reason="Cleaner maintenance",
        turn_id="activation-cleaner", round_no=2, mutation_id="activation-cleaner:0",
    )
    assert removed == ["e-3"]
    await gateway.set_status(TASK_ID, "e-2", "superseded", "planner")
    # A second identical status change is a no-op without a journal record.
    await gateway.set_status(TASK_ID, "e-2", "superseded", "planner")
    private = await gateway.append(
        TASK_ID, "expert.arithmetic", ["finding_writer"],
        [{"type": "finding", "title": "Private", "body": "Private note.", "confidence": 0.5,
          "_mutation_id": "activation-private:0"}],
        turn_id="activation-private", round_no=2, space="private:conflict-e-2",
    )
    private_id = private[0].id
    await gateway.archive_space(TASK_ID, "private:conflict-e-2", mutation_id="conflict-archive:e-2")

    decisions = await runtime_records(native_run["run_id"], "proposal_decision")
    assert [record.payload["board"]["kind"] for record in decisions] == [
        "append", "append", "append", "remove", "status", "append", "archive",
    ]
    rows = {row["entry_id"]: row for row in await db.get_classic_board_projection(native_run["run_id"])}
    assert rows["e-3"]["status"] == "removed" and rows["e-3"]["journal_cursor"] == decisions[3].journal_cursor
    assert rows["e-2"]["status"] == "superseded"
    assert rows[private_id]["status"] == "archived"
    tombstones = await db.get_classic_board_tombstones(native_run["run_id"])
    assert [(row["entry_id"], row["reason"], row["actor"]) for row in tombstones] == [
        ("e-3", "Cleaner maintenance", "cleaner"), (private_id, "space archived", "control_unit"),
    ]
    assert (await store.get_entry(TASK_ID, "e-3")).status == "removed"
    assert private_id not in await store.get_snapshot(TASK_ID)
    # The tombstones are immutable rows.
    async with db._connect() as connection:  # noqa: SLF001
        with pytest.raises(Exception, match="immutable"):
            await connection.execute(
                "DELETE FROM classic_board_tombstones WHERE entry_id = 'e-3'",
            )
    board, _run_state, _cursor = await proj.replay_run_board(native_run["run_id"])
    assert proj.board_projection_digest(board) == decisions[-1].payload["checkpoint_digest"] == binding.board_digest()
    await journal.verify_durable_projections()


@pytest.mark.asyncio
async def test_the_durable_projection_check_detects_a_row_that_left_the_journal(native_run):
    gateway, _store, _emitter = make_gateway(native_run["binding"])
    await seed_board(gateway, TASK_ID)
    await journal.verify_durable_projections()
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE classic_board_projection SET status = 'superseded' WHERE entry_id = 'e-2'",
        )
        await connection.commit()
    with pytest.raises(journal.JournalIntegrityError, match="board projection"):
        await journal.verify_durable_projections()


@pytest.mark.asyncio
async def test_the_reducer_rejects_a_malformed_board_section():
    board = journal.empty_board_state()
    section = {"schema_version": "1", "kind": "append", "actor": "planner", "activation_id": "a", "round": 1,
               "mutation_id": None, "entries": [{"entry_id": "e-1", "entry_type": "plan", "author": "planner",
               "round": 1, "space": "public", "body_digest": "0" * 64, "confidence": "0.5", "status": "open"}],
               "status_changes": [], "tombstones": []}
    journal.fold_board_section(board, section, task_id="t", journal_cursor=None, recorded_at=None)
    with pytest.raises(journal.JournalIntegrityError, match="already holds"):
        journal.fold_board_section(board, section, task_id="t", journal_cursor=None, recorded_at=None)
    with pytest.raises(journal.JournalIntegrityError, match="no entry"):
        journal.fold_board_section(board, {**section, "entries": [], "tombstones": [{"entry_id": "e-9"}]},
                                   task_id="t", journal_cursor=None, recorded_at=None)
    with pytest.raises(journal.JournalIntegrityError, match="schema version"):
        journal.fold_board_section(board, {**section, "schema_version": "9"}, task_id="t", journal_cursor=None, recorded_at=None)
    # The content digest ignores the fields the transaction assigns.
    stamped = journal.fold_board_section(
        journal.empty_board_state(), section, task_id="t", journal_cursor=7, recorded_at="2026-09-08T00:00:00.000Z",
    )
    assert proj.board_projection_digest(stamped) == proj.board_projection_digest(board)
    assert stamped["entries"]["e-1"]["journal_cursor"] == 7


# ── Checkpoints and restarts ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_checkpoint_is_a_verified_snapshot_that_a_restart_resumes_under_the_same_fence(native_run):
    binding = native_run["binding"]
    gateway, store, _emitter = make_gateway(binding)
    await seed_board(gateway, TASK_ID)
    await gateway.remove(TASK_ID, "cleaner", ["board_maintenance"], ["e-3"], reason="Cleaner maintenance",
                         turn_id="activation-cleaner", round_no=2, mutation_id="activation-cleaner:0")
    control = {"phase": "Discovery", "round": 2, "budget_spent": 0.0125, "turn_durations": [1.5, 2.25],
               "roster": {"planner": "planning"}, "effective_configuration": {"secret": "static"},
               "variant_checkpoint": {"old": True}}
    checkpoint = await binding.checkpoint(control)
    snapshot = checkpoint["snapshot"]
    journal.verify_snapshot(snapshot)
    assert snapshot["state"]["task_fence"] == native_run["context"].task_fence
    assert snapshot["state"]["board_digest"] == binding.board_digest()
    assert snapshot["last_journal_cursor"] == (await journal.read_journal(run_id=native_run["run_id"]))[-1].journal_cursor
    # The control metadata keeps its exact values and excludes the envelopes.
    restored_control = json.loads(snapshot["state"]["control"])
    assert restored_control["budget_spent"] == 0.0125 and restored_control["turn_durations"] == [1.5, 2.25]
    assert "effective_configuration" not in restored_control and "variant_checkpoint" not in restored_control

    verified = await ClassicRuntime.read_checkpoint(
        checkpoint, run_id=native_run["run_id"], task_fence=native_run["context"].task_fence,
    )
    assert verified.board_digest == binding.board_digest()
    assert verified.control == restored_control
    assert verified.board["entries"]["e-3"]["status"] == "removed"

    # A restart restores the hot store from the projection and the artifacts.
    fresh = InMemoryBoardStore()
    mutation_ids = await proj.board_mutation_ids(native_run["run_id"])
    assert mutation_ids == {"e-1": "genesis:objective:v1", "e-2": "activation-plan:0"}
    repaired = await proj.restore_board_store(
        fresh, TASK_ID, verified.board, binding.artifacts, mutation_ids=mutation_ids,
    )
    assert repaired == 3
    entries = await fresh.get_snapshot(TASK_ID)
    assert {entry_id: entry.status for entry_id, entry in entries.items()} == {
        "e-1": "open", "e-2": "open", "e-3": "removed",
    }
    assert entries["e-2"].body == "Add the operands." and entries["e-2"].refs == ["e-1"]
    assert entries["e-2"].created_by_turn == "activation-plan"
    # A replayed engine mutation finds its restored event and commits nothing new.
    records_before = len(await journal.read_journal(run_id=native_run["run_id"]))
    restored_gateway = proj.NativeBoardGateway(fresh, InMemoryEventEmitter(), committer=binding)
    replayed = await restored_gateway.append(
        TASK_ID, "control_unit", ["decision_writer"],
        [{"type": "objective", "title": "Objective", "body": "Add 20 and 22.", "confidence": 1.0,
          "_mutation_id": "genesis:objective:v1"}],
        turn_id="genesis", round_no=0,
    )
    assert [entry.id for entry in replayed] == ["e-1"]
    assert len(await journal.read_journal(run_id=native_run["run_id"])) == records_before
    # The next gateway identifier never collides with an imported one.
    assert await fresh.get_next_seq(TASK_ID) == 4
    # A store that already matches needs no repair.
    assert await proj.restore_board_store(store, TASK_ID, verified.board, binding.artifacts) == 0

    # A tampered snapshot, a foreign fence, and a foreign run fail closed.
    tampered = copy.deepcopy(checkpoint)
    tampered["snapshot"]["state"]["board_digest"] = "0" * 64
    with pytest.raises(proj.ClassicIntegrityError):
        await ClassicRuntime.read_checkpoint(tampered, run_id=native_run["run_id"], task_fence=native_run["context"].task_fence)
    with pytest.raises(proj.ClassicFenceError):
        await ClassicRuntime.read_checkpoint(checkpoint, run_id=native_run["run_id"], task_fence="fence-stale")
    with pytest.raises(proj.ClassicIntegrityError):
        await ClassicRuntime.read_checkpoint(checkpoint, run_id="run-other", task_fence=native_run["context"].task_fence)
    # A snapshot whose journal moved on still verifies at its own cursor.
    await gateway.set_status(TASK_ID, "e-2", "superseded", "planner")
    later = await ClassicRuntime.read_checkpoint(
        checkpoint, run_id=native_run["run_id"], task_fence=native_run["context"].task_fence,
    )
    assert later.board["entries"]["e-2"]["status"] == "open"


# ── The live run authority ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stale_fence_an_expired_lease_a_deadline_and_a_cancellation_reject_the_mutation(native_run):
    binding = native_run["binding"]
    gateway, store, _emitter = make_gateway(binding)
    await seed_board(gateway, TASK_ID)
    committed = len(await journal.read_journal(run_id=native_run["run_id"]))
    rows = len(await db.get_classic_board_projection(native_run["run_id"]))

    def proposal(index: int) -> list[dict]:
        return [{"type": "finding", "title": f"Late {index}", "body": f"Late finding {index}.", "confidence": 0.5,
                 "_mutation_id": f"activation-late:{index}"}]

    async def rejected(error: type[Exception], index: int) -> None:
        with pytest.raises(error):
            await gateway.append(TASK_ID, "expert.arithmetic", ["finding_writer"], proposal(index),
                                 turn_id="activation-late", round_no=3)
        assert len(await journal.read_journal(run_id=native_run["run_id"])) == committed
        assert len(await db.get_classic_board_projection(native_run["run_id"])) == rows
        assert f"e-{4 + index}" not in await store.get_snapshot(TASK_ID)

    # A stale task fence: the journal refuses the record against the live row.
    stale = NativeRunBinding(
        context=dataclasses.replace(native_run["context"], task_fence="fence-stale"),
        services=native_run["services"], board=copy.deepcopy(binding.board),
        reason_registry=binding.reason_registry,
    )
    stale_gateway = proj.NativeBoardGateway(store, InMemoryEventEmitter(), committer=stale)
    with pytest.raises(LeaseLostError, match="stale"):
        await stale_gateway.append(TASK_ID, "expert.arithmetic", ["finding_writer"], proposal(0),
                                   turn_id="activation-late", round_no=3)
    assert len(await journal.read_journal(run_id=native_run["run_id"])) == committed

    # An expired lease: the run authority denies the mutation.
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute(
            "UPDATE run_controls SET lease_expires_at = ? WHERE run_id = ?",
            (LONG_AGO, native_run["run_id"]),
        )
        await connection.commit()
    await rejected(LeaseLostError, 1)
    control = await db.get_run_control(native_run["run_id"])
    assert control["lease_expired"] == 1
    # The host re-acquires the run lease after the expiry.
    assert await native_run["services"].task_leases.acquire() is True

    # A deadline: the run authority denies the mutation.
    assert await native_run["services"].run_controls.set_deadline("2000-01-01T00:00:00.000Z", "cancel")
    await rejected(proj.RunDeadlineError, 2)

    # A cancellation: the operator abort reaches the run-control row.
    assert await db.request_task_run_cancellation(TASK_ID) == 1
    await rejected(proj.RunCancelledError, 3)

    # The control mutations validate the same authority.
    with pytest.raises(proj.RunCancelledError):
        await gateway.set_meta(TASK_ID, phase="Solved")
    with pytest.raises(proj.RunCancelledError):
        await gateway.boost_salience(TASK_ID, "e-2", "operator")
    await journal.verify_durable_projections()
