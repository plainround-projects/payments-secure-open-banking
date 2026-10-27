"""`open-banking-customer`: the customer's side. It talks to the bank directly; the merchant is never in the path.

`approve` authenticates (PIN, then the one-time code) and creates a consent; it prints the consent id, which the
customer takes back to the merchant. `revoke` withdraws a consent.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from open_banking.infrastructure.otp import FileOtpChannel


class CustomerError(Exception):
    """The bank said no, or the input was wrong."""


def _terms(args: argparse.Namespace, payer_account: str) -> dict[str, Any]:
    try:
        minor = int(Decimal(args.max) * 100)
    except InvalidOperation as error:
        raise CustomerError(f"--max must be an amount such as 50.00, got {args.max!r}") from error
    return {
        "payer_account": payer_account,
        "payee_account": args.payee,
        "client_id": args.merchant,
        "max_amount_minor": minor,
        "currency": args.currency,
        "scope": args.scope,
        "period_seconds": None if args.scope == "single-payment" else int(args.period_days) * 86400,
    }


def _check(response: httpx.Response) -> dict[str, Any]:
    payload: dict[str, Any] = response.json()
    if response.status_code >= 400:
        error = payload.get("error", {})
        raise CustomerError(f"{error.get('code', response.status_code)}: {error.get('message', response.text)}")
    return payload


def approve(args: argparse.Namespace, pin: str, read_code: Any) -> str | None:
    """Start the challenge, enter the code, create the consent. Returns the consent id, or None when abandoned."""
    with httpx.Client(base_url=args.bank_url, timeout=15.0) as bank:
        challenge = _check(
            bank.post(
                "/v1/sca/challenges",
                json={"username": args.username, "pin": pin, "terms": _terms(args, args.account)},
            )
        )
        print(f"a code was sent to your device; challenge {challenge['challenge_id']}")
        if args.abandon:
            print("abandoned: no code entered, no consent created")
            return None
        proof = _check(
            bank.post(f"/v1/sca/challenges/{challenge['challenge_id']}/verify", json={"code": read_code(challenge)})
        )
        consent = _check(
            bank.post(
                "/v1/consents",
                json={"terms": _terms(args, args.account)},
                headers={"Authorization": f"SCA {proof['authentication_proof']}"},
            )
        )
        print(json.dumps({"consent_id": consent["consent_id"], "expires_at": consent["expires_at"]}))
        return str(consent["consent_id"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="open-banking-customer", description=__doc__)
    parser.add_argument("--bank-url", default=os.environ.get("BANK_URL", "http://127.0.0.1:8463"))
    commands = parser.add_subparsers(dest="command", required=True)
    approve_parser = commands.add_parser("approve", help="authenticate and create a consent")
    approve_parser.add_argument("--username", required=True)
    approve_parser.add_argument("--account", required=True, help="the payer account (yours)")
    approve_parser.add_argument("--payee", required=True, help="the account the merchant is paid into")
    approve_parser.add_argument("--merchant", required=True, help="the merchant's client id")
    approve_parser.add_argument("--max", required=True, help="the most the merchant may take, such as 50.00")
    approve_parser.add_argument("--currency", default="USD")
    approve_parser.add_argument("--scope", choices=["single-payment", "recurring"], default="single-payment")
    approve_parser.add_argument("--period-days", default="30", help="the period of a recurring consent's cap")
    approve_parser.add_argument("--pin-file", type=Path, help="read the PIN from this file instead of asking")
    approve_parser.add_argument("--code", help="the one-time code (otherwise read from --outbox, or asked)")
    approve_parser.add_argument("--outbox", type=Path, help="read the code from the stand-in phone file")
    approve_parser.add_argument("--abandon", action="store_true", help="stop after the code is sent")
    revoke_parser = commands.add_parser("revoke", help="withdraw a consent")
    revoke_parser.add_argument("--username", required=True)
    revoke_parser.add_argument("--consent-id", required=True)
    revoke_parser.add_argument("--pin-file", type=Path)
    return parser


def _read_pin(args: argparse.Namespace) -> str:
    if args.pin_file:
        return str(args.pin_file.read_text(encoding="utf-8").strip())
    return os.environ.get("CUSTOMER_PIN") or getpass.getpass("PIN: ")


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command; exit 0 on success, 1 when the bank refused."""
    args = _parser().parse_args(argv)
    try:
        pin = _read_pin(args)
        if args.command == "revoke":
            with httpx.Client(base_url=args.bank_url, timeout=15.0) as bank:
                _check(
                    bank.post(f"/v1/consents/{args.consent_id}/revoke", json={"username": args.username, "pin": pin})
                )
            print(f"consent {args.consent_id} revoked")
            return 0

        def read_code(challenge: dict[str, Any]) -> str:
            if args.code:
                return str(args.code)
            latest = FileOtpChannel(args.outbox).latest_any() if args.outbox else None
            return latest or input("Code from your device: ").strip()

        approve(args, pin, read_code)
        return 0
    except (CustomerError, httpx.HTTPError, OSError) as error:
        print(f"refused: {error}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
