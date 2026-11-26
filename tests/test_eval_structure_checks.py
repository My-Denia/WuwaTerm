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


def _write_textmap(root, rows):
    for language, content in rows.items():
        path = root / language / "multi_text" / "MultiText.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")


def test_corpus_mode_counts_new_flags_against_a_baseline(ev, tmp_path):
    _write_textmap(tmp_path, {
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
    })
    pairs = ev.corpus_pairs(tmp_path)
    assert [key for key, *_ in pairs] == ["a", "b"]

    def never_flags(source, target):
        return type("Report", (), {"findings": ()})()

    summary, new = ev.evaluate_corpus(pairs, ev._engine(), never_flags)
    assert summary["pairs"] == 2
    assert summary["newly_flagged"] == {"zh-en": 1, "en-zh": 1}
    assert [(direction, key) for direction, key, *_ in new] == [("zh-en", "a"), ("en-zh", "a")]


def test_attribution_fixture_meets_the_frozen_gates(ev):
    document = json.loads(ev.ATTRIBUTION_FIXTURE.read_text(encoding="utf-8"))
    report = ev.evaluate_attribution(document["cases"], ev.attribution_candidate)
    assert report["swapped_runs"] >= 20
    assert report["swapped_recall"] >= 0.8
    assert report["misses"] == []
    assert report["negative_runs"] >= 30
    assert report["false_positives"] == {}
    assert report["negative_candidates"] == 0
    # Every case ran as written, in the direction it is written.
    assert sum(bucket["runs"] for bucket in report["buckets"].values()) == report["cases"]
    assert {case["direction"] for case in document["cases"]} == {"zh-en", "en-zh"}


def test_attribution_hint_names_the_slots_that_moved(ev):
    hint = ev.attribution_candidate("攻击力提升20%，暴击率提升10%。", "ATK +10%, CRIT Rate +20%.")
    assert hint == {
        "version": ev.ATTRIBUTION_VERSION,
        "shared_slots": 2,
        "slots": [
            {"slot": "atk", "source": ["20"], "target": ["10"]},
            {"slot": "crit_rate", "source": ["10"], "target": ["20"]},
        ],
    }
    # The same facts in another order are the same facts, whichever way round.
    assert ev.attribution_candidate("造成20%伤害，冷却8秒。", "Cooldown: 8s. Deals 20% DMG.") is None
    assert ev.attribution_candidate("Cooldown: 8s. Deals 20% DMG.", "造成20%伤害，冷却8秒。") is None


def test_attribution_reads_neither_markup_nor_templates_nor_builds(ev):
    hint = ev.attribution_candidate(
        "在{Cus:Sap,S=stack P=stacks SapTag=0}任务中，<color=Red>攻击力提升20%</color>，"
        "暴击率提升10%，S3R1时再解锁。",
        "During the <color=Red>ATK +10%</color> task, CRIT Rate +20% at S3R1.",
    )
    assert hint is not None
    assert [(slot["slot"], slot["source"], slot["target"]) for slot in hint["slots"]] == [
        ("atk", ["20"], ["10"]),
        ("crit_rate", ["10"], ["20"]),
    ]


def test_attribution_stays_under_a_hundred_milliseconds(ev):
    measured = ev.timing(ev.attribution_candidate, 2)
    assert measured["slowest_ms"] < 100, measured["slowest"]


def test_attribution_corpus_reports_counts_only(ev, tmp_path):
    _write_textmap(tmp_path, {
        "zh-Hans": [
            {"Id": "a", "Content": "攻击力提升20%，暴击率提升10%。"},
            {"Id": "b", "Content": "攻击力提升20%，暴击率提升20%。"},
        ],
        "en": [
            {"Id": "a", "Content": "ATK +10%, CRIT Rate +20%."},
            {"Id": "b", "Content": "ATK +20%, CRIT Rate +20%."},
        ],
    })
    summary = ev.evaluate_attribution_corpus(ev.corpus_pairs(tmp_path), ev.attribution_candidate)
    assert summary == {
        "pairs": 2,
        "runs": 4,
        "candidates": {"zh-en": 1, "en-zh": 1},
        "candidate_rate": 0.5,
        "also_structure_finding": {},
    }


def test_attribution_mode_prints_json_and_names_no_text(ev, capsys):
    assert ev.main(["attribution"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"attribution"}
    attribution = report["attribution"]
    assert attribution["swapped_recall"] >= 0.8
    assert attribution["negative_candidates"] == 0
    assert attribution["timing"]["slowest_ms"] < 100
    assert "corpus" not in attribution


def test_the_existing_modes_keep_their_own_output(ev, capsys):
    assert ev.main(["timing", "--repeats", "1"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"timing"}
    assert ev.main(["fixture"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"fixture"}
    assert ev.main([]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"fixture", "timing"}
    with pytest.raises(SystemExit) as raised:
        ev.main(["corpus"])
    assert raised.value.code == 2
