"""Reserve native calls and seal responses from the verified receipt chain."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any

import activation_service as activations
import agent_protocol as protocol
import budget_service as budget
import database as db
import effect_service as effects
import protocol_keys
import runtime_journal as journal
from core.digest_profile import canonicalize, plain_json
from core.money import Money
from core.signing import SigningError
from core.variants.classic.compiler import load_specification, specification_store
from core.variants.classic.proposals import parse_proposal
from execution_envelope import ModelProposalError, VerifiedReceiptChain, build_envelope


async def reserve_call(run_id: str, activation_id: str, attempt: int, request: dict[str, Any]) -> str:
    """Reserve the call's bounded cost, tokens, and execution slot."""
    admission = await db.get_runtime_admission(run_id)
    if admission is None:
        raise budget.BudgetError("The call requires an admitted run")
    specification = await db.get_classic_specification(run_id)
    if specification is None:
        raise budget.BudgetError("The native activation requires its immutable specification")
    spec = load_specification(specification_store(), str(specification["artifact_digest"]))
    if (request.get("role") == "cleaner" or (request.get("context") or {}).get("classic_proposal_role") == "cleaner") and not spec.cleaner.enabled:
        raise budget.BudgetError("The immutable specification disables the cleaner")
    price = spec.prices.rates.get(str(request.get("model")))
    if price is None:
        raise budget.UnknownPriceError(f"No immutable price is registered for {request.get('model')!r}")
    # UTF-8 bytes bound text tokenization. Reserve rendering and schema overhead.
    input_tokens = max(1, len(canonicalize(plain_json(request)).encode("utf-8")) + 8192)
    ceiling = request.get("max_completion_tokens", request.get("max_tokens", 4096))
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
        raise budget.BudgetError("The output token ceiling must be a positive integer")
    output_tokens = min(ceiling, spec.limits.max_output_tokens)
    request.pop("max_tokens", None)
    request["max_completion_tokens"] = output_tokens
    cost = price.input_per_million.to_money().scale_ratio(input_tokens, 1_000_000).add(
        price.output_per_million.to_money().scale_ratio(output_tokens, 1_000_000)
    ).amount_nanos
    await require_resource_limits(str(admission["run_budget_id"]), run_id)
    initial = str(admission["initial_reservation_id"])
    initial_row = await budget.get_reservation(initial)
    if initial_row["state"] == "reserved":
        try:
            await budget.release(initial)
        except budget.BudgetStateError:
            # Independent activations can release the admission hold together.
            if (await budget.get_reservation(initial))["state"] != "released":
                raise
    reservation_id = f"reservation-{activation_id}-{attempt}"
    await budget.request_reservation(
        reservation_id=reservation_id, budget_id=str(admission["run_budget_id"]),
        activation_id=activation_id, provider=str(request.get("model")),
        resources={"provider_cost": cost, "input_tokens": input_tokens,
                   "output_tokens": output_tokens, "model_calls": 1},
    )
    if not await budget.reserve(reservation_id):
        raise budget.BudgetError("The activation exceeds the available run budget")
    return reservation_id


async def require_resource_limits(budget_id: str, run_id: str) -> None:
    """Reject older or incomplete budgets before they authorize native work."""
    limits = await budget.get_limits(budget_id)
    resources = {row["resource"] for row in limits if row["scope"] == "run" and row["scope_key"] == run_id}
    if not {"provider_cost", "input_tokens", "output_tokens", "model_calls"}.issubset(resources):
        raise budget.BudgetError("The native budget requires all four resource limits")


async def validate_call_reservation(reservation_id: str, run_id: str, activation_id: str,
                                    request: dict[str, Any]) -> None:
    """Reject a missing, stale, foreign, or insufficient dispatch reservation."""
    reservation = await budget.get_reservation(reservation_id)
    await require_resource_limits(str(reservation["budget_id"]), run_id)
    resources = reservation["resources"]
    ceiling = request.get("max_completion_tokens", request.get("max_output_tokens", request.get("max_tokens")))
    if (reservation["run_id"] != run_id or reservation["activation_id"] != activation_id
            or reservation["state"] != "reserved" or reservation["provider"] != request.get("model")
            or isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0
            or ceiling > resources.get("output_tokens", 0)
            or resources.get("model_calls") != 1
            or len(canonicalize(plain_json(request)).encode("utf-8")) > resources.get("input_tokens", 0)):
        raise budget.BudgetError("The native call requires its current bounded reservation")
    specification = await db.get_classic_specification(run_id)
    if specification is None:
        raise budget.BudgetError("The native reservation requires its immutable prices")
    spec = load_specification(specification_store(), str(specification["artifact_digest"]))
    price = spec.prices.rates.get(str(request.get("model")))
    if price is None:
        raise budget.UnknownPriceError("The native reservation requires a known price")
    required_cost = price.input_per_million.to_money().scale_ratio(resources["input_tokens"], 1_000_000).add(
        price.output_per_million.to_money().scale_ratio(resources["output_tokens"], 1_000_000)
    )
    if not required_cost.fits_within(Money("USD", resources.get("provider_cost", 0))):
        raise budget.BudgetError("The native reservation does not cover its token ceilings")
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT budget_mode FROM run_budgets WHERE budget_id = ?", (reservation["budget_id"],)
        )
    if not rows or rows[0]["budget_mode"] != "strict":
        raise budget.BudgetError("The native call requires a strict budget")


async def reconcile_call(*, run_id: str, activation_id: str, attempt: int, known_usage: bool = True) -> None:
    """Reconcile verified usage with the immutable prices and journal the charge."""
    activation = await activations.get_activation(activation_id, attempt)
    specification = await db.get_classic_specification(run_id)
    if specification is None:
        raise budget.BudgetError("The activation requires its immutable specification")
    spec = load_specification(specification_store(), str(specification["artifact_digest"]))
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT r.model, r.usage FROM attempt_receipts r JOIN effect_operations o "
            "ON r.effect_operation_id = o.effect_operation_id WHERE r.activation_id = ? "
            "AND r.activation_attempt = ? AND r.stage = 'response_observed' AND o.kind = 'provider'",
            (activation_id, attempt),
        )
    actual = {"provider_cost": 0, "input_tokens": 0, "output_tokens": 0, "model_calls": len(rows)}
    complete_usage = bool(rows) and known_usage
    for row in rows if known_usage else []:
        usage = json.loads(row["usage"]) if row["usage"] else {}
        price = spec.prices.rates.get(str(row["model"]))
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if price is None or prompt is None or completion is None:
            complete_usage = False
            continue
        actual["input_tokens"] += prompt
        actual["output_tokens"] += completion
        charge = price.input_per_million.to_money().scale_ratio(prompt, 1_000_000).add(
            price.output_per_million.to_money().scale_ratio(completion, 1_000_000)
        )
        actual["provider_cost"] = Money("USD", actual["provider_cost"]).add(charge).amount_nanos
    usage_digest = hashlib.sha256(canonicalize(actual if complete_usage else {"usage": "unknown"}).encode()).hexdigest()
    reservation = await budget.reconcile(str(activation["reservation_id"]),
        reconciliation_key=f"activation-{activation_id}-{attempt}-{usage_digest}",
        actual_resources=actual if complete_usage else None, pricing_version=spec.prices.table_version)
    await journal_reconciliation(run_id, reservation, usage_digest)


async def journal_reconciliation(run_id: str, reservation: dict[str, Any], usage_digest: str) -> None:
    """Record the current charge so replay replaces an earlier estimate."""
    identity = await activations.run_identity(run_id)
    await journal.commit_operation(journal.JournalOperation(
        operation_type="budget_reconciliation", task_id=identity["task_id"], run_id=run_id,
        runtime_id=identity["runtime_id"], runtime_contract_version=identity["runtime_contract_version"],
        payload={"reservation_id": reservation["reservation_id"],
                 "consumed_usd_millionths": (int(reservation["consumed_amount_nanos"]) + 999) // 1000,
                 "consumed_amount_nanos": int(reservation["consumed_amount_nanos"]),
                 "consumption_kind": reservation["consumption_kind"], "cumulative": True,
                 "policy_set_digest": identity["policy_set_digest"],
                 "specification_digest": identity["specification_digest"]},
        authority_type="runtime", idempotency_token=f"call-budget-{reservation['reservation_id']}-{usage_digest}",
    ))


async def verified_receipts(activation_id: str, attempt: int, raw_response: bytes) -> tuple[VerifiedReceiptChain, bool]:
    """Verify each stored signature and its effect, grant, and response binding."""
    keys = await protocol_keys.registry()
    async with db._connect() as connection:  # noqa: SLF001
        rows = await connection.execute_fetchall(
            "SELECT r.* FROM attempt_receipts r WHERE activation_id = ? "
            "AND activation_attempt = ? ORDER BY effect_id, receipt_sequence",
            (activation_id, attempt),
        )
        attempts = await connection.execute_fetchall(
            "SELECT a.* FROM effect_attempts a JOIN effect_operations o "
            "ON a.effect_operation_id = o.effect_operation_id "
            "WHERE o.activation_id = ? AND o.activation_attempt = ?", (activation_id, attempt),
        )
    fields = {field.name for field in dataclasses.fields(protocol.AgentAttemptReceipt)}
    digests: list[str] = []
    usage: dict[str, int] = {}
    by_effect: dict[str, list[Any]] = {}
    for row in rows:
        stored = dict(row)
        data = {name: stored[name] for name in fields if name in stored}
        data.update(schema_version="1", signature_algorithm="ed25519-jcs")
        data["usage"] = json.loads(data["usage"]) if data["usage"] is not None else None
        receipt = protocol.parse_attempt_receipt(canonicalize(data))
        protocol.verify_attempt_receipt_signature(receipt, keys)
        grant = await effects.get_effect_grant_row(receipt.token_id)
        for name in ("activation_id", "activation_attempt", "effect_id", "effect_operation_id",
                     "effect_attempt_number", "dispatch_ref", "request_digest", "agent_id", "provider",
                     "model", "tool", "operation"):
            if getattr(receipt, name) != grant[name]:
                raise protocol.ReceiptError(f"The stored receipt binds a different {name}")
        by_effect.setdefault(receipt.effect_id, []).append(receipt)
        digests.append(hashlib.sha256(canonicalize(data).encode()).hexdigest())
        if receipt.stage == "response_observed" and receipt.usage is not None:
            for name, value in receipt.usage.items():
                usage[name] = usage.get(name, 0) + value
    complete = bool(attempts)
    matches_response = False
    for effect in attempts:
        receipts = by_effect.get(str(effect["effect_id"]), [])
        valid = (len(receipts) == 2 and [r.receipt_sequence for r in receipts] == [1, 2]
                 and [r.stage for r in receipts] == ["transport_starting", "response_observed"]
                 and receipts[-1].raw_response_digest is not None
                 and effect["raw_response_artifact_digest"] is not None)
        complete = complete and valid
        if not valid and effect["state"] == "dispatch_claimed":
            await effects.mark_outcome_unknown(run_id=str(effect["run_id"]),
                effect_id=str(effect["effect_id"]), reason="The response receipt is incomplete")
        if valid:
            raw = protocol_keys.artifact_store().read_object(str(effect["raw_response_artifact_digest"]))
            if raw.get("redacted") or hashlib.sha256(bytes(raw["payload"])).hexdigest() != receipts[-1].raw_response_digest:
                raise protocol.ReceiptError("The protected response differs from its signed receipt")
            if receipts[-1].raw_response_digest == hashlib.sha256(raw_response).hexdigest():
                matches_response = True
    if complete and not matches_response:
        raise protocol.ReceiptError("The activation result differs from its signed response")
    return VerifiedReceiptChain(dispatch_ref=activation_id, receipt_digests=tuple(digests),
                                usage=usage or None), complete


async def seal_response(*, run_id: str, activation_id: str, attempt: int,
                        result: dict[str, Any], role: str | None) -> dict[str, Any]:
    """Persist the raw response, validate one proposal, and seal its envelope."""
    store = protocol_keys.artifact_store()
    raw = str(result.get("result") or "").encode("utf-8")
    raw_digest = activations.persist_protected_artifact(
        store, raw, media_type="text/plain", access_policy="foundation-raw-response",
        referenced_by=f"activation-{activation_id}-{attempt}",
    )
    activation = await activations.get_activation(activation_id, attempt)
    verification_failed = False
    try:
        chain, complete = await verified_receipts(activation_id, attempt, raw)
    except (protocol.ReceiptError, SigningError):
        # A forged chain supplies no trusted usage or proposal. Retain the
        # raw response and seal a failure so the activation stays reviewable.
        verification_failed = True
        chain, complete = VerifiedReceiptChain(dispatch_ref=activation_id, receipt_digests=()), False
    proposal, failure, reason = None, None, None
    proposal_artifact = None
    if verification_failed:
        reason = "receipt_verification_failed"
    elif not complete:
        reason = "effect_outcome_unknown"
    elif role is None:
        reason = "control_response"
    else:
        try:
            # Parse the stored bytes, never the agent's parsed entries.
            protected = store.read_object(raw_digest)
            proposal = parse_proposal(bytes(protected["payload"]), role=role)
            proposal_artifact = activations.persist_protected_artifact(
                store, canonicalize({"schema_version": proposal.schema_version,
                                     "content": proposal.content}).encode(),
                media_type="application/json", access_policy="foundation-proposal",
                referenced_by=proposal.digest(),
            )
        except ModelProposalError as exc:
            failure = activations.persist_protected_artifact(
                store, str(exc).encode(), media_type="text/plain",
                access_policy="foundation-parse-failure", referenced_by=activation_id,
            )
    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, None)  # noqa: SLF001
    async with db._connect() as connection:  # noqa: SLF001
        effect_rows = await connection.execute_fetchall(
            "SELECT a.effect_id, a.state FROM effect_attempts a JOIN effect_operations o "
            "ON o.effect_operation_id = a.effect_operation_id "
            "WHERE o.activation_id = ? AND o.activation_attempt = ?", (activation_id, attempt),
        )
    cancelled = bool(effect_rows) and all(row["state"] == "cancelled" for row in effect_rows)
    if cancelled:
        reason = "cancelled_before_transport"
    envelope = build_envelope(
        trusted_status=("cancelled" if cancelled else "failed" if verification_failed or failure
                        else "unknown" if not complete else "completed"),
        task_id=str(activation["task_id"]), run_id=run_id, activation_id=activation_id,
        activation_attempt=attempt, receipt_chain=chain, raw_response_artifact_digest=raw_digest,
        started_at=str(activation["created_at"]), observed_at=now,
        proposal=proposal, parse_failure_ref=failure, no_proposal_reason=reason,
    )
    envelope_artifact = activations.persist_protected_artifact(
        store, canonicalize(envelope.to_dict()).encode(), media_type="application/json",
        access_policy="foundation-envelope", referenced_by=envelope.digest(),
    )
    updates = {"raw_result_artifact_digest": raw_digest, "execution_envelope_digest": envelope.digest(),
               "usage": chain.usage, "effect_ids": [str(row["effect_id"]) for row in effect_rows]}
    await activations.transition_activation(
        run_id=run_id, activation_id=activation_id, attempt=attempt,
        target_state="result_received" if complete else "suspended", ledger_updates=updates,
        evidence={"execution_envelope_artifact_digest": envelope_artifact,
                  "proposal_artifact_digest": proposal_artifact},
    )
    if proposal is not None:
        await activations.transition_activation(
            run_id=run_id, activation_id=activation_id, attempt=attempt, target_state="proposal_recorded",
            ledger_updates={"proposal_digest": proposal.digest()},
        )
        await reconcile_call(run_id=run_id, activation_id=activation_id, attempt=attempt)
    elif cancelled:
        await activations.transition_activation(run_id=run_id, activation_id=activation_id,
            attempt=attempt, target_state="cancelled")
        await budget.release(str(activation["reservation_id"]))
    elif complete:
        await activations.transition_activation(
            run_id=run_id, activation_id=activation_id, attempt=attempt, target_state="abandoned",
        )
        await reconcile_call(run_id=run_id, activation_id=activation_id, attempt=attempt)
    else:
        await reconcile_call(run_id=run_id, activation_id=activation_id, attempt=attempt, known_usage=False)
    return {**result, "status": "completed" if complete and not failure else "failed",
            "native_execution": {"attempt": attempt, "envelope": envelope.to_dict(),
                                 "envelope_digest": envelope.digest(),
                                 "proposal": proposal.content if proposal is not None else None}}
