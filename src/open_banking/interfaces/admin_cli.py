"""`open-banking-admin`: the bank operator's program. Register a merchant, enroll a customer, read balances.

Secrets it creates (a merchant's key and client secret, a customer's PIN) are written to files readable by their owner
only, under `--out` (default `var/`, which git ignores). They are never printed, and the bank keeps only hashes.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path

from open_banking.domain.errors import OpenBankingError
from open_banking.infrastructure import keys
from open_banking.infrastructure.bank_settings import BankConfig
from open_banking.infrastructure.logging import configure_logging
from open_banking.infrastructure.settings import ConfigurationError
from open_banking.interfaces.bank_container import BankContainer, build_bank_container


def _write_secret_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(text, encoding="utf-8")


def register_merchant(bank: BankContainer, client_id: str, name: str, out: Path) -> list[Path]:
    """Register a merchant and write its private key and credentials file. Returns the files written."""
    private_pem, public_jwk = keys.generate_keypair()
    client_secret = secrets.token_urlsafe(32)
    bank.register_client.execute(client_id, name, client_secret, public_jwk)
    key_file = out / f"{client_id}-key.pem"
    env_file = out / f"{client_id}.env"
    keys.write_private_key(key_file, private_pem)
    _write_secret_file(
        env_file,
        f"MERCHANT_CLIENT_ID={client_id}\nMERCHANT_CLIENT_SECRET={client_secret}\n"
        f"MERCHANT_PRIVATE_KEY_FILE={key_file}\n",
    )
    return [key_file, env_file]


def enroll_customer(bank: BankContainer, username: str, account_id: str, out: Path) -> Path:
    """Enroll a customer with a random six-digit PIN, written to a file. Returns the file."""
    pin = f"{secrets.randbelow(10**6):06d}"
    bank.enroll_customer.execute(username, pin, account_id)
    pin_file = out / f"{username}.pin"
    _write_secret_file(pin_file, pin + "\n")
    return pin_file


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="open-banking-admin", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    merchant = commands.add_parser("register-merchant", help="register a merchant and create its key and secret")
    merchant.add_argument("--client-id", required=True)
    merchant.add_argument("--name", required=True)
    merchant.add_argument("--out", type=Path, default=Path("var"))
    customer = commands.add_parser("enroll-customer", help="enroll a customer with a random PIN")
    customer.add_argument("--username", required=True)
    customer.add_argument("--account", required=True)
    customer.add_argument("--out", type=Path, default=Path("var"))
    balances = commands.add_parser("balances", help="print account balances and the ledger sum")
    balances.add_argument("accounts", nargs="+")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command; exit 0 on success, 1 for a refused operation, 2 for configuration."""
    args = _parser().parse_args(argv)
    try:
        config = BankConfig.from_env()
    except ConfigurationError as error:
        print(f"configuration error: {error}")
        return 2
    configure_logging("WARNING")
    bank = build_bank_container(config)
    try:
        if args.command == "register-merchant":
            files = register_merchant(bank, args.client_id, args.name, args.out)
            print(f"registered {args.client_id}; wrote " + ", ".join(str(path) for path in files))
        elif args.command == "enroll-customer":
            pin_file = enroll_customer(bank, args.username, args.account, args.out)
            print(f"enrolled {args.username}; the PIN is in {pin_file}")
        else:
            balances, total = bank.balances.execute(args.accounts)
            for account, balance in balances.items():
                print(f"{account}: {balance.format()} {balance.currency}")
            print(f"ledger sum across every posting: {total.format()}")
        return 0
    except OpenBankingError as error:
        print(f"refused: {error.message}")
        return 1
    finally:
        bank.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
