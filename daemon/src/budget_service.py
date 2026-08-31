"""Foundation Stage 0E: the budget reservation authority.

Every cost-bearing action reserves before it starts. A reservation
moves through the registered states — requested, reserved, consumed,
released, and expired — under one durable authority. The reserve step
is one atomic compare-and-reserve statement per aggregate limit inside
one ``BEGIN IMMEDIATE`` transaction, so concurrent reservations can
never exceed any task, run, activation, provider, tool, or resource
limit.

Reserved and consumed amounts stay in separate columns, the way a
two-phase ledger separates pending and posted balances. Missing usage
never becomes zero cost: a terminal reconciliation without usage
consumes the full pessimistic reservation as an estimated amount until
late actual usage replaces it. Late usage after release or expiry
still consumes, and records a documented overshoot.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import database as db
from core.digest_profile import digest_hex
from core.failpoints import failpoint
from core.money import Money, MoneyError, require_exact_integer

if TYPE_CHECKING:
    import aiosqlite

RESERVATION_STATES = (
    "requested",
    "reserved",
    "consumed",
    "released",
    "expired",
)

# The registered transition table. Late reconciliation is the only
# path from released or expired into consumed.
BUDGET_TRANSITIONS: dict[str, frozenset[str]] = {
    "requested": frozenset({"reserved", "released", "expired"}),
    "reserved": frozenset({"consumed", "released", "expired"}),
    "released": frozenset({"consumed"}),
    "expired": frozenset({"consumed"}),
    "consumed": frozenset(),
}

RESOURCES = (
    "input_tokens",
    "output_tokens",
    "provider_cost",
    "wall_time_ms",
    "model_calls",
    "tool_actions",
    "storage_bytes",
    "human_review_units",
)

RESOURCE_UNITS = {
    "input_tokens": "tokens",
    "output_tokens": "tokens",
    "provider_cost": "nanos",
    "wall_time_ms": "milliseconds",
    "model_calls": "calls",
    "tool_actions": "actions",
    "storage_bytes": "bytes",
    "human_review_units": "reviews",
}

LIMIT_SCOPES = ("task", "run", "activation", "provider", "tool")

BUDGET_FAILPOINTS = (
    "budget.before_reserve",
    "budget.before_limit_update",
    "budget.before_commit",
    "budget.before_reconcile",
    "budget.before_reconcile_commit",
)


class BudgetError(ValueError):
    """A budget rule was violated."""


class BudgetStateError(BudgetError):
    """The requested reservation transition is not registered."""


class BudgetConflictError(BudgetError):
    """A reconciliation idempotency key was reused with other content."""


class UnknownPriceError(BudgetError):
    """A strict budget rejects an unknown price."""


def validate_budget_transition(current: str, target: str) -> None:
    """Validate one reservation state transition or fail closed."""
    if current not in BUDGET_TRANSITIONS:
        raise BudgetStateError(f"Unknown reservation state: {current!r}")
    if target not in BUDGET_TRANSITIONS:
        raise BudgetStateError(f"Unknown reservation state: {target!r}")
    if target not in BUDGET_TRANSITIONS[current]:
        raise BudgetStateError(
            f"No registered transition from {current} to {target}"
        )


@dataclass(frozen=True)
class LimitSpec:
    """One aggregate limit for one scope and one resource."""

    scope: str
    scope_key: str
    resource: str
    limit_amount: int
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.scope not in LIMIT_SCOPES:
            raise BudgetError(f"Unknown limit scope: {self.scope!r}")
        if self.resource not in RESOURCES:
            raise BudgetError(f"Unknown resource: {self.resource!r}")
        require_exact_integer("limit_amount", self.limit_amount)
        if self.limit_amount < 0:
            raise BudgetError("A limit amount cannot be negative")
        if not self.scope_key:
            raise BudgetError("A limit names its scope key")


def resolve_price(
    price_table: dict[str, str],
    model: str,
    *,
    mode: str,
    currency: str,
    pessimistic_price: str | None = None,
) -> Money:
    """Resolve one per-unit price from decimal strings.

    A strict budget rejects an unknown price. A permissive budget uses
    the declared pessimistic price instead.
    """
    if mode not in ("strict", "permissive"):
        raise BudgetError(f"Unknown budget mode: {mode!r}")
    text = price_table.get(model)
    if text is None:
        if mode == "strict":
            raise UnknownPriceError(
                f"No price is registered for {model!r}; a strict budget "
                "rejects unknown prices"
            )
        if pessimistic_price is None:
            raise UnknownPriceError(
                "A permissive budget requires a declared pessimistic price"
            )
        text = pessimistic_price
    return Money.from_decimal_string(currency, text)


def _validate_resources(resources: dict[str, int]) -> dict[str, int]:
    if not resources:
        raise BudgetError("A reservation names at least one resource")
    validated: dict[str, int] = {}
    for resource, amount in resources.items():
        if resource not in RESOURCES:
            raise BudgetError(f"Unknown resource: {resource!r}")
        require_exact_integer(f"resources[{resource}]", amount)
        if amount < 0:
            raise BudgetError("A reserved amount cannot be negative")
        validated[resource] = amount
    return validated


async def _now(
    connection: aiosqlite.Connection, database_time: str | None,
) -> str:
    return await db._control_now(connection, database_time)  # noqa: SLF001


async def create_run_budget(
    connection: aiosqlite.Connection,
    *,
    budget_id: str,
    run_id: str,
    task_id: str,
    currency: str,
    limits: tuple[LimitSpec, ...],
    budget_mode: str = "strict",
    pessimistic_price_version: str | None = None,
    journal_cursor: int | None = None,
) -> None:
    """Create one run budget and its aggregate limits.

    The caller owns the surrounding transaction, so run admission can
    create the budget atomically with the run, the admission, the
    reservation, the journal genesis, and the queue row.
    """
    if budget_mode not in ("strict", "permissive"):
        raise BudgetError(f"Unknown budget mode: {budget_mode!r}")
    await connection.execute(
        "INSERT INTO run_budgets (budget_id, run_id, task_id, currency, "
        "budget_mode, pessimistic_price_version, journal_cursor) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            budget_id,
            run_id,
            task_id,
            currency,
            budget_mode,
            pessimistic_price_version,
            journal_cursor,
        ),
    )
    for limit in limits:
        # One aggregate row exists per scope, scope key, and resource.
        # A task, provider, or tool aggregate is shared across every
        # run that names it, so concurrent runs compete for one row;
        # the first declaration fixes the limit.
        await connection.execute(
            "INSERT INTO budget_limits (budget_id, scope, scope_key, "
            "resource, unit, currency, limit_amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope, scope_key, resource) DO NOTHING",
            (
                budget_id,
                limit.scope,
                limit.scope_key,
                limit.resource,
                RESOURCE_UNITS[limit.resource],
                limit.currency,
                limit.limit_amount,
            ),
        )


async def insert_requested_reservation(
    connection: aiosqlite.Connection,
    *,
    reservation_id: str,
    budget_id: str,
    run_id: str,
    task_id: str,
    resources: dict[str, int],
    currency: str,
    activation_id: str | None = None,
    provider: str | None = None,
    tool: str | None = None,
    request_deadline_at: str | None = None,
    now: str,
) -> None:
    """Insert one requested reservation inside the caller's transaction."""
    validated = _validate_resources(resources)
    requested_nanos = int(validated.get("provider_cost", 0))
    await connection.execute(
        "INSERT INTO budget_reservations (reservation_id, budget_id, "
        "run_id, task_id, activation_id, provider, tool, state, currency, "
        "resources, requested_amount_nanos, request_deadline_at, "
        "created_at, state_changed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?, ?, ?, ?)",
        (
            reservation_id,
            budget_id,
            run_id,
            task_id,
            activation_id,
            provider,
            tool,
            currency,
            json.dumps(validated, sort_keys=True),
            requested_nanos,
            request_deadline_at,
            now,
            now,
        ),
    )


async def _load_reservation(
    connection: aiosqlite.Connection, reservation_id: str,
) -> dict[str, Any]:
    cursor = await connection.execute(
        "SELECT * FROM budget_reservations WHERE reservation_id = ?",
        (reservation_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise BudgetError(f"Unknown reservation: {reservation_id}")
    record = dict(row)
    record["resources"] = json.loads(record["resources"])
    record["consumed_resources"] = (
        json.loads(record["consumed_resources"])
        if record["consumed_resources"]
        else {}
    )
    return record


def _reservation_scopes(record: dict[str, Any]) -> list[tuple[str, str]]:
    scopes = [("run", str(record["run_id"])), ("task", str(record["task_id"]))]
    for scope, key in (
        ("activation", record["activation_id"]),
        ("provider", record["provider"]),
        ("tool", record["tool"]),
    ):
        if key:
            scopes.append((scope, str(key)))
    return scopes


async def compare_and_reserve_limits(
    connection: aiosqlite.Connection, record: dict[str, Any],
) -> bool:
    """Reserve capacity atomically across every matching aggregate.

    One conditional update per limit row fails when the new total
    would exceed the limit. The caller rolls the whole transaction
    back on the first failure, so no aggregate can overshoot.
    """
    for scope, scope_key in _reservation_scopes(record):
        for resource, amount in sorted(record["resources"].items()):
            if amount == 0:
                continue
            cursor = await connection.execute(
                "UPDATE budget_limits SET "
                "reserved_amount = reserved_amount + ? "
                "WHERE scope = ? AND scope_key = ? AND resource = ? "
                "AND reserved_amount + consumed_amount + ? <= limit_amount",
                (
                    amount,
                    scope,
                    scope_key,
                    resource,
                    amount,
                ),
            )
            if cursor.rowcount == 1:
                continue
            exists = await connection.execute(
                "SELECT COUNT(*) FROM budget_limits "
                "WHERE scope = ? AND scope_key = ? AND resource = ?",
                (scope, scope_key, resource),
            )
            row = await exists.fetchone()
            if row is not None and int(row[0]) > 0:
                return False
    return True


async def _shift_limits(
    connection: aiosqlite.Connection,
    record: dict[str, Any],
    *,
    reserve_delta: dict[str, int] | None = None,
    consume_delta: dict[str, int] | None = None,
) -> None:
    """Apply unconditional aggregate adjustments for the reservation.

    Consumption is a fact: late actual usage adds to the consumed
    aggregates even beyond a limit, which appears as a documented
    overshoot.
    """
    resources = set(reserve_delta or {}) | set(consume_delta or {})
    for scope, scope_key in _reservation_scopes(record):
        for resource in sorted(resources):
            reserve_amount = (reserve_delta or {}).get(resource, 0)
            consume_amount = (consume_delta or {}).get(resource, 0)
            if reserve_amount == 0 and consume_amount == 0:
                continue
            await connection.execute(
                "UPDATE budget_limits SET "
                "reserved_amount = MAX(reserved_amount + ?, 0), "
                "consumed_amount = MAX(consumed_amount + ?, 0) "
                "WHERE scope = ? AND scope_key = ? AND resource = ?",
                (
                    reserve_amount,
                    consume_amount,
                    scope,
                    scope_key,
                    resource,
                ),
            )


async def request_reservation(
    *,
    reservation_id: str,
    budget_id: str,
    resources: dict[str, int],
    activation_id: str | None = None,
    provider: str | None = None,
    tool: str | None = None,
    request_deadline_at: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Create one reservation in the requested state."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM run_budgets WHERE budget_id = ?", (budget_id,),
        )
        budget = await cursor.fetchone()
        if budget is None:
            raise BudgetError(f"Unknown budget: {budget_id}")
        now = await _now(connection, database_time)
        await insert_requested_reservation(
            connection,
            reservation_id=reservation_id,
            budget_id=budget_id,
            run_id=str(budget["run_id"]),
            task_id=str(budget["task_id"]),
            resources=resources,
            currency=str(budget["currency"]),
            activation_id=activation_id,
            provider=provider,
            tool=tool,
            request_deadline_at=request_deadline_at,
            now=now,
        )
        await connection.commit()
    return await get_reservation(reservation_id)


async def reserve(
    reservation_id: str, *, database_time: str | None = None,
) -> bool:
    """Move one requested reservation to reserved atomically.

    Every aggregate limit checks and updates in one conditional
    statement inside one transaction. Any exceeded aggregate rolls the
    whole transaction back, and the reservation stays requested. A
    request past its deadline expires instead.
    """
    failpoint("budget.before_reserve")
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            record = await _load_reservation(connection, reservation_id)
            now = await _now(connection, database_time)
            if record["state"] != "requested":
                raise BudgetStateError(
                    f"Only a requested reservation reserves; the state is "
                    f"{record['state']}"
                )
            deadline = record["request_deadline_at"]
            if deadline is not None and str(deadline) <= now:
                validate_budget_transition("requested", "expired")
                await connection.execute(
                    "UPDATE budget_reservations SET state = 'expired', "
                    "state_changed_at = ? WHERE reservation_id = ?",
                    (now, reservation_id),
                )
                await connection.commit()
                return False
            failpoint("budget.before_limit_update")
            fits = await compare_and_reserve_limits(connection, record)
            if not fits:
                await connection.rollback()
                return False
            validate_budget_transition("requested", "reserved")
            await connection.execute(
                "UPDATE budget_reservations SET state = 'reserved', "
                "reserved_amount_nanos = requested_amount_nanos, "
                "state_changed_at = ? WHERE reservation_id = ?",
                (now, reservation_id),
            )
            failpoint("budget.before_commit")
            await connection.commit()
            return True
        except BaseException:
            await connection.rollback()
            raise


async def release(
    reservation_id: str, *, database_time: str | None = None,
) -> dict[str, Any]:
    """Release one requested or reserved reservation exactly once."""
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            record = await _load_reservation(connection, reservation_id)
            now = await _now(connection, database_time)
            validate_budget_transition(str(record["state"]), "released")
            released = 0
            if record["state"] == "reserved":
                await _shift_limits(
                    connection,
                    record,
                    reserve_delta={
                        resource: -amount
                        for resource, amount in record["resources"].items()
                    },
                )
                released = int(record["reserved_amount_nanos"])
            await connection.execute(
                "UPDATE budget_reservations SET state = 'released', "
                "released_amount_nanos = ?, state_changed_at = ? "
                "WHERE reservation_id = ?",
                (released, now, reservation_id),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    return await get_reservation(reservation_id)


async def expire(
    reservation_id: str,
    *,
    has_active_or_uncertain_effect: bool,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Expire one reservation with no active or uncertain effect."""
    if has_active_or_uncertain_effect:
        raise BudgetStateError(
            "A reservation with an active or uncertain effect cannot expire"
        )
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            record = await _load_reservation(connection, reservation_id)
            now = await _now(connection, database_time)
            validate_budget_transition(str(record["state"]), "expired")
            if record["state"] == "reserved":
                await _shift_limits(
                    connection,
                    record,
                    reserve_delta={
                        resource: -amount
                        for resource, amount in record["resources"].items()
                    },
                )
            await connection.execute(
                "UPDATE budget_reservations SET state = 'expired', "
                "state_changed_at = ? WHERE reservation_id = ?",
                (now, reservation_id),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    return await get_reservation(reservation_id)


async def reconcile(
    reservation_id: str,
    *,
    reconciliation_key: str,
    actual_resources: dict[str, int] | None,
    original_amount_text: str | None = None,
    pricing_version: str | None = None,
    database_time: str | None = None,
) -> dict[str, Any]:
    """Reconcile one reservation with actual or missing usage.

    Missing usage consumes the full pessimistic reservation as an
    estimated amount; it never becomes zero cost. Partial usage stores
    the consumed amount and the released remainder separately. Late
    usage after release or expiry consumes with a documented
    overshoot. An equal repeated reconciliation returns the stored
    state, a conflicting reuse of the key fails closed, and a new key
    on a consumed reservation updates the amounts without another
    transition.
    """
    if not reconciliation_key:
        raise BudgetError("A reconciliation names its idempotency key")
    if isinstance(original_amount_text, float):
        raise MoneyError(
            "An original provider amount is a decimal string, never a float"
        )
    content_digest = digest_hex(
        "budget-reconciliation",
        {
            "reservation_id": reservation_id,
            "actual_resources": actual_resources,
            "original_amount_text": original_amount_text,
            "pricing_version": pricing_version,
        },
    )
    if actual_resources is not None:
        actual_resources = _validate_resources(actual_resources)

    failpoint("budget.before_reconcile")
    async with db._connect() as connection:  # noqa: SLF001
        await connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await connection.execute(
                "SELECT * FROM budget_reconciliations "
                "WHERE reconciliation_key = ?",
                (reconciliation_key,),
            )
            stored = await cursor.fetchone()
            if stored is not None:
                await connection.commit()
                if str(stored["content_digest"]) != content_digest:
                    raise BudgetConflictError(
                        "The reconciliation key was reused with different "
                        "content"
                    )
                return await get_reservation(reservation_id)

            record = await _load_reservation(connection, reservation_id)
            now = await _now(connection, database_time)
            state = str(record["state"])
            reserved_resources = dict(record["resources"])
            reserved_nanos = int(record["reserved_amount_nanos"])

            if state == "requested":
                raise BudgetStateError(
                    "A requested reservation reconciles after reserve, "
                    "release, or expiry"
                )

            if state == "reserved":
                if actual_resources is None:
                    # Missing usage: consume the pessimistic reservation.
                    consumed = dict(reserved_resources)
                    kind = "estimated"
                else:
                    consumed = actual_resources
                    kind = "actual"
                consumed_nanos = int(consumed.get("provider_cost", 0))
                released_nanos = max(reserved_nanos - consumed_nanos, 0)
                overshoot = 1 if consumed_nanos > reserved_nanos else 0
                await _shift_limits(
                    connection,
                    record,
                    reserve_delta={
                        resource: -amount
                        for resource, amount in reserved_resources.items()
                    },
                    consume_delta=consumed,
                )
            elif state in ("released", "expired"):
                # Late authoritative usage after release or expiry.
                if actual_resources is None:
                    raise BudgetError(
                        "Late reconciliation requires authoritative usage"
                    )
                consumed = actual_resources
                kind = "actual"
                consumed_nanos = int(consumed.get("provider_cost", 0))
                released_nanos = int(record["released_amount_nanos"])
                overshoot = 1
                await _shift_limits(
                    connection, record, consume_delta=consumed,
                )
            else:
                # Consumed: late actual usage replaces the estimate and
                # updates the amounts without another transition.
                if actual_resources is None:
                    raise BudgetError(
                        "A repeated reconciliation carries authoritative "
                        "usage"
                    )
                consumed = actual_resources
                kind = "actual"
                consumed_nanos = int(consumed.get("provider_cost", 0))
                released_nanos = int(record["released_amount_nanos"])
                previous = dict(record["consumed_resources"])
                overshoot = 1 if consumed_nanos > reserved_nanos else int(
                    record["overshoot"],
                )
                await _shift_limits(
                    connection,
                    record,
                    consume_delta={
                        resource: consumed.get(resource, 0)
                        - previous.get(resource, 0)
                        for resource in set(consumed) | set(previous)
                    },
                )

            if state != "consumed":
                validate_budget_transition(state, "consumed")
            await connection.execute(
                "UPDATE budget_reservations SET state = 'consumed', "
                "consumed_amount_nanos = ?, released_amount_nanos = ?, "
                "consumed_resources = ?, consumption_kind = ?, "
                "original_amount_text = COALESCE(?, original_amount_text), "
                "pricing_version = COALESCE(?, pricing_version), "
                "overshoot = ?, state_changed_at = ? "
                "WHERE reservation_id = ?",
                (
                    consumed_nanos,
                    released_nanos,
                    json.dumps(consumed, sort_keys=True),
                    kind,
                    original_amount_text,
                    pricing_version,
                    overshoot,
                    now,
                    reservation_id,
                ),
            )
            await connection.execute(
                "INSERT INTO budget_reconciliations (reconciliation_key, "
                "reservation_id, content_digest, recorded_at) "
                "VALUES (?, ?, ?, ?)",
                (reconciliation_key, reservation_id, content_digest, now),
            )
            failpoint("budget.before_reconcile_commit")
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
    return await get_reservation(reservation_id)


async def get_reservation(reservation_id: str) -> dict[str, Any]:
    """Read one reservation."""
    async with db._connect() as connection:  # noqa: SLF001
        return await _load_reservation(connection, reservation_id)


async def get_limits(budget_id: str) -> list[dict[str, Any]]:
    """Read every aggregate limit of one budget."""
    async with db._connect() as connection:  # noqa: SLF001
        cursor = await connection.execute(
            "SELECT * FROM budget_limits WHERE budget_id = ? "
            "ORDER BY scope, scope_key, resource",
            (budget_id,),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def reservation_is_valid(reservation_id: str) -> bool:
    """Report whether one reservation currently authorizes cost.

    Only a reservation in the reserved state authorizes a cost-bearing
    action.
    """
    try:
        record = await get_reservation(reservation_id)
    except BudgetError:
        return False
    return str(record["state"]) == "reserved"
