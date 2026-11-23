"""Offline tests for scripts/build_q7_eval_manifest.py (zero network, zero live LLM)."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest

from scripts import build_q7_eval_manifest as manifest_mod
from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.models import TermRecord


def _build_synthetic_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "synthetic-terms.db"
    with connect(db_path) as conn:
        initialize(conn)
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (("schema_version", "2"), ("source_commit", "synthetic-fixture")),
        )
        insert_records(
            conn,
            [
                TermRecord("core_term", "BinData/skill/skilltype.json", "5", "SkillType_5_TypeName", "变奏技能", "Intro Skill"),
                TermRecord("resonator", "fixture", "1", "1", "今汐", "Jinhsi"),
                TermRecord("item", "fixture", "2", "2", "星声", "Astrite"),
            ],
        )
        conn.commit()
    return db_path


def _write_inputs(tmp_path: Path) -> Path:
    doc1 = "今汐切换角色触发变奏入场。"
    doc2 = "Collect Astrite with Jinhsi."
    bundle = {
        "schema": "usability-run-inputs-v1",
        "documents": [
            {
                "doc_id": "SYN-001",
                "set": "D",
                "direction": "zh-en",
                "text": doc1,
                "source_sha256": manifest_mod.sha256_text(doc1),
            },
            {
                "doc_id": "SYN-002",
                "set": "D",
                "direction": "en-zh",
                "text": doc2,
                "source_sha256": manifest_mod.sha256_text(doc2),
            },
        ],
    }
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    return path


def test_offline_network_guard_blocks_sockets():
    with manifest_mod.offline_network_guard():
        with pytest.raises(RuntimeError, match="forbids all network access"):
            socket.getaddrinfo("example.invalid", 443)


def test_build_arm_manifest_captures_reuse_keys_and_forbids_random_lock_reuse(tmp_path: Path):
    db_path = _build_synthetic_db(tmp_path)
    inputs_path = _write_inputs(tmp_path)

    first = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-test",
            db_path=db_path,
            input_paths=[inputs_path],
        )
    )
    assert first["offline_only"] is True
    assert first["live_llm_calls"] == 0
    assert first["summary"]["total_segments"] == 2
    assert first["summary"]["must_post_segments"] == 2
    assert first["summary"]["reusable_exact_segments"] == 0
    assert first["summary"]["random_lock_segments"] == 1
    assert first["summary"]["scoped_skill_stem_segments"] == 1

    zh_row, en_row = first["requests"]
    assert zh_row["doc_id"] == "SYN-001"
    assert zh_row["direction"] == "zh-en"
    assert zh_row["thinking"] == "disabled"
    assert zh_row["timeout_seconds"] == 45.0
    assert zh_row["has_random_locks"] is False
    assert zh_row["scoped_skill_stem_count"] == 1
    assert zh_row["reuse_status"] == "must_post"

    assert en_row["doc_id"] == "SYN-002"
    assert en_row["direction"] == "en-zh"
    assert en_row["thinking"] == "omit"
    assert en_row["timeout_seconds"] == 45.0
    assert en_row["has_random_locks"] is True
    assert en_row["lock_count"] == 1
    assert en_row["reuse_status"] == "must_post"
    assert en_row["reuse_reason"] == "random_lock_placeholder_nonce"

    # Build a synthetic archived output matching both adr0020_reuse_key values.
    # SYN-001 (no random lock) must become reusable_exact; SYN-002 (random lock)
    # must stay must_post even if an archived entry exists.
    archived_path = tmp_path / "archived.json"
    archived_path.write_text(
        json.dumps(
            {
                "arm": "prior",
                "core_commit": "prior-sha",
                "db_sha256": first["db_sha256"],
                "documents": [
                    {
                        "doc_id": "SYN-001",
                        "direction": "zh-en",
                        "status": "complete",
                        "segments": [
                            {
                                "reuse_key": zh_row["adr0020_reuse_key"],
                                "raw_content": "Jinhsi switches in with Intro Skill.",
                                "translation": "Jinhsi switches in with Intro Skill.",
                            }
                        ],
                    },
                    {
                        "doc_id": "SYN-002",
                        "direction": "en-zh",
                        "status": "complete",
                        "segments": [
                            {
                                "reuse_key": en_row["adr0020_reuse_key"],
                                "raw_content": "收集占位符。",
                                "translation": "收集星声。",
                            }
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    second = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-test",
            db_path=db_path,
            input_paths=[inputs_path],
            archived_paths=[archived_path],
        )
    )
    assert second["summary"]["reusable_exact_segments"] == 1
    assert second["summary"]["must_post_segments"] == 1
    zh_second, en_second = second["requests"]
    assert zh_second["reuse_status"] == "reusable_exact"
    assert zh_second["reuse_reason"] == "exact_adr0020_reuse_key_match"
    assert en_second["reuse_status"] == "must_post"
    assert en_second["reuse_reason"] == "random_lock_placeholder_nonce"
    # Verify no source text leaked into the manifest JSON.
    rendered = json.dumps(second, ensure_ascii=False)
    assert "变奏入场" not in rendered
    assert "Collect Astrite" not in rendered


def test_paragraph_group_ranges_for_d009_diagnostic_split():
    assert manifest_mod.paragraph_group_ranges(30, 10) == [[1, 10], [11, 20], [21, 30]]
    assert manifest_mod.paragraph_group_ranges(25, 10) == [[1, 10], [11, 20], [21, 25]]
