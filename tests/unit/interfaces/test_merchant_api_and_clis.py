"""The merchant's HTTP API with a scripted bank, the command-line programs' pure parts, and its fault layer."""

from __future__ import annotations

import argparse
import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from open_banking.application.merchant.checkout import (
    BankUnavailableError,
    CompleteCheckout,
    CreateCheckout,
    InMemoryCheckouts,
    MerchantProfile,
    Paid,
    Refused,
)
from open_banking.infrastructure import keys
from open_banking.infrastructure.faults import FaultScript
from open_banking.interfaces import admin_cli, customer_cli
from open_banking.interfaces.merchant_api import create_merchant_app
from open_banking.interfaces.merchant_container import MerchantContainer
from tests.unit.conftest import World, build_world
from tests.unit.fakes import SequentialIds


class Gateway:
    def __init__(self, *answers):
        self.answers = list(answers)

    def initiate_payment(self, *, consent_id, payee_account, amount, idempotency_key):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def container(*answers) -> MerchantContainer:
    checkouts = InMemoryCheckouts()
    return MerchantContainer(
        create_checkout=CreateCheckout(checkouts, SequentialIds()),
        complete_checkout=CompleteCheckout(checkouts, Gateway(*answers), MerchantProfile("merchant-1", "acct-shop")),
        checkouts=checkouts,
        faults=FaultScript(),
        client_id="merchant-1",
        close=lambda: None,
    )


@pytest.fixture
def merchant() -> Iterator[TestClient]:
    with TestClient(
        create_merchant_app(lambda: container(Paid("p-1")), "acct-shop"), raise_server_exceptions=False
    ) as client:
        yield client


def test_a_checkout_names_the_terms_the_customer_must_approve_at_the_bank(merchant):
    created = merchant.post("/v1/checkouts", json={"amount_minor": 3000, "currency": "USD"})
    body = created.json()
    assert created.status_code == 201 and body["status"] == "AWAITING_CONSENT" and body["amount"] == "30.00"
    assert body["consent_request"] == {
        "client_id": "merchant-1",
        "payee_account": "acct-shop",
        "max_amount_minor": 3000,
        "currency": "USD",
        "scope": "single-payment",
    }
    assert merchant.get(f"/v1/checkouts/{body['checkout_id']}").json() == body


def test_completing_a_checkout_with_a_consent_pays_it(merchant):
    checkout = merchant.post("/v1/checkouts", json={"amount_minor": 3000, "currency": "USD"}).json()
    done = merchant.post(f"/v1/checkouts/{checkout['checkout_id']}/complete", json={"consent_id": "c-1"})
    assert done.status_code == 200 and done.json()["status"] == "PAID" and done.json()["payment_id"] == "p-1"


def test_a_refusal_by_the_bank_is_a_403_with_its_reason():
    app = create_merchant_app(lambda: container(Refused("consent_expired", "consent expired")), "acct-shop")
    with TestClient(app) as client:
        checkout = client.post("/v1/checkouts", json={"amount_minor": 3000, "currency": "USD"}).json()
        done = client.post(f"/v1/checkouts/{checkout['checkout_id']}/complete", json={"consent_id": "c-1"})
    assert done.status_code == 403 and done.json()["refusal"] == {
        "code": "consent_expired",
        "message": "consent expired",
    }


def test_a_bank_that_cannot_be_reached_is_a_retryable_504():
    app = create_merchant_app(lambda: container(BankUnavailableError("no answer after 3 attempts")), "acct-shop")
    with TestClient(app) as client:
        checkout = client.post("/v1/checkouts", json={"amount_minor": 3000, "currency": "USD"}).json()
        done = client.post(f"/v1/checkouts/{checkout['checkout_id']}/complete", json={"consent_id": "c-1"})
        assert done.status_code == 504 and done.json()["error"] == {
            "code": "bank_unavailable",
            "message": "no answer after 3 attempts",
            "retryable": True,
        }
        assert client.get(f"/v1/checkouts/{checkout['checkout_id']}").json()["status"] == "UNKNOWN"


def test_validation_unknown_checkouts_health_and_unexpected_errors(merchant):
    assert merchant.post("/v1/checkouts", json={"amount_minor": 30.5, "currency": "USD"}).status_code == 400
    assert merchant.post("/v1/checkouts", json={"amount_minor": 0, "currency": "USD"}).status_code == 400
    assert merchant.get("/v1/checkouts/nope").json()["error"]["code"] == "checkout_not_found"
    assert merchant.post("/v1/checkouts/nope/complete", json={"consent_id": "c-1"}).status_code == 404
    assert merchant.get("/health").json() == {"status": "ok"} and merchant.get("/health/live").json() == {
        "status": "ok"
    }


def test_the_container_is_closed_on_shutdown_and_unexpected_errors_are_generic():
    closed: list[bool] = []

    class Boom:
        def execute(self, *args):
            raise RuntimeError("secret internals")

    broken = dataclasses.replace(container(), create_checkout=Boom(), close=lambda: closed.append(True))  # type: ignore[arg-type]
    with TestClient(create_merchant_app(lambda: broken, "acct-shop"), raise_server_exceptions=False) as client:
        response = client.post("/v1/checkouts", json={"amount_minor": 3000, "currency": "USD"})
        assert response.status_code == 500 and "secret" not in response.text
    assert closed == [True]


def test_registering_a_merchant_and_enrolling_a_customer_write_secrets_only_to_private_files(tmp_path: Path):
    world: World = build_world()
    files = admin_cli.register_merchant(world.bank, "merchant-3", "Third Shop", tmp_path)
    key_file, env_file = files
    assert key_file.stat().st_mode & 0o777 == 0o600 and env_file.stat().st_mode & 0o777 == 0o600
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert values["MERCHANT_CLIENT_ID"] == "merchant-3" and len(values["MERCHANT_CLIENT_SECRET"]) >= 40
    assert world.db.clients["merchant-3"].public_jwk == keys.public_jwk_of(key_file.read_text())
    assert values["MERCHANT_CLIENT_SECRET"] not in world.db.clients["merchant-3"].secret_hash
    pin_file = admin_cli.enroll_customer(world.bank, "carol", "acct-bob", tmp_path)
    pin = pin_file.read_text().strip()
    assert pin_file.stat().st_mode & 0o777 == 0o600 and len(pin) == 6 and pin.isdigit()
    assert pin not in world.db.customers_by_name("carol").pin_hash


def test_the_customer_program_builds_the_terms_the_bank_will_see():
    args = argparse.Namespace(
        max="50.00", payee="acct-shop", merchant="merchant-1", currency="USD", scope="single-payment", period_days="30"
    )
    assert customer_cli._terms(args, "acct-ada") == {
        "payer_account": "acct-ada",
        "payee_account": "acct-shop",
        "client_id": "merchant-1",
        "max_amount_minor": 5000,
        "currency": "USD",
        "scope": "single-payment",
        "period_seconds": None,
    }
    recurring = argparse.Namespace(**{**vars(args), "scope": "recurring", "period_days": "7"})
    assert customer_cli._terms(recurring, "acct-ada")["period_seconds"] == 7 * 86400
    with pytest.raises(customer_cli.CustomerError, match="--max"):
        customer_cli._terms(argparse.Namespace(**{**vars(args), "max": "lots"}), "acct-ada")


def test_the_customer_program_reports_what_the_bank_said(capsys):
    import httpx

    with pytest.raises(customer_cli.CustomerError, match="authentication_failed: the username or PIN is not right"):
        customer_cli._check(
            httpx.Response(
                401, json={"error": {"code": "authentication_failed", "message": "the username or PIN is not right"}}
            )
        )
    assert customer_cli._check(httpx.Response(200, json={"ok": 1})) == {"ok": 1}
    parser = customer_cli._parser()
    assert (
        parser.parse_args(
            ["approve", "--username", "ada", "--account", "a", "--payee", "p", "--merchant", "m", "--max", "5.00"]
        ).scope
        == "single-payment"
    )
    assert json.dumps({}) == "{}" and capsys.readouterr().out == ""
