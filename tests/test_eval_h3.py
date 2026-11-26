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

import httpx
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
            "tmp": tmp_path, "db": sample_db, "annotations": annotations, "freeze": freeze}


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


def _write_live_preconditions(
    h3,
    tmp: Path,
    *,
    manifest: Path,
    src_tree: str,
    terms_db: Path,
    annotations: dict[str, Path],
    verdict: str = "pass",
    matcher_revision: str | None = None,
    core_commit: str | None = None,
    freeze_core_commit: str | None = None,
    non_default_floor: bool = False,
    floor_profile: str = "default",
    non_default_parameters: dict[str, object] | None = None,
    windows_uncleared: int = 0,
    tamper_bindings_sha256: bool = False,
    tools_sha256_override: dict[str, str] | None = None,
    failed_rules: list[str] | None = None,
    upstream_clearances: Path | None = None,
    community_lexicon: Path | None = None,
    annotation_adjudication: Path | None = None,
) -> tuple[Path, Path]:
    matcher = matcher_revision or sys.modules["wuwaterm.cjk_span"].MATCHER_REVISION
    frozen_tools = h3.frozen_file_hashes()
    tools_sha = {
        "scripts/eval_h3_corpus.py": frozen_tools["scripts/eval_h3_corpus.py"],
        "scripts/eval_usability.py": frozen_tools["scripts/eval_usability.py"],
    }
    if tools_sha256_override:
        tools_sha.update(tools_sha256_override)
    tag_commit = core_commit or h3.PINNED_TAG_COMMIT
    freeze_commit = freeze_core_commit or ("a" * 40)
    non_default = dict(non_default_parameters or {})
    if non_default_floor and not non_default:
        non_default["announcement_floor_zh_en"] = 10
    inputs_sha: dict[str, object] = {
        "manifest": {"name": manifest.name, "sha256": _sha256(manifest)},
        "sample": {"name": annotations["sample.json"].name, "sha256": _sha256(annotations["sample.json"])},
        "names": {"name": annotations["names.json"].name, "sha256": _sha256(annotations["names.json"])},
        "community_names": {
            "name": annotations["community.json"].name,
            "sha256": _sha256(annotations["community.json"]),
        },
        "annotation_adjudication": (
            {"name": annotation_adjudication.name, "sha256": _sha256(annotation_adjudication)}
            if annotation_adjudication is not None
            else None
        ),
        "metadata": None,
        "prior_ids": {"name": "prior_ids.json", "sha256": "1" * 64},
        "prior_texts": [{"name": "prior_texts.json", "sha256": "2" * 64}],
        "clear_names": [],
        "community_lexicon": (
            [{"name": community_lexicon.name, "sha256": _sha256(community_lexicon)}]
            if community_lexicon is not None
            else []
        ),
        "upstream_clearances": (
            {"name": upstream_clearances.name, "sha256": _sha256(upstream_clearances)}
            if upstream_clearances is not None
            else None
        ),
    }
    bindings = {
        "core_tag_commit": tag_commit,
        "core_commit": tag_commit,
        "freeze_core_commit": freeze_commit,
        "bound_tree_src_sha": src_tree,
        "terms_db_sha256": _sha256(terms_db),
        "matcher_revision": matcher,
        "non_default_parameters": non_default,
        "tools_sha256": tools_sha,
        "inputs_sha256": inputs_sha,
    }
    rules_failed = list(failed_rules) if failed_rules is not None else ([] if verdict == "pass" else ["C8"])
    checks = {
        f"C{i}": {"status": "fail" if f"C{i}" in rules_failed else "pass"}
        for i in range(1, 11)
    }
    checks["C3"].update({
        "floor_profile": floor_profile,
        "non_default_floor": non_default_floor,
        "announcement_units_by_level": {
            "zh->en": {"L0": 30, "L1": 0, "L2": 0},
            "en->zh": {"L0": 30, "L1": 0, "L2": 0},
        },
    })
    checks["C4"].update({
        "windows_hit": 0,
        "windows_cleared_m1": 0,
        "windows_cleared_m2": 0,
        "windows_cleared_m3": 0,
        "windows_uncleared": windows_uncleared,
        "scorer_sha256_ok": True,
        "required_prior_sets_complete": True,
        "m2_max_shared_span_threshold": 24,
    })
    checks["C7"].update({"cutoff": "2026-11-06T19:45:00+08:00"})
    bindings_sha = h3.sha256_bytes(h3.canonical(bindings))
    if tamper_bindings_sha256:
        bindings_sha = "0" * 64
    acceptance_body = {
        "schema": "h3-acceptance-v1",
        "verdict": verdict,
        "failed_rules": rules_failed,
        "non_default_parameters": non_default,
        "checks": checks,
        "bindings": bindings,
        "bindings_sha256": bindings_sha,
    }
    acceptance = tmp / "acceptance.json"
    acceptance.write_text(json.dumps(acceptance_body, ensure_ascii=False), encoding="utf-8")
    judges = tmp / "judges.json"
    judges.write_text(
        json.dumps(
            {
                "schema": "h3-judges-v1",
                "judges": {
                    "j1": {"model": "strong-judge-a", "provider": "sub-a", "context_id": "ctx-j1"},
                    "j2": {"model": "strong-judge-b", "provider": "sub-b", "context_id": "ctx-j2"},
                    "adj": {"model": "strong-judge-c", "provider": "sub-c", "context_id": "ctx-adj"},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return acceptance, judges


def test_live_mode_is_refused_without_a_matching_approval(h3, env, capsys, monkeypatch, no_network):
    cost_flags = ["--ledger-budget-usd", "5.0", "--price-input-per-1m", "0.5", "--price-output-per-1m", "2.0"]
    code, err = _run(h3, env["args"]("pilot", "raw_baseline", "--live"), capsys)
    assert code == 1 and "--owner-approval-token" in err
    code, err = _run(h3, env["args"]("pilot", "raw_baseline", "--live", "--fake-transport"), capsys)
    assert code == 1 and "exactly one" in err
    code, err = _run(h3, env["args"]("pilot", "raw_baseline"), capsys)
    assert code == 1 and "exactly one" in err
    code, err = _run(
        h3, env["args"]("pilot", "raw_baseline", "--live", "--owner-approval-token", "x", *cost_flags), capsys
    )
    assert code == 1 and "dry-run only" in err  # sample_db is not the pinned production terms.db
    # With this tree and test db pinned and valid preconditions, a wrong token is refused before any client exists.
    monkeypatch.setattr(h3, "PINNED_SRC_TREE", _src_tree())
    monkeypatch.setattr(h3, "PINNED_TERMS_DB_SHA256", _sha256(env["db"]))
    monkeypatch.setattr(h3, "PINNED_MATCHER_REVISION", sys.modules["wuwaterm.cjk_span"].MATCHER_REVISION)
    acceptance, judges = _write_live_preconditions(
        h3, env["tmp"], manifest=env["manifest"], src_tree=_src_tree(),
        terms_db=env["db"], annotations=env["annotations"],
    )
    live = ["run", "--bound-tree", str(ROOT), "--terms-db", str(env["db"]), *env["freeze"],
            "--freeze", str(acceptance), "--freeze", str(judges),
            "--run-dir", str(env["run_dir"]), "--manifest", str(env["manifest"]),
            "--stage", "pilot", "--arm", "raw_baseline", "--max-calls", "5",
            "--live", "--owner-approval-token", "h3-live-000000000000000000000000", *cost_flags]
    code, err = _run(h3, live, capsys)
    assert code == 1 and "does not match" in err
    assert not env["run_dir"].exists()
    assert no_network == []


def test_v014_pinned_bindings_and_missing_corpus_script_refusal(h3, env, tmp_path, monkeypatch, capsys):
    assert h3.PINNED_TAG_COMMIT == "2737530187f1e1c3a6b8758de357c8d32d21dfc7"
    assert h3.PINNED_SRC_TREE == "8e6bd426bfc12625f050c03ed14e419d3dba145f"
    assert h3.PINNED_MATCHER_REVISION == "286d29e8f25b1ee53d0634c89bf6fb8a83ef950888c21f15d485413cb25d0664"
    assert "scripts/eval_h3_corpus.py" in h3.FROZEN_FILES

    # Build a repo mirror deliberately missing scripts/eval_h3_corpus.py.
    incomplete_root = tmp_path / "_missing_corpus_repo"
    for rel in h3.FROZEN_FILES:
        if rel == "scripts/eval_h3_corpus.py":
            continue
        dest = incomplete_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT / rel).read_bytes())
    monkeypatch.setattr(h3, "ROOT", incomplete_root)
    with pytest.raises(h3.Refused, match=r"scripts/eval_h3_corpus\.py"):
        h3.frozen_file_hashes()
    code, err = _run(h3, env["args"]("pilot", "raw_baseline", "--fake-transport"), capsys)
    assert code == 1 and "scripts/eval_h3_corpus.py" in err


def test_live_missing_cost_params_refused_before_client_construction(h3, env, capsys, monkeypatch, no_network):
    def fail_client(*args, **kwargs):
        raise AssertionError("HTTP client/transport must not be constructed when cost params are missing")

    monkeypatch.setattr(httpx, "AsyncClient", fail_client)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", fail_client)

    base = env["args"]("pilot", "raw_baseline", "--live", "--owner-approval-token", "h3-live-000000000000000000000000")
    cases = [
        [],  # missing budget and both prices
        ["--max-cost-usd", "5.0"],  # missing both prices
        ["--ledger-budget-usd", "5.0", "--price-input-per-1m", "0.5"],  # missing output price
        ["--ledger-budget-usd", "5.0", "--price-output-per-1m", "2.0"],  # missing input price
        ["--price-input-per-1m", "0.5", "--price-output-per-1m", "2.0"],  # missing budget
        ["--max-cost-usd", "0", "--price-input-per-1m", "0.5", "--price-output-per-1m", "2.0"],  # zero budget
        ["--ledger-budget-usd", "5.0", "--max-cost-usd", "0",
         "--price-input-per-1m", "0.5", "--price-output-per-1m", "2.0"],  # zero per-run cap
        ["--max-cost-usd", "5.0", "--price-input-per-1m", "-0.5", "--price-output-per-1m", "2.0"],  # negative price
    ]
    for extra in cases:
        code, err = _run(h3, [*base, *extra], capsys)
        assert code == 1, (extra, err)
    assert not env["run_dir"].exists()
    assert no_network == []

    # CLI help must explicitly state --owner-approval-token is not a budget authorization,
    # and describe --ledger-budget-usd as a pre-send USD budget gate.
    run_parser = h3.build_parser()._subparsers._group_actions[0].choices["run"]
    help_text = run_parser.format_help()
    assert "--owner-approval-token" in help_text
    assert "NOT a budget" in help_text or "not a budget" in help_text.lower()
    assert "pre-send USD budget gate" in help_text


def test_cost_cap_pre_send_refusal_exits_3_and_keeps_transport_posts_equal(h3, env, capsys, no_network):
    cost_cfg = h3.CostConfig(ledger_budget_usd=1.0, price_input_per_1m=10.0, price_output_per_1m=40.0)
    reserve_first = cost_cfg.reserve_usd(DOCS[0]["text"])
    # Budget allows the first POST (plus a tiny margin) once actual cost settles, OR if we use
    # http_500 on doc 1 attempt 1 so doc 1 attempt 1 keeps full reserve and doc 1 attempt 2 exceeds budget:
    scenario = env["tmp"] / "cap_scenario.json"
    scenario.write_text(
        json.dumps({"schema": "h3-fake-scenario-v1", "rules": [{"doc_id": "z1", "doc_attempt": 1, "action": "http_500"}]}),
        encoding="utf-8",
    )
    budget = round(reserve_first * 1.5, 12)
    code, summary = _run(
        h3,
        env["args"](
            "pilot", "raw_baseline", "--fake-transport", "--fake-scenario", str(scenario),
            "--max-cost-usd", str(budget), "--price-input-per-1m", "10.0", "--price-output-per-1m", "40.0",
        ),
        capsys,
    )
    assert code == 3, summary
    assert summary["status"] == "capped"
    assert summary["stop_reason"] == "cost_cap"
    assert summary["posts"] == summary["transport_posts"] == 1
    assert summary["spent_usd"] == pytest.approx(reserve_first)
    assert summary["ledger_spent_usd"] == pytest.approx(reserve_first)
    assert summary["ledger_budget_usd"] == pytest.approx(budget)
    log = [json.loads(line) for line in (env["run_dir"] / "h3-ledger-log.jsonl").read_text().splitlines()]
    assert [e["event"] for e in log].count("post_charged") == 1
    refused = [e for e in log if e["event"] == "post_refused"]
    assert len(refused) == 1 and refused[0]["reason"] == "cost_cap"
    assert no_network == []


def test_failed_and_timeout_posts_charge_full_reserve_and_ok_settles_actual(h3, env, capsys, no_network):
    price_in, price_out = 2.0, 8.0
    cost_cfg = h3.CostConfig(ledger_budget_usd=10.0, price_input_per_1m=price_in, price_output_per_1m=price_out)
    # Unit check: reasoning tokens billed at output price (both inclusive OpenAI and separate formats).
    assert cost_cfg.actual_usd(
        {"prompt_tokens": 100, "completion_tokens": 250, "total_tokens": 350, "reasoning_tokens": 150}
    ) == pytest.approx(100 * 2.0 / 1e6 + 250 * 8.0 / 1e6)
    assert cost_cfg.actual_usd(
        {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 350, "reasoning_tokens": 150}
    ) == pytest.approx(100 * 2.0 / 1e6 + 250 * 8.0 / 1e6)
    assert cost_cfg.actual_usd(
        {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": None, "reasoning_tokens": 150},
        raw_usage={"prompt_tokens": 100, "completion_tokens": 100, "reasoning_tokens": 150},
    ) == pytest.approx(100 * 2.0 / 1e6 + 250 * 8.0 / 1e6)

    # End-to-end: z1 attempt 1 times out (full reserve charged), z1 attempt 2 succeeds (actual charged).
    one_doc = env["tmp"] / "one_doc.json"
    one_doc.write_text(
        json.dumps({"schema": "h3-manifest-v1", "set": "H3-one", "documents": [DOCS[0]]}, ensure_ascii=False),
        encoding="utf-8",
    )
    scenario = env["tmp"] / "timeout_then_ok.json"
    scenario.write_text(
        json.dumps({"schema": "h3-fake-scenario-v1", "rules": [{"doc_id": "z1", "doc_attempt": 1, "action": "timeout"}]}),
        encoding="utf-8",
    )
    code, summary = _run(
        h3,
        ["run", *env["pins"], *env["freeze"], "--run-dir", str(env["run_dir"]), "--manifest", str(one_doc),
         "--stage", "pilot", "--arm", "raw_baseline", "--max-calls", "10",
         "--fake-transport", "--fake-scenario", str(scenario),
         "--ledger-budget-usd", "10.0", "--price-input-per-1m", str(price_in), "--price-output-per-1m", str(price_out)],
        capsys,
    )
    assert code == 0, summary
    assert summary["posts"] == summary["transport_posts"] == 2
    reserve = cost_cfg.reserve_usd(DOCS[0]["text"])
    tel = summary["telemetry"]["zh->en"]
    actual_ok = tel["prompt_tokens"] * price_in / 1e6 + tel["completion_tokens"] * price_out / 1e6
    assert summary["spent_usd"] == pytest.approx(reserve + actual_ok)
    log = [json.loads(line) for line in (env["run_dir"] / "h3-ledger-log.jsonl").read_text().splitlines()]
    settled = [e for e in log if e["event"] == "post_settled"]
    assert [e["billing"] for e in settled] == ["reserved_on_failure", "actual"]
    assert settled[0]["settled_usd"] == pytest.approx(reserve)
    assert settled[1]["settled_usd"] == pytest.approx(actual_ok)
    assert no_network == []


def test_missing_usage_charges_full_reserve_and_stops_immediately(h3, env, capsys, no_network):
    price_in, price_out = 1.0, 4.0
    cost_cfg = h3.CostConfig(ledger_budget_usd=10.0, price_input_per_1m=price_in, price_output_per_1m=price_out)
    scenario = env["tmp"] / "missing_usage.json"
    scenario.write_text(
        json.dumps({"schema": "h3-fake-scenario-v1", "rules": [{"doc_id": "z1", "action": "missing_usage"}]}),
        encoding="utf-8",
    )
    code, summary = _run(
        h3,
        env["args"](
            "pilot", "raw_baseline", "--fake-transport", "--fake-scenario", str(scenario),
            "--ledger-budget-usd", "10.0", "--price-input-per-1m", str(price_in), "--price-output-per-1m", str(price_out),
        ),
        capsys,
    )
    assert code == 3, summary
    assert summary["status"] == "capped"
    assert summary["stop_reason"] == "missing_usage"
    assert summary["posts"] == summary["transport_posts"] == 1
    assert summary["documents"] == 1
    expected_reserve = cost_cfg.reserve_usd(DOCS[0]["text"])
    assert summary["spent_usd"] == pytest.approx(expected_reserve)
    assert summary["ledger_spent_usd"] == pytest.approx(expected_reserve)
    log = [json.loads(line) for line in (env["run_dir"] / "h3-ledger-log.jsonl").read_text().splitlines()]
    settled = [e for e in log if e["event"] == "post_settled"]
    assert len(settled) == 1 and settled[0]["billing"] == "reserved_missing_usage"
    assert settled[0]["settled_usd"] == pytest.approx(expected_reserve)
    assert no_network == []


def test_ledger_budget_usd_is_immutable_and_tamper_evident(h3, env, capsys, no_network):
    cost = ["--ledger-budget-usd", "5.0", "--price-input-per-1m", "0.5", "--price-output-per-1m", "2.0"]
    code, summary = _run(
        h3, env["args"]("pilot", "raw_baseline", "--fake-transport", "--direction", "zh->en", *cost), capsys
    )
    assert code == 0, summary
    ledger_path = env["run_dir"] / "h3-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["ledger_budget_usd"] == 5.0
    assert ledger["prices_usd_per_1m"] == {"input": 0.5, "output": 2.0}
    assert ledger["spent_usd"] == pytest.approx(summary["spent_usd"])

    # Subsequent run with a different budget must be refused (exit 1).
    changed_cost = ["--ledger-budget-usd", "6.0", "--price-input-per-1m", "0.5", "--price-output-per-1m", "2.0"]
    code, err = _run(
        h3, env["args"]("pilot", "raw_baseline", "--fake-transport", "--direction", "en->zh", *changed_cost), capsys
    )
    assert code == 1 and "immutable" in err

    # Subsequent run with lower token prices must be refused (exit 1).
    lower_prices = ["--ledger-budget-usd", "5.0", "--price-input-per-1m", "0.25", "--price-output-per-1m", "1.0"]
    code, err = _run(
        h3, env["args"]("pilot", "raw_baseline", "--fake-transport", "--direction", "en->zh", *lower_prices), capsys
    )
    assert code == 1 and "prices_usd_per_1m" in err and "immutable" in err

    # Tampering ledger_budget_usd in h3-ledger.json is caught against h3-ledger-log.jsonl.
    original = ledger_path.read_text(encoding="utf-8")
    tampered = dict(ledger, ledger_budget_usd=500.0)
    ledger_path.write_text(json.dumps(tampered), encoding="utf-8")
    code, err = _run(
        h3,
        env["args"]("pilot", "raw_baseline", "--fake-transport", "--direction", "en->zh",
                    "--ledger-budget-usd", "500.0", "--price-input-per-1m", "0.5", "--price-output-per-1m", "2.0"),
        capsys,
    )
    assert code == 1 and "ledger_budget_usd" in err

    # Tampering prices_usd_per_1m in h3-ledger.json is caught against h3-ledger-log.jsonl.
    tampered_prices = dict(ledger, prices_usd_per_1m={"input": 0.25, "output": 1.0})
    ledger_path.write_text(json.dumps(tampered_prices), encoding="utf-8")
    code, err = _run(
        h3, env["args"]("pilot", "raw_baseline", "--fake-transport", "--direction", "en->zh", *lower_prices), capsys
    )
    assert code == 1 and "prices_usd_per_1m" in err

    # Tampering spent_usd in h3-ledger.json is caught against h3-ledger-log.jsonl.
    tampered_spent = dict(ledger, spent_usd=0.0)
    ledger_path.write_text(json.dumps(tampered_spent), encoding="utf-8")
    code, err = _run(
        h3, env["args"]("pilot", "raw_baseline", "--fake-transport", "--direction", "en->zh", *cost), capsys
    )
    assert code == 1 and "spent_usd" in err

    # Restoring original ledger allows the next direction with the same immutable budget and prices.
    ledger_path.write_text(original, encoding="utf-8")
    code, summary2 = _run(
        h3, env["args"]("pilot", "raw_baseline", "--fake-transport", "--direction", "en->zh", *cost), capsys
    )
    assert code == 0, summary2
    assert summary2["ledger_spent_usd"] == pytest.approx(summary["spent_usd"] + summary2["spent_usd"])
    assert no_network == []


def test_acceptance_and_judges_preconditions_gate_preflight_and_live(h3, env, capsys, monkeypatch, no_network):
    monkeypatch.setattr(h3, "PINNED_SRC_TREE", _src_tree())
    monkeypatch.setattr(h3, "PINNED_TERMS_DB_SHA256", _sha256(env["db"]))
    monkeypatch.setattr(h3, "PINNED_MATCHER_REVISION", sys.modules["wuwaterm.cjk_span"].MATCHER_REVISION)
    pinned_args = ["--bound-tree", str(ROOT), "--terms-db", str(env["db"])]
    cost_flags = ["--ledger-budget-usd", "5.0", "--price-input-per-1m", "0.5", "--price-output-per-1m", "2.0"]

    def run_preflight(*extra_freeze: Path) -> dict:
        out = env["tmp"] / f"pf-{len(list(env['tmp'].glob('pf-*.json')))}.json"
        freeze = [*env["freeze"], *(arg for p in extra_freeze for arg in ("--freeze", str(p)))]
        code = h3.main(["preflight", *pinned_args, *freeze, "--manifest", str(env["manifest"]), "--json-out", str(out)])
        assert code == 0
        return json.loads(out.read_text(encoding="utf-8"))

    # 1. Missing both h3-acceptance-v1 and h3-judges-v1 -> unavailable.
    pf_none = run_preflight()
    assert pf_none["approval_tokens"]["full"].startswith("unavailable")
    assert "h3-acceptance-v1" in pf_none["approval_tokens"]["full"]
    assert "not a budget" in pf_none["approval_token_note"].lower()

    # 2. Acceptance verdict != pass -> unavailable, and run --live refused.
    bad_acc, judges = _write_live_preconditions(
        h3, env["tmp"], manifest=env["manifest"], src_tree=_src_tree(),
        terms_db=env["db"], annotations=env["annotations"], verdict="fail",
    )
    pf_fail = run_preflight(bad_acc, judges)
    assert pf_fail["approval_tokens"]["full"].startswith("unavailable")
    assert "verdict" in pf_fail["approval_tokens"]["full"]
    code, err = _run(
        h3,
        ["run", *pinned_args, *env["freeze"], "--freeze", str(bad_acc), "--freeze", str(judges),
         "--run-dir", str(env["run_dir"]), "--manifest", str(env["manifest"]),
         "--stage", "pilot", "--arm", "raw_baseline", "--max-calls", "5",
         "--live", "--owner-approval-token", "h3-live-000000000000000000000000", *cost_flags],
        capsys,
    )
    assert code == 1 and "verdict" in err

    # 3. Valid acceptance without judges -> unavailable, and run --live refused.
    good_acc, judges = _write_live_preconditions(
        h3, env["tmp"], manifest=env["manifest"], src_tree=_src_tree(),
        terms_db=env["db"], annotations=env["annotations"], verdict="pass",
    )
    pf_no_judges = run_preflight(good_acc)
    assert pf_no_judges["approval_tokens"]["full"].startswith("unavailable")
    assert "h3-judges-v1" in pf_no_judges["approval_tokens"]["full"]
    code, err = _run(
        h3,
        ["run", *pinned_args, *env["freeze"], "--freeze", str(good_acc),
         "--run-dir", str(env["run_dir"]), "--manifest", str(env["manifest"]),
         "--stage", "pilot", "--arm", "raw_baseline", "--max-calls", "5",
         "--live", "--owner-approval-token", "h3-live-000000000000000000000000", *cost_flags],
        capsys,
    )
    assert code == 1 and "h3-judges-v1" in err

    # 4. Acceptance with mismatched bound_tree_src_sha -> unavailable.
    wrong_tree_acc, _ = _write_live_preconditions(
        h3, env["tmp"], manifest=env["manifest"], src_tree="0" * 40,
        terms_db=env["db"], annotations=env["annotations"], verdict="pass",
    )
    pf_wrong_tree = run_preflight(wrong_tree_acc, judges)
    assert pf_wrong_tree["approval_tokens"]["full"].startswith("unavailable")
    assert "bound_tree_src_sha" in pf_wrong_tree["approval_tokens"]["full"]

    # 5. Tampered bindings_sha256 -> unavailable.
    tampered_acc, _ = _write_live_preconditions(
        h3, env["tmp"], manifest=env["manifest"], src_tree=_src_tree(),
        terms_db=env["db"], annotations=env["annotations"], tamper_bindings_sha256=True,
    )
    pf_tampered = run_preflight(tampered_acc, judges)
    assert pf_tampered["approval_tokens"]["full"].startswith("unavailable")
    assert "bindings_sha256" in pf_tampered["approval_tokens"]["full"]

    # 6. Non-default C3 floor override -> unavailable (fail-closed in validate_acceptance_receipt).
    custom_floor_acc, _ = _write_live_preconditions(
        h3, env["tmp"], manifest=env["manifest"], src_tree=_src_tree(),
        terms_db=env["db"], annotations=env["annotations"],
        non_default_floor=True, floor_profile="custom:announcement[zh->en]=10",
    )
    pf_floor = run_preflight(custom_floor_acc, judges)
    assert pf_floor["approval_tokens"]["full"].startswith("unavailable")
    assert "non_default_parameters" in pf_floor["approval_tokens"]["full"] or "floor_profile" in pf_floor["approval_tokens"]["full"]

    # 7. C4 windows_uncleared > 0 -> unavailable.
    uncleared_acc, _ = _write_live_preconditions(
        h3, env["tmp"], manifest=env["manifest"], src_tree=_src_tree(),
        terms_db=env["db"], annotations=env["annotations"], windows_uncleared=1,
    )
    pf_c4 = run_preflight(uncleared_acc, judges)
    assert pf_c4["approval_tokens"]["full"].startswith("unavailable")
    assert "windows_uncleared" in pf_c4["approval_tokens"]["full"]

    # 8. Mismatched tools_sha256 -> unavailable (gate_sha256 mismatch).
    bad_tool_acc, _ = _write_live_preconditions(
        h3, env["tmp"], manifest=env["manifest"], src_tree=_src_tree(),
        terms_db=env["db"], annotations=env["annotations"],
        tools_sha256_override={"scripts/eval_h3_corpus.py": "f" * 64},
    )
    pf_tool = run_preflight(bad_tool_acc, judges)
    assert pf_tool["approval_tokens"]["full"].startswith("unavailable")
    assert "gate_sha256" in pf_tool["approval_tokens"]["full"]

    # 8b. freeze_core_commit equal to core_tag_commit -> unavailable.
    same_commit_acc, _ = _write_live_preconditions(
        h3, env["tmp"], manifest=env["manifest"], src_tree=_src_tree(),
        terms_db=env["db"], annotations=env["annotations"],
        freeze_core_commit=h3.PINNED_TAG_COMMIT,
    )
    pf_same = run_preflight(same_commit_acc, judges)
    assert pf_same["approval_tokens"]["full"].startswith("unavailable")
    assert "freeze_core_commit" in pf_same["approval_tokens"]["full"]

    # 8c. External clearance/adjudication files recorded in receipt must be frozen and untampered.
    clearances = env["tmp"] / "upstream_clearances.json"
    clearances.write_text(json.dumps({"schema": "h3-upstream-clearances-v1", "entries": []}), encoding="utf-8")
    lexicon = env["tmp"] / "community_lexicon.json"
    lexicon.write_text(json.dumps({"schema": "h3-community-lexicon-v1", "terms": []}), encoding="utf-8")
    acc_with_ext, _ = _write_live_preconditions(
        h3, env["tmp"], manifest=env["manifest"], src_tree=_src_tree(),
        terms_db=env["db"], annotations=env["annotations"],
        upstream_clearances=clearances, community_lexicon=lexicon,
    )
    pf_missing_ext = run_preflight(acc_with_ext, judges)
    assert pf_missing_ext["approval_tokens"]["full"].startswith("unavailable")
    assert "upstream_clearances" in pf_missing_ext["approval_tokens"]["full"]
    pf_ok_ext = run_preflight(acc_with_ext, judges, clearances, lexicon)
    assert re.fullmatch(r"h3-live-[0-9a-f]{24}", pf_ok_ext["approval_tokens"]["full"])
    assert pf_ok_ext["preconditions"]["acceptance_bindings_sha256"] == json.loads(
        acc_with_ext.read_text(encoding="utf-8")
    )["bindings_sha256"]

    # 9. Valid acceptance + valid judges via --acceptance-receipt and --judges flags -> issues h3-live-... token.
    good_acc, judges = _write_live_preconditions(
        h3, env["tmp"], manifest=env["manifest"], src_tree=_src_tree(),
        terms_db=env["db"], annotations=env["annotations"], verdict="pass",
        matcher_revision=sys.modules["wuwaterm.cjk_span"].MATCHER_REVISION,
    )
    pf_ok = run_preflight(good_acc, judges)
    token = pf_ok["approval_tokens"]["full"]
    assert re.fullmatch(r"h3-live-[0-9a-f]{24}", token), token
    assert pf_ok["approval_tokens"]["ablation"] == token
    pf_flags_out = env["tmp"] / "pf_flags.json"
    assert h3.main(
        ["preflight", *pinned_args, *env["freeze"],
         "--acceptance-receipt", str(good_acc), "--judges", str(judges),
         "--manifest", str(env["manifest"]), "--json-out", str(pf_flags_out)]
    ) == 0
    assert json.loads(pf_flags_out.read_text(encoding="utf-8"))["approval_tokens"]["full"] == token
    assert no_network == []


def test_proxy_refused_in_fake_mode_and_mounted_in_live_without_leaking_url(h3, env, capsys, monkeypatch, no_network):
    secret_proxy = "http://proxyuser:super-secret-pass@proxy.internal.invalid:8443"
    # 1. --proxy combined with --fake-transport is refused (exit 1), and unsupported schemes (socks5) are refused.
    code, err = _run(
        h3, env["args"]("pilot", "raw_baseline", "--fake-transport", "--proxy", secret_proxy), capsys
    )
    assert code == 1 and "--proxy" in err and "super-secret-pass" not in err
    code_socks, err_socks = _run(
        h3,
        env["args"](
            "full", "raw_baseline", "--live", "--owner-approval-token", "h3-live-000000000000000000000000",
            "--ledger-budget-usd", "5.0", "--price-input-per-1m", "0.5", "--price-output-per-1m", "2.0",
            "--proxy", "socks5://proxy.internal.invalid:1080",
        ),
        capsys,
    )
    assert code_socks == 1 and "scheme" in err_socks and "proxy.internal.invalid" not in err_socks

    # 2. Live path mounts AsyncHTTPTransport(proxy=..., trust_env=False) and never writes URL/creds to ledger.
    monkeypatch.setattr(h3, "PINNED_SRC_TREE", _src_tree())
    monkeypatch.setattr(h3, "PINNED_TERMS_DB_SHA256", _sha256(env["db"]))
    monkeypatch.setattr(h3, "PINNED_MATCHER_REVISION", sys.modules["wuwaterm.cjk_span"].MATCHER_REVISION)
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://live-gateway.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "live-test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", h3.PINNED_MODEL)
    monkeypatch.setenv("HTTPS_PROXY", "http://unrequested-env-proxy.invalid:9999")

    two_docs = env["tmp"] / "live_two_docs.json"
    two_docs.write_text(
        json.dumps(
            {"schema": "h3-manifest-v1", "set": "H3-live-proxy", "documents": [DOCS[0], DOCS[2]]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    good_acc, judges = _write_live_preconditions(
        h3, env["tmp"], manifest=two_docs, src_tree=_src_tree(),
        terms_db=env["db"], annotations=env["annotations"], verdict="pass",
    )
    pinned_args = ["--bound-tree", str(ROOT), "--terms-db", str(env["db"])]
    freeze_all = [*env["freeze"], "--freeze", str(good_acc), "--freeze", str(judges)]
    pf_out = env["tmp"] / "pf_live_proxy.json"
    assert h3.main(["preflight", *pinned_args, *freeze_all, "--manifest", str(two_docs), "--json-out", str(pf_out)]) == 0
    token = json.loads(pf_out.read_text(encoding="utf-8"))["approval_tokens"]["full"]
    assert token.startswith("h3-live-")

    captured_transport_kwargs: list[dict] = []
    captured_payloads: list[bytes] = []
    real_async_transport = httpx.AsyncHTTPTransport

    def spy_transport(*args, **kwargs):
        captured_transport_kwargs.append(dict(kwargs))
        # Validate httpx accepts the exact arguments (proxy + trust_env=False) without making a socket call.
        real = real_async_transport(*args, **kwargs)
        assert real is not None
        fake_gw = h3.FakeGateway("raw_baseline", [])

        def handler(request: httpx.Request) -> httpx.Response:
            captured_payloads.append(bytes(request.content))
            return fake_gw(request)

        return httpx.MockTransport(handler)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", spy_transport)

    # Run 1 (with --proxy)
    run_dir_proxy = env["tmp"] / "live_run_proxy"
    code, summary = _run(
        h3,
        ["run", *pinned_args, *freeze_all, "--run-dir", str(run_dir_proxy), "--manifest", str(two_docs),
         "--stage", "full", "--arm", "raw_baseline", "--direction", "zh->en", "--max-calls", "5",
         "--live", "--owner-approval-token", token,
         "--ledger-budget-usd", "5.0", "--price-input-per-1m", "0.5", "--price-output-per-1m", "2.0",
         "--proxy", secret_proxy],
        capsys,
    )
    assert code == 0, summary
    assert summary["proxy_enabled"] is True
    assert captured_transport_kwargs == [{"proxy": secret_proxy, "trust_env": False}]
    ledger_text = (run_dir_proxy / "h3-ledger.json").read_text(encoding="utf-8")
    log_text = (run_dir_proxy / "h3-ledger-log.jsonl").read_text(encoding="utf-8")
    ledger = json.loads(ledger_text)
    assert ledger["proxy_enabled"] is True
    assert ledger["records"][-1]["proxy_enabled"] is True
    assert "super-secret-pass" not in ledger_text and "proxy.internal.invalid" not in ledger_text
    assert "super-secret-pass" not in log_text and "proxy.internal.invalid" not in log_text

    # Run 2 (without --proxy, different budget to prove token excludes budget and proxy; payload bytes identical)
    run_dir_direct = env["tmp"] / "live_run_direct"
    code2, summary2 = _run(
        h3,
        ["run", *pinned_args, *freeze_all, "--run-dir", str(run_dir_direct), "--manifest", str(two_docs),
         "--stage", "full", "--arm", "raw_baseline", "--direction", "zh->en", "--max-calls", "5",
         "--live", "--owner-approval-token", token,
         "--max-cost-usd", "9.5", "--price-input-per-1m", "0.5", "--price-output-per-1m", "2.0"],
        capsys,
    )
    assert code2 == 0, summary2
    assert summary2["proxy_enabled"] is False
    assert captured_transport_kwargs[-1] == {"proxy": None, "trust_env": False}
    assert json.loads((run_dir_direct / "h3-ledger.json").read_text(encoding="utf-8"))["proxy_enabled"] is False
    # Wire request bytes sent to the gateway are unchanged by budget/proxy settings.
    assert len(captured_payloads) == 2 and captured_payloads[0] == captured_payloads[1]
    assert no_network == []


def test_pilot_stage_live_path_with_full_manifest_acceptance_receipt(h3, env, capsys, monkeypatch, no_network):
    """Stage 1 pilot (separate 8-doc diagnostic slice) passes preflight and live runs
    using the H3 full manifest's h3-acceptance-v1 receipt and h3-judges-v1 roster,
    sharing one h3-ledger.json across pilot, full, and ablation.
    """
    monkeypatch.setattr(h3, "PINNED_SRC_TREE", _src_tree())
    monkeypatch.setattr(h3, "PINNED_TERMS_DB_SHA256", _sha256(env["db"]))
    monkeypatch.setattr(h3, "PINNED_MATCHER_REVISION", sys.modules["wuwaterm.cjk_span"].MATCHER_REVISION)
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://live-gateway.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "live-test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", h3.PINNED_MODEL)

    # Separate 8-doc pilot diagnostic manifest (distinct from the full H3 manifest).
    pilot_docs = [
        {"doc_id": f"d_zh_{i}", "direction": "zh->en", "genre": "guide", "text": f"诊断中文段落 {i}。"}
        for i in range(4)
    ] + [
        {"doc_id": f"d_en_{i}", "direction": "en->zh", "genre": "guide", "text": f"Diagnostic English line {i}."}
        for i in range(4)
    ]
    pilot_manifest = env["tmp"] / "pilot_manifest.json"
    pilot_manifest.write_text(
        json.dumps({"schema": "h3-manifest-v1", "set": "D-pilot-8", "documents": pilot_docs}, ensure_ascii=False),
        encoding="utf-8",
    )
    full_manifest = env["manifest"]
    assert _sha256(pilot_manifest) != _sha256(full_manifest)

    # Receipt accepts ONLY the full H3 manifest (no pilot_manifest field, matching eval_h3_corpus).
    good_acc, judges = _write_live_preconditions(
        h3, env["tmp"], manifest=full_manifest, src_tree=_src_tree(),
        terms_db=env["db"], annotations=env["annotations"], verdict="pass",
    )
    # Pin manifest_sha256 on judges.json to the full H3 manifest too.
    judges_data = json.loads(judges.read_text(encoding="utf-8"))
    judges_data["manifest_sha256"] = _sha256(full_manifest)
    judges.write_text(json.dumps(judges_data, ensure_ascii=False), encoding="utf-8")

    pinned_args = ["--bound-tree", str(ROOT), "--terms-db", str(env["db"])]
    freeze_all = [*env["freeze"], "--freeze", str(good_acc), "--freeze", str(judges)]

    # Preflight with --pilot-manifest issues valid tokens for pilot, full, and ablation.
    pf_out = env["tmp"] / "pf_pilot_live.json"
    code = h3.main(
        ["preflight", *pinned_args, *freeze_all, "--manifest", str(full_manifest),
         "--pilot-manifest", str(pilot_manifest), "--json-out", str(pf_out)]
    )
    assert code == 0
    pf = json.loads(pf_out.read_text(encoding="utf-8"))
    pilot_tok = pf["approval_tokens"]["pilot"]
    full_tok = pf["approval_tokens"]["full"]
    ablation_tok = pf["approval_tokens"]["ablation"]
    assert re.fullmatch(r"h3-live-[0-9a-f]{24}", pilot_tok), pilot_tok
    assert re.fullmatch(r"h3-live-[0-9a-f]{24}", full_tok), full_tok
    assert ablation_tok == full_tok
    assert pilot_tok != full_tok

    # Verify missing context_id in h3-judges-v1 is rejected.
    bad_judges = env["tmp"] / "judges_no_ctx.json"
    bad_judges_data = json.loads(judges.read_text(encoding="utf-8"))
    del bad_judges_data["judges"]["j2"]["context_id"]
    bad_judges.write_text(json.dumps(bad_judges_data, ensure_ascii=False), encoding="utf-8")
    pf_bad_j = env["tmp"] / "pf_bad_j.json"
    assert h3.main(
        ["preflight", *pinned_args, *env["freeze"], "--freeze", str(good_acc), "--freeze", str(bad_judges),
         "--manifest", str(full_manifest), "--pilot-manifest", str(pilot_manifest), "--json-out", str(pf_bad_j)]
    ) == 0
    assert json.loads(pf_bad_j.read_text(encoding="utf-8"))["approval_tokens"]["pilot"].startswith("unavailable")

    # Monkeypatch AsyncHTTPTransport to in-process FakeGateway so live path runs with 0 network.
    monkeypatch.setattr(
        httpx,
        "AsyncHTTPTransport",
        lambda *a, **kw: httpx.MockTransport(h3.FakeGateway("wuwaterm_prod", [])),
    )
    live_dir = env["tmp"] / "live_all_stages"
    cost_flags = ["--ledger-budget-usd", "10.0", "--price-input-per-1m", "0.5", "--price-output-per-1m", "2.0"]

    # Stage 1 pilot (without --full-manifest, then with --full-manifest) succeeds and binds pilot_manifest_sha256.
    code, p_prod = _run(
        h3,
        ["run", *pinned_args, *freeze_all, "--run-dir", str(live_dir), "--manifest", str(pilot_manifest),
         "--stage", "pilot", "--arm", "wuwaterm_prod", "--max-calls", "56",
         "--live", "--owner-approval-token", pilot_tok, *cost_flags],
        capsys,
    )
    assert code == 0, p_prod
    code, p_raw = _run(
        h3,
        ["run", *pinned_args, *freeze_all, "--run-dir", str(live_dir), "--manifest", str(pilot_manifest),
         "--full-manifest", str(full_manifest),
         "--stage", "pilot", "--arm", "raw_baseline", "--max-calls", "56",
         "--live", "--owner-approval-token", pilot_tok, *cost_flags],
        capsys,
    )
    assert code == 0, p_raw
    code, p_matched = _run(
        h3,
        ["run", *pinned_args, *freeze_all, "--run-dir", str(live_dir), "--manifest", str(pilot_manifest),
         "--stage", "pilot", "--arm", "raw_matched", "--max-calls", "56",
         "--live", "--owner-approval-token", pilot_tok, *cost_flags],
        capsys,
    )
    assert code == 0, p_matched

    # Stage 2 full and Stage 3 ablation on the SAME h3-ledger.json with --pilot-manifest pass cleanly.
    for stage, arm, tok in (
        ("full", "wuwaterm_prod", full_tok),
        ("full", "raw_baseline", full_tok),
        ("ablation", "raw_matched", ablation_tok),
    ):
        code, res = _run(
            h3,
            ["run", *pinned_args, *freeze_all, "--run-dir", str(live_dir), "--manifest", str(full_manifest),
             "--pilot-manifest", str(pilot_manifest),
             "--stage", stage, "--arm", arm, "--max-calls", "240",
             "--live", "--owner-approval-token", tok, *cost_flags],
            capsys,
        )
        assert code == 0, (stage, arm, res)

    ledger = json.loads((live_dir / "h3-ledger.json").read_text(encoding="utf-8"))
    assert ledger["mode"] == "live"
    assert ledger["bindings"]["pilot_manifest_sha256"] == _sha256(pilot_manifest)
    assert len(ledger["records"]) == 6
    assert ledger["records"][0]["preconditions"]["acceptance_bindings_sha256"] == json.loads(
        good_acc.read_text(encoding="utf-8")
    )["bindings_sha256"]
    assert ledger["records"][0]["preconditions"]["judges_sha256"] == _sha256(judges)

    # Negative check: mismatched --full-manifest on pilot is refused (exit 1).
    code_bad, err_bad = _run(
        h3,
        ["run", *pinned_args, *freeze_all, "--run-dir", str(env["tmp"] / "bad_pilot"), "--manifest", str(pilot_manifest),
         "--full-manifest", str(pilot_manifest),
         "--stage", "pilot", "--arm", "raw_baseline", "--max-calls", "56",
         "--live", "--owner-approval-token", pilot_tok, *cost_flags],
        capsys,
    )
    assert code_bad == 1 and "manifest_sha256" in err_bad
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
    for rel in (
        "scripts/eval_h3.py",
        "scripts/eval_h3_corpus.py",
        "scripts/eval_h3_tree_diff.py",
        "scripts/eval_h3_export.py",
        "scripts/eval_usability.py",
        "scripts/eval_semantic.py",
        "docs/eval/usability-judging.md",
        "docs/eval/h3-judging-addendum.md",
    ):
        recorded = re.findall(rf"`{re.escape(rel)}` SHA-256 `([0-9a-f]{{64}})`", text)
        assert recorded, rel
        assert set(recorded) == {_sha256(ROOT / rel)}, rel
