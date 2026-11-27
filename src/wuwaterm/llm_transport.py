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

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from . import request_trace

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
        "operation_budget",
        "cancelled",
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
        "truncated_output",
        "missing_placeholder",
        "duplicate_placeholder",
        "unsupported_thinking",
        "unsupported_reasoning_effort",
        "unspecified",
    }
)
_MAX_LLM_DIAGNOSTIC_COUNT = 2_147_483_647

# ``choices[0].finish_reason`` reports why the provider stopped generating.
# The documented Chat Completions value for "hit the length/token cap" is
# ``length``; some OpenAI-compatible gateways echo Anthropic's stop
# vocabulary, where ``max_tokens`` names the same cap. Both mean the content is
# a prefix of an answer, never a finished one. Every other value - ``stop``,
# ``tool_calls``, ``content_filter``, a missing field, ``null`` or a value this
# set does not know - keeps the pre-existing behaviour: only the content is
# judged.
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})


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


FailureReason = Literal[
    "budget",
    "operation_budget",
    "cancelled",
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
]
FailureDetail = Literal[
    "empty_output",
    "non_text_output",
    "truncated_output",
    "missing_placeholder",
    "duplicate_placeholder",
    "unsupported_thinking",
    "unsupported_reasoning_effort",
    "unspecified",
]
CallOutcome = Literal[
    "ok",
    "http_error",
    "timeout",
    "connect",
    "request",
    "invalid_api_response",
    "cancelled",
]
_SAFE_METADATA_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _safe_metadata_token(value: object) -> str | None:
    if type(value) is str and _SAFE_METADATA_TOKEN_RE.fullmatch(value):
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
        self.failure_stage: str | None = None


def safe_llm_failure_diagnostic(exc: object) -> LLMFailureDiagnostic:
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


_safe_llm_failure_diagnostic = safe_llm_failure_diagnostic


def log_llm_failure(diagnostic: LLMFailureDiagnostic) -> None:
    """Log one swallowed LLM failure with its fixed-vocabulary detail.

    ``reason`` alone cannot separate a provider timeout from a 200-OK answer
    that ``LockedSentence.restore`` rejected because a placeholder came back
    missing or duplicated, and those two have different owners (the provider
    versus this process' prompt-and-restore pair). ``detail`` and the two
    counts are bounded values from this module's own vocabulary — never source
    text, never model output — so they are safe beside the reason. Counts stay
    absent when the failure carried none, and no caller text is logged.
    """
    LOGGER.warning(
        "llm translation failed reason=%s detail=%s expected=%s actual=%s",
        diagnostic.reason,
        diagnostic.detail,
        diagnostic.expected_count,
        diagnostic.actual_count,
    )


def require_nonblank_llm_output(content: str) -> str:
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


_require_nonblank_llm_output = require_nonblank_llm_output


def _declares_truncation(choice: dict) -> bool:
    """True when the choice says the provider stopped at a length cap."""
    finish_reason = choice.get("finish_reason")
    # Require a builtin str: a foreign string subclass must not reach the
    # membership test's hash/equality.
    return type(finish_reason) is str and finish_reason in _TRUNCATED_FINISH_REASONS


def extract_llm_content(data: Any) -> str:
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
    if _declares_truncation(first):
        # The provider says it stopped early, so whatever content survived is
        # a prefix. It must not reach a caller as a finished translation; the
        # reason stays ``invalid_response`` (unusable model output) and only
        # the diagnostic detail distinguishes a cap stop from blank output.
        raise LLMTranslationError(
            TRANSLATION_UNAVAILABLE_NOTICE,
            reason="invalid_response",
            detail="truncated_output",
        )
    return require_nonblank_llm_output(content)


_extract_llm_content = extract_llm_content


def llm_error_from_response(response: httpx.Response) -> LLMTranslationError:
    if response.status_code >= 500:
        return LLMTranslationError(
            TRANSLATION_UNAVAILABLE_NOTICE, reason="upstream"
        )
    if response.status_code == 429:
        reason = "budget" if has_structured_budget_signal(response) else "rate_limit"
        return LLMTranslationError(BUDGET_EXHAUSTED_NOTICE, reason=reason)
    parameter = _unsupported_reasoning_parameter(response)
    if parameter is not None:
        LOGGER.warning(
            "llm provider rejected parameter=%s; for translation set "
            "WUWATERM_TRANSLATION_THINKING_ZH_EN=omit (Chinese to English) or "
            "WUWATERM_TRANSLATION_THINKING_EN_ZH=omit (English to Chinese) "
            "for the affected direction, or choose a provider supporting this parameter; "
            "omit uses the provider default and does not disable thinking",
            parameter,
        )
        # Keep the notice and reason: the sync application classifies the
        # returned notice, while async callers receive the safe diagnostic.
        return LLMTranslationError(
            TRANSLATION_UNAVAILABLE_NOTICE,
            reason="http",
            detail=f"unsupported_{parameter}",
        )
    if has_structured_budget_signal(response):
        return LLMTranslationError(BUDGET_EXHAUSTED_NOTICE, reason="budget")
    return LLMTranslationError(TRANSLATION_UNAVAILABLE_NOTICE, reason="http")


_llm_error_from_response = llm_error_from_response


def _unsupported_reasoning_parameter(response: httpx.Response) -> str | None:
    """Recognize a structured parameter rejection without reading its message."""
    if response.status_code != 400:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if not isinstance(error, dict) or error.get("code") != "unsupported_parameter":
        return None
    parameter = error.get("param")
    if type(parameter) is str and parameter in {"thinking", "reasoning_effort"}:
        return parameter
    return None


def has_structured_budget_signal(response: httpx.Response) -> bool:
    text = structured_llm_error_text(response)
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


_has_structured_budget_signal = has_structured_budget_signal


def structured_llm_error_text(response: httpx.Response) -> str:
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


_structured_llm_error_text = structured_llm_error_text


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
class LlmCallMetadata:
    """Non-sensitive execution metadata attached to one LLM transport attempt."""

    requested_effort: str | None = None
    effective_effort: str | None = None
    provider_profile: str | None = None
    timeout_seconds: float | None = None
    effort_omitted: bool | None = None
    effort_passthrough: bool | None = None
    attempt_kind: str | None = None
    direction: str | None = None


@dataclass(frozen=True)
class LlmCallRecord:
    """Outcome of one attempted HTTP POST. Carries no request or response text."""

    purpose: str
    outcome: CallOutcome
    status_code: int | None
    elapsed_ms: int
    requested_effort: str | None = None
    effective_effort: str | None = None
    provider_profile: str | None = None
    timeout_seconds: float | None = None
    cancelled: bool = False
    cancel_reason: str | None = None
    attempt_kind: str | None = None

    def to_metadata_dict(self) -> dict[str, Any]:
        """Return redacted per-attempt execution metadata for offline analysis."""
        return {
            "purpose": self.purpose,
            "outcome": self.outcome,
            "status_code": self.status_code,
            "elapsed_ms": self.elapsed_ms,
            "requested_effort": self.requested_effort,
            "effective_effort": self.effective_effort,
            "provider_profile": self.provider_profile,
            "timeout_seconds": self.timeout_seconds,
            "cancelled": self.cancelled,
            "cancel_reason": self.cancel_reason,
            "attempt_kind": self.attempt_kind,
        }


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


class ExecutionBudgetGuard(Protocol):
    """Protocol for operation-scoped call, cancellation, and deadline guards."""

    @property
    def is_cancelled(self) -> bool: ...

    def remaining_seconds(self) -> float | None: ...

    def check_can_continue(self) -> None: ...

    def ensure_transport_can_start(self) -> None: ...


_ACTIVE_EXECUTION_BUDGET: contextvars.ContextVar[ExecutionBudgetGuard | None] = (
    contextvars.ContextVar("wuwaterm_execution_budget", default=None)
)
_ACTIVE_CALL_METADATA: contextvars.ContextVar[LlmCallMetadata | None] = (
    contextvars.ContextVar("wuwaterm_llm_call_metadata", default=None)
)


def current_execution_budget() -> ExecutionBudgetGuard | None:
    """Return the active operation execution budget, if any."""
    return _ACTIVE_EXECUTION_BUDGET.get()


@contextmanager
def activate_execution_budget(
    budget: ExecutionBudgetGuard | None,
) -> Iterator[ExecutionBudgetGuard | None]:
    """Bind ``budget`` for the current async context and restore on exit."""
    token = _ACTIVE_EXECUTION_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _ACTIVE_EXECUTION_BUDGET.reset(token)


def current_call_metadata() -> LlmCallMetadata | None:
    """Return the active non-sensitive LLM call metadata, if any."""
    return _ACTIVE_CALL_METADATA.get()


@contextmanager
def activate_call_metadata(
    metadata: LlmCallMetadata | None,
) -> Iterator[LlmCallMetadata | None]:
    """Bind per-attempt call metadata for the current async context."""
    token = _ACTIVE_CALL_METADATA.set(metadata)
    try:
        yield metadata
    finally:
        _ACTIVE_CALL_METADATA.reset(token)


def _client_read_timeout(client: httpx.AsyncClient) -> float | None:
    timeout = getattr(client, "timeout", None)
    read_timeout = getattr(timeout, "read", None)
    if isinstance(read_timeout, (int, float)) and not isinstance(read_timeout, bool):
        return float(read_timeout)
    return None


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
    extra_body: dict | None = None,
    timeout_seconds: float | None = None,
    call_metadata: LlmCallMetadata | None = None,
) -> str:
    """POST one chat completion and return the message content.

    ``extra_body`` adds provider-specific top-level fields (for example
    DeepSeek's ``thinking``); it may not replace the fields built here.
    """
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
    for key, value in (extra_body or {}).items():
        if key in payload:
            raise ValueError(f"extra_body may not replace {key}")
        payload[key] = value
    meta = call_metadata if call_metadata is not None else current_call_metadata()
    requested_effort = (
        _safe_metadata_token(meta.requested_effort) if meta is not None else None
    )
    effective_effort = (
        _safe_metadata_token(meta.effective_effort) if meta is not None else None
    )
    provider_profile = (
        _safe_metadata_token(meta.provider_profile) if meta is not None else None
    )
    attempt_kind = (
        _safe_metadata_token(meta.attempt_kind) if meta is not None else None
    )
    recorded_timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None
        else (
            float(meta.timeout_seconds)
            if meta is not None and meta.timeout_seconds is not None
            else _client_read_timeout(client)
        )
    )
    # The last point before money is spent, and the one place the decision is
    # made. A request whose own budget is already spent must not start a POST:
    # the deadline normally stops it by cancelling the task, but a layer that
    # swallows cancellation, a retry shaped as a new call, or a fallback that
    # runs after the cancel arrived would each reach here with the answer
    # already sent. Refusing HERE is what makes "no paid work after the 504" a
    # property of the transport rather than of the chain above it. No-op
    # outside an HTTP request, and no-op for a request with no deadline.
    if request_trace.deadline_expired():
        raise LLMTranslationError(
            TRANSLATION_UNAVAILABLE_NOTICE, reason="timeout"
        )
    active_budget = current_execution_budget()
    if active_budget is not None:
        try:
            active_budget.ensure_transport_can_start()
        except LLMTranslationError as exc:
            if exc.reason == "cancelled":
                request_trace.note_cancelled()
            raise
    for hook in tuple(_PRE_SEND_HOOKS):
        hook(purpose)
    outcome: CallOutcome = "request"
    status_code: int | None = None
    cancelled = False
    cancel_reason: str | None = None
    started = time.monotonic()
    # One stage mark per attempt, so a request timed out inside this window
    # reports ``llm_post`` (with the elapsed part of the POST included) rather
    # than whatever it was doing before. No-op outside an HTTP request.
    request_trace.note_effort_resolution(
        requested_effort=requested_effort,
        effective_effort=effective_effort,
        provider_profile=provider_profile,
    )
    request_trace.begin_llm_post()
    try:
        post_options: dict[str, Any] = {}
        if timeout_seconds is not None:
            # Overrides the client's timeout for this request only.
            post_options["timeout"] = httpx.Timeout(timeout_seconds)
        add_cancel_callback = (
            getattr(active_budget, "add_cancel_callback", None)
            if active_budget is not None
            else None
        )
        if active_budget is not None and callable(add_cancel_callback):
            loop = asyncio.get_running_loop()
            post_task = asyncio.create_task(
                client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                    **post_options,
                )
            )

            def _cancel_in_flight() -> None:
                if post_task.done():
                    return
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is loop:
                    post_task.cancel()
                else:
                    with contextlib.suppress(RuntimeError):
                        loop.call_soon_threadsafe(post_task.cancel)

            remove_cancel_cb = add_cancel_callback(_cancel_in_flight)
            try:
                if active_budget.is_cancelled:
                    _cancel_in_flight()
                response = await post_task
            except asyncio.CancelledError:
                if not post_task.done():
                    post_task.cancel()
                    with contextlib.suppress(BaseException):
                        await post_task
                outcome = "cancelled"
                cancelled = True
                request_trace.note_cancelled()
                current = asyncio.current_task()
                outer_cancelling = current is not None and current.cancelling() > 0
                if not outer_cancelling and active_budget.is_cancelled:
                    cancel_reason = _safe_metadata_token(
                        getattr(active_budget, "cancel_reason", None)
                    )
                    raise LLMTranslationError(
                        TRANSLATION_UNAVAILABLE_NOTICE, reason="cancelled"
                    ) from None
                raise
            finally:
                remove_cancel_cb()
        else:
            try:
                response = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                    **post_options,
                )
            except asyncio.CancelledError:
                outcome = "cancelled"
                cancelled = True
                request_trace.note_cancelled()
                raise
        status_code = response.status_code
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            outcome = "http_error"
            raise llm_error_from_response(exc.response) from exc
        outcome = "invalid_api_response"
        data = response.json()
        content = extract_llm_content(data)
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
        request_trace.note_llm_outcome(outcome)
        request_trace.end_llm_post()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        LOGGER.info(
            "llm call purpose=%s outcome=%s status=%s elapsed_ms=%s "
            "requested_effort=%s effective_effort=%s provider_profile=%s "
            "timeout_s=%s cancelled=%s",
            purpose,
            outcome,
            status_code,
            elapsed_ms,
            requested_effort or "-",
            effective_effort or "-",
            provider_profile or "-",
            f"{recorded_timeout:.2f}" if recorded_timeout is not None else "-",
            "true" if cancelled else "false",
        )
        record = LlmCallRecord(
            purpose=purpose,
            outcome=outcome,
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            requested_effort=requested_effort,
            effective_effort=effective_effort,
            provider_profile=provider_profile,
            timeout_seconds=recorded_timeout,
            cancelled=cancelled,
            cancel_reason=cancel_reason,
            attempt_kind=attempt_kind,
        )
        for observer in tuple(_CALL_OBSERVERS):
            observer(record)

