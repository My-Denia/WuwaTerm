"""Stable error envelope for the HTTP adapter.

Every non-2xx response has exactly this shape::

    {"error": {"code": "<enumerated code>", "message": "<short text>"},
     "request_id": "<id>"}

Codes come from :mod:`wuwaterm.application` so the Telegram adapter and the
HTTP adapter classify failures identically. Messages are short, English and
operator-facing; they never repeat the bot's Telegram-worded notices and never
contain paths, credentials or upstream response text.
"""

from __future__ import annotations

from typing import Any

from wuwaterm.application import (
    ERROR_FORBIDDEN,
    ERROR_INPUT_TOO_LONG,
    ERROR_INTERNAL,
    ERROR_INVALID_REQUEST,
    ERROR_KNOWLEDGE_UNAVAILABLE,
    ERROR_LLM_BUDGET_EXHAUSTED,
    ERROR_LLM_UNAVAILABLE,
    ERROR_PAYLOAD_TOO_LARGE,
    ERROR_RATE_LIMITED,
    ERROR_UNAUTHORIZED,
)


STATUS_BY_CODE: dict[str, int] = {
    ERROR_UNAUTHORIZED: 401,
    ERROR_FORBIDDEN: 403,
    ERROR_RATE_LIMITED: 429,
    ERROR_PAYLOAD_TOO_LARGE: 413,
    ERROR_INVALID_REQUEST: 400,
    ERROR_INPUT_TOO_LONG: 422,
    ERROR_LLM_UNAVAILABLE: 503,
    ERROR_LLM_BUDGET_EXHAUSTED: 503,
    ERROR_KNOWLEDGE_UNAVAILABLE: 503,
    ERROR_INTERNAL: 500,
}

# Short, stable, operator-facing wording per code.
MESSAGE_BY_CODE: dict[str, str] = {
    ERROR_UNAUTHORIZED: "missing or invalid device credential",
    ERROR_FORBIDDEN: "device credential lacks the required scope",
    ERROR_RATE_LIMITED: "request rate limit exceeded for this device",
    ERROR_PAYLOAD_TOO_LARGE: "request body is larger than the configured limit",
    ERROR_INVALID_REQUEST: "request payload is not valid",
    ERROR_INPUT_TOO_LONG: "input text is longer than the translation limit",
    ERROR_LLM_UNAVAILABLE: "translation provider is unavailable",
    ERROR_LLM_BUDGET_EXHAUSTED: "translation call budget is exhausted",
    ERROR_KNOWLEDGE_UNAVAILABLE: "domain knowledge base is unavailable",
    ERROR_INTERNAL: "internal error",
}


class ApiError(Exception):
    """An error that renders as the stable envelope."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.message = message or MESSAGE_BY_CODE.get(code, MESSAGE_BY_CODE[ERROR_INTERNAL])
        self.status_code = status_code or STATUS_BY_CODE.get(code, 500)
        super().__init__(f"{code}: {self.message}")


def error_body(code: str, message: str, request_id: str) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message},
        "request_id": request_id,
    }
