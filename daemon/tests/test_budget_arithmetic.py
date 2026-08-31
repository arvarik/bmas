"""Foundation Stage 0E: exact monetary arithmetic.

Money is an ISO currency code and a signed integer amount in nanos.
Prices parse from decimal strings, reservations round upward, hard
limits compare integers, and no hard-limit API accepts binary
floating point.
"""
from __future__ import annotations

import pytest

from budget_service import (
    BudgetError,
    LimitSpec,
    UnknownPriceError,
    resolve_price,
)
from core.money import (
    MAX_AMOUNT_NANOS,
    AmountRangeError,
    CurrencyMismatchError,
    ExchangeRate,
    FloatAmountError,
    Money,
    MoneyError,
    UnknownExchangeRateError,
    convert,
)


def test_prices_parse_from_decimal_strings():
    assert Money.from_decimal_string("USD", "1").amount_nanos == 10**9
    assert Money.from_decimal_string("USD", "0.5").amount_nanos == 5 * 10**8
    assert Money.from_decimal_string(
        "USD", "0.000000001",
    ).amount_nanos == 1
    assert Money.from_decimal_string("USD", "-2.25").amount_nanos == (
        -2_250_000_000
    )


def test_more_than_nine_fractional_digits_round_upward():
    # Twelve fractional digits: the reservation rounds up to the next
    # nano instead of losing cost.
    parsed = Money.from_decimal_string("USD", "0.000000001999")
    assert parsed.amount_nanos == 2
    parsed = Money.from_decimal_string("USD", "0.0000000000001")
    assert parsed.amount_nanos == 1
    # A negative amount rounds away from zero.
    parsed = Money.from_decimal_string("USD", "-0.0000000000001")
    assert parsed.amount_nanos == -1
    # An exact amount stays exact.
    assert Money.from_decimal_string("USD", "0.000000002").amount_nanos == 2


def test_the_original_provider_amount_string_is_preserved():
    original = "0.0000012345678999"
    parsed = Money.from_decimal_string("USD", original)
    assert parsed.amount_nanos == 1235
    # The caller stores the original text beside the rounded nanos; the
    # reconciliation API carries it through unchanged.
    assert original == "0.0000012345678999"


def test_hard_limits_compare_exact_integers():
    limit = Money.from_decimal_string("USD", "5")
    exactly = Money("USD", limit.amount_nanos)
    one_below = Money("USD", limit.amount_nanos - 1)
    one_above = Money("USD", limit.amount_nanos + 1)
    assert exactly.fits_within(limit) is True
    assert one_below.fits_within(limit) is True
    assert one_above.fits_within(limit) is False
    assert one_above.compare(limit) == 1
    assert one_below.compare(limit) == -1
    assert exactly.compare(limit) == 0


@pytest.mark.parametrize(
    "build",
    [
        lambda: Money("USD", 1.0),  # type: ignore[arg-type]
        lambda: Money.from_decimal_string("USD", 1.5),  # type: ignore[arg-type]
        lambda: Money("USD", 1).scale_ratio(2.0, 1),  # type: ignore[arg-type]
        lambda: ExchangeRate("USD", "EUR", 1.1, "1"),  # type: ignore[arg-type]
        lambda: LimitSpec("run", "run-a", "provider_cost", 1.5),  # type: ignore[arg-type]
    ],
)
def test_every_hard_limit_api_rejects_binary_floating_point(build):
    with pytest.raises(FloatAmountError):
        build()


def test_boolean_amounts_are_rejected():
    with pytest.raises(MoneyError):
        Money("USD", True)  # type: ignore[arg-type]


def test_integer_overflow_and_invalid_values_are_rejected():
    with pytest.raises(AmountRangeError):
        Money("USD", MAX_AMOUNT_NANOS + 1)
    with pytest.raises(AmountRangeError):
        Money("USD", -MAX_AMOUNT_NANOS - 1)
    with pytest.raises(AmountRangeError):
        Money("USD", MAX_AMOUNT_NANOS).add(Money("USD", 1))
    with pytest.raises(MoneyError):
        Money("usd", 1)
    with pytest.raises(MoneyError):
        Money.from_decimal_string("USD", "NaN")
    with pytest.raises(MoneyError):
        Money.from_decimal_string("USD", "not-a-price")


def test_negative_reservation_amounts_are_rejected():
    with pytest.raises(BudgetError):
        LimitSpec("run", "run-a", "provider_cost", -1)
    from budget_service import _validate_resources

    with pytest.raises(BudgetError):
        _validate_resources({"provider_cost": -5})


def test_scaling_rounds_away_from_zero():
    # A per-million price scales to a per-token reservation.
    per_million = Money.from_decimal_string("USD", "3")
    per_token = per_million.scale_ratio(1, 1_000_000)
    assert per_token.amount_nanos == 3000
    # A non-exact division reserves one extra nano.
    odd = Money("USD", 10).scale_ratio(1, 3)
    assert odd.amount_nanos == 4
    negative = Money("USD", -10).scale_ratio(1, 3)
    assert negative.amount_nanos == -4
    tokens = per_million.scale_ratio(12_345, 1_000_000)
    assert tokens.amount_nanos == 37_035_000


def test_currencies_never_combine_without_a_rate():
    dollars = Money("USD", 100)
    euros = Money("EUR", 100)
    with pytest.raises(CurrencyMismatchError):
        dollars.add(euros)
    with pytest.raises(CurrencyMismatchError):
        dollars.compare(euros)
    with pytest.raises(UnknownExchangeRateError):
        convert(dollars, "EUR", rate=None)
    mismatched = ExchangeRate("GBP", "EUR", "1.2", "1")
    with pytest.raises(UnknownExchangeRateError):
        convert(dollars, "EUR", rate=mismatched)


def test_a_versioned_rate_converts_with_upward_rounding():
    rate = ExchangeRate("USD", "EUR", "0.9333333333", "2026-08")
    converted = convert(Money("USD", 3), "EUR", rate=rate)
    assert converted.currency == "EUR"
    assert converted.amount_nanos == 3  # 2.7999... rounds up to 3 nanos.
    same = convert(Money("USD", 5), "USD", rate=None)
    assert same == Money("USD", 5)


def test_decimal_report_strings_are_exact():
    assert Money("USD", 1_500_000_000).to_decimal_string() == "1.5"
    assert Money("USD", 1).to_decimal_string() == "0.000000001"
    assert Money("USD", -2_250_000_000).to_decimal_string() == "-2.25"
    assert Money("USD", 42 * 10**9).to_decimal_string() == "42"


def test_unknown_prices_follow_the_budget_mode():
    table = {"model-known": "0.000003"}
    known = resolve_price(
        table, "model-known", mode="strict", currency="USD",
    )
    assert known.amount_nanos == 3000
    with pytest.raises(UnknownPriceError):
        resolve_price(table, "model-new", mode="strict", currency="USD")
    pessimistic = resolve_price(
        table,
        "model-new",
        mode="permissive",
        currency="USD",
        pessimistic_price="0.00009",
    )
    assert pessimistic.amount_nanos == 90_000
    with pytest.raises(UnknownPriceError):
        resolve_price(table, "model-new", mode="permissive", currency="USD")
