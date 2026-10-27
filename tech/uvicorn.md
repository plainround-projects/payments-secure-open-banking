# Uvicorn

## What it is

Uvicorn is an **ASGI web server**. A web server is the program that listens on a network port, accepts connections and speaks HTTP. It does not know your business rules; it hands each request to your application (here FastAPI) through a standard interface called **ASGI**, and sends the answer back. Uvicorn is a long-running process that stays up until told to stop.

## Why this repository uses it

FastAPI cannot listen on a port by itself. Uvicorn runs both services (the bank and the merchant) for `make serve-bank`, `make serve-merchant`, the Docker image, the end-to-end tests and the demo program. This repository also cares about how the server **stops**: in-flight payments must finish, the database pool must close, and the process must exit with status 0, so it customizes the shutdown.

## Where it appears

| Path | What it does with uvicorn |
| --- | --- |
| `src/open_banking/interfaces/serve_bank.py` | `main()` builds the bank app and `serve()` runs it with `GracefulServer` (a subclass of `uvicorn.Server`). |
| `src/open_banking/interfaces/serve_merchant.py` | The same for the merchant (it has its own copy of `GracefulServer` and `serve()`). |
| `src/open_banking/interfaces/demo_cli.py` | `_serve_in_thread()` starts a `uvicorn.Server` in a background thread, once for the bank and once for each merchant, for the demo. |
| `tests/e2e/conftest.py` | Starts the bank and merchant as child processes, then sends them SIGTERM. |
| `pyproject.toml` | Entry points `open-banking-bank = "open_banking.interfaces.serve_bank:main"` and `open-banking-merchant = "open_banking.interfaces.serve_merchant:main"`. |
| `Dockerfile` | `CMD ["open-banking-bank"]`. |

## How it works underneath

- Uvicorn opens a listening **socket** on `host:port`. Each client connection becomes work for an **asyncio event loop**, a single thread that switches between many waiting tasks.
- It parses HTTP/1.1, builds the ASGI request and calls the app.
- It also drives the app's **lifespan**: before accepting traffic it tells the app "startup"; on exit it says "shutdown".
- A **signal** is a small message the operating system sends a process. `SIGINT` is what Ctrl-C sends. `SIGTERM` is the polite "please stop" that Docker, Kubernetes and `kill` send first.
- On a signal, uvicorn sets `should_exit`, stops accepting new connections, waits for running requests up to `timeout_graceful_shutdown` seconds, then runs the lifespan shutdown.

## Walkthrough of this repository's use

### Configuration

```python
def serve(app: FastAPI, server: ServerSettings) -> None:
    config = uvicorn.Config(
        app,
        host=server.host,
        port=server.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=server.shutdown_grace_seconds,
    )
    GracefulServer(config).run()
```

- `host`/`port`: from the environment. The bank uses `BANK_HOST` and `BANK_PORT` (defaults `127.0.0.1` and `8463`; the Docker image sets `0.0.0.0` so the container is reachable); the merchant uses `MERCHANT_HOST` and `MERCHANT_PORT` (default `8464`).
- `log_config=None`: do not install uvicorn's own logging setup. The app installs one logfmt handler (see `logging-and-logfmt.md`) and uvicorn's messages flow through it.
- `access_log=False`: no per-request line from uvicorn, because the app's own middleware already logs each request with an id and timing.
- `timeout_graceful_shutdown`: the most seconds to wait for in-flight requests (`SHUTDOWN_GRACE_SECONDS`, default 10). After that, remaining tasks are cancelled.

### Why `GracefulServer` exists

```python
class GracefulServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
```

Uvicorn's own `capture_signals` does the same, but when it finishes it **raises the same signal again** at the process, so the process dies "by signal". That is meant to preserve the usual behavior, but to an orchestrator the exit status looks like a crash (for example `-15`). This subclass skips the re-raise: the server shuts down cleanly and `main()` returns 0.

- `signal.signal(sig, handler)` installs a handler and returns the old one, which is saved in `previous` and restored in `finally`.
- `self.handle_exit` is uvicorn's own handler; it sets `should_exit = True`.
- Signals can only be installed from the main thread, so in another thread (the demo's) the method does nothing.

### In the demo and tests

```python
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
```

The demo starts a real bank and real merchant servers **inside its own process**, each on a free port in a background thread (`daemon=True` means the thread will not keep the program alive). It polls `server.started`, which turns true when the server is ready, and fails after 30 seconds. To stop one, the demo sets `server.should_exit = True`, which is what a signal would do. The end-to-end tests do the real thing with child processes: start the servers, wait for `/health`, run the scenario, send `SIGTERM`, and assert the exit status is 0 and the log says "shutdown complete".

## Try it yourself

1. Start the stack and the server (two terminals):

   ```bash
   make up
   make serve-bank
   ```

   Expected on stderr: a logfmt line `msg=listening ... host=127.0.0.1 port=8463` and `msg=started`.

2. Ask it if it is healthy:

   ```bash
   make health
   ```

   Expected: JSON such as `{"status":"ok","checks":{"database":"ok"}}` and exit status 0 (`echo $?`).

3. Press Ctrl-C in the server terminal. Expected: `msg="shutdown complete"` and the prompt returns; `echo $?` prints `0`.

4. Try a port that is busy (`BANK_PORT=22 make serve-bank`): uvicorn reports it cannot bind and the process exits non-zero.

## Mistakes people make

- **Binding to `127.0.0.1` inside a container.** Nothing outside can connect. The Dockerfile sets `BANK_HOST=0.0.0.0`.
- **Killing with `SIGKILL`** (`kill -9`). It cannot be caught, so there is no graceful shutdown and in-flight work is cut off.
- **Letting the process die by re-raised signal.** Orchestrators report it as a failure; `GracefulServer` avoids this.
- **Ignoring the grace timeout.** If it is too short, long requests are cancelled mid-flight; if it is too long, the platform may `SIGKILL` first.

## Check yourself

1. What is ASGI? *The standard interface between an async Python web server and an app.*
2. What does `timeout_graceful_shutdown` control? *How long to wait for running requests before cancelling them at shutdown.*
3. Why subclass `uvicorn.Server`? *To stop it re-raising the signal, so the process exits with status 0 after a clean stop.*
4. Why check for the main thread? *Python allows signal handlers to be installed only from the main thread.*
5. Which signal does Docker send first on `docker stop`? *SIGTERM.*

## Further reading

- Uvicorn documentation (pinned: 0.54.0): https://www.uvicorn.org/
- Settings, including timeouts: https://www.uvicorn.org/settings/
- Python `signal`: https://docs.python.org/3.12/library/signal.html
- ASGI specification: https://asgi.readthedocs.io/en/latest/
