"""Money as an integer count of minor units (cents) with a currency. Never a float."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from open_banking.domain.errors import CurrencyMismatchError, InvalidMoneyError

# Minor-unit exponent per ISO 4217 currency this service accepts.
CURRENCY_EXPONENTS: dict[str, int] = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0}


@dataclass(frozen=True, slots=True)
class Money:
    """An amount in minor units. May be negative (a credit leg); a payment amount is checked positive elsewhere."""

    minor: int
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise InvalidMoneyError(f"minor units must be an integer, got {type(self.minor).__name__}")
        if self.currency not in CURRENCY_EXPONENTS:
            raise InvalidMoneyError(f"unsupported currency {self.currency!r}; supported: {sorted(CURRENCY_EXPONENTS)}")

    @classmethod
    def zero(cls, currency: str) -> Money:
        """Zero in `currency`."""
        return cls(0, currency)

    @classmethod
    def parse(cls, text: str, currency: str) -> Money:
        """Read a decimal string such as `42.50`; more fractional digits than the currency has is an error."""
        if currency not in CURRENCY_EXPONENTS:
            raise InvalidMoneyError(f"unsupported currency {currency!r}")
        exponent = CURRENCY_EXPONENTS[currency]
        try:
            value = Decimal(text)
        except InvalidOperation as error:
            raise InvalidMoneyError(f"not an amount: {text!r}") from error
        if not value.is_finite():
            raise InvalidMoneyError(f"not an amount: {text!r}")
        scaled = value.scaleb(exponent)
        if scaled != scaled.to_integral_value():
            raise InvalidMoneyError(f"{text!r} has more than {exponent} decimal places for {currency}")
        return cls(int(scaled), currency)

    @property
    def exponent(self) -> int:
        """Decimal places of the currency."""
        return CURRENCY_EXPONENTS[self.currency]

    def is_positive(self) -> bool:
        """True when the amount is greater than zero."""
        return self.minor > 0

    def is_zero(self) -> bool:
        """True when the amount is zero."""
        return self.minor == 0

    def __add__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.minor - other.minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor < other.minor

    def __le__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor <= other.minor

    def __gt__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor > other.minor

    def __ge__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor >= other.minor

    def format(self) -> str:
        """Decimal text with the currency's own number of places, for example `42.50` or `-1.23`."""
        sign = "-" if self.minor < 0 else ""
        digits = str(abs(self.minor))
        if self.exponent == 0:
            return f"{sign}{digits}"
        digits = digits.rjust(self.exponent + 1, "0")
        return f"{sign}{digits[: -self.exponent]}.{digits[-self.exponent :]}"

    def _same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"cannot combine {self.currency} with {other.currency}")


def sum_money(amounts: list[Money] | tuple[Money, ...], currency: str) -> Money:
    """Add `amounts`; the empty sum is zero in `currency`."""
    total = Money.zero(currency)
    for amount in amounts:
        total = total + amount
    return total
