"""scripts/eval_structure_checks.py on the committed fixture and a synthetic textmap."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location(
        "eval_structure_checks", ROOT / "scripts" / "eval_structure_checks.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["eval_structure_checks"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("eval_structure_checks", None)


def test_benchmark_fixture_has_no_false_alarm_and_no_miss(ev):
    document = json.loads(ev.FIXTURE.read_text(encoding="utf-8"))
    report = ev.evaluate_fixture(
        document["cases"],
        ev._engine(),
        document["negative_buckets"],
        document["out_of_scope_buckets"],
    )
    assert report["false_positives"] == []
    assert report["misses"] == []
    # Both directions of every case ran, and the attribution bucket stayed apart.
    assert report["negative_runs"] + report["positive_runs"] + report["out_of_scope_runs"] == (
        2 * report["cases"]
    )


def test_corpus_mode_counts_new_flags_against_a_baseline(ev, tmp_path):
    rows = {
        "zh-Hans": [
            {"Id": "a", "Content": "攻击力提升50%"},
            {"Id": "b", "Content": "持续8秒"},
            {"Id": "c", "Content": "没有数字"},
        ],
        "en": [
            {"Id": "a", "Content": "ATK +50"},
            {"Id": "b", "Content": "Lasts 8s"},
            {"Id": "c", "Content": "No digits"},
        ],
    }
    for language, content in rows.items():
        path = tmp_path / language / "multi_text" / "MultiText.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    pairs = ev.corpus_pairs(tmp_path)
    assert [key for key, *_ in pairs] == ["a", "b"]

    def never_flags(source, target):
        return type("Report", (), {"findings": ()})()

    summary, new = ev.evaluate_corpus(pairs, ev._engine(), never_flags)
    assert summary["pairs"] == 2
    assert summary["newly_flagged"] == {"zh-en": 1, "en-zh": 1}
    assert [(direction, key) for direction, key, *_ in new] == [("zh-en", "a"), ("en-zh", "a")]
