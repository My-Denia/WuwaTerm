"""Dictionary-backed pair review on submitted Unicode scalar coordinates.

This module is domain core. It reads the terminology database through
``TermService`` and must not import ``sentence`` or construct a translator.
Offsets are counted on the submitted source/target strings (Unicode scalars,
half-open), never on ``prepare_text`` output.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .cjk_span import MATCHER_REVISION, crosses_cjk_word_boundary
from .constants import CATEGORY_ORDER
from .lookup import TermService
from .models import TermEntry

RULE_VERSION = "review-v1"
RULE_VERSION_V2 = "review-v2"
RULE_TERM_PAIR = "review.term_pair"
RULE_SENTENCE_MEANING = "review.sentence_meaning"
MAX_SIDE_SCALARS = 2000
MAX_ALIGNMENTS = 64
CHOICE_OFFICIAL_PAIR = "official_pair"
CHOICE_NOT_A_TERM = "not_a_term"
VERDICT_VERIFIED = "verified_constraint"
VERDICT_CONFLICT = "confirmed_conflict"
VERDICT_NEEDS_REVIEW = "needs_review"
VERDICT_NOT_EVALUATED = "not_evaluated"
_SURROGATE_MIN = "\ud800"
_SURROGATE_MAX = "\udfff"
_SENTENCE_END = re.compile(r"[。．.！？!?\n]")


class ReviewRequestError(ValueError):
    """Input the review engine refuses. ``code`` is an application error code."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


@dataclass(frozen=True)
class ReviewResolution:
    mention_id: str
    choice: str
    zh: str | None = None
    en: str | None = None
    candidate_id: str | None = None


@dataclass(frozen=True)
class ReviewSpan:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class ReviewSource:
    source_file: str
    source_id: str


@dataclass(frozen=True)
class ReviewCandidate:
    zh: str
    en: str
    category: str
    sources: tuple[ReviewSource, ...]
    candidate_id: str | None = None


@dataclass(frozen=True)
class ReviewFinding:
    id: str
    verdict: str
    rule_id: str
    source_span: ReviewSpan
    target_span: ReviewSpan | None
    candidates: tuple[ReviewCandidate, ...]
    candidates_truncated: bool = False


@dataclass(frozen=True)
class ReviewDictionary:
    schema_version: str | None
    source_commit: str | None
    term_count: int
    revision: str | None = None


@dataclass(frozen=True)
class ReviewCoverage:
    evaluated: int
    not_evaluated: int
    rules: tuple[str, ...]


@dataclass(frozen=True)
class ReviewReport:
    source_revision: str
    target_revision: str
    rule_version: str
    dictionary: ReviewDictionary
    coverage: ReviewCoverage
    findings: tuple[ReviewFinding, ...]
    truncated: bool = False
    matcher_revision: str | None = None


@dataclass(frozen=True)
class _TermSpan:
    start: int
    end: int
    source: str
    order: int


@dataclass(frozen=True)
class ReviewAlignment:
    source: ReviewSpan
    target: ReviewSpan | None


@dataclass(frozen=True)
class ReviewResolutionContext:
    source_revision: str
    rule_version: str
    dictionary_revision: str
    matcher_revision: str | None = None


def mention_id(start: int, end: int, text: str) -> str:
    return f"{start}:{end}:{text}"


def text_revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _has_isolated_surrogate(text: str) -> bool:
    return any(_SURROGATE_MIN <= char <= _SURROGATE_MAX for char in text)


def _is_ascii_word_char(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _ascii_word_boundaries_ok(text: str, start: int, end: int, source: str) -> bool:
    if not source:
        return False
    if (
        _is_ascii_word_char(source[0])
        and start > 0
        and _is_ascii_word_char(text[start - 1])
    ):
        return False
    if (
        _is_ascii_word_char(source[-1])
        and end < len(text)
        and _is_ascii_word_char(text[end])
    ):
        return False
    return True


def _source_priority(entry: TermEntry) -> int:
    if entry.source_id.startswith("OccupationConfig_") and entry.source_id.endswith(
        "_Name"
    ):
        return 0
    if "RoleInfo" in entry.source_file:
        return 1
    if entry.category == "speaker":
        return 8
    return 5


def _validate_side(name: str, text: object) -> str:
    if not isinstance(text, str) or text == "":
        raise ReviewRequestError(
            "invalid_request", f"{name} must be a non-empty string"
        )
    if _has_isolated_surrogate(text):
        raise ReviewRequestError(
            "invalid_request", f"{name} contains an isolated surrogate"
        )
    if len(text) > MAX_SIDE_SCALARS:
        raise ReviewRequestError(
            "input_too_long", f"{name} exceeds {MAX_SIDE_SCALARS} Unicode scalars"
        )
    return text


def _validate_direction(direction: object) -> str:
    if direction not in {"en", "zh"}:
        raise ReviewRequestError("invalid_request", "direction must be en or zh")
    return direction


def _coerce_resolution(item: object) -> ReviewResolution:
    if isinstance(item, ReviewResolution):
        resolution = item
    elif isinstance(item, dict):
        mention_id_value = item.get("mention_id")
        choice = item.get("choice")
        if not isinstance(mention_id_value, str) or not mention_id_value:
            raise ReviewRequestError(
                "invalid_request", "resolution mention_id is required"
            )
        if choice not in {CHOICE_OFFICIAL_PAIR, CHOICE_NOT_A_TERM}:
            raise ReviewRequestError(
                "invalid_request", "resolution choice is not valid"
            )
        allowed = (
            {"mention_id", "choice", "zh", "en"}
            if choice == CHOICE_OFFICIAL_PAIR
            else {"mention_id", "choice"}
        )
        if set(item) != allowed:
            raise ReviewRequestError("invalid_request", "resolution keys are not valid")
        zh = item.get("zh") if choice == CHOICE_OFFICIAL_PAIR else None
        en = item.get("en") if choice == CHOICE_OFFICIAL_PAIR else None
        if choice == CHOICE_OFFICIAL_PAIR and (
            not isinstance(zh, str) or not zh or not isinstance(en, str) or not en
        ):
            raise ReviewRequestError(
                "invalid_request", "official_pair requires zh and en"
            )
        resolution = ReviewResolution(
            mention_id=mention_id_value, choice=choice, zh=zh, en=en
        )
    else:
        raise ReviewRequestError("invalid_request", "resolution is not valid")
    if resolution.choice == CHOICE_OFFICIAL_PAIR and (
        not resolution.zh or not resolution.en
    ):
        raise ReviewRequestError("invalid_request", "official_pair requires zh and en")
    if resolution.choice not in {CHOICE_OFFICIAL_PAIR, CHOICE_NOT_A_TERM}:
        raise ReviewRequestError("invalid_request", "resolution choice is not valid")
    if not resolution.mention_id:
        raise ReviewRequestError("invalid_request", "resolution mention_id is required")
    return resolution


def _coerce_resolution_v2(item: object) -> ReviewResolution:
    if not isinstance(item, dict):
        raise ReviewRequestError("invalid_request", "resolution is not valid")
    mention_id_value = item.get("mention_id")
    choice = item.get("choice")
    if not isinstance(mention_id_value, str) or not mention_id_value:
        raise ReviewRequestError("invalid_request", "resolution mention_id is required")
    if choice not in {CHOICE_OFFICIAL_PAIR, CHOICE_NOT_A_TERM}:
        raise ReviewRequestError("invalid_request", "resolution choice is not valid")
    allowed = (
        {"mention_id", "choice", "candidate_id"}
        if choice == CHOICE_OFFICIAL_PAIR
        else {"mention_id", "choice"}
    )
    if set(item) != allowed:
        raise ReviewRequestError("invalid_request", "resolution keys are not valid")
    candidate_id = item.get("candidate_id")
    if choice == CHOICE_OFFICIAL_PAIR and (
        not isinstance(candidate_id, str) or not candidate_id
    ):
        raise ReviewRequestError(
            "invalid_request", "official_pair requires candidate_id"
        )
    return ReviewResolution(
        mention_id=mention_id_value,
        choice=choice,
        candidate_id=candidate_id if choice == CHOICE_OFFICIAL_PAIR else None,
    )


def _coerce_resolution_context(item: object) -> ReviewResolutionContext:
    base = {"source_revision", "rule_version", "dictionary_revision"}
    allowed = (base, base | {"matcher_revision"})
    if not isinstance(item, dict) or set(item) not in allowed:
        raise ReviewRequestError("invalid_request", "resolution_context is not valid")
    values = tuple(
        item[key]
        for key in (
            "source_revision",
            "rule_version",
            "dictionary_revision",
        )
    )
    matcher = item.get("matcher_revision")
    if "matcher_revision" in item and (not isinstance(matcher, str) or not matcher):
        raise ReviewRequestError("invalid_request", "resolution_context is not valid")
    if any(not isinstance(value, str) or not value for value in values):
        raise ReviewRequestError("invalid_request", "resolution_context is not valid")
    return ReviewResolutionContext(
        *values,
        matcher_revision=matcher if isinstance(matcher, str) else None,
    )


def _dictionary_snapshot(service: TermService) -> ReviewDictionary:
    metadata = service.metadata()
    return ReviewDictionary(
        schema_version=metadata.get("schema_version"),
        source_commit=metadata.get("source_commit"),
        term_count=service.term_count(),
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _dictionary_revision(entries: Sequence[TermEntry]) -> str:
    rows = sorted(
        (
            entry.category,
            entry.source_file,
            entry.source_id,
            entry.text_key,
            entry.zh,
            entry.en,
            entry.pinyin,
            entry.pinyin_abbrev,
        )
        for entry in entries
    )
    return hashlib.sha256(
        _canonical_json(("wuwaterm-review-dictionary-v1", rows))
    ).hexdigest()


def _candidate_id(
    zh: str,
    en: str,
    category: str,
    sources: Sequence[ReviewSource],
) -> str:
    provenance = sorted((source.source_file, source.source_id) for source in sources)
    return hashlib.sha256(
        _canonical_json(("wuwaterm-review-candidate-v1", zh, en, category, provenance))
    ).hexdigest()


def _surface_index(
    entries: Sequence[TermEntry],
) -> dict[str, list[TermEntry]]:
    surfaces: dict[str, list[TermEntry]] = {}
    for entry in entries:
        if "\n" in entry.zh or "\n" in entry.en:
            continue
        if entry.zh:
            surfaces.setdefault(entry.zh, []).append(entry)
        if entry.en and entry.en != entry.zh:
            surfaces.setdefault(entry.en, []).append(entry)
    return surfaces


def _is_cjk_character(char: str) -> bool:
    # Same range as sentence._is_cjk_character. This module must not import
    # sentence; the review tests pin the shared one-character results.
    return "\u3400" <= char <= "\u9fff"


def _single_character_name_context_ok(text: str, start: int, end: int) -> bool:
    """Admit bounded Chinese name mentions, avoiding compounds like 香椿/椿树.

    Verbatim copy of sentence._single_character_name_context_ok. Only
    one-character resonators use it. Unlisted contexts (for example 欢迎椿)
    stay ineligible. This function does not decide multi-character spans.
    """
    if start and _is_cjk_character(text[start - 1]):
        if text[start - 1] not in "和与让给由是用有":
            return False
    return (
        end == len(text)
        or not _is_cjk_character(text[end])
        or text.startswith(("的", "和", "与", "在", "是", "也", "会", "要", "能",
                            "加入", "离开", "登场", "使用", "说"), end)
    )


def _v2_one_character_eligible(
    text: str,
    start: int,
    end: int,
    surface: str,
    entries: Sequence[TermEntry],
) -> bool:
    """Match sentence locking: ordinary one-character surfaces are not terms.

    The winning entry is the lowest CATEGORY_ORDER record for this surface,
    which is the record entries() yields first. A one-character span stays
    only when that record is the surface's own Chinese resonator name and
    the surrounding characters look like a name mention.
    """
    if len(surface) != 1:
        return True
    winner = min(
        entries, key=lambda entry: CATEGORY_ORDER.get(entry.category, 999)
    )
    return (
        surface == winner.zh
        and winner.category == "resonator"
        and _is_cjk_character(surface)
        and _single_character_name_context_ok(text, start, end)
    )


def _collect_mentions(
    source: str,
    surfaces: dict[str, list[TermEntry]],
    *,
    eligible: object = None,
) -> list[_TermSpan]:
    spans: list[_TermSpan] = []
    for order, surface in enumerate(surfaces):
        start = source.find(surface)
        while start != -1:
            end = start + len(surface)
            # eligible is set only by review-v2. The default leaves the
            # frozen v1 span set unchanged.
            allowed = _ascii_word_boundaries_ok(source, start, end, surface)
            if allowed and eligible is not None:
                allowed = eligible(start, end, surface, surfaces[surface])
            if allowed:
                spans.append(
                    _TermSpan(start=start, end=end, source=surface, order=order)
                )
            start = source.find(surface, start + 1)
    selected: list[_TermSpan] = []
    occupied: list[tuple[int, int]] = []
    for span in sorted(
        spans,
        key=lambda item: (
            -(item.end - item.start),
            item.order,
            item.start,
            item.source,
        ),
    ):
        if any(span.start < end and start < span.end for start, end in occupied):
            continue
        selected.append(span)
        occupied.append((span.start, span.end))
    selected.sort(key=lambda item: (item.start, -item.end, item.source))
    return selected


def _candidates_for(
    entries: Sequence[TermEntry], *, include_id: bool = False
) -> tuple[ReviewCandidate, ...]:
    grouped: dict[tuple[str, str, str], list[ReviewSource]] = {}
    first_entry: dict[tuple[str, str, str], TermEntry] = {}
    for entry in entries:
        key = (entry.zh, entry.en, entry.category)
        first_entry.setdefault(key, entry)
        sources = grouped.setdefault(key, [])
        source = ReviewSource(source_file=entry.source_file, source_id=entry.source_id)
        if source not in sources:
            sources.append(source)
    candidates = []
    for zh, en, category in grouped:
        sources = tuple(grouped[(zh, en, category)])
        candidates.append(
            ReviewCandidate(
                zh=zh,
                en=en,
                category=category,
                sources=sources,
                candidate_id=(
                    _candidate_id(zh, en, category, sources) if include_id else None
                ),
            )
        )
    candidates.sort(
        key=lambda candidate: (
            CATEGORY_ORDER.get(candidate.category, 999),
            _source_priority(
                first_entry[(candidate.zh, candidate.en, candidate.category)]
            ),
            len(candidate.zh),
            candidate.en,
            candidate.zh,
        )
    )
    return tuple(candidates)


def _separator_only(text: str, start: int, end: int) -> bool:
    return start < end and all(
        _SENTENCE_END.match(char) or char.isspace() for char in text[start:end]
    )


def _sentence_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        if _SENTENCE_END.match(text[index]):
            end = index + 1
            while end < length and _SENTENCE_END.match(text[end]):
                end += 1
            if start < end and not _separator_only(text, start, end):
                ranges.append((start, end))
            start = end
            index = end
            continue
        index += 1
    if start < length and not _separator_only(text, start, length):
        ranges.append((start, length))
    if not ranges and text and not _separator_only(text, 0, length):
        ranges.append((0, length))
    return ranges


def _is_sentence_end_v2(text: str, index: int) -> bool:
    char = text[index]
    if not _SENTENCE_END.match(char):
        return False
    if (
        char in {".", "．"}
        and index > 0
        and index + 1 < len(text)
        and text[index - 1].isdigit()
        and text[index + 1].isdigit()
    ):
        return False
    return True


def _sentence_ranges_v2(text: str) -> list[tuple[int, int]]:
    """V2 sentence ranges, keeping decimal punctuation inside a sentence."""
    ranges: list[tuple[int, int]] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        if _is_sentence_end_v2(text, index):
            end = index + 1
            while end < length and _is_sentence_end_v2(text, end):
                end += 1
            if start < end and not _separator_only(text, start, end):
                ranges.append((start, end))
            start = end
            index = end
            continue
        index += 1
    if start < length and not _separator_only(text, start, length):
        ranges.append((start, length))
    if not ranges and text and not _separator_only(text, 0, length):
        ranges.append((0, length))
    return ranges


def _coerce_alignment_span(name: str, item: object, text: str) -> ReviewSpan:
    if not isinstance(item, dict) or set(item) != {"start", "end", "text"}:
        raise ReviewRequestError("invalid_request", f"{name} span is not valid")
    start = item.get("start")
    end = item.get("end")
    span_text = item.get("text")
    if type(start) is not int or type(end) is not int:  # bool is not an offset
        raise ReviewRequestError("invalid_request", f"{name} offsets must be integers")
    if start < 0 or start >= end or end > len(text):
        raise ReviewRequestError("invalid_request", f"{name} span is out of bounds")
    if not isinstance(span_text, str) or text[start:end] != span_text:
        raise ReviewRequestError("invalid_request", f"{name} span text is stale")
    return ReviewSpan(start=start, end=end, text=span_text)


def _coerce_alignments(
    items: object,
    source: str,
    target: str,
    mentions: Sequence[_TermSpan],
) -> tuple[ReviewAlignment, ...]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        raise ReviewRequestError("invalid_request", "alignments must be an array")
    if len(items) > MAX_ALIGNMENTS:
        raise ReviewRequestError(
            "invalid_request", f"alignments exceeds {MAX_ALIGNMENTS} entries"
        )
    alignments: list[ReviewAlignment] = []
    previous_source_end = -1
    previous_target_end = -1
    for item in items:
        if not isinstance(item, dict) or set(item) != {"source", "target"}:
            raise ReviewRequestError("invalid_request", "alignment keys are not valid")
        source_span = _coerce_alignment_span("source", item["source"], source)
        target_value = item["target"]
        target_span = (
            None
            if target_value is None
            else _coerce_alignment_span("target", target_value, target)
        )
        if source_span.start < previous_source_end:
            raise ReviewRequestError(
                "invalid_request", "alignment source spans overlap or are unordered"
            )
        if target_span is not None and target_span.start < previous_target_end:
            raise ReviewRequestError(
                "invalid_request", "alignment target spans overlap or are unordered"
            )
        for mention in mentions:
            if (
                mention.start < source_span.start < mention.end
                or mention.start < source_span.end < mention.end
            ):
                raise ReviewRequestError(
                    "invalid_request", "alignment source boundary cuts a term mention"
                )
        previous_source_end = source_span.end
        if target_span is not None:
            previous_target_end = target_span.end
        alignments.append(ReviewAlignment(source=source_span, target=target_span))
    return tuple(alignments)


def _mapped_target_range(
    mention: _TermSpan,
    alignments: Sequence[ReviewAlignment],
) -> tuple[int, int] | None:
    for alignment in alignments:
        if (
            alignment.source.start <= mention.start
            and mention.end <= alignment.source.end
        ):
            if alignment.target is None:
                return None
            return (alignment.target.start, alignment.target.end)
    return None


def _sentence_index(ranges: Sequence[tuple[int, int]], start: int) -> int | None:
    for index, (lo, hi) in enumerate(ranges):
        if lo <= start < hi:
            return index
    if ranges and start == ranges[-1][1]:
        return len(ranges) - 1
    return None


def _find_occurrences(text: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    found: list[tuple[int, int]] = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index == -1:
            break
        end = index + len(needle)
        if _ascii_word_boundaries_ok(text, index, end, needle):
            found.append((index, end))
        start = index + 1
    return found


def _find_occurrences_v2(text: str, needle: str) -> list[tuple[int, int]]:
    """Review-v2 target search: a cross-word CJK substring is not an occurrence."""
    found: list[tuple[int, int]] = []
    for start, end in _find_occurrences(text, needle):
        if not crosses_cjk_word_boundary(text, start, end):
            found.append((start, end))
    return found


def _official_form(candidate: ReviewCandidate, direction: str) -> str:
    return candidate.en if direction == "en" else candidate.zh


def _span_at(text: str, start: int, end: int) -> ReviewSpan:
    return ReviewSpan(start=start, end=end, text=text[start:end])


def _elsewhere_has_form(
    target: str,
    sentence: tuple[int, int],
    forms: Sequence[str],
    *,
    occurrences: Callable[[str, str], list[tuple[int, int]]] = _find_occurrences,
) -> bool:
    lo, hi = sentence
    for form in forms:
        for start, end in occurrences(target, form):
            if start < lo or end > hi:
                return True
    return False


def _spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _span_available(span: tuple[int, int], used: set[tuple[int, int]]) -> bool:
    return all(not _spans_overlap(span, taken) for taken in used)


def _next_unused(
    occurrences: Sequence[tuple[int, int]], used: set[tuple[int, int]]
) -> tuple[int, int] | None:
    for span in occurrences:
        if _span_available(span, used):
            return span
    return None


def _judge_mention(
    *,
    source: str,
    target: str,
    direction: str,
    mention: _TermSpan,
    candidates: tuple[ReviewCandidate, ...],
    source_sentences: Sequence[tuple[int, int]],
    target_sentences: Sequence[tuple[int, int]],
    used_target: set[tuple[int, int]],
    resolution: ReviewResolution | None,
) -> ReviewFinding:
    finding_id = mention_id(mention.start, mention.end, mention.source)
    source_span = _span_at(source, mention.start, mention.end)
    source_index = _sentence_index(source_sentences, mention.start)
    corresponding: tuple[int, int] | None = None
    if (
        source_index is not None
        and target_sentences
        and source_index < len(target_sentences)
    ):
        corresponding = target_sentences[source_index]

    if resolution is not None and resolution.choice == CHOICE_NOT_A_TERM:
        return ReviewFinding(
            id=finding_id,
            verdict=VERDICT_NOT_EVALUATED,
            rule_id=RULE_TERM_PAIR,
            source_span=source_span,
            target_span=None,
            candidates=candidates,
        )

    selected: ReviewCandidate | None = None
    if resolution is not None and resolution.choice == CHOICE_OFFICIAL_PAIR:
        for candidate in candidates:
            if candidate.zh == resolution.zh and candidate.en == resolution.en:
                selected = candidate
                break
        if selected is None:
            raise ReviewRequestError(
                "invalid_request",
                "official_pair is not a candidate of this mention",
            )

    forms = (
        (_official_form(selected, direction),)
        if selected is not None
        else tuple(
            dict.fromkeys(
                _official_form(candidate, direction) for candidate in candidates
            )
        )
    )

    if corresponding is None:
        return ReviewFinding(
            id=finding_id,
            verdict=VERDICT_NOT_EVALUATED,
            rule_id=RULE_TERM_PAIR,
            source_span=source_span,
            target_span=None,
            candidates=candidates,
        )

    lo, hi = corresponding
    region = target[lo:hi]
    in_region: dict[str, list[tuple[int, int]]] = {}
    appearing: list[str] = []
    for form in forms:
        local = []
        for start, end in _find_occurrences(region, form):
            absolute = (lo + start, lo + end)
            if _span_available(absolute, used_target):
                local.append(absolute)
        in_region[form] = local
        if local:
            appearing.append(form)

    if selected is not None:
        chosen_form = _official_form(selected, direction)
        hit = _next_unused(in_region.get(chosen_form, ()), used_target)
        if hit is not None:
            used_target.add(hit)
            return ReviewFinding(
                id=finding_id,
                verdict=VERDICT_VERIFIED,
                rule_id=RULE_TERM_PAIR,
                source_span=source_span,
                target_span=_span_at(target, hit[0], hit[1]),
                candidates=candidates,
            )
        if _elsewhere_has_form(target, corresponding, forms):
            return ReviewFinding(
                id=finding_id,
                verdict=VERDICT_NOT_EVALUATED,
                rule_id=RULE_TERM_PAIR,
                source_span=source_span,
                target_span=None,
                candidates=candidates,
            )
        mismatch = None
        for form, hits in in_region.items():
            if form == chosen_form:
                continue
            mismatch = _next_unused(hits, used_target)
            if mismatch is not None:
                break
        target_span = (
            _span_at(target, mismatch[0], mismatch[1]) if mismatch is not None else None
        )
        return ReviewFinding(
            id=finding_id,
            verdict=VERDICT_CONFLICT,
            rule_id=RULE_TERM_PAIR,
            source_span=source_span,
            target_span=target_span,
            candidates=candidates,
        )

    if len(appearing) > 1:
        hit = _next_unused(in_region[appearing[0]], used_target)
        if hit is not None:
            used_target.add(hit)
        return ReviewFinding(
            id=finding_id,
            verdict=VERDICT_NEEDS_REVIEW,
            rule_id=RULE_TERM_PAIR,
            source_span=source_span,
            target_span=_span_at(target, hit[0], hit[1]) if hit is not None else None,
            candidates=candidates,
        )

    if appearing:
        hit = _next_unused(in_region[appearing[0]], used_target)
        if hit is not None:
            used_target.add(hit)
            return ReviewFinding(
                id=finding_id,
                verdict=VERDICT_VERIFIED,
                rule_id=RULE_TERM_PAIR,
                source_span=source_span,
                target_span=_span_at(target, hit[0], hit[1]),
                candidates=candidates,
            )

    if _elsewhere_has_form(target, corresponding, forms):
        return ReviewFinding(
            id=finding_id,
            verdict=VERDICT_NOT_EVALUATED,
            rule_id=RULE_TERM_PAIR,
            source_span=source_span,
            target_span=None,
            candidates=candidates,
        )
    return ReviewFinding(
        id=finding_id,
        verdict=VERDICT_NEEDS_REVIEW,
        rule_id=RULE_TERM_PAIR,
        source_span=source_span,
        target_span=None,
        candidates=candidates,
    )


def _judge_mention_v2(
    *,
    source: str,
    target: str,
    direction: str,
    mention: _TermSpan,
    candidates: tuple[ReviewCandidate, ...],
    corresponding: tuple[int, int] | None,
    used_target: set[tuple[int, int]],
    resolution: ReviewResolution | None,
) -> ReviewFinding:
    finding_id = mention_id(mention.start, mention.end, mention.source)
    source_span = _span_at(source, mention.start, mention.end)

    if resolution is not None and resolution.choice == CHOICE_NOT_A_TERM:
        return ReviewFinding(
            id=finding_id,
            verdict=VERDICT_NOT_EVALUATED,
            rule_id=RULE_TERM_PAIR,
            source_span=source_span,
            target_span=None,
            candidates=candidates,
        )

    selected: ReviewCandidate | None = None
    if resolution is not None and resolution.choice == CHOICE_OFFICIAL_PAIR:
        for candidate in candidates:
            if candidate.candidate_id == resolution.candidate_id:
                selected = candidate
                break
        if selected is None:
            raise ReviewRequestError(
                "invalid_request",
                "official_pair candidate_id is not current for this mention",
            )

    forms = (
        (_official_form(selected, direction),)
        if selected is not None
        else tuple(
            dict.fromkeys(
                _official_form(candidate, direction) for candidate in candidates
            )
        )
    )

    if corresponding is None:
        return ReviewFinding(
            id=finding_id,
            verdict=VERDICT_NOT_EVALUATED,
            rule_id=RULE_TERM_PAIR,
            source_span=source_span,
            target_span=None,
            candidates=candidates,
        )

    lo, hi = corresponding
    region = target[lo:hi]
    in_region: dict[str, list[tuple[int, int]]] = {}
    appearing: list[str] = []
    for form in forms:
        local = []
        for start, end in _find_occurrences(region, form):
            absolute = (lo + start, lo + end)
            if crosses_cjk_word_boundary(target, absolute[0], absolute[1]):
                continue
            if _span_available(absolute, used_target):
                local.append(absolute)
        in_region[form] = local
        if local:
            appearing.append(form)

    if selected is not None:
        chosen_form = _official_form(selected, direction)
        hit = _next_unused(in_region.get(chosen_form, ()), used_target)
        if hit is not None:
            used_target.add(hit)
            return ReviewFinding(
                id=finding_id,
                verdict=VERDICT_VERIFIED,
                rule_id=RULE_TERM_PAIR,
                source_span=source_span,
                target_span=_span_at(target, hit[0], hit[1]),
                candidates=candidates,
            )
        if _elsewhere_has_form(
            target, corresponding, forms, occurrences=_find_occurrences_v2
        ):
            return ReviewFinding(
                id=finding_id,
                verdict=VERDICT_NOT_EVALUATED,
                rule_id=RULE_TERM_PAIR,
                source_span=source_span,
                target_span=None,
                candidates=candidates,
            )
        mismatch = None
        for form, hits in in_region.items():
            if form == chosen_form:
                continue
            mismatch = _next_unused(hits, used_target)
            if mismatch is not None:
                break
        return ReviewFinding(
            id=finding_id,
            verdict=VERDICT_CONFLICT,
            rule_id=RULE_TERM_PAIR,
            source_span=source_span,
            target_span=(
                _span_at(target, mismatch[0], mismatch[1])
                if mismatch is not None
                else None
            ),
            candidates=candidates,
        )

    if len(appearing) > 1:
        hit = _next_unused(in_region[appearing[0]], used_target)
        if hit is not None:
            used_target.add(hit)
        return ReviewFinding(
            id=finding_id,
            verdict=VERDICT_NEEDS_REVIEW,
            rule_id=RULE_TERM_PAIR,
            source_span=source_span,
            target_span=_span_at(target, hit[0], hit[1]) if hit is not None else None,
            candidates=candidates,
        )

    if appearing:
        hit = _next_unused(in_region[appearing[0]], used_target)
        if hit is not None:
            used_target.add(hit)
            return ReviewFinding(
                id=finding_id,
                verdict=VERDICT_VERIFIED,
                rule_id=RULE_TERM_PAIR,
                source_span=source_span,
                target_span=_span_at(target, hit[0], hit[1]),
                candidates=candidates,
            )

    if _elsewhere_has_form(
        target, corresponding, forms, occurrences=_find_occurrences_v2
    ):
        return ReviewFinding(
            id=finding_id,
            verdict=VERDICT_NOT_EVALUATED,
            rule_id=RULE_TERM_PAIR,
            source_span=source_span,
            target_span=None,
            candidates=candidates,
        )
    return ReviewFinding(
        id=finding_id,
        verdict=VERDICT_NEEDS_REVIEW,
        rule_id=RULE_TERM_PAIR,
        source_span=source_span,
        target_span=None,
        candidates=candidates,
    )


def review_pair(
    service: TermService,
    source: str,
    target: str,
    direction: str,
    resolutions: Sequence[object] = (),
    *,
    review_version: str = RULE_VERSION,
    alignments: object = None,
    resolution_context: object = None,
    require_matcher_revision: bool = True,
) -> ReviewReport:
    """Review a pair under the explicitly selected additive protocol."""
    if review_version == RULE_VERSION:
        if alignments is not None or resolution_context is not None:
            raise ReviewRequestError(
                "invalid_request", "review-v1 does not accept v2 request fields"
            )
        return _review_pair_v1(service, source, target, direction, resolutions)
    if review_version == RULE_VERSION_V2:
        return _review_pair_v2(
            service,
            source,
            target,
            direction,
            resolutions,
            alignments=alignments,
            resolution_context=resolution_context,
            require_matcher_revision=require_matcher_revision,
        )
    raise ReviewRequestError("invalid_request", "review_version is not supported")


def _review_pair_v1(
    service: TermService,
    source: str,
    target: str,
    direction: str,
    resolutions: Sequence[object],
) -> ReviewReport:
    """The frozen v1 path; keep its reads, segmentation, and choices unchanged."""
    source = _validate_side("source", source)
    target = _validate_side("target", target)
    direction = _validate_direction(direction)
    parsed_resolutions = tuple(_coerce_resolution(item) for item in resolutions)
    dictionary = _dictionary_snapshot(service)
    surfaces = _surface_index(service.entries())
    mentions = _collect_mentions(source, surfaces)
    source_sentences = _sentence_ranges(source)
    target_sentences = _sentence_ranges(target)
    used_target: set[tuple[int, int]] = set()
    findings: list[ReviewFinding] = []
    seen_resolution_ids: set[str] = set()
    by_id: dict[str, ReviewResolution] = {}
    for resolution in parsed_resolutions:
        if resolution.mention_id in by_id:
            raise ReviewRequestError(
                "invalid_request", "duplicate resolution mention_id"
            )
        by_id[resolution.mention_id] = resolution

    for mention in mentions:
        candidates = _candidates_for(surfaces[mention.source])
        finding_id = mention_id(mention.start, mention.end, mention.source)
        resolution = by_id.get(finding_id)
        if resolution is not None:
            seen_resolution_ids.add(finding_id)
        findings.append(
            _judge_mention(
                source=source,
                target=target,
                direction=direction,
                mention=mention,
                candidates=candidates,
                source_sentences=source_sentences,
                target_sentences=target_sentences,
                used_target=used_target,
                resolution=resolution,
            )
        )

    missing = set(by_id) - seen_resolution_ids
    if missing:
        raise ReviewRequestError(
            "invalid_request", "resolution mention_id does not match a finding"
        )

    evaluated = sum(
        1
        for finding in findings
        if finding.verdict in {VERDICT_VERIFIED, VERDICT_CONFLICT, VERDICT_NEEDS_REVIEW}
    )
    not_evaluated = 1 + sum(
        1 for finding in findings if finding.verdict == VERDICT_NOT_EVALUATED
    )
    return ReviewReport(
        source_revision=text_revision(source),
        target_revision=text_revision(target),
        rule_version=RULE_VERSION,
        dictionary=dictionary,
        coverage=ReviewCoverage(
            evaluated=evaluated,
            not_evaluated=not_evaluated,
            rules=(RULE_TERM_PAIR, RULE_SENTENCE_MEANING),
        ),
        findings=tuple(findings),
        truncated=False,
    )


def _review_pair_v2(
    service: TermService,
    source: str,
    target: str,
    direction: str,
    resolutions: Sequence[object],
    *,
    alignments: object,
    resolution_context: object,
    require_matcher_revision: bool,
) -> ReviewReport:
    source = _validate_side("source", source)
    target = _validate_side("target", target)
    direction = _validate_direction(direction)
    parsed_resolutions = tuple(_coerce_resolution_v2(item) for item in resolutions)

    snapshot = service.review_snapshot()
    entries = snapshot.entries
    dictionary_revision = _dictionary_revision(entries)
    dictionary = ReviewDictionary(
        schema_version=snapshot.metadata.get("schema_version"),
        source_commit=snapshot.metadata.get("source_commit"),
        term_count=snapshot.term_count,
        revision=dictionary_revision,
    )
    current_source_revision = text_revision(source)
    if parsed_resolutions:
        if resolution_context is None:
            raise ReviewRequestError(
                "invalid_request", "nonempty resolutions require resolution_context"
            )
        context = _coerce_resolution_context(resolution_context)
        if (
            context.source_revision != current_source_revision
            or context.rule_version != RULE_VERSION_V2
            or context.dictionary_revision != dictionary_revision
            or (
                (require_matcher_revision or context.matcher_revision is not None)
                and context.matcher_revision != MATCHER_REVISION
            )
        ):
            raise ReviewRequestError("invalid_request", "resolution_context is stale")
    elif resolution_context is not None:
        _coerce_resolution_context(resolution_context)

    surfaces = _surface_index(entries)
    mentions = _collect_mentions(
        source,
        surfaces,
        eligible=lambda start, end, surface, entries: (
            _v2_one_character_eligible(source, start, end, surface, entries)
            and not crosses_cjk_word_boundary(source, start, end)
        ),
    )
    if alignments is None:
        source_sentences = _sentence_ranges_v2(source)
        target_sentences = _sentence_ranges_v2(target)
        if len(source_sentences) == len(target_sentences):
            mappings = tuple(
                ReviewAlignment(
                    source=_span_at(source, source_range[0], source_range[1]),
                    target=_span_at(target, target_range[0], target_range[1]),
                )
                for source_range, target_range in zip(
                    source_sentences, target_sentences, strict=True
                )
            )
        else:
            mappings = ()
    else:
        mappings = _coerce_alignments(alignments, source, target, mentions)

    used_target: set[tuple[int, int]] = set()
    findings: list[ReviewFinding] = []
    seen_resolution_ids: set[str] = set()
    by_id: dict[str, ReviewResolution] = {}
    for resolution in parsed_resolutions:
        if resolution.mention_id in by_id:
            raise ReviewRequestError(
                "invalid_request", "duplicate resolution mention_id"
            )
        by_id[resolution.mention_id] = resolution

    for mention in mentions:
        candidates = _candidates_for(surfaces[mention.source], include_id=True)
        finding_id = mention_id(mention.start, mention.end, mention.source)
        resolution = by_id.get(finding_id)
        if resolution is not None:
            seen_resolution_ids.add(finding_id)
        findings.append(
            _judge_mention_v2(
                source=source,
                target=target,
                direction=direction,
                mention=mention,
                candidates=candidates,
                corresponding=_mapped_target_range(mention, mappings),
                used_target=used_target,
                resolution=resolution,
            )
        )

    if set(by_id) - seen_resolution_ids:
        raise ReviewRequestError(
            "invalid_request", "resolution mention_id does not match a finding"
        )

    evaluated = sum(
        1
        for finding in findings
        if finding.verdict in {VERDICT_VERIFIED, VERDICT_CONFLICT, VERDICT_NEEDS_REVIEW}
    )
    not_evaluated = 1 + sum(
        1 for finding in findings if finding.verdict == VERDICT_NOT_EVALUATED
    )
    return ReviewReport(
        source_revision=current_source_revision,
        target_revision=text_revision(target),
        rule_version=RULE_VERSION_V2,
        matcher_revision=MATCHER_REVISION,
        dictionary=dictionary,
        coverage=ReviewCoverage(
            evaluated=evaluated,
            not_evaluated=not_evaluated,
            rules=(RULE_TERM_PAIR, RULE_SENTENCE_MEANING),
        ),
        findings=tuple(findings),
        truncated=False,
    )
