"""The Classic native pair.

``ClassicRuntime`` is the runtime of the native Classic pair. Work
package 1 registers it as a test-only pair that delegates every call to
the legacy engine through the same host call as the legacy adapter, so
the pair runs today with the legacy behavior. Each later work package
replaces one delegated step with a native step and flips the capability
values the step earns.
"""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, cast

from core.variants.classic.adapter import ClassicHost, ClassicVariantRuntime
from core.variants.traditional import StepResult, TraditionalVariant

if TYPE_CHECKING:
    from core.variants import (
        VariantExecutionRequest,
        VariantHost,
        VariantOutcome,
    )

NATIVE_CONTRACT_VERSION = "2"


class ClassicRuntime:
    """Run the Classic native pair through the legacy engine for now."""

    descriptor = dataclasses.replace(
        ClassicVariantRuntime.descriptor,
        label="Classic blackboard (native)",
        contract_version=NATIVE_CONTRACT_VERSION,
        # The bare identifier and the legacy alias stay bound to the
        # legacy pair. A submission reaches this pair only when it names
        # the exact contract version.
        aliases=(),
        supports_recovery=True,
        # The pair stays out of the public capability document until
        # work package 16 qualifies it.
        listed=False,
    )

    @classmethod
    async def capture_configuration(
        cls, overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Capture the legacy envelope and stamp the native contract version."""
        configuration = await ClassicVariantRuntime.capture_configuration(overrides)
        configuration["variant_contract_version"] = cls.descriptor.contract_version
        return configuration

    @classmethod
    def configuration_from_metadata(
        cls, metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Load a saved envelope through the legacy reader."""
        return ClassicVariantRuntime.configuration_from_metadata(metadata)

    @classmethod
    async def run(
        cls, host: VariantHost, request: VariantExecutionRequest,
    ) -> VariantOutcome:
        """Delegate the coordination loop to the legacy engine."""
        classic_host = cast("ClassicHost", host)
        return await classic_host.run_classic_runtime(
            request,
            engine_class=TraditionalVariant,
            step_result_class=StepResult,
        )
