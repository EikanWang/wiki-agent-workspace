"""Stable API error shape: {"error": {"code", "message", "details"}}."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status,
            content={"error": {"code": self.code, "message": self.message, "details": self.details}},
        )


# Stable error codes (DESIGN §13)
DOCUMENT_VERSION_CONFLICT = "DOCUMENT_VERSION_CONFLICT"
SESSION_BUSY = "SESSION_BUSY"
SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
PROVIDER_AUTH_REQUIRED = "PROVIDER_AUTH_REQUIRED"
PREVIEW_UNAVAILABLE = "PREVIEW_UNAVAILABLE"
TERMINAL_GONE = "TERMINAL_GONE"
IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
INVALID_ANCHOR = "INVALID_ANCHOR"


def bad_request(message: str, code: str = "BAD_REQUEST", **details: Any) -> ApiError:
    return ApiError(400, code, message, details)


def not_found(message: str, code: str = "NOT_FOUND", **details: Any) -> ApiError:
    return ApiError(404, code, message, details)


def conflict(code: str, message: str, **details: Any) -> ApiError:
    return ApiError(409, code, message, details)


def unauthorized(message: str = "Authentication required") -> ApiError:
    return ApiError(401, "UNAUTHENTICATED", message)


def forbidden(message: str, code: str = "FORBIDDEN") -> ApiError:
    return ApiError(403, code, message)


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return exc.to_response()


async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # Never leak stack traces to the client; the server log has the traceback.
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "Unexpected server error", "details": {}}},
    )
