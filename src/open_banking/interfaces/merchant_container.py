"""The merchant's composition root: an HTTP client to the bank and its own in-memory checkouts. No database."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from open_banking.application.merchant.checkout import (
    CompleteCheckout,
    CreateCheckout,
    InMemoryCheckouts,
    MerchantProfile,
)
from open_banking.application.ports import IdSource
from open_banking.infrastructure import keys
from open_banking.infrastructure.bank_client import HttpBankGateway
from open_banking.infrastructure.faults import FaultInjectingTransport, FaultScript
from open_banking.infrastructure.settings import MerchantConfig
from open_banking.infrastructure.system import RandomIds


@dataclass(frozen=True, slots=True)
class MerchantContainer:
    """The merchant's use cases, the faults scriptable into its calls to the bank, and how to release resources."""

    create_checkout: CreateCheckout
    complete_checkout: CompleteCheckout
    checkouts: InMemoryCheckouts
    faults: FaultScript
    client_id: str
    close: Callable[[], None]


def build_merchant_container(
    config: MerchantConfig,
    *,
    ids: IdSource | None = None,
    clock: Callable[..., Any] | None = None,
    bank_public_jwk: dict[str, Any] | None = None,
    inner_transport: httpx.BaseTransport | None = None,
) -> MerchantContainer:
    """The real wiring: an HTTP client to the bank (with a scriptable fault layer) and in-memory checkouts."""
    faults = FaultScript()
    transport = FaultInjectingTransport(inner_transport or httpx.HTTPTransport(), faults)
    client = httpx.Client(transport=transport, timeout=config.request_timeout_seconds)
    private_pem = keys.read_private_key(config.private_key_file)
    options: dict[str, Any] = {}
    if clock is not None:
        options["clock"] = clock
    gateway = HttpBankGateway(
        client,
        bank_url=config.bank_url,
        client_id=config.client_id,
        client_secret=config.client_secret,
        private_pem=private_pem,
        attempts=config.attempts,
        retry_base_delay_seconds=config.retry_base_delay_ms / 1000,
        bank_public_jwk=bank_public_jwk,
        **options,
    )
    checkouts = InMemoryCheckouts()
    id_source = ids or RandomIds()
    return MerchantContainer(
        create_checkout=CreateCheckout(checkouts, id_source),
        complete_checkout=CompleteCheckout(checkouts, gateway, MerchantProfile(config.client_id, config.payee_account)),
        checkouts=checkouts,
        faults=faults,
        client_id=config.client_id,
        close=client.close,
    )
