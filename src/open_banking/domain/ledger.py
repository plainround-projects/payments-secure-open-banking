"""The bank's ledger: a transfer is one entry of two postings that sum to zero. Positive credits, negative debits."""

from __future__ import annotations

from dataclasses import dataclass

from open_banking.domain.errors import UnbalancedEntryError
from open_banking.domain.money import Money


@dataclass(frozen=True, slots=True)
class Posting:
    """One row of the ledger: an account and a signed amount (positive credit, negative debit)."""

    account_id: str
    amount: Money


@dataclass(frozen=True, slots=True)
class Transfer:
    """Money moved from one account to another. It cannot be built unbalanced: one positive amount, two accounts."""

    debit_account: str
    credit_account: str
    amount: Money

    def __post_init__(self) -> None:
        if not self.amount.is_positive():
            raise UnbalancedEntryError(f"a transfer moves a positive amount, got {self.amount.format()}")
        if self.debit_account == self.credit_account:
            raise UnbalancedEntryError("a transfer needs two different accounts")

    def postings(self) -> tuple[Posting, Posting]:
        """The two rows: -amount on the debited account, +amount on the credited one."""
        return Posting(self.debit_account, -self.amount), Posting(self.credit_account, self.amount)
