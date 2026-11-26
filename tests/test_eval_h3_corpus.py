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


def _commit_git_files(path: Path, files: dict[str, str], message: str) -> str:
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", message)
    return _git(path, "rev-parse", "HEAD")


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sync_manifest_and_sample_hashes(case_dir: Path) -> None:
    """Recompute sample.json manifest_sha256, source_sha256, and adjudication unit counts."""
    manifest_path = case_dir / "manifest.json"
    sample_path = case_dir / "sample.json"
    adj_path = case_dir / "annotation_adjudication.json"
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

    if adj_path.is_file():
        adj = json.loads(adj_path.read_text(encoding="utf-8"))
        scored = [p for p in sample_data["paragraphs"] if p["scored"]]
        comm_scored = [p for p in scored if p["genre"] in ("guide", "mixed")]
        adj["official"]["units_reviewed"] = len(scored)
        adj["community"]["units_reviewed"] = len(comm_scored)
        _write_json(adj_path, adj)


@pytest.fixture()
def corpus_env(tmp_path: Path, sample_db: Path):
    """Copy synthetic fixtures into tmp_path with an isolated Core git repo for C4."""
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
    freeze_core_commit = _commit_git_files(
        core_repo,
        {"docs/freeze_snapshot.md": "Synthetic clean post-tag freeze commit note.\n"},
        "synthetic freeze commit",
    )
    src_tree = _git(ROOT, "rev-parse", "HEAD:src")
    from wuwaterm import cjk_span

    matcher_rev = str(cjk_span.MATCHER_REVISION)
    terms_sha = _sha256_file(sample_db)

    def build_argv(
        *extra: str,
        receipt_out: Path | None = None,
        include_adjudication: bool = True,
        freeze_commit_override: str | None = freeze_core_commit,
    ) -> list[str]:
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
            "--community-lexicon",
            str(case_dir / "community_lexicon.json"),
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
        if freeze_commit_override is not None:
            argv.extend(["--freeze-core-commit", freeze_commit_override])
        if include_adjudication:
            argv.extend(["--annotation-adjudication", str(case_dir / "annotation_adjudication.json")])
        if receipt_out is not None:
            argv.extend(["--json-out", str(receipt_out)])
        argv.extend(extra)
        return argv

    def validate_kwargs(**overrides: Any) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "expected_manifest_sha256": _sha256_file(case_dir / "manifest.json"),
            "expected_sample_sha256": _sha256_file(case_dir / "sample.json"),
            "expected_names_sha256": _sha256_file(case_dir / "names.json"),
            "expected_community_names_sha256": _sha256_file(case_dir / "community_names.json"),
            "expected_metadata_sha256": _sha256_file(case_dir / "metadata.json"),
            "expected_src_tree": src_tree,
            "expected_core_commit": core_commit,
            "expected_freeze_core_commit": freeze_core_commit,
            "expected_terms_sha256": terms_sha,
            "expected_matcher_revision": matcher_rev,
            "allow_non_default_bindings": True,
        }
        kw.update(overrides)
        return kw

    return {
        "case_dir": case_dir,
        "core_repo": core_repo,
        "core_commit": core_commit,
        "freeze_core_commit": freeze_core_commit,
        "src_tree": src_tree,
        "matcher_rev": matcher_rev,
        "terms_db": sample_db,
        "terms_sha": terms_sha,
        "build_argv": build_argv,
        "validate_kwargs": validate_kwargs,
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
    assert receipt["checks"]["C3"]["announcement_units_by_level"] == {
        "zh->en": {"L0": 30, "L1": 0, "L2": 0},
        "en->zh": {"L0": 35, "L1": 0, "L2": 0},
    }
    assert receipt["checks"]["C4"]["windows_uncleared"] == 0
    assert receipt["checks"]["C4"]["scorer_sha256_ok"] is True
    assert receipt["checks"]["C4"]["required_prior_sets_complete"] is True
    assert receipt["checks"]["C4"]["core_unique_blobs_scanned"] == 3
    assert receipt["checks"]["C4"]["core_blob_entries_scanned"] == 5
    assert receipt["checks"]["C9"]["community_target_scored_units"] == 150
    assert receipt["bindings"]["core_tag_commit"] == corpus_env["core_commit"]
    assert receipt["bindings"]["core_commit"] == corpus_env["core_commit"]
    assert receipt["bindings"]["freeze_core_commit"] == corpus_env["freeze_core_commit"]

    # Default validate_acceptance_receipt fails closed because test uses non-pinned core_commit / sample_db.
    with pytest.raises(gate.ReceiptValidationError, match="non_default_parameters"):
        gate.validate_acceptance_receipt(receipt)

    # Explicit matching bindings + allow_non_default_bindings=True passes.
    validated = gate.validate_acceptance_receipt(receipt, **corpus_env["validate_kwargs"]())
    assert validated["verdict"] == "pass"

    # CLI verify-receipt also enforces pinned defaults and accepts explicit overrides.
    assert gate.main(["verify-receipt", "--receipt", str(receipt_path)]) == 1
    capsys.readouterr()
    verify_rc = gate.main(
        [
            "verify-receipt",
            "--receipt",
            str(receipt_path),
            "--expected-core-commit",
            corpus_env["core_commit"],
            "--expected-src-tree",
            corpus_env["src_tree"],
            "--expected-terms-sha256",
            corpus_env["terms_sha"],
            "--expected-matcher-revision",
            corpus_env["matcher_rev"],
            "--allow-non-default-bindings",
        ]
    )
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
    """Fixture corpus has zero uncleared 12-scalar overlap against real Core v0.14.0 + descendant commit union."""
    v14_commit = gate.PINNED_CORE_COMMIT
    # Use v0.14.0's immediate descendant d50919e (present in branch history and excludes our fixture files).
    freeze_candidate = "d50919e517a399d5a3099aa5fbf8944ca038a33b"
    for rev in (v14_commit, freeze_candidate):
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", f"{rev}^{{commit}}"],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            pytest.skip(f"commit {rev[:8]} not present in shallow clone")
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
        "--annotation-adjudication",
        str(corpus_env["case_dir"] / "annotation_adjudication.json"),
        "--metadata",
        str(corpus_env["case_dir"] / "metadata.json"),
        "--prior-texts",
        str(corpus_env["case_dir"] / "prior_texts.json"),
        "--prior-ids",
        str(corpus_env["case_dir"] / "prior_ids.json"),
        "--clear-names",
        str(corpus_env["case_dir"] / "clear_names.txt"),
        "--community-lexicon",
        str(corpus_env["case_dir"] / "community_lexicon.json"),
        "--upstream-clearances",
        str(corpus_env["case_dir"] / "upstream_clearances.json"),
        "--bound-tree",
        str(ROOT),
        "--core-repo",
        str(ROOT),
        "--core-commit",
        v14_commit,
        "--freeze-core-commit",
        freeze_candidate,
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
    assert receipt["checks"]["C4"]["core_unique_blobs_scanned"] > 100
    assert receipt["checks"]["C4"]["core_blob_entries_scanned"] >= receipt["checks"]["C4"]["core_unique_blobs_scanned"]
    assert receipt["checks"]["C4"]["windows_uncleared"] == 0
    _assert_no_text_leak(out, err, None, ["合成甲标九", "SYNTHCANARYEN"])


# ------------------------------------------------------------ C1 fail cases


def test_c1_fail_manifest_sample_mismatch(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
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
    sample = json.loads(sample_path.read_text(encoding="utf-8"))

    if mutation == "dict_marked_scored":
        target = next(p for p in sample["paragraphs"] if p["exclusion"] == "dictionary")
        target["scored"] = True
        target["exclusion"] = None
    elif mutation == "no_letter_marked_scored":
        target = next(p for p in sample["paragraphs"] if p["exclusion"] == "no_letter")
        target["scored"] = True
        target["exclusion"] = None
    elif mutation == "scored_marked_dict":
        target = sample["paragraphs"][2]
        assert target["scored"] is True
        target["scored"] = False
        target["exclusion"] = "dictionary"
    elif mutation == "duplicate_marked_scored":
        sample["paragraphs"][4]["source"] = sample["paragraphs"][3]["source"]

    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)

    code, receipt, out, err = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert "C2" in receipt["failed_rules"]
    mismatches = receipt["checks"]["C2"]["mismatches"]
    assert any(m["computed_exclusion"] == expected_computed for m in mismatches)
    _assert_no_text_leak(out, err, None, ["合成甲标九", "SYNTHCANARYEN", "Shorekeeper"])


# ------------------------------------------------------------ C3 pass/fail & per-direction override


def test_c3_fail_default_and_per_direction_announcement_floor_override(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))

    # Reduce zh->en announcement from 30 to 25 and add 5 extra zh->en story units so total stays 140.
    kept = []
    ann_removed = 0
    for para in sample["paragraphs"]:
        if para["direction"] == "zh-en" and para["genre"] == "announcement" and para["scored"] and ann_removed < 5:
            if not para["id"].endswith("-p00"):
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

    sample["paragraphs"] = kept
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)

    # Default mode must strictly fail C3 (zh->en announcement 25 < 30).
    code_def, receipt_def, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_def == 1
    assert receipt_def["failed_rules"] == ["C3"]
    assert receipt_def["checks"]["C3"]["floor_profile"] == "default"
    assert receipt_def["checks"]["C3"]["non_default_floor"] is False

    # Passing only --announcement-floor-en-zh 25 still fails zh->en (direction-specific!).
    code_wrong_dir, receipt_wrong_dir, _, _ = _run_gate(
        gate, corpus_env["build_argv"]("--announcement-floor-en-zh", "25"), capsys
    )
    assert code_wrong_dir == 1
    assert receipt_wrong_dir["failed_rules"] == ["C3"]

    # Explicit --announcement-floor-zh-en 25 passes C3 and records direction in floor_profile & non_default_parameters.
    code_ovr, receipt_ovr, _, _ = _run_gate(
        gate, corpus_env["build_argv"]("--announcement-floor-zh-en", "25"), capsys
    )
    assert code_ovr == 0, receipt_ovr
    assert receipt_ovr["verdict"] == "pass"
    assert receipt_ovr["checks"]["C3"]["non_default_floor"] is True
    assert receipt_ovr["checks"]["C3"]["floor_profile"] == "custom:announcement[zh->en]=25"
    assert receipt_ovr["non_default_parameters"]["announcement_floor_zh_en"] == 25
    assert "announcement_floor_en_zh" not in receipt_ovr["non_default_parameters"]

    # validate_acceptance_receipt rejects non-default floor unless allow_non_default_floor=True.
    with pytest.raises(gate.ReceiptValidationError, match="announcement_floor_zh_en"):
        gate.validate_acceptance_receipt(receipt_ovr, **corpus_env["validate_kwargs"]())
    gate.validate_acceptance_receipt(
        receipt_ovr, **corpus_env["validate_kwargs"](allow_non_default_floor=True)
    )


def test_c3_announcement_levels_breakdown_and_fail_cases(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    meta_path = case_dir / "metadata.json"

    # 1) Mixed L0/L1/L2 across the 4 announcement docs -> pass with accurate per-direction breakdown.
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    by_id = {d["doc_id"]: d for d in meta["documents"]}
    by_id["syn-zh-ann-01"]["announcement_level"] = "L0"
    by_id["syn-zh-ann-02"]["announcement_level"] = "L1"
    by_id["syn-en-ann-01"]["announcement_level"] = "L0"
    by_id["syn-en-ann-02"]["announcement_level"] = "L2"
    _write_json(meta_path, meta)

    code_mix, rec_mix, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_mix == 0, rec_mix["checks"]["C3"]
    assert rec_mix["checks"]["C3"]["announcement_units_by_level"] == {
        "zh->en": {"L0": 15, "L1": 15, "L2": 0},
        "en->zh": {"L0": 18, "L1": 0, "L2": 17},
    }

    # 2) Missing announcement_level on an announcement doc fails C3.
    del by_id["syn-zh-ann-01"]["announcement_level"]
    _write_json(meta_path, meta)
    code_miss, rec_miss, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_miss == 1
    assert rec_miss["failed_rules"] == ["C3"]
    assert any(i["code"] == "missing_announcement_level" for i in rec_miss["checks"]["C3"]["announcement_level_issues"])

    # 3) Invalid announcement_level ("L3") on an announcement doc fails C3.
    by_id["syn-zh-ann-01"]["announcement_level"] = "L3"
    _write_json(meta_path, meta)
    code_bad, rec_bad, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_bad == 1
    assert rec_bad["failed_rules"] == ["C3"]
    assert any(i["code"] == "invalid_announcement_level" for i in rec_bad["checks"]["C3"]["announcement_level_issues"])

    # 4) Non-announcement doc carrying announcement_level fails C3.
    by_id["syn-zh-ann-01"]["announcement_level"] = "L0"
    by_id["syn-zh-gui-01"]["announcement_level"] = "L0"
    _write_json(meta_path, meta)
    code_non, rec_non, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_non == 1
    assert rec_non["failed_rules"] == ["C3"]
    assert any(
        i["code"] == "unexpected_announcement_level_on_non_announcement"
        for i in rec_non["checks"]["C3"]["announcement_level_issues"]
    )


def test_c3_fail_zh_en_guide_below_50(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))

    victim = next(
        p["id"]
        for p in sample["paragraphs"]
        if p["direction"] == "zh-en" and p["genre"] == "guide" and p["scored"] and not p["id"].endswith("-p00")
    )
    sample["paragraphs"] = [p for p in sample["paragraphs"] if p["id"] != victim]
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)

    code, receipt, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert "C3" in receipt["failed_rules"]
    assert any(
        s.get("direction") == "zh->en" and s.get("genre") == "guide"
        for s in receipt["checks"]["C3"]["shortfalls"]
    )


# ------------------------------------------------------------ C4 tests (protocol #338 §2.4.1)


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


def test_c4_fail_missing_required_prior_set(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    prior_path = case_dir / "prior_texts.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    prior["sets"] = [s for s in prior["sets"] if s["set"] != "M-Gold"]
    _write_json(prior_path, prior)

    code, receipt, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert receipt["failed_rules"] == ["C4"]
    c4 = receipt["checks"]["C4"]
    assert c4["required_prior_sets_complete"] is False
    assert any(
        i["code"] == "missing_required_prior_sets" and "M-Gold" in i["missing"] for i in c4["issues"]
    )


def test_c4_fail_cjk_windows_never_cleared_by_m2(gate, corpus_env, capsys):
    """M2 rejects all CJK windows: 18-char Han, non-BMP Han, 々/〇, Hiragana, halfwidth Katakana, Hangul."""
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    prior_path = case_dir / "prior_texts.json"

    cases = [
        "松径晴岚溪桥石阶云帆浅渚翠岭霜林星砂",  # 18 Han chars, no digits, no dict surfaces
        "alpha beta 𠀀gamma",  # non-BMP CJK Extension B (U+20000)
        "alpha ｶﾀｶﾅ delta",  # halfwidth Katakana (U+FF76..FF85)
        "alpha beta 々〇end",  # Han iteration mark 々 (U+3005) and ideographic zero 〇 (U+3007)
        "alpha 한글 phrase",  # Hangul syllables
    ]
    for phrase in cases:
        assert len(phrase) >= 12
        shutil.copyfile(FIXTURES_DIR / "sample.json", sample_path)
        shutil.copyfile(FIXTURES_DIR / "manifest.json", case_dir / "manifest.json")
        shutil.copyfile(FIXTURES_DIR / "prior_texts.json", prior_path)

        sample = json.loads(sample_path.read_text(encoding="utf-8"))
        target_p = sample["paragraphs"][2]
        target_p["source"] = target_p["source"] + f"「{phrase}」"
        _write_json(sample_path, sample)
        _sync_manifest_and_sample_hashes(case_dir)

        prior = json.loads(prior_path.read_text(encoding="utf-8"))
        prior["sets"][0]["texts"].append(f"【{phrase}】")
        _write_json(prior_path, prior)

        code, receipt, out, err = _run_gate(gate, corpus_env["build_argv"](), capsys)
        assert code == 1, f"CJK phrase should not be cleared by M2: {phrase!r}"
        assert "C4" in receipt["failed_rules"]
        assert receipt["checks"]["C4"]["windows_uncleared"] == len(phrase) - 12 + 1
        _assert_no_text_leak(out, err, None, [phrase, phrase[:12]])


def test_c4_cross_set_spliced_fragment_length_consecutive_windows_plus_11(gate, corpus_env, capsys):
    """H1 + H2 spliced across 17 consecutive hit window positions => fragment_len = 28 >= 24 => M2 rejects."""
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    prior_path = case_dir / "prior_texts.json"

    # 28-char generic English phrase (no CJK, no digits, no dict surfaces).
    # Split into two 16-char overlapping halves placed in H1 and H2 separately:
    # H1 has positions [0..16) -> hit windows at offsets 0..4
    # H2 has positions [5..28) -> wait: to keep EACH half < 24 chars, use 16 + 16 with 4-char overlap?
    # Better: H1 has phrase[0:20] (windows 0..8), H2 has phrase[9:28] (windows 9..16) -> 17 consecutive hit windows!
    # Neither H1 (20 chars) nor H2 (19 chars) alone reaches 24, yet in the H3 paragraph positions 0..16 are
    # ALL consecutive hit windows => fragment_len = 17 + 11 = 28 >= 24 => M2 MUST reject!
    spliced = "autumn breezes softly wander"  # 28 chars
    assert len(spliced) == 28
    part_h1 = spliced[:20]  # 20 chars -> windows at pos 0..8
    part_h2 = spliced[9:]  # 19 chars -> windows at pos 9..16
    assert len(part_h1) < 24 and len(part_h2) < 24

    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    en_para = next(
        p for p in sample["paragraphs"] if p["direction"] == "en-zh" and p["scored"] and not p["id"].endswith("-p00")
    )
    en_para["source"] = en_para["source"] + f" ({spliced})!"
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)

    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    prior["sets"][0]["texts"].append(f"X[{part_h1}]Y")  # H1
    prior["sets"][1]["texts"].append(f"P[{part_h2}]Q")  # H2
    _write_json(prior_path, prior)

    code, receipt, out, err = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert receipt["failed_rules"] == ["C4"]
    c4 = receipt["checks"]["C4"]
    assert c4["max_shared_span_length"] == 28
    assert c4["windows_uncleared"] == 17
    assert all(w["fragment_len"] == 28 and w["class"] == "uncleared" for w in c4["uncleared_windows"])
    _assert_no_text_leak(out, err, None, [spliced, part_h1, part_h2])


def test_c4_dual_core_commit_mandatory_ancestry_and_union(gate, corpus_env, capsys):
    """Mandatory --freeze-core-commit must be a strict descendant of --core-commit and its blobs are scanned."""
    case_dir = corpus_env["case_dir"]
    sample = json.loads((case_dir / "sample.json").read_text(encoding="utf-8"))
    zh_source = sample["paragraphs"][0]["source"]

    # 1) Omitting --freeze-core-commit on CLI is rejected by argparse (exit 2).
    with pytest.raises(SystemExit) as exc_info:
        gate.main(corpus_env["build_argv"](freeze_commit_override=None))
    assert exc_info.value.code == 2
    capsys.readouterr()

    # 2) Passing freeze_core_commit == core_commit fails C4 and C10.
    code_eq, rec_eq, _, _ = _run_gate(
        gate,
        corpus_env["build_argv"](freeze_commit_override=corpus_env["core_commit"]),
        capsys,
    )
    assert code_eq == 1
    assert "C4" in rec_eq["failed_rules"]
    assert "C10" in rec_eq["failed_rules"]
    assert any(i["code"] == "freeze_core_commit_equals_tag_commit" for i in rec_eq["checks"]["C10"]["issues"])

    # 3) Non-ancestor (swapped: tag commit is descendant of freeze commit) fails C4 and C10.
    argv_swapped = corpus_env["build_argv"](freeze_commit_override=corpus_env["core_commit"])
    cc_idx = argv_swapped.index("--core-commit")
    argv_swapped[cc_idx + 1] = corpus_env["freeze_core_commit"]
    exp_idx = argv_swapped.index("--expected-core-commit")
    argv_swapped[exp_idx + 1] = corpus_env["freeze_core_commit"]
    code_anc, rec_anc, _, _ = _run_gate(gate, argv_swapped, capsys)
    assert code_anc == 1
    assert "C4" in rec_anc["failed_rules"]
    assert "C10" in rec_anc["failed_rules"]
    assert any(i["code"] == "core_tag_not_ancestor_of_freeze_commit" for i in rec_anc["checks"]["C10"]["issues"])

    # 4) Valid descendant freeze commit adding an overlapping file is caught by C4 union scan.
    overlap_freeze = _commit_git_files(
        corpus_env["core_repo"],
        {"docs/post_tag_note.md": f"added before freeze: {zh_source}\n"},
        "add post-tag overlap",
    )
    code_ov, rec_ov, out_ov, err_ov = _run_gate(
        gate,
        corpus_env["build_argv"](freeze_commit_override=overlap_freeze),
        capsys,
    )
    assert code_ov == 1
    assert rec_ov["failed_rules"] == ["C4"]
    assert rec_ov["bindings"]["core_tag_commit"] == corpus_env["core_commit"]
    assert rec_ov["bindings"]["freeze_core_commit"] == overlap_freeze
    assert rec_ov["checks"]["C4"]["windows_uncleared"] > 0
    assert any("core_tree" in u["targets"] for u in rec_ov["checks"]["C4"]["uncleared_windows"])
    _assert_no_text_leak(out_ov, err_ov, None, [zh_source, zh_source[:12]])


def test_c4_m1_tightening_m2_m3_strict_and_full_hit_audit(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    names_path = case_dir / "names.json"
    prior_path = case_dir / "prior_texts.json"
    upstream_path = case_dir / "upstream_clearances.json"

    base_rc, base_receipt, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert base_rc == 0
    base_m2 = base_receipt["checks"]["C4"]["cleared_counts"]["M2"]

    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    en_para = next(
        p for p in sample["paragraphs"] if p["direction"] == "en-zh" and p["scored"] and not p["id"].endswith("-p00")
    )
    zh_para = next(
        p for p in sample["paragraphs"] if p["direction"] == "zh-en" and p["scored"] and not p["id"].endswith("-p00")
    )

    # 0) M1 tightening check: a 16-char hit present ONLY in H3's own names.json must NOT self-exempt!
    self_exempt_try = "自标名称不得自豁免十六字甲乙丙丁"
    assert len(self_exempt_try) == 16
    zh_p_extra = [p for p in sample["paragraphs"] if p["direction"] == "zh-en" and p["scored"] and not p["id"].endswith("-p00")][1]
    zh_p_extra_orig = zh_p_extra["source"]
    zh_p_extra["source"] = zh_p_extra_orig + f"「{self_exempt_try}」"
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)

    names_data = json.loads(names_path.read_text(encoding="utf-8"))
    st = zh_p_extra["source"].index(self_exempt_try)
    names_data["occurrences"].append(
        {
            "occ": "off-self-exempt",
            "paragraph": zh_p_extra["id"],
            "start": st,
            "end": st + len(self_exempt_try),
            "surface": self_exempt_try,
            "name_id": "self-exempt-id",
            "official_forms": ["Self Exempt Name"],
        }
    )
    _write_json(names_path, names_data)

    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    prior["sets"][0]["texts"].append(f"【{self_exempt_try}】")
    _write_json(prior_path, prior)

    code_self, receipt_self, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_self == 1
    assert receipt_self["failed_rules"] == ["C4"]
    assert receipt_self["checks"]["C4"]["cleared_counts"]["M1"] == 0
    assert receipt_self["checks"]["C4"]["windows_uncleared"] == len(self_exempt_try) - 12 + 1

    # Restore self_exempt_try and test M1 (--clear-names + --community-lexicon), M2, and strict M3.
    shutil.copyfile(FIXTURES_DIR / "sample.json", sample_path)
    shutil.copyfile(FIXTURES_DIR / "names.json", names_path)
    shutil.copyfile(FIXTURES_DIR / "prior_texts.json", prior_path)
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    en_para = next(
        p for p in sample["paragraphs"] if p["direction"] == "en-zh" and p["scored"] and not p["id"].endswith("-p00")
    )
    zh_para = next(
        p for p in sample["paragraphs"] if p["direction"] == "zh-en" and p["scored"] and not p["id"].endswith("-p00")
    )

    m1_official = "Vanguard Junrock"  # in clear_names.txt (16 chars -> 5 windows)
    m1_community = "合成社区专有长词条十六字甲乙丙丁"  # in community_lexicon.json (16 chars -> 5 windows)
    m2_phrase = "under gentle winds"  # 18 chars -> 7 windows (M2)
    m3_phrase = "上游共鸣者模板专用片段甲乙丙丁"  # 15 chars -> 4 windows (CJK + dict -> M3 only)

    en_para["source"] = en_para["source"] + f" sighting({m1_official})nearby. Travel({m2_phrase})today."
    zh_para["source"] = zh_para["source"] + f"「{m1_community}」「{m3_phrase}」"
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)

    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    prior["sets"][0]["texts"].extend(
        [
            f"alpha[{m1_official}]omega",
            f"before[{m2_phrase}]after",
            f"【{m1_community}】",
            f"【{m3_phrase}】",
        ]
    )
    _write_json(prior_path, prior)

    # M3 strict rejections: text entry, reason alias, missing location.
    for bad_entry, expected_code in (
        ({"classification": "upstream_data", "text": m3_phrase, "location": "TextMap:1"}, "upstream_clearance_text_forbidden"),
        (
            {"reason": "upstream_data", "window_sha256": _sha256_text(m3_phrase[:12]), "location": "TextMap:1"},
            "upstream_clearance_reason_alias_forbidden",
        ),
        (
            {"classification": "upstream_data", "window_sha256": _sha256_text(m3_phrase[:12])},
            "missing_upstream_clearance_location",
        ),
    ):
        _write_json(upstream_path, {"schema": "h3-upstream-clearances-v1", "entries": [bad_entry]})
        rc_bad, rec_bad, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
        assert rc_bad == 1
        assert "C4" in rec_bad["failed_rules"]
        assert any(i["code"] == expected_code for i in rec_bad["checks"]["C4"]["issues"])

    # Valid M3 entries (window_sha256 + location); also include m2_phrase windows to prove M1 -> M2 -> M3 order.
    m3_entries = [
        {
            "classification": "upstream_data",
            "window_sha256": _sha256_text(m3_phrase[i : i + 12]),
            "location": f"TextMap/zh-Hans/Synthetic.json:Key_01@{i}",
        }
        for i in range(len(m3_phrase) - 12 + 1)
    ] + [
        {
            "classification": "upstream_data",
            "window_sha256": _sha256_text(m2_phrase[i : i + 12]),
            "location": f"TextMap/en/Synthetic.json:Order@{i}",
        }
        for i in range(len(m2_phrase) - 12 + 1)
    ]
    _write_json(upstream_path, {"schema": "h3-upstream-clearances-v1", "entries": m3_entries})

    code_ok, receipt_ok, out_ok, err_ok = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_ok == 0, receipt_ok["checks"]["C4"]
    c4 = receipt_ok["checks"]["C4"]
    expected_m1 = (len(m1_official) - 12 + 1) + (len(m1_community) - 12 + 1)
    expected_m2 = len(m2_phrase) - 12 + 1
    expected_m3 = len(m3_phrase) - 12 + 1
    assert c4["cleared_counts"]["M1"] == expected_m1
    assert c4["cleared_counts"]["M2"] - base_m2 == expected_m2
    assert c4["cleared_counts"]["M3"] == expected_m3
    assert c4["windows_uncleared"] == 0

    # Verify full hit_windows audit trail records ALL cleared windows with required keys and zero text.
    assert len(c4["hit_windows"]) == c4["windows_hit"]
    classes_seen = {w["class"] for w in c4["hit_windows"]}
    assert classes_seen == {"M1", "M2", "M3"}
    for rec in c4["hit_windows"]:
        assert set(rec) == {"sha256", "class", "paragraph_ids", "targets", "fragment_len"}
    _assert_no_text_leak(out_ok, err_ok, None, [m1_official, m1_community, m2_phrase, m3_phrase])

    # M2 threshold override T=30 clears a 27-char English phrase, records non_default_parameters,
    # and default validate_acceptance_receipt MUST reject it unless allow_non_default_m2_threshold=True.
    long_phrase = "walking quietly along paths"  # 27 chars >= 24
    en_p2 = [p for p in sample["paragraphs"] if p["direction"] == "en-zh" and p["scored"] and not p["id"].endswith("-p00")][1]
    en_p2["source"] = en_p2["source"] + f" Keep({long_phrase})home."
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)
    prior["sets"][1]["texts"].append(f"start[{long_phrase}]end")
    _write_json(prior_path, prior)

    code_t24, receipt_t24, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_t24 == 1
    assert receipt_t24["failed_rules"] == ["C4"]

    code_t30, receipt_t30, _, _ = _run_gate(
        gate, corpus_env["build_argv"]("--m2-max-shared-span", "30"), capsys
    )
    assert code_t30 == 0, receipt_t30["checks"]["C4"]
    assert receipt_t30["non_default_parameters"]["m2_max_shared_span"] == 30
    with pytest.raises(gate.ReceiptValidationError, match="m2_max_shared_span"):
        gate.validate_acceptance_receipt(receipt_t30, **corpus_env["validate_kwargs"]())
    gate.validate_acceptance_receipt(
        receipt_t30, **corpus_env["validate_kwargs"](allow_non_default_m2_threshold=True)
    )


# ------------------------------------------------------------ C5 fail cases


def test_c5_fail_normalized_duplicate_and_bilingual_pair_both_directions(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    meta_path = case_dir / "metadata.json"

    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    en_scored = [p for p in sample["paragraphs"] if p["direction"] == "en-zh" and p["scored"]]
    en_scored[3]["source"] = en_scored[2]["source"].upper().replace(" ", "   ") + "!!!"
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)

    code, receipt, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert "C5" in receipt["failed_rules"]
    assert any(i["code"] == "duplicate_paragraph" for i in receipt["checks"]["C5"]["issues"])

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


def test_c6_fail_all_document_identifiers_empty(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    meta_path = case_dir / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["documents"][0]["source_id"] = None
    meta["documents"][0]["textmap_keys"] = []
    meta["documents"][0]["series"] = ""
    meta["documents"][0]["author"] = None
    _write_json(meta_path, meta)

    code, receipt, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert receipt["failed_rules"] == ["C6"]
    assert any(i["code"] == "missing_doc_identifiers" for i in receipt["checks"]["C6"]["issues"])


# ------------------------------------------------------------ C7 fail cases (strictly < cutoff)


@pytest.mark.parametrize(
    "bad_timestamp",
    [
        "2026-11-06T19:45:00+08:00",  # exact cutoff boundary MUST fail (strictly < cutoff per ADR 0020)
        "2026-11-06T11:45:00+00:00",  # UTC equivalent of exact cutoff boundary MUST fail
        "2026-11-06T19:45:01+08:00",  # 1s after cutoff
    ],
)
def test_c7_fail_at_or_after_cutoff(gate, corpus_env, capsys, bad_timestamp):
    case_dir = corpus_env["case_dir"]
    meta_path = case_dir / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["documents"][0]["published"] = bad_timestamp
    _write_json(meta_path, meta)

    code, receipt, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert receipt["failed_rules"] == ["C7"]
    assert any(i["code"] == "published_at_or_after_cutoff" for i in receipt["checks"]["C7"]["issues"])


def test_c7_fail_version_ge_3_8(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    sample_path = case_dir / "sample.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    sample["paragraphs"][5]["source"] = sample["paragraphs"][5]["source"] + "期待3.8版本更新内容。"
    _write_json(sample_path, sample)
    _sync_manifest_and_sample_hashes(case_dir)

    code, receipt, out, err = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code == 1
    assert receipt["failed_rules"] == ["C7"]
    assert any("version_ge_3_8" in i["code"] for i in receipt["checks"]["C7"]["issues"])
    _assert_no_text_leak(out, err, None, ["期待3.8版本更新内容"])


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
    _assert_no_text_leak(
        out, err, None, [pii_snippet.strip(), "102345678", "synth.user@example.org", "88421093", "synth9988"]
    )


# ------------------------------------------------------------ C9 pass/fail cases (protocol §5.3 + adjudication)


def test_c9_pass_with_embedded_adjudication_and_fail_cases(gate, corpus_env, capsys):
    case_dir = corpus_env["case_dir"]
    names_path = case_dir / "names.json"
    comm_path = case_dir / "community_names.json"
    adj_path = case_dir / "annotation_adjudication.json"

    # 1) Embedded adjudication blocks inside names.json / community_names.json also pass without separate file.
    adj = json.loads(adj_path.read_text(encoding="utf-8"))
    names = json.loads(names_path.read_text(encoding="utf-8"))
    comm = json.loads(comm_path.read_text(encoding="utf-8"))
    names["adjudication"] = adj["official"]
    comm["adjudication"] = adj["community"]
    _write_json(names_path, names)
    _write_json(comm_path, comm)
    code_emb, receipt_emb, _, _ = _run_gate(gate, corpus_env["build_argv"](include_adjudication=False), capsys)
    assert code_emb == 0, receipt_emb["checks"]["C9"]

    # 2) Missing adjudication metadata fails C9.
    shutil.copyfile(FIXTURES_DIR / "names.json", names_path)
    shutil.copyfile(FIXTURES_DIR / "community_names.json", comm_path)
    code_noadj, receipt_noadj, _, _ = _run_gate(gate, corpus_env["build_argv"](include_adjudication=False), capsys)
    assert code_noadj == 1
    assert receipt_noadj["failed_rules"] == ["C9"]
    assert any(i["code"] == "missing_or_invalid_adjudication" for i in receipt_noadj["checks"]["C9"]["issues"])

    # 3) Unresolved disagreements > 0 or wrong units_reviewed fails C9.
    adj_bad = json.loads(adj_path.read_text(encoding="utf-8"))
    adj_bad["official"]["unresolved_disagreements"] = 1
    adj_bad["community"]["units_reviewed"] = 280  # must be 150 (guide + mixed only)
    _write_json(adj_path, adj_bad)
    code_badadj, receipt_badadj, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_badadj == 1
    assert receipt_badadj["failed_rules"] == ["C9"]
    c9_codes = {i["code"] for i in receipt_badadj["checks"]["C9"]["issues"]}
    assert "unresolved_annotation_disagreements" in c9_codes
    assert "adjudication_units_reviewed_mismatch" in c9_codes

    # 4) Community occurrence targeting an announcement paragraph fails C9.
    shutil.copyfile(FIXTURES_DIR / "annotation_adjudication.json", adj_path)
    sample = json.loads((case_dir / "sample.json").read_text(encoding="utf-8"))
    ann_p = next(p for p in sample["paragraphs"] if p["genre"] == "announcement" and p["scored"])
    comm = json.loads(comm_path.read_text(encoding="utf-8"))
    comm["occurrences"].append(
        {
            "occ": "com-bad-genre",
            "paragraph": ann_p["id"],
            "start": 0,
            "end": 2,
            "surface": ann_p["source"][:2],
            "term_id": "t-bad",
            "accepted_forms": ["bad"],
        }
    )
    _write_json(comm_path, comm)
    code_genre, receipt_genre, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_genre == 1
    assert receipt_genre["failed_rules"] == ["C9"]
    assert any(i["code"] == "community_occurrence_outside_guide_mixed" for i in receipt_genre["checks"]["C9"]["issues"])

    # 5) Invalid span surface mismatch + duplicate occ across official and community fails C9.
    shutil.copyfile(FIXTURES_DIR / "community_names.json", comm_path)
    names = json.loads((FIXTURES_DIR / "names.json").read_text(encoding="utf-8"))
    names["occurrences"][0]["surface"] = "WRONG"
    _write_json(names_path, names)
    comm = json.loads(comm_path.read_text(encoding="utf-8"))
    comm["occurrences"][0]["occ"] = names["occurrences"][1]["occ"]
    _write_json(comm_path, comm)
    code_span, receipt_span, out_span, err_span = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_span == 1
    assert receipt_span["failed_rules"] == ["C9"]
    codes = {i["code"] for i in receipt_span["checks"]["C9"]["issues"]}
    assert "surface_span_mismatch" in codes
    assert "duplicate_occ_id" in codes
    _assert_no_text_leak(out_span, err_span, None, ["WRONG", "合成甲标九"])


# ------------------------------------------------------------ C10 fail & fail-closed validator


def test_c10_fail_mismatched_binding_and_receipt_validator_rejections(gate, corpus_env, capsys):
    code, receipt, _, _ = _run_gate(
        gate,
        corpus_env["build_argv"]("--expected-src-tree", "0" * 40),
        capsys,
    )
    assert code == 1
    assert receipt["verdict"] == "fail"
    assert receipt["failed_rules"] == ["C10"]
    assert any(i["code"] == "bound_tree_src_sha_mismatch" for i in receipt["checks"]["C10"]["issues"])

    with pytest.raises(gate.ReceiptValidationError, match="verdict"):
        gate.validate_acceptance_receipt(receipt, **corpus_env["validate_kwargs"](expected_src_tree="0" * 40))

    code_ok, ok_receipt, _, _ = _run_gate(gate, corpus_env["build_argv"](), capsys)
    assert code_ok == 0

    # Tampered bindings fail bindings_sha256 check.
    tampered = json.loads(json.dumps(ok_receipt))
    tampered["bindings"]["terms_db_sha256"] = "f" * 64
    with pytest.raises(gate.ReceiptValidationError, match="bindings_sha256"):
        gate.validate_acceptance_receipt(tampered, **corpus_env["validate_kwargs"]())

    # Wrong expected_gate_sha256 fails.
    with pytest.raises(gate.ReceiptValidationError, match="gate_sha256"):
        gate.validate_acceptance_receipt(
            ok_receipt, **corpus_env["validate_kwargs"](expected_gate_sha256="e" * 64)
        )

    # Wrong expected_manifest_sha256 / expected_src_tree fails.
    with pytest.raises(gate.ReceiptValidationError, match="manifest_sha256"):
        gate.validate_acceptance_receipt(
            ok_receipt, **corpus_env["validate_kwargs"](expected_manifest_sha256="a" * 64)
        )
    with pytest.raises(gate.ReceiptValidationError, match="bound_tree_src_sha"):
        gate.validate_acceptance_receipt(
            ok_receipt, **corpus_env["validate_kwargs"](expected_src_tree="b" * 40)
        )

    # Tampered freeze_core_commit == core_tag_commit (even with recomputed bindings_sha256) fails validator.
    eq_freeze = json.loads(json.dumps(ok_receipt))
    eq_freeze["bindings"]["freeze_core_commit"] = eq_freeze["bindings"]["core_tag_commit"]
    eq_freeze["bindings_sha256"] = gate.sha256_bytes(gate.canonical(eq_freeze["bindings"]))
    with pytest.raises(gate.ReceiptValidationError, match="freeze_core_commit must differ"):
        gate.validate_acceptance_receipt(
            eq_freeze, **corpus_env["validate_kwargs"](expected_freeze_core_commit=None)
        )

    # Missing freeze_core_commit fails validator.
    no_freeze = json.loads(json.dumps(ok_receipt))
    no_freeze["bindings"]["freeze_core_commit"] = None
    no_freeze["bindings_sha256"] = gate.sha256_bytes(gate.canonical(no_freeze["bindings"]))
    with pytest.raises(gate.ReceiptValidationError, match="freeze_core_commit"):
        gate.validate_acceptance_receipt(
            no_freeze, **corpus_env["validate_kwargs"](expected_freeze_core_commit=None)
        )

    # Inconsistent failed_rules vs checks fails.
    bad_rules = json.loads(json.dumps(ok_receipt))
    bad_rules["checks"]["C8"]["status"] = "fail"
    with pytest.raises(gate.ReceiptValidationError, match="failed_rules"):
        gate.validate_acceptance_receipt(bad_rules, **corpus_env["validate_kwargs"]())
