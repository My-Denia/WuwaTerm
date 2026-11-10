"""Brand entry added through ``CORE_TERM_KEYS``: 飞讯摆件 -> WavesLine Decor.

The three original members of ``CORE_TERM_KEYS`` are stable system names that
no BinData Name field carries, so they are read straight from the pinned text
map. This module covers the entry added beside them and, just as importantly,
what the mechanism must not grow: a second record for the identical mirror key,
or a bare fragment of the brand becoming a dictionary term.

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

BRAND_ZH = "飞讯摆件"
BRAND_EN = "WavesLine Decor"

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


def test_chat_charm_key_registers_one_core_term_row(core_term_db: Path):
    rows = _direct_rows(core_term_db)

    # The mirror key holds the same pair upstream and is still not a second row.
    assert set(rows) == {*ORIGINAL_CORE_KEYS, "ItemType_ChatCharm"}

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


def test_missing_upstream_key_still_skips_and_keeps_the_original_three(
    tmp_path: Path,
):
    texts = {
        key: pair for key, pair in UPSTREAM_TEXT.items() if key != "ItemType_ChatCharm"
    }

    rows = _direct_rows(_build(tmp_path, texts))

    # Registering a key never makes a build fail when the pinned snapshot has
    # no such row: _record_for_key skips it, exactly as before.
    assert set(rows) == set(ORIGINAL_CORE_KEYS)
