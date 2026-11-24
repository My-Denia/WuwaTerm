#!/usr/bin/env python3
"""Build an offline, zero-LLM per-request reuse_key manifest for q7 evaluation.

This script runs strictly offline (sockets are blocked before any translation
code runs, and every model call hits an in-memory ``httpx.MockTransport``). It
captures the exact system prompt, user prompt, payload, thinking parameters,
timeout, dictionary hash and matcher revision for every segment in one or more
``usability-run-inputs-v1`` JSON bundles, computes both the explicit manifest
``reuse_key`` and the ADR 0020 ``adr0020_reuse_key``, and classifies whether
each segment can legally reuse an archived response or must issue a fresh POST.

No corpus text, reference text or model output text is written to any artifact;
only document IDs, counts, SHA-256 digests and structured classification codes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import secrets
import socket
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wuwaterm.translation_plan import (  # noqa: E402
    DEFAULT_PROVIDER_PROFILE,
    canonical_json,
    normalize_term_placeholders as normalize_placeholders,
    sha256_bytes,
    sha256_text,
)

_WS = set("\t\n\x0b\x0c\r \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000")

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_TIMEOUT_SECONDS = 45.0
DEFAULT_ZH_EN_THINKING = "disabled"
DEFAULT_EN_ZH_THINKING = "omit"
DEFAULT_D009_GROUP_SIZE = 10


def split_paragraphs(text: str) -> list[str]:
    """Split text on segmentation-v1 paragraph breaks (>= 2 U+000A in a WS run)."""
    pieces: list[str] = []
    start = i = 0
    n = len(text)
    while i < n:
        if text[i] in _WS:
            j = i
            while j < n and text[j] in _WS:
                j += 1
            if text[i:j].count("\n") >= 2 and i > 0 and j < n:
                pieces.append(text[start:i])
                start = j
            i = j
        else:
            i += 1
    pieces.append(text[start:])
    ws = "".join(_WS)
    return [piece.strip(ws) for piece in pieces]


def paragraph_group_ranges(paragraph_count: int, group_size: int = DEFAULT_D009_GROUP_SIZE) -> list[list[int]]:
    """Deterministic 1-based inclusive [start, end] paragraph groups for diagnostics."""
    if group_size < 1:
        raise ValueError("group_size must be >= 1")
    return [
        [start, min(start + group_size - 1, paragraph_count)]
        for start in range(1, paragraph_count + 1, group_size)
    ]


@contextlib.contextmanager
def offline_network_guard() -> Iterator[None]:
    """Fail closed if any code attempts network I/O during manifest generation."""

    def _deny(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError("offline manifest generator forbids all network access")

    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_create = socket.create_connection
    orig_getaddrinfo = socket.getaddrinfo
    socket.socket.connect = _deny  # type: ignore[method-assign]
    socket.socket.connect_ex = _deny  # type: ignore[method-assign]
    socket.create_connection = _deny  # type: ignore[assignment]
    socket.getaddrinfo = _deny  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = orig_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = orig_connect_ex  # type: ignore[method-assign]
        socket.create_connection = orig_create  # type: ignore[assignment]
        socket.getaddrinfo = orig_getaddrinfo  # type: ignore[assignment]


@contextlib.contextmanager
def _scoped_env(overrides: dict[str, str]) -> Iterator[None]:
    previous: dict[str, str | None] = {}
    for key in list(os.environ):
        if key.startswith(("WUWATERM_", "OPENAI_")):
            previous[key] = os.environ.pop(key)
    for key, value in overrides.items():
        previous.setdefault(key, None)
        os.environ[key] = value
    try:
        yield
    finally:
        for key in list(os.environ):
            if key.startswith(("WUWATERM_", "OPENAI_")) and key not in previous:
                os.environ.pop(key, None)
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def compute_manifest_reuse_key(
    *,
    model: str,
    direction: str,
    thinking: Any,
    timeout_seconds: float,
    system_prompt_sha256: str,
    user_prompt_sha256: str,
    db_sha256: str,
    matcher_revision: str,
) -> str:
    """Explicit q7 manifest reuse key including prompt hashes, thinking and timeout."""
    return sha256_bytes(
        canonical_json(
            {
                "db_sha256": db_sha256,
                "direction": direction,
                "matcher_revision": matcher_revision,
                "model": model,
                "system_prompt_sha256": system_prompt_sha256,
                "thinking": thinking,
                "timeout_seconds": float(timeout_seconds),
                "user_prompt_sha256": user_prompt_sha256,
            }
        )
    )


def compute_adr0020_reuse_key(
    *,
    core_sha256: str,
    direction: str,
    payload_sha256: str,
    db_sha256: str,
    matcher_revision: str,
    references: list[list[str]],
    model: str,
    temperature: int | float,
    thinking_payload: Any,
) -> str:
    """Exact ADR 0020 / run_usability.py / run_q6.py segment reuse key."""
    key_parts = [
        core_sha256,
        direction,
        payload_sha256,
        db_sha256,
        matcher_revision,
        references,
        model,
        temperature,
        thinking_payload,
    ]
    return sha256_bytes(canonical_json(key_parts))


def load_archived_index(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    """Index archived raw_outputs.json files by doc/segment and by adr0020_reuse_key."""
    by_reuse_key: dict[str, dict[str, Any]] = {}
    by_doc_segment: dict[tuple[str, str, int], dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    for path in paths:
        data = json.loads(path.read_bytes())
        sources.append(
            {
                "arm": data.get("arm"),
                "core_commit": data.get("core_commit"),
                "db_sha256": data.get("db_sha256"),
                "file_sha256": sha256_bytes(path.read_bytes()),
                "documents": len(data.get("documents", [])),
            }
        )
        for doc in data.get("documents", []):
            doc_id = str(doc.get("doc_id", ""))
            direction = str(doc.get("direction", ""))
            status = str(doc.get("status", ""))
            calls = doc.get("calls", [])
            segments = doc.get("segments", [])
            if not segments:
                by_doc_segment[(doc_id, direction, 1)] = {
                    "status": status,
                    "has_raw_content": False,
                    "reuse_key": None,
                    "body_sha256": calls[0].get("body_sha256") if calls else None,
                    "translation": None,
                    "core_commit": data.get("core_commit"),
                    "db_sha256": data.get("db_sha256"),
                }
                continue
            for idx, seg in enumerate(segments, start=1):
                call = calls[idx - 1] if idx - 1 < len(calls) else {}
                entry = {
                    "status": status,
                    "has_raw_content": seg.get("raw_content") is not None,
                    "reuse_key": seg.get("reuse_key"),
                    "body_sha256": call.get("body_sha256"),
                    "translation": seg.get("translation"),
                    "core_commit": data.get("core_commit"),
                    "db_sha256": data.get("db_sha256"),
                }
                by_doc_segment[(doc_id, direction, idx)] = entry
                if seg.get("reuse_key") and seg.get("raw_content") is not None:
                    by_reuse_key[str(seg["reuse_key"])] = {
                        "doc_id": doc_id,
                        "direction": direction,
                        "segment_index": idx,
                        "core_commit": data.get("core_commit"),
                        "db_sha256": data.get("db_sha256"),
                        "translation": seg.get("translation"),
                    }
    return {
        "sources": sources,
        "by_reuse_key": by_reuse_key,
        "by_doc_segment": by_doc_segment,
    }


def _seed_prior_translations(paths: Sequence[Path]) -> dict[tuple[str, str, int], str]:
    """Load prior segment translations (in-memory only) to replay multi-segment context."""
    seeded: dict[tuple[str, str, int], str] = {}
    for path in paths:
        data = json.loads(path.read_bytes())
        for doc in data.get("documents", []):
            doc_id = str(doc.get("doc_id", ""))
            direction = str(doc.get("direction", ""))
            for idx, seg in enumerate(doc.get("segments", []), start=1):
                translation = seg.get("translation")
                if isinstance(translation, str):
                    seeded[(doc_id, direction, idx)] = translation
    return seeded


async def build_arm_manifest_async(
    *,
    arm_name: str,
    db_path: Path,
    input_paths: Sequence[Path],
    model: str = DEFAULT_MODEL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    zh_en_thinking: str = DEFAULT_ZH_EN_THINKING,
    en_zh_thinking: str = DEFAULT_EN_ZH_THINKING,
    archived_paths: Sequence[Path] = (),
    prior_context_paths: Sequence[Path] = (),
    deterministic_placeholder_hex: str | None = None,
) -> dict[str, Any]:
    """Generate the per-segment offline request manifest for one Core/DB arm."""
    from wuwaterm import application
    from wuwaterm.cjk_span import MATCHER_REVISION
    from wuwaterm.segmentation import segment_text
    from wuwaterm.sentence import SentenceTranslator

    db_sha = sha256_bytes(db_path.read_bytes())
    archived = load_archived_index(archived_paths)
    prior_translations = _seed_prior_translations(prior_context_paths)

    captured: list[dict[str, Any]] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        messages = body["messages"]
        system_text = messages[0]["content"]
        user_text = messages[-1]["content"]
        captured.append(
            {
                "body": body,
                "raw_bytes": request.content,
                "system_text": system_text,
                "user_text": user_text,
            }
        )
        # Echo user content so restore() succeeds without altering lock placeholders.
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": user_text}}]},
        )

    env_overrides = {
        "WUWATERM_OPENAI_BASE_URL": "https://offline-manifest.invalid/v1",
        "WUWATERM_OPENAI_API_KEY": "offline-manifest-only",
        "WUWATERM_OPENAI_MODEL": model,
        "WUWATERM_TRANSLATION_THINKING_ZH_EN": zh_en_thinking,
        "WUWATERM_TRANSLATION_THINKING_EN_ZH": en_zh_thinking,
    }

    rows: list[dict[str, Any]] = []
    input_digests: list[dict[str, Any]] = []
    orig_token_hex = secrets.token_hex

    with offline_network_guard(), _scoped_env(env_overrides):
        if deterministic_placeholder_hex is not None:
            secrets.token_hex = lambda _n=8: deterministic_placeholder_hex  # type: ignore[assignment]
        logging.disable(logging.CRITICAL)
        translator = SentenceTranslator(
            db_path,
            llm_timeout_seconds=timeout_seconds,
            llm_max_concurrency=1,
            llm_transport=httpx.MockTransport(mock_handler),
            en_zh_protocol=True,
        )
        zh_en_policy = translator.request_policy(to_chinese=False)
        en_zh_policy = translator.request_policy(to_chinese=True)
        overall_policy = translator.request_policy(to_chinese=None)
        manifest_policy_sha = sha256_bytes(
            canonical_json(overall_policy.to_identity_dict())
        )
        manifest_profile_sha = sha256_bytes(
            canonical_json(DEFAULT_PROVIDER_PROFILE.to_identity_dict())
        )
        try:
            for input_path in input_paths:
                raw_input = input_path.read_bytes()
                bundle = json.loads(raw_input)
                docs = bundle.get("documents", [])
                input_digests.append(
                    {
                        "name": input_path.name,
                        "sha256": sha256_bytes(raw_input),
                        "documents": len(docs),
                    }
                )
                for doc in docs:
                    doc_id = str(doc["doc_id"])
                    dataset = str(doc.get("set", input_path.stem))
                    direction = str(doc["direction"])
                    if direction not in ("zh-en", "en-zh"):
                        raise ValueError(f"unsupported direction {direction!r} in {doc_id}")
                    text = str(doc["text"])
                    if "source_sha256" in doc and sha256_text(text) != doc["source_sha256"]:
                        raise ValueError(f"source_sha256 mismatch for {doc_id}")
                    to_chinese = direction == "en-zh"
                    doc_policy = en_zh_policy if to_chinese else zh_en_policy
                    cores = [
                        text[seg.core_start : seg.core_end]
                        for seg in segment_text(text)
                        if seg.core_end > seg.core_start
                    ]
                    before = None
                    prior_chain_reused = True
                    for seg_idx, core in enumerate(cores, start=1):
                        seg_context = application.TranslationContext(before=before)
                        _exact, plan = translator.plan_translation(
                            core,
                            to_chinese=to_chinese,
                            context=seg_context,
                            dictionary_identity=db_sha,
                        )
                        if plan is not None:
                            planned_spec = translator.build_request_spec(
                                plan, policy=doc_policy, model=model
                            )
                            refs_tuples = [
                                [ref.source, ref.zh, ref.en, ref.category]
                                for ref in plan.references
                            ]
                            scoped_skill_count = sum(
                                1 for ref in plan.references if ref.origin == "scoped_skill_stem"
                            )
                            lock_count = len(plan.locks)
                        else:
                            planned_spec = None
                            refs_tuples = []
                            scoped_skill_count = 0
                            lock_count = 0
                        captured.clear()
                        translated = await translator.translate_async(
                            core,
                            to_chinese=to_chinese,
                            propagate_errors=True,
                            context=seg_context,
                        )
                        model_called = len(captured) == 1
                        if not model_called:
                            rows.append(
                                {
                                    "arm": arm_name,
                                    "dataset": dataset,
                                    "doc_id": doc_id,
                                    "segment_index": seg_idx,
                                    "segment_count": len(cores),
                                    "direction": direction,
                                    "source_chars": len(core),
                                    "source_paragraphs": len(split_paragraphs(core)),
                                    "source_sha256": sha256_text(core),
                                    "model_call": False,
                                    "reuse_status": "exact_dictionary_hit",
                                    "reuse_reason": "answered_by_dictionary_no_post",
                                }
                            )
                            seed = prior_translations.get((doc_id, direction, seg_idx), translated)
                            before = application.ContextBefore(
                                source=core[-400:] if len(core) > 400 else core,
                                target=seed[-400:] if len(seed) > 400 else seed,
                            )
                            continue

                        if planned_spec is None or plan is None:
                            raise RuntimeError(
                                f"plan_translation returned None for model-called segment {doc_id}#{seg_idx}"
                            )

                        wire = captured[0]
                        body = wire["body"]
                        user_text = wire["user_text"]
                        thinking_field = body.get("thinking", "omitted")
                        thinking_setting = zh_en_thinking if not to_chinese else en_zh_thinking
                        has_random_locks = plan.has_random_locks or ("__WUWA_TERM_" in user_text)

                        norm_wire_body = dict(body)
                        norm_wire_body["messages"] = [
                            {
                                "role": msg["role"],
                                "content": normalize_placeholders(msg["content"]),
                            }
                            for msg in body["messages"]
                        ]
                        if canonical_json(planned_spec.normalized_wire_payload()) != canonical_json(
                            norm_wire_body
                        ):
                            raise RuntimeError(
                                f"TranslationRequestSpec drifted from transport wire body at {doc_id}#{seg_idx}"
                            )
                        if not has_random_locks and canonical_json(
                            planned_spec.wire_payload()
                        ) != canonical_json(body):
                            raise RuntimeError(
                                f"Exact TranslationRequestSpec drifted from wire body at {doc_id}#{seg_idx}"
                            )

                        wire_spec = dataclasses.replace(
                            planned_spec,
                            messages=tuple(
                                {"role": msg["role"], "content": msg["content"]}
                                for msg in body["messages"]
                            ),
                            has_random_locks=has_random_locks,
                        )
                        op_identity = wire_spec.operation_identity(
                            doc_policy, DEFAULT_PROVIDER_PROFILE
                        )
                        req_identity = op_identity.request

                        sys_sha = req_identity.system_prompt_sha256
                        usr_sha = req_identity.user_prompt_sha256
                        norm_sys_sha = req_identity.normalized_system_prompt_sha256
                        norm_usr_sha = req_identity.normalized_user_prompt_sha256

                        wire_body_sha = sha256_bytes(wire["raw_bytes"])
                        payload_sha = req_identity.payload_sha256
                        norm_payload_sha = req_identity.normalized_payload_sha256

                        manifest_key = compute_manifest_reuse_key(
                            model=body["model"],
                            direction=direction,
                            thinking=thinking_setting,
                            timeout_seconds=timeout_seconds,
                            system_prompt_sha256=sys_sha,
                            user_prompt_sha256=usr_sha,
                            db_sha256=db_sha,
                            matcher_revision=MATCHER_REVISION,
                        )
                        norm_manifest_key = compute_manifest_reuse_key(
                            model=body["model"],
                            direction=direction,
                            thinking=thinking_setting,
                            timeout_seconds=timeout_seconds,
                            system_prompt_sha256=norm_sys_sha,
                            user_prompt_sha256=norm_usr_sha,
                            db_sha256=db_sha,
                            matcher_revision=MATCHER_REVISION,
                        )
                        adr_key = compute_adr0020_reuse_key(
                            core_sha256=sha256_text(core),
                            direction=direction,
                            payload_sha256=payload_sha,
                            db_sha256=db_sha,
                            matcher_revision=MATCHER_REVISION,
                            references=refs_tuples,
                            model=body["model"],
                            temperature=body.get("temperature", 0),
                            thinking_payload=thinking_field,
                        )

                        archived_exact = archived["by_reuse_key"].get(adr_key)
                        archived_slot = archived["by_doc_segment"].get((doc_id, direction, seg_idx))
                        if not prior_chain_reused:
                            reuse_status = "must_post"
                            reuse_reason = "prior_segment_not_reused"
                        elif has_random_locks:
                            reuse_status = "must_post"
                            reuse_reason = "random_lock_placeholder_nonce"
                        elif archived_exact is not None:
                            reuse_status = "reusable_exact"
                            reuse_reason = "exact_adr0020_reuse_key_match"
                        elif archived_slot is None:
                            reuse_status = "must_post"
                            reuse_reason = "no_archived_output"
                        elif archived_slot["status"] != "complete":
                            reuse_status = "must_post"
                            reuse_reason = f"archived_status_{archived_slot['status']}"
                        elif not archived_slot["has_raw_content"]:
                            reuse_status = "must_post"
                            reuse_reason = "archived_missing_raw_content_and_reuse_key"
                        else:
                            reuse_status = "must_post"
                            reuse_reason = "reuse_key_mismatch"

                        if reuse_status != "reusable_exact":
                            prior_chain_reused = False

                        rows.append(
                            {
                                "arm": arm_name,
                                "dataset": dataset,
                                "doc_id": doc_id,
                                "segment_index": seg_idx,
                                "segment_count": len(cores),
                                "direction": direction,
                                "source_chars": len(core),
                                "source_paragraphs": len(split_paragraphs(core)),
                                "source_sha256": sha256_text(core),
                                "model_call": True,
                                "model": body["model"],
                                "temperature": body.get("temperature", 0),
                                "thinking": thinking_setting,
                                "thinking_wire": thinking_field,
                                "timeout_seconds": float(timeout_seconds),
                                "lock_count": lock_count,
                                "has_random_locks": has_random_locks,
                                "reference_count": len(plan.references),
                                "scoped_skill_stem_count": scoped_skill_count,
                                "system_prompt_sha256": sys_sha,
                                "user_prompt_sha256": usr_sha,
                                "wire_body_sha256": wire_body_sha,
                                "request_payload_sha256": payload_sha,
                                "normalized_system_prompt_sha256": norm_sys_sha,
                                "normalized_user_prompt_sha256": norm_usr_sha,
                                "normalized_request_payload_sha256": norm_payload_sha,
                                "request_identity_sha256": req_identity.request_sha256,
                                "normalized_request_identity_sha256": req_identity.normalized_request_sha256,
                                "operation_identity_sha256": op_identity.operation_sha256,
                                "policy_sha256": op_identity.policy_sha256,
                                "provider_profile_sha256": op_identity.provider_profile_sha256,
                                "reuse_key": manifest_key,
                                "normalized_reuse_key": norm_manifest_key,
                                "adr0020_reuse_key": adr_key,
                                "reuse_status": reuse_status,
                                "reuse_reason": reuse_reason,
                            }
                        )
                        seed = (
                            (archived_exact or {}).get("translation")
                            or prior_translations.get((doc_id, direction, seg_idx))
                            or translated
                        )
                        before = application.ContextBefore(
                            source=core[-400:] if len(core) > 400 else core,
                            target=seed[-400:] if len(seed) > 400 else seed,
                        )
        finally:
            secrets.token_hex = orig_token_hex
            logging.disable(logging.NOTSET)
            await translator.aclose()

    model_rows = [row for row in rows if row["model_call"]]
    reusable_count = sum(1 for row in model_rows if row["reuse_status"] == "reusable_exact")
    must_post_count = sum(1 for row in model_rows if row["reuse_status"] == "must_post")

    return {
        "schema": "q7-eval-manifest-v1",
        "arm": arm_name,
        "offline_only": True,
        "live_llm_calls": 0,
        "db_sha256": db_sha,
        "matcher_revision": MATCHER_REVISION,
        "policy_sha256": manifest_policy_sha,
        "provider_profile_sha256": manifest_profile_sha,
        "settings": {
            "model": model,
            "timeout_seconds": float(timeout_seconds),
            "zh_en_thinking": zh_en_thinking,
            "en_zh_thinking": en_zh_thinking,
            "en_zh_protocol": True,
        },
        "inputs": input_digests,
        "archived_sources": archived["sources"],
        "summary": {
            "total_segments": len(rows),
            "model_call_segments": len(model_rows),
            "dictionary_only_segments": len(rows) - len(model_rows),
            "reusable_exact_segments": reusable_count,
            "must_post_segments": must_post_count,
            "random_lock_segments": sum(1 for row in model_rows if row["has_random_locks"]),
            "scoped_skill_stem_segments": sum(
                1 for row in model_rows if row["scoped_skill_stem_count"] > 0
            ),
        },
        "requests": rows,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an offline, zero-LLM per-request reuse_key manifest."
    )
    parser.add_argument("--arm", default="q7-candidate", help="Label for this arm")
    parser.add_argument("--db", required=True, type=Path, help="Path to SQLite terms DB")
    parser.add_argument(
        "--inputs",
        required=True,
        action="append",
        type=Path,
        help="Path to usability-run-inputs-v1 JSON bundle (repeatable)",
    )
    parser.add_argument(
        "--archived-outputs",
        action="append",
        default=[],
        type=Path,
        help="Optional archived raw_outputs.json to check exact reuse_key matches",
    )
    parser.add_argument(
        "--prior-context-outputs",
        action="append",
        default=[],
        type=Path,
        help="Optional prior raw_outputs.json supplying previous-segment translations for multi-segment docs",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--zh-en-thinking", default=DEFAULT_ZH_EN_THINKING)
    parser.add_argument("--en-zh-thinking", default=DEFAULT_EN_ZH_THINKING)
    parser.add_argument(
        "--out",
        type=Path,
        help="Write JSON manifest to this path (default: print to stdout)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = asyncio.run(
        build_arm_manifest_async(
            arm_name=args.arm,
            db_path=args.db,
            input_paths=args.inputs,
            model=args.model,
            timeout_seconds=args.timeout,
            zh_en_thinking=args.zh_en_thinking,
            en_zh_thinking=args.en_zh_thinking,
            archived_paths=args.archived_outputs,
            prior_context_paths=args.prior_context_outputs,
        )
    )
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
