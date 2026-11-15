from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from wuwaterm.constants import SOURCE_PROFILES, get_source_profile
from wuwaterm.data_source import DataSourceError, SourceProvenance
from wuwaterm.builder import (
    ARIKATSU_BIN_SPECS,
    BuildError,
    build_database,
    build_database_atomic,
    iter_records,
    measure_free_text_exclusions,
    measure_free_text_zh_exclusions,
    source_profile_for_data_dir,
)
from wuwaterm.db import category_counts, connect, create_database
from wuwaterm.english_plural import number_variants, plural_forms, plural_suffix_candidates
from wuwaterm.models import TermRecord


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def legacy_checkout(monkeypatch, sample_data_dir):
    _git("init", "-b", "main", cwd=sample_data_dir)
    _git("add", ".", cwd=sample_data_dir)
    _git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "fixture",
        cwd=sample_data_dir,
    )
    commit = _git("rev-parse", "HEAD", cwd=sample_data_dir)
    profile = replace(SOURCE_PROFILES["dimbreath_legacy"], pinned_commit=commit)
    monkeypatch.setitem(SOURCE_PROFILES, "dimbreath_legacy", profile)
    _git("remote", "add", "origin", profile.repo_url, cwd=sample_data_dir)
    return sample_data_dir, profile


def test_builder_extracts_required_categories(sample_db):
    with connect(sample_db) as conn:
        counts = category_counts(conn)

    for category in (
        "core_term",
        "resonator",
        "weapon",
        "echo",
        "item",
        "skill",
        "sonata_effect",
        "location",
    ):
        assert counts[category] > 0


def test_builder_uses_multitext_symbol_keys(sample_db):
    with connect(sample_db) as conn:
        row = conn.execute(
            "SELECT zh, en FROM terms WHERE text_key = 'RoleInfo_1304_Name'"
        ).fetchone()

    assert row["zh"] == "今汐"
    assert row["en"] == "Jinhsi"


def test_builder_selects_legacy_profile_from_configdb_layout(sample_data_dir):
    profile = source_profile_for_data_dir(sample_data_dir)

    assert profile.name == "dimbreath_legacy"


def test_build_database_accepts_measured_legacy_source(legacy_checkout, tmp_path):
    sample_data_dir, profile = legacy_checkout
    db_path = tmp_path / "profiled.db"
    build_database(sample_data_dir, db_path, profile_name="dimbreath_legacy")

    with connect(db_path) as conn:
        metadata = dict(conn.execute("SELECT key, value FROM metadata").fetchall())

    assert metadata["source_profile"] == "dimbreath_legacy"
    assert metadata["wutheringdata_commit"] == profile.pinned_commit
    assert metadata["schema_version"] == "2"
    assert metadata["source_commit"] == metadata["wutheringdata_commit"]
    assert metadata["source_game_version"] == "unavailable"


def test_build_database_rejects_non_git_legacy_source(sample_data_dir, tmp_path):
    with pytest.raises(DataSourceError, match="not a Git checkout"):
        build_database(
            sample_data_dir,
            tmp_path / "profiled.db",
            profile_name="dimbreath_legacy",
        )


def test_build_database_atomic_replaces_target_after_success(legacy_checkout, tmp_path):
    sample_data_dir, _profile = legacy_checkout
    db_path = tmp_path / "terms.db"
    db_path.write_text("old database", encoding="utf-8")

    count = build_database_atomic(
        sample_data_dir, db_path, profile_name="dimbreath_legacy"
    )

    assert count > 0
    with connect(db_path) as conn:
        counts = category_counts(conn)
    assert counts["resonator"] > 0
    assert not list(tmp_path.glob(".terms.db.*.tmp"))


def test_build_database_atomic_failure_keeps_existing_db(
    monkeypatch, legacy_checkout, tmp_path
):
    sample_data_dir, _profile = legacy_checkout
    db_path = tmp_path / "terms.db"
    db_path.write_text("old database", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise BuildError("boom")

    monkeypatch.setattr("wuwaterm.builder.iter_records", boom)

    with pytest.raises(BuildError):
        build_database_atomic(sample_data_dir, db_path, profile_name="dimbreath_legacy")

    assert db_path.read_text(encoding="utf-8") == "old database"
    assert not list(tmp_path.glob(".terms.db.*.tmp"))


# Free-text lock exclusions. Synthetic names and corpus lines only.
def _records_only_db(tmp_path: Path) -> Path:
    profile = get_source_profile("dimbreath_legacy")
    db_path = tmp_path / "exclusions.db"
    create_database(
        db_path,
        [
            TermRecord("speaker", "fixture", "1", "1", "雷恩", "Wren"),
            TermRecord("speaker", "fixture", "2", "2", "布鲁克", "Brook"),
            TermRecord("location", "fixture", "3", "3", "布鲁克谷", "Brook Vale"),
            TermRecord("resonator", "fixture", "4", "4", "维米拉", "Velmira"),
        ],
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


def _exclusion_corpus() -> tuple[dict[str, str], dict[str, str]]:
    en: dict[str, str] = {}
    zh: dict[str, str] = {}
    for index in range(12):
        # Wren is an ordinary word in its official lines: never 雷恩 in zh.
        en[f"wren_{index}"] = f"Wren is raining, line {index}."
        zh[f"wren_{index}"] = f"在下雨，第{index}行。"
        # Velmira is a name: the parallel line always carries 维米拉.
        en[f"velmira_{index}"] = f"Velmira waves, line {index}."
        zh[f"velmira_{index}"] = f"维米拉挥手，第{index}行。"
        # Only the longer Brook Vale span is selected in these unaligned
        # lines; were the inner Brook counted, it would reach 15 unaligned.
        en[f"vale_{index}"] = f"Brook Vale is quiet, line {index}."
        zh[f"vale_{index}"] = f"第{index}行，很安静。"
        # Case variants are not measured, and lines without zh are skipped.
        en[f"lower_{index}"] = f"velmira and wren, line {index}."
        zh[f"lower_{index}"] = f"第{index}行。"
        en[f"no_zh_{index}"] = f"Wren has no zh line {index}."
    for index in range(3):
        en[f"brook_{index}"] = f"Brook runs, line {index}."
        zh[f"brook_{index}"] = f"溪流在流，第{index}行。"
    # Text with CJK uses the full lockable set and is still counted.
    en["mixed"] = "Wren 说话了。"
    zh["mixed"] = "有人说话了。"
    return en, zh


def test_measure_free_text_exclusions_counts_exact_selected_spans(tmp_path):
    db_path = _records_only_db(tmp_path)
    en, zh = _exclusion_corpus()

    value = measure_free_text_exclusions(db_path, en, zh)

    assert value == (
        '{"max_aligned_ratio":[1,5],"min_occurrences":10,'
        '"surfaces":[["Brook Vale",12,0],["Wren",13,0]],"version":1}'
    )
    assert measure_free_text_exclusions(db_path, dict(reversed(en.items())), zh) == value


def test_create_database_alone_writes_no_exclusions(tmp_path):
    db_path = _records_only_db(tmp_path)
    with connect(db_path) as conn:
        keys = {row[0] for row in conn.execute("SELECT key FROM metadata")}
    assert "free_text_lock_exclusions" not in keys


def test_build_database_records_exclusions_deterministically(legacy_checkout, tmp_path):
    sample_data_dir, _profile = legacy_checkout
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    build_database(sample_data_dir, first, profile_name="dimbreath_legacy")
    build_database(sample_data_dir, second, profile_name="dimbreath_legacy")

    def exclusions(path: Path) -> str:
        with connect(path) as conn:
            return conn.execute(
                "SELECT value FROM metadata WHERE key = 'free_text_lock_exclusions'"
            ).fetchone()[0]

    # Every fixture name occurs once and is aligned, so nothing is excluded.
    assert json.loads(exclusions(first)) == {
        "version": 1,
        "min_occurrences": 10,
        "max_aligned_ratio": [1, 5],
        "surfaces": [],
    }
    assert exclusions(first) == exclusions(second)


# Chinese-surface free-text lock exclusions (the zh metadata key).
def _zh_records_only_db(tmp_path: Path) -> Path:
    profile = get_source_profile("dimbreath_legacy")
    db_path = tmp_path / "zh_exclusions.db"
    create_database(
        db_path,
        [
            TermRecord("speaker", "fixture", "1", "1", "雷恩", "Wren"),
            TermRecord("speaker", "fixture", "2", "2", "布鲁克", "Brook"),
            TermRecord("location", "fixture", "3", "3", "布鲁克谷", "Brook Vale"),
            TermRecord("resonator", "fixture", "4", "4", "维米拉", "Velmira"),
            # Quote-wrapped official English: aligned only when the parallel
            # line is matched casefolded and quote-stripped.
            TermRecord("speaker", "fixture", "5", "5", "角先生", '"Jue"'),
            # A second record carrying 布鲁克; the first record owns the
            # surface, but this record's English still counts as aligned.
            TermRecord("speaker", "fixture", "6", "6", "布鲁克", "Brook the Guard"),
            TermRecord("speaker", "fixture", "7", "7", "小小", "Teensy"),
            # Corner-bracket-wrapped official English: aligned only when
            # both the opening and closing brackets strip.
            TermRecord("speaker", "fixture", "8", "8", "岸鹭", "「Heron」"),
        ],
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


def _zh_exclusion_corpus() -> tuple[dict[str, str], dict[str, str]]:
    zh: dict[str, str] = {}
    en: dict[str, str] = {}
    for index in range(12):
        # 雷恩 is an ordinary word in its official lines: never Wren in en.
        zh[f"wren_{index}"] = f"雷恩在下雨，第{index}行。"
        en[f"wren_{index}"] = f"It is raining, line {index}."
        # 维米拉 is a name: the parallel line carries it in another case.
        zh[f"velmira_{index}"] = f"维米拉挥手，第{index}行。"
        en[f"velmira_{index}"] = f"VELMIRA waves, line {index}."
        # 角先生's official English is quote-wrapped; the parallel line is
        # bare and lower-case, aligned only via casefold + quote-strip.
        zh[f"jue_{index}"] = f"角先生说话，第{index}行。"
        en[f"jue_{index}"] = f"mr jue spoke, line {index}."
        # The owner of 布鲁克 is Brook, but these parallel lines render the
        # other record carrying that surface; any record's English aligns.
        zh[f"brook_{index}"] = f"布鲁克挥手，第{index}行。"
        en[f"brook_{index}"] = f"Brook the Guard waves, line {index}."
        # Only the longer 布鲁克谷 span is selected in these unaligned
        # lines; were the inner 布鲁克 counted, it would reach 24.
        zh[f"vale_{index}"] = f"布鲁克谷很安静，第{index}行。"
        en[f"vale_{index}"] = f"Quiet valley, line {index}."
        # 岸鹭's official English is wrapped in corner brackets; without
        # stripping the closing 」 the bare parallel line never aligns and
        # the surface would be wrongly excluded.
        zh[f"heron_{index}"] = f"岸鹭飞过，第{index}行。"
        en[f"heron_{index}"] = f"heron flew by, line {index}."
    for index in range(4):
        # Below the occurrence floor even with zero alignment.
        zh[f"teensy_{index}"] = f"小小很生气，第{index}行。"
        en[f"teensy_{index}"] = f"Someone is angry, line {index}."
    # Lines without a parallel en are skipped.
    for index in range(5):
        zh[f"no_en_{index}"] = f"雷恩没有英文，第{index}行。"
    # Latin words beside CJK still count the CJK spans; the Latin surface
    # is tried (first-character bucket) but never counted.
    zh["mixed"] = "雷恩 Wren 说话了。"
    en["mixed"] = "Someone spoke."
    return zh, en


def test_measure_free_text_zh_exclusions_counts_exact_selected_spans(tmp_path):
    db_path = _zh_records_only_db(tmp_path)
    zh, en = _zh_exclusion_corpus()

    value = measure_free_text_zh_exclusions(db_path, zh, en)

    assert value == (
        '{"max_aligned_ratio":[1,5],"min_occurrences":10,'
        '"surfaces":[["布鲁克谷",12,0],["雷恩",13,0]],"version":1}'
    )
    assert measure_free_text_zh_exclusions(db_path, dict(reversed(zh.items())), en) == value


def test_measure_free_text_zh_exclusions_keeps_high_alignment_surfaces(tmp_path):
    db_path = _zh_records_only_db(tmp_path)
    zh, en = _zh_exclusion_corpus()

    surfaces = {
        row[0]
        for row in json.loads(measure_free_text_zh_exclusions(db_path, zh, en))["surfaces"]
    }

    # Casefolding aligns VELMIRA; quote-strip + casefold aligns bare jue;
    # any record's English aligns 布鲁克; corner-bracket strip (both sides)
    # aligns bare heron; 小小 stays under the floor.
    assert surfaces == {"雷恩", "布鲁克谷"}
    assert surfaces.isdisjoint({"维米拉", "角先生", "布鲁克", "小小", "岸鹭"})


def test_create_database_alone_writes_no_zh_exclusions(tmp_path):
    db_path = _zh_records_only_db(tmp_path)
    with connect(db_path) as conn:
        keys = {row[0] for row in conn.execute("SELECT key FROM metadata")}
    assert "free_text_lock_exclusions_zh" not in keys


def test_build_database_records_zh_exclusions_deterministically(
    legacy_checkout, tmp_path
):
    sample_data_dir, _profile = legacy_checkout
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    build_database(sample_data_dir, first, profile_name="dimbreath_legacy")
    build_database(sample_data_dir, second, profile_name="dimbreath_legacy")

    def zh_exclusions(path: Path) -> str:
        with connect(path) as conn:
            return conn.execute(
                "SELECT value FROM metadata WHERE key = 'free_text_lock_exclusions_zh'"
            ).fetchone()[0]

    # Every fixture name occurs once and is aligned, so nothing is excluded.
    assert json.loads(zh_exclusions(first)) == {
        "version": 1,
        "min_occurrences": 10,
        "max_aligned_ratio": [1, 5],
        "surfaces": [],
    }
    assert zh_exclusions(first) == zh_exclusions(second)

    # The zh key is written after the English key.
    with connect(first) as conn:
        keys = [
            key
            for (key,) in conn.execute("SELECT key FROM metadata ORDER BY rowid")
            if key.startswith("free_text_lock_exclusions")
        ]
    assert keys == ["free_text_lock_exclusions", "free_text_lock_exclusions_zh"]


def test_measure_free_text_exclusions_stays_on_raw_surfaces(tmp_path: Path):
    # The runtime can compare a raw surface through its pure NFKC spelling in
    # prepared plain text; the builder measures the official raw corpus, so a
    # canonical spelling in that corpus must not be counted as this surface.
    profile = get_source_profile("dimbreath_legacy")
    db_path = tmp_path / "raw-surface-exclusions.db"
    create_database(
        db_path,
        [TermRecord("term", "fixture", "1", "1", "回声（稀有）", "Echo（Rare）")],
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
    en = {f"line_{index}": "Echo(Rare) appears here." for index in range(12)}
    zh = {f"line_{index}": "这里没有对应内容。" for index in range(12)}

    assert json.loads(measure_free_text_exclusions(db_path, en, zh))["surfaces"] == []


def test_english_plural_number_variants_bidirectional_and_guards():
    assert number_variants("Waveworn Phenomena") == ("Waveworn Phenomenon",)
    assert number_variants("Waveworn Phenomenon") == ("Waveworn Phenomena",)
    assert number_variants("WAVEWORN PHENOMENA") == ("WAVEWORN PHENOMENON",)
    assert number_variants("Main Quests") == ("Main Quest",)
    assert number_variants("Main Quest") == ("Main Quests",)
    assert number_variants("Companion Stories") == ("Companion Story",)
    assert number_variants("Companion Story") == ("Companion Stories",)
    assert number_variants("Bosses") == ("Boss",)
    # Singular -ss/-us/-is surfaces must never drop trailing s; non-phenomenon
    # -on nouns must never mutate to -a.
    for invariant_singular in ("Solaris", "Genesis", "Status"):
        assert number_variants(invariant_singular) == ()
    assert number_variants("Boss") == ("Bosses",)
    for regular_on in ("Weapon", "Beacon", "Dragon", "Subphenomenon"):
        assert number_variants(regular_on) == (f"{regular_on}s",)
    # Existing sentence-locking helpers keep their exact singular->plural rules.
    assert plural_forms("Waveworn Phenomena") == ("Waveworn Phenomenas",)
    assert plural_suffix_candidates("Solaris", ("索拉里斯", "Solaris")) == ()


def test_number_variants_never_invents_a_short_ordinary_word():
    # Silent final e takes a bare s, so the plural must not lose its es.
    assert number_variants("Prizes") == ("Prize",)
    assert number_variants("Shoes") == ("Shoe",)
    assert number_variants("Toes") == ("Toe",)
    # A -ies plural only becomes -y when the stem and the result are long
    # enough, so the short forms stay out of the variant set.
    assert number_variants("Lies") == ("Lie",)
    assert number_variants("Ties") == ("Tie",)
    assert number_variants("Stories") == ("Story",)
    # -os/-as endings and the invariant words keep their s: "Mar" must never
    # answer for the speaker "Mars" and "Na" never for "Nas".
    for kept in ("Chaos", "Kronos", "Atlas", "Elias", "Nas"):
        assert number_variants(kept) == ()
    for invariant in ("Lens", "News", "Mars", "Series", "Species"):
        assert number_variants(invariant) == ()
    # Two letters is too short to be a dictionary surface.
    assert number_variants("TDs") == ()


def test_measure_free_text_zh_exclusions_aligns_singular_and_plural_variants(
    tmp_path: Path,
):
    profile = get_source_profile("dimbreath_legacy")
    db_path = tmp_path / "plural-zh-exclusions.db"
    create_database(
        db_path,
        [
            TermRecord("core_term", "fixture", "1", "1", "海蚀", "Waveworn Phenomena"),
            TermRecord("core_term", "fixture", "2", "2", "潮汐任务", "Main Quests"),
            TermRecord("core_term", "fixture", "3", "3", "伴星任务", "Companion Stories"),
            TermRecord("core_term", "fixture", "4", "4", "索拉里斯", "Solaris"),
        ],
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
    zh = {}
    en = {}
    for index in range(12):
        zh[f"p_{index}"] = f"海蚀异象出现，第{index}行。"
        en[f"p_{index}"] = f"A Waveworn Phenomenon appeared, line {index}."
        zh[f"q_{index}"] = f"完成潮汐任务与伴星任务，第{index}行。"
        en[f"q_{index}"] = f"Complete the Main Quest and Companion Story, line {index}."
        # "Solari" is not a singular variant of "Solaris", so these stay unaligned.
        zh[f"s_{index}"] = f"索拉里斯记录，第{index}行。"
        en[f"s_{index}"] = f"Solari log entry, line {index}."

    excluded = json.loads(measure_free_text_zh_exclusions(db_path, zh, en))["surfaces"]
    assert excluded == [["索拉里斯", 12, 0]]


def _write_arikatsu(
    root: Path,
    texts: dict[str, tuple[str, str]],
    table_rows: dict[str, list[dict[str, object]]],
) -> Path:
    """Write a synthetic Arikatsu checkout; every spec table not given is empty."""
    for lang, idx in (("zh-Hans", 0), ("en", 1)):
        path = root / "Textmaps" / lang / "multi_text" / "MultiText.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([{"Id": k, "Content": v[idx]} for k, v in texts.items()]),
            encoding="utf-8",
        )
    for spec in ARIKATSU_BIN_SPECS:
        cfg = root / "BinData" / spec.config_file
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            json.dumps(table_rows.get(spec.config_file, [])), encoding="utf-8"
        )
    return root


def test_arikatsu_system_bindata_tables_skip_default_rows(tmp_path: Path):
    root = tmp_path / "arikatsu"
    texts = {
        "t_def_theme": ("默认主题", "Default Theme"),
        "t_moon_theme": ("朝月清辉", "Light of Waking Moon"),
        "t_def_charm": ("默认摆件", "Default Decor"),
        "t_mica_charm": ("云母小铃", "Mica Chime"),
        "t_def_bg": ("默认背景", "Default Background"),
        "t_def_bubble": ("默认气泡", "Default Bubble"),
        "t_pangu_bubble": ("盘古终端", "Pangu Terminal"),
        "t_quest_all": ("任务", "Quests"),
        "t_quest_test": ("存在异常", "Test Task. Main"),
        "t_quest_main": ("潮汐任务", "Main Quests"),
        "t_skill_norm": ("常态攻击", "Normal Attack"),
        "t_gacha_res": ("角色活动唤取", "Featured Resonator Convene"),
        # Ordinary English words a removed cosmetic table would have imported.
        "t_sonic": ("索尼克", "Sonic"),
        "t_shadow": ("夏特", "Shadow"),
    }
    table_rows = {
        "PhoneMsg/chattheme.json": [
            {"Id": 80891001, "SortId": 0, "ItemAccess": [], "BgDescription": "", "Name": "t_def_theme"},
            {"Id": 80891002, "SortId": 1, "ItemAccess": [914601], "BgDescription": "d", "Name": "t_moon_theme"},
        ],
        "PhoneMsg/chatcharm.json": [
            {"Id": 80890001, "SortId": 0, "ItemAccess": [], "BgDescription": "", "Name": "t_def_charm"},
            {"Id": 80890002, "SortId": 1, "ItemAccess": [914602], "BgDescription": "d", "Name": "t_mica_charm"},
        ],
        "PhoneMsg/chatbg.json": [
            {"Id": 80820001, "SortId": 1, "ItemAccess": [], "BgDescription": "", "Name": "t_def_bg"},
        ],
        "PhoneMsg/chatdialog.json": [
            {"Id": 80810001, "SortId": 1, "ItemAccess": [], "BgDescription": "", "Name": "t_def_bubble"},
            {"Id": 80810002, "SortId": 2, "ItemAccess": [], "BgDescription": "", "Name": "t_pangu_bubble"},
        ],
        "questtype/questmaintype.json": [
            {"Id": 0, "MainTypeName": "t_quest_all"},
            {"Id": 100, "MainTypeName": "t_quest_test"},
            {"Id": 1, "MainTypeName": "t_quest_main"},
        ],
        "skill/skilltype.json": [{"Id": 1, "TypeName": "t_skill_norm"}],
        "gacha/gachaviewtypeinfo.json": [{"Type": 2, "TypeText": "t_gacha_res"}],
    }
    _write_arikatsu(root, texts, table_rows)
    # A cosmetic table may still exist in the checkout; no spec reads it, which
    # is how one table import cannot drag an unrelated everyday word in.
    for config_file, row in (
        ("MotorDIY/motorsticker.json", {"Id": 89103070, "Title": "t_sonic"}),
        ("Ornament/ornament.json", {"Id": 80871006, "Name": "t_shadow"}),
    ):
        cfg = root / "BinData" / config_file
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps([row]), encoding="utf-8")

    records = iter_records(root, "arikatsu")
    bin_pairs = {
        (r.zh, r.en, r.category)
        for r in records
        if r.source_file.startswith("BinData/")
    }
    assert bin_pairs == {
        ("朝月清辉", "Light of Waking Moon", "item"),
        ("云母小铃", "Mica Chime", "item"),
        ("盘古终端", "Pangu Terminal", "item"),
        ("潮汐任务", "Main Quests", "core_term"),
        ("常态攻击", "Normal Attack", "core_term"),
        ("角色活动唤取", "Featured Resonator Convene", "core_term"),
    }
    # The ordinary words those cosmetic tables also carry never enter.
    assert {r.en for r in records} & {"Sonic", "Shadow"} == set()


def _tier_rows(first_id: int, key: str) -> list[dict[str, object]]:
    return [{"Id": first_id + tier, "Name": f"{key}_{tier}"} for tier in range(4)]


def _tier_texts(key: str, zh: str, en: str, *, en_premium: str | None = None):
    names = [("初级", "Basic"), ("中级", "Medium"), ("高级", "Advanced"), ("特级", "Premium")]
    texts = {
        f"{key}_{tier}": (f"{zh_prefix}{zh}", f"{en_prefix} {en}")
        for tier, (zh_prefix, en_prefix) in enumerate(names)
    }
    if en_premium is not None:
        texts[f"{key}_3"] = (f"特级{zh}", f"Premium {en_premium}")
    return texts


@pytest.fixture()
def names_checkout(tmp_path: Path) -> Path:
    # Upstream names on synthetic keys and ids.
    texts = {
        "Area_2103_Title": ("泊金港", "Gold's Landing"),
        "Area_41037_Title": ("拉贝尔学部", "Rabelle College"),
        "Term900001_Title": ("核心", "Core"),
        "Term900002_Title": ("调谐器", "Tuner"),
        **_tier_texts("core", "能源核心", "Energy Core"),
        # Only three tiers ship: no base row.
        **{
            key: value
            for key, value in _tier_texts("potion", "共鸣促剂", "Resonance Potion").items()
            if not key.endswith("_3")
        },
        # Same Chinese base, a different English suffix on one tier: no base row.
        **_tier_texts("tube", "密音筒", "Sealed Tube", en_premium="Sealed Canister"),
        # The base pair is already an official record: kept once.
        **_tier_texts("tuner", "调谐器", "Tuner"),
    }
    table_rows = {
        "area/area.json": [{"AreaId": 2103, "Title": "Area_2103_Title"}],
        "item/iteminfo.json": [
            # Two Basic rows: the derived id is the smallest one.
            {"Id": 43020009, "Name": "core_0"},
            *_tier_rows(43020001, "core"),
            *_tier_rows(43010001, "potion")[:3],
            *_tier_rows(36000001, "tube"),
            *_tier_rows(36000011, "tuner"),
        ],
    }
    return _write_arikatsu(tmp_path / "arikatsu", texts, table_rows)


def test_arikatsu_imports_area_titles_no_area_row_references(names_checkout: Path):
    records = iter_records(names_checkout, "arikatsu")
    area_records = sorted(
        (r.category, r.source_file, r.source_id, r.zh, r.en)
        for r in records
        if r.text_key.startswith("Area_")
    )
    assert area_records == [
        ("location", "BinData/area/area.json", "2103", "泊金港", "Gold's Landing"),
        (
            "location",
            "Textmaps/multi_text/MultiText.json",
            "Area_41037_Title",
            "拉贝尔学部",
            "Rabelle College",
        ),
    ]


def test_arikatsu_derives_base_row_only_for_complete_tier_families(
    names_checkout: Path,
):
    records = iter_records(names_checkout, "arikatsu")
    assert [r for r in records if r.source_id.startswith("tier-base:")] == [
        TermRecord(
            "item",
            "BinData/item/iteminfo.json",
            "tier-base:43020001",
            "tier-base:43020001",
            "能源核心",
            "Energy Core",
        )
    ]
    assert [(r.category, r.source_id) for r in records if r.zh == "调谐器"] == [
        ("core_term", "Term900002_Title")
    ]


def test_tier_base_row_owns_its_span_in_both_directions(
    names_checkout: Path, tmp_path: Path
):
    # MY-18: "Energy Cores" is one material, not "Energy" plus 核心 / Core,
    # and the lowercase ordinary phrase stays prose.
    from wuwaterm.sentence import SentenceTranslator

    profile = get_source_profile("arikatsu")
    db_path = tmp_path / "names.db"
    create_database(
        db_path,
        iter_records(names_checkout, profile.name),
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
    translator = SentenceTranslator(db_path)

    def lock(text: str, *, to_chinese: bool):
        return translator._lock_terms_with_references(
            translator.prepare_text(text),
            to_chinese=to_chinese,
            allow_plural_inflection=to_chinese,
            allow_nfkc_spelling=True,
        )

    plural = lock("Farm Energy Cores.", to_chinese=True)
    assert [(zh, en) for _, zh, en in plural.locks] == [("能源核心", "Energy Core")]
    assert plural.restore(plural.locked_text, to_en=False) == "Farm 能源核心."

    ordinary = lock("The energy core of a star.", to_chinese=True)
    assert (ordinary.locks, ordinary.references) == ((), ())
    assert ordinary.locked_text == "The energy core of a star."

    zh_source = lock("今天去刷能源核心。", to_chinese=False)
    assert zh_source.locks == ()
    assert zh_source.references == (("能源核心", "能源核心", "Energy Core", "item"),)
