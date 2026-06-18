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


class SettingsStore:
    """In-memory singleton for runtime-overridable bMAS settings."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._routing: dict[str, str] | None = None          # complexity → model alias
        self._role_registry: dict[str, dict] | None = None   # role → registry entry
        self._defaults_routing: dict[str, str] | None = None
        self._defaults_registry: dict[str, dict] | None = None

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
            # Accept: model aliases from bmas.yaml, 'local', 'edge-node-N' internal aliases,
            # and the current default values (which may be 'edge-node-1' etc.)
            from config import RAW_CONFIG
            available_models = set(RAW_CONFIG.get("models", {}).keys()) | {"local"}
            # Also accept edge-node-N aliases (internal resolution of "local" in yaml)
            # and any values currently in the routing table (e.g. seeded defaults)
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

                self._role_registry[role_name] = merged  # type: ignore[index]

            logger.info("Role registry overridden for roles: %s", list(overrides.keys()))
            return copy.deepcopy(self._role_registry)  # type: ignore[arg-type]

    async def get_defaults_role_registry(self) -> dict[str, dict]:
        """Return the bmas.yaml-seeded default role registry."""
        async with self._lock:
            self._ensure_seeded()
            return copy.deepcopy(self._defaults_registry)  # type: ignore[arg-type]

    # ── Reset ────────────────────────────────────────────────────────────

    async def reset_to_defaults(self) -> dict[str, Any]:
        """Reset all overrides back to bmas.yaml values.

        Returns:
            dict with keys 'routing' and 'role_registry' showing restored values.
        """
        async with self._lock:
            self._ensure_seeded()
            self._routing = dict(self._defaults_routing)  # type: ignore[arg-type]
            self._role_registry = copy.deepcopy(self._defaults_registry)  # type: ignore[arg-type]
            logger.info("Settings reset to bmas.yaml defaults")
            return {
                "routing": dict(self._routing),  # type: ignore[arg-type]
                "role_registry": copy.deepcopy(self._role_registry),  # type: ignore[arg-type]
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
