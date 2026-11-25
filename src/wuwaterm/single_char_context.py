"""Deterministic context evidence for ambiguous one-character name mentions.

``cjk_span.classify_single_character_mention`` sorts a one-character
resonator/speaker surface into ``name_mention``, ``ambiguous_mention`` or
``ordinary_word``. Only ``name_mention`` is admitted as a reference. This
module looks at the ``ambiguous_mention`` occurrences (``红椿蕊``, ``椿姐``,
``心很重要``) and records which of four independent kinds of context evidence
the submitted request itself supplies:

``document_topic``
    The head of the request -- its first non-empty paragraph, whose first
    line is the title -- contains a longer official name of the same
    character (an official target that carries the mention's official English
    as a whole word, such as ``Camellya`` or ``心月狐 = Hsin the Moon Fox``) or
    an official game-system term (dictionary category ``core_term``, such as
    ``共鸣者`` or ``声骸``). The topic signal comes from the dictionary, not
    from a hand-written word list.
``same_paragraph_entity``
    Another admitted, non-derived official dictionary entity sits in the same
    natural paragraph.
``resonator_cooccurrence``
    Another confirmed resonator sits in the same sentence or within
    ``COOCCURRENCE_WINDOW`` scalars of the same paragraph, or the same
    character is already an admitted ``name_mention`` elsewhere in the request.
``position_context``
    The mention opens a line-initial speaker label (``椿姐：``), is an item of
    a ``、`` enumeration, sits next to a numeric build token (``0+1 椿姐``,
    ``椿姐 S0R1``), or opens a quotation, a Markdown heading or a title (a
    short first line, followed by more text, without sentence-final
    punctuation).

The evaluation is ``admitted_as_low_constraint`` only when at least two kinds
hold with distinct supporting spans and at least one of them is an entity kind
(``same_paragraph_entity`` or ``resonator_cooccurrence``). A single dictionary
hit that would satisfy two kinds at once is counted once.

Inputs are only the visible text of the current request and the entities the
caller admitted from that same text. Read-only previous context
(``TranslationContext.before``) never reaches this module. Other ambiguous
mentions are never entities, so one uncertain character cannot vouch for
another, and nothing here creates a placeholder lock or a review judgment.
This module imports only the standard library and ``cjk_span``.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .cjk_span import SINGLE_CHAR_AMBIGUOUS_MENTION

EvidenceKind = Literal[
    "document_topic",
    "same_paragraph_entity",
    "resonator_cooccurrence",
    "position_context",
]
EVIDENCE_KINDS: tuple[EvidenceKind, ...] = (
    "document_topic",
    "same_paragraph_entity",
    "resonator_cooccurrence",
    "position_context",
)
ENTITY_EVIDENCE_KINDS: frozenset[EvidenceKind] = frozenset(
    {"same_paragraph_entity", "resonator_cooccurrence"}
)
MIN_INDEPENDENT_EVIDENCE = 2
COOCCURRENCE_WINDOW = 24
TOPIC_CATEGORIES = frozenset({"core_term"})
RESONATOR_CATEGORY = "resonator"
SPEAKER_LABEL_MAX_LEN = 8
ENUMERATION_ITEM_MAX_LEN = 8
TITLE_MAX_LEN = 40

_SENTENCE_END = frozenset("。！？!?；;\n")
_TITLE_END_PUNCTUATION = frozenset("。！？!?.…；;，,、")
_SPEAKER_COLONS = frozenset("：:")
_SPEAKER_LABEL_BREAKS = frozenset("，。！？、,.!?;；「」『』“”\"'‘’（）()\n")
_INLINE_SPACE = " \t\u3000"
_LINE_LEAD = frozenset(_INLINE_SPACE + "-*•·>#")
_OPENING_QUOTES = frozenset("「『“‘\"《【〈(（[")
_BUILD_TOKEN_BEFORE_RE = re.compile(
    r"(?:(?<![0-9A-Za-z])\d\s*\+\s*\d|(?<![0-9A-Za-z])[SsCc]\d(?:\s*[Rr]\d)?|\d\s*[命链])$"
)
_BUILD_TOKEN_AFTER_RE = re.compile(
    r"^(?:\d\s*\+\s*\d|[SsCc]\d(?:\s*[Rr]\d)?|\d\s*[命链])(?![0-9A-Za-z])"
)
_MARKDOWN_HEADING_RE = re.compile(r"#{1,6}[ \t]+")

Span = tuple[int, int]


def _is_cjk(char: str) -> bool:
    return "㐀" <= char <= "鿿"


@dataclass(frozen=True)
class ContextEntity:
    """An admitted, non-derived official dictionary hit in the request text.

    ``start``/``end`` index the visible request text the evaluation reads.
    Single-character entries are admitted ``name_mention`` occurrences; an
    ``ambiguous_mention`` is never an entity.
    """

    start: int
    end: int
    source: str
    zh: str
    en: str
    category: str


@dataclass(frozen=True)
class EvidenceHit:
    """One kind of evidence and the request-text spans that support it."""

    kind: EvidenceKind
    supports: tuple[Span, ...]


@dataclass(frozen=True)
class ContextEvaluation:
    """Evidence found for one ambiguous occurrence; empty means none."""

    hits: tuple[EvidenceHit, ...] = ()

    def _has(self, kind: EvidenceKind) -> bool:
        return any(hit.kind == kind and hit.supports for hit in self.hits)

    @property
    def document_topic(self) -> bool:
        return self._has("document_topic")

    @property
    def same_paragraph_entity(self) -> bool:
        return self._has("same_paragraph_entity")

    @property
    def resonator_cooccurrence(self) -> bool:
        return self._has("resonator_cooccurrence")

    @property
    def position_context(self) -> bool:
        return self._has("position_context")

    @property
    def evidence(self) -> tuple[EvidenceKind, ...]:
        """Kinds with at least one supporting span, in ``EVIDENCE_KINDS`` order."""
        return tuple(kind for kind in EVIDENCE_KINDS if self._has(kind))

    @property
    def admitted_as_low_constraint(self) -> bool:
        """Two independent kinds, at least one of them an entity kind.

        Independence means the two kinds can be supported by two different
        spans: a single dictionary hit that is both the topic of the request
        and the only entity of the paragraph counts as one piece of evidence.
        """
        supports = {
            hit.kind: frozenset(hit.supports) for hit in self.hits if hit.supports
        }
        if len(supports) < MIN_INDEPENDENT_EVIDENCE:
            return False
        for kind in ENTITY_EVIDENCE_KINDS:
            entity_spans = supports.get(kind)
            if not entity_spans:
                continue
            for other, other_spans in supports.items():
                if other == kind:
                    continue
                if any(a != b for a in entity_spans for b in other_spans):
                    return True
        return False


class RequestContextIndex:
    """Request-level facts shared by every ambiguous occurrence of one request.

    Built once per request so that many occurrences of the same character do
    not rescan the whole text.
    """

    def __init__(
        self,
        text: str,
        *,
        paragraphs: Sequence[Span],
        entities: Sequence[ContextEntity],
    ) -> None:
        self.text = text
        spans = tuple(
            (max(0, start), min(len(text), end))
            for start, end in paragraphs
            if 0 <= start <= end
        ) or ((0, len(text)),)
        self.paragraphs: tuple[Span, ...] = tuple(sorted(spans))
        self._paragraph_starts = [start for start, _end in self.paragraphs]
        self.entities: tuple[ContextEntity, ...] = tuple(
            sorted(
                (
                    entity
                    for entity in entities
                    if 0 <= entity.start < entity.end <= len(text)
                ),
                key=lambda item: (item.start, item.end, item.source),
            )
        )
        self.head: Span | None = next(
            (
                (start, end)
                for start, end in self.paragraphs
                if text[start:end].strip()
            ),
            None,
        )
        self._sentence_breaks = [
            index for index, char in enumerate(text) if char in _SENTENCE_END
        ]
        first_line = self._first_content_line()
        self.title_line: Span | None = first_line

    def _first_content_line(self) -> Span | None:
        text = self.text
        pos = 0
        while pos <= len(text):
            newline = text.find("\n", pos)
            end = len(text) if newline == -1 else newline
            line = text[pos:end].strip()
            if line:
                # A title is a short first line, followed by more text, that
                # does not end like a sentence (心很重要。 is prose, not a title).
                if newline == -1 or not text[end:].strip():
                    return None
                if len(line) > TITLE_MAX_LEN or line[-1] in _TITLE_END_PUNCTUATION:
                    return None
                return (pos, end)
            if newline == -1:
                return None
            pos = newline + 1
        return None

    def paragraph_of(self, offset: int) -> Span:
        index = bisect.bisect_right(self._paragraph_starts, offset) - 1
        if index < 0:
            return self.paragraphs[0]
        start, end = self.paragraphs[index]
        if start <= offset <= end:
            return (start, end)
        return (offset, offset)

    def sentence_of(self, start: int, end: int) -> Span:
        breaks = self._sentence_breaks
        left_index = bisect.bisect_left(breaks, start) - 1
        lo = breaks[left_index] + 1 if left_index >= 0 else 0
        right_index = bisect.bisect_left(breaks, end)
        hi = breaks[right_index] if right_index < len(breaks) else len(self.text)
        return (lo, hi)


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def _carries_official_word(longer_en: str, official_en: str) -> bool:
    if not longer_en or not official_en:
        return False
    pattern = re.compile(rf"(?<!\w){re.escape(official_en)}(?!\w)", re.IGNORECASE)
    return pattern.search(longer_en) is not None


def _cjk_item(text: str, start: int, end: int) -> Span:
    lo = start
    while lo > 0 and _is_cjk(text[lo - 1]):
        lo -= 1
    hi = end
    while hi < len(text) and _is_cjk(text[hi]):
        hi += 1
    return (lo, hi)


def _line_bounds(text: str, start: int, end: int) -> Span:
    line_start = text.rfind("\n", 0, start) + 1
    newline = text.find("\n", end)
    return (line_start, len(text) if newline == -1 else newline)


def _speaker_label(text: str, start: int, end: int) -> bool:
    line_start, line_end = _line_bounds(text, start, end)
    if any(char not in _LINE_LEAD for char in text[line_start:start]):
        return False
    limit = min(line_end, start + SPEAKER_LABEL_MAX_LEN + 1)
    for index in range(end, limit):
        char = text[index]
        if char in _SPEAKER_COLONS:
            return True
        if char in _SPEAKER_LABEL_BREAKS or char.isspace():
            return False
    return False


def _enumeration_item(text: str, start: int, end: int) -> bool:
    item_start, item_end = _cjk_item(text, start, end)
    if item_end - item_start > ENUMERATION_ITEM_MAX_LEN:
        return False
    return (item_start > 0 and text[item_start - 1] == "、") or (
        item_end < len(text) and text[item_end] == "、"
    )


def _build_token_adjacent(text: str, start: int, end: int) -> bool:
    """A numeric build token touches the mention or its CJK item.

    ``0+1 椿姐`` and ``椿姐 S0R1`` put the token beside the item; ``6链椿姐``
    glues a CJK-suffixed token directly to the character.
    """
    item_start, item_end = _cjk_item(text, start, end)
    line_start, line_end = _line_bounds(text, start, end)
    for left in (item_start, start):
        before = text[line_start:left].rstrip(_INLINE_SPACE)
        if _BUILD_TOKEN_BEFORE_RE.search(before[-12:]) is not None:
            return True
    for right in (item_end, end):
        after = text[right:line_end].lstrip(_INLINE_SPACE)
        if _BUILD_TOKEN_AFTER_RE.match(after[:12]) is not None:
            return True
    return False


def _quote_or_title_initial(
    index: RequestContextIndex, start: int, end: int
) -> bool:
    text = index.text
    item_start, _item_end = _cjk_item(text, start, end)
    if item_start != start:
        return False
    if start > 0 and text[start - 1] in _OPENING_QUOTES:
        return True
    line_start, _line_end = _line_bounds(text, start, end)
    heading = _MARKDOWN_HEADING_RE.match(text, line_start)
    if heading is not None and heading.end() == start:
        return True
    title = index.title_line
    if title is not None and title[0] <= start < title[1]:
        return not text[title[0] : start].strip(_INLINE_SPACE)
    return False


def _position_context(index: RequestContextIndex, start: int, end: int) -> bool:
    text = index.text
    return (
        _speaker_label(text, start, end)
        or _enumeration_item(text, start, end)
        or _build_token_adjacent(text, start, end)
        or _quote_or_title_initial(index, start, end)
    )


def evaluate_ambiguous_mention(
    index: RequestContextIndex,
    start: int,
    end: int,
    *,
    source: str,
    en: str,
) -> ContextEvaluation:
    """Collect request-local evidence for the ambiguous mention ``[start, end)``.

    ``start``/``end`` index ``index.text``. ``source`` is the one-character
    dictionary surface and ``en`` its official English, used to recognize a
    longer official name of the same character and to tell another resonator
    from this one.
    """
    text = index.text
    if not (0 <= start < end <= len(text)) or text[start:end] != source:
        return ContextEvaluation()

    topic: list[Span] = []
    paragraph: list[Span] = []
    cooccurrence: list[Span] = []

    paragraph_start, paragraph_end = index.paragraph_of(start)
    sentence_start, sentence_end = index.sentence_of(start, end)
    # The co-occurrence window never reaches into another paragraph.
    window_start = max(paragraph_start, start - COOCCURRENCE_WINDOW)
    window_end = min(paragraph_end, end + COOCCURRENCE_WINDOW)
    head = index.head
    official_en = en.casefold()

    for entity in index.entities:
        if _overlaps(entity.start, entity.end, start, end):
            continue
        span = (entity.start, entity.end)
        if (
            head is not None
            and head[0] <= entity.start
            and entity.end <= head[1]
            and (
                entity.category in TOPIC_CATEGORIES
                or (
                    entity.source != source
                    and _carries_official_word(entity.en, en)
                )
            )
        ):
            topic.append(span)
        if paragraph_start <= entity.start and entity.end <= paragraph_end:
            paragraph.append(span)
        if entity.source == source:
            # The same character is already an admitted name mention here.
            cooccurrence.append(span)
        elif (
            entity.category == RESONATOR_CATEGORY
            and entity.en.casefold() != official_en
            and (
                (sentence_start <= entity.start and entity.end <= sentence_end)
                or _overlaps(entity.start, entity.end, window_start, window_end)
            )
        ):
            cooccurrence.append(span)

    hits: list[EvidenceHit] = []
    if topic:
        hits.append(EvidenceHit("document_topic", tuple(topic)))
    if paragraph:
        hits.append(EvidenceHit("same_paragraph_entity", tuple(paragraph)))
    if cooccurrence:
        hits.append(EvidenceHit("resonator_cooccurrence", tuple(cooccurrence)))
    if _position_context(index, start, end):
        hits.append(EvidenceHit("position_context", ((start, end),)))
    return ContextEvaluation(tuple(hits))


__all__ = [
    "COOCCURRENCE_WINDOW",
    "ENTITY_EVIDENCE_KINDS",
    "EVIDENCE_KINDS",
    "MIN_INDEPENDENT_EVIDENCE",
    "SINGLE_CHAR_AMBIGUOUS_MENTION",
    "TOPIC_CATEGORIES",
    "ContextEntity",
    "ContextEvaluation",
    "EvidenceHit",
    "EvidenceKind",
    "RequestContextIndex",
    "evaluate_ambiguous_mention",
]
