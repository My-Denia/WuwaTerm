"""Build a term-only SQLite dictionary from WutheringData entity configs."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import (
    CORE_TERM_KEYS,
    FREE_TEXT_LOCK_EXCLUSIONS_KEY,
    FREE_TEXT_LOCK_EXCLUSIONS_VERSION,
    FREE_TEXT_LOCK_MAX_ALIGNED_RATIO,
    FREE_TEXT_LOCK_MIN_OCCURRENCES,
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY,
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_VERSION,
    FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO,
    FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES,
    SourceProfile,
    get_source_profile,
)
from .data_source import inspect_data_source
from .db import connect, create_database
from .models import TermRecord
from .normalize import clean_source_text


@dataclass(frozen=True)
class CategorySpec:
    category: str
    config_file: str
    id_field: str
    text_fields: tuple[str, ...]


CATEGORY_SPECS = (
    CategorySpec("resonator", "RoleInfo.json", "Id", ("Name",)),
    CategorySpec("weapon", "WeaponConf.json", "ItemId", ("WeaponName",)),
    CategorySpec("echo", "PhantomItem.json", "ItemId", ("MonsterName",)),
    CategorySpec("item", "ItemInfo.json", "Id", ("Name",)),
    CategorySpec("skill", "Skill.json", "Id", ("SkillName",)),
    CategorySpec("sonata_effect", "PhantomFetterGroup.json", "Id", ("FetterGroupName",)),
    CategorySpec("location", "Area.json", "AreaId", ("Title",)),
)

ARIKATSU_CORE_TEXT_IDS = frozenset(CORE_TERM_KEYS)

ARIKATSU_BIN_SPECS = (
    CategorySpec("resonator", "role/roleinfo.json", "Id", ("Name",)),
    CategorySpec("weapon", "weapon/weaponconf.json", "ItemId", ("WeaponName",)),
    CategorySpec("echo", "phantom/phantomitem.json", "ItemId", ("MonsterName",)),
    CategorySpec("item", "item/iteminfo.json", "Id", ("Name",)),
    CategorySpec("skill", "skill/skill.json", "Id", ("SkillName",)),
    CategorySpec("sonata_effect", "phantom/phantomfettergroup.json", "Id", ("FetterGroupName",)),
    CategorySpec("location", "area/area.json", "AreaId", ("Title",)),
)


class BuildError(RuntimeError):
    pass


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_multitext(data_dir: Path, lang: str) -> dict[str, str]:
    path = data_dir / "TextMap" / lang / "MultiText.json"
    if not path.exists():
        raise BuildError(f"missing TextMap file: {path}")
    raw = load_json(path)
    if not isinstance(raw, dict):
        raise BuildError(f"expected object in {path}")
    return {str(k): str(v) for k, v in raw.items()}


def load_arikatsu_string_texts(data_dir: Path, lang: str) -> dict[str, str]:
    path = data_dir / "Textmaps" / lang / "multi_text" / "MultiText.json"
    if not path.exists():
        raise BuildError(f"missing Arikatsu multi_text file: {path}")
    raw = load_json(path)
    if not isinstance(raw, list):
        raise BuildError(f"expected array in {path}")
    mapping: dict[str, str] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        if "Id" not in row or "Content" not in row:
            continue
        text = clean_source_text(str(row["Content"]))
        if text:
            mapping[str(row["Id"])] = text
    return mapping


def _record_for_key(
    *,
    category: str,
    source_file: str,
    source_id: str,
    text_key: str,
    zh_map: dict[str, str],
    en_map: dict[str, str],
) -> TermRecord | None:
    zh_raw = zh_map.get(text_key)
    en_raw = en_map.get(text_key)
    if zh_raw is None or en_raw is None:
        return None
    zh = clean_source_text(zh_raw)
    en = clean_source_text(en_raw)
    if not zh or not en:
        return None
    return TermRecord(
        category=category,
        source_file=source_file,
        source_id=source_id,
        text_key=text_key,
        zh=zh,
        en=en,
    )


def source_profile_for_data_dir(data_dir: str | Path, profile_name: str | None = None) -> SourceProfile:
    root = Path(data_dir)
    if profile_name is not None:
        return get_source_profile(profile_name)
    if (root / "Textmaps").exists():
        return get_source_profile("arikatsu")
    if (root / "ConfigDB").exists():
        return get_source_profile("dimbreath_legacy")
    return get_source_profile()


TextMaps = tuple[dict[str, str], dict[str, str]]


def iter_records(
    data_dir: str | Path,
    profile_name: str | None = None,
    loaded_text_maps: list[TextMaps] | None = None,
) -> list[TermRecord]:
    """Extract term records; append the (zh, en) text maps read to
    ``loaded_text_maps`` when given, so the caller can reuse them."""
    profile = source_profile_for_data_dir(data_dir, profile_name)
    if profile.layout == "arikatsu_textmaps":
        return iter_arikatsu_records(data_dir, loaded_text_maps)
    return iter_dimbreath_records(data_dir, loaded_text_maps)


def iter_arikatsu_records(
    data_dir: str | Path, loaded_text_maps: list[TextMaps] | None = None
) -> list[TermRecord]:
    root = Path(data_dir)
    bin_root = root / "BinData"
    if not bin_root.exists():
        raise BuildError(f"missing BinData directory: {bin_root}")
    zh_map = load_arikatsu_string_texts(root, "zh-Hans")
    en_map = load_arikatsu_string_texts(root, "en")
    if loaded_text_maps is not None:
        loaded_text_maps.append((zh_map, en_map))
    records: list[TermRecord] = []

    for text_key, category in CORE_TERM_KEYS.items():
        record = _record_for_key(
            category=category,
            source_file="Textmaps/multi_text/MultiText.json",
            source_id=text_key,
            text_key=text_key,
            zh_map=zh_map,
            en_map=en_map,
        )
        if record:
            records.append(record)

    for spec in ARIKATSU_BIN_SPECS:
        path = bin_root / spec.config_file
        if not path.exists():
            raise BuildError(f"missing required BinData file: {path}")
        rows = load_json(path)
        if not isinstance(rows, list):
            raise BuildError(f"expected list in {path}")
        for row in rows:
            if not isinstance(row, dict):
                continue
            source_id = str(row.get(spec.id_field, ""))
            if not source_id:
                continue
            for field in spec.text_fields:
                text_key = row.get(field)
                if not isinstance(text_key, str) or not text_key:
                    continue
                record = _record_for_key(
                    category=spec.category,
                    source_file=f"BinData/{spec.config_file}",
                    source_id=source_id,
                    text_key=text_key,
                    zh_map=zh_map,
                    en_map=en_map,
                )
                if record:
                    records.append(record)

    speaker_path = bin_root / "speaker" / "speaker.json"
    if speaker_path.exists():
        rows = load_json(speaker_path)
        if not isinstance(rows, list):
            raise BuildError(f"expected list in {speaker_path}")
        for row in rows:
            if not isinstance(row, dict) or not row.get("Id"):
                continue
            source_id = str(row["Id"])
            text_key = f"Speaker_{source_id}_Name"
            record = _record_for_key(
                category="speaker",
                source_file="BinData/speaker/speaker.json",
                source_id=source_id,
                text_key=text_key,
                zh_map=zh_map,
                en_map=en_map,
            )
            if record:
                records.append(record)

    if not records:
        raise BuildError("no term records were extracted")
    return records


def iter_dimbreath_records(
    data_dir: str | Path, loaded_text_maps: list[TextMaps] | None = None
) -> list[TermRecord]:
    root = Path(data_dir)
    config_root = root / "ConfigDB"
    if not config_root.exists():
        raise BuildError(f"missing ConfigDB directory: {config_root}")

    zh_map = load_multitext(root, "zh-Hans")
    en_map = load_multitext(root, "en")
    if loaded_text_maps is not None:
        loaded_text_maps.append((zh_map, en_map))
    records: list[TermRecord] = []

    for text_key, category in CORE_TERM_KEYS.items():
        record = _record_for_key(
            category=category,
            source_file="TextMap/MultiText.json",
            source_id=text_key,
            text_key=text_key,
            zh_map=zh_map,
            en_map=en_map,
        )
        if record:
            records.append(record)

    for spec in CATEGORY_SPECS:
        path = config_root / spec.config_file
        if not path.exists():
            raise BuildError(f"missing required config file: {path}")
        rows = load_json(path)
        if not isinstance(rows, list):
            raise BuildError(f"expected list in {path}")
        for row in rows:
            if not isinstance(row, dict):
                continue
            source_id = str(row.get(spec.id_field, ""))
            if not source_id:
                continue
            for field in spec.text_fields:
                text_key = row.get(field)
                if not isinstance(text_key, str) or not text_key:
                    continue
                record = _record_for_key(
                    category=spec.category,
                    source_file=spec.config_file,
                    source_id=source_id,
                    text_key=text_key,
                    zh_map=zh_map,
                    en_map=en_map,
                )
                if record:
                    records.append(record)

    if not records:
        raise BuildError("no term records were extracted")
    return records


_ASCII_LETTER_RUN_RE = re.compile(r"[A-Za-z]+")


def _is_cjk_text(text: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in text)


def _is_latin_surface(text: str) -> bool:
    return not _is_cjk_text(text) and any(
        char.isalpha() and unicodedata.name(char, "").startswith("LATIN ")
        for char in text
    )


def measure_free_text_exclusions(
    db_path: str | Path, en_map: dict[str, str], zh_map: dict[str, str]
) -> str:
    """Return the free-text lock exclusion metadata value for a built DB.

    Every official English string with a Chinese counterpart goes through
    the runtime span selection over the exact lockable surfaces (no case
    variants, no exclusions). Each selected Latin-script surface counts one
    occurrence, and is aligned when the parallel Chinese string contains
    the locked official Chinese or the Chinese of any record carrying that
    surface. A surface is excluded when it occurs at least
    FREE_TEXT_LOCK_MIN_OCCURRENCES times and its aligned share is below
    FREE_TEXT_LOCK_MAX_ALIGNED_RATIO. The value is compact, key-sorted JSON
    with surfaces sorted, so equal inputs give equal bytes.
    """
    from .lookup import TermService
    from .sentence import exact_lockable_sources, select_term_spans

    entries = TermService(db_path).entries()
    lockable = exact_lockable_sources(entries)
    aligned_zh: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        if entry.en and entry.zh:
            aligned_zh[entry.en].add(entry.zh)
            aligned_zh[entry.zh].add(entry.zh)

    # A matching surface's longest ASCII letter run is a whole letter run of
    # the text: inner runs are bounded by the surface itself and edge runs
    # by the ASCII word-boundary check. Surfaces without such a run are
    # always tried, and text containing CJK falls back to the full set so
    # Chinese surfaces keep their place in span selection.
    by_anchor: dict[str, list[int]] = defaultdict(list)
    always: list[int] = []
    for index, (source, _official) in enumerate(lockable):
        runs = _ASCII_LETTER_RUN_RE.findall(source)
        if runs:
            by_anchor[max(runs, key=len)].append(index)
        elif not _is_cjk_text(source):
            always.append(index)

    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for text_key, text in en_map.items():
        zh_text = zh_map.get(text_key)
        if zh_text is None:
            continue
        if _is_cjk_text(text):
            candidates = lockable
        else:
            indexes = set(always)
            for run in set(_ASCII_LETTER_RUN_RE.findall(text)):
                indexes.update(by_anchor.get(run, ()))
            if not indexes:
                continue
            candidates = tuple(lockable[index] for index in sorted(indexes))
        for span in select_term_spans(text, candidates, text, 0):
            surface = span.source
            if not _is_latin_surface(surface):
                continue
            count = counts[surface]
            count[0] += 1
            if span.official[0] in zh_text or any(
                zh in zh_text for zh in aligned_zh.get(surface, ())
            ):
                count[1] += 1

    numerator, denominator = FREE_TEXT_LOCK_MAX_ALIGNED_RATIO
    surfaces = [
        [surface, occurrences, aligned]
        for surface, (occurrences, aligned) in sorted(counts.items())
        if occurrences >= FREE_TEXT_LOCK_MIN_OCCURRENCES
        and aligned * denominator < occurrences * numerator
    ]
    return json.dumps(
        {
            "version": FREE_TEXT_LOCK_EXCLUSIONS_VERSION,
            "min_occurrences": FREE_TEXT_LOCK_MIN_OCCURRENCES,
            "max_aligned_ratio": list(FREE_TEXT_LOCK_MAX_ALIGNED_RATIO),
            "surfaces": surfaces,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


# Quote characters stripped from candidate English values before the zh
# alignment check: official English often wraps a name in quotes ("Jué",
# «Rinascita», 「Jue」) while the parallel line renders it bare. The zh
# direction needs this because the English side it checks carries quoting
# the English rule's Chinese side never faced.
_ZH_ALIGNMENT_QUOTE_CHARS = "\"'“”‘’«»『』「」"


def measure_free_text_zh_exclusions(
    db_path: str | Path, zh_map: dict[str, str], en_map: dict[str, str]
) -> str:
    """Return the Chinese-surface lock exclusion metadata value for a DB.

    Every official Chinese string with an English counterpart goes through
    the runtime span selection over the exact lockable surfaces (no case
    variants, no exclusions). Each selected surface containing a CJK
    character counts one occurrence, and is aligned when the parallel
    English string, casefolded, contains the surface's official English or
    the English of any record carrying that surface — each candidate also
    tried with surrounding quote characters stripped, because case-sensitive
    and un-stripped checks were measured to wrongly exclude 99 real-term
    surfaces (黑海岸 -> The Black Shores, 「角」 -> "Jué"). A surface is
    excluded when it occurs at least FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES times
    and its aligned share is below FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO. The
    value is compact, key-sorted JSON with surfaces sorted, so equal inputs
    give equal bytes.
    """
    from .lookup import TermService
    from .sentence import exact_lockable_sources, select_term_spans

    entries = TermService(db_path).entries()
    lockable = exact_lockable_sources(entries)
    aligned_en: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        if entry.en and entry.zh:
            aligned_en[entry.zh].add(entry.en)
            aligned_en[entry.en].add(entry.en)

    # A matching surface's first character must occur in the text, so every
    # surface lives in its first character's bucket and a line only tries
    # the buckets of the characters it contains. Buckets are merged in
    # lockable order, so span selection is identical to the full set (the
    # candidate-build verification re-counts without the prefilter and
    # requires byte-identical output).
    by_first: dict[str, list[int]] = defaultdict(list)
    for index, (source, _official) in enumerate(lockable):
        by_first[source[0]].append(index)

    variant_cache: dict[str, tuple[str, ...]] = {}

    def aligned_variants(surface: str, official_en: str) -> tuple[str, ...]:
        # A given surface always locks to the same official record, so the
        # variant set depends only on the surface.
        cached = variant_cache.get(surface)
        if cached is None:
            values: set[str] = set()
            for value in aligned_en.get(surface, ()) | {official_en}:
                if not value:
                    continue
                values.add(value.casefold())
                stripped = value.strip(_ZH_ALIGNMENT_QUOTE_CHARS)
                if stripped:
                    values.add(stripped.casefold())
            cached = tuple(values)
            variant_cache[surface] = cached
        return cached

    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for text_key, text in zh_map.items():
        en_text = en_map.get(text_key)
        if en_text is None:
            continue
        indexes: set[int] = set()
        for char in set(text):
            indexes.update(by_first.get(char, ()))
        if not indexes:
            continue
        candidates = tuple(lockable[index] for index in sorted(indexes))
        en_folded = en_text.casefold()
        for span in select_term_spans(text, candidates, text, 0):
            surface = span.source
            if not _is_cjk_text(surface):
                continue
            count = counts[surface]
            count[0] += 1
            if any(value in en_folded for value in aligned_variants(surface, span.official[1])):
                count[1] += 1

    numerator, denominator = FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO
    surfaces = [
        [surface, occurrences, aligned]
        for surface, (occurrences, aligned) in sorted(counts.items())
        if occurrences >= FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES
        and aligned * denominator < occurrences * numerator
    ]
    return json.dumps(
        {
            "version": FREE_TEXT_LOCK_ZH_EXCLUSIONS_VERSION,
            "min_occurrences": FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES,
            "max_aligned_ratio": list(FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO),
            "surfaces": surfaces,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def build_database(
    data_dir: str | Path,
    db_path: str | Path,
    profile_name: str | None = None,
) -> int:
    profile = source_profile_for_data_dir(data_dir, profile_name)
    provenance = inspect_data_source(data_dir, profile.name)
    text_maps: list[TextMaps] = []
    records = iter_records(data_dir, profile.name, loaded_text_maps=text_maps)
    if inspect_data_source(data_dir, profile.name) != provenance:
        raise BuildError("source provenance changed during database extraction")
    create_database(
        db_path,
        records,
        source_profile=profile,
        source_provenance=provenance,
    )
    # Exclusions need the official corpus the records were read from;
    # create_database alone, or a records source that read no text maps,
    # writes none, and runtime then keeps every surface lockable.
    if not text_maps:
        return len(records)
    zh_map, en_map = text_maps[0]
    exclusions = measure_free_text_exclusions(db_path, en_map, zh_map)
    zh_exclusions = measure_free_text_zh_exclusions(db_path, zh_map, en_map)
    with closing(connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (FREE_TEXT_LOCK_EXCLUSIONS_KEY, exclusions),
        )
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY, zh_exclusions),
        )
        conn.commit()
    return len(records)


def build_database_atomic(
    data_dir: str | Path,
    db_path: str | Path,
    profile_name: str | None = None,
) -> int:
    """Build a database in the target directory, then atomically replace it."""
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        count = build_database(data_dir, tmp_path, profile_name=profile_name)
        os.replace(tmp_path, target)
        return count
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
