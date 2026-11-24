# Research Report: Pure-Algorithm Rust Text Kernel & Offline Aho–Corasick Evaluation

Date: 2026-11-24  
Baseline commit: `301efed4813bdc79359fc4dfa864cfb4ad13f7a9` (`WuwaTerm v0.12.0` + `#310` `text_spans` boundary)

## 1. Objective

Evaluate whether the pure-algorithm boundary introduced in `#310` ([`src/wuwaterm/text_spans.py`](../../src/wuwaterm/text_spans.py), [`src/wuwaterm/segmentation.py`](../../src/wuwaterm/segmentation.py), and exact span selection in [`src/wuwaterm/term_matching.py`](../../src/wuwaterm/term_matching.py)) supports:

1. A deterministic, zero-dependency (`std`-only) Rust text kernel matching Python behavior across all `segmentation-v1` contracts, Unicode scalar / UTF-16 coordinates, and exact dictionary interval selection.
2. Quantifying how much wall-clock time Aho–Corasick saves vs the current per-surface `str.find()` loop over the `19,175`-surface dictionary, separating **algorithmic speedup** (linear scan -> single-pass Aho–Corasick) from **implementation language speedup** (Python -> Rust) and from Python's downstream boundary/lexicon checks.

Production code under `src/wuwaterm/`, `src/wuwaterm_api/`, `pyproject.toml`, and `uv.lock` was left untouched.

## 2. Prototype Scope (`research/rust-text-kernel/`)

- Zero external Cargo dependencies (`Cargo.toml` uses Rust `std` only, `edition = "2024"`, compiled with `rustc 1.98.0` / `cargo 1.98.0`; release CLI binary `wuwaterm-kernel-cli` is `642,176` bytes).
- [`src/text_spans.rs`](../../research/rust-text-kernel/src/text_spans.rs): exact 24-scalar spec `WHITESPACE`, Python `str.isspace()`, Python 3.12 `str.isdigit()` closed range table, `segment_core`, `paragraph_ranges`, `sentence_ranges`, `sentence_ranges_v2`, `review_sentence_ranges_v2`, and scalar <-> UTF-16 <-> UTF-8 coordinate maps.
- [`src/segmentation.rs`](../../research/rust-text-kernel/src/segmentation.rs): exact `segment_text` and `pair_segments` (paragraph/sentence/hard-cut partitioning, proportional bilingual fallback cuts, alignment validation, empty-pair absorption, and greedy merge).
- [`src/aho_corasick.rs`](../../research/rust-text-kernel/src/aho_corasick.rs): UTF-8 byte-trie Aho–Corasick automaton with dictionary suffix links (`dict_link`) emitting every overlapping match mapped to exact Unicode scalar `[start, end)` intervals, naive `str::find` baseline, and `select_longest_non_overlapping` matching `term_matching.py:435-452`.
- [`run_differential_and_bench.py`](../../research/rust-text-kernel/run_differential_and_bench.py): binary-framed differential harness comparing Rust against Python and committed contract fixtures, plus pure-Python `PyAhoCorasick` and `select_term_spans` phase timing breakdowns.

## 3. Differential Equivalence Results

| Suite | Scope | Result | Notes |
| --- | --- | --- | --- |
| Rust unit tests (`cargo test`) | Coordinate roundtrips, Unicode markers, overlaps | `4/4` pass | — |
| Exhaustive Unicode predicates | All `1,112,064` non-surrogate scalars (`U+0000..=U+10FFFF`) | `0` mismatches on Python `3.12.3` (`Unicode 15.0.0`) | On Python `3.11.15` (`Unicode 14.0.0`), `isspace()` has `0` mismatches and `isdigit()` differs on `20` scalars (`U+11F50..=U+11F59` Kawi, `U+1E4F0..=U+1E4F9` Nag Mundari) |
| `contracts/segmentation-v1.json` | `20` segment + `20` pair fixtures (incl. alignment errors) | `20/20` + `20/20` | Exact match with Python & committed fixture outputs |
| Real corpus + Unicode fuzz (`text_spans` + `segment_text`) | `23` real manuscripts (`40,956` scalars) + `200` seeded stress texts | `223/223` | Zero drift on surrogate pairs, decimal dots, list markers, and multi-pass hard cuts |
| Dynamic bilingual `pair_segments` | `5` real `A1`–`A5` pairs + `40` synthetic stress pairs | `45/45` | Exact status, hard-cut, and partition parity |
| Real dictionary multi-pattern scan + interval select | `19,175` `exact_lockable_sources` (`cjk-census-3.7.db`) across `23` real docs | `775` spans, `0` mismatches | Python naive `str.find`, pure-Python AC, Rust naive `str::find`, and Rust AC all produce identical `(start, end, order)` triples |

### Concrete Cross-Language Pitfalls Observed

1. **`str.isdigit()` is Unicode-version-sensitive and differs from `is_ascii_digit()` / `is_numeric()`**: Python `str.isdigit()` includes superscripts (`²`, `³`) and excludes vulgar fractions (`½`), and expanded by `20` codepoints between Python 3.11 (`Unicode 14.0.0`) and Python 3.12 (`Unicode 15.0.0`). Any shared cross-client kernel must freeze an explicit codepoint table rather than delegating to each host language's stdlib digit predicate.
2. **Dual whitespace predicates in `segmentation-v1`**: `_separator_only` intentionally uses Python's `str.isspace()` (`24` spec whitespace scalars + `U+001C..=U+001F` + `U+0085`), whereas `segment_core`, `paragraph_ranges`, and `HARD_CUT` use only the `24` spec scalars (`WHITESPACE`). Conflating the two breaks partition/separator parity.
3. **Byte vs scalar vs UTF-16 coordinates**: Running Aho–Corasick over UTF-8 bytes avoids per-char Unicode decoding during scanning, so mapping matched `[start_byte, end_byte)` through an O(1) `byte_to_scalar` vector preserves exact Unicode scalar spans for mixed CJK/ASCII/astral text without scanning overhead.

## 4. Offline Benchmark Results (`data/cjk-census-3.7.db` + `wuwaterm-real-manuscripts-20261110`)

Measured offline on Linux `x86_64` (`Python 3.11.15`, `rustc 1.98.0 --release`; `11,329` terms -> `19,175` lockable surfaces; `169,387` UTF-8 trie states; `warmup=3`, `iterations=20`, median ms):

### A. Automaton Build & `segmentation-v1`

| Operation | Pure Python | Rust (`std`-only) | Ratio |
| --- | --- | --- | --- |
| Build Aho–Corasick (`19,175` surfaces) | `96.098 ms` | `12.514 ms` (p95 `16.775 ms`) | `7.7x` faster |
| `segment_text + pair_segments` (`A3`: `4,121` zh / `11,464` en) | `0.6938 ms` (p95 `0.7714 ms`) | `0.0414 ms` (p95 `0.0615 ms`) | `16.8x` faster |

### B. Exact Term Scanning & Phase Decomposition

`PY_FULL` runs production `select_term_spans(..., text, 0)` (including `_matched_span_ok` + `crosses_cjk_word_boundary`); `PY_FIND` times only the `19,175`-iteration `text.find` loop; `PY_GATE` times `_matched_span_ok` over the raw hits; `PY_NAIVE` / `PY_AC` / `RUST_NAIVE` / `RUST_AC` compute the identical raw multi-pattern match + global longest non-overlapping interval selection function.

| Workload | Scalars | Raw / Selected Hits | `PY_FULL` (ms) | `PY_FIND` only (ms) | `PY_GATE` only (ms) | `PY_NAIVE` (ms) | Pure-Py `PY_AC` (ms) | `RUST_NAIVE` (ms) | `RUST_AC` (ms) [p95] | Speedup (`PY_NAIVE` -> `PY_AC` / `RUST_AC`) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `2000ch_zh` | `2,000` | `49` / `41` | `11.742` | `10.736` (`91.4%`) | `0.244` | `10.937` | `0.195` | `20.773` | `0.0331` [`0.0412`] | `56.1x` / `330x` |
| `2000ch_en` | `2,000` | `11` / `11` | `12.708` | `12.340` (`97.1%`) | `0.003` | `12.422` | `0.109` | `12.629` | `0.0055` [`0.0056`] | `114x` / `2,259x` |
| `A3_zh` | `4,121` | `97` / `85` | `22.418` | `20.672` (`92.2%`) | `0.468` | `21.244` | `0.471` | `40.072` | `0.0900` [`0.0990`] | `45.1x` / `236x` |
| `A3_en` | `11,464` | `180` / `155` | `66.046` | `64.441` (`97.6%`) | `0.044` | `65.278` | `1.051` | `64.147` | `0.0727` [`0.0825`] | `62.1x` / `898x` |
| `all_docs` (`23` files) | `40,956` | `883` / `775` | `244.008` | `228.318` (`93.6%`) | `2.654` | `236.798` | `4.762` | `325.136` | `0.7728` [`0.9279`] | `49.7x` / `306x` |

### Key Analytical Takeaways

1. **Rewriting without changing the algorithm does not speed up scanning**: Rust naive `str::find` (`325.1 ms` on `23` docs, `20.8 ms` on `2000ch_zh`) is slightly slower than CPython's C-implemented `str.find` loop (`236.8 ms` / `10.9 ms`) because UTF-8 scalar projection runs on every pattern pass when scanning naively.
2. **Algorithm change dominates language change**: Replacing the `19,175`-pass scan with Aho–Corasick in **pure Python alone** cuts `all_docs` raw scan+select from `236.8 ms` to `4.762 ms` (`49.7x`) and single `2,000`-scalar segments from `10.9–12.4 ms` to `0.109–0.195 ms` (`56–114x`).
3. **Downstream boundary checks are NOT a hidden bottleneck**: Across all `23` real manuscripts, `_matched_span_ok` (including jieba `crosses_cjk_word_boundary`) takes `2.654 ms` (`1.1%` of `244.0 ms`), while the `19,175`-pattern `text.find` loop consumes `228.318 ms` (`93.6%`). Eliminating repeated candidate passes captures >`90%` of CPU time even if boundary checks stay unchanged in Python.
4. **Build cost is one-time**: Constructing the `19,175`-surface automaton takes `~96 ms` in pure Python (or `~12 ms` in Rust), so caching it per `TermService` snapshot amortizes to zero after ~9 `2,000`-char calls in Python or ~1 call in Rust.

## 5. Conclusions & Adoption Recommendation

1. **Keep production on Python baseline for now (no PyO3 / cross-platform wheel matrix)**: Single-segment online translation/review spends `~12 ms` in `select_term_spans` (`< 1%` of external LLM latency). Introducing PyO3/maturin cross-platform wheel builds adds release complexity without solving a live API bottleneck.
2. **If `select_term_spans` throughput ever matters (e.g., `builder.py` full-corpus scans or higher offline batch volume), land pure-Python Aho–Corasick first**: A lazily cached pure-Python automaton captures a `49.7x` scan speedup (`~12 ms -> ~0.4 ms` per `2,000`-scalar segment including boundary checks) with zero native build dependencies. Note that opt-in modes (`allow_plural_inflection`, `nfkc_spelling_sources`) can either feed a combined surface automaton or append candidates cleanly while preserving `exact_bounds` priority.
3. **Strongest value for the Rust kernel is single-source cross-client reuse (`Core` + `Web` WASM + Desktop/Tauri)**: The prototype already proves `100%` differential equivalence on `segmentation-v1`, `text_spans`, `scalar <-> UTF-16` coordinates, exact multi-pattern selection, and isolates subtle host Unicode drift (such as Python 3.11 vs 3.12 `isdigit()`). Next justified milestone before production integration is a lightweight WASM (`wasm32-unknown-unknown`) + PyO3 experiment gated behind explicit multi-client ownership reduction once `WuwaTerm-Web` pure workbench state stabilizes.
