"""Tests for scripts/eval_h3_corpus.py (H3 corpus acceptance gate C1-C10).

All fixtures are 100% synthetic text. Socket connects and DNS lookups are
blocked in every test so nothing can touch the network.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = ROOT / "scripts" / "eval_h3_corpus.py"
SCORER_SCRIPT = ROOT / "scripts" / "eval_usability.py"
FIXTURES_DIR = ROOT / "tests" / "fixtures" / "h3-corpus"


def _load_gate():
    spec = importlib.util.spec_from_file_location("eval_h3_corpus", GATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_h3_corpus"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    try:
        yield _load_gate()
    finally:
        sys.modules.pop("eval_h3_corpus", None)
        sys.modules.pop("_h3_corpus_eval_usability", None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _make_git_repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Synth Tester")
    _git(path, "config", "user.email", "synth@example.invalid")
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "synthetic snapshot")
    return _git(path, "rev-parse", "HEAD")


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sync_manifest_and_sample_hashes(case_dir: Path) -> None:
    """Recompute sample.json manifest_sha256 and per-paragraph source_sha256 after mutating fixtures."""
    manifest_path = case_dir / "manifest.json"
    sample_path = case_dir / "sample.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    sample_data = json.loads(sample_path.read_text(encoding="utf-8"))
    by_doc: dict[str, list[str]] = {}
    for para in sample_data["paragraphs"]:
        para["source_sha256"] = _sha256_text(para["source"])
        by_doc.setdefault(para["doc"], []).append(para["source"])
    for doc in manifest_data["documents"]:
        if doc["doc_id"] in by_doc:
            doc["text"] = "\n\n".join(by_doc[doc["doc_id"]])
    _write_json(manifest_path, manifest_data)
    sample_data["manifest_sha256"] = _sha256_file(manifest_path)
    _write_json(sample_path, sample_data)


@pytest.fixture()
def corpus_env(tmp_path: Path, sample_db: Path):
    """Copy the synthetic fixtures into tmp_path with a clean isolated Core git repo for C4."""
    case_dir = tmp_path / "corpus"
    shutil.copytree(FIXTURES_DIR, case_dir)
    core_repo = tmp_path / "core-repo"
    core_commit = _make_git_repo(
        core_repo,
        {
            "src/placeholder.py": "# synthetic clean core repo file without corpus overlap\nVALUE = 1\n",
            "docs/note.md": "Synthetic repository documentation line for isolated C4 scan.\n",
        },
    )
    src_tree = _git(ROOT, "rev-parse", "HEAD:src")
    from wuwaterm import cjk_span

    matcher_rev = str(cjk_span.MATCHER_REVISION)
    terms_sha = _sha256_file(sample_db)

    def build_argv(*extra: str, receipt_out: Path | None = None) -> list[str]:
        argv = [
            "accept",
            "--manifest",
            str(case_dir / "manifest.json"),
            "--sample",
            str(case_dir / "sample.json"),
            "--names",
            str(case_dir / "names.json"),
            "--community-names",
            str(case_dir / "community_names.json"),
            "--metadata",
            str(case_dir / "metadata.json"),
            "--prior-texts",
            str(case_dir / "prior_texts.json"),
            "--prior-ids",
            str(case_dir / "prior_ids.json"),
            "--clear-names",
            str(case_dir / "clear_names.txt"),
            "--upstream-clearances",
            str(case_dir / "upstream_clearances.json"),
            "--bound-tree",
            str(ROOT),
            "--core-repo",
            str(core_repo),
            "--core-commit",
            core_commit,
            "--terms-db",
            str(sample_db),
            "--expected-core-commit",
            core_commit,
            "--expected-src-tree",
            src_tree,
            "--expected-matcher-revision",
            matcher_rev,
            "--expected-terms-sha256",
            terms_sha,
        ]
        if receipt_out is not None:
            argv.extend(["--json-out", str(receipt_out)])
        argv.extend(extra)
        return argv

    return {
        "case_dir": case_dir,
        "core_repo": core_repo,
        "core_commit": core_commit,
        "src_tree": src_tree,
        "matcher_rev": matcher_rev,
        "terms_db": sample_db,
        "terms_sha": terms_sha,
        "build_argv": build_argv,
        "tmp": tmp_path,
    }


def _run_gate(gate: Any, argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any], str, str]:
    code = gate.main(argv)
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    return code, receipt, captured.out, captured.err


def _assert_no_text_leak(out: str, err: str, receipt_path: Path | None, forbidden: list[str]) -> None:
    blobs = [out, err]
    if receipt_path is not None and receipt_path.is_file():
        blobs.append(receipt_path.read_text(encoding="utf-8"))
    combined = "\n".join(blobs)
    for secret in forbidden:
        assert secret not in combined, f"corpus text leaked into gate output: {secret!r}"


# ------------------------------------------------------------ full baseline pass


def test_accept_baseline_pass_and_zero_text_leak(gate, corpus_env, capsys):
    receipt_path = corpus_env["tmp"] / "receipt.json"
    code, receipt, out, err = _run_gate(gate, corpus_env["build_argv"](receipt_out=receipt_path), capsys)
    assert code == 0, receipt
    assert receipt["schema"] == "h3-acceptance-v1"
    assert receipt["verdict"] == "pass"
    assert receipt["failed_rules"] == []
    assert set(receipt["checks"]) == {"C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10"}
    for rule, block in receipt["checks"].items():
        assert block["status"] == "pass", (rule, block)
    assert receipt["checks"]["C3"]["floor_profile"] == "default"
    assert receipt["checks"]["C3"]["non_default_floor"] is False
    assert receipt["checks"]["C4"]["windows_uncleared"] == 0
    assert receipt["checks"]["C4"]["scorer_sha256_ok"] is True

    # Verify validate_acceptance_receipt and CLI verify-receipt accept it.
    validated = gate.validate_acceptance_receipt(
        receipt,
        expected_manifest_sha256=_sha256_file(corpus_env["case_dir"] / "manifest.json"),
        expected_sample_sha256=_sha256_file(corpus_env["case_dir"] / "sample.json"),
        expected_names_sha256=_sha256_file(corpus_env["case_dir"] / "names.json"),
        expected_community_names_sha256=_sha256_file(corpus_env["case_dir"] / "community_names.json"),
        expected_metadata_sha256=_sha256_file(corpus_env["case_dir"] / "metadata.json"),
        expected_src_tree=corpus_env["src_tree"],
        expected_core_commit=corpus_env["core_commit"],
        expected_terms_sha256=corpus_env["terms_sha"],
        expected_matcher_revision=corpus_env["matcher_rev"],
    )
    assert validated["verdict"] == "pass"

    verify_rc = gate.main(["verify-receipt", "--receipt", str(receipt_path)])
    verify_cap = capsys.readouterr()
    assert verify_rc == 0, verify_cap.err

    # Assert zero corpus text leakage (source, reference, surfaces, prior text).
    sample = json.loads((corpus_env["case_dir"] / "sample.json").read_text(encoding="utf-8"))
    forbidden = [
        "合成甲标九",
        "SYNTHCANARYEN",
        "SYNREFZH001",
        "合成参考译文乙",
        "Shorekeeper",
        "Jinhsi",
        "quickswap",
        "主C",
        "Prior held-out one synthetic archive",
        sample["paragraphs"][0]["source"],
        sample["paragraphs"][10]["source"],
    ]
    _assert_no_text_leak(out, err, receipt_path, forbidden)


def test_accept_against_real_v0_14_0_commit_blobs(gate, corpus_env, capsys):
    """Fixture corpus has zero uncleared 12-scalar overlap against real Core v0.14.0 tracked blobs."""
    v14_commit = gate.PINNED_CORE_COMMIT
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", f"{v14_commit}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("v0.14.0 commit not present in shallow clone")
    argv = [
        "accept",
        "--manifest",
        str(corpus_env["case_dir"] / "manifest.json"),
        "--sample",
        str(corpus_env["case_dir"] / "sample.json"),
        "--names",
        str(corpus_env["case_dir"] / "names.json"),
        "--community-names",
        str(corpus_env["case_dir"] / "community_names.json"),
        "--metadata",
        str(corpus_env["case_dir"] / "metadata.json"),
        "--prior-texts",
        str(corpus_env["case_dir"] / "prior_texts.json"),
        "--prior-ids",
        str(corpus_env["case_dir"] / "prior_ids.json"),
        "--clear-names",
        str(corpus_env["case_dir"] / "clear_names.txt"),
        "--upstream-clearances",
        str(corpus_env["case_dir"] / "upstream_clearances.json"),
        "--bound-tree",
        str(ROOT),
        "--core-repo",
        str(ROOT),
        "--core-commit",
        v14_commit,
        "--terms-db",
        str(corpus_env["terms_db"]),
        "--expected-core-commit",
        v14_commit,
        "--expected-src-tree",
        corpus_env["src_tree"],
        "--expected-matcher-revision",
        corpus_env["matcher_rev"],
        "--expected-terms-sha256",
        corpus_env["terms_sha"],
    ]
    code, receipt, out, err = _run_gate(gate, argv, capsys)
    assert code == 0, receipt["checks"]["C4"]
    assert receipt["checks"]["C4"]["core_blobs_scanned"] > 100
    assert receipt["checks"]["C4"]["windows_uncleared"] == 0
    _assert_no_text_leak(out, err, None, ["合成甲标九", "SYNTHCANARYEN"])


# ------------------------------------------------------------ C1 fail cases


def test_c1_fail_manifest_sample_mismatch(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    # Mutate genre and text hash on first paragraph without updating manifest.
    sample["paragraphs"][0]["genre"] = "story"
    sample["paragraphs"][0]["source"] = sample["paragraphs"][0]["source"] + "额外篡改"
    _write_json(sample_path, sample)

    code, receipt, out, err = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert receipt["verdict"] == "fail"
    assert "C1" in receipt["failed_rules"]
    codes = {issue["code"] for issue in receipt["checks"]["C1"]["issues"]}
    assert "genre_mismatch" in codes
    assert "paragraph_text_hash_mismatch" in codes
    _assert_no_text_leak(out, err, None, ["额外篡改", "合成甲标九"])


def test_c1_fail_manifest_sha256_mismatch_and_doc_id_mismatch(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    sample["manifest_sha256"] = "0" * 64
    sample["paragraphs"][0]["doc"] = "nonexistent-doc-99"
    _write_json(sample_path, sample)

    code, receipt, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert "C1" in receipt["failed_rules"]
    codes = {issue["code"] for issue in receipt["checks"]["C1"]["issues"]}
    assert "manifest_sha256_mismatch" in codes
    assert "doc_missing_in_manifest" in codes


# ------------------------------------------------------------ C2 fail cases


@pytest.mark.parametrize(
    ("mutation", "expected_computed"),
    [
        ("dict_marked_scored", "dictionary"),
        ("no_letter_marked_scored", "no_letter"),
        ("scored_marked_dict", None),
        ("duplicate_marked_scored", "duplicate"),
    ],
)
def test_c2_fail_exclusion_label_recomputation(gate, corpus_env, capsys, mutation, expected_computed):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    names_path = case_dir / "names.json"
    comm_path = case_dir / "community_names.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    names = json.loads(names_path.read_text(encoding="utf-8"))
    comm = json.loads(comm_path.read_text(encoding="utf-8"))

    if mutation == "dict_marked_scored":
        target = next(p for p in sample["paragraphs"] if p["exclusion"] == "dictionary")
        target["scored"] = True
        target["exclusion"] = None
        names["annotated_paragraphs"].append(target["id"])
        comm["annotated_paragraphs"].append(target["id"])
    elif mutation == "no_letter_marked_scored":
        target = next(p for p in sample["paragraphs"] if p["exclusion"] == "no_letter")
        target["scored"] = True
        target["exclusion"] = None
        names["annotated_paragraphs"].append(target["id"])
        comm["annotated_paragraphs"].append(target["id"])
    elif mutation == "scored_marked_dict":
        target = sample["paragraphs"][2]
        assert target["scored"] is True
        target["scored"] = False
        target["exclusion"] = "dictionary"
        names["annotated_paragraphs"].remove(target["id"])
        comm["annotated_paragraphs"].remove(target["id"])
    elif mutation == "duplicate_marked_scored":
        # Make paragraph 4 identical to paragraph 3 (both scored in same direction).
        sample["paragraphs"][4]["source"] = sample["paragraphs"][3]["source"]

    _write_json(sample_path, sample)
    _write_json(names_path, names)
    _write_json(comm_path, comm)
    _sync_manifest_and_sample_hashes(case_dir)

    code, receipt, out, err = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert "C2" in receipt["failed_rules"]
    mismatches = receipt["checks"]["C2"]["mismatches"]
    assert any(m["computed_exclusion"] == expected_computed for m in mismatches)
    _assert_no_text_leak(out, err, None, ["合成甲标九", "SYNTHCANARYEN", "Shorekeeper"])


# ------------------------------------------------------------ C3 pass/fail & override


def test_c3_fail_default_and_pass_with_announcement_floor_override(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    names_path = case_dir / "names.json"
    comm_path = case_dir / "community_names.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    names = json.loads(names_path.read_text(encoding="utf-8"))
    comm = json.loads(comm_path.read_text(encoding="utf-8"))

    # Reduce zh->en announcement from 30 to 25 and add 5 extra zh->en story units so total stays 140.
    removed_ids: set[str] = set()
    kept = []
    ann_removed = 0
    for para in sample["paragraphs"]:
        if para["direction"] == "zh-en" and para["genre"] == "announcement" and para["scored"] and ann_removed < 5:
            # Keep p00 (which has annotations), remove later paragraphs.
            if not para["id"].endswith("-p00"):
                removed_ids.add(para["id"])
                ann_removed += 1
                continue
        kept.append(para)
    assert ann_removed == 5

    story_doc = "syn-zh-sto-02"
    existing_story = sum(1 for p in kept if p["doc"] == story_doc)
    for k in range(5):
        pid = f"{story_doc}-p{existing_story + k:02d}"
        src = f"补录剧情甲{k + 1:02d}：秋岚漫过远峰并映照素帆。"
        kept.append(
            {
                "id": pid,
                "doc": story_doc,
                "direction": "zh-en",
                "genre": "story",
                "scored": True,
                "exclusion": None,
                "source": src,
                "reference": None,
                "source_sha256": _sha256_text(src),
            }
        )
        names["annotated_paragraphs"].append(pid)
        comm["annotated_paragraphs"].append(pid)

    sample["paragraphs"] = kept
    names["annotated_paragraphs"] = [p for p in names["annotated_paragraphs"] if p not in removed_ids]
    comm["annotated_paragraphs"] = [p for p in comm["annotated_paragraphs"] if p not in removed_ids]
    _write_json(sample_path, sample)
    _write_json(names_path, names)
    _write_json(comm_path, comm)
    _sync_manifest_and_sample_hashes(case_dir)

    # Default mode must strictly fail C3 (announcement 25 < 30).
    code_def, receipt_def, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_def == 1
    assert receipt_def["failed_rules"] == ["C3"]
    assert receipt_def["checks"]["C3"]["floor_profile"] == "default"
    assert receipt_def["checks"]["C3"]["non_default_floor"] is False

    # Explicit --announcement-floor 25 passes C3 and records non-default floor_profile.
    code_ovr, receipt_ovr, _, _ = _run_gate(
        gate, corpus_env["build_argv"]("--announcement-floor", "25"), capsys
    )
    assert code_ovr == 0, receipt_ovr
    assert receipt_ovr["verdict"] == "pass"
    assert receipt_ovr["checks"]["C3"]["non_default_floor"] is True
    assert receipt_ovr["checks"]["C3"]["floor_profile"] == "custom:announcement=25"

    # validate_acceptance_receipt rejects non-default floor unless allow_non_default_floor=True.
    with pytest.raises(gate.ReceiptValidationError, match="non-default"):
        gate.validate_acceptance_receipt(receipt_ovr)
    gate.validate_acceptance_receipt(receipt_ovr, allow_non_default_floor=True)


def test_c3_fail_zh_en_guide_below_50(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    names_path = case_dir / "names.json"
    comm_path = case_dir / "community_names.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    names = json.loads(names_path.read_text(encoding="utf-8"))
    comm = json.loads(comm_path.read_text(encoding="utf-8"))

    # Remove 1 zh->en guide scored paragraph (49 < 50).
    victim = next(
        p["id"]
        for p in sample["paragraphs"]
        if p["direction"] == "zh-en" and p["genre"] == "guide" and p["scored"] and not p["id"].endswith("-p00")
    )
    sample["paragraphs"] = [p for p in sample["paragraphs"] if p["id"] != victim]
    names["annotated_paragraphs"] = [p for p in names["annotated_paragraphs"] if p != victim]
    comm["annotated_paragraphs"] = [p for p in comm["annotated_paragraphs"] if p != victim]
    _write_json(sample_path, sample)
    _write_json(names_path, names)
    _write_json(comm_path, comm)
    _sync_manifest_and_sample_hashes(case_dir)

    code, receipt, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert "C3" in receipt["failed_rules"]
    assert any(
        s.get("direction") == "zh->en" and s.get("genre") == "guide"
        for s in receipt["checks"]["C3"]["shortfalls"]
    )


# ------------------------------------------------------------ C4 pass/fail (scorer SHA, prior, core blob, M1/M2/M3)


def test_c4_fail_tampered_eval_usability_sha256(gate, corpus_env, capsys):
    fake_scorer = corpus_env["tmp"] / "tampered_eval_usability.py"
    fake_scorer.write_text("raise RuntimeError('must never execute tampered scorer')\n", encoding="utf-8")

    code, receipt, _, _ = _run_gate(
        gate,
        corpus_env["build_argv"]("--scorer-path", str(fake_scorer)),
        capsys,
    )
    assert code == 1
    assert "C4" in receipt["failed_rules"]
    assert "C10" in receipt["failed_rules"]
    assert receipt["checks"]["C4"]["scorer_sha256_ok"] is False


def test_c4_fail_prior_text_and_core_blob_overlap_without_text_leak(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    # Use a Chinese paragraph containing a >=2-char dictionary surface (今汐) and digits
    # so M2 cannot clear it (both len >= 2 dict surface and Nd digit block M2).
    sample = json.loads((case_dir / "sample.json").read_text(encoding="utf-8"))
    zh_source = sample["paragraphs"][0]["source"]
    assert "今汐" in zh_source
    prior_path = case_dir / "prior_texts.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    prior["sets"][0]["texts"].append(f"前缀：{zh_source}后缀")
    _write_json(prior_path, prior)

    code, receipt, out, err = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert "C4" in receipt["failed_rules"]
    c4 = receipt["checks"]["C4"]
    assert c4["windows_uncleared"] > 0
    assert c4["max_shared_span_length"] >= len(zh_source)
    _assert_no_text_leak(out, err, None, [zh_source, zh_source[:12]])

    # Also test overlap against a tracked blob in the Core commit.
    _write_json(prior_path, json.loads((FIXTURES_DIR / "prior_texts.json").read_text(encoding="utf-8")))
    dirty_repo = corpus_env["tmp"] / "core-overlap"
    overlap_commit = _make_git_repo(dirty_repo, {"notes/leak.txt": f"core tracked blob: {zh_source}\n"})
    code2, receipt2, out2, err2 = _run_gate(
        gate,
        corpus_env["build_argv"](
            "--core-repo",
            str(dirty_repo),
            "--core-commit",
            overlap_commit,
            "--expected-core-commit",
            overlap_commit,
        ),
        capsys,
    )
    assert code2 == 1
    assert "C4" in receipt2["failed_rules"]
    assert receipt2["checks"]["C4"]["windows_uncleared"] > 0
    assert any("core_tree" in u["targets"] for u in receipt2["checks"]["C4"]["uncleared_windows"])
    _assert_no_text_leak(out2, err2, None, [zh_source, zh_source[:12]])


def test_c4_mechanical_clearances_m1_m2_m3_and_m2_rejections(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    prior_path = case_dir / "prior_texts.json"
    clear_names_path = case_dir / "clear_names.txt"
    upstream_path = case_dir / "upstream_clearances.json"

    base_rc, base_receipt, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert base_rc == 0
    base_m2 = base_receipt["checks"]["C4"]["cleared_counts"]["M2"]

    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    en_para = next(p for p in sample["paragraphs"] if p["direction"] == "en-zh" and p["scored"] and not p["id"].endswith("-p00"))
    zh_para = next(p for p in sample["paragraphs"] if p["direction"] == "zh-en" and p["scored"] and not p["id"].endswith("-p00"))

    # 1) M1 clearance: 16-char official name in both H3 and prior_texts, declared in clear_names.txt.
    m1_phrase = "Vanguard Junrock"
    en_para["source"] = en_para["source"] + f" sighting({m1_phrase})nearby."
    # 2) M2 clearance: 18-char generic phrase (< T=24, no Nd digits, no len>=2 dict surface).
    #    Also put its windows in upstream_clearances later to verify M2 runs BEFORE M3 (M1 -> M2 -> M3).
    m2_phrase = "under gentle winds"
    en_para["source"] = en_para["source"] + f" Travel({m2_phrase})today."
    # 3) M3 clearance: 15-char Chinese upstream phrase containing >=2-char dict surface ("共鸣者")
    #    so M2 rejects it and M3 (upstream_data) must clear it.
    m3_phrase = "上游共鸣者模板专用片段甲乙丙丁"
    assert len(m3_phrase) == 15
    zh_para["source"] = zh_para["source"] + f"「{m3_phrase}」"

    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)

    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    prior["sets"][0]["texts"].extend(
        [
            f"alpha[{m1_phrase}]omega",
            f"before[{m2_phrase}]after",
            f"【{m3_phrase}】",
        ]
    )
    _write_json(prior_path, prior)

    # Without M3 clearance file, the dict-containing Chinese phrase fails C4 (M2 refuses it).
    code_no_m3, receipt_no_m3, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_no_m3 == 1
    assert receipt_no_m3["failed_rules"] == ["C4"]
    assert receipt_no_m3["checks"]["C4"]["cleared_counts"]["M1"] == len(m1_phrase) - 12 + 1
    assert receipt_no_m3["checks"]["C4"]["cleared_counts"]["M2"] - base_m2 == len(m2_phrase) - 12 + 1
    assert receipt_no_m3["checks"]["C4"]["windows_uncleared"] == len(m3_phrase) - 12 + 1

    # With valid M3 upstream_data clearances (including m2_phrase windows too to assert M2 precedes M3),
    # M1 -> M2 -> M3 order credits m2_phrase to M2 and m3_phrase to M3.
    m3_entries = [
        {
            "classification": "upstream_data",
            "window_sha256": _sha256_text(m3_phrase[i : i + 12]),
            "source_ref": "TextMap/zh-Hans/Synthetic.json:Key_01",
        }
        for i in range(len(m3_phrase) - 12 + 1)
    ] + [
        {
            "classification": "upstream_data",
            "window_sha256": _sha256_text(m2_phrase[i : i + 12]),
            "source_ref": "TextMap/en/Synthetic.json:OrderCheck",
        }
        for i in range(len(m2_phrase) - 12 + 1)
    ]
    _write_json(upstream_path, {"schema": "h3-upstream-clearances-v1", "entries": m3_entries})

    code_ok, receipt_ok, out_ok, err_ok = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_ok == 0, receipt_ok["checks"]["C4"]
    c4 = receipt_ok["checks"]["C4"]
    assert c4["cleared_counts"]["M1"] == len(m1_phrase) - 12 + 1
    assert c4["cleared_counts"]["M2"] - base_m2 == len(m2_phrase) - 12 + 1
    assert c4["cleared_counts"]["M3"] == len(m3_phrase) - 12 + 1
    assert c4["windows_uncleared"] == 0
    _assert_no_text_leak(out_ok, err_ok, None, [m1_phrase, m2_phrase, m3_phrase])

    # Non-upstream_data classification in M3 is rejected.
    bad_entries = [{**m3_entries[0], "classification": "manual_waiver"}]
    _write_json(upstream_path, {"schema": "h3-upstream-clearances-v1", "entries": bad_entries})
    code_bad_m3, receipt_bad_m3, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_bad_m3 == 1
    assert "C4" in receipt_bad_m3["failed_rules"]
    _write_json(upstream_path, {"schema": "h3-upstream-clearances-v1", "entries": m3_entries})

    # M2 rejection 1: Unicode Nd digit inside shared phrase.
    digit_phrase = "clear phase 2 now"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    en_p2 = [p for p in sample["paragraphs"] if p["direction"] == "en-zh" and p["scored"] and not p["id"].endswith("-p00")][1]
    orig_en_p2 = en_p2["source"]
    en_p2["source"] = orig_en_p2 + f" Please({digit_phrase})fast."
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)
    prior["sets"][1]["texts"].append(f"note[{digit_phrase}]!")
    _write_json(prior_path, prior)
    code_dig, receipt_dig, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_dig == 1
    assert "C4" in receipt_dig["failed_rules"]
    assert receipt_dig["checks"]["C4"]["windows_uncleared"] > 0

    # M2 rejection 2: dictionary surface (len >= 2, both en and zh) blocks M2.
    dict_phrase = "Autumntrace shines"
    en_p2["source"] = orig_en_p2 + f" Watch({dict_phrase})bright."
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)
    prior["sets"][1]["texts"] = [f"item[{dict_phrase}]!"]
    _write_json(prior_path, prior)
    code_dict, receipt_dict, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_dict == 1
    assert "C4" in receipt_dict["failed_rules"]
    assert receipt_dict["checks"]["C4"]["windows_uncleared"] > 0

    # M2 rejection 3: contiguous shared span length >= T (27 chars >= default 24);
    # raising --m2-max-shared-span 30 clears it and writes threshold to receipt.
    long_phrase = "walking quietly along paths"  # 27 chars >= 24
    assert len(long_phrase) == 27
    en_p2["source"] = orig_en_p2 + f" Keep({long_phrase})home."
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)
    prior["sets"][1]["texts"] = [f"start[{long_phrase}]end"]
    _write_json(prior_path, prior)
    code_long, receipt_long, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_long == 1
    assert "C4" in receipt_long["failed_rules"]
    assert receipt_long["checks"]["C4"]["max_shared_span_length"] >= 27
    assert receipt_long["checks"]["C4"]["m2_max_shared_span_threshold"] == 24

    code_t30, receipt_t30, _, _ = _run_gate(
        gate, corpus_env["build_argv"]("--m2-max-shared-span", "30"), capsys
    )
    assert code_t30 == 0, receipt_t30["checks"]["C4"]
    assert receipt_t30["checks"]["C4"]["m2_max_shared_span_threshold"] == 30
    assert clear_names_path.is_file()


# ------------------------------------------------------------ C5 fail cases


def test_c5_fail_normalized_duplicate_and_bilingual_pair_both_directions(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    meta_path = case_dir / "metadata.json"

    # 1) Normalized duplicate (NFKC + casefold + punctuation/whitespace stripped).
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    en_scored = [p for p in sample["paragraphs"] if p["direction"] == "en-zh" and p["scored"]]
    en_scored[3]["source"] = en_scored[2]["source"].upper().replace(" ", "   ") + "!!!"
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)

    code, receipt, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert "C5" in receipt["failed_rules"]
    assert any(i["code"] == "duplicate_paragraph" for i in receipt["checks"]["C5"]["issues"])

    # 2) Bilingual pair in both directions (via metadata bilingual_pair_id).
    shutil.copyfile(FIXTURES_DIR / "sample.json", sample_path)
    shutil.copyfile(FIXTURES_DIR / "manifest.json", case_dir / "manifest.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    zh_ann = next(d for d in meta["documents"] if d["doc_id"].startswith("syn-zh-ann"))
    en_ann = next(d for d in meta["documents"] if d["doc_id"].startswith("syn-en-ann"))
    en_ann["bilingual_pair_id"] = zh_ann["bilingual_pair_id"]
    _write_json(meta_path, meta)

    code2, receipt2, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code2 == 1
    assert receipt2["failed_rules"] == ["C5"]
    assert any(i["code"] == "bilingual_pair_both_directions" for i in receipt2["checks"]["C5"]["issues"])


# ------------------------------------------------------------ C6 fail cases


@pytest.mark.parametrize(
    ("meta_field", "prior_value"),
    [
        ("source_id", "prior-src-901"),
        ("textmap_keys", ["PriorTextMap_36_001"]),
        ("series", "prior-series-901"),
        ("author", "PRIOR-AUTHOR-901"),
    ],
)
def test_c6_fail_prior_id_overlap(gate, corpus_env, capsys, meta_field, prior_value):
    case_dir = corpus_env["case_dir"]
    meta_path = case_dir / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["documents"][0][meta_field] = prior_value
    _write_json(meta_path, meta)

    code, receipt, out, err = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert "C6" in receipt["failed_rules"]
    assert any(i["code"] == "prior_id_overlap" for i in receipt["checks"]["C6"]["issues"])
    raw_val = prior_value[0] if isinstance(prior_value, list) else prior_value
    _assert_no_text_leak(out, err, None, [raw_val])


# ------------------------------------------------------------ C7 fail cases


def test_c7_fail_after_cutoff_and_version_ge_3_8(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    meta_path = case_dir / "metadata.json"
    sample_path = case_dir / "sample.json"

    # 1) 1 second after cutoff.
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["documents"][0]["published"] = "2026-11-06T19:45:01+08:00"
    _write_json(meta_path, meta)
    code1, receipt1, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code1 == 1
    assert receipt1["failed_rules"] == ["C7"]
    assert any(i["code"] == "published_after_cutoff" for i in receipt1["checks"]["C7"]["issues"])

    # 2) Version >= 3.8 mention in text.
    shutil.copyfile(FIXTURES_DIR / "metadata.json", meta_path)
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    sample["paragraphs"][5]["source"] = sample["paragraphs"][5]["source"] + "期待3.8版本更新内容。"
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)
    code2, receipt2, out2, err2 = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code2 == 1
    assert receipt2["failed_rules"] == ["C7"]
    assert any("version_ge_3_8" in i["code"] for i in receipt2["checks"]["C7"]["issues"])
    _assert_no_text_leak(out2, err2, None, ["期待3.8版本更新内容"])


# ------------------------------------------------------------ C8 fail cases


@pytest.mark.parametrize(
    ("pii_snippet", "expected_category"),
    [
        (" 玩家UID 102345678 留念", "player_uid"),
        (" contact synth.user@example.org now", "email"),
        (" 欢迎加入交流群：88421093 讨论", "contact_or_group"),
        (" invite link discord.gg/synth9988 here", "contact_or_group"),
    ],
)
def test_c8_fail_pii_detected_without_leaking_pii(gate, corpus_env, capsys, pii_snippet, expected_category):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    sample["paragraphs"][6]["source"] = sample["paragraphs"][6]["source"] + pii_snippet
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)

    code, receipt, out, err = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert "C8" in receipt["failed_rules"]
    assert expected_category in receipt["checks"]["C8"]["hits_by_category"]
    _assert_no_text_leak(out, err, None, [pii_snippet.strip(), "102345678", "synth.user@example.org", "88421093", "synth9988"])


# ------------------------------------------------------------ C9 fail cases


def test_c9_fail_incomplete_coverage_invalid_span_and_duplicate_occ(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    names_path = case_dir / "names.json"
    comm_path = case_dir / "community_names.json"

    # 1) Missing scored unit coverage.
    names = json.loads(names_path.read_text(encoding="utf-8"))
    names["annotated_paragraphs"].pop()
    _write_json(names_path, names)
    code1, receipt1, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code1 == 1
    assert receipt1["failed_rules"] == ["C9"]
    assert any(i["code"] == "incomplete_scored_unit_coverage" for i in receipt1["checks"]["C9"]["issues"])

    # 2) Invalid span surface mismatch + duplicate occ id across official/community.
    names = json.loads((FIXTURES_DIR / "names.json").read_text(encoding="utf-8"))
    names["occurrences"][0]["surface"] = "WRONG"
    _write_json(names_path, names)
    comm = json.loads(comm_path.read_text(encoding="utf-8"))
    comm["occurrences"][0]["occ"] = names["occurrences"][1]["occ"]
    _write_json(comm_path, comm)
    code2, receipt2, out2, err2 = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code2 == 1
    assert receipt2["failed_rules"] == ["C9"]
    codes = {i["code"] for i in receipt2["checks"]["C9"]["issues"]}
    assert "surface_span_mismatch" in codes
    assert "duplicate_occ_id" in codes
    _assert_no_text_leak(out2, err2, None, ["WRONG", "合成甲标九"])


# ------------------------------------------------------------ C10 fail & validator


def test_c10_fail_mismatched_binding_and_receipt_validator_rejections(gate, corpus_env, capsys):
    # Mismatched expected src tree fails C10.
    code, receipt, _, _ = _run_gate(
        gate,
        corpus_env["build_argv"]("--expected-src-tree", "0" * 40),
        capsys,
    )
    assert code == 1
    assert receipt["verdict"] == "fail"
    assert receipt["failed_rules"] == ["C10"]
    assert any(i["code"] == "bound_tree_src_sha_mismatch" for i in receipt["checks"]["C10"]["issues"])

    # Failing receipt rejected by validate_acceptance_receipt.
    with pytest.raises(gate.ReceiptValidationError, match="verdict"):
        gate.validate_acceptance_receipt(receipt)

    # Produce a passing receipt, then test tamper & expected-hash rejections.
    code_ok, ok_receipt, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_ok == 0

    tampered = json.loads(json.dumps(ok_receipt))
    tampered["bindings"]["terms_db_sha256"] = "f" * 64
    with pytest.raises(gate.ReceiptValidationError, match="bindings_sha256"):
        gate.validate_acceptance_receipt(tampered)

    with pytest.raises(gate.ReceiptValidationError, match="manifest_sha256"):
        gate.validate_acceptance_receipt(ok_receipt, expected_manifest_sha256="a" * 64)

    with pytest.raises(gate.ReceiptValidationError, match="bound_tree_src_sha"):
        gate.validate_acceptance_receipt(ok_receipt, expected_src_tree="b" * 40)

    bad_rules = json.loads(json.dumps(ok_receipt))
    bad_rules["checks"]["C8"]["status"] = "fail"
    with pytest.raises(gate.ReceiptValidationError, match="failed_rules"):
        gate.validate_acceptance_receipt(bad_rules)
