# import-linter

## What it is

import-linter is a command-line tool (`lint-imports`) that checks **which modules are allowed to import which**. You write the rules once, in a config file, as "contracts". It reads all the `import` statements in the package, builds a graph of who imports whom, and fails if any contract is broken.

## Why this repository uses it

The design is **layered**: the domain (money rules) must know nothing about the web, the database or the framework, so it can be tested alone and never changes because a library does. A sentence in a README does not stop someone adding `import fastapi` to a domain file next month. A failing check does.

## Where it appears

| Path | Role |
| --- | --- |
| `pyproject.toml`, `[tool.importlinter]` | The two contracts. |
| `Makefile` target `lint` | Runs `lint-imports`. |
| `.import_linter_cache/` | Cache (ignored by git). |
| `src/open_banking/{domain,application,infrastructure,interfaces}` | The layers being checked. |

## How it works underneath

The tool uses a library called Grimp to parse every module under `root_package` and record each import as an edge (`A imports B`). A contract is a rule about edges. The `layers` kind orders packages from highest to lowest and forbids an edge that points from a lower layer up to a higher one. The `forbidden` kind forbids any path from the source modules to the listed modules, including **indirect** paths (A imports B imports fastapi).

## Walkthrough of this repository's use

```toml
[tool.importlinter]
root_package = "open_banking"
include_external_packages = true
```

- `root_package`: the package to analyze.
- `include_external_packages = true`: also track imports of third-party packages (`fastapi`, `psycopg`), which the second contract needs.

```toml
[[tool.importlinter.contracts]]
name = "Layers: interfaces -> infrastructure -> application -> domain, never upward"
type = "layers"
layers = [
    "open_banking.interfaces",
    "open_banking.infrastructure",
    "open_banking.application",
    "open_banking.domain",
]
```

The list is ordered from the top layer to the bottom. Each layer may import the ones below it, never the ones above. `interfaces` (HTTP, programs) may use everything; `domain` may import nothing from the other three. The adapters in `infrastructure` implement ports defined in `application`, so the arrow of knowledge points inward.

```toml
[[tool.importlinter.contracts]]
name = "Domain and application know nothing about PostgreSQL, HTTP or the web framework"
type = "forbidden"
source_modules = ["open_banking.domain", "open_banking.application"]
forbidden_modules = ["psycopg", "psycopg_pool", "fastapi", "starlette", "uvicorn", "httpx", "pydantic", "jwt", "cryptography"]
```

Neither the domain nor the use cases may import (even through another module) any of these libraries, **including the cryptography libraries** (`jwt` and `cryptography`). The use cases reach cryptography only through a `Crypto` port, a `Protocol` that `infrastructure/crypto.py` implements. That is why the use cases talk to the database through `UnitOfWork` and friends, which are `Protocol` classes in `application/ports.py`, and why only `infrastructure/postgres.py` imports `psycopg`.

### The third contract: the merchant cannot see the bank's side

```toml
[[tool.importlinter.contracts]]
name = "The merchant side never imports the bank's authentication, consent or payment code (it cannot see the customer's PIN or code)"
type = "forbidden"
source_modules = ["open_banking.application.merchant", "open_banking.interfaces.merchant_api", "open_banking.interfaces.serve_merchant"]
forbidden_modules = [
    "open_banking.application.bank",
    "open_banking.domain.authentication",
    "open_banking.infrastructure.otp",
    "open_banking.interfaces.bank_api",
    "open_banking.interfaces.customer_cli",
]
```

This one is about **trust**, not layering. In open banking the merchant must never see the customer's PIN, the one-time code, or the bank's internals; it may only talk to the bank over HTTP. If the merchant's code could import `Challenge` or the outbox reader, a future change could quietly give it that knowledge. The contract makes such an import a build failure. Because the `forbidden` type follows chains, an indirect import counts too. For example, adding `import open_banking.domain.authentication` to the merchant's checkout module is reported as a path from `serve_merchant` through `merchant_container` and `application.merchant.checkout` to `domain.authentication`.

### Reading a failure

If someone adds `import fastapi` to `domain/money.py`, the output names the importer and line:

```
open_banking.domain is not allowed to import fastapi:

-   open_banking.domain.money -> fastapi (l.109)
```

and the application layer is reported too, because it imports `money`: `application.bank.payments -> domain.money -> fastapi`. The exit status is non-zero, so `make lint` and CI fail.

## Try it yourself

1. Run it:

   ```bash
   .venv/bin/lint-imports
   ```

   Expected: `Analyzed 85 files, 372 dependencies.`, all three contracts marked `KEPT`, and `Contracts: 3 kept, 0 broken.`

2. Break a rule, then restore the file:

   ```bash
   cp src/open_banking/domain/money.py /tmp/money.bak
   echo "import fastapi" >> src/open_banking/domain/money.py
   .venv/bin/lint-imports | tail -12
   cp /tmp/money.bak src/open_banking/domain/money.py
   ```

   Expected: `BROKEN` for the forbidden contract and the lines shown above.

3. Check the tool's version: `.venv/bin/lint-imports --version` (this machine: `import-linter 2.15`).

## Mistakes people make

- **Forgetting indirect imports.** The `forbidden` contract follows chains, so hiding `import fastapi` in a helper module does not help.
- **Leaving `include_external_packages` off.** Then rules about third-party libraries silently check nothing.
- **Reversing the layer order.** The first name in `layers` is the highest layer.
- **Fixing a violation by deleting the contract.** Move the code to the right layer instead.

## Check yourself

1. What does the `layers` contract forbid? *A lower layer importing a higher layer.*
2. Why is `pydantic` forbidden in `domain` and `application`? *Validation of HTTP bodies belongs at the edge; the rules of money must not depend on a web library.*
3. Does the `forbidden` contract catch indirect imports? *Yes, any import path from the source modules to the forbidden ones.*
4. Which file is the only one allowed to import `psycopg`? *`infrastructure/postgres.py` (and the tests).*
5. What happens in CI when a contract is broken? *`lint-imports` exits non-zero and `make check` fails.*

## Further reading

- import-linter (2.x) documentation: https://import-linter.readthedocs.io/en/stable/
- Contract types: https://import-linter.readthedocs.io/en/stable/contract_types.html
- Usage and configuration: https://import-linter.readthedocs.io/en/stable/usage.html
