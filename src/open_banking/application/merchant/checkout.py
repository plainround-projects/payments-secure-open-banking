"""The merchant's side: a checkout, and the payment it initiates under a consent the customer gave at the bank."""

from __future__ import annotations

import dataclasses
import logging
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from open_banking.application.logs import fields
from open_banking.application.ports import IdSource
from open_banking.domain.errors import OpenBankingError
from open_banking.domain.money import Money

logger = logging.getLogger(__name__)


class BankUnavailableError(OpenBankingError):
    """The bank did not answer in time on every attempt. The payment may or may not have been made; ask again."""

    code = "bank_unavailable"


class CheckoutNotFoundError(OpenBankingError):
    """No checkout has this id."""

    code = "checkout_not_found"


class CheckoutStatus(StrEnum):
    """Where a checkout is."""

    AWAITING_CONSENT = "AWAITING_CONSENT"
    PAID = "PAID"
    UNKNOWN = "UNKNOWN"  # the bank did not answer; completing again with the same consent is safe


@dataclass(frozen=True, slots=True)
class Paid:
    """The bank made the payment."""

    payment_id: str


@dataclass(frozen=True, slots=True)
class Refused:
    """The bank refused, with its reason."""

    code: str
    message: str


class BankGateway(Protocol):
    """The merchant's way to the bank: authenticated, key-bound, signed, and retried with the same key."""

    def initiate_payment(
        self, *, consent_id: str, payee_account: str, amount: Money, idempotency_key: str
    ) -> Paid | Refused:
        """Raises `BankUnavailableError` when every attempt timed out."""
        ...


@dataclass(frozen=True, slots=True)
class MerchantProfile:
    """Who the merchant is at the bank, and where it is paid."""

    client_id: str
    payee_account: str


@dataclass(frozen=True, slots=True)
class Checkout:
    """One purchase. `refusal` holds the bank's last reason when the last attempt was refused."""

    checkout_id: str
    amount: Money
    status: CheckoutStatus
    consent_id: str | None = None
    payment_id: str | None = None
    refusal: Refused | None = None


class CheckoutRepository(Protocol):
    """The merchant's checkouts."""

    def add(self, checkout: Checkout) -> None:
        """Store a new checkout."""
        ...

    def get(self, checkout_id: str) -> Checkout | None:
        """Find one."""
        ...

    def save(self, checkout: Checkout) -> None:
        """Write a changed checkout."""
        ...


class InMemoryCheckouts:
    """`CheckoutRepository` in memory. The merchant's own state is small and this build does not give it a database."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Checkout] = {}

    def add(self, checkout: Checkout) -> None:
        """Store a new checkout."""
        with self._lock:
            self._items[checkout.checkout_id] = checkout

    def get(self, checkout_id: str) -> Checkout | None:
        """Find one."""
        with self._lock:
            return self._items.get(checkout_id)

    def save(self, checkout: Checkout) -> None:
        """Write a changed checkout."""
        with self._lock:
            self._items[checkout.checkout_id] = checkout


class CreateCheckout:
    """Start a purchase. The customer is then sent to the bank with the terms to approve."""

    def __init__(self, checkouts: CheckoutRepository, ids: IdSource) -> None:
        self._checkouts = checkouts
        self._ids = ids

    def execute(self, amount: Money) -> Checkout:
        """A new checkout awaiting the customer's consent."""
        checkout = Checkout(str(self._ids.new_id()), amount, CheckoutStatus.AWAITING_CONSENT)
        self._checkouts.add(checkout)
        logger.info("checkout created", extra=fields(checkout_id=checkout.checkout_id, amount=amount.format()))
        return checkout


class CompleteCheckout:
    """Initiate the payment with the consent id the customer brought back from the bank.

    The idempotency key is built from the checkout and the consent, so a retry of the same attempt always sends the
    same key and a different consent gets a different one. Completing a paid checkout returns it unchanged.
    """

    def __init__(self, checkouts: CheckoutRepository, gateway: BankGateway, profile: MerchantProfile) -> None:
        self._checkouts = checkouts
        self._gateway = gateway
        self._profile = profile

    def execute(self, checkout_id: str, consent_id: str) -> Checkout:
        """Raises `CheckoutNotFoundError`, or `BankUnavailableError` when the bank cannot be reached."""
        checkout = self._checkouts.get(checkout_id)
        if checkout is None:
            raise CheckoutNotFoundError(f"no checkout {checkout_id!r}")
        if checkout.status is CheckoutStatus.PAID:
            return checkout
        try:
            result = self._gateway.initiate_payment(
                consent_id=consent_id,
                payee_account=self._profile.payee_account,
                amount=checkout.amount,
                idempotency_key=f"checkout-{checkout.checkout_id}-{consent_id}",
            )
        except BankUnavailableError:
            self._checkouts.save(dataclasses.replace(checkout, status=CheckoutStatus.UNKNOWN, consent_id=consent_id))
            raise
        if isinstance(result, Paid):
            paid = dataclasses.replace(
                checkout, status=CheckoutStatus.PAID, consent_id=consent_id, payment_id=result.payment_id, refusal=None
            )
            self._checkouts.save(paid)
            logger.info("checkout paid", extra=fields(checkout_id=checkout_id, payment_id=result.payment_id))
            return paid
        refused = dataclasses.replace(
            checkout, status=CheckoutStatus.AWAITING_CONSENT, consent_id=consent_id, refusal=result
        )
        self._checkouts.save(refused)
        logger.warning("checkout refused", extra=fields(checkout_id=checkout_id, refusal=result.code))
        return refused
