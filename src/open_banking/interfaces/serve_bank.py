"""`open-banking-bank`: run the bank HTTP API until SIGTERM or SIGINT, then finish in-flight requests and exit 0."""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Generator
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI

from open_banking.infrastructure.bank_settings import BankConfig
from open_banking.infrastructure.logging import configure_logging
from open_banking.infrastructure.settings import ConfigurationError, ServerSettings
from open_banking.interfaces.bank_api import create_bank_app
from open_banking.interfaces.bank_container import build_bank_container

logger = logging.getLogger("open_banking.serve_bank")


class GracefulServer(uvicorn.Server):
    """A uvicorn server that exits normally (status 0) after SIGINT or SIGTERM.

    uvicorn finishes in-flight requests and runs the lifespan shutdown on a signal, then raises the same signal again
    so the process dies "by signal". An orchestrator reads that as a crash; this server just returns.
    """

    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        """Route SIGINT and SIGTERM to the server's own graceful exit, and put the old handlers back afterwards."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def serve(app: FastAPI, server: ServerSettings) -> None:
    """Serve `app` until told to stop."""
    config = uvicorn.Config(
        app,
        host=server.host,
        port=server.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=server.shutdown_grace_seconds,
    )
    GracefulServer(config).run()


def main() -> int:
    """Start the server; the exit code is 0 after a graceful stop and 2 for a configuration error."""
    try:
        config = BankConfig.from_env()
    except ConfigurationError as error:
        print(f"configuration error: {error}")
        return 2
    configure_logging(config.log_level)
    logger.info("listening", extra={"fields": {"host": config.server.host, "port": config.server.port}})
    serve(create_bank_app(lambda: build_bank_container(config)), config.server)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
