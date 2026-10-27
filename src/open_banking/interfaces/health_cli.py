"""`open-banking-health`: exit 0 when the bank (or, with HEALTH_URL, any service) reports healthy."""

from __future__ import annotations

import os
import sys

import httpx


def main() -> int:
    """Ask `/health` of HEALTH_URL (default: the bank at BANK_HOST:BANK_PORT); exit 0 for healthy, 1 otherwise."""
    default = f"http://{os.environ.get('BANK_HOST', '127.0.0.1')}:{os.environ.get('BANK_PORT', '8463')}/health"
    try:
        response = httpx.get(os.environ.get("HEALTH_URL", default), timeout=5.0)
    except httpx.HTTPError as error:
        print(f"unreachable: {type(error).__name__}")
        return 1
    print(response.text)
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
