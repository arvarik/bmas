"""The executable deterministic reference runtime.

The reference runtime is the executable half of the conformance kit.
It runs inside the daemon with no agent and no provider. Every step
derives one digest from the previous digest, so two executions with
equal input and an equal seed produce equal output, and a different
seed produces different output. The runtime checks the abort signal
before every step, saves a checkpoint after every step, and resumes
from the saved checkpoint after a restart. It registers as one
admissible runtime pair and stays out of the public capability
document, so a client never offers it as a product choice.
"""

from __future__ import annotations

from typing import Any

from core.digest_profile import digest_hex
from core.variants import (
    REFERENCE_VARIANT,
    VariantBenchmarkContract,
    VariantConfigurationError,
    VariantDescriptor,
    VariantExecutionRequest,
    VariantFeatures,
    VariantHost,
    VariantOutcome,
)

REFERENCE_CONTRACT_VERSION = "1"
CONFIGURATION_SCHEMA_VERSION = "1"
DIGEST_DOMAIN = "reference-runtime"
DEFAULT_STEPS = 3
MAX_STEPS = 64
STEP_PHASE = "reference_step"
_CONFIGURATION_KEYS = frozenset({"steps", "seed", "answer"})


def build_configuration(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate one configuration and return its complete effective form."""
    values = {key: value for key, value in dict(overrides or {}).items() if key != "schema_version"}
    unknown = sorted(set(values) - _CONFIGURATION_KEYS)
    if unknown:
        raise VariantConfigurationError(
            f"The reference runtime accepts no configuration key {unknown}"
        )
    steps = values.get("steps", DEFAULT_STEPS)
    if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= MAX_STEPS:
        raise VariantConfigurationError(
            f"steps must be an integer between 1 and {MAX_STEPS}"
        )
    seed = values.get("seed", 0)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise VariantConfigurationError("seed must be an integer")
    answer = values.get("answer")
    if answer is not None and not isinstance(answer, str):
        raise VariantConfigurationError("answer must be a string when set")
    return {
        "schema_version": CONFIGURATION_SCHEMA_VERSION,
        "steps": steps,
        "seed": seed,
        "answer": answer,
    }


def initial_digest(user_task: str, seed: int) -> str:
    """Derive the step-zero digest from the task text and the applied seed."""
    return digest_hex(DIGEST_DOMAIN, {"user_task": user_task, "seed": seed, "step": 0})


def step_digest(previous: str, step: int) -> str:
    """Derive the digest after one step from the previous digest."""
    return digest_hex(DIGEST_DOMAIN, {"previous": previous, "step": step})


class ReferenceVariantRuntime:
    """Run the deterministic reference steps against the shared host."""

    descriptor = VariantDescriptor(
        id=REFERENCE_VARIANT,
        label="Deterministic reference",
        contract_version=REFERENCE_CONTRACT_VERSION,
        configuration_schema_version=CONFIGURATION_SCHEMA_VERSION,
        supports_recovery=True,
        listed=False,
        features=VariantFeatures(
            events=("initial_state", STEP_PHASE, "final_result"),
            panels=("trace", "assets", "costs", "final_result"),
            progress=("phase", "step"),
            result=("answer", "digest", "steps", "seed", "resumed_from_step"),
        ),
        benchmark=VariantBenchmarkContract(
            configuration_schema={
                "type": "object",
                "properties": {
                    "steps": {"type": "integer", "minimum": 1, "maximum": MAX_STEPS},
                    "seed": {"type": "integer"},
                    "answer": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
        ),
    )

    @classmethod
    async def capture_configuration(
        cls, overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_configuration(overrides)

    @classmethod
    def configuration_from_metadata(
        cls, metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        value = metadata.get("effective_configuration")
        if not isinstance(value, dict):
            return None
        if str(value.get("schema_version", "")) != CONFIGURATION_SCHEMA_VERSION:
            return None
        return build_configuration(value)

    @classmethod
    async def run(
        cls, host: VariantHost, request: VariantExecutionRequest,
    ) -> VariantOutcome:
        configuration = (
            build_configuration(request.effective_configuration)
            if isinstance(request.effective_configuration, dict)
            else build_configuration(request.overrides)
        )
        steps = int(configuration["steps"])
        seed = int(configuration["seed"])
        checkpoint = None
        if request.resume:
            checkpoint = await host.load_variant_checkpoint(request.task_id, REFERENCE_VARIANT)
        if checkpoint is not None:
            completed = int(checkpoint.get("completed_steps", 0))
            digest = str(checkpoint["digest"])
        else:
            completed = 0
            digest = initial_digest(request.user_task, seed)
            await host.publish_phase("initial_state", 0, request.task_id)
        resumed_from = completed
        for step in range(completed + 1, steps + 1):
            await host.check_abort(request.task_id)
            await host.publish_phase(STEP_PHASE, step, request.task_id)
            digest = step_digest(digest, step)
            await host.log_event(
                "reference", f"step {step} of {steps}", request.task_id, step=step,
            )
            await host.save_variant_checkpoint(
                request.task_id, REFERENCE_VARIANT,
                {"completed_steps": step, "steps": steps, "digest": digest, "seed": seed},
            )
        answer = configuration["answer"] or f"reference:{digest[:16]}"
        await host.publish_phase("final_result", steps, request.task_id)
        await host.publish_progress(
            request.task_id, "reference", "completed",
            [{"id": "steps", "label": "Steps", "status": "completed", "value": steps}],
        )
        result = {
            "answer": answer,
            "digest": digest,
            "steps": steps,
            "seed": seed,
            "resumed_from_step": resumed_from,
        }
        return VariantOutcome(
            variant_id=REFERENCE_VARIANT,
            answer=answer,
            result=result,
            public_result=dict(result),
            cost_usd=0.0,
        )
