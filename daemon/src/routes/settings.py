# /opt/bmas/daemon/src/routes/settings.py
"""
Settings API — runtime configuration overrides.

All changes are session-only (in-memory). Restarting the container
reverts to bmas.yaml defaults.

Endpoints:
  GET  /settings              → current routing + role_registry + defaults
  PATCH /settings/routing     → override complexity → model routing
  PATCH /settings/role_registry → override role registry entries
  POST  /settings/reset       → reset all overrides to bmas.yaml defaults
  GET  /settings/schema       → available models, node hosts, tiers
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from settings_store import get_store

logger = logging.getLogger("bmas.settings")

router = APIRouter(prefix="/settings", tags=["settings"])


# ── Response / Request models ─────────────────────────────────────────────


class RoutingPatch(BaseModel):
    """Partial or full routing override. Only provided tiers are changed."""
    simple: str | None = Field(None, description="Model alias for SIMPLE complexity")
    light: str | None = Field(None, description="Model alias for LIGHT complexity")
    medium: str | None = Field(None, description="Model alias for MEDIUM complexity")
    complex: str | None = Field(None, description="Model alias for COMPLEX complexity")

    def to_dict(self) -> dict[str, str]:
        """Return only the provided overrides, including explicit nulls."""
        return self.model_dump(exclude_unset=True)


class RoleEntryPatch(BaseModel):
    """Partial override for a single role registry entry."""
    preferred_host: str | None = Field(
        None, description="Preferred dispatch host IP (null = load-balanced)"
    )
    profile: str | None = Field(None, description="Hermes profile name")
    dispatch_port: int | None = Field(None, ge=1, le=65535, description="Dispatch port")


class RoleRegistryPatch(BaseModel):
    """Mapping of role_name → partial entry override."""
    entries: dict[str, RoleEntryPatch] = Field(
        ..., description="Role name → partial entry to merge"
    )


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("")
async def get_settings():
    """Return the current active settings (routing + role_registry) with defaults."""
    store = get_store()
    routing = await store.get_routing()
    role_registry = await store.get_role_registry()
    defaults_routing = await store.get_defaults_routing()
    defaults_registry = await store.get_defaults_role_registry()
    return {
        "routing": routing,
        "role_registry": role_registry,
        "defaults": {
            "routing": defaults_routing,
            "role_registry": defaults_registry,
        },
    }


@router.patch("/routing")
async def patch_routing(body: RoutingPatch):
    """Override complexity → model routing for this session.

    Only provided tiers are changed; omitted tiers keep their current value.
    """
    overrides = body.to_dict()
    if not overrides:
        raise HTTPException(status_code=400, detail="No routing overrides provided")

    store = get_store()
    try:
        new_routing = await store.patch_routing(overrides)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    logger.info("Routing patched via API: %s", overrides)
    return {"routing": new_routing, "changed": overrides}


@router.patch("/role_registry")
async def patch_role_registry(body: RoleRegistryPatch):
    """Override role registry entries for this session.

    Only provided fields in each entry are changed.
    """
    raw_overrides: dict[str, dict] = {
        role: entry.model_dump(exclude_unset=True)
        for role, entry in body.entries.items()
    }
    if not raw_overrides:
        raise HTTPException(status_code=400, detail="No role_registry overrides provided")

    store = get_store()
    try:
        new_registry = await store.patch_role_registry(raw_overrides)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    logger.info("Role registry patched via API for roles: %s", list(raw_overrides.keys()))
    return {"role_registry": new_registry, "changed_roles": list(raw_overrides.keys())}


@router.post("/reset")
async def reset_settings():
    """Reset all runtime overrides back to bmas.yaml defaults."""
    store = get_store()
    restored = await store.reset_to_defaults()
    logger.info("Settings reset to bmas.yaml defaults via API")
    return {"message": "Settings reset to bmas.yaml defaults", **restored}


@router.get("/schema")
async def get_schema():
    """Return available options for settings editors.

    Provides the data needed for the Settings UI dropdowns:
    - available_models: model aliases defined in bmas.yaml + 'local'
    - configured_hosts: agent node IPs from bmas.yaml
    - complexity_tiers: valid tier names
    - known_roles: known blackboard role names
    """
    store = get_store()
    return await store.get_schema()
