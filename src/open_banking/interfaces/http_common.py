"""What both HTTP applications share: request logging that never records bodies or headers, and the error shape."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from open_banking.application.logs import fields

logger = logging.getLogger(__name__)


def error_response(code: str, message: str, status: int, **extra: Any) -> JSONResponse:
    """The one error shape: `{"error": {"code", "message", ...}}`."""
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status)


def install_common(app: FastAPI) -> None:
    """Request logging (method, path, status, duration, request id; no bodies, no headers) and the catch-alls."""

    @app.middleware("http")
    async def log_requests(request: Request, call_next: Callable[[Request], Any]) -> Response:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        started = time.perf_counter()
        response: Response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        logger.info(
            "request",
            extra=fields(
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            ),
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        problems = "; ".join(f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}" for e in error.errors())
        return error_response("invalid_request", problems, 400)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception) -> JSONResponse:
        logger.error("unexpected error", extra=fields(error=type(error).__name__), exc_info=error)
        return error_response("internal_error", "an unexpected error occurred", 500)
