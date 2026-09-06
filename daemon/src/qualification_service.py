"""Foundation live provider, model, and adapter qualification.

Each live provider, model, and adapter tuple qualifies before
production use. Every probe runs through the effect and budget
services with dedicated qualification credentials and non-sensitive
fixtures. The record expires; admission uses the latest unexpired
qualification and fails closed when a required capability loses
qualification.
"""
from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import database as db

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    ProbeRunner = Callable[[str], Awaitable[dict[str, Any]]]

QUALIFICATION_PROBES = (
    "strict_structured_output",
    "usage_completeness",
    "late_usage",
    "streaming_termination",
    "streaming_interruption",
    "cancellation_acknowledgement",
    "idempotency_behavior",
    "idempotency_retention",
    "provider_run_lookup",
    "nested_tool_callbacks",
    "receipt_correlation",
)


class QualificationServiceError(ValueError):
    """One qualification rule failed closed."""


class AdmissionBlockedError(QualificationServiceError):
    """Admission fails closed without a live qualification."""


async def run_qualification_probes(
    *,
    provider: str,
    model: str,
    adapter: str,
    adapter_version: str,
    provider_version: str,
    probe_runner: ProbeRunner,
    credentials_kind: str,
    expires_at: str,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Run every registered probe and store one expiring record.

    The probe runner executes each probe as one effect through the
    effect and budget services. Only dedicated qualification
    credentials and non-sensitive fixtures qualify a tuple.
    """
    if credentials_kind != "dedicated-qualification":
        raise QualificationServiceError(
            "Qualification runs with dedicated qualification credentials"
        )
    capabilities: dict[str, bool] = {}
    probe_effect_ids: list[str] = []
    for probe in QUALIFICATION_PROBES:
        outcome = await probe_runner(probe)
        capabilities[probe] = bool(outcome.get("passed"))
        effect_id = outcome.get("effect_id")
        if effect_id is None:
            raise QualificationServiceError(
                f"The probe {probe!r} did not run through the effect "
                "service"
            )
        probe_effect_ids.append(str(effect_id))
    qualification_id = f"qualification-{uuid.uuid4()}"
    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001
        await connection.execute(
            "INSERT INTO provider_qualifications ("
            "qualification_id, provider, model, adapter, adapter_version, "
            "provider_version, capabilities, probe_effect_ids, issued_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                qualification_id,
                provider,
                model,
                adapter,
                adapter_version,
                provider_version,
                json.dumps(capabilities, sort_keys=True),
                json.dumps(probe_effect_ids),
                now,
                expires_at,
            ),
        )
        await connection.commit()
    return {
        "qualification_id": qualification_id,
        "capabilities": capabilities,
        "probe_effect_ids": probe_effect_ids,
        "issued_at": now,
        "expires_at": expires_at,
    }


async def get_qualification(qualification_id: str) -> dict[str, Any] | None:
    """Read one live qualification record by its identifier."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM provider_qualifications WHERE qualification_id = ?",
            (qualification_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def latest_unexpired(
    *,
    provider: str,
    model: str,
    adapter: str,
    database_time: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest unexpired unrevoked qualification record."""
    async with db._connect() as connection:  # noqa: SLF001
        now = await db._control_now(connection, database_time)  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM provider_qualifications WHERE provider = ? AND "
            "model = ? AND adapter = ? AND revoked = 0 AND expires_at > ? "
            "ORDER BY issued_at DESC LIMIT 1",
            (provider, model, adapter, now),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        record = dict(row)
        record["capabilities"] = json.loads(str(record["capabilities"]))
        record["probe_effect_ids"] = json.loads(
            str(record["probe_effect_ids"]),
        )
        return record


async def check_admission(
    *,
    provider: str,
    model: str,
    adapter: str,
    required_capabilities: tuple[str, ...],
    database_time: str | None = None,
) -> dict[str, Any]:
    """Admit one tuple only with a live qualification of every capability."""
    record = await latest_unexpired(
        provider=provider,
        model=model,
        adapter=adapter,
        database_time=database_time,
    )
    if record is None:
        raise AdmissionBlockedError(
            f"No live qualification exists for {provider}/{model}/{adapter}"
        )
    missing = [
        capability
        for capability in required_capabilities
        if not record["capabilities"].get(capability)
    ]
    if missing:
        raise AdmissionBlockedError(
            f"The qualification misses required capabilities: {missing}"
        )
    return record


def verify_advertised_capabilities(
    *,
    advertised: dict[str, bool],
    probed: dict[str, bool],
) -> None:
    """Fail qualification when advertisement and probes disagree.

    A changed advertised capability without a changed adapter version
    blocks production admission.
    """
    changed = sorted(
        name
        for name in set(advertised) | set(probed)
        if bool(advertised.get(name)) != bool(probed.get(name))
    )
    if changed:
        raise QualificationServiceError(
            "The advertised capabilities disagree with the probes: "
            f"{changed}"
        )


async def revoke_qualification(qualification_id: str) -> bool:
    """Revoke one qualification record."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "UPDATE provider_qualifications SET revoked = 1 "
            "WHERE qualification_id = ? AND revoked = 0",
            (qualification_id,),
        )
        await connection.commit()
        return cursor.rowcount == 1
