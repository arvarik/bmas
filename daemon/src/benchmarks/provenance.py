"""Create safe, stable execution snapshots for benchmark reproduction."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)


def canonical_json(value: Any) -> str:
    """Return deterministic JSON for checksums and stored contracts."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def content_checksum(value: Any) -> str:
    """Return a SHA-256 checksum for one canonical JSON value."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def redact_secrets(value: Any) -> Any:
    """Remove secret values while preserving the configuration structure."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.lower().replace("-", "_")
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_secrets(item) for item in value]
    return value


def build_execution_snapshot(
    *,
    runtime_id: str,
    effective_configuration: Mapping[str, Any],
    submission_overrides: Mapping[str, Any] | None = None,
    benchmark_context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Build one variant-neutral and secret-free execution identity."""
    snapshot = redact_secrets(
        {
            "schema_version": "1",
            "runtime": {
                "id": runtime_id,
                "configuration": dict(effective_configuration),
            },
            "submission_overrides": dict(submission_overrides or {}),
            "benchmark": dict(benchmark_context or {}),
            "build": {
                "revision": os.getenv("BMAS_BUILD_REVISION", "unknown"),
                "image": os.getenv("BMAS_IMAGE_VERSION", "unknown"),
            },
        }
    )
    return snapshot, content_checksum(snapshot)
