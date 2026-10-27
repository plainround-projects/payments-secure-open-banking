import pytest

from open_banking.domain.errors import CurrencyMismatchError, InvalidMoneyError
from open_banking.domain.money import Money, sum_money


def test_parse_reads_dollars_into_exact_cents():
    assert Money.parse("42.50", "USD") == Money(4250, "USD")


def test_parse_keeps_a_whole_number_of_yen_as_is():
    assert Money.parse("500", "JPY") == Money(500, "JPY")


@pytest.mark.parametrize("text", ["42.505", "abc", "NaN", "Infinity", ""])
def test_parse_refuses_what_is_not_an_exact_amount(text):
    with pytest.raises(InvalidMoneyError):
        Money.parse(text, "USD")


def test_parse_refuses_decimals_in_a_currency_without_minor_units():
    with pytest.raises(InvalidMoneyError):
        Money.parse("5.5", "JPY")


def test_an_unknown_currency_is_refused():
    with pytest.raises(InvalidMoneyError):
        Money(100, "XXX")
    with pytest.raises(InvalidMoneyError):
        Money.parse("1.00", "XXX")


@pytest.mark.parametrize("minor", [1.5, "100", True])
def test_minor_units_must_be_an_integer(minor):
    with pytest.raises(InvalidMoneyError):
        Money(minor, "USD")


@pytest.mark.parametrize(
    ("minor", "currency", "text"),
    [(4250, "USD", "42.50"), (5, "USD", "0.05"), (0, "USD", "0.00"), (-123, "USD", "-1.23"), (500, "JPY", "500")],
)
def test_format_uses_the_currencys_own_places(minor, currency, text):
    assert Money(minor, currency).format() == text


def test_arithmetic_and_comparison_stay_in_integers():
    a, b = Money(4250, "USD"), Money(123, "USD")
    assert a - b == Money(4127, "USD")
    assert a + b == Money(4373, "USD")
    assert -a == Money(-4250, "USD")
    assert b < a and b <= a and a > b and a >= b and not a < b
    assert a.is_positive() and not Money.zero("USD").is_positive() and Money.zero("USD").is_zero()


def test_combining_two_currencies_is_an_error():
    with pytest.raises(CurrencyMismatchError):
        Money(1, "USD") + Money(1, "EUR")
    with pytest.raises(CurrencyMismatchError):
        assert Money(1, "USD") < Money(1, "EUR")


def test_sum_money_of_nothing_is_zero_in_the_given_currency():
    assert sum_money([], "EUR") == Money(0, "EUR")
    assert sum_money([Money(1, "USD"), Money(2, "USD")], "USD") == Money(3, "USD")
