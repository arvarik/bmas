"""Live-provider smoke against a running daemon and a real model gateway.

Every other automated test answers model calls with the deterministic
fake provider, which hides provider-specific behaviour such as reasoning
tokens inside the completion budget, empty structured replies, and the
material a judge needs to label an anchor item. This module runs only
through the optional ``daemon.live-provider-smoke`` manifest group. It
needs a daemon that already runs against a real provider (for example
the compose starter after ``./scripts/bmas up``) and reads its address
and operator key from the environment:

    BMAS_LIVE_PROVIDER=1 BMAS_LIVE_DAEMON_URL=http://127.0.0.1:9000 \\
    BMAS_API_KEY=... python3 -m pytest tests/test_live_provider.py -q

The checks spend real provider budget: one classic task and one
four-item judge calibration, about fifteen cents on Gemini Flash.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("BMAS_LIVE_PROVIDER") != "1",
    reason="The live-provider smoke runs only through the optional manifest group",
)

DAEMON = os.getenv("BMAS_LIVE_DAEMON_URL", "http://127.0.0.1:9000").rstrip("/")
API_KEY = os.getenv("BMAS_API_KEY", "")
DIGEST = "a" * 64


def _call(method: str, path: str, data=None, *, timeout: int = 300):
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
        headers["X-API-Key"] = API_KEY
    request = urllib.request.Request(
        f"{DAEMON}{path}", data=body, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        text = error.read().decode()
        try:
            return error.code, json.loads(text)
        except ValueError:
            return error.code, {"raw": text}


def _wait_task(task_id: str, timeout_s: int = 600) -> dict:
    deadline = time.monotonic() + timeout_s
    task: dict = {}
    while time.monotonic() < deadline:
        status, body = _call("GET", f"/tasks/{task_id}")
        assert status == 200, body
        task = body.get("task") or {}
        if task.get("status") in {"completed", "failed", "aborted"}:
            return task
        time.sleep(3)
    raise AssertionError(f"task {task_id} stayed {task.get('status')} for {timeout_s}s")


def test_the_live_daemon_is_ready():
    status, readiness = _call("GET", "/readiness")
    assert status == 200, readiness
    checks = readiness.get("checks") or []
    assert checks, readiness
    assert all(check.get("ready") for check in checks), checks


def test_a_classic_task_completes_through_the_real_model():
    status, submitted = _call("POST", "/submit", {
        "task": "Explain the classic blackboard workflow in three concise bullets.",
        "variant": "classic",
    })
    assert status in (200, 201, 202), submitted
    task = _wait_task(str(submitted["task_id"]))
    assert task["status"] == "completed", task
    # A real answer comes from the decider, not from the fallback finding.
    assert task.get("terminated_by") == "solution", task
    assert float(task.get("total_cost_usd") or 0) > 0
    assert int(task.get("total_tokens") or 0) > 0
    # The control unit records its own cost; a truncated reply would show
    # as a failed round in the daemon log, and the task would still
    # complete through the deterministic fallback. The cost route lists
    # every control-plane call so a reviewer can compare the tokens.
    status, cost = _call("GET", f"/tasks/{task['id']}/cost")
    assert status == 200, cost


def test_the_judge_labels_anchor_items_with_their_content():
    suffix = time.strftime("%H%M%S")
    status, scorers = _call("GET", "/benchmarks/scorers")
    assert status == 200, scorers
    scorer_id = str((scorers.get("scorers") or [{}])[0].get("id") or "scorer-exact-match")
    anchor_id = f"anchor-live-{suffix}"
    status, registered = _call("POST", "/api/evaluation/judges/anchor-sets", {
        "anchor_id": anchor_id,
        "judge_id": f"judge-live-{suffix}",
        "judge_version": "1",
        "judge_model": "starter-model",
        "prompt_digest": DIGEST,
        "scorer_id": scorer_id,
        "scorer_version": "1",
        "label_set": {
            "dataset_id": "live-anchors",
            "version": "1",
            "items": [
                {"item_id": "live-1", "label": "pass", "input": "What is 20 plus 22?",
                 "expected_output": "42", "candidate": "42"},
                {"item_id": "live-2", "label": "fail", "input": "What is 1 plus 2?",
                 "expected_output": "3", "candidate": "4"},
                {"item_id": "live-3", "label": "pass", "input": "What is 10 plus 5?",
                 "expected_output": "15", "candidate": "15"},
                {"item_id": "live-4", "label": "fail", "input": "What is 7 plus 8?",
                 "expected_output": "15", "candidate": "16"},
            ],
        },
        "candidate_models": [],
        "interval_days": 7,
        "threshold": 0.7,
        "drift_tolerance": 0.1,
        "registered_at": "2026-09-05T00:00:00Z",
    })
    assert status in (200, 201), registered
    stored = str(registered.get("anchor_id") or anchor_id)
    status, calibration = _call(
        "POST",
        f"/api/evaluation/judges/anchor-sets/{stored}/calibrate?calibrated_at=2026-09-05T12:00:00Z",
        {},
    )
    assert status == 200, calibration
    outputs = calibration.get("judge_outputs") or {}
    assert outputs and all(label != "abstain" for label in outputs.values()), outputs
    assert calibration.get("state") == "current", calibration
    assert float(calibration.get("raw_agreement") or 0) >= 0.75, calibration
