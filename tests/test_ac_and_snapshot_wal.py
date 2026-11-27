"""Differential Aho-Corasick parity tests and SQLite WAL snapshot regression tests."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from wuwaterm.application import build_term_service, build_translator, dictionary_identity
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
    with connect(wal_db) as conn:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert mode is not None and mode[0].lower() == "wal"
        conn.commit()

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
    # Hot read must hit the cached DictionaryIdentity instance without rescanning.
    assert service.dictionary_identity() is identity_before

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
        assert inflight_snapshot.exact_index.get("星尘魔核") is None
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
    monkeypatch, tmp_path: Path, sample_db: Path
):
    """WAL cache key tracks -shm WAL-index headers and refuses to cache mid-commit states."""
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

        # Part A: Simulate a writer that has already appended frames to -wal
        # before updating -shm mxFrame (the exact WAL commit visibility window).
        wal_path = Path(f"{wal_db}-wal")
        wal_before = wal_path.read_bytes()
        with open(wal_path, "ab") as wal_handle:
            wal_handle.write(b"\x00" * 4120)
        try:
            # Reading during the window must still reflect committed v1 state.
            assert service.dictionary_identity().revision == rev_v1
        finally:
            wal_path.write_bytes(wal_before)

        # Now writer finishes committing a second row (advancing -shm mxFrame).
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

        # Service must immediately observe the new -shm mxFrame and return v2.
        assert service.lookup_exact("窗口新词").exact is True
        assert service.dictionary_identity().revision != rev_v1

        # Part B: Torn -shm header (hdr1 != hdr2) must not pin a snapshot into cache.
        real_pread = service._pread_slot_locked
        torn_once = {"active": True}

        def torn_shm_pread(slot, path, inode, size, offset=0):
            data = real_pread(slot, path, inode, size, offset)
            if slot == 1 and torn_once["active"] and len(data) >= 96:
                # Flip one byte in hdr2 so hdr1 != hdr2 (mid-commit state).
                return data[:48] + bytes([data[48] ^ 0xFF]) + data[49:]
            return data

        service._snapshot_cache = None
        service._snapshot_stamp = None
        monkeypatch.setattr(service, "_pread_slot_locked", torn_shm_pread)
        snap = service.snapshot()
        assert snap.term_count > 0
        assert service._snapshot_cache is None

        # Once hdr1 == hdr2 again, normal caching resumes.
        torn_once["active"] = False
        cached_snap = service.snapshot()
        assert service._snapshot_cache is cached_snap
    finally:
        writer.close()
        service.close()


def test_last_closing_short_connection_cleans_up_wal_before_atomic_replace(
    monkeypatch, tmp_path: Path, sample_db: Path
):
    """When writer closes first and TermService's short read connection closes last, WAL is cleaned up."""
    live_db = tmp_path / "last-closer.db"
    shutil.copy2(sample_db, live_db)
    init_conn = connect(live_db)
    try:
        assert init_conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        init_conn.commit()
    finally:
        init_conn.close()

    writer = connect(live_db)
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

    # Coordinate so TermService opens its short connection BEFORE writer closes,
    # writer closes FIRST, and TermService's short connection closes LAST.
    reader_opened = threading.Event()
    writer_closed = threading.Event()
    from wuwaterm import lookup as lookup_mod

    real_connect = lookup_mod._connect_for_snapshot

    def coordinated_connect(path: Path) -> sqlite3.Connection:
        conn = real_connect(path)
        if not reader_opened.is_set():
            reader_opened.set()
            assert writer_closed.wait(timeout=5.0)
        return conn

    service = TermService(live_db)
    monkeypatch.setattr(lookup_mod, "_connect_for_snapshot", coordinated_connect)

    errors: list[BaseException] = []

    def run_reader() -> None:
        try:
            assert service.lookup_exact("旧尾词条").exact is True
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=run_reader)
    t.start()
    assert reader_opened.wait(timeout=5.0)
    writer.close()
    writer_closed.set()
    t.join(timeout=5.0)
    assert not errors

    # Because TermService uses a standard RW short connection when writable,
    # closing last cleanly checkpoints and removes -wal/-shm (matching main).
    assert not Path(f"{live_db}-wal").exists()
    assert not Path(f"{live_db}-shm").exists()

    # Replace live_db atomically with a clean candidate and open in RW mode:
    # zero old WAL pages can corrupt the new database.
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

    os.replace(candidate, live_db)
    assert service.lookup_exact("干净新词").exact is True
    assert service.lookup_exact("旧尾词条").exact is False
    service.close()

    verify = sqlite3.connect(live_db)
    try:
        assert verify.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert verify.execute("SELECT zh, en FROM terms").fetchall() == [
            ("干净新词", "Clean Replacement")
        ]
    finally:
        verify.close()

    # Read-only fallback check: read-only file + directory still loads via ?mode=ro.
    ro_dir = tmp_path / "ro-dir"
    ro_dir.mkdir()
    ro_db = ro_dir / "ro.db"
    shutil.copy2(live_db, ro_db)
    os.chmod(ro_db, 0o444)
    os.chmod(ro_dir, 0o555)
    try:
        ro_svc = TermService(ro_db)
        assert ro_svc.lookup_exact("干净新词").exact is True
        ro_svc.close()
    finally:
        os.chmod(ro_dir, 0o755)
        os.chmod(ro_db, 0o644)


def test_delete_mode_coarse_timestamp_same_size_inplace_update_invalidates_cache(
    monkeypatch, tmp_path: Path, sample_db: Path
):
    """DELETE-mode same-second same-size in-place UPDATE invalidates via SQLite file_change_counter."""
    db_path = tmp_path / "coarse-delete.db"
    shutil.copy2(sample_db, db_path)
    service = TermService(db_path)

    before_id = service.dictionary_identity()
    before_exact = service.lookup_exact("声骸")
    assert before_exact.best is not None and before_exact.best.entry.en == "Echo"

    frozen_stat = os.stat(db_path)

    # In-place same-length UPDATE in DELETE journal mode.
    w = connect(db_path)
    try:
        w.execute(
            "UPDATE terms SET en = ?, en_norm = ? WHERE zh = ?",
            ("Ech0", "ech0", "声骸"),
        )
        w.commit()
    finally:
        w.close()

    # Simulate a coarse 1-second filesystem where stat (dev, ino, size, mtime, ctime) is identical.
    os.utime(db_path, ns=(frozen_stat.st_atime_ns, frozen_stat.st_mtime_ns))
    from wuwaterm import lookup as lookup_mod

    real_stat_tuple = lookup_mod._stat_tuple

    def coarse_stat_tuple(path: Path) -> tuple[int, int, int, int, int]:
        if path == db_path.resolve(strict=False):
            return (
                frozen_stat.st_dev,
                frozen_stat.st_ino,
                frozen_stat.st_size,
                frozen_stat.st_mtime_ns,
                frozen_stat.st_ctime_ns,
            )
        return real_stat_tuple(path)

    monkeypatch.setattr(lookup_mod, "_stat_tuple", coarse_stat_tuple)

    after_id = service.dictionary_identity()
    after_exact = service.lookup_exact("声骸")
    assert after_id.revision != before_id.revision
    assert after_exact.best is not None and after_exact.best.entry.en == "Ech0"
    service.close()


def test_exact_lookup_cross_index_zh_norm_vs_en_norm_tie_matches_sqlite(
    tmp_path: Path,
):
    """Cross-index tie (en_norm-only id=1 vs zh_norm+en_norm id=2) matches SQLite MULTI-INDEX OR."""
    tie_db = tmp_path / "cross-index-tie.db"
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

    assert sql_order == [(2, "fo"), (1, "甲乙")]
    service = TermService(tie_db)
    result = service.lookup_exact("Foo", limit=10)
    assert result.exact is True
    assert [(c.entry.id, c.entry.zh) for c in result.candidates] == [(2, "fo"), (1, "甲乙")]
    service.close()
