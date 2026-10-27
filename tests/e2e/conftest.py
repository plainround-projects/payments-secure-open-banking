"""Fixtures for the end-to-end tests: the bank and the merchant as two real server processes, and the real programs."""

from __future__ import annotations

import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx
import pytest


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def run_program(module: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run one of the project's programs the way a user would, in its own process."""
    return subprocess.run(
        [sys.executable, "-m", f"open_banking.interfaces.{module}", *args],
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )


@dataclass
class Server:
    name: str
    process: subprocess.Popen[str]
    port: int
    output: str = field(default="")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def start(name: str, module: str, port: int, env: dict[str, str]) -> Server:
    process = subprocess.Popen(
        [sys.executable, "-m", f"open_banking.interfaces.{module}"],
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the {name} exited early:\n{process.stdout.read() if process.stdout else ''}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                return Server(name, process, port)
        except httpx.HTTPError:
            time.sleep(0.2)
    process.kill()
    raise RuntimeError(f"the {name} did not become healthy in 90 seconds")


def stop(server: Server) -> tuple[int, str]:
    """SIGTERM, then the exit code and everything the process printed."""
    if server.process.poll() is None:
        server.process.send_signal(signal.SIGTERM)
    try:
        output, _ = server.process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        server.process.kill()
        output, _ = server.process.communicate()
    server.output = output or server.output
    return server.process.returncode, server.output


@dataclass
class Stack:
    directory: pathlib.Path
    bank: Server
    merchant: Server
    pin: str
    client_secret: str

    @property
    def outbox(self) -> pathlib.Path:
        return self.directory / "outbox.log"


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory, database: None) -> Iterator[Stack]:
    directory = tmp_path_factory.mktemp("openbanking")
    admin_env = {
        "BANK_SIGNING_KEY_FILE": str(directory / "bank-key.pem"),
        "OTP_OUTBOX_FILE": str(directory / "outbox.log"),
    }
    registered = run_program(
        "admin_cli",
        "register-merchant",
        "--client-id",
        "merchant-1",
        "--name",
        "Shop",
        "--out",
        str(directory),
        env=admin_env,
    )
    assert registered.returncode == 0, registered.stdout + registered.stderr
    enrolled = run_program(
        "admin_cli",
        "enroll-customer",
        "--username",
        "ada",
        "--account",
        "acct-ada",
        "--out",
        str(directory),
        env=admin_env,
    )
    assert enrolled.returncode == 0, enrolled.stdout + enrolled.stderr
    merchant_env = dict(line.split("=", 1) for line in (directory / "merchant-1.env").read_text().splitlines())
    bank_port, merchant_port = free_port(), free_port()
    bank = start(
        "bank",
        "serve_bank",
        bank_port,
        {
            **admin_env,
            "BANK_HOST": "127.0.0.1",
            "BANK_PORT": str(bank_port),
            "BANK_PUBLIC_URL": f"http://127.0.0.1:{bank_port}",
            "LOG_LEVEL": "INFO",
        },
    )
    merchant = start(
        "merchant",
        "serve_merchant",
        merchant_port,
        {
            **merchant_env,
            "MERCHANT_HOST": "127.0.0.1",
            "MERCHANT_PORT": str(merchant_port),
            "BANK_URL": f"http://127.0.0.1:{bank_port}",
            "MERCHANT_PAYEE_ACCOUNT": "acct-shop",
            "LOG_LEVEL": "INFO",
        },
    )
    running = Stack(
        directory, bank, merchant, (directory / "ada.pin").read_text().strip(), merchant_env["MERCHANT_CLIENT_SECRET"]
    )
    yield running
    for server in (merchant, bank):
        stop(server)
