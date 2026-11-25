#!/usr/bin/env python3
"""Frozen scorer and leakage scan for the held-out usability evaluation (ADR 0020).

``score``           validate the frozen inputs and the judgment files, resolve every item
                  to its final judgment (two agreeing judges, or the adjudicator) and
                  write the ``usability-report-v1`` metrics, per arm and direction.
``scan``            count 12-scalar windows of the sealed sets (H1, H2) that occur in an
                  input; counts only, the matched text is never printed.
``paired-compare``  compare two scored arms 1:1 per direction with stratified
                  document-cluster bootstrap 95% CIs, MQM revision cost, and unit
                  improvement cost.

The script imports nothing from ``wuwaterm``, only the standard library, so a
later Core change cannot change how frozen judgments are scored.

Exit codes. ``score``: 0 complete, 3 at least one item pending (the report is
still written), 2 invalid input. ``scan``: 0 no uncleared hit, 1 an uncleared
hit, 2 invalid input. Messages for invalid input go to stderr and name the file
and the rule, never evaluation text.

Choices the ADR leaves open (each is exercised by a test):

* A cell (arm, direction) with a pending item reports its counts, but every
  derived ratio of that cell (usable rate, interval, name accuracy and
  consistency, critical share and density) is null.
* An ``adj`` record is accepted only for an item whose ``j1`` and ``j2`` both
  exist and differ; anything else is invalid.
* Metrics 2, 3 and 4 count final-judged, non-``run_failed`` paragraphs only.
* A judgment is consistent only if grade A comes with naturalness ``natural`` and
  grade B does not come with ``disfluent``; anything else is invalid.
* An items file may list ``partial_arms`` (a drift sample). Such an arm skips the
  one-item-per-scored-paragraph completeness check (at most one item per
  paragraph still holds) and its cells cover its own items only. An arm is
  partial if any items file lists it.
* ``documents`` counts the distinct documents of all items of a cell or genre,
  ``run_failed`` and pending items included, as ``n`` does.
* ``scan`` stdout is ``{"schema", "window", "rows": [...]}``.
* ``scan --audit-out PATH`` writes one JSON line per uncleared (window, sealed
  set, input, location): the SHA-256 of the window, never its text. The same
  window at the same location collapses to one line. The file is written, empty
  if need be, before stdout is printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import subprocess
import sys
import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

Z95 = 1.959963984540054
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SAMPLE_SCHEMA = "usability-sample-v1"
NAMES_SCHEMA = "usability-names-v1"
ITEMS_SCHEMA = "usability-items-v1"
JUDGMENT_SCHEMA = "usability-judgment-v1"
REPORT_SCHEMA = "usability-report-v1"
SCAN_SCHEMA = "usability-scan-v1"
ARM_SCORES_SCHEMA = "usability-arm-scores-v1"
PAIRED_COMPARE_SCHEMA = "usability-paired-compare-v1"

DIRECTIONS = ("zh-en", "en-zh")
PAIRED_DIRECTIONS = ("zh->en", "en->zh")
DIRECTION_ALIASES = {
    "zh->en": "zh->en",
    "zh-en": "zh->en",
    "en->zh": "en->zh",
    "en-zh": "en->zh",
}
GENRES = ("announcement", "story", "guide", "mixed")
EXCLUSIONS = ("dictionary", "no_letter", "duplicate")
PASSES = ("j1", "j2", "adj")
GRADES = ("A", "B", "C", "undecidable")
NATURALNESS = ("natural", "stiff", "disfluent", "undecidable")
ERROR_TYPES = ("omission", "addition", "meaning_shift", "reference", "number", "negation")
CRITICAL_BREAKDOWN_TYPES = ("accuracy", "negation", "number", "reference", "omission", "term")
LABELS = ("official", "not_official", "omitted", "undecidable")
COMMUNITY_LABELS = ("official", "accepted", "not_official", "omitted", "undecidable")
RENDERED_LABELS = ("official", "not_official")

ENDERS = "。．.！？!?\n"  # review-v2 sentence enders
DECIMAL_POINTS = ".．"
DEFAULT_WINDOW = 12
DEFAULT_BOOTSTRAP_SAMPLES = 10_000
DEFAULT_SEED = 0
NOT_COMPUTABLE_DELTA_NS = "not_computable (delta_not_significant)"
MQM_WEIGHTS = {
    "A": 0,
    "B_or_stiff": 1,
    "minor_term": 2,
    "disfluent_or_misleading": 5,
    "critical": 10,
}

EXIT_OK = 0
EXIT_SCAN_HIT = 1
EXIT_INVALID = 2
EXIT_PENDING = 3


class InputError(Exception):
    """Invalid input. The message names the file and the rule, never evaluation text."""


def fail(where: str, rule: str) -> NoReturn:
    raise InputError(f"{where}: {rule}")


# --------------------------------------------------------------------- reading


def read_text(path: str) -> str:
    """The file as UTF-8 with no newline translation (windows are compared raw)."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        fail(path, "cannot read file")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        fail(path, "not valid UTF-8")


def load_json(path: str) -> Any:
    text = read_text(path)

    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({key for key, _ in pairs}) != len(pairs):
            fail(path, "duplicate object key")
        return dict(pairs)

    def no_constant(_name: str) -> NoReturn:
        fail(path, "NaN and Infinity are not valid JSON")

    try:
        return json.loads(text, object_pairs_hook=no_duplicate_keys, parse_constant=no_constant)
    except (ValueError, RecursionError):
        fail(path, "not valid JSON")


def obj(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(where, "must be an object")
    return value


def arr(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        fail(where, "must be an array")
    return value


def need_keys(data: dict[str, Any], where: str, keys: Sequence[str]) -> dict[str, Any]:
    for key in keys:
        if key not in data:
            fail(where, f"missing key {key!r}")
    return data


def exact_keys(
    data: dict[str, Any], where: str, keys: Sequence[str], optional: Sequence[str] = ()
) -> dict[str, Any]:
    need_keys(data, where, keys)
    if set(data) - set(keys) - set(optional):
        fail(where, "unknown key")
    return data


def get_str(data: dict[str, Any], key: str, where: str, *, empty: bool = False) -> str:
    value = data[key]
    if not isinstance(value, str) or (not empty and value == ""):
        fail(where, f"{key} must be a {'string' if empty else 'non-empty string'}")
    return value


def get_opt_str(data: dict[str, Any], key: str, where: str) -> str | None:
    value = data[key]
    if value is not None and not isinstance(value, str):
        fail(where, f"{key} must be a string or null")
    return value


def get_int(data: dict[str, Any], key: str, where: str, minimum: int = 0) -> int:
    value = data[key]
    if type(value) is not int or value < minimum:  # bool is not a count
        fail(where, f"{key} must be an integer of at least {minimum}")
    return value


def get_bool(data: dict[str, Any], key: str, where: str) -> bool:
    value = data[key]
    if type(value) is not bool:
        fail(where, f"{key} must be true or false")
    return value


def get_enum(data: dict[str, Any], key: str, allowed: Sequence[str], where: str) -> str:
    value = data[key]
    if not isinstance(value, str) or value not in allowed:
        fail(where, f"{key} is not one of {', '.join(allowed)}")
    return value


def check_schema(data: dict[str, Any], schema: str, path: str) -> None:
    need_keys(data, path, ("schema",))
    if data["schema"] != schema:
        fail(path, f"schema must be {schema!r}")


# ----------------------------------------------------------------------- model


@dataclass(frozen=True)
class Paragraph:
    id: str
    doc: str
    direction: str
    genre: str
    scored: bool
    source: str
    reference: str | None


@dataclass(frozen=True)
class Sample:
    path: str
    label: str
    paragraphs: list[Paragraph]


@dataclass(frozen=True)
class Occurrence:
    occ: str
    paragraph: str
    name_id: str


@dataclass(frozen=True)
class Item:
    item: str
    paragraph: str
    arm: str
    translation: str | None
    run_failed: bool


@dataclass(frozen=True)
class NameJudgment:
    label: str
    rendering: str | None
    misleading: bool


@dataclass(frozen=True)
class Judgment:
    item: str
    grade: str
    critical: dict[str, int]
    naturalness: str
    names: dict[str, NameJudgment]
    origin: str  # where the record came from, for messages


def load_sample(path: str) -> Sample:
    data = obj(load_json(path), path)
    check_schema(data, SAMPLE_SCHEMA, path)
    need_keys(data, path, ("set", "paragraphs"))
    label = get_str(data, "set", path)
    paragraphs: list[Paragraph] = []
    seen: set[str] = set()
    for index, raw in enumerate(arr(data["paragraphs"], f"{path}: paragraphs")):
        where = f"{path}: paragraphs[{index}]"
        row = need_keys(
            obj(raw, where),
            where,
            ("id", "doc", "direction", "genre", "scored", "exclusion", "source", "reference"),
        )
        pid = get_str(row, "id", where)
        if pid in seen:
            fail(where, "paragraph id repeated in this file")
        seen.add(pid)
        scored = get_bool(row, "scored", where)
        exclusion = row["exclusion"]
        if exclusion is not None:
            exclusion = get_enum(row, "exclusion", EXCLUSIONS, where)
        if scored != (exclusion is None):
            fail(where, "scored must be true exactly when exclusion is null")
        paragraphs.append(
            Paragraph(
                id=pid,
                doc=get_str(row, "doc", where),
                direction=get_enum(row, "direction", DIRECTIONS, where),
                genre=get_enum(row, "genre", GENRES, where),
                scored=scored,
                source=get_str(row, "source", where, empty=True),
                reference=get_opt_str(row, "reference", where),
            )
        )
    return Sample(path, label, paragraphs)


def load_names(
    path: str, paragraphs: dict[str, Paragraph], seen: set[str]
) -> list[Occurrence]:
    data = obj(load_json(path), path)
    check_schema(data, NAMES_SCHEMA, path)
    need_keys(data, path, ("occurrences",))
    found: list[Occurrence] = []
    for index, raw in enumerate(arr(data["occurrences"], f"{path}: occurrences")):
        where = f"{path}: occurrences[{index}]"
        row = need_keys(
            obj(raw, where),
            where,
            ("occ", "paragraph", "start", "end", "surface", "name_id", "official_forms"),
        )
        occ = get_str(row, "occ", where)
        if occ in seen:
            fail(where, "occ id repeated")
        seen.add(occ)
        paragraph = paragraphs.get(get_str(row, "paragraph", where))
        if paragraph is None:
            fail(where, "paragraph is not in the sample files")
        if not paragraph.scored:
            fail(where, "paragraph is not scored")
        start, end = get_int(row, "start", where), get_int(row, "end", where)
        if not start < end <= len(paragraph.source):
            fail(where, "start and end must be a non-empty range inside the paragraph source")
        if get_str(row, "surface", where) != paragraph.source[start:end]:
            fail(where, "surface does not equal source[start:end]")
        forms = arr(row["official_forms"], f"{where}: official_forms")
        if not forms or not all(isinstance(form, str) and form for form in forms):
            fail(where, "official_forms must be a non-empty array of non-empty strings")
        found.append(Occurrence(occ, paragraph.id, get_str(row, "name_id", where)))
    return found


def load_items(
    path: str, paragraphs: dict[str, Paragraph], seen: set[str]
) -> tuple[list[Item], set[str]]:
    """The items of one file and the arms it lists as partial (a drift sample)."""
    data = obj(load_json(path), path)
    check_schema(data, ITEMS_SCHEMA, path)
    need_keys(data, path, ("items",))
    partial: set[str] = set()
    if "partial_arms" in data:
        listed = arr(data["partial_arms"], f"{path}: partial_arms")
        if not all(isinstance(arm, str) and arm for arm in listed):
            fail(path, "partial_arms must be an array of non-empty strings")
        partial.update(listed)
    found: list[Item] = []
    for index, raw in enumerate(arr(data["items"], f"{path}: items")):
        where = f"{path}: items[{index}]"
        row = need_keys(
            obj(raw, where), where, ("item", "paragraph", "arm", "translation", "run_failed")
        )
        item = get_str(row, "item", where)
        if item in seen:
            fail(where, "item id repeated")
        seen.add(item)
        paragraph = paragraphs.get(get_str(row, "paragraph", where))
        if paragraph is None:
            fail(where, "paragraph is not in the sample files")
        if not paragraph.scored:
            fail(where, "item for an unscored paragraph")
        translation = get_opt_str(row, "translation", where)
        run_failed = get_bool(row, "run_failed", where)
        if run_failed != (translation is None):
            fail(where, "run_failed must be true exactly when translation is null")
        found.append(Item(item, paragraph.id, get_str(row, "arm", where), translation, run_failed))
    return found, partial


def parse_judgment(
    raw: Any,
    where: str,
    pass_name: str,
    items: dict[str, Item],
    occurrences: dict[str, list[Occurrence]],
) -> Judgment:
    row = exact_keys(
        obj(raw, where),
        where,
        ("item", "grade", "critical_errors", "naturalness", "names"),
        optional=("reason",),
    )
    item_id = get_str(row, "item", where)
    item = items.get(item_id)
    if item is None:
        fail(where, "item is not in the items files")
    where = f"{where} (item {item_id!r})"
    if item.run_failed:
        fail(where, "a run_failed item has no judgment")
    grade = get_enum(row, "grade", GRADES, where)
    naturalness = get_enum(row, "naturalness", NATURALNESS, where)

    critical: dict[str, int] = {}
    for index, raw_error in enumerate(arr(row["critical_errors"], f"{where}: critical_errors")):
        spot = f"{where}: critical_errors[{index}]"
        error = exact_keys(obj(raw_error, spot), spot, ("type", "count"))
        kind = get_enum(error, "type", ERROR_TYPES, spot)
        critical[kind] = critical.get(kind, 0) + get_int(error, "count", spot, minimum=1)

    names: dict[str, NameJudgment] = {}
    for index, raw_name in enumerate(arr(row["names"], f"{where}: names")):
        spot = f"{where}: names[{index}]"
        entry = exact_keys(obj(raw_name, spot), spot, ("occ", "label", "rendering", "misleading"))
        occ = get_str(entry, "occ", spot)
        if occ in names:
            fail(spot, "occ listed twice")
        label = get_enum(entry, "label", LABELS, spot)
        rendering = get_opt_str(entry, "rendering", spot)
        misleading = get_bool(entry, "misleading", spot)
        if label in RENDERED_LABELS:
            if rendering is None or not normalize_rendering(rendering):
                fail(spot, "label official or not_official needs a non-empty rendering")
        elif rendering is not None:
            fail(spot, "label omitted or undecidable needs rendering null")
        if misleading and label != "not_official":
            fail(spot, "misleading is only allowed with label not_official")
        names[occ] = NameJudgment(label, rendering, misleading)
    if set(names) != {o.occ for o in occurrences.get(item.paragraph, ())}:
        fail(where, "names must list exactly the occurrences of the item's paragraph")

    if (critical or any(n.misleading for n in names.values())) and grade != "C":
        fail(where, "a critical error or a misleading name requires grade C")
    if grade == "A" and naturalness != "natural":
        fail(where, "grade A requires naturalness natural")
    if grade == "B" and naturalness == "disfluent":
        fail(where, "grade B is not allowed with naturalness disfluent")
    reason = row["reason"] if "reason" in row else None
    if reason is not None and not isinstance(reason, str):
        fail(where, "reason must be a string")
    if (grade in ("C", "undecidable") or pass_name == "adj") and not (reason or "").strip():
        fail(where, "reason is required for grade C, grade undecidable and every adj record")
    return Judgment(item_id, grade, critical, naturalness, names, where)


def load_judgments(
    path: str,
    items: dict[str, Item],
    occurrences: dict[str, list[Occurrence]],
    by_pass: dict[str, dict[str, Judgment]],
) -> None:
    data = exact_keys(obj(load_json(path), path), path, ("schema", "pass", "records"))
    check_schema(data, JUDGMENT_SCHEMA, path)
    pass_name = get_enum(data, "pass", PASSES, path)
    for index, raw in enumerate(arr(data["records"], f"{path}: records")):
        where = f"{path}: records[{index}]"
        judgment = parse_judgment(raw, where, pass_name, items, occurrences)
        if judgment.item in by_pass[pass_name]:
            fail(f"{where} (item {judgment.item!r})", f"item judged twice in pass {pass_name}")
        by_pass[pass_name][judgment.item] = judgment


# ---------------------------------------------------------- normalization, math


def trimmable(char: str) -> bool:
    return char.isspace() or unicodedata.category(char).startswith("P")


def normalize_rendering(text: str) -> str:
    """NFKC, case folding, then whitespace and punctuation trimmed at both ends."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    start, end = 0, len(folded)
    while start < end and trimmable(folded[start]):
        start += 1
    while end > start and trimmable(folded[end - 1]):
        end -= 1
    return folded[start:end]


def is_ender(text: str, index: int) -> bool:
    char = text[index]
    if char not in ENDERS:
        return False
    return not (
        char in DECIMAL_POINTS
        and 0 < index < len(text) - 1
        and text[index - 1].isdigit()
        and text[index + 1].isdigit()
    )


def split_sentences(text: str) -> list[str]:
    """review-v2: a run of enders ends a sentence; `.` between two digits is not one.

    A piece made only of enders and whitespace joins the sentence before it.
    """
    sentences: list[str] = []

    def add(piece: str) -> None:
        if all(char in ENDERS or char.isspace() for char in piece):
            if sentences:
                sentences[-1] += piece
        else:
            sentences.append(piece)

    start = index = 0
    length = len(text)
    while index < length:
        if is_ender(text, index):
            end = index + 1
            while end < length and is_ender(text, end):
                end += 1
            add(text[start:end])
            start = index = end
        else:
            index += 1
    if start < length:
        add(text[start:])
    return sentences


def normalize_sentence(text: str) -> str:
    """NFKC, then every whitespace and Unicode punctuation scalar removed; case kept."""
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", text)
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def literal_match(reference: str, translation: str) -> tuple[int, int]:
    """(reference sentences counted, matched) by multiset match of normalized sentences."""
    pool = Counter(normalize_sentence(s) for s in split_sentences(translation))
    counted = matched = 0
    for sentence in split_sentences(reference):
        key = normalize_sentence(sentence)
        if not key:
            continue
        counted += 1
        if pool[key] > 0:
            pool[key] -= 1
            matched += 1
    return counted, matched


def wilson(successes: int, total: int) -> tuple[float, float] | None:
    """Wilson score interval, z = 1.959963984540054; None without observations."""
    if total <= 0:
        return None
    p = successes / total
    z2 = Z95 * Z95
    denominator = 1 + z2 / total
    centre = (p + z2 / (2 * total)) / denominator
    half = Z95 * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / denominator
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == total else min(1.0, centre + half)
    return low, high


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


# ----------------------------------------------------------------------- score


def signature(judgment: Judgment) -> tuple[Any, ...]:
    """What two judges must agree on; `reason` is not part of it."""
    names = sorted(
        (
            occ,
            n.label,
            n.misleading,
            None if n.rendering is None else normalize_rendering(n.rendering),
        )
        for occ, n in judgment.names.items()
    )
    return judgment.grade, sorted(judgment.critical.items()), judgment.naturalness, names


def resolve_finals(
    items: dict[str, Item], by_pass: dict[str, dict[str, Judgment]]
) -> dict[str, Judgment | None]:
    """The final judgment per item; None means pending."""
    j1, j2, adj = by_pass["j1"], by_pass["j2"], by_pass["adj"]
    for item_id, record in adj.items():
        first, second = j1.get(item_id), j2.get(item_id)
        if first is None or second is None or signature(first) == signature(second):
            fail(record.origin, "adj record without two differing j1 and j2 judgments")
    finals: dict[str, Judgment | None] = {}
    for item_id, item in items.items():
        if item.run_failed:
            continue
        first, second = j1.get(item_id), j2.get(item_id)
        if first is None or second is None:
            finals[item_id] = None
        elif signature(first) == signature(second):
            finals[item_id] = first
        else:
            finals[item_id] = adj.get(item_id)
    return finals


def usable_block(
    n: int, documents: int, usable: int, grades: Counter[str], failed: int, pending: int
) -> dict[str, Any]:
    interval = wilson(usable, n) if pending == 0 else None
    return {
        "n": n,
        "documents": documents,
        "usable": usable,
        "rate": ratio(usable, n) if pending == 0 else None,
        "wilson95": list(interval) if interval else None,
        "grades": {grade: grades[grade] for grade in GRADES},
        "run_failed": failed,
        "pending": pending,
    }


def cell_report(
    cell_items: list[Item],
    paragraphs: dict[str, Paragraph],
    occurrences: dict[str, list[Occurrence]],
    finals: dict[str, Judgment | None],
) -> dict[str, Any]:
    failed = [it for it in cell_items if it.run_failed]
    judged = [
        (it, final)
        for it in cell_items
        if not it.run_failed and (final := finals[it.item]) is not None
    ]
    pending = len(cell_items) - len(failed) - len(judged)
    complete = pending == 0
    grades = Counter(j.grade for _, j in judged)
    usable = grades["A"] + grades["B"]

    genres: dict[str, Any] = {}
    for genre in GENRES:
        genre_items = [it for it in cell_items if paragraphs[it.paragraph].genre == genre]
        in_genre = {it.item for it in genre_items}
        good = sum(1 for it, j in judged if it.item in in_genre and j.grade in ("A", "B"))
        waiting = sum(1 for it in genre_items if not it.run_failed and finals[it.item] is None)
        genre_interval = wilson(good, len(in_genre)) if waiting == 0 else None
        genres[genre] = {
            "n": len(in_genre),
            "documents": len({paragraphs[it.paragraph].doc for it in genre_items}),
            "usable": good,
            "rate": ratio(good, len(in_genre)) if waiting == 0 else None,
            "wilson95": list(genre_interval) if genre_interval else None,
        }

    name_count = official = 0
    groups: dict[tuple[str, str], list[NameJudgment]] = {}
    for it, j in judged:
        paragraph = paragraphs[it.paragraph]
        name_ids = {o.occ: o.name_id for o in occurrences.get(it.paragraph, ())}
        for occ, name in j.names.items():
            name_count += 1
            official += name.label == "official"
            groups.setdefault((paragraph.doc, name_ids[occ]), []).append(name)
    multi = [g for g in groups.values() if len(g) >= 2]
    consistent = sum(
        1
        for g in multi
        if all(n.label in RENDERED_LABELS for n in g)
        and len({normalize_rendering(n.rendering or "") for n in g}) == 1
    )

    with_critical = sum(1 for _, j in judged if j.critical)
    errors = sum(sum(j.critical.values()) for _, j in judged)
    scalars = sum(len(paragraphs[it.paragraph].source) for it, _ in judged)

    reference_paragraphs = reference_sentences = matched = 0
    for it in cell_items:
        reference = paragraphs[it.paragraph].reference
        if it.run_failed or reference is None or it.translation is None:
            continue
        reference_paragraphs += 1
        counted, hit = literal_match(reference, it.translation)
        reference_sentences += counted
        matched += hit

    return {
        "usable": usable_block(
            len(cell_items),
            len({paragraphs[it.paragraph].doc for it in cell_items}),
            usable,
            grades,
            len(failed),
            pending,
        ),
        "names": {
            "occurrences": name_count,
            "official": official,
            "accuracy": ratio(official, name_count) if complete else None,
            "multi_occurrence_names": len(multi),
            "consistent_names": consistent,
            "consistency": ratio(consistent, len(multi)) if complete else None,
        },
        "critical": {
            "paragraphs": len(judged),
            "with_critical": with_critical,
            "share": ratio(with_critical, len(judged)) if complete else None,
            "errors": errors,
            "source_scalars": scalars,
            "per_1000_source_scalars": (
                ratio(errors * 1000, scalars) if complete else None
            ),
        },
        "naturalness": {
            kind: sum(1 for _, j in judged if j.naturalness == kind) for kind in NATURALNESS
        },
        "literal": {
            "paragraphs_with_reference": reference_paragraphs,
            "reference_sentences": reference_sentences,
            "matched": matched,
            "rate": ratio(matched, reference_sentences),
        },
        "excluded_from_metrics_2_5": len(failed),
        "undecidable_items": sorted(it.item for it, j in judged if j.grade == "undecidable"),
        "genres": genres,
    }


def cmd_score(args: argparse.Namespace) -> int:
    samples = [load_sample(path) for path in args.sample]
    paragraphs: dict[str, Paragraph] = {}
    for sample in samples:
        for paragraph in sample.paragraphs:
            if paragraph.id in paragraphs:
                fail(sample.path, "paragraph id also used in another sample file")
            paragraphs[paragraph.id] = paragraph

    occurrences: dict[str, list[Occurrence]] = {}
    seen_occ: set[str] = set()
    for path in args.names:
        for occurrence in load_names(path, paragraphs, seen_occ):
            occurrences.setdefault(occurrence.paragraph, []).append(occurrence)

    items: dict[str, Item] = {}
    seen_items: set[str] = set()
    partial_arms: set[str] = set()
    for path in args.items:
        found, partial = load_items(path, paragraphs, seen_items)
        partial_arms |= partial
        for item in found:
            items[item.item] = item
    if not items:
        fail("items", "no items")
    arms = sorted({item.arm for item in items.values()})
    per_slot = Counter((item.paragraph, item.arm) for item in items.values())
    for paragraph in paragraphs.values():
        for arm in arms if paragraph.scored else ():
            count = per_slot[(paragraph.id, arm)]
            if arm in partial_arms:
                if count > 1:
                    fail(
                        "items",
                        f"scored paragraph {paragraph.id!r} has several items for arm {arm!r}",
                    )
            elif count != 1:
                fail(
                    "items",
                    f"scored paragraph {paragraph.id!r} needs exactly one item for arm {arm!r}",
                )

    by_pass: dict[str, dict[str, Judgment]] = {name: {} for name in PASSES}
    for path in args.judgments:
        load_judgments(path, items, occurrences, by_pass)
    finals = resolve_finals(items, by_pass)

    report_arms: dict[str, Any] = {}
    pending_items = 0
    for arm in arms:
        report_arms[arm] = {}
        for direction in DIRECTIONS:
            cell_items = sorted(
                (
                    it
                    for it in items.values()
                    if it.arm == arm and paragraphs[it.paragraph].direction == direction
                ),
                key=lambda it: it.item,
            )
            cell = cell_report(cell_items, paragraphs, occurrences, finals)
            pending_items += cell["usable"]["pending"]
            report_arms[arm][direction] = cell
    report = {
        "schema": REPORT_SCHEMA,
        "state": "pending" if pending_items else "complete",
        "pending_items": pending_items,
        "sets": sorted({sample.label for sample in samples}),
        "arms": report_arms,
    }

    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        try:
            Path(args.out).write_bytes(text.encode("utf-8"))
        except OSError:
            fail(args.out, "cannot write report")
    else:
        sys.stdout.write(text)
    return EXIT_PENDING if pending_items else EXIT_OK


# ------------------------------------------------------------------------ scan


@dataclass(frozen=True)
class InputSource:
    label: str
    units: list[str]
    unit_ids: list[str] | None  # input paragraph id per unit, for --input-sample
    locate: Callable[[int, int], str]  # (unit, scalar offset) -> where it is, for the audit file


def windows(text: str, size: int) -> Iterator[str]:
    for start in range(len(text) - size + 1):
        yield text[start : start + size]


def line_locator(text: str) -> Callable[[int, int], str]:
    """Locations of a text as "line <n>": 1-based, newline characters only count."""
    newlines = [pos for pos, char in enumerate(text) if char == "\n"]
    return lambda _unit, offset: f"line {bisect_left(newlines, offset) + 1}"


def id_locator(ids: list[str]) -> Callable[[int, int], str]:
    return lambda unit, _offset: ids[unit]


def added_lines(diff: str) -> tuple[str, Callable[[int, int], str]]:
    """The added lines of a unified diff, without the leading '+', joined by newline.

    The locator names the diff-file line of the added line a text offset falls on; the
    joining newline belongs to the added line before it.
    """
    pieces: list[str] = []
    starts: list[int] = []
    numbers: list[int] = []
    offset = 0
    old_left = new_left = 0  # lines still owed to the current hunk, per its @@ header
    after_minus_header = False
    for number, line in enumerate(diff.split("\n"), start=1):
        added = False
        if old_left > 0 or new_left > 0:
            # Inside a hunk every '+' line is content, even one that reads "+++".
            if line.startswith("+"):
                added, new_left = True, new_left - 1
            elif line.startswith("-"):
                old_left -= 1
            elif line.startswith(" ") or line == "":
                old_left, new_left = old_left - 1, new_left - 1
            elif not line.startswith("\\"):
                old_left = new_left = 0  # not hunk syntax: the hunk is over
        if not added and old_left <= 0 and new_left <= 0:
            hunk = HUNK_HEADER.match(line)
            if hunk:
                old_left = int(hunk.group(2)) if hunk.group(2) is not None else 1
                new_left = int(hunk.group(4)) if hunk.group(4) is not None else 1
                after_minus_header = False
                continue
            if after_minus_header and line.startswith("+++ "):
                after_minus_header = False  # the "+++ b/path" file header
                continue
            after_minus_header = line.startswith("--- ")
            # Outside a hunk a '+' line is not hunk syntax; scan it rather than drop it.
            added = line.startswith("+")
        if added:
            pieces.append(line[1:])
            starts.append(offset)
            numbers.append(number)
            offset += len(line)  # the text without '+', plus the joining newline
    return "\n".join(pieces), lambda _unit, off: f"line {numbers[bisect_right(starts, off) - 1]}"


def git_output(spec: str, repo: str, args: list[str]) -> bytes:
    try:
        done = subprocess.run(["git", "-C", repo, *args], capture_output=True, check=False)
    except OSError:
        fail("--clear-git", "git cannot be started")
    if done.returncode != 0:
        fail(f"--clear-git {spec}", f"git {args[0]} failed")
    return done.stdout


def git_blob_texts(spec: str) -> Iterator[str]:
    repo, _, commit = spec.rpartition(":")
    if not repo or not commit or commit.startswith("-"):
        fail(f"--clear-git {spec}", "expected REPO:COMMIT")
    listing = git_output(spec, repo, ["ls-tree", "-r", "-z", commit])
    for entry in listing.split(b"\0"):
        meta, _, _path = entry.partition(b"\t")
        fields = meta.split()
        if len(fields) != 3 or fields[1] != b"blob" or fields[0] == b"120000":
            continue  # submodules, symlinks, blank trailer
        data = git_output(spec, repo, ["cat-file", "blob", fields[2].decode("ascii")])
        if b"\0" in data:
            continue
        try:
            yield data.decode("utf-8")
        except UnicodeDecodeError:
            continue


def audit_records(
    source: InputSource,
    uncleared_by_label: dict[str, set[str]],
    size: int,
    index: dict[str, list[int]],
    paragraph_label: list[str],
    paragraph_ids: list[str],
) -> list[dict[str, Any]]:
    """One record per uncleared (window, sealed set, location) of one input.

    A record holds the SHA-256 of the window, never the window or any input text.
    """
    wanted = set().union(*uncleared_by_label.values())
    seen: set[tuple[str, str, str]] = set()
    records: list[dict[str, Any]] = []
    for unit_no, unit in enumerate(source.units):
        for start, window in enumerate(windows(unit, size)):
            if window not in wanted:
                continue
            location = source.locate(unit_no, start)
            digest = hashlib.sha256(window.encode("utf-8")).hexdigest()
            for label, uncleared in uncleared_by_label.items():
                if window not in uncleared or (digest, label, location) in seen:
                    continue
                seen.add((digest, label, location))
                records.append(
                    {
                        "window_sha256": digest,
                        "sealed": label,
                        "sealed_paragraph_ids": sorted(
                            {paragraph_ids[g] for g in index[window] if paragraph_label[g] == label}
                        ),
                        "input": source.label,
                        "location": location,
                    }
                )
    return records


def cmd_scan(args: argparse.Namespace) -> int:
    size = args.window
    if size < 1:
        fail("--window", "must be at least 1")
    sealed = [load_sample(path) for path in args.sealed]

    sources: list[InputSource] = []
    for path in args.input or ():
        text = read_text(path)
        sources.append(InputSource(path, [text], None, line_locator(text)))
    for path in args.diff or ():
        text, locate = added_lines(read_text(path))
        sources.append(InputSource(path, [text], None, locate))
    for path in args.input_sample or ():
        sample = load_sample(path)
        units: list[str] = []
        ids: list[str] = []
        for paragraph in sample.paragraphs:
            for text in (paragraph.source, paragraph.reference):
                if text is not None:
                    units.append(text)
                    ids.append(paragraph.id)
        sources.append(InputSource(sample.label, units, ids, id_locator(ids)))
    if not sources:
        fail("scan", "give at least one of --input, --diff, --input-sample")

    clear_texts = [read_text(path) for path in args.clear or ()]
    for spec in args.clear_git or ():
        clear_texts.extend(git_blob_texts(spec))

    labels: list[str] = []
    paragraph_label: list[str] = []
    paragraph_ids: list[str] = []
    index: dict[str, list[int]] = {}
    for sample in sealed:
        if sample.label not in labels:
            labels.append(sample.label)
        for paragraph in sample.paragraphs:
            gid = len(paragraph_label)
            paragraph_label.append(sample.label)
            paragraph_ids.append(paragraph.id)
            for text in (paragraph.source, paragraph.reference):
                for window in windows(text or "", size):
                    owners = index.setdefault(window, [])
                    if not owners or owners[-1] != gid:
                        owners.append(gid)

    unit_hits = [
        [{w for w in windows(unit, size) if w in index} for unit in source.units]
        for source in sources
    ]
    hit_windows = set().union(*(h for per_source in unit_hits for h in per_source))
    cleared: set[str] = set()
    if hit_windows:
        for text in clear_texts:
            cleared.update(w for w in windows(text, size) if w in hit_windows)
            if len(cleared) == len(hit_windows):
                break

    rows: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for source, per_unit in zip(sources, unit_hits):
        uncleared_by_label: dict[str, set[str]] = {}
        for label in labels:
            hit = {
                w
                for h in per_unit
                for w in h
                if any(paragraph_label[gid] == label for gid in index[w])
            }
            uncleared = hit - cleared
            uncleared_by_label[label] = uncleared
            row: dict[str, Any] = {
                "input": source.label,
                "sealed": label,
                "windows_hit": len(hit),
                "windows_cleared": len(hit & cleared),
                "windows_uncleared": len(uncleared),
                "input_units_with_uncleared_hit": sum(1 for h in per_unit if h & uncleared),
                "sealed_paragraphs_with_uncleared_hit": len(
                    {
                        gid
                        for w in uncleared
                        for gid in index[w]
                        if paragraph_label[gid] == label
                    }
                ),
            }
            if source.unit_ids is not None:
                row["uncleared_input_paragraph_ids"] = sorted(
                    {pid for pid, h in zip(source.unit_ids, per_unit) if h & uncleared}
                )
            rows.append(row)
        if args.audit_out and any(uncleared_by_label.values()):
            audit.extend(
                audit_records(source, uncleared_by_label, size, index, paragraph_label, paragraph_ids)
            )

    if args.audit_out:
        lines = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in audit)
        try:
            Path(args.audit_out).write_bytes(lines.encode("utf-8"))
        except OSError:
            fail(args.audit_out, "cannot write audit file")
    print(json.dumps({"schema": SCAN_SCHEMA, "window": size, "rows": rows}, ensure_ascii=False, indent=2))
    return EXIT_SCAN_HIT if any(row["windows_uncleared"] for row in rows) else EXIT_OK


# -------------------------------------------------------------- paired-compare


@dataclass(frozen=True)
class ScoredUnit:
    unit_id: str
    doc_id: str
    direction: str
    genre: str
    source_scalars: int
    run_failed: bool
    grade: str | None
    naturalness: str | None
    critical_breakdown: dict[str, int]
    critical_10pt_count: int
    minor_term_errors: int
    misleading_term_errors: int
    mqm_points: int
    post_edit_distance: float | None


@dataclass(frozen=True)
class ArmTelemetry:
    posts: int | None = None
    tokens: int | None = None
    latency_seconds: float | None = None
    cost: float | None = None


@dataclass(frozen=True)
class ArmScores:
    arm: str
    units: dict[str, ScoredUnit]
    telemetry: dict[str, ArmTelemetry]


def get_non_neg_float(data: dict[str, Any], key: str, where: str) -> float:
    value = data[key]
    if type(value) is bool or not isinstance(value, (int, float)):
        fail(where, f"{key} must be a non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        fail(where, f"{key} must be a finite non-negative number")
    return result


def normalize_direction(value: Any, where: str) -> str:
    if not isinstance(value, str) or value not in DIRECTION_ALIASES:
        fail(where, "direction must be one of zh->en, en->zh, zh-en, en-zh")
    return DIRECTION_ALIASES[value]


def levenshtein_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        curr = [i]
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            curr.append(min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def normalized_post_edit_distance(translation: str, reference: str) -> float:
    return levenshtein_distance(translation, reference) / max(len(translation), len(reference), 1)


def parse_telemetry_block(raw: Any, where: str) -> dict[str, ArmTelemetry]:
    table = obj(raw, where)
    parsed: dict[str, ArmTelemetry] = {}
    for raw_dir, raw_entry in table.items():
        direction = normalize_direction(raw_dir, f"{where}: {raw_dir!r}")
        if direction in parsed:
            fail(where, f"duplicate direction {direction!r} in telemetry")
        entry_where = f"{where}.{raw_dir}"
        entry = exact_keys(
            obj(raw_entry, entry_where),
            entry_where,
            (),
            optional=(
                "posts",
                "tokens",
                "total_tokens",
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "latency_seconds",
                "cost_usd",
                "cost",
            ),
        )
        posts: int | None = None
        if entry.get("posts") is not None:
            posts = get_int(entry, "posts", entry_where, minimum=0)

        if "tokens" in entry and "total_tokens" in entry:
            fail(entry_where, "cannot specify both tokens and total_tokens")
        tokens: int | None = None
        if "tokens" in entry and entry["tokens"] is not None:
            if entry["tokens"] != "unknown":
                tokens = get_int(entry, "tokens", entry_where, minimum=0)
        elif "total_tokens" in entry and entry["total_tokens"] is not None:
            if entry["total_tokens"] != "unknown":
                tokens = get_int(entry, "total_tokens", entry_where, minimum=0)
        else:
            part_keys = ("prompt_tokens", "completion_tokens", "reasoning_tokens")
            present_parts = [entry[k] for k in part_keys if entry.get(k) is not None]
            if present_parts:
                for k in part_keys:
                    if entry.get(k) is not None and entry.get(k) != "unknown":
                        get_int(entry, k, entry_where, minimum=0)
                if "unknown" not in present_parts:
                    tokens = sum(
                        get_int(entry, k, entry_where, minimum=0)
                        for k in part_keys
                        if entry.get(k) is not None
                    )

        latency_seconds: float | None = None
        if entry.get("latency_seconds") is not None:
            latency_seconds = get_non_neg_float(entry, "latency_seconds", entry_where)

        if "cost_usd" in entry and "cost" in entry:
            fail(entry_where, "cannot specify both cost_usd and cost")
        cost: float | None = None
        if entry.get("cost_usd") is not None:
            cost = get_non_neg_float(entry, "cost_usd", entry_where)
        elif entry.get("cost") is not None:
            cost = get_non_neg_float(entry, "cost", entry_where)

        parsed[direction] = ArmTelemetry(
            posts=posts,
            tokens=tokens,
            latency_seconds=latency_seconds,
            cost=cost,
        )
    return parsed


def parse_unit_critical_errors(
    raw: Any, where: str
) -> tuple[dict[str, int], int, int]:
    """Return (6-category breakdown without term, 10pt critical count, term count from critical_errors)."""
    breakdown = {kind: 0 for kind in CRITICAL_BREAKDOWN_TYPES}
    critical_10pt = 0
    term_from_critical = 0
    allowed = (*ERROR_TYPES, "accuracy", "term")

    def add_error(kind: str, count: int) -> None:
        nonlocal critical_10pt, term_from_critical
        if kind == "term":
            term_from_critical += count
            breakdown["term"] += count
            return
        critical_10pt += count
        if kind in ("addition", "meaning_shift", "accuracy"):
            breakdown["accuracy"] += count
        else:
            breakdown[kind] += count

    if isinstance(raw, list):
        for idx, item in enumerate(raw):
            spot = f"{where}: critical_errors[{idx}]"
            entry = exact_keys(obj(item, spot), spot, ("type", "count"))
            kind = get_enum(entry, "type", allowed, spot)
            add_error(kind, get_int(entry, "count", spot, minimum=1))
    elif isinstance(raw, dict):
        for raw_kind, raw_count in raw.items():
            if not isinstance(raw_kind, str) or raw_kind not in allowed:
                fail(where, f"unknown critical error type {raw_kind!r}")
            if type(raw_count) is not int or raw_count < 0:
                fail(where, f"critical_errors[{raw_kind!r}] must be an integer >= 0")
            if raw_count > 0:
                add_error(raw_kind, raw_count)
    else:
        fail(where, "critical_errors must be an array or an object")
    return breakdown, critical_10pt, term_from_critical


def parse_unit_name_list(raw: Any, where: str) -> tuple[int, int]:
    """Return (minor_term_errors, misleading_term_errors) from a name judgment list."""
    minor = 0
    misleading = 0
    for idx, item in enumerate(arr(raw, where)):
        spot = f"{where}[{idx}]"
        entry = exact_keys(
            obj(item, spot),
            spot,
            ("label", "misleading"),
            optional=("occ", "rendering"),
        )
        label = get_enum(entry, "label", COMMUNITY_LABELS, spot)
        is_misleading = get_bool(entry, "misleading", spot)
        if is_misleading and label != "not_official":
            fail(spot, "misleading is only allowed with label not_official")
        if label == "not_official":
            if is_misleading:
                misleading += 1
            else:
                minor += 1
    return minor, misleading


def compute_mqm_points(
    grade: str,
    naturalness: str,
    critical_10pt_count: int,
    minor_term_errors: int,
    misleading_term_errors: int,
) -> int:
    """Compute H3 protocol §4 deterministic MQM revision cost points for a non-failed unit."""
    points = 0
    if grade in ("B", "C") and naturalness != "disfluent":
        if grade == "B" or naturalness == "stiff":
            points += 1
    if naturalness == "disfluent":
        points += 5
    points += minor_term_errors * 2
    points += misleading_term_errors * 5
    points += critical_10pt_count * 10
    return points


def load_arm_scores(path: str) -> ArmScores:
    data = exact_keys(
        obj(load_json(path), path),
        path,
        ("schema", "arm", "units"),
        optional=("telemetry",),
    )
    check_schema(data, ARM_SCORES_SCHEMA, path)
    arm = get_str(data, "arm", path)
    telemetry = parse_telemetry_block(data["telemetry"], f"{path}: telemetry") if "telemetry" in data else {}
    units: dict[str, ScoredUnit] = {}

    for index, raw in enumerate(arr(data["units"], f"{path}: units")):
        where = f"{path}: units[{index}]"
        row = exact_keys(
            obj(raw, where),
            where,
            ("unit_id", "doc_id", "direction", "genre"),
            optional=(
                "source_scalars",
                "source",
                "run_failed",
                "grade",
                "naturalness",
                "critical_errors",
                "names",
                "community_names",
                "minor_term_errors",
                "misleading_term_errors",
                "post_edit_distance",
                "translation",
                "post_edited_reference",
                "reason",
                "pending",
            ),
        )
        if "pending" in row and get_bool(row, "pending", where):
            fail(where, "pending items cannot be paired-compared")

        unit_id = get_str(row, "unit_id", where)
        if unit_id in units:
            fail(where, f"duplicate unit_id {unit_id!r}")
        doc_id = get_str(row, "doc_id", where)
        direction = normalize_direction(row["direction"], where)
        genre = get_enum(row, "genre", GENRES, where)

        if "source_scalars" in row:
            source_scalars = get_int(row, "source_scalars", where, minimum=0)
            if "source" in row and row["source"] is not None:
                source_text = get_str(row, "source", where, empty=True)
                if len(source_text) != source_scalars:
                    fail(where, "source_scalars does not match length of source")
        elif "source" in row and row["source"] is not None:
            source_scalars = len(get_str(row, "source", where, empty=True))
        else:
            fail(where, "missing key 'source_scalars'")

        run_failed = get_bool(row, "run_failed", where) if "run_failed" in row else False
        if run_failed:
            if row.get("grade") is not None:
                fail(where, "a run_failed unit has no grade")
            if row.get("naturalness") is not None:
                fail(where, "a run_failed unit has no naturalness")
            if row.get("critical_errors") not in (None, [], {}):
                fail(where, "a run_failed unit has no critical_errors")
            if row.get("names") not in (None, []):
                fail(where, "a run_failed unit has no names")
            if row.get("community_names") not in (None, []):
                fail(where, "a run_failed unit has no community_names")
            if row.get("minor_term_errors") is not None:
                fail(where, "a run_failed unit has no minor_term_errors")
            if row.get("misleading_term_errors") is not None:
                fail(where, "a run_failed unit has no misleading_term_errors")
            if row.get("post_edit_distance") is not None:
                fail(where, "a run_failed unit has no post_edit_distance")
            if row.get("post_edited_reference") is not None:
                fail(where, "a run_failed unit has no post_edited_reference")
            units[unit_id] = ScoredUnit(
                unit_id=unit_id,
                doc_id=doc_id,
                direction=direction,
                genre=genre,
                source_scalars=source_scalars,
                run_failed=True,
                grade=None,
                naturalness=None,
                critical_breakdown={kind: 0 for kind in CRITICAL_BREAKDOWN_TYPES},
                critical_10pt_count=0,
                minor_term_errors=0,
                misleading_term_errors=0,
                mqm_points=0,
                post_edit_distance=None,
            )
            continue

        if "grade" not in row or row["grade"] is None:
            fail(where, "non-failed unit requires grade")
        grade = get_enum(row, "grade", GRADES, where)
        if "naturalness" not in row or row["naturalness"] is None:
            fail(where, "non-failed unit requires naturalness")
        naturalness = get_enum(row, "naturalness", NATURALNESS, where)

        if "critical_errors" in row and row["critical_errors"] is not None:
            breakdown, critical_10pt, term_from_critical = parse_unit_critical_errors(
                row["critical_errors"], where
            )
        else:
            breakdown = {kind: 0 for kind in CRITICAL_BREAKDOWN_TYPES}
            critical_10pt = 0
            term_from_critical = 0

        has_name_lists = "names" in row or "community_names" in row
        minor_from_names = misleading_from_names = 0
        for key in ("names", "community_names"):
            if key in row and row[key] is not None:
                m_cnt, mis_cnt = parse_unit_name_list(row[key], f"{where}: {key}")
                minor_from_names += m_cnt
                misleading_from_names += mis_cnt

        if "minor_term_errors" in row and row["minor_term_errors"] is not None:
            if has_name_lists:
                fail(where, "cannot combine minor_term_errors with names/community_names")
            minor_term_errors = get_int(row, "minor_term_errors", where, minimum=0)
        else:
            minor_term_errors = minor_from_names

        if "misleading_term_errors" in row and row["misleading_term_errors"] is not None:
            if has_name_lists or term_from_critical > 0:
                fail(
                    where,
                    "cannot combine misleading_term_errors with names/community_names or term critical_errors",
                )
            misleading_term_errors = get_int(row, "misleading_term_errors", where, minimum=0)
        else:
            if term_from_critical > 0 and misleading_from_names > 0:
                fail(where, "cannot combine term critical_errors with misleading names")
            misleading_term_errors = term_from_critical + misleading_from_names

        breakdown["term"] = misleading_term_errors

        if (critical_10pt > 0 or misleading_term_errors > 0) and grade != "C":
            fail(where, "a critical error or a misleading term requires grade C")
        if grade == "A" and naturalness != "natural":
            fail(where, "grade A requires naturalness natural")
        if grade == "B" and naturalness == "disfluent":
            fail(where, "grade B is not allowed with naturalness disfluent")

        post_edit_distance: float | None = None
        if row.get("post_edit_distance") is not None:
            post_edit_distance = get_non_neg_float(row, "post_edit_distance", where)
        elif row.get("translation") is not None and row.get("post_edited_reference") is not None:
            translation = get_str(row, "translation", where, empty=True)
            post_ref = get_str(row, "post_edited_reference", where, empty=True)
            post_edit_distance = normalized_post_edit_distance(translation, post_ref)

        mqm_points = compute_mqm_points(
            grade,
            naturalness,
            critical_10pt,
            minor_term_errors,
            misleading_term_errors,
        )
        units[unit_id] = ScoredUnit(
            unit_id=unit_id,
            doc_id=doc_id,
            direction=direction,
            genre=genre,
            source_scalars=source_scalars,
            run_failed=False,
            grade=grade,
            naturalness=naturalness,
            critical_breakdown=breakdown,
            critical_10pt_count=critical_10pt,
            minor_term_errors=minor_term_errors,
            misleading_term_errors=misleading_term_errors,
            mqm_points=mqm_points,
            post_edit_distance=post_edit_distance,
        )

    return ArmScores(arm=arm, units=units, telemetry=telemetry)


def pair_units(
    arm_a: ArmScores, arm_b: ArmScores
) -> dict[str, list[tuple[ScoredUnit, ScoredUnit]]]:
    ids_a = set(arm_a.units)
    ids_b = set(arm_b.units)
    if ids_a != ids_b:
        only_a = len(ids_a - ids_b)
        only_b = len(ids_b - ids_a)
        fail(
            "paired-compare",
            f"scored unit ids do not match 1:1 between arm-a and arm-b "
            f"(only_in_arm_a={only_a}, only_in_arm_b={only_b})",
        )
    if not ids_a:
        fail("paired-compare", "no scored units to compare")

    by_direction: dict[str, list[tuple[ScoredUnit, ScoredUnit]]] = {
        direction: [] for direction in PAIRED_DIRECTIONS
    }
    doc_genres: dict[tuple[str, str], str] = {}

    for unit_id in sorted(ids_a):
        ua = arm_a.units[unit_id]
        ub = arm_b.units[unit_id]
        if (ua.doc_id, ua.direction, ua.genre, ua.source_scalars) != (
            ub.doc_id,
            ub.direction,
            ub.genre,
            ub.source_scalars,
        ):
            fail("paired-compare", f"unit {unit_id!r} metadata mismatch between arm-a and arm-b")
        if (not ua.run_failed and not ub.run_failed) and (
            (ua.post_edit_distance is None) != (ub.post_edit_distance is None)
        ):
            fail(
                "paired-compare",
                f"unit {unit_id!r} has mismatched post_edit_distance presence between arm-a and arm-b",
            )
        doc_key = (ua.direction, ua.doc_id)
        prev_genre = doc_genres.get(doc_key)
        if prev_genre is not None and prev_genre != ua.genre:
            fail(
                "paired-compare",
                f"doc_id {ua.doc_id!r} in direction {ua.direction} has conflicting genres",
            )
        doc_genres[doc_key] = ua.genre
        by_direction[ua.direction].append((ua, ub))

    return by_direction


def clean_zero(val: float) -> float:
    return 0.0 if val == 0.0 else val


def percentile(sorted_vals: Sequence[float], q: float) -> float:
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return clean_zero(float(sorted_vals[lo]))
    frac = pos - lo
    return clean_zero(float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac))


def bootstrap_ci(vals: list[float]) -> list[float] | None:
    if not vals:
        return None
    ordered = sorted(vals)
    return [percentile(ordered, 0.025), percentile(ordered, 0.975)]


def verdict_from_ci(ci: list[float] | None) -> str | None:
    if ci is None:
        return None
    if ci[0] > 0.0:
        return "gain"
    if ci[1] < 0.0:
        return "worse"
    return "indistinguishable"


def single_arm_summary(units: list[ScoredUnit]) -> dict[str, Any]:
    n = len(units)
    documents = len({u.doc_id for u in units})
    failed = sum(1 for u in units if u.run_failed)
    non_failed = [u for u in units if not u.run_failed]
    grades = Counter(u.grade for u in non_failed if u.grade is not None)
    a_count = grades["A"]
    usable = a_count + grades["B"]
    a_interval = wilson(a_count, n)
    usable_interval = wilson(usable, n)
    scalars = sum(u.source_scalars for u in non_failed)

    categories = {
        kind: sum(u.critical_breakdown[kind] for u in non_failed)
        for kind in CRITICAL_BREAKDOWN_TYPES
    }
    crit_total = sum(categories.values())
    with_crit = sum(1 for u in non_failed if sum(u.critical_breakdown.values()) > 0)
    mqm_total = sum(u.mqm_points for u in non_failed)

    genres: dict[str, Any] = {}
    for genre in GENRES:
        g_units = [u for u in units if u.genre == genre]
        g_n = len(g_units)
        g_a = sum(1 for u in g_units if not u.run_failed and u.grade == "A")
        g_u = sum(1 for u in g_units if not u.run_failed and u.grade in ("A", "B"))
        g_a_ci = wilson(g_a, g_n)
        g_u_ci = wilson(g_u, g_n)
        genres[genre] = {
            "n": g_n,
            "documents": len({u.doc_id for u in g_units}),
            "a_count": g_a,
            "a_rate": ratio(g_a, g_n),
            "a_wilson95": list(g_a_ci) if g_a_ci else None,
            "usable": g_u,
            "usable_rate": ratio(g_u, g_n),
            "usable_wilson95": list(g_u_ci) if g_u_ci else None,
            "run_failed": sum(1 for u in g_units if u.run_failed),
        }

    return {
        "n": n,
        "documents": documents,
        "a_count": a_count,
        "a_rate": ratio(a_count, n),
        "a_wilson95": list(a_interval) if a_interval else None,
        "usable": usable,
        "usable_rate": ratio(usable, n),
        "usable_wilson95": list(usable_interval) if usable_interval else None,
        "grades": {grade: grades[grade] for grade in GRADES},
        "run_failed": failed,
        "critical_errors": {
            "total": crit_total,
            "units_with_critical": with_crit,
            "source_scalars": scalars,
            "per_1000_source_scalars": (crit_total * 1000 / scalars) if scalars > 0 else None,
            "categories": categories,
        },
        "mqm": {
            "total_points": mqm_total,
            "source_scalars": scalars,
            "per_1000_source_scalars": (mqm_total * 1000 / scalars) if scalars > 0 else None,
            "run_failed_count": failed,
        },
        "genres": genres,
    }


def stratified_cluster_bootstrap(
    pairs: list[tuple[ScoredUnit, ScoredUnit]],
    bootstrap_samples: int,
    seed: int,
) -> dict[str, list[float] | None]:
    """Stratified by genre, resampling doc_id clusters with replacement within one direction."""
    # Pre-aggregate per document:
    # (n_units, a_A, a_B, u_A, u_B, crit_A, crit_B, sc_A, sc_B, mqm_A, mqm_B)
    by_genre_doc: dict[str, dict[str, list[int]]] = {genre: {} for genre in GENRES}
    pe_by_genre_doc: dict[str, dict[str, list[float]]] = {genre: {} for genre in GENRES}

    for ua, ub in pairs:
        vec = by_genre_doc[ua.genre].setdefault(ua.doc_id, [0] * 11)
        vec[0] += 1
        if not ua.run_failed:
            if ua.grade == "A":
                vec[1] += 1
            if ua.grade in ("A", "B"):
                vec[3] += 1
            vec[5] += sum(ua.critical_breakdown.values())
            vec[7] += ua.source_scalars
            vec[9] += ua.mqm_points
        if not ub.run_failed:
            if ub.grade == "A":
                vec[2] += 1
            if ub.grade in ("A", "B"):
                vec[4] += 1
            vec[6] += sum(ub.critical_breakdown.values())
            vec[8] += ub.source_scalars
            vec[10] += ub.mqm_points

        if (
            not ua.run_failed
            and not ub.run_failed
            and ua.post_edit_distance is not None
            and ub.post_edit_distance is not None
        ):
            pe_vec = pe_by_genre_doc[ua.genre].setdefault(ua.doc_id, [0.0, 0.0, 0.0])
            pe_vec[0] += 1.0
            pe_vec[1] += ua.post_edit_distance
            pe_vec[2] += ub.post_edit_distance

    strata: list[list[tuple[int, ...]]] = []
    for genre in GENRES:
        docs = by_genre_doc[genre]
        if docs:
            strata.append([tuple(docs[doc_id]) for doc_id in sorted(docs)])

    rng = random.Random(seed)
    delta_a_reps: list[float] = []
    delta_u_reps: list[float] = []
    crit_total_red_reps: list[float] = []
    crit_rate_red_reps: list[float] = []
    mqm_rate_diff_reps: list[float] = []
    mqm_rate_red_reps: list[float] = []

    for _ in range(bootstrap_samples):
        n_tot = a_a = a_b = u_a = u_b = c_a = c_b = s_a = s_b = m_a = m_b = 0
        for stratum in strata:
            for doc in rng.choices(stratum, k=len(stratum)):
                n_tot += doc[0]
                a_a += doc[1]
                a_b += doc[2]
                u_a += doc[3]
                u_b += doc[4]
                c_a += doc[5]
                c_b += doc[6]
                s_a += doc[7]
                s_b += doc[8]
                m_a += doc[9]
                m_b += doc[10]
        delta_a_reps.append((a_b - a_a) / n_tot)
        delta_u_reps.append((u_b - u_a) / n_tot)
        crit_total_red_reps.append(float(c_a - c_b))
        if s_a > 0 and s_b > 0:
            crit_rate_red_reps.append(clean_zero((c_a * 1000.0 / s_a) - (c_b * 1000.0 / s_b)))
            mqm_diff = clean_zero((m_b * 1000.0 / s_b) - (m_a * 1000.0 / s_a))
            mqm_rate_diff_reps.append(mqm_diff)
            mqm_rate_red_reps.append(clean_zero(-mqm_diff))

    pe_strata: list[list[tuple[float, float, float]]] = []
    for genre in GENRES:
        pe_docs = pe_by_genre_doc[genre]
        if pe_docs:
            pe_strata.append(
                [(pe_docs[d][0], pe_docs[d][1], pe_docs[d][2]) for d in sorted(pe_docs)]
            )

    pe_diff_reps: list[float] = []
    pe_red_reps: list[float] = []
    if pe_strata:
        pe_rng = random.Random(seed ^ 0x517CC1B7)
        for _ in range(bootstrap_samples):
            cnt = sum_a = sum_b = 0.0
            for stratum in pe_strata:
                for doc_pe in pe_rng.choices(stratum, k=len(stratum)):
                    cnt += doc_pe[0]
                    sum_a += doc_pe[1]
                    sum_b += doc_pe[2]
            diff = clean_zero((sum_b - sum_a) / cnt)
            pe_diff_reps.append(diff)
            pe_red_reps.append(clean_zero(-diff))

    return {
        "delta_a_ci95": bootstrap_ci(delta_a_reps),
        "delta_usable_ci95": bootstrap_ci(delta_u_reps),
        "crit_total_reduction_ci95": bootstrap_ci(crit_total_red_reps),
        "crit_per_1000_reduction_ci95": bootstrap_ci(crit_rate_red_reps),
        "mqm_per_1000_diff_ci95": bootstrap_ci(mqm_rate_diff_reps),
        "mqm_per_1000_reduction_ci95": bootstrap_ci(mqm_rate_red_reps),
        "post_edit_diff_ci95": bootstrap_ci(pe_diff_reps),
        "post_edit_reduction_ci95": bootstrap_ci(pe_red_reps),
    }


def unit_cost_entry(
    net_gain: int,
    ci95: list[float] | None,
    extra: dict[str, float | int | None],
    gain_key: str,
) -> dict[str, Any]:
    significant = ci95 is not None and ci95[0] > 0.0 and net_gain > 0
    if not significant:
        return {
            "computable": False,
            "status": NOT_COMPUTABLE_DELTA_NS,
            gain_key: net_gain,
            "posts": NOT_COMPUTABLE_DELTA_NS,
            "tokens": NOT_COMPUTABLE_DELTA_NS,
            "latency_seconds": NOT_COMPUTABLE_DELTA_NS,
            "cost": NOT_COMPUTABLE_DELTA_NS,
        }
    return {
        "computable": True,
        "status": "computable",
        gain_key: net_gain,
        "posts": (extra["posts"] / net_gain) if extra["posts"] is not None else None,
        "tokens": (extra["tokens"] / net_gain) if extra["tokens"] is not None else None,
        "latency_seconds": (
            (extra["latency_seconds"] / net_gain)
            if extra["latency_seconds"] is not None
            else None
        ),
        "cost": (extra["cost"] / net_gain) if extra["cost"] is not None else None,
    }


def compare_direction(
    direction: str,
    pairs: list[tuple[ScoredUnit, ScoredUnit]],
    tel_a: ArmTelemetry | None,
    tel_b: ArmTelemetry | None,
    bootstrap_samples: int,
    seed: int,
) -> dict[str, Any]:
    if not pairs:
        return {
            "n": 0,
            "documents": 0,
            "arms": {"arm_a": single_arm_summary([]), "arm_b": single_arm_summary([])},
            "a_rate": None,
            "usable_rate": None,
            "critical_errors": None,
            "mqm_cost": None,
            "post_edit_distance": None,
            "unit_improvement_cost": None,
        }

    units_a = [ua for ua, _ in pairs]
    units_b = [ub for _, ub in pairs]
    sum_a = single_arm_summary(units_a)
    sum_b = single_arm_summary(units_b)
    n = len(pairs)

    a_wins = a_losses = a_ties_both = a_ties_neither = 0
    u_wins = u_losses = u_ties_both = u_ties_neither = 0
    for ua, ub in pairs:
        is_a_a = not ua.run_failed and ua.grade == "A"
        is_a_b = not ub.run_failed and ub.grade == "A"
        if is_a_b and not is_a_a:
            a_wins += 1
        elif is_a_a and not is_a_b:
            a_losses += 1
        elif is_a_a and is_a_b:
            a_ties_both += 1
        else:
            a_ties_neither += 1

        is_u_a = not ua.run_failed and ua.grade in ("A", "B")
        is_u_b = not ub.run_failed and ub.grade in ("A", "B")
        if is_u_b and not is_u_a:
            u_wins += 1
        elif is_u_a and not is_u_b:
            u_losses += 1
        elif is_u_a and is_u_b:
            u_ties_both += 1
        else:
            u_ties_neither += 1

    dir_seed = seed if direction == "zh->en" else (seed + 1_000_003)
    boot = stratified_cluster_bootstrap(pairs, bootstrap_samples, dir_seed)

    delta_a = sum_b["a_rate"] - sum_a["a_rate"]
    net_a = sum_b["a_count"] - sum_a["a_count"]
    delta_u = sum_b["usable_rate"] - sum_a["usable_rate"]
    net_u = sum_b["usable"] - sum_a["usable"]

    crit_a = sum_a["critical_errors"]
    crit_b = sum_b["critical_errors"]
    crit_net_total = crit_a["total"] - crit_b["total"]
    crit_rate_a = crit_a["per_1000_source_scalars"]
    crit_rate_b = crit_b["per_1000_source_scalars"]
    crit_rate_red = (
        (crit_rate_a - crit_rate_b)
        if crit_rate_a is not None and crit_rate_b is not None
        else None
    )
    crit_cats = {
        kind: {
            "arm_a": crit_a["categories"][kind],
            "arm_b": crit_b["categories"][kind],
            "net_reduction": crit_a["categories"][kind] - crit_b["categories"][kind],
        }
        for kind in CRITICAL_BREAKDOWN_TYPES
    }

    mqm_a = sum_a["mqm"]
    mqm_b = sum_b["mqm"]
    mqm_diff_total = mqm_b["total_points"] - mqm_a["total_points"]
    mqm_red_total = -mqm_diff_total
    mqm_rate_a = mqm_a["per_1000_source_scalars"]
    mqm_rate_b = mqm_b["per_1000_source_scalars"]
    mqm_rate_diff = (
        clean_zero(mqm_rate_b - mqm_rate_a)
        if mqm_rate_a is not None and mqm_rate_b is not None
        else None
    )
    mqm_rate_red = clean_zero(-mqm_rate_diff) if mqm_rate_diff is not None else None

    pe_pairs = [
        (ua.post_edit_distance, ub.post_edit_distance)
        for ua, ub in pairs
        if (
            not ua.run_failed
            and not ub.run_failed
            and ua.post_edit_distance is not None
            and ub.post_edit_distance is not None
        )
    ]
    if pe_pairs:
        pe_a_mean = sum(a for a, _ in pe_pairs) / len(pe_pairs)
        pe_b_mean = sum(b for _, b in pe_pairs) / len(pe_pairs)
        pe_block: dict[str, Any] = {
            "units_evaluated": len(pe_pairs),
            "arm_a_mean": pe_a_mean,
            "arm_b_mean": pe_b_mean,
            "delta_b_minus_a": clean_zero(pe_b_mean - pe_a_mean),
            "delta_b_minus_a_ci95": boot["post_edit_diff_ci95"],
            "net_reduction": clean_zero(pe_a_mean - pe_b_mean),
            "net_reduction_ci95": boot["post_edit_reduction_ci95"],
        }
    else:
        pe_block = {
            "units_evaluated": 0,
            "arm_a_mean": None,
            "arm_b_mean": None,
            "delta_b_minus_a": None,
            "delta_b_minus_a_ci95": None,
            "net_reduction": None,
            "net_reduction_ci95": None,
        }

    def extra_diff(field: str) -> float | int | None:
        if tel_a is None or tel_b is None:
            return None
        va = getattr(tel_a, field)
        vb = getattr(tel_b, field)
        if va is None or vb is None:
            return None
        return vb - va

    extra = {
        "posts": extra_diff("posts"),
        "tokens": extra_diff("tokens"),
        "latency_seconds": extra_diff("latency_seconds"),
        "cost": extra_diff("cost"),
    }

    return {
        "n": n,
        "documents": sum_a["documents"],
        "arms": {"arm_a": sum_a, "arm_b": sum_b},
        "a_rate": {
            "arm_a": sum_a["a_rate"],
            "arm_b": sum_b["a_rate"],
            "delta": delta_a,
            "net_units": net_a,
            "ci95": boot["delta_a_ci95"],
            "wins": a_wins,
            "losses": a_losses,
            "ties": a_ties_both + a_ties_neither,
            "ties_both_a": a_ties_both,
            "ties_both_not_a": a_ties_neither,
            "verdict": verdict_from_ci(boot["delta_a_ci95"]),
        },
        "usable_rate": {
            "arm_a": sum_a["usable_rate"],
            "arm_b": sum_b["usable_rate"],
            "delta": delta_u,
            "net_units": net_u,
            "ci95": boot["delta_usable_ci95"],
            "wins": u_wins,
            "losses": u_losses,
            "ties": u_ties_both + u_ties_neither,
            "ties_both_usable": u_ties_both,
            "ties_both_not_usable": u_ties_neither,
            "verdict": verdict_from_ci(boot["delta_usable_ci95"]),
        },
        "critical_errors": {
            "arm_a": crit_a,
            "arm_b": crit_b,
            "net_reduction_total": crit_net_total,
            "net_reduction_total_ci95": boot["crit_total_reduction_ci95"],
            "per_1000_source_scalars_net_reduction": crit_rate_red,
            "per_1000_source_scalars_net_reduction_ci95": boot["crit_per_1000_reduction_ci95"],
            "categories": crit_cats,
            "verdict": verdict_from_ci(boot["crit_per_1000_reduction_ci95"]),
        },
        "mqm_cost": {
            "weights": dict(MQM_WEIGHTS),
            "arm_a": {
                **mqm_a,
                "run_failed_note": "max_unit_penalty (scored separately)",
            },
            "arm_b": {
                **mqm_b,
                "run_failed_note": "max_unit_penalty (scored separately)",
            },
            "delta_total_points": mqm_diff_total,
            "net_reduction_total_points": mqm_red_total,
            "per_1000_source_scalars_diff": mqm_rate_diff,
            "per_1000_source_scalars_diff_ci95": boot["mqm_per_1000_diff_ci95"],
            "per_1000_source_scalars_net_reduction": mqm_rate_red,
            "per_1000_source_scalars_net_reduction_ci95": boot["mqm_per_1000_reduction_ci95"],
            "run_failed_diff": mqm_b["run_failed_count"] - mqm_a["run_failed_count"],
        },
        "post_edit_distance": pe_block,
        "unit_improvement_cost": {
            "extra_telemetry": extra,
            "per_net_a_unit": unit_cost_entry(
                net_a, boot["delta_a_ci95"], extra, "net_a_units"
            ),
            "per_net_critical_error_reduction": unit_cost_entry(
                crit_net_total,
                boot["crit_per_1000_reduction_ci95"],
                extra,
                "net_critical_error_reduction",
            ),
        },
    }


def cmd_paired_compare(args: argparse.Namespace) -> int:
    if args.bootstrap_samples < 1:
        fail("--bootstrap-samples", "must be at least 1")
    arm_a = load_arm_scores(args.arm_a)
    arm_b = load_arm_scores(args.arm_b)
    by_direction = pair_units(arm_a, arm_b)

    directions_report = {
        direction: compare_direction(
            direction,
            by_direction[direction],
            arm_a.telemetry.get(direction),
            arm_b.telemetry.get(direction),
            args.bootstrap_samples,
            args.seed,
        )
        for direction in PAIRED_DIRECTIONS
    }
    report = {
        "schema": PAIRED_COMPARE_SCHEMA,
        "arm_a": arm_a.arm,
        "arm_b": arm_b.arm,
        "seed": args.seed,
        "bootstrap_samples": args.bootstrap_samples,
        "directions": directions_report,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.json_out:
        try:
            Path(args.json_out).write_bytes(text.encode("utf-8"))
        except OSError:
            fail(args.json_out, "cannot write report")
    else:
        sys.stdout.write(text)
    return EXIT_OK


# ------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval_usability.py", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    score = commands.add_parser("score", help="score the judgments into usability-report-v1")
    score.add_argument("--sample", action="append", required=True, metavar="SAMPLE.json")
    score.add_argument("--names", action="append", required=True, metavar="NAMES.json")
    score.add_argument("--items", action="append", required=True, metavar="ITEMS.json")
    score.add_argument("--judgments", action="extend", nargs="+", required=True, metavar="FILE")
    score.add_argument("--out", metavar="REPORT.json", help="write the report here, not to stdout")
    score.set_defaults(func=cmd_score)

    scan = commands.add_parser("scan", help="count sealed 12-scalar windows found in an input")
    scan.add_argument("--sealed", action="append", required=True, metavar="SAMPLE.json")
    scan.add_argument("--input", action="append", metavar="PATH")
    scan.add_argument("--diff", action="append", metavar="PATH")
    scan.add_argument("--input-sample", action="append", metavar="SAMPLE.json")
    scan.add_argument("--clear", action="append", metavar="PATH")
    scan.add_argument("--clear-git", action="append", metavar="REPO:COMMIT")
    scan.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    scan.add_argument(
        "--audit-out",
        metavar="PATH",
        help="write one JSON line per uncleared window hit (hash and location, no text) here",
    )
    scan.set_defaults(func=cmd_scan)

    paired = commands.add_parser(
        "paired-compare",
        help="compare two scored arms 1:1 per direction with cluster bootstrap CIs",
    )
    paired.add_argument("--arm-a", required=True, metavar="ARM_A.json")
    paired.add_argument("--arm-b", required=True, metavar="ARM_B.json")
    paired.add_argument("--seed", type=int, default=DEFAULT_SEED)
    paired.add_argument(
        "--bootstrap-samples",
        type=int,
        default=DEFAULT_BOOTSTRAP_SAMPLES,
        help="number of stratified cluster bootstrap replicates (default: 10000)",
    )
    paired.add_argument(
        "--json-out",
        metavar="REPORT.json",
        help="write the paired comparison JSON here, not to stdout",
    )
    paired.set_defaults(func=cmd_paired_compare)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main())
