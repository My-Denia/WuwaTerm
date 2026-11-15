#!/usr/bin/env python3
"""Frozen scorer and leakage scan for the held-out usability evaluation (ADR 0020).

``score``  validate the frozen inputs and the judgment files, resolve every item
           to its final judgment (two agreeing judges, or the adjudicator) and
           write the ``usability-report-v1`` metrics, per arm and direction.
``scan``   count 12-scalar windows of the sealed sets (H1, H2) that occur in an
           input; counts only, the matched text is never printed.

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
* ``scan`` stdout is ``{"schema", "window", "rows": [...]}``.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

Z95 = 1.959963984540054
SAMPLE_SCHEMA = "usability-sample-v1"
NAMES_SCHEMA = "usability-names-v1"
ITEMS_SCHEMA = "usability-items-v1"
JUDGMENT_SCHEMA = "usability-judgment-v1"
REPORT_SCHEMA = "usability-report-v1"
SCAN_SCHEMA = "usability-scan-v1"

DIRECTIONS = ("zh-en", "en-zh")
GENRES = ("announcement", "story", "guide", "mixed")
EXCLUSIONS = ("dictionary", "no_letter", "duplicate")
PASSES = ("j1", "j2", "adj")
GRADES = ("A", "B", "C", "undecidable")
NATURALNESS = ("natural", "stiff", "disfluent", "undecidable")
ERROR_TYPES = ("omission", "addition", "meaning_shift", "reference", "number", "negation")
LABELS = ("official", "not_official", "omitted", "undecidable")
RENDERED_LABELS = ("official", "not_official")

ENDERS = "。．.！？!?\n"  # review-v2 sentence enders
DECIMAL_POINTS = ".．"
DEFAULT_WINDOW = 12

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


def load_items(path: str, paragraphs: dict[str, Paragraph], seen: set[str]) -> list[Item]:
    data = obj(load_json(path), path)
    check_schema(data, ITEMS_SCHEMA, path)
    need_keys(data, path, ("items",))
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
    return found


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


def usable_block(n: int, usable: int, grades: Counter[str], failed: int, pending: int) -> dict[str, Any]:
    interval = wilson(usable, n) if pending == 0 else None
    return {
        "n": n,
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
        in_genre = {it.item for it in cell_items if paragraphs[it.paragraph].genre == genre}
        good = sum(1 for it, j in judged if it.item in in_genre and j.grade in ("A", "B"))
        waiting = sum(
            1
            for it in cell_items
            if it.item in in_genre and not it.run_failed and finals[it.item] is None
        )
        genres[genre] = {
            "n": len(in_genre),
            "usable": good,
            "rate": ratio(good, len(in_genre)) if waiting == 0 else None,
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
        "usable": usable_block(len(cell_items), usable, grades, len(failed), pending),
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
    for path in args.items:
        for item in load_items(path, paragraphs, seen_items):
            items[item.item] = item
    if not items:
        fail("items", "no items")
    arms = sorted({item.arm for item in items.values()})
    per_slot = Counter((item.paragraph, item.arm) for item in items.values())
    for paragraph in paragraphs.values():
        for arm in arms if paragraph.scored else ():
            if per_slot[(paragraph.id, arm)] != 1:
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


def windows(text: str, size: int) -> Iterator[str]:
    for start in range(len(text) - size + 1):
        yield text[start : start + size]


def added_lines(diff: str) -> str:
    """The added lines of a unified diff, without the leading '+', joined by newline."""
    return "\n".join(
        line[1:] for line in diff.split("\n") if line.startswith("+") and not line.startswith("+++")
    )


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


def cmd_scan(args: argparse.Namespace) -> int:
    size = args.window
    if size < 1:
        fail("--window", "must be at least 1")
    sealed = [load_sample(path) for path in args.sealed]

    sources: list[InputSource] = []
    for path in args.input or ():
        sources.append(InputSource(path, [read_text(path)], None))
    for path in args.diff or ():
        sources.append(InputSource(path, [added_lines(read_text(path))], None))
    for path in args.input_sample or ():
        sample = load_sample(path)
        units: list[str] = []
        ids: list[str] = []
        for paragraph in sample.paragraphs:
            for text in (paragraph.source, paragraph.reference):
                if text is not None:
                    units.append(text)
                    ids.append(paragraph.id)
        sources.append(InputSource(sample.label, units, ids))
    if not sources:
        fail("scan", "give at least one of --input, --diff, --input-sample")

    clear_texts = [read_text(path) for path in args.clear or ()]
    for spec in args.clear_git or ():
        clear_texts.extend(git_blob_texts(spec))

    labels: list[str] = []
    paragraph_label: list[str] = []
    index: dict[str, list[int]] = {}
    for sample in sealed:
        if sample.label not in labels:
            labels.append(sample.label)
        for paragraph in sample.paragraphs:
            gid = len(paragraph_label)
            paragraph_label.append(sample.label)
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
    for source, per_unit in zip(sources, unit_hits):
        for label in labels:
            hit = {
                w
                for h in per_unit
                for w in h
                if any(paragraph_label[gid] == label for gid in index[w])
            }
            uncleared = hit - cleared
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

    print(json.dumps({"schema": SCAN_SCHEMA, "window": size, "rows": rows}, ensure_ascii=False, indent=2))
    return EXIT_SCAN_HIT if any(row["windows_uncleared"] for row in rows) else EXIT_OK


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
    scan.set_defaults(func=cmd_scan)
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
