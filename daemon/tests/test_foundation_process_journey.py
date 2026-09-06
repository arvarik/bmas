"""The Foundation journey across real processes with a genuine restart.

The test-stack controller starts Redis, the fake provider, the daemon,
and the agent as separate processes. The daemon admits one run and
dispatches one signed activation grant to the agent over HTTP. The
agent verifies the grant, acknowledges under its own key, requests one
effect grant for its model call, calls the fake provider, and posts
signed receipts. The controller then stops and respawns the daemon and
the agent over the same database, signing key, and activation cache.
After the restart the agent still serves the same acknowledgement, the
daemon still treats the same bytes as a duplicate, the run keeps its
fence and projection digest, and a second activation dispatches under
the same fence.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = REPO_ROOT / "scripts" / "test-stack.py"
RESULTS = REPO_ROOT / "test-results" / "foundation-process-journey"
RUN_ID = "run-process-journey"
TASK_ID = "task-process-journey"

pytestmark = pytest.mark.skipif(
    shutil.which("redis-server") is None,
    reason="the real-process journey needs redis-server on PATH",
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
    credentials = json.loads(Path(state["credentials_path"]).read_text())
    handle = {"env_file": env_file, "state": state, "credentials": credentials}
    try:
        yield handle
    finally:
        try:
            _controller("stop", "--env-file", str(env_file))
        except AssertionError as error:
            print(error)


def _operator(stack: dict) -> httpx.Client:
    return httpx.Client(
        base_url=stack["state"]["urls"]["daemon"],
        headers={"Authorization": f"Bearer {stack['credentials']['api_key']}"},
        timeout=120.0,
    )


def _node(stack: dict) -> httpx.Client:
    return httpx.Client(
        base_url=stack["state"]["urls"]["daemon"],
        headers={"Authorization": f"Bearer {stack['credentials']['node_key']}", "X-Node-Id": "journey"},
        timeout=60.0,
    )


def _agent(stack: dict) -> httpx.Client:
    return httpx.Client(
        base_url=stack["state"]["urls"]["agent"],
        headers={"Authorization": f"Bearer {stack['credentials']['execute_key']}"},
        timeout=60.0,
    )


def _dispatch(operator: httpx.Client, stack: dict, activation_id: str) -> dict:
    response = operator.post("/agent-protocol/dispatch", json={
        "run_id": RUN_ID, "task_id": TASK_ID, "agent_url": stack["state"]["urls"]["agent"],
        "activation_id": activation_id, "reservation_id": f"reservation-{RUN_ID}",
        "request": {
            "description": "Add 20 and 22 and answer with the number only.",
            "role": "expert", "model": "fake-model", "timeout": 60,
        },
        "timeout_s": 90.0,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_the_foundation_journey_survives_a_real_restart(stack):
    operator, node, agent = _operator(stack), _node(stack), _agent(stack)
    # 1. The agent publishes a qualified capability document and registered its key.
    document = agent.get("/bmas/capabilities").json()
    assert "2" in document["document"]["supported_protocol_versions"]
    keys = node.get("/agent-protocol/agent-keys").json()["keys"]
    assert any(key["agent_id"] == document["document"]["agent_id"] for key in keys), keys
    # 2. The daemon admits one run with its exact pair, fence, budget, and reservation.
    admitted = operator.post("/agent-protocol/runs", json={"run_id": RUN_ID, "task_id": TASK_ID}).json()
    assert admitted["runtime_key"]["runtime_id"] == "classic"
    fence = admitted["task_fence"]
    # 3. The daemon dispatches one signed grant. The agent acknowledges,
    # executes one model call under an effect grant, and returns receipts.
    first = _dispatch(operator, stack, "activation-journey-1")
    assert first["activation_state"] == "dispatched"
    assert first["acknowledgement_status"] in ("accepted", "duplicate")
    assert first["result"]["status"] == "completed", first["result"]
    assert first["replayed"] is False
    view = operator.get("/agent-protocol/activations/activation-journey-1/1").json()
    assert [ack["decision"] for ack in view["acknowledgements"]] == ["accepted"]
    stages = [receipt["stage"] for receipt in view["receipts"]]
    assert stages == ["transport_starting", "response_observed"], stages
    assert view["receipts"][1]["usage"] and view["receipts"][1]["usage"].get("total_tokens", 0) > 0
    before = operator.get(f"/agent-protocol/runs/{RUN_ID}").json()
    assert before["task_fence"] == fence
    stored = agent.get(f"/bmas/acknowledgements/{first['grant_id']}").json()
    assert stored["decision"] == "accepted" and stored["completed"] is True
    # 4. Restart the daemon and the agent for real.
    report = _controller("restart", "--env-file", str(stack["env_file"]))
    assert report["restarted"] == ["daemon", "agent"], report
    stack["state"] = json.loads(stack["env_file"].read_text())
    operator, node, agent = _operator(stack), _node(stack), _agent(stack)
    # 5. The durable state survives: the same fence, the same projection
    # digest, the same acknowledgement, and the same key registration.
    after = operator.get(f"/agent-protocol/runs/{RUN_ID}").json()
    assert after["task_fence"] == fence
    assert after["projection_digest"] == before["projection_digest"]
    assert after["journal_cursor"] == before["journal_cursor"]
    again = agent.get(f"/bmas/acknowledgements/{first['grant_id']}").json()
    assert again["acknowledgement"] == stored["acknowledgement"]
    duplicate = node.post(
        "/agent-protocol/acknowledgements",
        content=json.dumps(again["acknowledgement"], separators=(",", ":"), sort_keys=True).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["status"] == "duplicate"
    keys_after = node.get("/agent-protocol/agent-keys").json()["keys"]
    assert [key["key_id"] for key in keys_after] == [key["key_id"] for key in keys]
    # 6. A second activation dispatches under the same fence and protocol.
    second = _dispatch(operator, stack, "activation-journey-2")
    assert second["activation_state"] == "dispatched"
    assert second["result"]["status"] == "completed"
    resumed = operator.get(f"/agent-protocol/runs/{RUN_ID}").json()
    assert resumed["task_fence"] == fence
    assert {(row["activation_id"], row["state"]) for row in resumed["activations"]} == {
        ("activation-journey-1", "dispatched"), ("activation-journey-2", "dispatched"),
    }
    assert resumed["journal_cursor"] > after["journal_cursor"]
    # 7. The process logs prove two daemon starts and two agent starts.
    log_dir = Path(stack["state"]["log_dir"])
    daemon_log = (log_dir / "daemon.log").read_text(errors="replace")
    agent_log = (log_dir / "agent.log").read_text(errors="replace")
    assert daemon_log.count("Application startup complete") >= 2
    assert agent_log.count("Application startup complete") >= 2
    (RESULTS / "journey-summary.json").write_text(json.dumps({
        "run_id": RUN_ID, "fence": fence, "first": first["grant_id"], "second": second["grant_id"],
        "projection_digest_before": before["projection_digest"],
        "projection_digest_after_restart": after["projection_digest"],
        "restart": report,
    }, indent=2, sort_keys=True))
    assert os.environ.get("BMAS_KEEP_JOURNEY_STACK", "") == ""
