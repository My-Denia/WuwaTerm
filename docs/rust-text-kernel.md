# Shared Rust Text Kernel (`crates/` & `wuwaterm.native_kernel`)

WuwaTerm ships a multi-target Rust Cargo workspace under [`crates/`](../crates/README.md) alongside an optional, non-breaking Python adapter [`src/wuwaterm/native_kernel.py`](../src/wuwaterm/native_kernel.py).

## Why a Shared Pure-Algorithm Kernel

`segmentation-v1`, Unicode scalar <-> UTF-16 coordinate mapping, and multi-pattern exact term scanning are deterministic pure text algorithms shared by Core (Python) and Web/Worker clients (JavaScript/TypeScript). Centralizing them in a zero-dependency (`std`-only) Rust crate eliminates cross-runtime Unicode drift (such as host-language `isdigit()` version differences or UTF-16 surrogate slicing bugs) without introducing mandatory native builds into the Python package.

## Workspace Layout

- [`crates/wuwaterm-text-kernel/`](../crates/wuwaterm-text-kernel/): Pure `std`-only core library (`text_spans`, `segmentation`, `aho_corasick`) and `wuwaterm-kernel-cli` binary. Zero network, SQLite, or UI dependencies.
- [`crates/wuwaterm-text-kernel-py/`](../crates/wuwaterm-text-kernel-py/): Optional PyO3 / maturin extension building the `_wuwaterm_text_kernel` Python module.
- [`crates/wuwaterm-text-kernel-wasm/`](../crates/wuwaterm-text-kernel-wasm/): `wasm32-unknown-unknown` `cdylib` plus a zero-bundler ESM wrapper ([`js/index.mjs`](../crates/wuwaterm-text-kernel-wasm/js/index.mjs)) and TypeScript definitions ([`js/index.d.ts`](../crates/wuwaterm-text-kernel-wasm/js/index.d.ts)) for Browser, Web Worker, and Node.js runtimes.
- [`crates/scripts/run_differential_and_bench.py`](../crates/scripts/run_differential_and_bench.py): Differential parity harness against `contracts/segmentation-v1.json` (40/40 fixtures), all 1,112,064 non-surrogate Unicode scalars, seeded fuzz corpora, and optional real dictionary benchmarks.

## Non-Breaking Python Adoption (`wuwaterm.native_kernel`)

`src/wuwaterm/native_kernel.py` is an opt-in pure-matching adapter:

1. It probes for the optional `_wuwaterm_text_kernel` native extension lazily on first call.
2. When the native extension is absent (the default for pure-Python installs) or `WUWATERM_DISABLE_NATIVE_KERNEL=1` is set, every function and `TermScanner` transparently delegates to the pure-Python implementation with identical return types and Unicode-scalar semantics.
3. Existing runtime modules (`lookup.py`, `term_matching.py`, `application.py`, `wuwaterm_api/`) and `pyproject.toml` require no Rust toolchain and incur zero breaking changes.

## Build & CI Verification

Heavy builds (`wasm32-unknown-unknown` release compilation, PyO3 wheel builds via `maturin`, and full differential parity) run automatically on `ubuntu-latest` in [`.github/workflows/rust-text-kernel.yml`](../.github/workflows/rust-text-kernel.yml).

For quick local verification:

```bash
# Core pure-Rust unit tests
cd crates && cargo test -p wuwaterm-text-kernel

# Python fallback & delegation unit tests
python -m pytest tests/test_native_kernel.py
```

See [`crates/README.md`](../crates/README.md) for full PyO3 and WASM build commands, and [`docs/research/rust-text-kernel-and-aho-corasick.md`](research/rust-text-kernel-and-aho-corasick.md) for the earlier research baseline and phase timing analysis.
