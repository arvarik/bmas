"""Foundation Stage 0E: budget reservation states and concurrency.

Only the declared transitions pass, missing usage never becomes zero
cost, release cannot occur twice, and concurrent reservations cannot
exceed any aggregate limit.
"""
from __future__ import annotations

import asyncio
import itertools

import pytest

import budget_service as budget
import database as db

RUN_ID = "run-budget"
TASK_ID = "task-budget"


@pytest.fixture()
async def budget_db(tmp_path, monkeypatch):
    monkeypatch.setattr("database.DB_PATH", str(tmp_path / "budget.db"))
    await db.init_db()
    async with db._connect() as connection:  # noqa: SLF001
        await budget.create_run_budget(
            connection,
            budget_id="budget-a",
            run_id=RUN_ID,
            task_id=TASK_ID,
            currency="USD",
            limits=(
                budget.LimitSpec(
                    "run", RUN_ID, "provider_cost", 10_000, currency="USD",
                ),
                budget.LimitSpec(
                    "task", TASK_ID, "provider_cost", 15_000, currency="USD",
                ),
                budget.LimitSpec("run", RUN_ID, "output_tokens", 1_000),
                budget.LimitSpec(
                    "provider", "provider-a", "provider_cost", 8_000,
                    currency="USD",
                ),
                budget.LimitSpec("tool", "tool-search", "tool_actions", 3),
            ),
        )
        await connection.commit()
    return tmp_path


async def make_reservation(
    reservation_id: str = "reservation-a",
    *,
    cost: int = 4_000,
    tokens: int = 100,
    provider: str | None = "provider-a",
    deadline: str | None = None,
) -> dict:
    return await budget.request_reservation(
        reservation_id=reservation_id,
        budget_id="budget-a",
        resources={"provider_cost": cost, "output_tokens": tokens},
        provider=provider,
        request_deadline_at=deadline,
    )


async def limit_amounts(scope: str, resource: str) -> tuple[int, int]:
    limits = await budget.get_limits("budget-a")
    for row in limits:
        if row["scope"] == scope and row["resource"] == resource:
            return int(row["reserved_amount"]), int(row["consumed_amount"])
    raise AssertionError(f"no limit for {scope}/{resource}")


def test_only_declared_transitions_pass():
    allowed = {
        ("requested", "reserved"),
        ("requested", "released"),
        ("requested", "expired"),
        ("reserved", "consumed"),
        ("reserved", "released"),
        ("reserved", "expired"),
        ("released", "consumed"),
        ("expired", "consumed"),
    }
    for current, target in itertools.product(
        budget.RESERVATION_STATES, budget.RESERVATION_STATES,
    ):
        if (current, target) in allowed:
            budget.validate_budget_transition(current, target)
        else:
            with pytest.raises(budget.BudgetStateError):
                budget.validate_budget_transition(current, target)


@pytest.mark.asyncio
async def test_full_consumption(budget_db):
    await make_reservation()
    assert await budget.reserve("reservation-a") is True
    record = await budget.reconcile(
        "reservation-a",
        reconciliation_key="rec-full",
        actual_resources={"provider_cost": 4_000, "output_tokens": 100},
        original_amount_text="0.000004",
    )
    assert record["state"] == "consumed"
    assert record["consumed_amount_nanos"] == 4_000
    assert record["released_amount_nanos"] == 0
    assert record["consumption_kind"] == "actual"
    assert record["original_amount_text"] == "0.000004"
    reserved, consumed = await limit_amounts("run", "provider_cost")
    assert (reserved, consumed) == (0, 4_000)


@pytest.mark.asyncio
async def test_partial_consumption_stores_the_released_remainder(budget_db):
    await make_reservation()
    assert await budget.reserve("reservation-a") is True
    record = await budget.reconcile(
        "reservation-a",
        reconciliation_key="rec-partial",
        actual_resources={"provider_cost": 1_500, "output_tokens": 40},
    )
    assert record["state"] == "consumed"
    assert record["consumed_amount_nanos"] == 1_500
    assert record["released_amount_nanos"] == 2_500
    reserved, consumed = await limit_amounts("run", "provider_cost")
    assert (reserved, consumed) == (0, 1_500)


@pytest.mark.asyncio
async def test_cancellation_before_dispatch_releases_everything(budget_db):
    await make_reservation()
    assert await budget.reserve("reservation-a") is True
    record = await budget.release("reservation-a")
    assert record["state"] == "released"
    assert record["released_amount_nanos"] == 4_000
    reserved, consumed = await limit_amounts("run", "provider_cost")
    assert (reserved, consumed) == (0, 0)
    # Release cannot occur twice.
    with pytest.raises(budget.BudgetStateError):
        await budget.release("reservation-a")


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_claim_reconciles(budget_db):
    # After a dispatch claim, cancellation cannot release blindly; the
    # in-flight effect reconciles with observed usage instead.
    await make_reservation()
    assert await budget.reserve("reservation-a") is True
    with pytest.raises(budget.BudgetStateError):
        await budget.expire(
            "reservation-a", has_active_or_uncertain_effect=True,
        )
    record = await budget.reconcile(
        "reservation-a",
        reconciliation_key="rec-cancelled",
        actual_resources={"provider_cost": 900, "output_tokens": 10},
    )
    assert record["state"] == "consumed"
    assert record["consumed_amount_nanos"] == 900


@pytest.mark.asyncio
async def test_missing_usage_never_becomes_zero_cost(budget_db):
    await make_reservation()
    assert await budget.reserve("reservation-a") is True
    record = await budget.reconcile(
        "reservation-a",
        reconciliation_key="rec-missing",
        actual_resources=None,
    )
    # The pessimistic reserved amount consumes as an estimate.
    assert record["state"] == "consumed"
    assert record["consumed_amount_nanos"] == 4_000
    assert record["consumption_kind"] == "estimated"
    reserved, consumed = await limit_amounts("run", "provider_cost")
    assert (reserved, consumed) == (0, 4_000)


@pytest.mark.asyncio
async def test_late_actual_usage_replaces_the_estimate(budget_db):
    await make_reservation()
    assert await budget.reserve("reservation-a") is True
    await budget.reconcile(
        "reservation-a", reconciliation_key="rec-first", actual_resources=None,
    )
    record = await budget.reconcile(
        "reservation-a",
        reconciliation_key="rec-late-actual",
        actual_resources={"provider_cost": 2_000, "output_tokens": 55},
        original_amount_text="0.000002",
    )
    # The amounts update without another transition.
    assert record["state"] == "consumed"
    assert record["consumption_kind"] == "actual"
    assert record["consumed_amount_nanos"] == 2_000
    reserved, consumed = await limit_amounts("run", "provider_cost")
    assert (reserved, consumed) == (0, 2_000)


@pytest.mark.asyncio
async def test_partial_output_consumes_observed_usage(budget_db):
    await make_reservation()
    assert await budget.reserve("reservation-a") is True
    record = await budget.reconcile(
        "reservation-a",
        reconciliation_key="rec-partial-output",
        actual_resources={"provider_cost": 700, "output_tokens": 17},
    )
    assert record["consumed_resources"] == {
        "provider_cost": 700,
        "output_tokens": 17,
    }
    _, consumed_tokens = await limit_amounts("run", "output_tokens")
    assert consumed_tokens == 17


@pytest.mark.asyncio
async def test_each_retry_reserves_separately(budget_db):
    await make_reservation("reservation-try")
    assert await budget.reserve("reservation-try") is True
    await budget.reconcile(
        "reservation-try",
        reconciliation_key="rec-try",
        actual_resources=None,
    )
    await make_reservation("reservation-retry")
    assert await budget.reserve("reservation-retry") is True
    reserved, consumed = await limit_amounts("run", "provider_cost")
    assert (reserved, consumed) == (4_000, 4_000)


@pytest.mark.asyncio
async def test_expiry_frees_capacity_but_late_usage_stays_visible(budget_db):
    await make_reservation()
    assert await budget.reserve("reservation-a") is True
    record = await budget.expire(
        "reservation-a", has_active_or_uncertain_effect=False,
    )
    assert record["state"] == "expired"
    reserved, _ = await limit_amounts("run", "provider_cost")
    assert reserved == 0
    # Late authoritative usage after expiry consumes with a documented
    # overshoot; the expiry does not erase actual cost.
    record = await budget.reconcile(
        "reservation-a",
        reconciliation_key="rec-late-expired",
        actual_resources={"provider_cost": 3_000, "output_tokens": 80},
    )
    assert record["state"] == "consumed"
    assert record["overshoot"] == 1
    _, consumed = await limit_amounts("run", "provider_cost")
    assert consumed == 3_000


@pytest.mark.asyncio
async def test_late_usage_after_release_consumes(budget_db):
    await make_reservation()
    assert await budget.reserve("reservation-a") is True
    await budget.release("reservation-a")
    record = await budget.reconcile(
        "reservation-a",
        reconciliation_key="rec-late-released",
        actual_resources={"provider_cost": 1_000, "output_tokens": 5},
    )
    assert record["state"] == "consumed"
    assert record["overshoot"] == 1
    with pytest.raises(budget.BudgetStateError):
        await budget.release("reservation-a")


@pytest.mark.asyncio
async def test_a_request_past_its_deadline_expires(budget_db):
    await make_reservation(
        "reservation-late", deadline="2026-01-01T00:00:00.000Z",
    )
    assert await budget.reserve(
        "reservation-late", database_time="2026-01-01T00:00:01.000Z",
    ) is False
    record = await budget.get_reservation("reservation-late")
    assert record["state"] == "expired"


@pytest.mark.asyncio
async def test_reconciliation_keys_are_idempotent_and_fail_closed(budget_db):
    await make_reservation()
    assert await budget.reserve("reservation-a") is True
    first = await budget.reconcile(
        "reservation-a",
        reconciliation_key="rec-key",
        actual_resources={"provider_cost": 2_000, "output_tokens": 30},
    )
    repeat = await budget.reconcile(
        "reservation-a",
        reconciliation_key="rec-key",
        actual_resources={"provider_cost": 2_000, "output_tokens": 30},
    )
    assert repeat["consumed_amount_nanos"] == first["consumed_amount_nanos"]
    with pytest.raises(budget.BudgetConflictError):
        await budget.reconcile(
            "reservation-a",
            reconciliation_key="rec-key",
            actual_resources={"provider_cost": 9_999, "output_tokens": 30},
        )
    _, consumed = await limit_amounts("run", "provider_cost")
    assert consumed == 2_000


@pytest.mark.asyncio
async def test_concurrent_reservations_cannot_exceed_any_limit(budget_db):
    # Twelve concurrent reservations of 2000 nanos compete for a
    # 10000-nano run limit and an 8000-nano provider limit: at most
    # four fit the provider aggregate.
    for index in range(12):
        await make_reservation(
            f"reservation-race-{index}", cost=2_000, tokens=10,
        )

    results = await asyncio.gather(
        *(budget.reserve(f"reservation-race-{index}") for index in range(12))
    )
    assert results.count(True) == 4
    reserved, consumed = await limit_amounts("provider", "provider_cost")
    assert reserved <= 8_000
    run_reserved, _ = await limit_amounts("run", "provider_cost")
    assert run_reserved == reserved == 8_000
    # The losers stay requested; nothing partially reserved.
    for index in range(12):
        record = await budget.get_reservation(f"reservation-race-{index}")
        assert record["state"] in ("requested", "reserved")


@pytest.mark.asyncio
async def test_a_boundary_reservation_fits_and_one_nano_more_fails(
    budget_db,
):
    await make_reservation("reservation-exact", cost=8_000, tokens=1)
    assert await budget.reserve("reservation-exact") is True
    await budget.release("reservation-exact")
    await make_reservation("reservation-over", cost=8_001, tokens=1)
    assert await budget.reserve("reservation-over") is False
    await make_reservation("reservation-under", cost=7_999, tokens=1)
    assert await budget.reserve("reservation-under") is True


@pytest.mark.asyncio
async def test_tool_action_limits_enforce_per_tool(budget_db):
    for index in range(4):
        await budget.request_reservation(
            reservation_id=f"reservation-tool-{index}",
            budget_id="budget-a",
            resources={"tool_actions": 1},
            tool="tool-search",
        )
    outcomes = [
        await budget.reserve(f"reservation-tool-{index}")
        for index in range(4)
    ]
    assert outcomes == [True, True, True, False]
