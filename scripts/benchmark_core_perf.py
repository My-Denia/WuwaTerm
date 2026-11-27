#!/usr/bin/env python3
"""Zero-cost offline Core performance benchmark (Aho-Corasick + DictionarySnapshot + WAL).

Strictly offline: never makes any network or LLM API requests. Measures:
- Cold start / initial snapshot load & warm-up time + RSS
- Exact lookup latency (P50 / P95 / P99)
- Fuzzy lookup latency (pinyin + fuzzy scoring)
- Term span matching (2,000 chars, 10,000 chars, batch): naive text.find() vs Aho-Corasick
- Dictionary identity latency: cold computation vs hot cached read vs WAL-write refresh
Supports the real ~12k dictionary plus synthetic 50k and 100k scales.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wuwaterm.cjk_span import crosses_cjk_word_boundary, single_character_name_context_ok  # noqa: E402
from wuwaterm.db import connect, initialize, insert_records  # noqa: E402
from wuwaterm.english_plural import plural_suffix_candidates as _plural_suffix_candidates  # noqa: E402
from wuwaterm.lookup import TermService  # noqa: E402
from wuwaterm.models import TermRecord  # noqa: E402
from wuwaterm.normalize import normalize_text  # noqa: E402
from wuwaterm.sentence import SentenceTranslator  # noqa: E402
from wuwaterm.term_matching import (  # noqa: E402
    TermSpan,
    _ascii_word_boundaries_ok,
    _case_variant_boundaries_ok,
    _matched_span_ok,
    _nfkc_spelling,
    select_term_spans,
)


def _current_rss_mib() -> float:
    """Return current resident set size in MiB (Linux /proc fallback to ru_maxrss)."""
    status_path = Path("/proc/self/status")
    if status_path.is_file():
        for line in status_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) / 1024.0
    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return maxrss / (1024.0 * 1024.0)
    return maxrss / 1024.0


def _percentile(ordered: list[float], pct: float) -> float:
    if not ordered:
        return 0.0
    idx = max(0, min(len(ordered) - 1, round(len(ordered) * pct) - 1))
    return ordered[idx]


def _measure(
    call: Callable[[], Any], *, warmup: int, iterations: int
) -> dict[str, float]:
    for _ in range(warmup):
        call()
    samples: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        call()
        samples.append((time.perf_counter() - t0) * 1000.0)
    ordered = sorted(samples)
    return {
        "p50_ms": round(statistics.median(ordered), 4),
        "p95_ms": round(_percentile(ordered, 0.95), 4),
        "p99_ms": round(_percentile(ordered, 0.99), 4),
        "min_ms": round(ordered[0], 4),
        "max_ms": round(ordered[-1], 4),
    }


def _naive_select_term_spans(
    text: str,
    lockable: tuple[tuple[str, tuple[str, str]], ...],
    context_text: str,
    context_offset: int,
    *,
    allow_plural_inflection: bool = False,
    nfkc_spelling_sources: frozenset[str] = frozenset(),
) -> list[TermSpan]:
    """Pre-AC per-surface text.find() baseline for direct A/B comparison."""
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


def _discover_default_db() -> Path | None:
    candidates = [ROOT / "data" / "cjk-census-3.7.db"]
    try:
        common_dir = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=ROOT,
            text=True,
        ).strip()
        if common_dir:
            candidates.append(
                (ROOT / common_dir).resolve().parent / "data" / "cjk-census-3.7.db"
            )
    except (OSError, subprocess.SubprocessError):
        pass
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def _build_scaled_db(
    base_db: Path | None,
    target_rows: int,
    dest: Path,
) -> tuple[Path, int]:
    """Create a WAL-enabled benchmark database scaled to ``target_rows`` terms.

    Uses ``sqlite3.Connection.backup()`` when copying ``base_db`` so committed
    frames sitting in an uncheckpointed ``-wal`` file are preserved in full.
    """
    if base_db is not None:
        if not base_db.is_file() or base_db.stat().st_size == 0:
            raise FileNotFoundError(
                f"Base dictionary database does not exist or is empty: {base_db}"
            )
        src_conn = sqlite3.connect(
            f"{base_db.resolve().as_uri()}?mode=ro", uri=True
        )
        try:
            dst_conn = sqlite3.connect(dest)
            try:
                src_conn.backup(dst_conn)
                dst_conn.commit()
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
    else:
        with connect(dest) as conn:
            initialize(conn)
            conn.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [
                    ("schema_version", "2"),
                    ("source_commit", "synthetic-bench"),
                    ("source_game_version", "3.7"),
                ],
            )
            insert_records(
                conn,
                [
                    TermRecord("resonator", "RoleInfo.json", "1", "r1", "今汐", "Jinhsi"),
                    TermRecord("resonator", "RoleInfo.json", "2", "r2", "守岸人", "Shorekeeper"),
                    TermRecord("term", "Core.json", "3", "t1", "声骸", "Echo"),
                    TermRecord("term", "Core.json", "4", "t2", "共鸣者", "Resonator"),
                    TermRecord("term", "Core.json", "5", "t3", "声骸技能", "Echo Skill"),
                ],
            )
            conn.commit()

    with connect(dest) as conn:
        current = int(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0])
        if target_rows > current:
            need = target_rows - current
            categories = ("item", "skill", "term", "resonator")
            cjk_pool = "星海鸣潮声骸共鸣今汐守岸长离椿风雷冰火光暗裂隙残象律动核心"
            rows = []
            for i in range(need):
                cat = categories[i % len(categories)]
                a = cjk_pool[i % len(cjk_pool)]
                b = cjk_pool[(i // len(cjk_pool)) % len(cjk_pool)]
                c = cjk_pool[(i // (len(cjk_pool) * len(cjk_pool))) % len(cjk_pool)]
                zh = f"合成{a}{b}{c}{i:05d}"
                en = f"SynthTerm {cat.title()} {i:05d}"
                py = f"hecheng{i:05d}"
                rows.append(
                    (
                        cat,
                        "synthetic/bench.json",
                        f"synth_{i}",
                        f"Synth_{i}_Name",
                        zh,
                        en,
                        normalize_text(zh),
                        normalize_text(en),
                        py,
                        f"hc{i % 97:02d}",
                        3,
                    )
                )
            conn.executemany(
                """
                INSERT INTO terms(
                  category, source_file, source_id, text_key, zh, en, zh_norm, en_norm,
                  pinyin, pinyin_abbrev, priority
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        final_count = int(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0])
    return dest, final_count


def _build_sample_texts(target_len: int) -> str:
    seed = (
        "今汐与守岸人在共鸣者演练中触发声骸技能，漂泊者随后检查了声骸属性与Echo Skill效果。"
        "Resonators and Echoes interact across Solaris-3 while Jinhsi and Shorekeeper review "
        "every official term without breaking ordinary prose or 回声骸骨 boundaries。\n"
    )
    repeats = max(1, (target_len // len(seed)) + 1)
    return (seed * repeats)[:target_len]


def _benchmark_scale(
    scale_label: str,
    db_path: Path,
    term_count: int,
    *,
    warmup: int,
    iterations: int,
    naive_iterations: int,
    batch_size: int,
) -> dict[str, Any]:
    rss_before = _current_rss_mib()

    # 1. Cold start: initial snapshot load + warm all derived indices/AC matcher.
    t0 = time.perf_counter()
    service = TermService(db_path)
    translator = SentenceTranslator(db_path, service=service)
    snap = service.snapshot()
    cold_base_load_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    identity_cold = service.dictionary_identity()
    cold_identity_ms = (time.perf_counter() - t1) * 1000.0

    t3 = time.perf_counter()
    _ = snap.scoring_rows
    cold_scoring_rows_ms = (time.perf_counter() - t3) * 1000.0

    t4 = time.perf_counter()
    bundle = snap.lockable_snapshot()
    cold_lockable_ac_ms = (time.perf_counter() - t4) * 1000.0
    rss_after_lockable = _current_rss_mib()

    t5 = time.perf_counter()
    _ = snap.fuzzy_index
    cold_fuzzy_index_ms = (time.perf_counter() - t5) * 1000.0
    rss_after = _current_rss_mib()

    # 2. Exact lookup P50/P95/P99.
    exact_hit = _measure(
        lambda: service.lookup_exact("声骸"),
        warmup=warmup,
        iterations=iterations,
    )
    exact_miss = _measure(
        lambda: service.lookup_exact("不存在的普通长句" * 20),
        warmup=warmup,
        iterations=iterations,
    )

    # 3. Fuzzy lookup latency.
    fuzzy_iters = max(1, min(iterations, 5))
    fuzzy_pinyin = _measure(
        lambda: service.lookup("shenghai"),
        warmup=min(warmup, 1),
        iterations=fuzzy_iters,
    )
    fuzzy_cjk = _measure(
        lambda: service.lookup("今汐声"),
        warmup=min(warmup, 1),
        iterations=fuzzy_iters,
    )

    # 4. Term matching: naive vs Aho-Corasick (2k, 10k, batch).
    text_2k = _build_sample_texts(2000)
    text_10k = _build_sample_texts(10000)
    batch_texts = [text_2k for _ in range(batch_size)]
    lockable = bundle.eligible_lockable
    nfkc_sources = bundle.nfkc_spelling_sources
    matcher = bundle.matcher

    # Differential check on 2k before timing.
    naive_2k_spans = _naive_select_term_spans(
        text_2k,
        lockable,
        text_2k,
        0,
        allow_plural_inflection=True,
        nfkc_spelling_sources=nfkc_sources,
    )
    ac_2k_spans = select_term_spans(
        text_2k,
        lockable,
        text_2k,
        0,
        allow_plural_inflection=True,
        nfkc_spelling_sources=nfkc_sources,
        matcher=matcher,
    )
    if ac_2k_spans != naive_2k_spans:
        raise AssertionError(
            f"Differential mismatch at scale={scale_label}: "
            f"ac={len(ac_2k_spans)} vs naive={len(naive_2k_spans)}"
        )

    ac_2k = _measure(
        lambda: select_term_spans(
            text_2k,
            lockable,
            text_2k,
            0,
            allow_plural_inflection=True,
            nfkc_spelling_sources=nfkc_sources,
            matcher=matcher,
        ),
        warmup=warmup,
        iterations=iterations,
    )
    naive_2k = _measure(
        lambda: _naive_select_term_spans(
            text_2k,
            lockable,
            text_2k,
            0,
            allow_plural_inflection=True,
            nfkc_spelling_sources=nfkc_sources,
        ),
        warmup=0,
        iterations=naive_iterations,
    )
    ac_10k = _measure(
        lambda: select_term_spans(
            text_10k,
            lockable,
            text_10k,
            0,
            allow_plural_inflection=True,
            nfkc_spelling_sources=nfkc_sources,
            matcher=matcher,
        ),
        warmup=min(warmup, 2),
        iterations=max(3, min(iterations, 10)),
    )
    naive_10k = _measure(
        lambda: _naive_select_term_spans(
            text_10k,
            lockable,
            text_10k,
            0,
            allow_plural_inflection=True,
            nfkc_spelling_sources=nfkc_sources,
        ),
        warmup=0,
        iterations=1,
    )
    ac_batch = _measure(
        lambda: sum(
            len(
                select_term_spans(
                    t,
                    lockable,
                    t,
                    0,
                    allow_plural_inflection=True,
                    nfkc_spelling_sources=nfkc_sources,
                    matcher=matcher,
                )
            )
            for t in batch_texts
        ),
        warmup=1,
        iterations=max(2, min(iterations, 5)),
    )

    # 5. Dictionary identity: hot cached read (production DELETE mode) + WAL write refresh.
    identity_hot = _measure(
        lambda: service.dictionary_identity(),
        warmup=warmup,
        iterations=iterations,
    )

    wal_setup = connect(db_path)
    try:
        wal_setup.execute("PRAGMA journal_mode=WAL")
        wal_setup.commit()
    finally:
        wal_setup.close()

    stat_before = os.stat(db_path)
    writer = connect(db_path)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        insert_records(
            writer,
            [
                TermRecord(
                    category="item",
                    source_file="synthetic/wal_bench.json",
                    source_id="wal_bench_1",
                    text_key="WalBench_1",
                    zh="基准测试晶核",
                    en="Bench Crystal Core",
                )
            ],
        )
        writer.commit()
        stat_after = os.stat(db_path)
        main_stat_unchanged = (
            stat_before.st_size == stat_after.st_size
            and stat_before.st_mtime_ns == stat_after.st_mtime_ns
            and stat_before.st_ino == stat_after.st_ino
        )

        t_wal = time.perf_counter()
        identity_after_wal = service.dictionary_identity()
        wal_identity_refresh_ms = (time.perf_counter() - t_wal) * 1000.0
        locked_wal = translator.lock_terms("携带基准测试晶核")
        wal_lock_ok = (
            locked_wal.restore(locked_wal.locked_text) == "携带Bench Crystal Core"
        )
        assert identity_after_wal.revision != identity_cold.revision
        assert wal_lock_ok
    finally:
        writer.close()

    service.close()

    speedup_2k = (
        round(naive_2k["p50_ms"] / ac_2k["p50_ms"], 2) if ac_2k["p50_ms"] > 0 else 0.0
    )
    speedup_10k = (
        round(naive_10k["p50_ms"] / ac_10k["p50_ms"], 2)
        if ac_10k["p50_ms"] > 0
        else 0.0
    )

    return {
        "scale": scale_label,
        "term_count": term_count,
        "eligible_surfaces": len(lockable),
        "ac_patterns": matcher.pattern_count,
        "ac_states": matcher.state_count,
        "cold_start": {
            "base_load_ms": round(cold_base_load_ms, 3),
            "identity_ms": round(cold_identity_ms, 3),
            "scoring_rows_ms": round(cold_scoring_rows_ms, 3),
            "lockable_ac_ms": round(cold_lockable_ac_ms, 3),
            "fuzzy_index_ms": round(cold_fuzzy_index_ms, 3),
            "rss_before_mib": round(rss_before, 2),
            "rss_after_lockable_mib": round(rss_after_lockable, 2),
            "rss_after_mib": round(rss_after, 2),
        },
        "exact_lookup": {
            "hit": exact_hit,
            "miss_long": exact_miss,
        },
        "fuzzy_lookup": {
            "pinyin": fuzzy_pinyin,
            "cjk": fuzzy_cjk,
        },
        "term_matching": {
            "span_hits_2k": len(ac_2k_spans),
            "diff_mismatches": 0,
            "ac_2000": ac_2k,
            "naive_2000": naive_2k,
            "speedup_2000x": speedup_2k,
            "ac_10000": ac_10k,
            "naive_10000": naive_10k,
            "speedup_10000x": speedup_10k,
            "batch_size": batch_size,
            "ac_batch_2000": ac_batch,
        },
        "dictionary_identity": {
            "cold_ms": round(cold_identity_ms, 3),
            "hot": identity_hot,
            "wal_refresh_ms": round(wal_identity_refresh_ms, 3),
            "wal_main_stat_unchanged": main_stat_unchanged,
        },
    }


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Core Performance Benchmark Summary (Zero-LLM Offline)",
        "",
        f"- Python: `{report['python']}`",
        f"- Base DB: `{report['base_db']}`",
        "",
        "| Scale | Terms | Surfaces | Cold Base (ms) | Cold AC (ms) | RSS (MiB) | Exact Hit P50/P95/P99 (ms) | Fuzzy Pinyin P50 (ms) | Match 2k Naive -> AC (ms / speedup) | Match 10k Naive -> AC (ms / speedup) | Dict Identity Cold -> Hot / WAL (ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- | --- | --- |",
    ]
    for row in report["results"]:
        cs = row["cold_start"]
        ex = row["exact_lookup"]["hit"]
        fz = row["fuzzy_lookup"]["pinyin"]
        tm = row["term_matching"]
        di = row["dictionary_identity"]
        lines.append(
            f"| {row['scale']} | {row['term_count']} | {row['eligible_surfaces']} | "
            f"{cs['base_load_ms']:.2f} | {cs['lockable_ac_ms']:.2f} | {cs['rss_after_mib']:.1f} | "
            f"{ex['p50_ms']:.3f} / {ex['p95_ms']:.3f} / {ex['p99_ms']:.3f} | "
            f"{fz['p50_ms']:.2f} | "
            f"{tm['naive_2000']['p50_ms']:.2f} -> {tm['ac_2000']['p50_ms']:.3f} ({tm['speedup_2000x']}x) | "
            f"{tm['naive_10000']['p50_ms']:.2f} -> {tm['ac_10000']['p50_ms']:.3f} ({tm['speedup_10000x']}x) | "
            f"{di['cold_ms']:.2f} -> {di['hot']['p50_ms']:.3f} / {di['wal_refresh_ms']:.2f} |"
        )
    return "\n".join(lines) + "\n"


def _parse_scale(spec: str) -> tuple[str, int]:
    raw = spec.strip().lower()
    if raw == "real":
        return "real", 0
    if raw.endswith("k"):
        return raw, int(float(raw[:-1]) * 1000)
    return raw, int(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run zero-cost offline Core performance benchmarks."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to base SQLite dictionary (defaults to data/cjk-census-3.7.db if present).",
    )
    parser.add_argument(
        "--scales",
        default="12k,50k,100k",
        help="Comma-separated dictionary scales (e.g. '12k,50k,100k' or 'real').",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Fast smoke mode (<5s): runs only the base scale with small iteration counts.",
    )
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--naive-iterations", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--md-out", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.db is not None:
        if not args.db.is_file() or args.db.stat().st_size == 0:
            parser.error(
                f"--db path does not exist or is empty: {args.db}"
            )
        base_db: Path | None = args.db
    else:
        base_db = _discover_default_db()
    if args.quick:
        scales = [
            ("real-12k" if base_db is not None else "synthetic-12k", 0 if base_db else 12000)
        ]
        warmup = 1
        iterations = 10
        naive_iterations = 1
        batch_size = 4
    else:
        scales = [_parse_scale(part) for part in args.scales.split(",") if part.strip()]
        warmup = args.warmup
        iterations = args.iterations
        naive_iterations = args.naive_iterations
        batch_size = args.batch_size

    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="wuwaterm-core-bench-") as tmpdir:
        tmp = Path(tmpdir)
        for label, target_rows in scales:
            db_copy, actual_count = _build_scaled_db(
                base_db, target_rows, tmp / f"bench-{label}.db"
            )
            results.append(
                _benchmark_scale(
                    label,
                    db_copy,
                    actual_count,
                    warmup=warmup,
                    iterations=iterations,
                    naive_iterations=naive_iterations,
                    batch_size=batch_size,
                )
            )

    report = {
        "python": sys.version.split()[0],
        "base_db": base_db.name if base_db is not None else "synthetic",
        "results": results,
    }
    markdown = _render_markdown(report)
    if args.json_out is not None:
        args.json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if args.md_out is not None:
        args.md_out.write_text(markdown, encoding="utf-8")

    print(markdown)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
