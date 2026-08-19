"""Runtime contracts and registry for coordination variants.

The orchestrator owns the durable task lifecycle. A registered variant owns
its coordination loop and returns one stable task result.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

logger = logging.getLogger("bmas.variants")

CLASSIC_VARIANT = "classic"
LEGACY_CLASSIC_VARIANT = "traditional"
VARIANT_API_VERSION = "1"


class UnknownVariantError(ValueError):
    """The requested coordination variant has no registered runtime."""


class VariantConfigurationError(ValueError):
    """A saved task configuration is incompatible with this runtime."""


@dataclass(frozen=True)
class VariantFeatures:
    """List the public interface features that one variant supports."""

    events: tuple[str, ...] = ()
    panels: tuple[str, ...] = ()
    graphs: tuple[str, ...] = ()
    controls: tuple[str, ...] = ()
    progress: tuple[str, ...] = ()
    result: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        """Return the JSON response shape for this feature set."""
        return {
            "events": list(self.events),
            "panels": list(self.panels),
            "graphs": list(self.graphs),
            "controls": list(self.controls),
            "progress": list(self.progress),
            "result": list(self.result),
        }


@dataclass(frozen=True)
class VariantBenchmarkContract:
    """Describe how one runtime participates in repeatable benchmarks."""

    supported: bool = True
    configuration_schema: dict[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "properties": {"submission_overrides": {"type": "object"}},
        "additionalProperties": False,
    })
    seed_strategy: str = "recorded"
    supports_repetitions: bool = True
    required_snapshot_fields: tuple[str, ...] = (
        "runtime_id",
        "runtime_configuration",
        "random_seed",
    )

    def to_dict(self) -> dict[str, Any]:
        """Return the public benchmark contract."""
        return {
            "supported": self.supported,
            "configuration_schema": self.configuration_schema,
            "seed_strategy": self.seed_strategy,
            "supports_repetitions": self.supports_repetitions,
            "required_snapshot_fields": list(self.required_snapshot_fields),
        }


@dataclass(frozen=True)
class VariantDescriptor:
    """Describe one registered coordination runtime."""

    id: str
    label: str
    contract_version: str
    aliases: tuple[str, ...] = ()
    features: VariantFeatures = field(default_factory=VariantFeatures)
    configuration_schema_version: str = "1"
    supports_recovery: bool = False
    required_agent_features: tuple[str, ...] = ()
    benchmark: VariantBenchmarkContract = field(default_factory=VariantBenchmarkContract)

    def to_dict(self) -> dict[str, Any]:
        """Return the authoritative public capability record."""
        return {
            "id": self.id,
            "label": self.label,
            "available": True,
            "contract_version": self.contract_version,
            "aliases": list(self.aliases),
            "features": self.features.to_dict(),
            "configuration_schema_version": self.configuration_schema_version,
            "supports_recovery": self.supports_recovery,
            "required_agent_features": list(self.required_agent_features),
            "benchmark": self.benchmark.to_dict(),
        }


@dataclass(frozen=True)
class VariantExecutionRequest:
    """Provide immutable input to one coordination runtime."""

    task_id: str
    session_id: str
    user_task: str
    triage: Any
    overrides: dict[str, Any] | None = None
    resume: bool = False
    effective_configuration: dict[str, Any] | None = None


@dataclass(frozen=True)
class VariantOutcome:
    """Return one coordination result to the shared terminal lifecycle."""

    variant_id: str
    answer: str
    result: dict[str, Any]
    public_result: dict[str, Any]
    cost_usd: float = 0.0
    completed_subtasks: tuple[dict[str, Any], ...] = ()


@runtime_checkable
class VariantHost(Protocol):
    """Expose shared daemon services to coordination runtimes."""

    async def publish_phase(
        self, phase: str, iteration: int, task_id: str,
    ) -> None:
        """Publish and persist one task phase."""
        ...

    async def check_abort(self, task_id: str) -> None:
        """Raise an error when a task must stop."""
        ...

    async def log_event(
        self,
        node_id: str,
        message: str,
        task_id: str,
        **kwargs: Any,
    ) -> None:
        """Write one structured task log."""
        ...

    async def dispatch_agent(
        self,
        *,
        task_id: str,
        activation_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Dispatch one activation with a stable idempotency identity."""
        ...

    async def publish_progress(
        self,
        task_id: str,
        label: str,
        status: str,
        items: list[dict[str, Any]],
    ) -> None:
        """Publish variant-defined progress items for one task."""
        ...

    def task_lease_token(self, task_id: str) -> str | None:
        """Return the current fenced lease token for a task."""
        ...


@runtime_checkable
class CoordinationVariant(Protocol):
    """Define the complete outer interface for a coordination runtime."""

    descriptor: ClassVar[VariantDescriptor]

    @classmethod
    async def capture_configuration(
        cls, overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Capture the complete effective configuration for a new task."""
        ...

    @classmethod
    def configuration_from_metadata(
        cls, metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Load and migrate a saved configuration from task metadata."""
        ...

    @classmethod
    async def run(
        cls, host: VariantHost, request: VariantExecutionRequest,
    ) -> VariantOutcome:
        """Start or resume one task and return its coordination outcome."""
        ...


SEAMS_CHECKLIST: list[str] = [
    (
        "1. The daemon calls the registered CoordinationVariant runtime. "
        "The daemon does not select actors or define a coordination loop."
    ),
    (
        "2. The event log stores variant-neutral envelopes. The saved runtime "
        "and contract version define each event payload."
    ),
    (
        "3. Actor and author identifiers remain opaque strings in every "
        "storage, transport, and user interface contract."
    ),
    (
        "4. Write authorization uses capabilities and resources. It does not "
        "depend on a fixed role name."
    ),
    (
        "5. Each variant registers its state projection and derived-field "
        "hooks behind a shared commit boundary."
    ),
    (
        "6. Dispatch can support push execution and durable pull claims "
        "without changing the task lifecycle."
    ),
    (
        "7. Each variant owns coordination termination. The daemon owns final "
        "task persistence and delivery."
    ),
    (
        "8. Mission Control reads variants and interface features from the "
        "daemon capabilities endpoint."
    ),
]


def verify_seams_checklist() -> list[str]:
    """Return a copy of the coordination seam checklist."""
    return list(SEAMS_CHECKLIST)


_VARIANTS: dict[str, type[CoordinationVariant]] = {}
_ALIASES: dict[str, str] = {}
_BUILTINS_LOADED = False


def register_variant(
    name: str,
    cls: type[CoordinationVariant],
    *,
    aliases: tuple[str, ...] = (),
) -> None:
    """Register one runtime under a canonical identifier and its aliases."""
    if not isinstance(cls, type):
        raise TypeError(f"register_variant expects a class, got {type(cls)}")
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("A variant identifier cannot be empty")
    descriptor = getattr(cls, "descriptor", None)
    if not isinstance(descriptor, VariantDescriptor):
        raise TypeError("A variant runtime must define a VariantDescriptor")
    if descriptor.id != normalized:
        raise ValueError(
            "The variant descriptor identifier must match its registration"
        )
    for method_name in (
        "capture_configuration",
        "configuration_from_metadata",
        "run",
    ):
        if not callable(getattr(cls, method_name, None)):
            raise TypeError(
                f"A variant runtime must define {method_name}()"
            )
    previous = _VARIANTS.get(normalized)
    if previous is not None and previous is not cls:
        logger.warning(
            "Variant '%s' is being re-registered from %s to %s",
            normalized,
            previous.__name__,
            cls.__name__,
        )
    _VARIANTS[normalized] = cls
    _ALIASES[normalized] = normalized
    for alias in aliases:
        alias_id = alias.strip().lower()
        if not alias_id:
            raise ValueError("A variant alias cannot be empty")
        owner = _ALIASES.get(alias_id)
        if owner is not None and owner != normalized:
            raise ValueError(
                f"Variant alias '{alias_id}' is already owned by '{owner}'"
            )
        _ALIASES[alias_id] = normalized


def load_builtin_variants() -> None:
    """Load the built-in runtimes once."""
    global _BUILTINS_LOADED
    module = importlib.import_module("core.variants.classic")
    if CLASSIC_VARIANT not in _VARIANTS:
        runtime = module.ClassicVariantRuntime
        register_variant(
            CLASSIC_VARIANT,
            runtime,
            aliases=(LEGACY_CLASSIC_VARIANT,),
        )
    _BUILTINS_LOADED = True


def canonical_variant_id(name: str) -> str:
    """Return the registered canonical identifier or raise an error."""
    load_builtin_variants()
    normalized = str(name or "").strip().lower()
    canonical = _ALIASES.get(normalized)
    if canonical is None or canonical not in _VARIANTS:
        raise UnknownVariantError(f"Unavailable coordination variant: {name!r}")
    return canonical


def get_variant_class(name: str) -> type[CoordinationVariant] | None:
    """Return the registered runtime for a canonical identifier or alias."""
    try:
        canonical = canonical_variant_id(name)
    except UnknownVariantError:
        return None
    return _VARIANTS[canonical]


def require_variant_class(name: str) -> type[CoordinationVariant]:
    """Return one runtime or raise an explicit fail-closed error."""
    canonical = canonical_variant_id(name)
    return _VARIANTS[canonical]


def available_variants() -> list[str]:
    """Return all registered canonical identifiers in stable order."""
    load_builtin_variants()
    return sorted(_VARIANTS)


def variant_capabilities() -> dict[str, Any]:
    """Return the authoritative coordination capability document."""
    load_builtin_variants()
    descriptors = [
        _VARIANTS[name].descriptor.to_dict()
        for name in sorted(_VARIANTS)
    ]
    return {"api_version": VARIANT_API_VERSION, "variants": descriptors}
