"""scripts/eval_h3.py and scripts/eval_h3_export.py: the H3 runner and exporter.

Every run here is a ``--fake-transport`` dry run against an in-process gateway.
Socket connects and DNS lookups fail in every test, so a request that tried to
leave the process would fail the test. Synthetic strings only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "eval_h3.py"
EXPORTER = ROOT / "scripts" / "eval_h3_export.py"
SCORER = ROOT / "scripts" / "eval_usability.py"
PROTOCOL = ROOT / "docs" / "eval" / "h3-and-raw-baseline-protocol.md"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def h3():
    try:
        yield _load("eval_h3", RUNNER)
    finally:
        sys.modules.pop("eval_h3", None)


@pytest.fixture(scope="module")
def export():
    try:
        yield _load("eval_h3_export", EXPORTER)
    finally:
        sys.modules.pop("eval_h3_export", None)
        sys.modules.pop("eval_usability", None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts: list[object] = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    return attempts


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _src_tree() -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD:src"], check=True, capture_output=True, text=True
    ).stdout.strip()


PARAS_25 = "\n\n".join(f"Short line number {i} is here." for i in range(25))
LONG_ZH = "\n\n".join("这是一个用于测试分段的普通句子，没有任何专有名词。" * 6 for _ in range(14))
DOCS = [
    {"doc_id": "z1", "direction": "zh->en", "genre": "guide", "text": "今汐和守岸人是共鸣者。\n\n漂泊者收集声骸。"},
    {"doc_id": "z2", "direction": "zh->en", "genre": "story", "text": LONG_ZH},
    {"doc_id": "e_low", "direction": "en->zh", "genre": "guide",
     "text": "The weather is calm today.\n\nJinhsi waits by the shore."},
    {"doc_id": "e_exact", "direction": "en->zh", "genre": "announcement", "text": "Shorekeeper"},
    {"doc_id": "e_timeout", "direction": "en->zh", "genre": "mixed",
     "text": "A quiet morning.\n\nRover walks to the gate."},
    {"doc_id": "e_repair", "direction": "en->zh", "genre": "story", "text": PARAS_25},
    {"doc_id": "e_rerun", "direction": "en->zh", "genre": "mixed", "text": "Evening falls.\n\nThe bell rings twice."},
    {"doc_id": "e_dead", "direction": "en->zh", "genre": "guide", "text": "Nothing will answer.\n\nNot even twice."},
]
SCENARIO = {
    "schema": "h3-fake-scenario-v1",
    "rules": [
        {"arm": "wuwaterm_prod", "doc_id": "e_timeout", "doc_attempt": 1, "post": 1, "action": "timeout"},
        {"arm": "wuwaterm_prod", "doc_id": "e_repair", "post": 1, "action": "merge_paragraphs"},
        {"doc_id": "e_rerun", "doc_attempt": 1, "action": "http_500"},
        {"arm": "wuwaterm_prod", "doc_id": "e_dead", "action": "http_500"},
    ],
}


def _annotations(tmp: Path) -> dict[str, Path]:
    """Sample, official names and community terms: frozen before the first run."""
    paragraphs, names, community = [], [], []
    for doc in DOCS:
        for i, source in enumerate(doc["text"].split("\n\n")):
            dictionary = doc["doc_id"] == "e_exact"
            paragraphs.append({
                "id": f"{doc['doc_id']}-p{i}", "doc": doc["doc_id"], "direction": doc["direction"].replace(">", ""),
                "genre": doc["genre"], "scored": not dictionary,
                "exclusion": "dictionary" if dictionary else None, "source": source, "reference": None,
            })
    names.append({"occ": "o1", "paragraph": "z1-p0", "start": 0, "end": 2, "surface": "今汐",
                  "name_id": "n-jinhsi", "official_forms": ["Jinhsi"]})
    start = DOCS[2]["text"].index("calm")
    community.append({"occ": "c1", "paragraph": "e_low-p0", "start": start, "end": start + 4,
                      "surface": "calm", "term_id": "t-calm", "accepted_forms": ["平静"]})
    files = {}
    for name, body in (
        ("sample.json", {"schema": "usability-sample-v1", "set": "H3-test", "paragraphs": paragraphs}),
        ("names.json", {"schema": "usability-names-v1", "occurrences": names}),
        ("community.json", {"schema": "h3-community-names-v1", "occurrences": community}),
    ):
        files[name] = tmp / name
        files[name].write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return files


@pytest.fixture()
def env(tmp_path, sample_db):
    annotations = _annotations(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema": "h3-manifest-v1", "set": "H3-test", "documents": DOCS},
                                   ensure_ascii=False), encoding="utf-8")
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps(SCENARIO), encoding="utf-8")
    run_dir = tmp_path / "run"
    pins = ["--bound-tree", str(ROOT), "--terms-db", str(sample_db),
            "--expected-src-tree", _src_tree(), "--expected-terms-sha256", _sha256(sample_db)]

    freeze = [arg for path in annotations.values() for arg in ("--freeze", str(path))]

    def args(stage: str, arm: str, *extra: str, max_calls: int = 50) -> list[str]:
        return ["run", *pins, *freeze, "--run-dir", str(run_dir), "--manifest", str(manifest),
                "--stage", stage, "--arm", arm, "--max-calls", str(max_calls), *extra]

    fake = ["--fake-transport", "--fake-scenario", str(scenario)]
    return {"args": args, "pins": pins, "fake": fake, "run_dir": run_dir, "manifest": manifest,
            "tmp": tmp_path, "db": sample_db, "annotations": annotations}


def _run(module, argv, capsys):
    code = module.main(argv)
    captured = capsys.readouterr()
    out = captured.out.strip().splitlines()
    return code, (json.loads(out[-1]) if out else captured.err)


def _outputs(run_dir: Path, summary: dict) -> dict:
    data = json.loads((run_dir / summary["outputs"]["path"]).read_text(encoding="utf-8"))
    return {d["doc_id"]: d for d in data["documents"]}


def _final(doc: dict, index: int = 0) -> dict:
    return doc["attempts"][-1]["segments"][index]


# --------------------------------------------------------- judging fixtures


def _judging_inputs(tmp: Path, run_dir: Path, outputs: dict[str, dict],
                    annotations: dict[str, Path], stage: str = "pilot") -> dict[str, list[str]]:
    """Items and j1/j2/adj judgments for the dry run.

    The grades are chosen so the paired comparison has a known answer:
    zh->en wuwaterm_prod is all A where raw_baseline is B with one critical
    error; en->zh wuwaterm_prod gains A units but adds one critical error per
    document over raw_matched.
    """
    paragraphs = json.loads(annotations["sample.json"].read_text(encoding="utf-8"))["paragraphs"]
    items, records = [], {"j1": [], "j2": [], "adj": []}
    first_unit: dict[tuple[str, str], str] = {}
    for arm, arm_outputs in outputs.items():
        for p in paragraphs:
            doc = arm_outputs.get(p["doc"])
            if not p["scored"] or doc is None:
                continue
            item_id = f"{arm}:{p['id']}"
            failed = doc["status"] == "run_failed"
            items.append({"item": item_id, "paragraph": p["id"], "arm": arm,
                          "translation": None if failed else doc["output"], "run_failed": failed})
            if failed:
                continue
            first = first_unit.setdefault((arm, p["doc"]), p["id"]) == p["id"]
            record = {"item": item_id, "grade": "B", "critical_errors": [], "naturalness": "stiff", "names": []}
            if arm == "wuwaterm_prod":
                record.update(grade="A", naturalness="natural")
                if p["direction"] == "en-zh" and first:
                    record.update(grade="C", critical_errors=[{"type": "omission", "count": 1}], reason="r")
            elif arm == "raw_baseline" and p["id"] == "z2-p0":
                record.update(grade="C", critical_errors=[{"type": "number", "count": 1}], reason="r")
            if p["id"] == "z1-p0":
                label = ("official", "Jinhsi") if arm == "wuwaterm_prod" else ("not_official", "Jin Xi")
                record["names"] = [{"occ": "o1", "label": label[0], "rendering": label[1], "misleading": False}]
            if p["id"] == "e_low-p0":
                record["community_names"] = [{"occ": "c1", "label": "accepted", "rendering": "平静",
                                              "misleading": False}]
            records["j1"].append(record)
            other = json.loads(json.dumps(record))
            if item_id == "raw_baseline:z2-p1":  # a disagreement settled by adj
                other.update(grade="C", critical_errors=[{"type": "meaning_shift", "count": 1}], reason="r")
                records["adj"].append({"pass": "adj", **record, "reason": "j1 is right"})
            records["j2"].append(other)
    files = {name: str(path) for name, path in annotations.items()}
    for name, body in (
        ("items.json", {"schema": "usability-items-v1", "items": items}),
        ("j1.json", {"schema": "h3-judgment-v1", "pass": "j1", "records": records["j1"]}),
        ("j2.json", {"schema": "h3-judgment-v1", "pass": "j2", "records": records["j2"]}),
    ):
        (tmp / name).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        files[name] = str(tmp / name)
    (tmp / "adj.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records["adj"]) + "\n",
                                   encoding="utf-8")
    return {
        "argv": ["--sample", files["sample.json"], "--names", files["names.json"],
                 "--community-names", files["community.json"], "--items", files["items.json"],
                 "--judgments", files["j1.json"], files["j2.json"], str(tmp / "adj.jsonl"),
                 "--ledger", str(run_dir / "h3-ledger.json"), "--stage", stage],
        "j2": [files["j2.json"]],
    }


def _paired_compare(arm_a: Path, arm_b: Path, out: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCORER), "paired-compare", "--arm-a", str(arm_a), "--arm-b", str(arm_b),
         "--seed", "0", "--bootstrap-samples", "200", "--json-out", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text(encoding="utf-8"))


# -------------------------------------------------------------------- tests


def test_dry_run_end_to_end_runs_arms_exports_scores_and_decides(h3, export, env, capsys, no_network):
    code, prod = _run(h3, env["args"]("pilot", "wuwaterm_prod", *env["fake"]), capsys)
    assert code == 0, prod
    assert prod["posts"] == prod["transport_posts"] > 0
    docs = _outputs(env["run_dir"], prod)
    assert len(docs["z2"]["attempts"][-1]["segments"]) == 2
    assert _final(docs["e_exact"])["status"] == "dictionary" and not _final(docs["e_exact"])["requests"]
    assert _final(docs["e_low"])["returned_thinking_mode"] == "low"
    timeout = _final(docs["e_timeout"])
    assert [r["attempt_kind"] for r in timeout["requests"]] == ["initial", "timeout_fallback"]
    assert (timeout["initial_thinking_mode"], timeout["returned_thinking_mode"]) == ("low", "disabled")
    repair = _final(docs["e_repair"])
    assert [r["attempt_kind"] for r in repair["requests"]] == ["initial", "paragraph_repair"]
    assert (repair["initial_thinking_mode"], repair["returned_thinking_mode"]) == ("disabled", "low")
    assert len(docs["e_rerun"]["attempts"]) == 2 and docs["e_rerun"]["status"] == "ok"
    assert docs["e_dead"]["status"] == "run_failed" and len(docs["e_dead"]["attempts"]) == 2
    assert {r["thinking_mode"] for r in _final(docs["z1"])["requests"]} == {"disabled"}

    code, raw = _run(h3, env["args"]("pilot", "raw_baseline", *env["fake"]), capsys)
    assert code == 0, raw
    raw_docs = _outputs(env["run_dir"], raw)
    assert {r["thinking_mode"] for r in _final(raw_docs["z1"])["requests"]} == {"disabled"}
    assert {r["thinking_mode"] for r in _final(raw_docs["e_low"])["requests"]} == {"omit"}
    assert _final(raw_docs["e_exact"])["requests"]  # the raw arm has no dictionary
    assert raw_docs["z1"]["output"] == DOCS[0]["text"]

    code, matched = _run(h3, env["args"]("pilot", "raw_matched", *env["fake"]), capsys)
    assert code == 0, matched
    assert matched["parity"]["mismatches"] == 0
    assert matched["parity"]["requests_checked"] == matched["posts"]
    assert matched["parity"]["segments_by_mode"] == {"low": 4, "disabled": 1, "unmatched": 1}
    m_docs = _outputs(env["run_dir"], matched)
    assert set(m_docs) == {d["doc_id"] for d in DOCS if d["direction"] == "en->zh"}
    plan = {d: (_final(m_docs[d])["planned_mode"], _final(m_docs[d])["copy_source"]) for d in m_docs}
    assert plan["e_timeout"] == ("disabled", "returned")
    assert plan["e_repair"] == ("low", "returned")
    assert plan["e_exact"] == ("omit", "unmatched")
    assert plan["e_dead"] == ("low", "initial_no_output")
    assert [r["thinking_mode"] for r in _final(m_docs["e_timeout"])["requests"]] == ["disabled"]
    ledger = json.loads((env["run_dir"] / "h3-ledger.json").read_text(encoding="utf-8"))
    assert ledger["mode"] == "dry_run"
    assert ledger["records"][-1]["matched_source"]["segments_initial_and_returned_mode_differ"] == 2
    assert ledger["posts_total"] == prod["posts"] + raw["posts"] + matched["posts"]
    assert "Jinhsi" not in (env["run_dir"] / "h3-ledger.json").read_text(encoding="utf-8")

    judging = _judging_inputs(env["tmp"], env["run_dir"], {
        "raw_baseline": raw_docs, "wuwaterm_prod": docs, "raw_matched": m_docs}, env["annotations"])
    out = env["tmp"] / "scores"
    code, written = _run(export, ["export-scores", *judging["argv"], "--price-input-per-1m", "0.5",
                                  "--price-output-per-1m", "2", "--out-dir", str(out)], capsys)
    assert code == 0, written
    assert sorted(p.name for p in out.iterdir()) == [
        "export-summary.json", "raw_baseline.en-zh.json", "raw_baseline.json", "raw_matched.json",
        "wuwaterm_prod.en-zh.json", "wuwaterm_prod.json"]
    summary = json.loads((out / "export-summary.json").read_text(encoding="utf-8"))
    assert summary["judging"] == {"judged_by_both": summary["judging"]["judged_by_both"], "agreed":
                                  summary["judging"]["judged_by_both"] - 1, "adjudicated": 1,
                                  "agreement_rate": summary["judging"]["agreement_rate"]}
    raw_zh = summary["files"]["raw_baseline.json"]["directions"]["zh->en"]
    # z1-p0: B (1) + not_official name (2); z2-p0: C, stiff, one number error (1 + 10); 14 other B units.
    assert raw_zh["mqm"]["total_points"] == 3 + 11 + 14
    assert raw_zh["critical_errors"]["categories"]["number"] == 1
    prod_en = summary["files"]["wuwaterm_prod.json"]["directions"]["en->zh"]
    assert prod_en["run_failed"] == 2 and prod_en["critical_errors"]["categories"]["omission"] == 4
    assert summary["files"]["wuwaterm_prod.json"]["telemetry"]["en->zh"]["cost_usd"] > 0
    prod_terms = summary["files"]["wuwaterm_prod.json"]["directions"]
    assert prod_terms["zh->en"]["terms"]["official"]["accuracy"] == 1.0
    assert prod_terms["en->zh"]["terms"]["community"] == {
        "occurrences": 1, "accepted": 1, "accuracy": 1.0, "misleading": 0, "misleading_rate": 0.0}
    assert summary["files"]["raw_baseline.json"]["directions"]["zh->en"]["terms"]["official"]["accuracy"] == 0.0

    zh_en = _paired_compare(out / "raw_baseline.json", out / "wuwaterm_prod.json", env["tmp"] / "zh.json")
    en_zh = _paired_compare(out / "raw_matched.json", out / "wuwaterm_prod.en-zh.json", env["tmp"] / "en.json")
    assert zh_en["directions"]["zh->en"]["a_rate"]["verdict"] == "gain"
    assert en_zh["directions"]["en->zh"]["critical_errors"]["verdict"] == "worse"
    assert en_zh["directions"]["zh->en"]["n"] == 0

    code, _ = _run(export, ["decide", "--ledger", str(env["run_dir"] / "h3-ledger.json"),
                            "--zh-en", str(env["tmp"] / "zh.json"), "--en-zh", str(env["tmp"] / "en.json"),
                            "--json-out", str(env["tmp"] / "decision.json")], capsys)
    assert code == 0
    decision = json.loads((env["tmp"] / "decision.json").read_text(encoding="utf-8"))
    assert decision["dry_run"] is True
    zh = decision["directions"]["zh->en"]
    assert zh["claim"] == "gain" and zh["critical_error_gate"]["passed"] is True
    en = decision["directions"]["en->zh"]
    assert en["a_rate"]["verdict"] == "gain"
    assert en["critical_error_gate"]["significant_worsening"] is True
    assert en["claim"] == "worse" and en["gain_blocked_by_critical_gate"] is True
    assert no_network == []


def test_full_stage_export_and_its_refusals(h3, export, env, capsys):
    outputs = {}
    for stage, arm in (("full", "wuwaterm_prod"), ("full", "raw_baseline"), ("ablation", "raw_matched")):
        code, summary = _run(h3, env["args"](stage, arm, "--fake-transport"), capsys)
        assert code == 0, summary
        outputs[arm] = _outputs(env["run_dir"], summary)
    judging = _judging_inputs(env["tmp"], env["run_dir"], outputs, env["annotations"], stage="full")
    code, written = _run(export, ["export-scores", *judging["argv"], "--out-dir", str(env["tmp"] / "ok")], capsys)
    assert code == 0, written
    assert "raw_matched.json" in written["written"]
    argv = judging["argv"]
    j2 = argv.index(judging["j2"][0])
    only_j1 = [*argv[:j2], *argv[j2 + 2:]]  # without j2 and adj
    code, err = _run(export, ["export-scores", *only_j1, "--out-dir", str(env["tmp"] / "a")], capsys)
    assert code == 3 and "pending" in err and not (env["tmp"] / "a").exists()

    items_path = Path(argv[argv.index("--items") + 1])
    items = json.loads(items_path.read_text(encoding="utf-8"))
    edited = next(i for i in items["items"] if i["translation"])
    edited["translation"] = "not what the arm returned"
    items_path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    code, err = _run(export, ["export-scores", *argv, "--out-dir", str(env["tmp"] / "b")], capsys)
    assert code == 2 and "not part of the" in err

    names = env["annotations"]["names.json"]
    names.write_text(names.read_text(encoding="utf-8").replace("Jinhsi", "Jin Hsi"), encoding="utf-8")
    code, err = _run(export, ["export-scores", *argv, "--out-dir", str(env["tmp"] / "c")], capsys)
    assert code == 2 and "frozen in the ledger" in err


@pytest.mark.parametrize(
    ("a_verdict", "crit_verdict", "rate_a", "rate_b", "claim", "blocked"),
    [
        ("gain", "gain", 2.0, 1.0, "gain", False),
        ("gain", "indistinguishable", 2.0, 2.1, "gain", False),  # within the 10% ceiling
        ("gain", "indistinguishable", 2.0, 2.3, "indistinguishable", True),  # above the ceiling
        ("gain", "worse", 2.0, 4.0, "worse", True),
        ("gain", None, None, None, "indistinguishable", True),  # no scalars: fails closed
        ("indistinguishable", "gain", 2.0, 1.0, "indistinguishable", False),
        ("worse", "gain", 2.0, 1.0, "worse", False),
    ],
)
def test_decision_rule_critical_error_gate(export, env, h3, capsys, a_verdict, crit_verdict, rate_a, rate_b,
                                           claim, blocked):
    code, _ = _run(h3, env["args"]("full", "wuwaterm_prod", "--fake-transport", "--direction", "zh->en"), capsys)
    assert code == 0
    block = {
        "n": 10,
        "a_rate": {"delta": 0.1, "ci95": [0.05, 0.2], "verdict": a_verdict},
        "critical_errors": {
            "arm_a": {"per_1000_source_scalars": rate_a}, "arm_b": {"per_1000_source_scalars": rate_b},
            "per_1000_source_scalars_net_reduction_ci95": [-1.0, 1.0], "verdict": crit_verdict,
        },
    }
    compare = env["tmp"] / "compare.json"
    compare.write_text(json.dumps({"schema": "usability-paired-compare-v1", "arm_a": "raw_baseline",
                                   "arm_b": "wuwaterm_prod", "seed": 0, "bootstrap_samples": 10000,
                                   "directions": {"zh->en": block}}), encoding="utf-8")
    code, _ = _run(export, ["decide", "--ledger", str(env["run_dir"] / "h3-ledger.json"),
                            "--zh-en", str(compare), "--json-out", str(env["tmp"] / "d.json")], capsys)
    assert code == 0
    result = json.loads((env["tmp"] / "d.json").read_text(encoding="utf-8"))["directions"]["zh->en"]
    assert (result["claim"], result["gain_blocked_by_critical_gate"]) == (claim, blocked)


def test_max_calls_refuses_the_next_post_and_stops(h3, env, capsys, no_network):
    code, summary = _run(h3, env["args"]("pilot", "raw_baseline", "--fake-transport", max_calls=2), capsys)
    assert code == 3, summary
    assert summary["status"] == "capped"
    assert summary["posts"] == summary["transport_posts"] == 2
    assert summary["documents"] == 2  # the run stopped at the refused document, no rerun
    log = [json.loads(line) for line in (env["run_dir"] / "h3-ledger-log.jsonl").read_text().splitlines()]
    assert [e["event"] for e in log].count("post_charged") == 2
    assert [e["reason"] for e in log if e["event"] == "post_refused"] == ["max_calls"]
    assert no_network == []


def test_stage_cap_is_cumulative_across_runs(h3, env, capsys, monkeypatch):
    monkeypatch.setitem(h3.STAGE_CAPS, "pilot", 12)
    code, prod = _run(h3, env["args"]("pilot", "wuwaterm_prod", "--fake-transport", "--direction", "en->zh"), capsys)
    assert code == 0, prod
    code, raw = _run(h3, env["args"]("pilot", "raw_baseline", "--fake-transport"), capsys)
    assert code == 3 and raw["posts"] == 12 - prod["posts"]


def test_live_mode_is_refused_without_a_matching_approval(h3, env, capsys, monkeypatch, no_network):
    code, err = _run(h3, env["args"]("pilot", "raw_baseline", "--live"), capsys)
    assert code == 1 and "--owner-approval-token" in err
    code, err = _run(h3, env["args"]("pilot", "raw_baseline", "--live", "--fake-transport"), capsys)
    assert code == 1 and "exactly one" in err
    code, err = _run(h3, env["args"]("pilot", "raw_baseline"), capsys)
    assert code == 1 and "exactly one" in err
    code, err = _run(h3, env["args"]("pilot", "raw_baseline", "--live", "--owner-approval-token", "x"), capsys)
    assert code == 1 and "dry-run only" in err  # the test tree is not the pinned tree
    # With this tree pinned, a wrong token is refused before any client exists.
    monkeypatch.setattr(h3, "PINNED_SRC_TREE", _src_tree())
    monkeypatch.setattr(h3, "PINNED_TERMS_DB_SHA256", _sha256(env["db"]))
    monkeypatch.setattr(h3, "PINNED_MATCHER_REVISION", sys.modules["wuwaterm.cjk_span"].MATCHER_REVISION)
    live = ["run", "--bound-tree", str(ROOT), "--terms-db", str(env["db"]), "--run-dir", str(env["run_dir"]),
            "--manifest", str(env["manifest"]), "--stage", "pilot", "--arm", "raw_baseline", "--max-calls", "5",
            "--live", "--owner-approval-token", "h3-live-000000000000000000000000"]
    code, err = _run(h3, live, capsys)
    assert code == 1 and "does not match" in err
    assert not env["run_dir"].exists()
    assert no_network == []


def test_refusals_for_tampering_foreign_ledgers_and_wrong_bindings(h3, env, capsys):
    code, err = _run(h3, env["args"]("pilot", "raw_baseline", "--fake-transport") + ["--expected-src-tree", "0" * 40],
                     capsys)
    assert code == 1 and "HEAD:src" in err
    code, prod = _run(h3, env["args"]("pilot", "wuwaterm_prod", "--fake-transport"), capsys)
    assert code == 0
    outputs = env["run_dir"] / prod["outputs"]["path"]
    original = outputs.read_bytes()
    outputs.write_bytes(original.replace(b'"low"', b'"disabled"'))
    code, err = _run(h3, env["args"]("pilot", "raw_matched", "--fake-transport"), capsys)
    assert code == 1 and "SHA-256 recorded in the ledger" in err
    outputs.write_bytes(original)
    log = env["run_dir"] / "h3-ledger-log.jsonl"
    log.write_bytes(log.read_bytes().replace(b'"post_charged"', b'"post_chargeD"', 1))
    code, err = _run(h3, env["args"]("pilot", "raw_matched", "--fake-transport"), capsys)
    assert code == 1 and "hash chain" in err
    other = env["tmp"] / "q8"
    other.mkdir()
    (other / "ledger.json").write_text("{}", encoding="utf-8")
    code, err = _run(h3, env["args"]("pilot", "raw_baseline", "--fake-transport") + ["--run-dir", str(other)], capsys)
    assert code == 1 and "another evaluation" in err
    assert (other / "ledger.json").read_text(encoding="utf-8") == "{}"


def test_preflight_and_cost_estimate_itemize_the_dry_run(h3, env, capsys, no_network):
    out = env["tmp"] / "cost.json"
    code = h3.main(["cost-estimate", *env["pins"], "--manifest", str(env["manifest"]),
                    "--price-input-per-1m", "0.5", "--price-output-per-1m", "2", "--json-out", str(out)])
    assert code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    rows = {(r["stage"], r["arm"]): r["posts"] for r in report["rows"]}
    # 9 non-empty segments; prod skips the dictionary-answered one; raw_matched is en->zh only.
    assert rows == {("full", "wuwaterm_prod"): 8, ("full", "raw_baseline"): 9, ("ablation", "raw_matched"): 6}
    stages = {s["stage"]: s for s in report["stages"]}
    assert stages["full"]["posts"] == 17 and stages["full"]["headroom"] == 240 - 17
    assert stages["pilot"]["posts"] is None and stages["arm_c"]["hard_cap"] == 0
    assert report["total"] == {"posts": 23, "hard_cap": 354, "within_cap": True}
    assert report["approval_tokens"]["full"].startswith("unavailable")
    cost = report["cost"]
    assert cost["total"]["cost_usd"] == pytest.approx(sum(r["cost_usd"] for r in cost["rows"]))
    assert cost["total"]["cost_usd"] > 0
    assert no_network == []


def test_protocol_records_the_sha256_of_the_frozen_h3_tools():
    text = PROTOCOL.read_text(encoding="utf-8")
    for rel in ("scripts/eval_h3.py", "scripts/eval_h3_export.py", "docs/eval/h3-judging-addendum.md",
                "scripts/eval_usability.py"):
        recorded = re.findall(rf"`{re.escape(rel)}` SHA-256 `([0-9a-f]{{64}})`", text)
        assert recorded, rel
        assert set(recorded) == {_sha256(ROOT / rel)}, rel
