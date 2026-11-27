#!/usr/bin/env python3
"""Differential equivalence & optional benchmark harness for `crates/wuwaterm-text-kernel`.

Verifies 0-diff parity between:
1. Rust core CLI (`wuwaterm-kernel-cli`)
2. Pure-Python reference (`wuwaterm.text_spans`, `wuwaterm.segmentation`, `wuwaterm.native_kernel`)
3. Committed contract fixtures (`contracts/segmentation-v1.json`: 20 segment + 20 pair)
4. Optional PyO3 extension (`_wuwaterm_text_kernel`, enabled via `--verify-pyo3`)
5. Optional real dictionary (`cjk-census-3.7.db`) & manuscript corpus when present
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import statistics
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
CRATES_DIR = ROOT / "crates"
sys.path.insert(0, str(ROOT / "src"))

from wuwaterm import native_kernel, segmentation, text_spans  # noqa: E402
from wuwaterm.lookup import TermService  # noqa: E402
from wuwaterm.term_matching import (  # noqa: E402
    _matched_span_ok,
    exact_lockable_sources,
    select_term_spans,
)

STATUS_BY_CODE = {
    0: segmentation.STATUS_READY,
    1: segmentation.STATUS_NEEDS_MANUAL_SPLIT,
    2: segmentation.STATUS_UNTRANSLATED,
    3: segmentation.STATUS_UNPAIRED_TARGET,
}


def pack_str(buf: bytearray, text: str) -> None:
    raw = text.encode("utf-8")
    buf.extend(struct.pack("<I", len(raw)))
    buf.extend(raw)


def unpack_u8(data: bytes, pos: int) -> tuple[int, int]:
    return data[pos], pos + 1


def unpack_u32(data: bytes, pos: int) -> tuple[int, int]:
    return struct.unpack_from("<I", data, pos)[0], pos + 4


def unpack_str(data: bytes, pos: int) -> tuple[str, int]:
    length, pos = unpack_u32(data, pos)
    return data[pos : pos + length].decode("utf-8"), pos + length


def unpack_pairs(data: bytes, pos: int) -> tuple[list[tuple[int, int]], int]:
    count, pos = unpack_u32(data, pos)
    out: list[tuple[int, int]] = []
    for _ in range(count):
        a, pos = unpack_u32(data, pos)
        b, pos = unpack_u32(data, pos)
        out.append((a, b))
    return out, pos


def run_cli(cli_bin: Path, subcmd: str, payload: bytes) -> bytes:
    proc = subprocess.run(
        [str(cli_bin), subcmd],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{subcmd} failed (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace')}"
        )
    return proc.stdout


def verify_unicode_tables(cli_bin: Path) -> int:
    scalars = [chr(cp) for cp in range(0x110000) if not (0xD800 <= cp <= 0xDFFF)]
    payload = bytearray()
    pack_str(payload, "".join(scalars))
    out = run_cli(cli_bin, "unicode-predicates", bytes(payload))
    count, pos = unpack_u32(out, 0)
    assert count == len(scalars) == len(out) - pos
    flags = out[pos:]
    # Rust table is pinned to Unicode 15.0.0 (Python 3.12); Python 3.11 uses
    # Unicode 14.0.0, which lacks Kawi (U+11F50..U+11F59) and Nag Mundari
    # (U+1E4F0..U+1E4F9) digits.
    unicode_15_digit_blocks = set(range(0x11F50, 0x11F5A)) | set(range(0x1E4F0, 0x1E4FA))
    digit_version_deltas = 0
    for idx, ch in enumerate(scalars):
        got_space = bool(flags[idx] & 1)
        got_digit = bool(flags[idx] & 2)
        if got_space != ch.isspace():
            raise AssertionError(
                f"Unicode isspace mismatch at U+{ord(ch):04X}: "
                f"rust={got_space} vs py={ch.isspace()}"
            )
        if got_digit != ch.isdigit():
            if sys.version_info < (3, 12) and ord(ch) in unicode_15_digit_blocks:
                digit_version_deltas += 1
                continue
            raise AssertionError(
                f"Unicode isdigit mismatch at U+{ord(ch):04X}: "
                f"rust={got_digit} vs py={ch.isdigit()}"
            )
    return digit_version_deltas


def call_rust_text_spans(
    cli_bin: Path, texts: list[str]
) -> list[dict[str, list[tuple[int, int]] | tuple[int, int]]]:
    payload = bytearray(struct.pack("<I", len(texts)))
    for t in texts:
        pack_str(payload, t)
    out = run_cli(cli_bin, "text-spans", bytes(payload))
    pos = 0
    results = []
    for _ in texts:
        core_list, pos = unpack_pairs(out, pos)
        paras, pos = unpack_pairs(out, pos)
        sents_v1, pos = unpack_pairs(out, pos)
        sents_v2, pos = unpack_pairs(out, pos)
        rev_v2, pos = unpack_pairs(out, pos)
        assert len(core_list) == 1
        results.append(
            {
                "core": core_list[0],
                "paragraphs": paras,
                "sentences": sents_v1,
                "sentences_v2": sents_v2,
                "review_v2": rev_v2,
            }
        )
    assert pos == len(out)
    return results


def call_rust_segment(
    cli_bin: Path, cases: list[tuple[int, str]]
) -> list[list[dict[str, object]]]:
    payload = bytearray(struct.pack("<I", len(cases)))
    for limit, text in cases:
        payload.extend(struct.pack("<I", limit))
        pack_str(payload, text)
    out = run_cli(cli_bin, "segment", bytes(payload))
    pos = 0
    results = []
    for _ in cases:
        n, pos = unpack_u32(out, pos)
        segs = []
        for _ in range(n):
            start, pos = unpack_u32(out, pos)
            end, pos = unpack_u32(out, pos)
            cs, pos = unpack_u32(out, pos)
            ce, pos = unpack_u32(out, pos)
            hc, pos = unpack_u8(out, pos)
            segs.append(
                {
                    "start": start,
                    "end": end,
                    "core_start": cs,
                    "core_end": ce,
                    "hard_cut": bool(hc),
                }
            )
        results.append(segs)
    assert pos == len(out)
    return results


def call_rust_pair(
    cli_bin: Path,
    cases: list[tuple[int, str, str, list[dict[str, object]] | None]],
) -> list[tuple[str, object]]:
    payload = bytearray(struct.pack("<I", len(cases)))
    for limit, source, target, alignments in cases:
        payload.extend(struct.pack("<I", limit))
        pack_str(payload, source)
        pack_str(payload, target)
        if alignments is None:
            payload.append(0)
        else:
            payload.append(1)
            payload.extend(struct.pack("<I", len(alignments)))
            for item in alignments:
                src = item["source"]
                assert isinstance(src, dict)
                payload.extend(struct.pack("<II", src["start"], src["end"]))
                pack_str(payload, src["text"])
                tgt = item["target"]
                if tgt is None:
                    payload.append(0)
                else:
                    assert isinstance(tgt, dict)
                    payload.append(1)
                    payload.extend(struct.pack("<II", tgt["start"], tgt["end"]))
                    pack_str(payload, tgt["text"])
    out = run_cli(cli_bin, "pair", bytes(payload))
    pos = 0
    results: list[tuple[str, object]] = []
    for _ in cases:
        ok, pos = unpack_u8(out, pos)
        if ok == 0:
            msg, pos = unpack_str(out, pos)
            results.append(("err", msg))
        else:
            n, pos = unpack_u32(out, pos)
            pairs = []
            for _ in range(n):
                idx, pos = unpack_u32(out, pos)
                code, pos = unpack_u8(out, pos)
                hc, pos = unpack_u8(out, pos)
                ss, pos = unpack_u32(out, pos)
                se, pos = unpack_u32(out, pos)
                scs, pos = unpack_u32(out, pos)
                sce, pos = unpack_u32(out, pos)
                ts, pos = unpack_u32(out, pos)
                te, pos = unpack_u32(out, pos)
                tcs, pos = unpack_u32(out, pos)
                tce, pos = unpack_u32(out, pos)
                pairs.append(
                    {
                        "index": idx,
                        "status": STATUS_BY_CODE[code],
                        "hard_cut": bool(hc),
                        "source": {
                            "start": ss,
                            "end": se,
                            "core_start": scs,
                            "core_end": sce,
                        },
                        "target": {
                            "start": ts,
                            "end": te,
                            "core_start": tcs,
                            "core_end": tce,
                        },
                    }
                )
            results.append(("ok", pairs))
    assert pos == len(out)
    return results


def call_rust_scan_select(
    cli_bin: Path, patterns: list[str], texts: list[str]
) -> list[list[tuple[int, int, int]]]:
    payload = bytearray(struct.pack("<I", len(patterns)))
    for pat in patterns:
        pack_str(payload, pat)
    payload.extend(struct.pack("<I", len(texts)))
    for t in texts:
        pack_str(payload, t)
    out = run_cli(cli_bin, "scan-select", bytes(payload))
    pos = 0
    results = []
    for _ in texts:
        n, pos = unpack_u32(out, pos)
        spans = []
        for _ in range(n):
            a, pos = unpack_u32(out, pos)
            b, pos = unpack_u32(out, pos)
            order, pos = unpack_u32(out, pos)
            spans.append((a, b, order))
        results.append(spans)
    assert pos == len(out)
    return results


def py_find_all_naive(patterns: list[str], text: str) -> list[tuple[int, int, int]]:
    matches: list[tuple[int, int, int]] = []
    for order, pat in enumerate(patterns):
        if not pat:
            continue
        start = text.find(pat)
        while start != -1:
            matches.append((start, start + len(pat), order))
            start = text.find(pat, start + 1)
    return matches


def build_unicode_stress_cases(seed: int = 20261124, count: int = 200) -> list[str]:
    rng = random.Random(seed)
    ws_chars = sorted(text_spans.WHITESPACE)
    py_only_spaces = ["\u001c", "\u001d", "\u001e", "\u001f", "\u0085"]
    tokens = [
        "甲", "乙", "声骸", "椿", "。", "．", ".", "！", "？", "!", "?", "\n",
        "\r\n", "\r\n\r\n", "\n\u3000\n", "，", "、", "；", "：", ",", ";", ":",
        "1.", "12.", "IV.", "3.7", "3²．5", "３．７", "🌊", "✨", "Alpha", "Beta",
        "Echo", "Resonator", " ", "\t",
    ]
    cases = [
        "",
        "".join(ws_chars),
        "".join(py_only_spaces),
        "".join(py_only_spaces) + "。",
        "🌊✨。3.7 和 3²．5\n\u001c\u0085。\n\nIV. Item two.\n  12.\tTabbed label.",
        "甲" * 1900,
        ("甲" * 300 + "，") * 8,
        ("Alpha " * 320).strip(),
    ]
    while len(cases) < count:
        parts = []
        n_tok = rng.randint(5, 90)
        for _ in range(n_tok):
            if rng.random() < 0.15:
                parts.append(rng.choice(ws_chars))
            elif rng.random() < 0.05:
                parts.append(rng.choice(py_only_spaces))
            else:
                parts.append(rng.choice(tokens))
        if rng.random() < 0.12:
            parts.append("甲" * rng.randint(1805, 2200))
        cases.append("".join(parts))
    return cases[:count]


def load_real_corpus(data_dir: Path) -> list[tuple[str, str]]:
    corpus_dir = data_dir / "eval-corpus" / "wuwaterm-real-manuscripts-20261110"
    if not corpus_dir.exists():
        return []
    docs: list[tuple[str, str]] = []
    for path in sorted(corpus_dir.glob("*/*.txt")):
        rel = f"{path.parent.name}/{path.name}"
        docs.append((rel, path.read_text(encoding="utf-8")))
    return docs


def verify_segmentation_fixtures(cli_bin: Path, *, verify_pyo3: bool) -> tuple[int, int]:
    fixture = json.loads(
        (ROOT / "contracts" / "segmentation-v1.json").read_text(encoding="utf-8")
    )
    limit = int(fixture["limit"])
    seg_cases = fixture["segment_cases"]
    rust_segs = call_rust_segment(cli_bin, [(limit, case["text"]) for case in seg_cases])
    for case, got_rust in zip(seg_cases, rust_segs, strict=True):
        got_py = [s.to_dict() for s in segmentation.segment_text(case["text"], limit)]
        if got_py != case["segments"] or got_rust != got_py:
            raise AssertionError(f"segment_case mismatch on {case['name']}")
        if verify_pyo3:
            got_native = [
                s.to_dict()
                for s in native_kernel.segment_text(case["text"], limit, prefer_native=True)
            ]
            if got_native != got_py:
                raise AssertionError(f"pyo3 segment_case mismatch on {case['name']}")

    pair_cases = fixture["pair_cases"]
    rust_pairs = call_rust_pair(
        cli_bin,
        [
            (limit, case["source"], case["target"], case.get("alignments"))
            for case in pair_cases
        ],
    )
    for case, got_rust in zip(pair_cases, rust_pairs, strict=True):
        if "error" in case:
            try:
                segmentation.pair_segments(
                    case["source"],
                    case["target"],
                    limit,
                    alignments=case.get("alignments"),
                )
                raise AssertionError(f"Python expected error on {case['name']}")
            except ValueError:
                pass
            if got_rust[0] != "err":
                raise AssertionError(f"Rust expected error on {case['name']}, got {got_rust}")
            if verify_pyo3:
                try:
                    native_kernel.pair_segments(
                        case["source"],
                        case["target"],
                        limit,
                        alignments=case.get("alignments"),
                        prefer_native=True,
                    )
                    raise AssertionError(f"PyO3 expected error on {case['name']}")
                except ValueError:
                    pass
        else:
            got_py = [
                p.to_dict()
                for p in segmentation.pair_segments(
                    case["source"],
                    case["target"],
                    limit,
                    alignments=case.get("alignments"),
                )
            ]
            if got_py != case["pairs"] or got_rust != ("ok", got_py):
                raise AssertionError(f"pair_case mismatch on {case['name']}")
            if verify_pyo3:
                got_native = [
                    p.to_dict()
                    for p in native_kernel.pair_segments(
                        case["source"],
                        case["target"],
                        limit,
                        alignments=case.get("alignments"),
                        prefer_native=True,
                    )
                ]
                if got_native != got_py:
                    raise AssertionError(f"pyo3 pair_case mismatch on {case['name']}")
    return len(seg_cases), len(pair_cases)


def verify_corpus_and_fuzz_parity(
    cli_bin: Path, real_docs: list[tuple[str, str]], *, verify_pyo3: bool
) -> tuple[int, int]:
    stress = build_unicode_stress_cases()
    all_texts = [t for _, t in real_docs] + stress
    rust_spans = call_rust_text_spans(cli_bin, all_texts)
    rust_segs = call_rust_segment(
        cli_bin, [(segmentation.SEGMENT_LIMIT, t) for t in all_texts]
    )

    for idx, text in enumerate(all_texts):
        py_core = text_spans.segment_core(text, 0, len(text))
        py_paras = text_spans.paragraph_ranges(text)
        py_v1 = text_spans.sentence_ranges(text)
        py_v2 = text_spans.sentence_ranges_v2(text)
        py_rev = text_spans.review_sentence_ranges_v2(text)
        rs = rust_spans[idx]
        if (
            rs["core"] != py_core
            or rs["paragraphs"] != py_paras
            or rs["sentences"] != py_v1
            or rs["sentences_v2"] != py_v2
            or rs["review_v2"] != py_rev
        ):
            raise AssertionError(f"text_spans mismatch at index {idx}")

        py_s = [s.to_dict() for s in segmentation.segment_text(text)]
        if rust_segs[idx] != py_s:
            raise AssertionError(f"segment_text mismatch at index {idx}")

        if verify_pyo3:
            if (
                native_kernel.segment_core(text, 0, len(text), prefer_native=True) != py_core
                or native_kernel.paragraph_ranges(text, prefer_native=True) != py_paras
                or native_kernel.sentence_ranges(text, prefer_native=True) != py_v1
                or native_kernel.sentence_ranges_v2(text, prefer_native=True) != py_v2
                or native_kernel.review_sentence_ranges_v2(text, prefer_native=True) != py_rev
            ):
                raise AssertionError(f"pyo3 text_spans mismatch at index {idx}")

    by_name = dict(real_docs)
    pair_inputs: list[tuple[int, str, str, list[dict[str, object]] | None]] = []
    for folder in (
        "A1-sanhua-developer-notes",
        "A2-danjin-developer-notes",
        "A3-v3.7-maintenance-notice",
        "A4-convene-jinhsi",
        "A5-convene-camellya",
    ):
        if f"{folder}/zh.txt" in by_name and f"{folder}/en.txt" in by_name:
            pair_inputs.append(
                (
                    segmentation.SEGMENT_LIMIT,
                    by_name[f"{folder}/zh.txt"],
                    by_name[f"{folder}/en.txt"],
                    None,
                )
            )
    rng = random.Random(9911)
    for _ in range(40):
        src = rng.choice(stress)
        tgt = rng.choice(stress)
        pair_inputs.append((segmentation.SEGMENT_LIMIT, src, tgt, None))

    rust_pairs = call_rust_pair(cli_bin, pair_inputs)
    for idx, (limit, src, tgt, align) in enumerate(pair_inputs):
        py_pairs = [
            p.to_dict() for p in segmentation.pair_segments(src, tgt, limit, align)
        ]
        if rust_pairs[idx] != ("ok", py_pairs):
            raise AssertionError(f"pair_segments mismatch at index {idx}")

    return len(all_texts), len(pair_inputs)


def verify_multi_pattern_parity(
    cli_bin: Path,
    patterns: list[str],
    real_docs: list[tuple[str, str]],
    *,
    verify_pyo3: bool,
) -> int:
    synth_patterns = ["声骸", "声骸技能", "技能", "ab", "bab", "🌊术语", "术语"]
    synth_texts = [
        "",
        "🌊使用声骸技能ababa✨🌊术语声骸",
        "abababa技能声骸技能",
    ]
    py_scanner_synth = native_kernel.TermScanner(synth_patterns, prefer_native=False)
    native_scanner_synth = (
        native_kernel.TermScanner(synth_patterns, prefer_native=True) if verify_pyo3 else None
    )
    rust_synth = call_rust_scan_select(cli_bin, synth_patterns, synth_texts)
    for idx, text in enumerate(synth_texts):
        naive = native_kernel.select_longest_non_overlapping(
            py_find_all_naive(synth_patterns, text), synth_patterns
        )
        ac_res = py_scanner_synth.scan_and_select(text)
        if rust_synth[idx] != naive or ac_res != naive:
            raise AssertionError(
                f"Synthetic multi-pattern mismatch on doc {idx}: "
                f"rust={rust_synth[idx]} ac={ac_res} naive={naive}"
            )
        if native_scanner_synth is not None:
            got_nat = native_scanner_synth.scan_and_select(text)
            if got_nat != naive:
                raise AssertionError(f"PyO3 synthetic scan mismatch on doc {idx}")

    if not patterns or not real_docs:
        return 0

    doc_texts = [t for _, t in real_docs]
    py_scanner = native_kernel.TermScanner(patterns, prefer_native=False)
    native_scanner = (
        native_kernel.TermScanner(patterns, prefer_native=True) if verify_pyo3 else None
    )
    rust_real = call_rust_scan_select(cli_bin, patterns, doc_texts)
    total_selected = 0
    for (name, text), got_rust in zip(real_docs, rust_real, strict=True):
        got_naive = native_kernel.select_longest_non_overlapping(
            py_find_all_naive(patterns, text), patterns
        )
        got_ac = py_scanner.scan_and_select(text)
        if got_rust != got_naive or got_ac != got_naive:
            raise AssertionError(f"Real corpus scan mismatch on {name}")
        if native_scanner is not None and native_scanner.scan_and_select(text) != got_naive:
            raise AssertionError(f"PyO3 real corpus scan mismatch on {name}")
        total_selected += len(got_rust)
    return total_selected


def measure_py(call, *, warmup: int, iterations: int) -> tuple[float, float]:
    for _ in range(warmup):
        call()
    samples: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        call()
        samples.append((time.perf_counter() - t0) * 1000.0)
    ordered = sorted(samples)
    med = statistics.median(ordered)
    p95 = ordered[max(0, min(len(ordered) - 1, round(len(ordered) * 0.95) - 1))]
    return med, p95


def run_benchmarks(
    cli_bin: Path,
    lockable: tuple[tuple[str, tuple[str, str]], ...],
    real_docs: list[tuple[str, str]],
    *,
    warmup: int,
    iterations: int,
) -> dict[str, object]:
    patterns = [surface for surface, _ in lockable]
    by_name = dict(real_docs)
    a3_zh = by_name["A3-v3.7-maintenance-notice/zh.txt"]
    a3_en = by_name["A3-v3.7-maintenance-notice/en.txt"]
    workloads = [
        ("2000ch_zh", a3_zh[:2000]),
        ("2000ch_en", a3_en[:2000]),
        ("A3_zh", a3_zh),
        ("A3_en", a3_en),
    ]

    py_ac_build_med, py_ac_build_p95 = measure_py(
        lambda: native_kernel.TermScanner(patterns, prefer_native=False),
        warmup=1,
        iterations=3,
    )
    py_scanner = native_kernel.TermScanner(patterns, prefer_native=False)

    py_rows: list[dict[str, object]] = []
    all_texts = [t for _, t in real_docs]

    for wname, wtext in [*workloads, ("all_docs", None)]:
        texts = [wtext] if wtext is not None else all_texts
        b_iters = max(1, min(iterations, 5 if wname in ("A3_en", "all_docs") else 10))
        ac_iters = iterations

        full_med, full_p95 = measure_py(
            lambda ts=texts: sum(len(select_term_spans(t, lockable, t, 0)) for t in ts),
            warmup=1,
            iterations=b_iters,
        )
        naive_sel_med, naive_sel_p95 = measure_py(
            lambda ts=texts: sum(
                len(
                    native_kernel.select_longest_non_overlapping(
                        py_find_all_naive(patterns, t), patterns
                    )
                )
                for t in ts
            ),
            warmup=1,
            iterations=b_iters,
        )
        py_ac_med, py_ac_p95 = measure_py(
            lambda ts=texts: sum(len(py_scanner.scan_and_select(t)) for t in ts),
            warmup=warmup,
            iterations=ac_iters,
        )

        raw_cands_per_doc = []
        for t in texts:
            cands = []
            for surface, official in lockable:
                p = t.find(surface)
                while p != -1:
                    cands.append((p, p + len(surface), surface, official))
                    p = t.find(surface, p + 1)
            raw_cands_per_doc.append((t, cands))
        gate_med, _ = measure_py(
            lambda rc=raw_cands_per_doc: sum(
                1
                for t, cands in rc
                for a, b, surf, off in cands
                if _matched_span_ok(t, 0, a, b, surf, off)
            ),
            warmup=warmup,
            iterations=ac_iters,
        )

        py_rows.append(
            {
                "workload": wname,
                "chars": sum(len(t) for t in texts),
                "py_select_term_spans_med": full_med,
                "py_select_term_spans_p95": full_p95,
                "py_boundary_gate_med": gate_med,
                "py_naive_scan_select_med": naive_sel_med,
                "py_naive_scan_select_p95": naive_sel_p95,
                "py_ac_scan_select_med": py_ac_med,
                "py_ac_scan_select_p95": py_ac_p95,
                "raw_candidate_hits": sum(len(c) for _, c in raw_cands_per_doc),
            }
        )

    py_seg_med, py_seg_p95 = measure_py(
        lambda: (
            len(segmentation.segment_text(a3_zh)),
            len(segmentation.pair_segments(a3_zh, a3_en)),
        ),
        warmup=warmup,
        iterations=iterations,
    )

    payload = bytearray(struct.pack("<I", len(patterns)))
    for pat in patterns:
        pack_str(payload, pat)
    payload.extend(struct.pack("<I", len(workloads)))
    for wname, wtext in workloads:
        pack_str(payload, wname)
        pack_str(payload, wtext)
    payload.extend(struct.pack("<II", warmup, iterations))
    pack_str(payload, a3_zh)
    pack_str(payload, a3_en)
    sub_tsv = run_cli(cli_bin, "bench", bytes(payload)).decode("utf-8").strip().splitlines()

    return {
        "py_ac_build_med": py_ac_build_med,
        "py_ac_build_p95": py_ac_build_p95,
        "py_rows": py_rows,
        "py_seg_pair_a3": (py_seg_med, py_seg_p95),
        "rust_workloads_tsv": sub_tsv,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run differential equivalence checks and optional benchmarks for crates/wuwaterm-text-kernel."
    )
    parser.add_argument("--skip-bench", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--release", action="store_true", help="Build/use release profile CLI")
    parser.add_argument("--cli-bin", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--verify-pyo3", action="store_true")
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()

    if args.verify_pyo3 and not native_kernel.native_kernel_available():
        raise SystemExit("--verify-pyo3 requested but _wuwaterm_text_kernel is not available")

    profile = "release" if (args.release or not args.skip_bench) else "debug"
    if args.cli_bin is not None:
        cli_bin = args.cli_bin
    else:
        if not args.skip_build:
            cmd = ["cargo", "build", "-p", "wuwaterm-text-kernel", "--bin", "wuwaterm-kernel-cli"]
            if profile == "release":
                cmd.append("--release")
            subprocess.run(cmd, cwd=CRATES_DIR, check=True)
        cli_bin = CRATES_DIR / "target" / profile / "wuwaterm-kernel-cli"

    digit_deltas = verify_unicode_tables(cli_bin)
    seg_n, pair_n = verify_segmentation_fixtures(cli_bin, verify_pyo3=args.verify_pyo3)
    real_docs = load_real_corpus(args.data_dir)
    ts_n, dyn_pair_n = verify_corpus_and_fuzz_parity(
        cli_bin, real_docs, verify_pyo3=args.verify_pyo3
    )

    db_path = args.data_dir / "cjk-census-3.7.db"
    patterns: list[str] = []
    lockable: tuple[tuple[str, tuple[str, str]], ...] = ()
    if db_path.exists():
        svc = TermService(db_path)
        lockable = exact_lockable_sources(svc.entries())
        patterns = [surface for surface, _ in lockable]

    selected_hits = verify_multi_pattern_parity(
        cli_bin, patterns, real_docs, verify_pyo3=args.verify_pyo3
    )

    print("DIFF_EQUIVALENCE PASS")
    print(f"unicode_predicates_checked\t1112064\tunicode15_digit_deltas={digit_deltas}")
    print(f"contract_segment_cases\t{seg_n}/{seg_n}")
    print(f"contract_pair_cases\t{pair_n}/{pair_n}")
    print(f"text_spans_and_segment_corpus_fuzz\t{ts_n}/{ts_n}")
    print(f"dynamic_pair_corpus_fuzz\t{dyn_pair_n}/{dyn_pair_n}")
    print(
        f"multi_pattern_parity\tdocs={len(real_docs)}\t"
        f"surfaces={len(patterns)}\tselected_hits={selected_hits}\tmismatches=0"
    )
    print(f"pyo3_verified\t{args.verify_pyo3}")

    if not args.skip_bench and real_docs and lockable:
        report = run_benchmarks(
            cli_bin,
            lockable,
            real_docs,
            warmup=args.warmup,
            iterations=args.iterations,
        )
        print("\nBENCH_PYTHON_AC_BUILD_MS", f"{report['py_ac_build_med']:.3f}")
        print(
            "BENCH_PYTHON_SEG_PAIR_A3_MS",
            f"med={report['py_seg_pair_a3'][0]:.4f}",
            f"p95={report['py_seg_pair_a3'][1]:.4f}",
        )
        print("\nWORKLOAD\tCHARS\tPY_FULL_MS\tPY_GATE_MS\tPY_NAIVE_MS\tPY_AC_MS\tCAND_HITS")
        for row in report["py_rows"]:
            print(
                f"{row['workload']}\t{row['chars']}\t"
                f"{row['py_select_term_spans_med']:.3f}\t"
                f"{row['py_boundary_gate_med']:.3f}\t"
                f"{row['py_naive_scan_select_med']:.3f}\t"
                f"{row['py_ac_scan_select_med']:.3f}\t"
                f"{row['raw_candidate_hits']}"
            )
        print("\nRUST_WORKLOAD_TSV")
        for line in report["rust_workloads_tsv"]:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
