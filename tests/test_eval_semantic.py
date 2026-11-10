"""scripts/eval_semantic.py on a synthetic run directory (no corpus text, no network)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location("eval_semantic", ROOT / "scripts" / "eval_semantic.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["eval_semantic"] = module  # dataclasses resolve their module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("eval_semantic", None)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


ZH = "她没有离开。\n\n今天有三个人来了。"
EN = "She did not leave.\n\nThree people came today."


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def run_dirs(tmp_path, sample_db):
    run = tmp_path / "run"
    corpus = tmp_path / "corpus"
    _write(corpus / "B1-guide" / "zh.txt", "原始版本，含未脱敏内容。")
    _write(corpus / "B2-story" / "zh.txt", "他说：今天不去了。\n\n椿来了。")
    _write(run / "work" / "B1-zh.sanitized.txt", "这是脱敏副本。椿说她没有来。")
    shutil.copyfile(sample_db, run / "work" / "terms-3.7.snapshot.db")
    _write(run / "annotation-guidelines.md", "synthetic")
    _write(
        run / "p-layer-alignment.json",
        {
            "layer": "P",
            "zh_char_denominator": 1000,
            "units": [{"unit_id": "S1-u1", "doc": "S1", "zh_text": ZH, "en_text": EN}],
        },
    )
    _write(
        run / "p-diff-freeze.json",
        {
            "items": [
                {"id": "D1", "unit_id": "S1-u1", "class": "allowed_rewrite",
                 "zh": {"unit_range": [0, 6]}, "en": {"unit_range": [0, 18]}},
                {"id": "D2", "unit_id": "S1-u1", "class": "real_error",
                 "zh": {"unit_range": [8, 18]}, "en": None},
            ]
        },
    )
    _write(
        run / "e-injections.json",
        {"injections": [{"id": "N01", "category": "negation_change", "doc": "S1"}]},
    )
    _write(
        run / "e-variants.json",
        {
            "variants": [
                {
                    "variant_id": "S1-u1-v1",
                    "unit_id": "S1-u1",
                    "source_text": ZH,
                    "target_text": "She left.\n\nThree people came today.",
                    "injections": [
                        {"injection_id": "N01", "category": "negation_change",
                         "source_range": [0, 6], "target_injected_range": [0, 9]}
                    ],
                    "p_items_in_variant_coords": [],
                }
            ]
        },
    )
    _write(
        run / "m-gold.json",
        {
            "docs": {
                "B1": "/elsewhere/run/work/B1-zh.sanitized.txt",
                "B2": "/elsewhere/corpus/B2-story/zh.txt",
            },
            "items": [
                {"id": "M1", "doc": "B2", "kind": "negation", "gold": "neg"},
                {"id": "M2", "doc": "B2", "kind": "term_sense", "gold": "official"},
            ],
        },
    )
    _write(
        run / "mprime-gold.json",
        {
            "experiments": [
                {
                    "experiment": "X1",
                    "doc": "B2",
                    "supplementary": False,
                    "precedent": {"surface": "椿", "choice": "official_pair", "rendering": "Camellya",
                                  "snippet": "椿来了。", "anchor": {"segment_index": 0, "source_start": 0, "source_end": 1},
                                  "scope": "occurrence"},
                    "targets": [{"para": 2, "segment_index": 1, "text": "椿来了。"}],
                }
            ],
            "occurrences": [
                {"id": "X1-P2-0", "experiment": "X1", "target_para": 2, "relation": "same", "supplementary": False}
            ],
        },
    )
    _write(run / "sample-stats.json", {})
    _write(run / "owner-spotcheck-candidates.json", [])
    frozen = [
        "annotation-guidelines.md", "p-layer-alignment.json", "p-diff-freeze.json",
        "e-injections.json", "e-variants.json", "m-gold.json", "mprime-gold.json",
        "sample-stats.json", "owner-spotcheck-candidates.json",
        "work/B1-zh.sanitized.txt", "work/terms-3.7.snapshot.db",
    ]
    ledger = {
        "model_calls_total": 0,
        "records": [],
        "wave0_freeze": {
            "files": {rel: {"sha256": _sha(run / rel), "bytes": (run / rel).stat().st_size} for rel in frozen},
            "corpus_inputs": {
                "B1-guide/zh.txt": _sha(corpus / "B1-guide" / "zh.txt"),
                "B2-story/zh.txt": _sha(corpus / "B2-story" / "zh.txt"),
            },
        },
    }
    _write(run / "ledger.json", ledger)
    return run, corpus


def test_verify_passes_then_catches_a_changed_file(ev, run_dirs):
    run, corpus = run_dirs
    assert ev.main(["verify", "--run-dir", str(run), "--corpus-dir", str(corpus)]) == 0
    (run / "m-gold.json").write_text("{}", encoding="utf-8")
    assert ev.main(["verify", "--run-dir", str(run), "--corpus-dir", str(corpus)]) == 1


def test_changed_corpus_input_refuses_a_run_before_any_call(ev, run_dirs, tmp_path, capsys):
    run, corpus = run_dirs
    (corpus / "B2-story" / "zh.txt").write_text("changed", encoding="utf-8")
    out = tmp_path / "dry"
    code = ev.main(["run", "--run-dir", str(run), "--corpus-dir", str(corpus), "--fake-transport", "--out-dir", str(out)])
    assert code == 1
    assert not (out / "ledger.dry-run.json").exists()


def test_dry_run_counts_every_attempt_in_a_copy_and_never_touches_the_real_ledger(ev, run_dirs, tmp_path):
    run, corpus = run_dirs
    before = _sha(run / "ledger.json")
    out = tmp_path / "dry"
    code = ev.main(["run", "--run-dir", str(run), "--corpus-dir", str(corpus), "--fake-transport", "--out-dir", str(out)])
    assert code == 0
    assert _sha(run / "ledger.json") == before
    dry = json.loads((out / "ledger.dry-run.json").read_text(encoding="utf-8"))
    calls = json.loads((out / "calls.json").read_text(encoding="utf-8"))
    # P: 1 unit x 2 directions; E: 1 variant; M: per doc old + new + review
    # per new segment; M': 1 target x 2 conditions.
    assert dry["model_calls_total"] == len(calls) == dry["records"][-1]["calls"]
    assert dry["records"][-1]["status"] == "complete"
    assert {c["purpose"] for c in calls} == {"translation", "semantic_review"}
    m_b1 = json.loads((out / "M" / "B1.json").read_text(encoding="utf-8"))
    assert "脱敏副本" in m_b1["prepared"]  # read from the sanitized copy only

    assert ev.main(["score", "--run-dir", str(run), "--corpus-dir", str(corpus), "--out-dir", str(out), "--dry-run"]) == 0
    receipt = json.loads((out / "receipt.dry-run.json").read_text(encoding="utf-8"))
    gates = receipt["rounds"]["1"]["gates"]
    assert not (run / "receipt.json").exists()
    assert gates["G_regression"]["state"] == "undecidable"
    assert gates["P_real_error_recall"]["state"] == "undecidable"  # 1 item < 10
    assert gates["separator_fidelity"]["state"] == "met"
    assert gates["bytes"]["max_bytes"] <= 57_344
    assert (out / "worklists" / "m-translation-packet.json").is_file()


def test_eval_stops_at_its_self_limit_before_sending(ev, run_dirs, tmp_path):
    run, corpus = run_dirs
    ledger = json.loads((run / "ledger.json").read_text(encoding="utf-8"))
    ledger["records"].append({"purpose": "eval", "calls": 419})
    ledger["model_calls_total"] = 419
    (run / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    out = tmp_path / "dry"
    code = ev.main(["run", "--run-dir", str(run), "--corpus-dir", str(corpus), "--layer", "P", "--fake-transport", "--out-dir", str(out)])
    assert code == 1
    dry = json.loads((out / "ledger.dry-run.json").read_text(encoding="utf-8"))
    assert dry["model_calls_total"] == 420
    assert dry["records"][-1]["calls"] == 1
    assert dry["records"][-1]["status"].startswith("stopped")
    calls = json.loads((out / "calls.json").read_text(encoding="utf-8"))
    assert len(calls) == 1  # the refused attempt was never sent


def test_reservations_respect_the_stop_line_and_settle_to_actual(ev, run_dirs, capsys):
    run, _ = run_dirs
    ledger_args = ["ledger", "--run-dir", str(run)]
    assert ev.main([*ledger_args, "reserve", "--purpose", "w2", "--max-calls", "451"]) == 1
    assert ev.main([*ledger_args, "reserve", "--purpose", "w2", "--max-calls", "12"]) == 0
    rid = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["reservation_id"]
    data = json.loads((run / "ledger.json").read_text(encoding="utf-8"))
    assert data["model_calls_total"] == 12
    assert ev.main([*ledger_args, "settle", "--reservation", rid, "--actual", "7"]) == 0
    data = json.loads((run / "ledger.json").read_text(encoding="utf-8"))
    assert data["model_calls_total"] == 7
    assert ev.main([*ledger_args, "settle", "--reservation", rid, "--actual", "7"]) == 1


def test_overlap_rules_and_class_priority(ev):
    assert ev.overlaps({"start": 2, "end": 5}, [4, 9])
    assert not ev.overlaps({"start": 2, "end": 4}, [4, 9])
    assert ev.overlaps({"start": 2, "end": 4}, [4, 4])  # zero-width injection point
    assert not ev.overlaps(None, [0, 1])
    items = [
        {"id": "a", "class": "allowed_rewrite", "source": [0, 10], "target": None},
        {"id": "u", "class": "undecidable", "source": [5, 12], "target": None},
    ]
    cls, ids, multi = ev.classify_issue(
        {"source_span": {"start": 6, "end": 8}, "target_span": None}, items,
        source_key="source", target_key="target",
    )
    assert cls == "undecidable" and multi and set(ids) == {"a", "u"}


def test_env_file_sets_only_gateway_keys_and_prints_nothing(ev, tmp_path, monkeypatch, capsys):
    path = tmp_path / "creds.env"
    path.write_text("WUWATERM_OPENAI_API_KEY='sk-secret-value'\nexport WUWATERM_OPENAI_MODEL=m\nHOME=/x\n", encoding="utf-8")
    # setenv first so monkeypatch restores both keys after the test.
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "")
    home = __import__("os").environ.get("HOME")
    assert ev.load_env_file(path) == 2
    assert __import__("os").environ["WUWATERM_OPENAI_API_KEY"] == "sk-secret-value"
    assert __import__("os").environ.get("HOME") == home
    captured = capsys.readouterr()
    assert "sk-secret" not in captured.out + captured.err


def test_a_gateway_error_stops_the_run_after_one_attempt(ev, run_dirs, tmp_path, monkeypatch):
    run, corpus = run_dirs
    monkeypatch.setattr(ev, "fake_gateway", lambda request: ev.httpx.Response(400, json={"error": "bad"}))
    out = tmp_path / "dry"
    code = ev.main(["run", "--run-dir", str(run), "--corpus-dir", str(corpus), "--fake-transport", "--out-dir", str(out)])
    assert code == 1
    dry = json.loads((out / "ledger.dry-run.json").read_text(encoding="utf-8"))
    assert dry["model_calls_total"] == 1
    assert "gateway error" in dry["records"][-1]["status"]


def test_max_calls_caps_a_smoke_run(ev, run_dirs, tmp_path):
    run, corpus = run_dirs
    out = tmp_path / "dry"
    code = ev.main([
        "run", "--run-dir", str(run), "--corpus-dir", str(corpus), "--layer", "P",
        "--max-calls", "1", "--fake-transport", "--out-dir", str(out),
    ])
    assert code == 0
    dry = json.loads((out / "ledger.dry-run.json").read_text(encoding="utf-8"))
    assert dry["model_calls_total"] == 1
    assert dry["records"][-1]["status"] == "capped by --max-calls"
    assert (out / "P" / "S1-u1.zh->en.json").is_file()


def test_formal_round_needs_the_frozen_config_and_refuses_a_changed_one(ev, run_dirs, tmp_path, monkeypatch):
    run, corpus = run_dirs
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://gateway.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "never-sent")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "m")
    monkeypatch.setenv("WUWATERM_SEMANTIC_REVIEW_THINKING", "low")
    args = ["run", "--run-dir", str(run), "--corpus-dir", str(corpus), "--layer", "P", "--round", "1",
            "--out-dir", str(tmp_path / "r1")]
    assert ev.main(args) == 1  # no freeze yet: refused before any call
    assert json.loads((run / "ledger.json").read_text(encoding="utf-8"))["model_calls_total"] == 0

    assert ev.main(["freeze-config", "--run-dir", str(run), "--git-commit", "abc"]) == 0
    frozen = json.loads((run / "ledger.json").read_text(encoding="utf-8"))["config_freeze"]
    assert frozen["options"]["thinking_mode"] == "low"
    assert "never-sent" not in json.dumps(frozen)
    assert ev.main(["freeze-config", "--run-dir", str(run), "--git-commit", "abc"]) == 1  # no silent overwrite

    monkeypatch.setenv("WUWATERM_SEMANTIC_REVIEW_THINKING", "disabled")
    assert ev.main(args) == 1  # options changed after the freeze
    ledger = json.loads((run / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["model_calls_total"] == 0 and ledger["records"] == []
