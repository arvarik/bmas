"""Foundation Stage 0H: populated migration, cutover, and downgrade.

The deployment phases are expand, backfill, dual_read, cutover, and
contract. This module answers the negotiation questions each phase
asks: which authority owns each write, whether two read paths agree,
whether a legacy table can retire, and whether a downgrade preserves
every new write.

The legacy ``event_journal`` stays a legacy-only writer. Every native
write
uses the ``runtime_journal`` authority alone. Dual read compares the
declared projections without creating a second write authority. A
cutover changes only new run admissions; every existing run keeps its
original runtime pair. A legacy table cannot retire while one active
reader needs it, and a downgrade that cannot preserve new writes
refuses with a clear reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import database as db

DEPLOYMENT_PHASES = ("expand", "backfill", "dual_read", "cutover", "contract")

# The one native write authority. No native write uses any other table.
NATIVE_WRITE_AUTHORITY = "runtime_journal"

# The legacy writer that stays legacy-only.
LEGACY_EVENT_WRITER = "event_journal"


class MigrationNegotiationError(ValueError):
    """One migration negotiation rule failed closed."""


class DowngradeRefusedError(MigrationNegotiationError):
    """A downgrade cannot preserve new writes."""


class RetirementRefusedError(MigrationNegotiationError):
    """A legacy table cannot retire while an active reader needs it."""


@dataclass(frozen=True)
class WriterClassification:
    """Which authority owns one writer's durable writes."""

    writer: str
    authority: str
    generation: str

    def is_native_authority(self) -> bool:
        return self.authority == NATIVE_WRITE_AUTHORITY


def classify_writer(writer: str) -> WriterClassification:
    """Classify one writer by its durable authority and generation.

    The legacy event writer stays legacy-only. Every native contract
    writer
    routes through the runtime journal authority.
    """
    if writer == LEGACY_EVENT_WRITER:
        return WriterClassification(
            writer=writer, authority="event_journal", generation="legacy",
        )
    native_writers = {
        "runtime_journal",
        "activation_service",
        "effect_service",
        "budget_service",
        "evidence_service",
        "goal_service",
        "run_admission",
    }
    if writer in native_writers:
        return WriterClassification(
            writer=writer,
            authority=NATIVE_WRITE_AUTHORITY,
            generation="native",
        )
    raise MigrationNegotiationError(f"Unknown writer: {writer!r}")


def assert_native_writes_use_one_authority(writers: tuple[str, ...]) -> None:
    """Assert every native writer uses only the runtime-journal authority."""
    for writer in writers:
        classification = classify_writer(writer)
        if classification.generation == "native" and (
            not classification.is_native_authority()
        ):
            raise MigrationNegotiationError(
                f"The v2 writer {writer!r} does not use the "
                f"{NATIVE_WRITE_AUTHORITY} authority"
            )


def assert_legacy_writer_stays_legacy(writer: str) -> None:
    """Assert one legacy writer never gains native authority."""
    classification = classify_writer(writer)
    if classification.generation != "legacy":
        raise MigrationNegotiationError(
            f"{writer!r} is not a legacy-only writer"
        )


# ── Dual-read comparison ─────────────────────────────────────────────


def dual_read_agrees(
    legacy_projection: dict[str, Any],
    native_projection: dict[str, Any],
    *,
    compared_fields: tuple[str, ...],
) -> bool:
    """Compare the declared fields of two read projections.

    Dual read compares old and new read results. It never writes, so
    it can never become a second authority.
    """
    return all(
        legacy_projection.get(name) == native_projection.get(name)
        for name in compared_fields
    )


# ── Cutover ──────────────────────────────────────────────────────────


async def existing_run_pairs() -> dict[str, dict[str, str]]:
    """Return the runtime pair of every existing run."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT run_id, runtime_id, runtime_contract_version FROM runs",
        )
        return {
            str(row["run_id"]): {
                "runtime_id": str(row["runtime_id"]),
                "runtime_contract_version": str(
                    row["runtime_contract_version"],
                ),
            }
            for row in await cursor.fetchall()
        }


async def assert_cutover_preserves_existing_pairs(
    before: dict[str, dict[str, str]],
) -> None:
    """Assert a cutover left every existing run's pair unchanged.

    A cutover enables the new writer for new run admissions only. It
    never migrates an active run across runtime contracts in place.
    """
    after = await existing_run_pairs()
    for run_id, pair in before.items():
        current = after.get(run_id)
        if current != pair:
            raise MigrationNegotiationError(
                f"The cutover changed the runtime pair of run {run_id}: "
                f"{pair} -> {current}"
            )


# ── Legacy import with digest preservation ───────────────────────────


@dataclass(frozen=True)
class LegacyRowImport:
    """One imported legacy row with its source cursor and digest."""

    source_table: str
    source_cursor: int
    source_row_digest: str


def import_legacy_rows(
    rows: list[dict[str, Any]],
    *,
    source_table: str,
) -> list[LegacyRowImport]:
    """Import retained legacy rows, preserving cursor and row digest.

    Each imported record keeps the source cursor and the source row
    digest, so the import is verifiable against the original rows.
    """
    from core.digest_profile import digest_hex

    imports = []
    for index, row in enumerate(rows):
        imports.append(LegacyRowImport(
            source_table=source_table,
            source_cursor=index,
            source_row_digest=digest_hex("legacy-row", row),
        ))
    return imports


# ── Table retirement ─────────────────────────────────────────────────


def assert_table_retirement_allowed(
    table: str,
    *,
    active_readers: tuple[str, ...],
    phase: str,
) -> None:
    """Assert one legacy table may retire.

    A legacy table cannot retire while any active reader needs it. The
    retirement is a contract-phase action; an active reader refuses it.
    """
    if phase != "contract":
        raise RetirementRefusedError(
            f"Table retirement is a contract-phase action; the phase is "
            f"{phase!r}"
        )
    if active_readers:
        raise RetirementRefusedError(
            f"The table {table!r} keeps active readers: "
            f"{sorted(active_readers)}"
        )


# ── Supported downgrade ──────────────────────────────────────────────


@dataclass(frozen=True)
class DowngradePlan:
    """One downgrade evaluation before a destructive migration."""

    from_schema_version: int
    to_schema_version: int
    new_writes_present: bool
    new_writes_are_reversible: bool


def evaluate_downgrade(plan: DowngradePlan) -> dict[str, Any]:
    """Evaluate one supported downgrade before any contract migration.

    The supported downgrade runs before a contract migration. When the
    downgrade cannot preserve new writes, it refuses with a clear
    reason instead of dropping data.
    """
    if plan.to_schema_version >= plan.from_schema_version:
        raise DowngradeRefusedError(
            "A downgrade lowers the schema version"
        )
    if plan.new_writes_present and not plan.new_writes_are_reversible:
        raise DowngradeRefusedError(
            "The downgrade cannot preserve new writes; it refuses "
            "instead of dropping them"
        )
    return {
        "supported": True,
        "from_schema_version": plan.from_schema_version,
        "to_schema_version": plan.to_schema_version,
        "preserves_new_writes": True,
    }
