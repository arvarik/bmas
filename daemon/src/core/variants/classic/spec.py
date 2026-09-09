"""The immutable Classic specification and its input.

``ClassicSpecInput`` is what one admission submits: the runtime pair,
the fidelity profile, the requested effort level, the deployment
snapshot, and the task overrides. ``ClassicSpec`` is what the compiler
produces: every resolved policy value of one native run, the record of
what every layer offered for every field, every rejection and clamp,
the estimate, and the warnings. Every controlled object forbids unknown
fields, so the published schema carries ``additionalProperties: false``
throughout.

Money fields hold exact integer nanos through ``MoneyValue``. Decimal
strings appear only as source evidence in the price snapshot.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.money import Money

SPEC_SCHEMA_VERSION = "classic-spec/1"
RUNTIME_ID = "classic"
NATIVE_CONTRACT_VERSION = "2"
TIERS: tuple[str, ...] = ("simple", "light", "medium", "complex")

Layer = Literal["schema_default", "fidelity", "effort", "deployment", "task_overrides", "deployment_caps"]
LAYERS: tuple[str, ...] = ("fidelity", "effort", "deployment", "task_overrides", "deployment_caps")


class ControlledModel(BaseModel):
    """A controlled object: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class MoneyValue(ControlledModel):
    """One exact amount: a currency code and integer nanos."""

    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount_nanos: int = Field(strict=True)

    @classmethod
    def from_money(cls, money: Money) -> MoneyValue:
        return cls(currency=money.currency, amount_nanos=money.amount_nanos)

    def to_money(self) -> Money:
        return Money(self.currency, self.amount_nanos)


# ── Input ────────────────────────────────────────────────────────────


class SpecRuntime(ControlledModel):
    runtime_id: str = RUNTIME_ID
    runtime_contract_version: str = NATIVE_CONTRACT_VERSION
    runtime_spec_schema_version: str = SPEC_SCHEMA_VERSION


class ModelProfileSnapshot(ControlledModel):
    """One configured model alias as the deployment declares it."""

    provider: str
    model: str
    reasoning: str = "provider_default"


class PriceSnapshot(ControlledModel):
    """One alias price as the deployment declares it, per token."""

    input_cost_per_token: str
    output_cost_per_token: str
    source: str = Field(default="bmas.yaml", min_length=1)


class BoardSettings(ControlledModel):
    max_entry_chars: int = Field(ge=1)
    max_title_len: int = Field(ge=1)
    salience_weights: dict[str, float] = Field(default_factory=dict)


class RoleRegistryEntry(ControlledModel):
    enabled: bool = True
    profile: str | None = None
    preferred_host: str | None = None
    dispatch_port: int | None = None
    endpoints: list[str] = Field(default_factory=list)


class DeploymentSnapshot(ControlledModel):
    """The deployment settings in force when the task was submitted."""

    classic: dict[str, Any] = Field(default_factory=dict)
    routing: dict[str, str] = Field(default_factory=dict)
    role_registry: dict[str, RoleRegistryEntry] = Field(default_factory=dict)
    board: BoardSettings
    model_pools: dict[str, list[str]] = Field(default_factory=dict)
    model_profiles: dict[str, ModelProfileSnapshot] = Field(default_factory=dict)
    model_pricing: dict[str, PriceSnapshot] = Field(default_factory=dict)
    triage_model: str
    node_endpoints: list[str] = Field(default_factory=list)
    # The capability document digest of every endpoint the dispatcher
    # has already qualified. An endpoint without one binds its document
    # at dispatch and the specification records no digest for it.
    endpoint_capability_digests: dict[str, str] = Field(default_factory=dict)
    qualification_ids: list[str] = Field(default_factory=list)


class TaskOverrideSet(ControlledModel):
    """The overrides one submission carries for its own admission."""

    classic: dict[str, Any] = Field(default_factory=dict)
    routing: dict[str, str] = Field(default_factory=dict)
    role_registry: dict[str, dict[str, Any]] = Field(default_factory=dict)
    price_overrides: dict[str, PriceSnapshot] = Field(default_factory=dict)
    seed: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _exact_override_amounts(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for name, amount in (value.get("classic") or {}).items():
                if name in ("budget_ceiling_usd", "limits.max_cost") and isinstance(amount, float):
                    raise ValueError("Money overrides require decimal strings, never binary floating point")
            for price in (value.get("price_overrides") or {}).values():
                source = price.get("source") if isinstance(price, dict) else price.source
                if not source or source == "bmas.yaml":
                    raise ValueError("An explicit price override requires its own provenance")
        return value


class ClassicSpecInput(ControlledModel):
    """What one admission submits to the specification compiler."""

    runtime: SpecRuntime = Field(default_factory=SpecRuntime)
    fidelity: str
    effort: str
    deployment: DeploymentSnapshot
    task_overrides: TaskOverrideSet = Field(default_factory=TaskOverrideSet)
    asset_manifest_digest: str | None = None


# ── Profiles ─────────────────────────────────────────────────────────


class ClassicFidelityProfile(ControlledModel):
    """One fidelity profile: coordination semantics and the fields it fixes."""

    profile_id: str
    profile_version: str
    description: str
    values: dict[str, Any]
    fixed_fields: list[str]

    @model_validator(mode="after")
    def _fixed_fields_have_values(self) -> ClassicFidelityProfile:
        missing = [name for name in self.fixed_fields if name not in self.values]
        if missing:
            raise ValueError(f"the profile fixes fields without values: {missing}")
        return self


class ClassicEffortProfile(ControlledModel):
    """One effort preset: resource intensity and procedure strength."""

    profile_id: str
    profile_version: str
    description: str
    values: dict[str, Any]
    # A preset whose values are documented defaults yields to the
    # deployment settings. Every other preset keeps its own values, and
    # only a task override or a cap changes them.
    overridable_by_deployment: bool


# ── Specification sections ───────────────────────────────────────────


class SpecFidelity(ControlledModel):
    profile_id: str
    profile_version: str


class SpecEffort(ControlledModel):
    requested_level: str
    profile_id: str
    profile_version: str
    alias_table_version: str


class SpecInputs(ControlledModel):
    asset_manifest_digest: str | None = None
    deployment_snapshot_digest: str
    task_overrides_digest: str


class SpecTeam(ControlledModel):
    experts_by_tier: dict[str, int]
    required_roles: list[str]
    model_assignment: Literal["capability_round_robin", "seeded_random"]
    independent_model_families: int = Field(ge=1)

    @model_validator(mode="after")
    def _tiers_complete(self) -> SpecTeam:
        missing = [tier for tier in TIERS if tier not in self.experts_by_tier]
        if missing:
            raise ValueError(f"experts_by_tier misses the tiers {missing}")
        if any(count < 0 for count in self.experts_by_tier.values()):
            raise ValueError("experts_by_tier holds a negative count")
        return self


class ModelRef(ControlledModel):
    alias: str
    model_id: str
    model_revision: str
    tokenizer_id: str
    tokenizer_revision: str
    reasoning: str


class SpecModels(ControlledModel):
    """The model every role calls.

    The triage result selects the tier of one task at run time, so the
    roles that follow the tier bind to ``tier_models`` through
    ``role_tier_binding``. The control unit and the vote bind to one
    fixed tier. The expert pool lists the models the generator assigns
    per tier.
    """

    triage: ModelRef
    control_unit: ModelRef
    sole: ModelRef
    verifier: ModelRef | None = None
    tier_models: dict[str, ModelRef]
    expert_pool: dict[str, list[ModelRef]]
    role_tier_binding: dict[str, str]
    temperature: float | None = None

    @model_validator(mode="after")
    def _bindings_name_tiers(self) -> SpecModels:
        missing = [tier for tier in TIERS if tier not in self.tier_models]
        if missing:
            raise ValueError(f"tier_models misses the tiers {missing}")
        unknown = sorted(
            binding for binding in self.role_tier_binding.values()
            if binding != "triage_tier" and binding not in self.tier_models
        )
        if unknown:
            raise ValueError(f"role_tier_binding names unknown tiers {unknown}")
        return self


class SpecProviderCapabilities(ControlledModel):
    snapshot_version: str
    snapshot_digest: str
    seed_support: Literal["unsupported", "best_effort", "applied"]
    session_support: bool
    qualification_ids: list[str]


class SpecEndpoint(ControlledModel):
    endpoint_id: str
    order: int = Field(ge=1)
    adapter_id: str
    adapter_version: str
    capability_record_digest: str | None = None


class SpecFailoverPolicy(ControlledModel):
    policy_id: str
    policy_version: str
    retryable_failures: list[str]
    stateful_session_action: str
    max_endpoint_changes: int = Field(ge=0)


class SpecEndpointSet(ControlledModel):
    endpoint_set_version: str
    endpoints: list[SpecEndpoint]
    failover_policy: SpecFailoverPolicy

    @model_validator(mode="after")
    def _ordered_and_present(self) -> SpecEndpointSet:
        if not self.endpoints:
            raise ValueError("an endpoint set needs at least one endpoint")
        if [endpoint.order for endpoint in self.endpoints] != list(range(1, len(self.endpoints) + 1)):
            raise ValueError("endpoint orders must run from one without gaps")
        return self


class SpecControlPlaneAdapter(ControlledModel):
    adapter_id: str
    adapter_version: str


class SpecRouting(ControlledModel):
    endpoint_sets: dict[str, SpecEndpointSet]
    role_endpoint_sets: dict[str, str]
    control_plane_adapter: SpecControlPlaneAdapter

    @model_validator(mode="after")
    def _roles_name_known_sets(self) -> SpecRouting:
        unknown = sorted(name for name in self.role_endpoint_sets.values() if name not in self.endpoint_sets)
        if unknown:
            raise ValueError(f"role_endpoint_sets names unknown endpoint sets {unknown}")
        return self


class LineageAssignment(ControlledModel):
    lineage_id: str
    lineage_status: Literal["verified", "unverified", "disputed", "unknown"]


class SpecModelLineage(ControlledModel):
    registry_version: str
    registry_digest: str
    assignments: dict[str, LineageAssignment]


class SpecPrompts(ControlledModel):
    registry_version: str
    render_engine_version: str
    static_template_digests: dict[str, str]
    generated_definition_schema_version: str
    generated_definition_asset_digest: str | None = None
    activation_render_receipt_schema_version: str


class PriceSourceStrings(ControlledModel):
    input_per_million: str
    output_per_million: str


class PriceRates(ControlledModel):
    input_per_million: MoneyValue
    output_per_million: MoneyValue


class SpecPrices(ControlledModel):
    table_version: str
    source_amount_strings: dict[str, PriceSourceStrings]
    rates: dict[str, PriceRates]
    provenance: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _rates_match_sources(self) -> SpecPrices:
        if set(self.rates) != set(self.source_amount_strings):
            raise ValueError("every price rate needs its source string and no other")
        return self


class SpecRandomness(ControlledModel):
    seed_policy: Literal["recorded", "applied"]
    task_seed: int | None = None
    roster_seed: int | None = None
    candidate_order_seed: int | None = None
    candidate_order: Literal["board_order", "recorded_random"]


class SpecCoordination(ControlledModel):
    control_strategy: Literal["model_ranked", "deterministic_first"]
    proposal_mode: Literal["shared_board", "blind_independent"]
    round_execution: Literal["sequential", "concurrent"]
    max_parallel_agents: int = Field(ge=1)
    max_rounds: int = Field(ge=1)
    narration: bool


class SpecSalienceWeights(ControlledModel):
    confidence: float = Field(ge=0)
    recency: float = Field(ge=0)
    refs_in: float = Field(ge=0)
    penalty: float = Field(ge=0)
    operator_boost_factor: float = Field(gt=0)


class SpecBoard(ControlledModel):
    view_strategy: Literal["role_bounded", "full_board"]
    view_budget_tokens: int = Field(ge=512)
    max_title_characters: int = Field(ge=1)
    max_entry_body_characters: int = Field(ge=1)
    retain_counterevidence: bool
    salience_weights: SpecSalienceWeights


class SpecCleanerWeights(ControlledModel):
    salience: float = Field(ge=0)
    confidence: float = Field(ge=0)
    recency: float = Field(ge=0)
    size_penalty: float = Field(ge=0)
    minority_claim: float = Field(ge=0)


class SpecCleaner(ControlledModel):
    enabled: bool
    policy_id: str
    policy_version: str
    entry_threshold: int = Field(ge=1)
    token_threshold: int = Field(ge=1)
    retain_recent_rounds: int = Field(ge=0)
    retention_weights: SpecCleanerWeights


class SpecMemory(ControlledModel):
    actor_memory: Literal["none", "host_owned"]
    provider_session_scope: Literal["task", "activation"]
    durable_goals: bool
    causal_retrieval: bool


class SpecVerification(ControlledModel):
    evidence_policy: Literal["optional_sources", "typed_sources", "typed_sources_strict"]
    solution_review: Literal["not_required", "required"]
    independent_verifiers: int = Field(ge=0)
    critical_claims_only: bool
    adversarial_critic: bool
    final_verification: bool


class SpecConsensus(ControlledModel):
    strategy: str
    strategy_version: str
    threshold: float = Field(ge=0, le=1)
    failure_behavior: Literal["continue_work", "use_verified_single_candidate", "fail_closed"]


class SpecLimits(ControlledModel):
    max_cost: MoneyValue
    max_duration_seconds: int = Field(ge=1)
    max_input_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    strict_pricing: bool

    @model_validator(mode="after")
    def _limits_consistent(self) -> SpecLimits:
        if self.max_output_tokens > self.max_input_tokens:
            raise ValueError("max_output_tokens cannot exceed max_input_tokens")
        if self.max_cost.amount_nanos <= 0:
            raise ValueError("max_cost must be positive")
        return self


class SpecRecovery(ControlledModel):
    stall_rounds: int = Field(ge=1)
    max_replans: int = Field(ge=0)
    checkpoint_every_rounds: int = Field(ge=1)
    loop_detection: Literal["standard", "strong"]
    fault_policy: Literal["retry_then_replan", "replan_only"]


class SpecTermination(ControlledModel):
    policy_id: str
    policy_version: str
    require_completion_evidence: bool
    allow_empty_candidate_set: bool
    terminal_reasons: list[str]

    @model_validator(mode="after")
    def _reasons_unique(self) -> SpecTermination:
        if len(set(self.terminal_reasons)) != len(self.terminal_reasons):
            raise ValueError("terminal_reasons repeat a reason")
        if "completed" not in self.terminal_reasons:
            raise ValueError("terminal_reasons must name the success reason")
        return self


class CapAdjustment(ControlledModel):
    field: str
    requested: Any
    effective: Any
    bound: Any
    rule: Literal["minimum", "maximum"]


class SpecDeploymentCaps(ControlledModel):
    policy_version: str
    applied_after_user_overrides: bool
    adjustments: list[CapAdjustment]


class FieldResolution(ControlledModel):
    """Which layer set one effective value and what every layer offered.

    ``offered`` maps a layer to the value it declared. ``not_applied``
    lists the layers whose value did not become effective: a
    fidelity-fixed field refused it, or the effort preset kept its own
    value over the deployment default.
    """

    layer: Layer
    offered: dict[str, Any]
    not_applied: list[str] = Field(default_factory=list)


class ClassicSpecWarning(ControlledModel):
    kind: Literal["rejected_override", "clamped_value", "missing_price", "unverified_lineage", "effort_alias"]
    field: str | None = None
    layer: Layer | None = None
    message: str
    requested: Any = None
    effective: Any = None


class ClassicSpecEstimate(ControlledModel):
    max_in_flight_activations: int = Field(ge=1)
    maximum_in_flight_allowance: MoneyValue = Field(default_factory=lambda: MoneyValue(currency="USD", amount_nanos=0))
    cost_minimum: MoneyValue | None
    cost_maximum: MoneyValue
    latency_minimum_seconds: int = Field(ge=0)
    latency_maximum_seconds: int = Field(ge=1)

    @model_validator(mode="after")
    def _ranges_ordered(self) -> ClassicSpecEstimate:
        if self.cost_minimum is not None and self.cost_minimum.amount_nanos > self.cost_maximum.amount_nanos:
            raise ValueError("the cost range is inverted")
        if self.latency_minimum_seconds > self.latency_maximum_seconds:
            raise ValueError("the latency range is inverted")
        return self


class ClassicSpec(ControlledModel):
    """The complete, immutable specification of one native Classic run."""

    runtime: SpecRuntime
    fidelity: SpecFidelity
    effort: SpecEffort
    inputs: SpecInputs
    team: SpecTeam
    models: SpecModels
    provider_capabilities: SpecProviderCapabilities
    routing: SpecRouting
    model_lineage: SpecModelLineage
    prompts: SpecPrompts
    prices: SpecPrices
    randomness: SpecRandomness
    coordination: SpecCoordination
    board: SpecBoard
    cleaner: SpecCleaner
    memory: SpecMemory
    verification: SpecVerification
    consensus: SpecConsensus
    limits: SpecLimits
    recovery: SpecRecovery
    termination: SpecTermination
    deployment_caps: SpecDeploymentCaps
    resolution: dict[str, FieldResolution]
    warnings: list[ClassicSpecWarning]
    estimate: ClassicSpecEstimate

    @model_validator(mode="after")
    def _sections_consistent(self) -> ClassicSpec:
        if self.fidelity.profile_id == "paper_aligned":
            if self.coordination.round_execution != "sequential":
                raise ValueError("the paper-aligned profile keeps sequential rounds")
            if self.board.view_strategy != "full_board":
                raise ValueError("the paper-aligned profile keeps full board views")
            if self.cleaner.enabled:
                raise ValueError("the paper-aligned profile keeps the cleaner disabled")
            if self.memory.actor_memory != "none":
                raise ValueError("the paper-aligned profile keeps actor memory off")
            if self.verification.independent_verifiers != 0:
                raise ValueError("the paper-aligned profile keeps an empty verifier set")
            if "paper_aligned_context_limit_exceeded" not in self.termination.terminal_reasons:
                raise ValueError("the paper-aligned profile names its context limit reason")
        if self.estimate.max_in_flight_activations > self.coordination.max_parallel_agents:
            raise ValueError("the in-flight estimate cannot exceed the parallel agent limit")
        if self.limits.max_cost != self.estimate.cost_maximum:
            raise ValueError("the cost estimate must end at the cost limit")
        if self.limits.max_duration_seconds != self.estimate.latency_maximum_seconds:
            raise ValueError("the latency estimate must end at the duration limit")
        if self.team.independent_model_families > 1 and not self.model_lineage.assignments:
            raise ValueError("an independence requirement needs lineage assignments")
        if self.verification.independent_verifiers > 0 and self.models.verifier is None:
            raise ValueError("a verifier count needs a verifier model")
        if self.verification.independent_verifiers == 0 and self.models.verifier is not None:
            raise ValueError("an empty verifier set binds no verifier model")
        for name in self.team.required_roles:
            if name not in self.routing.role_endpoint_sets:
                raise ValueError(f"the required role {name!r} has no endpoint set")
        return self


def open_objects(schema: dict[str, Any]) -> list[str]:
    """Return every object in one JSON schema that allows extra properties."""
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node and node.get("additionalProperties", True) is not False:
                found.append(path or "<root>")
            for key, value in node.items():
                walk(value, f"{path}/{key}" if path else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(schema, "")
    return found
