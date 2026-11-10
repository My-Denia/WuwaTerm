"""Deterministic wire contract for ``semantic-review-v1``.

This module owns everything about a semantic review response that does not
need a model: field limits, display excerpts, withholding a suggestion whose
executable range is too long, ordering, ids, and the byte budget. The engine
that calls the model hands it anchored issues and gets back the exact dict and
bytes the HTTP adapter serves. Nothing here imports LLM code; see ADR 0018.

Two rules shape the module:

* An executable range is never shortened. ``source_span``/``target_span`` and
  the suggestion's ``target_span`` always carry the complete anchored text.
  Shortening is only allowed in the display-only excerpts. A suggestion whose
  range or replacement is too long is removed as a whole and flagged with
  ``suggestion_withheld``.
* The byte budget is measured on the final serialized response, counters
  included, with the same encoding Starlette's ``JSONResponse`` uses. An issue
  that does not fit is dropped whole and counted in ``omitted_issues``.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# segmentation-v1's whitespace set, so the fingerprint normalization does not
# depend on a runtime's idea of whitespace.
from .segmentation import WHITESPACE as _WHITESPACE

REVIEW_VERSION = "semantic-review-v1"

CAPABILITY_SEMANTIC_REVIEW = "semantic-review-v1"
CAPABILITY_TRANSLATION_CONTEXT = "translation-context-v1"

CATEGORIES = (
    "omission",
    "unsupported_addition",
    "meaning_shift",
    "negation_change",
    "quantity_change",
    "reference_inconsistency",
    "term_sense",
)
# Ordered most to least confident; this order is also the sort order.
CONFIDENCES = ("high", "medium", "low")
TERM_EFFECTS = (
    "none",
    "alters_official_match",
    "introduces_official_form",
    "not_checked",
)
STATUS_COMPLETE = "complete"
STATUS_NOT_REVIEWED = "not_reviewed"
STATUS_REASON_INVALID_MODEL_OUTPUT = "invalid_model_output"
STATUS_REASONS = (STATUS_REASON_INVALID_MODEL_OUTPUT,)
TRUNCATED_ISSUE_LIMIT = "issue_limit"
TRUNCATED_BYTE_LIMIT = "byte_limit"
WITHHELD_SPAN_TOO_LONG = "span_too_long"
WITHHELD_REPLACEMENT_TOO_LONG = "replacement_too_long"
ORIGIN_MODEL = "model"

# Request limits (Unicode scalars unless noted).
MAX_SIDE_SCALARS = 2000
MAX_CONTEXT_SCALARS = 400
MAX_PRECEDENTS = 12
MAX_PRECEDENT_SURFACE_SCALARS = 32
MAX_PRECEDENT_RENDERING_SCALARS = 48
MAX_PRECEDENT_SNIPPET_SCALARS = 80
PRECEDENT_CHOICES = ("official_pair", "not_a_term", "user_rendering")
PRECEDENT_SCOPES = ("occurrence",)
EXPLAIN_LANGUAGES = ("zh", "en")
REQUEST_BYTE_LIMIT = 32 * 1024

# Response limits.
MAX_EXCERPT_SCALARS = 200
EXCERPT_HEAD_SCALARS = 95
EXCERPT_TAIL_SCALARS = 95
EXCERPT_JOINER = "…"
MAX_RATIONALE_SCALARS = 240
MAX_SUGGESTION_SPAN_SCALARS = 200
MAX_REPLACEMENT_SCALARS = 240
MAX_ALTERNATIVES = 1
MAX_ISSUES = 16
SEMANTIC_RESPONSE_BYTE_BUDGET = 57_344
ID_HEX_LENGTH = 32

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_POSITION_SENTINEL = MAX_SIDE_SCALARS + 1


class SemanticContractError(ValueError):
    """Input the contract refuses to serialize. A bug in the caller, not a user error."""


@dataclass(frozen=True)
class Span:
    """Half-open range ``[start, end)`` in Unicode scalars of one side."""

    start: int
    end: int


@dataclass(frozen=True)
class Alternative:
    replacement: str
    term_effect: str


@dataclass(frozen=True)
class Suggestion:
    target_span: Span
    replacement: str
    term_effect: str


@dataclass(frozen=True)
class AnchoredIssue:
    """One model issue after the server located both quotes in the pair."""

    category: str
    confidence: str
    source_span: Span | None
    target_span: Span | None
    rationale: str
    suggestion: Suggestion | None = None
    alternatives: tuple[Alternative, ...] = ()


@dataclass(frozen=True)
class FittedResponse:
    """A response that fits the byte budget, plus what the fit decided."""

    body: dict[str, Any]
    data: bytes
    kept: int
    total: int
    # Size of the response with one more issue, or None when no further issue
    # was a candidate. Lets a test prove the fit stopped at the right place.
    next_size: int | None


def serialize(body: Mapping[str, Any]) -> bytes:
    """Encode exactly as ``starlette.responses.JSONResponse.render`` does."""
    return json.dumps(
        body,
        ensure_ascii=False,
        allow_nan=False,
        indent=None,
        separators=(",", ":"),
    ).encode("utf-8")


def text_revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def excerpt(text: str) -> str:
    """Display-only excerpt: the text itself up to 200 scalars, else head…tail."""
    if len(text) <= MAX_EXCERPT_SCALARS:
        return text
    return text[:EXCERPT_HEAD_SCALARS] + EXCERPT_JOINER + text[-EXCERPT_TAIL_SCALARS:]


def normalize_quote(text: str) -> str:
    """Fingerprint normalization: NFKC, casefold, collapse whitespace runs."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    out: list[str] = []
    in_space = False
    for char in folded:
        if char in _WHITESPACE:
            in_space = True
            continue
        if in_space and out:
            out.append(" ")
        in_space = False
        out.append(char)
    return "".join(out)


def _digest(parts: list[Any]) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:ID_HEX_LENGTH]


def issue_id(
    category: str,
    source_revision: str,
    target_revision: str,
    source_span: Span | None,
    target_span: Span | None,
) -> str:
    """Bound to this occurrence and this text version."""
    return _digest(
        [
            category,
            source_revision,
            target_revision,
            None if source_span is None else [source_span.start, source_span.end],
            None if target_span is None else [target_span.start, target_span.end],
        ]
    )


def issue_fingerprint(category: str, source_quote: str | None, target_quote: str | None) -> str:
    """Only for finding candidates across revisions; never a decision key."""
    return _digest(
        [
            category,
            None if source_quote is None else normalize_quote(source_quote),
            None if target_quote is None else normalize_quote(target_quote),
        ]
    )


def _has_isolated_surrogate(text: str) -> bool:
    return any("\ud800" <= char <= "\udfff" for char in text)


def _check_text(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise SemanticContractError(f"{name} must be a string")
    if _has_isolated_surrogate(value):
        raise SemanticContractError(f"{name} contains an isolated surrogate")
    return value


def _check_span(name: str, span: object, side: str) -> Span:
    if not isinstance(span, Span):
        raise SemanticContractError(f"{name} must be a Span")
    if type(span.start) is not int or type(span.end) is not int:
        raise SemanticContractError(f"{name} offsets must be integers")
    if not 0 <= span.start < span.end <= len(side):
        raise SemanticContractError(f"{name} is out of bounds or empty")
    return span


def _check_choice(name: str, value: object, allowed: Sequence[str]) -> str:
    if value not in allowed:
        raise SemanticContractError(f"{name} is not one of {', '.join(allowed)}")
    return value  # type: ignore[return-value]


def _span_body(span: Span, side: str) -> dict[str, Any]:
    return {"start": span.start, "end": span.end, "text": side[span.start : span.end]}


def _rationale(text: str) -> str:
    if len(text) <= MAX_RATIONALE_SCALARS:
        return text
    return text[: MAX_RATIONALE_SCALARS - 1] + EXCERPT_JOINER


def _project_issue(
    issue: AnchoredIssue,
    source: str,
    target: str,
    source_revision: str,
    target_revision: str,
) -> dict[str, Any]:
    if not isinstance(issue, AnchoredIssue):
        raise SemanticContractError("issues must be AnchoredIssue values")
    category = _check_choice("category", issue.category, CATEGORIES)
    confidence = _check_choice("confidence", issue.confidence, CONFIDENCES)
    source_span = (
        None
        if issue.source_span is None
        else _check_span("source_span", issue.source_span, source)
    )
    target_span = (
        None
        if issue.target_span is None
        else _check_span("target_span", issue.target_span, target)
    )
    if source_span is None and target_span is None:
        raise SemanticContractError("an issue needs at least one span")
    rationale = _rationale(_check_text("rationale", issue.rationale))

    suggestion_body: dict[str, Any] | None = None
    withheld: str | None = None
    alternatives: list[dict[str, Any]] = []
    if issue.suggestion is not None:
        suggestion = issue.suggestion
        if not isinstance(suggestion, Suggestion):
            raise SemanticContractError("suggestion must be a Suggestion")
        span = _check_span("suggestion.target_span", suggestion.target_span, target)
        replacement = _check_text("suggestion.replacement", suggestion.replacement)
        term_effect = _check_choice(
            "suggestion.term_effect", suggestion.term_effect, TERM_EFFECTS
        )
        # The executable range, its text and the replacement are one unit: it
        # is kept whole or removed whole, never shortened.
        if span.end - span.start > MAX_SUGGESTION_SPAN_SCALARS:
            withheld = WITHHELD_SPAN_TOO_LONG
        elif len(replacement) > MAX_REPLACEMENT_SCALARS:
            withheld = WITHHELD_REPLACEMENT_TOO_LONG
        else:
            suggestion_body = {
                "target_span": _span_body(span, target),
                "replacement": replacement,
                "term_effect": term_effect,
            }
            for alternative in issue.alternatives:
                if not isinstance(alternative, Alternative):
                    raise SemanticContractError("alternatives must be Alternative values")
                alt_replacement = _check_text(
                    "alternative.replacement", alternative.replacement
                )
                alt_effect = _check_choice(
                    "alternative.term_effect", alternative.term_effect, TERM_EFFECTS
                )
                if len(alt_replacement) > MAX_REPLACEMENT_SCALARS:
                    continue
                alternatives.append(
                    {"replacement": alt_replacement, "term_effect": alt_effect}
                )
                if len(alternatives) == MAX_ALTERNATIVES:
                    break
    elif issue.alternatives:
        raise SemanticContractError("alternatives require a suggestion")

    source_quote = None if source_span is None else source[source_span.start : source_span.end]
    target_quote = None if target_span is None else target[target_span.start : target_span.end]
    return {
        "id": issue_id(category, source_revision, target_revision, source_span, target_span),
        "fingerprint": issue_fingerprint(category, source_quote, target_quote),
        "category": category,
        "origin": ORIGIN_MODEL,
        "confidence": confidence,
        "source_span": None if source_span is None else _span_body(source_span, source),
        "target_span": None if target_span is None else _span_body(target_span, target),
        "source_excerpt": None if source_quote is None else excerpt(source_quote),
        "target_excerpt": None if target_quote is None else excerpt(target_quote),
        "rationale": rationale,
        "suggestion": suggestion_body,
        "suggestion_withheld": withheld,
        "alternatives": alternatives,
    }


def _sort_key(body: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
    source_span = body["source_span"]
    target_span = body["target_span"]
    return (
        CONFIDENCES.index(body["confidence"]),
        _POSITION_SENTINEL if source_span is None else source_span["start"],
        _POSITION_SENTINEL if target_span is None else target_span["start"],
        CATEGORIES.index(body["category"]),
        body["id"],
    )


def _check_side(name: str, value: object) -> str:
    text = _check_text(name, value)
    if len(text) > MAX_SIDE_SCALARS:
        raise SemanticContractError(f"{name} exceeds {MAX_SIDE_SCALARS} scalars")
    return text


def _check_term_basis(term_basis: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(term_basis, Mapping) or set(term_basis) != {
        "dictionary_revision",
        "matcher_revision",
    }:
        raise SemanticContractError(
            "term_basis needs exactly dictionary_revision and matcher_revision"
        )
    out: dict[str, str] = {}
    for key in ("dictionary_revision", "matcher_revision"):
        value = term_basis[key]
        if not isinstance(value, str) or not _HEX64.match(value):
            raise SemanticContractError(f"term_basis.{key} must be 64 lowercase hex")
        out[key] = value
    return out


def _envelope(
    *,
    request_id: str,
    source_revision: str,
    target_revision: str,
    term_basis: dict[str, str],
    status: str,
    status_reason: str | None,
    dropped_unanchored: int,
    checked: list[str],
    issues: list[dict[str, Any]],
    omitted_issues: int,
    truncated_reason: str | None,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "review_version": REVIEW_VERSION,
        "source_revision": source_revision,
        "target_revision": target_revision,
        "term_basis": dict(term_basis),
        "status": status,
        "status_reason": status_reason,
        "dropped_unanchored": dropped_unanchored,
        "checked": list(checked),
        "issues": issues,
        "truncated": omitted_issues > 0,
        "truncated_reason": truncated_reason,
        "omitted_issues": omitted_issues,
    }


def _check_count(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise SemanticContractError(f"{name} must be a non-negative integer")
    return value


def build_response(
    *,
    request_id: str,
    source: str,
    target: str,
    term_basis: Mapping[str, Any],
    issues: Sequence[AnchoredIssue],
    dropped_unanchored: int = 0,
    byte_budget: int = SEMANTIC_RESPONSE_BYTE_BUDGET,
) -> FittedResponse:
    """Project anchored issues into a ``complete`` response within the budget.

    Order of operations, each one specified in ADR 0018:

    1. validate and project every issue (field limits, excerpts, withheld
       suggestions);
    2. sort by confidence, then source start, then target start, then category,
       then id (stable), and keep only the first issue of each ``id``;
    3. keep at most 16;
    4. add issues one at a time, rendering the whole response with its final
       counters each time, and stop before the first one that would push the
       serialized size over the budget.

    ``truncated_reason`` is ``byte_limit`` when the byte budget dropped at least
    one of the first 16, otherwise ``issue_limit`` when there were more than 16.
    """
    request_id = _check_text("request_id", request_id)
    source = _check_side("source", source)
    target = _check_side("target", target)
    basis = _check_term_basis(term_basis)
    dropped = _check_count("dropped_unanchored", dropped_unanchored)
    source_revision = text_revision(source)
    target_revision = text_revision(target)

    ordered = sorted(
        (
            _project_issue(issue, source, target, source_revision, target_revision)
            for issue in issues
        ),
        key=_sort_key,
    )
    # Same id means same category and same spans: one finding. The sort runs
    # first, so the most confident copy is the one kept.
    projected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for body in ordered:
        if body["id"] not in seen:
            seen.add(body["id"])
            projected.append(body)
    total = len(projected)
    candidates = projected[:MAX_ISSUES]

    def compose(kept: int) -> dict[str, Any]:
        omitted = total - kept
        if omitted == 0:
            reason = None
        elif kept < len(candidates):
            reason = TRUNCATED_BYTE_LIMIT
        else:
            reason = TRUNCATED_ISSUE_LIMIT
        return _envelope(
            request_id=request_id,
            source_revision=source_revision,
            target_revision=target_revision,
            term_basis=basis,
            status=STATUS_COMPLETE,
            status_reason=None,
            dropped_unanchored=dropped,
            checked=list(CATEGORIES),
            issues=candidates[:kept],
            omitted_issues=omitted,
            truncated_reason=reason,
        )

    kept = 0
    body = compose(0)
    data = serialize(body)
    if len(data) > byte_budget:
        raise SemanticContractError("an empty response does not fit the byte budget")
    next_size: int | None = None
    while kept < len(candidates):
        trial = compose(kept + 1)
        trial_data = serialize(trial)
        if len(trial_data) > byte_budget:
            next_size = len(trial_data)
            break
        kept += 1
        body, data = trial, trial_data
    return FittedResponse(body=body, data=data, kept=kept, total=total, next_size=next_size)


def build_not_reviewed(
    *,
    request_id: str,
    source: str,
    target: str,
    term_basis: Mapping[str, Any],
    status_reason: str = STATUS_REASON_INVALID_MODEL_OUTPUT,
) -> FittedResponse:
    """The model answered, but not with output the contract can use.

    Nothing was checked, so ``checked`` is empty and there are no issues. The
    client shows the segment as not reviewed; it is not an all-clear.
    """
    request_id = _check_text("request_id", request_id)
    source = _check_side("source", source)
    target = _check_side("target", target)
    basis = _check_term_basis(term_basis)
    reason = _check_choice("status_reason", status_reason, STATUS_REASONS)
    body = _envelope(
        request_id=request_id,
        source_revision=text_revision(source),
        target_revision=text_revision(target),
        term_basis=basis,
        status=STATUS_NOT_REVIEWED,
        status_reason=reason,
        dropped_unanchored=0,
        checked=[],
        issues=[],
        omitted_issues=0,
        truncated_reason=None,
    )
    return FittedResponse(body=body, data=serialize(body), kept=0, total=0, next_size=None)
