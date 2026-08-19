"""Prepare benchmark arms through the registered runtime contract."""

from __future__ import annotations

from typing import Any

from benchmarks.provenance import content_checksum, redact_secrets
from core.variants import canonical_variant_id, require_variant_class


class BenchmarkRuntimeConfigurationError(ValueError):
    """A benchmark arm contains invalid runtime configuration."""


async def prepare_benchmark_arm(
    runtime_id: str,
    requested_configuration: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve one arm into a stable runtime-neutral execution envelope."""
    canonical_id = canonical_variant_id(runtime_id)
    runtime = require_variant_class(canonical_id)
    requested = requested_configuration or {}
    overrides = requested.get("submission_overrides")
    if overrides is not None and not isinstance(overrides, dict):
        raise BenchmarkRuntimeConfigurationError(
            "submission_overrides must contain an object"
        )

    prepare = getattr(runtime, "prepare_benchmark_configuration", None)
    if callable(prepare):
        effective = await prepare(requested)
    else:
        unknown = set(requested) - {"submission_overrides"}
        if unknown:
            raise BenchmarkRuntimeConfigurationError(
                f"The {canonical_id} runtime does not support: {', '.join(sorted(unknown))}"
            )
        effective = await runtime.capture_configuration(overrides)

    envelope = redact_secrets({
        "schema_version": "1",
        "runtime_id": canonical_id,
        "runtime_contract_version": runtime.descriptor.contract_version,
        "configuration_schema_version": runtime.descriptor.configuration_schema_version,
        "submission_overrides": overrides or {},
        "effective_configuration": effective,
    })
    return {
        "runtime_id": canonical_id,
        "configuration": envelope,
        "configuration_checksum": content_checksum(envelope),
        "descriptor": runtime.descriptor.to_dict(),
    }
