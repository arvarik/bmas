"""Registered Patchboard and Stigmergic coordination runtimes.

Both runtimes use the shared dispatch, lifecycle, and checkpoint services.
Patchboard collects independent patches before one integration turn.
Stigmergic passes one shared artifact through an ordered revision loop.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from dataclasses import dataclass
from typing import Any, ClassVar

from config import AGENT_ENDPOINTS, MODEL_POOLS, MODEL_PRICING
from core.variants import (
    PATCHBOARD_VARIANT,
    STIGMERGIC_VARIANT,
    VariantBenchmarkContract,
    VariantConfigurationError,
    VariantDescriptor,
    VariantExecutionRequest,
    VariantFeatures,
    VariantHost,
    VariantOutcome,
    register_variant,
)
from settings_store import get_store

_MAX_CONTEXT_CHARS = 40_000
_MAX_OUTPUT_CHARS = 40_000
_DEFAULT_CONTRIBUTORS = ("planner", "critic")
_DEFAULT_INTEGRATOR = "decider"


def _activation_id(task_id: str, runtime_id: str, step: str) -> str:
    """Return one stable activation identifier for an idempotent step."""
    return "activation-" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"bmas:{task_id}:{runtime_id}:{step}",
    ).hex


def _text_result(response: dict[str, Any]) -> str:
    """Return one bounded text result or raise the agent error."""
    status = str(response.get("status") or "failed").lower()
    value = response.get("result")
    if status != "completed":
        detail = value or response.get("error") or "The agent did not complete"
        raise RuntimeError(str(detail)[:2000])
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(value, sort_keys=True) if value is not None else ""
    if not text:
        raise RuntimeError("The agent returned an empty result")
    return text[:_MAX_OUTPUT_CHARS]


def _role_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    """Validate one ordered runtime role list."""
    if not isinstance(value, list) or not 1 <= len(value) <= 6:
        raise VariantConfigurationError(
            f"{field_name} must contain between one and six roles"
        )
    roles = tuple(str(item).strip() for item in value)
    if any(not role or len(role) > 100 for role in roles):
        raise VariantConfigurationError(f"{field_name} contains an invalid role")
    if len(set(roles)) != len(roles):
        raise VariantConfigurationError(f"{field_name} must contain unique roles")
    return roles


@dataclass(frozen=True)
class _RuntimePlan:
    roles: tuple[str, ...]
    integrator: str
    rounds: int


class _CollaborativeRuntime:
    """Supply shared configuration, dispatch, and checkpoint behavior."""

    descriptor: ClassVar[VariantDescriptor]
    default_rounds: ClassVar[int] = 1
    max_rounds: ClassVar[int] = 6
    role_field: ClassVar[str]

    @classmethod
    def _settings(cls, requested: dict[str, Any] | None = None) -> dict[str, Any]:
        selected = requested or {}
        unknown = set(selected) - {
            "submission_overrides",
            cls.role_field,
            "integrator_role",
            "rounds",
        }
        if unknown:
            raise VariantConfigurationError(
                f"The {cls.descriptor.id} runtime does not support: "
                f"{', '.join(sorted(unknown))}"
            )
        roles = _role_list(
            selected.get(cls.role_field, list(_DEFAULT_CONTRIBUTORS)),
            field_name=cls.role_field,
        )
        integrator = str(
            selected.get("integrator_role", _DEFAULT_INTEGRATOR)
        ).strip()
        if not integrator or len(integrator) > 100:
            raise VariantConfigurationError("integrator_role is invalid")
        rounds = selected.get("rounds", cls.default_rounds)
        if (
            not isinstance(rounds, int)
            or isinstance(rounds, bool)
            or not 1 <= rounds <= cls.max_rounds
        ):
            allowed = "equal 1" if cls.max_rounds == 1 else f"be from 1 to {cls.max_rounds}"
            raise VariantConfigurationError(
                f"rounds must be an integer and {allowed}"
            )
        return {
            cls.role_field: list(roles),
            "integrator_role": integrator,
            "rounds": rounds,
        }

    @classmethod
    async def _capture(
        cls,
        overrides: dict[str, Any] | None,
        runtime_settings: dict[str, Any],
    ) -> dict[str, Any]:
        store = get_store()
        routing = await store.get_routing()
        registry = await store.get_role_registry()
        if overrides and isinstance(overrides.get("routing"), dict):
            routing.update(overrides["routing"])
        if overrides and isinstance(overrides.get("role_registry"), dict):
            for role_name, role_patch in overrides["role_registry"].items():
                existing = copy.deepcopy(registry.get(role_name, {}))
                existing.update(role_patch)
                registry[role_name] = existing
        required_roles = [
            *runtime_settings[cls.role_field],
            runtime_settings["integrator_role"],
        ]
        unavailable_roles = sorted({
            role
            for role in required_roles
            if not isinstance(registry.get(role), dict)
            or registry[role].get("enabled") is False
            or not registry[role].get("endpoints")
        })
        if unavailable_roles:
            raise VariantConfigurationError(
                "The runtime role registry cannot dispatch: "
                + ", ".join(unavailable_roles)
            )
        return {
            "variant": cls.descriptor.id,
            "variant_contract_version": cls.descriptor.contract_version,
            "configuration_schema_version": (
                cls.descriptor.configuration_schema_version
            ),
            "settings": {
                cls.descriptor.id: copy.deepcopy(runtime_settings),
                "model_pools": copy.deepcopy(MODEL_POOLS),
                "model_pricing": copy.deepcopy(MODEL_PRICING),
                "node_endpoints": sorted(set(AGENT_ENDPOINTS.values())),
            },
            "model_routing": routing,
            "role_registry": registry,
        }

    @classmethod
    async def capture_configuration(
        cls,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Capture one complete default runtime configuration."""
        return await cls._capture(overrides, cls._settings())

    @classmethod
    async def prepare_benchmark_configuration(
        cls,
        requested: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate benchmark choices and capture their effective values."""
        overrides = requested.get("submission_overrides")
        if overrides is not None and not isinstance(overrides, dict):
            raise VariantConfigurationError(
                "submission_overrides must contain an object"
            )
        return await cls._capture(overrides, cls._settings(requested))

    @classmethod
    def configuration_from_metadata(
        cls,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Load one saved configuration after strict version validation."""
        saved = metadata.get("effective_configuration")
        if not isinstance(saved, dict):
            return None
        if saved.get("variant") != cls.descriptor.id:
            raise VariantConfigurationError(
                f"The saved configuration does not belong to {cls.descriptor.id}"
            )
        version = str(saved.get("configuration_schema_version") or "")
        if version != cls.descriptor.configuration_schema_version:
            raise VariantConfigurationError(
                f"Unsupported {cls.descriptor.id} configuration schema version: "
                f"{version or 'missing'}"
            )
        settings = saved.get("settings")
        runtime_settings = settings.get(cls.descriptor.id) if isinstance(settings, dict) else None
        if not isinstance(runtime_settings, dict):
            raise VariantConfigurationError(
                f"The saved {cls.descriptor.id} settings are missing"
            )
        cls._settings(runtime_settings)
        return copy.deepcopy(saved)

    @classmethod
    def _plan(cls, request: VariantExecutionRequest) -> _RuntimePlan:
        effective = request.effective_configuration or {}
        settings = effective.get("settings")
        runtime_settings = settings.get(cls.descriptor.id) if isinstance(settings, dict) else {}
        validated = cls._settings(runtime_settings)
        return _RuntimePlan(
            roles=tuple(validated[cls.role_field]),
            integrator=str(validated["integrator_role"]),
            rounds=int(validated["rounds"]),
        )

    @classmethod
    def _dispatch_values(
        cls,
        request: VariantExecutionRequest,
        role: str,
    ) -> dict[str, Any]:
        effective = request.effective_configuration or {}
        registry = effective.get("role_registry")
        role_record = registry.get(role, {}) if isinstance(registry, dict) else {}
        endpoints = role_record.get("endpoints") if isinstance(role_record, dict) else None
        complexity = getattr(getattr(request, "triage", None), "complexity", None)
        tier = getattr(complexity, "value", "medium")
        routing = effective.get("model_routing")
        selected_model = routing.get(tier) if isinstance(routing, dict) else None
        if not selected_model:
            selected_model = getattr(getattr(request, "triage", None), "litellm_model", None)
        values: dict[str, Any] = {
            "role": role,
            "actor": f"{cls.descriptor.id}.{role}",
            "model": selected_model,
            "profile": role_record.get("profile") if isinstance(role_record, dict) else None,
            "session_id": request.session_id,
        }
        if isinstance(endpoints, list):
            clean = [str(item) for item in endpoints if str(item).strip()]
            if clean:
                values["endpoint"] = clean[0]
                values["endpoints"] = clean
        return values

    @classmethod
    async def _dispatch(
        cls,
        host: VariantHost,
        request: VariantExecutionRequest,
        *,
        role: str,
        step: str,
        persona: str,
        context: dict[str, Any],
        round_no: int,
        phase: str,
    ) -> str:
        await host.check_abort(request.task_id)
        response = await host.dispatch_agent(
            task_id=request.task_id,
            activation_id=_activation_id(
                request.task_id,
                cls.descriptor.id,
                step,
            ),
            description=request.user_task,
            persona=persona,
            context=context,
            round_no=round_no,
            rationale=f"{cls.descriptor.label} step {step}",
            phase=phase,
            **cls._dispatch_values(request, role),
        )
        return _text_result(response)

    @classmethod
    async def _checkpoint(
        cls,
        host: VariantHost,
        request: VariantExecutionRequest,
        value: dict[str, Any],
    ) -> None:
        await host.save_variant_checkpoint(
            request.task_id,
            cls.descriptor.id,
            {
                "schema_version": "1",
                "contract_version": cls.descriptor.contract_version,
                **value,
            },
        )

    @classmethod
    def _restore(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if value is None:
            return {}
        if str(value.get("schema_version") or "") != "1":
            raise VariantConfigurationError(
                f"Unsupported {cls.descriptor.id} checkpoint schema"
            )
        if str(value.get("contract_version") or "") != cls.descriptor.contract_version:
            raise VariantConfigurationError(
                f"Unsupported {cls.descriptor.id} checkpoint contract"
            )
        return value


_WORKFLOW_FEATURES = VariantFeatures(
    events=(
        "complete",
        "error",
        "initial_state",
        "log",
        "phase",
        "subtask",
        "turn_end",
        "turn_start",
    ),
    panels=("mission", "logs", "artifacts"),
    graphs=("turns",),
    controls=("abort",),
    progress=("phase", "effective_actions"),
    result=("answer", "coordination", "steps", "runtime_id"),
)


def _benchmark_contract(
    role_field: str,
    default_rounds: int,
    max_rounds: int,
) -> VariantBenchmarkContract:
    """Build one truthful benchmark configuration schema."""
    return VariantBenchmarkContract(
        configuration_schema={
            "type": "object",
            "properties": {
                "submission_overrides": {"type": "object"},
                role_field: {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": 6,
                    "uniqueItems": True,
                },
                "integrator_role": {"type": "string", "minLength": 1},
                "rounds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": max_rounds,
                    "default": default_rounds,
                },
            },
            "additionalProperties": False,
        },
        seed_strategy="recorded",
    )


class PatchboardVariantRuntime(_CollaborativeRuntime):
    """Collect independent patches and integrate them once."""

    role_field = "contributor_roles"
    default_rounds = 1
    max_rounds = 1
    descriptor = VariantDescriptor(
        id=PATCHBOARD_VARIANT,
        label="Patchboard",
        contract_version="1",
        configuration_schema_version="1",
        supports_recovery=True,
        required_agent_features=(
            "execute",
            "activation_idempotency",
            "task_cancellation",
        ),
        features=_WORKFLOW_FEATURES,
        benchmark=_benchmark_contract("contributor_roles", 1, 1),
    )

    @classmethod
    async def run(
        cls,
        host: VariantHost,
        request: VariantExecutionRequest,
    ) -> VariantOutcome:
        plan = cls._plan(request)
        checkpoint = cls._restore(await host.load_variant_checkpoint(
            request.task_id,
            cls.descriptor.id,
        ))
        patches = checkpoint.get("patches")
        if not isinstance(patches, list):
            await host.publish_phase("collect_patches", 1, request.task_id)
            await host.publish_progress(
                request.task_id,
                request.user_task[:80],
                "running",
                [
                    {
                        "id": f"{request.task_id}-patch-{index}",
                        "label": f"Independent patch from {role}",
                        "status": "running",
                        "agent_role": role,
                        "depends_on": [],
                    }
                    for index, role in enumerate(plan.roles)
                ],
            )
            patches = list(await asyncio.gather(*(
                cls._dispatch(
                    host,
                    request,
                    role=role,
                    step=f"patch:{index}:{role}",
                    persona=(
                        "Create one independent solution patch. State assumptions, "
                        "evidence, risks, and the proposed answer. Do not imitate other roles."
                    ),
                    context={
                        "coordination": "patchboard",
                        "objective": request.user_task,
                        "patch_index": index,
                    },
                    round_no=1,
                    phase="collect_patches",
                )
                for index, role in enumerate(plan.roles)
            )))
            await cls._checkpoint(host, request, {"step": "integrate", "patches": patches})

        answer = checkpoint.get("answer")
        if not isinstance(answer, str) or not answer:
            await host.publish_phase("integrate", 1, request.task_id)
            answer = await cls._dispatch(
                host,
                request,
                role=plan.integrator,
                step=f"integrate:{plan.integrator}",
                persona=(
                    "Integrate the independent patches into one accurate final answer. "
                    "Resolve conflicts explicitly. Return only the complete answer."
                ),
                context={
                    "coordination": "patchboard",
                    "objective": request.user_task,
                    "patches": [str(item)[:_MAX_CONTEXT_CHARS] for item in patches],
                },
                round_no=2,
                phase="integrate",
            )
            await cls._checkpoint(
                host,
                request,
                {"step": "complete", "patches": patches, "answer": answer},
            )
        completed = tuple(
            {
                "id": f"{request.task_id}-patch-{index}",
                "label": f"Independent patch from {role}",
                "status": "completed",
                "agent_role": role,
                "depends_on": [],
            }
            for index, role in enumerate(plan.roles)
        )
        result = {
            "answer": answer,
            "runtime_id": cls.descriptor.id,
            "coordination": "independent_patches_then_integration",
            "steps": len(patches) + 1,
            "contributors": list(plan.roles),
            "integrator": plan.integrator,
        }
        return VariantOutcome(
            variant_id=cls.descriptor.id,
            answer=answer,
            result=result,
            public_result={"task_id": request.task_id, **result},
            completed_subtasks=completed,
        )


class StigmergicVariantRuntime(_CollaborativeRuntime):
    """Revise one shared artifact through ordered agent interactions."""

    role_field = "worker_roles"
    default_rounds = 2
    descriptor = VariantDescriptor(
        id=STIGMERGIC_VARIANT,
        label="Stigmergic workspace",
        contract_version="1",
        configuration_schema_version="1",
        supports_recovery=True,
        required_agent_features=(
            "execute",
            "activation_idempotency",
            "task_cancellation",
        ),
        features=_WORKFLOW_FEATURES,
        benchmark=_benchmark_contract("worker_roles", 2, 6),
    )

    @classmethod
    async def run(
        cls,
        host: VariantHost,
        request: VariantExecutionRequest,
    ) -> VariantOutcome:
        plan = cls._plan(request)
        checkpoint = cls._restore(await host.load_variant_checkpoint(
            request.task_id,
            cls.descriptor.id,
        ))
        artifact = str(checkpoint.get("artifact") or request.user_task)
        completed_steps = int(checkpoint.get("completed_steps") or 0)
        workflow = [
            (round_no, role)
            for round_no in range(1, plan.rounds + 1)
            for role in plan.roles
        ]
        await host.publish_progress(
            request.task_id,
            request.user_task[:80],
            "running",
            [
                {
                    "id": f"{request.task_id}-revision-{index}",
                    "label": f"Round {round_no} revision from {role}",
                    "status": "completed" if index < completed_steps else "pending",
                    "agent_role": role,
                    "depends_on": (
                        [f"{request.task_id}-revision-{index - 1}"] if index else []
                    ),
                }
                for index, (round_no, role) in enumerate(workflow)
            ],
        )
        for index, (round_no, role) in enumerate(workflow):
            if index < completed_steps:
                continue
            await host.publish_phase("revise_artifact", round_no, request.task_id)
            artifact = await cls._dispatch(
                host,
                request,
                role=role,
                step=f"revise:{index}:{round_no}:{role}",
                persona=(
                    "Improve the shared artifact for the objective. Preserve correct work. "
                    "Correct errors, add missing evidence, and return the complete revision."
                ),
                context={
                    "coordination": "stigmergic",
                    "objective": request.user_task,
                    "shared_artifact": artifact[:_MAX_CONTEXT_CHARS],
                    "round": round_no,
                    "revision": index + 1,
                },
                round_no=round_no,
                phase="revise_artifact",
            )
            completed_steps = index + 1
            await cls._checkpoint(
                host,
                request,
                {
                    "step": "revise_artifact",
                    "completed_steps": completed_steps,
                    "artifact": artifact,
                },
            )

        answer = checkpoint.get("answer")
        if not isinstance(answer, str) or not answer:
            await host.publish_phase("finalize_artifact", plan.rounds + 1, request.task_id)
            answer = await cls._dispatch(
                host,
                request,
                role=plan.integrator,
                step=f"finalize:{plan.integrator}",
                persona=(
                    "Audit the shared artifact for correctness and completeness. "
                    "Return the final answer without review notes."
                ),
                context={
                    "coordination": "stigmergic",
                    "objective": request.user_task,
                    "shared_artifact": artifact[:_MAX_CONTEXT_CHARS],
                },
                round_no=plan.rounds + 1,
                phase="finalize_artifact",
            )
            await cls._checkpoint(
                host,
                request,
                {
                    "step": "complete",
                    "completed_steps": completed_steps,
                    "artifact": artifact,
                    "answer": answer,
                },
            )
        completed = tuple(
            {
                "id": f"{request.task_id}-revision-{index}",
                "label": f"Round {round_no} revision from {role}",
                "status": "completed",
                "agent_role": role,
                "depends_on": (
                    [f"{request.task_id}-revision-{index - 1}"] if index else []
                ),
            }
            for index, (round_no, role) in enumerate(workflow)
        )
        result = {
            "answer": answer,
            "runtime_id": cls.descriptor.id,
            "coordination": "ordered_shared_artifact_revisions",
            "steps": completed_steps + 1,
            "rounds": plan.rounds,
            "workers": list(plan.roles),
            "integrator": plan.integrator,
        }
        return VariantOutcome(
            variant_id=cls.descriptor.id,
            answer=answer,
            result=result,
            public_result={"task_id": request.task_id, **result},
            completed_subtasks=completed,
        )


register_variant(PATCHBOARD_VARIANT, PatchboardVariantRuntime)
register_variant(STIGMERGIC_VARIANT, StigmergicVariantRuntime)
