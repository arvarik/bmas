"""The evaluation API client for the legacy tooling.

The legacy package reaches every canonical evaluation record through
this client and the versioned daemon API only. It imports nothing from
the daemon, writes no database, and merges no partial records: every
response is one complete record from the one facade. The transport is
injectable so tests exercise every command without a network.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

Transport = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]

API_PREFIX = "/api/evaluation"


class EvaluationClientError(RuntimeError):
    """The daemon rejected one client request."""


def _http_transport(daemon_url: str, api_key: str) -> Transport:
    def send(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            daemon_url.rstrip("/") + path, data=payload, method=method,
        )
        request.add_header("Content-Type", "application/json")
        if api_key:
            request.add_header("X-API-Key", api_key)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise EvaluationClientError(
                f"{method} {path} failed with {error.code}: {detail[:500]}"
            ) from error
        except urllib.error.URLError as error:
            raise EvaluationClientError(
                f"{method} {path} failed: {error.reason}"
            ) from error
        return json.loads(text) if text else {}

    return send


class EvaluationClient:
    """One facade client for every supported legacy command."""

    def __init__(
        self,
        daemon_url: str = "http://127.0.0.1:8000",
        *,
        api_key: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.getenv("BMAS_API_KEY", "")
        self._send = transport or _http_transport(daemon_url, key)

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._send(method, API_PREFIX + path, body)

    # ── Authority and migration evidence ─────────────────────────────

    def authority(self) -> dict[str, Any]:
        return self._call("GET", "/authority")

    def removal_gates(self) -> dict[str, Any]:
        return self._call("GET", "/removal-gates")

    def measure_fallback_gate(self, *, window_start: str, actor: str, threshold: int = 0) -> dict[str, Any]:
        return self._call("POST", "/removal-gates/fallback", {
            "window_start": window_start, "actor": actor, "threshold": threshold,
        })

    def record_fallback(self, entry_point: str) -> dict[str, Any]:
        """Record one legacy command use as a measured fallback."""
        return self._call("POST", "/legacy-fallbacks", {"entry_point": entry_point})

    # ── Sources, drafts, and publication ─────────────────────────────

    def import_source(self, adapter_id: str, request: dict[str, Any], **options: Any) -> dict[str, Any]:
        return self._call("POST", f"/adapters/{adapter_id}/import", {
            "request": request, **options,
        })

    def create_draft(self, record: dict[str, Any], *, source_id: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/drafts", {"record": record, "links": {"source_id": source_id}})

    def edit_case(self, draft_id: str, case: dict[str, Any]) -> dict[str, Any]:
        return self._call("PUT", f"/drafts/{draft_id}/editor/cases", {"case": case})

    def publish(self, draft_id: str, *, dataset_id: str, version_id: str, name: str) -> dict[str, Any]:
        return self._call("POST", f"/drafts/{draft_id}/publish-governed", {
            "dataset_id": dataset_id, "version_id": version_id, "name": name,
        })

    # ── Scoring and evidence ─────────────────────────────────────────

    def preview_score(self, plugin_type: str, evidence: dict[str, Any], configuration: dict[str, Any] | None = None) -> dict[str, Any]:
        """Score through the daemon boundary without persisting."""
        return self._call("POST", "/scorers/preview", {
            "plugin_type": plugin_type, "evidence": evidence,
            "configuration": configuration or {},
        })

    def score_attempt(self, attempt_id: str, **payload: Any) -> dict[str, Any]:
        return self._call("POST", f"/attempts/{attempt_id}/scores", payload)

    def capture_evidence(self, attempt_id: str, **payload: Any) -> dict[str, Any]:
        return self._call("POST", f"/attempts/{attempt_id}/evidence", payload)

    def migrate_legacy_result(self, summary: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/legacy-results", {"summary": summary})

    # ── Analysis, export, and replay ─────────────────────────────────

    def freeze_analysis(self, run_id: str, **payload: Any) -> dict[str, Any]:
        return self._call("POST", f"/runs/{run_id}/analyses/freeze", payload)

    def overview(self, run_id: str, snapshot_id: str) -> dict[str, Any]:
        return self._call("GET", f"/runs/{run_id}/analyses/{snapshot_id}/overview")

    def export_bundle(self, run_id: str, *, policy: str = "redacted") -> dict[str, Any]:
        return self._call("POST", f"/runs/{run_id}/replay-bundles?policy={policy}")

    def replay_bundle(self, archive_base64: str, *, actor: str | None = None, policy_version: str = "1") -> dict[str, Any]:
        """Import one bundle; replay only with an authenticated approval.

        The replay recomputes the analysis from stored evidence. It
        never repeats the model execution.
        """
        body: dict[str, Any] = {"archive_base64": archive_base64}
        if actor:
            body["approval"] = {"actor": actor, "policy_version": policy_version}
        return self._call("POST", "/replay-bundles/import", body)
