"""Tests for scripts/eval_rag_offline.py (zero-cost offline RAG evaluation harness)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import eval_rag_offline

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "eval_rag_offline.py"


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("network disabled in offline RAG eval tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_synthetic_offline_rag_eval_meets_all_six_deterministic_metrics():
    report = eval_rag_offline.run_synthetic_eval(iterations=2)

    assert report["schema_version"] == eval_rag_offline.SCHEMA_VERSION
    assert report["mode"] == "synthetic"
    assert report["gates_passed"] is True

    # 1. Ordinary-word false-lock and false-injection rate must be strictly 0.0%.
    neg = report["ordinary_word_false_lock"]
    assert neg["total_negative_cases"] >= 7
    assert neg["false_lock_rate"] == 0.0
    assert neg["false_injection_rate"] == 0.0
    assert neg["false_locks"] == 0
    assert neg["false_injections"] == 0
    by_id = {c["case_id"]: c for c in neg["cases"]}
    for required_neg in (
        "neg_xiangchun",
        "neg_chouchun",
        "neg_xinqing",
        "neg_encore",
    ):
        assert required_neg in by_id
        assert by_id[required_neg]["false_lock"] is False
        assert by_id[required_neg]["false_injection"] is False
        assert by_id[required_neg]["graph_triggers"] == 0

    # 2. Domain entity & short-term recall coverage lift + 1-hop disambiguation.
    rec = report["domain_recall"]
    assert rec["with_knowledge_rag"]["recall_rate"] > rec["baseline_official_only"]["recall_rate"]
    assert rec["coverage_lift"] > 0.0
    assert rec["short_term_recall"]["rag_rate"] > rec["short_term_recall"]["baseline_rate"]
    assert rec["graph_disambiguation"]["positive_passed"] is True
    assert rec["graph_disambiguation"]["negative_passed"] is True

    # 3. Lock-isolation integrity: 100% lock_eligible == False, zero span conflicts,
    # and byte-identical default OperationIdentity.
    iso = report["lock_isolation_integrity"]
    assert iso["total_candidates_checked"] > 0
    assert iso["all_lock_eligible_false"] is True
    assert iso["zero_lock_span_conflicts"] is True
    assert iso["integrity_rate"] == 1.0
    assert iso["default_operation_identity_unchanged"] is True
    assert iso["rag_locks_isolated"] is True

    # 4. Query latency P50 / P95 (ms).
    lat = report["latency_ms"]
    assert lat["samples"] > 0
    assert lat["baseline_p50_ms"] >= 0.0
    assert lat["baseline_p95_ms"] >= lat["baseline_p50_ms"]
    assert lat["rag_p50_ms"] >= 0.0
    assert lat["rag_p95_ms"] >= lat["rag_p50_ms"]

    # 5. Companion KB & 1-hop graph memory telemetry.
    mem = report["memory_mib"]
    assert mem["kb_and_graph_rss_delta_mib"] >= 0.0
    assert mem["graph_tracemalloc_mib"] < 3.0
    assert mem["kb_file_size_mib"] > 0.0
    assert mem["graph_node_count"] >= 2
    assert mem["graph_edge_count"] >= 2

    # 6. Markdown summary rendering.
    md = eval_rag_offline.format_markdown_report(report)
    assert "WuwaTerm Offline RAG Evaluation (synthetic — PASS)" in md
    assert "Ordinary-word false-lock rate" in md
    assert "Lock-isolation integrity" in md
    assert "Query latency P50 / P95" in md


def test_eval_rag_offline_cli_synthetic_and_explicit_db_paths(tmp_path: Path):
    json_out = tmp_path / "rag_eval.json"
    md_out = tmp_path / "rag_eval.md"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--synthetic",
            "--iterations",
            "2",
            "--strict",
            "--format",
            "json",
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(md_out),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stderr == ""
    parsed_stdout = json.loads(proc.stdout)
    parsed_file = json.loads(json_out.read_text(encoding="utf-8"))
    assert parsed_stdout == parsed_file
    assert parsed_file["gates_passed"] is True
    assert md_out.is_file()
    assert "PASS" in md_out.read_text(encoding="utf-8")

    # Explicit --terms-db + --knowledge-db ("real" mode) works against DB files.
    terms_db, kb_db = eval_rag_offline._build_synthetic_fixture(tmp_path / "explicit")
    real_proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--terms-db",
            str(terms_db),
            "--knowledge-db",
            str(kb_db),
            "--iterations",
            "1",
            "--strict",
            "--format",
            "json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert real_proc.returncode == 0, real_proc.stderr or real_proc.stdout
    real_report = json.loads(real_proc.stdout)
    assert real_report["mode"] == "real"
    assert real_report["gates_passed"] is True

    # Missing database exits cleanly with code 2.
    missing_proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--terms-db",
            str(tmp_path / "missing_terms.db"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing_proc.returncode == 2
