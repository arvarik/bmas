"""Compile one ``ClassicSpecInput`` into one immutable ``ClassicSpec``.

The compiler resolves the layers in one declared order: the fidelity
profile, the effort preset, the deployment settings, the task
overrides, and the deployment caps. Every field records which layer set
its effective value and what every layer offered. A field the fidelity
profile fixes refuses every later value and records a warning. The
``balanced`` preset yields its documented values to the deployment
settings, so the ``standard`` alias runs with the session settings.
Every other preset keeps its own values over the deployment defaults,
and only a task override or a cap changes them. The caps clamp every
bounded value after the task overrides and record each adjustment.

The compiler then resolves the models, the prices as ``Money``, the
seeds, the endpoint sets, the prompt template digests, the lineage
registry, and the termination rules, and it estimates the maximum
in-flight allowance, the cost range, and the latency range. It
normalizes the result with ``plain_json`` and digests it. The stored
specification is a promoted artifact, and the admission binds its
digest.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agent_protocol import CURRENT_AGENT_PROTOCOL_VERSION
from config_schema import CLASSIC_ROLES, REQUIRED_CLASSIC_ROLES, resolve_consensus_strategy
from core.asset_store import (
    ARTIFACT_CONTENT_DIGEST_DOMAIN,
    ArtifactStore,
    DataClass,
    RetentionClass,
)
from core.digest_profile import digest_bytes, digest_hex, plain_json
from core.money import Money, MoneyError
from core.variants.classic.outcomes import terminal_reasons_for
from core.variants.classic.profiles import (
    DEPLOYMENT_CAPS,
    DEPLOYMENT_CAPS_VERSION,
    EFFORT_ALIAS_TABLE_VERSION,
    EFFORT_PROFILES,
    FIDELITY_PROFILES,
    SCHEMA_DEFAULTS,
    resolve_effort_level,
    resolve_fidelity,
)
from core.variants.classic.spec import (
    TIERS,
    CapAdjustment,
    ClassicSpec,
    ClassicSpecEstimate,
    ClassicSpecInput,
    ClassicSpecWarning,
    FieldResolution,
    LineageAssignment,
    ModelRef,
    MoneyValue,
    PriceRates,
    PriceSourceStrings,
    SpecBoard,
    SpecCleaner,
    SpecCleanerWeights,
    SpecConsensus,
    SpecControlPlaneAdapter,
    SpecCoordination,
    SpecDeploymentCaps,
    SpecEffort,
    SpecEndpoint,
    SpecEndpointSet,
    SpecFailoverPolicy,
    SpecFidelity,
    SpecInputs,
    SpecLimits,
    SpecMemory,
    SpecModelLineage,
    SpecModels,
    SpecPrices,
    SpecPrompts,
    SpecProviderCapabilities,
    SpecRandomness,
    SpecRecovery,
    SpecRouting,
    SpecSalienceWeights,
    SpecTeam,
    SpecTermination,
    SpecVerification,
)

SPEC_DIGEST_DOMAIN = "classic-specification"
SPEC_INPUT_DIGEST_DOMAIN = "classic-specification-input"
SEED_STREAM_DOMAIN = "classic-seed-stream"
SPEC_MEDIA_TYPE = "application/vnd.bmas.classic-spec+json"
PROMPT_MEDIA_TYPE = "text/plain"
SPEC_ACCESS_POLICY = "run-scope"
CURRENCY = "USD"
MILLION = 1_000_000

PROMPT_REGISTRY_VERSION = "classic-prompts/1"
PROMPT_RENDER_ENGINE_VERSION = "classic-prompt-renderer/1"
GENERATED_DEFINITION_SCHEMA_VERSION = "expert-definition/1"
RENDER_RECEIPT_SCHEMA_VERSION = "prompt-render-receipt/1"
PROVIDER_SNAPSHOT_VERSION = "provider-capabilities/1"
LINEAGE_REGISTRY_VERSION = "model-lineage/1"
PRICE_TABLE_VERSION = "task-price-snapshot/1"
ENDPOINT_SET_VERSION = "endpoint-set/1"
ENDPOINT_ADAPTER_ID = "bmas-agent"
CONTROL_PLANE_ADAPTER = SpecControlPlaneAdapter(
    adapter_id="bmas-local-model", adapter_version="local-model-adapter/1",
)
FAILOVER_POLICY = SpecFailoverPolicy(
    policy_id="ordered-safe-failover",
    policy_version="ordered-safe-failover/1",
    retryable_failures=["capacity", "transport_before_dispatch"],
    stateful_session_action="clear_or_rebind",
    max_endpoint_changes=1,
)
# The board views estimate tokens as characters divided by four. No
# provider tokenizer is pinned, so the specification records the
# estimator the runtime applies.
TOKENIZER_ID = "bmas-character-estimate"
TOKENIZER_REVISION = "four-characters-per-token/1"
# The floor one activation needs from dispatch to receipt, for the
# latency estimate. The first round activates the required roles.
ACTIVATION_LATENCY_FLOOR_SECONDS = 5
ESTIMATE_OUTPUT_TOKENS = 512
ENDPOINT_SET_NAMES = (
    "worker_primary", "worker_secondary", "worker_tertiary", "worker_quaternary",
    "worker_quinary", "worker_senary", "worker_septenary",
)
# The roles the deployment activates and the tier each one follows.
# The triage result selects the tier at run time; the control unit and
# the vote call the light tier model.
ROLE_TIER_BINDING: dict[str, str] = {
    "expert_generator": "triage_tier",
    "planner": "triage_tier",
    "expert": "triage_tier",
    "critic": "triage_tier",
    "conflict_resolver": "triage_tier",
    "cleaner": "triage_tier",
    "decider": "triage_tier",
    "control_unit": "light",
    "sole": "light",
}
VERIFIER_TIER = "medium"

# The legacy classic settings and the specification field each one
# sets. The deployment settings and the ``classic`` task overrides use
# these keys, so the submit route contract stays the same.
LEGACY_FIELDS: dict[str, str] = {
    "max_rounds": "coordination.max_rounds",
    "max_duration_s": "limits.max_duration_seconds",
    "budget_ceiling_usd": "limits.max_cost",
    "max_concurrent_activations": "coordination.max_parallel_agents",
    "cleaner_entry_threshold": "cleaner.entry_threshold",
    "cleaner_token_threshold": "cleaner.token_threshold",
    "stall_rounds": "recovery.stall_rounds",
    "max_replans": "recovery.max_replans",
    "cu_mode": "coordination.control_strategy",
    "coordinator_narration": "coordination.narration",
    "sole_similarity": "consensus.strategy",
    "grace_verification": "verification.solution_review",
    "require_evidence": "verification.evidence_policy",
    "actor_context": "memory.provider_session_scope",
    "round_execution": "coordination.round_execution",
    "view_budget_tokens": "board.view_budget_tokens",
}
LEGACY_MAP_FIELDS: dict[str, str] = {
    "experts_per_tier": "team.experts_by_tier",
    "cleaner_retention_weights": "cleaner.retention_weights",
}
CU_MODES = {"llm": "model_ranked", "heuristic_first": "deterministic_first"}
ACTOR_CONTEXTS = {"chained": "task", "fresh": "activation"}


class ClassicSpecError(ValueError):
    """The specification input cannot compile."""


@dataclass(frozen=True)
class StoredSpecification:
    """One compiled specification with its digests and artifact bytes."""

    spec: ClassicSpec
    specification_digest: str
    artifact_digest: str
    payload: bytes


# ── Value conversion ──────────────────────────────────────────────────


def money_text(value: Any) -> str:
    """Return the decimal text of one configured amount.

    A float travels as its shortest round-trip text, so ``0.5`` becomes
    ``"0.5"`` and never a binary expansion. The text then parses into
    exact nanos through ``Money``.
    """
    if isinstance(value, bool):
        raise ClassicSpecError("A money amount cannot be a boolean")
    if isinstance(value, (int, float)):
        value = repr(value)
    if not isinstance(value, str):
        raise ClassicSpecError(f"A money amount needs decimal text, not {type(value).__name__}")
    try:
        amount = Decimal(value.strip())
    except ArithmeticError as exc:
        raise ClassicSpecError(f"Invalid money amount: {value!r}") from exc
    if not amount.is_finite() or amount <= 0:
        raise ClassicSpecError(f"A money amount must be positive: {value!r}")
    return format(amount.normalize(), "f")


def _money(text: str) -> Money:
    try:
        return Money.from_decimal_string(CURRENCY, text)
    except MoneyError as exc:
        raise ClassicSpecError(str(exc)) from exc


def _per_million_text(per_token: str) -> str:
    """Convert one per-token price text into per-million text."""
    try:
        amount = Decimal(per_token.strip()) * MILLION
    except ArithmeticError as exc:
        raise ClassicSpecError(f"Invalid price: {per_token!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise ClassicSpecError(f"A price cannot be negative: {per_token!r}")
    return format(amount.normalize(), "f")


def _legacy_value(key: str, value: Any) -> Any:
    """Translate one legacy classic setting into its specification value."""
    if key == "budget_ceiling_usd":
        return money_text(value)
    if key == "cu_mode":
        if value not in CU_MODES:
            raise ClassicSpecError(f"Unknown cu_mode {value!r}")
        return CU_MODES[str(value)]
    if key == "sole_similarity":
        try:
            return resolve_consensus_strategy(value)
        except ValueError as exc:
            raise ClassicSpecError(str(exc)) from exc
    if key == "grace_verification":
        return "required" if bool(value) else "not_required"
    if key == "require_evidence":
        return "typed_sources" if bool(value) else "optional_sources"
    if key == "actor_context":
        if value not in ACTOR_CONTEXTS:
            raise ClassicSpecError(f"Unknown actor_context {value!r}")
        return ACTOR_CONTEXTS[str(value)]
    return copy.deepcopy(value)


def legacy_settings_to_fields(settings: dict[str, Any]) -> dict[str, Any]:
    """Translate one legacy classic settings mapping into field values.

    A dotted key names a specification field directly. Any other key
    must be a legacy classic setting.
    """
    fields: dict[str, Any] = {}
    for key, value in settings.items():
        name = str(key)
        if name in LEGACY_FIELDS:
            fields[LEGACY_FIELDS[name]] = _legacy_value(name, value)
        elif name in LEGACY_MAP_FIELDS:
            if not isinstance(value, dict):
                raise ClassicSpecError(f"The setting {name} needs a mapping")
            for sub_key, sub_value in value.items():
                fields[f"{LEGACY_MAP_FIELDS[name]}.{sub_key}"] = copy.deepcopy(sub_value)
        elif name in SCHEMA_DEFAULTS:
            fields[name] = money_text(value) if name == "limits.max_cost" else copy.deepcopy(value)
        else:
            raise ClassicSpecError(f"Unknown classic setting {name!r}")
    unknown = sorted(name for name in fields if name not in SCHEMA_DEFAULTS)
    if unknown:
        raise ClassicSpecError(f"Unknown specification field(s): {', '.join(unknown)}")
    return fields


def deployment_fields(deployment: Any) -> dict[str, Any]:
    """The field values the deployment settings offer."""
    fields = legacy_settings_to_fields(dict(deployment.classic))
    board = deployment.board
    fields["board.max_entry_body_characters"] = int(board.max_entry_chars)
    fields["board.max_title_characters"] = int(board.max_title_len)
    for name, weight in board.salience_weights.items():
        path = f"board.salience_weights.{name}"
        if path not in SCHEMA_DEFAULTS:
            raise ClassicSpecError(f"Unknown salience weight {name!r}")
        fields[path] = float(weight)
    return fields


# ── Layered resolution ────────────────────────────────────────────────


class _Resolution:
    """The working state of the layered field resolution."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = dict(SCHEMA_DEFAULTS)
        self.records: dict[str, dict[str, Any]] = {
            path: {"layer": "schema_default", "offered": {"schema_default": value}, "not_applied": []}
            for path, value in SCHEMA_DEFAULTS.items()
        }
        self.warnings: list[ClassicSpecWarning] = []
        self.fixed: set[str] = set()
        self.declared_by_effort: set[str] = set()

    def apply(self, layer: str, offered: dict[str, Any], *, keep_effort_values: bool = False) -> None:
        for path in sorted(offered):
            value = offered[path]
            record = self.records[path]
            record["offered"][layer] = value
            if path in self.fixed and layer != "fidelity":
                if value == self.values[path]:
                    # The layer agrees with the fixed value: nothing to reject.
                    continue
                record["not_applied"].append(layer)
                self.warnings.append(ClassicSpecWarning(
                    kind="rejected_override", field=path, layer=layer,  # type: ignore[arg-type]
                    message=f"The fidelity profile fixes {path}; the {layer} value does not apply.",
                    requested=value, effective=self.values[path],
                ))
                continue
            if keep_effort_values and path in self.declared_by_effort:
                record["not_applied"].append(layer)
                continue
            self.values[path] = value
            record["layer"] = layer

    def clamp(self) -> list[CapAdjustment]:
        adjustments: list[CapAdjustment] = []
        for path in sorted(DEPLOYMENT_CAPS):
            minimum, maximum = DEPLOYMENT_CAPS[path]
            requested = self.values[path]
            if path == "limits.max_cost":
                money = _money(money_text(requested))
                low, high = _money(str(minimum)), _money(str(maximum))
                if money.compare(low) < 0:
                    effective, bound, rule = low.to_decimal_string(), minimum, "minimum"
                elif money.compare(high) > 0:
                    effective, bound, rule = high.to_decimal_string(), maximum, "maximum"
                else:
                    continue
            else:
                if isinstance(requested, bool) or not isinstance(requested, (int, float)):
                    raise ClassicSpecError(f"The field {path} needs a number, not {requested!r}")
                if requested < minimum:
                    effective, bound, rule = minimum, minimum, "minimum"
                elif requested > maximum:
                    effective, bound, rule = maximum, maximum, "maximum"
                else:
                    continue
            self.values[path] = effective
            record = self.records[path]
            record["offered"]["deployment_caps"] = effective
            record["layer"] = "deployment_caps"
            adjustments.append(CapAdjustment(
                field=path, requested=requested, effective=effective, bound=bound, rule=rule,  # type: ignore[arg-type]
            ))
            self.warnings.append(ClassicSpecWarning(
                kind="clamped_value", field=path, layer="deployment_caps",
                message=f"The deployment cap {rule} {bound} clamps {path}.",
                requested=requested, effective=effective,
            ))
        return adjustments


def resolve_fields(spec_input: ClassicSpecInput) -> tuple[_Resolution, list[CapAdjustment], str, str, str]:
    """Resolve every policy field through the five layers, in order."""
    try:
        fidelity_id = resolve_fidelity(spec_input.fidelity)
        level, preset_id = resolve_effort_level(spec_input.effort)
    except ValueError as exc:
        raise ClassicSpecError(str(exc)) from exc
    fidelity = FIDELITY_PROFILES[fidelity_id]
    preset = EFFORT_PROFILES[preset_id]
    state = _Resolution()
    state.apply("fidelity", dict(fidelity.values))
    state.fixed = set(fidelity.fixed_fields)
    state.apply("effort", dict(preset.values))
    state.declared_by_effort = set(preset.values)
    state.apply(
        "deployment", deployment_fields(spec_input.deployment),
        keep_effort_values=not preset.overridable_by_deployment,
    )
    state.apply("task_overrides", legacy_settings_to_fields(dict(spec_input.task_overrides.classic)))
    adjustments = state.clamp()
    return state, adjustments, fidelity_id, level, preset_id


# ── Model, price, endpoint, and prompt resolution ─────────────────────


def _model_ref(alias: str, deployment: Any) -> ModelRef:
    profile = deployment.model_profiles.get(alias)
    provider = profile.provider if profile else "unknown"
    model = profile.model if profile else alias
    reasoning = profile.reasoning if profile else "provider_default"
    return ModelRef(
        alias=alias,
        model_id=f"{provider}/{model}",
        # No provider revision pin exists today: the configured model
        # string is the only revision the deployment declares.
        model_revision=model,
        tokenizer_id=TOKENIZER_ID,
        tokenizer_revision=TOKENIZER_REVISION,
        reasoning=reasoning,
    )


def _routing(spec_input: ClassicSpecInput) -> dict[str, str]:
    routing = dict(spec_input.deployment.routing)
    routing.update(spec_input.task_overrides.routing)
    return routing


def _registry(spec_input: ClassicSpecInput) -> dict[str, dict[str, Any]]:
    registry = {
        role: entry.model_dump() for role, entry in spec_input.deployment.role_registry.items()
    }
    for role, patch in spec_input.task_overrides.role_registry.items():
        existing = dict(registry.get(role, {}))
        existing.update(copy.deepcopy(patch))
        registry[role] = existing
    unknown = sorted(role for role in registry if role not in CLASSIC_ROLES)
    if unknown:
        raise ClassicSpecError(f"Unknown role registry key(s): {', '.join(unknown)}")
    return registry


def resolve_models(spec_input: ClassicSpecInput, values: dict[str, Any]) -> SpecModels:
    deployment = spec_input.deployment
    routing = _routing(spec_input)
    tier_models = {tier: _model_ref(routing.get(tier, "medium"), deployment) for tier in TIERS}
    expert_pool = {
        tier: [
            _model_ref(alias, deployment)
            for alias in (deployment.model_pools.get(tier) or [routing.get(tier, "medium")])
        ]
        for tier in TIERS
    }
    binding = dict(ROLE_TIER_BINDING)
    verifier = None
    if int(values["verification.independent_verifiers"]) > 0:
        verifier = tier_models[VERIFIER_TIER]
        binding["verifier"] = VERIFIER_TIER
    return SpecModels(
        triage=_model_ref(deployment.triage_model, deployment),
        control_unit=tier_models["light"],
        sole=tier_models["light"],
        verifier=verifier,
        tier_models=tier_models,
        expert_pool=expert_pool,
        role_tier_binding=binding,
        temperature=values["models.temperature"],
    )


def _referenced_refs(models: SpecModels) -> list[ModelRef]:
    refs = [models.triage, models.control_unit, models.sole]
    refs.extend(models.tier_models[tier] for tier in TIERS)
    for tier in TIERS:
        refs.extend(models.expert_pool[tier])
    if models.verifier is not None:
        refs.append(models.verifier)
    unique: dict[str, ModelRef] = {}
    for ref in refs:
        unique.setdefault(ref.alias, ref)
    return [unique[alias] for alias in sorted(unique)]


def resolve_prices(
    refs: list[ModelRef], deployment: Any, warnings: list[ClassicSpecWarning],
) -> SpecPrices:
    sources: dict[str, PriceSourceStrings] = {}
    rates: dict[str, PriceRates] = {}
    for ref in refs:
        snapshot = deployment.model_pricing.get(ref.alias)
        if snapshot is None:
            warnings.append(ClassicSpecWarning(
                kind="missing_price", field=f"prices.rates.{ref.alias}",
                message=f"The deployment declares no price for the model alias {ref.alias!r}.",
            ))
            continue
        input_text = _per_million_text(snapshot.input_cost_per_token)
        output_text = _per_million_text(snapshot.output_cost_per_token)
        sources[ref.alias] = PriceSourceStrings(input_per_million=input_text, output_per_million=output_text)
        rates[ref.alias] = PriceRates(
            input_per_million=MoneyValue.from_money(_money(input_text)),
            output_per_million=MoneyValue.from_money(_money(output_text)),
        )
    return SpecPrices(table_version=PRICE_TABLE_VERSION, source_amount_strings=sources, rates=rates,
                      provenance={alias: deployment.model_pricing[alias].source for alias in rates})


def resolve_provider_capabilities(
    refs: list[ModelRef], spec_input: ClassicSpecInput, values: dict[str, Any],
) -> SpecProviderCapabilities:
    snapshot = {
        ref.alias: {"model_id": ref.model_id, "model_revision": ref.model_revision, "reasoning": ref.reasoning}
        for ref in refs
    }
    return SpecProviderCapabilities(
        snapshot_version=PROVIDER_SNAPSHOT_VERSION,
        snapshot_digest=digest_hex(SPEC_INPUT_DIGEST_DOMAIN, {"provider_snapshot": snapshot}),
        # The delegated engine sends no seed to a provider yet.
        seed_support="unsupported",
        session_support=values["memory.provider_session_scope"] == "task",
        qualification_ids=sorted(spec_input.deployment.qualification_ids),
    )


def resolve_routing(spec_input: ClassicSpecInput) -> SpecRouting:
    registry = _registry(spec_input)
    node_endpoints = list(spec_input.deployment.node_endpoints)
    digests = spec_input.deployment.endpoint_capability_digests
    sets: dict[str, SpecEndpointSet] = {}
    set_names: dict[tuple[str, ...], str] = {}
    role_sets: dict[str, str] = {}
    for role in CLASSIC_ROLES:
        entry = registry.get(role, {})
        if entry.get("enabled", True) is False:
            continue
        endpoints = [str(url) for url in (entry.get("endpoints") or []) if url] or node_endpoints
        if not endpoints:
            if role in REQUIRED_CLASSIC_ROLES:
                raise ClassicSpecError(f"The required role {role!r} has no endpoint")
            continue
        key = tuple(endpoints)
        name = set_names.get(key)
        if name is None:
            if len(set_names) >= len(ENDPOINT_SET_NAMES):
                raise ClassicSpecError("Too many distinct endpoint sets")
            name = ENDPOINT_SET_NAMES[len(set_names)]
            set_names[key] = name
            sets[name] = SpecEndpointSet(
                endpoint_set_version=ENDPOINT_SET_VERSION,
                endpoints=[
                    SpecEndpoint(
                        endpoint_id=url, order=index + 1,
                        adapter_id=ENDPOINT_ADAPTER_ID,
                        adapter_version=f"agent-protocol/{CURRENT_AGENT_PROTOCOL_VERSION}",
                        capability_record_digest=digests.get(url),
                    )
                    for index, url in enumerate(endpoints)
                ],
                failover_policy=FAILOVER_POLICY,
            )
        role_sets[role] = name
    missing = [role for role in REQUIRED_CLASSIC_ROLES if role not in role_sets]
    if missing:
        raise ClassicSpecError(f"The role registry disables a required role: {', '.join(missing)}")
    return SpecRouting(endpoint_sets=sets, role_endpoint_sets=role_sets, control_plane_adapter=CONTROL_PLANE_ADAPTER)


def resolve_lineage(
    refs: list[ModelRef], values: dict[str, Any], warnings: list[ClassicSpecWarning],
) -> SpecModelLineage:
    # No verified lineage registry exists yet. Every resolved model maps
    # to its own unverified lineage, so an independence rule counts no
    # verified family and the diagnostics stay visible.
    assignments = {
        ref.model_id: LineageAssignment(lineage_id=ref.model_id, lineage_status="unverified")
        for ref in sorted(refs, key=lambda item: item.model_id)
    }
    if int(values["team.independent_model_families"]) > 1:
        warnings.append(ClassicSpecWarning(
            kind="unverified_lineage", field="team.independent_model_families",
            message="The independence rule counts verified lineages only, and no lineage is verified.",
            requested=values["team.independent_model_families"], effective=0,
        ))
    return SpecModelLineage(
        registry_version=LINEAGE_REGISTRY_VERSION,
        registry_digest=digest_hex(SPEC_INPUT_DIGEST_DOMAIN, {
            "registry_version": LINEAGE_REGISTRY_VERSION,
            "assignments": {key: value.model_dump() for key, value in assignments.items()},
        }),
        assignments=assignments,
    )


def static_prompt_templates() -> dict[str, str]:
    """The static prompt templates of the runtime, keyed by template id.

    The expert template renders through ``generate_expert_persona``
    with placeholder tokens, so its digest covers the template and
    never one generated definition.
    """
    from core.triage import TRIAGE_SYSTEM_PROMPT
    from models.personas import (
        AG_SYSTEM_PROMPT,
        CU_SYSTEM_PROMPT,
        ROLE_PERSONAS,
        SOLE_SYSTEM_PROMPT,
        generate_expert_persona,
    )

    templates = {
        "triage": TRIAGE_SYSTEM_PROMPT,
        "expert_generator": AG_SYSTEM_PROMPT,
        "control_unit": CU_SYSTEM_PROMPT,
        "expert": generate_expert_persona("<expert-name>", "<expert-ability>", "<task-context>"),
        "sole": SOLE_SYSTEM_PROMPT,
    }
    for role in ("planner", "critic", "conflict_resolver", "cleaner", "decider"):
        templates[role] = ROLE_PERSONAS[role]
    return templates


def prompt_template_digest(body: str) -> str:
    return digest_bytes(ARTIFACT_CONTENT_DIGEST_DOMAIN, body.encode("utf-8"))


def resolve_prompts() -> SpecPrompts:
    templates = static_prompt_templates()
    return SpecPrompts(
        registry_version=PROMPT_REGISTRY_VERSION,
        render_engine_version=PROMPT_RENDER_ENGINE_VERSION,
        static_template_digests={name: prompt_template_digest(templates[name]) for name in sorted(templates)},
        generated_definition_schema_version=GENERATED_DEFINITION_SCHEMA_VERSION,
        generated_definition_asset_digest=None,
        activation_render_receipt_schema_version=RENDER_RECEIPT_SCHEMA_VERSION,
    )


def _seed_stream(task_seed: int, stream: str) -> int:
    # Thirteen hexadecimal digits keep the stream seed inside the
    # I-JSON safe integer range the digest profile accepts.
    return int(digest_hex(SEED_STREAM_DOMAIN, {"task_seed": task_seed, "stream": stream})[:13], 16)


def resolve_randomness(spec_input: ClassicSpecInput, values: dict[str, Any]) -> SpecRandomness:
    task_seed = spec_input.task_overrides.seed
    return SpecRandomness(
        # The delegated engine records the seed and never applies it yet.
        seed_policy="recorded",
        task_seed=task_seed,
        roster_seed=None if task_seed is None else _seed_stream(task_seed, "roster"),
        candidate_order_seed=None if task_seed is None else _seed_stream(task_seed, "candidate_order"),
        candidate_order=values["randomness.candidate_order"],
    )


def estimate(
    values: dict[str, Any], prices: SpecPrices, limits: SpecLimits, required_roles: int,
    prices_complete: bool = True,
) -> ClassicSpecEstimate:
    sequential = values["coordination.round_execution"] == "sequential"
    in_flight = 1 if sequential else int(values["coordination.max_parallel_agents"])
    cost_minimum = Money.zero(CURRENCY)
    if prices.rates:
        cheapest = min(
            (rate for rate in prices.rates.values()),
            key=lambda rate: (rate.input_per_million.amount_nanos, rate.output_per_million.amount_nanos),
        )
        cost_minimum = (
            cheapest.input_per_million.to_money().scale_ratio(int(values["board.view_budget_tokens"]), MILLION)
            .add(cheapest.output_per_million.to_money().scale_ratio(ESTIMATE_OUTPUT_TOKENS, MILLION))
        )
    if cost_minimum.compare(limits.max_cost.to_money()) > 0:
        cost_minimum = limits.max_cost.to_money()
    activations = required_roles if sequential else 2
    latency_minimum = min(limits.max_duration_seconds, ACTIVATION_LATENCY_FLOOR_SECONDS * activations)
    return ClassicSpecEstimate(
        max_in_flight_activations=in_flight,
        cost_minimum=MoneyValue.from_money(cost_minimum) if prices.rates and prices_complete else None,
        cost_maximum=limits.max_cost,
        latency_minimum_seconds=latency_minimum,
        latency_maximum_seconds=limits.max_duration_seconds,
    )


# ── The compiler ──────────────────────────────────────────────────────


def _section(values: dict[str, Any], prefix: str) -> dict[str, Any]:
    return {path[len(prefix) + 1:]: value for path, value in values.items() if path.startswith(prefix + ".") and "." not in path[len(prefix) + 1:]}


def compile_specification(spec_input: ClassicSpecInput) -> ClassicSpec:
    """Compile one input into one complete, validated specification."""
    state, adjustments, fidelity_id, level, preset_id = resolve_fields(spec_input)
    values = state.values
    warnings = state.warnings
    fidelity = FIDELITY_PROFILES[fidelity_id]
    preset = EFFORT_PROFILES[preset_id]
    if level != preset_id:
        warnings.append(ClassicSpecWarning(
            kind="effort_alias", field="effort.profile_id", layer="effort",
            message=f"The shipped level {level!r} resolves to the preset {preset_id!r}.",
            requested=level, effective=preset_id,
        ))
    deployment = spec_input.deployment.model_copy(update={"model_pricing": {
        **spec_input.deployment.model_pricing, **spec_input.task_overrides.price_overrides,
    }})
    try:
        models = resolve_models(spec_input, values)
        refs = _referenced_refs(models)
        prices = resolve_prices(refs, deployment, warnings)
        limits = SpecLimits(
            max_cost=MoneyValue.from_money(_money(money_text(values["limits.max_cost"]))),
            **{key: value for key, value in _section(values, "limits").items() if key != "max_cost"},
        )
        if not limits.strict_pricing:
            raise ClassicSpecError("Native Classic requires strict pricing")
        routing = resolve_routing(spec_input)
        team = SpecTeam(
            experts_by_tier={tier: int(values[f"team.experts_by_tier.{tier}"]) for tier in TIERS},
            required_roles=list(REQUIRED_CLASSIC_ROLES),
            model_assignment=values["team.model_assignment"],
            independent_model_families=int(values["team.independent_model_families"]),
        )
        spec = ClassicSpec(
            runtime=spec_input.runtime,
            fidelity=SpecFidelity(profile_id=fidelity.profile_id, profile_version=fidelity.profile_version),
            effort=SpecEffort(
                requested_level=level, profile_id=preset.profile_id,
                profile_version=preset.profile_version, alias_table_version=EFFORT_ALIAS_TABLE_VERSION,
            ),
            inputs=SpecInputs(
                asset_manifest_digest=spec_input.asset_manifest_digest,
                deployment_snapshot_digest=digest_hex(SPEC_INPUT_DIGEST_DOMAIN, plain_json(deployment.model_dump())),
                task_overrides_digest=digest_hex(SPEC_INPUT_DIGEST_DOMAIN, plain_json(spec_input.task_overrides.model_dump())),
            ),
            team=team,
            models=models,
            provider_capabilities=resolve_provider_capabilities(refs, spec_input, values),
            routing=routing,
            model_lineage=resolve_lineage(refs, values, warnings),
            prompts=resolve_prompts(),
            prices=prices,
            randomness=resolve_randomness(spec_input, values),
            coordination=SpecCoordination(**_section(values, "coordination")),
            board=SpecBoard(
                salience_weights=SpecSalienceWeights(**_section(values, "board.salience_weights")),
                **_section(values, "board"),
            ),
            cleaner=SpecCleaner(
                retention_weights=SpecCleanerWeights(**_section(values, "cleaner.retention_weights")),
                **_section(values, "cleaner"),
            ),
            memory=SpecMemory(**_section(values, "memory")),
            verification=SpecVerification(**_section(values, "verification")),
            consensus=SpecConsensus(**_section(values, "consensus")),
            limits=limits,
            recovery=SpecRecovery(**_section(values, "recovery")),
            termination=SpecTermination(
                terminal_reasons=terminal_reasons_for(fidelity_id), **_section(values, "termination"),
            ),
            deployment_caps=SpecDeploymentCaps(
                policy_version=DEPLOYMENT_CAPS_VERSION, applied_after_user_overrides=True, adjustments=adjustments,
            ),
            resolution={path: FieldResolution(**state.records[path]) for path in sorted(state.records)},
            warnings=warnings,
            estimate=estimate(values, prices, limits, len(REQUIRED_CLASSIC_ROLES),
                              all(ref.alias in prices.rates for ref in refs)),
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        raise ClassicSpecError(f"{location}: {first.get('msg', 'invalid value')}") from exc
    return spec


def normalized_specification(spec: ClassicSpec) -> dict[str, Any]:
    """The JSON-safe view of one specification under the digest profile."""
    return plain_json(spec.model_dump(mode="json"))


def specification_digest(spec: ClassicSpec) -> str:
    return digest_hex(SPEC_DIGEST_DOMAIN, normalized_specification(spec))


def specification_payload(spec: ClassicSpec) -> bytes:
    """The canonical artifact bytes of one specification."""
    text = json.dumps(normalized_specification(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("utf-8")


def specification_store() -> ArtifactStore:
    """The artifact store that holds every compiled specification."""
    import database as db

    return ArtifactStore(Path(db.DB_PATH).parent / "classic-specifications", "tenant-default")


def _promote(store: ArtifactStore, payload: bytes, *, media_type: str, referenced_by: str) -> str:
    staged = store.stage(
        payload,
        declared_digest=digest_bytes(ARTIFACT_CONTENT_DIGEST_DOMAIN, payload),
        declared_size=len(payload),
        media_type=media_type,
        scanner_result="clean",
        data_class=DataClass.INTERNAL,
        access_policy=SPEC_ACCESS_POLICY,
        retention_class=RetentionClass.REPLAY_REQUIRED,
    )
    digest = store.promote(staged)
    store.commit_reference(digest, referenced_by=referenced_by)
    return digest


def store_specification(
    spec: ClassicSpec, *, store: ArtifactStore, referenced_by: str,
) -> StoredSpecification:
    """Promote one specification and its prompt templates as artifacts.

    The static templates promote first, so every template digest in
    the specification references immutable bytes before the
    specification itself promotes.
    """
    templates = static_prompt_templates()
    for name in sorted(templates):
        digest = _promote(
            store, templates[name].encode("utf-8"), media_type=PROMPT_MEDIA_TYPE, referenced_by=referenced_by,
        )
        if digest != spec.prompts.static_template_digests[name]:
            raise ClassicSpecError(f"The prompt template {name!r} changed after compilation")
    payload = specification_payload(spec)
    artifact_digest = _promote(store, payload, media_type=SPEC_MEDIA_TYPE, referenced_by=referenced_by)
    return StoredSpecification(
        spec=spec,
        specification_digest=specification_digest(spec),
        artifact_digest=artifact_digest,
        payload=payload,
    )


def load_specification(store: ArtifactStore, artifact_digest: str) -> ClassicSpec:
    """Read one promoted specification back into its model."""
    record = store.read_object(artifact_digest)
    if record.get("redacted"):
        raise ClassicSpecError("The specification artifact is erased")
    return ClassicSpec.model_validate(json.loads(record["payload"].decode("utf-8")))


# ── The legacy projection ─────────────────────────────────────────────


def legacy_settings_from_spec(spec: ClassicSpec) -> dict[str, Any]:
    """Project the resolved values into the settings the engine reads.

    The native pair delegates its rounds to the legacy engine until the
    later work packages replace each step. The engine reads these
    keys, so the run honors the compiled limits and policies.
    """
    cu_modes = {value: key for key, value in CU_MODES.items()}
    actor_contexts = {value: key for key, value in ACTOR_CONTEXTS.items()}
    weights = spec.cleaner.retention_weights
    return {
        "max_rounds": spec.coordination.max_rounds,
        "max_duration_s": spec.limits.max_duration_seconds,
        "budget_ceiling_usd": float(spec.limits.max_cost.to_money().to_decimal_string()),
        "max_concurrent_activations": spec.coordination.max_parallel_agents,
        "experts_per_tier": dict(spec.team.experts_by_tier),
        "cleaner_entry_threshold": spec.cleaner.entry_threshold,
        "cleaner_token_threshold": spec.cleaner.token_threshold,
        "cleaner_retention_weights": {
            "salience": weights.salience,
            "confidence": weights.confidence,
            "recency": weights.recency,
            "size_penalty": weights.size_penalty,
        },
        "stall_rounds": spec.recovery.stall_rounds,
        "max_replans": spec.recovery.max_replans,
        "cu_mode": cu_modes[spec.coordination.control_strategy],
        "coordinator_narration": spec.coordination.narration,
        "sole_similarity": spec.consensus.strategy,
        "grace_verification": spec.verification.solution_review == "required",
        "require_evidence": spec.verification.evidence_policy != "optional_sources",
        "actor_context": actor_contexts[spec.memory.provider_session_scope],
        "round_execution": spec.coordination.round_execution,
        "view_budget_tokens": spec.board.view_budget_tokens,
    }
