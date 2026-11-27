"""Where a single HTTP request spent its time, as structured log fields.

An operator reading a slow — or timed-out — translation request could see only
the total: the request completed (or was refused at the 90 s budget) after N
milliseconds. That number cannot say whether the request waited for a model
slot, waited for the provider, or was post-processed, and those three have
different owners (this process' concurrency limit, the upstream provider, this
process' own pipeline). This module carries the split.

What is recorded is a small, fixed vocabulary of numbers and stage names:

* ``stage`` — one of the names below, i.e. what the request was DOING when the
  record was written. The stage names are module constants; a caller cannot
  put anything else in the field. For a request that was cancelled at its
  deadline this is the stage it DIED in, not the stage its unwinding restored
  (see :attr:`RequestTrace.stage`).
* ``slot_wait_ms`` — monotonic milliseconds spent awaiting the shared model
  concurrency slot (``SentenceTranslator._current_llm_slots``).
* ``llm_post_ms`` — monotonic milliseconds spent inside the one HTTP POST path
  (``llm_transport.post_chat_completion``), retries included.
* ``llm_calls`` — how many POST attempts were started.
* ``direction`` — ``zh->en``, ``en->zh``, or ``-`` when it is not known yet.
* ``elapsed_ms`` — from the start of this trace (the outermost middleware) to
  the moment the record is written.

Nothing else is recorded, and nothing is recorded that a caller supplied: every
value is either a number this module measured or a constant from this module's
own vocabulary. That is why this file has no way to accept text.

## Why a mutable object in a ContextVar

The trace is created once, by the outermost middleware, and every stage below it
reaches the same object through ``contextvars``. Stage marks MUTATE that object;
they never rebind the variable. That is deliberate: a child task or a worker
thread inherits a COPY of the context, so a mutation of the one shared object is
visible everywhere the request reaches, while rebinding the variable inside the
handler would not be. ``asyncio.to_thread`` — which the API uses for the
dictionary stage — is one such copy.

## The deadline guard

The middleware that bounds a request also arms :func:`set_deadline` here, and
the one place that spends money — ``llm_transport.post_chat_completion`` —
refuses to start a POST once that budget has run out. Cancelling the handler is
what normally stops a request. This guard is the part that cannot be swallowed:
a request that has already been answered must never OCCUR A SECOND model
charge, so the decision is recorded in the trace and re-read at the choke point
rather than relied on to arrive through the cancellation chain.

:func:`remaining_budget_seconds` is the read-only companion of that guard. A
layer that is deciding whether to spend one more model call asks how much of
the request's budget is left, not only whether it is gone; the answer is
derived from the same armed deadline, so the two can never disagree about
whether a budget exists.

## Not a request context

Every helper here is a no-op when no trace is active: ``current()`` is ``None``,
``llm_slot`` behaves exactly like ``async with semaphore``, and the POST hooks
return without touching anything. The bot, the channel adapter, the CLI and the
builder therefore never pay for this beyond one ContextVar lookup, and cannot
break because of it.

## Clock

One clock, ``_monotonic``, for every number this module reports, so the fields
are comparable with each other and a test can substitute a fake. The transport
keeps its own ``time.monotonic()`` measurement for the per-call log line that
already existed; the two describe the same window and are deliberately
independent, so neither can perturb the other's behaviour.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import sys
import threading
from collections import deque
from contextlib import asynccontextmanager
from time import monotonic as _monotonic
from typing import Any, AsyncIterator, Final

LOGGER = logging.getLogger(__name__)

# What the request was doing. Only these names may appear in a record.
STAGE_INIT: Final = "init"
STAGE_PREPARE_TERMS: Final = "prepare_terms"
STAGE_LLM_SLOT_WAIT: Final = "llm_slot_wait"
STAGE_LLM_SLOT_HELD: Final = "llm_slot_held"
STAGE_LLM_POST: Final = "llm_post"
STAGE_POSTPROCESS: Final = "postprocess"
STAGE_DONE: Final = "done"

_STAGES: Final[frozenset[str]] = frozenset(
    {
        STAGE_INIT,
        STAGE_PREPARE_TERMS,
        STAGE_LLM_SLOT_WAIT,
        STAGE_LLM_SLOT_HELD,
        STAGE_LLM_POST,
        STAGE_POSTPROCESS,
        STAGE_DONE,
    }
)

# The pair a model call translates between. Recorded rather than derived later,
# because a request that never returns still has a direction worth knowing.
DIRECTION_EN_TO_ZH: Final = "en->zh"
DIRECTION_ZH_TO_EN: Final = "zh->en"
DIRECTION_UNKNOWN: Final = "-"

_DIRECTIONS: Final[frozenset[str]] = frozenset(
    {DIRECTION_EN_TO_ZH, DIRECTION_ZH_TO_EN}
)

_CONFIGURED_THINKING_MODES: Final[frozenset[str]] = frozenset(
    {
        "disabled",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
        "omit",
        "auto",
    }
)
_CONCRETE_THINKING_MODES: Final[frozenset[str]] = frozenset(
    {
        "disabled",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
        "omit",
    }
)
_LLM_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        "ok",
        "http_error",
        "timeout",
        "connect",
        "request",
        "invalid_api_response",
        "cancelled",
    }
)
_SAFE_TRACE_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

UNKNOWN_VALUE: Final = "-"


def _safe_trace_token(value: object) -> str | None:
    if type(value) is str and _SAFE_TRACE_TOKEN_RE.fullmatch(value):
        return value
    return None


def direction_from_target(target: str | None) -> str:
    """Pair token from the target language a caller reported.

    ``application.TranslationOutcome.direction`` and the semantic-review
    request body both name the TARGET language (``zh`` or ``en``). Anything
    else becomes ``-``: the field never repeats a caller's string.
    """
    if target == "zh":
        return DIRECTION_EN_TO_ZH
    if target == "en":
        return DIRECTION_ZH_TO_EN
    return DIRECTION_UNKNOWN


class RequestTrace:
    """Mutable stage state for one request; see the module docstring.

    Constructed by :func:`start` and reachable through :func:`current`. Read the
    properties from outside; the ``_``-prefixed members are the primitives the
    helpers below use.
    """

    __slots__ = (
        "started_at",
        "_stage",
        "_stage_stack",
        "_slot_wait_ms",
        "_slot_wait_started",
        "_llm_post_ms",
        "_llm_post_started",
        "_llm_calls",
        "_direction",
        "_deadline_at",
        "_stop_stage",
        "_configured_thinking",
        "_initial_thinking_mode",
        "_requested_effort",
        "_effective_effort",
        "_provider_profile",
        "_cancelled",
        "_timeout_fallback_attempts",
        "_timeout_fallback_succeeded",
        "_paragraph_regroup_applied",
        "_paragraph_repair_attempts",
        "_paragraph_repair_succeeded",
        "_html_fallback_attempts",
        "_budget_exhausted",
        "_timed_out",
        "_llm_outcomes",
    )

    def __init__(self) -> None:
        self.started_at = _monotonic()
        self._stage = STAGE_INIT
        self._stage_stack: list[str] = []
        self._slot_wait_ms = 0.0
        self._slot_wait_started: float | None = None
        self._llm_post_ms = 0.0
        self._llm_post_started: float | None = None
        self._llm_calls = 0
        self._direction = DIRECTION_UNKNOWN
        self._deadline_at: float | None = None
        self._stop_stage: str | None = None
        self._configured_thinking = UNKNOWN_VALUE
        self._initial_thinking_mode = UNKNOWN_VALUE
        self._requested_effort = UNKNOWN_VALUE
        self._effective_effort = UNKNOWN_VALUE
        self._provider_profile = UNKNOWN_VALUE
        self._cancelled = False
        self._timeout_fallback_attempts = 0
        self._timeout_fallback_succeeded = 0
        self._paragraph_regroup_applied = 0
        self._paragraph_repair_attempts = 0
        self._paragraph_repair_succeeded = 0
        self._html_fallback_attempts = 0
        self._budget_exhausted = 0
        self._timed_out = False
        self._llm_outcomes: dict[str, int] = {}

    # -- what a record is written from -------------------------------------

    @property
    def stage(self) -> str:
        """What the request was doing when it stopped, or is doing now.

        For a request cancelled at its deadline these differ. A span that opens
        a stage also restores the one it was in when it closes, which is what
        nesting is for — so by the time the deadline layer writes its record,
        the unwind has ALREADY put the enclosing stage back, and reporting that
        would name ``prepare_terms`` for a request that spent its whole budget
        queued for a model slot. The span that was cancelled therefore records
        the stage in force at that moment (:meth:`_note_cancellation`), and it
        wins here: a timeout record has to say where the time went.
        """
        return self._stop_stage if self._stop_stage is not None else self._stage

    @property
    def slot_wait_ms(self) -> float:
        """Accumulated slot wait, including a wait still in progress.

        The in-flight part matters for the timeout record: a request refused at
        the budget WHILE queued has no completed acquisition to have recorded.
        """
        waited = self._slot_wait_ms
        started = self._slot_wait_started
        if started is not None:
            waited += (_monotonic() - started) * 1000.0
        return waited

    @property
    def llm_post_ms(self) -> float:
        """Accumulated POST time, including a POST still in progress."""
        posted = self._llm_post_ms
        started = self._llm_post_started
        if started is not None:
            posted += (_monotonic() - started) * 1000.0
        return posted

    @property
    def llm_calls(self) -> int:
        return self._llm_calls

    @property
    def direction(self) -> str:
        return self._direction

    @property
    def elapsed_ms(self) -> float:
        return (_monotonic() - self.started_at) * 1000.0

    @property
    def configured_thinking(self) -> str:
        return self._configured_thinking

    @property
    def initial_thinking_mode(self) -> str:
        return self._initial_thinking_mode

    @property
    def requested_effort(self) -> str:
        return self._requested_effort

    @property
    def effective_effort(self) -> str:
        return self._effective_effort

    @property
    def provider_profile(self) -> str:
        return self._provider_profile

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def timeout_fallback_attempts(self) -> int:
        return self._timeout_fallback_attempts

    @property
    def timeout_fallback_succeeded(self) -> int:
        return self._timeout_fallback_succeeded

    @property
    def paragraph_regroup_applied(self) -> int:
        return self._paragraph_regroup_applied

    @property
    def paragraph_repair_attempts(self) -> int:
        return self._paragraph_repair_attempts

    @property
    def paragraph_repair_succeeded(self) -> int:
        return self._paragraph_repair_succeeded

    @property
    def html_fallback_attempts(self) -> int:
        return self._html_fallback_attempts

    @property
    def budget_exhausted(self) -> int:
        return self._budget_exhausted

    @property
    def timed_out(self) -> bool:
        return self._timed_out

    @property
    def llm_outcomes(self) -> dict[str, int]:
        return dict(self._llm_outcomes)

    @property
    def llm_involved(self) -> bool:
        """Whether this request got as far as the model stage.

        The completion record carries the timing split only for such a request,
        so every other record keeps exactly the shape it had before.
        """
        return self._llm_calls > 0 or self._slot_wait_ms > 0.0

    @property
    def has_strategy_activity(self) -> bool:
        """Whether any execution strategy, thinking mode, or budget bound fired."""
        return (
            self._configured_thinking != UNKNOWN_VALUE
            or self._initial_thinking_mode != UNKNOWN_VALUE
            or self._timeout_fallback_attempts > 0
            or self._timeout_fallback_succeeded > 0
            or self._paragraph_regroup_applied > 0
            or self._paragraph_repair_attempts > 0
            or self._paragraph_repair_succeeded > 0
            or self._html_fallback_attempts > 0
            or self._budget_exhausted > 0
        )

    # -- the deadline guard ------------------------------------------------

    def _arm_deadline(self, seconds: float) -> None:
        self._deadline_at = _monotonic() + seconds

    def _deadline_expired(self) -> bool:
        at = self._deadline_at
        return at is not None and _monotonic() >= at

    def _remaining_budget_seconds(self) -> float | None:
        at = self._deadline_at
        if at is None:
            return None
        return max(0.0, at - _monotonic())

    # -- stage marks -------------------------------------------------------

    def _set_stage(self, name: str) -> None:
        self._stage = name if name in _STAGES else STAGE_INIT
        # A stage set from outside the pipeline's own nesting (the route
        # handler marking prepare_terms/postprocess) is where the request is
        # now; anything left on the stack by a cancelled inner span would
        # otherwise be restored over it later.
        self._stage_stack.clear()

    def _push_stage(self, name: str) -> None:
        self._stage_stack.append(self._stage)
        self._stage = name if name in _STAGES else STAGE_INIT

    def _pop_stage(self) -> None:
        if self._stage_stack:
            self._stage = self._stage_stack.pop()

    def _replace_top_stage(self, name: str) -> None:
        self._stage = name if name in _STAGES else STAGE_INIT

    def _note_cancellation(self) -> None:
        """Remember the stage a cancellation interrupted, the first time it does.

        Called from a span's ``finally``, which runs on the way out in either
        direction and sees the exception still in flight there. Only a span that
        was ACTIVE at the moment of cancellation has anything to record, and
        only the innermost one is that span — an outer span sees the same
        cancellation a moment later, when the inner one has already answered.
        """
        if self._stop_stage is not None:
            return
        if isinstance(sys.exc_info()[1], asyncio.CancelledError):
            self._stop_stage = self._stage

    def _finish_slot_wait(self) -> None:
        started = self._slot_wait_started
        if started is None:
            return
        self._slot_wait_started = None
        self._slot_wait_ms += (_monotonic() - started) * 1000.0

    def _finish_llm_post(self) -> None:
        started = self._llm_post_started
        if started is None:
            return
        self._llm_post_started = None
        self._llm_post_ms += (_monotonic() - started) * 1000.0

    def _note_thinking(self, configured: str, initial: str) -> None:
        if type(configured) is str and configured in _CONFIGURED_THINKING_MODES:
            self._configured_thinking = configured
        if type(initial) is str and initial in _CONCRETE_THINKING_MODES:
            self._initial_thinking_mode = initial
            self._requested_effort = initial

    def _note_effort_resolution(
        self,
        *,
        requested_effort: str | None = None,
        effective_effort: str | None = None,
        provider_profile: str | None = None,
    ) -> None:
        if requested_effort is not None:
            if type(requested_effort) is str and requested_effort in _CONCRETE_THINKING_MODES:
                self._requested_effort = requested_effort
        if effective_effort is not None:
            token = _safe_trace_token(effective_effort)
            if token is not None:
                self._effective_effort = token
        if provider_profile is not None:
            token = _safe_trace_token(provider_profile)
            if token is not None:
                self._provider_profile = token

    def _note_cancelled(self) -> None:
        self._cancelled = True

    def _note_timeout_fallback(self, *, succeeded: bool) -> None:
        self._timeout_fallback_attempts += 1
        if succeeded:
            self._timeout_fallback_succeeded += 1

    def _note_paragraph_regroup(self) -> None:
        self._paragraph_regroup_applied += 1

    def _note_paragraph_repair(self, *, succeeded: bool) -> None:
        self._paragraph_repair_attempts += 1
        if succeeded:
            self._paragraph_repair_succeeded += 1

    def _note_html_fallback(self) -> None:
        self._html_fallback_attempts += 1

    def _note_budget_exhausted(self) -> None:
        self._budget_exhausted += 1

    def _note_llm_outcome(self, outcome: str) -> None:
        token = (
            outcome
            if type(outcome) is str and outcome in _LLM_OUTCOMES
            else UNKNOWN_VALUE
        )
        self._llm_outcomes[token] = self._llm_outcomes.get(token, 0) + 1

    def _mark_timed_out(self) -> None:
        self._timed_out = True


_active: contextvars.ContextVar[RequestTrace | None] = contextvars.ContextVar(
    "wuwaterm_request_trace", default=None
)


def start() -> RequestTrace:
    """Begin the trace for the request running in this context."""
    trace = RequestTrace()
    _active.set(trace)
    return trace


def reset() -> None:
    """Forget the trace for this context; later marks become no-ops."""
    _active.set(None)


def current() -> RequestTrace | None:
    """The trace for the request in this context, or ``None`` outside one."""
    return _active.get()


def set_stage(name: str) -> None:
    """Record what the request is doing now. No-op outside a request."""
    trace = _active.get()
    if trace is not None:
        trace._set_stage(name)


def set_direction(direction: str) -> None:
    """Record the language pair. Anything but a known token becomes ``-``."""
    trace = _active.get()
    if trace is None:
        return
    trace._direction = direction if direction in _DIRECTIONS else DIRECTION_UNKNOWN


@asynccontextmanager
async def llm_slot(semaphore: asyncio.Semaphore) -> AsyncIterator[None]:
    """Hold the model concurrency slot, recording the wait as its own stage.

    Outside a request this is ``async with semaphore`` and nothing else: the
    exact acquisition semantics callers had before this module existed are
    preserved, including cancellation while queued.

    The wait is recorded whether the slot is eventually acquired or the request
    is cancelled at its time budget, because a request that never got a slot is
    the case the timing exists for.
    """
    trace = _active.get()
    if trace is None:
        async with semaphore:
            yield
        return
    trace._push_stage(STAGE_LLM_SLOT_WAIT)
    trace._slot_wait_started = _monotonic()
    try:
        async with semaphore:
            trace._finish_slot_wait()
            trace._replace_top_stage(STAGE_LLM_SLOT_HELD)
            yield
    finally:
        # Runs before the stack unwinds in either direction: acquired and
        # finished, cancelled while queued, or raised inside the slot.
        trace._note_cancellation()
        trace._finish_slot_wait()
        trace._pop_stage()


def set_deadline(seconds: float) -> None:
    """Arm the guard that keeps a request from starting work after its budget.

    Called by the middleware that applies a budget, so the deadline is measured
    from the moment that budget starts rather than from the start of the trace.
    No-op outside a request.
    """
    trace = _active.get()
    if trace is not None:
        trace._arm_deadline(seconds)


def deadline_expired() -> bool:
    """Whether this request's own budget is already spent.

    ``False`` outside a request and ``False`` for a request whose middleware
    never armed a budget: both mean "nothing says this work may not start".
    """
    trace = _active.get()
    return trace is not None and trace._deadline_expired()


def remaining_budget_seconds() -> float | None:
    """Seconds left of this request's budget, or ``None`` when there is none.

    A layer that is about to spend a second model call needs the same answer
    the guard gives, one step earlier: not "may anything start" (the guard's
    question) but "is there still enough of the request's own budget for this
    attempt to have a chance". ``None`` is the honest answer outside a request
    and for a request whose middleware never armed a budget — both mean no
    budget is known, which is not the same as a budget of zero. An armed
    deadline that has already passed reports ``0.0`` rather than a negative
    number, so callers compare against a positive bound.
    """
    trace = _active.get()
    if trace is None:
        return None
    return trace._remaining_budget_seconds()


def begin_llm_post() -> None:
    """Mark the start of one model HTTP POST attempt. No-op outside a request."""
    trace = _active.get()
    if trace is None:
        return
    trace._llm_calls += 1
    trace._llm_post_started = _monotonic()
    trace._push_stage(STAGE_LLM_POST)


def end_llm_post() -> None:
    """Accumulate the attempt and leave the ``llm_post`` stage. Idempotent."""
    trace = _active.get()
    if trace is None:
        return
    trace._note_cancellation()
    trace._finish_llm_post()
    trace._pop_stage()


def timing_fields(
    trace: RequestTrace | None,
    *,
    include_elapsed: bool = False,
    include_stage: bool = False,
) -> str:
    """Render the timing fields as one leading-space-delimited fragment.

    Values are formatted here, not passed as arguments, so a record cannot gain
    a caller-influenced field through a mismatched format string. ``trace``
    being ``None`` renders ``-`` for every value rather than a zero: nothing was
    measured, and that is not the same as measuring nothing.
    """
    parts: list[str] = []
    if include_elapsed:
        parts.append(
            f"elapsed_ms={trace.elapsed_ms:.1f}" if trace is not None
            else f"elapsed_ms={UNKNOWN_VALUE}"
        )
    if include_stage:
        parts.append(f"stage={trace.stage}" if trace is not None
                     else f"stage={UNKNOWN_VALUE}")
    if trace is None:
        parts.extend(
            (
                f"slot_wait_ms={UNKNOWN_VALUE}",
                f"llm_post_ms={UNKNOWN_VALUE}",
                f"llm_calls={UNKNOWN_VALUE}",
                f"direction={UNKNOWN_VALUE}",
            )
        )
    else:
        parts.extend(
            (
                f"slot_wait_ms={trace.slot_wait_ms:.1f}",
                f"llm_post_ms={trace.llm_post_ms:.1f}",
                f"llm_calls={trace.llm_calls}",
                f"direction={trace.direction}",
            )
        )
    return " " + " ".join(parts)


def completion_fields(trace: RequestTrace | None) -> str:
    """The timing split for a completion record, or ``""`` when there is none.

    A request that never reached the model keeps the record shape it has always
    had; only a request with model work adds the fields.
    """
    if trace is None or not trace.llm_involved:
        return ""
    return timing_fields(trace)


def note_thinking(configured: str, initial: str) -> None:
    """Record configured and initial thinking modes for the active request."""
    trace = _active.get()
    if trace is not None:
        trace._note_thinking(configured, initial)


def note_effort_resolution(
    *,
    requested_effort: str | None = None,
    effective_effort: str | None = None,
    provider_profile: str | None = None,
) -> None:
    """Record resolved reasoning effort and provider profile for the active request."""
    trace = _active.get()
    if trace is not None:
        trace._note_effort_resolution(
            requested_effort=requested_effort,
            effective_effort=effective_effort,
            provider_profile=provider_profile,
        )


def note_cancelled() -> None:
    """Mark the active request trace as observing explicit or task cancellation."""
    trace = _active.get()
    if trace is not None:
        trace._note_cancelled()


def note_timeout_fallback(*, succeeded: bool) -> None:
    """Record one timeout fallback attempt and whether it rescued the call."""
    trace = _active.get()
    if trace is not None:
        trace._note_timeout_fallback(succeeded=succeeded)


def note_paragraph_regroup() -> None:
    """Record that conservative paragraph regrouping reshaped an answer."""
    trace = _active.get()
    if trace is not None:
        trace._note_paragraph_regroup()


def note_paragraph_repair(*, succeeded: bool) -> None:
    """Record one paragraph-repair attempt and whether it matched layout."""
    trace = _active.get()
    if trace is not None:
        trace._note_paragraph_repair(succeeded=succeeded)


def note_html_fallback() -> None:
    """Record one HTML-to-plain fallback inside a translation operation."""
    trace = _active.get()
    if trace is not None:
        trace._note_html_fallback()


def note_budget_exhausted() -> None:
    """Record that an operation execution budget stopped an extra call."""
    trace = _active.get()
    if trace is not None:
        trace._note_budget_exhausted()


def note_llm_outcome(outcome: str) -> None:
    """Record the outcome vocabulary token for one attempted LLM POST."""
    trace = _active.get()
    if trace is not None:
        trace._note_llm_outcome(outcome)


def mark_timed_out() -> None:
    """Mark the active request trace as ended by wall-clock timeout."""
    trace = _active.get()
    if trace is not None:
        trace._mark_timed_out()


def strategy_fields(trace: RequestTrace | None) -> str:
    """Render fixed-vocabulary strategy counters for structured logging."""
    if trace is None:
        return (
            f"configured_thinking={UNKNOWN_VALUE} "
            f"initial_thinking={UNKNOWN_VALUE} "
            "timeout_fallback=0/0 "
            "paragraph_regroup=0 "
            "paragraph_repair=0/0 "
            "html_fallback=0 "
            "budget_exhausted=0"
        )
    return (
        f"configured_thinking={trace.configured_thinking} "
        f"initial_thinking={trace.initial_thinking_mode} "
        f"timeout_fallback={trace.timeout_fallback_succeeded}/{trace.timeout_fallback_attempts} "
        f"paragraph_regroup={trace.paragraph_regroup_applied} "
        f"paragraph_repair={trace.paragraph_repair_succeeded}/{trace.paragraph_repair_attempts} "
        f"html_fallback={trace.html_fallback_attempts} "
        f"budget_exhausted={trace.budget_exhausted}"
    )


def log_strategy_summary(request_id: str, trace: RequestTrace | None) -> None:
    """Emit a separate non-sensitive strategy log line on ``wuwaterm.request_trace``."""
    if trace is None or not trace.has_strategy_activity:
        return
    LOGGER.info(
        "llm strategy request_id=%s direction=%s %s",
        request_id,
        trace.direction,
        strategy_fields(trace),
    )


def _rate(num: int | float, den: int | float) -> float | None:
    if den <= 0:
        return None
    return round(float(num) / float(den), 4)


def _percentile(sorted_vals: list[float], q: float) -> float:
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _latency_summary(values: deque[float]) -> dict[str, int | float | None]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    n = len(ordered)
    return {
        "count": n,
        "mean": round(sum(ordered) / n, 3),
        "p50": round(_percentile(ordered, 0.50), 3),
        "p95": round(_percentile(ordered, 0.95), 3),
        "max": round(ordered[-1], 3),
    }


class RuntimeMetrics:
    """Thread-safe in-process aggregator for request, latency, cost, and strategy metrics."""

    def __init__(self, *, sample_window: int = 512) -> None:
        if sample_window < 1:
            raise ValueError("sample_window must be at least 1")
        self._lock = threading.Lock()
        self._sample_window = sample_window
        self._requests_total = 0
        self._requests_succeeded = 0
        self._requests_client_error = 0
        self._requests_server_error = 0
        self._requests_timed_out = 0
        self._requests_client_gone = 0

        self._llm_requests_total = 0
        self._llm_requests_succeeded = 0
        self._llm_requests_timed_out = 0
        self._llm_requests_failed = 0
        self._llm_calls_total = 0
        self._llm_outcomes: dict[str, int] = {}
        self._by_direction: dict[str, dict[str, int]] = {}

        self._request_elapsed_ms: deque[float] = deque(maxlen=sample_window)
        self._slot_wait_ms: deque[float] = deque(maxlen=sample_window)
        self._llm_post_ms: deque[float] = deque(maxlen=sample_window)

        self._configured_thinking_counts: dict[str, int] = {}
        self._initial_thinking_counts: dict[str, int] = {}
        self._timeout_fallback_attempts = 0
        self._timeout_fallback_succeeded = 0
        self._paragraph_regroup_applied = 0
        self._paragraph_repair_attempts = 0
        self._paragraph_repair_succeeded = 0
        self._html_fallback_attempts = 0
        self._budget_exhausted_count = 0

    def record_request(
        self,
        *,
        status_code: int,
        elapsed_ms: float,
        trace: RequestTrace | None,
        timed_out: bool = False,
    ) -> None:
        """Record metrics for one finished HTTP request without retaining caller text."""
        is_timed_out = (
            timed_out
            or status_code == 504
            or (trace is not None and trace.timed_out)
        )
        is_succeeded = 200 <= status_code < 400 and not is_timed_out
        is_client_gone = status_code == 499
        bounded_elapsed = max(0.0, float(elapsed_ms))

        with self._lock:
            self._requests_total += 1
            if is_timed_out:
                self._requests_timed_out += 1
            if is_succeeded:
                self._requests_succeeded += 1
            elif is_client_gone:
                self._requests_client_gone += 1
            elif 400 <= status_code < 500:
                self._requests_client_error += 1
            elif 500 <= status_code < 600:
                self._requests_server_error += 1

            self._request_elapsed_ms.append(bounded_elapsed)
            if trace is None:
                return

            if trace.llm_involved:
                self._slot_wait_ms.append(max(0.0, float(trace.slot_wait_ms)))
                self._llm_post_ms.append(max(0.0, float(trace.llm_post_ms)))

            outcomes = trace.llm_outcomes
            is_llm_req = (
                trace.llm_involved or trace.has_strategy_activity or bool(outcomes)
            )
            if is_llm_req:
                self._llm_requests_total += 1
                if is_succeeded:
                    self._llm_requests_succeeded += 1
                elif is_timed_out:
                    self._llm_requests_timed_out += 1
                else:
                    self._llm_requests_failed += 1
                self._llm_calls_total += trace.llm_calls

                direction = trace.direction
                bucket = self._by_direction.get(direction)
                if bucket is None:
                    bucket = {
                        "requests": 0,
                        "succeeded": 0,
                        "timed_out": 0,
                        "calls": 0,
                    }
                    self._by_direction[direction] = bucket
                bucket["requests"] += 1
                if is_succeeded:
                    bucket["succeeded"] += 1
                elif is_timed_out:
                    bucket["timed_out"] += 1
                bucket["calls"] += trace.llm_calls

                for outcome_key, count in outcomes.items():
                    self._llm_outcomes[outcome_key] = (
                        self._llm_outcomes.get(outcome_key, 0) + count
                    )

            cfg = trace.configured_thinking
            if cfg != UNKNOWN_VALUE:
                self._configured_thinking_counts[cfg] = (
                    self._configured_thinking_counts.get(cfg, 0) + 1
                )
            init_mode = trace.initial_thinking_mode
            if init_mode != UNKNOWN_VALUE:
                self._initial_thinking_counts[init_mode] = (
                    self._initial_thinking_counts.get(init_mode, 0) + 1
                )
            self._timeout_fallback_attempts += trace.timeout_fallback_attempts
            self._timeout_fallback_succeeded += trace.timeout_fallback_succeeded
            self._paragraph_regroup_applied += trace.paragraph_regroup_applied
            self._paragraph_repair_attempts += trace.paragraph_repair_attempts
            self._paragraph_repair_succeeded += trace.paragraph_repair_succeeded
            self._html_fallback_attempts += trace.html_fallback_attempts
            self._budget_exhausted_count += trace.budget_exhausted

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of aggregated metrics."""
        with self._lock:
            return {
                "requests": {
                    "total": self._requests_total,
                    "succeeded": self._requests_succeeded,
                    "client_error": self._requests_client_error,
                    "server_error": self._requests_server_error,
                    "timed_out": self._requests_timed_out,
                    "client_gone": self._requests_client_gone,
                    "success_rate": _rate(
                        self._requests_succeeded, self._requests_total
                    ),
                    "timeout_rate": _rate(
                        self._requests_timed_out, self._requests_total
                    ),
                },
                "llm": {
                    "requests": self._llm_requests_total,
                    "succeeded": self._llm_requests_succeeded,
                    "timed_out": self._llm_requests_timed_out,
                    "failed": self._llm_requests_failed,
                    "success_rate": _rate(
                        self._llm_requests_succeeded, self._llm_requests_total
                    ),
                    "timeout_rate": _rate(
                        self._llm_requests_timed_out, self._llm_requests_total
                    ),
                    "calls_total": self._llm_calls_total,
                    "avg_calls_per_request": _rate(
                        self._llm_calls_total, self._llm_requests_total
                    ),
                    "outcomes": dict(sorted(self._llm_outcomes.items())),
                    "by_direction": {
                        direction: {
                            "requests": entry["requests"],
                            "succeeded": entry["succeeded"],
                            "timed_out": entry["timed_out"],
                            "calls": entry["calls"],
                            "avg_calls_per_request": _rate(
                                entry["calls"], entry["requests"]
                            ),
                        }
                        for direction, entry in sorted(self._by_direction.items())
                    },
                },
                "latency_ms": {
                    "request_elapsed": _latency_summary(self._request_elapsed_ms),
                    "slot_wait": _latency_summary(self._slot_wait_ms),
                    "llm_post": _latency_summary(self._llm_post_ms),
                },
                "strategy": {
                    "configured_thinking": dict(
                        sorted(self._configured_thinking_counts.items())
                    ),
                    "initial_thinking": dict(
                        sorted(self._initial_thinking_counts.items())
                    ),
                    "timeout_fallback": {
                        "attempts": self._timeout_fallback_attempts,
                        "succeeded": self._timeout_fallback_succeeded,
                        "rescue_rate": _rate(
                            self._timeout_fallback_succeeded,
                            self._timeout_fallback_attempts,
                        ),
                    },
                    "paragraph_regroup_applied": self._paragraph_regroup_applied,
                    "paragraph_repair": {
                        "attempts": self._paragraph_repair_attempts,
                        "succeeded": self._paragraph_repair_succeeded,
                        "rescue_rate": _rate(
                            self._paragraph_repair_succeeded,
                            self._paragraph_repair_attempts,
                        ),
                    },
                    "html_fallback_attempts": self._html_fallback_attempts,
                    "budget_exhausted_count": self._budget_exhausted_count,
                },
            }
