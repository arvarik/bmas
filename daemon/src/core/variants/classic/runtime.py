"""The Classic native pair.

``ClassicRuntime`` is the runtime of the native Classic pair. Work
package 1 registered it as a test-only pair that delegates every call
to the legacy engine through the same host call as the legacy adapter.
Work package 4 compiles every submission into one immutable
specification: the capture validates the fidelity profile and the
effort level, snapshots the deployment, compiles the specification
once to validate it, and stores the specification input in the
envelope. The admission compiles the input again with the asset
manifest digest, promotes the specification as an artifact, and binds
its digest. The resolved values project into the legacy settings so
the delegated engine honors the compiled limits and policies. Each
later work package replaces one delegated step with a native step.
"""
from __future__ import annotations

import copy
import dataclasses
from typing import TYPE_CHECKING, Any, cast

from core.variants import VariantConfigurationError
from core.variants.classic.adapter import ClassicHost, ClassicVariantRuntime
from core.variants.classic.compiler import (
    ClassicSpecError,
    StoredSpecification,
    compile_specification,
    legacy_settings_from_spec,
    specification_store,
    store_specification,
)
from core.variants.classic.profiles import resolve_effort_level, resolve_fidelity
from core.variants.classic.spec import (
    BoardSettings,
    ClassicSpecInput,
    DeploymentSnapshot,
    ModelProfileSnapshot,
    PriceSnapshot,
    TaskOverrideSet,
)
from core.variants.traditional import StepResult, TraditionalVariant

if TYPE_CHECKING:
    from core.variants import (
        VariantExecutionRequest,
        VariantHost,
        VariantOutcome,
    )

NATIVE_CONTRACT_VERSION = "2"
NATIVE_CONFIGURATION_SCHEMA_VERSION = "2"


def _price_text(value: Any) -> str:
    """The decimal text of one configured per-token price."""
    if isinstance(value, float):
        return repr(value)
    return str(value)


async def deployment_snapshot(qualification_ids: tuple[str, ...] = ()) -> DeploymentSnapshot:
    """Snapshot the deployment settings in force for one admission."""
    import config
    from config import (
        AGENT_ENDPOINTS,
        MODEL_POOLS,
        MODEL_PRICING,
        TRIAGE_MODEL,
    )
    from settings_store import get_store

    store = get_store()
    profiles = {
        str(alias): ModelProfileSnapshot(
            provider=str(profile.provider), model=str(profile.model),
            reasoning=str(getattr(profile, "reasoning", None) or "provider_default"),
        )
        for alias, profile in (getattr(config, "MODEL_PROFILES", None) or {}).items()
    }
    pricing = {
        str(alias): PriceSnapshot(
            input_cost_per_token=_price_text(price.get("input_cost_per_token", 0)),
            output_cost_per_token=_price_text(price.get("output_cost_per_token", 0)),
            source=str(price.get("source", "bmas.yaml")),
        )
        for alias, price in (MODEL_PRICING or {}).items()
    }
    board = ClassicVariantRuntime.board_settings()
    return DeploymentSnapshot(
        classic=await store.get_classic(),
        routing=await store.get_routing(),
        role_registry=await store.get_role_registry(),
        board=BoardSettings(**board),
        model_pools={str(tier): list(pool) for tier, pool in (MODEL_POOLS or {}).items()},
        model_profiles=profiles,
        model_pricing=pricing,
        triage_model=str(TRIAGE_MODEL),
        node_endpoints=sorted(set(AGENT_ENDPOINTS.values())),
        endpoint_capability_digests=_cached_capability_digests(),
        qualification_ids=sorted(qualification_ids),
    )


def _cached_capability_digests() -> dict[str, str]:
    """The capability document digests the dispatcher already fetched."""
    try:
        import agent_dispatch
    except ImportError:  # pragma: no cover - the daemon always ships it
        return {}
    cache = getattr(agent_dispatch, "_capability_cache", {})
    digests: dict[str, str] = {}
    for url, cached in cache.items():
        document = cached[1] if isinstance(cached, tuple) and len(cached) == 2 else None
        if document is not None:
            digests[str(url)] = document.digest()
    return digests


def specification_input_from(
    overrides: dict[str, Any] | None, deployment: DeploymentSnapshot,
) -> ClassicSpecInput:
    """Build the specification input of one submission."""
    overrides = dict(overrides or {})
    try:
        fidelity = resolve_fidelity(overrides.get("fidelity"))
        level, _preset = resolve_effort_level(overrides.get("effort"))
        task_overrides = TaskOverrideSet(
            classic=dict(overrides.get("classic") or {}),
            routing=dict(overrides.get("routing") or {}),
            role_registry=copy.deepcopy(overrides.get("role_registry") or {}),
            seed=overrides.get("seed"),
        )
    except ValueError as exc:
        raise VariantConfigurationError(str(exc)) from exc
    return ClassicSpecInput(
        fidelity=fidelity, effort=level, deployment=deployment, task_overrides=task_overrides,
    )


class ClassicRuntime:
    """Run the Classic native pair through the legacy engine for now."""

    descriptor = dataclasses.replace(
        ClassicVariantRuntime.descriptor,
        label="Classic blackboard (native)",
        contract_version=NATIVE_CONTRACT_VERSION,
        configuration_schema_version=NATIVE_CONFIGURATION_SCHEMA_VERSION,
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
        """Compile the submission once and store its specification input.

        The envelope keeps the legacy sections the engine reads, with
        the resolved values projected into ``settings.classic``, and
        adds the fidelity profile and the complete specification input.
        """
        from config import EDGE_NODE_MODELS, MODEL_POOLS, MODEL_PRICING
        from settings_store import validate_role_registry

        deployment = await deployment_snapshot()
        spec_input = specification_input_from(overrides, deployment)
        try:
            spec = compile_specification(spec_input)
        except ClassicSpecError as exc:
            raise VariantConfigurationError(f"Invalid classic specification: {exc}") from exc
        routing = dict(deployment.routing)
        routing.update(spec_input.task_overrides.routing)
        registry = {role: entry.model_dump() for role, entry in deployment.role_registry.items()}
        for role, patch in spec_input.task_overrides.role_registry.items():
            existing = copy.deepcopy(registry.get(role, {}))
            existing.update(patch)
            registry[role] = existing
        try:
            validate_role_registry(registry)
        except ValueError as exc:
            raise VariantConfigurationError(str(exc)) from exc
        return {
            "variant": cls.descriptor.id,
            "variant_contract_version": cls.descriptor.contract_version,
            "configuration_schema_version": cls.descriptor.configuration_schema_version,
            "effort": spec.effort.requested_level,
            "fidelity": spec.fidelity.profile_id,
            "settings": {
                "classic": legacy_settings_from_spec(spec),
                "board": deployment.board.model_dump(),
                "model_pools": copy.deepcopy(MODEL_POOLS),
                "model_pricing": copy.deepcopy(MODEL_PRICING),
                "edge_node_models": copy.deepcopy(EDGE_NODE_MODELS),
                "node_endpoints": list(deployment.node_endpoints),
            },
            "model_routing": routing,
            "role_registry": registry,
            "specification_input": spec_input.model_dump(mode="json"),
        }

    @classmethod
    def configuration_from_metadata(
        cls, metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Load a saved native envelope.

        The native pair has no legacy metadata shape to migrate, so an
        envelope of another schema version fails closed.
        """
        saved = metadata.get("effective_configuration")
        if not isinstance(saved, dict):
            return None
        version = str(saved.get("configuration_schema_version") or "")
        if version != cls.descriptor.configuration_schema_version:
            raise VariantConfigurationError(
                "Unsupported native classic configuration schema version: "
                f"{version or 'missing'}"
            )
        if str(saved.get("variant") or "") != cls.descriptor.id:
            raise VariantConfigurationError(
                "The saved configuration variant does not match classic"
            )
        if not isinstance(saved.get("specification_input"), dict):
            raise VariantConfigurationError(
                "The saved native configuration carries no specification input"
            )
        return copy.deepcopy(saved)

    @classmethod
    def compile_specification_for_admission(
        cls,
        effective_configuration: dict[str, Any] | None,
        *,
        run_id: str,
        asset_manifest_digest: str,
        qualification_ids: tuple[str, ...] = (),
    ) -> StoredSpecification:
        """Compile the stored input and promote the specification artifact.

        The admission calls this once per new run. The input comes from
        the envelope the submission captured, so the compiled values
        equal the ones the submission validated, plus the asset
        manifest digest and the live qualification identifiers.
        """
        stored_input = (effective_configuration or {}).get("specification_input")
        if not isinstance(stored_input, dict):
            raise ClassicSpecError("The native pair needs a compiled specification input")
        try:
            spec_input = ClassicSpecInput.model_validate(stored_input)
        except ValueError as exc:
            raise ClassicSpecError(f"Invalid specification input: {exc}") from exc
        deployment = spec_input.deployment.model_copy(update={"qualification_ids": sorted(qualification_ids)})
        spec_input = spec_input.model_copy(update={
            "deployment": deployment, "asset_manifest_digest": asset_manifest_digest,
        })
        spec = compile_specification(spec_input)
        return store_specification(spec, store=specification_store(), referenced_by=run_id)

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
