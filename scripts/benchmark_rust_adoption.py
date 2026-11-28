#!/usr/bin/env python3
"""End-to-end Rust / PyO3 / WASM adoption benchmark for WuwaTerm.

Measures full product-level adoption costs and net speedups rather than bare
micro-benchmarks alone:
- Artifact sizes (PyO3 wheel / .so raw+gzip, WASM .wasm + ESM wrapper raw+gzip)
- Cold start (isolated Python import vs PyO3 import, AC build, Node WASM load)
- FFI / cross-boundary overhead across 50 -> 10,000 Unicode scalars
- Product-level E2E workflows:
  1. Short 50-scalar segment (normalize, segment, lock_terms, structure check)
  2. Long 2,000-scalar segment (normalize, segment, select_term_spans, lock_terms, structure check)
  3. 500-segment manuscript batch (50 segments in --quick)
  4. Single-request lock_terms + check_segment_structure E2E
  5. 277k-line bulk corpus scan (real MultiText when available, synthetic fallback; 5k in --quick)
- Optional Node.js WASM vs pure-JS baseline (when `node` and release `.wasm` exist)

Works with or without the optional `_wuwaterm_text_kernel` extension installed
(reports `native_kernel_available: false` cleanly when absent for CI safety).
"""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import os
import platform
import resource
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wuwaterm import native_kernel  # noqa: E402
from wuwaterm.lookup import TermService  # noqa: E402
from wuwaterm.normalize import normalize_prose_text  # noqa: E402
from wuwaterm.structural_checks import check_segment_structure  # noqa: E402
from wuwaterm.term_matching import (  # noqa: E402
    TermSpan,
    TermSpanMatcher,
    _matched_span_ok,
    exact_lockable_sources,
    select_term_spans,
)
from wuwaterm.term_preparation import new_placeholder_prefix, prepare_term_segment  # noqa: E402

DEFAULT_WASM_PATH = (
    ROOT
    / "crates"
    / "target"
    / "wasm32-unknown-unknown"
    / "release"
    / "wuwaterm_text_kernel_wasm.wasm"
)
DEFAULT_WASM_MJS_PATH = (
    ROOT / "crates" / "wuwaterm-text-kernel-wasm" / "js" / "index.mjs"
)


def _current_rss_mib() -> float:
    """Return current resident set size in MiB."""
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
    fn: Callable[[], Any], *, warmup: int, iterations: int
) -> dict[str, float]:
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    ordered = sorted(samples)
    return {
        "p50_ms": round(statistics.median(ordered), 4),
        "p95_ms": round(_percentile(ordered, 0.95), 4),
        "min_ms": round(ordered[0], 4),
        "max_ms": round(ordered[-1], 4),
    }


def _discover_data_dir(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit if explicit.is_dir() else None
    candidates: list[Path] = [ROOT / "data"]
    try:
        common = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if common:
            candidates.append((ROOT / common).resolve().parent / "data")
    except (OSError, subprocess.SubprocessError):
        pass
    for cand in candidates:
        if cand.is_dir():
            return cand
    return None


def _safe_label(path: Path) -> str:
    """Return a repo-relative path or basename without leaking host home paths."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.name


def _file_size_entry(path: Path | None, *, missing_reason: str) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"available": False, "reason": missing_reason}
    raw = path.read_bytes()
    gz = gzip.compress(raw, compresslevel=9)
    return {
        "available": True,
        "artifact": _safe_label(path),
        "raw_bytes": len(raw),
        "gzip_bytes": len(gz),
        "raw_kib": round(len(raw) / 1024.0, 2),
        "gzip_kib": round(len(gz) / 1024.0, 2),
    }


def _resolve_pyo3_so() -> Path | None:
    spec = importlib.util.find_spec("_wuwaterm_text_kernel")
    if spec is None:
        return None
    if spec.origin and spec.origin.endswith((".so", ".pyd")):
        return Path(spec.origin)
    if spec.submodule_search_locations:
        for loc in spec.submodule_search_locations:
            loc_path = Path(loc)
            for pattern in ("*.so", "*.pyd"):
                matches = sorted(loc_path.glob(pattern))
                if matches:
                    return matches[0]
    return None


_SUBPROCESS_RSS_EXPR = (
    "(next((int(l.split()[1]) / 1024.0 for l in p.read_text().splitlines() "
    "if l.startswith('VmRSS:')), None) if p.is_file() else None) "
    "or (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / "
    "(1024.0 * 1024.0 if sys.platform == 'darwin' else 1024.0))"
)


def _measure_python_cold_import(stmt: str, *, runs: int) -> dict[str, float]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    code = (
        "import json, resource, sys, time; "
        "from pathlib import Path; "
        "t0 = time.perf_counter(); "
        f"{stmt}; "
        "dt = (time.perf_counter() - t0) * 1000.0; "
        "p = Path('/proc/self/status'); "
        f"rss = {_SUBPROCESS_RSS_EXPR}; "
        "print(json.dumps({'ms': dt, 'rss_mib': rss}))"
    )
    times: list[float] = []
    rss_vals: list[float] = []
    for _ in range(runs):
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout.strip())
        times.append(float(payload["ms"]))
        rss_vals.append(float(payload["rss_mib"]))
    return {
        "median_ms": round(statistics.median(times), 3),
        "p95_ms": round(_percentile(sorted(times), 0.95), 3),
        "rss_mib": round(statistics.median(rss_vals), 2),
    }


def _measure_isolated_ac_build(
    mode: str,
    *,
    db_path: Path | None,
    runs: int,
) -> dict[str, Any]:
    """Measure AC build time and RSS delta in a clean subprocess without MultiText heap churn."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    if db_path is not None and db_path.is_file():
        setup_stmt = (
            "from wuwaterm.lookup import TermService; "
            "from wuwaterm.term_matching import exact_lockable_sources; "
            f"lockable = exact_lockable_sources(TermService({str(db_path)!r}).entries()); "
            "surfaces = [s for s, _ in lockable]"
        )
    else:
        setup_stmt = (
            "import sys; "
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
            "from benchmark_rust_adoption import _synthetic_surfaces; "
            "lockable = _synthetic_surfaces(2000); "
            "surfaces = [s for s, _ in lockable]"
        )

    if mode == "python_only":
        build_stmt = (
            "from wuwaterm.term_matching import TermSpanMatcher; "
            "_m = TermSpanMatcher.build(lockable, include_plural=False)"
        )
    elif mode == "native_only":
        build_stmt = (
            "from wuwaterm import native_kernel; "
            "assert native_kernel.native_kernel_available(); "
            "_s = native_kernel.TermScanner(surfaces, prefer_native=True)"
        )
    elif mode == "coexisting_both":
        build_stmt = (
            "from wuwaterm import native_kernel; "
            "from wuwaterm.term_matching import TermSpanMatcher; "
            "assert native_kernel.native_kernel_available(); "
            "_m = TermSpanMatcher.build(lockable, include_plural=False); "
            "_s = native_kernel.TermScanner(surfaces, prefer_native=True)"
        )
    else:
        raise ValueError(f"unsupported AC build mode: {mode}")

    code = (
        "import json, resource, sys, time; "
        "from pathlib import Path; "
        f"{setup_stmt}; "
        "p = Path('/proc/self/status'); "
        f"rss_before = {_SUBPROCESS_RSS_EXPR}; "
        "t0 = time.perf_counter(); "
        f"{build_stmt}; "
        "dt = (time.perf_counter() - t0) * 1000.0; "
        f"rss_after = {_SUBPROCESS_RSS_EXPR}; "
        "print(json.dumps({'ms': dt, 'rss_before_mib': rss_before, "
        "'rss_after_mib': rss_after, 'rss_delta_mib': max(0.0, rss_after - rss_before)}))"
    )
    times: list[float] = []
    before_vals: list[float] = []
    after_vals: list[float] = []
    delta_vals: list[float] = []
    for _ in range(runs):
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout.strip())
        times.append(float(payload["ms"]))
        before_vals.append(float(payload["rss_before_mib"]))
        after_vals.append(float(payload["rss_after_mib"]))
        delta_vals.append(float(payload["rss_delta_mib"]))
    return {
        "available": True,
        "isolated_subprocess": True,
        "build_ms": round(statistics.median(times), 3),
        "rss_before_mib": round(statistics.median(before_vals), 2),
        "rss_after_mib": round(statistics.median(after_vals), 2),
        "rss_delta_mib": round(statistics.median(delta_vals), 2),
    }


def _synthetic_surfaces(count: int = 2000) -> tuple[tuple[str, tuple[str, str]], ...]:
    items: list[tuple[str, tuple[str, str]]] = [
        ("声骸", ("声骸", "Echo")),
        ("声骸技能", ("声骸技能", "Echo Skill")),
        ("共鸣者", ("共鸣者", "Resonator")),
        ("黑海岸", ("黑海岸", "The Black Shores")),
        ("今州城", ("今州城", "Jinzhou")),
        ("鸣潮", ("鸣潮", "Wuthering Waves")),
        ("共鸣解放", ("共鸣解放", "Resonance Liberation")),
        ("共鸣回路", ("共鸣回路", "Forte Circuit")),
        ("变奏技能", ("变奏技能", "Intro Skill")),
        ("延奏技能", ("延奏技能", "Outro Skill")),
        ("Echo", ("声骸", "Echo")),
        ("Echo Skill", ("声骸技能", "Echo Skill")),
        ("Resonator", ("共鸣者", "Resonator")),
        ("Jinzhou", ("今州城", "Jinzhou")),
        ("The Black Shores", ("黑海岸", "The Black Shores")),
    ]
    idx = 0
    while len(items) < count:
        zh = f"合成术语{idx:04d}"
        en = f"SyntheticTerm{idx:04d}"
        items.append((zh, (zh, en)))
        if len(items) < count:
            items.append((en, (zh, en)))
        idx += 1
    return tuple(items[:count])


def _synthetic_zh_text(target_scalars: int) -> str:
    unit = (
        "在黑海岸的共鸣者汐汐开启了声骸技能，准备前往今州城处理v3.7版本更新（1.2倍效率）。"
        "共鸣解放与共鸣回路将在3.5秒内完成充能，🌊请注意延奏技能与变奏技能衔接。\n"
    )
    repeats = max(1, (target_scalars // len(unit)) + 2)
    return (unit * repeats)[:target_scalars]


def _synthetic_en_text(target_scalars: int) -> str:
    unit = (
        "The Resonator in The Black Shores activated Echo Skill at Jinzhou for v3.7 (1.2x rate). "
        "Resonance Liberation and Forte Circuit charge within 3.5 seconds before Outro Skill.\n"
    )
    repeats = max(1, (target_scalars // len(unit)) + 2)
    return (unit * repeats)[:target_scalars]


def _load_workloads(
    data_dir: Path | None, *, quick: bool
) -> dict[str, Any]:
    lockable: tuple[tuple[str, tuple[str, str]], ...]
    real_db = False
    if data_dir is not None and (data_dir / "cjk-census-3.7.db").is_file():
        svc = TermService(data_dir / "cjk-census-3.7.db")
        lockable = exact_lockable_sources(svc.entries())
        real_db = True
    else:
        lockable = _synthetic_surfaces(2000)

    short_zh = "在黑海岸的共鸣者汐汐开启了声骸技能，准备前往今州城进行v3.7版本更新核验与术语锁定流程测试。"
    short_en = "Resonator at The Black Shores used Echo Skill in Jinzhou for v3.7 check."

    real_corpus = False
    corpus_dir = (
        data_dir / "eval-corpus" / "wuwaterm-real-manuscripts-20261110"
        if data_dir is not None
        else None
    )
    a3_zh_path = (
        corpus_dir / "A3-v3.7-maintenance-notice" / "zh.txt"
        if corpus_dir is not None
        else None
    )
    a3_en_path = (
        corpus_dir / "A3-v3.7-maintenance-notice" / "en.txt"
        if corpus_dir is not None
        else None
    )
    if a3_zh_path is not None and a3_zh_path.is_file() and a3_en_path is not None and a3_en_path.is_file():
        a3_zh = a3_zh_path.read_text(encoding="utf-8")
        a3_en = a3_en_path.read_text(encoding="utf-8")
        real_corpus = True
    else:
        a3_zh = _synthetic_zh_text(4200)
        a3_en = _synthetic_en_text(11000)

    long_2000_zh = a3_zh[:2000]
    long_2000_en = a3_en[:2000]
    xlarge_10000_en = (a3_en * 2)[:10000]

    # Build 500-segment (or 50 in quick) bilingual manuscript workload from paragraphs/chunks
    raw_paras_zh = [p.strip() for p in a3_zh.split("\n") if p.strip()]
    raw_paras_en = [p.strip() for p in a3_en.split("\n") if p.strip()]
    pair_count = min(len(raw_paras_zh), len(raw_paras_en)) or 1
    if pair_count == 1:
        base_pairs = [(short_zh, short_en)]
    else:
        base_pairs = list(zip(raw_paras_zh[:pair_count], raw_paras_en[:pair_count]))

    manuscript_target = 50 if quick else 500
    manuscript_pairs = [
        base_pairs[i % len(base_pairs)] for i in range(manuscript_target)
    ]

    # 277k-line bulk corpus scan (real MultiText when present, else synthetic)
    bulk_target = 5000 if quick else 277917
    real_multitext = False
    multitext_path = (
        data_dir / "wutheringdata" / "Textmaps" / "zh-Hans" / "multi_text" / "MultiText.json"
        if data_dir is not None
        else None
    )
    bulk_lines: list[str] = []
    if multitext_path is not None and multitext_path.is_file():
        rows = json.loads(multitext_path.read_text(encoding="utf-8"))
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    content = str(row.get("Content", "")).strip()
                    if content:
                        bulk_lines.append(content)
                        if len(bulk_lines) >= bulk_target:
                            break
        real_multitext = bool(bulk_lines)

    if not real_multitext:
        seed_lines = [p for p, _ in base_pairs] + [short_zh]
        while len(bulk_lines) < bulk_target:
            bulk_lines.append(seed_lines[len(bulk_lines) % len(seed_lines)])

    return {
        "real_db_available": real_db,
        "real_corpus_available": real_corpus,
        "real_multitext_available": real_multitext,
        "lockable": lockable,
        "surfaces": [s for s, _ in lockable],
        "short_50": (short_zh, short_en),
        "long_2000": (long_2000_zh, long_2000_en),
        "xlarge_10000": xlarge_10000_en,
        "a3_pair": (a3_zh, a3_en),
        "manuscript_pairs": manuscript_pairs,
        "bulk_lines": bulk_lines,
    }


class _NativeBackedSpanMatcher:
    """Adapter exposing PyO3 TermAutomaton through TermSpanMatcher's contract.

    Used purely inside this benchmark to measure the real product-level cost of
    replacing the pure-Python AC pass in `select_term_spans` / `prepare_term_segment`
    while keeping Python boundary gates (`_matched_span_ok`) and global interval
    selection identical.
    """

    def __init__(
        self,
        lockable: tuple[tuple[str, tuple[str, str]], ...],
        scanner: native_kernel.TermScanner,
    ) -> None:
        self._lockable = lockable
        self._scanner = scanner

    def can_serve(
        self,
        *,
        allow_plural_inflection: bool,
        nfkc_spelling_sources: frozenset[str],
    ) -> bool:
        return (not allow_plural_inflection) and (not nfkc_spelling_sources)

    def select_spans(
        self,
        text: str,
        context_text: str,
        context_offset: int,
        *,
        allow_plural_inflection: bool = False,
        nfkc_spelling_sources: frozenset[str] = frozenset(),
    ) -> list[TermSpan]:
        if not text or not self._lockable:
            return []
        raw_hits = self._scanner.find_all(text)
        spans: list[TermSpan] = []
        for start, end, order in raw_hits:
            source, official = self._lockable[order]
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
        if not spans:
            return []
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
            if any(span.start < oe and os < span.end for os, oe in occupied):
                continue
            selected.append(span)
            occupied.append((span.start, span.end))
        selected.sort(key=lambda item: item.start)
        return selected


def _run_wasm_benchmarks(
    *,
    wasm_path: Path,
    mjs_path: Path,
    surfaces: Sequence[str],
    short_text: str,
    medium_text: str,
    long_text: str,
    pair_src: str,
    pair_tgt: str,
    manuscript_pairs: Sequence[tuple[str, str]],
    warmup: int,
    iterations: int,
) -> dict[str, Any]:
    node_bin = shutil.which("node")
    if node_bin is None:
        return {"available": False, "reason": "node binary not found"}
    if not wasm_path.is_file():
        return {
            "available": False,
            "reason": f"wasm artifact not built ({_safe_label(wasm_path)})",
        }
    if not mjs_path.is_file():
        return {
            "available": False,
            "reason": f"wasm JS wrapper missing ({_safe_label(mjs_path)})",
        }

    # Note: pure-JS indexOf scan over 19k surfaces on 500 segments would be slow;
    # bound pure-JS naive comparison to single segments where it already shows O(P*N).
    payload = {
        "wasmPath": str(wasm_path.resolve()),
        "mjsUrl": mjs_path.resolve().as_uri(),
        "surfaces": list(surfaces),
        "shortText": short_text,
        "mediumText": medium_text,
        "longText": long_text,
        "pairSrc": pair_src,
        "pairTgt": pair_tgt,
        "manuscriptTexts": [src for src, _ in manuscript_pairs],
        "warmup": warmup,
        "iterations": iterations,
    }

    node_script = r"""
import fs from "node:fs";
import { performance } from "node:perf_hooks";

const input = JSON.parse(fs.readFileSync(0, "utf-8"));
const { loadKernel } = await import(input.mjsUrl);
const wasmBytes = fs.readFileSync(input.wasmPath);

function percentile(sorted, p) {
  if (!sorted.length) return 0;
  const idx = Math.max(0, Math.min(sorted.length - 1, Math.round(sorted.length * p) - 1));
  return sorted[idx];
}

function measure(fn, warmup, iters) {
  for (let i = 0; i < warmup; i++) fn();
  const samples = [];
  for (let i = 0; i < iters; i++) {
    const t0 = performance.now();
    fn();
    samples.push(performance.now() - t0);
  }
  samples.sort((a, b) => a - b);
  return {
    p50_ms: Number(percentile(samples, 0.5).toFixed(4)),
    p95_ms: Number(percentile(samples, 0.95).toFixed(4)),
  };
}

// Pure JS baselines
function jsScalarToUtf16(text, scalar) {
  let u16 = 0;
  let s = 0;
  for (const ch of text) {
    if (s >= scalar) break;
    u16 += ch.length;
    s += 1;
  }
  return u16;
}

function jsNaiveScan(surfaces, text) {
  const hits = [];
  for (let order = 0; order < surfaces.length; order++) {
    const pat = surfaces[order];
    if (!pat) continue;
    let idx = text.indexOf(pat);
    while (idx !== -1) {
      hits.push([idx, idx + pat.length, order]);
      idx = text.indexOf(pat, idx + 1);
    }
  }
  return hits;
}

const rssBefore = process.memoryUsage().rss / (1024 * 1024);
const tLoad0 = performance.now();
const kernel = await loadKernel(wasmBytes);
const loadMs = performance.now() - tLoad0;
const rssAfterLoad = process.memoryUsage().rss / (1024 * 1024);

const tAc0 = performance.now();
const ac = kernel.createTermAutomaton(input.surfaces);
const acBuildMs = performance.now() - tAc0;
const rssAfterAc = process.memoryUsage().rss / (1024 * 1024);

const scalarTargets = [
  ["short_50", input.shortText],
  ["long_2000", input.mediumText],
  ["xlarge_10000", input.longText],
];

const operations = [];
for (const [label, txt] of scalarTargets) {
  const scalarLen = Array.from(txt).length;
  const targetIdx = Math.floor(scalarLen / 2);
  const jsS2u = measure(() => jsScalarToUtf16(txt, targetIdx), input.warmup, input.iterations);
  const wasmS2u = measure(() => kernel.scalarToUtf16(txt, targetIdx), input.warmup, input.iterations);
  operations.push({
    operation: "scalar_to_utf16",
    payload: label,
    scalars: scalarLen,
    js_p50_ms: jsS2u.p50_ms,
    wasm_p50_ms: wasmS2u.p50_ms,
    wasm_p95_ms: wasmS2u.p95_ms,
  });

  const wasmSeg = measure(() => kernel.segmentText(txt, 1800), input.warmup, input.iterations);
  operations.push({
    operation: "segment_text",
    payload: label,
    scalars: scalarLen,
    js_p50_ms: null,
    wasm_p50_ms: wasmSeg.p50_ms,
    wasm_p95_ms: wasmSeg.p95_ms,
  });

  const wasmHandleScan = measure(
    () => ac.scan(txt, { select: true, includeUtf16: true }),
    input.warmup,
    input.iterations,
  );
  const oneShotIters = Math.max(1, Math.min(3, input.iterations));
  const wasmOneShot = measure(
    () => kernel.scanTerms(input.surfaces, txt, { select: true, includeUtf16: true }),
    0,
    oneShotIters,
  );
  const jsNaive = measure(() => jsNaiveScan(input.surfaces, txt), 1, Math.min(5, input.iterations));
  operations.push({
    operation: "term_scan_select",
    payload: label,
    scalars: scalarLen,
    js_naive_p50_ms: jsNaive.p50_ms,
    wasm_handle_p50_ms: wasmHandleScan.p50_ms,
    wasm_handle_p95_ms: wasmHandleScan.p95_ms,
    wasm_oneshot_p50_ms: wasmOneShot.p50_ms,
  });
}

const pairBench = measure(
  () => kernel.pairSegments(input.pairSrc, input.pairTgt, 1800),
  input.warmup,
  input.iterations,
);
operations.push({
  operation: "pair_segments",
  payload: "a3_pair",
  scalars: Array.from(input.pairSrc).length + Array.from(input.pairTgt).length,
  wasm_p50_ms: pairBench.p50_ms,
  wasm_p95_ms: pairBench.p95_ms,
});

const batchT0 = performance.now();
for (const txt of input.manuscriptTexts) {
  kernel.segmentText(txt, 1800);
  ac.scan(txt, { select: true, includeUtf16: true });
}
const batchMs = performance.now() - batchT0;

ac.free();

console.log(JSON.stringify({
  available: true,
  node_version: process.version,
  cold_start: {
    load_kernel_ms: Number(loadMs.toFixed(3)),
    ac_build_ms: Number(acBuildMs.toFixed(3)),
    rss_before_mib: Number(rssBefore.toFixed(2)),
    rss_after_load_mib: Number(rssAfterLoad.toFixed(2)),
    rss_after_ac_mib: Number(rssAfterAc.toFixed(2)),
    rss_delta_load_mib: Number((rssAfterLoad - rssBefore).toFixed(2)),
    rss_delta_ac_mib: Number((rssAfterAc - rssAfterLoad).toFixed(2)),
  },
  operations,
  manuscript_batch: {
    segment_count: input.manuscriptTexts.length,
    segment_plus_scan_ms: Number(batchMs.toFixed(3)),
  },
}));
"""

    proc = subprocess.run(
        [node_bin, "--input-type=module", "-e", node_script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip())


def run_benchmark(
    *,
    quick: bool,
    data_dir: Path | None,
    wasm_path: Path,
    wheel_path: Path | None,
    warmup: int | None = None,
    iterations: int | None = None,
) -> dict[str, Any]:
    w_iters = warmup if warmup is not None else (1 if quick else 3)
    n_iters = iterations if iterations is not None else (5 if quick else 20)

    resolved_data = _discover_data_dir(data_dir)
    workloads = _load_workloads(resolved_data, quick=quick)
    lockable: tuple[tuple[str, tuple[str, str]], ...] = workloads["lockable"]
    surfaces: list[str] = workloads["surfaces"]

    native_available = native_kernel.native_kernel_available()
    native_mod = native_kernel._load_native_module() if native_available else None
    digit_parity = (
        native_kernel._native_digit_parity_ok(native_mod)
        if native_mod is not None
        else None
    )

    node_bin = shutil.which("node")
    node_version: str | None = None
    if node_bin is not None:
        try:
            node_version = subprocess.check_output(
                [node_bin, "--version"], text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (OSError, subprocess.SubprocessError):
            node_version = None

    so_path = _resolve_pyo3_so()
    artifacts = {
        "pyo3_so": _file_size_entry(
            so_path, missing_reason="_wuwaterm_text_kernel not installed"
        ),
        "pyo3_wheel": _file_size_entry(
            wheel_path, missing_reason="wheel path not provided"
        ),
        "wasm_binary": _file_size_entry(
            wasm_path, missing_reason="wasm32 release artifact not built"
        ),
        "wasm_esm_wrapper": _file_size_entry(
            DEFAULT_WASM_MJS_PATH, missing_reason="ESM wrapper missing"
        ),
    }

    # 1. Cold start & RSS
    cold_runs = 2 if quick else 5
    py_base_cold = _measure_python_cold_import("pass", runs=cold_runs)
    py_spans_cold = _measure_python_cold_import(
        "import wuwaterm.text_spans, wuwaterm.segmentation", runs=cold_runs
    )
    py_spans_cold["rss_delta_mib"] = round(
        py_spans_cold["rss_mib"] - py_base_cold["rss_mib"], 2
    )

    pyo3_cold: dict[str, Any]
    if native_available:
        pyo3_cold = _measure_python_cold_import(
            "import _wuwaterm_text_kernel", runs=cold_runs
        )
        pyo3_cold["available"] = True
        pyo3_cold["rss_delta_mib"] = round(
            pyo3_cold["rss_mib"] - py_base_cold["rss_mib"], 2
        )
    else:
        pyo3_cold = {"available": False, "reason": "_wuwaterm_text_kernel not installed"}

    db_path = resolved_data / "cjk-census-3.7.db" if resolved_data is not None else None
    ac_build_runs = 2 if quick else 3
    py_ac_isolated = _measure_isolated_ac_build(
        "python_only", db_path=db_path, runs=ac_build_runs
    )
    py_ac_build_ms = float(py_ac_isolated["build_ms"])
    py_ac_rss_delta = float(py_ac_isolated["rss_delta_mib"])

    py_matcher = TermSpanMatcher.build(lockable, include_plural=False)
    py_scanner = native_kernel.TermScanner(surfaces, prefer_native=False)

    native_scanner: native_kernel.TermScanner | None = None
    native_matcher: _NativeBackedSpanMatcher | None = None
    native_ac_isolated: dict[str, Any] = {
        "available": False,
        "reason": "_wuwaterm_text_kernel not installed",
    }
    coexisting_ac_isolated: dict[str, Any] = {
        "available": False,
        "reason": "_wuwaterm_text_kernel not installed",
    }
    native_ac_build_ms: float | None = None
    native_ac_rss_delta: float | None = None
    if native_available:
        native_ac_isolated = _measure_isolated_ac_build(
            "native_only", db_path=db_path, runs=ac_build_runs
        )
        coexisting_ac_isolated = _measure_isolated_ac_build(
            "coexisting_both", db_path=db_path, runs=ac_build_runs
        )
        native_ac_build_ms = float(native_ac_isolated["build_ms"])
        native_ac_rss_delta = float(native_ac_isolated["rss_delta_mib"])
        native_scanner = native_kernel.TermScanner(surfaces, prefer_native=True)
        native_matcher = _NativeBackedSpanMatcher(lockable, native_scanner)

    # 2. FFI Boundary Micro-Call Overhead across payload sizes
    short_zh, short_en = workloads["short_50"]
    long_zh, long_en = workloads["long_2000"]
    xlarge_en = workloads["xlarge_10000"]
    a3_zh, a3_en = workloads["a3_pair"]

    ffi_payloads = [
        ("short_50", short_zh),
        ("long_2000", long_zh),
        ("xlarge_10000", xlarge_en),
    ]
    ffi_rows: list[dict[str, Any]] = []
    for pname, ptext in ffi_payloads:
        plen = len(ptext)
        mid = plen // 2

        # scalar_to_utf16
        py_s2u = _measure(
            lambda t=ptext, m=mid: native_kernel.scalar_to_utf16(
                t, m, prefer_native=False
            ),
            warmup=w_iters,
            iterations=n_iters,
        )
        nat_s2u = (
            _measure(
                lambda t=ptext, m=mid: native_kernel.scalar_to_utf16(
                    t, m, prefer_native=True
                ),
                warmup=w_iters,
                iterations=n_iters,
            )
            if native_available
            else None
        )
        ffi_rows.append(
            {
                "operation": "scalar_to_utf16",
                "payload": pname,
                "scalars": plen,
                "python_p50_ms": py_s2u["p50_ms"],
                "python_p95_ms": py_s2u["p95_ms"],
                "native_p50_ms": nat_s2u["p50_ms"] if nat_s2u else None,
                "native_p95_ms": nat_s2u["p95_ms"] if nat_s2u else None,
                "speedup_native_vs_py": (
                    round(py_s2u["p50_ms"] / nat_s2u["p50_ms"], 2)
                    if nat_s2u and nat_s2u["p50_ms"] > 0
                    else None
                ),
            }
        )

        # segment_text
        py_seg = _measure(
            lambda t=ptext: native_kernel.segment_text(t, prefer_native=False),
            warmup=w_iters,
            iterations=n_iters,
        )
        nat_seg = (
            _measure(
                lambda t=ptext: native_kernel.segment_text(t, prefer_native=True),
                warmup=w_iters,
                iterations=n_iters,
            )
            if native_available and digit_parity
            else None
        )
        ffi_rows.append(
            {
                "operation": "segment_text",
                "payload": pname,
                "scalars": plen,
                "python_p50_ms": py_seg["p50_ms"],
                "python_p95_ms": py_seg["p95_ms"],
                "native_p50_ms": nat_seg["p50_ms"] if nat_seg else None,
                "native_p95_ms": nat_seg["p95_ms"] if nat_seg else None,
                "speedup_native_vs_py": (
                    round(py_seg["p50_ms"] / nat_seg["p50_ms"], 2)
                    if nat_seg and nat_seg["p50_ms"] > 0
                    else None
                ),
            }
        )

        # term_scan_select (reusable automaton)
        py_scan = _measure(
            lambda t=ptext: py_scanner.scan_and_select(t),
            warmup=w_iters,
            iterations=n_iters,
        )
        nat_scan = (
            _measure(
                lambda t=ptext, sc=native_scanner: sc.scan_and_select(t),
                warmup=w_iters,
                iterations=n_iters,
            )
            if native_scanner is not None
            else None
        )
        nat_direct_scan = (
            _measure(
                lambda t=ptext, sc=native_scanner: getattr(
                    sc._impl, "scan_and_select"
                )(t),
                warmup=w_iters,
                iterations=n_iters,
            )
            if native_scanner is not None
            else None
        )
        ffi_rows.append(
            {
                "operation": "term_scan_select",
                "payload": pname,
                "scalars": plen,
                "python_p50_ms": py_scan["p50_ms"],
                "python_p95_ms": py_scan["p95_ms"],
                "native_p50_ms": nat_scan["p50_ms"] if nat_scan else None,
                "native_p95_ms": nat_scan["p95_ms"] if nat_scan else None,
                "native_direct_p50_ms": (
                    nat_direct_scan["p50_ms"] if nat_direct_scan else None
                ),
                "speedup_native_vs_py": (
                    round(py_scan["p50_ms"] / nat_scan["p50_ms"], 2)
                    if nat_scan and nat_scan["p50_ms"] > 0
                    else None
                ),
            }
        )

    def _lock_segment(
        text: str,
        *,
        prefix: str,
        matcher: Any,
    ) -> Any:
        return prepare_term_segment(
            text,
            lockable=lockable,
            context_text=text,
            context_offset=0,
            categories={},
            reference_only=frozenset(),
            to_chinese=None,
            allow_plural_inflection=False,
            nfkc_spelling_sources=frozenset(),
            placeholder_prefix=prefix,
            matcher=matcher,
        )

    # 3. Product-level E2E Workflows
    def _e2e_single_segment(src: str, tgt: str) -> dict[str, Any]:
        norm_stats = _measure(
            lambda: normalize_prose_text(src), warmup=w_iters, iterations=n_iters
        )
        seg_py = _measure(
            lambda: native_kernel.segment_text(src, prefer_native=False),
            warmup=w_iters,
            iterations=n_iters,
        )
        seg_nat = (
            _measure(
                lambda: native_kernel.segment_text(src, prefer_native=True),
                warmup=w_iters,
                iterations=n_iters,
            )
            if native_available and digit_parity
            else None
        )
        sel_py = _measure(
            lambda: select_term_spans(src, lockable, src, 0, matcher=py_matcher),
            warmup=w_iters,
            iterations=n_iters,
        )
        sel_nat = (
            _measure(
                lambda m=native_matcher: select_term_spans(
                    src, lockable, src, 0, matcher=m  # type: ignore[arg-type]
                ),
                warmup=w_iters,
                iterations=n_iters,
            )
            if native_matcher is not None
            else None
        )
        # Parity check between Pure Python TermSpanMatcher and Native AC + Python gates
        if native_matcher is not None:
            got_py = select_term_spans(src, lockable, src, 0, matcher=py_matcher)
            got_nat = select_term_spans(
                src, lockable, src, 0, matcher=native_matcher  # type: ignore[arg-type]
            )
            if got_py != got_nat:
                raise AssertionError(
                    f"TermSpan parity mismatch: py={len(got_py)} nat={len(got_nat)}"
                )

        prefix = new_placeholder_prefix(src)
        lock_py = _measure(
            lambda: _lock_segment(
                normalize_prose_text(src), prefix=prefix, matcher=py_matcher
            ),
            warmup=w_iters,
            iterations=n_iters,
        )
        lock_nat = (
            _measure(
                lambda m=native_matcher: _lock_segment(
                    normalize_prose_text(src), prefix=prefix, matcher=m
                ),
                warmup=w_iters,
                iterations=n_iters,
            )
            if native_matcher is not None
            else None
        )
        struct_stats = _measure(
            lambda: check_segment_structure(src, tgt), warmup=w_iters, iterations=n_iters
        )

        # Combined single-request E2E: normalize + segment + lock_terms + structure_check
        def _req_py() -> None:
            norm = normalize_prose_text(src)
            native_kernel.segment_text(norm, prefer_native=False)
            _lock_segment(norm, prefix=prefix, matcher=py_matcher)
            check_segment_structure(src, tgt)

        def _req_nat() -> None:
            assert native_matcher is not None
            norm = normalize_prose_text(src)
            native_kernel.segment_text(norm, prefer_native=True)
            _lock_segment(norm, prefix=prefix, matcher=native_matcher)
            check_segment_structure(src, tgt)

        req_py = _measure(_req_py, warmup=w_iters, iterations=n_iters)
        req_nat = (
            _measure(_req_nat, warmup=w_iters, iterations=n_iters)
            if native_matcher is not None and digit_parity
            else None
        )
        return {
            "source_scalars": len(src),
            "target_scalars": len(tgt),
            "normalize_p50_ms": norm_stats["p50_ms"],
            "segment_python_p50_ms": seg_py["p50_ms"],
            "segment_native_p50_ms": seg_nat["p50_ms"] if seg_nat else None,
            "select_term_spans_python_p50_ms": sel_py["p50_ms"],
            "select_term_spans_native_ac_p50_ms": sel_nat["p50_ms"] if sel_nat else None,
            "lock_terms_python_p50_ms": lock_py["p50_ms"],
            "lock_terms_native_ac_p50_ms": lock_nat["p50_ms"] if lock_nat else None,
            "structure_check_p50_ms": struct_stats["p50_ms"],
            "single_request_e2e_python_p50_ms": req_py["p50_ms"],
            "single_request_e2e_native_p50_ms": req_nat["p50_ms"] if req_nat else None,
            "single_request_e2e_speedup": (
                round(req_py["p50_ms"] / req_nat["p50_ms"], 2)
                if req_nat and req_nat["p50_ms"] > 0
                else None
            ),
        }

    short_e2e = _e2e_single_segment(short_zh, short_en)
    long_e2e = _e2e_single_segment(long_zh, long_en)

    # 500-segment manuscript batch
    manuscript_pairs: list[tuple[str, str]] = workloads["manuscript_pairs"]
    t0 = time.perf_counter()
    for src, _ in manuscript_pairs:
        normalize_prose_text(src)
    ms_norm_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    t0 = time.perf_counter()
    for src, tgt in manuscript_pairs:
        native_kernel.segment_text(src, prefer_native=False)
        native_kernel.pair_segments(src, tgt, prefer_native=False)
    ms_seg_py_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    ms_seg_nat_ms: float | None = None
    if native_available and digit_parity:
        t0 = time.perf_counter()
        for src, tgt in manuscript_pairs:
            native_kernel.segment_text(src, prefer_native=True)
            native_kernel.pair_segments(src, tgt, prefer_native=True)
        ms_seg_nat_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    t0 = time.perf_counter()
    for src, tgt in manuscript_pairs:
        check_segment_structure(src, tgt)
    ms_struct_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    t0 = time.perf_counter()
    for src, _ in manuscript_pairs:
        _lock_segment(src, prefix="__WUWA_BENCH_", matcher=py_matcher)
    ms_lock_py_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    ms_lock_nat_ms: float | None = None
    if native_matcher is not None:
        t0 = time.perf_counter()
        for src, _ in manuscript_pairs:
            _lock_segment(src, prefix="__WUWA_BENCH_", matcher=native_matcher)
        ms_lock_nat_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    ms_total_py = round(ms_norm_ms + ms_seg_py_ms + ms_struct_ms + ms_lock_py_ms, 3)
    ms_total_nat = (
        round(ms_norm_ms + ms_seg_nat_ms + ms_struct_ms + ms_lock_nat_ms, 3)
        if ms_seg_nat_ms is not None and ms_lock_nat_ms is not None
        else None
    )

    manuscript_batch = {
        "segment_count": len(manuscript_pairs),
        "total_chars": sum(len(s) + len(t) for s, t in manuscript_pairs),
        "normalize_ms": ms_norm_ms,
        "segment_and_pair_python_ms": ms_seg_py_ms,
        "segment_and_pair_native_ms": ms_seg_nat_ms,
        "structure_check_ms": ms_struct_ms,
        "lock_terms_python_ms": ms_lock_py_ms,
        "lock_terms_with_native_ac_ms": ms_lock_nat_ms,
        "total_python_ms": ms_total_py,
        "total_with_native_ms": ms_total_nat,
        "net_speedup": (
            round(ms_total_py / ms_total_nat, 2)
            if ms_total_nat is not None and ms_total_nat > 0
            else None
        ),
    }

    # 277k-line bulk corpus scan (build-db / MultiText scale)
    bulk_lines: list[str] = workloads["bulk_lines"]
    t0 = time.perf_counter()
    py_bulk_raw_hits = sum(len(py_scanner.scan_and_select(line)) for line in bulk_lines)
    bulk_py_ac_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    t0 = time.perf_counter()
    py_bulk_gated_hits = sum(
        len(select_term_spans(line, lockable, line, 0, matcher=py_matcher))
        for line in bulk_lines
    )
    bulk_py_e2e_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    bulk_nat_ac_ms: float | None = None
    bulk_nat_direct_ms: float | None = None
    bulk_nat_e2e_ms: float | None = None
    if native_scanner is not None and native_matcher is not None:
        t0 = time.perf_counter()
        nat_bulk_raw_hits = sum(
            len(native_scanner.scan_and_select(line)) for line in bulk_lines
        )
        bulk_nat_ac_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        if nat_bulk_raw_hits != py_bulk_raw_hits:
            raise AssertionError(
                f"Bulk raw hit mismatch: py={py_bulk_raw_hits} nat={nat_bulk_raw_hits}"
            )

        direct_scan_fn = getattr(native_scanner._impl, "scan_and_select")
        t0 = time.perf_counter()
        sum(len(direct_scan_fn(line)) for line in bulk_lines)
        bulk_nat_direct_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        t0 = time.perf_counter()
        nat_bulk_gated_hits = sum(
            len(select_term_spans(line, lockable, line, 0, matcher=native_matcher))
            for line in bulk_lines
        )
        bulk_nat_e2e_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        if nat_bulk_gated_hits != py_bulk_gated_hits:
            raise AssertionError(
                f"Bulk gated hit mismatch: py={py_bulk_gated_hits} nat={nat_bulk_gated_hits}"
            )

    bulk_corpus_scan = {
        "line_count": len(bulk_lines),
        "total_chars": sum(len(line) for line in bulk_lines),
        "source": "real_multitext" if workloads["real_multitext_available"] else "synthetic",
        "raw_hits": py_bulk_raw_hits,
        "gated_hits": py_bulk_gated_hits,
        "python_ac_scan_ms": bulk_py_ac_ms,
        "python_select_term_spans_ms": bulk_py_e2e_ms,
        "native_ac_scan_ms": bulk_nat_ac_ms,
        "native_direct_ac_scan_ms": bulk_nat_direct_ms,
        "native_with_py_gates_ms": bulk_nat_e2e_ms,
        "speedup_native_ac_vs_py_ac": (
            round(bulk_py_ac_ms / bulk_nat_ac_ms, 2)
            if bulk_nat_ac_ms is not None and bulk_nat_ac_ms > 0
            else None
        ),
        "speedup_native_direct_vs_py_ac": (
            round(bulk_py_ac_ms / bulk_nat_direct_ms, 2)
            if bulk_nat_direct_ms is not None and bulk_nat_direct_ms > 0
            else None
        ),
        "speedup_native_e2e_vs_py_e2e": (
            round(bulk_py_e2e_ms / bulk_nat_e2e_ms, 2)
            if bulk_nat_e2e_ms is not None and bulk_nat_e2e_ms > 0
            else None
        ),
    }

    # 4. WASM / Node.js E2E benchmark
    wasm_bench = _run_wasm_benchmarks(
        wasm_path=wasm_path,
        mjs_path=DEFAULT_WASM_MJS_PATH,
        surfaces=surfaces,
        short_text=short_zh,
        medium_text=long_zh,
        long_text=xlarge_en,
        pair_src=a3_zh,
        pair_tgt=a3_en,
        manuscript_pairs=manuscript_pairs,
        warmup=w_iters,
        iterations=n_iters,
    )

    return {
        "schema_version": "rust-adoption-bench-v1",
        "mode": "quick" if quick else "full",
        "environment": {
            "python_version": platform.python_version(),
            "unicode_version": __import__("unicodedata").unidata_version,
            "platform": f"{sys.platform}-{platform.machine()}",
            "native_kernel_available": native_available,
            "native_digit_parity_ok": digit_parity,
            "node_available": node_bin is not None,
            "node_version": node_version,
            "wasm_available": wasm_path.is_file(),
            "real_db_available": workloads["real_db_available"],
            "real_corpus_available": workloads["real_corpus_available"],
            "real_multitext_available": workloads["real_multitext_available"],
            "dictionary_surfaces": len(surfaces),
        },
        "artifacts": artifacts,
        "cold_start": {
            "python_baseline": py_base_cold,
            "python_text_spans_import": py_spans_cold,
            "pyo3_module_import": pyo3_cold,
            "ac_build_python_ms": py_ac_build_ms,
            "ac_build_python_rss_delta_mib": py_ac_rss_delta,
            "ac_build_native_ms": native_ac_build_ms,
            "ac_build_native_rss_delta_mib": native_ac_rss_delta,
            "ac_build_isolated": {
                "python_only": py_ac_isolated,
                "native_only": native_ac_isolated,
                "coexisting_both": coexisting_ac_isolated,
            },
            "wasm_load_kernel": (
                wasm_bench.get("cold_start")
                if wasm_bench.get("available")
                else {"available": False, "reason": wasm_bench.get("reason")}
            ),
        },
        "ffi_boundary": ffi_rows,
        "product_workflows": {
            "short_50": short_e2e,
            "long_2000": long_e2e,
            "manuscript_batch": manuscript_batch,
            "single_request_lock_and_structure": {
                "short_50_python_ms": short_e2e["single_request_e2e_python_p50_ms"],
                "short_50_native_ms": short_e2e["single_request_e2e_native_p50_ms"],
                "long_2000_python_ms": long_e2e["single_request_e2e_python_p50_ms"],
                "long_2000_native_ms": long_e2e["single_request_e2e_native_p50_ms"],
            },
            "bulk_corpus_scan": bulk_corpus_scan,
        },
        "wasm_benchmarks": wasm_bench,
    }


def format_markdown(report: dict[str, Any]) -> str:
    env = report["environment"]
    art = report["artifacts"]
    cold = report["cold_start"]
    pw = report["product_workflows"]
    lines: list[str] = [
        f"# Rust / PyO3 / WASM Adoption Benchmark ({report['mode']})",
        "",
        "## Environment",
        f"- Python: `{env['python_version']}` (Unicode `{env['unicode_version']}`), Platform: `{env['platform']}`",
        f"- `native_kernel_available`: `{env['native_kernel_available']}` (`digit_parity_ok={env['native_digit_parity_ok']}`)",
        f"- Node.js: `{env['node_version'] or 'missing'}`, WASM built: `{env['wasm_available']}`",
        f"- Corpus: `real_db={env['real_db_available']}`, `real_corpus={env['real_corpus_available']}`, `real_multitext={env['real_multitext_available']}`, surfaces=`{env['dictionary_surfaces']}`",
        "",
        "## Artifacts",
        "| Artifact | Available | Raw Size | Gzip Size |",
        "| --- | --- | --- | --- |",
    ]
    for key in ("pyo3_wheel", "pyo3_so", "wasm_binary", "wasm_esm_wrapper"):
        item = art[key]
        if item["available"]:
            lines.append(
                f"| `{key}` | yes | {item['raw_bytes']} B ({item['raw_kib']} KiB) | {item['gzip_bytes']} B ({item['gzip_kib']} KiB) |"
            )
        else:
            lines.append(f"| `{key}` | no ({item['reason']}) | - | - |")

    lines.extend(
        [
            "",
            "## Cold Start & Build",
            f"- Python baseline RSS: `{cold['python_baseline']['rss_mib']} MiB` (`{cold['python_baseline']['median_ms']} ms`)",
            f"- `text_spans + segmentation` import: `{cold['python_text_spans_import']['median_ms']} ms` (RSS delta `{cold['python_text_spans_import']['rss_delta_mib']} MiB`)",
        ]
    )
    if cold["pyo3_module_import"]["available"]:
        pyo3_c = cold["pyo3_module_import"]
        lines.append(
            f"- PyO3 `_wuwaterm_text_kernel` import: `{pyo3_c['median_ms']} ms` (RSS delta `{pyo3_c['rss_delta_mib']} MiB`)"
        )
    else:
        lines.append(
            f"- PyO3 `_wuwaterm_text_kernel` import: unavailable ({cold['pyo3_module_import']['reason']})"
        )
    def _fmt_ms(val: float | None) -> str:
        return f"{val} ms" if val is not None else "-"

    def _fmt_ratio(val: float | None) -> str:
        return f"{val}x" if val is not None else "-"

    coex_info = cold["ac_build_isolated"]["coexisting_both"]
    coex_str = (
        f"; coexisting both +{coex_info['rss_delta_mib']} MiB"
        if coex_info.get("available")
        else ""
    )
    lines.append(
        f"- AC build (`{env['dictionary_surfaces']}` surfaces, isolated): Python `{cold['ac_build_python_ms']} ms` (+{cold['ac_build_python_rss_delta_mib']} MiB), PyO3 `{_fmt_ms(cold['ac_build_native_ms'])}` (+{cold['ac_build_native_rss_delta_mib'] if cold['ac_build_native_rss_delta_mib'] is not None else '-'} MiB){coex_str}"
    )

    lines.extend(
        [
            "",
            "## FFI Boundary Overhead (p50 ms)",
            "| Operation | Payload | Scalars | Python (ms) | PyO3 (ms) | Speedup |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in report["ffi_boundary"]:
        nat_s = f"{row['native_p50_ms']:.4f}" if row["native_p50_ms"] is not None else "-"
        spd_s = (
            f"{row['speedup_native_vs_py']:.2f}x"
            if row["speedup_native_vs_py"] is not None
            else "-"
        )
        lines.append(
            f"| `{row['operation']}` | `{row['payload']}` | {row['scalars']} | {row['python_p50_ms']:.4f} | {nat_s} | {spd_s} |"
        )

    mb = pw["manuscript_batch"]
    bk = pw["bulk_corpus_scan"]
    lines.extend(
        [
            "",
            "## Product-Level E2E Workflows",
            f"- Single request (`short_50`): Python `{pw['short_50']['single_request_e2e_python_p50_ms']} ms` vs PyO3 `{_fmt_ms(pw['short_50']['single_request_e2e_native_p50_ms'])}`",
            f"- Single request (`long_2000`): Python `{pw['long_2000']['single_request_e2e_python_p50_ms']} ms` vs PyO3 `{_fmt_ms(pw['long_2000']['single_request_e2e_native_p50_ms'])}`",
            f"- Manuscript batch (`{mb['segment_count']}` segments): total Python `{mb['total_python_ms']} ms` vs PyO3 `{_fmt_ms(mb['total_with_native_ms'])}` (speedup `{_fmt_ratio(mb['net_speedup'])}`)",
            f"- Bulk scan (`{bk['line_count']}` lines, `{bk['source']}`): Py AC `{bk['python_ac_scan_ms']} ms` vs PyO3 AC `{_fmt_ms(bk['native_ac_scan_ms'])}` (direct `{_fmt_ms(bk['native_direct_ac_scan_ms'])}`); E2E w/ gates: Py `{bk['python_select_term_spans_ms']} ms` vs PyO3 `{_fmt_ms(bk['native_with_py_gates_ms'])}`",
        ]
    )

    wb = report["wasm_benchmarks"]
    if wb.get("available"):
        wcs = wb["cold_start"]
        lines.extend(
            [
                "",
                "## WASM / Node.js Comparison",
                f"- `loadKernel` cold compile+instantiate: `{wcs['load_kernel_ms']} ms` (+`{wcs['rss_delta_load_mib']} MiB` RSS); AC build: `{wcs['ac_build_ms']} ms` (+`{wcs['rss_delta_ac_mib']} MiB` RSS)",
                f"- Manuscript batch (`{wb['manuscript_batch']['segment_count']}` segs segment+scan): `{wb['manuscript_batch']['segment_plus_scan_ms']} ms`",
            ]
        )
    else:
        lines.extend(["", f"## WASM / Node.js Comparison\n- Unavailable: {wb.get('reason')}"])

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure end-to-end Rust / PyO3 / WASM adoption costs and speedups."
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument("--markdown", action="store_true", help="Emit Markdown summary")
    parser.add_argument("--quick", action="store_true", help="Fast CI-friendly subset")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--wasm-path", type=Path, default=DEFAULT_WASM_PATH)
    parser.add_argument("--wheel-path", type=Path, default=None)
    parser.add_argument("--warmup", type=int, default=None)
    parser.add_argument("--iterations", type=int, default=None)
    args = parser.parse_args(argv)

    report = run_benchmark(
        quick=args.quick,
        data_dir=args.data_dir,
        wasm_path=args.wasm_path,
        wheel_path=args.wheel_path,
        warmup=args.warmup,
        iterations=args.iterations,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
