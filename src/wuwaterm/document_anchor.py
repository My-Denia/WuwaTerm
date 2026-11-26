"""Bounded read-only document anchor for one translation request.

ADR 0021's Companion Design adds one read-only metadata block beside the
previous-segment context of ADR 0019: the active Markdown heading, a document
title/topic and recently established entity mentions from earlier segments. It
exists because ``TranslationContext.before`` carries only the immediately
preceding segment, so a long guide loses the section it is in.

What this module guarantees, because the rules are the reason it exists:

* **Bounded.** ``MAX_DOCUMENT_ANCHOR_SCALARS`` covers topic plus heading plus
  every entity together, and each field has its own smaller bound. Exceeding the
  total raises on :class:`DocumentAnchor` and clamps in
  :func:`bounded_anchor_fields`, so a foreign context object cannot widen it.
* **Read-only.** Nothing here participates in ``term_preparation`` or
  ``skill_references`` admission (ADR 0017), and nothing here is or creates a
  placeholder lock or a cross-segment sense lock (ADR 0019 decision 4). The value
  is prompt background only.
* **Deterministic.** :func:`extract_document_anchor` reads only the segments it
  is given and the index it is given; the same input always produces the same
  anchor, with no dictionary, model or clock involved.

Pure: standard library only, no storage, no matcher, no transport.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

MAX_DOCUMENT_ANCHOR_SCALARS = 280
MAX_DOCUMENT_ANCHOR_TOPIC_SCALARS = 120
MAX_DOCUMENT_ANCHOR_HEADING_SCALARS = 120
MAX_DOCUMENT_ANCHOR_ENTITY_SCALARS = 60
MAX_DOCUMENT_ANCHOR_ENTITIES = 8
# How far back the extractor looks for a heading or an entity mention. A bound,
# not a tuning knob: the anchor is background for one segment.
ANCHOR_SEGMENT_WINDOW = 8

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_QUOTED_SPAN_RE = re.compile(r"[《「『“]([^《》「」『』“”\n]{1,40})[》」』”]")
_LATIN_NAME_RE = re.compile(r"[A-Z][A-Za-z0-9'’\-]{1,}")
_LIST_MARKER_RE = re.compile(r"^[ \t]*(?:[-*+>]|[0-9]+[.)])[ \t]+")


def _has_isolated_surrogate(text: str) -> bool:
    return any("\ud800" <= char <= "\udfff" for char in text)


def _clean(text: object) -> str:
    if not isinstance(text, str):
        return ""
    return "".join(
        char for char in text if not _has_isolated_surrogate(char) and char != "\x00"
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


@dataclass(frozen=True)
class DocumentAnchor:
    """Read-only document background for one request, bounded as a whole."""

    topic: str = ""
    section_heading: str = ""
    active_entities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.topic, str):
            raise ValueError("document anchor topic must be a string")
        if not isinstance(self.section_heading, str):
            raise ValueError("document anchor section_heading must be a string")
        if not isinstance(self.active_entities, tuple) or not all(
            isinstance(entity, str) for entity in self.active_entities
        ):
            raise ValueError("document anchor active_entities must be a tuple of strings")
        if len(self.topic) > MAX_DOCUMENT_ANCHOR_TOPIC_SCALARS:
            raise ValueError(
                "document anchor topic exceeds "
                f"{MAX_DOCUMENT_ANCHOR_TOPIC_SCALARS} scalars"
            )
        if len(self.section_heading) > MAX_DOCUMENT_ANCHOR_HEADING_SCALARS:
            raise ValueError(
                "document anchor section_heading exceeds "
                f"{MAX_DOCUMENT_ANCHOR_HEADING_SCALARS} scalars"
            )
        if len(self.active_entities) > MAX_DOCUMENT_ANCHOR_ENTITIES:
            raise ValueError(
                "document anchor carries more than "
                f"{MAX_DOCUMENT_ANCHOR_ENTITIES} entities"
            )
        if any(
            len(entity) > MAX_DOCUMENT_ANCHOR_ENTITY_SCALARS
            for entity in self.active_entities
        ):
            raise ValueError(
                "document anchor entity exceeds "
                f"{MAX_DOCUMENT_ANCHOR_ENTITY_SCALARS} scalars"
            )
        if self.scalar_count > MAX_DOCUMENT_ANCHOR_SCALARS:
            raise ValueError(
                "document anchor exceeds "
                f"{MAX_DOCUMENT_ANCHOR_SCALARS} scalars in total"
            )

    @property
    def scalar_count(self) -> int:
        return (
            len(self.topic)
            + len(self.section_heading)
            + sum(len(entity) for entity in self.active_entities)
        )

    @property
    def is_empty(self) -> bool:
        return not (
            self.topic.strip() or self.section_heading.strip() or self.active_entities
        )

    def as_prompt_dict(self) -> dict[str, object]:
        """The bounded shape the anchor contributes to a prompt."""
        return {
            "topic": self.topic,
            "section_heading": self.section_heading,
            "active_entities": list(self.active_entities),
        }


def bounded_anchor_fields(
    anchor: object,
) -> tuple[str, str, tuple[str, ...]]:
    """Clamp any anchor-like object to the bounds, never raising.

    A context object is caller data, so the render path cannot assume it is a
    :class:`DocumentAnchor`. Fields outside the cap are cut here instead of
    reaching a prompt, and a value that is not a string is dropped.
    """
    # Strip first: a whitespace-only field is not background, and rendering it
    # would add an empty block to a prompt that asked for nothing.
    topic = _clip(
        _clean(getattr(anchor, "topic", "")).strip(),
        MAX_DOCUMENT_ANCHOR_TOPIC_SCALARS,
    )
    heading = _clip(
        _clean(getattr(anchor, "section_heading", "")).strip(),
        MAX_DOCUMENT_ANCHOR_HEADING_SCALARS,
    )
    raw_entities = getattr(anchor, "active_entities", ())
    entities: list[str] = []
    used = len(topic) + len(heading)
    if isinstance(raw_entities, Iterable) and not isinstance(
        raw_entities, (str, bytes)
    ):
        for item in raw_entities:
            if len(entities) >= MAX_DOCUMENT_ANCHOR_ENTITIES:
                break
            entity = _clip(_clean(item), MAX_DOCUMENT_ANCHOR_ENTITY_SCALARS).strip()
            if not entity or entity in entities:
                continue
            if used + len(entity) > MAX_DOCUMENT_ANCHOR_SCALARS:
                break
            entities.append(entity)
            used += len(entity)
    return topic, heading, tuple(entities)


def build_document_anchor(
    *,
    topic: str = "",
    section_heading: str = "",
    active_entities: Sequence[str] = (),
) -> DocumentAnchor:
    """Build a clamped anchor from arbitrary input.

    The bounded constructor for callers that hold text of unknown size (a
    client, an evaluation script, :func:`extract_document_anchor`). It never
    raises on oversized input; it cuts to the bounds.
    """
    topic_value, heading_value, entities = bounded_anchor_fields(
        _AnchorInput(topic, section_heading, tuple(active_entities))
    )
    return DocumentAnchor(
        topic=topic_value,
        section_heading=heading_value,
        active_entities=entities,
    )


@dataclass(frozen=True)
class _AnchorInput:
    topic: str
    section_heading: str
    active_entities: tuple[str, ...]


def _heading_of(line: str) -> str | None:
    match = _HEADING_RE.match(line)
    if match is None:
        return None
    text = match.group(2).strip()
    return text or None


def _first_line_title(segment: str) -> str | None:
    for line in segment.splitlines():
        candidate = _LIST_MARKER_RE.sub("", line.strip())
        heading = _heading_of(candidate)
        if heading is not None:
            candidate = heading
        candidate = candidate.strip()
        if candidate:
            return candidate
    return None


def _active_entities(segments: Sequence[str]) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    position = 0
    for segment in segments:
        for match in _QUOTED_SPAN_RE.finditer(segment):
            span = match.group(1).strip()
            if span:
                counts[span] = counts.get(span, 0) + 1
                order.setdefault(span, position)
                position += 1
        for match in _LATIN_NAME_RE.finditer(segment):
            token = match.group(0)
            counts[token] = counts.get(token, 0) + 1
            order.setdefault(token, position)
            position += 1
    ranked = sorted(
        (name for name, count in counts.items() if count >= 2),
        key=lambda name: (-counts[name], -order[name]),
    )
    return tuple(ranked)


def extract_document_anchor(
    segments: Sequence[str], current_index: int
) -> DocumentAnchor:
    """The read-only anchor for segment ``current_index`` of ``segments``.

    Pure and deterministic, and bounded in work as well as in output: it reads
    at most ``ANCHOR_SEGMENT_WINDOW`` preceding segments for the heading and the
    entities, and the same number of segments at the start of the document for
    the title, so no call touches more than ``2 * ANCHOR_SEGMENT_WINDOW``
    segments however long the document is. ``topic`` is the first level-1
    heading in the document head (or, failing that, the first non-blank line of
    the first non-empty segment there); ``section_heading`` is the nearest
    Markdown heading in the window of preceding segments; ``active_entities``
    are quoted spans and Latin names that occur at least twice in that same
    window, most frequent and most recent first.

    ``segments`` are the document's segments in order (segment cores are what a
    caller has). ``current_index`` is the segment being translated. Out-of-range
    indices raise :class:`ValueError`: an anchor for a segment the caller does
    not have would silently describe the wrong place.
    """
    if not 0 <= current_index < len(segments):
        raise ValueError("current_index must address one of the given segments")
    window_start = max(0, current_index - ANCHOR_SEGMENT_WINDOW)
    preceding = list(segments[window_start:current_index])
    # The title belongs to the document's head, so both topic passes read the
    # same bounded prefix instead of walking every preceding segment.
    topic_scan = list(segments[: min(current_index, ANCHOR_SEGMENT_WINDOW)])
    topic = ""
    for segment in topic_scan:
        for line in segment.splitlines():
            match = _HEADING_RE.match(line)
            if match is not None and match.group(1) == "#":
                topic = match.group(2).strip()
                break
        if topic:
            break
    if not topic and preceding:
        for segment in topic_scan:
            candidate = _first_line_title(segment)
            if candidate:
                topic = candidate
                break
    heading = ""
    for segment in reversed(preceding):
        for line in reversed(segment.splitlines()):
            candidate = _heading_of(line)
            if candidate is not None:
                heading = candidate
                break
        if heading:
            break
    entities = _active_entities(preceding)
    return build_document_anchor(
        topic=topic,
        section_heading=heading,
        active_entities=entities,
    )
