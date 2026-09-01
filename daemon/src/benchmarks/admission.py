"""Protect benchmark task admission through the shared effect ledger.

Every benchmark attempt admits its task through the Foundation effect
ledger with the ``benchmark_admission`` effect kind. The chain commits
the admission intent, reserves the maximum task cost through the
Foundation budget ledger, approves after the fence and budget checks,
commits the dispatch outbox row, claims it through the shared
dispatcher, stores the raw admission response before parsing, and
links the task to the attempt under the active attempt fence. There is
no benchmark-only effect ledger.

The stable idempotency key is the benchmark attempt identifier, and
the task identifier derives deterministically from it, so a crash at
any point creates no second task and recovery links the original task
by querying the stable key.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

import activation_service as activations
import budget_service as budget
import database as db
import effect_service as effects
import runtime_journal as journal
from benchmarks import costs, repository
from benchmarks.provenance import content_checksum
from core.asset_store import ArtifactStore
from core.failpoints import failpoint
from core.signing import KeyRegistry, SigningKeyRecord, public_bytes_of

if TYPE_CHECKING:
    from routes.submit import TaskSubmission

ADMISSION_SCOPE = "benchmark-admission"
ADMISSION_RUNTIME_ID = "benchmark-scheduler"
ADMISSION_AGENT_ID = "benchmark-scheduler"
ADMISSION_AUDIENCE = "bmas-daemon"
ADMISSION_TARGET = "task-service"
ADMISSION_KEY_ID = "benchmark-admission-key"
ADMISSION_KEY_NOT_BEFORE = "2000-01-01T00:00:00.000Z"
GRANT_TTL_SECONDS = 3600.0
CLAIM_TTL_SECONDS = 3600.0


class BudgetBlockedError(RuntimeError):
    """No valid budget reservation fits; admission must wait."""


class AdmissionUnknownError(RuntimeError):
    """The admission outcome stays unknown and needs reconciliation."""


# The scheduler is both the dispatcher and the accepting agent for
# its admission anchor, so both keys live with the process and
# register at first use. The anchor handshake completes in one call,
# so a restart never needs to verify another process's grant.
ADMISSION_AGENT_KEY_ID = "benchmark-admission-agent-key"
_keyring: dict[str, Any] | None = None


def _keys() -> dict[str, Any]:
    global _keyring
    if _keyring is None:
        daemon_key = Ed25519PrivateKey.generate()
        agent_key = Ed25519PrivateKey.generate()
        registry = KeyRegistry()
        registry.register(
            SigningKeyRecord(
                key_id=ADMISSION_KEY_ID,
                owner_id="daemon",
                purpose="daemon-grant",
                public_bytes=public_bytes_of(daemon_key),
                not_before=ADMISSION_KEY_NOT_BEFORE,
            ),
        )
        registry.register(
            SigningKeyRecord(
                key_id=ADMISSION_AGENT_KEY_ID,
                owner_id=ADMISSION_AGENT_ID,
                purpose="agent-receipt",
                public_bytes=public_bytes_of(agent_key),
                not_before=ADMISSION_KEY_NOT_BEFORE,
            ),
        )
        _keyring = {
            "daemon": daemon_key,
            "agent": agent_key,
            "registry": registry,
        }
    return _keyring


def _artifact_store() -> ArtifactStore:
    root = Path(db.DB_PATH).parent / "benchmark-admission-artifacts"
    return ArtifactStore(root, "tenant-default")


def admission_task_id(attempt_id: str) -> str:
    """Derive the deterministic task identifier for one attempt."""
    return "task-benchmark-" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"bmas:benchmark:{attempt_id}",
    ).hex


def authority_run_id(run_id: str) -> str:
    """Name the Foundation journal run that anchors one benchmark run."""
    return f"benchmark-authority-{run_id}"


def authority_fence(run_id: str) -> str:
    """Name the durable admission fence for one benchmark run."""
    return f"benchmark-fence-{run_id}"


def _authority_activation(run_id: str) -> str:
    return f"benchmark-admission-{run_id}"


async def ensure_run_authority(attempt: dict[str, Any]) -> dict[str, Any]:
    """Create the Foundation anchor for one benchmark run exactly once.

    The anchor holds one journal run, one durable run control with the
    admission fence, one run budget with the declared cost limit, and
    one claimed admission activation. Every step checks existing state
    first, so a crash between steps recovers idempotently.
    """
    run_id = str(attempt["run_id"])
    journal_run = str(attempt.get("authority_run_id") or "") or (
        authority_run_id(run_id)
    )
    fence = str(attempt.get("authority_fence") or "") or authority_fence(
        run_id,
    )
    budget_id = str(attempt.get("run_budget_id") or "") or (
        f"benchmark-budget-{run_id}"
    )
    task_anchor = f"benchmark-run-{run_id}"

    try:
        await activations.run_identity(journal_run)
    except activations.ActivationServiceError:
        payload = {
            "admission_id": f"admission-{journal_run}",
            "version_set": {"checkpoint_schema_version": "1"},
            "specification_digest": content_checksum(
                {"benchmark_run_id": run_id},
            ),
            "capability_document_digest": content_checksum(
                {"agent_id": ADMISSION_AGENT_ID},
            ),
            "admission_digest": content_checksum(
                {"benchmark_run_id": run_id, "fence": fence},
            ),
        }
        await journal.commit_operation(
            journal.JournalOperation(
                operation_type="admission_identity",
                task_id=task_anchor,
                run_id=journal_run,
                runtime_id=ADMISSION_RUNTIME_ID,
                runtime_contract_version="1",
                payload=payload,
                idempotency_token=f"admission-{journal_run}",
            ),
        )
    if await db.get_run_control(journal_run) is None:
        await db.create_run_control(journal_run, task_anchor, fence)

    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT budget_id FROM run_budgets WHERE budget_id = ?",
            (budget_id,),
        )
        existing_budget = await cursor.fetchone()
    if existing_budget is None:
        configuration = attempt.get("test_configuration") or {}
        limit_nanos = costs.budget_limit_nanos(configuration)
        async with db._connect() as connection:  # noqa: SLF001
            await budget.create_run_budget(
                connection,
                budget_id=budget_id,
                run_id=journal_run,
                task_id=task_anchor,
                currency=costs.BENCHMARK_CURRENCY,
                limits=(
                    budget.LimitSpec(
                        "run",
                        journal_run,
                        "provider_cost",
                        limit_nanos,
                        currency=costs.BENCHMARK_CURRENCY,
                    ),
                ),
            )
            await connection.commit()

    activation_id = _authority_activation(run_id)
    try:
        activation = await activations.get_activation(activation_id, 1)
    except activations.ActivationServiceError:
        await activations.create_activation(
            run_id=journal_run,
            activation_id=activation_id,
            attempt=1,
            request_digest=content_checksum({"benchmark_run_id": run_id}),
            context_view_digest=content_checksum({"scope": ADMISSION_SCOPE}),
            task_fence=fence,
        )
        activation = await activations.get_activation(activation_id, 1)
    if str(activation["state"]) != "dispatched":
        await _dispatch_authority_activation(
            journal_run=journal_run,
            fence=fence,
            budget_id=budget_id,
            activation=activation,
        )

    await repository.set_run_authority(
        run_id,
        authority_run_id=journal_run,
        authority_fence=fence,
        budget_id=budget_id,
    )
    return {
        "journal_run": journal_run,
        "fence": fence,
        "budget_id": budget_id,
        "activation_id": activation_id,
    }


async def _dispatch_authority_activation(
    *,
    journal_run: str,
    fence: str,
    budget_id: str,
    activation: dict[str, Any],
) -> None:
    """Drive the anchor activation to ``dispatched`` idempotently.

    The scheduler queues the signed activation grant, claims its own
    dispatch, and accepts with a signed acknowledgement, so the shared
    ledger's nested-effect rules hold for benchmark admissions without
    any external agent. Each step resumes from the recorded state.
    """
    import agent_protocol as protocol

    keys = _keys()
    store = _artifact_store()
    activation_id = str(activation["activation_id"])
    state = str(activation["state"])
    if state == "queued":
        await activations.claim_activation(
            run_id=journal_run,
            activation_id=activation_id,
            attempt=1,
            owner=ADMISSION_AGENT_ID,
            lease_ttl_seconds=GRANT_TTL_SECONDS,
            task_fence=fence,
        )
        activation = await activations.get_activation(activation_id, 1)
        state = str(activation["state"])
    grant_id = f"benchmark-activation-grant-{activation_id}"
    if state == "leased":
        reservation_id = (
            f"benchmark-activation-reservation-{activation_id}"
        )
        try:
            record = await budget.get_reservation(reservation_id)
        except budget.BudgetError:
            await budget.request_reservation(
                reservation_id=reservation_id,
                budget_id=budget_id,
                resources={"provider_cost": 1},
            )
            record = await budget.get_reservation(reservation_id)
        if str(record["state"]) == "requested" and not await budget.reserve(
            reservation_id,
        ):
            raise BudgetBlockedError(
                "The admission anchor reservation does not fit"
            )
        queued = await activations.queue_activation_dispatch(
            run_id=journal_run,
            activation_id=activation_id,
            attempt=1,
            agent_id=ADMISSION_AGENT_ID,
            audience=ADMISSION_AUDIENCE,
            agent_protocol_version=_protocol_version(),
            request_digest=str(activation["request_digest"]),
            context_view_digest=str(activation["context_view_digest"]),
            task_fence=fence,
            lease_id=str(activation["lease_id"]),
            owner=ADMISSION_AGENT_ID,
            reservation_id=reservation_id,
            daemon_private_key=keys["daemon"],
            key_id=ADMISSION_KEY_ID,
            key_registry=keys["registry"],
            artifact_store=store,
            grant_ttl_seconds=GRANT_TTL_SECONDS,
            grant_id=grant_id,
        )
        grant = queued["grant"]
        grant_digest = str(queued["grant_artifact_digest"])
    elif state == "dispatch_queued":
        grant_row = await activations.get_grant_row(grant_id)
        grant_digest = str(grant_row["grant_artifact_digest"])
        stored = store.read_object(grant_digest)
        grant = _parse_stored_grant(stored["payload"])
    else:
        raise AdmissionUnknownError(
            f"The admission anchor activation is {state}; an operator "
            "must inspect it"
        )
    claimed = await activations.claim_activation_dispatch(
        grant_id=grant.activation_grant_id,
        run_id=journal_run,
        dispatcher=ADMISSION_AGENT_ID,
        claim_ttl_seconds=CLAIM_TTL_SECONDS,
        key_registry=keys["registry"],
        artifact_store=store,
        expected_target_agent_id=ADMISSION_AGENT_ID,
        task_fence=fence,
    )
    await activations.record_send_start(
        grant_id=grant.activation_grant_id,
        claim_owner=str(claimed["claim_owner"]),
        claim_fence=str(claimed["claim_fence"]),
    )
    acknowledgement = protocol.sign_acknowledgement(
        {
            "schema_version": "1",
            "acknowledgement_id": (
                f"acknowledgement-{grant.activation_grant_id}"
            ),
            "activation_grant_id": grant.activation_grant_id,
            "activation_grant_digest": grant_digest,
            "task_id": grant.task_id,
            "run_id": grant.run_id,
            "runtime_key": grant.runtime_key,
            "activation_id": grant.activation_id,
            "attempt": grant.attempt,
            "task_fence": grant.task_fence,
            "activation_fence": grant.activation_fence,
            "agent_id": ADMISSION_AGENT_ID,
            "audience": ADMISSION_AUDIENCE,
            "agent_protocol_version": _protocol_version(),
            "capability_digest": content_checksum(
                {"agent_id": ADMISSION_AGENT_ID},
            ),
            "decision": "accepted",
            "decision_reason_code": "accepted",
            "agent_execution_id": None,
            "grant_nonce": grant.grant_nonce,
            "agent_observed_at": await db.database_utc_now(),
            "key_id": ADMISSION_AGENT_KEY_ID,
        },
        keys["agent"],
    )
    await activations.process_acknowledgement(
        text=acknowledgement.to_bytes().decode("utf-8"),
        key_registry=keys["registry"],
        task_fence=fence,
    )


def _parse_stored_grant(payload: bytes) -> Any:
    """Rebuild one stored activation grant from its exact bytes."""
    import agent_protocol as protocol
    from core.variants import RuntimeKey

    fields = json.loads(payload.decode("utf-8"))
    fields["runtime_key"] = RuntimeKey(**fields["runtime_key"])
    return protocol.ActivationGrant(**fields)


def build_submission(attempt: dict[str, Any]) -> TaskSubmission:
    """Build the deterministic task submission for one attempt.

    The submission carries the shared item and repetition seed, the
    seed-control label, the stable admission key, and the request
    digest, so the task service can reject an equal key with a
    different request.
    """
    from routes.submit import (
        BenchmarkContext,
        TaskOverrides,
        TaskSubmission,
    )

    arm = attempt.get("arm_configuration") or {}
    overrides = arm.get("submission_overrides") or {}
    attempt_id = str(attempt["id"])
    core = {
        "task": str(attempt["input"]),
        "variant": str(attempt["runtime_id"]),
        "overrides": overrides,
        "run_id": str(attempt["run_id"]),
        "trial_id": str(attempt["trial_id"]),
        "attempt_id": attempt_id,
        "random_seed": attempt.get("random_seed"),
        "seed_control": str(attempt.get("seed_control") or "recorded"),
        "captured_configuration": arm.get("effective_configuration"),
        "task_id": admission_task_id(attempt_id),
    }
    digest = content_checksum(core)
    return TaskSubmission(
        task=core["task"],
        variant=core["variant"],
        overrides=(
            TaskOverrides.model_validate(overrides) if overrides else None
        ),
        benchmark=BenchmarkContext(
            run_id=core["run_id"],
            trial_id=core["trial_id"],
            attempt_id=attempt_id,
            random_seed=attempt.get("random_seed"),
            seed_control=core["seed_control"],
            admission_key=attempt_id,
            request_digest=digest,
        ),
    )


def request_digest_for(attempt: dict[str, Any]) -> str:
    """Return the stable request digest for one attempt admission."""
    submission = build_submission(attempt)
    assert submission.benchmark is not None
    return submission.benchmark.request_digest or ""


async def _reserve_attempt_cost(
    attempt: dict[str, Any],
    authority: dict[str, Any],
    reservation_id: str,
) -> None:
    """Reserve the maximum task cost or report the budget block."""
    try:
        record = await budget.get_reservation(reservation_id)
    except budget.BudgetError:
        configuration = attempt.get("test_configuration") or {}
        amount = costs.attempt_reservation_amount(
            configuration,
            int(attempt.get("run_total_attempts") or 1),
        )
        await budget.request_reservation(
            reservation_id=reservation_id,
            budget_id=authority["budget_id"],
            resources={"provider_cost": amount.amount_nanos},
            provider=ADMISSION_TARGET,
        )
        record = await budget.get_reservation(reservation_id)
    state = str(record["state"])
    if state == "reserved":
        return
    if state != "requested" or not await budget.reserve(reservation_id):
        raise BudgetBlockedError(
            "No valid budget reservation fits this admission"
        )


async def _call_task_service(
    attempt: dict[str, Any],
    authority: dict[str, Any],
    effect_id: str,
) -> dict[str, str]:
    """Send the admission and store the raw response before parsing."""
    from routes.submit import _admit_task

    submission = build_submission(attempt)
    task_id = admission_task_id(str(attempt["id"]))
    failpoint("benchmark.before_task_call")
    try:
        response = await _admit_task(
            submission,
            captured_configuration=(
                (attempt.get("arm_configuration") or {}).get(
                    "effective_configuration",
                )
            ),
            task_id=task_id,
        )
    except Exception as error:
        # A raised admission is a delivered rejection: the raw
        # rejection stores before any interpretation, the outcome
        # observes as rejected, and the reservation releases.
        raw = json.dumps(
            {"error": str(error)}, separators=(",", ":"), sort_keys=True,
        ).encode()
        await effects.observe_response(
            run_id=authority["journal_run"],
            effect_id=effect_id,
            raw_response=raw,
            artifact_store=_artifact_store(),
            outcome="rejected",
            task_fence=authority["fence"],
        )
        await effects.reconcile_effect(
            run_id=authority["journal_run"],
            effect_id=effect_id,
            usage={"provider_cost": 0},
            set_authoritative=False,
            task_fence=authority["fence"],
        )
        raise
    failpoint("benchmark.after_task_call")
    raw = json.dumps(
        response, separators=(",", ":"), sort_keys=True, default=str,
    ).encode()
    await effects.observe_response(
        run_id=authority["journal_run"],
        effect_id=effect_id,
        raw_response=raw,
        artifact_store=_artifact_store(),
        outcome="admitted",
        task_fence=authority["fence"],
    )
    return response


async def _link_task(
    attempt: dict[str, Any], response: dict[str, str],
) -> None:
    """Attach the admitted task under the active attempt fence."""
    failpoint("benchmark.before_attempt_link")
    task_id = str(response["task_id"])
    metadata = await db.get_board_meta(task_id)
    snapshot = {
        "schema_version": "1",
        "benchmark_plan": attempt.get("execution_snapshot") or {},
        "task_execution": metadata.get("execution_snapshot") or {},
    }
    await repository.attach_attempt_task(
        str(attempt["id"]),
        task_id,
        snapshot,
        content_checksum(snapshot),
        lease_token=str(attempt["lease_token"]),
    )


async def admit_attempt(attempt: dict[str, Any]) -> dict[str, str]:
    """Admit one attempt through the shared effect ledger.

    The chain resumes idempotently from the recorded effect state, so
    a crash at any boundary continues without a second task. The
    stable key is the attempt identifier; an equal key with a
    different request digest fails closed.
    """
    authority = await ensure_run_authority(attempt)
    attempt_id = str(attempt["id"])
    digest = request_digest_for(attempt)
    reservation_id = f"benchmark-reservation-{attempt_id}"
    intent = await effects.create_effect_intent(
        run_id=authority["journal_run"],
        activation_id=authority["activation_id"],
        activation_attempt=1,
        kind="benchmark_admission",
        request_digest=digest,
        idempotency_scope=ADMISSION_SCOPE,
        child_idempotency_key=attempt_id,
        reservation_id=reservation_id,
        retry_safety="safe",
        provider_operation_key=admission_task_id(attempt_id),
        task_fence=authority["fence"],
    )
    effect_id = str(intent["effect_id"])
    for _ in range(8):
        record = await effects.get_attempt(effect_id)
        state = str(record["state"])
        if state == "intent":
            await _reserve_attempt_cost(
                attempt, authority, str(record["reservation_id"]),
            )
            await effects.approve_effect(
                run_id=authority["journal_run"],
                effect_id=effect_id,
                task_fence=authority["fence"],
            )
        elif state == "approved":
            await effects.queue_effect_dispatch(
                run_id=authority["journal_run"],
                effect_id=effect_id,
                target=ADMISSION_TARGET,
                task_fence=authority["fence"],
            )
        elif state == "dispatch_queued":
            keys = _keys()
            await effects.claim_effect_dispatch(
                run_id=authority["journal_run"],
                effect_id=effect_id,
                dispatcher=ADMISSION_AGENT_ID,
                claim_ttl_seconds=CLAIM_TTL_SECONDS,
                grant_ttl_seconds=GRANT_TTL_SECONDS,
                daemon_private_key=keys["daemon"],
                key_id=ADMISSION_KEY_ID,
                key_registry=keys["registry"],
                artifact_store=_artifact_store(),
                agent_id=ADMISSION_AGENT_ID,
                audience=ADMISSION_AUDIENCE,
                protocol_version=(
                    _protocol_version()
                ),
                capability_digest=content_checksum(
                    {"agent_id": ADMISSION_AGENT_ID},
                ),
                operation="benchmark_admission",
                max_authorized_amount_nanos=await _reserved_nanos(record),
                task_fence=authority["fence"],
            )
        elif state == "dispatch_claimed":
            dispatch = await effects.get_effect_dispatch(
                str(record["dispatch_ref"]),
            )
            if dispatch.get("transport_started_at") is None:
                await effects.validate_before_transport(
                    dispatch_ref=str(record["dispatch_ref"]),
                    dispatcher=ADMISSION_AGENT_ID,
                )
                await effects.record_transport_start(
                    dispatch_ref=str(record["dispatch_ref"]),
                    dispatcher=ADMISSION_AGENT_ID,
                )
                response = await _call_task_service(
                    attempt, authority, effect_id,
                )
                await _link_task(attempt, response)
                return response
            # Transport already started, then crashed. The stable key
            # resolves the outcome through the authoritative lookup.
            return await _recover_by_lookup(
                attempt, authority, effect_id,
            )
        elif state == "observed":
            if str(record.get("observed_outcome") or "") == "rejected":
                # A crash landed between the observed rejection and
                # its reconciliation. Reconcile now; the loop then
                # opens a safe retry from the reconciled state.
                await effects.reconcile_effect(
                    run_id=authority["journal_run"],
                    effect_id=effect_id,
                    usage={"provider_cost": 0},
                    set_authoritative=False,
                    task_fence=authority["fence"],
                )
                continue
            response = {
                "task_id": admission_task_id(attempt_id),
                "variant": str(attempt["runtime_id"]),
                "status": "queued",
            }
            await _link_task(attempt, response)
            return response
        elif state == "outcome_unknown":
            return await _recover_by_lookup(
                attempt, authority, effect_id,
            )
        elif state == "reconciled":
            # A reconciled rejection ended without an admitted task,
            # but a linked task may already exist after a crash between
            # linking and requeue. The authoritative lookup decides.
            existing_task = await db.get_task(
                admission_task_id(attempt_id),
            )
            if existing_task is not None:
                return await _recover_by_lookup(
                    attempt, authority, effect_id,
                )
            # A safe retry opens the next effect attempt under the
            # same operation and request digest.
            retry = await effects.retry_effect(
                run_id=authority["journal_run"],
                predecessor_effect_id=effect_id,
                reservation_id=(
                    f"benchmark-reservation-{attempt_id}-retry-"
                    f"{int(record['effect_attempt_number']) + 1}"
                ),
                adapter_capabilities=_ADAPTER,
                requested_by=ADMISSION_AGENT_ID,
                task_fence=authority["fence"],
            )
            effect_id = str(retry["effect_id"])
        else:
            raise AdmissionUnknownError(
                f"The admission effect ended in state {state}"
            )
    raise AdmissionUnknownError(
        "The admission chain did not converge; an operator must "
        "inspect the effect"
    )


async def _recover_by_lookup(
    attempt: dict[str, Any],
    authority: dict[str, Any],
    effect_id: str,
) -> dict[str, str]:
    """Resolve one uncertain admission through the durable task store.

    The task store is the authoritative in-process admission target,
    so a present row proves delivery and a missing row proves
    nondelivery. Without that proof the effect would stay unknown for
    the operator.
    """
    attempt_id = str(attempt["id"])
    task_id = admission_task_id(attempt_id)
    task = await db.get_task(task_id)
    record = await effects.get_attempt(effect_id)
    if task is not None:
        if str(record["state"]) in {"dispatch_claimed", "outcome_unknown"}:
            await effects.observe_via_lookup(
                run_id=authority["journal_run"],
                effect_id=effect_id,
                lookup_evidence=f"task-store:{task_id}",
                outcome="admitted",
                task_fence=authority["fence"],
            )
        response = {
            "task_id": task_id,
            "variant": str(task.get("variant") or attempt["runtime_id"]),
            "status": str(task.get("status") or "queued"),
        }
        await _link_task(attempt, response)
        return response
    # Proven nondelivery: the durable task store is authoritative, so
    # a missing row is delivery proof. The lookup observes the
    # nondelivery, the reconciliation releases the reservation at zero
    # cost, and a safe retry opens the next attempt.
    if str(record["state"]) == "dispatch_claimed":
        await effects.mark_outcome_unknown(
            run_id=authority["journal_run"],
            effect_id=effect_id,
            reason="admission_crash_before_response",
            task_fence=authority["fence"],
        )
        record = await effects.get_attempt(effect_id)
    if str(record["state"]) == "outcome_unknown":
        await effects.observe_via_lookup(
            run_id=authority["journal_run"],
            effect_id=effect_id,
            lookup_evidence=f"task-store-miss:{task_id}",
            outcome="not_delivered",
            task_fence=authority["fence"],
        )
        await effects.reconcile_effect(
            run_id=authority["journal_run"],
            effect_id=effect_id,
            usage={"provider_cost": 0},
            set_authoritative=False,
            task_fence=authority["fence"],
        )
    retry = await effects.retry_effect(
        run_id=authority["journal_run"],
        predecessor_effect_id=effect_id,
        reservation_id=(
            f"benchmark-reservation-{attempt_id}-retry-"
            f"{int(record['effect_attempt_number']) + 1}"
        ),
        adapter_capabilities=_ADAPTER,
        requested_by=ADMISSION_AGENT_ID,
        task_fence=authority["fence"],
    )
    return await _continue_retry(
        dict(attempt), authority, str(retry["effect_id"]),
    )


async def _continue_retry(
    attempt: dict[str, Any],
    authority: dict[str, Any],
    effect_id: str,
) -> dict[str, str]:
    """Drive one fresh retry attempt from intent to a linked task."""
    record = await effects.get_attempt(effect_id)
    await _reserve_attempt_cost(
        attempt, authority, str(record["reservation_id"]),
    )
    await effects.approve_effect(
        run_id=authority["journal_run"],
        effect_id=effect_id,
        task_fence=authority["fence"],
    )
    await effects.queue_effect_dispatch(
        run_id=authority["journal_run"],
        effect_id=effect_id,
        target=ADMISSION_TARGET,
        task_fence=authority["fence"],
    )
    keys = _keys()
    await effects.claim_effect_dispatch(
        run_id=authority["journal_run"],
        effect_id=effect_id,
        dispatcher=ADMISSION_AGENT_ID,
        claim_ttl_seconds=CLAIM_TTL_SECONDS,
        grant_ttl_seconds=GRANT_TTL_SECONDS,
        daemon_private_key=keys["daemon"],
        key_id=ADMISSION_KEY_ID,
        key_registry=keys["registry"],
        artifact_store=_artifact_store(),
        agent_id=ADMISSION_AGENT_ID,
        audience=ADMISSION_AUDIENCE,
        protocol_version=_protocol_version(),
        capability_digest=content_checksum(
            {"agent_id": ADMISSION_AGENT_ID},
        ),
        operation="benchmark_admission",
        max_authorized_amount_nanos=await _reserved_nanos(record),
        task_fence=authority["fence"],
    )
    await effects.validate_before_transport(
        dispatch_ref=str(record["dispatch_ref"]),
        dispatcher=ADMISSION_AGENT_ID,
    )
    await effects.record_transport_start(
        dispatch_ref=str(record["dispatch_ref"]),
        dispatcher=ADMISSION_AGENT_ID,
    )
    response = await _call_task_service(attempt, authority, effect_id)
    await _link_task(attempt, response)
    return response


def _protocol_version() -> str:
    import agent_protocol as protocol

    return protocol.CURRENT_AGENT_PROTOCOL_VERSION


async def _reserved_nanos(record: dict[str, Any]) -> int:
    """Read the authorized maximum from the reservation itself."""
    reservation = await budget.get_reservation(
        str(record["reservation_id"]),
    )
    return int(reservation["requested_amount_nanos"])


_ADAPTER = effects.AdapterCapabilities(
    adapter_id="benchmark-task-service",
    adapter_version="1",
    idempotency_key_scope="provider-operation-key",
    idempotency_retention="unbounded",
    provider_run_lookup=True,
    result_retrieval=True,
    cancellation_semantics="acknowledged",
    compensation_support="none",
    provider_receipt_support=False,
    usage_finalization="late",
    retry_safety="safe",
)


async def record_admission_link(attempt: dict[str, Any]) -> None:
    """Persist the effect and reservation link on the attempt row."""
    record = await _latest_admission_effect(str(attempt["id"]))
    if record is None:
        return
    await repository.record_attempt_admission(
        str(attempt["id"]),
        effect_id=str(record["effect_id"]),
        reservation_id=str(record["reservation_id"]),
        lease_token=str(attempt.get("lease_token") or "") or None,
    )


async def _latest_admission_effect(
    attempt_id: str,
) -> dict[str, Any] | None:
    """Query the shared ledger by the stable admission key."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT attempt.* FROM effect_attempts AS attempt "
            "JOIN effect_operations AS operation "
            "ON operation.effect_operation_id = attempt.effect_operation_id "
            "WHERE operation.idempotency_scope = ? "
            "AND operation.child_idempotency_key = ? "
            "ORDER BY attempt.effect_attempt_number DESC LIMIT 1",
            (ADMISSION_SCOPE, attempt_id),
        )
        row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def settle_attempt_admission(attempt: dict[str, Any]) -> None:
    """Reconcile one finished attempt's admission cost exactly once.

    Observed task usage reconciles the reservation and releases the
    unused remainder; the reconciliation records any overshoot.
    Missing usage consumes the pessimistic reservation and records an
    unknown charge, which never becomes zero.
    """
    attempt_id = str(attempt["id"])
    record = await _latest_admission_effect(attempt_id)
    if record is None or str(record["state"]) != "observed":
        return
    run_id = str(attempt["run_id"])
    run = await repository.get_run(run_id)
    if run is None:
        return
    journal_run = str(run.get("authority_run_id") or "")
    fence = str(run.get("authority_fence") or "")
    if not journal_run:
        return
    raw_cost = attempt.get("total_cost_usd")
    usage: dict[str, int] | None = None
    if raw_cost is not None:
        adapted = costs.legacy_cost_adapter(float(raw_cost))
        assert adapted is not None
        usage = {"provider_cost": adapted["money"]["amount_nanos"]}
        await repository.record_cost_charge(
            run_id,
            kind="charge",
            currency=costs.BENCHMARK_CURRENCY,
            amount_nanos=adapted["money"]["amount_nanos"],
            attempt_id=attempt_id,
            provider=ADMISSION_TARGET,
            source_text=adapted["source_text"],
            source_kind="legacy_float",
            evidence={"authoritative": False, "source": "task_total_cost"},
        )
    else:
        await repository.record_cost_charge(
            run_id,
            kind="unknown",
            currency=costs.BENCHMARK_CURRENCY,
            amount_nanos=None,
            attempt_id=attempt_id,
            provider=ADMISSION_TARGET,
            source_kind="none",
            evidence={"reason": "The task reported no usage"},
        )
    await effects.reconcile_effect(
        run_id=journal_run,
        effect_id=str(record["effect_id"]),
        usage=usage,
        task_fence=fence,
    )


async def try_settle_run(run_id: str) -> bool:
    """Move one settling run to settled when every charge is final.

    Settlement needs every admission effect reconciled and every
    unknown charge either bounded or operator-accepted. An unbounded
    unknown amount keeps the run in settling and keeps a cost gate
    blocked.
    """
    run = await repository.get_run(run_id)
    if run is None or str(run.get("cost_status") or "") != "settling":
        return False
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT COUNT(*) AS open_effects FROM benchmark_attempts "
            "AS attempt JOIN benchmark_trials AS trial "
            "ON trial.id = attempt.trial_id "
            "JOIN effect_attempts AS effect "
            "ON effect.effect_id = attempt.admission_effect_id "
            "WHERE trial.run_id = ? AND effect.state NOT IN "
            "('reconciled','denied','cancelled')",
            (run_id,),
        )
        row = await cursor.fetchone()
        assert row is not None  # An aggregate query returns one row.
        open_effects = int(row["open_effects"])
    if open_effects:
        return False
    charges = await repository.list_cost_charges(run_id)
    summary = costs.summarize_charges(charges)
    unknown_open = [
        charge
        for charge in charges
        if charge["kind"] == "unknown"
        and not charge.get("evidence", {}).get("accepted_by")
    ]
    if summary["unbounded_unknown"] or unknown_open:
        return False
    await repository.set_run_cost_status(
        run_id,
        "settled",
        settled_cost=summary["settled_total"],
        cost_bound=summary["settled_total"],
    )
    return True
