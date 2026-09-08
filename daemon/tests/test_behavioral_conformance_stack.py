"""The Classic conformance columns run the real runtime behind the fake provider.

The test stack starts Redis, the fake provider, the daemon, and the
agent as real processes with every Foundation writer gate on and the
test-only runtime admission on. Each column submits real classic tasks
over HTTP with its exact runtime pair, aborts one through the operator
route, resumes one through a real daemon restart, and reads the durable
footprint from the daemon's own database.

The legacy column proves the frozen legacy pair. The native column
proves the honest starting values of the native pair: the pair runs
through the legacy engine today, the host admits each task into one
Foundation run and dispatches signed grants on the runtime's behalf,
and the runtime itself authors no native authority record. Each later
work package flips the values it earns and this column proves the flip.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import capability_publication as cap
import conformance_behavior as behavior
import database as db
import release_gates
from core.variants import RuntimeKey

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = REPO_ROOT / "scripts" / "test-stack.py"
CLASSIC_LEGACY = RuntimeKey("classic", "1")
CLASSIC_NATIVE = RuntimeKey("classic", "2")
LEGACY_COLUMN = "behavioral-conformance-stack"
NATIVE_COLUMN = "classic-native-column"

pytestmark = pytest.mark.skipif(
    shutil.which("redis-server") is None,
    reason="the stack-backed conformance suite needs redis-server on PATH",
)


def _controller(*arguments: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(CONTROLLER), *arguments],
        capture_output=True, text=True, timeout=600, check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"test-stack {arguments[0]} failed: {completed.stdout[-2000:]}\n{completed.stderr[-2000:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1]) if completed.stdout.strip() else {}


@contextlib.contextmanager
def running_stack(column: str) -> Iterator[dict]:
    """Start one stack for one column and stop it afterwards."""
    results = REPO_ROOT / "test-results" / column
    results.mkdir(parents=True, exist_ok=True)
    env_file = results / "test-env.json"
    if env_file.exists():
        env_file.unlink()
    _controller("start", "--env-file", str(env_file), "--without-mission-control", "--keep-on-failure")
    state = json.loads(env_file.read_text())
    try:
        yield {"env_file": env_file, "state": state, "results": results}
    finally:
        try:
            _controller("stop", "--env-file", str(env_file))
        except AssertionError as error:
            print(error)


def _executor(stack: dict, key: RuntimeKey) -> behavior.StackExecutor:
    def restart() -> None:
        _controller("restart", "--env-file", str(stack["env_file"]))

    return behavior.StackExecutor(
        runtime_key=key,
        daemon_url=stack["state"]["urls"]["daemon"],
        operator_key=stack["state"]["api_key"],
        restart=restart,
    )


async def _run_column(
    stack: dict, key: RuntimeKey, *, run_id: str, report_name: str,
) -> tuple[dict[str, behavior.CaseResult], behavior.StackExecutor]:
    """Run one column, write its report, and return the observed cases."""
    record = cap.CapabilityDirectory().get(key)
    executor = _executor(stack, key)
    env = await behavior.prepare_environment(
        record, executor, Path(stack["state"]["root"]) / "conformance", run_id=run_id,
    )
    report = await behavior.run_behavioral_suite(env)
    (stack["results"] / report_name).write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    assert report.passed, [dataclasses.asdict(result) for result in report.failures()]
    # Every observed value is the declared value of the record.
    observed = {result.case_id: result for result in report.case_results}
    for case_id, capability in behavior.CASE_CAPABILITIES.items():
        assert observed[case_id].observed_value == record.capabilities[capability], case_id
    ledger = release_gates.GateLedger()
    ledger.record_conformance(report)
    assert ledger.gate_passed("conformance", key)
    return observed, executor


@pytest.mark.asyncio
async def test_the_classic_column_passes_with_the_real_runtime(monkeypatch):
    with running_stack(LEGACY_COLUMN) as stack:
        monkeypatch.setattr(db, "DB_PATH", stack["state"]["database_path"])
        observed, executor = await _run_column(
            stack, CLASSIC_LEGACY,
            run_id="run-conformance-classic-stack", report_name="classic-report.json",
        )
        # Every task of the column ran under the legacy pair.
        for real_id in executor.task_ids.values():
            task = await db.get_task(real_id)
            assert task is not None and str(task["runtime_contract_version"]) == "1", real_id
    # The runtime recorded the seed and never applied it.
    assert observed["seed_state"].observed_value == "recorded_only"
    # The abort stopped a real running task, and a real restart resumed one.
    assert observed["cancellation_deadlines"].observed_value == "legacy"
    assert observed["lease_fencing_restart_replay"].observed_value == "compatibility_adapter"
    assert "resumed_answer" in observed["lease_fencing_restart_replay"].detail
    # The host dispatched signed grants; the runtime authored no native record.
    assert observed["activation_effect_ledgers"].observed_value == "compatibility_adapter"
    assert "host_dispatched_grants=" in observed["activation_effect_ledgers"].detail
    assert "runtime_authored_rows=0" in observed["activation_effect_ledgers"].detail


@pytest.mark.asyncio
async def test_the_classic_native_column_passes_with_the_real_runtime(monkeypatch):
    with running_stack(NATIVE_COLUMN) as stack:
        monkeypatch.setattr(db, "DB_PATH", stack["state"]["database_path"])
        observed, executor = await _run_column(
            stack, CLASSIC_NATIVE,
            run_id="run-conformance-classic-native-stack", report_name="classic-native-report.json",
        )
        # Every task of the column ran under the native pair with the
        # native contract version in its stored envelope.
        assert executor.task_ids
        for real_id in executor.task_ids.values():
            task = await db.get_task(real_id)
            assert task is not None and str(task["runtime_contract_version"]) == "2", real_id
            envelope = (await db.get_board_meta(real_id)).get("effective_configuration") or {}
            assert envelope.get("variant_contract_version") == "2", real_id
        # Work package 5A: every native run is journal-backed. Each task's
        # run holds runtime-authored board decisions with the common
        # event envelope, exactly one runtime-authored terminal outcome
        # under a registered Classic reason, a board projection that a
        # replay from cursor zero rebuilds, and the admitted policy set
        # bound into every record.
        await _assert_native_runs_are_journal_backed(executor.task_ids.values())
    assert cap.CapabilityDirectory().get(CLASSIC_NATIVE).availability == "test_only"
    # The values of the ladder after work package 5A.
    assert observed["admission_identity"].observed_value == "native"
    assert observed["assets_privacy"].observed_value == "native"
    assert observed["ui_fallback"].observed_value == "native"
    assert observed["goals"].observed_value == "native"
    assert observed["seed_state"].observed_value == "recorded_only"
    assert observed["cancellation_deadlines"].observed_value == "legacy"
    assert observed["evidence_decisions"].observed_value == "legacy"
    assert observed["budget_reservations"].observed_value == "advisory_legacy"
    # The fence validation is native: the resumed run and the complete
    # run both authored fenced records, and the restart resumed the
    # task from its verified snapshot under the same fence.
    assert observed["lease_fencing_restart_replay"].observed_value == "native"
    assert "resumed_answer" in observed["lease_fencing_restart_replay"].detail
    assert "resumed_runtime_rows=0" not in observed["lease_fencing_restart_replay"].detail
    assert observed["agent_protocol_negotiation"].observed_value == "compatibility_adapter"
    assert observed["trace_envelope"].observed_value == "compatibility_adapter"
    # The host still dispatched the signed grants for the native pair,
    # while the runtime authored its board and its outcome itself.
    assert observed["activation_effect_ledgers"].observed_value == "compatibility_adapter"
    assert "host_dispatched_grants=" in observed["activation_effect_ledgers"].detail
    assert "runtime_ledger_rows=0" in observed["activation_effect_ledgers"].detail
    assert "runtime_authored_rows=0" not in observed["activation_effect_ledgers"].detail


async def _assert_native_runs_are_journal_backed(task_ids) -> None:
    """Prove the 5A values on every task the native column ran."""
    import runtime_journal as journal
    from core.run_context import PolicySet
    from core.variants.classic import outcomes, projection

    registry = outcomes.classic_reason_registry()
    checked = 0
    for real_id in task_ids:
        run_id = f"run-{real_id}"
        chain = await journal.read_journal(run_id=run_id)
        assert chain, real_id
        journal.verify_chain(chain)
        admission = chain[0]
        assert admission.operation_type == "admission_identity"
        # The immutable policy set: the members and the digest of the
        # admission bind every runtime-authored record.
        policy_digest = PolicySet(**admission.payload["policy_set"]).digest()
        assert policy_digest == admission.payload["policy_set_digest"]
        runtime_records = [record for record in chain if record.authority_type == "runtime"]
        assert runtime_records, real_id
        for record in runtime_records:
            assert record.payload["policy_set_digest"] == policy_digest, real_id
            assert record.producer and record.data_classification and record.redaction_policy_version
            assert journal.record_transaction_digest(record) == record.transaction_digest
        decisions = [record for record in runtime_records if record.operation_type == "proposal_decision"]
        terminal = [record for record in chain if record.operation_type == "terminal_outcome"]
        # Exactly one terminal outcome, authored by the runtime under a
        # registered reason, and the run state follows its class.
        assert len(terminal) == 1, real_id
        assert terminal[0].authority_type == "runtime"
        reason = terminal[0].payload["reason_code"]
        binding = registry.binding_for(CLASSIC_NATIVE, reason)
        assert terminal[0].payload["common_class"] == binding.common_class.value
        run_row = await db.get_run(run_id)
        assert run_row is not None
        assert run_row["state"] == journal.TERMINAL_STATE_FOR_CLASS[binding.common_class.value], real_id
        task = await db.get_task(real_id)
        assert task is not None
        if str(task["status"]) == "completed":
            assert reason == "completed", real_id
            assert decisions, real_id
        # A replay from cursor zero rebuilds the board projection digest
        # of the last accepted decision and equals the live rows.
        accepted = [record for record in decisions if record.payload["decision"] == "accepted"]
        if accepted:
            board, _run_state, _cursor = await projection.replay_run_board(run_id)
            assert projection.board_projection_digest(board) == accepted[-1].payload["checkpoint_digest"], real_id
            rows = await db.get_classic_board_projection(run_id)
            assert {row["entry_id"] for row in rows} == set(board["entries"]), real_id
        # The recovery reader: the stored checkpoint is a verified snapshot
        # of the journal state under the live fence.
        checkpoint = (await db.get_board_meta(real_id)).get("variant_checkpoint")
        if isinstance(checkpoint, dict) and checkpoint.get("snapshot"):
            control = await db.get_run_control(run_id)
            assert control is not None
            verified = await projection.read_checkpoint(
                checkpoint, run_id=run_id, task_fence=str(control["task_fence"]),
            )
            assert verified.board_digest == projection.board_projection_digest(verified.board)
            checked += 1
    assert checked, "no native task stored a verified checkpoint"
    await journal.verify_durable_projections()

