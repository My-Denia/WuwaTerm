"""Small helpers for privacy-safe operational logs."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any


REDACTION_SECRET_ENV = "WUWATERM_REDACTION_SECRET"
_runtime_redaction_secret: bytes | None = None


def configure_redaction_secret(secret: str | bytes | None) -> None:
    """Set a process-local log redaction secret.

    Passing None clears the runtime override so redact_id falls back to the
    environment variable, or to the legacy deterministic hash when unset.
    """
    global _runtime_redaction_secret
    _runtime_redaction_secret = _coerce_secret(secret)


def _coerce_secret(secret: str | bytes | None) -> bytes | None:
    if secret is None:
        return None
    if isinstance(secret, bytes):
        return secret or None
    return secret.encode("utf-8") or None


def _active_redaction_secret() -> bytes | None:
    if _runtime_redaction_secret is not None:
        return _runtime_redaction_secret
    return _coerce_secret(os.environ.get(REDACTION_SECRET_ENV))


def redact_id(value: Any) -> str:
    if value is None:
        return "none"
    payload = str(value).encode("utf-8")
    secret = _active_redaction_secret()
    if secret is not None:
        digest = hmac.new(secret, payload, hashlib.sha256).hexdigest()[:8]
    else:
        digest = hashlib.sha256(payload).hexdigest()[:8]
    return f"id:{digest}"


def safe_text_len(text: str | None) -> int:
    return len(text or "")


def safe_error_type(exc: BaseException | None) -> str:
    return type(exc).__name__
