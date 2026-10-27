"""The bank's HTTP API, end to end through FastAPI's test client over in-memory storage and real cryptography."""

from __future__ import annotations

import base64
import io
import json
import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from open_banking.application.merchant.checkout import BankUnavailableError, Paid, Refused
from open_banking.domain.errors import ClientAuthenticationError
from open_banking.domain.money import Money
from open_banking.infrastructure import keys
from open_banking.infrastructure.bank_client import HttpBankGateway
from open_banking.infrastructure.faults import Fault, FaultInjectingTransport, FaultScript
from open_banking.infrastructure.logging import configure_logging
from open_banking.interfaces.bank_api import create_bank_app
from tests.unit.conftest import BANK_URL, World, build_world
from tests.unit.helpers import dpop, sign_request


@pytest.fixture
def stack() -> Iterator[tuple[TestClient, World]]:
    world = build_world()
    with TestClient(create_bank_app(lambda: world.bank), base_url=BANK_URL, raise_server_exceptions=False) as client:
        yield client, world


def terms_json(maximum: int = 5000, **extra: Any) -> dict[str, Any]:
    return {
        "payer_account": "acct-ada",
        "payee_account": "acct-shop",
        "client_id": "merchant-1",
        "max_amount_minor": maximum,
        "currency": "USD",
        "scope": "single-payment",
        "period_seconds": None,
        **extra,
    }


def consent_over_http(client: TestClient, world: World, maximum: int = 5000) -> dict[str, Any]:
    started = client.post(
        "/v1/sca/challenges", json={"username": "ada", "pin": world.ada_pin, "terms": terms_json(maximum)}
    )
    assert started.status_code == 201
    code = world.phone.latest_code(world.db.customers_by_name("ada").customer_id)
    proof = client.post(f"/v1/sca/challenges/{started.json()['challenge_id']}/verify", json={"code": code})
    assert proof.status_code == 200 and proof.headers["Cache-Control"] == "no-store"
    created = client.post(
        "/v1/consents",
        json={"terms": terms_json(maximum)},
        headers={"Authorization": f"SCA {proof.json()['authentication_proof']}"},
    )
    assert created.status_code == 201
    body: dict[str, Any] = created.json()
    return body


def token_over_http(client: TestClient, world: World) -> str:
    merchant = world.merchant
    response = client.post(
        "/oauth/token",
        data={"grant_type": "client_credentials"},
        auth=(merchant.client_id, merchant.secret),
        headers={"DPoP": dpop(merchant, world, "POST", "/oauth/token")},
    )
    assert response.status_code == 200 and response.json()["token_type"] == "DPoP"
    return str(response.json()["access_token"])


def pay_over_http(client, world, token, consent_id, amount=3000, key="k-1", merchant=None, **overrides):
    merchant = merchant or world.merchant
    signature = sign_request(merchant, consent_id, "acct-shop", Money(amount, "USD"), key)
    headers = {
        "Authorization": f"DPoP {token}",
        "DPoP": dpop(merchant, world, "POST", "/v1/payments", token),
        "Idempotency-Key": key,
        "X-Request-Signature": signature,
    }
    headers.update(overrides)
    body = {"consent_id": consent_id, "payee_account": "acct-shop", "amount_minor": amount, "currency": "USD"}
    return client.post("/v1/payments", json=body, headers={k: v for k, v in headers.items() if v is not None})


def test_the_whole_lesson_flow_over_http(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    assert consent["scope"] == "single-payment" and consent["revoked_at"] is None
    token = token_over_http(client, world)
    paid = pay_over_http(client, world, token, consent["consent_id"])
    assert paid.status_code == 201 and paid.json()["status"] == "POSTED" and "Idempotent-Replay" not in paid.headers
    replay = pay_over_http(client, world, token, consent["consent_id"])
    assert replay.status_code == 201 and replay.json() == paid.json() and replay.headers["Idempotent-Replay"] == "true"
    assert world.db.balance("acct-ada") == 47_000 and world.db.ledger_sum() == 0
    fetched = client.get(
        f"/v1/payments/{paid.json()['payment_id']}",
        headers={
            "Authorization": f"DPoP {token}",
            "DPoP": dpop(world.merchant, world, "GET", f"/v1/payments/{paid.json()['payment_id']}", token),
        },
    )
    assert fetched.status_code == 200 and fetched.json() == paid.json()


def test_the_banks_signature_on_a_result_verifies_against_its_published_key(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    paid = pay_over_http(client, world, token_over_http(client, world), consent["consent_id"])
    [bank_key] = client.get("/.well-known/bank-keys.json").json()["keys"]
    message = json.dumps(paid.json(), sort_keys=True, separators=(",", ":")).encode()
    assert keys.verify(bank_key, message, paid.headers["Bank-Signature"])
    assert not keys.verify(bank_key, message + b" ", paid.headers["Bank-Signature"])


def test_a_payment_without_a_credential_or_with_a_bad_one_is_a_401(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    token = token_over_http(client, world)
    for overrides in (
        {"Authorization": None},
        {"DPoP": None},
        {"Authorization": "DPoP nonsense"},
        {"Authorization": "Bearer " + token},
    ):
        response = pay_over_http(
            client, world, token, consent["consent_id"], key=f"k-{len(overrides)}-{id(overrides)}", **overrides
        )
        assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_client_credential"
        assert response.headers["WWW-Authenticate"].startswith("DPoP")
    assert world.db.payments == {}


def test_a_stolen_token_with_another_key_is_a_401(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    token = token_over_http(client, world)
    response = pay_over_http(
        client,
        world,
        token,
        consent["consent_id"],
        DPoP=dpop(world.other_merchant, world, "POST", "/v1/payments", token),
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "amount_exceeds_consent"),
        (404, "consent_not_found"),
        (400, "request_not_as_signed"),
        (422, "insufficient_funds"),
    ],
)
def test_refusals_map_to_statuses_and_keep_their_own_code(stack, status, code):
    client, world = stack
    token = token_over_http(client, world)
    consent = consent_over_http(client, world, maximum=90_000 if code == "insufficient_funds" else 5000)
    if code == "amount_exceeds_consent":
        response = pay_over_http(client, world, token, consent["consent_id"], amount=6000)
    elif code == "consent_not_found":
        response = pay_over_http(client, world, token, "00000000-0000-4000-8000-0000000000ff")
    elif code == "request_not_as_signed":
        response = pay_over_http(
            client,
            world,
            token,
            consent["consent_id"],
            **{"X-Request-Signature": keys.sign(world.merchant.private_pem, b"other")},
        )
    else:
        response = pay_over_http(client, world, token, consent["consent_id"], amount=60_000)
    assert response.status_code == status and response.json()["error"]["code"] == code


def test_a_missing_idempotency_key_and_a_reused_one_are_refused(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    token = token_over_http(client, world)
    missing = pay_over_http(client, world, token, consent["consent_id"], **{"Idempotency-Key": None})
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "invalid_idempotency_key"
    pay_over_http(client, world, token, consent["consent_id"], amount=3000, key="k-same")
    reused = pay_over_http(client, world, token, consent["consent_id"], amount=3100, key="k-same")
    assert reused.status_code == 422 and reused.json()["error"]["code"] == "idempotency_key_reused"


def test_validation_errors_are_400_and_never_echo_the_pin_or_the_code(stack):
    client, world = stack
    bad_bodies = [
        {"username": "ada", "pin": "9999", "terms": {**terms_json(), "max_amount_minor": 50.5}},
        {"username": "ada", "pin": "9999", "terms": {**terms_json(), "currency": "usd"}},
        {"username": "ada", "pin": "9999", "terms": {**terms_json(), "scope": "forever"}},
        {"username": "ada", "pin": "9999", "terms": terms_json(), "extra": 1},
    ]
    for body in bad_bodies:
        response = client.post("/v1/sca/challenges", json=body)
        assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"
        assert "9999" not in response.text
    started = client.post("/v1/sca/challenges", json={"username": "ada", "pin": world.ada_pin, "terms": terms_json()})
    bad_code = client.post(f"/v1/sca/challenges/{started.json()['challenge_id']}/verify", json={"code": "12ab56"})
    assert bad_code.status_code == 400 and "12ab56" not in bad_code.text


def test_wrong_and_locking_codes_over_http(stack):
    client, world = stack
    started = client.post(
        "/v1/sca/challenges", json={"username": "ada", "pin": world.ada_pin, "terms": terms_json()}
    ).json()
    url = f"/v1/sca/challenges/{started['challenge_id']}/verify"
    answers = [client.post(url, json={"code": "000000"}) for _ in range(3)]
    assert [a.json()["error"]["code"] for a in answers] == ["wrong_code", "wrong_code", "challenge_locked"]
    assert answers[0].json()["error"]["attempts_left"] == 2 and all(a.status_code == 401 for a in answers)
    assert client.post("/v1/sca/challenges/not-a-uuid/verify", json={"code": "000000"}).status_code == 404


def test_a_consent_without_a_proof_scheme_is_refused_and_a_wrong_pin_is_a_401(stack):
    client, world = stack
    assert client.post("/v1/consents", json={"terms": terms_json()}).status_code == 401
    assert (
        client.post("/v1/consents", json={"terms": terms_json()}, headers={"Authorization": "Bearer x"}).status_code
        == 401
    )
    wrong_pin = client.post("/v1/sca/challenges", json={"username": "ada", "pin": "000000", "terms": terms_json()})
    assert wrong_pin.status_code == 401 and wrong_pin.json()["error"]["code"] == "authentication_failed"
    not_mine = client.post(
        "/v1/sca/challenges",
        json={"username": "ada", "pin": world.ada_pin, "terms": terms_json(payer_account="acct-bob")},
    )
    assert not_mine.status_code == 403 and not_mine.json()["error"]["code"] == "account_not_owned"
    unknown = client.post(
        "/v1/sca/challenges", json={"username": "ada", "pin": world.ada_pin, "terms": terms_json(client_id="nobody")}
    )
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "unknown_client"


def test_revocation_over_http(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    wrong = client.post(f"/v1/consents/{consent['consent_id']}/revoke", json={"username": "ada", "pin": "000000"})
    assert wrong.status_code == 401
    revoked = client.post(
        f"/v1/consents/{consent['consent_id']}/revoke", json={"username": "ada", "pin": world.ada_pin}
    )
    assert revoked.status_code == 200 and revoked.json()["revoked_at"] is not None
    token = token_over_http(client, world)
    refused = pay_over_http(client, world, token, consent["consent_id"])
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "consent_revoked"
    missing = client.post("/v1/consents/not-a-uuid/revoke", json={"username": "ada", "pin": world.ada_pin})
    assert missing.status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic !!!"},
        {"Authorization": "Bearer abc"},
        {"Authorization": "Basic " + base64.b64encode(b"merchant-1:wrong").decode()},
    ],
)
def test_the_token_endpoint_refuses_bad_credentials(stack, headers):
    client, world = stack
    proof = dpop(world.merchant, world, "POST", "/oauth/token")
    response = client.post(
        "/oauth/token", data={"grant_type": "client_credentials"}, headers={**headers, "DPoP": proof}
    )
    assert response.status_code == 401


def test_the_token_endpoint_needs_the_client_credentials_grant_and_a_proof(stack):
    client, world = stack
    auth = (world.merchant.client_id, world.merchant.secret)
    proof = dpop(world.merchant, world, "POST", "/oauth/token")
    assert (
        client.post("/oauth/token", data={"grant_type": "password"}, auth=auth, headers={"DPoP": proof}).status_code
        == 401
    )
    assert client.post("/oauth/token", data={"grant_type": "client_credentials"}, auth=auth).status_code == 401


def test_health_live_and_a_down_dependency(stack):
    import dataclasses

    from open_banking.application.bank.queries import CheckHealth

    client, world = stack
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health").json() == {"status": "ok", "checks": {}}

    class Down:
        def check(self) -> None:
            raise ConnectionError("database unreachable")

    down = dataclasses.replace(world.bank, health=CheckHealth({"database": Down()}))
    with TestClient(create_bank_app(lambda: down), base_url=BANK_URL) as down_client:
        response = down_client.get("/health")
    assert response.status_code == 503 and response.json()["checks"] == {"database": "ConnectionError"}


def test_an_unexpected_error_is_a_generic_500_and_the_container_closes_on_shutdown(stack):
    import dataclasses

    _, world = stack
    closed: list[bool] = []

    class Boom:
        def execute(self, *args):
            raise RuntimeError("secret internals")

    broken = dataclasses.replace(world.bank, get_payment=Boom(), close=lambda: closed.append(True))
    with TestClient(create_bank_app(lambda: broken), base_url=BANK_URL, raise_server_exceptions=False) as client:
        token = token_over_http(client, world)
        response = client.get(
            "/v1/payments/x",
            headers={
                "Authorization": f"DPoP {token}",
                "DPoP": dpop(world.merchant, world, "GET", "/v1/payments/x", token),
            },
        )
        assert response.status_code == 500 and "secret" not in response.text
    assert closed == [True]


def test_logs_never_contain_the_pin_the_code_the_secret_or_a_token(stack):
    client, world = stack
    stream = io.StringIO()
    configure_logging("DEBUG", stream)
    try:
        consent = consent_over_http(client, world)
        token = token_over_http(client, world)
        pay_over_http(client, world, token, consent["consent_id"])
        client.post("/v1/sca/challenges", json={"username": "ada", "pin": "000000", "terms": terms_json()})
    finally:
        logging.getLogger().handlers.clear()
    code = world.phone.latest_code(world.db.customers_by_name("ada").customer_id)
    output = stream.getvalue()
    assert "payment posted" in output and "strong authentication succeeded" in output
    for secret in (world.ada_pin, code, world.merchant.secret, token, "000000"):
        assert secret not in output


class Bridge(httpx.BaseTransport):
    """Lets the merchant's real HTTP client talk to the bank app inside the test process."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        sent = self._client.send(request)
        return httpx.Response(sent.status_code, headers=sent.headers, content=sent.content)


def gateway(
    client: TestClient, world: World, script: FaultScript | None = None, attempts: int = 3, verify_results: bool = True
):
    transport = FaultInjectingTransport(Bridge(client), script or FaultScript())
    sleeps: list[float] = []
    instance = HttpBankGateway(
        httpx.Client(transport=transport),
        bank_url=BANK_URL,
        client_id=world.merchant.client_id,
        client_secret=world.merchant.secret,
        private_pem=world.merchant.private_pem,
        attempts=attempts,
        retry_base_delay_seconds=0.01,
        clock=world.clock.now,
        sleep=sleeps.append,
        bank_public_jwk=world.bank.signing_jwk if verify_results else None,
    )
    return instance, sleeps


def test_the_merchants_client_pays_and_checks_the_banks_signature(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    merchant, _ = gateway(client, world)
    result = merchant.initiate_payment(
        consent_id=consent["consent_id"], payee_account="acct-shop", amount=Money(3000, "USD"), idempotency_key="g-1"
    )
    assert isinstance(result, Paid) and world.db.balance("acct-shop") == 3000


def test_the_merchants_client_reports_the_banks_refusal(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    merchant, _ = gateway(client, world)
    result = merchant.initiate_payment(
        consent_id=consent["consent_id"], payee_account="acct-shop", amount=Money(6000, "USD"), idempotency_key="g-1"
    )
    assert result == Refused("amount_exceeds_consent", "amount 60.00 exceeds consent max 50.00")


def warm_up(merchant, client, world) -> None:
    """One ordinary payment, so the merchant's token is cached and scripted faults hit payment requests only."""
    consent = consent_over_http(client, world)
    merchant.initiate_payment(
        consent_id=consent["consent_id"],
        payee_account="acct-shop",
        amount=Money(1000, "USD"),
        idempotency_key="warm-up",
    )


def test_a_lost_response_is_retried_with_the_same_key_and_pays_once(stack):
    client, world = stack
    script = FaultScript()
    merchant, sleeps = gateway(client, world, script)
    warm_up(merchant, client, world)
    consent = consent_over_http(client, world)
    script.push(Fault.DROP_RESPONSE)
    result = merchant.initiate_payment(
        consent_id=consent["consent_id"], payee_account="acct-shop", amount=Money(3000, "USD"), idempotency_key="g-1"
    )
    assert isinstance(result, Paid) and len(world.db.payments) == 2
    assert world.db.balance("acct-ada") == 50_000 - 1000 - 3000 and len(sleeps) == 1


def test_when_every_answer_is_lost_the_merchant_gives_up_but_the_bank_paid_once(stack):
    client, world = stack
    script = FaultScript()
    merchant, _ = gateway(client, world, script)
    warm_up(merchant, client, world)
    consent = consent_over_http(client, world)
    script.push(Fault.DROP_RESPONSE, 3)
    with pytest.raises(BankUnavailableError, match="3 attempts"):
        merchant.initiate_payment(
            consent_id=consent["consent_id"],
            payee_account="acct-shop",
            amount=Money(3000, "USD"),
            idempotency_key="g-1",
        )
    assert len(world.db.payments) == 2 and world.db.balance("acct-ada") == 50_000 - 1000 - 3000
    again = merchant.initiate_payment(
        consent_id=consent["consent_id"], payee_account="acct-shop", amount=Money(3000, "USD"), idempotency_key="g-1"
    )
    assert isinstance(again, Paid) and len(world.db.payments) == 2  # the later retry finds the stored answer


def test_a_request_that_never_reaches_the_bank_is_retried_too(stack):
    client, world = stack
    script = FaultScript()
    merchant, _ = gateway(client, world, script)
    warm_up(merchant, client, world)
    consent = consent_over_http(client, world)
    script.push(Fault.DROP_REQUEST, 2)
    result = merchant.initiate_payment(
        consent_id=consent["consent_id"], payee_account="acct-shop", amount=Money(3000, "USD"), idempotency_key="g-1"
    )
    assert isinstance(result, Paid) and len(world.db.payments) == 2


def test_the_merchant_refuses_a_result_the_bank_did_not_sign(stack):
    client, world = stack
    consent = consent_over_http(client, world)
    merchant, _ = gateway(client, world)
    merchant._bank_public_jwk = keys.generate_keypair()[1]  # a different bank key than the one that signs
    with pytest.raises(ClientAuthenticationError, match="not signed by the bank"):
        merchant.initiate_payment(
            consent_id=consent["consent_id"],
            payee_account="acct-shop",
            amount=Money(3000, "USD"),
            idempotency_key="g-1",
        )


def test_the_merchants_client_fetches_a_new_token_when_the_old_one_expires(stack):
    client, world = stack
    merchant, _ = gateway(client, world)
    for index in range(2):
        consent = consent_over_http(client, world)
        result = merchant.initiate_payment(
            consent_id=consent["consent_id"],
            payee_account="acct-shop",
            amount=Money(1000, "USD"),
            idempotency_key=f"g-{index}",
        )
        assert isinstance(result, Paid)
        world.clock.advance(timedelta(minutes=10))
    assert len(world.db.tokens) == 2


def test_a_wrong_secret_stops_the_merchants_client_at_once(stack):
    client, world = stack
    merchant, _ = gateway(client, world)
    merchant._client_secret = "wrong"
    with pytest.raises(ClientAuthenticationError):
        merchant.initiate_payment(
            consent_id="c", payee_account="acct-shop", amount=Money(1, "USD"), idempotency_key="g-1"
        )
