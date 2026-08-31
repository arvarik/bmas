"""Foundation Stage 0E: the complete run admission gate.

A run admits through one unit-of-work transaction after every
prerequisite passes: runtime identity, required readers, the asset
manifest, the policy set, the qualifications, storage readiness, and
the budget. The transaction creates the run, the immutable admission,
the run budget with its initial reservation, the journal genesis
record, and the queue row — atomically, or not at all. A failed
admission keeps the task open, because nothing was persisted.

Stage 0E uses a fixed qualification fixture; a later stage creates
live qualification records, and the queue writer stays disabled until
the conformance stage passes.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import budget_service
import database as db
import runtime_journal
from core.digest_profile import digest_hex
from core.run_context import PolicySet, validate_policy_set_digest
from core.run_contracts import SEED_POLICIES, VersionSet
from core.variants import RuntimeKey, require_admissible_runtime

if TYPE_CHECKING:
    import aiosqlite

    from core.asset_store import AssetManifest


class AdmissionPrerequisiteError(ValueError):
    """One admission prerequisite failed before the transaction."""


class AdmissionReservationError(AdmissionPrerequisiteError):
    """The initial reservation did not fit the aggregate limits."""


@dataclass(frozen=True)
class QualificationRecord:
    """One qualification entry in the fixed Stage 0E fixture."""

    qualification_id: str
    state: str
    expires_at: str


@dataclass(frozen=True)
class AdmissionRequest:
    """Every input of one complete run admission."""

    task_id: str
    run_id: str
    tenant_id: str
    runtime_key: RuntimeKey
    version_set: VersionSet
    specification_digest: str
    capability_document_digest: str
    prompt_profile_digest: str
    role_profile_digest: str
    asset_manifest: AssetManifest
    asset_manifest_digest: str
    policy_set: PolicySet
    policy_set_digest: str
    seed_policy: str
    requested_seed: int | str | None
    required_reader_ids: tuple[str, ...]
    required_qualification_ids: tuple[str, ...]
    budget_currency: str
    budget_limits: tuple[budget_service.LimitSpec, ...]
    initial_reservation_resources: dict[str, int]
    budget_mode: str = "strict"
    task_fence: str | None = None
    admission_id: str = field(
        default_factory=lambda: f"admission-{uuid.uuid4()}",
    )


def _validate_prerequisites(
    request: AdmissionRequest,
    *,
    available_reader_ids: frozenset[str],
    qualification_fixture: dict[str, QualificationRecord],
    storage_report: dict[str, Any],
    database_time: str,
) -> None:
    """Validate every prerequisite before the admission transaction."""
    # Identity: the exact runtime pair must be qualified for admission.
    require_admissible_runtime(request.runtime_key)

    # Readers: every required reader must be available, without fallback.
    missing_readers = [
        reader
        for reader in request.required_reader_ids
        if reader not in available_reader_ids
    ]
    if missing_readers:
        raise AdmissionPrerequisiteError(
            f"Required readers are unavailable: {sorted(missing_readers)}"
        )

    # Assets: the authorized manifest must match its declared digest.
    if request.asset_manifest.digest() != request.asset_manifest_digest:
        raise AdmissionPrerequisiteError(
            "The asset manifest does not match its declared digest"
        )
    if request.asset_manifest.task_id != request.task_id:
        raise AdmissionPrerequisiteError(
            "The asset manifest belongs to another task"
        )

    # Policy: the policy set must match its declared digest.
    validate_policy_set_digest(request.policy_set, request.policy_set_digest)

    # Qualifications: every required record must be qualified and live.
    for qualification_id in request.required_qualification_ids:
        record = qualification_fixture.get(qualification_id)
        if record is None:
            raise AdmissionPrerequisiteError(
                f"The qualification {qualification_id!r} is missing"
            )
        if record.state != "qualified":
            raise AdmissionPrerequisiteError(
                f"The qualification {qualification_id!r} is "
                f"{record.state}"
            )
        if record.expires_at <= database_time:
            raise AdmissionPrerequisiteError(
                f"The qualification {qualification_id!r} expired"
            )

    # Storage: readiness must hold before a journal genesis writes.
    if not storage_report.get("ready"):
        raise AdmissionPrerequisiteError(
            "The storage readiness check rejected journal writers"
        )

    # Seed policy: one registered value.
    if request.seed_policy not in SEED_POLICIES:
        raise AdmissionPrerequisiteError(
            f"Unknown seed policy: {request.seed_policy!r}"
        )


async def admit_run(
    request: AdmissionRequest,
    *,
    available_reader_ids: frozenset[str],
    qualification_fixture: dict[str, QualificationRecord],
    storage_report: dict[str, Any],
    database_time: str | None = None,
) -> dict[str, Any]:
    """Admit one run through one atomic unit-of-work transaction.

    The transaction creates the run, the immutable admission with every
    final field, the run budget, the initial reservation in the
    reserved state, the journal genesis record, and the queue row. A
    reservation that does not fit the aggregate limits rejects the
    whole transaction, and nothing persists.
    """
    now = database_time or await db.database_utc_now()
    _validate_prerequisites(
        request,
        available_reader_ids=available_reader_ids,
        qualification_fixture=qualification_fixture,
        storage_report=storage_report,
        database_time=now,
    )

    budget_id = f"budget-{request.run_id}"
    reservation_id = f"reservation-{request.run_id}-genesis"
    payload = {
        "admission_id": request.admission_id,
        "version_set": request.version_set.to_dict(),
        "specification_digest": request.specification_digest,
        "capability_document_digest": request.capability_document_digest,
        "prompt_profile_digest": request.prompt_profile_digest,
        "role_profile_digest": request.role_profile_digest,
        "asset_manifest_id": request.asset_manifest.manifest_id,
        "asset_manifest_digest": request.asset_manifest_digest,
        "policy_set_digest": request.policy_set_digest,
        "seed_policy": request.seed_policy,
        "requested_seed": request.requested_seed,
        "qualification_ids": sorted(request.required_qualification_ids),
        "run_budget_id": budget_id,
        "initial_reservation_id": reservation_id,
    }
    payload["admission_digest"] = digest_hex("runtime-admission", payload)

    async def create_budget_rows(
        connection: aiosqlite.Connection, journal_cursor: int, txn_now: str,
    ) -> None:
        await budget_service.create_run_budget(
            connection,
            budget_id=budget_id,
            run_id=request.run_id,
            task_id=request.task_id,
            currency=request.budget_currency,
            limits=request.budget_limits,
            budget_mode=request.budget_mode,
            journal_cursor=journal_cursor,
        )
        await budget_service.insert_requested_reservation(
            connection,
            reservation_id=reservation_id,
            budget_id=budget_id,
            run_id=request.run_id,
            task_id=request.task_id,
            resources=request.initial_reservation_resources,
            currency=request.budget_currency,
            now=txn_now,
        )
        reservation = await budget_service._load_reservation(  # noqa: SLF001
            connection, reservation_id,
        )
        fits = await budget_service.compare_and_reserve_limits(
            connection, reservation,
        )
        if not fits:
            raise AdmissionReservationError(
                "The initial reservation does not fit the aggregate limits"
            )
        await connection.execute(
            "UPDATE budget_reservations SET state = 'reserved', "
            "reserved_amount_nanos = requested_amount_nanos, "
            "state_changed_at = ? WHERE reservation_id = ?",
            (txn_now, reservation_id),
        )

    operation = runtime_journal.JournalOperation(
        operation_type="admission_identity",
        task_id=request.task_id,
        run_id=request.run_id,
        runtime_id=request.runtime_key.runtime_id,
        runtime_contract_version=(
            request.runtime_key.runtime_contract_version
        ),
        payload=payload,
        idempotency_token=f"admission-{request.admission_id}",
        tenant_id=request.tenant_id,
        task_fence=request.task_fence,
    )
    record = await runtime_journal.commit_operation(
        operation, database_time=now, extra_writes=create_budget_rows,
    )
    return {
        "journal_record": record,
        "admission_id": request.admission_id,
        "admission_digest": payload["admission_digest"],
        "run_budget_id": budget_id,
        "initial_reservation_id": reservation_id,
    }
