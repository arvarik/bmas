"""Typed schema for bMAS YAML configuration files."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    input_cost_per_token: float = Field(ge=0)
    output_cost_per_token: float = Field(ge=0)
    source: str | None = None


class ModelConfig(StrictModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    api_base: str | None = None
    max_tokens: int = Field(default=4096, ge=1)
    pricing: PricingConfig | None = None


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
    sole_similarity: Literal["auto", "exact", "embedding", "judge"] = "auto"
    grace_verification: bool = True
    actor_context: Literal["chained", "fresh"] = "chained"
    require_evidence: bool = False


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
    classic: ClassicConfig | None = None
    traditional: ClassicConfig | None = None
    role_registry: dict[str, RoleConfig] = Field(default_factory=dict)
    board: BoardConfig = Field(default_factory=BoardConfig)


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
    storage: StorageConfig = Field(default_factory=StorageConfig)
    monitoring: MonitoringConfig | None = None


def validate_config_document(value: object) -> BmasConfig:
    """Validate one parsed YAML document."""
    return BmasConfig.model_validate(value)
