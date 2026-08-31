"""Foundation Stage 0E: exact monetary arithmetic.

``Money`` holds an ISO currency code and a signed integer amount in
nanos. One nano equals one billionth of the currency unit. Prices
parse from decimal strings, reservations round upward to the next
nano, and every hard limit compares integers. No hard monetary limit
touches binary floating-point arithmetic, and no constructor accepts a
float.

Currencies never combine without one versioned exchange-rate record.
A strict budget rejects an unknown conversion rate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

NANOS_PER_UNIT = 10**9

# The declared integer range: one signed 64-bit integer, the widest
# range every supported storage and transport represents exactly.
MAX_AMOUNT_NANOS = 2**63 - 1
MIN_AMOUNT_NANOS = -(2**63 - 1)

_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


class MoneyError(ValueError):
    """A monetary value or operation was rejected."""


class FloatAmountError(MoneyError):
    """A hard monetary limit never accepts binary floating point."""


class CurrencyMismatchError(MoneyError):
    """Two currencies cannot combine without an exchange-rate record."""


class UnknownExchangeRateError(MoneyError):
    """A strict budget rejects an unknown conversion rate."""


class AmountRangeError(MoneyError):
    """The amount exceeds the declared integer range."""


def require_exact_integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float):
            raise FloatAmountError(
                f"{name} rejects binary floating-point values"
            )
        raise MoneyError(f"{name} must be an integer")
    return value


def _check_range(amount_nanos: int) -> int:
    if amount_nanos > MAX_AMOUNT_NANOS or amount_nanos < MIN_AMOUNT_NANOS:
        raise AmountRangeError(
            f"The amount {amount_nanos} exceeds the declared integer range"
        )
    return amount_nanos


@dataclass(frozen=True, order=False)
class Money:
    """One exact monetary amount: a currency code and integer nanos."""

    currency: str
    amount_nanos: int

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or not _CURRENCY_PATTERN.match(
            self.currency,
        ):
            raise MoneyError(
                f"Invalid ISO currency code: {self.currency!r}"
            )
        require_exact_integer("amount_nanos", self.amount_nanos)
        _check_range(self.amount_nanos)

    # ── Construction ────────────────────────────────────────────────

    @classmethod
    def from_decimal_string(
        cls, currency: str, text: str, *, rounding: str = "up",
    ) -> Money:
        """Parse one decimal string into exact nanos.

        ``rounding="up"`` rounds away from zero to the next nano, which
        keeps every reservation conservative. A string with more than
        nine fractional digits therefore reserves one extra nano
        instead of losing cost.
        """
        if isinstance(text, float):
            raise FloatAmountError(
                "Prices parse from decimal strings, never from floats"
            )
        if not isinstance(text, str) or not text.strip():
            raise MoneyError("A price parses from one decimal string")
        try:
            with localcontext() as context:
                context.prec = 50
                value = Decimal(text.strip())
        except InvalidOperation as exc:
            raise MoneyError(f"Invalid decimal amount: {text!r}") from exc
        if not value.is_finite():
            raise MoneyError(f"Invalid decimal amount: {text!r}")
        return cls(currency, _decimal_to_nanos(value, rounding=rounding))

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(currency, 0)

    # ── Arithmetic (same currency only) ─────────────────────────────

    def _require_same_currency(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise MoneyError("Money combines with Money only")
        if other.currency != self.currency:
            raise CurrencyMismatchError(
                f"{self.currency} and {other.currency} cannot combine "
                "without one versioned exchange-rate record"
            )

    def add(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(
            self.currency,
            _check_range(self.amount_nanos + other.amount_nanos),
        )

    def subtract(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(
            self.currency,
            _check_range(self.amount_nanos - other.amount_nanos),
        )

    def scale_ratio(self, numerator: int, denominator: int) -> Money:
        """Scale by an exact integer ratio, rounding away from zero.

        Per-token pricing divides a per-million price by one million;
        the ceiling keeps the reservation conservative.
        """
        require_exact_integer("numerator", numerator)
        require_exact_integer("denominator", denominator)
        if denominator <= 0:
            raise MoneyError("The denominator must be positive")
        product = self.amount_nanos * numerator
        quotient, remainder = divmod(abs(product), denominator)
        if remainder:
            quotient += 1
        if product < 0:
            quotient = -quotient
        return Money(self.currency, _check_range(quotient))

    # ── Integer comparison for hard limits ──────────────────────────

    def compare(self, other: Money) -> int:
        """Return -1, 0, or 1 by exact integer comparison."""
        self._require_same_currency(other)
        if self.amount_nanos < other.amount_nanos:
            return -1
        if self.amount_nanos > other.amount_nanos:
            return 1
        return 0

    def fits_within(self, limit: Money) -> bool:
        """Report whether this amount fits one hard limit exactly."""
        return self.compare(limit) <= 0

    # ── Conversion and reports ──────────────────────────────────────

    def to_decimal_string(self) -> str:
        """Return the exact decimal text for reports."""
        sign = "-" if self.amount_nanos < 0 else ""
        units, nanos = divmod(abs(self.amount_nanos), NANOS_PER_UNIT)
        fraction = f"{nanos:09d}".rstrip("0")
        if fraction:
            return f"{sign}{units}.{fraction}"
        return f"{sign}{units}"


def _decimal_to_nanos(value: Decimal, *, rounding: str) -> int:
    if rounding != "up":
        raise MoneyError(f"Unknown rounding mode: {rounding!r}")
    scaled = value * NANOS_PER_UNIT
    integral = int(scaled)
    if scaled != integral:
        integral += 1 if scaled > 0 else -1
    return _check_range(integral)


@dataclass(frozen=True)
class ExchangeRate:
    """One versioned exchange-rate record between two currencies."""

    base_currency: str
    quote_currency: str
    rate_text: str
    rate_version: str

    def __post_init__(self) -> None:
        for code in (self.base_currency, self.quote_currency):
            if not _CURRENCY_PATTERN.match(code):
                raise MoneyError(f"Invalid ISO currency code: {code!r}")
        if isinstance(self.rate_text, float):
            raise FloatAmountError(
                "An exchange rate parses from one decimal string"
            )
        try:
            rate = Decimal(self.rate_text)
        except InvalidOperation as exc:
            raise MoneyError(
                f"Invalid exchange rate: {self.rate_text!r}"
            ) from exc
        if not rate.is_finite() or rate <= 0:
            raise MoneyError(f"Invalid exchange rate: {self.rate_text!r}")
        if not self.rate_version:
            raise MoneyError("An exchange rate names its version")


def convert(
    money: Money,
    target_currency: str,
    *,
    rate: ExchangeRate | None,
) -> Money:
    """Convert one amount through one versioned exchange-rate record.

    A missing or mismatched rate fails closed: a strict budget rejects
    an unknown conversion.
    """
    if money.currency == target_currency:
        return money
    if (
        rate is None
        or rate.base_currency != money.currency
        or rate.quote_currency != target_currency
    ):
        raise UnknownExchangeRateError(
            f"No qualified exchange-rate record converts {money.currency} "
            f"to {target_currency}"
        )
    with localcontext() as context:
        context.prec = 50
        converted = Decimal(money.amount_nanos) * Decimal(rate.rate_text)
    integral = int(converted)
    if converted != integral:
        integral += 1 if converted > 0 else -1
    return Money(target_currency, _check_range(integral))
