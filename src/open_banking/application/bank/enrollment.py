"""Setting up the bank's side: enroll a customer, register a merchant. Used by the admin program and the tests."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from open_banking.application.bank.ports import Crypto, Customer, OAuthClient, UnitOfWorkFactory
from open_banking.application.logs import fields
from open_banking.application.ports import IdSource
from open_banking.domain.errors import AccountNotFoundError

logger = logging.getLogger(__name__)


class EnrollCustomer:
    """A customer who can authenticate with a PIN and who owns an existing account."""

    def __init__(self, uow_factory: UnitOfWorkFactory, crypto: Crypto, ids: IdSource) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto
        self._ids = ids

    def execute(self, username: str, pin: str, account_id: str) -> Customer:
        """The PIN is stored as a slow salted hash and never again in any form."""
        with self._uow_factory() as uow:
            if uow.accounts.get(account_id) is None:
                raise AccountNotFoundError(f"no account {account_id!r}")
            customer = Customer(str(self._ids.new_id()), username, account_id, self._crypto.hash_secret(pin))
            uow.customers.add(customer)
            logger.info("customer enrolled", extra=fields(username=username, account_id=account_id))
            return customer


class RegisterClient:
    """A merchant that may ask for payments: its secret (as a hash) and the public half of its key."""

    def __init__(self, uow_factory: UnitOfWorkFactory, crypto: Crypto) -> None:
        self._uow_factory = uow_factory
        self._crypto = crypto

    def execute(self, client_id: str, name: str, secret: str, public_jwk: Mapping[str, Any]) -> OAuthClient:
        """Register; the private key stays with the merchant."""
        with self._uow_factory() as uow:
            client = OAuthClient(client_id, name, self._crypto.hash_secret(secret), dict(public_jwk))
            uow.clients.add(client)
            logger.info("merchant registered", extra=fields(client_id=client_id))
            return client
