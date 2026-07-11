"""Request-scoped trace ID propagation helpers."""

from __future__ import annotations

from contextvars import ContextVar, Token

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def set_trace_id(value: str | None) -> Token[str | None]:
    """Set the trace ID for the current context and return its reset token."""

    return trace_id_var.set(value)


def reset_trace_id(token: Token[str | None]) -> None:
    """Restore the trace ID stored before ``set_trace_id``."""

    trace_id_var.reset(token)


def current_trace_id() -> str | None:
    """Return the trace ID for the current context, if one is set."""

    return trace_id_var.get()
