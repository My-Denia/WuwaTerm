"""Differential parity and regression tests for ``wuwaterm.fuzzy_index``."""

from __future__ import annotations

from pathlib import Path

from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.fuzzy_index import FuzzyIndex
from wuwaterm.lookup import TermService
from wuwaterm.models import LookupCandidate, TermRecord


def _candidate_signature(
    candidates: list[LookupCandidate],
) -> list[tuple[str, str, str, str, float, str]]:
    return [
        (
            c.entry.zh,
            c.entry.en,
            c.entry.category,
            c.entry.source_id,
            c.score,
            c.reason,
        )
        for c in candidates
    ]


def _build_rich_synthetic_db(db_path: Path) -> None:
    cjk_pool = "星海鸣潮声骸共鸣今汐守岸长离椿风雷冰火光暗裂隙残象律动核心"
    records: list[TermRecord] = [
        # Tie-breaking and category/source priority cases
        TermRecord("item", "Generic.json", "g1", "k1", "守岸人", "Shorekeeper"),
        TermRecord("resonator", "RoleInfo.json", "r1", "RoleInfo_1_Name", "守岸人", "Shorekeeper"),
        TermRecord("speaker", "Talk.json", "s1", "Talk_1", "守岸人", "Shorekeeper"),
        TermRecord(
            "core_term",
            "Core.json",
            "OccupationConfig_漂泊者_Name",
            "OccupationConfig_漂泊者_Name",
            "漂泊者",
            "Rover",
        ),
        TermRecord("resonator", "RoleInfo.json", "r2", "RoleInfo_2_Name", "漂泊者", "Rover"),
        # Duplicate (zh, en) with different pinyin-like / category rows
        TermRecord("term", "Term.json", "t1", "T1", "声骸", "Echo"),
        TermRecord("core_term", "Core.json", "Term850082_Title", "Term850082_Title", "声骸", "Echo"),
        TermRecord("echo", "Echo.json", "e1", "E1", "声骸技能", "Echo Skill"),
        TermRecord("skill", "Skill.json", "sk1", "Sk1", "共鸣技能", "Resonance Skill"),
        TermRecord("resonator", "RoleInfo.json", "1304", "RoleInfo_1304_Name", "今汐", "Jinhsi"),
        TermRecord("resonator", "RoleInfo.json", "1207", "RoleInfo_1207_Name", "椿", "Camellya"),
        # NFKC / punctuation / mixed-script terms
        TermRecord("item", "Item.json", "nfkc1", "N1", "ＡＢＣ核心", "ABC Core"),
        TermRecord("item", "Item.json", "nfkc2", "N2", "Café 咖啡", "Cafe Coffee"),
        TermRecord("item", "Item.json", "sym1", "S1", "α-原型", "Alpha Prototype"),
        # Long pinyin to exercise max(80.0, 96.0 - delta * 0.5) clamping
        TermRecord(
            "skill",
            "Skill.json",
            "longpy",
            "LongPy",
            "超长技能测试名称用于验证拼音前缀分值下限夹紧逻辑是否完全一致",
            "Ultra Long Pinyin Prefix Clamping Test Skill",
        ),
    ]

    # Add >300 entries to exceed both _OCC_BITSET_THRESHOLD (64) and _ZH_BITSET_THRESHOLD (128).
    categories = ("item", "skill", "term", "resonator", "echo", "location")
    for idx in range(320):
        cat = categories[idx % len(categories)]
        a = cjk_pool[idx % len(cjk_pool)]
        b = cjk_pool[(idx // len(cjk_pool)) % len(cjk_pool)]
        c = cjk_pool[(idx + 7) % len(cjk_pool)]
        zh = f"合成{a}{b}{c}{idx:03d}"
        en = f"Synth {cat.title()} Alpha{idx:03d}"
        records.append(
            TermRecord(
                category=cat,
                source_file="Synthetic.json",
                source_id=f"synth_{idx:03d}",
                text_key=f"Synth_{idx:03d}",
                zh=zh,
                en=en,
            )
        )

    with connect(db_path) as conn:
        initialize(conn)
        insert_records(conn, records)
        conn.commit()


def test_empty_dictionary_fuzzy_index_returns_empty(tmp_path: Path):
    db_path = tmp_path / "empty.db"
    with connect(db_path) as conn:
        initialize(conn)
        conn.commit()

    service = TermService(db_path)
    try:
        assert service.lookup("shenghai").candidates == ()
        assert service._fuzzy("今汐", 5) == []
        assert service._fuzzy("", 5) == []
        assert FuzzyIndex.build(()).search("echo", limit=5) == []
    finally:
        service.close()


def test_differential_parity_against_full_scan_on_rich_synthetic_db(tmp_path: Path):
    db_path = tmp_path / "rich.db"
    _build_rich_synthetic_db(db_path)

    service = TermService(db_path)
    try:
        queries = [
            "",
            "   ",
            "a",
            "s",
            "z",
            "声",
            "今",
            "今汐",
            "今汐声",
            "守岸",
            "守岸人人",
            "漂泊者",
            "声骸",
            "shenghai",
            "shenghaii",
            "shanghai",
            "sh",
            "sr",
            "sx",
            "sar",
            "jinhsi",
            "jinhsii",
            "jinxi",
            "shouanren",
            "shouanrenn",
            "chun",
            "echo",
            "Echo",
            "echoo",
            "Echo Skil",
            "Shorekeeper",
            "Shorekeper",
            "Resonance Skil",
            "Synth",
            "synth",
            "hecheng",
            "hecheng050",
            "hechengx",
            "chaochangjineng",
            "ＡＢＣ",
            "abc",
            "café",
            "cafe",
            "α-原型",
            "Alpha Prototyp",
            "---",
            "123",
            "050",
            "不存在的查询词",
            "xyz999notfound",
            "a" * 400,
            "合" * 400,
        ]

        for limit in (0, 1, 3, 5, 25):
            for query in queries:
                indexed = service._fuzzy(query, limit)
                reference = service._fuzzy_full_scan(query, limit)
                assert _candidate_signature(indexed) == _candidate_signature(
                    reference
                ), f"Parity mismatch for query={query!r}, limit={limit}"
    finally:
        service.close()


def test_differential_parity_on_sample_db_and_warm_snapshot(sample_db: Path):
    service = TermService(sample_db)
    try:
        snap = service.snapshot().warm()
        assert snap.fuzzy_index is service.snapshot().fuzzy_index

        entries = service.entries()
        queries: list[str] = [
            "gongmingzhe",
            "shenghai",
            "shenghaii",
            "piaobozhe",
            "jinxi",
            "shouanren",
            "anke",
            "kakaluo",
            "chun",
            "今汐声",
            "共鸣",
            "Shorekeper",
            "Jinhsi",
            "Resonator",
            "巧手",
            "Freezing Frost",
            "Gorges",
        ]
        for entry in entries:
            if entry.zh:
                queries.append(entry.zh[: max(1, len(entry.zh) - 1)])
            if entry.en:
                queries.append(entry.en[: max(1, len(entry.en) - 1)])
            if entry.pinyin:
                queries.append(entry.pinyin[:-1] + "x")

        for limit in (1, 5, 20):
            for query in queries:
                assert _candidate_signature(
                    service._fuzzy(query, limit)
                ) == _candidate_signature(service._fuzzy_full_scan(query, limit))
    finally:
        service.close()


def test_fuzzy_index_tracks_wal_updates_and_isolates_inflight_snapshot(
    sample_db: Path,
):
    with connect(sample_db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()

    service = TermService(sample_db)
    try:
        inflight_snap = service.snapshot()
        before = inflight_snap.fuzzy_index.search("星辰回响", limit=5)
        assert all(c.entry.zh != "星辰回响" for c in before)

        with connect(sample_db) as writer:
            writer.execute("PRAGMA wal_autocheckpoint = 0")
            insert_records(
                writer,
                [
                    TermRecord(
                        category="item",
                        source_file="wal.json",
                        source_id="wal_1",
                        text_key="Wal_1",
                        zh="星辰回响",
                        en="Astral Echo",
                    )
                ],
            )
            writer.commit()

            # Held snapshot remains isolated.
            still_absent = inflight_snap.fuzzy_index.search("星辰回响", limit=5)
            assert all(c.entry.zh != "星辰回响" for c in still_absent)

            # New lookup sees the WAL-committed row via a fresh snapshot.
            hit = service.lookup("xingchenhuixiang", limit=5)
            assert hit.best is not None
            assert hit.best.entry.zh == "星辰回响"
            assert hit.best.entry.en == "Astral Echo"
    finally:
        service.close()


def test_six_tuple_tie_preserves_snapshot_row_order_across_distinct_pairs(
    tmp_path: Path,
):
    """Exact 6-tuple ties across distinct (zh, en) pairs must break by row index."""
    db_path = tmp_path / "tie_distinct.db"
    with connect(db_path) as conn:
        initialize(conn)
        conn.executemany(
            """
            INSERT INTO terms(
              category, source_file, source_id, text_key, zh, en, zh_norm, en_norm,
              pinyin, pinyin_abbrev, priority
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "item",
                    "A.json",
                    "same_id",
                    "k1",
                    "00000000",
                    "abcdefgh",
                    "00000000",
                    "abcdefgh",
                    "p0",
                    "a0",
                    5,
                ),
                (
                    "item",
                    "B.json",
                    "same_id",
                    "k2",
                    "abcdefgx",
                    "abcdefgh",
                    "abcdefgx",
                    "abcdefgh",
                    "p1",
                    "a1",
                    5,
                ),
            ],
        )
        conn.commit()

    service = TermService(db_path)
    try:
        for limit in (1, 2):
            indexed = service._fuzzy("abcdefgh", limit)
            reference = service._fuzzy_full_scan("abcdefgh", limit)
            assert _candidate_signature(indexed) == _candidate_signature(reference)
            assert indexed[0].entry.zh == "00000000"
    finally:
        service.close()


def test_six_tuple_tie_preserves_snapshot_row_order_for_same_pair(tmp_path: Path):
    """Exact 6-tuple ties on the same (zh, en) pair must keep the smaller row index."""
    db_path = tmp_path / "tie_same.db"
    with connect(db_path) as conn:
        initialize(conn)
        conn.executemany(
            """
            INSERT INTO terms(
              category, source_file, source_id, text_key, zh, en, zh_norm, en_norm,
              pinyin, pinyin_abbrev, priority
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "item",
                    "A.json",
                    "s1",
                    "k1",
                    "xyzwvuts",
                    "Star Sea",
                    "xyzwvuts",
                    "starsea",
                    "zzzzzz",
                    "z",
                    5,
                ),
                (
                    "item",
                    "B.json",
                    "s1",
                    "k2",
                    "xyzwvuts",
                    "Star Sea",
                    "xyzwvuts",
                    "starsea",
                    "xyzwvuts" + "q" * 32,
                    "x",
                    5,
                ),
            ],
        )
        conn.commit()

    service = TermService(db_path)
    try:
        indexed = service._fuzzy("xyzwvuts", 5)
        reference = service._fuzzy_full_scan("xyzwvuts", 5)
        assert _candidate_signature(indexed) == _candidate_signature(reference)
        assert indexed[0].entry.source_file == "A.json"
        assert indexed[0].reason == "fuzzy"
    finally:
        service.close()
