"""``wuwaterm-termbase-v1``: a user-local termbase as read-only prompt references.

This module owns the format ADR 0021 specifies and the only thing Core may do
with it: match bounded entries against one submitted segment and hand them to
the model as non-binding preference references. It never locks a term, never
writes ``terms.db``, never touches ``/v1/reviews`` and never reaches
``term_preparation`` admission.

Three properties are decisions, not accidents:

* **A reference, never a lock.** Nothing here produces a ``__WUWA_TERM_...__``
  placeholder or a ``TermReference``. ``origin`` stays ``"user_glossary"`` so a
  reader can tell a user preference from an official pair.
* **Official data wins.** ``select_prompt_references`` drops any entry whose
  source collides with an official surface the segment already carries, so an
  exact ``terms.db`` hit, an official lock and an official reference always take
  precedence (ADR 0017, ADR 0021 §3.2).
* **Fail closed on the file.** Unknown keys, duplicate keys, non-finite
  numbers, isolated surrogates, NUL bytes and prototype-pollution keys are
  rejected before an entry exists; every cap is a module constant, and error
  messages never echo file content.

Pure: standard library only, no storage, no matcher, no transport.
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

TERMBASE_FORMAT = "wuwaterm-termbase-v1"
TERMBASE_SCHEMA_VERSION = 1
MAX_TERMBASE_ENTRIES = 2_000
MAX_TERMBASE_BYTES = 1_048_576
MAX_TERMBASE_ID_SCALARS = 64
MAX_TERMBASE_SOURCE_SCALARS = 120
MAX_TERMBASE_TARGET_SCALARS = 160
MAX_TERMBASE_NOTE_SCALARS = 160
MAX_TERMBASE_TIMESTAMP_SCALARS = 35
# Hard per-request cap on matched entries. A caller may ask for fewer; it can
# never ask for more, because the number of prompt lines is the exposure.
MAX_USER_GLOSSARY_REFERENCES = 16
TermbaseDirection = Literal["en", "zh"]
TERMBASE_DIRECTIONS: tuple[TermbaseDirection, ...] = ("en", "zh")
TermbaseKind = Literal["community_term", "preferred_term", "do_not_translate"]
TERMBASE_KINDS: tuple[TermbaseKind, ...] = (
    "community_term",
    "preferred_term",
    "do_not_translate",
)
USER_GLOSSARY_ORIGIN = "user_glossary"

TERMBASE_TOP_LEVEL_KEYS = frozenset(
    {"format", "schema_version", "updated_at", "entries"}
)
TERMBASE_ENTRY_KEYS = frozenset(
    {"id", "source", "target", "direction", "kind", "note", "updated_at"}
)
# A JSON object with one of these keys is refused at any depth: the format is
# read by clients in JavaScript, where they rewrite an object's prototype.
PROTOTYPE_POLLUTION_KEYS = frozenset({"__proto__", "constructor", "prototype"})

_TIMESTAMP_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,9})?"
    r"(Z|[+-]00:00)$"
)
_LATIN_LETTER_RE = re.compile(r"[0-9A-Za-z]")


class TermbaseError(ValueError):
    """A ``wuwaterm-termbase-v1`` payload is malformed or unsafe.

    Messages name the field or the rule and never quote the payload, so the
    exception is safe to surface to a caller or a log.
    """


@dataclass(frozen=True)
class TermbaseEntry:
    """One validated termbase row."""

    id: str
    source: str
    target: str
    direction: TermbaseDirection
    kind: TermbaseKind
    note: str = ""

    def as_prompt_dict(self) -> dict[str, str]:
        """The bounded shape one entry contributes to a prompt."""
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "note": self.note,
        }


@dataclass(frozen=True)
class UserGlossaryReference:
    """A matched termbase entry on its way into one request's context.

    ``origin`` is the ADR 0021 label for this layer. It is a plain label: the
    value never becomes a ``TermReference`` and never reaches admission.
    """

    id: str
    source: str
    target: str
    direction: TermbaseDirection
    kind: TermbaseKind
    note: str = ""

    @property
    def origin(self) -> str:
        return USER_GLOSSARY_ORIGIN

    def as_prompt_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "note": self.note,
        }


def _scalar_count(text: str) -> int:
    return len(text)


def _has_isolated_surrogate(text: str) -> bool:
    return any("\ud800" <= char <= "\udfff" for char in text)


def _has_control_nul(text: str) -> bool:
    return "\x00" in text


def _has_latin_or_digit(text: str) -> bool:
    return _LATIN_LETTER_RE.search(text) is not None


def _contains_non_whitespace(text: str) -> bool:
    return any(not char.isspace() for char in text)


def _reject_duplicate_or_unsafe_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise TermbaseError("duplicate object key")
        seen.add(key)
        if key in PROTOTYPE_POLLUTION_KEYS:
            raise TermbaseError("reserved object key")
    return dict(pairs)


def _reject_non_finite_number(value: str) -> Any:
    raise TermbaseError("non-finite JSON number")


def _scan_strings(value: Any, *, depth: int = 0) -> None:
    """Reject an isolated surrogate or a NUL byte anywhere in ``value``."""
    if depth > 8:
        raise TermbaseError("payload nests too deeply")
    if isinstance(value, str):
        if _has_isolated_surrogate(value):
            raise TermbaseError("payload contains an isolated surrogate")
        if _has_control_nul(value):
            raise TermbaseError("payload contains a NUL byte")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _scan_strings(key, depth=depth + 1)
            _scan_strings(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _scan_strings(item, depth=depth + 1)


def _is_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if not 20 <= _scalar_count(value) <= MAX_TERMBASE_TIMESTAMP_SCALARS:
        return False
    match = _TIMESTAMP_RE.match(value)
    if match is None:
        return False
    year, month, day, hour, minute, second = (
        int(part) for part in match.groups()[:6]
    )
    try:
        datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return False
    return True


def _require_scalar_bounded(
    value: object, *, name: str, minimum: int, maximum: int
) -> str:
    if not isinstance(value, str):
        raise TermbaseError(f"{name} must be a string")
    count = _scalar_count(value)
    if count < minimum or count > maximum:
        raise TermbaseError(f"{name} must be {minimum}..{maximum} scalars")
    if _has_isolated_surrogate(value) or _has_control_nul(value):
        raise TermbaseError(f"{name} contains a forbidden character")
    return value


def _parse_entry(raw: object, *, index: int) -> TermbaseEntry:
    if not isinstance(raw, dict):
        raise TermbaseError(f"entry {index} must be an object")
    if set(raw) != TERMBASE_ENTRY_KEYS:
        # Name the field count, never the payload: an unknown or missing key is
        # the same rejection and both must stay free of file content.
        raise TermbaseError(f"entry {index} must carry exactly its declared keys")
    entry_id = _require_scalar_bounded(
        raw["id"], name="entry id", minimum=1, maximum=MAX_TERMBASE_ID_SCALARS
    )
    source = _require_scalar_bounded(
        raw["source"],
        name="entry source",
        minimum=1,
        maximum=MAX_TERMBASE_SOURCE_SCALARS,
    )
    target = _require_scalar_bounded(
        raw["target"],
        name="entry target",
        minimum=1,
        maximum=MAX_TERMBASE_TARGET_SCALARS,
    )
    note = _require_scalar_bounded(
        raw["note"],
        name="entry note",
        minimum=0,
        maximum=MAX_TERMBASE_NOTE_SCALARS,
    )
    if not _contains_non_whitespace(entry_id):
        raise TermbaseError("entry id must not be blank")
    if not _contains_non_whitespace(source):
        raise TermbaseError("entry source must not be blank")
    if not _contains_non_whitespace(target):
        raise TermbaseError("entry target must not be blank")
    direction = raw["direction"]
    if direction not in TERMBASE_DIRECTIONS:
        raise TermbaseError("entry direction must be 'en' or 'zh'")
    kind = raw["kind"]
    if kind not in TERMBASE_KINDS:
        raise TermbaseError("entry kind is not a declared kind")
    if not _is_utc_timestamp(raw["updated_at"]):
        raise TermbaseError("entry updated_at must be an RFC 3339 UTC timestamp")
    return TermbaseEntry(
        id=entry_id,
        source=source,
        target=target,
        direction=direction,
        kind=kind,
        note=note,
    )


def parse_termbase(payload: str | bytes) -> tuple[TermbaseEntry, ...]:
    """Parse and validate a ``wuwaterm-termbase-v1`` payload.

    Accepts the exact UTF-8 JSON text of a termbase file (``str``) or its
    bytes. Raises :class:`TermbaseError` for anything the format does not
    allow; a valid payload returns its entries in file order.
    """
    if isinstance(payload, bytes):
        if len(payload) > MAX_TERMBASE_BYTES:
            raise TermbaseError("termbase exceeds the 1 MiB size bound")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TermbaseError("termbase is not valid UTF-8") from exc
    elif isinstance(payload, str):
        if _has_isolated_surrogate(payload):
            raise TermbaseError("payload contains an isolated surrogate")
        if _has_control_nul(payload):
            raise TermbaseError("payload contains a NUL byte")
        try:
            encoded = payload.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TermbaseError("termbase is not valid UTF-8") from exc
        if len(encoded) > MAX_TERMBASE_BYTES:
            raise TermbaseError("termbase exceeds the 1 MiB size bound")
        text = payload
    else:
        raise TermbaseError("termbase payload must be text or bytes")
    if _has_control_nul(text):
        raise TermbaseError("payload contains a NUL byte")
    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_or_unsafe_key,
            parse_constant=_reject_non_finite_number,
        )
    except TermbaseError:
        raise
    except ValueError as exc:
        raise TermbaseError("termbase is not valid JSON") from exc
    _scan_strings(data)
    if not isinstance(data, dict):
        raise TermbaseError("termbase must be a JSON object")
    if set(data) != TERMBASE_TOP_LEVEL_KEYS:
        raise TermbaseError("termbase must carry exactly its declared keys")
    if data["format"] != TERMBASE_FORMAT:
        raise TermbaseError(f"termbase format must be {TERMBASE_FORMAT}")
    if type(data["schema_version"]) is not int or (
        data["schema_version"] != TERMBASE_SCHEMA_VERSION
    ):
        raise TermbaseError(f"termbase schema_version must be {TERMBASE_SCHEMA_VERSION}")
    if not _is_utc_timestamp(data["updated_at"]):
        raise TermbaseError("termbase updated_at must be an RFC 3339 UTC timestamp")
    entries_raw = data["entries"]
    if not isinstance(entries_raw, list):
        raise TermbaseError("termbase entries must be a list")
    if len(entries_raw) > MAX_TERMBASE_ENTRIES:
        raise TermbaseError("termbase exceeds the 2,000 entry bound")
    entries = tuple(
        _parse_entry(raw, index=index) for index, raw in enumerate(entries_raw)
    )
    seen_ids: set[str] = set()
    for entry in entries:
        if entry.id in seen_ids:
            raise TermbaseError("termbase entry ids must be unique")
        seen_ids.add(entry.id)
    return entries


def _surface_pattern(surface: str) -> re.Pattern[str] | None:
    """Compile the match rule for one surface, or ``None`` for a blank one."""
    if not surface or not _contains_non_whitespace(surface):
        return None
    if _has_latin_or_digit(surface):
        # Latin/digit surfaces need word edges so ``Tuner`` does not match
        # inside ``retuner`` or ``Tuners``. A space, hyphen or punctuation is an
        # edge; another letter or digit is not.
        return re.compile(
            rf"(?<![0-9A-Za-z]){re.escape(surface)}(?![0-9A-Za-z])",
            re.IGNORECASE,
        )
    return re.compile(re.escape(surface))


def _first_occurrence(text: str, surface: str) -> int:
    pattern = _surface_pattern(surface)
    if pattern is None:
        return -1
    match = pattern.search(text)
    return -1 if match is None else match.start()


def match_user_glossary(
    entries: Sequence[TermbaseEntry],
    source_text: str,
    *,
    direction: TermbaseDirection,
    excluded_surfaces: Iterable[str] = (),
    limit: int = MAX_USER_GLOSSARY_REFERENCES,
) -> tuple[UserGlossaryReference, ...]:
    """Match at most ``limit`` entries of one direction against ``source_text``.

    ``direction`` is the entry's own encoding: ``"en"`` for Chinese into
    English, ``"zh"`` for English into Chinese (ADR 0021 §2). ``excluded_surfaces``
    carries official surfaces the caller already knows about; a matching entry is
    dropped here as well as at render time. The result is ordered by first
    occurrence in the segment, then by file order, so the same inputs always
    produce the same lines.
    """
    if direction not in TERMBASE_DIRECTIONS:
        raise ValueError(f"unknown termbase direction {direction!r}")
    if limit < 1:
        return ()
    effective_limit = min(int(limit), MAX_USER_GLOSSARY_REFERENCES)
    excluded = {surface.casefold() for surface in excluded_surfaces if surface}
    matches: list[tuple[int, int, UserGlossaryReference]] = []
    seen_sources: set[str] = set()
    for index, entry in enumerate(entries):
        if entry.direction != direction:
            continue
        source_key = entry.source.casefold()
        if source_key in excluded or source_key in seen_sources:
            continue
        position = _first_occurrence(source_text, entry.source)
        if position < 0:
            continue
        seen_sources.add(source_key)
        matches.append(
            (
                position,
                index,
                UserGlossaryReference(
                    id=entry.id,
                    source=entry.source,
                    target=entry.target,
                    direction=entry.direction,
                    kind=entry.kind,
                    note=entry.note,
                ),
            )
        )
    matches.sort(key=lambda item: (item[0], item[1]))
    return tuple(reference for _position, _index, reference in matches[:effective_limit])


def select_prompt_references(
    references: Sequence[UserGlossaryReference],
    *,
    direction: TermbaseDirection,
    official_surfaces: Iterable[str] = (),
    limit: int = MAX_USER_GLOSSARY_REFERENCES,
) -> tuple[UserGlossaryReference, ...]:
    """The references one request may render: same direction, no official collision.

    This is the last gate before a prompt. ``official_surfaces`` are the segment's
    own official dictionary surfaces (locks and references), so an entry that
    names the same surface loses to the official pair no matter how it was
    supplied. Entries also pass through the per-request count cap here, because a
    caller object is not required to obey it.
    """
    if direction not in TERMBASE_DIRECTIONS:
        raise ValueError(f"unknown termbase direction {direction!r}")
    if limit < 1:
        return ()
    effective_limit = min(int(limit), MAX_USER_GLOSSARY_REFERENCES)
    official = {surface.casefold() for surface in official_surfaces if surface}
    selected: list[UserGlossaryReference] = []
    seen_sources: set[str] = set()
    for reference in references:
        if not isinstance(reference, UserGlossaryReference):
            continue
        if reference.direction != direction:
            continue
        source_key = reference.source.casefold()
        if source_key in official or source_key in seen_sources:
            continue
        if not _reference_within_bounds(reference):
            continue
        seen_sources.add(source_key)
        selected.append(reference)
        if len(selected) >= effective_limit:
            break
    return tuple(selected)


def _reference_within_bounds(reference: UserGlossaryReference) -> bool:
    """Whether every rendered field is still inside the format's bounds."""
    if reference.kind not in TERMBASE_KINDS:
        return False
    for value, minimum, maximum in (
        (reference.id, 1, MAX_TERMBASE_ID_SCALARS),
        (reference.source, 1, MAX_TERMBASE_SOURCE_SCALARS),
        (reference.target, 1, MAX_TERMBASE_TARGET_SCALARS),
        (reference.note, 0, MAX_TERMBASE_NOTE_SCALARS),
    ):
        if not isinstance(value, str):
            return False
        if not minimum <= _scalar_count(value) <= maximum:
            return False
        if _has_isolated_surrogate(value) or _has_control_nul(value):
            return False
    return _contains_non_whitespace(reference.source) and _contains_non_whitespace(
        reference.target
    )
