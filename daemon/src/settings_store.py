# /opt/bmas/daemon/src/settings_store.py
"""
bMAS Runtime Settings Store — session-lifetime in-memory overrides.

Provides a singleton that holds the *active* configuration for:
  1. complexity → model routing   (config.MODEL_ROUTING as seed)
  2. role registry                (config.ROLE_REGISTRY as seed)

Values are seeded from bmas.yaml at first access and can be overridden
via the /settings REST API. All overrides are session-only: restarting
the container reverts to bmas.yaml defaults.

Per-task overrides (submitted alongside a task) are NOT stored here —
they are threaded directly through process_task() and live only for
the lifetime of a single task.

Thread-safety: all public methods are async and guarded by a single
asyncio.Lock — safe for concurrent FastAPI handlers.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

logger = logging.getLogger("bmas.settings_store")

# Lazy imports from config (avoid circular deps at module import time)
_VALID_COMPLEXITIES = {"simple", "light", "medium", "complex"}
_VALID_ROLES = {"planner", "expert", "critic", "conflict_resolver", "cleaner", "decider", "universal"}


# ── Classic runtime field contract ───────────────────────────────────────

CLASSIC_FIELD_METADATA: list[dict[str, Any]] = [
    {"key": "max_rounds", "label": "Maximum rounds", "type": "integer", "min": 1, "max": 50,
     "group": "limits", "description": "Rounds the control unit may run before the decider must finalize."},
    {"key": "max_duration_s", "label": "Maximum duration", "type": "integer", "min": 30, "max": 14400,
     "unit": "s", "group": "limits", "description": "Wall-clock limit for one task. The next round does not start after this time."},
    {"key": "budget_ceiling_usd", "label": "Budget ceiling", "type": "number", "min": 0.01, "max": 1000,
     "step": 0.01, "unit": "USD", "group": "limits", "description": "Spend limit for one task across every model call."},
    {"key": "max_concurrent_activations", "label": "Concurrent activations", "type": "integer", "min": 1, "max": 32,
     "group": "limits", "description": "Agents that may run at the same time in one round."},
    {"key": "experts_per_tier", "label": "Experts per tier", "type": "tier_map", "min": 0, "max": 12,
     "group": "roster", "description": "Domain experts the agent generator creates for each complexity tier."},
    {"key": "stall_rounds", "label": "Stall rounds", "type": "integer", "min": 1, "max": 20,
     "group": "control", "description": "Rounds without board changes before the planner must re-plan."},
    {"key": "max_replans", "label": "Maximum re-plans", "type": "integer", "min": 0, "max": 20,
     "group": "control", "description": "Re-plans allowed after a stall before the task finalizes."},
    {"key": "cu_mode", "label": "Control unit mode", "type": "enum", "options": ["llm", "heuristic_first"],
     "group": "control", "description": "How the control unit selects agents each round."},
    {"key": "coordinator_narration", "label": "Coordinator narration", "type": "boolean",
     "group": "control", "description": "Save the control unit's routing rationale for the task page."},
    {"key": "sole_similarity", "label": "Sole-answer check", "type": "enum",
     "options": ["auto", "exact", "embedding", "judge"],
     "group": "control", "description": "How the decider verifies a single unchallenged answer."},
    {"key": "grace_verification", "label": "Grace verification", "type": "boolean",
     "group": "control", "description": "Before a limit stop, spend one extra round so the critic can review the answer."},
    {"key": "actor_context", "label": "Agent context", "type": "enum", "options": ["chained", "fresh"],
     "group": "board", "description": "Chained keeps each agent's model session across rounds. Fresh sends only the board view each turn, which keeps long runs affordable."},
    {"key": "round_execution", "label": "Round execution", "type": "enum", "options": ["concurrent", "sequential"],
     "group": "control", "description": "Run the selected agents of one round together or one after another."},
    {"key": "view_budget_tokens", "label": "Board view budget", "type": "integer", "min": 512, "max": 200000,
     "unit": "tokens", "group": "board", "description": "Tokens of blackboard context sent to each agent."},
    {"key": "cleaner_entry_threshold", "label": "Cleaner entry threshold", "type": "integer", "min": 1, "max": 500,
     "group": "board", "description": "Open entries that trigger the cleaner."},
    {"key": "cleaner_token_threshold", "label": "Cleaner token threshold", "type": "integer", "min": 1, "max": 500000,
     "unit": "tokens", "group": "board", "description": "Board size in tokens that triggers the cleaner."},
    {"key": "cleaner_retention_weights", "label": "Cleaner retention weights", "type": "weight_map",
     "min": 0, "max": 100, "step": 0.01, "group": "board",
     "description": "Weights the cleaner uses to rank entries it keeps."},
]

CLASSIC_FIELDS: tuple[str, ...] = tuple(field["key"] for field in CLASSIC_FIELD_METADATA)
_VALID_TIERS_FOR_EXPERTS = ("simple", "light", "medium", "complex")
_VALID_RETENTION_WEIGHTS = ("salience", "confidence", "recency", "size_penalty")


def _seed_classic_defaults() -> dict[str, Any]:
    """Read the classic runtime settings that bmas.yaml configured at startup."""
    from config import CLASSIC_CONFIG, ROUND_EXECUTION, VIEW_BUDGET_TOKENS

    return {
        **copy.deepcopy(dict(CLASSIC_CONFIG)),
        "round_execution": ROUND_EXECUTION,
        "view_budget_tokens": VIEW_BUDGET_TOKENS,
        "grace_verification": bool(CLASSIC_CONFIG.get("grace_verification", True)),
        "actor_context": str(CLASSIC_CONFIG.get("actor_context", "chained")),
    }


def _validate_classic(candidate: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete classic settings mapping and return a clean copy."""
    from config_schema import ClassicConfig

    data = copy.deepcopy(candidate)
    round_execution = data.pop("round_execution", "concurrent")
    view_budget = data.pop("view_budget_tokens", 12000)
    grace_verification = data.pop("grace_verification", True)
    actor_context = data.pop("actor_context", "chained")
    if round_execution not in ("concurrent", "sequential"):
        raise ValueError("round_execution must be 'concurrent' or 'sequential'")
    if not isinstance(grace_verification, bool):
        raise ValueError("grace_verification must be true or false")
    if actor_context not in ("chained", "fresh"):
        raise ValueError("actor_context must be 'chained' or 'fresh'")
    if isinstance(view_budget, bool) or not isinstance(view_budget, int) or view_budget < 512:
        raise ValueError("view_budget_tokens must be an integer of at least 512")

    experts = data.get("experts_per_tier", {})
    if not isinstance(experts, dict):
        raise ValueError("experts_per_tier must be a mapping of tier to count")
    for tier, count in experts.items():
        if tier not in _VALID_TIERS_FOR_EXPERTS:
            raise ValueError(f"experts_per_tier has an unknown tier '{tier}'")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"experts_per_tier.{tier} must be an integer of 0 or more")
    weights = data.get("cleaner_retention_weights", {})
    if not isinstance(weights, dict):
        raise ValueError("cleaner_retention_weights must be a mapping")
    for name, weight in weights.items():
        if name not in _VALID_RETENTION_WEIGHTS:
            raise ValueError(f"cleaner_retention_weights has an unknown weight '{name}'")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
            raise ValueError(f"cleaner_retention_weights.{name} must be a number of 0 or more")

    try:
        model = ClassicConfig.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError
        raise ValueError(_first_validation_message(exc)) from exc
    validated = model.model_dump()
    validated["round_execution"] = round_execution
    validated["view_budget_tokens"] = int(view_budget)
    validated["grace_verification"] = grace_verification
    validated["actor_context"] = actor_context
    return validated


def validate_classic_settings(candidate: dict[str, Any]) -> dict[str, Any]:
    """Public wrapper: validate a complete classic settings mapping."""
    unknown = set(candidate) - set(CLASSIC_FIELDS)
    if unknown:
        raise ValueError(f"Unknown classic setting(s): {', '.join(sorted(unknown))}")
    return _validate_classic(candidate)


def _first_validation_message(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            location = ".".join(str(part) for part in first.get("loc", ()))
            return f"{location}: {first.get('msg', 'invalid value')}"
        except Exception:  # pragma: no cover - defensive
            pass
    return str(exc)


class SettingsStore:
    """In-memory singleton for runtime-overridable bMAS settings."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._routing: dict[str, str] | None = None          # complexity → model alias
        self._role_registry: dict[str, dict] | None = None   # role → registry entry
        self._defaults_routing: dict[str, str] | None = None
        self._defaults_registry: dict[str, dict] | None = None
        self._classic: dict[str, Any] | None = None             # classic runtime limits
        self._defaults_classic: dict[str, Any] | None = None

    # ── Internal seed ────────────────────────────────────────────────────

    def _ensure_seeded(self) -> None:
        """Seed from config on first access (lazy to avoid circular imports)."""
        if self._routing is not None:
            return

        from config import MODEL_ROUTING, ROLE_REGISTRY

        self._defaults_routing = dict(MODEL_ROUTING)
        self._defaults_registry = copy.deepcopy(dict(ROLE_REGISTRY))
        self._routing = dict(MODEL_ROUTING)
        self._role_registry = copy.deepcopy(dict(ROLE_REGISTRY))
        classic_defaults = _seed_classic_defaults()
        self._defaults_classic = copy.deepcopy(classic_defaults)
        self._classic = copy.deepcopy(classic_defaults)

    # ── Routing ──────────────────────────────────────────────────────────

    async def get_routing(self) -> dict[str, str]:
        """Return the active complexity → model routing table."""
        async with self._lock:
            self._ensure_seeded()
            return dict(self._routing)  # type: ignore[arg-type]

    async def patch_routing(self, overrides: dict[str, str]) -> dict[str, str]:
        """Merge ``overrides`` into the active routing table.

        Args:
            overrides: Partial or full mapping of complexity tier → model alias.
                Accepts model aliases defined in bmas.yaml, 'local' (edge inference),
                or the internal 'edge-node-N' aliases.

        Returns:
            The new full routing table after applying overrides.

        Raises:
            ValueError: If an unknown complexity tier or model alias is provided.
        """
        async with self._lock:
            self._ensure_seeded()

            # Validate tiers
            bad_tiers = set(overrides.keys()) - _VALID_COMPLEXITIES
            if bad_tiers:
                raise ValueError(
                    f"Unknown complexity tier(s): {', '.join(sorted(bad_tiers))}. "
                    f"Valid tiers: {', '.join(sorted(_VALID_COMPLEXITIES))}"
                )

            # Validate model aliases:
            # Accept: model aliases from bmas.yaml, 'local', 'edge-node-N' individual aliases,
            # and the current default values (which may be 'local' etc.)
            from config import EDGE_NODE_MODELS, RAW_CONFIG
            available_models = set(RAW_CONFIG.get("models", {}).keys()) | {"local"}
            # Accept individual edge-node-N aliases for direct targeting
            available_models |= set(EDGE_NODE_MODELS)
            # Accept any values currently in the routing table (e.g. seeded defaults)
            available_models |= {v for v in (self._defaults_routing or {}).values()}
            available_models |= {v for v in (self._routing or {}).values()}

            bad_models = {v for v in overrides.values() if v not in available_models}
            if bad_models:
                raise ValueError(
                    f"Unknown model alias(es): {', '.join(sorted(bad_models))}. "
                    f"Available: {', '.join(sorted(available_models))}"
                )

            self._routing.update(overrides)  # type: ignore[union-attr]
            logger.info("Routing overridden: %s", overrides)
            return dict(self._routing)  # type: ignore[arg-type]

    async def get_defaults_routing(self) -> dict[str, str]:
        """Return the bmas.yaml-seeded default routing (immutable reference)."""
        async with self._lock:
            self._ensure_seeded()
            return dict(self._defaults_routing)  # type: ignore[arg-type]

    # ── Role Registry ────────────────────────────────────────────────────

    async def get_role_registry(self) -> dict[str, dict]:
        """Return the active role registry."""
        async with self._lock:
            self._ensure_seeded()
            return copy.deepcopy(self._role_registry)  # type: ignore[arg-type]

    async def patch_role_registry(self, overrides: dict[str, Any]) -> dict[str, dict]:
        """Merge ``overrides`` into the active role registry.

        Each key is a role name; each value is a partial or full registry entry.
        Supports: preferred_host (str | null), profile (str), dispatch_port (int).

        Returns:
            The new full role registry after applying overrides.

        Raises:
            ValueError: If required fields are invalid.
        """
        async with self._lock:
            self._ensure_seeded()

            for role_name, entry in overrides.items():
                if not isinstance(entry, dict):
                    raise ValueError(f"Role registry entry for '{role_name}' must be a mapping, got {type(entry).__name__}")

                existing = self._role_registry.get(role_name, {})  # type: ignore[union-attr]
                merged = copy.deepcopy(existing)

                if "preferred_host" in entry:
                    merged["preferred_host"] = entry["preferred_host"]  # str or None

                if "profile" in entry:
                    if not isinstance(entry["profile"], str) or not entry["profile"].strip():
                        raise ValueError(f"Role '{role_name}': 'profile' must be a non-empty string")
                    merged["profile"] = entry["profile"].strip()

                if "dispatch_port" in entry:
                    try:
                        port = int(entry["dispatch_port"])
                        if not (1 <= port <= 65535):
                            raise ValueError(f"Role '{role_name}': 'dispatch_port' must be 1–65535, got {port}")
                        merged["dispatch_port"] = port
                    except (ValueError, TypeError) as exc:
                        raise ValueError(f"Role '{role_name}': 'dispatch_port' must be an integer") from exc

                if "enabled" in entry:
                    if not isinstance(entry["enabled"], bool):
                        raise ValueError(f"Role '{role_name}': 'enabled' must be true or false")
                    merged["enabled"] = entry["enabled"]

                self._role_registry[role_name] = merged  # type: ignore[index]

            logger.info("Role registry overridden for roles: %s", list(overrides.keys()))
            return copy.deepcopy(self._role_registry)  # type: ignore[arg-type]

    async def get_defaults_role_registry(self) -> dict[str, dict]:
        """Return the bmas.yaml-seeded default role registry."""
        async with self._lock:
            self._ensure_seeded()
            return copy.deepcopy(self._defaults_registry)  # type: ignore[arg-type]

    # ── Reset ────────────────────────────────────────────────────────────

    # ── Classic runtime limits ───────────────────────────────────────────

    async def get_classic(self) -> dict[str, Any]:
        """Return the active classic runtime settings."""
        async with self._lock:
            self._ensure_seeded()
            return copy.deepcopy(self._classic)  # type: ignore[arg-type]

    async def get_defaults_classic(self) -> dict[str, Any]:
        """Return the bmas.yaml-seeded classic runtime settings."""
        async with self._lock:
            self._ensure_seeded()
            return copy.deepcopy(self._defaults_classic)  # type: ignore[arg-type]

    async def patch_classic(self, overrides: dict[str, Any]) -> dict[str, Any]:
        """Merge ``overrides`` into the classic runtime settings.

        The merged result must satisfy the ``ClassicConfig`` schema plus the
        ``round_execution`` and ``view_budget_tokens`` coordination fields.
        New tasks read these values at submission time.

        Raises:
            ValueError: If a key is unknown or a value is out of range.
        """
        async with self._lock:
            self._ensure_seeded()
            unknown = set(overrides) - set(CLASSIC_FIELDS)
            if unknown:
                raise ValueError(
                    f"Unknown classic setting(s): {', '.join(sorted(unknown))}. "
                    f"Valid keys: {', '.join(CLASSIC_FIELDS)}"
                )
            merged: dict[str, Any] = copy.deepcopy(self._classic or {})
            for key, value in overrides.items():
                if key in ("experts_per_tier", "cleaner_retention_weights"):
                    if not isinstance(value, dict):
                        raise ValueError(f"'{key}' must be a mapping")
                    merged[key] = {**merged.get(key, {}), **value}
                else:
                    merged[key] = value
            validated = _validate_classic(merged)
            self._classic = validated
            logger.info("Classic settings overridden: %s", sorted(overrides))
            return copy.deepcopy(validated)

    async def reset_to_defaults(self) -> dict[str, Any]:
        """Reset all overrides back to bmas.yaml values.

        Returns:
            dict with keys 'routing', 'role_registry', and 'classic' showing restored values.
        """
        async with self._lock:
            self._ensure_seeded()
            self._routing = dict(self._defaults_routing)  # type: ignore[arg-type]
            self._role_registry = copy.deepcopy(self._defaults_registry)  # type: ignore[arg-type]
            self._classic = copy.deepcopy(self._defaults_classic)  # type: ignore[arg-type]
            logger.info("Settings reset to bmas.yaml defaults")
            return {
                "routing": dict(self._routing),  # type: ignore[arg-type]
                "role_registry": copy.deepcopy(self._role_registry),  # type: ignore[arg-type]
                "classic": copy.deepcopy(self._classic),  # type: ignore[arg-type]
            }

    # ── Schema / metadata ────────────────────────────────────────────────

    async def get_schema(self) -> dict[str, Any]:
        """Return available options for routing and role registry.

        Provides the data needed for the Settings UI to populate dropdowns.
        """
        async with self._lock:
            self._ensure_seeded()

        from config import RAW_CONFIG

        raw_models = RAW_CONFIG.get("models", {})
        available_models = [
            {
                "alias": alias,
                "provider": info.get("provider", ""),
                "model": info.get("model", ""),
                "max_tokens": info.get("max_tokens"),
            }
            for alias, info in raw_models.items()
        ]
        # Always include 'local' if there are inference nodes
        nodes_with_inference = [
            n for n in RAW_CONFIG.get("nodes", []) if n.get("inference")
        ]
        if nodes_with_inference:
            # Collect unique models across all edge nodes
            edge_models = list({n["inference"].get("model", "local-model") for n in nodes_with_inference})
            edge_model_name = edge_models[0] if len(edge_models) == 1 else ", ".join(edge_models)
            edge_hosts = [
                {
                    "node_name": n.get("name", f"node-{i+1}"),
                    "host": n["inference"].get("host", n.get("host", "")),
                    "port": n["inference"].get("port", 8080),
                    "model": n["inference"].get("model", "local-model"),
                }
                for i, n in enumerate(nodes_with_inference)
            ]
            available_models.append({
                "alias": "local",
                "provider": "local",
                "model": edge_model_name,
                "max_tokens": None,         # Edge models: no configured output limit
                "node_count": len(nodes_with_inference),
                "edge_nodes": edge_hosts,   # Detailed per-node info for UI display
            })

        # Available node hosts for role registry preferred_host dropdown
        configured_hosts = [
            {
                "host": n.get("host"),
                "name": n.get("name", n.get("role", "")),
                "role": n.get("role", ""),
            }
            for n in RAW_CONFIG.get("nodes", [])
        ]

        return {
            "complexity_tiers": list(_VALID_COMPLEXITIES),
            "available_models": available_models,
            "configured_hosts": configured_hosts,
            "known_roles": list(_VALID_ROLES),
            "classic_fields": copy.deepcopy(CLASSIC_FIELD_METADATA),
        }


# ── Module-level singleton ────────────────────────────────────────────────

_store: SettingsStore | None = None


def get_store() -> SettingsStore:
    """Return the module-level singleton SettingsStore.

    The store is lazily instantiated on first call and seeds from
    config.py values. This is safe for FastAPI's async handler model
    because all async methods are lock-guarded.
    """
    global _store
    if _store is None:
        _store = SettingsStore()
    return _store
