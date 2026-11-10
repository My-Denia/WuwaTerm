"""scripts/eval_semantic.py on a synthetic run directory (no corpus text, no network)."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import httpx
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
    assert ev.main(["freeze-config", "--run-dir", str(run), "--git-commit", "def", "--replace"]) == 0
    replaced = json.loads((run / "ledger.json").read_text(encoding="utf-8"))
    assert replaced["config_freeze"]["git_commit"] == "def"
    assert replaced["config_freeze_history"][0]["git_commit"] == "abc"

    monkeypatch.setenv("WUWATERM_SEMANTIC_REVIEW_THINKING", "disabled")
    assert ev.main(args) == 1  # options changed after the freeze
    ledger = json.loads((run / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["model_calls_total"] == 0 and ledger["records"] == []


def test_a_timeout_is_recorded_and_resume_reruns_only_unfinished_items(ev, run_dirs, tmp_path, monkeypatch):
    run, corpus = run_dirs
    real = ev.fake_gateway
    state = {"n": 0}

    def flaky(request):
        state["n"] += 1
        if state["n"] == 1:
            raise ev.httpx.ReadTimeout("slow", request=request)
        return real(request)

    monkeypatch.setattr(ev, "fake_gateway", flaky)
    out = tmp_path / "dry"
    base = ["run", "--run-dir", str(run), "--corpus-dir", str(corpus), "--layer", "P",
            "--fake-transport", "--out-dir", str(out)]
    assert ev.main([*base, "--llm-timeout", "90"]) == 0  # a timeout does not stop the run
    first = json.loads((out / "P" / "S1-u1.zh->en.json").read_text(encoding="utf-8"))
    assert first["timeout"] is True and first["error_code"] == "llm_unavailable"
    meta = json.loads((out / "run-meta.json").read_text(encoding="utf-8"))
    assert meta["runs"][-1]["translation_timeout_s"] == 90.0
    assert meta["runs"][-1]["semantic_review_timeout_s"] == 75.0

    # The second run copies the real ledger again; only the failed item is redone.
    assert ev.main([*base, "--resume"]) == 0
    dry = json.loads((out / "ledger.dry-run.json").read_text(encoding="utf-8"))
    assert dry["records"][-1]["calls"] == 1
    again = json.loads((out / "P" / "S1-u1.zh->en.json").read_text(encoding="utf-8"))
    assert again["error_code"] is None

    assert ev.main(["score", "--run-dir", str(run), "--corpus-dir", str(corpus), "--out-dir", str(out), "--dry-run"]) == 0


# --------------------------------------------------------------------------
# Interrupt -> resume -> rescore -> receipt (review findings and their kin)
# --------------------------------------------------------------------------


def _inputs(ev, tmp_path, units=("U1", "U2")):
    alignment = {
        "zh_char_denominator": 1000,
        "units": [{"unit_id": u, "zh_text": "甲乙丙丁戊。", "en_text": "Alpha beta gamma."} for u in units],
    }
    freeze = {"items": [
        {"id": f"D-{u}", "unit_id": u, "class": "allowed_rewrite",
         "zh": {"unit_range": [0, 2]}, "en": {"unit_range": [0, 5]}} for u in units
    ]}
    return ev.Inputs(run_dir=tmp_path, corpus_dir=tmp_path, alignment=alignment, freeze=freeze,
                     injections={"injections": []}, variants={"variants": []},
                     m_gold={"docs": {}, "items": []}, mprime={"experiments": [], "occurrences": []})


def _p_result(unit, direction, *, status="complete", issues=(), error=None, timeout=False):
    body = None if error else {"status": status, "dropped_unanchored": 0, "omitted_issues": 0, "issues": list(issues)}
    out = {"layer": "P", "unit_id": unit, "direction": direction, "latency_s": 1.0,
           "error_code": error, "bytes": 100, "body": body}
    if timeout:
        out["timeout"] = True
    return out


def _fp_issue(n=0):
    return {"id": f"{n:032x}", "category": "meaning_shift", "confidence": "high",
            "source_span": {"start": 0, "end": 2, "text": "甲乙"}, "target_span": None, "rationale": "r"}


def _write_p(ev, out, results):
    for r in results:
        ev.write_json_atomic(out / "P" / f"{r['unit_id']}.{r['direction']}.{id(r)}.json", r)


@pytest.mark.parametrize("case", ["one_unit", "all_not_reviewed", "all_timeouts"])
def test_p_gate_is_undecidable_without_full_coverage(ev, tmp_path, case):
    inputs, out = _inputs(ev, tmp_path), tmp_path / "round"
    results = []
    for d in ("zh->en", "en->zh"):
        if case == "one_unit":
            results.append(_p_result("U1", d))
        elif case == "all_not_reviewed":
            results += [_p_result(u, d, status="not_reviewed") for u in ("U1", "U2")]
        else:
            results += [_p_result(u, d, error="llm_unavailable", timeout=True) for u in ("U1", "U2")]
    _write_p(ev, out, results)
    gates = ev.score_p(inputs, out, ev.ReviewStats())["gates"]
    for d in ("zh->en", "en->zh"):
        assert gates[f"P_false_positives_{d}"]["state"] == "undecidable"
        assert gates[f"P_false_positives_{d}"]["units_missing"]


def test_p_gate_is_not_met_on_partial_coverage_once_the_limit_is_passed(ev, tmp_path):
    inputs, out = _inputs(ev, tmp_path), tmp_path / "round"  # limit 1.0 per direction
    _write_p(ev, out, [_p_result("U1", d, issues=[_fp_issue(1), _fp_issue(2)]) for d in ("zh->en", "en->zh")])
    gates = ev.score_p(inputs, out, ev.ReviewStats())["gates"]
    assert gates["P_false_positives_zh->en"]["state"] == "not_met"


def test_p_gate_is_met_only_with_every_unit_reviewed(ev, tmp_path):
    inputs, out = _inputs(ev, tmp_path), tmp_path / "round"
    _write_p(ev, out, [_p_result(u, d) for u in ("U1", "U2") for d in ("zh->en", "en->zh")])
    gates = ev.score_p(inputs, out, ev.ReviewStats())["gates"]
    assert gates["P_false_positives_zh->en"]["state"] == "met"


def test_blind_key_keeps_old_assignments_and_adds_new_documents(ev, tmp_path):
    path = tmp_path / "m-blind-key.json"
    first = ev.ensure_blind_key(path, ["A1"])
    second = ev.ensure_blind_key(path, ["A1", "B2"])
    assert second["A1"] == first["A1"]
    assert set(second) == {"A1", "B2"}
    assert json.loads(path.read_text(encoding="utf-8")) == second  # persisted


def test_ledger_writes_never_drop_a_concurrent_change(ev, run_dirs):
    run, _ = run_dirs
    a = ev.Ledger(run / "ledger.json")
    rid = a.open_record(purpose="eval", round=9, status="running")
    b = ev.Ledger(run / "ledger.json")  # another process, or an operator
    b.update(rid, status="corrected by hand")
    a.charge(rid)  # the first holder's stale copy must not undo the correction
    record = ev.Ledger(run / "ledger.json").record(rid)
    assert record["status"] == "corrected by hand"
    assert record["calls"] == 1


def test_scoring_refuses_while_a_run_is_still_writing(ev, run_dirs, tmp_path):
    run, corpus = run_dirs
    out = tmp_path / "dry"
    ev.acquire_run_lock(out if out.mkdir(parents=True) is None else out)  # this test's live pid
    try:
        code = ev.main(["score", "--run-dir", str(run), "--corpus-dir", str(corpus), "--out-dir", str(out), "--dry-run"])
        assert code == 1
    finally:
        ev.release_run_lock(out)


def test_rescoring_keeps_other_rounds_and_top_level_records(ev, run_dirs, tmp_path):
    run, corpus = run_dirs
    out = run / "rounds" / "round-3"
    out.mkdir(parents=True)
    (run / "receipt.json").write_text(
        json.dumps({"run": "x", "owner_session_1": {"kept": True}, "rounds": {"1": {"headline": "old"}}}),
        encoding="utf-8",
    )
    assert ev.main(["score", "--run-dir", str(run), "--corpus-dir", str(corpus), "--round", "3"]) == 0
    receipt = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["owner_session_1"] == {"kept": True}
    assert receipt["rounds"]["1"] == {"headline": "old"}
    assert "3" in receipt["rounds"] and receipt["rounds"]["3"]["ledger_records"] == []


def test_an_interrupted_run_is_recorded_and_releases_its_lock(ev, run_dirs, tmp_path, monkeypatch):
    run, corpus = run_dirs

    def interrupt(request):
        raise KeyboardInterrupt

    monkeypatch.setattr(ev, "fake_gateway", interrupt)
    out = tmp_path / "dry"
    code = ev.main(["run", "--run-dir", str(run), "--corpus-dir", str(corpus), "--layer", "P",
                    "--fake-transport", "--out-dir", str(out)])
    assert code == 1
    dry = json.loads((out / "ledger.dry-run.json").read_text(encoding="utf-8"))
    assert dry["records"][-1]["status"] == "interrupted"
    assert not (out / "run.lock").exists()


def test_e_recall_with_unreviewed_variants_is_decided_only_by_its_bounds(ev, tmp_path):
    inputs = _inputs(ev, tmp_path)
    inj = [{"id": f"N{i:02d}", "category": "negation_change", "doc": f"G{i % 3}"} for i in range(10)]
    inputs.injections = {"injections": inj}
    inputs.variants = {"variants": [
        {"variant_id": f"V{i}", "source_text": "甲乙", "target_text": "ab",
         "injections": [{"injection_id": f"N{i:02d}", "category": "negation_change",
                         "source_range": [0, 1], "target_injected_range": [0, 1]}]}
        for i in range(10)
    ]}
    out = tmp_path / "round"
    hit = {"id": "0" * 32, "category": "negation_change", "confidence": "high",
           "source_span": {"start": 0, "end": 1, "text": "甲"}, "target_span": None, "rationale": "r"}

    def write(n_reviewed, with_hit):
        for i in range(n_reviewed):
            ev.write_json_atomic(out / "E" / f"V{i}.json", {
                "layer": "E", "variant_id": f"V{i}", "latency_s": 1.0, "error_code": None, "bytes": 10,
                "body": {"status": "complete", "dropped_unanchored": 0, "omitted_issues": 0,
                         "issues": [hit] if with_hit else []}})

    write(7, True)  # 7 of 10 found, 3 unreviewed: 70%..100% straddles 80%
    gate = ev.score_e(inputs, out, ev.ReviewStats())["gates"]["E_recall_negation_change"]
    assert gate["state"] == "undecidable" and len(gate["injections_unreviewed"]) == 3
    write(8, True)  # 8 found: met even if the other 2 were missed
    assert ev.score_e(inputs, out, ev.ReviewStats())["gates"]["E_recall_negation_change"]["state"] == "met"


def test_resume_retries_nested_failures(ev):
    ok = {"kind": "llm", "error_code": None, "text": "t"}
    timed_out = {"kind": "error", "error_code": "llm_unavailable", "text": None, "timeout": True}
    assert ev.result_complete({"layer": "Mprime", "with_precedent": ok, "without_precedent": ok})
    assert not ev.result_complete({"layer": "Mprime", "with_precedent": timed_out, "without_precedent": ok})
    assert not ev.result_complete({"layer": "Mprime", "with_precedent": ok, "without_precedent": timed_out})
    review_ok = {"error_code": None, "body": {}}
    assert ev.result_complete({"layer": "M", "reviews": [review_ok, {"skipped": True}]})
    assert not ev.result_complete({"layer": "M", "reviews": [review_ok, {"error_code": "llm_unavailable", "timeout": True}]})
    assert not ev.result_complete({"layer": "P", "error_code": "llm_unavailable"})
    assert ev.result_complete({"layer": "P", "error_code": None})


def test_fingerprint_v2_sees_a_translation_model_change_v1_frozen_hashes_still_check(ev, run_dirs, tmp_path, monkeypatch):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://gateway.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "never-sent")
    monkeypatch.setenv("WUWATERM_SEMANTIC_REVIEW_MODEL", "review-model")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "translation-a")
    v1_a, v2_a = ev.config_fingerprint(1), ev.config_fingerprint(2)
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "translation-b")
    v1_b, v2_b = ev.config_fingerprint(1), ev.config_fingerprint(2)
    assert v1_a["config_sha256"] == v1_b["config_sha256"]  # the v1 blind spot, kept for old freezes
    assert v2_a["config_sha256"] != v2_b["config_sha256"]
    assert v2_b["options"]["translation_model"] == "translation-b"

    run, corpus = run_dirs
    ledger = json.loads((run / "ledger.json").read_text(encoding="utf-8"))
    ledger["config_freeze"] = {k: v for k, v in v1_b.items() if k != "fingerprint_version"}  # an old-style freeze
    (run / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(ev, "build_runtime", lambda *a, **k: (_ for _ in ()).throw(ev.Refused("stop before any call")))
    code = ev.main(["run", "--run-dir", str(run), "--corpus-dir", str(corpus), "--layer", "P", "--round", "1",
                    "--out-dir", str(tmp_path / "r")])
    # It passed the freeze check (v1 compared with v1): a run record exists,
    # stopped at the stub, and the run lock was released.
    assert code == 1
    records = json.loads((run / "ledger.json").read_text(encoding="utf-8"))["records"]
    assert records[-1]["status"] == "stopped: stop before any call"
    assert records[-1]["calls"] == 0
    assert not (tmp_path / "r" / "run.lock").exists()

    # A translation model change after a v2 freeze is refused.
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "translation-a")
    ledger = json.loads((run / "ledger.json").read_text(encoding="utf-8"))
    ledger["config_freeze"] = v2_b
    (run / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    before = len(ledger["records"])
    assert ev.main(["run", "--run-dir", str(run), "--corpus-dir", str(corpus), "--layer", "P", "--round", "1",
                    "--out-dir", str(tmp_path / "r")]) == 1
    assert len(json.loads((run / "ledger.json").read_text(encoding="utf-8"))["records"]) == before


def _mprime_inputs(ev, tmp_path):
    inputs = _inputs(ev, tmp_path)
    targets = [{"para": i, "text": f"椿{i}"} for i in range(5)]
    inputs.mprime = {
        "experiments": [{"experiment": "X", "supplementary": False, "targets": targets,
                         "precedent": {"rendering": "Camellya"}}],
        "occurrences": (
            [{"id": f"S{i}", "experiment": "X", "target_para": i % 5, "relation": "same"} for i in range(10)]
            + [{"id": f"D{i}", "experiment": "X", "target_para": i, "relation": "diff"} for i in range(5)]
        ),
    }
    out = tmp_path / "round"
    for t in targets:
        text = {"text": "Camellya came.", "error_code": None}
        ev.write_json_atomic(out / "Mprime" / f"X-P{t['para']}.json", {
            "layer": "Mprime", "experiment": "X", "target_para": t["para"], "source": t["text"],
            "with_precedent": text, "without_precedent": text})
    return inputs, out


@pytest.mark.parametrize(
    "verdict",
    [{}, {"with_precedent": "correct"}, {"with_precedent": "correct", "without_precedent": "fine"}],
    ids=["empty", "one_side_missing", "illegal_value"],
)
def test_incomplete_mprime_verdicts_never_pass_the_gates(ev, tmp_path, verdict):
    inputs, out = _mprime_inputs(ev, tmp_path)
    ids = [o["id"] for o in inputs.mprime["occurrences"]]
    ev.write_json_atomic(out / "adjudication" / "mprime.json", {i: verdict for i in ids})
    result = ev.score_mprime(inputs, out)
    assert result["gates"]["Mprime_same_sense"]["state"] == "pending_adjudication"
    assert result["gates"]["Mprime_led_astray"]["state"] == "pending_adjudication"
    assert result["precedents_default"] == "off"
    assert len(json.loads((out / "worklists" / "mprime.json").read_text(encoding="utf-8"))) == len(ids)


def test_complete_mprime_verdicts_still_decide(ev, tmp_path):
    inputs, out = _mprime_inputs(ev, tmp_path)
    good = {"with_precedent": "correct", "without_precedent": "correct"}
    ev.write_json_atomic(out / "adjudication" / "mprime.json", {o["id"]: good for o in inputs.mprime["occurrences"]})
    result = ev.score_mprime(inputs, out)
    assert result["gates"]["Mprime_same_sense"]["state"] == "met"
    assert result["gates"]["Mprime_led_astray"]["state"] == "met"


def test_illegal_offlist_and_m_verdicts_stay_pending(ev, tmp_path):
    inputs, out = _inputs(ev, tmp_path, units=("U1",)), tmp_path / "round"
    off = {"id": "1" * 32, "category": "omission", "confidence": "high",
           "source_span": {"start": 3, "end": 5, "text": "丁戊"}, "target_span": None, "rationale": "r"}
    _write_p(ev, out, [_p_result("U1", d, issues=[off]) for d in ("zh->en", "en->zh")])
    keys = [f"P:U1:{d}:{'1' * 32}" for d in ("zh->en", "en->zh")]
    ev.write_json_atomic(out / "adjudication" / "p-offlist.json", {k: "looks fine" for k in keys})
    gates = ev.score_p(inputs, out, ev.ReviewStats())["gates"]
    # zh->en: the span is off-list on the Chinese side; an invalid verdict keeps it pending.
    assert gates["P_false_positives_zh->en"]["offlist_pending_mh"] == 1
    assert gates["P_false_positives_zh->en"]["offlist_fp_mh"] == 0
    worklist = json.loads((out / "worklists" / "p-offlist.json").read_text(encoding="utf-8"))
    assert [item["key"] for item in worklist] == [keys[0]]
    blind = {"B9": ["old", "new"]}
    item = {"id": "M1", "doc": "B9"}
    assert ev._verdict({"M1": {"A": "ok", "B": "wrong"}}, blind, item, "old") is None
    assert ev._verdict({"M1": {"A": "ok", "B": "wrong"}}, blind, item, "new") == "wrong"
    assert ev._verdict({"M1": {}}, blind, item, "new") is None


def test_direct_model_translation_bypasses_dictionary_and_segmentation(ev, sample_db, monkeypatch):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://test.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "deepseek-chat")
    transport = httpx.MockTransport(ev.fake_gateway)
    translator = ev.SentenceTranslator(
        sample_db,
        llm_timeout_seconds=45.0,
        llm_max_concurrency=1,
        llm_transport=transport,
    )
    try:
        # 椿 is an official character name in sample_db
        text = "今天天气很好，椿来到了今州城。"
        result = asyncio.run(
            ev.direct_model_translate(
                translator,
                text,
                to_chinese=False,
                case_id="case-1",
            )
        )
        assert result["status"] == "complete"
        assert result["error_code"] is None
        assert result["direction"] == "zh->en"
        assert result["to_chinese"] is False
        assert result["case_id"] == "case-1"
        assert result["model"] == "deepseek-chat"
        assert result["raw_response"] == result["final_output"]
        assert result["source_length"] == len(text)
        assert result["source_text_hash"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        # Credentials must not appear in request_params
        assert "api_key" not in result["request_params"]
        assert "base_url" not in result["request_params"]
        assert "token" not in result["request_params"]
        assert result["request_params"]["direction"] == "zh->en"
        assert result["request_params"]["instruction"] == ev.DIRECT_PROMPTS["zh->en"]
        assert "Locked terms:" not in result["request_params"]["instruction"]
        assert "Dictionary term references:" not in result["request_params"]["instruction"]
    finally:
        asyncio.run(translator.aclose())


def test_direct_model_translation_fails_on_unconfigured_gateway(ev, sample_db, monkeypatch):
    monkeypatch.delenv("WUWATERM_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    transport = httpx.MockTransport(ev.fake_gateway)
    translator = ev.SentenceTranslator(
        sample_db,
        llm_timeout_seconds=45.0,
        llm_max_concurrency=1,
        llm_transport=transport,
    )
    try:
        result = asyncio.run(
            ev.direct_model_translate(
                translator,
                "测试文本",
                to_chinese=False,
                case_id="unconf-case",
            )
        )
        assert result["status"] == "failed"
        assert result["error_code"] == "gateway_not_configured"
        assert result["error_stage"] == "configuration"
        assert result["final_output"] is None
    finally:
        asyncio.run(translator.aclose())


def _build_direct_runner(
    ev,
    run_dirs,
    out: Path,
    monkeypatch,
    *,
    model: str = "deepseek-chat",
    resume: bool = False,
    gateway_fn=None,
    timeout: float = 45.0,
    max_calls: int | None = None,
):
    run, corpus = run_dirs
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://test.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", model)
    monkeypatch.setenv("WUWATERM_SEMANTIC_REVIEW_ENABLED", "1")
    out.mkdir(parents=True, exist_ok=True)
    ledger_file = out / "ledger.dry-run.json"
    if not ledger_file.is_file():
        shutil.copyfile(run / "ledger.json", ledger_file)
    inputs = ev.load_inputs(run, corpus)
    ledger = ev.Ledger(ledger_file)
    rid = ledger.open_record(purpose="eval", round=1, max_calls=max_calls, status="running")
    payloads: list[dict] = []
    handler = gateway_fn or ev.fake_gateway

    def capturing(req: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(req.content.decode("utf-8")))
        return handler(req)

    translator = ev.SentenceTranslator(
        run / "work" / "terms-3.7.snapshot.db",
        llm_timeout_seconds=timeout,
        llm_max_concurrency=1,
        llm_transport=httpx.MockTransport(capturing),
    )
    service = ev.app_layer.build_term_service(run / "work" / "terms-3.7.snapshot.db")
    return ev.Runner(inputs, out, ledger, rid, translator, service, resume=resume), payloads


def _run_with_pre_send_hook(ev, runner, coro, *, hook_fn=None):
    remove = ev.llm_transport.add_pre_send_hook(
        hook_fn or (lambda purpose: runner.ledger.charge(runner.record_id))
    )
    try:
        return asyncio.run(coro)
    finally:
        remove()
        asyncio.run(runner.translator.aclose())


def _assert_manifest_counts(
    manifest: dict, *, total: int, completed: int, failed: int, not_started: int
) -> None:
    assert manifest["total_cases"] == total
    assert manifest["completed"] == completed
    assert manifest["failed"] == failed
    assert manifest["not_started"] == not_started
    assert completed + failed + not_started == total == len(manifest["cases"])
    for entry in manifest["cases"]:
        assert entry["status"] in ("complete", "failed", "not_started")
        if entry["status"] == "not_started":
            assert entry["error_code"] is None
            assert entry["latency_s"] is None


def test_direct_model_translation_gt_2000_chars_sent_verbatim_without_cap(ev, sample_db, monkeypatch):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://test.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "deepseek-chat")
    captured_requests = []

    def capturing_gateway(req: httpx.Request):
        captured_requests.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Translated long text successfully."}}]})

    transport = httpx.MockTransport(capturing_gateway)
    translator = ev.SentenceTranslator(
        sample_db,
        llm_timeout_seconds=45.0,
        llm_max_concurrency=1,
        llm_transport=transport,
    )
    try:
        # > 2000 characters (e.g. 2500 chars)
        long_text = "这是测试长文本。" * 280  # 2520 characters
        assert len(long_text) > 2000

        result = asyncio.run(
            ev.direct_model_translate(
                translator,
                long_text,
                to_chinese=False,
                case_id="long-case-1",
            )
        )
        assert result["status"] == "complete"
        assert result["error_code"] is None
        assert result["final_output"] == "Translated long text successfully."
        assert len(captured_requests) == 1
        req_payload = captured_requests[0]

        # Verbatim text unchanged: no chunking, no placeholders, no term modification
        assert req_payload["messages"][1]["content"] == long_text
        assert "Locked terms:" not in req_payload["messages"][0]["content"]

        # Default request matches B: no max_tokens sent in HTTP payload
        assert "max_tokens" not in req_payload
        assert result["request_params"]["max_tokens"] is None

        # When caller opts in with explicit max_tokens cap, it is sent and traced
        captured_requests.clear()
        res_capped = asyncio.run(
            ev.direct_model_translate(
                translator,
                "短文本",
                to_chinese=False,
                case_id="capped-case",
                max_tokens=2048,
            )
        )
        assert res_capped["status"] == "complete"
        assert len(captured_requests) == 1
        assert captured_requests[0]["max_tokens"] == 2048
        assert res_capped["request_params"]["max_tokens"] == 2048
    finally:
        asyncio.run(translator.aclose())


def test_direct_model_translation_supports_both_directions(ev, sample_db, monkeypatch):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://test.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "deepseek-chat")
    transport = httpx.MockTransport(ev.fake_gateway)
    translator = ev.SentenceTranslator(
        sample_db,
        llm_timeout_seconds=45.0,
        llm_max_concurrency=1,
        llm_transport=transport,
    )
    try:
        zh_text = "你好，世界！"
        res_en = asyncio.run(
            ev.direct_model_translate(translator, zh_text, to_chinese=False, case_id="dir-en")
        )
        assert res_en["direction"] == "zh->en"
        assert res_en["to_chinese"] is False
        assert res_en["request_params"]["instruction"] == ev.DIRECT_PROMPTS["zh->en"]
        assert "Chinese text into English" in res_en["request_params"]["instruction"]

        en_text = "Hello, world!"
        res_zh = asyncio.run(
            ev.direct_model_translate(translator, en_text, to_chinese=True, case_id="dir-zh")
        )
        assert res_zh["direction"] == "en->zh"
        assert res_zh["to_chinese"] is True
        assert res_zh["request_params"]["instruction"] == ev.DIRECT_PROMPTS["en->zh"]
        assert "English text into Chinese" in res_zh["request_params"]["instruction"]
    finally:
        asyncio.run(translator.aclose())


def test_direct_model_failures_not_skipped_on_resume_and_retry_budget_enforced(ev, run_dirs, tmp_path):
    # Verify result_complete behavior on various failure shapes
    good = {
        "layer": "direct_baseline",
        "status": "complete",
        "error_code": None,
        "final_output": "Translated text",
    }
    assert ev.result_complete(good) is True

    for bad in [
        {"layer": "direct_baseline", "status": "failed", "error_code": "timeout", "final_output": None},
        {"layer": "direct_baseline", "status": "failed", "error_code": "budget_exhausted", "final_output": None},
        {"layer": "direct_baseline", "status": "failed", "error_code": "empty_response", "final_output": None},
        {"layer": "direct_baseline", "status": "failed", "error_code": "input_too_long", "final_output": None},
        {"layer": "direct_baseline", "status": "complete", "error_code": None, "final_output": ""},
        {"layer": "direct_baseline", "status": "complete", "error_code": None, "final_output": None},
        {"layer": "direct_baseline", "status": "failed", "error_code": None, "final_output": "some text"},
    ]:
        assert ev.result_complete(bad) is False


def test_direct_model_empty_and_timeout_error_handling(ev, sample_db, monkeypatch):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://test.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "deepseek-chat")
    # Gateway returning empty response
    def empty_gateway(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})

    translator_empty = ev.SentenceTranslator(
        sample_db,
        llm_timeout_seconds=45.0,
        llm_max_concurrency=1,
        llm_transport=httpx.MockTransport(empty_gateway),
    )
    try:
        res_empty = asyncio.run(
            ev.direct_model_translate(translator_empty, "测试", to_chinese=False, case_id="empty-1")
        )
        assert res_empty["status"] == "failed"
        assert res_empty["error_code"] == "empty_response"
        assert res_empty["error_stage"] == "response_validation"
        assert res_empty["final_output"] is None
    finally:
        asyncio.run(translator_empty.aclose())

    # Gateway timing out
    def timeout_gateway(req):
        raise httpx.ReadTimeout("read timeout")

    translator_timeout = ev.SentenceTranslator(
        sample_db,
        llm_timeout_seconds=0.1,
        llm_max_concurrency=1,
        llm_transport=httpx.MockTransport(timeout_gateway),
    )
    try:
        res_timeout = asyncio.run(
            ev.direct_model_translate(translator_timeout, "测试", to_chinese=False, case_id="timeout-1")
        )
        assert res_timeout["status"] == "failed"
        assert res_timeout["error_code"] == "timeout"
        assert res_timeout["error_stage"] == "gateway_call"
        assert res_timeout["final_output"] is None
    finally:
        asyncio.run(translator_timeout.aclose())


def test_direct_baseline_pairing_by_case_id(ev, run_dirs, tmp_path):
    run, corpus = run_dirs
    out = tmp_path / "baseline_out"
    code = ev.main([
        "run",
        "--run-dir", str(run),
        "--corpus-dir", str(corpus),
        "--fake-transport",
        "--out-dir", str(out),
        "--layer", "M",
        "--direct-baseline",
        "--direct-baseline-scope", "m",
    ])
    assert code == 0

    manifest_path = out / "direct_baseline" / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["total_cases"] == len(manifest["cases"]) >= 1
    assert manifest["completed"] >= 1

    # Pair with M:B1
    m_b1 = json.loads((out / "M" / "B1.json").read_text(encoding="utf-8"))
    assert m_b1["case_id"] == "M:B1"
    assert m_b1["model"] == "dry-run"
    assert m_b1["request_params"]["model"] == "dry-run"
    paired = ev.pair_direct_baseline(out, "M:B1", "zh->en")
    assert paired is not None
    assert paired["case_id"] == "M:B1"
    assert paired["direction"] == "zh->en"
    assert paired["comparable"] is True
    assert paired["direct"]["status"] == "complete"
    assert paired["direct"]["text"] is not None
    assert paired["system"]["status"] == "complete"
    assert paired["system"]["text"] is not None


def test_direct_baseline_resume_retries_failed_cases(ev, run_dirs, tmp_path):
    run, corpus = run_dirs
    out = tmp_path / "resume_direct"
    # First run standalone direct_baseline
    code = ev.main([
        "run",
        "--run-dir", str(run),
        "--corpus-dir", str(corpus),
        "--fake-transport",
        "--out-dir", str(out),
        "--layer", "direct_baseline",
        "--direct-baseline-scope", "m",
    ])
    assert code == 0

    direct_b1 = out / "direct_baseline" / "M_B1.zh->en.json"
    assert direct_b1.is_file()
    data = json.loads(direct_b1.read_text(encoding="utf-8"))
    assert data["status"] == "complete"

    # Corrupt B1 to look like a timeout failure
    failed_data = dict(data)
    failed_data.update(status="failed", error_code="timeout", final_output=None)
    ev.write_json_atomic(direct_b1, failed_data)

    # Now verify done() sees it as unfinished
    inputs = ev.load_inputs(run, corpus)
    ledger = ev.Ledger(out / "ledger.dry-run.json")
    rid = ledger.open_record(purpose="eval", round=1, status="running")
    translator, service, _ = ev.build_runtime(inputs, out, fake=True)
    runner = ev.Runner(inputs, out, ledger, rid, translator, service, resume=True)

    assert runner.done("direct_baseline/M_B1.zh->en.json") is False
    assert runner.done("direct_baseline/M_B2.zh->en.json") is True

    # Re-running with --resume should retry B1 and restore it to complete
    calls_before = len(json.loads((out / "calls.json").read_text(encoding="utf-8")))
    code = ev.main([
        "run",
        "--run-dir", str(run),
        "--corpus-dir", str(corpus),
        "--fake-transport",
        "--out-dir", str(out),
        "--layer", "direct_baseline",
        "--direct-baseline-scope", "m",
        "--resume",
    ])
    assert code == 0

    re_data = json.loads(direct_b1.read_text(encoding="utf-8"))
    assert re_data["status"] == "complete"
    assert re_data["error_code"] is None
    assert re_data["final_output"] is not None

    calls_after = len(json.loads((out / "calls.json").read_text(encoding="utf-8")))
    # Exactly one call was retried for B1
    assert calls_after == calls_before + 1


def test_direct_baseline_standalone_layer_fake_transport(ev, run_dirs, tmp_path):
    run, corpus = run_dirs
    out = tmp_path / "standalone_direct"
    code = ev.main([
        "run",
        "--run-dir", str(run),
        "--corpus-dir", str(corpus),
        "--fake-transport",
        "--out-dir", str(out),
        "--layer", "direct_baseline",
        "--direct-baseline-scope", "m",
    ])
    assert code == 0

    # Ensure P, E, M directories were NOT created
    assert not (out / "P").exists()
    assert not (out / "E").exists()
    assert not (out / "M").exists()
    assert (out / "direct_baseline").is_dir()
    assert (out / "direct_baseline" / "manifest.json").is_file()


def test_direct_baseline_four_cases_pre_send_cap_and_direct_refused_hook(ev, run_dirs, tmp_path, monkeypatch):
    cases = [
        ev.DirectBaselineCase(case_id=f"case-{i}", source_text=f"source text {i}")
        for i in range(4)
    ]
    # 1. Real Runner + Ledger.charge + add_pre_send_hook with max_calls=1 across 4 cases
    out_cap = tmp_path / "pre_send_cap1"
    runner_cap, cap_payloads = _build_direct_runner(
        ev, run_dirs, out_cap, monkeypatch, model="deepseek-chat", max_calls=1
    )
    with pytest.raises(ev.Refused, match="budget stop"):
        _run_with_pre_send_hook(ev, runner_cap, runner_cap.run_direct_baseline(cases))

    assert len(cap_payloads) == 1
    rec_cap = runner_cap.ledger.record(runner_cap.record_id)
    assert rec_cap["calls"] == 1
    assert rec_cap["cap_reached"] is True
    assert runner_cap.ledger.total == 1
    assert (out_cap / "direct_baseline" / "case-0.zh->en.json").is_file()
    for idx in (1, 2, 3):
        assert not (out_cap / "direct_baseline" / f"case-{idx}.zh->en.json").exists()

    manifest_cap = json.loads((out_cap / "direct_baseline" / "manifest.json").read_text(encoding="utf-8"))
    _assert_manifest_counts(manifest_cap, total=4, completed=1, failed=0, not_started=3)
    assert [c["status"] for c in manifest_cap["cases"]] == [
        "complete",
        "not_started",
        "not_started",
        "not_started",
    ]

    # Resume after pre-send cap: case-0 reused, cases 1..3 sent (3 HTTP calls)
    runner_resume, resume_payloads = _build_direct_runner(
        ev, run_dirs, out_cap, monkeypatch, model="deepseek-chat", resume=True
    )
    resumed = _run_with_pre_send_hook(ev, runner_resume, runner_resume.run_direct_baseline(cases))
    assert len(resumed) == 4
    assert len(resume_payloads) == 3
    assert runner_resume.ledger.record(runner_resume.record_id)["calls"] == 3
    assert runner_resume.ledger.total == 4
    manifest_resumed = json.loads((out_cap / "direct_baseline" / "manifest.json").read_text(encoding="utf-8"))
    _assert_manifest_counts(manifest_resumed, total=4, completed=4, failed=0, not_started=0)

    # 2. Pre-send hook directly raising Refused on the 2nd attempt propagates verbatim
    out_ref = tmp_path / "pre_send_direct_refused"
    runner_ref, ref_payloads = _build_direct_runner(
        ev, run_dirs, out_ref, monkeypatch, model="deepseek-chat"
    )
    attempts = 0

    def direct_refused_hook(purpose: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts >= 2:
            raise ev.Refused(f"custom pre-send refused on {purpose}")
        runner_ref.ledger.charge(runner_ref.record_id)

    with pytest.raises(ev.Refused, match="custom pre-send refused on direct_translation"):
        _run_with_pre_send_hook(
            ev, runner_ref, runner_ref.run_direct_baseline(cases), hook_fn=direct_refused_hook
        )

    assert len(ref_payloads) == 1
    assert runner_ref.ledger.record(runner_ref.record_id)["calls"] == 1
    assert (out_ref / "direct_baseline" / "case-0.zh->en.json").is_file()
    for idx in (1, 2, 3):
        assert not (out_ref / "direct_baseline" / f"case-{idx}.zh->en.json").exists()
    manifest_ref = json.loads((out_ref / "direct_baseline" / "manifest.json").read_text(encoding="utf-8"))
    _assert_manifest_counts(manifest_ref, total=4, completed=1, failed=0, not_started=3)


def test_direct_baseline_four_cases_budget_stop_manifest_and_denominator(ev, run_dirs, tmp_path, monkeypatch):
    out = tmp_path / "budget_stop_run"
    call_count = 0

    def budget_stop_gateway(req: httpx.Request):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return httpx.Response(
                429,
                json={"error": {"code": "insufficient_quota", "message": "budget exhausted"}},
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": f"Translation {call_count}"}}]})

    runner, payloads = _build_direct_runner(
        ev, run_dirs, out, monkeypatch, model="deepseek-chat", gateway_fn=budget_stop_gateway
    )
    cases = [
        ev.DirectBaselineCase(case_id=f"case-{i}", source_text=f"source text {i}")
        for i in range(4)
    ]
    with pytest.raises(ev.Refused, match="budget stop"):
        _run_with_pre_send_hook(ev, runner, runner.run_direct_baseline(cases))

    manifest_path = out / "direct_baseline" / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _assert_manifest_counts(manifest, total=4, completed=1, failed=1, not_started=2)
    assert manifest["cases"][0]["status"] == "complete"
    assert manifest["cases"][1]["status"] == "failed"
    assert manifest["cases"][1]["error_code"] == "budget_exhausted"
    assert manifest["cases"][2]["status"] == "not_started"
    assert manifest["cases"][3]["status"] == "not_started"
    assert call_count == len(payloads) == 2
    assert runner.ledger.record(runner.record_id)["calls"] == 2
    assert (out / "direct_baseline" / "case-1.zh->en.json").is_file()
    assert not (out / "direct_baseline" / "case-2.zh->en.json").exists()
    assert not (out / "direct_baseline" / "case-3.zh->en.json").exists()

    # Resume: case 0 (complete) kept, case 1 (failed) retried, cases 2-3 run
    runner_resume, resume_payloads = _build_direct_runner(
        ev, run_dirs, out, monkeypatch, model="deepseek-chat", resume=True
    )
    resumed_results = _run_with_pre_send_hook(
        ev, runner_resume, runner_resume.run_direct_baseline(cases)
    )
    assert len(resumed_results) == 4
    assert len(resume_payloads) == 3
    manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
    _assert_manifest_counts(manifest_after, total=4, completed=4, failed=0, not_started=0)


def test_direct_baseline_timeout_streak_stop_manifest_and_denominator(ev, run_dirs, tmp_path, monkeypatch):
    out = tmp_path / "timeout_stop_run"
    call_count = 0

    def timeout_gateway(req: httpx.Request):
        nonlocal call_count
        call_count += 1
        raise httpx.ReadTimeout("read timed out")

    runner, payloads = _build_direct_runner(
        ev, run_dirs, out, monkeypatch, model="deepseek-chat", gateway_fn=timeout_gateway, timeout=0.1
    )
    cases = [
        ev.DirectBaselineCase(case_id=f"timeout-case-{i}", source_text=f"timeout text {i}")
        for i in range(4)
    ]
    with pytest.raises(ev.Refused, match="timeouts in a row"):
        _run_with_pre_send_hook(ev, runner, runner.run_direct_baseline(cases))

    manifest_path = out / "direct_baseline" / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _assert_manifest_counts(manifest, total=4, completed=0, failed=3, not_started=1)
    assert call_count == len(payloads) == 3
    assert runner.ledger.record(runner.record_id)["calls"] == 3
    assert not (out / "direct_baseline" / "timeout-case-3.zh->en.json").exists()


def test_direct_baseline_resume_preserves_untraversed_valid_complete_caches(
    ev, run_dirs, tmp_path, monkeypatch
):
    out = tmp_path / "resume_preserve_valid"
    cases = [
        ev.DirectBaselineCase(case_id=f"case-{i}", source_text=f"source text {i}")
        for i in range(4)
    ]
    # Seed valid complete caches for case-0, case-1, case-2
    runner_seed, seed_payloads = _build_direct_runner(
        ev, run_dirs, out, monkeypatch, model="deepseek-chat"
    )
    _run_with_pre_send_hook(ev, runner_seed, runner_seed.run_direct_baseline(cases[:3]))
    assert len(seed_payloads) == 3

    # Corrupt case-1 identity on disk; keep case-0 and case-2 valid complete; case-3 not on disk
    case1_path = out / "direct_baseline" / "case-1.zh->en.json"
    case2_path = out / "direct_baseline" / "case-2.zh->en.json"
    case1_orig = json.loads(case1_path.read_text(encoding="utf-8"))
    case2_bytes_before = case2_path.read_bytes()
    tampered_case1 = {**case1_orig, "source_text_hash": "a" * 64}
    ev.write_json_atomic(case1_path, tampered_case1)
    tampered_case1_bytes = case1_path.read_bytes()

    runner_resume, resume_payloads = _build_direct_runner(
        ev, run_dirs, out, monkeypatch, model="deepseek-chat", resume=True
    )
    with pytest.raises(ev.Refused, match="identity mismatch.*source hash"):
        _run_with_pre_send_hook(ev, runner_resume, runner_resume.run_direct_baseline(cases))

    # Zero requests sent (0 auto-rebuy), tampered evidence not overwritten/deleted,
    # and untraversed valid complete cache (case-2) preserved as complete in manifest
    assert len(resume_payloads) == 0
    assert runner_resume.ledger.record(runner_resume.record_id)["calls"] == 0
    assert case1_path.read_bytes() == tampered_case1_bytes
    assert case2_path.read_bytes() == case2_bytes_before

    manifest = json.loads((out / "direct_baseline" / "manifest.json").read_text(encoding="utf-8"))
    _assert_manifest_counts(manifest, total=4, completed=2, failed=0, not_started=2)
    assert [c["status"] for c in manifest["cases"]] == [
        "complete",
        "not_started",
        "complete",
        "not_started",
    ]

    # Also verify pre-send cap before reaching case-2 preserves case-2 as complete
    (out / "direct_baseline" / "case-0.zh->en.json").unlink()
    case1_path.unlink()
    runner_cap_resume, cap_resume_payloads = _build_direct_runner(
        ev, run_dirs, out, monkeypatch, model="deepseek-chat", resume=True, max_calls=1
    )
    with pytest.raises(ev.Refused, match="budget stop"):
        _run_with_pre_send_hook(
            ev, runner_cap_resume, runner_cap_resume.run_direct_baseline(cases)
        )
    assert len(cap_resume_payloads) == 1
    manifest_cap = json.loads((out / "direct_baseline" / "manifest.json").read_text(encoding="utf-8"))
    _assert_manifest_counts(manifest_cap, total=4, completed=2, failed=0, not_started=2)
    assert [c["status"] for c in manifest_cap["cases"]] == [
        "complete",
        "not_started",
        "complete",
        "not_started",
    ]


def test_direct_baseline_manifest_written_when_all_stops_in_earlier_layer(
    ev, run_dirs, tmp_path, capsys
):
    run, corpus = run_dirs
    out = tmp_path / "all_capped_before_direct"
    code = ev.main(
        [
            "run",
            "--run-dir",
            str(run),
            "--corpus-dir",
            str(corpus),
            "--layer",
            "all",
            "--direct-baseline",
            "--direct-baseline-scope",
            "all",
            "--max-calls",
            "1",
            "--fake-transport",
            "--out-dir",
            str(out),
        ]
    )
    assert code == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["status"] == "capped by --max-calls"
    assert summary["calls_this_run"] == 1
    assert summary["ledger_total"] == 1

    calls = json.loads((out / "calls.json").read_text(encoding="utf-8"))
    assert len(calls) == 1
    assert calls[0]["purpose"] == "semantic_review"

    manifest_path = out / "direct_baseline" / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["scope"] == "all"
    _assert_manifest_counts(manifest, total=4, completed=0, failed=0, not_started=4)
    assert [(c["case_id"], c["direction"]) for c in manifest["cases"]] == [
        ("M:B1", "zh->en"),
        ("M:B2", "zh->en"),
        ("P:S1-u1", "zh->en"),
        ("P:S1-u1", "en->zh"),
    ]


def test_direct_model_blank_input_and_blank_response_handling(ev, sample_db, monkeypatch):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://test.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "deepseek-chat")
    call_count = 0

    def gateway(req: httpx.Request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})

    transport = httpx.MockTransport(gateway)
    translator = ev.SentenceTranslator(
        sample_db,
        llm_timeout_seconds=45.0,
        llm_max_concurrency=1,
        llm_transport=transport,
    )
    try:
        # Blank input: failed, empty_input, input_validation, final_output=None, latency=0, no requests
        res_blank = asyncio.run(
            ev.direct_model_translate(translator, "   ", case_id="blank-in")
        )
        assert res_blank["status"] == "failed"
        assert res_blank["error_code"] == "empty_input"
        assert res_blank["error_stage"] == "input_validation"
        assert res_blank["final_output"] is None
        assert res_blank["latency_s"] == 0.0
        assert call_count == 0  # No request sent!
        assert ev.result_complete(res_blank) is False

        # Blank response from model: failed, empty_response, response_validation, final_output=None
        res_resp = asyncio.run(
            ev.direct_model_translate(translator, "有效文本", case_id="blank-out")
        )
        assert res_resp["status"] == "failed"
        assert res_resp["error_code"] == "empty_response"
        assert res_resp["error_stage"] == "response_validation"
        assert res_resp["final_output"] is None
        assert call_count == 1
        assert ev.result_complete(res_resp) is False
    finally:
        asyncio.run(translator.aclose())


def test_direct_baseline_same_name_different_source_cache_refused(ev, run_dirs, tmp_path, monkeypatch):
    out = tmp_path / "cache_mismatch_run"
    runner_init, init_payloads = _build_direct_runner(ev, run_dirs, out, monkeypatch, model="deepseek-chat")
    try:
        asyncio.run(
            runner_init.run_direct_baseline(
                [ev.DirectBaselineCase(case_id="case-1", source_text="ALIEN SOURCE TEXT")]
            )
        )
    finally:
        asyncio.run(runner_init.translator.aclose())
    assert len(init_payloads) == 1
    assert init_payloads[0]["messages"][1]["content"] == "ALIEN SOURCE TEXT"

    runner_resume, resume_payloads = _build_direct_runner(
        ev, run_dirs, out, monkeypatch, model="deepseek-chat", resume=True
    )
    try:
        cases = [ev.DirectBaselineCase(case_id="case-1", source_text="ORIGINAL EXPECTED TEXT")]
        with pytest.raises(ev.Refused, match="identity mismatch.*source hash"):
            asyncio.run(runner_resume.run_direct_baseline(cases))
    finally:
        asyncio.run(runner_resume.translator.aclose())

    # Zero requests sent on refused resume: must not auto-rebuy or overwrite cached file
    assert len(resume_payloads) == 0


def test_direct_baseline_heterogeneous_source_m_pairing_refused(ev, run_dirs, tmp_path, monkeypatch):
    out = tmp_path / "pairing_test_run"
    runner, payloads = _build_direct_runner(ev, run_dirs, out, monkeypatch, model="deepseek-chat")
    try:
        asyncio.run(runner.run_m())
        asyncio.run(runner.run_direct_baseline(scope="all"))
    finally:
        asyncio.run(runner.translator.aclose())
    assert len(payloads) >= 4

    # Real Runner-generated M:B1 and direct_baseline M:B1 pair with comparable=True
    paired = ev.pair_direct_baseline(out, "M:B1", "zh->en")
    assert paired is not None
    assert paired["comparable"] is True
    assert paired["model"] == "deepseek-chat"
    assert paired["direct"]["status"] == "complete"
    assert paired["system"]["status"] == "complete"

    # Real Runner-generated P:S1-u1 has no system translation baseline: comparable=False, not_available
    paired_p = ev.pair_direct_baseline(out, "P:S1-u1", "zh->en")
    assert paired_p is not None
    assert paired_p["comparable"] is False
    assert paired_p["system"]["status"] == "not_available"
    assert "cannot form A/B conclusion" in paired_p["comparison_note"]

    # Copying M:B1 direct baseline file into P:S1-u1 slot (mismatched case_id) must fail closed
    d_b1_path = out / "direct_baseline" / "M_B1.zh->en.json"
    d_p_path = out / "direct_baseline" / "P_S1-u1.zh->en.json"
    d_b1_orig = json.loads(d_b1_path.read_text(encoding="utf-8"))
    ev.write_json_atomic(d_p_path, d_b1_orig)
    assert ev.pair_direct_baseline(out, "P:S1-u1", "zh->en") is None

    # Heterogeneous source text between M/B1.json and direct_baseline/M_B1.zh->en.json must be refused
    d_b2_orig = json.loads((out / "direct_baseline" / "M_B2.zh->en.json").read_text(encoding="utf-8"))
    ev.write_json_atomic(d_b1_path, {**d_b2_orig, "case_id": "M:B1"})
    assert ev.pair_direct_baseline(out, "M:B1", "zh->en") is None

    # Same source text but different case_id (M:B2 case_id in M_B1 file) must be refused
    ev.write_json_atomic(d_b1_path, {**d_b1_orig, "case_id": "M:B2"})
    assert ev.pair_direct_baseline(out, "M:B1", "zh->en") is None

    # Missing request_params or unknown model in direct baseline must fail closed
    no_params = dict(d_b1_orig)
    del no_params["request_params"]
    ev.write_json_atomic(d_b1_path, no_params)
    assert ev.pair_direct_baseline(out, "M:B1", "zh->en") is None

    unknown_direct = {
        **d_b1_orig,
        "model": "unknown",
        "request_params": {**d_b1_orig["request_params"], "model": "unknown"},
    }
    ev.write_json_atomic(d_b1_path, unknown_direct)
    assert ev.pair_direct_baseline(out, "M:B1", "zh->en") is None


def test_direct_baseline_cache_identity_mismatches_refused_all_dimensions(ev, run_dirs, tmp_path, monkeypatch):
    out = tmp_path / "cache_dimensions_run"
    case = ev.DirectBaselineCase(case_id="case-dim", source_text="标准测试原文", direction="zh->en")

    # Generate real cache via Runner + MockTransport
    runner_gen, gen_payloads = _build_direct_runner(ev, run_dirs, out, monkeypatch, model="deepseek-chat")
    try:
        asyncio.run(runner_gen.run_direct_baseline([case]))
    finally:
        asyncio.run(runner_gen.translator.aclose())
    assert len(gen_payloads) == 1
    assert gen_payloads[0]["model"] == "deepseek-chat"
    assert gen_payloads[0]["temperature"] == 0.0
    assert "max_tokens" not in gen_payloads[0]

    target_file = out / "direct_baseline" / "case-dim.zh->en.json"
    base_valid = json.loads(target_file.read_text(encoding="utf-8"))

    # Matching resume reuses cache without sending any request
    runner_ok, ok_payloads = _build_direct_runner(
        ev, run_dirs, out, monkeypatch, model="deepseek-chat", resume=True
    )
    try:
        res_ok = asyncio.run(runner_ok.run_direct_baseline([case]))
        assert len(res_ok) == 1 and len(ok_payloads) == 0
    finally:
        asyncio.run(runner_ok.translator.aclose())

    def assert_resume_refused(mutated_record, match_pattern: str, *, runtime_model="deepseek-chat", run_case=case):
        ev.write_json_atomic(target_file, mutated_record)
        runner_r, r_payloads = _build_direct_runner(
            ev, run_dirs, out, monkeypatch, model=runtime_model, resume=True
        )
        try:
            with pytest.raises(ev.Refused, match=match_pattern):
                asyncio.run(runner_r.run_direct_baseline([run_case]))
        finally:
            asyncio.run(runner_r.translator.aclose())
        assert len(r_payloads) == 0

    # 1. Runtime model changed vs cached model
    assert_resume_refused(base_valid, "model.*does not match", runtime_model="gpt-4o")
    # 2. Unknown or empty model rejected even if cached and runtime both say "unknown"
    unknown_rec = {
        **base_valid,
        "model": "unknown",
        "request_params": {**base_valid["request_params"], "model": "unknown"},
    }
    assert_resume_refused(unknown_rec, "unknown model|missing model", runtime_model="unknown")
    assert_resume_refused(base_valid, "unknown model|missing model", runtime_model="")
    # 3. Prompt hash mismatch
    assert_resume_refused(
        {**base_valid, "request_params": {**base_valid["request_params"], "prompt_sha256": "badhash123"}},
        "prompt hash.*does not match",
    )
    # 4. Direction & to_chinese mismatch
    assert_resume_refused({**base_valid, "direction": "en->zh"}, "direction.*does not match")
    assert_resume_refused({**base_valid, "to_chinese": True}, "to_chinese.*does not match")
    # 5. Same source text but different case_id
    assert_resume_refused({**base_valid, "case_id": "case-other"}, "case_id.*does not match")
    # 6. Temperature & max_tokens (cap) mismatch or missing
    assert_resume_refused(
        base_valid,
        "temperature.*does not match",
        run_case=ev.DirectBaselineCase(
            case_id="case-dim", source_text="标准测试原文", direction="zh->en", temperature=0.5
        ),
    )
    assert_resume_refused(
        base_valid,
        "max_tokens.*does not match",
        run_case=ev.DirectBaselineCase(
            case_id="case-dim", source_text="标准测试原文", direction="zh->en", max_tokens=2048
        ),
    )
    no_temp_params = dict(base_valid["request_params"])
    del no_temp_params["temperature"]
    assert_resume_refused({**base_valid, "request_params": no_temp_params}, "temperature")
    no_cap_params = dict(base_valid["request_params"])
    del no_cap_params["max_tokens"]
    assert_resume_refused({**base_valid, "request_params": no_cap_params}, "max_tokens")
    # 7. Missing request_params, missing model, missing source_text_hash
    no_req = dict(base_valid)
    del no_req["request_params"]
    assert_resume_refused(no_req, "missing request_params")
    no_model = dict(base_valid)
    del no_model["model"]
    assert_resume_refused(no_model, "missing model")
    no_hash = dict(base_valid)
    del no_hash["source_text_hash"]
    assert_resume_refused(no_hash, "source hash")


def test_direct_baseline_pairing_system_status_and_model_mismatch(ev, run_dirs, tmp_path, monkeypatch):
    out = tmp_path / "pairing_variations"
    # 1. Generate real A under model-A before M exists: system_status="not_found", comparable=False
    runner_a, a_payloads = _build_direct_runner(ev, run_dirs, out, monkeypatch, model="deepseek-chat")
    try:
        asyncio.run(runner_a.run_direct_baseline(scope="m"))
    finally:
        asyncio.run(runner_a.translator.aclose())
    assert len(a_payloads) == 2

    paired_missing = ev.pair_direct_baseline(out, "M:B1", "zh->en")
    assert paired_missing is not None
    assert paired_missing["comparable"] is False
    assert paired_missing["system"]["status"] == "not_found"
    assert paired_missing["system"]["text"] is None

    # 2. Generate real B via Runner.run_m under a DIFFERENT model (claude-3-5-sonnet): pairing refused
    runner_b_diff, _ = _build_direct_runner(ev, run_dirs, out, monkeypatch, model="claude-3-5-sonnet")
    try:
        asyncio.run(runner_b_diff.run_m())
    finally:
        asyncio.run(runner_b_diff.translator.aclose())
    assert ev.pair_direct_baseline(out, "M:B1", "zh->en") is None

    # 3. Re-run real B under matching model (deepseek-chat): comparable=True
    shutil.rmtree(out / "M")
    runner_b_same, _ = _build_direct_runner(ev, run_dirs, out, monkeypatch, model="deepseek-chat")
    try:
        asyncio.run(runner_b_same.run_m())
    finally:
        asyncio.run(runner_b_same.translator.aclose())
    paired_ok = ev.pair_direct_baseline(out, "M:B1", "zh->en")
    assert paired_ok is not None and paired_ok["comparable"] is True

    # 4. Request parameter differences (cap max_tokens or temperature) on A vs B refused
    m_b1_path = out / "M" / "B1.json"
    m_b1_orig = json.loads(m_b1_path.read_text(encoding="utf-8"))

    runner_capped, cap_payloads = _build_direct_runner(ev, run_dirs, out, monkeypatch, model="deepseek-chat")
    try:
        raw_b1 = runner_capped.inputs.doc_text("B1")
        asyncio.run(
            runner_capped.run_direct_baseline(
                [ev.DirectBaselineCase(case_id="M:B1", source_text=raw_b1, max_tokens=1024)]
            )
        )
    finally:
        asyncio.run(runner_capped.translator.aclose())
    assert cap_payloads[-1]["max_tokens"] == 1024
    assert ev.pair_direct_baseline(out, "M:B1", "zh->en") is None

    runner_temp, temp_payloads = _build_direct_runner(ev, run_dirs, out, monkeypatch, model="deepseek-chat")
    try:
        raw_b1 = runner_temp.inputs.doc_text("B1")
        asyncio.run(
            runner_temp.run_direct_baseline(
                [ev.DirectBaselineCase(case_id="M:B1", source_text=raw_b1, temperature=0.7)]
            )
        )
    finally:
        asyncio.run(runner_temp.translator.aclose())
    assert temp_payloads[-1]["temperature"] == 0.7
    assert ev.pair_direct_baseline(out, "M:B1", "zh->en") is None

    # Restore matching A (temperature=0.0, max_tokens=None) via real Runner
    runner_restore, _ = _build_direct_runner(ev, run_dirs, out, monkeypatch, model="deepseek-chat")
    try:
        asyncio.run(runner_restore.run_direct_baseline(scope="m"))
    finally:
        asyncio.run(runner_restore.translator.aclose())
    assert ev.pair_direct_baseline(out, "M:B1", "zh->en")["comparable"] is True

    # 5. Legacy M artifact lacking model provenance -> comparable=False, status="not_comparable" (never guessed from run-meta)
    legacy_m = dict(m_b1_orig)
    del legacy_m["model"]
    del legacy_m["request_params"]
    ev.write_json_atomic(m_b1_path, legacy_m)
    paired_legacy = ev.pair_direct_baseline(out, "M:B1", "zh->en")
    assert paired_legacy is not None
    assert paired_legacy["comparable"] is False
    assert paired_legacy["system"]["status"] == "not_comparable"

    # 6. Unknown model on M artifact -> refused (returns None)
    ev.write_json_atomic(
        m_b1_path,
        {
            **m_b1_orig,
            "model": "unknown",
            "request_params": {**m_b1_orig["request_params"], "model": "unknown"},
        },
    )
    assert ev.pair_direct_baseline(out, "M:B1", "zh->en") is None

    # 7. Failed system translation (new.text is None) -> comparable=False, status="failed"
    ev.write_json_atomic(
        m_b1_path,
        {**m_b1_orig, "new": {**m_b1_orig["new"], "text": None}},
    )
    paired_failed = ev.pair_direct_baseline(out, "M:B1", "zh->en")
    assert paired_failed is not None
    assert paired_failed["comparable"] is False
    assert paired_failed["system"]["status"] == "failed"
