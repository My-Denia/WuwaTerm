# Rust Text Kernel Prototype (`research/rust-text-kernel`)

Zero-dependency (`std`-only) Rust prototype porting `wuwaterm.text_spans`, `wuwaterm.segmentation`, and exact multi-pattern Aho–Corasick term scanning + longest non-overlapping interval selection from [the pure text primitives](../../src/wuwaterm/text_spans.py) and [the segmentation spec](../../docs/segmentation-v1.md).

Research-only: production Python under `src/wuwaterm/` remains unchanged.

## Build, Differential Check, and Offline Benchmark

From repository root:

```bash
# 1. Rust unit tests + release binary build
cd research/rust-text-kernel && cargo test && cargo build --release && cd ../..

# 2. Differential parity against Python + real-corpus/dictionary Aho-Corasick benchmark
.venv/bin/python -B research/rust-text-kernel/run_differential_and_bench.py

# Differential parity only (no timing loops)
.venv/bin/python -B research/rust-text-kernel/run_differential_and_bench.py --skip-bench
```

See [the evaluation report](../../docs/research/rust-text-kernel-and-aho-corasick.md) for measured results, cross-runtime findings, and adoption criteria.
