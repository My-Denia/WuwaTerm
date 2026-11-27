"""Differential Aho-Corasick parity tests and SQLite WAL snapshot regression tests."""

from __future__ import annotations

import asyncio
import gc
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from wuwaterm.application import build_term_service, build_translator, dictionary_identity
from wuwaterm.builder import BuildError, build_database_atomic
from wuwaterm.cjk_span import crosses_cjk_word_boundary, single_character_name_context_ok
from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.english_plural import plural_suffix_candidates as _plural_suffix_candidates
from wuwaterm.lookup import DictionarySnapshot, TermService
from wuwaterm.models import TermRecord
from wuwaterm.sentence import SentenceTranslator
from wuwaterm.term_matching import (
    TermSpan,
    TermSpanMatcher,
    _ascii_word_boundaries_ok,
    _case_variant_boundaries_ok,
    _matched_span_ok,
    _nfkc_spelling,
    exact_lockable_sources,
    select_term_spans,
)
from wuwaterm_api.app import create_app
from wuwaterm_api.auth import DeviceStore, SCOPE_META, SCOPE_TRANSLATE, TOKEN_SCHEME
from wuwaterm_api.settings import ApiSettings


def _naive_select_term_spans(
    text: str,
    lockable: tuple[tuple[str, tuple[str, str]], ...],
    context_text: str,
    context_offset: int,
    *,
    allow_plural_inflection: bool = False,
    nfkc_spelling_sources: frozenset[str] = frozenset(),
) -> list[TermSpan]:
    """Reference pre-Aho-Corasick per-surface ``text.find()`` selector."""
    spans: list[TermSpan] = []
    for order, (source, official) in enumerate(lockable):
        if not source:
            continue
        start = text.find(source)
        while start != -1:
            end = start + len(source)
            if _matched_span_ok(
                context_text, context_offset, start, end, source, official
            ):
                spans.append(
                    TermSpan(
                        start=start,
                        end=end,
                        source=source,
                        official=official,
                        order=order,
                    )
                )
            start = text.find(source, start + 1)

    if nfkc_spelling_sources:
        exact_bounds = {(span.start, span.end) for span in spans}
        for order, (source, official) in enumerate(lockable):
            if source not in nfkc_spelling_sources:
                continue
            spelling = _nfkc_spelling(source)
            if not spelling or spelling == source:
                continue
            start = text.find(spelling)
            while start != -1:
                end = start + len(spelling)
                if (start, end) not in exact_bounds and _matched_span_ok(
                    context_text, context_offset, start, end, spelling, official
                ):
                    spans.append(
                        TermSpan(
                            start=start,
                            end=end,
                            source=source,
                            official=official,
                            order=order,
                        )
                    )
                start = text.find(spelling, start + 1)

    if allow_plural_inflection:
        exact_bounds = {(span.start, span.end) for span in spans}
        for order, (source, official) in enumerate(lockable):
            for suffix in _plural_suffix_candidates(source, official):
                inflected = source + suffix
                start = text.find(inflected)
                while start != -1:
                    end = start + len(inflected)
                    if (start, end) not in exact_bounds and (
                        _ascii_word_boundaries_ok(
                            context_text,
                            context_offset + start,
                            context_offset + end,
                            inflected,
                        )
                        and (
                            source in official
                            or _case_variant_boundaries_ok(
                                context_text,
                                context_offset + start,
                                context_offset + end,
                            )
                        )
                        and (
                            len(source) > 1
                            or single_character_name_context_ok(
                                context_text,
                                context_offset + start,
                                context_offset + end,
                            )
                        )
                        and not crosses_cjk_word_boundary(
                            context_text, context_offset + start, context_offset + end
                        )
                    ):
                        spans.append(
                            TermSpan(
                                start=start,
                                end=end,
                                source=inflected,
                                official=official,
                                order=order,
                            )
                        )
                    start = text.find(inflected, start + 1)

    selected: list[TermSpan] = []
    occupied: list[tuple[int, int]] = []
    for span in sorted(
        spans,
        key=lambda item: (
            -(item.end - item.start),
            item.order,
            item.start,
            item.source,
        ),
    ):
        if any(span.start < end and start < span.end for start, end in occupied):
            continue
        selected.append(span)
        occupied.append((span.start, span.end))

    selected.sort(key=lambda item: item.start)
    return selected


def _locate_real_db() -> Path | None:
    root = Path(__file__).resolve().parents[1]
    candidates = [root / "data" / "cjk-census-3.7.db"]
    try:
        common_dir = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=root,
            text=True,
        ).strip()
        if common_dir:
            candidates.append(
                (root / common_dir).resolve().parent / "data" / "cjk-census-3.7.db"
            )
    except (OSError, subprocess.SubprocessError):
        pass
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


@pytest.mark.parametrize("allow_plural", [False, True])
@pytest.mark.parametrize("use_nfkc", [False, True])
def test_ac_select_term_spans_differential_boundary_and_priority_cases(
    allow_plural: bool, use_nfkc: bool
):
    lockable: tuple[tuple[str, tuple[str, str]], ...] = (
        ("声骸", ("声骸", "Echo")),
        ("声骸技能", ("声骸技能", "Echo Skill")),
        ("技能", ("技能", "Skill")),
        ("椿", ("椿", "Camellya")),
        ("Star", ("星星", "Star")),
        ("Stars", ("群星", "Stars")),
        ("Echo", ("声骸", "Echo")),
        ("Resonator", ("共鸣者", "Resonator")),
        ("jinhsi", ("今汐", "Jinhsi")),
        ("élodie", ("艾洛迪", "Élodie")),
        ("ab", ("甲乙", "ab")),
        ("bab", ("乙甲乙", "bab")),
        ("🌊术语", ("海浪术语", "Wave Term")),
        ("术语", ("普通术语", "Term")),
        ("镜锁妄世，心照红尘", ("镜锁妄世，心照红尘", "Mirror Locked")),
        ("ﬁre", ("火焰", "fire")),
        ("fire", ("火", "fire")),
        ("Echo", ("重复声骸", "Duplicate Echo")),
    )
    nfkc_sources = (
        frozenset({"镜锁妄世，心照红尘", "ﬁre"}) if use_nfkc else frozenset()
    )
    matcher = TermSpanMatcher.build(
        lockable,
        nfkc_spelling_sources=frozenset({"镜锁妄世，心照红尘", "ﬁre"}),
        include_plural=True,
    )

    cases: list[tuple[str, str, int]] = [
        ("", "", 0),
        ("🌊使用声骸技能ababa✨🌊术语声骸", "🌊使用声骸技能ababa✨🌊术语声骸", 0),
        ("回声骸骨里没有声骸，但欢迎椿和红椿蕊。", "回声骸骨里没有声骸，但欢迎椿和红椿蕊。", 0),
        (
            "Star and Stars with Echoes, New Echoes, Resonators, Resonatorses, jinhsi, xjinhsi, élodie, xélodie.",
            "Star and Stars with Echoes, New Echoes, Resonators, Resonatorses, jinhsi, xjinhsi, élodie, xélodie.",
            0,
        ),
        (
            "镜锁妄世,心照红尘 and fire and ﬁre.",
            "镜锁妄世,心照红尘 and fire and ﬁre.",
            0,
        ),
        # Context-offset glue across invisible markup boundary: "Resonator" + "s"
        ("Resonator", "Use Resonators now", 4),
        ("Echo", "New Echos here", 4),
        ("Echo", "Use Echo here", 4),
    ]

    for text, context_text, context_offset in cases:
        expected = _naive_select_term_spans(
            text,
            lockable,
            context_text,
            context_offset,
            allow_plural_inflection=allow_plural,
            nfkc_spelling_sources=nfkc_sources,
        )
        got_cached = select_term_spans(
            text,
            lockable,
            context_text,
            context_offset,
            allow_plural_inflection=allow_plural,
            nfkc_spelling_sources=nfkc_sources,
        )
        got_explicit = select_term_spans(
            text,
            lockable,
            context_text,
            context_offset,
            allow_plural_inflection=allow_plural,
            nfkc_spelling_sources=nfkc_sources,
            matcher=matcher,
        )
        assert got_cached == expected
        assert got_explicit == expected


def test_ac_select_term_spans_differential_against_real_dictionary(sample_db):
    db_path = _locate_real_db() or sample_db
    translator = SentenceTranslator(db_path)
    bundle = translator.service.snapshot().lockable_snapshot()
    lockable = bundle.eligible_lockable
    nfkc_sources = bundle.nfkc_spelling_sources
    exact_only = exact_lockable_sources(translator.service.entries())

    texts = [
        "今汐和守岸人在共鸣者测试中装备了声骸技能，给椿留下一份记录，同时避开回声骸骨与红椿蕊。",
        "Resonators equipped Echoes while Jinhsi and Shorekeeper reviewed Stars and Echo Skill interactions.",
        "🌊✨ 漂泊者、卡卡罗、安可与声骸共振；jinhsi 与 JINHSI，New Echoes 以及 Resonatorses。",
    ]

    for allow_plural in (False, True):
        for active_nfkc in (frozenset(), nfkc_sources):
            for text in texts:
                expected = _naive_select_term_spans(
                    text,
                    lockable,
                    text,
                    0,
                    allow_plural_inflection=allow_plural,
                    nfkc_spelling_sources=active_nfkc,
                )
                got = select_term_spans(
                    text,
                    lockable,
                    text,
                    0,
                    allow_plural_inflection=allow_plural,
                    nfkc_spelling_sources=active_nfkc,
                    matcher=bundle.matcher,
                )
                assert got == expected

    # Verify standalone exact_lockable_sources path (builder/corpus measurement).
    for text in texts:
        assert select_term_spans(text, exact_only, text, 0) == _naive_select_term_spans(
            text, exact_only, text, 0
        )


def test_wal_commit_with_unchanged_main_stat_refreshes_all_paths_and_isolates_inflight(
    monkeypatch, tmp_path: Path, sample_db: Path
):
    """Zero-network SQLite WAL regression: unchanged main-file stat must invalidate all caches."""
    monkeypatch.delenv("WUWATERM_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_MODEL", raising=False)

    wal_db = tmp_path / "wal-unified.db"
    wal_db.write_bytes(sample_db.read_bytes())
    init_conn = connect(wal_db)
    try:
        mode = init_conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert mode is not None and mode[0].lower() == "wal"
        init_conn.commit()
    finally:
        init_conn.close()

    service = build_term_service(wal_db)
    translator = build_translator(wal_db, service=service)
    standalone_translator = SentenceTranslator(wal_db)

    device_db = tmp_path / "devices.db"
    store = DeviceStore(device_db)
    secret = "wal-test-secret-0123456789abcdef"
    device = store.issue("wal-test-device", [SCOPE_META, SCOPE_TRANSLATE], secret=secret)
    token = f"{TOKEN_SCHEME}.{device.device_id}.{secret}"
    app = create_app(
        ApiSettings(db_path=wal_db, device_db_path=device_db),
        device_store=store,
        term_service=service,
        translator=translator,
    )

    async def get_dictionary() -> dict[str, object]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            resp = await client.get(
                "/v1/dictionary", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 200, resp.text
            return resp.json()

    # Warm all three paths + capture an in-flight immutable snapshot before WAL write.
    dict_before = asyncio.run(get_dictionary())
    identity_before = dictionary_identity(service)
    assert dict_before["revision"] == identity_before.revision
    # WAL mode bypasses caching and holds no persistent handle while staying consistent.
    assert service.dictionary_identity() == identity_before
    assert service._version_conn is None

    fuzzy_before = service.lookup("xingchenmohe")
    assert all(c.entry.zh != "星尘魔核" for c in fuzzy_before.candidates)
    assert service.lookup("shenghai").best is not None

    lock_before = translator.lock_terms("请携带星尘魔核进入副本。")
    assert lock_before.locks == ()
    standalone_before = standalone_translator.lock_terms("请携带星尘魔核进入副本。")
    assert standalone_before.locks == ()

    inflight_snapshot: DictionarySnapshot = service.snapshot()
    inflight_lockable = inflight_snapshot.lockable_snapshot()
    inflight_scoring_count = len(inflight_snapshot.scoring_rows)
    inflight_identity = inflight_snapshot.dictionary_identity

    # Record main DB file stat strictly BEFORE the uncheckpointed WAL write.
    stat_before = os.stat(wal_db)
    main_stat_before = (
        stat_before.st_dev,
        stat_before.st_ino,
        stat_before.st_size,
        stat_before.st_mtime_ns,
        stat_before.st_ctime_ns,
    )

    # Commit a new term into the WAL without checkpointing the main DB file.
    # Keep writer open while verifying so SQLite does not auto-checkpoint on last close.
    writer = connect(wal_db)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        insert_records(
            writer,
            [
                TermRecord(
                    category="item",
                    source_file="BinData/item/wal_test.json",
                    source_id="990001",
                    text_key="Item_990001_Name",
                    zh="星尘魔核",
                    en="Stardust Core",
                )
            ],
        )
        writer.commit()

        stat_after = os.stat(wal_db)
        main_stat_after = (
            stat_after.st_dev,
            stat_after.st_ino,
            stat_after.st_size,
            stat_after.st_mtime_ns,
            stat_after.st_ctime_ns,
        )
        assert main_stat_after == main_stat_before, (
            "main DB file stat changed unexpectedly; test must exercise uncheckpointed WAL"
        )
        wal_file = Path(f"{wal_db}-wal")
        assert wal_file.is_file() and wal_file.stat().st_size > 0

        # 1. /v1/dictionary and TermService.dictionary_identity() immediately see new revision & count.
        dict_after = asyncio.run(get_dictionary())
        identity_after = dictionary_identity(service)
        assert dict_after["revision"] == identity_after.revision
        assert dict_after["revision"] != dict_before["revision"]
        assert dict_after["term_count"] == int(dict_before["term_count"]) + 1  # type: ignore[arg-type]

        # 2. Fuzzy lookup and exact lookup immediately see the WAL-committed term.
        fuzzy_after = service.lookup("xingchenmohe")
        assert fuzzy_after.best is not None
        assert fuzzy_after.best.entry.zh == "星尘魔核"
        assert fuzzy_after.best.entry.en == "Stardust Core"
        exact_after = service.lookup_exact("星尘魔核")
        assert exact_after.exact is True
        assert exact_after.best is not None
        assert exact_after.best.entry.en == "Stardust Core"

        # 3. Translation term locking (both shared-service and standalone) immediately locks the new term.
        lock_after = translator.lock_terms("请携带星尘魔核进入副本。")
        assert lock_after.restore(lock_after.locked_text) == "请携带Stardust Core进入副本。"
        standalone_after = standalone_translator.lock_terms("请携带星尘魔核进入副本。")
        assert (
            standalone_after.restore(standalone_after.locked_text)
            == "请携带Stardust Core进入副本。"
        )

        # 4. In-flight snapshot reference remains strictly immutable and isolated.
        current_snapshot = service.snapshot()
        assert current_snapshot is not inflight_snapshot
        assert inflight_snapshot.dictionary_identity == inflight_identity
        assert inflight_snapshot.term_count == inflight_scoring_count
        assert all(entry.zh != "星尘魔核" for entry in inflight_snapshot.entries)
        assert inflight_snapshot.lockable_snapshot() is inflight_lockable
        assert all(src != "星尘魔核" for src, _ in inflight_lockable.eligible_lockable)
    finally:
        writer.close()

    service.close()
    standalone_translator.service.close()


def test_two_term_services_atomic_wal_db_replacement_does_not_corrupt_new_db(
    tmp_path: Path, sample_db: Path
):
    """Two TermServices reading a WAL DB must not corrupt an atomically replaced DB."""
    live_db = tmp_path / "live.db"
    shutil.copy2(sample_db, live_db)
    init_conn = connect(live_db)
    try:
        mode = init_conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert mode is not None and mode[0].lower() == "wal"
        init_conn.commit()
    finally:
        init_conn.close()

    svc_a = TermService(live_db)
    svc_b = TermService(live_db)

    # Read the old WAL DB through both TermServices while an uncheckpointed WAL
    # commit is active, then close the writer (the normal state before atomic DB
    # promotion). With no persistent watcher connections held by TermService,
    # SQLite cleanly releases the old WAL so os.replace() cannot leave stale WAL
    # pages that corrupt the replacement database.
    old_writer = connect(live_db)
    try:
        old_writer.execute("PRAGMA wal_autocheckpoint = 0")
        insert_records(
            old_writer,
            [
                TermRecord(
                    category="item",
                    source_file="old_wal.json",
                    source_id="old-1",
                    text_key="OldWal_1",
                    zh="旧库词条",
                    en="Old WAL Term",
                )
            ],
        )
        old_writer.commit()

        assert svc_a.lookup_exact("旧库词条").exact is True
        assert svc_b.lookup_exact("旧库词条").exact is True
        old_rev = svc_a.dictionary_identity().revision
    finally:
        old_writer.close()

    # Build a clean replacement candidate with completely distinct content.
    candidate = tmp_path / "candidate.db"
    cand_conn = connect(candidate)
    try:
        initialize(cand_conn)
        cand_conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", "2"),
                ("source_commit", "replacement-commit"),
                ("source_game_version", "3.8"),
            ],
        )
        insert_records(
            cand_conn,
            [
                TermRecord(
                    category="item",
                    source_file="new_db.json",
                    source_id="new-1",
                    text_key="NewDb_1",
                    zh="新库词条",
                    en="Fresh Replaced Term",
                )
            ],
        )
        cand_conn.commit()
    finally:
        cand_conn.close()

    os.replace(candidate, live_db)

    try:
        # Both TermServices must immediately see the new DB and never write old WAL pages into it.
        assert svc_a.lookup_exact("新库词条").exact is True
        assert svc_a.lookup_exact("旧库词条").exact is False
        assert svc_b.lookup_exact("新库词条").exact is True
        assert svc_b.lookup_exact("旧库词条").exact is False
        assert svc_a.dictionary_identity().revision == svc_b.dictionary_identity().revision
        assert svc_a.dictionary_identity().revision != old_rev
    finally:
        svc_a.close()
        svc_b.close()

    # Independently reopen the replaced DB and verify integrity and exact contents.
    verify_conn = sqlite3.connect(live_db)
    try:
        integrity = verify_conn.execute("PRAGMA integrity_check").fetchone()
        assert integrity is not None and integrity[0] == "ok"
        rows = verify_conn.execute("SELECT zh, en FROM terms").fetchall()
        assert rows == [("新库词条", "Fresh Replaced Term")]
    finally:
        verify_conn.close()


def test_asgi_lifespan_exit_leaves_no_open_sqlite_handles_for_wal_mode_switch(
    monkeypatch, tmp_path: Path, sample_db: Path
):
    """After ASGI lifespan shutdown, no open SQLite handle may block PRAGMA journal_mode=DELETE."""
    monkeypatch.delenv("WUWATERM_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_MODEL", raising=False)

    wal_db = tmp_path / "lifespan-wal.db"
    shutil.copy2(sample_db, wal_db)
    init_conn = connect(wal_db)
    try:
        mode = init_conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert mode is not None and mode[0].lower() == "wal"
        init_conn.commit()
    finally:
        init_conn.close()

    device_db = tmp_path / "devices.db"
    store = DeviceStore(device_db)
    secret = "lifespan-test-secret-0123456789abcdef"
    device = store.issue("lifespan-device", [SCOPE_META, SCOPE_TRANSLATE], secret=secret)
    token = f"{TOKEN_SCHEME}.{device.device_id}.{secret}"
    headers = {"Authorization": f"Bearer {token}"}

    app = create_app(
        ApiSettings(db_path=wal_db, device_db_path=device_db),
        device_store=store,
    )
    with TestClient(app) as client:
        assert client.get("/v1/dictionary", headers=headers).status_code == 200
        assert client.get("/v1/terms", params={"q": "声骸"}, headers=headers).status_code == 200

    # With lifespan exited, switching WAL -> DELETE with timeout=0.0 must succeed immediately.
    conn = sqlite3.connect(wal_db, timeout=0.0)
    try:
        switched = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        assert switched is not None and switched[0].lower() == "delete"
    finally:
        conn.close()


def test_review_snapshot_metadata_mutation_does_not_pollute_cache(sample_db: Path):
    """Mutating metadata on one review_snapshot() must never pollute subsequent reads."""
    service = TermService(sample_db)
    snap1 = service.review_snapshot()
    original_commit = snap1.metadata.get("source_commit")

    snap1.metadata["source_commit"] = "poisoned-commit"
    snap1.metadata["injected_key"] = "bad"

    snap2 = service.review_snapshot()
    assert snap2.metadata.get("source_commit") == original_commit
    assert "injected_key" not in snap2.metadata
    assert service.metadata().get("source_commit") == original_commit
    assert "injected_key" not in service.metadata()
    assert service.dictionary_identity().source_commit == original_commit


def test_exact_lookup_tie_breaking_matches_sqlite_id_order_for_shared_en_surface(
    tmp_path: Path,
):
    """Tied exact candidates sharing English surface preserve SQLite id ASC order."""
    tie_db = tmp_path / "tie-order.db"
    with connect(tie_db) as conn:
        initialize(conn)
        conn.execute("INSERT INTO metadata(key, value) VALUES ('schema_version', '2')")
        # Insert 长离 (id=1, higher Unicode codepoint) BEFORE 今汐 (id=2, lower codepoint)
        # with identical category, source_file, source_id, en, and len(zh) == 2.
        conn.executemany(
            """
            INSERT INTO terms(
              id, category, source_file, source_id, text_key, zh, en, zh_norm, en_norm,
              pinyin, pinyin_abbrev, priority
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "resonator", "RoleInfo.json", "shared-id", "k1", "长离", "Shared Hero", "长离", "sharedhero", "changli", "cl", 1),
                (2, "resonator", "RoleInfo.json", "shared-id", "k2", "今汐", "Shared Hero", "今汐", "sharedhero", "jinxi", "jx", 1),
            ],
        )
        conn.commit()
        sql_order = [
            row["zh"]
            for row in conn.execute(
                """
                SELECT zh FROM terms
                WHERE zh_norm = ? OR en_norm = ?
                ORDER BY priority, length(zh), category, source_file, source_id
                """,
                ("sharedhero", "sharedhero"),
            ).fetchall()
        ]

    assert sql_order == ["长离", "今汐"]
    service = TermService(tie_db)
    result = service.lookup_exact("Shared Hero", limit=10)
    assert result.exact is True
    assert [cand.entry.zh for cand in result.candidates] == ["长离", "今汐"]


def test_benchmark_core_perf_fail_closed_and_wal_backup(tmp_path: Path, sample_db: Path):
    """Benchmark script must fail-closed on missing --db and copy WAL commits via backup()."""
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "benchmark_core_perf.py"
    missing = tmp_path / "does-not-exist.db"
    proc = subprocess.run(
        [sys.executable, str(script), "--db", str(missing), "--quick"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "does not exist" in (proc.stderr + proc.stdout)

    # Verify _build_scaled_db preserves uncheckpointed WAL commits via sqlite3.backup().
    from scripts import benchmark_core_perf as bench

    wal_src = tmp_path / "wal-src.db"
    shutil.copy2(sample_db, wal_src)
    with connect(wal_src) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()

    writer = connect(wal_src)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        insert_records(
            writer,
            [
                TermRecord(
                    category="item",
                    source_file="wal_backup.json",
                    source_id="wb-1",
                    text_key="WalBackup_1",
                    zh="备份验证词条",
                    en="WAL Backup Term",
                )
            ],
        )
        writer.commit()

        dest = tmp_path / "copied.db"
        _, count = bench._build_scaled_db(wal_src, 0, dest)
        assert count > 0
        copied_svc = TermService(dest)
        assert copied_svc.lookup_exact("备份验证词条").exact is True
    finally:
        writer.close()


def test_wal_visibility_window_does_not_cache_stale_snapshot_before_shm_commit(
    tmp_path: Path, sample_db: Path
):
    """Non-empty -wal bypasses caching so WAL visibility windows can never pin stale data."""
    wal_db = tmp_path / "wal-window.db"
    shutil.copy2(sample_db, wal_db)
    init_conn = connect(wal_db)
    try:
        assert init_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        init_conn.commit()
    finally:
        init_conn.close()

    service = TermService(wal_db)
    writer = connect(wal_db)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        insert_records(
            writer,
            [
                TermRecord(
                    category="item",
                    source_file="win.json",
                    source_id="w1",
                    text_key="Win_1",
                    zh="窗口初始词",
                    en="Window Initial",
                )
            ],
        )
        writer.commit()
        assert service.lookup_exact("窗口初始词").exact is True
        rev_v1 = service.dictionary_identity().revision
        # Non-empty -wal must not populate the snapshot cache.
        assert Path(f"{wal_db}-wal").stat().st_size > 0
        assert service._snapshot_cache is None

        # Simulate extra -wal bytes synced before -shm mxFrame advances.
        wal_path = Path(f"{wal_db}-wal")
        wal_before = wal_path.read_bytes()
        with open(wal_path, "ab") as wal_handle:
            wal_handle.write(b"\x00" * 4120)
        try:
            assert service.dictionary_identity().revision == rev_v1
            assert service._snapshot_cache is None
        finally:
            wal_path.write_bytes(wal_before)

        # Writer commits a second row; service immediately sees v2 with zero stale caching.
        insert_records(
            writer,
            [
                TermRecord(
                    category="item",
                    source_file="win.json",
                    source_id="w2",
                    text_key="Win_2",
                    zh="窗口新词",
                    en="Window Committed",
                )
            ],
        )
        writer.commit()

        assert service.lookup_exact("窗口新词").exact is True
        assert service.dictionary_identity().revision != rev_v1
    finally:
        writer.close()
        service.close()


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_cross_process_write_lock_preserved_across_queries_close_and_gc(
    tmp_path: Path, sample_db: Path, journal_mode: str
):
    """TermService queries, close(), and GC must never release another in-process write lock."""
    db_path = tmp_path / f"lock-{journal_mode.lower()}.db"
    shutil.copy2(sample_db, db_path)
    init_conn = connect(db_path)
    try:
        mode = init_conn.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()
        assert mode is not None and mode[0].upper() == journal_mode
        init_conn.commit()
    finally:
        init_conn.close()

    probe_script = (
        "import sqlite3, sys; "
        "c = sqlite3.connect(sys.argv[1], timeout=0.0); "
        "c.execute('BEGIN IMMEDIATE')"
    )

    def assert_other_process_busy() -> None:
        proc = subprocess.run(
            [sys.executable, "-c", probe_script, str(db_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0, "other process unexpectedly acquired BEGIN IMMEDIATE"
        assert "locked" in proc.stderr.lower()

    writer = sqlite3.connect(db_path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE terms SET en = en WHERE id = (SELECT MIN(id) FROM terms)"
        )
        assert_other_process_busy()

        service = TermService(db_path)
        if journal_mode == "WAL":
            # In WAL mode, ?mode=ro reads concurrently while writer holds BEGIN IMMEDIATE.
            assert service.lookup_exact("声骸").exact is True
            assert service.dictionary_identity().term_count > 0
            assert_other_process_busy()
        else:
            # In DELETE mode, snapshot/lookup must not drop writer's RESERVED/EXCLUSIVE lock.
            _ = service._identity()
            assert service.lookup_exact("声骸").exact is True
            assert service.dictionary_identity().term_count > 0
            assert_other_process_busy()

        service.close()
        assert_other_process_busy()

        del service
        gc.collect()
        assert_other_process_busy()
    finally:
        if writer.in_transaction:
            writer.execute("ROLLBACK")
        writer.close()


def test_stale_nonempty_wal_atomic_replace_never_corrupts_new_db_bytes(
    tmp_path: Path, sample_db: Path
):
    """?mode=ro never writes old WAL pages into an atomically replaced DB; clean checkpoint swaps cleanly."""
    live_db = tmp_path / "stale-wal-replace.db"
    shutil.copy2(sample_db, live_db)
    init_conn = connect(live_db)
    try:
        assert init_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        init_conn.commit()
    finally:
        init_conn.close()

    # Hold a reader so a non-empty -wal remains on disk after writer commits and closes.
    pinned_reader = connect(live_db)
    pinned_reader.execute("BEGIN")
    pinned_reader.execute("SELECT COUNT(*) FROM terms").fetchone()

    writer = connect(live_db)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        insert_records(
            writer,
            [
                TermRecord(
                    category="item",
                    source_file="old.json",
                    source_id="o1",
                    text_key="Old_1",
                    zh="旧尾词条",
                    en="Old Tail Term",
                )
            ],
        )
        writer.commit()
    finally:
        writer.close()

    assert Path(f"{live_db}-wal").stat().st_size > 0

    candidate = tmp_path / "clean-candidate.db"
    cand_conn = connect(candidate)
    try:
        initialize(cand_conn)
        cand_conn.execute("INSERT INTO metadata VALUES ('schema_version', '2')")
        insert_records(
            cand_conn,
            [
                TermRecord(
                    category="item",
                    source_file="clean.json",
                    source_id="c1",
                    text_key="Clean_1",
                    zh="干净新词",
                    en="Clean Replacement",
                )
            ],
        )
        cand_conn.commit()
    finally:
        cand_conn.close()

    expected_candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()

    # Atomically replace live_db even while stale -wal is still present on disk.
    os.replace(candidate, live_db)

    service = TermService(live_db)
    try:
        # First query via ?mode=ro must NEVER write old WAL pages into the replaced main DB file.
        _ = service.lookup_exact("干净新词")
        _ = service.dictionary_identity()
    finally:
        service.close()
        pinned_reader.close()

    actual_live_sha = hashlib.sha256(live_db.read_bytes()).hexdigest()
    assert actual_live_sha == expected_candidate_sha

    # Proper WAL promotion: checkpoint(TRUNCATE) old WAL before os.replace(), then read new DB.
    promo_db = tmp_path / "promo.db"
    shutil.copy2(sample_db, promo_db)
    p_conn = connect(promo_db)
    try:
        assert p_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        p_conn.execute("PRAGMA wal_autocheckpoint = 0")
        insert_records(
            p_conn,
            [
                TermRecord(
                    category="item",
                    source_file="p_old.json",
                    source_id="po1",
                    text_key="PO_1",
                    zh="旧升级词",
                    en="Old Promo Term",
                )
            ],
        )
        p_conn.commit()
    finally:
        p_conn.close()

    promo_svc = TermService(promo_db)
    assert promo_svc.lookup_exact("旧升级词").exact is True

    ckpt = connect(promo_db)
    try:
        ckpt.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        ckpt.close()

    promo_cand = tmp_path / "promo-candidate.db"
    pc = connect(promo_cand)
    try:
        initialize(pc)
        pc.execute("INSERT INTO metadata VALUES ('schema_version', '2')")
        insert_records(
            pc,
            [
                TermRecord(
                    category="item",
                    source_file="p_new.json",
                    source_id="pn1",
                    text_key="PN_1",
                    zh="新升级词",
                    en="New Promo Term",
                )
            ],
        )
        pc.commit()
    finally:
        pc.close()

    os.replace(promo_cand, promo_db)
    assert promo_svc.lookup_exact("新升级词").exact is True
    assert promo_svc.lookup_exact("旧升级词").exact is False
    promo_svc.close()

    # Read-only directory + file still works seamlessly via ?mode=ro.
    ro_dir = tmp_path / "ro-dir"
    ro_dir.mkdir()
    ro_db = ro_dir / "ro.db"
    shutil.copy2(promo_db, ro_db)
    os.chmod(ro_db, 0o444)
    os.chmod(ro_dir, 0o555)
    try:
        ro_svc = TermService(ro_db)
        assert ro_svc.lookup_exact("新升级词").exact is True
        ro_svc.close()
    finally:
        os.chmod(ro_dir, 0o755)
        os.chmod(ro_db, 0o644)


def test_delete_mode_coarse_timestamp_same_size_inplace_update_invalidates_cache(
    monkeypatch, tmp_path: Path, sample_db: Path
):
    """Whole-second coarse timestamps bypass cache so same-second same-size DELETE updates are seen."""
    db_path = tmp_path / "coarse-delete.db"
    shutil.copy2(sample_db, db_path)

    # Set coarse whole-second mtime and freeze stat to whole-second mtime & ctime (HFS+ semantics).
    whole_sec = int(os.stat(db_path).st_mtime)
    whole_sec_ns = whole_sec * 1_000_000_000
    os.utime(db_path, ns=(whole_sec_ns, whole_sec_ns))
    frozen_stat = os.stat(db_path)

    from wuwaterm import lookup as lookup_mod

    real_stat_tuple = lookup_mod._stat_tuple

    def coarse_stat_tuple(path: Path) -> tuple[int, int, int, int, int]:
        if path == db_path.resolve(strict=False):
            return (
                frozen_stat.st_dev,
                frozen_stat.st_ino,
                frozen_stat.st_size,
                whole_sec_ns,
                whole_sec_ns,
            )
        return real_stat_tuple(path)

    monkeypatch.setattr(lookup_mod, "_stat_tuple", coarse_stat_tuple)

    service = TermService(db_path)
    before_id = service.dictionary_identity()
    before_exact = service.lookup_exact("声骸")
    assert before_exact.best is not None and before_exact.best.entry.en == "Echo"
    assert service._snapshot_cache is None

    # In-place same-length UPDATE in DELETE journal mode within the same whole second.
    w = connect(db_path)
    try:
        w.execute(
            "UPDATE terms SET en = ?, en_norm = ? WHERE zh = ?",
            ("Ech0", "ech0", "声骸"),
        )
        w.commit()
    finally:
        w.close()

    after_id = service.dictionary_identity()
    after_exact = service.lookup_exact("声骸")
    assert after_id.revision != before_id.revision
    assert after_exact.best is not None and after_exact.best.entry.en == "Ech0"
    service.close()


@pytest.mark.parametrize("run_analyze", [False, True])
def test_exact_lookup_cross_index_zh_norm_vs_en_norm_tie_matches_sqlite(
    tmp_path: Path, run_analyze: bool
):
    """Exact lookup matches origin/main SQLite ordering both without and with ANALYZE stats."""
    tie_db = tmp_path / f"cross-index-tie-{run_analyze}.db"
    conn = connect(tie_db)
    try:
        initialize(conn)
        conn.execute("INSERT INTO metadata(key, value) VALUES ('schema_version', '2')")
        conn.executemany(
            """
            INSERT INTO terms(
              id, category, source_file, source_id, text_key, zh, en, zh_norm, en_norm,
              pinyin, pinyin_abbrev, priority
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "term", "a.json", "s1", "k1", "甲乙", "Foo", "甲乙", "foo", "jiayi", "jy", 5),
                (2, "term", "a.json", "s1", "k2", "fo", "Foo", "foo", "foo", "fo", "f", 5),
            ],
        )
        conn.commit()
        if run_analyze:
            conn.execute("ANALYZE")
            conn.commit()
        sql_order = [
            (row["id"], row["zh"])
            for row in conn.execute(
                """
                SELECT id, zh FROM terms
                WHERE zh_norm = ? OR en_norm = ?
                ORDER BY priority, length(zh), category, source_file, source_id
                """,
                ("foo", "foo"),
            ).fetchall()
        ]
    finally:
        conn.close()

    service = TermService(tie_db)
    result = service.lookup_exact("Foo", limit=10)
    assert result.exact is True
    assert [(c.entry.id, c.entry.zh) for c in result.candidates] == sql_order
    service.close()


def test_build_database_atomic_checkpoints_wal_and_fails_closed_when_reader_blocks(
    monkeypatch, tmp_path: Path, sample_db: Path
):
    """build_database_atomic drains WAL before os.replace and fails closed if a reader blocks."""

    def fake_build_database(
        _data_dir: str | Path, db_path: str | Path, profile_name: str | None = None
    ) -> int:
        del profile_name
        conn = connect(db_path)
        try:
            initialize(conn)
            conn.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [
                    ("schema_version", "2"),
                    ("source_commit", "atomic-replacement-commit"),
                    ("source_game_version", "3.8"),
                ],
            )
            insert_records(
                conn,
                [
                    TermRecord(
                        category="item",
                        source_file="atomic_new.json",
                        source_id="an-1",
                        text_key="AtomicNew_1",
                        zh="原子新词",
                        en="Atomic Replacement",
                    )
                ],
            )
            conn.commit()
        finally:
            conn.close()
        return 1

    monkeypatch.setattr("wuwaterm.builder.build_database", fake_build_database)

    # 1. Blocked live reader holding an older WAL snapshot must fail closed with BuildError.
    blocked_target = tmp_path / "blocked-wal.db"
    shutil.copy2(sample_db, blocked_target)
    init_conn = connect(blocked_target)
    try:
        assert init_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        init_conn.execute("PRAGMA wal_autocheckpoint = 0")
        insert_records(
            init_conn,
            [
                TermRecord(
                    category="item",
                    source_file="old_wal.json",
                    source_id="ow-1",
                    text_key="OldWal_1",
                    zh="旧阻断词",
                    en="Old Blocked Term",
                )
            ],
        )
        init_conn.commit()
    finally:
        init_conn.close()

    pinned_reader = connect(blocked_target)
    try:
        pinned_reader.execute("BEGIN")
        assert pinned_reader.execute("SELECT COUNT(*) FROM terms").fetchone()[0] > 0

        writer = connect(blocked_target)
        try:
            writer.execute("PRAGMA wal_autocheckpoint = 0")
            insert_records(
                writer,
                [
                    TermRecord(
                        category="item",
                        source_file="old_wal.json",
                        source_id="ow-2",
                        text_key="OldWal_2",
                        zh="旧未刷词",
                        en="Old Uncheckpointed Term",
                    )
                ],
            )
            writer.commit()
        finally:
            writer.close()

        assert Path(f"{blocked_target}-wal").stat().st_size > 0

        with pytest.raises(BuildError, match="wal_checkpoint|active WAL|active database|locked"):
            build_database_atomic(tmp_path, blocked_target)

        # Temp build file must be cleaned up and existing WAL database left intact.
        assert not list(tmp_path.glob(f".{blocked_target.name}.*.tmp"))
        check_svc = TermService(blocked_target)
        try:
            assert check_svc.lookup_exact("旧未刷词").exact is True
            assert check_svc.lookup_exact("原子新词").exact is False
        finally:
            check_svc.close()

        # Copy the uncheckpointed DB + non-empty -wal into a fresh unblocked target
        # to verify normal WAL draining when a real non-empty -wal is present on disk.
        unblocked_target = tmp_path / "unblocked-wal.db"
        shutil.copy2(blocked_target, unblocked_target)
        shutil.copy2(Path(f"{blocked_target}-wal"), Path(f"{unblocked_target}-wal"))
    finally:
        pinned_reader.close()

    assert Path(f"{unblocked_target}-wal").stat().st_size > 0
    existing_svc = TermService(unblocked_target)
    try:
        assert existing_svc.lookup_exact("旧未刷词").exact is True

        # 2. Unblocked WAL target with non-empty -wal checkpoints, clears sidecars, and swaps cleanly.
        count = build_database_atomic(tmp_path, unblocked_target)
        assert count == 1
        assert not Path(f"{unblocked_target}-wal").exists()
        assert not Path(f"{unblocked_target}-shm").exists()
        assert not list(tmp_path.glob(f".{unblocked_target.name}.*.tmp"))

        assert existing_svc.lookup_exact("原子新词").exact is True
        assert existing_svc.lookup_exact("旧未刷词").exact is False
        assert existing_svc.dictionary_identity().source_commit == "atomic-replacement-commit"
    finally:
        existing_svc.close()

    verify_conn = sqlite3.connect(unblocked_target)
    try:
        assert verify_conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert verify_conn.execute("SELECT zh, en FROM terms").fetchall() == [
            ("原子新词", "Atomic Replacement")
        ]
    finally:
        verify_conn.close()


def test_same_subsecond_tick_equal_length_metadata_and_terms_update_invalidates_cache(
    monkeypatch, tmp_path: Path, sample_db: Path
):
    """Same-subsecond-tick equal-length metadata or terms updates must invalidate cached snapshots."""
    db_path = tmp_path / "subsec-probe.db"
    shutil.copy2(sample_db, db_path)

    frozen = os.stat(db_path)
    subsec_ns = int(frozen.st_mtime) * 1_000_000_000 + 123_456_789

    from wuwaterm import lookup as lookup_mod

    real_stat_tuple = lookup_mod._stat_tuple

    def frozen_subsec_stat(path: Path) -> tuple[int, int, int, int, int]:
        if path == db_path.resolve(strict=False):
            return (
                frozen.st_dev,
                frozen.st_ino,
                frozen.st_size,
                subsec_ns,
                subsec_ns,
            )
        return real_stat_tuple(path)

    monkeypatch.setattr(lookup_mod, "_stat_tuple", frozen_subsec_stat)

    service = TermService(db_path)
    try:
        snap1 = service.snapshot()
        assert service._snapshot_cache is snap1
        id1 = service.dictionary_identity()
        assert service.dictionary_identity() is id1

        old_commit = service.metadata().get("source_commit")
        assert old_commit is not None and len(old_commit) > 0
        replacement_commit = ("f" if old_commit[0] != "f" else "0") + old_commit[1:]
        assert len(replacement_commit) == len(old_commit)

        # 1. Equal-length metadata UPDATE with completely unchanged stat().
        w = connect(db_path)
        try:
            w.execute(
                "UPDATE metadata SET value = ? WHERE key = 'source_commit'",
                (replacement_commit,),
            )
            w.commit()
        finally:
            w.close()

        snap2 = service.snapshot()
        assert snap2 is not snap1
        assert service._snapshot_cache is snap2
        assert service.metadata()["source_commit"] == replacement_commit
        assert service.dictionary_identity().source_commit == replacement_commit
        assert service.review_snapshot().metadata["source_commit"] == replacement_commit

        # 2. Terms row insertion with completely unchanged stat().
        w = connect(db_path)
        try:
            insert_records(
                w,
                [
                    TermRecord(
                        category="item",
                        source_file="subsec.json",
                        source_id="ss-1",
                        text_key="Subsec_1",
                        zh="子秒新词",
                        en="Subsecond Term",
                    )
                ],
            )
            w.commit()
        finally:
            w.close()

        snap3 = service.snapshot()
        assert snap3 is not snap2
        assert service._snapshot_cache is snap3
        id3 = service.dictionary_identity()
        assert id3.term_count == id1.term_count + 1
        assert id3.revision != id1.revision
        assert service.review_snapshot().term_count == id1.term_count + 1
        exact = service.lookup_exact("子秒新词")
        assert exact.exact is True and exact.best is not None
        assert exact.best.entry.en == "Subsecond Term"
    finally:
        service.close()


def _install_fake_atomic_builder(monkeypatch, *, zh: str = "原子新词", en: str = "Atomic Replacement") -> None:
    def fake_build_database(
        _data_dir: str | Path, db_path: str | Path, profile_name: str | None = None
    ) -> int:
        del profile_name
        conn = connect(db_path)
        try:
            initialize(conn)
            conn.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [
                    ("schema_version", "2"),
                    ("source_commit", "atomic-replacement-commit"),
                    ("source_game_version", "3.8"),
                ],
            )
            insert_records(
                conn,
                [
                    TermRecord(
                        category="item",
                        source_file="atomic_new.json",
                        source_id="an-1",
                        text_key="AtomicNew_1",
                        zh=zh,
                        en=en,
                    )
                ],
            )
            conn.commit()
        finally:
            conn.close()
        return 1

    monkeypatch.setattr("wuwaterm.builder.build_database", fake_build_database)


def test_build_database_atomic_preserves_in_process_delete_write_lock(
    monkeypatch, tmp_path: Path, sample_db: Path
):
    """build_database_atomic must fail closed and never release another in-process DELETE write lock."""
    _install_fake_atomic_builder(monkeypatch)
    target = tmp_path / "delete-locked.db"
    shutil.copy2(sample_db, target)

    probe_script = (
        "import sqlite3, sys; "
        "c = sqlite3.connect(sys.argv[1], timeout=0.0); "
        "c.execute('BEGIN IMMEDIATE')"
    )

    def assert_other_process_busy() -> None:
        proc = subprocess.run(
            [sys.executable, "-c", probe_script, str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0, "other process unexpectedly acquired BEGIN IMMEDIATE"
        assert "locked" in proc.stderr.lower()

    writer = sqlite3.connect(target, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE terms SET en = en WHERE id = (SELECT MIN(id) FROM terms)"
        )
        assert_other_process_busy()

        with pytest.raises(BuildError, match="active database|locked"):
            build_database_atomic(tmp_path, target)

        assert not list(tmp_path.glob(f".{target.name}.*.tmp"))
        # Critical POSIX fcntl check: in-process writer's lock must still block other processes!
        assert_other_process_busy()
    finally:
        if writer.in_transaction:
            writer.execute("ROLLBACK")
        writer.close()


def test_build_database_atomic_holds_exclusive_lock_through_replace_and_rejects_orphan_wal(
    monkeypatch, tmp_path: Path, sample_db: Path
):
    """Exclusive lock stays held across os.replace and missing-target orphaned non-empty WAL fails closed."""
    _install_fake_atomic_builder(monkeypatch)

    # 1. During os.replace inside build_database_atomic, another process must be blocked
    # from switching target to WAL mode or starting a write transaction.
    target = tmp_path / "replace-mutex.db"
    shutil.copy2(sample_db, target)
    with connect(target) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        conn.commit()

    from wuwaterm import builder as builder_mod

    real_replace = os.replace
    probe_results: list[tuple[int, str]] = []

    def intercepted_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst) == target:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sqlite3, sys; "
                        "c = sqlite3.connect(sys.argv[1], timeout=0.0, isolation_level=None); "
                        "c.execute('PRAGMA journal_mode = WAL')"
                    ),
                    str(target),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            probe_results.append((proc.returncode, proc.stderr))
        real_replace(src, dst)

    monkeypatch.setattr(builder_mod.os, "replace", intercepted_replace)

    assert build_database_atomic(tmp_path, target) == 1
    assert len(probe_results) == 1
    assert probe_results[0][0] != 0
    assert "locked" in probe_results[0][1].lower()
    assert not Path(f"{target}-wal").exists()
    assert not Path(f"{target}-shm").exists()

    # 2. Missing target DB with an orphaned non-empty -wal must fail closed.
    orphan_target = tmp_path / "orphan-missing.db"
    orphan_wal = Path(f"{orphan_target}-wal")
    orphan_wal.write_bytes(b"orphaned-wal-frames" * 16)

    with pytest.raises(BuildError, match="non-empty WAL sidecar"):
        build_database_atomic(tmp_path, orphan_target)

    assert not orphan_target.exists()
    assert orphan_wal.exists()
    assert not list(tmp_path.glob(f".{orphan_target.name}.*.tmp"))

    # Empty (0-byte) orphaned sidecars are safely cleared on initial publish.
    orphan_wal.write_bytes(b"")
    Path(f"{orphan_target}-shm").write_bytes(b"")
    assert build_database_atomic(tmp_path, orphan_target) == 1
    assert orphan_target.exists()
    assert not orphan_wal.exists()
    assert not Path(f"{orphan_target}-shm").exists()


def test_same_subsecond_tick_equal_length_terms_inplace_update_invalidates_cache(
    tmp_path: Path, sample_db: Path
):
    """Equal-length in-place UPDATE terms with frozen sub-second mtime/ctime must invalidate cache."""
    db_path = tmp_path / "subsec-inplace-terms.db"
    shutil.copy2(sample_db, db_path)

    initial_stat = os.stat(db_path)
    subsec_ns = int(initial_stat.st_mtime) * 1_000_000_000 + 234_567_891
    os.utime(db_path, ns=(subsec_ns, subsec_ns))

    service = TermService(db_path)
    try:
        snap_before = service.snapshot()
        assert service._snapshot_cache is snap_before
        assert service._version_conn is not None
        id_before = service.dictionary_identity()
        rev_before = service.review_snapshot()
        meta_before = service.metadata()
        count_before = service.term_count()
        exact_before = service.lookup_exact("声骸")
        assert exact_before.best is not None and exact_before.best.entry.en == "Echo"

        size_before = os.stat(db_path).st_size
        with sqlite3.connect(db_path) as check_conn:
            max_id_before = check_conn.execute("SELECT MAX(id) FROM terms").fetchone()[0]

        # Perform equal-length in-place UPDATE on terms and restore exact sub-second mtime.
        w = connect(db_path)
        try:
            w.execute(
                "UPDATE terms SET en = 'Ech0', en_norm = 'ech0' WHERE en = 'Echo'"
            )
            w.commit()
        finally:
            w.close()
        os.utime(db_path, ns=(subsec_ns, subsec_ns))

        # Verify st_size, metadata, COUNT(*), and MAX(id) are all unchanged!
        assert os.stat(db_path).st_size == size_before
        with sqlite3.connect(db_path) as check_conn:
            assert check_conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == count_before
            assert check_conn.execute("SELECT MAX(id) FROM terms").fetchone()[0] == max_id_before

        # All read paths must immediately observe 'Ech0' and a fresh revision via PRAGMA data_version.
        exact_after = service.lookup_exact("声骸")
        assert exact_after.best is not None and exact_after.best.entry.en == "Ech0"
        snap_after = service.snapshot()
        assert snap_after is not snap_before
        assert any(e.zh == "声骸" and e.en == "Ech0" for e in snap_after.entries)
        rev_after = service.review_snapshot()
        assert rev_after.revision != rev_before.revision
        assert any(e.zh == "声骸" and e.en == "Ech0" for e in rev_after.entries)
        id_after = service.dictionary_identity()
        assert id_after.revision != id_before.revision
        assert id_after.term_count == count_before
        assert service.metadata() == meta_before
    finally:
        service.close()


def test_idle_term_service_in_delete_mode_does_not_block_atomic_replace_exclusive_or_wal(
    monkeypatch, tmp_path: Path, sample_db: Path
):
    """Idle DELETE-mode TermService must never block BEGIN EXCLUSIVE, WAL switch, or build_database_atomic."""
    _install_fake_atomic_builder(monkeypatch)

    # 1. Idle DELETE-mode TermService does not block BEGIN EXCLUSIVE or PRAGMA journal_mode=WAL.
    db_a = tmp_path / "idle-a.db"
    shutil.copy2(sample_db, db_a)
    svc_a = TermService(db_a)
    try:
        snap = svc_a.snapshot()
        assert svc_a._snapshot_cache is snap
        assert svc_a._version_conn is not None

        # Another connection immediately acquires EXCLUSIVE lock without waiting.
        excl = sqlite3.connect(db_a, timeout=0.0, isolation_level=None)
        try:
            excl.execute("PRAGMA locking_mode = EXCLUSIVE")
            excl.execute("BEGIN EXCLUSIVE")
            excl.execute("COMMIT")
        finally:
            excl.close()

        # Another connection immediately switches to WAL mode without waiting.
        wal_conn = sqlite3.connect(db_a, timeout=0.0, isolation_level=None)
        try:
            mode = wal_conn.execute("PRAGMA journal_mode = WAL").fetchone()
            assert mode is not None and mode[0].lower() == "wal"
        finally:
            wal_conn.close()

        # Subsequent read observes WAL mode and drops _version_conn cleanly.
        assert svc_a.dictionary_identity().term_count > 0
        assert svc_a._version_conn is None
    finally:
        svc_a.close()

    # 2. Idle DELETE-mode TermService does not block build_database_atomic() and sees the new DB.
    db_b = tmp_path / "idle-b.db"
    shutil.copy2(sample_db, db_b)
    svc_b = TermService(db_b)
    try:
        snap_b = svc_b.snapshot()
        assert svc_b._snapshot_cache is snap_b
        assert svc_b._version_conn is not None

        assert build_database_atomic(tmp_path, db_b) == 1
        assert svc_b.lookup_exact("原子新词").exact is True
        assert svc_b.dictionary_identity().source_commit == "atomic-replacement-commit"
    finally:
        svc_b.close()
