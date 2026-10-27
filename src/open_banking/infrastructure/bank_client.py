"""The merchant's client for the bank: a key-bound token, a proof for every request, a signed request, the same
idempotency key on every retry, and a check of the bank's signature on what comes back."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from open_banking.application.logs import fields
from open_banking.application.merchant.checkout import BankUnavailableError, Paid, Refused
from open_banking.domain.errors import ClientAuthenticationError
from open_banking.domain.money import Money
from open_banking.domain.signing import canonical_payment_request
from open_banking.infrastructure import keys

logger = logging.getLogger(__name__)

_RETRYABLE_STATUSES = {502, 503, 504}


class HttpBankGateway:
    """`BankGateway` over HTTP. Timeouts and 5xx answers are retried with exponential backoff and the same key; the
    bank either does the work now or replays the answer it stored for that key, so a retry never pays twice.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        bank_url: str,
        client_id: str,
        client_secret: str,
        private_pem: str,
        attempts: int,
        retry_base_delay_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        bank_public_jwk: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._bank_url = bank_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._private_pem = private_pem
        self._public_jwk = keys.public_jwk_of(private_pem)
        self._attempts = attempts
        self._base_delay = retry_base_delay_seconds
        self._clock = clock
        self._sleep = sleep
        self._bank_public_jwk = bank_public_jwk
        self._token: tuple[str, datetime] | None = None

    def initiate_payment(
        self, *, consent_id: str, payee_account: str, amount: Money, idempotency_key: str
    ) -> Paid | Refused:
        """Raises `BankUnavailableError` when every attempt timed out or failed with a 5xx."""
        body = {
            "consent_id": consent_id,
            "payee_account": payee_account,
            "amount_minor": amount.minor,
            "currency": amount.currency,
        }
        signature = keys.sign(
            self._private_pem,
            canonical_payment_request(
                client_id=self._client_id,
                consent_id=consent_id,
                payee_account=payee_account,
                amount=amount,
                idempotency_key=idempotency_key,
            ),
        )
        last_error = "no attempt was made"
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._post_payment(body, idempotency_key, signature)
            except httpx.TransportError as error:
                last_error = type(error).__name__
                logger.warning(
                    "bank did not answer; retrying with the same key",
                    extra=fields(attempt=attempt, error=last_error, idempotency_key=idempotency_key),
                )
            else:
                if response.status_code == 401:
                    self._token = None  # the token may have expired; one fresh token per attempt
                    last_error = "401"
                elif response.status_code in _RETRYABLE_STATUSES:
                    last_error = str(response.status_code)
                else:
                    return self._result(response)
            if attempt < self._attempts:
                self._sleep(self._base_delay * 2 ** (attempt - 1))
        raise BankUnavailableError(f"the bank did not answer after {self._attempts} attempts ({last_error})")

    def _post_payment(self, body: dict[str, Any], idempotency_key: str, signature: str) -> httpx.Response:
        url = f"{self._bank_url}/v1/payments"
        token = self._access_token()
        proof = keys.build_dpop_proof(
            self._private_pem, self._public_jwk, method="POST", url=url, now=self._clock(), access_token=token
        )
        return self._client.post(
            url,
            json=body,
            headers={
                "Authorization": f"DPoP {token}",
                "DPoP": proof,
                "Idempotency-Key": idempotency_key,
                "X-Request-Signature": signature,
            },
        )

    def _access_token(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._token[1]:
            return self._token[0]
        url = f"{self._bank_url}/oauth/token"
        proof = keys.build_dpop_proof(self._private_pem, self._public_jwk, method="POST", url=url, now=now)
        response = self._client.post(
            url,
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
            headers={"DPoP": proof},
        )
        if response.status_code != 200:
            raise ClientAuthenticationError("the bank refused the merchant's credential")
        payload = response.json()
        lifetime = max(int(payload["expires_in"]) - 30, 1)  # refresh a little early
        self._token = (payload["access_token"], now + timedelta(seconds=lifetime))
        return str(payload["access_token"])

    def _result(self, response: httpx.Response) -> Paid | Refused:
        payload = response.json()
        if response.status_code == 201:
            self._check_bank_signature(response)
            return Paid(payload["payment_id"])
        error = payload.get("error", {})
        return Refused(str(error.get("code", "refused")), str(error.get("message", "the bank refused the payment")))

    def _check_bank_signature(self, response: httpx.Response) -> None:
        """When the bank's public key is known, the result must carry the bank's signature over its exact body."""
        if self._bank_public_jwk is None:
            return
        signature = response.headers.get("Bank-Signature", "")
        message = json.dumps(response.json(), sort_keys=True, separators=(",", ":")).encode()
        if not keys.verify(self._bank_public_jwk, message, signature):
            raise ClientAuthenticationError("the payment result is not signed by the bank")
