"""The lesson's scenario through real processes: a bank, a merchant, and the customer's program, failures included."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import Stack, run_program, stop

pytestmark = pytest.mark.e2e


def approve(stack: Stack, *extra: str):
    return run_program(
        "customer_cli",
        "--bank-url",
        stack.bank.url,
        "approve",
        "--username",
        "ada",
        "--account",
        "acct-ada",
        "--payee",
        "acct-shop",
        "--merchant",
        "merchant-1",
        "--max",
        "50.00",
        "--pin-file",
        str(stack.directory / "ada.pin"),
        "--outbox",
        str(stack.outbox),
        *extra,
    )


def consent_id_of(completed) -> str:
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return str(json.loads(completed.stdout.strip().splitlines()[-1])["consent_id"])


def checkout(stack: Stack, amount_minor: int = 3000) -> dict[str, Any]:
    response = httpx.post(
        f"{stack.merchant.url}/v1/checkouts", json={"amount_minor": amount_minor, "currency": "USD"}, timeout=30
    )
    assert response.status_code == 201
    return dict(response.json())


def complete(stack: Stack, checkout_id: str, consent_id: str) -> httpx.Response:
    return httpx.post(
        f"{stack.merchant.url}/v1/checkouts/{checkout_id}/complete", json={"consent_id": consent_id}, timeout=60
    )


def balances(stack: Stack) -> dict[str, str]:
    completed = run_program(
        "admin_cli",
        "balances",
        "acct-ada",
        "acct-shop",
        env={"OTP_OUTBOX_FILE": str(stack.outbox), "BANK_SIGNING_KEY_FILE": str(stack.directory / "bank-key.pem")},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return dict(line.split(": ", 1) for line in completed.stdout.strip().splitlines())


def test_the_customer_approves_at_the_bank_and_the_merchant_is_paid_once(stack):
    before = balances(stack)
    created = checkout(stack)
    assert created["consent_request"]["client_id"] == "merchant-1" and created["status"] == "AWAITING_CONSENT"
    consent = consent_id_of(approve(stack))  # the customer and the bank only; the merchant is not involved
    done = complete(stack, created["checkout_id"], consent)
    assert done.status_code == 200 and done.json()["status"] == "PAID"
    again = complete(stack, created["checkout_id"], consent)
    assert again.status_code == 200 and again.json()["payment_id"] == done.json()["payment_id"]
    after = balances(stack)
    assert (
        before["acct-ada"] == "500.00 USD" and after["acct-ada"] == "470.00 USD" and after["acct-shop"] == "30.00 USD"
    )
    assert after["ledger sum across every posting"] == "0.00"


def test_an_abandoned_authentication_creates_no_consent_and_nothing_to_pay_with(stack):
    abandoned = approve(stack, "--abandon")
    assert abandoned.returncode == 0 and "no consent created" in abandoned.stdout
    created = checkout(stack)
    refused = complete(stack, created["checkout_id"], "00000000-0000-4000-8000-0000000000ff")
    assert refused.status_code == 403 and refused.json()["refusal"]["code"] == "consent_not_found"
    assert balances(stack)["acct-ada"] == "470.00 USD"


def test_a_wrong_pin_is_refused_by_the_bank(stack, tmp_path):
    wrong = tmp_path / "wrong.pin"
    wrong.write_text("000000\n")
    completed = run_program(
        "customer_cli", "--bank-url", stack.bank.url, "approve", "--username", "ada", "--account", "acct-ada",
        "--payee", "acct-shop", "--merchant", "merchant-1", "--max", "50.00", "--pin-file", str(wrong),
        "--outbox", str(stack.outbox),
    )  # fmt: skip
    assert completed.returncode == 1 and "authentication_failed" in completed.stdout


def test_a_consent_the_customer_revoked_moves_no_money(stack):
    consent = consent_id_of(approve(stack))
    revoked = run_program(
        "customer_cli", "--bank-url", stack.bank.url, "revoke", "--username", "ada", "--consent-id", consent,
        "--pin-file", str(stack.directory / "ada.pin"),
    )  # fmt: skip
    assert revoked.returncode == 0 and "revoked" in revoked.stdout
    refused = complete(stack, checkout(stack)["checkout_id"], consent)
    assert refused.status_code == 403 and refused.json()["refusal"]["code"] == "consent_revoked"
    assert balances(stack)["acct-ada"] == "470.00 USD"


def test_the_servers_report_healthy_and_publish_the_banks_key(stack):
    assert httpx.get(f"{stack.bank.url}/health", timeout=10).json() == {"status": "ok", "checks": {"database": "ok"}}
    assert httpx.get(f"{stack.merchant.url}/health", timeout=10).json() == {"status": "ok"}
    assert len(httpx.get(f"{stack.bank.url}/.well-known/bank-keys.json", timeout=10).json()["keys"]) == 1


def test_both_servers_stop_gracefully_and_their_logs_hold_no_secrets(stack):
    codes = [json.loads(line)["code"] for line in stack.outbox.read_text().splitlines()]
    results = {server.name: stop(server) for server in (stack.merchant, stack.bank)}
    for name, (code, output) in results.items():
        assert code == 0, (name, output)
        assert "shutdown complete" in output and "level=ERROR" not in output
        for secret in (stack.pin, stack.client_secret, *codes):
            assert secret not in output, (name, "a secret reached the log")
        assert "payment posted" in output or name == "merchant"
    assert "checkout paid" in results["merchant"][1]


def test_the_demo_program_runs_the_whole_scenario_and_exits_zero():
    completed = run_program("demo_cli")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    for line in (
        "consent without authentication -> 401",
        "abandoned authentication: the code was sent",
        "three wrong codes -> wrong_code, wrong_code, challenge_locked",
        "a proof for 50.00 used for a consent of 5,000.00 -> 401",
        "a stolen token used with another key -> 401",
        "another merchant presents this consent -> consent_not_granted_to_caller",
        "amount=60.00                    -> 403 amount_exceeds_consent: amount 60.00 exceeds consent max 50.00",
        "payee_account=acct-other-shop   -> 403 payee_not_covered: consent does not cover this payee",
        "amount=30.00, correct payee     -> Success",
        "clock 1 second past expires_at  -> 403 consent_expired: consent expired",
        "Idempotent-Replay=true",
        "amount=35.00, signature covers amount=30.00 -> 400 request_not_as_signed",
        "revoked consent: revoke -> 200; initiate -> 403 consent_revoked: consent revoked",
        "one payment, not two",
        "period_cap_exceeded",
        "ledger sum across every posting: 0.00",
    ):
        assert line in completed.stdout, line
    assert "FAILED" not in completed.stdout
