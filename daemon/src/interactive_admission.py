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

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from core.run_context import RunContext

logger = logging.getLogger("bmas.daemon.interactive_admission")

CHECKPOINT_READER = "reader.checkpoint"
POLICY_DIGEST_DOMAIN = "policy-set-member"
SPECIFICATION_DIGEST_DOMAIN = "runtime-specification"
STORAGE_REPORT_TTL_SECONDS = 300.0
DEFAULT_BUDGET_CEILING_USD = 0.50
USD_MILLIONTHS = 1_000_000
NANOS_PER_MILLIONTH = 1_000
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


def compile_specification_for(
    runtime: Any,
    effective_configuration: dict[str, Any] | None,
    *,
    run_id: str,
    asset_manifest_digest: str,
    qualification_ids: tuple[str, ...],
) -> Any | None:
    """Compile and promote the specification of a runtime that compiles one.

    A runtime class that declares ``compile_specification_for_admission``
    returns its stored specification. Every other runtime returns None
    and keeps the envelope digest. A compile failure is an admission
    prerequisite failure, so the task fails closed.
    """
    compile_hook = getattr(runtime, "compile_specification_for_admission", None)
    if compile_hook is None:
        return None
    try:
        return compile_hook(
            effective_configuration,
            run_id=run_id,
            asset_manifest_digest=asset_manifest_digest,
            qualification_ids=qualification_ids,
        )
    except ValueError as exc:
        raise run_admission.AdmissionPrerequisiteError(
            f"The specification did not compile: {exc}"
        ) from exc


def specification_row_writer(
    compiled: Any | None, *, task_id: str, run_id: str, runtime_key: RuntimeKey,
) -> Any | None:
    """The transaction write that stores one compiled specification row."""
    if compiled is None:
        return None
    spec = compiled.spec

    async def write_row(connection: Any, journal_cursor: int, txn_now: str) -> None:
        await db.insert_classic_specification(
            connection,
            specification_digest=compiled.specification_digest,
            run_id=run_id,
            task_id=task_id,
            runtime_id=runtime_key.runtime_id,
            runtime_contract_version=runtime_key.runtime_contract_version,
            schema_version=spec.runtime.runtime_spec_schema_version,
            artifact_digest=compiled.artifact_digest,
            fidelity_profile_id=spec.fidelity.profile_id,
            effort_profile_id=spec.effort.profile_id,
            requested_effort_level=spec.effort.requested_level,
            journal_cursor=journal_cursor,
            created_at=txn_now,
        )

    return write_row


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
    policy_set = policy_set_from_configuration()
    manifest = await asset_manifest_for(task_id)
    descriptor = runtime.descriptor.to_dict()
    qualification_ids = await required_qualification_ids(effective_configuration)
    # A runtime that compiles a specification binds the compiled digest
    # and its exact cost limit. The legacy pair keeps the digest of its
    # configuration envelope and the float ceiling.
    compiled = compile_specification_for(
        runtime, effective_configuration,
        run_id=run_id, asset_manifest_digest=manifest.digest(), qualification_ids=qualification_ids,
    )
    if compiled is not None:
        specification_digest = compiled.specification_digest
        limit_millionths = max(-(-compiled.spec.limits.max_cost.amount_nanos // NANOS_PER_MILLIONTH), 1)
    else:
        specification_digest = digest_hex(SPECIFICATION_DIGEST_DOMAIN, plain_json({
            "runtime_key": runtime_key.to_dict(),
            "effective_configuration": effective_configuration or {},
        }))
        ceiling = budget_ceiling if budget_ceiling is not None else budget_ceiling_usd(effective_configuration)
        limit_millionths = max(int(round(ceiling * USD_MILLIONTHS)), 1)
    request = run_admission.AdmissionRequest(
        task_id=task_id,
        run_id=run_id,
        tenant_id=tenant_id,
        runtime_key=runtime_key,
        version_set=version_set_for(runtime_key),
        specification_digest=specification_digest,
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
        required_qualification_ids=qualification_ids,
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
        extra_writes=specification_row_writer(compiled, task_id=task_id, run_id=run_id, runtime_key=runtime_key),
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


async def run_context_for(run_id: str, *, lease_ref: str) -> RunContext:
    """Rebuild the fenced run context of one admitted run from its durable rows.

    The ``runs`` row gives the identity, the ``runtime_admissions`` row
    gives the immutable admission, the admission journal record gives
    the policy set members, and the ``run_controls`` row gives the
    task fence. The context freezes references only: the lease and the
    run-control row stay live authorities that every mutation reads
    again.
    """
    from core.run_context import PolicySet, create_run_context
    from core.run_contracts import RuntimeAdmission

    run_row = await db.get_run(run_id)
    admission_row = await db.get_runtime_admission(run_id)
    control = await db.get_run_control(run_id)
    if run_row is None or admission_row is None or control is None:
        raise run_admission.AdmissionPrerequisiteError(
            f"The run {run_id} has no complete admission rows"
        )
    genesis = None
    for record in await runtime_journal_records(run_id):
        if record.operation_type == "admission_identity":
            genesis = record
            break
    if genesis is None:
        raise run_admission.AdmissionPrerequisiteError(
            f"The run {run_id} has no admission record in the journal"
        )
    payload = genesis.payload
    runtime_key = RuntimeKey(str(run_row["runtime_id"]), str(run_row["runtime_contract_version"]))
    stored_members = payload.get("policy_set")
    if isinstance(stored_members, dict):
        policy_set = PolicySet(**{str(key): str(value) for key, value in stored_members.items()})
    else:
        # A run admitted before the members travelled with the digest
        # rebuilds the set from the configuration in force. The digest
        # check below fails closed when that configuration changed.
        policy_set = policy_set_from_configuration()
    policy_set_digest = str(admission_row.get("policy_set_digest") or payload.get("policy_set_digest") or "")
    requested_seed = payload.get("requested_seed")
    admission = RuntimeAdmission(
        admission_id=str(admission_row["admission_id"]),
        task_id=str(run_row["task_id"]),
        run_id=run_id,
        runtime_key=runtime_key,
        version_set=VersionSet(**json.loads(str(admission_row["version_set"]))),
        specification_digest=str(admission_row["specification_digest"]),
        capability_document_digest=str(admission_row["capability_document_digest"]),
        prompt_profile_digest=str(payload.get("prompt_profile_digest") or ""),
        role_profile_digest=str(payload.get("role_profile_digest") or ""),
        seed_policy=str(payload.get("seed_policy") or "recorded"),
        requested_seed=requested_seed if isinstance(requested_seed, int) else None,
        required_reader_ids=(CHECKPOINT_READER,),
        interface_adapter_id=str(require_runtime(runtime_key).descriptor.id),
    )
    return create_run_context(
        admission=admission,
        policy_set=policy_set,
        policy_set_digest=policy_set_digest,
        asset_manifest_id=str(payload.get("asset_manifest_id") or f"manifest-{run_row['task_id']}"),
        asset_manifest_digest=str(payload.get("asset_manifest_digest") or ""),
        task_fence=str(control["task_fence"]),
        lease_ref=lease_ref,
        run_control_ref=run_id,
    )


async def runtime_journal_records(run_id: str) -> list[Any]:
    """The journal chain of one run, in order."""
    import runtime_journal

    return await runtime_journal.read_journal(run_id=run_id)


async def runtime_services_for(
    context: RunContext,
    *,
    lease_owner: str,
    lease_fence: str,
    lease_ttl_seconds: float,
    tenant_id: str = "tenant-default",
    artifact_root: Path | None = None,
) -> Any:
    """Wire the fenced runtime services of one run for its lease holder.

    The services bind the run's identity, the reason registry of its
    runtime, the asset manifest of its task, and an artifact store for
    the objects the runtime promotes. Every mutating service validates
    the live run-control row for this owner and fence.
    """
    import budget_service
    from core.asset_store import ArtifactStore, AssetCatalog
    from core.human_controls import HumanControlService
    from core.run_contracts import (
        InvalidationService,
        OutcomeLedger,
        ReasonRegistry,
        RunLedger,
        RunRecord,
        RunState,
    )
    from core.runtime_services import create_runtime_services

    run_row = await db.get_run(context.run_id)
    if run_row is None:
        raise run_admission.AdmissionPrerequisiteError(f"Unknown run: {context.run_id}")
    run_ledger = RunLedger()
    run_ledger.restore_run(RunRecord(
        run_id=context.run_id,
        task_id=context.task_id,
        tenant_id=str(run_row["tenant_id"]),
        runtime_key=context.runtime_key,
        state=RunState(str(run_row["state"])),
        attempt=int(run_row["attempt"]),
    ))
    runtime = require_runtime(context.runtime_key)
    registry_hook = getattr(runtime, "reason_registry", None)
    reason_registry = registry_hook() if callable(registry_hook) else ReasonRegistry()
    outcome_ledger = OutcomeLedger(run_ledger, reason_registry)
    invalidations = InvalidationService(
        run_ledger, outcome_ledger,
        authorized_authority_ids=frozenset(),
        policy_version="1",
        known_targets=frozenset(),
    )
    controls = HumanControlService(
        run_ledger=run_ledger,
        authorized_actor_ids=frozenset(),
        database_time=db.database_utc_now,
    )
    root = artifact_root or (Path(db.DB_PATH).parent / f"{context.runtime_key.runtime_id}-board")
    return create_runtime_services(
        run_id=context.run_id,
        lease_owner=lease_owner,
        lease_fence=lease_fence,
        scheduler=True,
        run_ledger=run_ledger,
        outcome_ledger=outcome_ledger,
        invalidations=invalidations,
        assets=AssetCatalog(await asset_manifest_for(context.task_id)),
        artifacts=ArtifactStore(root, tenant_id),
        controls=controls,
        lease_ttl_seconds=lease_ttl_seconds,
        reservation_validator=budget_service.reservation_is_valid,
    )


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
