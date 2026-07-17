"""Consistent public error envelopes for the Mini App API."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.responses import JSONResponse

from core.tracing import current_trace_id


@dataclass
class WebAppError(Exception):
    """An expected API error with a safe stable code and user-facing message."""

    status_code: int
    code: str
    message: str


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Build a JSON error response tied to the current request trace."""

    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "trace_id": current_trace_id() or "-",
            }
        },
    )
