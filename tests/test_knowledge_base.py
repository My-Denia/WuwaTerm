from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from scripts.verify_knowledge_db import (
    VerificationError,
    main as verify_kb_main,
    verify_knowledge_database,
)
from wuwaterm.data_source import SourceProvenance
from wuwaterm.db import connect, initialize
from wuwaterm.domain_knowledge import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MAX_SCALARS,
    DomainKnowledgeCandidate,
    candidate_scalar_length,
    is_single_cjk_char,
    select_domain_knowledge_candidates,
)
from wuwaterm.knowledge_builder import (
    AliasSpec,
    EntitySpec,
    KnowledgeBuildError,
    build_knowledge_database,
    build_knowledge_database_atomic,
    clean_kb_text,
    create_knowledge_database,
    extract_knowledge_records,
    extract_te_anchors,
    is_dirty_text,
)
from wuwaterm.knowledge_db import (
    KB_SCHEMA_VERSION,
    KnowledgeBaseError,
    KnowledgeBaseReader,
    KnowledgeBaseRevisionMismatchError,
    compute_terms_db_revision,
)
from wuwaterm.lookup import TermService


def _provenance() -> SourceProvenance:
    return SourceProvenance(
        profile="arikatsu",
        repo_url="https://example.invalid/wutheringdata.git",
        commit="deadbeef1234567890",
        game_version="3.7.0",
        resource_version="3.7.8",
        changelist="8975829",
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_synthetic_terms_db(path: Path) -> str:
    with closing(connect(path)) as conn:
        initialize(conn)
        conn.execute(
            "INSERT INTO terms(category, source_file, source_id, text_key, zh, en, "
            "zh_norm, en_norm, pinyin, pinyin_abbrev, priority) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "resonator",
                "BinData/role/roleinfo.json",
                "1603",
                "RoleInfo_1603_Name",
                "椿",
                "Camellya",
                "椿",
                "camellya",
                "chun",
                "c",
                10,
            ),
        )
        conn.commit()
    return compute_terms_db_revision(path)


def _build_synthetic_wutheringdata(root: Path) -> Path:
    bin_root = root / "BinData"

    _write_json(
        bin_root / "Term" / "termconfig.json",
        [
            {"Id": 0, "Term": "无效零号术语", "Placeholder": []},
            {"Id": 850086, "Term": "夜归", "Placeholder": []},
            {"Id": 850099, "Term": "共鸣回路", "Placeholder": []},
            {"Id": 850100, "Term": "dnt/内部测试词条", "Placeholder": []},
        ],
    )
    _write_json(
        bin_root / "element_info" / "elementinfo.json",
        [
            {"Id": 0, "Name": "ElementInfo_0_Name", "Describe": ""},
            {"Id": 1, "Name": "ElementInfo_1_Name", "Describe": "ElementInfo_1_Describe"},
            {"Id": 6, "Name": "ElementInfo_6_Name", "Describe": "ElementInfo_6_Describe"},
        ],
    )
    _write_json(
        bin_root / "influence" / "influence.json",
        [
            {"Id": 0, "Title": "", "Introduction": ""},
            {"Id": 1, "Title": "", "Introduction": ""},
        ],
    )
    _write_json(
        bin_root / "influence" / "country.json",
        [
            {"Id": 0, "Influences": [], "Title": "Country_0_Title", "Desc": ""},
            {
                "Id": 1,
                "Influences": [0, 1],
                "Title": "Country_1_Title",
                "Desc": "Country_1_Desc",
            },
        ],
    )
    _write_json(
        bin_root / "area" / "area.json",
        [
            {"AreaId": 0, "CountryId": 0, "Title": "Area_0_Title", "Father": 0},
            {"AreaId": 35, "CountryId": 1, "Title": "Area_35_Title", "Father": 0},
            {"AreaId": 3, "CountryId": 1, "Title": "Area_3_Title", "Father": 35},
        ],
    )
    _write_json(
        bin_root / "role" / "roletag.json",
        [
            {"Id": 3, "TagName": "RoleTag3", "TagDesc": "RoleTag3_TagDesc"},
        ],
    )
    _write_json(
        bin_root / "weapon" / "weaponconf.json",
        [
            {
                "ItemId": 21020011,
                "WeaponName": "WeaponConf_21020011_WeaponName",
                "ResonId": 21020011,
                "Desc": "WeaponConf_21020011_Desc",
                "BgDescription": "WeaponConf_21020011_BgDescription",
            },
        ],
    )
    _write_json(
        bin_root / "weapon" / "weaponreson.json",
        [
            {"Id": 1, "ResonId": 21020011, "Level": 1, "Name": "WeaponReson_1_Name"},
            {"Id": 2, "ResonId": 21020011, "Level": 2, "Name": "WeaponReson_1_Name"},
        ],
    )
    _write_json(
        bin_root / "role" / "roleinfo.json",
        [
            {
                "Id": 0,
                "Name": "RoleInfo_0_Name",
                "NickName": "",
                "Introduction": "",
                "Tag": [],
                "ElementId": 0,
                "PartyId": 0,
                "InitWeaponItemId": 0,
            },
            {
                "Id": 1402,
                "Name": "RoleInfo_1402_Name",
                "NickName": "RoleInfo_1402_NickName",
                "Introduction": "FavorRoleInfo_1402_Info",
                "Tag": [3],
                "ElementId": 1,
                "PartyId": 1,
                "InitWeaponItemId": 21020011,
            },
            {
                "Id": 1603,
                "Name": "RoleInfo_1603_Name",
                "NickName": "RoleInfo_1603_NickName",
                "Introduction": "FavorRoleInfo_1603_Info",
                "Tag": [3],
                "ElementId": 6,
                "PartyId": 1,
                "InitWeaponItemId": 21020011,
            },
            {
                "Id": 1699,
                "Name": "RoleInfo_1699_Name",
                "NickName": "RoleInfo_1699_NickName",
                "Introduction": "",
                "Tag": [],
                "ElementId": 6,
                "PartyId": 0,
                "InitWeaponItemId": 0,
            },
            {
                "Id": 9999,
                "Name": "RoleInfo_9999_Dirty",
                "NickName": "",
                "Introduction": "",
                "Tag": [],
                "ElementId": 0,
                "PartyId": 0,
                "InitWeaponItemId": 0,
            },
        ],
    )
    _write_json(
        bin_root / "resonate_chain" / "resonantchain.json",
        [
            {
                "Id": 1,
                "GroupId": 1402,
                "NodeName": "ResonantChain_1_NodeName",
                "AttributesDescription": "ResonantChain_1_AttributesDescription",
            },
        ],
    )
    _write_json(
        bin_root / "skill" / "skill.json",
        [
            {
                "Id": 1000101,
                "SkillGroupId": 1402,
                "SkillName": "Skill_1000101_SkillName",
                "SkillLevelGroupId": 1000101,
                "SkillDescribe": "Skill_1000101_SkillDescribe",
                "SkillResume": "Skill_1000101_SkillResume",
            },
        ],
    )
    _write_json(
        bin_root / "skill" / "skilldescription.json",
        [
            {
                "Id": 900003,
                "SkillLevelGroupId": 1000101,
                "AttributeName": "SkillDescription_900003_AttributeName",
                "Description": "SkillDescription_900003_Description",
            },
        ],
    )
    _write_json(
        bin_root / "phantom" / "phantomfetter.json",
        [
            {
                "Id": 1,
                "Name": "PhantomFetter_1_Name",
                "EffectDescription": "PhantomFetter_1_EffectDescription",
                "SimplyEffectDesc": "PhantomFetter_1_SimplyEffectDesc",
            },
        ],
    )
    _write_json(
        bin_root / "phantom" / "phantomskill.json",
        [
            {
                "Id": 1,
                "PhantomSkillId": 200001,
                "DescriptionEx": "PhantomSkill_200001_DescriptionEx",
                "SimplyDescription": "PhantomSkill_200001_SimplyDescription",
            },
        ],
    )
    _write_json(
        bin_root / "monster_Info" / "monsterinfo.json",
        [
            {
                "Id": 310000010,
                "Name": "MonsterInfo_310000010_Name",
                "ElementIdArray": [1],
                "DiscoveredDes": "MonsterInfo_310000010_DiscoveredDes",
                "UndiscoveredDes": "MonsterInfo_310000010_UndiscoveredDes",
                "PerchId": [3],
            },
        ],
    )

    zh_entries = {
        "Term850086_Title": "夜归",
        "Term850086_Desc": "<color=Highlight>夜归军</color>守卫今州边疆。",
        "Term850099_Title": "共鸣回路",
        "Term850099_Desc": "释放<color=Highlight>共鸣回路</color>触发特殊机制。",
        "Term850100_Title": "dnt/不要入库",
        "ElementInfo_0_Name": "零号元素",
        "ElementInfo_1_Name": "冷凝",
        "ElementInfo_1_Describe": "存在异常，如有疑问请联系官方客服",
        "ElementInfo_6_Name": "湮灭",
        "ElementInfo_6_Describe": "能够湮灭物质频率的属性。",
        "Influence_1_Title": "先行公约",
        "Influence_1_Introduction": "先行公约是科学与探索组织。",
        "Country_0_Title": "零号国家",
        "Country_1_Title": "瑝珑",
        "Country_1_Desc": "历史悠久的东方国度。",
        "Area_0_Title": "零号区域",
        "Area_35_Title": "瑝珑中枢",
        "Area_3_Title": "今州城",
        "RoleTag3": "协奏效率",
        "RoleTag3_TagDesc": "快速积累协奏能量。",
        "WeaponConf_21020011_WeaponName": "千古洑流",
        "WeaponConf_21020011_Desc": "提升共鸣效率。",
        "WeaponConf_21020011_BgDescription": "古老的迅刀。",
        "WeaponReson_1_Name": "流光洄溯",
        "RoleInfo_0_Name": "零号角色",
        "RoleInfo_1402_Name": "秧秧",
        "RoleInfo_1402_NickName": "风声羽息",
        "FavorRoleInfo_1402_Info": "<te href=850086>夜归军团</te>下设临时调查员，秧秧。",
        "RoleInfo_1603_Name": "椿",
        "RoleInfo_1603_NickName": "存在异常，如有疑问请联系官方客服",
        "FavorRoleInfo_1603_Info": "红椿绽放于<te href=850099>共鸣核心</te>之中。",
        "RoleInfo_1699_Name": "心",
        "RoleInfo_1699_NickName": "敬请期待",
        "RoleInfo_9999_Dirty": "测试文本_待删除",
        "ResonantChain_1_NodeName": "青蓝澄澈的初风",
        "ResonantChain_1_AttributesDescription": "施放技能后提升<color=Highlight>冷凝</color>抗性。",
        "Skill_1000101_SkillName": "微风轻羽",
        "Skill_1000101_SkillDescribe": "造成连续斩击伤害。",
        "Skill_1000101_SkillResume": "基础攻击。",
        "SkillDescription_900003_AttributeName": "第一段伤害",
        "SkillDescription_900003_Description": "触发<te href=850099>回路联动</te>。",
        "PhantomFetter_1_Name": "凝夜白霜",
        "PhantomFetter_1_EffectDescription": "冷凝伤害提升10%。",
        "PhantomFetter_1_SimplyEffectDesc": "提升冷凝伤害。",
        "PhantomSkill_200001_DescriptionEx": "召唤<color=#c49e55>寒霜残象</color>发起突袭。",
        "PhantomSkill_200001_SimplyDescription": "寒霜残象突袭",
        "MonsterInfo_310000010_Name": "啸叫种残象",
        "MonsterInfo_310000010_DiscoveredDes": "盘踞于今州城外围的残象。",
        "MonsterInfo_310000010_UndiscoveredDes": "未知残象记录。",
        "Standalone_Te_Text": "关于<te href=850086>夜归先锋</te>的记载。",
        "Dirty_Test1_Key": "Test1-placeholder",
    }
    en_entries = {
        "Term850086_Title": "Midnight Rangers",
        "Term850086_Desc": "<color=Highlight>Midnight Rangers</color> guard Jinzhou.",
        "Term850099_Title": "Forte Circuit",
        "Term850099_Desc": "Release <color=Highlight>Forte Circuit</color> for special effects.",
        "Term850100_Title": "dnt/InternalOnly",
        "ElementInfo_0_Name": "Zero Element",
        "ElementInfo_1_Name": "Glacio",
        "ElementInfo_1_Describe": "The ability to cool and freeze.",
        "ElementInfo_6_Name": "Havoc",
        "ElementInfo_6_Describe": "Annihilates frequencies.",
        "Influence_1_Title": "Pioneer Association",
        "Influence_1_Introduction": "Scientific and exploration organization.",
        "Country_0_Title": "Zero Country",
        "Country_1_Title": "Huanglong",
        "Country_1_Desc": "An ancient eastern nation.",
        "Area_0_Title": "Zero Area",
        "Area_35_Title": "Huanglong Central",
        "Area_3_Title": "Jinzhou",
        "RoleTag3": "Concerto Efficiency",
        "RoleTag3_TagDesc": "Rapidly builds Concerto Energy.",
        "WeaponConf_21020011_WeaponName": "Emerald of Genesis",
        "WeaponConf_21020011_Desc": "Increases Energy Regen.",
        "WeaponConf_21020011_BgDescription": "An ancient sword.",
        "WeaponReson_1_Name": "Stormy Resolution",
        "RoleInfo_0_Name": "Zero Resonator",
        "RoleInfo_1402_Name": "Yangyang",
        "RoleInfo_1402_NickName": "Breath of Winds",
        "FavorRoleInfo_1402_Info": "Outrider of the <te href=850086>Midnight Vanguard</te>.",
        "RoleInfo_1603_Name": "Camellya",
        "RoleInfo_1603_NickName": "Camellya",
        "FavorRoleInfo_1603_Info": "Blooms inside <te href=850099>Forte Core</te>.",
        "RoleInfo_1699_Name": "Hsin",
        "RoleInfo_1699_NickName": "TBA",
        "RoleInfo_9999_Dirty": "Test1-DirtyName",
        "ResonantChain_1_NodeName": "Sapphire Skies",
        "ResonantChain_1_AttributesDescription": "Increases <color=Highlight>Glacio</color> RES.",
        "Skill_1000101_SkillName": "Feather as Blade",
        "Skill_1000101_SkillDescribe": "Performs consecutive slashes.",
        "Skill_1000101_SkillResume": "Basic attack.",
        "SkillDescription_900003_AttributeName": "Stage 1 DMG",
        "SkillDescription_900003_Description": "Triggers <te href=850099>Circuit Link</te>.",
        "PhantomFetter_1_Name": "Freezing Frost",
        "PhantomFetter_1_EffectDescription": "Glacio DMG +10%.",
        "PhantomFetter_1_SimplyEffectDesc": "Increases Glacio DMG.",
        "PhantomSkill_200001_DescriptionEx": "Summons <color=#c49e55>Frost Remnant</color> to strike.",
        "PhantomSkill_200001_SimplyDescription": "Frost Remnant Strike",
        "MonsterInfo_310000010_Name": "Howler Remnant",
        "MonsterInfo_310000010_DiscoveredDes": "Roams outside Jinzhou.",
        "MonsterInfo_310000010_UndiscoveredDes": "Unknown remnant record.",
        "Standalone_Te_Text": "Chronicle of <te href=850086>Rangers Vanguard</te>.",
        "Dirty_Test1_Key": "Test1-placeholder",
    }

    _write_json(
        root / "Textmaps" / "zh-Hans" / "multi_text" / "MultiText.json",
        [{"Id": k, "Content": v} for k, v in zh_entries.items()],
    )
    _write_json(
        root / "Textmaps" / "en" / "multi_text" / "MultiText.json",
        [{"Id": k, "Content": v} for k, v in en_entries.items()],
    )
    return root


def test_terms_db_revision_matches_lookup_identity(tmp_path: Path):
    terms_db = tmp_path / "terms.db"
    computed = _build_synthetic_terms_db(terms_db)
    identity = TermService(terms_db).dictionary_identity()
    assert computed == identity.revision


def test_kb_aliases_lock_eligible_check_constraint_and_candidate_isolation(tmp_path: Path):
    # (a) Runtime isolation on DomainKnowledgeCandidate
    with pytest.raises(ValueError, match="lock_eligible must always be False"):
        DomainKnowledgeCandidate(
            entity_id="resonator:1603",
            source_surface="椿",
            zh="椿",
            en="Camellya",
            entity_type="resonator",
            trust_tier="official_authoritative",
            retrieval_channel="short_ngram",
            score=1.0,
            lock_eligible=True,  # type: ignore[arg-type]
        )

    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    terms_db = tmp_path / "terms.db"
    revision = _build_synthetic_terms_db(terms_db)
    kb_path = tmp_path / "knowledge.db"

    build_knowledge_database(
        data_dir,
        kb_path,
        terms_db_path=terms_db,
        source_provenance=_provenance(),
    )

    # (a) SQLite hard CHECK(lock_eligible = 0) refuses both INSERT and UPDATE
    with closing(sqlite3.connect(kb_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO kb_aliases(
                  entity_id, alias_surface, alias_norm, lang, alias_kind,
                  trust_tier, lock_eligible
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "resonator:1402",
                    "非法锁词",
                    "非法锁词",
                    "zh",
                    "manual",
                    "official_authoritative",
                    1,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE kb_aliases SET lock_eligible = 1")

    # Reader is opened strictly read-only and refuses any write attempt
    with KnowledgeBaseReader(kb_path, expected_terms_db_revision=revision) as reader:
        assert reader.metadata["kb_schema_version"] == KB_SCHEMA_VERSION
        assert reader.entity_count() > 0
        with pytest.raises(sqlite3.OperationalError):
            reader._conn.execute("DELETE FROM kb_aliases")


def test_dirty_data_filtering_and_markup_cleaning(tmp_path: Path):
    # (b) Unit checks on dirty filter & markup cleaner
    assert is_dirty_text("dnt/内部测试")
    assert is_dirty_text("存在异常，如有疑问请联系官方客服")
    assert is_dirty_text("敬请期待")
    assert is_dirty_text("Coming soon TBA")
    assert is_dirty_text("测试文本_01")
    assert is_dirty_text("Test1-something")
    assert clean_kb_text("<color=Highlight>夜归军</color>") == "夜归军"
    assert clean_kb_text("<te href=850086>夜归</te>守卫") == "夜归守卫"
    assert clean_kb_text("dnt/should_drop") == ""

    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    terms_db = tmp_path / "terms.db"
    _build_synthetic_terms_db(terms_db)
    kb_path = tmp_path / "knowledge.db"

    build_knowledge_database(
        data_dir,
        kb_path,
        terms_db_path=terms_db,
        source_provenance=_provenance(),
    )

    with closing(sqlite3.connect(kb_path)) as conn:
        all_texts: list[str] = []
        for query in (
            "SELECT entity_id, zh, en, context_gloss FROM kb_entities",
            "SELECT alias_surface FROM kb_aliases",
            "SELECT zh_text, en_text FROM kb_contexts",
            "SELECT surface, zh, en, context_gloss FROM kb_fts_trigram",
            "SELECT ngram, surface FROM kb_short_ngrams",
        ):
            for row in conn.execute(query):
                all_texts.extend(str(col) for col in row)

        forbidden_markers = (
            "dnt/",
            "存在异常，如有疑问请联系官方客服",
            "敬请期待",
            "测试文本",
            "Test1-",
            "<color=",
            "<te ",
            "</te>",
            "</color>",
        )
        for text in all_texts:
            for marker in forbidden_markers:
                assert marker not in text, f"dirty marker {marker!r} leaked into {text!r}"

        # Id == 0 rows must never be persisted
        ids = {
            str(row[0])
            for row in conn.execute("SELECT entity_id FROM kb_entities")
        }
        assert not any(entity_id.endswith(":0") for entity_id in ids), ids
        # Element 1 (冷凝) stays even though its zh description was customer-service error
        assert "element:1" in ids
        # Dirty entity 9999 and dirty term 850100 are completely excluded
        assert "resonator:9999" not in ids
        assert "term:850100" not in ids


def test_terms_db_revision_tampering_fails_closed(tmp_path: Path):
    # (c) Tampered terms_db_revision must fail-closed on both reader and verifier
    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    terms_db = tmp_path / "terms.db"
    revision = _build_synthetic_terms_db(terms_db)
    kb_path = tmp_path / "knowledge.db"

    build_knowledge_database_atomic(
        data_dir,
        kb_path,
        terms_db_path=terms_db,
        source_provenance=_provenance(),
    )

    # Valid revision passes verification
    counts, metadata = verify_knowledge_database(kb_path, terms_db_path=terms_db)
    assert counts["resonator"] == 3
    assert metadata["terms_db_revision"] == revision

    # Wrong expected revision fails closed
    with pytest.raises(KnowledgeBaseRevisionMismatchError):
        KnowledgeBaseReader(kb_path, expected_terms_db_revision="0" * 64)

    # Tampering metadata inside knowledge.db fails both reader and verify_knowledge_db
    with closing(sqlite3.connect(kb_path)) as conn:
        conn.execute(
            "UPDATE kb_metadata SET value = ? WHERE key = 'terms_db_revision'",
            ("tampered-revision-sha256",),
        )
        conn.commit()

    with pytest.raises(KnowledgeBaseRevisionMismatchError):
        KnowledgeBaseReader(kb_path, expected_terms_db_revision=revision)

    with pytest.raises(VerificationError, match="terms_db_revision mismatch"):
        verify_knowledge_database(kb_path, terms_db_path=terms_db)


def test_short_ngram_recall_and_zero_false_injection_for_ordinary_words(tmp_path: Path):
    # (d) 1-2 char short terms recalled via kb_short_ngrams; 香椿/臭椿/心情 strictly 0.0%
    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    terms_db = tmp_path / "terms.db"
    revision = _build_synthetic_terms_db(terms_db)
    kb_path = tmp_path / "knowledge.db"

    build_knowledge_database(
        data_dir,
        kb_path,
        terms_db_path=terms_db,
        source_provenance=_provenance(),
    )

    with KnowledgeBaseReader(kb_path, expected_terms_db_revision=revision) as reader:
        # 2-char short term: 冷凝 -> Glacio
        glacio_hits = reader.query("冷凝伤害提升")
        assert any(c.zh == "冷凝" and c.en == "Glacio" for c in glacio_hits), glacio_hits
        assert all(c.lock_eligible is False for c in glacio_hits)

        # 1-char short term in valid resonator context: 椿 -> Camellya
        camellya_hits = reader.query("欢迎椿来到队伍")
        assert any(
            c.zh == "椿" and c.en == "Camellya" and c.lock_eligible is False
            for c in camellya_hits
        ), camellya_hits

        # 1-char short term in honorific context: 心大人 -> Hsin
        hsin_hits = reader.query("心大人请指示")
        assert any(
            c.zh == "心" and c.en == "Hsin" and c.lock_eligible is False
            for c in hsin_hits
        ), hsin_hits

        # Ordinary words containing 椿/心: strictly 0.0% false injection / false lock
        for ordinary_sentence in (
            "今天晚餐吃了香椿炒蛋",
            "院子里的臭椿长高了",
            "她今天的心情非常愉快",
            "香椿",
            "臭椿",
            "心情",
            "红椿·蕾",
        ):
            hits = reader.query(ordinary_sentence)
            assert not any(c.zh in {"椿", "心"} for c in hits), (
                ordinary_sentence,
                hits,
            )


def test_fts5_trigram_recall_and_te_anchor_aliases_and_relations(tmp_path: Path):
    # (e) FTS5 trigram >=3 scalar substring / superstring / alias recall
    # (f) <te href=TermId> official anchors generate kb_aliases & kb_relations
    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    terms_db = tmp_path / "terms.db"
    revision = _build_synthetic_terms_db(terms_db)
    kb_path = tmp_path / "knowledge.db"

    stats = build_knowledge_database(
        data_dir,
        kb_path,
        terms_db_path=terms_db,
        source_provenance=_provenance(),
    )
    assert stats.entities >= 12
    assert stats.aliases > 0
    assert stats.relations > 0
    assert stats.short_ngrams > 0
    assert stats.fts_rows > 0

    with closing(sqlite3.connect(kb_path)) as conn:
        te_aliases = {
            (str(r[0]), str(r[1]), str(r[2]), int(r[3]))
            for r in conn.execute(
                "SELECT entity_id, alias_surface, lang, lock_eligible "
                "FROM kb_aliases WHERE alias_kind = 'te_anchor'"
            )
        }
        assert ("term:850086", "夜归军团", "zh", 0) in te_aliases
        assert ("term:850086", "Midnight Vanguard", "en", 0) in te_aliases
        assert ("term:850086", "夜归先锋", "zh", 0) in te_aliases
        assert ("term:850099", "共鸣核心", "zh", 0) in te_aliases
        assert ("term:850099", "Forte Core", "en", 0) in te_aliases

        te_relations = {
            (str(r[0]), str(r[1]), str(r[2]))
            for r in conn.execute(
                "SELECT src_entity_id, relation_type, dst_entity_id "
                "FROM kb_relations WHERE relation_type = 'mentions_term'"
            )
        }
        assert ("resonator:1402", "mentions_term", "term:850086") in te_relations
        assert ("resonator:1603", "mentions_term", "term:850099") in te_relations
        assert ("skill:1000101", "mentions_term", "term:850099") in te_relations

    with KnowledgeBaseReader(kb_path, expected_terms_db_revision=revision) as reader:
        # >=3 scalar substring query via FTS5 trigram against nickname alias "风声羽息"
        sub_hits = reader.query("风声羽")
        assert any(
            c.entity_id == "resonator:1402"
            and c.zh == "秧秧"
            and c.en == "Yangyang"
            and c.retrieval_channel == "fts5_trigram"
            and c.lock_eligible is False
            for c in sub_hits
        ), sub_hits

        # >=3 scalar English case-insensitive substring via FTS5 trigram
        en_hits = reader.query("midnight vanguard")
        assert any(
            c.entity_id == "term:850086"
            and c.zh == "夜归"
            and c.en == "Midnight Rangers"
            and c.lock_eligible is False
            for c in en_hits
        ), en_hits

        # >=3 scalar superstring sentence matching <te> alias "夜归军团"
        super_hits = reader.query("这支队伍隶属于夜归军团的前锋")
        assert any(
            c.entity_id == "term:850086" and c.lock_eligible is False
            for c in super_hits
        ), super_hits

        # 1-hop graph neighbors ready for P2 consumption
        neighbors = reader.neighbors("resonator:1402")
        rel_triples = {
            (rel.src_entity_id, rel.relation_type, rel.dst_entity_id)
            for rel in neighbors
        }
        assert ("resonator:1402", "has_element", "element:1") in rel_triples
        assert ("resonator:1402", "mentions_term", "term:850086") in rel_triples
        assert ("skill:1000101", "skill_of", "resonator:1402") in rel_triples


def test_pure_selection_gate_conflicts_and_budgets():
    assert is_single_cjk_char("椿")
    assert not is_single_cjk_char("冷凝")
    assert not is_single_cjk_char("A")

    candidates = [
        DomainKnowledgeCandidate(
            entity_id="resonator:1603",
            source_surface="椿",
            zh="椿",
            en="Camellya",
            entity_type="resonator",
            trust_tier="official_authoritative",
            retrieval_channel="short_ngram",
            score=0.95,
            context_gloss="红椿共鸣者",
        ),
        DomainKnowledgeCandidate(
            entity_id="element:1",
            source_surface="冷凝",
            zh="冷凝",
            en="Glacio",
            entity_type="element",
            trust_tier="official_authoritative",
            retrieval_channel="short_ngram",
            score=0.90,
            context_gloss="寒冰属性" * 20,
        ),
        DomainKnowledgeCandidate(
            entity_id="term:850086",
            source_surface="夜归",
            zh="夜归",
            en="Midnight Rangers",
            entity_type="term",
            trust_tier="official_authoritative",
            retrieval_channel="lexical_alias",
            score=0.88,
            context_gloss="守卫今州的军队",
        ),
    ]

    # Locked/reference conflict drops 冷凝 and 夜归
    filtered = select_domain_knowledge_candidates(
        "欢迎椿使用冷凝技能加入夜归",
        candidates,
        locked_surfaces=("冷凝",),
        reference_surfaces=("Midnight Rangers",),
    )
    assert [c.entity_id for c in filtered] == ["resonator:1603"]

    # Span-covered conflict drops 冷凝 when only covered by locked superstring 冷凝伤害
    span_filtered = select_domain_knowledge_candidates(
        "提升冷凝伤害效果",
        candidates,
        locked_surfaces=("冷凝伤害",),
    )
    assert not any(c.entity_id == "element:1" for c in span_filtered)

    # Budget truncation: default max_candidates=8 and max_scalars=400
    many = [
        DomainKnowledgeCandidate(
            entity_id=f"term:{i}",
            source_surface=f"术语{i:02d}",
            zh=f"术语{i:02d}",
            en=f"Term {i:02d}",
            entity_type="term",
            trust_tier="official_authoritative",
            retrieval_channel="fts5_trigram",
            score=1.0 - i * 0.01,
            context_gloss="说明文字" * 25,
        )
        for i in range(15)
    ]
    bounded = select_domain_knowledge_candidates("术语00 术语01 术语02", many)
    assert len(bounded) <= DEFAULT_MAX_CANDIDATES
    assert sum(candidate_scalar_length(c) for c in bounded) <= DEFAULT_MAX_SCALARS
    assert all(c.lock_eligible is False for c in bounded)

    # Tight scalar budget trims gloss without exceeding budget
    tight = select_domain_knowledge_candidates("冷凝测试", [candidates[1]], max_scalars=20)
    assert len(tight) == 1
    assert candidate_scalar_length(tight[0]) <= 20


def test_verify_knowledge_db_cli_and_failure_modes(tmp_path: Path):
    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    terms_db = tmp_path / "terms.db"
    revision = _build_synthetic_terms_db(terms_db)
    kb_path = tmp_path / "knowledge.db"

    build_knowledge_database(
        data_dir,
        kb_path,
        terms_db_path=terms_db,
        source_provenance=_provenance(),
    )

    assert verify_kb_main([str(kb_path), "--terms-db", str(terms_db)]) == 0
    assert (
        verify_kb_main(
            [str(kb_path), "--expected-terms-db-revision", revision]
        )
        == 0
    )

    # Extra unexpected table fails exact table-set check
    bad_table_db = tmp_path / "extra_table.db"
    bad_table_db.write_bytes(kb_path.read_bytes())
    with closing(sqlite3.connect(bad_table_db)) as conn:
        conn.execute("CREATE TABLE rogue_table (id INTEGER PRIMARY KEY)")
        conn.commit()
    with pytest.raises(VerificationError, match="table set mismatch"):
        verify_knowledge_database(bad_table_db, terms_db_path=terms_db)
    assert verify_kb_main([str(bad_table_db), "--terms-db", str(terms_db)]) == 1

    # Missing revision arg fails builder cleanly
    with pytest.raises(KnowledgeBuildError):
        build_knowledge_database(data_dir, tmp_path / "fail.db")

    # Invalid empty revision fails reader cleanly
    with pytest.raises(KnowledgeBaseError):
        KnowledgeBaseReader(kb_path, expected_terms_db_revision="  ")

    # Extraction helpers smoke checks
    assert extract_te_anchors("<te href=850086>夜归</te>") == [("850086", "夜归")]
    assert extract_te_anchors("<te href=0>零</te>") == []
    entities, aliases, contexts, relations = extract_knowledge_records(data_dir)
    assert entities and aliases and contexts and relations


def test_edge_validation_and_error_paths(tmp_path: Path):
    # DomainKnowledgeCandidate validation errors
    with pytest.raises(ValueError, match="non-empty"):
        DomainKnowledgeCandidate(
            entity_id="",
            source_surface="椿",
            zh="椿",
            en="Camellya",
            entity_type="resonator",
            trust_tier="official_authoritative",
            retrieval_channel="short_ngram",
            score=1.0,
        )
    with pytest.raises(ValueError, match="trust_tier"):
        DomainKnowledgeCandidate(
            entity_id="resonator:1",
            source_surface="椿",
            zh="椿",
            en="Camellya",
            entity_type="resonator",
            trust_tier="untrusted",  # type: ignore[arg-type]
            retrieval_channel="short_ngram",
            score=1.0,
        )
    with pytest.raises(ValueError, match="retrieval_channel"):
        DomainKnowledgeCandidate(
            entity_id="resonator:1",
            source_surface="椿",
            zh="椿",
            en="Camellya",
            entity_type="resonator",
            trust_tier="official_authoritative",
            retrieval_channel="magic",  # type: ignore[arg-type]
            score=1.0,
        )

    # Empty text / zero budget / duplicate pair / oversized core
    c1 = DomainKnowledgeCandidate(
        entity_id="term:1",
        source_surface="冷凝",
        zh="冷凝",
        en="Glacio",
        entity_type="element",
        trust_tier="official_derived",
        retrieval_channel="short_ngram",
        score=0.9,
    )
    c_dup_pair = DomainKnowledgeCandidate(
        entity_id="term:2",
        source_surface="冷凝",
        zh="冷凝",
        en="Glacio",
        entity_type="element",
        trust_tier="community_curated",
        retrieval_channel="lexical_alias",
        score=0.8,
    )
    assert select_domain_knowledge_candidates("", [c1]) == ()
    assert select_domain_knowledge_candidates("冷凝", [c1], max_candidates=0) == ()
    assert select_domain_knowledge_candidates("冷凝", [c1], max_scalars=3) == ()
    deduped = select_domain_knowledge_candidates("冷凝", [c1, c_dup_pair])
    assert [c.entity_id for c in deduped] == ["term:1"]

    # Missing files & bad schema version fail closed
    with pytest.raises(KnowledgeBaseError):
        compute_terms_db_revision(tmp_path / "missing_terms.db")
    with pytest.raises(KnowledgeBaseError):
        KnowledgeBaseReader(
            tmp_path / "missing_kb.db", expected_terms_db_revision="rev1"
        )

    bad_schema_db = tmp_path / "bad_schema.db"
    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    terms_db = tmp_path / "terms.db"
    rev = _build_synthetic_terms_db(terms_db)
    build_knowledge_database(
        data_dir,
        bad_schema_db,
        terms_db_revision=rev,
        source_provenance=_provenance(),
    )
    with KnowledgeBaseReader(bad_schema_db, expected_terms_db_revision=rev) as reader:
        assert reader.query("   ") == ()
        # Exercise short Latin token & 3-word English phrase probe paths
        assert isinstance(reader.query("HP Emerald of Genesis"), tuple)

    with closing(sqlite3.connect(bad_schema_db)) as conn:
        conn.execute(
            "UPDATE kb_metadata SET value = '999' WHERE key = 'kb_schema_version'"
        )
        conn.commit()
    with pytest.raises(KnowledgeBaseError, match="kb_schema_version"):
        KnowledgeBaseReader(bad_schema_db, expected_terms_db_revision=rev)

    # Atomic build cleans up temporary file on failure
    missing_data = tmp_path / "no_bindata"
    missing_data.mkdir()
    target_db = tmp_path / "atomic_fail.db"
    with pytest.raises(KnowledgeBuildError, match="missing BinData"):
        build_knowledge_database_atomic(
            missing_data,
            target_db,
            terms_db_revision=rev,
            source_provenance=_provenance(),
        )
    assert not target_db.exists()
    assert not list(tmp_path.glob(".atomic_fail.db.*.tmp"))

    # Legacy TextMap format + empty entities fail closed
    legacy_dir = tmp_path / "legacy_data"
    (legacy_dir / "BinData" / "area").mkdir(parents=True)
    _write_json(
        legacy_dir / "BinData" / "area" / "area.json",
        [{"AreaId": 7, "CountryId": 0, "Title": "Area_7_Title", "Father": 0}],
    )
    _write_json(
        legacy_dir / "TextMap" / "zh-Hans" / "MultiText.json",
        {"Area_7_Title": "云陵谷"},
    )
    _write_json(
        legacy_dir / "TextMap" / "en" / "MultiText.json",
        {"Area_7_Title": "Gorges of Spirits"},
    )
    stats = build_knowledge_database(
        legacy_dir,
        tmp_path / "legacy_kb.db",
        terms_db_revision=rev,
        source_provenance=_provenance(),
    )
    assert stats.entities == 1

    # Missing MultiText fails cleanly
    no_textmap = tmp_path / "no_textmap"
    (no_textmap / "BinData").mkdir(parents=True)
    with pytest.raises(KnowledgeBuildError, match="missing MultiText"):
        extract_knowledge_records(no_textmap)


def test_joint_single_char_and_locked_span_conflict_regression(tmp_path: Path):
    # Finding 1a: "心大人今天心情很好" with locked "心大人" must not emit "心"
    # (occ 1 is a name_mention but covered by lock; occ 2 is uncovered but ordinary_word).
    hsin_candidate = DomainKnowledgeCandidate(
        entity_id="resonator:1699",
        source_surface="心",
        zh="心",
        en="Hsin",
        entity_type="resonator",
        trust_tier="official_authoritative",
        retrieval_channel="short_ngram",
        score=0.95,
    )
    assert (
        select_domain_knowledge_candidates(
            "心大人今天心情很好",
            [hsin_candidate],
            locked_surfaces=("心大人",),
        )
        == ()
    )

    # Finding 1b: case/whitespace-normalized span coverage blocks "Iron Forge"
    # when "Ancient IRON FORGE" is locked.
    forge_candidate = DomainKnowledgeCandidate(
        entity_id="area:900",
        source_surface="Iron Forge",
        zh="铁砧炉",
        en="Iron Forge",
        entity_type="area",
        trust_tier="official_authoritative",
        retrieval_channel="lexical_alias",
        score=0.9,
    )
    assert (
        select_domain_knowledge_candidates(
            "Ancient IRON FORGE",
            [forge_candidate],
            locked_surfaces=("Ancient IRON FORGE",),
        )
        == ()
    )
    # Uncovered second occurrence is still admitted.
    kept = select_domain_knowledge_candidates(
        "Ancient IRON FORGE near Iron Forge",
        [forge_candidate],
        locked_surfaces=("Ancient IRON FORGE",),
    )
    assert [c.entity_id for c in kept] == ["area:900"]

    # End-to-end reader verification on "心大人今天心情很好"
    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    terms_db = tmp_path / "terms.db"
    rev = _build_synthetic_terms_db(terms_db)
    kb_path = tmp_path / "knowledge.db"
    build_knowledge_database(
        data_dir, kb_path, terms_db_path=terms_db, source_provenance=_provenance()
    )
    with KnowledgeBaseReader(kb_path, expected_terms_db_revision=rev) as reader:
        hits = reader.query("心大人今天心情很好", locked_surfaces=("心大人",))
        assert not any(c.zh == "心" for c in hits), hits


def test_fts_gloss_match_cannot_bypass_single_char_ordinary_word_filter(tmp_path: Path):
    # Finding 2: querying "平复心情" matches context_gloss of 1-char CJK entity 心 (Hsin),
    # which must not bypass single-char ordinary_word filtering via surface="Hsin".
    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    role_path = data_dir / "BinData" / "role" / "roleinfo.json"
    roles = json.loads(role_path.read_text(encoding="utf-8"))
    for row in roles:
        if row.get("Id") == 1699:
            row["Introduction"] = "FavorRoleInfo_1699_Info"
    _write_json(role_path, roles)

    for lang_dir, text in (
        ("zh-Hans", "能帮助同伴平复心情的共鸣者。"),
        ("en", "A resonator who calms companions' emotions."),
    ):
        mt_path = data_dir / "Textmaps" / lang_dir / "multi_text" / "MultiText.json"
        rows = json.loads(mt_path.read_text(encoding="utf-8"))
        rows.append({"Id": "FavorRoleInfo_1699_Info", "Content": text})
        _write_json(mt_path, rows)

    terms_db = tmp_path / "terms.db"
    rev = _build_synthetic_terms_db(terms_db)
    kb_path = tmp_path / "knowledge.db"
    build_knowledge_database(
        data_dir, kb_path, terms_db_path=terms_db, source_provenance=_provenance()
    )

    with KnowledgeBaseReader(kb_path, expected_terms_db_revision=rev) as reader:
        assert not any(c.entity_id == "resonator:1699" for c in reader.query("平复心情"))
        # Direct English query still recalls Hsin normally.
        en_hits = reader.query("Hsin")
        assert any(
            c.entity_id == "resonator:1699" and c.lock_eligible is False
            for c in en_hits
        ), en_hits


def test_long_input_tail_single_char_not_starved_by_bigrams(tmp_path: Path):
    # Finding 3: 144+ char Chinese input with >130 distinct bigrams before tail "心大人".
    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    terms_db = tmp_path / "terms.db"
    rev = _build_synthetic_terms_db(terms_db)
    kb_path = tmp_path / "knowledge.db"
    build_knowledge_database(
        data_dir, kb_path, terms_db_path=terms_db, source_provenance=_provenance()
    )

    # 150 distinct Chinese characters producing 149 distinct 2-char bigrams before "心大人".
    filler_chars = [chr(0x4E00 + i) for i in range(150) if chr(0x4E00 + i) not in {"心", "椿"}]
    long_text = "".join(filler_chars[:145]) + "，请转告心大人。"
    assert len(long_text) > 144

    with KnowledgeBaseReader(kb_path, expected_terms_db_revision=rev) as reader:
        hits = reader.query(long_text)
        assert any(
            c.entity_id == "resonator:1699"
            and c.zh == "心"
            and c.en == "Hsin"
            and c.lock_eligible is False
            for c in hits
        ), hits


def test_build_preserves_existing_db_on_failure_and_forbids_overwriting_terms_db(
    tmp_path: Path,
):
    # Finding 4a: build failure must never delete or corrupt an existing knowledge.db.
    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    terms_db = tmp_path / "terms.db"
    rev = _build_synthetic_terms_db(terms_db)
    kb_path = tmp_path / "knowledge.db"

    build_knowledge_database(
        data_dir, kb_path, terms_db_path=terms_db, source_provenance=_provenance()
    )
    original_kb_bytes = kb_path.read_bytes()
    original_terms_bytes = terms_db.read_bytes()

    with pytest.raises(KnowledgeBuildError, match="no knowledge entities"):
        create_knowledge_database(
            kb_path,
            entities=[],
            aliases=[],
            contexts=[],
            relations=[],
            terms_db_revision=rev,
            source_provenance=_provenance(),
        )
    assert kb_path.read_bytes() == original_kb_bytes

    # Finding 4b: db_path pointing to terms_db_path must fail closed with ValueError.
    with pytest.raises(ValueError, match="must not point to the same file"):
        build_knowledge_database(
            data_dir,
            terms_db,
            terms_db_path=terms_db,
            source_provenance=_provenance(),
        )
    with pytest.raises(ValueError, match="must not point to the same file"):
        build_knowledge_database_atomic(
            data_dir,
            terms_db,
            terms_db_path=terms_db,
            source_provenance=_provenance(),
        )
    assert terms_db.read_bytes() == original_terms_bytes


def test_weapon_reson_multi_weapon_edges_en_only_te_anchors_and_strict_href(
    tmp_path: Path,
):
    # Finding 5c: strict numeric href rejects truncated/alphanumeric IDs.
    assert extract_te_anchors(
        '<te href="12a">坏锚点</te><te href=12b>坏锚点2</te>'
        '<te href="0">零</te><te href="850086">夜归正统</te>'
    ) == [("850086", "夜归正统")]

    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")

    # Finding 5a: ResonId=21010024 shared across 5 valid weapons.
    weapon_path = data_dir / "BinData" / "weapon" / "weaponconf.json"
    weapons = json.loads(weapon_path.read_text(encoding="utf-8"))
    shared_ids = [21010024, 21020024, 21030024, 21040024, 21050024]
    for wid in shared_ids:
        weapons.append(
            {
                "ItemId": wid,
                "WeaponName": f"WeaponConf_{wid}_WeaponName",
                "ResonId": 21010024,
                "Desc": "",
                "BgDescription": "",
            }
        )
    _write_json(weapon_path, weapons)

    reson_path = data_dir / "BinData" / "weapon" / "weaponreson.json"
    resons = json.loads(reson_path.read_text(encoding="utf-8"))
    resons.append(
        {"Id": 24, "ResonId": 21010024, "Level": 1, "Name": "WeaponReson_24_Name"}
    )
    _write_json(reson_path, resons)

    zh_mt = data_dir / "Textmaps" / "zh-Hans" / "multi_text" / "MultiText.json"
    en_mt = data_dir / "Textmaps" / "en" / "multi_text" / "MultiText.json"
    zh_rows = json.loads(zh_mt.read_text(encoding="utf-8"))
    en_rows = json.loads(en_mt.read_text(encoding="utf-8"))
    for idx, wid in enumerate(shared_ids, start=1):
        zh_rows.append({"Id": f"WeaponConf_{wid}_WeaponName", "Content": f"共享武器{idx}"})
        en_rows.append({"Id": f"WeaponConf_{wid}_WeaponName", "Content": f"Shared Weapon {idx}"})
    zh_rows.append({"Id": "WeaponReson_24_Name", "Content": "共鸣同调"})
    en_rows.append({"Id": "WeaponReson_24_Name", "Content": "Resonant Unison"})
    # Finding 5b: English-only standalone <te href=...> text (zh side has no <te> tag).
    zh_rows.append({"Id": "EnOnly_Te_Key", "Content": "纯中文说明不含锚点"})
    en_rows.append(
        {
            "Id": "EnOnly_Te_Key",
            "Content": "Only English has <te href=850086>Rangers Watch</te> and <te href=\"850086a\">Broken</te>.",
        }
    )
    _write_json(zh_mt, zh_rows)
    _write_json(en_mt, en_rows)

    terms_db = tmp_path / "terms.db"
    _build_synthetic_terms_db(terms_db)
    kb_path = tmp_path / "knowledge.db"
    build_knowledge_database(
        data_dir, kb_path, terms_db_path=terms_db, source_provenance=_provenance()
    )

    with closing(sqlite3.connect(kb_path)) as conn:
        passive_targets = {
            str(r[0])
            for r in conn.execute(
                "SELECT dst_entity_id FROM kb_relations "
                "WHERE src_entity_id = 'weapon_reson:21010024' AND relation_type = 'passive_of'"
            )
        }
        assert passive_targets == {f"weapon:{wid}" for wid in shared_ids}

        en_te_aliases = {
            str(r[0])
            for r in conn.execute(
                "SELECT alias_surface FROM kb_aliases "
                "WHERE entity_id = 'term:850086' AND lang = 'en' AND alias_kind = 'te_anchor'"
            )
        }
        assert "Rangers Watch" in en_te_aliases
        assert "Broken" not in en_te_aliases


def test_verify_knowledge_db_rejects_emptied_fts_and_short_ngram_indexes(tmp_path: Path):
    # Finding 6: emptying kb_fts_trigram or kb_short_ngrams must fail verification.
    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    terms_db = tmp_path / "terms.db"
    _build_synthetic_terms_db(terms_db)
    kb_path = tmp_path / "knowledge.db"
    build_knowledge_database(
        data_dir, kb_path, terms_db_path=terms_db, source_provenance=_provenance()
    )

    empty_fts = tmp_path / "empty_fts.db"
    empty_fts.write_bytes(kb_path.read_bytes())
    with closing(sqlite3.connect(empty_fts)) as conn:
        conn.execute("DELETE FROM kb_fts_trigram")
        conn.commit()
    with pytest.raises(VerificationError, match="kb_fts_trigram missing"):
        verify_knowledge_database(empty_fts, terms_db_path=terms_db)

    empty_short = tmp_path / "empty_short.db"
    empty_short.write_bytes(kb_path.read_bytes())
    with closing(sqlite3.connect(empty_short)) as conn:
        conn.execute("DELETE FROM kb_short_ngrams")
        conn.commit()
    with pytest.raises(VerificationError, match="kb_short_ngrams missing"):
        verify_knowledge_database(empty_short, terms_db_path=terms_db)


def test_unicode_nfd_nfc_lock_coverage_and_fts_substring_lock_regression(
    tmp_path: Path,
):
    # Repro 1a: NFC lock "Café Forge" must cover NFD source "Cafe\u0301 Forge" so "Forge" is dropped.
    forge_candidate = DomainKnowledgeCandidate(
        entity_id="area:901",
        source_surface="Forge",
        zh="熔炉",
        en="Forge",
        entity_type="area",
        trust_tier="official_authoritative",
        retrieval_channel="lexical_alias",
        score=0.9,
    )
    assert (
        select_domain_knowledge_candidates(
            "Cafe\u0301 Forge",
            [forge_candidate],
            locked_surfaces=("Café Forge",),
        )
        == ()
    )

    # Repro 1b: Querying "Ice" with locked "Ice" must not emit FTS hit "Ice Blade".
    ice_blade = DomainKnowledgeCandidate(
        entity_id="weapon:902",
        source_surface="Ice Blade",
        zh="冰刃",
        en="Ice Blade",
        entity_type="weapon",
        trust_tier="official_authoritative",
        retrieval_channel="fts5_trigram",
        score=0.85,
    )
    assert (
        select_domain_knowledge_candidates(
            "Ice",
            [ice_blade],
            locked_surfaces=("Ice",),
        )
        == ()
    )
    assert (
        select_domain_knowledge_candidates(
            "Cast Ice today",
            [ice_blade],
            locked_surfaces=("Ice",),
        )
        == ()
    )
    kept = select_domain_knowledge_candidates(
        "Cast Ice and Blade",
        [ice_blade],
        locked_surfaces=("Ice",),
    )
    assert [c.entity_id for c in kept] == ["weapon:902"]

    # End-to-end reader check on "Ice" -> "Ice Blade"
    kb_path = tmp_path / "ice_kb.db"
    rev = "a" * 64
    create_knowledge_database(
        kb_path,
        entities=[
            EntitySpec(
                entity_id="weapon:902",
                entity_type="weapon",
                source_file="BinData/weapon/weaponconf.json",
                source_id="902",
                zh="冰刃",
                en="Ice Blade",
            )
        ],
        aliases=[],
        contexts=[],
        relations=[],
        terms_db_revision=rev,
        source_provenance=_provenance(),
    )
    with KnowledgeBaseReader(kb_path, expected_terms_db_revision=rev) as reader:
        assert any(c.en == "Ice Blade" for c in reader.query("Ice"))
        assert reader.query("Ice", locked_surfaces=("Ice",)) == ()


def test_verify_knowledge_db_checks_per_row_ngram_len_and_nfkc_expanding_fts_probe(
    tmp_path: Path,
):
    # Repro 2a: tampering 心's ngram_len from 1 to 2 must fail verification.
    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    terms_db = tmp_path / "terms.db"
    _build_synthetic_terms_db(terms_db)
    kb_path = tmp_path / "knowledge.db"
    build_knowledge_database(
        data_dir, kb_path, terms_db_path=terms_db, source_provenance=_provenance()
    )

    bad_len_db = tmp_path / "bad_ngram_len.db"
    bad_len_db.write_bytes(kb_path.read_bytes())
    with closing(sqlite3.connect(bad_len_db)) as conn:
        conn.execute(
            "UPDATE kb_short_ngrams SET ngram_len = 2 WHERE ngram = '心'"
        )
        conn.commit()
    with pytest.raises(VerificationError, match="invalid ngram_len"):
        verify_knowledge_database(bad_len_db, terms_db_path=terms_db)

    # Repro 2b: single-codepoint "Ⅲ" (\u2162, NFKC-expands to "iii") must verify cleanly
    # without false failure when it is the only FTS entry.
    roman_kb = tmp_path / "roman_kb.db"
    rev = "b" * 64
    create_knowledge_database(
        roman_kb,
        entities=[
            EntitySpec(
                entity_id="term:3",
                entity_type="term",
                source_file="BinData/Term/termconfig.json",
                source_id="3",
                zh="Ⅲ",
                en="3",
            )
        ],
        aliases=[],
        contexts=[],
        relations=[],
        terms_db_revision=rev,
        source_provenance=_provenance(),
    )
    counts, _meta = verify_knowledge_database(
        roman_kb, expected_terms_db_revision=rev
    )
    assert counts == {"term": 1}


def test_te_anchor_parses_href_strictly_at_html_attribute_boundaries():
    # Repro 3: title="href=12" must never shadow the actual href="34" attribute.
    assert extract_te_anchors('<te title="href=12" href="34">术语</te>') == [
        ("34", "术语")
    ]
    assert extract_te_anchors('<te title="href=12">无真实属性</te>') == []
    assert extract_te_anchors(
        '<te data-href="12" title="a>b" href="34">术语</te>'
    ) == [("34", "术语")]


def test_matched_source_spans_gate_icebound_and_winter_blade_regressions(
    tmp_path: Path,
):
    # Invalid matched_source_spans fail closed on construction
    with pytest.raises(ValueError, match="invalid matched_source_span"):
        DomainKnowledgeCandidate(
            entity_id="weapon:1",
            source_surface="Icebound",
            zh="冰封",
            en="Icebound",
            entity_type="weapon",
            trust_tier="official_authoritative",
            retrieval_channel="fts5_trigram",
            score=0.8,
            matched_source_spans=((4, 4),),
        )

    kb_path = tmp_path / "span_gate_kb.db"
    rev = "c" * 64
    create_knowledge_database(
        kb_path,
        entities=[
            EntitySpec(
                entity_id="weapon:910",
                entity_type="weapon",
                source_file="BinData/weapon/weaponconf.json",
                source_id="910",
                zh="冰封",
                en="Icebound",
                context_gloss="Use Icebound in combat",
            ),
            EntitySpec(
                entity_id="weapon:911",
                entity_type="weapon",
                source_file="BinData/weapon/weaponconf.json",
                source_id="911",
                zh="寒冬之刃",
                en="Winter Blade",
            ),
        ],
        aliases=[],
        contexts=[],
        relations=[],
        terms_db_revision=rev,
        source_provenance=_provenance(),
    )

    with KnowledgeBaseReader(kb_path, expected_terms_db_revision=rev) as reader:
        # Case 1: "Use Ice now" with locked "Ice" must NOT emit "Icebound"
        # because its only real matched source span is the locked "Ice" (4, 7).
        unfiltered_ice = reader.query("Use Ice now")
        assert any(
            c.en == "Icebound" and c.matched_source_spans == ((4, 7),)
            for c in unfiltered_ice
        ), unfiltered_ice
        assert not any(
            c.en == "Icebound"
            for c in reader.query("Use Ice now", locked_surfaces=("Ice",))
        )

        # Case 2: "Use Winter or Blad" with locked "Winter" MUST still retain
        # "Winter Blade" via the uncovered matched source span "Blad" (14, 18).
        kept = reader.query("Use Winter or Blad", locked_surfaces=("Winter",))
        assert any(
            c.en == "Winter Blade"
            and (14, 18) in c.matched_source_spans
            and c.lock_eligible is False
            for c in kept
        ), kept

        # Locking both matched spans filters "Winter Blade" out cleanly.
        assert not any(
            c.en == "Winter Blade"
            for c in reader.query(
                "Use Winter or Blad", locked_surfaces=("Winter", "Blad")
            )
        )


def test_exact_probe_spans_entity_priority_and_out_of_bounds_span_regressions(
    tmp_path: Path,
):
    kb_path = tmp_path / "exact_spans_kb.db"
    rev = "d" * 64
    create_knowledge_database(
        kb_path,
        entities=[
            EntitySpec(
                entity_id="weapon:920",
                entity_type="weapon",
                source_file="BinData/weapon/weaponconf.json",
                source_id="920",
                zh="冰封之刃",
                en="Icebound",
            ),
            EntitySpec(
                entity_id="weapon:921",
                entity_type="weapon",
                source_file="BinData/weapon/weaponconf.json",
                source_id="921",
                zh="甲乙融合",
                en="A B Fusion",
            ),
            EntitySpec(
                entity_id="weapon:922",
                entity_type="weapon",
                source_file="BinData/weapon/weaponconf.json",
                source_id="922",
                zh="北境之锋",
                en="Northern Edge",
                context_gloss="Use this blade in northern winds.",
            ),
        ],
        aliases=[
            AliasSpec(
                entity_id="weapon:922",
                alias_surface="Icebound",
                lang="en",
                alias_kind="nickname",
            )
        ],
        contexts=[],
        relations=[],
        terms_db_revision=rev,
        source_provenance=_provenance(),
    )

    with KnowledgeBaseReader(kb_path, expected_terms_db_revision=rev) as reader:
        # Repro 1a: "Use Ice boost at this price" locked "Ice boost" must NOT return
        # "Icebound" (FTS matched token "Ice" at (4, 7), never "ice" inside "price").
        assert not any(
            c.entity_id == "weapon:920"
            for c in reader.query(
                "Use Ice boost at this price", locked_surfaces=("Ice boost",)
            )
        )

        # Repro 1b: "Use A B now" locked "A B" must NOT return "A B Fusion"
        # (raw 3-char FTS probe "A B" at (4, 7) keeps its exact span despite 2-char norm "ab").
        assert any(c.entity_id == "weapon:921" for c in reader.query("Use A B now"))
        assert not any(
            c.entity_id == "weapon:921"
            for c in reader.query("Use A B now", locked_surfaces=("A B",))
        )

        # Repro 2: "Northern Edge" has alias "Icebound" and gloss containing "Use".
        # Querying "Use Icebound now" with locked "Icebound" must NOT let Northern Edge
        # re-enter via the lower-priority gloss hit "Use".
        assert not any(
            c.entity_id == "weapon:922"
            for c in reader.query("Use Icebound now", locked_surfaces=("Icebound",))
        )

    # Repro 3: Out-of-bounds matched_source_spans ((40, 50),) must be rejected,
    # never treated as an uncovered match on "Ancient Iron Forge".
    oob_candidate = DomainKnowledgeCandidate(
        entity_id="area:923",
        source_surface="Iron Forge",
        zh="铁砧炉",
        en="Iron Forge",
        entity_type="area",
        trust_tier="official_authoritative",
        retrieval_channel="fts5_trigram",
        score=0.9,
        matched_source_spans=((40, 50),),
    )
    assert (
        select_domain_knowledge_candidates(
            "Ancient Iron Forge",
            [oob_candidate],
            locked_surfaces=("Ancient Iron Forge",),
        )
        == ()
    )
    assert (
        select_domain_knowledge_candidates(
            "Ancient Iron Forge",
            [oob_candidate],
        )
        == ()
    )


def test_unified_probe_sqlite_alignment_and_tier_truncation_invariants(tmp_path: Path):
    kb_path = tmp_path / "invariants_kb.db"
    rev = "e" * 64
    create_knowledge_database(
        kb_path,
        entities=[
            EntitySpec(
                entity_id="weapon:930",
                entity_type="weapon",
                source_file="BinData/weapon/weaponconf.json",
                source_id="930",
                zh="北境之锋",
                en="Northern Edge",
            ),
            EntitySpec(
                entity_id="weapon:931",
                entity_type="weapon",
                source_file="BinData/weapon/weaponconf.json",
                source_id="931",
                zh="冻霜甲乙",
                en="Frozen AB",
            ),
            EntitySpec(
                entity_id="stat:932",
                entity_type="term",
                source_file="BinData/Term/termconfig.json",
                source_id="932",
                zh="生命值",
                en="HP",
                context_gloss="Use HP to withstand enemy strikes.",
            ),
        ],
        aliases=[],
        contexts=[],
        relations=[],
        terms_db_revision=rev,
        source_provenance=_provenance(),
    )

    with KnowledgeBaseReader(kb_path, expected_terms_db_revision=rev) as reader:
        # Invariant 1: multi-word probes never cross punctuation or Chinese text.
        # "Northern，随后 Edge" with locked ("Northern", "Edge") must NOT fabricate
        # an unlocked "Northern Edge" span across "，随后 ".
        assert (
            reader.query(
                "Northern，随后 Edge", locked_surfaces=("Northern", "Edge")
            )
            == ()
        )

        # Invariant 2: FTS5 attribution matches raw casefolded substrings, never
        # space-stripped norms. "Frozen effect uses A B" locked "Frozen effect"
        # only matched "Frozen" in "Frozen AB" (not "A B"), so must return ().
        assert not any(
            c.entity_id == "weapon:931"
            for c in reader.query(
                "Frozen effect uses A B", locked_surfaces=("Frozen effect",)
            )
        )

        # Invariant 3: entity-level tier truncation runs BEFORE merging spans.
        # "Use HP boost now" locked "HP boost" has tier-0 name/short_ngram hit on
        # "HP" (4, 6); tier-1 gloss hit on "Use" (0, 3) is discarded before merge.
        assert any(c.entity_id == "stat:932" for c in reader.query("Use HP boost now"))
        assert (
            reader.query("Use HP boost now", locked_surfaces=("HP boost",)) == ()
        )


def test_fts5_authoritative_per_probe_unicode_folding_regression(tmp_path: Path):
    # Python .casefold() folds "ß" -> "ss", while SQLite FTS5 trigram does not.
    # Attribution must come directly from SQLite FTS5 column-qualified queries,
    # never Python .casefold() guessing.
    kb_path = tmp_path / "unicode_fts_kb.db"
    rev = "f" * 64
    create_knowledge_database(
        kb_path,
        entities=[
            EntitySpec(
                entity_id="area:940",
                entity_type="area",
                source_file="BinData/area/area.json",
                source_id="940",
                zh="冬日街道",
                en="Winter Straße",
            ),
        ],
        aliases=[],
        contexts=[],
        relations=[],
        terms_db_revision=rev,
        source_provenance=_provenance(),
    )

    with KnowledgeBaseReader(kb_path, expected_terms_db_revision=rev) as reader:
        # "Winter gear for Strasse" locked "Winter gear": SQLite only matches
        # "Winter" (0, 6), NOT "Strasse", so Winter Straße must be filtered out.
        assert (
            reader.query(
                "Winter gear for Strasse", locked_surfaces=("Winter gear",)
            )
            == ()
        )
        # Actual unlocked "Straße" in source IS matched by SQLite and retained.
        kept = reader.query(
            "Winter gear for Straße", locked_surfaces=("Winter gear",)
        )
        assert [c.entity_id for c in kept] == ["area:940"]
        assert kept[0].lock_eligible is False


def test_two_phase_fts_evidence_completion_avoids_limit_32_truncation(tmp_path: Path):
    # Populate 85 distractor aliases matching "Blade" so target entities' "Blade"
    # matches rank past row 42 and row 80 globally (beyond per-probe LIMIT 32).
    entities: list[EntitySpec] = [
        EntitySpec(
            entity_id=f"weapon:10{idx:02d}",
            entity_type="weapon",
            source_file="BinData/weapon/weaponconf.json",
            source_id=f"10{idx:02d}",
            zh=f"剑{idx:02d}",
            en=f"Blade {idx:02d}",
        )
        for idx in range(45)
    ]
    aliases: list[AliasSpec] = [
        AliasSpec(
            entity_id=f"weapon:10{idx:02d}",
            alias_surface=f"Blade{idx:02d}",
            lang="en",
            alias_kind="nickname",
        )
        for idx in range(45)
    ]
    # Target 1: Bladebound (rank > 42 on "Blade", plus "Use" in context_gloss)
    entities.append(
        EntitySpec(
            entity_id="weapon:9991",
            entity_type="weapon",
            source_file="BinData/weapon/weaponconf.json",
            source_id="9991",
            zh="剑缚长锋",
            en="Bladebound",
            context_gloss="Use this ancient blade to cleave shadows.",
        )
    )
    # Target 2: Winterbound Longblade (rank > 80 on "Blade", unique hit on "Winter")
    entities.append(
        EntitySpec(
            entity_id="weapon:9992",
            entity_type="weapon",
            source_file="BinData/weapon/weaponconf.json",
            source_id="9992",
            zh="冬缚长剑",
            en="Winterbound Longblade",
        )
    )

    kb_path = tmp_path / "limit_truncation_kb.db"
    rev = "9" * 64
    create_knowledge_database(
        kb_path,
        entities=entities,
        aliases=aliases,
        contexts=[],
        relations=[],
        terms_db_revision=rev,
        source_provenance=_provenance(),
    )

    with KnowledgeBaseReader(kb_path, expected_terms_db_revision=rev) as reader:
        # Case 1: "Use Blade boost now" locked "Blade boost" — Phase 2 must still
        # retrieve Bladebound's Tier-0 "Blade" match past LIMIT 32 so its Tier-1
        # gloss "Use" hit is dropped and Bladebound is filtered out by the lock.
        hits_case1 = reader.query(
            "Use Blade boost now", locked_surfaces=("Blade boost",)
        )
        assert not any(c.entity_id == "weapon:9991" for c in hits_case1), hits_case1

        # Case 2: "Use Winter gear or Blade" locked "Winter gear" — Phase 2 must
        # retrieve Winterbound Longblade's uncovered Tier-0 "Blade" match past
        # row 80 so Winterbound Longblade is retained (not falsely dropped).
        hits_case2 = reader.query(
            "Use Winter gear or Blade",
            locked_surfaces=("Winter gear",),
            max_candidates=64,
            max_scalars=4000,
        )
        assert any(
            c.entity_id == "weapon:9992"
            and (19, 24) in c.matched_source_spans
            and c.lock_eligible is False
            for c in hits_case2
        ), hits_case2








