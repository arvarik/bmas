"""Authoritative coordination capability endpoint."""

from fastapi import APIRouter

from capability_publication import CapabilityDirectory
from core.variants import variant_capabilities

router = APIRouter(tags=["capabilities"])


@router.get("/capabilities")
async def get_capabilities() -> dict:
    """Return all coordination runtimes available in this daemon."""
    return variant_capabilities()


@router.get("/capabilities/runtime-pairs")
async def runtime_pair_capabilities():
    """Publish one capability record per runtime pair with its availability."""
    directory = CapabilityDirectory()
    return {
        "records": [record.to_dict() for _key, record in sorted(directory.records.items())],
        "runnable": [key.to_dict() for key in directory.runnable_choices()],
        "planned": [key.to_dict() for key in directory.planned_pairs()],
    }
