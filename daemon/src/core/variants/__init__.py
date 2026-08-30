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
PATCHBOARD_VARIANT = "patchboard"
STIGMERGIC_VARIANT = "stigmergic"
VARIANT_API_VERSION = "1"

# The registry availability states from the compatibility matrix.
# Production run admission accepts only a qualified pair.
RUNTIME_AVAILABILITY_STATES = ("planned", "test_only", "qualified", "retired")
QUALIFIED_AVAILABILITY = "qualified"


class UnknownVariantError(ValueError):
    """The requested coordination variant has no registered runtime."""


class UnsupportedContractError(UnknownVariantError):
    """The runtime family exists, but the exact contract version does not."""


class RuntimeNotAdmissibleError(UnknownVariantError):
    """The exact runtime pair exists but is not qualified for admission."""


class MissingCheckpointReaderError(UnknownVariantError):
    """The exact runtime pair has no checkpoint reader for a resume."""


class VariantConfigurationError(ValueError):
    """A saved task configuration is incompatible with this runtime."""


@dataclass(frozen=True, order=True)
class RuntimeKey:
    """Identify one executable runtime pair.

    The pair holds the runtime family identifier and the exact stored
    contract version. The registry resolves only the complete pair.
    No caller infers one field from the other, and no lookup falls
    back to a newer contract version.
    """

    runtime_id: str
    runtime_contract_version: str

    def __post_init__(self) -> None:
        normalized = str(self.runtime_id or "").strip().lower()
        if not normalized:
            raise ValueError("A runtime identifier cannot be empty")
        object.__setattr__(self, "runtime_id", normalized)
        exact = str(self.runtime_contract_version or "")
        if not exact or exact != exact.strip():
            raise ValueError(
                "A runtime contract version must be one exact non-empty string"
            )
        object.__setattr__(self, "runtime_contract_version", exact)

    def to_dict(self) -> dict[str, str]:
        """Return the JSON shape of this pair."""
        return {
            "runtime_id": self.runtime_id,
            "runtime_contract_version": self.runtime_contract_version,
        }


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
    effort_profiles: Any = None

    def to_dict(self) -> dict[str, Any]:
        """Return the authoritative public capability record."""
        record = {
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
        if self.effort_profiles:
            record["effort_profiles"] = dict(self.effort_profiles)
        return record


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

    async def load_variant_checkpoint(
        self,
        task_id: str,
        variant_id: str,
    ) -> dict[str, Any] | None:
        """Load one runtime checkpoint from durable task metadata."""
        ...

    async def save_variant_checkpoint(
        self,
        task_id: str,
        variant_id: str,
        checkpoint: dict[str, Any],
    ) -> None:
        """Save one runtime checkpoint behind the current task lease."""
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


# The registry key is the complete RuntimeKey pair. An alias maps to
# one complete pair, never to a bare runtime name.
_VARIANTS: dict[RuntimeKey, type[CoordinationVariant]] = {}
_ALIASES: dict[str, RuntimeKey] = {}
_AVAILABILITY: dict[RuntimeKey, str] = {}


def register_variant(
    name: str,
    cls: type[CoordinationVariant],
    *,
    aliases: tuple[str, ...] = (),
    availability: str = QUALIFIED_AVAILABILITY,
    bind_aliases: bool = True,
) -> RuntimeKey:
    """Register one runtime under its exact (identifier, contract) pair.

    The registration key comes from the runtime descriptor. Several
    contract versions of one runtime family can register together.
    When ``bind_aliases`` is true, the bare identifier and every alias
    bind to this complete pair. A bound alias never moves to another
    pair silently; a conflicting bind raises an error.
    """
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
    if availability not in RUNTIME_AVAILABILITY_STATES:
        raise ValueError(
            f"Unknown runtime availability state: {availability!r}"
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
    key = RuntimeKey(descriptor.id, descriptor.contract_version)
    previous = _VARIANTS.get(key)
    if previous is not None and previous is not cls:
        logger.warning(
            "Runtime pair %s is being re-registered from %s to %s",
            key,
            previous.__name__,
            cls.__name__,
        )
    _VARIANTS[key] = cls
    _AVAILABILITY[key] = availability
    if bind_aliases:
        for alias in (normalized, *aliases):
            alias_id = alias.strip().lower()
            if not alias_id:
                raise ValueError("A variant alias cannot be empty")
            owner = _ALIASES.get(alias_id)
            if owner is not None and owner != key:
                raise ValueError(
                    f"Variant alias '{alias_id}' is already bound to the "
                    f"complete pair {owner}"
                )
            _ALIASES[alias_id] = key
    return key


def load_builtin_variants() -> None:
    """Load and register the built-in runtimes idempotently."""
    module = importlib.import_module("core.variants.classic")
    register_variant(
        CLASSIC_VARIANT,
        module.ClassicVariantRuntime,
        aliases=(LEGACY_CLASSIC_VARIANT,),
    )
    collaborative = importlib.import_module("core.variants.collaborative")
    for runtime_id, runtime in (
        (PATCHBOARD_VARIANT, collaborative.PatchboardVariantRuntime),
        (STIGMERGIC_VARIANT, collaborative.StigmergicVariantRuntime),
    ):
        register_variant(runtime_id, runtime)


def resolve_runtime_key(name: str) -> RuntimeKey:
    """Resolve one identifier or alias to its bound complete pair."""
    load_builtin_variants()
    normalized = str(name or "").strip().lower()
    key = _ALIASES.get(normalized)
    if key is None or key not in _VARIANTS:
        raise UnknownVariantError(f"Unavailable coordination variant: {name!r}")
    return key


def require_runtime(key: RuntimeKey) -> type[CoordinationVariant]:
    """Return the runtime for one exact pair or fail closed.

    An unknown runtime family raises ``UnknownVariantError``. A known
    family with an unregistered contract version raises
    ``UnsupportedContractError``. No path selects another contract
    version.
    """
    load_builtin_variants()
    cls = _VARIANTS.get(key)
    if cls is not None:
        return cls
    if any(other.runtime_id == key.runtime_id for other in _VARIANTS):
        raise UnsupportedContractError(
            f"Runtime '{key.runtime_id}' has no registered contract "
            f"version {key.runtime_contract_version!r}"
        )
    raise UnknownVariantError(
        f"Unavailable coordination variant: {key.runtime_id!r}"
    )


def runtime_availability(key: RuntimeKey) -> str:
    """Return the registered availability state for one exact pair."""
    require_runtime(key)
    return _AVAILABILITY[key]


def require_admissible_runtime(key: RuntimeKey) -> type[CoordinationVariant]:
    """Return the runtime for one exact qualified pair or fail closed."""
    cls = require_runtime(key)
    availability = _AVAILABILITY[key]
    if availability != QUALIFIED_AVAILABILITY:
        raise RuntimeNotAdmissibleError(
            f"Runtime pair {key} is {availability}; production admission "
            "accepts only a qualified pair"
        )
    return cls


def require_checkpoint_reader(key: RuntimeKey) -> type[CoordinationVariant]:
    """Return the runtime for one exact pair that can read checkpoints."""
    cls = require_runtime(key)
    if not cls.descriptor.supports_recovery:
        raise MissingCheckpointReaderError(
            f"Runtime pair {key} has no checkpoint reader"
        )
    return cls


def registered_runtime_keys() -> list[RuntimeKey]:
    """Return every registered complete pair in stable order."""
    load_builtin_variants()
    return sorted(_VARIANTS)


def capability_record(key: RuntimeKey) -> dict[str, Any]:
    """Return the capability record for one exact pair."""
    return require_runtime(key).descriptor.to_dict()


def canonical_variant_id(name: str) -> str:
    """Return the registered canonical identifier or raise an error."""
    return resolve_runtime_key(name).runtime_id


def get_variant_class(name: str) -> type[CoordinationVariant] | None:
    """Return the registered runtime for a canonical identifier or alias."""
    try:
        return _VARIANTS[resolve_runtime_key(name)]
    except UnknownVariantError:
        return None


def require_variant_class(name: str) -> type[CoordinationVariant]:
    """Return one runtime or raise an explicit fail-closed error."""
    return _VARIANTS[resolve_runtime_key(name)]


def available_variants() -> list[str]:
    """Return all registered canonical identifiers in stable order."""
    load_builtin_variants()
    return sorted({key.runtime_id for key in _VARIANTS})


def variant_capabilities() -> dict[str, Any]:
    """Return the authoritative coordination capability document.

    The document publishes one record per qualified pair. A planned,
    test-only, or retired pair stays out of the document, so a client
    never sees it as a runnable choice.
    """
    load_builtin_variants()
    descriptors = [
        _VARIANTS[key].descriptor.to_dict()
        for key in sorted(_VARIANTS)
        if _AVAILABILITY[key] == QUALIFIED_AVAILABILITY
    ]
    return {"api_version": VARIANT_API_VERSION, "variants": descriptors}
