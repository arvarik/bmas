"""Admit one interactive task into one Foundation run.

An interactive task starts through the orchestrator, not through the
benchmark scheduler. Before the runtime executes, the task receives
one Foundation run through the full admission writer: the exact
runtime pair, the version set, the policy set, the asset manifest of
the task's uploaded files, the storage readiness check, the live
qualification records, the run budget with its initial reservation,
the journal genesis, and the run-control row with the task fence. The
agent dispatch then binds every signed grant to that run and fence.

The admission writer stays behind the Foundation writer gates. With
the gates off, an interactive task keeps the legacy path and no run
exists, so the orchestrator dispatches over the bearer execute route.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import database as db
import journal_backup
import run_admission
from core.asset_store import (
    AssetManifest,
    AssetManifestEntry,
    DataClass,
    TrustLevel,
)
from core.digest_profile import digest_hex, plain_json
from core.foundation_gates import WriterDisabledError, require_writer_gates
from core.run_context import PolicySet
from core.run_contracts import VersionSet
from core.variants import RuntimeKey, require_runtime

logger = logging.getLogger("bmas.daemon.interactive_admission")

CHECKPOINT_READER = "reader.checkpoint"
POLICY_DIGEST_DOMAIN = "policy-set-member"
SPECIFICATION_DIGEST_DOMAIN = "runtime-specification"
STORAGE_REPORT_TTL_SECONDS = 300.0
DEFAULT_BUDGET_CEILING_USD = 0.50
USD_MILLIONTHS = 1_000_000
_storage_report: tuple[float, dict[str, Any]] | None = None


def run_id_for_task(task_id: str) -> str:
    return f"run-{task_id}"


def task_fence_for(task_id: str) -> str:
    return f"fence-{task_id}"


def _policy_digest(name: str, value: Any) -> str:
    return digest_hex(POLICY_DIGEST_DOMAIN, {"policy": name, "value": plain_json(value)})


def policy_set_from_configuration() -> PolicySet:
    """Derive the policy set from the daemon configuration in force.

    Every member digests the configuration section that governs it, so
    a changed model pool, tool registry, or endpoint map yields a new
    policy set digest and the admission records which policies applied.
    """
    import config

    def section(name: str, default: Any = None) -> Any:
        return getattr(config, name, default)

    return PolicySet(
        schema_version="1",
        access_policy_digest=_policy_digest("access", {
            "operator_key_configured": bool(section("BMAS_API_KEY", "")),
            "node_key_configured": bool(section("BMAS_NODE_KEY", "")),
        }),
        model_policy_digest=_policy_digest("model", {
            "model_pools": section("MODEL_POOLS", {}),
            "model_routing": {str(key): value for key, value in (section("MODEL_ROUTING", {}) or {}).items()},
            "model_profiles": {str(alias): plain_json(profile) for alias, profile in (section("MODEL_PROFILES", {}) or {}).items()},
        }),
        tool_policy_digest=_policy_digest("tool", section("ROLE_REGISTRY", {})),
        environment_policy_digest=_policy_digest("environment", section("AGENT_ENDPOINTS", {})),
        source_trust_policy_digest=_policy_digest("source_trust", section("SOURCE_TRUST", {})),
        redaction_policy_digest=_policy_digest("redaction", {"redaction_policy_version": "1"}),
        retention_policy_digest=_policy_digest("retention", section("STORAGE", {})),
    )


def version_set_for(runtime_key: RuntimeKey) -> VersionSet:
    """The version set of the pair, with the live database schema version."""
    from capability_publication import CapabilityDirectory

    versions = dict(CapabilityDirectory().get(runtime_key).schema_versions)
    versions["database_schema_version"] = db.SCHEMA_VERSION
    return VersionSet(**versions)  # type: ignore[arg-type]


async def asset_manifest_for(task_id: str) -> AssetManifest:
    """The manifest of the task's uploaded files."""
    entries = []
    for record in await db.get_task_files(task_id):
        entries.append(AssetManifestEntry(
            asset_id=str(record["id"]),
            content_digest=str(record["sha256"] or ""),
            size_bytes=int(record["bytes"] or 0),
            media_type=str(record["mime"] or "application/octet-stream"),
            source="user-upload",
            data_class=DataClass.INTERNAL,
            trust_level=TrustLevel.UNTRUSTED,
            access_policy="task-scope",
            scanner_version="1",
            extraction_version="1",
        ))
    return AssetManifest(manifest_id=f"manifest-{task_id}", task_id=task_id, entries=tuple(entries))


async def storage_report() -> dict[str, Any]:
    """The storage readiness report, cached for a short time per process."""
    import config

    global _storage_report
    now = time.monotonic()
    if _storage_report is not None and now - _storage_report[0] < STORAGE_REPORT_TTL_SECONDS:
        return _storage_report[1]
    report = await journal_backup.storage_readiness(
        operator_confirmed_storage=bool(getattr(config, "STORAGE_OPERATOR_CONFIRMED", False)),
    )
    _storage_report = (now, report)
    return report


def reset_for_tests() -> None:
    global _storage_report
    _storage_report = None


async def required_qualification_ids(effective_configuration: dict[str, Any] | None) -> tuple[str, ...]:
    """The live qualification the task's routing needs, when required."""
    import config
    import qualification_service
    from core.model_parameters import profile_for_alias

    if not getattr(config, "REQUIRE_PROVIDER_QUALIFICATION", False):
        return ()
    routing = (effective_configuration or {}).get("model_routing") or {}
    alias = str(routing.get("medium") or getattr(config, "MODEL_ROUTING", {}).get("medium", "medium"))
    profile = profile_for_alias(alias)
    record = await qualification_service.check_admission(
        provider=profile.provider, model=profile.model, adapter="litellm",
    )
    return (str(record["qualification_id"]),)


def budget_ceiling_usd(effective_configuration: dict[str, Any] | None) -> float:
    configuration = effective_configuration or {}
    for holder in (configuration, configuration.get("classic") or {}, configuration.get("effort") or {}):
        value = holder.get("budget_ceiling_usd") if isinstance(holder, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    return DEFAULT_BUDGET_CEILING_USD


async def admit_task_run(
    *,
    task_id: str,
    runtime_key: RuntimeKey,
    effective_configuration: dict[str, Any] | None,
    tenant_id: str = "tenant-default",
    requested_seed: int | None = None,
    budget_ceiling: float | None = None,
    database_time: str | None = None,
) -> dict[str, Any] | None:
    """Admit the task's run once and return its identity, or None when gated.

    The call is idempotent: an existing run returns its stored identity.
    A disabled writer gate returns None and leaves the task on the
    legacy path. Any other admission failure raises.
    """
    import activation_service as activations

    run_id = run_id_for_task(task_id)
    fence = task_fence_for(task_id)
    # The gates decide before any database read, so a legacy deployment
    # never touches the Foundation tables for an interactive task.
    try:
        require_writer_gates("run_context", "runtime_unit_of_work", "budget_reservations")
    except WriterDisabledError as exc:
        logger.info("Task %s stays on the legacy path: %s", task_id, exc)
        return None
    try:
        identity = await activations.run_identity(run_id)
        control = await db.get_run_control(run_id)
        if control is None:
            await db.create_run_control(run_id, task_id, fence)
            control = await db.get_run_control(run_id)
        return {"run_id": run_id, "task_id": task_id, "runtime_key": identity,
                "task_fence": str(control["task_fence"]) if control else fence, "new": False}
    except activations.ActivationServiceError:
        pass
    runtime = require_runtime(runtime_key)
    ceiling = budget_ceiling if budget_ceiling is not None else budget_ceiling_usd(effective_configuration)
    limit_millionths = max(int(round(ceiling * USD_MILLIONTHS)), 1)
    policy_set = policy_set_from_configuration()
    manifest = await asset_manifest_for(task_id)
    descriptor = runtime.descriptor.to_dict()
    request = run_admission.AdmissionRequest(
        task_id=task_id,
        run_id=run_id,
        tenant_id=tenant_id,
        runtime_key=runtime_key,
        version_set=version_set_for(runtime_key),
        specification_digest=digest_hex(SPECIFICATION_DIGEST_DOMAIN, plain_json({
            "runtime_key": runtime_key.to_dict(),
            "effective_configuration": effective_configuration or {},
        })),
        capability_document_digest=digest_hex(SPECIFICATION_DIGEST_DOMAIN, plain_json(descriptor)),
        prompt_profile_digest=digest_hex(SPECIFICATION_DIGEST_DOMAIN, plain_json({
            "prompt_profile": (effective_configuration or {}).get("prompt_profile"),
        })),
        role_profile_digest=digest_hex(SPECIFICATION_DIGEST_DOMAIN, plain_json({
            "role_registry": (effective_configuration or {}).get("role_registry"),
        })),
        asset_manifest=manifest,
        asset_manifest_digest=manifest.digest(),
        policy_set=policy_set,
        policy_set_digest=policy_set.digest(),
        seed_policy="recorded",
        requested_seed=requested_seed,
        required_reader_ids=(CHECKPOINT_READER,) if runtime.descriptor.supports_recovery else (),
        required_qualification_ids=await required_qualification_ids(effective_configuration),
        budget_currency="USD",
        budget_limits=(
            run_admission.budget_service.LimitSpec(
                "run", run_id, "provider_cost", limit_millionths, currency="USD",
            ),
        ),
        initial_reservation_resources={"provider_cost": limit_millionths},
        # A legacy contract keeps its budget advisory: the reservation records
        # intent and the classic ledger stays the spend authority.
        budget_mode="permissive" if runtime_key.runtime_contract_version == "1" else "strict",
        task_fence=None,
    )
    available_readers = frozenset({CHECKPOINT_READER}) if runtime.descriptor.supports_recovery else frozenset()
    admitted = await run_admission.admit_run(
        request,
        available_reader_ids=available_readers,
        storage_report=await storage_report(),
        database_time=database_time,
    )
    if await db.get_run_control(run_id) is None:
        await db.create_run_control(run_id, task_id, fence, database_time=database_time)
    logger.info("Admitted task %s into run %s (%s/%s)", task_id, run_id,
                runtime_key.runtime_id, runtime_key.runtime_contract_version)
    return {
        "run_id": run_id,
        "task_id": task_id,
        "runtime_key": runtime_key.to_dict(),
        "task_fence": fence,
        "budget_id": admitted.get("run_budget_id"),
        "reservation_id": admitted.get("initial_reservation_id"),
        "admission": admitted,
        "new": True,
    }


async def reservation_for_run(run_id: str) -> str | None:
    """The reserved reservation of one run, for the activation grants."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT reservation_id FROM budget_reservations WHERE run_id = ? AND state = 'reserved' "
            "ORDER BY reservation_id LIMIT 1",
            (run_id,),
        )
        row = await cursor.fetchone()
    return str(row["reservation_id"]) if row is not None else None
