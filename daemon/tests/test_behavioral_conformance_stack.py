"""The legacy conformance columns run the real runtime behind the fake provider.

The test stack starts Redis, the fake provider, the daemon, and the
agent as real processes with every Foundation writer gate on. The
suite submits real classic tasks over HTTP, aborts one through the
operator route, resumes one through a real daemon restart, and reads
the durable footprint from the daemon's own database. The host admits
each task into one Foundation run and dispatches signed grants on the
legacy runtime's behalf, and the runtime itself authors no native
authority record.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import capability_publication as cap
import conformance_behavior as behavior
import database as db
import release_gates
from core.variants import RuntimeKey

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = REPO_ROOT / "scripts" / "test-stack.py"
RESULTS = REPO_ROOT / "test-results" / "behavioral-conformance-stack"
CLASSIC = RuntimeKey("classic", "1")

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


@pytest.fixture(scope="module")
def stack():
    RESULTS.mkdir(parents=True, exist_ok=True)
    env_file = RESULTS / "test-env.json"
    if env_file.exists():
        env_file.unlink()
    _controller("start", "--env-file", str(env_file), "--without-mission-control", "--keep-on-failure")
    state = json.loads(env_file.read_text())
    handle = {"env_file": env_file, "state": state}
    try:
        yield handle
    finally:
        try:
            _controller("stop", "--env-file", str(env_file))
        except AssertionError as error:
            print(error)


@pytest.fixture
def stack_db(stack, monkeypatch):
    """Read and write the daemon's own database file."""
    monkeypatch.setattr(db, "DB_PATH", stack["state"]["database_path"])
    return stack


def _executor(stack: dict, key: RuntimeKey) -> behavior.StackExecutor:
    def restart() -> None:
        _controller("restart", "--env-file", str(stack["env_file"]))

    return behavior.StackExecutor(
        runtime_key=key,
        daemon_url=stack["state"]["urls"]["daemon"],
        operator_key=stack["state"]["api_key"],
        restart=restart,
    )


@pytest.mark.asyncio
async def test_the_classic_column_passes_with_the_real_runtime(stack_db):
    record = cap.CapabilityDirectory().get(CLASSIC)
    env = await behavior.prepare_environment(
        record, _executor(stack_db, CLASSIC), Path(stack_db["state"]["root"]) / "conformance",
        run_id="run-conformance-classic-stack",
    )
    report = await behavior.run_behavioral_suite(env)
    (RESULTS / "classic-report.json").write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    assert report.passed, [dataclasses.asdict(result) for result in report.failures()]
    observed = {result.case_id: result for result in report.case_results}
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
    ledger = release_gates.GateLedger()
    ledger.record_conformance(report)
    assert ledger.gate_passed("conformance", CLASSIC)
