"""The indivisible cleaner contract, atomic transaction, and real agent fixture."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from test_classic_journal_projection import TASK_ID, make_gateway
from test_classic_journal_projection import native_run as _native_run
from test_classic_native_activations import execute_proposal

import activation_service
import budget_service
import database as db
import runtime_journal as journal
from core.capabilities import capabilities_for_role
from core.entry import BoardEntry
from core.variants.classic.cleaner import (
    CondensationError,
    CondensationPlan,
    require_cleaner_dispatch,
)
from core.variants.classic.projection import board_projection_digest, replay_run_board

native_run = _native_run
FIXTURE = Path(__file__).resolve().parents[2] / "conformance/proposal_fixtures/cleaner.json"
CAPS = capabilities_for_role("cleaner")
CLEANER_POINTS = [f"cleaner.{side}_{boundary}" for boundary in (
    "artifact_stage", "artifact_promotion", "artifact_reference", "activation_write",
    "summary_write", "removed_status_write", "tombstone_write", "lease_write",
) for side in ("before", "after")]
JOURNAL_POINTS = [f"journal.{side}_{boundary}" for boundary in (
    "journal_insert", "projection_write", "outbox_write", "resource_write", "commit",
) for side in ("before", "after")]


def proposal():
    return json.loads(FIXTURE.read_text())["proposal"]


def snapshot():
    return {key: BoardEntry(id=key, task_id=TASK_ID, type="finding", author="expert", body="Source finding",
                           sources=["https://example.org/evidence"])
            for key in ("e-1", "e-2")}


def validate(value, board=None, **changes):
    options = dict(capabilities=CAPS, max_body=8000, max_title=200, max_entries=12,
                   max_tokens=8000, recent_rounds=2, claim_ids=set(), evidence_ids=set())
    options.update(changes)
    CondensationPlan.from_proposal(value).validate(snapshot() if board is None else board, **options)


def test_complete_plan_preserves_exact_removals_and_summary():
    value = proposal()
    plan = CondensationPlan.from_proposal(value)
    validate(value)
    assert plan.summary == value["entries"][0]
    assert list(plan.removals) == value["removals"]
    value["entries"][0]["body"] = "Changed input"
    assert plan.summary["body"] != value["entries"][0]["body"]


@pytest.mark.parametrize("defect", ["missing_removals", "empty_removals", "duplicate", "unknown", "reason",
    "two_summaries", "wrong_type", "empty_body", "long_body", "long_title", "missing_claim",
    "unknown_claim", "missing_evidence", "unknown_evidence", "retained_dependency", "entry_limit", "token_limit"])
def test_invalid_plan_rejects_every_invariant(defect):
    value, board, limits = proposal(), snapshot(), {}
    if defect == "missing_removals":
        del value["removals"]
    elif defect == "empty_removals":
        value["removals"] = []
    elif defect == "duplicate":
        value["removals"].append(value["removals"][0])
    elif defect == "unknown":
        value["removals"][0]["entry_id"] = "missing"
    elif defect == "reason":
        value["removals"][0]["reason"] = ""
    elif defect == "two_summaries":
        value["entries"].append(value["entries"][0])
    elif defect == "wrong_type":
        value["entries"][0]["type"] = "finding"
    elif defect in ("empty_body", "long_body", "long_title"):
        field = "title" if defect == "long_title" else "body"
        value["entries"][0][field] = "" if defect == "empty_body" else "x" * 9000
    elif defect == "missing_claim":
        board["e-1"].refs = ["claim-known"]
        limits["claim_ids"] = {"claim-known"}
    elif defect == "unknown_claim":
        value["entries"][0]["refs"].append("claim-unknown")
    elif defect == "missing_evidence":
        value["entries"][0]["sources"] = []
    elif defect == "unknown_evidence":
        value["entries"][0]["sources"].append("evidence-unknown")
    elif defect == "retained_dependency":
        board["retained"] = BoardEntry(id="retained", task_id=TASK_ID, type="finding", author="expert",
                                       body="Dependent finding", refs=["e-1"])
    elif defect == "entry_limit":
        limits["max_entries"] = 0
    elif defect == "token_limit":
        limits["max_tokens"] = 1
    with pytest.raises(CondensationError):
        validate(value, board, **limits)


@pytest.mark.parametrize("status", ["removed", "superseded", "archived", "protected"])
def test_non_open_targets_are_protected(status):
    board = snapshot()
    board["e-1"].status = status
    with pytest.raises(CondensationError, match="Protected"):
        validate(proposal(), board)


@pytest.mark.parametrize("entry_type", ["objective", "directive", "ledger", "conflict", "solution", "plan", "critique"])
def test_protected_types_and_recent_work_cannot_disappear(entry_type):
    board = snapshot()
    board["e-1"].type = entry_type
    with pytest.raises(CondensationError, match="Protected"):
        validate(proposal(), board)


def test_retained_claim_and_evidence_links_resolve():
    value, board = proposal(), snapshot()
    board["e-1"].refs = ["claim-known"]
    board["e-1"].sources.append("decision-known")
    value["entries"][0]["refs"].append("claim-known")
    value["entries"][0]["sources"].append("decision-known")
    validate(value, board, claim_ids={"claim-known"}, evidence_ids={"decision-known"})


async def prepared(run, monkeypatch, value=None):
    """Use the reference receipt executor with no provider-backed dispatch."""
    value = value or proposal()
    gateway, store, emitter = make_gateway(run["binding"])
    for entry in snapshot().values():
        await gateway.append(TASK_ID, "expert", ["finding_writer"],
            [{"type": "finding", "body": entry.body, "sources": entry.sources}], turn_id="seed")
    # The reference executor returns fixture bytes and never calls a provider.
    result = await execute_proposal(run, json.dumps(value).encode(), monkeypatch, role="cleaner")
    assert result["native_execution"]["proposal"] == value
    return gateway, store, emitter


async def apply(gateway, value=None):
    return await gateway.apply_condensation(task_id=TASK_ID, actor="cleaner", capabilities=CAPS,
        proposal=value or proposal(), turn_id="activation-proposal", attempt=1, round_no=5)


@pytest.mark.asyncio
async def test_summary_tombstones_and_references_share_one_decision(native_run, monkeypatch):
    gateway, store, emitter = await prepared(native_run, monkeypatch)
    before = len(await journal.read_journal(run_id=native_run["run_id"]))
    entries = await apply(gateway)
    assert len(entries) == 1 and entries[0].body == proposal()["entries"][0]["body"]
    records = (await journal.read_journal(run_id=native_run["run_id"]))[before:]
    decisions = [record for record in records if record.operation_type == "proposal_decision"]
    assert len(decisions) == 1
    record = decisions[0]
    assert record.payload["board"]["tombstones"] == [{**item, "status": "removed"} for item in proposal()["removals"]]
    assert record.payload["budget_reference"]
    assert record.payload["trace_event"]["event"] == "board.condensation"
    assert record.payload["checkpoint_digest"] == native_run["binding"].board_digest()
    async with db._connect() as connection:
        outbox = await connection.execute_fetchall("SELECT * FROM journal_outbox WHERE journal_cursor = ?", (record.journal_cursor,))
    assert outbox
    state = journal.empty_projection_state()
    for row in await journal.read_journal(run_id=native_run["run_id"]):
        state = journal.apply_record_to_state(state, row)
    restored = state["runtime_state"][native_run["run_id"]]["condensations"]["activation-proposal"]
    assert restored["budget_reference"] == record.payload["budget_reference"]
    assert restored["checkpoint_digest"] == record.payload["checkpoint_digest"]
    assert restored["removals"] == record.payload["board"]["tombstones"]
    assert len(await apply(gateway)) == 0
    assert len([row for row in await db.get_classic_board_projection(native_run["run_id"]) if row["status"] == "open"]) == 1
    await journal.verify_durable_projections()


@pytest.mark.asyncio
async def test_invalid_response_records_one_failure_without_board_change(native_run, monkeypatch):
    value = proposal()
    value["entries"][0]["sources"] = []
    gateway, store, emitter = await prepared(native_run, monkeypatch, value)
    before = await db.get_classic_board_projection(native_run["run_id"])
    assert await apply(gateway, value) == []
    assert await db.get_classic_board_projection(native_run["run_id"]) == before
    records = [row for row in await journal.read_journal(run_id=native_run["run_id"])
               if row.payload.get("mutation", {}).get("kind") == "condensation"]
    assert len(records) == 1 and records[0].payload["decision"] == "rejected"
    assert (await activation_service.get_activation("activation-proposal", 1))["state"] == "committed"
    assert len([event for event in emitter.events if event[1] == "entry_rejected"]) == 1
    await journal.verify_durable_projections()


@pytest.mark.asyncio
@pytest.mark.parametrize("point,occurrence", [(point, 1) for point in CLEANER_POINTS + JOURNAL_POINTS]
    + [(f"cleaner.{side}_{boundary}", 2) for boundary in ("removed_status_write", "tombstone_write")
       for side in ("before", "after")])
async def test_process_crash_at_each_durable_boundary_is_atomic(native_run, monkeypatch, point, occurrence):
    gateway, store, emitter = await prepared(native_run, monkeypatch)
    before = await db.get_classic_board_projection(native_run["run_id"])
    # A fresh interpreter exits without cleanup, so SQLite recovers the transaction.
    child = Path(__file__).with_name("classic_cleaner_crash.py")
    arguments = {"database": db.DB_PATH, "artifacts": str(native_run["root"] / "classic-board"),
                 "run_id": native_run["run_id"], "lease_ref": "lock-native-board",
                 "point": point, "occurrence": occurrence, "proposal": proposal()}
    completed = await asyncio.to_thread(subprocess.run, [sys.executable, str(child)],
        input=json.dumps(arguments), text=True, capture_output=True, timeout=30, check=False)
    assert completed.returncode == 73, completed.stderr
    after = await db.get_classic_board_projection(native_run["run_id"])
    if point == "journal.after_commit":
        assert len(after) == 3
        assert [row["status"] for row in after].count("removed") == 2
    else:
        assert after == before
    await journal.verify_durable_projections()
    await native_run["binding"].load_board()
    await apply(gateway)
    await journal.verify_durable_projections()
    board, _, _ = await replay_run_board(native_run["run_id"])
    assert board_projection_digest(board) == native_run["binding"].board_digest()


@pytest.mark.parametrize("payload", [{"role": "cleaner"}, {"context": {"classic_proposal_role": "cleaner"}}])
def test_provider_cleaner_stays_behind_strict_reservation(payload):
    with pytest.raises(budget_service.BudgetError, match="strict reservation"):
        require_cleaner_dispatch(payload)


@pytest.mark.asyncio
async def test_real_agent_cleaner_fixture_crosses_the_daemon_parser_and_gateway(native_run, monkeypatch):
    from test_behavioral_conformance_stack import running_stack

    with running_stack("classic-cleaner-slice") as stack:
        credentials = json.loads(Path(stack["state"]["credentials_path"]).read_text())
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{stack['state']['urls']['agent']}/execute",
                headers={"Authorization": f"Bearer {credentials['execute_key']}"},
                json={"task_id": TASK_ID, "description": "Condense the two source findings.",
                      "role": "cleaner", "model": "test-light", "activation_id": "fixture-cleaner",
                      "context": {"classic_proposal_role": "cleaner"}})
        assert response.status_code == 200, response.text
        output = response.json()
        assert output["removals"] == proposal()["removals"]
        assert output["entries"] == proposal()["entries"]
        value = json.loads(output["result"])
        # Only the test reference executor supplies these already-observed bytes.
        # Production provider dispatch stays behind the strict reservation gate.
        gateway, _, _ = await prepared(native_run, monkeypatch, value)
        assert len(await apply(gateway, value)) == 1
        await journal.verify_durable_projections()
        (stack["results"] / "cleaner-result.json").write_text(json.dumps({
            "removals": value["removals"], "summary": value["entries"][0],
            "journal_replay": "equal", "production_provider_dispatch": "blocked"}, indent=2))


@pytest.mark.asyncio
async def test_legacy_pair_never_applies_native_condensation():
    from classic_harness import ClassicLifecycleHarness

    harness = ClassicLifecycleHarness("sequential")
    value = proposal()
    for raw in (value, json.dumps(value), "```json\n" + json.dumps(value) + "\n```", {"result": json.dumps(value)}, {"action": "condense", "entries": value["entries"], "removals": value["removals"]}):
        assert harness.variant.parse_agent_response({"task_id": TASK_ID}, "cleaner", raw) == []
    assert not hasattr(harness.variant.gateway, "apply_condensation")


@pytest.mark.asyncio
async def test_concurrent_board_change_invalidates_the_complete_plan(native_run, monkeypatch):
    gateway, store, emitter = await prepared(native_run, monkeypatch)
    binding = native_run["binding"]
    commit = binding.commit
    competing = type(gateway)(store, emitter, committer=binding)

    async def change_then_commit(mutation):
        monkeypatch.setattr(binding, "commit", commit)
        await competing.append(TASK_ID, "expert", ["finding_writer"],
            [{"type": "finding", "body": "Concurrent dependency", "refs": ["e-1"]}], turn_id="concurrent")
        return await commit(mutation)

    monkeypatch.setattr(binding, "commit", change_then_commit)
    with pytest.raises(journal.JournalError, match="projection version"):
        await apply(gateway)
    rows = await db.get_classic_board_projection(native_run["run_id"])
    assert all(row["status"] == "open" and row["entry_type"] != "condensed_finding" for row in rows)
    await journal.verify_durable_projections()


@pytest.mark.asyncio
async def test_immutable_disabled_cleaner_rejects_the_complete_response(native_run, monkeypatch):
    gateway, _, _ = await prepared(native_run, monkeypatch)
    from core.variants.classic import compiler

    load = compiler.load_specification
    def disabled(*args):
        spec = load(*args)
        return spec.model_copy(update={"cleaner": spec.cleaner.model_copy(update={"enabled": False})})
    monkeypatch.setattr(compiler, "load_specification", disabled)
    before = await db.get_classic_board_projection(native_run["run_id"])
    assert await apply(gateway) == []
    assert await db.get_classic_board_projection(native_run["run_id"]) == before


def test_condensed_finding_exists_in_all_type_maps():
    from core.protocol import ENTRY_TYPES
    from core.response_parser import _VALID_TYPES

    assert "condensed_finding" in ENTRY_TYPES == _VALID_TYPES
    ui = Path(__file__).resolve().parents[2] / "mission-control/src/components/features/board/boardModel.ts"
    assert '"condensed_finding"' in ui.read_text()


@pytest.mark.asyncio
async def test_provider_guard_rejects_before_any_reservation_write(native_run):
    from core.variants.classic.activations import reserve_call

    before = await journal.read_journal(run_id=native_run["run_id"])
    with pytest.raises(budget_service.UnknownPriceError, match="immutable price"):
        await reserve_call(native_run["run_id"], "cleaner-blocked", 1, {"role": "cleaner"})
    assert await journal.read_journal(run_id=native_run["run_id"]) == before
    async with db._connect() as connection:
        rows = await connection.execute_fetchall("SELECT * FROM budget_reservations WHERE activation_id = ?", ("cleaner-blocked",))
    assert rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("native_plan", [None, {"required": False, "url": "http://agent"},
                                          {"required": True, "url": "http://another-agent"}])
async def test_cleaner_never_falls_back_to_legacy_transport(native_plan):
    from core.orchestrator import Orchestrator

    orchestrator = object.__new__(Orchestrator)
    response = await orchestrator._post_activation("http://agent", {"role": "cleaner", "timeout": 10}, None, native_plan)
    assert response.json()["status"] == "failed"
    assert "native pair" in response.json()["result"]


def test_recondensation_retains_historical_provenance():
    value, board = proposal(), snapshot()
    board["e-1"].type = "condensed_finding"
    board["e-1"].refs = ["historical"]
    board["historical"] = BoardEntry(id="historical", task_id=TASK_ID, type="finding",
        author="expert", body="Original source", status="removed")
    value["entries"][0]["refs"].append("historical")
    validate(value, board, tombstone_ids={"historical"})
    with pytest.raises(CondensationError, match="Inactive"):
        validate(value, board)


def test_cleaner_cannot_remove_entries_from_another_space():
    board = snapshot()
    board["e-1"].space = "private:other"
    with pytest.raises(CondensationError, match="Protected"):
        validate(proposal(), board)


@pytest.mark.parametrize("wrapper", [
    {"action": "condense", "result": '{"action":"contribute","entries":[]}'},
    {"action": "clean", "result": '```json\n{"action":"condense"}\n```'},
    {"result": '{"result":"{\\"action\\":\\"condense\\"}"}'},
    {"action": "contribute", "result": '{"action":"clean"}'},
])
def test_legacy_cleaner_rejects_ambiguous_response_wrappers(wrapper):
    from classic_harness import ClassicLifecycleHarness

    harness = ClassicLifecycleHarness("sequential")
    wrapper = {**wrapper, "removals": proposal()["removals"]}
    assert harness.variant.parse_agent_response({"task_id": TASK_ID}, "cleaner", wrapper) == []
