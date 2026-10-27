"""The merchant's HTTP API: start a checkout, and complete it with the consent id the customer brought back."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from open_banking.application.merchant.checkout import (
    BankUnavailableError,
    Checkout,
    CheckoutNotFoundError,
)
from open_banking.domain.errors import ClientAuthenticationError, InvalidMoneyError, OpenBankingError
from open_banking.domain.money import Money
from open_banking.interfaces.http_common import error_response, install_common
from open_banking.interfaces.merchant_container import MerchantContainer

logger = logging.getLogger(__name__)

_STATUS: dict[type[OpenBankingError], int] = {
    InvalidMoneyError: 400,
    CheckoutNotFoundError: 404,
    BankUnavailableError: 504,
    ClientAuthenticationError: 502,
}


class CheckoutBody(BaseModel):
    """`POST /v1/checkouts`."""

    model_config = ConfigDict(extra="forbid")

    amount_minor: int = Field(strict=True, gt=0, le=10**12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class CompleteBody(BaseModel):
    """`POST /v1/checkouts/{id}/complete`."""

    model_config = ConfigDict(extra="forbid")

    consent_id: str = Field(min_length=1, max_length=64)


def checkout_view(checkout: Checkout, container: MerchantContainer, payee_account: str) -> dict[str, Any]:
    """A checkout, with the terms the customer must approve at the bank."""
    return {
        "checkout_id": checkout.checkout_id,
        "status": checkout.status.value,
        "amount_minor": checkout.amount.minor,
        "currency": checkout.amount.currency,
        "amount": checkout.amount.format(),
        "consent_id": checkout.consent_id,
        "payment_id": checkout.payment_id,
        "refusal": None
        if checkout.refusal is None
        else {"code": checkout.refusal.code, "message": checkout.refusal.message},
        "consent_request": {
            "client_id": container.client_id,
            "payee_account": payee_account,
            "max_amount_minor": checkout.amount.minor,
            "currency": checkout.amount.currency,
            "scope": "single-payment",
        },
    }


def create_merchant_app(container_factory: Callable[[], MerchantContainer], payee_account: str) -> FastAPI:
    """Build the application. The container is created at startup and released at shutdown."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container_factory()
        logger.info("started")
        yield
        app.state.container.close()
        logger.info("shutdown complete")

    app = FastAPI(title="Merchant", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    install_common(app)

    def merchant(request: Request) -> MerchantContainer:
        result: MerchantContainer = request.app.state.container
        return result

    @app.exception_handler(OpenBankingError)
    async def named_error(request: Request, error: OpenBankingError) -> JSONResponse:
        status = _STATUS.get(type(error), 500)
        return error_response(error.code, error.message, status, retryable=isinstance(error, BankUnavailableError))

    @app.post("/v1/checkouts", status_code=201)
    def create_checkout(body: CheckoutBody, request: Request) -> JSONResponse:
        container = merchant(request)
        checkout = container.create_checkout.execute(Money(body.amount_minor, body.currency))
        return JSONResponse(checkout_view(checkout, container, payee_account), status_code=201)

    @app.post("/v1/checkouts/{checkout_id}/complete")
    def complete_checkout(checkout_id: str, body: CompleteBody, request: Request) -> JSONResponse:
        container = merchant(request)
        checkout = container.complete_checkout.execute(checkout_id, body.consent_id)
        status = 200 if checkout.refusal is None else 403
        return JSONResponse(checkout_view(checkout, container, payee_account), status_code=status)

    @app.get("/v1/checkouts/{checkout_id}")
    def get_checkout(checkout_id: str, request: Request) -> JSONResponse:
        container = merchant(request)
        checkout = container.checkouts.get(checkout_id)
        if checkout is None:
            raise CheckoutNotFoundError(f"no checkout {checkout_id!r}")
        return JSONResponse(checkout_view(checkout, container, payee_account))

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
