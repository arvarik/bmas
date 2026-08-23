"""Registered lifecycle adapter for the classic blackboard runtime."""
from __future__ import annotations

import copy
from typing import Any, Protocol, cast

from config import (
    AGENT_ENDPOINTS,
    EDGE_NODE_MODELS,
    MODEL_POOLS,
    MODEL_PRICING,
)
from core.protocol import LEGACY_EVENT_NAMES, V2_EVENT_NAMES
from core.variants import (
    CLASSIC_VARIANT,
    LEGACY_CLASSIC_VARIANT,
    VariantConfigurationError,
    VariantDescriptor,
    VariantExecutionRequest,
    VariantFeatures,
    VariantHost,
    VariantOutcome,
    register_variant,
)
from core.variants.effort import (
    CLASSIC_EFFORT_PROFILES,
    apply_effort_profile,
    public_effort_profiles,
    resolve_effort,
)
from core.variants.traditional import StepResult, TraditionalVariant
from settings_store import get_store, validate_classic_settings


class ClassicHost(VariantHost, Protocol):
    """Expose the classic engine runner supplied by the daemon host."""

    async def run_classic_runtime(
        self,
        request: VariantExecutionRequest,
        *,
        engine_class: type,
        step_result_class: type,
    ) -> VariantOutcome:
        """Run the classic engine and return its coordination outcome."""
        ...


class ClassicVariantRuntime:
    """Connect the classic engine to the shared task lifecycle."""

    descriptor = VariantDescriptor(
        id=CLASSIC_VARIANT,
        label="Classic blackboard",
        contract_version="1",
        aliases=(LEGACY_CLASSIC_VARIANT,),
        configuration_schema_version="1",
        supports_recovery=True,
        required_agent_features=(
            "execute",
            "activation_idempotency",
            "task_cancellation",
        ),
        features=VariantFeatures(
            events=tuple(sorted({
                *V2_EVENT_NAMES,
                *LEGACY_EVENT_NAMES,
                "initial_state",
                "error",
                "ag_fallback",
            })),
            panels=(
                "mission",
                "blackboard",
                "agents",
                "logs",
                "artifacts",
            ),
            graphs=("turns", "entry_references"),
            controls=(
                "pause",
                "resume",
                "abort",
                "directive",
                "boost",
                "retract",
            ),
            progress=("phase", "round", "effective_actions", "budget"),
            result=(
                "answer",
                "terminated_by",
                "answer_source",
                "verification_status",
                "rounds",
                "budget_spent",
            ),
        ),
        effort_profiles=public_effort_profiles(CLASSIC_EFFORT_PROFILES),
    )

    @classmethod
    async def capture_configuration(
        cls, overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Capture all settings that can change the classic task output."""
        store = get_store()
        routing = await store.get_routing()
        registry = await store.get_role_registry()
        classic_settings = await store.get_classic()
        try:
            effort = resolve_effort((overrides or {}).get("effort"))
        except ValueError as exc:
            raise VariantConfigurationError(str(exc)) from exc
        classic_settings = apply_effort_profile(
            classic_settings, CLASSIC_EFFORT_PROFILES, effort,
        )
        if overrides and isinstance(overrides.get("classic"), dict):
            classic_settings.update(copy.deepcopy(overrides["classic"]))
        if effort != "standard" or (overrides and overrides.get("classic")):
            try:
                classic_settings = validate_classic_settings(classic_settings)
            except ValueError as exc:
                raise VariantConfigurationError(
                    f"Invalid classic override: {exc}"
                ) from exc
        if overrides and isinstance(overrides.get("routing"), dict):
            routing.update(overrides["routing"])
        if overrides and isinstance(overrides.get("role_registry"), dict):
            for role_name, role_patch in overrides["role_registry"].items():
                existing = copy.deepcopy(registry.get(role_name, {}))
                existing.update(role_patch)
                registry[role_name] = existing
        return {
            "variant": cls.descriptor.id,
            "variant_contract_version": cls.descriptor.contract_version,
            "configuration_schema_version": "1",
            "effort": effort,
            "settings": {
                "classic": classic_settings,
                "model_pools": copy.deepcopy(MODEL_POOLS),
                "model_pricing": copy.deepcopy(MODEL_PRICING),
                "edge_node_models": copy.deepcopy(EDGE_NODE_MODELS),
                "node_endpoints": sorted(set(AGENT_ENDPOINTS.values())),
            },
            "model_routing": routing,
            "role_registry": registry,
        }

    @classmethod
    def configuration_from_metadata(
        cls, metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Load the current envelope or migrate saved classic settings."""
        saved = metadata.get("effective_configuration")
        if isinstance(saved, dict):
            version = str(
                saved.get("configuration_schema_version") or ""
            )
            if version != cls.descriptor.configuration_schema_version:
                raise VariantConfigurationError(
                    "Unsupported classic configuration schema version: "
                    f"{version or 'missing'}"
                )
            saved_variant = str(saved.get("variant") or "")
            if saved_variant not in {
                cls.descriptor.id,
                *cls.descriptor.aliases,
            }:
                raise VariantConfigurationError(
                    "The saved configuration variant does not match classic"
                )
            return copy.deepcopy(saved)

        legacy_settings = metadata.get("effective_task_config")
        legacy_routing = metadata.get("effective_routing")
        legacy_registry = metadata.get("effective_registry")
        if not any(
            isinstance(value, dict)
            for value in (
                legacy_settings,
                legacy_routing,
                legacy_registry,
            )
        ):
            return None

        settings = copy.deepcopy(
            legacy_settings if isinstance(legacy_settings, dict) else {}
        )
        if "traditional" in settings and "classic" not in settings:
            settings["classic"] = settings.pop("traditional")
        return {
            "variant": cls.descriptor.id,
            "variant_contract_version": cls.descriptor.contract_version,
            "configuration_schema_version": (
                cls.descriptor.configuration_schema_version
            ),
            "settings": settings,
            "model_routing": copy.deepcopy(
                legacy_routing if isinstance(legacy_routing, dict) else {}
            ),
            "role_registry": copy.deepcopy(
                legacy_registry if isinstance(legacy_registry, dict) else {}
            ),
        }

    @classmethod
    async def run(
        cls, host: VariantHost, request: VariantExecutionRequest,
    ) -> VariantOutcome:
        """Run the classic coordination loop through the shared host."""
        classic_host = cast("ClassicHost", host)
        return await classic_host.run_classic_runtime(
            request,
            engine_class=TraditionalVariant,
            step_result_class=StepResult,
        )


register_variant(
    CLASSIC_VARIANT,
    ClassicVariantRuntime,
    aliases=(LEGACY_CLASSIC_VARIANT,),
)
