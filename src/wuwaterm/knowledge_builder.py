"""Build the companion versioned entity knowledge base (``knowledge.db``).

Reads WutheringData BinData configs and MultiText tables, filters deterministic
dirty/placeholder strings, cleans rich-text markup, extracts official
``<te href=TermId>`` anchors and foreign-key relations, and writes an atomic
SQLite companion database bound to ``terms_db_revision``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import SourceProfile, get_source_profile
from .data_source import SourceProvenance, inspect_data_source
from .domain_knowledge import TrustTier
from .knowledge_db import (
    KB_SCHEMA_VERSION,
    compute_terms_db_revision,
    initialize_knowledge_db,
)
from .normalize import CJK_RE, LATIN_RE, TAG_RE, normalize_text


class KnowledgeBuildError(RuntimeError):
    """Raised when ``knowledge.db`` extraction or persistence fails."""


_SPACE_RE = re.compile(r"\s+")
_TE_ANCHOR_RE = re.compile(
    r"<te\s+href\s*=\s*['\"]?(\d+)['\"]?[^>]*>(.*?)</te>",
    re.IGNORECASE | re.DOTALL,
)
_WORD_TBA_RE = re.compile(r"\b(?:tba|tbd)\b", re.IGNORECASE)
_DIRTY_SUBSTRINGS = (
    "dnt/",
    "存在异常，如有疑问请联系官方客服",
    "存在异常,如有疑问请联系官方客服",
    "请联系官方客服",
    "敬请期待",
    "测试文本",
    "test1-",
    "prefabtextitem_empty",
)
_DIRTY_EXACT_FOLDED = frozenset(
    {
        "tba",
        "tbd",
        "todo",
        "none",
        "null",
        "placeholder",
        "test",
        "测试",
        "暂缺",
        "待定",
    }
)
_MAX_GLOSS_SCALARS = 240


@dataclass(frozen=True)
class EntitySpec:
    entity_id: str
    entity_type: str
    source_file: str
    source_id: str
    zh: str
    en: str
    trust_tier: TrustTier = "official_authoritative"
    context_gloss: str = ""


@dataclass(frozen=True)
class AliasSpec:
    entity_id: str
    alias_surface: str
    lang: str
    alias_kind: str
    trust_tier: TrustTier = "official_authoritative"


@dataclass(frozen=True)
class ContextSpec:
    entity_id: str
    context_kind: str
    source_file: str
    text_key: str
    zh_text: str
    en_text: str


@dataclass(frozen=True)
class RelationSpec:
    src_entity_id: str
    relation_type: str
    dst_entity_id: str
    source_file: str
    evidence_key: str = ""


@dataclass(frozen=True)
class KnowledgeBuildStats:
    entities: int
    aliases: int
    contexts: int
    relations: int
    short_ngrams: int
    fts_rows: int


def is_dirty_text(raw: str | None) -> bool:
    """Return True when ``raw`` is empty, placeholder, or developer-only noise."""
    if not raw or not isinstance(raw, str):
        return True
    stripped = raw.strip()
    if not stripped:
        return True
    if any(marker in stripped.casefold() for marker in _DIRTY_SUBSTRINGS):
        return True
    no_tags = _SPACE_RE.sub(" ", TAG_RE.sub("", stripped).replace("\\n", " ")).strip()
    if not no_tags or no_tags.casefold() in _DIRTY_EXACT_FOLDED:
        return True
    return _WORD_TBA_RE.search(no_tags) is not None


def clean_kb_text(raw: str | None) -> str:
    """Strip rich-text tags (``<color=...>``, ``<te ...>``) and reject dirty text."""
    if raw is None or is_dirty_text(raw):
        return ""
    cleaned = _SPACE_RE.sub(" ", TAG_RE.sub("", raw).replace("\\n", " ")).strip()
    return "" if is_dirty_text(cleaned) else cleaned


def extract_te_anchors(raw: str | None) -> list[tuple[str, str]]:
    """Extract ``(term_id, cleaned_anchor_surface)`` pairs from ``<te href=...>`` tags."""
    if not raw or "<te" not in raw.casefold() or is_dirty_text(raw):
        return []
    anchors: list[tuple[str, str]] = []
    for match in _TE_ANCHOR_RE.finditer(raw):
        term_id = match.group(1).strip()
        if not term_id or term_id == "0":
            continue
        surface = clean_kb_text(match.group(2))
        if surface:
            anchors.append((term_id, surface))
    return anchors


def _detect_lang(text: str) -> str:
    if CJK_RE.search(text):
        return "zh"
    if LATIN_RE.search(text):
        return "en"
    return "other"


def _valid_id_str(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    text = str(value).strip()
    return text if text and text != "0" else None


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _iter_valid_rows(
    path: Path, id_field: str
) -> Iterator[tuple[str, dict[str, Any]]]:
    if not path.is_file():
        return
    raw = _load_json(path)
    if not isinstance(raw, list):
        raise KnowledgeBuildError(f"expected JSON array in {path}")
    for row in raw:
        if not isinstance(row, dict):
            continue
        sid = _valid_id_str(row.get(id_field))
        if sid is not None:
            yield sid, row


def _load_raw_textmaps(data_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    arikatsu_zh = data_dir / "Textmaps" / "zh-Hans" / "multi_text" / "MultiText.json"
    arikatsu_en = data_dir / "Textmaps" / "en" / "multi_text" / "MultiText.json"
    if arikatsu_zh.is_file() and arikatsu_en.is_file():
        return _parse_arikatsu_raw(arikatsu_zh), _parse_arikatsu_raw(arikatsu_en)

    legacy_zh = data_dir / "TextMap" / "zh-Hans" / "MultiText.json"
    legacy_en = data_dir / "TextMap" / "en" / "MultiText.json"
    if legacy_zh.is_file() and legacy_en.is_file():
        zh_obj = _load_json(legacy_zh)
        en_obj = _load_json(legacy_en)
        if not isinstance(zh_obj, dict) or not isinstance(en_obj, dict):
            raise KnowledgeBuildError("legacy MultiText.json must be JSON objects")
        return (
            {str(k): str(v) for k, v in zh_obj.items()},
            {str(k): str(v) for k, v in en_obj.items()},
        )

    raise KnowledgeBuildError(f"missing MultiText files under {data_dir}")


def _parse_arikatsu_raw(path: Path) -> dict[str, str]:
    raw = _load_json(path)
    if not isinstance(raw, list):
        raise KnowledgeBuildError(f"expected JSON array in {path}")
    return {
        str(row["Id"]): str(row["Content"])
        for row in raw
        if isinstance(row, dict) and "Id" in row and "Content" in row
    }


def _format_gloss(zh_text: str, en_text: str) -> str:
    combined = " | ".join(part for part in (zh_text, en_text) if part)
    return combined[:_MAX_GLOSS_SCALARS].rstrip() if len(combined) > _MAX_GLOSS_SCALARS else combined


def _clean_pair(
    text_key: Any,
    zh_raw_map: dict[str, str],
    en_raw_map: dict[str, str],
) -> tuple[str, str, str, str]:
    if not isinstance(text_key, str) or not text_key:
        return "", "", "", ""
    zh_raw = zh_raw_map.get(text_key, "")
    en_raw = en_raw_map.get(text_key, "")
    return clean_kb_text(zh_raw), clean_kb_text(en_raw), zh_raw, en_raw


def extract_knowledge_records(
    data_dir: str | Path,
) -> tuple[
    list[EntitySpec],
    list[AliasSpec],
    list[ContextSpec],
    list[RelationSpec],
]:
    """Extract entities, aliases, bilingual contexts and FK relations from WutheringData."""
    root = Path(data_dir)
    bin_root = root / "BinData"
    if not bin_root.is_dir():
        raise KnowledgeBuildError(f"missing BinData directory: {bin_root}")

    zh_raw_map, en_raw_map = _load_raw_textmaps(root)

    entities: dict[str, EntitySpec] = {}
    aliases: list[AliasSpec] = []
    contexts: list[ContextSpec] = []
    relations: list[RelationSpec] = []
    anchor_sources: list[tuple[str | None, str, str, str, str]] = []

    def add_entity(spec: EntitySpec) -> None:
        prev = entities.get(spec.entity_id)
        if prev is None or (not prev.context_gloss and spec.context_gloss):
            entities[spec.entity_id] = spec

    def add_context_and_anchors(
        *,
        entity_id: str,
        context_kind: str,
        source_file: str,
        text_key: Any,
    ) -> str:
        zh_clean, en_clean, zh_raw, en_raw = _clean_pair(
            text_key, zh_raw_map, en_raw_map
        )
        if not isinstance(text_key, str) or (not zh_clean and not en_clean):
            return ""
        contexts.append(
            ContextSpec(
                entity_id=entity_id,
                context_kind=context_kind,
                source_file=source_file,
                text_key=text_key,
                zh_text=zh_clean,
                en_text=en_clean,
            )
        )
        anchor_sources.append((entity_id, source_file, text_key, zh_raw, en_raw))
        return _format_gloss(zh_clean, en_clean)

    def add_fk_relation(
        src_entity_id: str,
        relation_type: str,
        dst_prefix: str,
        raw_target: Any,
        source_file: str,
        evidence_prefix: str,
    ) -> None:
        tid = _valid_id_str(raw_target)
        if tid is not None and f"{dst_prefix}:{tid}" != src_entity_id:
            relations.append(
                RelationSpec(
                    src_entity_id=src_entity_id,
                    relation_type=relation_type,
                    dst_entity_id=f"{dst_prefix}:{tid}",
                    source_file=source_file,
                    evidence_key=f"{evidence_prefix}:{tid}",
                )
            )

    def add_fk_list(
        src_entity_id: str,
        relation_type: str,
        dst_prefix: str,
        raw_targets: Any,
        source_file: str,
        evidence_prefix: str,
    ) -> None:
        if isinstance(raw_targets, list):
            for item in raw_targets:
                add_fk_relation(
                    src_entity_id,
                    relation_type,
                    dst_prefix,
                    item,
                    source_file,
                    evidence_prefix,
                )

    def add_bilingual_entity(
        *,
        entity_id: str,
        entity_type: str,
        source_file: str,
        source_id: str,
        name_key: Any,
        context_specs: tuple[tuple[str, Any], ...] = (),
        fallback_zh: Any = None,
    ) -> tuple[str, str] | None:
        zh, en, _, _ = _clean_pair(name_key, zh_raw_map, en_raw_map)
        if not zh and isinstance(fallback_zh, str):
            zh = clean_kb_text(fallback_zh)
        if not zh or not en:
            return None
        gloss = ""
        for ctx_kind, ctx_key in context_specs:
            next_gloss = add_context_and_anchors(
                entity_id=entity_id,
                context_kind=ctx_kind,
                source_file=source_file,
                text_key=ctx_key,
            )
            if not gloss and next_gloss:
                gloss = next_gloss
        add_entity(
            EntitySpec(
                entity_id=entity_id,
                entity_type=entity_type,
                source_file=source_file,
                source_id=source_id,
                zh=zh,
                en=en,
                context_gloss=gloss,
            )
        )
        return zh, en

    # 1. termconfig.json
    for sid, row in _iter_valid_rows(bin_root / "Term" / "termconfig.json", "Id"):
        entity_id = f"term:{sid}"
        pair = add_bilingual_entity(
            entity_id=entity_id,
            entity_type="term",
            source_file="BinData/Term/termconfig.json",
            source_id=sid,
            name_key=f"Term{sid}_Title",
            context_specs=(("term_desc", f"Term{sid}_Desc"),),
            fallback_zh=row.get("Term"),
        )
        if pair is None:
            continue
        raw_term = row.get("Term")
        if isinstance(raw_term, str):
            cleaned_term = clean_kb_text(raw_term)
            if cleaned_term and cleaned_term != pair[0]:
                aliases.append(
                    AliasSpec(
                        entity_id=entity_id,
                        alias_surface=cleaned_term,
                        lang=_detect_lang(cleaned_term),
                        alias_kind="term_config_name",
                    )
                )

    # 2. elementinfo.json
    for sid, row in _iter_valid_rows(
        bin_root / "element_info" / "elementinfo.json", "Id"
    ):
        add_bilingual_entity(
            entity_id=f"element:{sid}",
            entity_type="element",
            source_file="BinData/element_info/elementinfo.json",
            source_id=sid,
            name_key=row.get("Name"),
            context_specs=(("element_desc", row.get("Describe")),),
        )

    # 3. influence.json
    for sid, row in _iter_valid_rows(bin_root / "influence" / "influence.json", "Id"):
        add_bilingual_entity(
            entity_id=f"influence:{sid}",
            entity_type="influence",
            source_file="BinData/influence/influence.json",
            source_id=sid,
            name_key=row.get("Title") or f"Influence_{sid}_Title",
            context_specs=(
                ("influence_intro", row.get("Introduction") or f"Influence_{sid}_Introduction"),
            ),
        )

    # 4. country.json
    for sid, row in _iter_valid_rows(bin_root / "influence" / "country.json", "Id"):
        entity_id = f"country:{sid}"
        if (
            add_bilingual_entity(
                entity_id=entity_id,
                entity_type="country",
                source_file="BinData/influence/country.json",
                source_id=sid,
                name_key=row.get("Title") or f"Country_{sid}_Title",
                context_specs=(("country_desc", row.get("Desc") or f"Country_{sid}_Desc"),),
            )
            is not None
        ):
            add_fk_list(
                entity_id,
                "contains_influence",
                "influence",
                row.get("Influences"),
                "BinData/influence/country.json",
                f"country:{sid}:influence",
            )

    # 5. area.json
    for sid, row in _iter_valid_rows(bin_root / "area" / "area.json", "AreaId"):
        entity_id = f"area:{sid}"
        if (
            add_bilingual_entity(
                entity_id=entity_id,
                entity_type="area",
                source_file="BinData/area/area.json",
                source_id=sid,
                name_key=row.get("Title"),
            )
            is not None
        ):
            add_fk_relation(
                entity_id,
                "in_country",
                "country",
                row.get("CountryId"),
                "BinData/area/area.json",
                f"area:{sid}:country",
            )
            add_fk_relation(
                entity_id,
                "child_of_area",
                "area",
                row.get("Father"),
                "BinData/area/area.json",
                f"area:{sid}:father",
            )

    # 6. roletag.json
    for sid, row in _iter_valid_rows(bin_root / "role" / "roletag.json", "Id"):
        add_bilingual_entity(
            entity_id=f"role_tag:{sid}",
            entity_type="role_tag",
            source_file="BinData/role/roletag.json",
            source_id=sid,
            name_key=row.get("TagName"),
            context_specs=(("role_tag_desc", row.get("TagDesc")),),
        )

    # 7. roleinfo.json
    for sid, row in _iter_valid_rows(bin_root / "role" / "roleinfo.json", "Id"):
        entity_id = f"resonator:{sid}"
        pair = add_bilingual_entity(
            entity_id=entity_id,
            entity_type="resonator",
            source_file="BinData/role/roleinfo.json",
            source_id=sid,
            name_key=row.get("Name"),
            context_specs=(("introduction", row.get("Introduction")),),
        )
        if pair is None:
            continue
        nick_zh, nick_en, _, _ = _clean_pair(
            row.get("NickName"), zh_raw_map, en_raw_map
        )
        for surface, canonical in ((nick_zh, pair[0]), (nick_en, pair[1])):
            if surface and surface != canonical:
                aliases.append(
                    AliasSpec(
                        entity_id=entity_id,
                        alias_surface=surface,
                        lang=_detect_lang(surface),
                        alias_kind="nickname",
                    )
                )
        add_fk_relation(
            entity_id,
            "has_element",
            "element",
            row.get("ElementId"),
            "BinData/role/roleinfo.json",
            f"role:{sid}:element",
        )
        add_fk_list(
            entity_id,
            "has_role_tag",
            "role_tag",
            row.get("Tag"),
            "BinData/role/roleinfo.json",
            f"role:{sid}:tag",
        )
        add_fk_relation(
            entity_id,
            "belongs_to_influence",
            "influence",
            row.get("PartyId"),
            "BinData/role/roleinfo.json",
            f"role:{sid}:party",
        )
        add_fk_relation(
            entity_id,
            "uses_weapon",
            "weapon",
            row.get("InitWeaponItemId"),
            "BinData/role/roleinfo.json",
            f"role:{sid}:weapon",
        )

    # 8. resonantchain.json
    for sid, row in _iter_valid_rows(
        bin_root / "resonate_chain" / "resonantchain.json", "Id"
    ):
        entity_id = f"resonant_chain:{sid}"
        if (
            add_bilingual_entity(
                entity_id=entity_id,
                entity_type="resonant_chain",
                source_file="BinData/resonate_chain/resonantchain.json",
                source_id=sid,
                name_key=row.get("NodeName"),
                context_specs=(("chain_effect", row.get("AttributesDescription")),),
            )
            is not None
        ):
            add_fk_relation(
                entity_id,
                "chain_of",
                "resonator",
                row.get("GroupId"),
                "BinData/resonate_chain/resonantchain.json",
                f"chain:{sid}:role",
            )

    # 9. skill.json & skilldescription.json
    skill_level_group_to_entity: dict[str, str] = {}
    for sid, row in _iter_valid_rows(bin_root / "skill" / "skill.json", "Id"):
        entity_id = f"skill:{sid}"
        if (
            add_bilingual_entity(
                entity_id=entity_id,
                entity_type="skill",
                source_file="BinData/skill/skill.json",
                source_id=sid,
                name_key=row.get("SkillName"),
                context_specs=(
                    ("skill_describe", row.get("SkillDescribe")),
                    ("skill_resume", row.get("SkillResume")),
                ),
            )
            is None
        ):
            continue
        slg = _valid_id_str(row.get("SkillLevelGroupId"))
        if slg is not None:
            skill_level_group_to_entity.setdefault(slg, entity_id)
        add_fk_relation(
            entity_id,
            "skill_of",
            "resonator",
            row.get("SkillGroupId"),
            "BinData/skill/skill.json",
            f"skill:{sid}:role",
        )

    for _sid, row in _iter_valid_rows(
        bin_root / "skill" / "skilldescription.json", "Id"
    ):
        slg = _valid_id_str(row.get("SkillLevelGroupId"))
        owner_entity = skill_level_group_to_entity.get(slg) if slg else None
        if owner_entity is None:
            continue
        add_context_and_anchors(
            entity_id=owner_entity,
            context_kind="skill_attribute",
            source_file="BinData/skill/skilldescription.json",
            text_key=row.get("AttributeName"),
        )
        add_context_and_anchors(
            entity_id=owner_entity,
            context_kind="skill_attribute_desc",
            source_file="BinData/skill/skilldescription.json",
            text_key=row.get("Description"),
        )

    # 10. weaponconf.json & weaponreson.json
    reson_id_to_weapon: dict[str, str] = {}
    for sid, row in _iter_valid_rows(bin_root / "weapon" / "weaponconf.json", "ItemId"):
        entity_id = f"weapon:{sid}"
        if (
            add_bilingual_entity(
                entity_id=entity_id,
                entity_type="weapon",
                source_file="BinData/weapon/weaponconf.json",
                source_id=sid,
                name_key=row.get("WeaponName"),
                context_specs=(
                    ("weapon_desc", row.get("Desc")),
                    ("weapon_bg", row.get("BgDescription")),
                ),
            )
            is None
        ):
            continue
        reson_id = _valid_id_str(row.get("ResonId"))
        if reson_id is not None:
            reson_id_to_weapon[reson_id] = entity_id

    for _sid, row in _iter_valid_rows(bin_root / "weapon" / "weaponreson.json", "Id"):
        reson_id = _valid_id_str(row.get("ResonId"))
        if reson_id is None:
            continue
        entity_id = f"weapon_reson:{reson_id}"
        if (
            add_bilingual_entity(
                entity_id=entity_id,
                entity_type="weapon_passive",
                source_file="BinData/weapon/weaponreson.json",
                source_id=reson_id,
                name_key=row.get("Name"),
            )
            is not None
            and reson_id in reson_id_to_weapon
        ):
            relations.append(
                RelationSpec(
                    src_entity_id=entity_id,
                    relation_type="passive_of",
                    dst_entity_id=reson_id_to_weapon[reson_id],
                    source_file="BinData/weapon/weaponreson.json",
                    evidence_key=f"weapon_reson:{reson_id}:weapon:{reson_id_to_weapon[reson_id]}",
                )
            )

    # 11. phantomfetter.json & phantomskill.json
    for sid, row in _iter_valid_rows(
        bin_root / "phantom" / "phantomfetter.json", "Id"
    ):
        add_bilingual_entity(
            entity_id=f"sonata_fetter:{sid}",
            entity_type="sonata_fetter",
            source_file="BinData/phantom/phantomfetter.json",
            source_id=sid,
            name_key=row.get("Name"),
            context_specs=(
                ("fetter_effect", row.get("EffectDescription")),
                ("fetter_simple", row.get("SimplyEffectDesc")),
            ),
        )

    for sid, row in _iter_valid_rows(
        bin_root / "phantom" / "phantomskill.json", "PhantomSkillId"
    ):
        entity_id = f"echo_skill:{sid}"
        desc_zh, desc_en, _, _ = _clean_pair(
            row.get("SimplyDescription"), zh_raw_map, en_raw_map
        )
        if not desc_zh or not desc_en:
            desc_zh, desc_en, _, _ = _clean_pair(
                row.get("DescriptionEx"), zh_raw_map, en_raw_map
            )
        if not desc_zh or not desc_en:
            continue
        gloss = add_context_and_anchors(
            entity_id=entity_id,
            context_kind="echo_skill_desc",
            source_file="BinData/phantom/phantomskill.json",
            text_key=row.get("DescriptionEx"),
        ) or add_context_and_anchors(
            entity_id=entity_id,
            context_kind="echo_skill_simple",
            source_file="BinData/phantom/phantomskill.json",
            text_key=row.get("SimplyDescription"),
        )
        add_entity(
            EntitySpec(
                entity_id=entity_id,
                entity_type="echo_skill",
                source_file="BinData/phantom/phantomskill.json",
                source_id=sid,
                zh=desc_zh[:80],
                en=desc_en[:120],
                trust_tier="official_derived",
                context_gloss=gloss,
            )
        )

    # 12. monsterinfo.json
    for sid, row in _iter_valid_rows(
        bin_root / "monster_Info" / "monsterinfo.json", "Id"
    ):
        entity_id = f"monster:{sid}"
        if (
            add_bilingual_entity(
                entity_id=entity_id,
                entity_type="monster",
                source_file="BinData/monster_Info/monsterinfo.json",
                source_id=sid,
                name_key=row.get("Name"),
                context_specs=(
                    ("monster_desc", row.get("DiscoveredDes")),
                    ("monster_undiscovered", row.get("UndiscoveredDes")),
                ),
            )
            is not None
        ):
            add_fk_list(
                entity_id,
                "has_element",
                "element",
                row.get("ElementIdArray"),
                "BinData/monster_Info/monsterinfo.json",
                f"monster:{sid}:element",
            )
            add_fk_list(
                entity_id,
                "inhabits_area",
                "area",
                row.get("PerchId"),
                "BinData/monster_Info/monsterinfo.json",
                f"monster:{sid}:area",
            )

    # 13. Process official <te href=TermId> anchors across contexts + standalone MultiText
    seen_context_keys: set[str] = set()
    for src_entity_id, source_file, text_key, zh_raw, en_raw in anchor_sources:
        seen_context_keys.add(text_key)
        for lang_code, raw_text in (("zh", zh_raw), ("en", en_raw)):
            for term_id, surface in extract_te_anchors(raw_text):
                target_id = f"term:{term_id}"
                if target_id not in entities:
                    continue
                aliases.append(
                    AliasSpec(
                        entity_id=target_id,
                        alias_surface=surface,
                        lang=lang_code,
                        alias_kind="te_anchor",
                    )
                )
                if src_entity_id is not None and src_entity_id != target_id:
                    relations.append(
                        RelationSpec(
                            src_entity_id=src_entity_id,
                            relation_type="mentions_term",
                            dst_entity_id=target_id,
                            source_file=source_file,
                            evidence_key=f"{text_key}:{term_id}",
                        )
                    )

    for text_key in sorted(zh_raw_map):
        if text_key in seen_context_keys:
            continue
        zh_raw = zh_raw_map[text_key]
        if "<te" not in zh_raw.casefold():
            continue
        en_raw = en_raw_map.get(text_key, "")
        for lang_code, raw_text in (("zh", zh_raw), ("en", en_raw)):
            for term_id, surface in extract_te_anchors(raw_text):
                target_id = f"term:{term_id}"
                if target_id in entities:
                    aliases.append(
                        AliasSpec(
                            entity_id=target_id,
                            alias_surface=surface,
                            lang=lang_code,
                            alias_kind="te_anchor",
                        )
                    )

    entity_list = sorted(entities.values(), key=lambda e: e.entity_id)
    valid_ids = set(entities)
    return (
        entity_list,
        [a for a in aliases if a.entity_id in valid_ids],
        [c for c in contexts if c.entity_id in valid_ids],
        [r for r in relations if r.src_entity_id in valid_ids and r.dst_entity_id in valid_ids],
    )


def create_knowledge_database(
    db_path: str | Path,
    *,
    entities: Iterable[EntitySpec],
    aliases: Iterable[AliasSpec],
    contexts: Iterable[ContextSpec],
    relations: Iterable[RelationSpec],
    terms_db_revision: str,
    source_provenance: SourceProvenance,
) -> KnowledgeBuildStats:
    """Create a deterministic ``knowledge.db`` from extracted specs."""
    if not terms_db_revision or not terms_db_revision.strip():
        raise KnowledgeBuildError("terms_db_revision is required")

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    entity_by_id: dict[str, EntitySpec] = {}
    for spec in sorted(entities, key=lambda item: item.entity_id):
        entity_by_id.setdefault(spec.entity_id, spec)
    if not entity_by_id:
        raise KnowledgeBuildError("no knowledge entities were extracted")

    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        initialize_knowledge_db(conn)
        conn.execute("PRAGMA foreign_keys = ON;")

        metadata = {
            "kb_schema_version": KB_SCHEMA_VERSION,
            "terms_db_revision": terms_db_revision.strip(),
            "source_profile": source_provenance.profile,
            "source_repo_url": source_provenance.repo_url,
            "source_commit": source_provenance.commit,
            "source_game_version": source_provenance.game_version,
            "source_resource_version": source_provenance.resource_version,
            "source_changelist": source_provenance.changelist,
        }
        conn.executemany(
            "INSERT INTO kb_metadata(key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )

        entity_rows = [
            (
                spec.entity_id,
                spec.entity_type,
                spec.source_file,
                spec.source_id,
                spec.zh,
                spec.en,
                normalize_text(spec.zh),
                normalize_text(spec.en),
                spec.trust_tier,
                spec.context_gloss,
            )
            for spec in entity_by_id.values()
        ]
        conn.executemany(
            """
            INSERT INTO kb_entities(
              entity_id, entity_type, source_file, source_id, zh, en,
              zh_norm, en_norm, trust_tier, context_gloss
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            entity_rows,
        )

        alias_rows: list[tuple[str, str, str, str, str, str, int]] = []
        seen_aliases: set[tuple[str, str, str, str]] = set()
        for alias in sorted(
            aliases,
            key=lambda a: (a.entity_id, a.alias_surface, a.lang, a.alias_kind),
        ):
            norm = normalize_text(alias.alias_surface)
            dedup_key = (alias.entity_id, alias.alias_surface, alias.lang, alias.alias_kind)
            if alias.entity_id not in entity_by_id or not norm or dedup_key in seen_aliases:
                continue
            seen_aliases.add(dedup_key)
            alias_rows.append(
                (
                    alias.entity_id,
                    alias.alias_surface,
                    norm,
                    alias.lang,
                    alias.alias_kind,
                    alias.trust_tier,
                    0,
                )
            )
        conn.executemany(
            """
            INSERT INTO kb_aliases(
              entity_id, alias_surface, alias_norm, lang, alias_kind,
              trust_tier, lock_eligible
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            alias_rows,
        )

        context_rows: list[tuple[str, str, str, str, str, str]] = []
        seen_contexts: set[tuple[str, str, str]] = set()
        for ctx in sorted(
            contexts,
            key=lambda c: (c.entity_id, c.context_kind, c.text_key),
        ):
            ctx_key = (ctx.entity_id, ctx.context_kind, ctx.text_key)
            if ctx.entity_id not in entity_by_id or ctx_key in seen_contexts:
                continue
            seen_contexts.add(ctx_key)
            context_rows.append(
                (
                    ctx.entity_id,
                    ctx.context_kind,
                    ctx.source_file,
                    ctx.text_key,
                    ctx.zh_text,
                    ctx.en_text,
                )
            )
        conn.executemany(
            """
            INSERT INTO kb_contexts(
              entity_id, context_kind, source_file, text_key, zh_text, en_text
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            context_rows,
        )

        relation_rows: list[tuple[str, str, str, str, str]] = []
        seen_relations: set[tuple[str, str, str, str]] = set()
        for rel in sorted(
            relations,
            key=lambda r: (
                r.src_entity_id,
                r.relation_type,
                r.dst_entity_id,
                r.evidence_key,
            ),
        ):
            rel_key = (
                rel.src_entity_id,
                rel.relation_type,
                rel.dst_entity_id,
                rel.evidence_key,
            )
            if (
                rel.src_entity_id not in entity_by_id
                or rel.dst_entity_id not in entity_by_id
                or rel_key in seen_relations
            ):
                continue
            seen_relations.add(rel_key)
            relation_rows.append(
                (
                    rel.src_entity_id,
                    rel.relation_type,
                    rel.dst_entity_id,
                    rel.source_file,
                    rel.evidence_key,
                )
            )
        conn.executemany(
            """
            INSERT INTO kb_relations(
              src_entity_id, relation_type, dst_entity_id, source_file, evidence_key
            ) VALUES (?, ?, ?, ?, ?)
            """,
            relation_rows,
        )

        short_rows: list[tuple[str, int, str, str, str]] = []
        seen_short: set[tuple[str, str, str]] = set()
        fts_rows: list[tuple[str, str, str, str, str]] = []
        seen_fts: set[tuple[str, str]] = set()

        def index_surface(entity: EntitySpec, surface: str, lang: str) -> None:
            norm = normalize_text(surface)
            if not norm:
                return
            if 1 <= len(norm) <= 2:
                skey = (norm, entity.entity_id, surface)
                if skey not in seen_short:
                    seen_short.add(skey)
                    short_rows.append((norm, len(norm), entity.entity_id, surface, lang))
            if len(norm) >= 3 or len(surface.strip()) >= 3:
                fkey = (entity.entity_id, surface)
                if fkey not in seen_fts:
                    seen_fts.add(fkey)
                    fts_rows.append(
                        (
                            entity.entity_id,
                            surface,
                            entity.zh,
                            entity.en,
                            entity.context_gloss,
                        )
                    )

        for spec in entity_by_id.values():
            index_surface(spec, spec.zh, _detect_lang(spec.zh))
            index_surface(spec, spec.en, _detect_lang(spec.en))
        for entity_id, alias_surface, _norm, lang, *_ in alias_rows:
            index_surface(entity_by_id[entity_id], alias_surface, lang)

        conn.executemany(
            """
            INSERT INTO kb_short_ngrams(ngram, ngram_len, entity_id, surface, lang)
            VALUES (?, ?, ?, ?, ?)
            """,
            short_rows,
        )
        conn.executemany(
            """
            INSERT INTO kb_fts_trigram(entity_id, surface, zh, en, context_gloss)
            VALUES (?, ?, ?, ?, ?)
            """,
            fts_rows,
        )

        conn.commit()
        conn.execute("VACUUM")
        return KnowledgeBuildStats(
            entities=len(entity_rows),
            aliases=len(alias_rows),
            contexts=len(context_rows),
            relations=len(relation_rows),
            short_ngrams=len(short_rows),
            fts_rows=len(fts_rows),
        )
    finally:
        conn.close()


def _profile_for_data_dir(
    data_dir: Path, profile_name: str | None = None
) -> SourceProfile:
    if profile_name is not None:
        return get_source_profile(profile_name)
    if (data_dir / "Textmaps").exists():
        return get_source_profile("arikatsu")
    if (data_dir / "ConfigDB").exists():
        return get_source_profile("dimbreath_legacy")
    return get_source_profile()


def build_knowledge_database(
    data_dir: str | Path,
    db_path: str | Path,
    *,
    terms_db_path: str | Path | None = None,
    terms_db_revision: str | None = None,
    profile_name: str | None = None,
    source_provenance: SourceProvenance | None = None,
) -> KnowledgeBuildStats:
    """Build ``knowledge.db`` from a WutheringData directory."""
    root = Path(data_dir)
    if terms_db_revision is None:
        if terms_db_path is None:
            raise KnowledgeBuildError(
                "either terms_db_path or terms_db_revision is required"
            )
        resolved_revision = compute_terms_db_revision(terms_db_path)
    else:
        resolved_revision = terms_db_revision

    provenance = (
        source_provenance
        if source_provenance is not None
        else inspect_data_source(root, _profile_for_data_dir(root, profile_name).name)
    )
    entities, aliases, contexts, relations = extract_knowledge_records(root)
    return create_knowledge_database(
        db_path,
        entities=entities,
        aliases=aliases,
        contexts=contexts,
        relations=relations,
        terms_db_revision=resolved_revision,
        source_provenance=provenance,
    )


def build_knowledge_database_atomic(
    data_dir: str | Path,
    db_path: str | Path,
    *,
    terms_db_path: str | Path | None = None,
    terms_db_revision: str | None = None,
    profile_name: str | None = None,
    source_provenance: SourceProvenance | None = None,
) -> KnowledgeBuildStats:
    """Build ``knowledge.db`` in the target directory, then atomically replace it."""
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        stats = build_knowledge_database(
            data_dir,
            tmp_path,
            terms_db_path=terms_db_path,
            terms_db_revision=terms_db_revision,
            profile_name=profile_name,
            source_provenance=source_provenance,
        )
        os.replace(tmp_path, target)
        return stats
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
