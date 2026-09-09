"""Typed schema for bMAS YAML configuration files."""

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    """Reject unknown configuration fields."""

    model_config = ConfigDict(extra="forbid")


class ProjectConfig(StrictModel):
    name: str = Field(min_length=1)
    description: str | None = None


class PortConfig(StrictModel):
    redis: int = Field(ge=1, le=65535)
    litellm: int = Field(ge=1, le=65535)
    daemon: int = Field(ge=1, le=65535)
    dashboard: int = Field(default=9321, ge=1, le=65535)
    triage: int = Field(default=8001, ge=1, le=65535)


class ControlPlaneConfig(StrictModel):
    host: str = Field(min_length=1)
    ports: PortConfig


class InferenceConfig(StrictModel):
    host: str = Field(min_length=1)
    port: int = Field(default=8080, ge=1, le=65535)
    model: str = Field(min_length=1)
    max_tokens: int | None = Field(default=None, ge=1)


class NodeConfig(StrictModel):
    name: str = Field(min_length=1)
    host: str = Field(min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)
    role: str = Field(min_length=1)
    color: str | None = None
    dashboard_port: int | None = Field(default=None, ge=1, le=65535)
    inference: InferenceConfig | None = None


class TriageConfig(StrictModel):
    enabled: bool = True
    backend: Literal["cloud", "local", "gemini"] = "cloud"
    model: str = "starter-model"
    local_model: str = "Qwen/Qwen3-1.7B"
    gpu_memory_utilization: float = Field(default=0.35, gt=0, le=1)
    max_model_len: int = Field(default=8192, ge=1)
    default_complexity: Literal["simple", "light", "medium", "complex"] = "medium"


class PricingConfig(StrictModel):
    input_cost_per_token: float | str
    output_cost_per_token: float | str
    source: str | None = None

    @field_validator("input_cost_per_token", "output_cost_per_token")
    @classmethod
    def _nonnegative_price(cls, value: float | str) -> float | str:
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("A price requires a decimal amount") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError("A price must be finite and nonnegative")
        return value


class ModelConfig(StrictModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    api_base: str | None = None
    max_tokens: int = Field(default=4096, ge=1)
    pricing: PricingConfig | None = None
    # How control-plane calls ask a reasoning model to think:
    # provider_default lets a model that reasons by default run at low
    # effort for structured replies, a level forces that effort, and
    # off never sends an effort.
    reasoning: Literal[
        "provider_default", "off", "minimal", "low", "medium", "high",
    ] | None = None


class RoutingConfig(StrictModel):
    simple: str = Field(min_length=1)
    light: str = Field(min_length=1)
    medium: str = Field(min_length=1)
    complex: str = Field(min_length=1)


class CleanerWeights(StrictModel):
    salience: float = 2.0
    confidence: float = 1.0
    recency: float = 0.1
    size_penalty: float = 0.01


# The consensus strategies the classic runtime implements today. The
# legacy value ``auto`` names the token-similarity strategy through a
# recorded alias. ``embedding`` and ``judge`` stay unregistered until the
# consensus registry ships, so the schema rejects them.
CONSENSUS_STRATEGIES: tuple[str, ...] = ("token_similarity", "exact")
CONSENSUS_STRATEGY_ALIASES: dict[str, str] = {"auto": "token_similarity"}
DEFAULT_CONSENSUS_STRATEGY: Literal["token_similarity"] = "token_similarity"

# The classic roles a registry entry can name. A custom role key needs
# a complete role specification, which does not exist yet, so the
# schema rejects an unknown key instead of storing a role that never
# becomes an actor.
CLASSIC_ROLES: tuple[str, ...] = (
    "planner", "expert", "critic", "conflict_resolver", "cleaner", "decider", "universal",
)
# The roles every classic task needs: the planner opens the first round
# and every replan, the critic reviews the answer, and the decider posts
# it. A registry that disables one of them stops every task.
REQUIRED_CLASSIC_ROLES: tuple[str, ...] = ("planner", "critic", "decider")


def resolve_consensus_strategy(value: object) -> str:
    """Return the canonical strategy name for one configured value.

    The alias ``auto`` resolves to ``token_similarity``. An unregistered
    strategy raises ``ValueError``.
    """
    name = str(value).strip()
    name = CONSENSUS_STRATEGY_ALIASES.get(name, name)
    if name not in CONSENSUS_STRATEGIES:
        raise ValueError(
            f"Unsupported consensus strategy {value!r}. "
            f"Supported strategies: {', '.join(CONSENSUS_STRATEGIES)}."
        )
    return name


def validate_role_registry_keys(registry: dict[str, object]) -> None:
    """Reject a role key outside the classic role vocabulary."""
    unknown = sorted(str(key) for key in registry if str(key) not in CLASSIC_ROLES)
    if unknown:
        raise ValueError(
            f"Unknown role registry key(s): {', '.join(unknown)}. "
            f"Known roles: {', '.join(CLASSIC_ROLES)}."
        )


class ClassicConfig(StrictModel):
    max_rounds: int = Field(default=4, ge=1)
    max_duration_s: int = Field(default=1800, ge=1)
    budget_ceiling_usd: float = Field(default=0.50, gt=0)
    max_concurrent_activations: int = Field(default=3, ge=1)
    experts_per_tier: dict[str, int] = Field(
        default_factory=lambda: {"simple": 0, "light": 1, "medium": 2, "complex": 4}
    )
    cleaner_entry_threshold: int = Field(default=12, ge=1)
    cleaner_token_threshold: int = Field(default=8000, ge=1)
    cleaner_retention_weights: CleanerWeights = Field(default_factory=CleanerWeights)
    stall_rounds: int = Field(default=2, ge=1)
    max_replans: int = Field(default=2, ge=0)
    cu_mode: Literal["llm", "heuristic_first"] = "llm"
    coordinator_narration: bool = False
    sole_similarity: Literal["token_similarity", "exact"] = DEFAULT_CONSENSUS_STRATEGY
    grace_verification: bool = True
    actor_context: Literal["chained", "fresh"] = "chained"
    require_evidence: bool = False

    @field_validator("sole_similarity", mode="before")
    @classmethod
    def _resolve_strategy(cls, value: object) -> object:
        """Rewrite the ``auto`` alias before the literal check runs."""
        if isinstance(value, str):
            return CONSENSUS_STRATEGY_ALIASES.get(value.strip(), value)
        return value


class RoleConfig(StrictModel):
    enabled: bool = True
    preferred_host: str | None = None
    profile: str = Field(min_length=1)
    dispatch_port: int = Field(default=8000, ge=1, le=65535)


class SalienceWeights(StrictModel):
    confidence: float = 0.4
    recency: float = 0.2
    refs_in: float = 0.3
    penalty: float = 0.3


class BoardConfig(StrictModel):
    max_entry_chars: int = Field(default=8000, ge=1)
    max_title_len: int = Field(default=200, ge=1)
    salience_weights: SalienceWeights = Field(default_factory=SalienceWeights)


class CoordinationConfig(StrictModel):
    variant: Literal["classic", "traditional"] = "classic"
    blackboard_v2: bool | None = Field(
        default=None,
        deprecated=True,
        description="Deprecated compatibility field. The durable board is always active.",
    )
    view_budget_tokens: int = Field(default=12000, ge=1)
    round_execution: Literal["concurrent", "sequential"] = "concurrent"
    admit_test_only_runtimes: bool = Field(
        default=False,
        description=(
            "Admit a test-only runtime pair when a submission names its exact "
            "contract version. Only a test deployment sets this to true."
        ),
    )
    classic: ClassicConfig | None = None
    traditional: ClassicConfig | None = None
    role_registry: dict[str, RoleConfig] = Field(default_factory=dict)
    board: BoardConfig = Field(default_factory=BoardConfig)

    @field_validator("role_registry", mode="before")
    @classmethod
    def _known_roles_only(cls, value: object) -> object:
        if isinstance(value, dict):
            validate_role_registry_keys(value)
        return value


class FoundationGatesConfig(StrictModel):
    """Gates for the planned shared Foundation writers.

    Every gate stays disabled by default. No current runtime consults a
    gate, so the default deployment keeps existing behavior unchanged.
    """

    runtime_registry: bool = False
    run_context: bool = False
    runtime_unit_of_work: bool = False
    activation_ledger: bool = False
    effect_ledger: bool = False
    budget_reservations: bool = False
    trace_envelope: bool = False
    evidence_index: bool = False
    goal_index: bool = False


class StorageConfig(StrictModel):
    enabled: bool = False
    user_media_dir: str = "/data/uploads"
    artifacts_dir: str = "/data/output"
    max_upload_mb: int = Field(default=50, ge=1)
    max_task_output_mb: int = Field(default=500, ge=1)
    allowed_upload_types: list[str] = Field(
        default_factory=lambda: ["pdf", "txt", "md", "csv", "json", "png", "jpg", "docx"]
    )
    pdf_extraction: Literal["pymupdf", "pypdf", "off"] = "pymupdf"
    extraction_max_chars: int = Field(default=60000, ge=1)


class MonitoringConfig(StrictModel):
    beszel_hub: str | None = None


class ModelPools(StrictModel):
    simple: list[str] | None = Field(default=None, min_length=1)
    light: list[str] | None = Field(default=None, min_length=1)
    medium: list[str] | None = Field(default=None, min_length=1)
    complex: list[str] | None = Field(default=None, min_length=1)


class BmasConfig(StrictModel):
    project: ProjectConfig
    control_plane: ControlPlaneConfig
    nodes: list[NodeConfig] = Field(min_length=1)
    triage: TriageConfig = Field(default_factory=TriageConfig)
    models: dict[str, ModelConfig]
    model_pools: ModelPools | None = None
    routing: RoutingConfig
    coordination: CoordinationConfig = Field(default_factory=CoordinationConfig)
    foundation_gates: FoundationGatesConfig = Field(default_factory=FoundationGatesConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    monitoring: MonitoringConfig | None = None


def validate_config_document(value: object) -> BmasConfig:
    """Validate one parsed YAML document."""
    return BmasConfig.model_validate(value)
