"""Protocol-neutral translation application layer.

This module owns the shared dictionary-first pipeline used by Telegram command
handlers, the HTTP API, and the API process's private web layer:

    prepare -> resolve direction -> exact dictionary hit -> trusted fuzzy hit
    -> input length gate -> long-text splitting -> term-locked LLM call

Linked-channel auto-translation is intentionally different: ``channel.py``
owns channel admission, freshness, edit and delivery orchestration and calls
the protocol-neutral term and sentence primitives directly. It does not return
a :class:`TranslationOutcome` from this pipeline.

Nothing here knows about Telegram, HTTP, chats, users or markup formats. For
callers that use this pipeline, the two adapter-specific steps are injected:

* ``markup_translator`` — an adapter-supplied async callable that translates a
  markup payload (Telegram HTML today) while preserving its structure. The
  Telegram command adapter injects one; the HTTP API deliberately injects none
  and is therefore plain-text only.
* ``splitter`` — how oversized text is cut into LLM-sized chunks. The default
  splits on plain line boundaries; the Telegram adapter injects its UTF-16
  aware splitter so message limits keep their existing meaning.

Pipeline callers receive a :class:`TranslationOutcome` and decide how to
render it.
User-facing wording that belongs to a protocol (bilingual Telegram notices,
HTTP error envelopes) stays in the adapter; this layer returns a stable
``kind`` plus a stable ``error_code`` from :data:`TRANSLATION_ERROR_CODES`.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Hashable

from .lookup import TermService
from .models import LookupCandidate
from .normalize import has_cjk, normalize_ascii
from .review import (
    MAX_SIDE_SCALARS,
    ReviewReport,
    ReviewRequestError,
    review_pair as _review_pair,
)
from .sentence import (
    BUDGET_EXHAUSTED_NOTICE,
    DEFAULT_LLM_MAX_CONCURRENCY,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    LLMFailureDiagnostic,  # Intentionally re-exported for adapters.
    LLMTranslationError,
    SentenceTranslator,
    _safe_llm_failure_diagnostic,
    _llm_configured,
)
from .translation_policy import LLM_FAILURE_NOTICES, LLM_INPUT_CHAR_LIMIT


LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Stable application outcome vocabulary
# --------------------------------------------------------------------------

KIND_NOOP = "noop"
KIND_EXACT = "exact"
KIND_FUZZY = "fuzzy"
KIND_LLM = "llm"
KIND_ERROR = "error"

TRANSLATION_KINDS = frozenset({KIND_NOOP, KIND_EXACT, KIND_FUZZY, KIND_LLM, KIND_ERROR})

# Error codes are part of the published contract: adapters map them to their
# own transport (HTTP status, Telegram notice) but must not invent new ones
# without extending this set.
ERROR_UNAUTHORIZED = "unauthorized"
ERROR_FORBIDDEN = "forbidden"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_PAYLOAD_TOO_LARGE = "payload_too_large"
ERROR_INVALID_REQUEST = "invalid_request"
ERROR_INPUT_TOO_LONG = "input_too_long"
ERROR_LLM_UNAVAILABLE = "llm_unavailable"
ERROR_LLM_BUDGET_EXHAUSTED = "llm_budget_exhausted"
ERROR_INTERNAL = "internal"

TRANSLATION_ERROR_CODES = frozenset(
    {
        ERROR_UNAUTHORIZED,
        ERROR_FORBIDDEN,
        ERROR_RATE_LIMITED,
        ERROR_PAYLOAD_TOO_LARGE,
        ERROR_INVALID_REQUEST,
        ERROR_INPUT_TOO_LONG,
        ERROR_LLM_UNAVAILABLE,
        ERROR_LLM_BUDGET_EXHAUSTED,
        ERROR_INTERNAL,
    }
)

DIRECTION_TO_CHINESE = "zh"
DIRECTION_TO_ENGLISH = "en"

# Protocol-neutral pipeline messages. Adapters may replace them; the Telegram
# adapter keeps them verbatim because they are already its published wording.
EMPTY_INPUT_MESSAGE = "Nothing to translate after removing metadata."


def input_too_long_message(limit: int) -> str:
    return f"Input is too long for translation ({limit} character limit)."


# LLM failure reasons raised by :mod:`wuwaterm.sentence`. Only quota
# exhaustion is separable for the caller; everything else is "upstream did not
# answer usably" and must not leak transport detail into the contract.
_BUDGET_REASONS = frozenset({"budget"})


def error_code_for_llm_reason(reason: str | None) -> str:
    """Map an :class:`LLMTranslationError` reason to a contract error code."""
    if reason in _BUDGET_REASONS:
        return ERROR_LLM_BUDGET_EXHAUSTED
    return ERROR_LLM_UNAVAILABLE


def _safe_business_reason(value: object) -> str | None:
    """Return a builtin string without custom hash/equality/string calls."""
    try:
        if not isinstance(value, str):
            return None
        # Calling the builtin directly bypasses a subclass's __str__. Only a
        # plain string can reach set membership and its hash/equality methods.
        normalized = str.__str__(value)
        return normalized if type(normalized) is str else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# Request / result models
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TranslationJob:
    """One protocol-neutral translation request.

    ``markup`` carries an optional adapter-specific rich-text payload that only
    a matching ``markup_translator`` understands. Adapters without markup pass
    ``None`` and always get plain text back.
    """

    text: str
    markup: str | None = None
    forced_to_chinese: bool | None = None


@dataclass(frozen=True)
class TranslationOutcome:
    """Result of the shared pipeline.

    ``dictionary_miss`` reports that the answer came from the LLM for a short
    query with no locked dictionary term; adapters decide whether and how to
    surface that (the Telegram adapter appends its bilingual flag).
    """

    kind: str
    text: str
    to_chinese: bool = False
    dictionary_miss: bool = False
    markup_used: bool = False
    error_code: str | None = None
    llm_failure: LLMFailureDiagnostic | None = field(
        default=None, compare=False, repr=False
    )

    @property
    def direction(self) -> str:
        return DIRECTION_TO_CHINESE if self.to_chinese else DIRECTION_TO_ENGLISH

    @property
    def failed(self) -> bool:
        return self.kind == KIND_ERROR


@dataclass(frozen=True)
class MarkupTranslation:
    """What an adapter's markup translator reports back to the pipeline.

    Exactly one of the three shapes is meaningful:

    * ``text`` set — markup translated successfully.
    * ``fallback_to_plain`` — markup translation is not possible or produced
      structurally broken output; retry the plain path (formatting is lost but
      the request is still answered).
    * ``message`` + ``error_code`` — hard failure; the pipeline stops.
    """

    text: str | None = None
    message: str | None = None
    error_code: str | None = None
    fallback_to_plain: bool = False


MarkupTranslator = Callable[..., Awaitable[MarkupTranslation]]
TextSplitter = Callable[[str, int], Sequence[str]]
# Called immediately before each LLM request, after the concurrency slot is
# held; awaitable results are awaited. Raise to refuse without an upstream call.
LlmCallGuard = Callable[[], None | Awaitable[None]]
# How an adapter runs the pipeline's BLOCKING stage. The dictionary stage opens
# SQLite and, for short ASCII queries, scores every term row; an adapter with a
# shared event loop (the HTTP server) passes ``asyncio.to_thread`` so that work
# cannot stall unrelated requests. The Telegram adapter leaves it unset because
# its single loop already serialises handler work.
Offload = Callable[..., Awaitable[object]]


# --------------------------------------------------------------------------
# Direction, dictionary and gating helpers
# --------------------------------------------------------------------------


def _resolve_to_chinese(prepared: str, forced_to_chinese: bool | None) -> bool:
    if forced_to_chinese is not None:
        return forced_to_chinese
    return not has_cjk(prepared)


# A single-token-ish query: no sentence punctuation, at most 32 characters.
SHORT_QUERY_RE = re.compile(r"^[^\s。！？!?，,；;：:\n]{1,32}$")


def _is_short_query(text: str) -> bool:
    return SHORT_QUERY_RE.match(text) is not None


def _is_ascii_fuzzy_query(text: str) -> bool:
    return bool(text) and text.isascii() and SHORT_QUERY_RE.match(text) is not None


# Fuzzy pinyin answers a query directly only for trustworthy match shapes.
# Prefix/substring hits score >=80 even for 2-3 letter queries, so common
# English words get hijacked ("he" is inside "shenghai" -> Echo); those
# reasons additionally require a 4+ letter query. "exact" appears when an
# exact hit had an empty requested-side official and lookup() fell through.
_FUZZY_TRUSTED_REASONS = frozenset({"exact", "pinyin", "pinyin-abbrev"})
_FUZZY_LENGTH_GATED_REASONS = frozenset({"pinyin-prefix", "pinyin-substring"})
_FUZZY_MIN_GATED_QUERY_LEN = 4


def _fuzzy_dictionary_answer(
    service: TermService, prepared: str, *, to_chinese: bool
) -> str | None:
    if not _is_ascii_fuzzy_query(prepared):
        return None
    fuzzy = service.lookup(prepared, limit=5)
    best = fuzzy.best
    if best is None or best.score < 80.0:
        return None
    query_ascii = normalize_ascii(prepared)
    if best.reason not in _FUZZY_TRUSTED_REASONS and not (
        best.reason in _FUZZY_LENGTH_GATED_REASONS
        and len(query_ascii) >= _FUZZY_MIN_GATED_QUERY_LEN
    ):
        # An exact abbreviation hit can be reported as "pinyin-prefix" when
        # the abbreviation is also a prefix of the full pinyin ("sh" for
        # "shenghai"); the abbreviation feature stays trusted either way.
        if not (query_ascii and best.entry.pinyin_abbrev == query_ascii):
            return None
    return best.entry.zh if to_chinese else best.entry.en


def _has_locked_terms(translator: SentenceTranslator, text: str) -> bool:
    return bool(translator.lock_terms(text).locks)


def _should_append_dict_miss(
    prepared: str, translator: SentenceTranslator, translated: str
) -> bool:
    return (
        translated not in LLM_FAILURE_NOTICES
        and _is_short_query(prepared)
        and not _has_locked_terms(translator, prepared)
    )


def split_plain_text(text: str, limit: int = LLM_INPUT_CHAR_LIMIT) -> list[str]:
    """Split ``text`` into <= ``limit`` character chunks on line boundaries.

    Protocol-neutral default splitter. Lines are kept whole when they fit; a
    single line longer than ``limit`` is hard-wrapped so no chunk can exceed
    the LLM input bound.

    The contract adapters may rely on: for non-empty input the result is never
    empty, so the caller always makes at least one translation attempt rather
    than silently answering with nothing.
    """
    limit = max(1, int(limit))
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0

    for line in text.split("\n"):
        while len(line) > limit:
            flush()
            chunks.append(line[:limit])
            line = line[limit:]
        extra = len(line) + (1 if current else 0)
        if current_len + extra > limit:
            flush()
            extra = len(line)
        current.append(line)
        current_len += extra
    flush()
    return [chunk for chunk in chunks if chunk] or ([text] if text else [])


# --------------------------------------------------------------------------
# Shared pipeline
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _DictionaryStage:
    prepared: str
    to_chinese: bool
    outcome: TranslationOutcome | None


def _dictionary_stage(
    service: TermService,
    translator: SentenceTranslator,
    request: TranslationJob,
    input_limit: int,
) -> _DictionaryStage:
    """Everything before the LLM: prepare, direction, dictionary, size gate."""
    prepared = translator.prepare_text(request.text)
    if not prepared:
        return _DictionaryStage(
            "", False, TranslationOutcome(kind=KIND_NOOP, text=EMPTY_INPUT_MESSAGE)
        )
    # Direction is auto-detected from the visible text unless the caller
    # supplies an explicit target. Dictionary and LLM use the same direction.
    to_chinese = _resolve_to_chinese(prepared, request.forced_to_chinese)
    result = service.lookup_exact(prepared, limit=5)
    if result.exact and result.best:
        official = result.best.entry.zh if to_chinese else result.best.entry.en
        if official:
            return _DictionaryStage(
                prepared,
                to_chinese,
                TranslationOutcome(
                    kind=KIND_EXACT, text=official, to_chinese=to_chinese
                ),
            )
    fuzzy_answer = _fuzzy_dictionary_answer(service, prepared, to_chinese=to_chinese)
    if fuzzy_answer:
        return _DictionaryStage(
            prepared,
            to_chinese,
            TranslationOutcome(kind=KIND_FUZZY, text=fuzzy_answer, to_chinese=to_chinese),
        )
    if len(prepared) > input_limit:
        return _DictionaryStage(
            prepared,
            to_chinese,
            TranslationOutcome(
                kind=KIND_ERROR,
                text=input_too_long_message(input_limit),
                to_chinese=to_chinese,
                error_code=ERROR_INPUT_TOO_LONG,
            ),
        )
    return _DictionaryStage(prepared, to_chinese, None)


def _llm_failure_outcome(
    exc: LLMTranslationError, to_chinese: bool
) -> TranslationOutcome:
    try:
        raw_reason = getattr(exc, "reason", None)
    except Exception:
        raw_reason = None
    business_reason = _safe_business_reason(raw_reason)
    diagnostic = _safe_llm_failure_diagnostic(exc)
    reason = diagnostic.reason
    # The pipeline swallows the exception by contract (callers get an outcome).
    # Keep the safe diagnostic on the outcome so an HTTP adapter can correlate
    # it with its server-minted request id without exposing exception text.
    LOGGER.warning("llm translation failed reason=%s", reason)
    return TranslationOutcome(
        kind=KIND_ERROR,
        text=exc.user_message,
        to_chinese=to_chinese,
        # Diagnostics cannot override the original reason's business meaning.
        error_code=error_code_for_llm_reason(business_reason),
        llm_failure=diagnostic,
    )


async def _translate_long_plain_text(
    translator: SentenceTranslator,
    text: str,
    *,
    to_chinese: bool,
    splitter: TextSplitter,
    before_llm_call: LlmCallGuard | None = None,
) -> TranslationOutcome:
    translated_chunks: list[str] = []
    for chunk in splitter(text, LLM_INPUT_CHAR_LIMIT):
        try:
            translated = await translator.translate_async(
                chunk,
                to_chinese=to_chinese,
                propagate_errors=True,
                before_llm_call=before_llm_call,
            )
        except LLMTranslationError as exc:
            return _llm_failure_outcome(exc, to_chinese)
        translated_chunks.append(translated)
    return TranslationOutcome(
        kind=KIND_LLM, text="\n".join(translated_chunks), to_chinese=to_chinese
    )


async def _translate_plain_async(
    translator: SentenceTranslator,
    prepared: str,
    *,
    to_chinese: bool,
    before_llm_call: LlmCallGuard | None = None,
) -> TranslationOutcome:
    try:
        translated = await translator.translate_async(
            prepared,
            to_chinese=to_chinese,
            propagate_errors=True,
            before_llm_call=before_llm_call,
        )
    except LLMTranslationError as exc:
        return _llm_failure_outcome(exc, to_chinese)
    return TranslationOutcome(
        kind=KIND_LLM,
        text=translated,
        to_chinese=to_chinese,
        dictionary_miss=_should_append_dict_miss(prepared, translator, translated),
    )


async def translate_request_async(
    service: TermService,
    translator: SentenceTranslator,
    request: TranslationJob,
    *,
    input_limit: int = LLM_INPUT_CHAR_LIMIT,
    markup_translator: MarkupTranslator | None = None,
    splitter: TextSplitter | None = None,
    before_llm_call: LlmCallGuard | None = None,
    offload: Offload | None = None,
) -> TranslationOutcome:
    """Run the shared dictionary-first pipeline for one job."""
    if offload is None:
        stage = _dictionary_stage(service, translator, request, input_limit)
    else:
        stage = await offload(
            _dictionary_stage, service, translator, request, input_limit
        )
    if stage.outcome is not None:
        return stage.outcome
    prepared, to_chinese = stage.prepared, stage.to_chinese

    # Only trusted callers get an input_limit above LLM_INPUT_CHAR_LIMIT.
    # Split internally so every LLM call keeps the public per-call bound.
    if len(prepared) > LLM_INPUT_CHAR_LIMIT:
        return await _translate_long_plain_text(
            translator,
            prepared,
            to_chinese=to_chinese,
            splitter=splitter or split_plain_text,
            before_llm_call=before_llm_call,
        )

    if request.markup and markup_translator is not None:
        markup = await markup_translator(request.markup, to_chinese=to_chinese)
        if markup.text is not None:
            return TranslationOutcome(
                kind=KIND_LLM,
                text=markup.text,
                to_chinese=to_chinese,
                dictionary_miss=_should_append_dict_miss(
                    prepared, translator, markup.text
                ),
                markup_used=True,
            )
        if not markup.fallback_to_plain:
            # The outcome vocabulary must stay stable no matter which adapter
            # supplied the hook, so an unknown code from an injected translator
            # is normalized rather than published.
            code = markup.error_code
            if code not in TRANSLATION_ERROR_CODES:
                if code is not None:
                    LOGGER.warning(
                        "markup translator returned an unknown error code; "
                        "reporting %s instead",
                        ERROR_LLM_UNAVAILABLE,
                    )
                code = ERROR_LLM_UNAVAILABLE
            return TranslationOutcome(
                kind=KIND_ERROR,
                text=markup.message or "",
                to_chinese=to_chinese,
                error_code=code,
            )

    return await _translate_plain_async(
        translator,
        prepared,
        to_chinese=to_chinese,
        before_llm_call=before_llm_call,
    )


def translate_request(
    service: TermService,
    translator: SentenceTranslator,
    request: TranslationJob,
    *,
    input_limit: int = LLM_INPUT_CHAR_LIMIT,
) -> TranslationOutcome:
    """Synchronous variant of :func:`translate_request_async`.

    Plain text only, no markup and no long-text splitting: the sync LLM API
    cannot run inside a running event loop, so this exists for command-line
    and diagnostic callers.
    """
    stage = _dictionary_stage(service, translator, request, input_limit)
    if stage.outcome is not None:
        return stage.outcome
    prepared, to_chinese = stage.prepared, stage.to_chinese
    translated = translator.translate(prepared, to_chinese=to_chinese)
    if translated in LLM_FAILURE_NOTICES:
        # The sync API swallows the exception and returns a notice, so the
        # notice itself is the only signal left. Classify it the same way the
        # async path classifies the reason, or the published taxonomy would
        # disagree with itself depending on which entry point was used.
        return TranslationOutcome(
            kind=KIND_ERROR,
            text=translated,
            to_chinese=to_chinese,
            error_code=(
                ERROR_LLM_BUDGET_EXHAUSTED
                if translated == BUDGET_EXHAUSTED_NOTICE
                else ERROR_LLM_UNAVAILABLE
            ),
        )
    return TranslationOutcome(
        kind=KIND_LLM,
        text=translated,
        to_chinese=to_chinese,
        dictionary_miss=_should_append_dict_miss(prepared, translator, translated),
    )


# --------------------------------------------------------------------------
# Construction and read-only service entry points for adapters
# --------------------------------------------------------------------------


def build_term_service(db_path: str | Path) -> TermService:
    """Create the dictionary service for ``db_path``."""
    return TermService(db_path)


def build_translator(
    db_path: str | Path,
    *,
    timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    max_concurrency: int = DEFAULT_LLM_MAX_CONCURRENCY,
    en_zh_protocol: bool = False,
) -> SentenceTranslator:
    """Create a term-locking translator with adapter-owned LLM budgets.

    Each adapter builds its own translator, so the LLM concurrency limit is
    per process and per adapter — never a global budget.
    """
    return SentenceTranslator(
        db_path,
        llm_timeout_seconds=timeout,
        llm_max_concurrency=max_concurrency,
        en_zh_protocol=en_zh_protocol,
    )


def llm_configured() -> bool:
    """Whether LLM credentials are present in this process' environment."""
    return _llm_configured()


@dataclass(frozen=True)
class TermMatch:
    """One dictionary candidate in protocol-neutral form."""

    zh: str
    en: str
    category: str
    score: float
    reason: str


def _term_match(candidate: LookupCandidate) -> TermMatch:
    return TermMatch(
        zh=candidate.entry.zh,
        en=candidate.entry.en,
        category=candidate.entry.category,
        score=candidate.score,
        reason=candidate.reason,
    )


def lookup_exact_terms(
    service: TermService, query: str, *, limit: int = 5
) -> list[TermMatch]:
    """Exact dictionary candidates for ``query`` (official strings only)."""
    result = service.lookup_exact(query, limit=limit)
    return [_term_match(candidate) for candidate in result.candidates]


def lookup_terms(
    service: TermService, query: str, *, limit: int = 5
) -> list[TermMatch]:
    """Backend-ranked exact or fuzzy dictionary candidates for ``query``."""
    result = service.lookup(query, limit=limit)
    return [_term_match(candidate) for candidate in result.candidates]


@dataclass(frozen=True)
class ServiceMetadata:
    """Non-sensitive service facts safe to expose to any adapter surface.

    Deliberately excludes filesystem paths, credentials and chat identifiers.
    """

    schema_version: str | None
    source_profile: str | None
    source_commit: str | None
    source_game_version: str | None
    source_resource_version: str | None
    source_changelist: str | None
    term_count: int


def service_metadata(service: TermService) -> ServiceMetadata:
    metadata = service.metadata()
    return ServiceMetadata(
        schema_version=metadata.get("schema_version"),
        source_profile=metadata.get("source_profile"),
        source_commit=metadata.get("source_commit"),
        source_game_version=metadata.get("source_game_version"),
        source_resource_version=metadata.get("source_resource_version"),
        source_changelist=metadata.get("source_changelist"),
        term_count=service.term_count(),
    )


def probe_database(service: TermService) -> bool:
    """Readiness probe: can the dictionary be read right now?

    Returns ``False`` instead of raising so adapters can answer a health check
    without leaking the underlying error text.
    """
    try:
        service.term_count()
    except Exception:  # noqa: BLE001 - probe must not raise into a health route
        LOGGER.warning("dictionary readiness probe failed")
        return False
    return True


# --------------------------------------------------------------------------
# Pair review (dictionary constraints; no LLM)
# --------------------------------------------------------------------------

REVIEW_MAX_FINDINGS = 32
REVIEW_MAX_CANDIDATES = 8
REVIEW_MAX_SOURCES = 8
REVIEW_MAX_JSON_BYTES = 65536
REVIEW_MAX_SIDE_SCALARS = MAX_SIDE_SCALARS


def review_pair(
    service: TermService,
    source: str,
    target: str,
    direction: str,
    resolutions: Sequence[object] = (),
    *,
    review_version: str = "review-v1",
    alignments: object = None,
    resolution_context: object = None,
) -> ReviewReport:
    """Unique application entry for pair review. Adapters must not import review."""
    try:
        return _review_pair(
            service,
            source,
            target,
            direction,
            resolutions,
            review_version=review_version,
            alignments=alignments,
            resolution_context=resolution_context,
        )
    except ReviewRequestError:
        raise


def _review_span_to_wire(span: object) -> dict[str, object] | None:
    if span is None:
        return None
    return {"start": span.start, "end": span.end, "text": span.text}


def _review_finding_to_wire(finding: object) -> dict[str, object]:
    candidates_truncated = bool(finding.candidates_truncated)
    wire_candidates: list[dict[str, object]] = []
    if len(finding.candidates) > REVIEW_MAX_CANDIDATES:
        candidates_truncated = True
    for candidate in finding.candidates[:REVIEW_MAX_CANDIDATES]:
        sources = candidate.sources
        if len(sources) > REVIEW_MAX_SOURCES:
            candidates_truncated = True
            sources = sources[:REVIEW_MAX_SOURCES]
        wire_candidate = {
            "zh": candidate.zh,
            "en": candidate.en,
            "category": candidate.category,
            "sources": [
                {"source_file": source.source_file, "source_id": source.source_id}
                for source in sources
            ],
        }
        if candidate.candidate_id is not None:
            wire_candidate["candidate_id"] = candidate.candidate_id
        wire_candidates.append(wire_candidate)
    return {
        "id": finding.id,
        "verdict": finding.verdict,
        "rule_id": finding.rule_id,
        "source_span": _review_span_to_wire(finding.source_span),
        "target_span": _review_span_to_wire(finding.target_span),
        "candidates": wire_candidates,
        "candidates_truncated": candidates_truncated,
    }


def _review_json_bytes(body: dict[str, object]) -> bytes:
    return json.dumps(
        body, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")
    ).encode("utf-8")


def _trim_finding_payload(finding: dict[str, object]) -> bool:
    """Drop one candidate/source from the end. Does not shorten text fields."""
    candidates = finding["candidates"]
    if not isinstance(candidates, list) or not candidates:
        return False
    last = candidates[-1]
    sources = last.get("sources")
    if isinstance(sources, list) and sources:
        sources.pop()
        finding["candidates_truncated"] = True
        if not sources:
            candidates.pop()
        return True
    candidates.pop()
    finding["candidates_truncated"] = True
    return True


def project_review_report(report: ReviewReport, request_id: str) -> dict[str, object]:
    """Project a review report to the published HTTP exact-key document."""
    ordered = sorted(
        report.findings,
        key=lambda item: (item.source_span.start, -item.source_span.end, item.id),
    )
    truncated = bool(report.truncated)
    if len(ordered) > REVIEW_MAX_FINDINGS:
        ordered = ordered[:REVIEW_MAX_FINDINGS]
        truncated = True
    dictionary: dict[str, object] = {
        "schema_version": report.dictionary.schema_version,
        "source_commit": report.dictionary.source_commit,
        "term_count": report.dictionary.term_count,
    }
    if report.dictionary.revision is not None:
        dictionary["revision"] = report.dictionary.revision
    body: dict[str, object] = {
        "request_id": request_id,
        "source_revision": report.source_revision,
        "target_revision": report.target_revision,
        "rule_version": report.rule_version,
        "dictionary": dictionary,
        "coverage": {
            "evaluated": report.coverage.evaluated,
            "not_evaluated": report.coverage.not_evaluated,
            "rules": list(report.coverage.rules),
        },
        "findings": [_review_finding_to_wire(item) for item in ordered],
        "truncated": truncated,
    }
    while len(_review_json_bytes(body)) > REVIEW_MAX_JSON_BYTES:
        findings = body["findings"]
        if not isinstance(findings, list) or not findings:
            break
        if len(findings) > 1:
            findings.pop()
            body["truncated"] = True
            continue
        if _trim_finding_payload(findings[0]):
            continue
        findings.pop()
        body["truncated"] = True
    return body



# --------------------------------------------------------------------------
# Per-principal request budget
# --------------------------------------------------------------------------


LLM_BUDGET_EXHAUSTED_REASON = "budget"


class LlmCallBudget:
    """Rolling per-minute cap on LLM calls made by ONE process.

    Used as the ``before_llm_call`` guard: it is invoked synchronously right
    before each outbound LLM request, after the concurrency slot is held, so a
    refusal costs no upstream call. Like every other budget in this codebase it
    is process-local — two processes do not share it, and the documented total
    is the sum of the per-process caps.
    """

    def __init__(
        self,
        calls_per_minute: int,
        *,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if calls_per_minute < 1:
            raise ValueError("calls_per_minute must be at least 1")
        self.calls_per_minute = calls_per_minute
        self.window_seconds = window_seconds
        self._clock = clock
        self._calls: Deque[float] = deque()

    def __call__(self) -> None:
        now = self._clock()
        cutoff = now - self.window_seconds
        while self._calls and self._calls[0] <= cutoff:
            self._calls.popleft()
        if len(self._calls) >= self.calls_per_minute:
            raise LLMTranslationError(
                "llm call budget exhausted for this process",
                reason=LLM_BUDGET_EXHAUSTED_REASON,
            )
        self._calls.append(now)

    def in_window(self) -> int:
        """Calls counted in the current window (diagnostics only)."""
        cutoff = self._clock() - self.window_seconds
        return sum(1 for moment in self._calls if moment > cutoff)


class SlidingWindowRateLimiter:
    """In-process sliding-window limiter keyed by any hashable principal.

    Keys are chat ids for the Telegram adapter and device ids for the HTTP
    API. The window is process-local: two processes do not share a budget.
    """

    def __init__(self, limit: int = 10, window_seconds: float = 60.0):
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[Hashable, Deque[float]] = defaultdict(deque)

    def allow(self, key: Hashable, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        events = self._events[key]
        cutoff = now - self.window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True
