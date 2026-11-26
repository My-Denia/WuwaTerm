"""Tests for scripts/eval_h3_tree_diff.py.

Every test runs purely offline against temporary git checkouts and an
in-process MockTransport gateway. Socket connects and DNS lookups are blocked.
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

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIFF_SCRIPT = ROOT / "scripts" / "eval_h3_tree_diff.py"


def _load_diff_module():
    spec = importlib.util.spec_from_file_location("eval_h3_tree_diff", DIFF_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_h3_tree_diff"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def diff_mod():
    try:
        yield _load_diff_module()
    finally:
        sys.modules.pop("eval_h3_tree_diff", None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    attempts: list[object] = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    return attempts


def _init_git_tree(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        ROOT / "src",
        dest / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(ROOT / "pyproject.toml", dest / "pyproject.toml")
    shutil.copy2(ROOT / ".gitignore", dest / ".gitignore")
    subprocess.run(["git", "-C", str(dest), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(dest), "add", "src", "pyproject.toml", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(dest), "commit", "-q", "-m", "init"], check=True)


def _commit_tree(dest: Path, message: str = "mutate") -> None:
    subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(dest), "commit", "-q", "-m", message], check=True)


SAMPLE_DOCS = [
    {
        "doc_id": "d_mixed_single_char",
        "direction": "zh->en",
        "genre": "story",
        "text": "椿和今汐是共鸣者。\n\n大家都喜欢那朵红椿蕊。",
    },
    {
        "doc_id": "d_unmixed_single_char",
        "direction": "zh->en",
        "genre": "story",
        "text": "椿和今汐在云陵谷。",
    },
    {
        "doc_id": "d_en_zh_low_multi_para",
        "direction": "en->zh",
        "genre": "guide",
        "text": "Camellya waited by the cliff.\n\nJinhsi checked the Echo data.",
    },
    {
        "doc_id": "d_en_zh_exact",
        "direction": "en->zh",
        "genre": "announcement",
        "text": "Shorekeeper",
    },
]

SENTINEL_SNIPPETS = (
    "红椿蕊",
    "云陵谷",
    "Camellya waited by the cliff",
    "Jinhsi checked the Echo data",
    "Shorekeeper",
    "今汐",
)


def _assert_no_raw_text_leak(report_text: str) -> None:
    for snippet in SENTINEL_SNIPPETS:
        assert snippet not in report_text, f"raw text leaked into report: {snippet!r}"


def test_identical_trees_pass_all_scenarios_without_text_leak(
    diff_mod, tmp_path: Path, sample_db: Path, no_network: list[object]
) -> None:
    tree_a = tmp_path / "tree_a"
    tree_b = tmp_path / "tree_b"
    _init_git_tree(tree_a)
    _init_git_tree(tree_b)

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema": "h3-manifest-v1", "set": "test", "documents": SAMPLE_DOCS}, ensure_ascii=False),
        encoding="utf-8",
    )
    out_json = tmp_path / "diff-report.json"

    code = diff_mod.main(
        [
            "run",
            "--tree-a",
            str(tree_a),
            "--tree-b",
            str(tree_b),
            "--terms-db",
            str(sample_db),
            "--manifest",
            str(manifest),
            "--skip-api-check",
            "--json-out",
            str(out_json),
        ]
    )
    assert code == 0
    raw_report = out_json.read_text(encoding="utf-8")
    _assert_no_raw_text_leak(raw_report)

    report = json.loads(raw_report)
    assert report["schema"] == "h3-tree-diff-v1"
    assert report["differential_clean"] is True
    assert report["corpus"]["documents_total"] == len(SAMPLE_DOCS)

    by_scenario = {s["scenario"]: s for s in report["scenarios"]}
    assert set(by_scenario) == {"default", "timeout_fallback", "paragraph_repair"}

    default_counts = by_scenario["default"]["counts"]
    assert default_counts["documents_matched"] == len(SAMPLE_DOCS)
    assert default_counts["documents_inconsistent"] == 0
    # Mixed single-char lock on d_mixed_single_char produces nonce-only raw variance
    # that must be verified 0-diff under deterministic fixed-nonce stage probing.
    assert default_counts["raw_payload_sha256_nonce_only_diffs"] > 0
    assert default_counts["raw_payload_sha256_real_diffs"] == 0
    assert default_counts["fixed_nonce_requests_verified"] > 0
    assert default_counts["fixed_nonce_request_diffs"] == 0

    assert by_scenario["timeout_fallback"]["counts"]["posts_a"] > default_counts["posts_a"]
    assert by_scenario["timeout_fallback"]["counts"]["documents_inconsistent"] == 0
    assert by_scenario["paragraph_repair"]["counts"]["posts_a"] > default_counts["posts_a"]
    assert by_scenario["paragraph_repair"]["counts"]["documents_inconsistent"] == 0
    assert no_network == []


def test_injected_prepared_terms_diff_is_localized_without_text_leak(
    diff_mod, tmp_path: Path, sample_db: Path
) -> None:
    tree_a = tmp_path / "tree_a"
    tree_b = tmp_path / "tree_b"
    _init_git_tree(tree_a)
    _init_git_tree(tree_b)

    # Inject a behavioral diff in tree_b: disable mixed single-char selective locking
    tp_path = tree_b / "src" / "wuwaterm" / "term_preparation.py"
    original = tp_path.read_text(encoding="utf-8")
    target = "if to_chinese is False and not defer_mixed_single_char_locking:"
    assert target in original
    mutated = original.replace(target, "if False and not defer_mixed_single_char_locking:", 1)
    tp_path.write_text(mutated, encoding="utf-8")
    _commit_tree(tree_b, "inject mixed lock diff")

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema": "h3-manifest-v1", "set": "test", "documents": SAMPLE_DOCS}, ensure_ascii=False),
        encoding="utf-8",
    )
    out_json = tmp_path / "diff-injected-locks.json"

    code = diff_mod.main(
        [
            "run",
            "--tree-a",
            str(tree_a),
            "--tree-b",
            str(tree_b),
            "--terms-db",
            str(sample_db),
            "--manifest",
            str(manifest),
            "--scenarios",
            "default",
            "--skip-api-check",
            "--json-out",
            str(out_json),
        ]
    )
    assert code == 1
    raw_report = out_json.read_text(encoding="utf-8")
    _assert_no_raw_text_leak(raw_report)

    report = json.loads(raw_report)
    assert report["differential_clean"] is False
    sc = report["scenarios"][0]
    assert sc["counts"]["documents_inconsistent"] == 1
    bad = sc["inconsistent_documents"][0]
    assert bad["doc_id"] == "d_mixed_single_char"
    expected_sha16 = hashlib.sha256(SAMPLE_DOCS[0]["text"].encode("utf-8")).hexdigest()[:16]
    assert bad["doc_sha16"] == expected_sha16
    loc = bad["stage_localization"]
    assert loc["earliest_diverging_stage"] == "prepared_terms"
    assert "locks" in loc["diverging_fields"]
    assert loc["fixed_nonce_request_diffs"] >= 1


def test_injected_request_spec_thinking_diff_is_localized(
    diff_mod, tmp_path: Path, sample_db: Path
) -> None:
    tree_a = tmp_path / "tree_a"
    tree_b = tmp_path / "tree_b"
    _init_git_tree(tree_a)
    _init_git_tree(tree_b)

    # Inject a thinking-mode diff for zh->en in tree_b's translation_plan.py
    tplan_path = tree_b / "src" / "wuwaterm" / "translation_plan.py"
    original = tplan_path.read_text(encoding="utf-8")
    old = "        if not to_chinese:\n            return self.configured_thinking_zh_en"
    new = (
        "        if not to_chinese:\n"
        '            return "low" if self.configured_thinking_zh_en == "disabled" else self.configured_thinking_zh_en'
    )
    assert old in original
    tplan_path.write_text(original.replace(old, new, 1), encoding="utf-8")
    _commit_tree(tree_b, "inject thinking mode diff")

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "h3-manifest-v1",
                "set": "test",
                "documents": [SAMPLE_DOCS[1], SAMPLE_DOCS[2]],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out_json = tmp_path / "diff-injected-thinking.json"

    code = diff_mod.main(
        [
            "run",
            "--tree-a",
            str(tree_a),
            "--tree-b",
            str(tree_b),
            "--terms-db",
            str(sample_db),
            "--manifest",
            str(manifest),
            "--scenarios",
            "default",
            "--skip-api-check",
            "--json-out",
            str(out_json),
        ]
    )
    assert code == 1
    raw_report = out_json.read_text(encoding="utf-8")
    _assert_no_raw_text_leak(raw_report)

    report = json.loads(raw_report)
    assert report["differential_clean"] is False
    sc = report["scenarios"][0]
    assert sc["counts"]["thinking_mode_diffs"] == 1
    assert sc["counts"]["normalized_payload_sha256_diffs"] == 1
    assert [d["doc_id"] for d in sc["inconsistent_documents"]] == ["d_unmixed_single_char"]
    loc = sc["inconsistent_documents"][0]["stage_localization"]
    assert loc["earliest_diverging_stage"] == "request_spec"
    assert "thinking_mode" in loc["diverging_fields"]


def test_synthetic_stress_set_and_blind_path_guard(diff_mod, tmp_path: Path, sample_db: Path) -> None:
    stress_docs = diff_mod.build_synthetic_single_char_stress_docs()
    ids = [d["doc_id"] for d in stress_docs]
    assert len(ids) == len(set(ids)) >= 18
    assert any(d["direction"] == "en->zh" for d in stress_docs)
    assert any("应世心" in d["text"] for d in stress_docs)
    assert any("心月狐" in d["text"] for d in stress_docs)
    assert any("椿的回音频段" in d["text"] for d in stress_docs)

    forbidden = tmp_path / "wuwaterm-eval" / "h3-20261126" / "manifest.json"
    with pytest.raises(diff_mod.DiffToolError, match="forbidden blind evaluation"):
        diff_mod.ensure_not_blind_path(forbidden)

    forbidden_real = tmp_path / "real-manuscript-eval-20261115" / "pool" / "item.json"
    with pytest.raises(diff_mod.DiffToolError, match="forbidden blind evaluation"):
        diff_mod.ensure_not_blind_path(forbidden_real)

    # CLI refuses forbidden manifest path with exit code 2
    code = diff_mod.main(
        [
            "run",
            "--tree-a",
            str(ROOT),
            "--tree-b",
            str(ROOT),
            "--manifest",
            str(forbidden),
            "--terms-db",
            str(sample_db),
            "--skip-api-check",
        ]
    )
    assert code == 2


def test_v014_api_check_fails_closed_when_pins_do_not_match(
    diff_mod, tmp_path: Path, sample_db: Path
) -> None:
    # sample_db does not match the pinned production dictionary_revision or term_count
    result = diff_mod.run_v014_api_check_subprocess(bound_tree=ROOT, terms_db=sample_db)
    assert result["passed"] is False
    assert result["http_meta_status"] == 200
    assert result["http_review_status"] == 200
    assert result["term_count"] != diff_mod.EXPECTED_V014_TERM_COUNT
