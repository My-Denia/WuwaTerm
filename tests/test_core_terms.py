"""Entries added through ``CORE_TERM_KEYS``: 飞讯摆件 -> WavesLine Decor
and the 3.7 version title.

The three original members of ``CORE_TERM_KEYS`` are stable system names that
no BinData Name field carries, so they are read straight from the pinned text
map. This module covers the entries added beside them and, just as importantly,
what the mechanism must not grow: a second record for an identical mirror key,
or a bare fragment of a brand or title becoming a dictionary term.

The whole build path is real here: a fixture in the pinned Arikatsu layout goes
through ``iter_records`` and ``create_database`` into ``TermService``, and the
sentence assertions call the same ``prepare_text``/lock/restore functions the
translation path calls. A key the builder never looks up fails this module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wuwaterm.builder import ARIKATSU_BIN_SPECS, iter_records
from wuwaterm.constants import CATEGORY_ORDER, SOURCE_PROFILES
from wuwaterm.data_source import SourceProvenance
from wuwaterm.db import connect, create_database
from wuwaterm.lookup import TermService
from wuwaterm.normalize import normalize_text
from wuwaterm.sentence import SentenceTranslator
from wuwaterm.telegram_html import protect_telegram_html

BRAND_ZH = "飞讯摆件"
BRAND_EN = "WavesLine Decor"
# The official saved pair for the 3.7 version title, verbatim from the pinned
# snapshot: U+FF0C inside the Chinese title, ASCII comma inside the English one.
VERSION_ZH = "镜锁妄世，心照红尘"
VERSION_EN = "Prism's Illusion, Heart's Illumination"
# Bare achievement name: the same snapshot's other official wording.
BARE_VERSION_ZH = "心照红尘"
BARE_VERSION_EN = "To the Mortal World, a Burning Heart"
MAPMARK_ZH = "扶风转标"
MAPMARK_EN = "Gravity Calibrator"

ORIGINAL_CORE_KEYS = (
    "LoadingTipsText_1005_Title",
    "Term850082_Title",
    "OccupationConfig_漂泊者_Name",
)

# One saved pair per key, exactly as the pinned snapshot stores it. The mirror
# key (PrefabTextItem_2083276822_Text) carries the same pair upstream on
# purpose: it must stay out of the dictionary, so the row count asserts it.
UPSTREAM_TEXT = {
    "LoadingTipsText_1005_Title": ("共鸣者", "Resonator"),
    "Term850082_Title": ("声骸", "Echo"),
    "OccupationConfig_漂泊者_Name": ("漂泊者", "Rover"),
    "ItemType_ChatCharm": (BRAND_ZH, BRAND_EN),
    "PrefabTextItem_2083276822_Text": (BRAND_ZH, BRAND_EN),
    "Advertising_VersionTitle_37": (VERSION_ZH, VERSION_EN),
    "MapMark_170100_MarkTitle": (MAPMARK_ZH, MAPMARK_EN),
    "Term850078_Title": ("无音区", "Tacet Field"),
    "Term850079_Title": ("索拉里斯", "Solaris"),
    "Term850093_Title": ("瑝珑", "Huanglong"),
    "Influence_998_Title": ("残星会", "Fractsidus"),
    # Mirror key carrying the identical pair, and the bare achievement name
    # with different official English: neither may become a second row.
    "LoadingTipsText_901401_Title": (VERSION_ZH, VERSION_EN),
    "Achievement_200691_Name": (BARE_VERSION_ZH, BARE_VERSION_EN),
}


def _write_arikatsu_tree(root: Path, texts: dict[str, tuple[str, str]]) -> Path:
    """Write the minimal Textmaps/BinData layout ``iter_arikatsu_records`` reads."""
    for lang, index in (("zh-Hans", 0), ("en", 1)):
        text_map = root / "Textmaps" / lang / "multi_text" / "MultiText.json"
        text_map.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"Id": key, "Content": pair[index]} for key, pair in texts.items()]
        text_map.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    for spec in ARIKATSU_BIN_SPECS:
        config = root / "BinData" / spec.config_file
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("[]\n", encoding="utf-8")
    return root


def _build(tmp_path: Path, texts: dict[str, tuple[str, str]]) -> Path:
    data_dir = _write_arikatsu_tree(tmp_path / "wutheringdata", texts)
    profile = SOURCE_PROFILES["arikatsu"]
    db_path = tmp_path / "terms.db"
    create_database(
        db_path,
        iter_records(data_dir, profile.name),
        source_profile=profile,
        source_provenance=SourceProvenance(
            profile=profile.name,
            repo_url=profile.repo_url,
            commit=profile.pinned_commit,
            game_version="fixture-unavailable",
            resource_version="fixture-unavailable",
            changelist="fixture-unavailable",
        ),
    )
    return db_path


@pytest.fixture()
def core_term_db(tmp_path: Path) -> Path:
    return _build(tmp_path, UPSTREAM_TEXT)


def _direct_rows(db_path: Path) -> dict[str, object]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM terms WHERE source_file = ?",
            ("Textmaps/multi_text/MultiText.json",),
        ).fetchall()
    return {row["text_key"]: row for row in rows}


def test_world_region_faction_and_field_names_register_exact_rows(core_term_db: Path):
    rows = _direct_rows(core_term_db)
    expected = {
        "Term850078_Title": ("无音区", "Tacet Field", "core_term"),
        "Term850079_Title": ("索拉里斯", "Solaris", "core_term"),
        "Term850093_Title": ("瑝珑", "Huanglong", "location"),
        "Influence_998_Title": ("残星会", "Fractsidus", "core_term"),
    }
    service = TermService(core_term_db)
    for key, (zh, en, category) in expected.items():
        row = rows[key]
        assert (row["zh"], row["en"], row["category"]) == (zh, en, category)
        assert row["priority"] == CATEGORY_ORDER[category]
        hit = service.lookup_exact(zh)
        assert hit.exact and hit.best.entry.en == en


def test_chat_charm_key_registers_one_core_term_row(core_term_db: Path):
    rows = _direct_rows(core_term_db)

    # The mirror key holds the same pair upstream and is still not a second row.
    assert set(rows) == {
        *ORIGINAL_CORE_KEYS,
        "ItemType_ChatCharm",
        "Advertising_VersionTitle_37",
        "MapMark_170100_MarkTitle",
        "Term850078_Title",
        "Term850079_Title",
        "Term850093_Title",
        "Influence_998_Title",
    }

    row = rows["ItemType_ChatCharm"]
    # Same category, priority and normalisation path as the three keys that
    # were already registered; no schema or annotation change.
    assert row["category"] == "core_term"
    assert row["priority"] == CATEGORY_ORDER["core_term"]
    assert (row["zh"], row["en"]) == (BRAND_ZH, BRAND_EN)
    assert row["zh_norm"] == normalize_text(BRAND_ZH)
    assert row["en_norm"] == normalize_text(BRAND_EN)


def test_chat_charm_is_an_exact_entry_in_both_directions(core_term_db: Path):
    service = TermService(core_term_db)

    zh_hit = service.lookup_exact(BRAND_ZH)
    assert zh_hit.exact
    assert zh_hit.best.entry.en == BRAND_EN
    assert zh_hit.best.entry.category == "core_term"

    en_hit = service.lookup_exact(BRAND_EN)
    assert en_hit.exact
    assert en_hit.best.entry.zh == BRAND_ZH
    assert en_hit.best.entry.category == "core_term"


def test_map_mark_registers_a_location_term_and_locks(core_term_db: Path):
    rows = _direct_rows(core_term_db)
    row = rows["MapMark_170100_MarkTitle"]
    assert row["category"] == "location"
    assert row["priority"] == CATEGORY_ORDER["location"]
    assert (row["zh"], row["en"]) == (MAPMARK_ZH, MAPMARK_EN)
    assert row["zh_norm"] == normalize_text(MAPMARK_ZH)
    assert row["en_norm"] == normalize_text(MAPMARK_EN)

    service = TermService(core_term_db)
    for query, expected_en in ((MAPMARK_ZH, MAPMARK_EN), (MAPMARK_EN, MAPMARK_EN)):
        hit = service.lookup_exact(query)
        assert hit.exact and hit.best.entry.category == "location"
    assert service.lookup_exact(MAPMARK_ZH).best.entry.en == MAPMARK_EN

    translator = SentenceTranslator(core_term_db)
    source = translator.prepare_text(f"前方有{MAPMARK_ZH}，先启动它。")
    locked = translator._lock_terms_with_references(source, to_chinese=False)
    assert (MAPMARK_ZH, MAPMARK_ZH, MAPMARK_EN, "location") in locked.references


def test_chat_charm_references_into_english_and_locks_into_chinese(
    core_term_db: Path,
):
    translator = SentenceTranslator(core_term_db)

    # Every call goes through prepare_text(), the same preparation translate()
    # applies, so the surfaces are matched against real user text.
    zh_source = translator.prepare_text(f"新版本上架了{BRAND_ZH}，可以在外观里查看。")
    zh_locked = translator._lock_terms_with_references(zh_source, to_chinese=False)
    assert (BRAND_ZH, BRAND_ZH, BRAND_EN, "core_term") in zh_locked.references

    en_source = translator.prepare_text(f"Claim the new {BRAND_EN} from the event.")
    en_locked = translator._lock_terms_with_references(en_source, to_chinese=True)
    assert len(en_locked.locks) == 1
    placeholder, lock_zh, lock_en = en_locked.locks[0]
    assert (lock_zh, lock_en) == (BRAND_ZH, BRAND_EN)
    assert en_locked.locked_text == f"Claim the new {placeholder} from the event."
    restored = en_locked.restore(en_locked.locked_text, to_en=False)
    assert BRAND_ZH in restored
    assert BRAND_EN not in restored


def test_bare_brand_word_is_not_a_dictionary_term(core_term_db: Path):
    translator = SentenceTranslator(core_term_db)

    # The registered pair is the ornament name; the product family word alone
    # is context, not a term, so it neither locks nor becomes a reference.
    assert translator.lock_terms("飞讯").locks == ()
    bare = translator._lock_terms_with_references(
        translator.prepare_text("打开飞讯看看新消息。"), to_chinese=False
    )
    assert bare.references == ()
    assert bare.locks == ()


def test_version_title_registers_one_row_and_keeps_mirror_and_bare_name_out(
    core_term_db: Path,
):
    rows = _direct_rows(core_term_db)

    # The mirror key carries the identical pair and the bare 心照红尘 has its
    # own different official English; neither is a dictionary entry.
    assert set(rows) == {
        *ORIGINAL_CORE_KEYS,
        "ItemType_ChatCharm",
        "Advertising_VersionTitle_37",
        "MapMark_170100_MarkTitle",
        "Term850078_Title",
        "Term850079_Title",
        "Term850093_Title",
        "Influence_998_Title",
    }

    row = rows["Advertising_VersionTitle_37"]
    assert row["category"] == "core_term"
    assert row["priority"] == CATEGORY_ORDER["core_term"]
    assert (row["zh"], row["en"]) == (VERSION_ZH, VERSION_EN)
    assert row["zh_norm"] == normalize_text(VERSION_ZH)
    assert row["en_norm"] == normalize_text(VERSION_EN)

    service = TermService(core_term_db)
    # Lookup keys are NFKC-folded on both sides, so the title is exact in the
    # saved spelling and in the ASCII-comma spelling a user may type.
    for zh in (VERSION_ZH, "镜锁妄世,心照红尘"):
        zh_hit = service.lookup_exact(zh)
        assert zh_hit.exact
        assert zh_hit.best.entry.en == VERSION_EN
    en_hit = service.lookup_exact(VERSION_EN)
    assert en_hit.exact
    assert en_hit.best.entry.zh == VERSION_ZH
    assert not service.lookup_exact(BARE_VERSION_ZH).exact


def test_version_title_references_and_locks_while_html_stays_raw_exact(
    core_term_db: Path,
):
    translator = SentenceTranslator(core_term_db)

    # The announcement line keeps its U+FF0C; prepare_text folds it to ASCII,
    # so this reference exists only because the prepared plain-text path also
    # compares the stored raw surface's NFKC spelling (the flag the translate
    # path passes). Bare 心照红尘 stays out of the dictionary and out of refs.
    zh_source = translator.prepare_text(f"全新3.7版本「{VERSION_ZH}」即将开启！")
    assert "，" not in zh_source and "," in zh_source
    zh_locked = translator._lock_terms_with_references(
        zh_source, to_chinese=False, allow_nfkc_spelling=True
    )
    assert (VERSION_ZH, VERSION_ZH, VERSION_EN, "core_term") in zh_locked.references
    bare = translator._lock_terms_with_references(
        translator.prepare_text(f"「{BARE_VERSION_ZH}」即将开启！"),
        to_chinese=False,
        allow_nfkc_spelling=True,
    )
    assert bare.references == ()

    # en→zh keeps the pair as one span; the comma inside the title does not
    # split it, and restore returns the official Chinese title.
    en_source = translator.prepare_text(f"Version 3.7 {VERSION_EN} is coming.")
    en_locked = translator._lock_terms_with_references(en_source, to_chinese=True)
    assert len(en_locked.locks) == 1
    placeholder, lock_zh, lock_en = en_locked.locks[0]
    assert (lock_zh, lock_en) == (VERSION_ZH, VERSION_EN)
    assert en_locked.locked_text == f"Version 3.7 {placeholder} is coming."
    assert VERSION_ZH in en_locked.restore(en_locked.locked_text, to_en=False)

    # Telegram HTML is not prepared, so its raw U+FF0C text node matches the
    # raw dictionary surface exactly. An ASCII-comma variant inside HTML has
    # no NFKC candidate by design (protected text keeps exact matching): that
    # is a stated source boundary, not full coverage.
    raw_html = protect_telegram_html(f"<b>全新3.7版本「{VERSION_ZH}」即将开启！</b>")
    raw_locked = translator._lock_protected_terms_with_references(
        raw_html.neutral, to_chinese=False
    )
    assert (VERSION_ZH, VERSION_ZH, VERSION_EN, "core_term") in raw_locked.references

    ascii_html = protect_telegram_html(
        "<b>全新3.7版本「镜锁妄世,心照红尘」即将开启！</b>"
    )
    ascii_locked = translator._lock_protected_terms_with_references(
        ascii_html.neutral, to_chinese=False
    )
    assert ascii_locked.references == ()


def test_missing_upstream_key_still_skips_and_keeps_the_original_three(
    tmp_path: Path,
):
    added_keys = {
        "ItemType_ChatCharm",
        "Advertising_VersionTitle_37",
        "MapMark_170100_MarkTitle",
        "Term850078_Title",
        "Term850079_Title",
        "Term850093_Title",
        "Influence_998_Title",
    }
    texts = {key: pair for key, pair in UPSTREAM_TEXT.items() if key not in added_keys}

    rows = _direct_rows(_build(tmp_path, texts))

    # Registering a key never makes a build fail when the pinned snapshot has
    # no such row: _record_for_key skips it, exactly as before.
    assert set(rows) == set(ORIGINAL_CORE_KEYS)
