"""Build a term-only SQLite dictionary from WutheringData entity configs."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.platform != "win32":
    import fcntl
else:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

from .constants import (
    AREA_TITLE_KEY_RE,
    ARIKATSU_MULTITEXT_SOURCE_FILE,
    CORE_TERM_KEYS,
    FREE_TEXT_LOCK_EXCLUSIONS_KEY,
    FREE_TEXT_LOCK_EXCLUSIONS_VERSION,
    FREE_TEXT_LOCK_MAX_ALIGNED_RATIO,
    FREE_TEXT_LOCK_MIN_OCCURRENCES,
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY,
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_VERSION,
    FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO,
    FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES,
    ITEM_TIER_BASE_SOURCE_PREFIX,
    OFFICIAL_ANNOUNCEMENT_SOURCE_FILE,
    OFFICIAL_ANNOUNCEMENT_TERMS,
    SourceProfile,
    get_source_profile,
)
from .data_source import inspect_data_source
from .db import connect, create_database
from .english_plural import number_variants
from .models import TermRecord
from .normalize import clean_source_text, normalize_text


@dataclass(frozen=True)
class CategorySpec:
    category: str
    config_file: str
    id_field: str
    text_fields: tuple[str, ...]
    row_filter: Callable[[dict[str, Any]], bool] | None = None


def _keep_phone_cosmetic(row: dict[str, Any]) -> bool:
    # Skip the built-in placeholder rows the client ships before any cosmetic is
    # owned: SortId == 0 marks the fixed default row, and Id % 10000 == 1 with
    # an empty ItemAccess and BgDescription is the unsold default (default
    # theme/decor/background/bubble). Named items stay, such as 80810002
    # (盘古终端 / Pangu Terminal).
    if row.get("SortId") == 0:
        return False
    row_id = row.get("Id")
    if (
        isinstance(row_id, int)
        and row_id % 10000 == 1
        and not row.get("ItemAccess")
        and not row.get("BgDescription")
    ):
        return False
    return True


def _keep_quest_main_type(row: dict[str, Any]) -> bool:
    return row.get("Id") not in (0, 100)


def _keep_item_info(row: dict[str, Any]) -> bool:
    # Skip generic everyday kitchen ingredients and condiments (ShowTypes 26
    # "Cooking Ingredient" paired with 25 "Processed Item" or deprecated 31),
    # such as Sugar, Salt, Rice, Milk, Beans, Tofu, Chili, Flour, Vinegar.
    # WuWa-specific collectibles carrying only ShowTypes [26] or [26, 44] stay.
    show_types = row.get("ShowTypes")
    if not isinstance(show_types, list):
        return True
    type_ids = {item for item in show_types if isinstance(item, int)}
    return not (26 in type_ids and (25 in type_ids or 31 in type_ids))


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

# Which BinData tables are dictionary terms at all, and which are only sources of
# ordinary English words. The four PhoneMsg cosmetic tables, the skill type
# names, the gacha view types and the quest main types name a system the game
# reuses verbatim, so they are imported whole apart from their built-in default
# rows. The remaining cosmetic tables (motor stickers and decorations, frames,
# skins, ornaments, glider skins, player heads, background cards, achievement
# groups) also carry ordinary English words as row titles: importing
# motorsticker.json added Sonic -> 索尼克 and Shadow -> 夏特, hard-locking the
# official "Imitative Sonic Device" and changing "/tr Shadow"; ornament.json
# added 无念 -> Stillmind over the speaker Wunian. Their verified individual rows
# are pinned by CORE_TERM_KEYS instead, so one table import cannot drag an
# unrelated everyday word into locking or review.
ARIKATSU_BIN_SPECS = (
    CategorySpec("resonator", "role/roleinfo.json", "Id", ("Name",)),
    CategorySpec("weapon", "weapon/weaponconf.json", "ItemId", ("WeaponName",)),
    CategorySpec("echo", "phantom/phantomitem.json", "ItemId", ("MonsterName",)),
    CategorySpec("item", "item/iteminfo.json", "Id", ("Name",), _keep_item_info),
    CategorySpec("skill", "skill/skill.json", "Id", ("SkillName",)),
    CategorySpec("sonata_effect", "phantom/phantomfettergroup.json", "Id", ("FetterGroupName",)),
    CategorySpec("location", "area/area.json", "AreaId", ("Title",)),
    CategorySpec("item", "PhoneMsg/chattheme.json", "Id", ("Name",), _keep_phone_cosmetic),
    CategorySpec("item", "PhoneMsg/chatcharm.json", "Id", ("Name",), _keep_phone_cosmetic),
    CategorySpec("item", "PhoneMsg/chatbg.json", "Id", ("Name",), _keep_phone_cosmetic),
    CategorySpec("item", "PhoneMsg/chatdialog.json", "Id", ("Name",), _keep_phone_cosmetic),
    CategorySpec("core_term", "gacha/gachaviewtypeinfo.json", "Type", ("TypeText",)),
    CategorySpec("core_term", "skill/skilltype.json", "Id", ("TypeName",)),
    CategorySpec(
        "core_term",
        "questtype/questmaintype.json",
        "Id",
        ("MainTypeName",),
        _keep_quest_main_type,
    ),
)


# item/iteminfo.json ships some materials only as four tiers: 初级X / 中级X /
# 高级X / 特级X with "Basic Y" / "Medium Y" / "Advanced Y" / "Premium Y".
# Official text also names the material itself (密音筒 / Sealed Tube), so a
# complete family adds its base pair (X, Y) as a derived item row.
ITEM_TIER_PREFIXES = (
    ("初级", "Basic "),
    ("中级", "Medium "),
    ("高级", "Advanced "),
    ("特级", "Premium "),
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


_PUNCT_DIGIT_NOISE_RE = re.compile(r"^[?？\dP]+$")
_UPPER_ACRONYM_RE = re.compile(r"[A-Z]{2,}")
_DOTTED_INITIALISM_RE = re.compile(r"(?:[A-Z0-9]+\.)+[A-Z0-9]*")
_SPEAKER_COMM_ZH_RE = re.compile(r"[（(]通讯中?[）)]$")
_SPEAKER_COMM_EN_RE = re.compile(r"\s*[（(]Contacting[）)]$")
_PRONOUN_SPEAKERS = frozenset({"我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "它们"})
_TEST_MARKER_ZH_SUBSTRINGS = ("test/", "测试用例")
_TEST_MARKER_EN_SUBSTRINGS = ("test/", "test case")
_ERROR_PLACEHOLDER_ZH_SUBSTRINGS = ("存在异常", "联系官方客服", "暂时空置")
_ERROR_PLACEHOLDER_EN_SUBSTRINGS = (
    "an error happened",
    "an error occurred",
    "contact customer service",
    "contact our customer service",
    "placeholder",
)


def _is_allowed_non_cjk_zh(zh: str) -> bool:
    """Allow official uppercase acronyms, dotted initialisms, and titled items."""
    if _UPPER_ACRONYM_RE.fullmatch(zh):
        return True
    if _DOTTED_INITIALISM_RE.fullmatch(zh) and any("A" <= ch <= "Z" for ch in zh):
        return True
    if (zh.startswith("《") and zh.endswith("》")) or (
        zh.startswith("「") and zh.endswith("」")
    ):
        inner = zh[1:-1].strip()
        return any("A" <= ch <= "Z" or "a" <= ch <= "z" for ch in inner)
    return False


def _has_speaker_comm_suffix(zh: str, en: str) -> bool:
    return bool(_SPEAKER_COMM_ZH_RE.search(zh) or _SPEAKER_COMM_EN_RE.search(en))


def _clean_speaker_text(zh: str, en: str) -> tuple[str, str]:
    """Strip speaker communication-status suffixes from both sides."""
    zh = _SPEAKER_COMM_ZH_RE.sub("", zh).strip()
    en = _SPEAKER_COMM_EN_RE.sub("", en).strip()
    return zh, en


def _keep_record(*, category: str, zh: str, en: str) -> bool:
    """Reject test markers, error placeholders, non-CJK noise, and bad speakers."""
    if not zh or not en:
        return False
    zh_cf = zh.casefold()
    en_cf = en.casefold()
    if any(marker in zh_cf for marker in _TEST_MARKER_ZH_SUBSTRINGS) or any(
        marker in en_cf for marker in _TEST_MARKER_EN_SUBSTRINGS
    ):
        return False
    if any(marker in zh for marker in _ERROR_PLACEHOLDER_ZH_SUBSTRINGS) or any(
        marker in en_cf for marker in _ERROR_PLACEHOLDER_EN_SUBSTRINGS
    ):
        return False
    if _PUNCT_DIGIT_NOISE_RE.fullmatch(zh) or _PUNCT_DIGIT_NOISE_RE.fullmatch(en):
        return False
    if not _is_cjk_text(zh) and not _is_allowed_non_cjk_zh(zh):
        return False
    if not any(char.isalpha() for char in en):
        return False
    if category == "speaker" and (
        zh in _PRONOUN_SPEAKERS or (zh == "通讯" and en == "Contacting")
    ):
        return False
    return True


def _record_for_key(
    *,
    category: str,
    source_file: str,
    source_id: str,
    text_key: str,
    zh_map: dict[str, str],
    en_map: dict[str, str],
    strip_speaker_comm: bool = False,
) -> TermRecord | None:
    zh_raw = zh_map.get(text_key)
    en_raw = en_map.get(text_key)
    if zh_raw is None or en_raw is None:
        return None
    zh = clean_source_text(zh_raw)
    en = clean_source_text(en_raw)
    if strip_speaker_comm:
        zh, en = _clean_speaker_text(zh, en)
    if not _keep_record(category=category, zh=zh, en=en):
        return None
    return TermRecord(
        category=category,
        source_file=source_file,
        source_id=source_id,
        text_key=text_key,
        zh=zh,
        en=en,
    )


def _numeric_id_key(value: str) -> tuple[int, int, str]:
    return (0, int(value), "") if value.isdigit() else (1, 0, value)


def _item_tier_base_records(
    rows: list[Any],
    zh_map: dict[str, str],
    en_map: dict[str, str],
    existing: list[TermRecord],
) -> list[TermRecord]:
    """Return one derived item row per complete four-tier item family.

    A family is the set of iteminfo rows whose cleaned names are a tier
    prefix plus the same Chinese base X and, with the matching English tier
    prefix, the same English base Y. Only a family with all four tiers names
    its base, provided no existing record already carries that same pair. A
    shared Chinese or English side paired with a different counterpart is not a
    reason to drop the family: both meanings stay as candidates, so 调谐器 /
    Tuner joins the speaker 调音师 / Tuner. The term-preparation layer resolves
    such a surface by context instead of dropping one of the meanings at build
    time. The row's source_id and text_key are ``tier-base:<id>`` with the
    smallest Basic-tier row id, so the row says it is derived and stays
    deterministic when one tier has several rows.
    """
    existing_pairs = {(record.zh, record.en) for record in existing}
    families: dict[tuple[str, str], dict[int, list[str]]] = defaultdict(dict)
    for row in rows:
        if not isinstance(row, dict) or not _keep_item_info(row):
            continue
        row_id = str(row.get("Id", ""))
        text_key = row.get("Name")
        if not row_id or not isinstance(text_key, str) or not text_key:
            continue
        zh = clean_source_text(zh_map.get(text_key, ""))
        en = clean_source_text(en_map.get(text_key, ""))
        for tier, (zh_prefix, en_prefix) in enumerate(ITEM_TIER_PREFIXES):
            if zh.startswith(zh_prefix) and en.startswith(en_prefix):
                base = (zh[len(zh_prefix) :].strip(), en[len(en_prefix) :].strip())
                if base[0] and base[1]:
                    families[base].setdefault(tier, []).append(row_id)
                break

    records: list[TermRecord] = []
    for (zh, en), tiers in sorted(families.items()):
        if (
            len(tiers) != len(ITEM_TIER_PREFIXES)
            or (zh, en) in existing_pairs
            or not _keep_record(category="item", zh=zh, en=en)
        ):
            continue
        basic_id = min(tiers[0], key=_numeric_id_key)
        source_id = f"{ITEM_TIER_BASE_SOURCE_PREFIX}{basic_id}"
        records.append(
            TermRecord(
                category="item",
                source_file="BinData/item/iteminfo.json",
                source_id=source_id,
                text_key=source_id,
                zh=zh,
                en=en,
            )
        )
    return records


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

    # The pinned Term table is the game's official glossary: 655
    # ``Term<n>_Title`` pairs. The curated CORE_TERM_KEYS covers a handful;
    # import the rest as core terms so exact lookup is not limited to those
    # hand-picked entries. The TextMap also carries the
    # ``Advertising_VersionTitle_<n>`` family, which names a release rather
    # than a glossary meaning; the builder imports it too and runtime treats
    # those rows as contextual references (term_classification.is_derived_name_entry), so
    # a title never hard-replaces ordinary prose. Other advertising and UI
    # text is not a term source. BinData still supplies its own categories.
    core_title = re.compile(r"(?:Term\d+_Title|Advertising_VersionTitle_\d+)$")
    for text_key in sorted(zh_map):
        if text_key in CORE_TERM_KEYS or not core_title.fullmatch(text_key):
            continue
        record = _record_for_key(
            category="core_term",
            source_file=ARIKATSU_MULTITEXT_SOURCE_FILE,
            source_id=text_key,
            text_key=text_key,
            zh_map=zh_map,
            en_map=en_map,
        )
        if record:
            records.append(record)

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

    bin_rows: dict[str, list[Any]] = {}
    for spec in ARIKATSU_BIN_SPECS:
        path = bin_root / spec.config_file
        if not path.exists():
            raise BuildError(f"missing required BinData file: {path}")
        rows = load_json(path)
        if not isinstance(rows, list):
            raise BuildError(f"expected list in {path}")
        bin_rows[spec.config_file] = rows
        for row in rows:
            if not isinstance(row, dict):
                continue
            if spec.row_filter is not None and not spec.row_filter(row):
                continue
            source_id = str(row.get(spec.id_field, ""))
            if not source_id:
                continue
            for field in spec.text_fields:
                raw_text_key = row.get(field)
                if not isinstance(raw_text_key, str) or not raw_text_key:
                    continue
                record = _record_for_key(
                    category=spec.category,
                    source_file=f"BinData/{spec.config_file}",
                    source_id=source_id,
                    text_key=raw_text_key,
                    zh_map=zh_map,
                    en_map=en_map,
                )
                if record:
                    records.append(record)

    # A location reaches the dictionary through an area/area.json row's Title.
    # The pinned TextMap also carries Area_<n>_Title keys that no area.json
    # row references (Area_41037_Title 拉贝尔学部 / Rabelle College and the rest
    # of the Startorch campus): those are imported directly. Like the Term
    # glossary import, no row filter applies; an empty or missing side is
    # skipped as elsewhere.
    referenced_area_titles = {
        row.get("Title") for row in bin_rows["area/area.json"] if isinstance(row, dict)
    }
    for text_key in sorted(zh_map):
        if (
            text_key in referenced_area_titles
            or text_key in CORE_TERM_KEYS
            or not AREA_TITLE_KEY_RE.fullmatch(text_key)
        ):
            continue
        record = _record_for_key(
            category="location",
            source_file=ARIKATSU_MULTITEXT_SOURCE_FILE,
            source_id=text_key,
            text_key=text_key,
            zh_map=zh_map,
            en_map=en_map,
        )
        if record:
            records.append(record)

    for zh, en, category, source_id in OFFICIAL_ANNOUNCEMENT_TERMS:
        records.append(
            TermRecord(
                category=category,
                source_file=OFFICIAL_ANNOUNCEMENT_SOURCE_FILE,
                source_id=source_id,
                text_key=source_id,
                zh=clean_source_text(zh),
                en=clean_source_text(en),
            )
        )

    speaker_path = bin_root / "speaker" / "speaker.json"
    if speaker_path.exists():
        rows = load_json(speaker_path)
        if not isinstance(rows, list):
            raise BuildError(f"expected list in {speaker_path}")
        comm_speakers: list[tuple[str, str]] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("Id"):
                continue
            source_id = str(row["Id"])
            text_key = f"Speaker_{source_id}_Name"
            zh_raw = zh_map.get(text_key)
            en_raw = en_map.get(text_key)
            if zh_raw is not None and en_raw is not None and _has_speaker_comm_suffix(
                clean_source_text(zh_raw), clean_source_text(en_raw)
            ):
                comm_speakers.append((source_id, text_key))
                continue
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

        if comm_speakers:
            # Defer suffix-stripped speakers until every non-comm record across
            # all tables is collected: drop exact (zh_norm, en_norm) duplicates
            # and noise, keep original suffixed rows on single-sided conflicts
            # (preserving Zhiyuan -> 致远, 残星会会长 -> Grand Architect, and the
            # reverse exact lookup for Fractsidus Grand Architect / 知远（通讯中）),
            # and strip the suffix only when neither side collides.
            covered_pairs = {
                (normalize_text(r.zh), normalize_text(r.en)) for r in records
            }
            covered_zh = {zh_norm for zh_norm, _ in covered_pairs}
            covered_en = {en_norm for _, en_norm in covered_pairs}
            for source_id, text_key in comm_speakers:
                cleaned = _record_for_key(
                    category="speaker",
                    source_file="BinData/speaker/speaker.json",
                    source_id=source_id,
                    text_key=text_key,
                    zh_map=zh_map,
                    en_map=en_map,
                    strip_speaker_comm=True,
                )
                if cleaned is None:
                    continue
                zh_norm = normalize_text(cleaned.zh)
                en_norm = normalize_text(cleaned.en)
                if (zh_norm, en_norm) in covered_pairs:
                    continue
                if zh_norm in covered_zh or en_norm in covered_en:
                    original = _record_for_key(
                        category="speaker",
                        source_file="BinData/speaker/speaker.json",
                        source_id=source_id,
                        text_key=text_key,
                        zh_map=zh_map,
                        en_map=en_map,
                        strip_speaker_comm=False,
                    )
                    if original is not None:
                        records.append(original)
                    continue
                covered_pairs.add((zh_norm, en_norm))
                covered_zh.add(zh_norm)
                covered_en.add(en_norm)
                records.append(cleaned)

    # Derived last, so an exact pair already present in another source is not
    # repeated. A shared surface with a different counterpart stays available
    # for contextual disambiguation instead of being dropped here.
    records.extend(
        _item_tier_base_records(bin_rows["item/iteminfo.json"], zh_map, en_map, records)
    )

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
                raw_text_key = row.get(field)
                if not isinstance(raw_text_key, str) or not raw_text_key:
                    continue
                record = _record_for_key(
                    category=spec.category,
                    source_file=spec.config_file,
                    source_id=source_id,
                    text_key=raw_text_key,
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
    from .term_matching import exact_lockable_sources, select_term_spans

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
    from .term_matching import exact_lockable_sources, select_term_spans

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
            for value in set(aligned_en.get(surface, ())) | {official_en}:
                if not value:
                    continue
                candidates = [value]
                stripped = value.strip(_ZH_ALIGNMENT_QUOTE_CHARS)
                if stripped and stripped != value:
                    candidates.append(stripped)
                for candidate in candidates:
                    values.add(candidate.casefold())
                    for variant in number_variants(candidate):
                        values.add(variant.casefold())
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


_PUBLISH_THREAD_LOCKS_GUARD = threading.Lock()
_PUBLISH_THREAD_LOCKS: dict[Path, threading.Lock] = {}


def _publish_thread_lock(lock_path: Path) -> threading.Lock:
    with _PUBLISH_THREAD_LOCKS_GUARD:
        lock = _PUBLISH_THREAD_LOCKS.get(lock_path)
        if lock is None:
            lock = threading.Lock()
            _PUBLISH_THREAD_LOCKS[lock_path] = lock
        return lock


@contextmanager
def _publish_lock(target: Path) -> Iterator[None]:
    """Serialize database publication on a dedicated sidecar lock file.

    Uses an independent inode (``.<target>.publish.lock``) so acquiring or
    releasing the lock never touches ``target``'s inode or POSIX ``fcntl``
    locks held by other SQLite connections in the same process.
    """
    lock_path = target.with_name(f".{target.name}.publish.lock")
    thread_lock = _publish_thread_lock(lock_path.resolve(strict=False))
    with thread_lock:
        if fcntl is None:  # pragma: no cover - Windows fallback
            yield
            return
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of the parent directory after ``os.replace``."""
    if sys.platform == "win32":  # pragma: no cover - Windows fallback
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _clear_sidecars_fail_closed(
    target: Path, wal_path: Path, shm_path: Path
) -> None:
    """Reject non-empty WAL sidecars and unlink only empty leftover sidecars."""
    if wal_path.exists():
        try:
            if wal_path.stat().st_size > 0:
                raise BuildError(
                    f"refusing to replace database {target}: non-empty WAL sidecar remains"
                )
            wal_path.unlink()
        except OSError as exc:
            raise BuildError(
                f"refusing to replace database {target}: could not clear {wal_path.name}: {exc}"
            ) from exc
    if shm_path.exists():
        try:
            shm_path.unlink()
        except OSError as exc:
            raise BuildError(
                f"refusing to replace database {target}: could not clear {shm_path.name}: {exc}"
            ) from exc


def _prepare_and_replace_target(tmp_path: Path, target: Path) -> None:
    """Drain WAL, hold exclusive SQLite + publish locks across ``os.replace``, and fsync.

    Never opens ``target`` with raw ``open()`` / ``os.open()`` so closing a raw
    file descriptor can never drop POSIX ``fcntl`` locks held by another
    in-process SQLite connection on ``target``'s inode.
    """
    wal_path = Path(f"{target}-wal")
    shm_path = Path(f"{target}-shm")
    with _publish_lock(target):
        if not target.exists():
            _clear_sidecars_fail_closed(target, wal_path, shm_path)
            os.replace(tmp_path, target)
            _fsync_dir(target.parent)
            return

        resolved_uri = f"{target.resolve(strict=False).as_uri()}?mode=rw"
        try:
            conn = sqlite3.connect(
                resolved_uri,
                uri=True,
                timeout=0.0,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise BuildError(
                f"refusing to replace database {target}: could not open existing target: {exc}"
            ) from exc

        try:
            is_sqlite_target = False
            try:
                conn.execute("PRAGMA locking_mode = EXCLUSIVE")
                conn.execute("BEGIN EXCLUSIVE")
                conn.execute("COMMIT")
                mode_row = conn.execute("PRAGMA journal_mode").fetchone()
                is_wal = mode_row is not None and str(mode_row[0]).lower() == "wal"
                if is_wal:
                    ckpt = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if ckpt is None:
                        raise BuildError(
                            f"refusing to replace WAL database {target}: checkpoint returned no status"
                        )
                    busy, log_frames, checkpointed = (
                        int(ckpt[0]),
                        int(ckpt[1]),
                        int(ckpt[2]),
                    )
                    if busy != 0 or log_frames < 0 or checkpointed < log_frames:
                        raise BuildError(
                            f"refusing to replace active WAL database {target}: "
                            f"wal_checkpoint(TRUNCATE) incomplete "
                            f"(busy={busy}, log={log_frames}, checkpointed={checkpointed})"
                        )
                    mode_after = conn.execute("PRAGMA journal_mode = DELETE").fetchone()
                    if mode_after is None or str(mode_after[0]).lower() != "delete":
                        raise BuildError(
                            f"refusing to replace active WAL database {target}: "
                            f"could not exit WAL mode cleanly (mode={mode_after})"
                        )
                is_sqlite_target = True
            except sqlite3.OperationalError as exc:
                raise BuildError(
                    f"refusing to replace active database {target}: {exc}"
                ) from exc
            except sqlite3.DatabaseError as exc:
                if wal_path.exists() or shm_path.exists():
                    raise BuildError(
                        f"refusing to replace database {target}: {exc}"
                    ) from exc
                # Non-SQLite placeholder file with no WAL/SHM sidecars: allow replace.

            _clear_sidecars_fail_closed(target, wal_path, shm_path)
            if is_sqlite_target:
                # Switch this connection's per-connection journal mode to MEMORY
                # before os.replace (without altering the on-disk header or releasing
                # locking_mode=EXCLUSIVE), so conn.close() after os.replace never
                # unlinks a new <target>-journal created by a writer on the replaced DB.
                try:
                    conn.execute("PRAGMA journal_mode = MEMORY")
                except sqlite3.Error as exc:
                    raise BuildError(
                        f"refusing to replace database {target}: could not isolate journal mode: {exc}"
                    ) from exc
            os.replace(tmp_path, target)
            _fsync_dir(target.parent)
        finally:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            conn.close()


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
        _prepare_and_replace_target(tmp_path, target)
        return count
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
