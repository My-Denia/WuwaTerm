"""HTTP transport for OpenAI-compatible chat completions.

Owns env resolution, the single POST path, error mapping, the failure
diagnostic types and a redacted per-attempt log line. It carries no prompt
construction and no domain logic, so sentence translation and other callers
(for example the semantic review engine) share one transport.

Per-attempt hooks:

* pre-send hooks run synchronously right before every POST attempt and may
  raise ``LLMTranslationError`` to refuse the call (no request is sent);
* call observers receive one ``LlmCallRecord`` per attempted POST.

Nothing here ever logs the base URL, key, model output or any text.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)

# Bilingual user-facing notices: Chinese line first, then English (single "\n").
BUDGET_EXHAUSTED_NOTICE = (
    "本月翻译额度已用完,请稍后再试。\n"
    "This month's translation quota is used up. Please try again later."
)
TRANSLATION_UNAVAILABLE_NOTICE = (
    "翻译服务暂时不可用，请稍后再试。\n"
    "Translation service is temporarily unavailable. Please try again later."
)


_LLM_FAILURE_REASONS = frozenset(
    {
        "budget",
        "rate_limit",
        "upstream",
        "timeout",
        "connect",
        "request",
        "http",
        "invalid_api_response",
        "invalid_response",
        "protected_text_integrity",
        "html_integrity",
        "stale_before_llm",
        "authorization_changed_before_llm",
        "translation_unavailable",
        "unknown",
    }
)
_LLM_FAILURE_DETAILS = frozenset(
    {
        "empty_output",
        "non_text_output",
        "missing_placeholder",
        "duplicate_placeholder",
        "unspecified",
    }
)
_MAX_LLM_DIAGNOSTIC_COUNT = 2_147_483_647


def _safe_diagnostic_value(value: object, allowed: frozenset[str], fallback: str) -> str:
    # Require a builtin str so a hostile subclass cannot retain custom string
    # behavior in an object that crosses into logging code.
    if type(value) is str and value in allowed:
        return value
    return fallback


def _safe_diagnostic_count(value: object) -> int | None:
    # bool is an int subclass; the diagnostic contract accepts exact ints only.
    if type(value) is int and 0 <= value <= _MAX_LLM_DIAGNOSTIC_COUNT:
        return value
    return None


@dataclass(frozen=True)
class LLMFailureDiagnostic:
    """Safe, bounded metadata for one failed LLM translation attempt."""

    reason: str
    detail: str = "unspecified"
    expected_count: int | None = None
    actual_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reason",
            _safe_diagnostic_value(
                self.reason, _LLM_FAILURE_REASONS, "unknown"
            ),
        )
        object.__setattr__(
            self,
            "detail",
            _safe_diagnostic_value(
                self.detail, _LLM_FAILURE_DETAILS, "unspecified"
            ),
        )
        object.__setattr__(
            self, "expected_count", _safe_diagnostic_count(self.expected_count)
        )
        object.__setattr__(
            self, "actual_count", _safe_diagnostic_count(self.actual_count)
        )


class LLMTranslationError(RuntimeError):
    def __init__(
        self,
        user_message: str,
        *,
        reason: str = "translation_unavailable",
        detail: str = "unspecified",
        expected_count: int | None = None,
        actual_count: int | None = None,
    ):
        super().__init__(user_message)
        self.user_message = user_message
        self.diagnostic = LLMFailureDiagnostic(
            reason,
            detail=detail,
            expected_count=expected_count,
            actual_count=actual_count,
        )
        # Keep the legacy attribute's original semantics. Consumers that
        # cross a logging or protocol boundary use ``diagnostic`` instead.
        self.reason = reason


def _safe_llm_failure_diagnostic(exc: object) -> LLMFailureDiagnostic:
    """Project an exception onto a safe diagnostic tied to its legacy reason."""

    try:
        original_reason = getattr(exc, "reason", "translation_unavailable")
    except Exception:
        original_reason = "translation_unavailable"
    base = LLMFailureDiagnostic(original_reason)

    try:
        attached = getattr(exc, "diagnostic", None)
        if type(attached) is not LLMFailureDiagnostic:
            return base
        if type(attached.reason) is not str or attached.reason != base.reason:
            return base
        return LLMFailureDiagnostic(
            base.reason,
            detail=attached.detail,
            expected_count=attached.expected_count,
            actual_count=attached.actual_count,
        )
    except Exception:
        return base


def _require_nonblank_llm_output(content: str) -> str:
    if not isinstance(content, str):
        raise LLMTranslationError(
            TRANSLATION_UNAVAILABLE_NOTICE,
            reason="invalid_response",
            detail="non_text_output",
        )
    normalized = content.strip()
    if not normalized:
        raise LLMTranslationError(
            TRANSLATION_UNAVAILABLE_NOTICE,
            reason="invalid_response",
            detail="empty_output",
        )
    return normalized


def _extract_llm_content(data: Any) -> str:
    if not isinstance(data, dict):
        raise ValueError("LLM response is not an object")
    choices = data["choices"]
    if not isinstance(choices, list) or not choices:
        raise ValueError("LLM response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("LLM choice is not an object")
    message = first["message"]
    if not isinstance(message, dict):
        raise ValueError("LLM message is not an object")
    content = message["content"]
    if not isinstance(content, str):
        raise ValueError("LLM content is not text")
    return _require_nonblank_llm_output(content)


def _llm_error_from_response(response: httpx.Response) -> LLMTranslationError:
    if response.status_code >= 500:
        return LLMTranslationError(
            TRANSLATION_UNAVAILABLE_NOTICE, reason="upstream"
        )
    if response.status_code == 429:
        reason = "budget" if _has_structured_budget_signal(response) else "rate_limit"
        return LLMTranslationError(BUDGET_EXHAUSTED_NOTICE, reason=reason)
    if _has_structured_budget_signal(response):
        return LLMTranslationError(BUDGET_EXHAUSTED_NOTICE, reason="budget")
    return LLMTranslationError(TRANSLATION_UNAVAILABLE_NOTICE, reason="http")


def _has_structured_budget_signal(response: httpx.Response) -> bool:
    text = _structured_llm_error_text(response)
    if not text:
        return False
    # Do not match generic "exceeded": context-length errors commonly use that
    # word but are ordinary translation failures, not quota exhaustion.
    return any(
        marker in text
        for marker in (
            "budget",
            "max_budget",
            "quota",
            "insufficient_quota",
            "billing",
            "rate limit",
            "rate_limit",
            "rate-limit",
        )
    )


def _structured_llm_error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    parts: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for key in ("code", "type", "message", "detail", "error", "param"):
                if key in value:
                    collect(value[key])
        elif isinstance(value, list):
            for item in value:
                collect(item)

    if isinstance(data, dict) and "error" in data:
        collect(data["error"])
    else:
        collect(data)
    return " ".join(parts).casefold()


def resolve_llm_config(model_env: str | None = None) -> tuple[str, str, str]:
    """Return ``(base_url_without_trailing_slash, api_key, model)``."""

    base_url = (
        os.getenv("WUWATERM_OPENAI_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or ""
    ).strip().rstrip("/")
    api_key = (
        os.getenv("WUWATERM_OPENAI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()
    model = os.getenv("WUWATERM_OPENAI_MODEL", "").strip()
    if model_env:
        override = os.getenv(model_env, "").strip()
        if override:
            model = override
    return base_url, api_key, model


def llm_configured() -> bool:
    return bool(
        (os.getenv("WUWATERM_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "").strip()
        and (os.getenv("WUWATERM_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
        and os.getenv("WUWATERM_OPENAI_MODEL", "").strip()
    )


@dataclass(frozen=True)
class LlmCallRecord:
    """Outcome of one attempted HTTP POST. Carries no request or response text."""

    purpose: str
    outcome: str  # ok|http_error|timeout|connect|request|invalid_api_response
    status_code: int | None
    elapsed_ms: int


PreSendHook = Callable[[str], None]
CallObserver = Callable[[LlmCallRecord], None]

_PRE_SEND_HOOKS: list[PreSendHook] = []
_CALL_OBSERVERS: list[CallObserver] = []


def add_pre_send_hook(fn: PreSendHook) -> Callable[[], None]:
    """Register ``fn(purpose)`` run before every POST; returns a remover."""

    _PRE_SEND_HOOKS.append(fn)

    def remove() -> None:
        try:
            _PRE_SEND_HOOKS.remove(fn)
        except ValueError:
            pass

    return remove


def add_call_observer(fn: CallObserver) -> Callable[[], None]:
    """Register ``fn(record)`` run once per attempted POST; returns a remover."""

    _CALL_OBSERVERS.append(fn)

    def remove() -> None:
        try:
            _CALL_OBSERVERS.remove(fn)
        except ValueError:
            pass

    return remove


async def post_chat_completion(
    client: httpx.AsyncClient,
    messages: list[dict],
    *,
    purpose: str,
    model: str | None = None,
    temperature: float = 0,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    model_env: str | None = None,
) -> str:
    base_url, api_key, resolved_model = resolve_llm_config(model_env)
    payload: dict[str, Any] = {
        "model": model if model is not None else resolved_model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if response_format is not None:
        payload["response_format"] = response_format
    for hook in tuple(_PRE_SEND_HOOKS):
        hook(purpose)
    outcome = "request"
    status_code: int | None = None
    started = time.monotonic()
    try:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        status_code = response.status_code
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            outcome = "http_error"
            raise _llm_error_from_response(exc.response) from exc
        outcome = "invalid_api_response"
        data = response.json()
        content = _extract_llm_content(data)
        outcome = "ok"
        return content
    except LLMTranslationError:
        raise
    except httpx.TimeoutException as exc:
        outcome = "timeout"
        raise LLMTranslationError(
            TRANSLATION_UNAVAILABLE_NOTICE, reason="timeout"
        ) from exc
    except httpx.ConnectError as exc:
        outcome = "connect"
        raise LLMTranslationError(
            TRANSLATION_UNAVAILABLE_NOTICE, reason="connect"
        ) from exc
    except httpx.RequestError as exc:
        outcome = "request"
        raise LLMTranslationError(
            TRANSLATION_UNAVAILABLE_NOTICE, reason="request"
        ) from exc
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        # Response ENVELOPE failure (malformed JSON, wrong schema): a gateway
        # or schema outage, not model content drift. Distinct from
        # "invalid_response" (blank output / broken placeholders) so the HTML
        # paths do not burn a plain-retry call on an outage that would fail
        # identically.
        outcome = "invalid_api_response"
        raise LLMTranslationError(
            TRANSLATION_UNAVAILABLE_NOTICE, reason="invalid_api_response"
        ) from exc
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        LOGGER.info(
            "llm call purpose=%s outcome=%s status=%s elapsed_ms=%s",
            purpose,
            outcome,
            status_code,
            elapsed_ms,
        )
        record = LlmCallRecord(purpose, outcome, status_code, elapsed_ms)
        for observer in tuple(_CALL_OBSERVERS):
            observer(record)

