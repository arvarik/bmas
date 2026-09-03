"""The legacy package as a facade client: no canonical write, one authority.

The client sends every command to the versioned evaluation API through
one injectable transport, the compatibility commands warn and record
fallback use, the scorer shim delegates to the daemon preview when a
client exists, the migration of legacy summary files writes only
beside the original file, and replay never repeats an execution.
"""

from __future__ import annotations

import json
import warnings

import pytest

from eval import scorer
from eval.client import API_PREFIX, EvaluationClient
from eval.legacy_results import find_summaries, migrate_directory


class FakeTransport:
    def __init__(self, responses: dict[str, dict] | None = None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses = responses or {}

    def __call__(self, method: str, path: str, body: dict | None) -> dict:
        self.calls.append((method, path, body))
        return self.responses.get(path.split("?")[0], {"ok": True})


def test_every_command_reaches_the_evaluation_api():
    transport = FakeTransport()
    client = EvaluationClient(transport=transport)
    client.authority()
    client.removal_gates()
    client.record_fallback("eval.cli.benchmark")
    client.preview_score("deterministic", {"final_output": "7",
                                           "reference_answer": "7"})
    client.migrate_legacy_result({"run_id": "r", "dataset_size": 1})
    client.export_bundle("run-a", policy="complete")
    client.replay_bundle("AAAA", actor="operator-a")
    client.measure_fallback_gate(window_start="1970-01-01T00:00:00Z",
                                 actor="operator-a")
    paths = [call[1] for call in transport.calls]
    assert all(path.startswith(API_PREFIX) for path in paths)
    assert f"{API_PREFIX}/legacy-fallbacks" in paths
    assert f"{API_PREFIX}/scorers/preview" in paths
    assert f"{API_PREFIX}/legacy-results" in paths
    assert f"{API_PREFIX}/runs/run-a/replay-bundles?policy=complete" in paths
    replay = next(call for call in transport.calls
                  if call[1].endswith("/replay-bundles/import"))
    assert replay[2]["approval"] == {"actor": "operator-a",
                                     "policy_version": "1"}


def test_replay_without_actor_stays_inert():
    transport = FakeTransport()
    EvaluationClient(transport=transport).replay_bundle("AAAA")
    body = transport.calls[0][2]
    assert "approval" not in body


def test_scorer_shim_delegates_to_the_daemon_when_configured(monkeypatch):
    transport = FakeTransport({
        f"{API_PREFIX}/scorers/preview": {
            "terminal_class": "completed",
            "result": {"status": "scored", "passed": True,
                       "explanation": "numeric_match",
                       "dimensions": [{"name": "accuracy", "value": 1.0,
                                       "category": "42"}]},
        },
    })
    monkeypatch.setattr(scorer, "SCORER_CLIENT",
                        EvaluationClient(transport=transport))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        extracted, correct, method = scorer.score_gsm8k("42", "#### 42")
    assert (extracted, correct, method) == ("42", True, "numeric_match")
    configuration = transport.calls[0][2]["configuration"]
    assert configuration == {"comparison": "last_number"}


def test_scorer_shim_warns_and_records_local_fallback(monkeypatch):
    recorded: list[str] = []
    monkeypatch.setattr(scorer, "SCORER_CLIENT", None)
    monkeypatch.setattr(scorer, "FALLBACK_RECORDER", recorded.append)
    with pytest.warns(DeprecationWarning, match="deprecation cycle"):
        extracted, correct, _ = scorer.score_gsm8k("7", "The answer is 7")
    assert (extracted, correct) == ("7", True)
    assert recorded == ["eval.scorer.score_gsm8k"]


def test_legacy_summaries_migrate_beside_the_original(tmp_path):
    summary = {"run_id": "bench-1", "dataset": "gsm8k", "dataset_size": 2,
               "accuracy": 0.5, "joules_estimate": None}
    (tmp_path / "bench-1_summary.json").write_text(json.dumps(summary))
    (tmp_path / "notes.txt").write_text("ignored")
    transport = FakeTransport({
        f"{API_PREFIX}/legacy-results": {
            "legacy_run_id": "bench-1",
            "unavailable_fields": ["joules_estimate"],
            "record_digest": "a" * 64,
        },
    })
    client = EvaluationClient(transport=transport)
    assert [path.name for path in find_summaries(tmp_path)] == [
        "bench-1_summary.json",
    ]
    results = migrate_directory(client, tmp_path)
    assert results[0]["unavailable_fields"] == ["joules_estimate"]
    migrated = json.loads((tmp_path / "bench-1_summary.migrated.json").read_text())
    assert migrated["record_digest"] == "a" * 64
    assert transport.calls[0][2] == {"summary": summary}
    assert find_summaries(tmp_path / "missing") == []


def test_compatibility_commands_warn_and_record_fallback(monkeypatch):
    import argparse

    from eval import cli

    recorded: list[str] = []

    class Recorder:
        def record_fallback(self, entry_point: str) -> dict:
            recorded.append(entry_point)
            return {"recorded": True}

    monkeypatch.setattr(cli, "_client_for", lambda args: Recorder())
    with pytest.warns(DeprecationWarning, match="compatibility command"):
        cli._deprecated_command("eval.cli.benchmark", argparse.Namespace())
    assert recorded == ["eval.cli.benchmark"]


def test_http_transport_raises_a_client_error_for_bad_responses():
    from eval.client import EvaluationClientError, _http_transport

    send = _http_transport("http://127.0.0.1:9", "key")
    with pytest.raises(EvaluationClientError, match="failed"):
        send("GET", "/api/evaluation/authority", None)
