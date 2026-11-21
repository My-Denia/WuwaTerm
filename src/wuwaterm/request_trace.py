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
  put anything else in the field.
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
they never rebind the variable. That is deliberate: Starlette's
``BaseHTTPMiddleware`` runs the rest of the stack in a child task, and a child
task inherits a COPY of the context. A mutation of a shared object is therefore
visible upstream when the middleware reads it again (after a timeout, or for the
completion record), while rebinding the variable inside the handler would not
be. ``asyncio.to_thread`` — which the API uses for the dictionary stage — copies
the context too, so the same object is reachable from that worker thread.

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
from contextlib import asynccontextmanager
from time import monotonic as _monotonic
from typing import AsyncIterator, Final

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

UNKNOWN_VALUE: Final = "-"


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

    # -- what a record is written from -------------------------------------

    @property
    def stage(self) -> str:
        return self._stage

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
    def llm_involved(self) -> bool:
        """Whether this request got as far as the model stage.

        The completion record carries the timing split only for such a request,
        so every other record keeps exactly the shape it had before.
        """
        return self._llm_calls > 0 or self._slot_wait_ms > 0.0

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
        trace._finish_slot_wait()
        trace._pop_stage()


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
