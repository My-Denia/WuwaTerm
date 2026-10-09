from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from wuwaterm.constants import SOURCE_PROFILES, get_source_profile
from wuwaterm.data_source import DataSourceError, SourceProvenance
from wuwaterm.builder import (
    BuildError,
    build_database,
    build_database_atomic,
    measure_free_text_exclusions,
    measure_free_text_zh_exclusions,
    source_profile_for_data_dir,
)
from wuwaterm.db import category_counts, connect, create_database
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
