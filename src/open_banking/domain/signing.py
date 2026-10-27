"""What the merchant signs: the exact payment request, in a form both sides compute identically."""

from __future__ import annotations

import json

from open_banking.domain.money import Money


def canonical_payment_request(
    *, client_id: str, consent_id: str, payee_account: str, amount: Money, idempotency_key: str
) -> bytes:
    """The bytes the merchant signs and the bank verifies: sorted keys, no spaces, amount in minor units.

    The idempotency key is part of it, so a signature cannot be moved onto a different attempt.
    """
    document = {
        "amount_minor": amount.minor,
        "client_id": client_id,
        "consent_id": consent_id,
        "currency": amount.currency,
        "idempotency_key": idempotency_key,
        "payee_account": payee_account,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
