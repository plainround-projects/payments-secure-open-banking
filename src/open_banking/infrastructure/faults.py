"""Scriptable network faults for the merchant's HTTP client: the bank processes a request, the answer never arrives."""

from __future__ import annotations

import threading
from collections import deque
from enum import StrEnum

import httpx


class Fault(StrEnum):
    """What goes wrong with one request."""

    DROP_REQUEST = "DROP_REQUEST"  # the request never reaches the bank
    DROP_RESPONSE = "DROP_RESPONSE"  # the bank processes it and commits; the response is lost


class FaultScript:
    """A queue of faults to apply to the next requests, in order. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._faults: deque[Fault] = deque()

    def push(self, fault: Fault, times: int = 1) -> None:
        """Queue `fault` for the next `times` requests."""
        with self._lock:
            self._faults.extend([fault] * times)

    def next(self) -> Fault | None:
        """Take the next fault, if any."""
        with self._lock:
            return self._faults.popleft() if self._faults else None


class FaultInjectingTransport(httpx.BaseTransport):
    """Wraps a real transport. With a scripted fault it either drops the request before it is sent, or sends it,
    lets the server finish, and then raises a read timeout instead of returning the response: exactly what a dropped
    connection on the way back looks like to the caller.
    """

    def __init__(self, inner: httpx.BaseTransport, script: FaultScript) -> None:
        self._inner = inner
        self._script = script

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Send the request, or fail it the way the script says."""
        fault = self._script.next()
        if fault is Fault.DROP_REQUEST:
            raise httpx.ConnectTimeout("scripted: the request never reached the bank", request=request)
        response = self._inner.handle_request(request)
        if fault is Fault.DROP_RESPONSE:
            response.read()
            response.close()
            raise httpx.ReadTimeout("scripted: the bank processed the request but its answer was lost", request=request)
        return response

    def close(self) -> None:
        """Close the wrapped transport."""
        self._inner.close()
