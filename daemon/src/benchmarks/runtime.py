"""Prepare benchmark arms through the registered runtime contract."""

from __future__ import annotations

from typing import Any

from benchmarks.data_classes import policy_digest as redaction_policy_digest
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
    if overrides:
        # The dispatch validates the same overrides at admission, so an
        # arm with an unsupported override fails here, at authoring,
        # instead of at every admission attempt.
        from pydantic import ValidationError

        from routes.submit import TaskOverrides

        try:
            TaskOverrides.model_validate(overrides)
        except ValidationError as error:
            details = error.errors()
            location = (
                ".".join(str(part) for part in details[0]["loc"])
                if details else "overrides"
            )
            message = str(details[0]["msg"]) if details else "invalid"
            raise BenchmarkRuntimeConfigurationError(
                "The submission overrides are not accepted at dispatch: "
                f"{location or 'overrides'}: {message}"
            ) from error

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
        "redaction_policy_digest": redaction_policy_digest(),
    })
    return {
        "runtime_id": canonical_id,
        "configuration": envelope,
        "configuration_checksum": content_checksum(envelope),
        "descriptor": runtime.descriptor.to_dict(),
    }
