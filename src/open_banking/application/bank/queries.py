"""Read-only use cases of the bank: balances for the demo and the checks, and the health check."""

from __future__ import annotations

from dataclasses import dataclass

from open_banking.application.bank.ports import HealthProbe, UnitOfWorkFactory
from open_banking.domain.errors import AccountNotFoundError
from open_banking.domain.money import Money


class GetBalances:
    """The balance of some accounts, and the sum of the whole ledger (always zero)."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def counts(self) -> Counts:
        """What exists, for the demo and the checks."""
        with self._uow_factory() as uow:
            return Counts(uow.consents.count())

    def execute(self, account_ids: list[str], currency: str = "USD") -> tuple[dict[str, Money], Money]:
        """Raises `AccountNotFoundError` for an unknown account."""
        with self._uow_factory() as uow:
            balances: dict[str, Money] = {}
            for account_id in account_ids:
                account = uow.accounts.get(account_id)
                if account is None:
                    raise AccountNotFoundError(f"no account {account_id!r}")
                balances[account_id] = account.balance
            return balances, uow.ledger.total(currency)


@dataclass(frozen=True, slots=True)
class Counts:
    """How many consents exist."""

    consents: int


class CheckHealth:
    """Is every dependency reachable?"""

    def __init__(self, probes: dict[str, HealthProbe]) -> None:
        self._probes = probes

    def execute(self) -> dict[str, str]:
        """Each dependency's status, `ok` or the error's type name."""
        results: dict[str, str] = {}
        for name, probe in self._probes.items():
            try:
                probe.check()
                results[name] = "ok"
            except Exception as error:  # a health check reports every failure; it must not raise
                results[name] = type(error).__name__
        return results
