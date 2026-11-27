from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wuwaterm import native_kernel, segmentation, text_spans

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "contracts" / "segmentation-v1.json"


def _load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_python_fallback_matches_all_segmentation_v1_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WUWATERM_DISABLE_NATIVE_KERNEL", "1")
    native_kernel.reset_native_kernel_cache()

    assert native_kernel.native_kernel_available() is False
    assert native_kernel.kernel_backend() == "python"

    fixture = _load_fixture()
    limit = int(fixture["limit"])  # type: ignore[arg-type]

    for case in fixture["segment_cases"]:  # type: ignore[attr-defined]
        got = [s.to_dict() for s in native_kernel.segment_text(case["text"], limit)]
        assert got == case["segments"], case["name"]

    for case in fixture["pair_cases"]:  # type: ignore[attr-defined]
        if "error" in case:
            with pytest.raises(ValueError):
                native_kernel.pair_segments(
                    case["source"],
                    case["target"],
                    limit,
                    alignments=case.get("alignments"),
                )
        else:
            got = [
                p.to_dict()
                for p in native_kernel.pair_segments(
                    case["source"],
                    case["target"],
                    limit,
                    alignments=case.get("alignments"),
                )
            ]
            assert got == case["pairs"], case["name"]


def test_text_spans_and_utf16_coordinates_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WUWATERM_DISABLE_NATIVE_KERNEL", "1")
    native_kernel.reset_native_kernel_cache()

    sample = "  1. First item. 3.7 和 3²．5。下一句。\n\n乙。  "
    assert native_kernel.segment_core(sample, 0, len(sample)) == text_spans.segment_core(
        sample, 0, len(sample)
    )
    assert native_kernel.paragraph_ranges(sample) == text_spans.paragraph_ranges(sample)
    assert native_kernel.sentence_ranges(sample) == text_spans.sentence_ranges(sample)
    assert native_kernel.sentence_ranges_v2(sample) == text_spans.sentence_ranges_v2(sample)
    assert native_kernel.review_sentence_ranges_v2(
        sample
    ) == text_spans.review_sentence_ranges_v2(sample)

    # Astral / surrogate UTF-16 roundtrip
    astral = "🌊椿A🚀"
    assert native_kernel.scalar_to_utf16(astral, 0) == 0
    assert native_kernel.scalar_to_utf16(astral, 1) == 2
    assert native_kernel.scalar_to_utf16(astral, 2) == 3
    assert native_kernel.scalar_to_utf16(astral, 3) == 4
    assert native_kernel.scalar_to_utf16(astral, 4) == 6
    assert native_kernel.utf16_to_scalar(astral, 0) == 0
    assert native_kernel.utf16_to_scalar(astral, 1) == 0
    assert native_kernel.utf16_to_scalar(astral, 2) == 1
    assert native_kernel.utf16_to_scalar(astral, 5) == 3
    assert native_kernel.utf16_to_scalar(astral, 6) == 4

    # ASCII fast path & bounds validation
    assert native_kernel.scalar_to_utf16("hello", 3) == 3
    assert native_kernel.utf16_to_scalar("hello", 99) == 5
    with pytest.raises(ValueError):
        native_kernel.scalar_to_utf16("hello", -1)
    with pytest.raises(ValueError):
        native_kernel.utf16_to_scalar("hello", -1)


def test_term_scanner_overlaps_priority_and_empty_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WUWATERM_DISABLE_NATIVE_KERNEL", "1")
    native_kernel.reset_native_kernel_cache()

    surfaces = ["声骸", "声骸技能", "技能", "ab", "bab", ""]
    scanner = native_kernel.TermScanner(surfaces)
    assert scanner.backend == "python"
    assert scanner.pattern_count == len(surfaces)
    assert scanner.state_count > 1

    text = "🌊使用声骸技能ababa✨"
    raw = sorted(scanner.find_all(text))
    assert raw == [
        (3, 5, 0),
        (3, 7, 1),
        (5, 7, 2),
        (7, 9, 3),
        (8, 11, 4),
        (9, 11, 3),
    ]
    assert scanner.scan_and_select(text) == [(3, 7, 1), (8, 11, 4)]
    assert scanner.find_all("") == []
    assert native_kernel.TermScanner([]).find_all(text) == []


def test_native_delegation_path_with_stub_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise every native-delegation branch deterministically even without a compiled wheel."""
    monkeypatch.delenv("WUWATERM_DISABLE_NATIVE_KERNEL", raising=False)
    native_kernel.reset_native_kernel_cache()

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
        scalar_to_utf16=lambda t, s: native_kernel.scalar_to_utf16(t, s, prefer_native=False),
        utf16_to_scalar=lambda t, u: native_kernel.utf16_to_scalar(t, u, prefer_native=False),
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
    monkeypatch.setattr(native_kernel, "_native_module_cache", fake_mod)
    monkeypatch.setattr(native_kernel.unicodedata, "unidata_version", "15.0.0")
    monkeypatch.setattr(native_kernel, "_digit_parity_cache", None)

    assert native_kernel.native_kernel_available() is True
    assert native_kernel.kernel_backend() == "native"
    assert native_kernel.kernel_backend(prefer_native=False) == "python"

    assert native_kernel.scalar_to_utf16("🌊A", 1) == 2
    assert native_kernel.utf16_to_scalar("🌊A", 2) == 1
    assert native_kernel.segment_core(" 甲 ", 0, 3) == (1, 2)
    assert native_kernel.paragraph_ranges("甲。\n\n乙。") == [(0, 4), (4, 6)]
    assert native_kernel.sentence_ranges("3.7。") == [(0, 2), (2, 4)]
    assert native_kernel.sentence_ranges_v2("3.7。") == [(0, 4)]
    assert native_kernel.review_sentence_ranges_v2("1. Item.") == [(0, 8)]
    assert native_kernel.segment_text("甲。", 1800) == segmentation.segment_text("甲。", 1800)
    assert native_kernel.pair_segments("甲。", "A.", 1800) == segmentation.pair_segments(
        "甲。", "A.", 1800
    )

    scanner = native_kernel.TermScanner(["声骸", "声骸技能"])
    assert scanner.backend == "native"
    assert scanner.pattern_count == 2
    assert scanner.state_count > 1
    assert scanner.scan_and_select("声骸技能") == [(0, 4, 1)]
    assert sorted(scanner.find_all("声骸技能")) == [(0, 2, 0), (0, 4, 1)]

    native_kernel.reset_native_kernel_cache()


def test_unicode_14_vs_15_isdigit_mismatch_falls_back_to_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Python's Unicode version disagrees with Rust's frozen 15.0.0 isdigit() table,
    digit-dependent segmentation/spans must fall back to pure Python (e.g. U+11F50 + '.1')."""
    monkeypatch.delenv("WUWATERM_DISABLE_NATIVE_KERNEL", raising=False)
    native_kernel.reset_native_kernel_cache()

    called_native: list[str] = []

    def _boom(name: str):
        def _fn(*_args, **_kwargs):
            called_native.append(name)
            raise AssertionError(f"native {name} should not run on mismatched Unicode runtime")

        return _fn

    fake_mod = SimpleNamespace(
        is_py_isdigit=lambda ch: ch in {"\u11f50", "\u1e4f0", "\u00b2"} or ch.isdigit(),
        sentence_ranges_v2=_boom("sentence_ranges_v2"),
        review_sentence_ranges_v2=_boom("review_sentence_ranges_v2"),
        segment_text=_boom("segment_text"),
        pair_segments=_boom("pair_segments"),
        scalar_to_utf16=lambda t, s: native_kernel.scalar_to_utf16(t, s, prefer_native=False),
    )
    monkeypatch.setattr(native_kernel, "_native_module_cache", fake_mod)
    monkeypatch.setattr(native_kernel.unicodedata, "unidata_version", "14.0.0")
    monkeypatch.setattr(native_kernel, "_digit_parity_cache", None)

    for sample in ("\u11f50.1", "\u1e4f0.1"):
        assert native_kernel.sentence_ranges_v2(sample) == text_spans.sentence_ranges_v2(sample)
        assert native_kernel.review_sentence_ranges_v2(
            sample
        ) == text_spans.review_sentence_ranges_v2(sample)
        assert native_kernel.segment_text(sample, limit=2) == segmentation.segment_text(
            sample, limit=2
        )
        assert native_kernel.pair_segments(sample, sample, limit=2) == segmentation.pair_segments(
            sample, sample, limit=2
        )
    assert called_native == []
    # Non-isdigit operations still use native module normally.
    assert native_kernel.scalar_to_utf16("🌊A", 1) == 2

    native_kernel.reset_native_kernel_cache()


def test_real_native_extension_parity_if_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When `_wuwaterm_text_kernel` is actually built & installed (e.g. in CI), verify 40/40 parity."""
    monkeypatch.delenv("WUWATERM_DISABLE_NATIVE_KERNEL", raising=False)
    native_kernel.reset_native_kernel_cache()
    if not native_kernel.native_kernel_available():
        pytest.skip("_wuwaterm_text_kernel native extension is not installed in this environment")

    fixture = _load_fixture()
    limit = int(fixture["limit"])  # type: ignore[arg-type]

    for case in fixture["segment_cases"]:  # type: ignore[attr-defined]
        native_out = [s.to_dict() for s in native_kernel.segment_text(case["text"], limit)]
        assert native_out == case["segments"], case["name"]

    for case in fixture["pair_cases"]:  # type: ignore[attr-defined]
        if "error" in case:
            with pytest.raises(ValueError):
                native_kernel.pair_segments(
                    case["source"],
                    case["target"],
                    limit,
                    alignments=case.get("alignments"),
                )
        else:
            native_out = [
                p.to_dict()
                for p in native_kernel.pair_segments(
                    case["source"],
                    case["target"],
                    limit,
                    alignments=case.get("alignments"),
                )
            ]
            assert native_out == case["pairs"], case["name"]

    # Unicode 14.0 vs 15.0 regression: U+11F50 (Kawi) and U+1E4F0 (Nag Mundari) + ".1" at limit=2.
    for sample in ("\u11f50.1", "\u1e4f0.1"):
        assert native_kernel.sentence_ranges_v2(sample) == text_spans.sentence_ranges_v2(sample)
        assert native_kernel.review_sentence_ranges_v2(
            sample
        ) == text_spans.review_sentence_ranges_v2(sample)
        assert native_kernel.segment_text(sample, limit=2) == segmentation.segment_text(
            sample, limit=2
        )
        assert native_kernel.pair_segments(sample, sample, limit=2) == segmentation.pair_segments(
            sample, sample, limit=2
        )

    surfaces = ["声骸", "声骸技能", "技能", "ab", "bab"]
    native_scanner = native_kernel.TermScanner(surfaces, prefer_native=True)
    py_scanner = native_kernel.TermScanner(surfaces, prefer_native=False)
    assert native_scanner.backend == "native"
    assert py_scanner.backend == "python"
    sample = "🌊使用声骸技能ababa✨"
    assert sorted(native_scanner.find_all(sample)) == sorted(py_scanner.find_all(sample))
    assert native_scanner.scan_and_select(sample) == py_scanner.scan_and_select(sample)
