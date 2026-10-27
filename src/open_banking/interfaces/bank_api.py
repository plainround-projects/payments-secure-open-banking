"""The bank's HTTP API: customer authentication and consent on one side, merchant access and payments on the other."""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from open_banking.domain.authentication import AttemptOutcome
from open_banking.domain.consent import Consent, ConsentScope, ConsentTerms, PaymentRequest
from open_banking.domain.errors import (
    AccountNotFoundError,
    AccountNotOwnedError,
    AuthenticationFailedError,
    ChallengeNotFoundError,
    ClientAuthenticationError,
    ConsentNotFoundError,
    CurrencyMismatchError,
    IdempotencyKeyReusedError,
    InvalidIdempotencyKeyError,
    InvalidMoneyError,
    InvalidProofError,
    OpenBankingError,
    PaymentNotFoundError,
    PaymentRefusedError,
    UnknownClientError,
)
from open_banking.domain.idempotency import IdempotencyKey
from open_banking.domain.money import Money
from open_banking.infrastructure import keys
from open_banking.interfaces.bank_container import BankContainer
from open_banking.interfaces.http_common import error_response, install_common

logger = logging.getLogger(__name__)

_STATUS: dict[type[OpenBankingError], int] = {
    InvalidMoneyError: 400,
    CurrencyMismatchError: 400,
    InvalidIdempotencyKeyError: 400,
    AuthenticationFailedError: 401,
    InvalidProofError: 401,
    ClientAuthenticationError: 401,
    AccountNotOwnedError: 403,
    UnknownClientError: 422,
    AccountNotFoundError: 404,
    ChallengeNotFoundError: 404,
    ConsentNotFoundError: 404,
    PaymentNotFoundError: 404,
    IdempotencyKeyReusedError: 422,
}
_REFUSAL_STATUS = {"consent_not_found": 404, "insufficient_funds": 422, "request_not_as_signed": 400}
_OUTCOME_CODE = {
    AttemptOutcome.WRONG_CODE: ("wrong_code", 401),
    AttemptOutcome.LOCKED: ("challenge_locked", 401),
    AttemptOutcome.EXPIRED: ("challenge_expired", 401),
    AttemptOutcome.ALREADY_USED: ("challenge_already_used", 409),
}


class TermsBody(BaseModel):
    """The terms a customer approves."""

    model_config = ConfigDict(extra="forbid")

    payer_account: str = Field(min_length=1, max_length=64)
    payee_account: str = Field(min_length=1, max_length=64)
    client_id: str = Field(min_length=1, max_length=64)
    max_amount_minor: int = Field(strict=True, gt=0, le=10**12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    scope: ConsentScope
    period_seconds: int | None = Field(default=None, strict=True, gt=0, le=366 * 86400)

    def to_terms(self) -> ConsentTerms:
        """The domain value."""
        return ConsentTerms(
            self.payer_account,
            self.payee_account,
            self.client_id,
            Money(self.max_amount_minor, self.currency),
            self.scope,
            None if self.period_seconds is None else timedelta(seconds=self.period_seconds),
        )


class ChallengeBody(BaseModel):
    """`POST /v1/sca/challenges`."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    pin: str = Field(min_length=1, max_length=64, repr=False)
    terms: TermsBody


class VerifyBody(BaseModel):
    """`POST /v1/sca/challenges/{id}/verify`."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^\d{6}$", repr=False)


class ConsentBody(BaseModel):
    """`POST /v1/consents`."""

    model_config = ConfigDict(extra="forbid")

    terms: TermsBody


class RevokeBody(BaseModel):
    """`POST /v1/consents/{id}/revoke`."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    pin: str = Field(min_length=1, max_length=64, repr=False)


class PaymentBody(BaseModel):
    """`POST /v1/payments`."""

    model_config = ConfigDict(extra="forbid")

    consent_id: str = Field(min_length=1, max_length=64)
    payee_account: str = Field(min_length=1, max_length=64)
    amount_minor: int = Field(strict=True, gt=0, le=10**12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


def consent_view(consent: Consent) -> dict[str, Any]:
    """A consent as the API shows it."""
    terms = consent.terms
    return {
        "consent_id": consent.consent_id,
        "payer_account": terms.payer_account,
        "payee_account": terms.payee_account,
        "client_id": terms.client_id,
        "max_amount_minor": terms.max_amount.minor,
        "currency": terms.max_amount.currency,
        "scope": terms.scope.value,
        "period_seconds": None if terms.period is None else int(terms.period.total_seconds()),
        "created_at": consent.created_at.isoformat(),
        "expires_at": consent.expires_at.isoformat(),
        "revoked_at": None if consent.revoked_at is None else consent.revoked_at.isoformat(),
    }


def create_bank_app(container_factory: Callable[[], BankContainer]) -> FastAPI:
    """Build the application. The container is created at startup and released at shutdown."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container_factory()
        logger.info("started")
        yield
        app.state.container.close()
        logger.info("shutdown complete")

    app = FastAPI(title="Mock bank", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    install_common(app)

    def bank(request: Request) -> BankContainer:
        result: BankContainer = request.app.state.container
        return result

    @app.exception_handler(PaymentRefusedError)
    async def refused(request: Request, error: PaymentRefusedError) -> JSONResponse:
        return error_response(error.refusal, error.message, _REFUSAL_STATUS.get(error.refusal, 403))

    @app.exception_handler(OpenBankingError)
    async def named_error(request: Request, error: OpenBankingError) -> JSONResponse:
        status = _STATUS.get(type(error), 500)
        response = error_response(error.code, error.message, status)
        if status == 401 and isinstance(error, ClientAuthenticationError):
            response.headers["WWW-Authenticate"] = 'DPoP error="invalid_token"'
        return response

    def caller(request: Request, authorization: str | None, dpop: str | None) -> str:
        """Authenticate a merchant request: its token, and a fresh proof it holds the token's key."""
        url = f"{bank(request).public_url}{request.url.path}"
        return bank(request).authenticate.execute(authorization, dpop, method=request.method, url=url)

    @app.post("/oauth/token")
    async def token(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        dpop: Annotated[str | None, Header(alias="DPoP")] = None,
    ) -> JSONResponse:
        # The body is a standard form (application/x-www-form-urlencoded); one field needs no multipart library.
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"), keep_blank_values=True)
        grant_type = form.get("grant_type", [""])[0]
        client_id, secret = _basic_credentials(authorization)
        if grant_type != "client_credentials" or not dpop:
            raise ClientAuthenticationError("invalid merchant credential")
        url = f"{bank(request).public_url}/oauth/token"
        issued = await run_in_threadpool(bank(request).issue_token.execute, client_id, secret, dpop, url)
        return JSONResponse(
            {"access_token": issued.access_token, "token_type": "DPoP", "expires_in": issued.expires_in_seconds},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/sca/challenges", status_code=201)
    def start_challenge(body: ChallengeBody, request: Request) -> JSONResponse:
        started = bank(request).start_challenge.execute(body.username, body.pin, body.terms.to_terms())
        return JSONResponse(
            {"challenge_id": started.challenge_id, "expires_at": started.expires_at.isoformat()}, status_code=201
        )

    @app.post("/v1/sca/challenges/{challenge_id}/verify")
    def verify_challenge(challenge_id: str, body: VerifyBody, request: Request) -> JSONResponse:
        result = bank(request).verify_challenge.execute(challenge_id, body.code)
        if result.outcome is AttemptOutcome.VERIFIED:
            assert result.proof is not None
            assert result.proof_expires_at is not None
            return JSONResponse(
                {"authentication_proof": result.proof, "expires_at": result.proof_expires_at.isoformat()},
                headers={"Cache-Control": "no-store"},
            )
        code, status = _OUTCOME_CODE[result.outcome]
        return error_response(
            code, f"the code was not accepted ({code.replace('_', ' ')})", status, attempts_left=result.attempts_left
        )

    @app.post("/v1/consents", status_code=201)
    def grant_consent(
        body: ConsentBody, request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> JSONResponse:
        scheme, _, proof = (authorization or "").partition(" ")
        if scheme != "SCA" or not proof:
            raise InvalidProofError("consent requires strong customer authentication first")
        consent = bank(request).grant_consent.execute(proof, body.terms.to_terms())
        return JSONResponse(consent_view(consent), status_code=201)

    @app.post("/v1/consents/{consent_id}/revoke")
    def revoke_consent(consent_id: str, body: RevokeBody, request: Request) -> JSONResponse:
        return JSONResponse(consent_view(bank(request).revoke_consent.execute(body.username, body.pin, consent_id)))

    @app.post("/v1/payments", status_code=201)
    def initiate_payment(
        body: PaymentBody,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        dpop: Annotated[str | None, Header(alias="DPoP")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        signature: Annotated[str | None, Header(alias="X-Request-Signature")] = None,
    ) -> JSONResponse:
        client_id = caller(request, authorization, dpop)
        if idempotency_key is None:
            raise InvalidIdempotencyKeyError("the Idempotency-Key header is required on every payment")
        result = bank(request).initiate_payment.execute(
            client_id,
            IdempotencyKey(idempotency_key),
            PaymentRequest(body.consent_id, body.payee_account, Money(body.amount_minor, body.currency)),
            signature,
        )
        payload = dict(result.response.body)
        headers = {"Bank-Signature": keys.sign(bank(request).signing_pem, _canonical(payload))}
        if result.replayed:
            headers["Idempotent-Replay"] = "true"
        return JSONResponse(payload, status_code=result.response.status, headers=headers)

    @app.get("/v1/payments/{payment_id}")
    def get_payment(
        payment_id: str,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        dpop: Annotated[str | None, Header(alias="DPoP")] = None,
    ) -> JSONResponse:
        client_id = caller(request, authorization, dpop)
        payload = bank(request).get_payment.execute(client_id, payment_id)
        return JSONResponse(
            payload, headers={"Bank-Signature": keys.sign(bank(request).signing_pem, _canonical(payload))}
        )

    @app.get("/.well-known/bank-keys.json")
    def bank_keys(request: Request) -> dict[str, Any]:
        return {"keys": [bank(request).signing_jwk]}

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health")
    def health(request: Request) -> JSONResponse:
        checks = bank(request).health.execute()
        healthy = all(value == "ok" for value in checks.values())
        return JSONResponse(
            {"status": "ok" if healthy else "unavailable", "checks": checks}, status_code=200 if healthy else 503
        )

    return app


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _basic_credentials(authorization: str | None) -> tuple[str, str]:
    """Read `Authorization: Basic base64(client_id:secret)`; anything else is an invalid merchant credential."""
    scheme, _, encoded = (authorization or "").partition(" ")
    if scheme.lower() != "basic" or not encoded:
        raise ClientAuthenticationError("invalid merchant credential")
    try:
        client_id, _, secret = base64.b64decode(encoded, validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise ClientAuthenticationError("invalid merchant credential") from error
    return client_id, secret
