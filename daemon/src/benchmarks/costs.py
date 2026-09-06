"""Exact benchmark run cost: Money parsing, charges, and settlement.

Every authoritative monetary field uses the Foundation
``Money(currency, amount_nanos)`` contract. A decimal string survives
only as source evidence or display input, and this module parses it
once at the trusted boundary. A legacy floating-point cost converts
through the compatibility adapter, keeps its legacy source mark, and
never authorizes a reservation or a terminal cost gate.

A run cost moves through three states. ``provisional`` covers an
active run. ``settling`` starts when every attempt ends. ``settled``
requires every admission reservation to reconcile and every unknown
charge to carry an operator acceptance. A late charge after settlement
reopens ``settling``, records new evidence, and supersedes every
stored gate evaluation for the run.
"""

from __future__ import annotations

from typing import Any

from core.money import MAX_AMOUNT_NANOS, Money, MoneyError

RUN_COST_STATES = ("provisional", "settling", "settled")
RUN_COST_TRANSITIONS = {
    ("provisional", "settling"),
    ("settling", "settled"),
    # A late charge reopens settlement; it never reopens execution.
    ("settled", "settling"),
}
BENCHMARK_CURRENCY = "USD"

# The reservation for one attempt when the test declares no explicit
# per-attempt limit and no run limit exists. One conservative unit of
# the benchmark currency; the reconciliation records any overshoot.
DEFAULT_ATTEMPT_RESERVATION_TEXT = "1"


class RunCostStateError(ValueError):
    """A run cost state change left the declared state machine."""


def validate_cost_transition(current: str, target: str) -> None:
    """Reject any run cost transition outside the declared table."""
    if current not in RUN_COST_STATES or target not in RUN_COST_STATES:
        raise RunCostStateError(
            f"Unknown run cost state: {current!r} -> {target!r}"
        )
    if (current, target) not in RUN_COST_TRANSITIONS:
        raise RunCostStateError(
            f"The run cost cannot move from {current} to {target}"
        )


def money_to_json(money: Money) -> dict[str, Any]:
    """Return the canonical JSON form of one exact amount."""
    return {"currency": money.currency, "amount_nanos": money.amount_nanos}


def money_from_json(value: Any) -> Money:
    """Parse one stored JSON amount back into ``Money``.

    The ``Money`` constructor rejects floats and unknown currencies,
    so a corrupted stored value fails closed.
    """
    if not isinstance(value, dict):
        raise MoneyError("A stored amount must be one JSON object")
    return Money(
        currency=value.get("currency"),
        amount_nanos=value.get("amount_nanos"),
    )


def parse_boundary_amount(
    value: Any, *, currency: str = BENCHMARK_CURRENCY,
) -> tuple[Money, str]:
    """Parse one configuration amount at the trusted boundary.

    A decimal string parses exactly. An integer counts whole currency
    units. A float converts through its shortest decimal text and
    keeps the ``legacy_float`` source mark, because binary floating
    point never becomes an authoritative monetary source.
    """
    if isinstance(value, str):
        return Money.from_decimal_string(currency, value), "decimal_string"
    if isinstance(value, bool):
        raise MoneyError("A boolean is not a monetary amount")
    if isinstance(value, int):
        return Money.from_decimal_string(currency, str(value)), "decimal_string"
    if isinstance(value, float):
        return Money.from_decimal_string(currency, repr(value)), "legacy_float"
    raise MoneyError(f"Unsupported monetary source: {type(value).__name__}")


def legacy_cost_adapter(
    value: float | None, *, currency: str = BENCHMARK_CURRENCY,
) -> dict[str, Any] | None:
    """Convert one legacy ``total_cost_usd`` float into marked Money.

    The adapter output carries the legacy source mark and
    ``authoritative: false``, so the value can render and reconcile
    but can never authorize a reservation or a terminal cost gate.
    """
    if value is None:
        return None
    money, _ = parse_boundary_amount(float(value), currency=currency)
    return {
        "money": money_to_json(money),
        "source": "legacy_float",
        "source_text": repr(float(value)),
        "authoritative": False,
    }


def run_cost_limit(configuration: dict[str, Any]) -> Money | None:
    """Parse the declared run cost limit, if any."""
    value = configuration.get("cost_limit_usd")
    if value is None:
        return None
    money, _ = parse_boundary_amount(value)
    return money


def budget_limit_nanos(configuration: dict[str, Any]) -> int:
    """Return the run budget limit for the Foundation budget ledger.

    Without a declared limit the budget still exists with the maximum
    representable cap, so every admission reserves through the same
    ledger and the reservation evidence stays uniform.
    """
    limit = run_cost_limit(configuration)
    if limit is None:
        return MAX_AMOUNT_NANOS
    return limit.amount_nanos


# The observed reservation multiplies the recent cost percentile by
# this safety factor and never drops below the floor, so a reservation
# stays close to real spend without starving a slightly larger attempt.
OBSERVED_RESERVATION_SAFETY = 2
OBSERVED_RESERVATION_FLOOR_TEXT = "0.02"
OBSERVED_RESERVATION_PERCENTILE = 0.95


def observed_reservation_amount(costs_usd: list[float]) -> Money | None:
    """Size one reservation from recent settled attempt costs.

    The amount is twice the ninety-fifth percentile of the observed
    costs, with a small floor. It returns None without observations,
    so the documented default applies to a revision's first attempts.
    """
    observed = sorted(float(value) for value in costs_usd if value is not None and float(value) >= 0)
    if not observed:
        return None
    position = max(0, min(len(observed) - 1, int(round(OBSERVED_RESERVATION_PERCENTILE * (len(observed) - 1)))))
    percentile, _ = parse_boundary_amount(observed[position])
    scaled = percentile.scale_ratio(OBSERVED_RESERVATION_SAFETY, 1)
    floor = Money.from_decimal_string(BENCHMARK_CURRENCY, OBSERVED_RESERVATION_FLOOR_TEXT)
    return scaled if scaled.amount_nanos >= floor.amount_nanos else floor


def attempt_reservation_amount(
    configuration: dict[str, Any], total_attempts: int,
    *, observed_costs_usd: list[float] | None = None,
) -> Money:
    """Return the expected maximum cost to reserve for one attempt.

    An explicit ``attempt_cost_limit_usd`` wins. Otherwise the run
    limit divides across the planned attempts with upward rounding, so
    the sum of all reservations still covers the run limit. Without
    any limit, recent settled attempts of the same revision size the
    reservation, and without observations the documented default
    applies; reconciliation records the observed overshoot either way.
    """
    declared = configuration.get("attempt_cost_limit_usd")
    if declared is not None:
        money, _ = parse_boundary_amount(declared)
        return money
    limit = run_cost_limit(configuration)
    if limit is not None:
        return limit.scale_ratio(1, max(int(total_attempts), 1))
    observed = observed_reservation_amount(observed_costs_usd or [])
    if observed is not None:
        return observed
    return Money.from_decimal_string(
        BENCHMARK_CURRENCY, DEFAULT_ATTEMPT_RESERVATION_TEXT,
    )


def summarize_charges(
    charges: list[dict[str, Any]], *, currency: str = BENCHMARK_CURRENCY,
) -> dict[str, Any]:
    """Total the recorded charges under the single-currency policy.

    A charge in another currency fails the total instead of silently
    converting, an unknown amount stays unknown instead of becoming
    zero, and a not-billable event contributes zero with its evidence
    kept.
    """
    total = Money.zero(currency)
    unknown: list[str] = []
    unbounded_unknown = False
    for charge in charges:
        kind = str(charge.get("kind"))
        if kind == "unknown":
            unknown.append(str(charge.get("id")))
            bound = charge.get("evidence", {}).get("accepted_bound")
            if bound is None:
                unbounded_unknown = True
            else:
                total = total.add(money_from_json(bound))
            continue
        if kind == "not_billable" or charge.get("amount_nanos") is None:
            continue
        if kind == "estimate":
            # Estimates stay separate from actual charges.
            continue
        amount = Money(
            str(charge.get("currency")), int(charge["amount_nanos"]),
        )
        total = total.add(amount)
    return {
        "currency": currency,
        "settled_total": money_to_json(total),
        "unknown_charge_ids": unknown,
        "unbounded_unknown": unbounded_unknown,
    }
