"""Publish the compiler input and its intent-based editor metadata."""
from __future__ import annotations

import copy
from typing import Any

from pydantic import Field

from core.variants.classic.profiles import (
    DEFAULT_EFFORT_LEVEL,
    DEFAULT_FIDELITY,
    DEPLOYMENT_CAPS,
    EFFORT_PROFILES,
    FIDELITY_PROFILES,
    SCHEMA_DEFAULTS,
    valid_effort_levels,
)
from core.variants.classic.spec import (
    ClassicSpec,
    ClassicSpecInput,
    ControlledModel,
    TaskOverrideSet,
)


class CompileRequest(ControlledModel):
    """User choices only. The server supplies the deployment snapshot."""

    fidelity: str = DEFAULT_FIDELITY
    effort: str = DEFAULT_EFFORT_LEVEL
    task_overrides: TaskOverrideSet = Field(default_factory=TaskOverrideSet)
    asset_manifest_digest: str | None = None


GROUPS = {
    "Team": ("team.", "models.", "randomness."),
    "Coordination": ("coordination.",),
    "Memory": ("memory.", "board.", "cleaner."),
    "Verification": ("verification.", "consensus."),
    "Limits": ("limits.",),
    "Recovery": ("recovery.", "termination."),
}
BEGINNER = {
    "team.experts_by_tier.complex", "coordination.max_parallel_agents",
    "memory.actor_memory", "verification.evidence_policy",
    "verification.solution_review", "limits.max_cost", "limits.max_duration_seconds",
    "recovery.max_replans",
}
LABELS = {
    "limits.max_cost": "Maximum cost (USD)",
    "limits.max_duration_seconds": "Maximum time (seconds)",
    "memory.actor_memory": "Memory mode",
    "verification.evidence_policy": "Evidence strictness",
    "verification.solution_review": "Verification strictness",
    "team.experts_by_tier.complex": "Experts for complex tasks",
    "coordination.max_parallel_agents": "Parallel agents",
    "recovery.max_replans": "Maximum replans",
}
TRADEOFFS = {
    "Team": "More experts and model diversity can increase cost and independent checks.",
    "Coordination": "More parallel work can reduce waiting and increase cost in flight.",
    "Memory": "Larger context retains more evidence and uses more input tokens.",
    "Verification": "Stronger evidence checks can increase cost and time.",
    "Limits": "Higher limits allow more work. A limit is a ceiling, not a spending target.",
    "Recovery": "More retries and replans allow more recovery work and consume the run limits.",
}


def field_schemas() -> dict[str, dict[str, Any]]:
    """Derive override types from the compiled model, without copying its fields."""
    schema = ClassicSpec.model_json_schema()

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        return schema["$defs"][node["$ref"].split("/")[-1]] if "$ref" in node else node

    fields = {}
    for path, default in SCHEMA_DEFAULTS.items():
        node = schema
        for part in path.split("."):
            node = resolve(node)
            node = node["properties"][part] if "properties" in node else node["additionalProperties"]
        node = copy.deepcopy(resolve(node))
        if path == "limits.max_cost":
            node = {"type": "string", "pattern": r"^[0-9]+(?:\.[0-9]+)?$"}
        node["default"] = default
        fields[path] = node
    return fields


def published_schema() -> dict[str, Any]:
    """Return JSON Schema with the editor extension and all deployment caps."""
    schema = ClassicSpecInput.model_json_schema()
    fields = field_schemas()
    override_schema = schema["$defs"]["TaskOverrideSet"]["properties"]["classic"]
    override_schema.update(properties=fields, additionalProperties=False)
    schema["properties"]["fidelity"]["enum"] = list(FIDELITY_PROFILES)
    schema["properties"]["effort"]["enum"] = list(valid_effort_levels())
    request_schema = CompileRequest.model_json_schema()
    request_schema["$defs"]["TaskOverrideSet"] = copy.deepcopy(schema["$defs"]["TaskOverrideSet"])
    request_schema["properties"]["fidelity"]["enum"] = list(FIDELITY_PROFILES)
    request_schema["properties"]["effort"]["enum"] = list(valid_effort_levels())
    schema["x-editor"] = {
        "request_schema": request_schema,
        "defaults": CompileRequest().model_dump(mode="json"),
        "availability": "test_only",
        "groups": [
            {"name": group, "description": TRADEOFFS[group], "controls": [
                {"path": path, "label": LABELS.get(path, path.replace(".", " / ").replace("_", " ").capitalize()),
                 "schema": field, "beginner": path in BEGINNER,
                 "cap": list(DEPLOYMENT_CAPS[path]) if path in DEPLOYMENT_CAPS else None}
                for path, field in fields.items() if path.startswith(prefixes)
            ]}
            for group, prefixes in GROUPS.items()
        ],
        "fidelity_profiles": [profile.model_dump(mode="json") for profile in FIDELITY_PROFILES.values()],
        "effort_profiles": [profile.model_dump(mode="json") for profile in EFFORT_PROFILES.values()],
        "caps": {path: list(bounds) for path, bounds in DEPLOYMENT_CAPS.items()},
    }
    return schema
