"""Define benchmark admission limits and conservative resource claims."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


def _limit_map(name: str) -> dict[str, int]:
    """Read one JSON object of positive capacity limits."""
    raw = os.getenv(name, "{}").strip() or "{}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{name} must contain valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    result: dict[str, int] = {}
    for key, limit in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{name} contains an invalid key")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ValueError(f"{name}.{key} must be an integer from 1 to 10000")
        result[key.strip()] = limit
    return result


def _string_map(name: str) -> dict[str, str]:
    """Read one JSON object that maps model aliases to providers."""
    raw = os.getenv(name, "{}").strip() or "{}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{name} must contain valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    result = {
        str(model).strip(): str(provider).strip()
        for model, provider in value.items()
        if str(model).strip() and str(provider).strip()
    }
    if len(result) != len(value):
        raise ValueError(f"{name} contains an empty model or provider")
    return result


@dataclass(frozen=True)
class CapacityPolicy:
    """Apply global, runtime, model, and provider attempt limits."""

    global_limit: int = 4
    runtime_limits: dict[str, int] = field(default_factory=dict)
    model_limits: dict[str, int] = field(default_factory=dict)
    provider_limits: dict[str, int] = field(default_factory=dict)
    model_providers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_environment(cls) -> CapacityPolicy:
        """Build one validated policy from daemon environment values."""
        global_limit = int(os.getenv("BMAS_BENCHMARK_MAX_ACTIVE", "4"))
        if not 1 <= global_limit <= 128:
            raise ValueError("BMAS_BENCHMARK_MAX_ACTIVE must be from 1 to 128")
        return cls(
            global_limit=global_limit,
            runtime_limits=_limit_map("BMAS_BENCHMARK_RUNTIME_LIMITS"),
            model_limits=_limit_map("BMAS_BENCHMARK_MODEL_LIMITS"),
            provider_limits=_limit_map("BMAS_BENCHMARK_PROVIDER_LIMITS"),
            model_providers=_string_map("BMAS_BENCHMARK_MODEL_PROVIDERS"),
        )

    def claims(self, attempt: dict[str, Any]) -> set[str]:
        """Return all conservative capacity keys for one attempt."""
        claims = {f"runtime:{attempt.get('runtime_id') or 'unknown'}"}
        task_model = str(attempt.get("task_model_used") or "").strip()
        models: set[str] = {task_model} if task_model else set()
        if not models:
            arm = attempt.get("arm_configuration")
            effective = arm.get("effective_configuration") if isinstance(arm, dict) else None
            routing = effective.get("model_routing") if isinstance(effective, dict) else None
            if isinstance(routing, dict):
                models.update(
                    str(model).strip()
                    for model in routing.values()
                    if str(model).strip()
                )
        for model in models:
            claims.add(f"model:{model}")
            provider = self.model_providers.get(model)
            if provider:
                claims.add(f"provider:{provider}")
        return claims

    def limits(self) -> dict[str, int]:
        """Return one flat resource-key limit map."""
        return {
            **{f"runtime:{key}": value for key, value in self.runtime_limits.items()},
            **{f"model:{key}": value for key, value in self.model_limits.items()},
            **{f"provider:{key}": value for key, value in self.provider_limits.items()},
        }

    def allows(
        self,
        candidate: dict[str, Any],
        active: list[dict[str, Any]],
    ) -> bool:
        """Return true when each candidate claim remains below its limit."""
        if len(active) >= self.global_limit:
            return False
        limits = self.limits()
        for claim in self.claims(candidate):
            limit = limits.get(claim)
            if limit is None:
                continue
            used = sum(claim in self.claims(item) for item in active)
            if used >= limit:
                return False
        return True
