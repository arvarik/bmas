"""Authenticated, read-only Classic schema and compile previews."""
from __future__ import annotations

import copy
from typing import Any, NoReturn

from fastapi import APIRouter, HTTPException, Request
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from auth import require_api_key
from config import BMAS_API_KEY
from core.variants.classic.compiler import (
    LEGACY_FIELDS,
    ClassicSpecError,
    compile_specification,
    legacy_settings_to_fields,
    specification_digest,
)
from core.variants.classic.editor import CompileRequest, field_schemas, published_schema
from core.variants.classic.profiles import (
    DEPLOYMENT_CAPS,
    EFFORT_PROFILES,
    FIDELITY_PROFILES,
    SCHEMA_DEFAULTS,
    resolve_effort_level,
    resolve_fidelity,
)
from core.variants.classic.runtime import deployment_snapshot
from core.variants.classic.spec import ClassicSpecInput

router = APIRouter(prefix="/classic/spec", tags=["classic specification"])


def reject(errors: list[dict[str, str]]) -> NoReturn:
    raise HTTPException(status_code=422, detail={"errors": errors})


@router.get("/schema")
async def schema(request: Request) -> dict[str, Any]:
    require_api_key(request, BMAS_API_KEY)
    return published_schema()


@router.post("/compile")
async def compile_preview(request: Request) -> dict[str, Any]:
    """Compile without admitting work, promoting assets, or calling a provider."""
    require_api_key(request, BMAS_API_KEY)
    try:
        body = CompileRequest.model_validate(await request.json(), strict=True)
    except ValidationError as exc:
        reject([{"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]} for error in exc.errors()])
    except ValueError:
        reject([{"field": "advanced", "message": "Enter a valid JSON object."}])
    errors = []
    for field, resolve in (("fidelity", resolve_fidelity), ("effort", resolve_effort_level)):
        try:
            resolve(getattr(body, field))
        except ValueError as exc:
            errors.append({"field": field, "message": str(exc)})
    overrides = {}
    for key, value in body.task_overrides.classic.items():
        try:
            overrides.update(legacy_settings_to_fields({key: value}))
        except (ClassicSpecError, TypeError, ValueError) as exc:
            errors.append({"field": LEGACY_FIELDS.get(key, key) if key in SCHEMA_DEFAULTS or key in LEGACY_FIELDS else "advanced", "message": str(exc)})
    definitions = field_schemas()
    for field, value in overrides.items():
        definition = copy.deepcopy(definitions[field])
        # Caps clamp after overrides. Validate the type before compilation.
        if field in DEPLOYMENT_CAPS:
            for bound in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
                definition.pop(bound, None)
        for error in Draft202012Validator(definition).iter_errors(value):
            errors.append({"field": field, "message": error.message})
    if errors:
        reject(errors)
    spec_input = ClassicSpecInput(
        fidelity=body.fidelity, effort=body.effort,
        deployment=await deployment_snapshot(), task_overrides=body.task_overrides,
        asset_manifest_digest=body.asset_manifest_digest,
    )
    try:
        spec = compile_specification(spec_input)
    except (ClassicSpecError, ValueError, TypeError) as exc:
        cause = exc.__cause__
        if isinstance(cause, ValidationError):
            mapped: list[dict[str, str]] = []
            for error in cause.errors():
                suffix = ".".join(str(part) for part in error["loc"])
                paths = [path for path in SCHEMA_DEFAULTS if suffix and path.endswith("." + suffix)]
                if not paths and "max_output_tokens" in error["msg"]:
                    paths = ["limits.max_output_tokens", "limits.max_input_tokens"]
                mapped.extend({"field": path, "message": error["msg"]} for path in paths or ["advanced"])
            reject(mapped)
        field = next((path for path in SCHEMA_DEFAULTS if path in str(exc)), "advanced")
        reject([{"field": field, "message": str(exc)}])
    effective = spec.model_dump(mode="json")
    differences = {}
    for layer, profile in (
        ("fidelity", FIDELITY_PROFILES[spec.fidelity.profile_id]),
        ("effort", EFFORT_PROFILES[spec.effort.profile_id]),
    ):
        differences[layer] = [
            {"field": path, "profile": value,
             "effective": spec.resolution[path].offered[spec.resolution[path].layer],
             "source": spec.resolution[path].layer}
            for path, value in profile.values.items()
            if value != spec.resolution[path].offered[spec.resolution[path].layer]
        ]
    return {
        "specification_digest": specification_digest(spec), "specification": effective,
        "differences": differences,
        "caps": {path: list(bounds) for path, bounds in DEPLOYMENT_CAPS.items()},
        "availability": "test_only", "admissible": False,
        "provider_limits": {
            "context_window_tokens": None, "output_tokens_per_call": None,
            "note": "Provider context and per-call output limits have no verified record. Run token limits are separate.",
        },
        "estimate_assumptions": [
            "This planning range is not a quote or a guarantee.",
            "The cost floor uses the cheapest configured model, the board view token budget, and 512 output tokens. Missing prices weaken the estimate.",
            "The latency floor uses five seconds per activation. The upper bounds are the run cost and time limits.",
            "The tokenizer estimate uses four characters per token. Provider revisions are not pinned.",
        ],
        "endpoint_notice": "Ordinary endpoint edits affect new runs only. Each admitted run keeps its immutable endpoint sets and effective values.",
    }
