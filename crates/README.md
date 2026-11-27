# WuwaTerm Shared Text Kernel Workspace (`crates/`)

Multi-target Rust workspace promoted from `research/rust-text-kernel/` so Core (Python/PyO3) and Web/Workers (WASM) can share one deterministic pure-algorithm implementation without runtime network, SQLite, or UI dependencies.

## Workspace Members

| Crate | Purpose | Runtime Dependencies |
| --- | --- | --- |
| [`wuwaterm-text-kernel`](wuwaterm-text-kernel/) | Pure `std`-only core library + `wuwaterm-kernel-cli` differential/bench runner | None (`std` only) |
| [`wuwaterm-text-kernel-py`](wuwaterm-text-kernel-py/) | Optional PyO3 / maturin Python native extension (`_wuwaterm_text_kernel`) | `pyo3`, core crate |
| [`wuwaterm-text-kernel-wasm`](wuwaterm-text-kernel-wasm/) | Optional `wasm32-unknown-unknown` `cdylib` + zero-bundler ESM wrapper (`js/index.mjs`) | `serde`, `serde_json`, core crate |

## Core Algorithms

- Frozen Unicode predicates (`is_spec_whitespace`, `is_py_isspace`, frozen Unicode 15.0.0 `is_py_isdigit`)
- Exact Unicode scalar <-> UTF-16 <-> UTF-8 coordinate conversion and O(N) batch maps
- Full `segmentation-v1` (`segment_core`, `paragraph_ranges`, `sentence_ranges`, `sentence_ranges_v2`, `review_sentence_ranges_v2`, `segment_text`, `pair_segments`)
- Linear-pass UTF-8 Aho–Corasick multi-pattern scanner preserving all overlapping matches, surface priority order, Unicode scalar spans, and WuwaTerm longest non-overlapping interval selection

## Build & Verification

### 1. Core pure-Rust unit tests (fast, zero external deps)

```bash
cd crates && cargo test -p wuwaterm-text-kernel
```

### 2. WASM target build + Node.js 40/40 contract verification

```bash
rustup target add wasm32-unknown-unknown
cd crates && cargo build -p wuwaterm-text-kernel-wasm --target wasm32-unknown-unknown --release
node wuwaterm-text-kernel-wasm/js/test_contracts.mjs
```

### 3. Optional PyO3 extension build (maturin)

```bash
maturin build --release -m crates/wuwaterm-text-kernel-py/Cargo.toml -o crates/target/wheels
pip install --force-reinstall crates/target/wheels/*.whl
```

### 4. Differential parity & optional benchmark harness

```bash
# Differential parity only (1,112,064 Unicode scalars + 40/40 contracts + 200 fuzz texts)
python -B crates/scripts/run_differential_and_bench.py --skip-bench

# Include PyO3 verification when `_wuwaterm_text_kernel` is installed
python -B crates/scripts/run_differential_and_bench.py --skip-bench --verify-pyo3
```

All three targets (core + differential, WASM + Node.js 40/40 contracts, PyO3 wheel + pytest) run automatically on GitHub Actions via [`.github/workflows/rust-text-kernel.yml`](../.github/workflows/rust-text-kernel.yml).
