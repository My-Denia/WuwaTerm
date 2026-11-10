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
