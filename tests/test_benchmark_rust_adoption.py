from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_rust_adoption.py"

sys.path.insert(0, str(ROOT / "scripts"))
import benchmark_rust_adoption as bench_mod  # noqa: E402


def _assert_no_local_home_paths(payload_text: str) -> None:
    assert "/home/" not in payload_text
    assert "/Users/" not in payload_text
    assert "C:\\Users\\" not in payload_text


def _assert_schema_complete(data: dict[str, object]) -> None:
    assert data["schema_version"] == "rust-adoption-bench-v1"
    assert data["mode"] in {"quick", "full"}
    env = data["environment"]
    assert isinstance(env, dict)
    for key in (
        "python_version",
        "unicode_version",
        "platform",
        "native_kernel_available",
        "node_available",
        "wasm_available",
        "dictionary_surfaces",
    ):
        assert key in env

    artifacts = data["artifacts"]
    assert isinstance(artifacts, dict)
    for key in ("pyo3_so", "pyo3_wheel", "wasm_binary", "wasm_esm_wrapper"):
        assert key in artifacts

    cold = data["cold_start"]
    assert isinstance(cold, dict)
    assert "python_baseline" in cold
    assert "python_text_spans_import" in cold
    assert "pyo3_module_import" in cold

    ffi = data["ffi_boundary"]
    assert isinstance(ffi, list)
    assert len(ffi) >= 6

    pw = data["product_workflows"]
    assert isinstance(pw, dict)
    for key in (
        "short_50",
        "long_2000",
        "manuscript_batch",
        "single_request_lock_and_structure",
        "bulk_corpus_scan",
    ):
        assert key in pw
    assert "wasm_benchmarks" in data


def test_benchmark_quick_json_without_native_kernel(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["WUWATERM_DISABLE_NATIVE_KERNEL"] = "1"
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--quick",
            "--json",
            "--data-dir",
            str(tmp_path / "missing-data"),
            "--wasm-path",
            str(tmp_path / "missing.wasm"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    _assert_no_local_home_paths(proc.stdout)
    data = json.loads(proc.stdout)
    _assert_schema_complete(data)
    assert data["mode"] == "quick"
    assert data["environment"]["native_kernel_available"] is False
    assert data["cold_start"]["pyo3_module_import"]["available"] is False
    assert data["wasm_benchmarks"]["available"] is False
    assert data["product_workflows"]["bulk_corpus_scan"]["source"] == "synthetic"
    assert data["product_workflows"]["bulk_corpus_scan"]["native_ac_scan_ms"] is None


def test_benchmark_quick_markdown_formats_cleanly(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["WUWATERM_DISABLE_NATIVE_KERNEL"] = "1"
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--quick",
            "--markdown",
            "--data-dir",
            str(tmp_path / "missing-data"),
            "--wasm-path",
            str(tmp_path / "missing.wasm"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    _assert_no_local_home_paths(proc.stdout)
    assert "# Rust / PyO3 / WASM Adoption Benchmark (quick)" in proc.stdout
    assert "None ms" not in proc.stdout
    assert "Nonex" not in proc.stdout


def test_benchmark_quick_with_stubbed_native_kernel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from wuwaterm import native_kernel, segmentation, text_spans

    class FakeAutomaton:
        def __init__(self, surfaces: list[str]) -> None:
            self._py = native_kernel._PyTermAutomaton(surfaces)

        @property
        def pattern_count(self) -> int:
            return self._py.pattern_count

        @property
        def state_count(self) -> int:
            return self._py.state_count

        def find_all(self, text: str) -> list[tuple[int, int, int]]:
            return self._py.find_all(text)

        def scan_and_select(self, text: str) -> list[tuple[int, int, int]]:
            return self._py.scan_and_select(text)

    fake_mod = SimpleNamespace(
        is_py_isdigit=lambda ch: ch.isdigit(),
        scalar_to_utf16=lambda t, s: native_kernel.scalar_to_utf16(
            t, s, prefer_native=False
        ),
        utf16_to_scalar=lambda t, u: native_kernel.utf16_to_scalar(
            t, u, prefer_native=False
        ),
        segment_core=text_spans.segment_core,
        paragraph_ranges=text_spans.paragraph_ranges,
        sentence_ranges=text_spans.sentence_ranges,
        sentence_ranges_v2=text_spans.sentence_ranges_v2,
        review_sentence_ranges_v2=text_spans.review_sentence_ranges_v2,
        segment_text=lambda t, lim: [
            (s.start, s.end, s.core_start, s.core_end, s.hard_cut)
            for s in segmentation.segment_text(t, lim)
        ],
        pair_segments=lambda src, tgt, lim, align: [
            (
                p.index,
                p.status,
                p.hard_cut,
                (p.source.start, p.source.end, p.source.core_start, p.source.core_end),
                (p.target.start, p.target.end, p.target.core_start, p.target.core_end),
            )
            for p in segmentation.pair_segments(src, tgt, lim, align)
        ],
        TermAutomaton=FakeAutomaton,
    )

    monkeypatch.delenv("WUWATERM_DISABLE_NATIVE_KERNEL", raising=False)
    native_kernel.reset_native_kernel_cache()
    monkeypatch.setattr(native_kernel, "_native_module_cache", fake_mod)
    monkeypatch.setattr(native_kernel.unicodedata, "unidata_version", "15.0.0")
    monkeypatch.setattr(native_kernel, "_digit_parity_cache", True)
    monkeypatch.setattr(
        bench_mod,
        "_measure_python_cold_import",
        lambda stmt, runs: {"median_ms": 1.0, "p95_ms": 1.0, "rss_mib": 12.0},
    )
    monkeypatch.setattr(bench_mod, "_resolve_pyo3_so", lambda: None)

    try:
        report = bench_mod.run_benchmark(
            quick=True,
            data_dir=tmp_path / "missing-data",
            wasm_path=tmp_path / "missing.wasm",
            wheel_path=None,
            warmup=1,
            iterations=2,
        )
    finally:
        native_kernel.reset_native_kernel_cache()

    raw_json = json.dumps(report, ensure_ascii=False)
    _assert_no_local_home_paths(raw_json)
    _assert_schema_complete(report)
    assert report["environment"]["native_kernel_available"] is True
    assert report["product_workflows"]["short_50"]["single_request_e2e_native_p50_ms"] is not None
    assert report["product_workflows"]["manuscript_batch"]["total_with_native_ms"] is not None
    assert report["product_workflows"]["bulk_corpus_scan"]["native_ac_scan_ms"] is not None
