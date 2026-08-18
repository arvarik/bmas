"""Authoritative coordination capability endpoint."""

from fastapi import APIRouter

from core.variants import variant_capabilities

router = APIRouter(tags=["capabilities"])


@router.get("/capabilities")
async def get_capabilities() -> dict:
    """Return all coordination runtimes available in this daemon."""
    return variant_capabilities()
