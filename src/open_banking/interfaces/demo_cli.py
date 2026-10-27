"""`open-banking-demo`: the lesson's scenario with a bank and a merchant server running in this process.

Customer actions go straight to the bank over HTTP (the merchant is never in that path). Each section prints what the
lesson's "Expected output" shows and the program exits non-zero if the system does not behave.
"""

from __future__ import annotations

import argparse
import io
import socket
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from open_banking.application.merchant.checkout import BankUnavailableError
from open_banking.domain.money import Money
from open_banking.domain.signing import canonical_payment_request
from open_banking.infrastructure import keys
from open_banking.infrastructure.bank_settings import BankConfig
from open_banking.infrastructure.faults import Fault
from open_banking.infrastructure.logging import configure_logging
from open_banking.infrastructure.otp import FileOtpChannel
from open_banking.infrastructure.settings import ConfigurationError, MerchantConfig, ServerSettings
from open_banking.infrastructure.system import OffsetClock
from open_banking.interfaces.admin_cli import register_merchant
from open_banking.interfaces.bank_api import create_bank_app
from open_banking.interfaces.bank_container import BankContainer, build_bank_container
from open_banking.interfaces.merchant_api import create_merchant_app
from open_banking.interfaces.merchant_container import MerchantContainer, build_merchant_container


class ScenarioError(AssertionError):
    """The system did something the lesson says it must not."""


def expect(condition: bool, message: str) -> None:
    """Raise `ScenarioError` with `message` unless `condition` holds."""
    if not condition:
        raise ScenarioError(message)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve_in_thread(app: Any, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise ScenarioError("a demo server did not start")
        time.sleep(0.05)
    return server, thread


@dataclass
class Person:
    """A customer's credentials, as the demo holds them (read from the files the admin step wrote)."""

    username: str
    account: str
    pin: str


@dataclass
class Stack:
    """Everything the scenario talks to."""

    bank: BankContainer
    merchant: MerchantContainer
    other_merchant: MerchantContainer
    clock: OffsetClock
    bank_url: str
    merchant_url: str
    phone: FileOtpChannel
    ada: Person
    bob: Person
    merchant_key: str
    other_key: str
    merchant_credentials: tuple[str, str]
    http: httpx.Client


def _merchant_config(bank_url: str, port: int, client_id: str, payee: str, out: Path) -> MerchantConfig:
    values = dict(
        line.split("=", 1)
        for line in (out / f"{client_id}.env").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return MerchantConfig(
        server=ServerSettings("127.0.0.1", port, 10),
        bank_url=bank_url,
        client_id=client_id,
        client_secret=values["MERCHANT_CLIENT_SECRET"],
        private_key_file=Path(values["MERCHANT_PRIVATE_KEY_FILE"]),
        payee_account=payee,
        request_timeout_seconds=10.0,
        attempts=3,
        retry_base_delay_ms=20,
        log_level="WARNING",
    )


@contextmanager
def running_stack(out: Path) -> Iterator[Stack]:
    """Start the bank and the merchant, register two merchants, enroll two customers; stop everything afterwards."""
    base = BankConfig.from_env()
    bank_port, merchant_port = _free_port(), _free_port()
    bank_url = f"http://127.0.0.1:{bank_port}"
    config = BankConfig(
        postgres=base.postgres,
        server=ServerSettings("127.0.0.1", bank_port, 10),
        public_url=bank_url,
        timings=base.timings,
        pepper=base.pepper,
        signing_key_file=out / "bank-signing-key.pem",
        otp_outbox_file=out / "otp-outbox.log",
        scrypt_n=base.scrypt_n,
        log_level="WARNING",
    )
    clock = OffsetClock()
    bank = build_bank_container(config, clock=clock)
    people: dict[str, Person] = {}
    for username, account in (("ada", "acct-ada"), ("bob", "acct-bob")):
        pin = f"{uuid.uuid4().int % 10**6:06d}"
        bank.enroll_customer.execute(username, pin, account)
        people[username] = Person(username, account, pin)
    register_merchant(bank, "merchant-1", "Shop", out)
    register_merchant(bank, "merchant-2", "Other Shop", out)
    shop = _merchant_config(bank_url, merchant_port, "merchant-1", "acct-shop", out)
    other = _merchant_config(bank_url, _free_port(), "merchant-2", "acct-other-shop", out)
    merchant = build_merchant_container(shop, clock=clock.now, bank_public_jwk=bank.signing_jwk)
    other_merchant = build_merchant_container(other, clock=clock.now, bank_public_jwk=bank.signing_jwk)
    bank_server, bank_thread = _serve_in_thread(create_bank_app(lambda: bank), bank_port)
    merchant_server, merchant_thread = _serve_in_thread(
        create_merchant_app(lambda: merchant, shop.payee_account), merchant_port
    )
    http = httpx.Client(timeout=30.0)
    try:
        yield Stack(
            bank=bank,
            merchant=merchant,
            other_merchant=other_merchant,
            clock=clock,
            bank_url=bank_url,
            merchant_url=f"http://127.0.0.1:{merchant_port}",
            phone=FileOtpChannel(config.otp_outbox_file),
            ada=people["ada"],
            bob=people["bob"],
            merchant_key=shop.private_key_file.read_text(encoding="utf-8"),
            other_key=other.private_key_file.read_text(encoding="utf-8"),
            merchant_credentials=(shop.client_id, shop.client_secret),
            http=http,
        )
    finally:
        http.close()
        for server, thread in ((merchant_server, merchant_thread), (bank_server, bank_thread)):
            server.should_exit = True
            thread.join(timeout=15)
        merchant.close()
        other_merchant.close()
        bank.close()


def _terms(person: Person, payee: str, merchant: str, max_minor: int, **extra: Any) -> dict[str, Any]:
    return {
        "payer_account": person.account,
        "payee_account": payee,
        "client_id": merchant,
        "max_amount_minor": max_minor,
        "currency": "USD",
        "scope": "single-payment",
        "period_seconds": None,
        **extra,
    }


def authenticate(stack: Stack, person: Person, terms: dict[str, Any]) -> str:
    """The customer, at the bank only: PIN, then the code from the phone. Returns the proof of authentication."""
    started = stack.http.post(
        f"{stack.bank_url}/v1/sca/challenges", json={"username": person.username, "pin": person.pin, "terms": terms}
    )
    expect(started.status_code == 201, f"challenge refused: {started.text}")
    code = stack.phone.latest_any()
    expect(code is not None, "no code reached the phone")
    verified = stack.http.post(
        f"{stack.bank_url}/v1/sca/challenges/{started.json()['challenge_id']}/verify", json={"code": code}
    )
    expect(verified.status_code == 200, f"code refused: {verified.text}")
    return str(verified.json()["authentication_proof"])


def grant(stack: Stack, person: Person, terms: dict[str, Any]) -> dict[str, Any]:
    """Authenticate and create a consent. Returns the consent."""
    proof = authenticate(stack, person, terms)
    response = stack.http.post(
        f"{stack.bank_url}/v1/consents", json={"terms": terms}, headers={"Authorization": f"SCA {proof}"}
    )
    expect(response.status_code == 201, f"consent refused: {response.text}")
    consent: dict[str, Any] = response.json()
    return consent


def raw_payment(
    stack: Stack,
    *,
    consent_id: str,
    amount_minor: int,
    key: str,
    payee: str = "acct-shop",
    signed_minor: int | None = None,
    signing_key: str | None = None,
    token_holder_key: str | None = None,
    token: str | None = None,
    client_id: str = "merchant-1",
    client_secret: str | None = None,
    send_credential: bool = True,
) -> httpx.Response:
    """A payment request built by hand, so the scenario can break one thing at a time."""
    private = signing_key or stack.merchant_key
    public = keys.public_jwk_of(private)
    url = f"{stack.bank_url}/v1/payments"
    headers: dict[str, str] = {"Idempotency-Key": key}
    if send_credential:
        access = token or _token(stack, client_id, client_secret or stack.merchant_credentials[1], private)
        holder = token_holder_key or private
        headers["Authorization"] = f"DPoP {access}"
        headers["DPoP"] = keys.build_dpop_proof(
            holder, keys.public_jwk_of(holder), method="POST", url=url, now=stack.clock.now(), access_token=access
        )
    signed = Money(signed_minor if signed_minor is not None else amount_minor, "USD")
    headers["X-Request-Signature"] = keys.sign(
        private,
        canonical_payment_request(
            client_id=client_id, consent_id=consent_id, payee_account=payee, amount=signed, idempotency_key=key
        ),
    )
    _ = public
    body = {"consent_id": consent_id, "payee_account": payee, "amount_minor": amount_minor, "currency": "USD"}
    return stack.http.post(url, json=body, headers=headers)


def token_request(stack: Stack, client_id: str, secret: str, private: str) -> httpx.Response:
    """Ask the bank for a token with a client secret and a proof made by `private`."""
    url = f"{stack.bank_url}/oauth/token"
    proof = keys.build_dpop_proof(private, keys.public_jwk_of(private), method="POST", url=url, now=stack.clock.now())
    return stack.http.post(
        url, data={"grant_type": "client_credentials"}, auth=(client_id, secret), headers={"DPoP": proof}
    )


def _token(stack: Stack, client_id: str, secret: str, private: str) -> str:
    response = token_request(stack, client_id, secret, private)
    expect(response.status_code == 200, f"token refused: {response.text}")
    return str(response.json()["access_token"])


def balances(stack: Stack) -> dict[str, int]:
    """The balances the scenario watches, in minor units."""
    found, _ = stack.bank.balances.execute(["acct-ada", "acct-bob", "acct-shop", "acct-other-shop"])
    return {account: money.minor for account, money in found.items()}


def ledger_total(stack: Stack) -> int:
    """The sum of every posting at the bank."""
    return stack.bank.balances.execute(["acct-ada"])[1].minor


def error_line(response: httpx.Response) -> str:
    """`status code: message` of an error response."""
    error = response.json()["error"]
    return f"{response.status_code} {error['code']}: {error['message']}"


def section_trust(stack: Stack) -> None:
    """Step 1: where the trust lies, and what the merchant can reach."""
    print("== 1. The parties and the trust")
    print("customer <-> bank:     strong customer authentication (PIN, then a code on the customer's device), direct")
    print("merchant <-> bank:     client secret + a token bound to the merchant's own key, a fresh proof per request")
    print("customer <-> merchant: the customer trusts the merchant to ask for what was agreed; the bank checks it")
    print("the merchant's client calls only: POST /oauth/token and POST /v1/payments")
    print("the merchant process imports none of the bank's authentication code (enforced by `make lint`)")
    print(
        "the customer's PIN and the code never leave customer and bank; "
        f"the merchant holds {stack.merchant_credentials[0]}'s key and nothing else"
    )
    print()


def section_consent(stack: Stack) -> dict[str, Any]:
    """Step 2: consent exists only after strong authentication."""
    print("== 2. Consent is an object, and it exists only after strong authentication")
    terms = _terms(stack.ada, "acct-shop", "merchant-1", 5000)
    response = stack.http.post(f"{stack.bank_url}/v1/consents", json={"terms": terms})
    print(f"consent without authentication -> {error_line(response)}")
    expect(response.status_code == 401, "a consent without a proof must be refused")
    response = stack.http.post(
        f"{stack.bank_url}/v1/sca/challenges", json={"username": "ada", "pin": "000000", "terms": terms}
    )
    print(f"wrong PIN -> {error_line(response)}")
    expect(response.status_code == 401, "a wrong PIN must be refused")
    count = _consent_count(stack)
    started = stack.http.post(
        f"{stack.bank_url}/v1/sca/challenges", json={"username": "ada", "pin": stack.ada.pin, "terms": terms}
    )
    print(f"abandoned authentication: the code was sent ({started.json()['challenge_id'][:8]}...) and never entered")
    expect(_consent_count(stack) == count, "an abandoned authentication must not create a consent")
    print(f"consents before and after: {count} and {_consent_count(stack)}")
    locked = [
        stack.http.post(
            f"{stack.bank_url}/v1/sca/challenges/{started.json()['challenge_id']}/verify", json={"code": "000000"}
        )
        for _ in range(3)
    ]
    print("three wrong codes -> " + ", ".join(response.json()["error"]["code"] for response in locked))
    expect(locked[-1].json()["error"]["code"] == "challenge_locked", "three wrong codes must lock the challenge")
    proof = authenticate(stack, stack.ada, terms)
    bigger = _terms(stack.ada, "acct-shop", "merchant-1", 500_000)
    response = stack.http.post(
        f"{stack.bank_url}/v1/consents", json={"terms": bigger}, headers={"Authorization": f"SCA {proof}"}
    )
    print(f"a proof for 50.00 used for a consent of 5,000.00 -> {error_line(response)}")
    expect(response.status_code == 401, "a proof must be tied to the terms the customer saw")
    consent = stack.http.post(
        f"{stack.bank_url}/v1/consents", json={"terms": terms}, headers={"Authorization": f"SCA {proof}"}
    ).json()
    print(
        f"consent_id={consent['consent_id']}, scope={consent['scope']}, "
        f"max={Money(consent['max_amount_minor'], 'USD').format()}, "
        f"expires_at={consent['expires_at']} (10 minutes from now)"
    )
    again = stack.http.post(
        f"{stack.bank_url}/v1/consents", json={"terms": terms}, headers={"Authorization": f"SCA {proof}"}
    )
    print(f"the same proof used again -> {error_line(again)}")
    expect(again.status_code == 401, "a proof is good for one consent")
    print()
    return dict(consent)


def _consent_count(stack: Stack) -> int:
    return stack.bank.balances.counts().consents


def section_merchant_access(stack: Stack, consent: dict[str, Any]) -> None:
    """Step 3: the merchant-to-bank API."""
    print("== 3. The merchant-to-bank API refuses what it should")
    key = f"s3-{uuid.uuid4().hex[:8]}"
    response = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=3000, key=key, send_credential=False)
    print(f"no merchant credential -> {error_line(response)}")
    expect(response.status_code == 401, "a request with no credential must be refused")
    token = _token(stack, "merchant-1", stack.merchant_credentials[1], stack.merchant_key)
    response = raw_payment(
        stack,
        consent_id=consent["consent_id"],
        amount_minor=3000,
        key=key,
        token=token,
        token_holder_key=stack.other_key,
    )
    print(f"a stolen token used with another key -> {error_line(response)}")
    expect(response.status_code == 401, "a token must only work with the key it is bound to")
    response = token_request(stack, "merchant-1", "wrong-secret-value", stack.merchant_key)
    print(f"a wrong client secret at the token endpoint -> {error_line(response)}")
    expect(response.status_code == 401, "a wrong secret must be refused")
    token_url = f"{stack.bank_url}/oauth/token"
    proof = keys.build_dpop_proof(
        stack.merchant_key, keys.public_jwk_of(stack.merchant_key), method="POST", url=token_url, now=stack.clock.now()
    )
    form = {"grant_type": "client_credentials"}
    first = stack.http.post(token_url, data=form, auth=stack.merchant_credentials, headers={"DPoP": proof})
    replay = stack.http.post(token_url, data=form, auth=stack.merchant_credentials, headers={"DPoP": proof})
    print(f"a proof of possession used twice -> {first.status_code}, then {error_line(replay)}")
    expect(first.status_code == 200 and replay.status_code == 401, "a captured proof must not be replayable")
    other = stack.other_merchant.complete_checkout
    checkout = stack.other_merchant.create_checkout.execute(Money(3000, "USD"))
    refused = other.execute(checkout.checkout_id, consent["consent_id"])
    print(
        "another merchant presents this consent -> "
        f"{refused.refusal.code if refused.refusal else 'paid'}: {refused.refusal.message if refused.refusal else ''}"
    )
    expect(
        refused.refusal is not None and refused.refusal.code == "consent_not_granted_to_caller",
        "another merchant's consent",
    )
    print()


def section_within_consent(stack: Stack) -> dict[str, Any]:
    """Step 4: each rule of the consent, one refusal at a time, then a payment through the merchant."""
    print("== 4. A payment within consent succeeds; over the amount, another payee and expiry are each refused")
    consent = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    for label, kwargs in (
        ("amount=60.00", {"amount_minor": 6000}),
        ("payee_account=acct-other-shop", {"amount_minor": 3000, "payee": "acct-other-shop"}),
    ):
        response = raw_payment(stack, consent_id=consent["consent_id"], key=f"s4-{uuid.uuid4().hex[:8]}", **kwargs)
        print(f"{label:<32}-> {error_line(response)}")
        expect(response.status_code == 403, f"{label} must be refused")
    before = balances(stack)
    created = stack.http.post(f"{stack.merchant_url}/v1/checkouts", json={"amount_minor": 3000, "currency": "USD"})
    checkout = created.json()
    done = stack.http.post(
        f"{stack.merchant_url}/v1/checkouts/{checkout['checkout_id']}/complete",
        json={"consent_id": consent["consent_id"]},
    ).json()
    after = balances(stack)
    print(f"amount=30.00, correct payee     -> Success, payment {done['payment_id']}, status {done['status']}")
    print(
        f"ada {Money(before['acct-ada'], 'USD').format()} -> {Money(after['acct-ada'], 'USD').format()}; "
        f"shop {Money(before['acct-shop'], 'USD').format()} -> {Money(after['acct-shop'], 'USD').format()}; "
        f"ledger sum {Money(ledger_total(stack), 'USD').format()}"
    )
    expect(done["status"] == "PAID", "a payment within the consent must succeed")
    expect(
        before["acct-ada"] - after["acct-ada"] == 3000 and after["acct-shop"] - before["acct-shop"] == 3000,
        "30.00 moved",
    )
    expect(ledger_total(stack) == 0, "the ledger must sum to zero")
    expired = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    stack.clock.advance(timedelta(minutes=10, seconds=1))
    response = raw_payment(stack, consent_id=expired["consent_id"], amount_minor=3000, key=f"s4-{uuid.uuid4().hex[:8]}")
    print(f"clock 1 second past expires_at  -> {error_line(response)}")
    expect(response.status_code == 403 and response.json()["error"]["code"] == "consent_expired", "an expired consent")
    print()
    return dict(consent)


def section_replay_and_tampering(stack: Stack) -> None:
    """Step 5: once only, and tamper-evident."""
    print("== 5. A replay moves money once; a changed amount is rejected")
    consent = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    before = balances(stack)
    first = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=3000, key="k-1-" + uuid.uuid4().hex[:6])
    key = first.request.headers["Idempotency-Key"]
    second = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=3000, key=key)
    after = balances(stack)
    print(f"initiate (key {key})  -> {first.status_code}, payment {first.json()['payment_id']} posted once")
    print(
        f"initiate (same key)    -> {second.status_code}, same payment {second.json()['payment_id']}, "
        f"Idempotent-Replay={second.headers.get('Idempotent-Replay')}, "
        f"ada debited {before['acct-ada'] - after['acct-ada']} minor units in total"
    )
    expect(
        first.json() == second.json() and second.headers.get("Idempotent-Replay") == "true",
        "the replay must answer the same",
    )
    expect(before["acct-ada"] - after["acct-ada"] == 3000, "a replay must not move money twice")
    another = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=3000, key="k-2-" + uuid.uuid4().hex[:6])
    print(f"a new key under the same single-payment consent -> {error_line(another)}")
    expect(another.json()["error"]["code"] == "consent_already_used", "a single-payment consent is used once")
    fresh = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    tampered = raw_payment(
        stack, consent_id=fresh["consent_id"], amount_minor=3500, signed_minor=3000, key="k-3-" + uuid.uuid4().hex[:6]
    )
    print(f"amount=35.00, signature covers amount=30.00 -> {error_line(tampered)}")
    expect(tampered.json()["error"]["code"] == "request_not_as_signed", "a changed amount must be detected")
    expect(balances(stack)["acct-ada"] == after["acct-ada"], "a tampered request must move no money")
    print()


def section_failure_paths(stack: Stack) -> None:
    """Step 6: abandonment, revocation, a lost response."""
    print("== 6. Failure paths leave a consistent state")
    response = raw_payment(stack, consent_id=str(uuid.uuid4()), amount_minor=3000, key="s6-" + uuid.uuid4().hex[:6])
    print(f"abandoned authentication: no consent exists; initiate against a fabricated id -> {error_line(response)}")
    expect(response.json()["error"]["code"] == "consent_not_found", "no consent, no payment")
    revoked = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    before = balances(stack)
    gone = stack.http.post(
        f"{stack.bank_url}/v1/consents/{revoked['consent_id']}/revoke", json={"username": "ada", "pin": stack.ada.pin}
    )
    response = raw_payment(stack, consent_id=revoked["consent_id"], amount_minor=3000, key="s6-" + uuid.uuid4().hex[:6])
    print(f"revoked consent: revoke -> {gone.status_code}; initiate -> {error_line(response)}")
    expect(
        response.json()["error"]["code"] == "consent_revoked" and balances(stack) == before,
        "a revoked consent moves no money",
    )
    consent = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    before = balances(stack)
    checkout = stack.merchant.create_checkout.execute(Money(3000, "USD"))
    stack.merchant.faults.push(Fault.DROP_RESPONSE, 1)
    paid = stack.merchant.complete_checkout.execute(checkout.checkout_id, consent["consent_id"])
    after = balances(stack)
    print(
        f"bank timeout: the bank posted the payment, the answer was lost, the merchant retried with the same key -> "
        f"{paid.status.value}, payment {paid.payment_id}"
    )
    expect(before["acct-ada"] - after["acct-ada"] == 3000, "a timeout and a retry must move money exactly once")
    consent = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))
    before = balances(stack)
    checkout = stack.merchant.create_checkout.execute(Money(2000, "USD"))
    stack.merchant.faults.push(Fault.DROP_RESPONSE, 3)
    try:
        stack.merchant.complete_checkout.execute(checkout.checkout_id, consent["consent_id"])
    except BankUnavailableError as error:
        status = stack.merchant.checkouts.get(checkout.checkout_id)
        print(
            f"every answer lost -> the merchant gives up: {type(error).__name__}, "
            f"checkout {status and status.status.value}"
        )
    else:
        raise ScenarioError("three lost answers must exhaust the merchant's attempts")
    settled = stack.merchant.complete_checkout.execute(checkout.checkout_id, consent["consent_id"])
    after = balances(stack)
    print(
        f"completing again later -> {settled.status.value}; ada debited {before['acct-ada'] - after['acct-ada']} "
        "minor units: one payment, not two"
    )
    expect(before["acct-ada"] - after["acct-ada"] == 2000, "the unknown outcome must reconcile to exactly one payment")
    print()


def section_stretch(stack: Stack) -> None:
    """The stretch goals: a recurring cap, revocation in flight, the bank's signature on results."""
    print("== 7. Stretch: a recurring consent with a period cap, revocation in flight, signed results")
    terms = _terms(stack.ada, "acct-shop", "merchant-1", 10_000, scope="recurring", period_seconds=30 * 86400)
    consent = grant(stack, stack.ada, terms)
    first = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=6000, key="r1-" + uuid.uuid4().hex[:6])
    second = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=5000, key="r2-" + uuid.uuid4().hex[:6])
    print(
        f"recurring, cap 100.00 per 30 days: pay 60.00 -> {first.status_code}; pay 50.00 more -> {error_line(second)}"
    )
    expect(first.status_code == 201 and second.json()["error"]["code"] == "period_cap_exceeded", "the period cap")
    stack.clock.advance(timedelta(days=31))
    third = raw_payment(stack, consent_id=consent["consent_id"], amount_minor=5000, key="r3-" + uuid.uuid4().hex[:6])
    print(f"31 days later the window has moved: pay 50.00 -> {third.status_code}")
    expect(third.status_code == 201, "a new period has room")
    outcomes = _revocation_race(stack, rounds=12)
    print(
        f"revocation in flight, {len(outcomes)} races: "
        + ", ".join(f"{name} {count}" for name, count in sorted(_tally(outcomes).items()))
    )
    expect(all(outcomes), "no payment may be made after its consent was revoked")
    signed = stack.merchant.complete_checkout.execute(
        stack.merchant.create_checkout.execute(Money(1000, "USD")).checkout_id,
        grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 5000))["consent_id"],
    )
    print(f"the merchant verified the bank's signature on the result of payment {signed.payment_id}")
    expect(signed.payment_id is not None, "signed result")
    print()


def _tally(outcomes: list[bool]) -> dict[str, int]:
    return {"consistent": sum(outcomes), "inconsistent": len(outcomes) - sum(outcomes)}


def _revocation_race(stack: Stack, rounds: int) -> list[bool]:
    """Pay and revoke at the same moment, `rounds` times. Consistent means: a payment exists only if it came first."""
    results: list[bool] = []
    for _ in range(rounds):
        consent = grant(stack, stack.ada, _terms(stack.ada, "acct-shop", "merchant-1", 500))
        barrier = threading.Barrier(2)

        def pay(consent_id: str = consent["consent_id"], barrier: threading.Barrier = barrier) -> httpx.Response:
            barrier.wait(timeout=30)
            return raw_payment(stack, consent_id=consent_id, amount_minor=100, key="race-" + uuid.uuid4().hex[:8])

        def revoke(consent_id: str = consent["consent_id"], barrier: threading.Barrier = barrier) -> httpx.Response:
            barrier.wait(timeout=30)
            return stack.http.post(
                f"{stack.bank_url}/v1/consents/{consent_id}/revoke", json={"username": "ada", "pin": stack.ada.pin}
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            payment, revocation = pool.submit(pay), pool.submit(revoke)
            paid, revoked = payment.result(timeout=60), revocation.result(timeout=60)
        with stack.bank.uow_factory() as uow:
            stored = uow.consents.get_for_update(consent["consent_id"])
        assert stored is not None
        assert stored.revoked_at is not None
        assert revoked.status_code == 200
        if paid.status_code == 201:
            with stack.bank.uow_factory() as uow:
                payment_row = uow.payments.get(paid.json()["payment_id"], "merchant-1")
            results.append(payment_row is not None and payment_row.created_at <= stored.revoked_at)
        else:
            results.append(paid.json()["error"]["code"] == "consent_revoked")
    return results


def section_final(stack: Stack) -> None:
    """The books after everything."""
    print("== 8. The books")
    found = balances(stack)
    print(", ".join(f"{account} {Money(minor, 'USD').format()}" for account, minor in found.items()))
    print(f"ledger sum across every posting: {Money(ledger_total(stack), 'USD').format()}")
    expect(ledger_total(stack) == 0, "the ledger must sum to zero")
    expect(
        found["acct-ada"] + found["acct-bob"] + found["acct-shop"] + found["acct-other-shop"] == 70_000,
        "no money created or lost",
    )


def run_scenario(out: Path) -> None:
    """Run every section."""
    with running_stack(out) as stack:
        section_trust(stack)
        consent = section_consent(stack)
        section_merchant_access(stack, consent)
        section_within_consent(stack)
        section_replay_and_tampering(stack)
        section_failure_paths(stack)
        section_stretch(stack)
        section_final(stack)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the scenario; exit 0 when it behaved, 1 when the system broke a rule, 2 for a configuration error."""
    parser = argparse.ArgumentParser(prog="open-banking-demo", description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=Path("var"), help="where the demo's keys and files go (default var/)"
    )
    args = parser.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    configure_logging("ERROR")
    try:
        run_scenario(args.out)
    except ConfigurationError as error:
        print(f"configuration error: {error}")
        return 2
    except ScenarioError as failure:
        print(f"FAILED: {failure}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
