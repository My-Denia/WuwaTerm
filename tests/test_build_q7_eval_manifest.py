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
    assert len(zh_row["request_identity_sha256"]) == 64
    assert len(zh_row["normalized_request_identity_sha256"]) == 64
    assert len(zh_row["operation_identity_sha256"]) == 64
    assert zh_row["request_identity_sha256"] == zh_row["normalized_request_identity_sha256"]

    assert en_row["doc_id"] == "SYN-002"
    assert en_row["direction"] == "en-zh"
    assert en_row["thinking"] == "omit"
    assert en_row["timeout_seconds"] == 45.0
    assert en_row["has_random_locks"] is True
    assert en_row["lock_count"] == 1
    assert en_row["reuse_status"] == "must_post"
    assert en_row["reuse_reason"] == "random_lock_placeholder_nonce"
    assert len(en_row["request_identity_sha256"]) == 64
    assert len(en_row["operation_identity_sha256"]) == 64

    # 1) Legacy archive containing only adr0020_reuse_key (no operation/request
    # identity): SYN-001 becomes reusable_legacy_adr0020 (never reusable_exact);
    # SYN-002 (random lock) stays must_post.
    archived_legacy_path = tmp_path / "archived-legacy.json"
    archived_legacy_path.write_text(
        json.dumps(
            {
                "arm": "prior-legacy",
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

    legacy_second = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-test",
            db_path=db_path,
            input_paths=[inputs_path],
            archived_paths=[archived_legacy_path],
        )
    )
    assert legacy_second["summary"]["reusable_exact_segments"] == 0
    assert legacy_second["summary"]["reusable_legacy_adr0020_segments"] == 1
    assert legacy_second["summary"]["must_post_segments"] == 1
    zh_legacy, en_legacy = legacy_second["requests"]
    assert zh_legacy["reuse_status"] == "reusable_legacy_adr0020"
    assert zh_legacy["reuse_reason"] == "legacy_adr0020_reuse_key_match_only"
    assert en_legacy["reuse_status"] == "must_post"
    assert en_legacy["reuse_reason"] == "random_lock_placeholder_nonce"

    # 2) Modern archive with OperationIdentity: exact operation_identity_sha256
    # yields reusable_exact while SYN-002 (random lock) still stays must_post.
    archived_modern_path = tmp_path / "archived-modern.json"
    archived_modern_path.write_text(
        json.dumps(
            {
                "arm": "prior-modern",
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
                                "request_identity_sha256": zh_row["request_identity_sha256"],
                                "operation_identity_sha256": zh_row["operation_identity_sha256"],
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
                                "request_identity_sha256": en_row["request_identity_sha256"],
                                "operation_identity_sha256": en_row["operation_identity_sha256"],
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
            archived_paths=[archived_modern_path],
        )
    )
    assert second["summary"]["reusable_exact_segments"] == 1
    assert second["summary"]["reusable_raw_request_only_segments"] == 0
    assert second["summary"]["reusable_legacy_adr0020_segments"] == 0
    assert second["summary"]["must_post_segments"] == 1
    zh_second, en_second = second["requests"]
    assert zh_second["reuse_status"] == "reusable_exact"
    assert zh_second["reuse_reason"] == "exact_operation_identity_match"
    assert en_second["reuse_status"] == "must_post"
    assert en_second["reuse_reason"] == "random_lock_placeholder_nonce"
    # Verify no source text leaked into the manifest JSON.
    rendered = json.dumps(second, ensure_ascii=False)
    assert "变奏入场" not in rendered
    assert "Collect Astrite" not in rendered


def test_manifest_reuse_decision_honors_operation_identity_timeout_status_and_raw_match(
    tmp_path: Path,
):
    db_path = _build_synthetic_db(tmp_path)
    inputs_path = _write_inputs(tmp_path)

    base = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-45s",
            db_path=db_path,
            input_paths=[inputs_path],
            timeout_seconds=45.0,
        )
    )
    zh_base = base["requests"][0]

    modern_45s = tmp_path / "modern-45s.json"
    modern_45s.write_text(
        json.dumps(
            {
                "arm": "prior-45s",
                "core_commit": "prior-sha",
                "db_sha256": base["db_sha256"],
                "documents": [
                    {
                        "doc_id": "SYN-001",
                        "direction": "zh-en",
                        "status": "complete",
                        "segments": [
                            {
                                "reuse_key": zh_base["adr0020_reuse_key"],
                                "request_identity_sha256": zh_base["request_identity_sha256"],
                                "operation_identity_sha256": zh_base["operation_identity_sha256"],
                                "raw_content": "Jinhsi switches in with Intro Skill.",
                                "translation": "Jinhsi switches in with Intro Skill.",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # F1 counterexample: identical payload + identical adr0020_reuse_key, but timeout changed 45s -> 10s.
    timeout_10s = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-10s",
            db_path=db_path,
            input_paths=[inputs_path],
            timeout_seconds=10.0,
            archived_paths=[modern_45s],
        )
    )
    zh_10s = timeout_10s["requests"][0]
    assert zh_10s["adr0020_reuse_key"] == zh_base["adr0020_reuse_key"]
    assert zh_10s["reuse_status"] == "must_post"
    assert zh_10s["reuse_reason"] == "operation_and_request_identity_mismatch"

    # Matching request_identity_sha256 with mismatched operation_identity_sha256 -> raw request only.
    raw_only_archive = tmp_path / "raw-only.json"
    raw_only_archive.write_text(
        json.dumps(
            {
                "arm": "prior-raw-only",
                "core_commit": "prior-sha",
                "db_sha256": base["db_sha256"],
                "documents": [
                    {
                        "doc_id": "SYN-001",
                        "direction": "zh-en",
                        "status": "complete",
                        "segments": [
                            {
                                "reuse_key": zh_base["adr0020_reuse_key"],
                                "request_identity_sha256": zh_base["request_identity_sha256"],
                                "operation_identity_sha256": "0" * 64,
                                "raw_content": "Jinhsi switches in with Intro Skill.",
                                "translation": "Jinhsi switches in with Intro Skill.",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    raw_only_manifest = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-raw",
            db_path=db_path,
            input_paths=[inputs_path],
            archived_paths=[raw_only_archive],
        )
    )
    assert raw_only_manifest["summary"]["reusable_exact_segments"] == 0
    assert raw_only_manifest["summary"]["reusable_raw_request_only_segments"] == 1
    assert raw_only_manifest["requests"][0]["reuse_status"] == "reusable_raw_request_only"
    assert (
        raw_only_manifest["requests"][0]["reuse_reason"]
        == "request_identity_match_operation_mismatch"
    )

    # Incomplete archived document must never become reusable even if keys match.
    partial_archive = tmp_path / "partial.json"
    partial_archive.write_text(
        json.dumps(
            {
                "arm": "partial",
                "core_commit": "prior-sha",
                "db_sha256": base["db_sha256"],
                "documents": [
                    {
                        "doc_id": "SYN-001",
                        "direction": "zh-en",
                        "status": "partial",
                        "segments": [
                            {
                                "reuse_key": zh_base["adr0020_reuse_key"],
                                "request_identity_sha256": zh_base["request_identity_sha256"],
                                "operation_identity_sha256": zh_base["operation_identity_sha256"],
                                "raw_content": "Jinhsi switches in with Intro Skill.",
                                "translation": "Jinhsi switches in with Intro Skill.",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    partial_manifest = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-partial",
            db_path=db_path,
            input_paths=[inputs_path],
            archived_paths=[partial_archive],
        )
    )
    assert partial_manifest["summary"]["reusable_exact_segments"] == 0
    assert partial_manifest["requests"][0]["reuse_status"] == "must_post"
    assert partial_manifest["requests"][0]["reuse_reason"] == "archived_status_partial"


def test_build_arm_manifest_fails_closed_when_segment_issues_multiple_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from wuwaterm import sentence

    db_path = _build_synthetic_db(tmp_path)
    inputs_path = _write_inputs(tmp_path)
    orig_translate = sentence.SentenceTranslator.translate_async

    async def double_posting_translate(self, text, *args, **kwargs):
        await orig_translate(self, text, *args, **kwargs)
        return await orig_translate(self, text, *args, **kwargs)

    monkeypatch.setattr(sentence.SentenceTranslator, "translate_async", double_posting_translate)
    with pytest.raises(RuntimeError, match="at most 1 POST per segment, captured 2"):
        asyncio.run(
            manifest_mod.build_arm_manifest_async(
                arm_name="q7-double-post",
                db_path=db_path,
                input_paths=[inputs_path],
            )
        )


def test_paragraph_group_ranges_for_d009_diagnostic_split():
    assert manifest_mod.paragraph_group_ranges(30, 10) == [[1, 10], [11, 20], [21, 30]]
    assert manifest_mod.paragraph_group_ranges(25, 10) == [[1, 10], [11, 20], [21, 25]]


def test_build_arm_manifest_identities_reflect_thinking_and_policy_changes(tmp_path: Path):
    db_path = _build_synthetic_db(tmp_path)
    inputs_path = _write_inputs(tmp_path)

    omit_manifest = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-omit",
            db_path=db_path,
            input_paths=[inputs_path],
            en_zh_thinking="omit",
            deterministic_placeholder_hex="0123456789abcdef",
        )
    )
    auto_manifest = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-auto",
            db_path=db_path,
            input_paths=[inputs_path],
            en_zh_thinking="auto",
            deterministic_placeholder_hex="0123456789abcdef",
        )
    )

    assert omit_manifest["policy_sha256"] != auto_manifest["policy_sha256"]
    assert omit_manifest["provider_profile_sha256"] == auto_manifest["provider_profile_sha256"]

    zh_omit, en_omit = omit_manifest["requests"]
    zh_auto, en_auto = auto_manifest["requests"]
    assert zh_omit["request_identity_sha256"] == zh_auto["request_identity_sha256"]
    assert zh_omit["operation_identity_sha256"] == zh_auto["operation_identity_sha256"]
    assert en_omit["request_identity_sha256"] != en_auto["request_identity_sha256"]
    assert en_omit["operation_identity_sha256"] != en_auto["operation_identity_sha256"]
    assert "变奏入场" not in json.dumps(auto_manifest, ensure_ascii=False)
    assert "Collect Astrite" not in json.dumps(auto_manifest, ensure_ascii=False)


def test_archive_valid_translation_context_chaining_and_dictionary_hit(tmp_path: Path):
    db_path = _build_synthetic_db(tmp_path)

    chain_text = ("今天天气不错，大家准备出发。" * 140) + "\n\n今汐切换角色触发变奏入场。"
    chain_bundle = tmp_path / "chain-inputs.json"
    chain_bundle.write_text(
        json.dumps(
            {
                "schema": "usability-run-inputs-v1",
                "documents": [
                    {
                        "doc_id": "CHAIN-001",
                        "set": "D",
                        "direction": "zh-en",
                        "text": chain_text,
                        "source_sha256": manifest_mod.sha256_text(chain_text),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    real_seed = tmp_path / "real-seed.json"
    real_seed.write_text(
        json.dumps(
            {
                "arm": "real-seed",
                "documents": [
                    {
                        "doc_id": "CHAIN-001",
                        "direction": "zh-en",
                        "status": "complete",
                        "segments": [
                            {"translation": "Jinhsi is ready."},
                            {"translation": "Jinhsi switches in with Intro Skill."},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    conflict_seed = tmp_path / "conflict-seed.json"
    conflict_seed.write_text(
        json.dumps(
            {
                "arm": "conflict-seed",
                "documents": [
                    {
                        "doc_id": "CHAIN-001",
                        "direction": "zh-en",
                        "status": "complete",
                        "segments": [{"translation": "CONFLICTING_SEED_SHOULD_BE_IGNORED"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    seeded_manifest = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-seeded",
            db_path=db_path,
            input_paths=[chain_bundle],
            prior_context_paths=[real_seed],
        )
    )
    seg1_base, seg2_base = seeded_manifest["requests"]

    # 1) Missing, empty, or whitespace-only translation in a complete archive with
    # matching OperationIdentity & raw_content cannot be marked reusable_exact,
    # and breaks downstream exact chaining on segment 2.
    for bad_translation in (None, "", "   "):
        bad_archive = tmp_path / f"bad-translation-{repr(bad_translation)}.json"
        bad_archive.write_text(
            json.dumps(
                {
                    "arm": "bad-tr",
                    "core_commit": "prior-sha",
                    "db_sha256": seeded_manifest["db_sha256"],
                    "documents": [
                        {
                            "doc_id": "CHAIN-001",
                            "direction": "zh-en",
                            "status": "complete",
                            "segments": [
                                {
                                    "reuse_key": seg1_base["adr0020_reuse_key"],
                                    "request_identity_sha256": seg1_base["request_identity_sha256"],
                                    "operation_identity_sha256": seg1_base["operation_identity_sha256"],
                                    "raw_content": "Jinhsi is ready.",
                                    "translation": bad_translation,
                                },
                                {
                                    "reuse_key": seg2_base["adr0020_reuse_key"],
                                    "request_identity_sha256": seg2_base["request_identity_sha256"],
                                    "operation_identity_sha256": seg2_base["operation_identity_sha256"],
                                    "raw_content": "Jinhsi switches in with Intro Skill.",
                                    "translation": "Jinhsi switches in with Intro Skill.",
                                },
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        bad_manifest = asyncio.run(
            manifest_mod.build_arm_manifest_async(
                arm_name="q7-bad-tr",
                db_path=db_path,
                input_paths=[chain_bundle],
                archived_paths=[bad_archive],
            )
        )
        s1, s2 = bad_manifest["requests"]
        assert s1["reuse_status"] == "must_post"
        assert s1["reuse_reason"] == "archived_missing_valid_translation"
        assert s2["reuse_status"] == "must_post"
        assert s2["reuse_reason"] == "prior_segment_not_reused"
        assert bad_manifest["summary"]["reusable_exact_segments"] == 0
        assert bad_manifest["summary"]["non_exact_model_segments"] == 2

    # 2) Valid reusable_exact seg 1 chains its archived translation into seg 2,
    # overriding conflicting prior_translations and allowing seg 2 to be reusable_exact.
    valid_archive = tmp_path / "valid-chain.json"
    valid_archive.write_text(
        json.dumps(
            {
                "arm": "valid-chain",
                "core_commit": "prior-sha",
                "db_sha256": seeded_manifest["db_sha256"],
                "documents": [
                    {
                        "doc_id": "CHAIN-001",
                        "direction": "zh-en",
                        "status": "complete",
                        "segments": [
                            {
                                "reuse_key": seg1_base["adr0020_reuse_key"],
                                "request_identity_sha256": seg1_base["request_identity_sha256"],
                                "operation_identity_sha256": seg1_base["operation_identity_sha256"],
                                "raw_content": "Jinhsi is ready.",
                                "translation": "Jinhsi is ready.",
                            },
                            {
                                "reuse_key": seg2_base["adr0020_reuse_key"],
                                "request_identity_sha256": seg2_base["request_identity_sha256"],
                                "operation_identity_sha256": seg2_base["operation_identity_sha256"],
                                "raw_content": "Jinhsi switches in with Intro Skill.",
                                "translation": "Jinhsi switches in with Intro Skill.",
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    exact_chain_manifest = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-exact-chain",
            db_path=db_path,
            input_paths=[chain_bundle],
            archived_paths=[valid_archive],
            prior_context_paths=[conflict_seed],
        )
    )
    assert [r["reuse_status"] for r in exact_chain_manifest["requests"]] == [
        "reusable_exact",
        "reusable_exact",
    ]
    assert exact_chain_manifest["summary"]["reusable_exact_segments"] == 2
    assert exact_chain_manifest["summary"]["non_exact_model_segments"] == 0

    # Non-exact seg 1 (raw_request_only or legacy_adr0020) keeps seg 2 must_post even
    # when diagnostic prior_translations supplies the seg 1 translation.
    raw_seg1_archive = tmp_path / "raw-seg1.json"
    raw_seg1_archive.write_text(
        json.dumps(
            {
                "arm": "raw-seg1",
                "core_commit": "prior-sha",
                "db_sha256": seeded_manifest["db_sha256"],
                "documents": [
                    {
                        "doc_id": "CHAIN-001",
                        "direction": "zh-en",
                        "status": "complete",
                        "segments": [
                            {
                                "reuse_key": seg1_base["adr0020_reuse_key"],
                                "request_identity_sha256": seg1_base["request_identity_sha256"],
                                "operation_identity_sha256": "0" * 64,
                                "raw_content": "Jinhsi is ready.",
                                "translation": "Jinhsi is ready.",
                            },
                            {
                                "reuse_key": seg2_base["adr0020_reuse_key"],
                                "request_identity_sha256": seg2_base["request_identity_sha256"],
                                "operation_identity_sha256": seg2_base["operation_identity_sha256"],
                                "raw_content": "Jinhsi switches in with Intro Skill.",
                                "translation": "Jinhsi switches in with Intro Skill.",
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    raw_chain_manifest = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-raw-chain",
            db_path=db_path,
            input_paths=[chain_bundle],
            archived_paths=[raw_seg1_archive],
            prior_context_paths=[real_seed],
        )
    )
    r1, r2 = raw_chain_manifest["requests"]
    assert r1["reuse_status"] == "reusable_raw_request_only"
    assert r2["reuse_status"] == "must_post"
    assert r2["reuse_reason"] == "prior_segment_not_reused"
    raw_sum = raw_chain_manifest["summary"]
    assert raw_sum["non_exact_model_segments"] == 2
    assert (
        raw_sum["must_post_segments"]
        + raw_sum["reusable_raw_request_only_segments"]
        + raw_sum["reusable_legacy_adr0020_segments"]
        == raw_sum["non_exact_model_segments"]
    )
    assert raw_sum["reusable_exact_segments"] + raw_sum["non_exact_model_segments"] == raw_sum["model_call_segments"]

    # 3) Exact dictionary hit uses current dictionary answer as context before.target,
    # never stale prior_translations.
    dict_text = "今汐\n\n" + ("测试内容" * 450)
    dict_bundle = tmp_path / "dict-inputs.json"
    dict_bundle.write_text(
        json.dumps(
            {
                "schema": "usability-run-inputs-v1",
                "documents": [
                    {
                        "doc_id": "DICT-001",
                        "set": "D",
                        "direction": "zh-en",
                        "text": dict_text,
                        "source_sha256": manifest_mod.sha256_text(dict_text),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stale_dict_prior = tmp_path / "stale-dict-prior.json"
    stale_dict_prior.write_text(
        json.dumps(
            {
                "arm": "stale-prior",
                "documents": [
                    {
                        "doc_id": "DICT-001",
                        "direction": "zh-en",
                        "status": "complete",
                        "segments": [{"translation": "StaleOldName"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    dict_without_prior = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-dict-clean",
            db_path=db_path,
            input_paths=[dict_bundle],
        )
    )
    dict_with_stale_prior = asyncio.run(
        manifest_mod.build_arm_manifest_async(
            arm_name="q7-dict-stale",
            db_path=db_path,
            input_paths=[dict_bundle],
            prior_context_paths=[stale_dict_prior],
        )
    )
    assert dict_without_prior["requests"][0]["reuse_status"] == "exact_dictionary_hit"
    assert dict_without_prior["requests"][1]["request_identity_sha256"] == dict_with_stale_prior["requests"][1]["request_identity_sha256"]
    assert dict_without_prior["summary"]["dictionary_only_segments"] == 1
    assert dict_without_prior["summary"]["non_exact_model_segments"] == 1
    assert (
        dict_without_prior["summary"]["reusable_exact_segments"]
        + dict_without_prior["summary"]["non_exact_model_segments"]
        == dict_without_prior["summary"]["model_call_segments"]
    )
