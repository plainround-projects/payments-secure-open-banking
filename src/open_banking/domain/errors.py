"""Named errors of the domain. Each carries a stable `code` that clients and logs can rely on."""

from __future__ import annotations


class OpenBankingError(Exception):
    """Base of every error this service raises on purpose."""

    code = "open_banking_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidMoneyError(OpenBankingError):
    """An amount or currency that cannot be represented."""

    code = "invalid_money"


class CurrencyMismatchError(OpenBankingError):
    """Two amounts in different currencies were combined."""

    code = "currency_mismatch"


class UnbalancedEntryError(OpenBankingError):
    """A ledger entry that does not move one positive amount between two different accounts."""

    code = "unbalanced_entry"


class InvalidIdempotencyKeyError(OpenBankingError):
    """The idempotency key is missing or has characters or a length we do not accept."""

    code = "invalid_idempotency_key"


class IdempotencyKeyReusedError(OpenBankingError):
    """A stored key arrived again with a different request, so it cannot be a retry."""

    code = "idempotency_key_reused"


class AuthenticationFailedError(OpenBankingError):
    """The customer could not be authenticated (unknown user or wrong PIN, with one answer for both)."""

    code = "authentication_failed"


class InvalidProofError(OpenBankingError):
    """The proof of strong authentication is unknown, expired, already used, or for other terms."""

    code = "invalid_authentication_proof"


class ClientAuthenticationError(OpenBankingError):
    """The merchant's credential, access token or proof of possession is not valid."""

    code = "invalid_client_credential"


class ConsentNotFoundError(OpenBankingError):
    """No consent has this id."""

    code = "consent_not_found"


class PaymentNotFoundError(OpenBankingError):
    """No payment has this id for this caller."""

    code = "payment_not_found"


class AccountNotFoundError(OpenBankingError):
    """No account has this id."""

    code = "account_not_found"


class ChallengeNotFoundError(OpenBankingError):
    """No authentication challenge has this id."""

    code = "challenge_not_found"


class AccountNotOwnedError(OpenBankingError):
    """The payer account named in the terms does not belong to the customer who authenticated."""

    code = "account_not_owned"


class UnknownClientError(OpenBankingError):
    """The merchant named in the terms is not registered with the bank."""

    code = "unknown_client"


class PaymentRefusedError(OpenBankingError):
    """The bank will not make this payment. `refusal` is the reason, in a form a program can branch on."""

    code = "payment_refused"

    def __init__(self, refusal: str, message: str) -> None:
        super().__init__(message)
        self.refusal = refusal
