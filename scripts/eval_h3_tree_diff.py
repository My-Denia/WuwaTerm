#!/usr/bin/env python3
"""Offline differential harness comparing Core v0.13.0 vs v0.14.0 via ``eval_h3.py``.

Drives ``scripts/eval_h3.py run --fake-transport`` over identical manifests and
the pinned dictionary under three deterministic scenarios:

1. ``default``          normal ``wuwaterm_prod`` execution
2. ``timeout_fallback`` first POST of attempt 1 times out on every segment
3. ``paragraph_repair`` first POST collapses paragraph breaks

Compares per-request raw ``payload_sha256``, ``normalized_payload_sha256``,
``system_prompt_sha256``, ``user_prompt_sha256``, ``thinking_mode``,
``attempt_kind``, document ``output_sha256``, and POST counts. Whenever any
difference (including placeholder-nonce variance) is seen, isolated
stage-probe subprocesses run with a deterministic placeholder prefix to
compare ``PreparedTerms`` (locks / references / locked_text),
``TranslationPlan``, and ``TranslationRequestSpec`` stage by stage.

The JSON report NEVER contains corpus, prompt, or translation text — only
SHA-256 / sha16 digests, counts, and difference classifications.

Exit codes:
  0  no behavioral or fixed-nonce differential (and API check passed if enabled)
  1  differential detected or API verification failed
  2  usage / input / safety refusal
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVAL_H3_SCRIPT = ROOT / "scripts" / "eval_h3.py"

EXPECTED_V014_DICTIONARY_REVISION = (
    "d9f5e4ae93755b1479943dfff8adf176c461a23bd0d8a337d39d28ea7051f2a5"
)
EXPECTED_V014_TERM_COUNT = 12097
EXPECTED_V014_MATCHER_REVISION = (
    "286d29e8f25b1ee53d0634c89bf6fb8a83ef950888c21f15d485413cb25d0664"
)
PINNED_V013_SRC_TREE = "9e23fd5975caeab024dcb00267e20a6266f4678e"
PINNED_V014_SRC_TREE = "8e6bd426bfc12625f050c03ed14e419d3dba145f"

FORBIDDEN_H3_DIR_RE = re.compile(r"^h3-\d{8}(?:-[A-Za-z0-9_-]+)?$")
FORBIDDEN_BLIND_SUBDIRS = frozenset({"pool", "raw", "corpus", "annotations", "work"})

SCENARIOS: dict[str, list[dict[str, Any]]] = {
    "default": [],
    "timeout_fallback": [
        {"arm": "wuwaterm_prod", "doc_attempt": 1, "post": 1, "action": "timeout"}
    ],
    "paragraph_repair": [
        {"arm": "wuwaterm_prod", "post": 1, "action": "merge_paragraphs"}
    ],
}

DEFAULT_BATCH_MAX_DOCS = 20
DEFAULT_BATCH_MAX_CHARS = 30_000
STAGE_FULL_MAX_CALLS = 240
PROD_THINKING_ENV = {
    "WUWATERM_TRANSLATION_THINKING_ZH_EN": "disabled",
    "WUWATERM_TRANSLATION_THINKING_EN_ZH": "auto",
}
FAKE_BASE_URL = "http://h3-dry-run.invalid/v1"
FAKE_MODEL = "h3-dry-run-model"


class DiffToolError(Exception):
    """Raised on invalid CLI configuration or refused input."""


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha16_text(text: str) -> str:
    return sha256_text(text)[:16]


def ensure_not_blind_path(path: Path) -> None:
    parts = path.resolve(strict=False).parts
    if any(FORBIDDEN_H3_DIR_RE.match(part) for part in parts):
        raise DiffToolError("refused: path lies inside a forbidden blind evaluation directory")
    for idx, part in enumerate(parts[:-1]):
        if part.startswith("real-manuscript-eval-") and parts[idx + 1] in FORBIDDEN_BLIND_SUBDIRS:
            raise DiffToolError("refused: path lies inside a forbidden blind evaluation directory")


def git_rev_parse(repo: Path, rev: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", rev],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise DiffToolError(f"git rev-parse {rev} failed in {repo.name}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def normalize_direction(raw: str) -> str:
    mapping = {
        "zh->en": "zh->en",
        "zh-en": "zh->en",
        "en->zh": "en->zh",
        "en-zh": "en->zh",
    }
    if raw not in mapping:
        raise DiffToolError(f"unsupported direction: {raw!r}")
    return mapping[raw]


# ------------------------------------------------ synthetic ambiguous stress set


def build_synthetic_single_char_stress_docs() -> list[dict[str, str]]:
    """Construct synthetic cases covering all ``_lock_mixed_single_char_names`` boundaries."""
    long_mixed_para_a = "心和今汐沿着旧港巡查声骸异常，大家心里都记着当年那件事。" * 6
    long_mixed_para_b = "椿站在高处看着那株红椿蕊，随后又把椿的回音频段收进背包。" * 6
    long_mixed_para_c = "应世心与心月狐的记录摆在桌上，心沉默了片刻才开口。" * 6
    return [
        {
            "doc_id": "synth-01-unmixed-single-char",
            "direction": "zh->en",
            "genre": "story",
            "text": "椿和今汐是共鸣者。",
        },
        {
            "doc_id": "synth-02-mixed-ordinary-prose",
            "direction": "zh->en",
            "genre": "story",
            "text": "心和今汐一起巡逻，她始终把大家的安危放在心上。",
        },
        {
            "doc_id": "synth-03-mixed-novel-compound",
            "direction": "zh->en",
            "genre": "story",
            "text": "椿看着窗台上的红椿蕊陷入沉思。",
        },
        {
            "doc_id": "synth-04-mixed-suffix-title",
            "direction": "zh->en",
            "genre": "story",
            "text": "椿微笑着点头，大家都称呼椿姐。",
        },
        {
            "doc_id": "synth-05-mixed-quoted-raw",
            "direction": "zh->en",
            "genre": "story",
            "text": "心停下脚步，抬头看着石碑上的「心」字。",
        },
        {
            "doc_id": "synth-06-longer-global-no-share-unmixed",
            "direction": "zh->en",
            "genre": "guide",
            "text": "心和今汐讨论了应世心的含义。",
        },
        {
            "doc_id": "synth-07-longer-global-no-share-mixed",
            "direction": "zh->en",
            "genre": "guide",
            "text": "心和今汐讨论了应世心，心里感到非常踏实。",
        },
        {
            "doc_id": "synth-08-longer-global-shared-target-camellya",
            "direction": "zh->en",
            "genre": "guide",
            "text": "椿收好了椿的回音频段，又摘下一朵红椿蕊。",
        },
        {
            "doc_id": "synth-09-longer-global-shared-target-hsin",
            "direction": "zh->en",
            "genre": "story",
            "text": "心面对着心月狐的幻影，早已下定了心意。",
        },
        {
            "doc_id": "synth-10-only-inside-longer-global",
            "direction": "zh->en",
            "genre": "guide",
            "text": "今汐研读了应世心与心念的记录。",
        },
        {
            "doc_id": "synth-11-multi-single-char-delta-shift",
            "direction": "zh->en",
            "genre": "story",
            "text": "心和今汐走向长街，随后岚与椿也赶了过来，大家心里都记挂着那株红椿蕊。",
        },
        {
            "doc_id": "synth-12-ambiguous-admitted-evidence-all-kinds",
            "direction": "zh->en",
            "genre": "guide",
            "text": (
                "# 椿姐与今汐配队记录\n\n"
                "椿姐：这期测试用 0+1 椿姐和今汐、散华、守岸人。\n\n"
                "但大家心里都清楚，红椿蕊并不是重点，声骸配置才是。"
            ),
        },
        {
            "doc_id": "synth-13-ambiguous-no-entity-evidence",
            "direction": "zh->en",
            "genre": "mixed",
            "text": "心很重要。\n\n红椿蕊开得很好。",
        },
        {
            "doc_id": "synth-14-multi-paragraph-repair-target",
            "direction": "zh->en",
            "genre": "story",
            "text": (
                "心和今汐站在海边。\n\n"
                "大家心里都很平静，椿也点了点头。\n\n"
                "远处传来声骸的鸣响。"
            ),
        },
        {
            "doc_id": "synth-15-long-multi-segment-mixed",
            "direction": "zh->en",
            "genre": "story",
            "text": f"{long_mixed_para_a}\n\n{long_mixed_para_b}\n\n{long_mixed_para_c}",
        },
        {
            "doc_id": "synth-16-en-zh-single-char-names-low",
            "direction": "en->zh",
            "genre": "guide",
            "text": (
                "Camellya and Hsin waited near the Shorekeeper.\n\n"
                "Meanwhile, Jinhsi checked the Echo data and calm waves."
            ),
        },
        {
            "doc_id": "synth-17-en-zh-high-locks-disabled",
            "direction": "en->zh",
            "genre": "guide",
            "text": (
                "Camellya equipped Astrite, Radiant Tide, Forte Circuit, Tacet Mark, "
                "Tacet Discord, and Waveworn Phenomenon."
            ),
        },
        {
            "doc_id": "synth-18-en-zh-exact-hit",
            "direction": "en->zh",
            "genre": "announcement",
            "text": "Shorekeeper",
        },
    ]


def load_allowed_corpora(
    *,
    corpus_dir: Path | None = None,
    vps_inputs: Path | None = None,
    r_inputs: Path | None = None,
    m_gold: Path | None = None,
    include_synthetic: bool = True,
) -> list[dict[str, str]]:
    """Load the allowed offline evaluation corpora plus optional synthetic stress docs."""
    docs: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    def add_doc(doc_id: str, direction: str, genre: str, text: str) -> None:
        if not text.strip():
            return
        if doc_id in seen_ids:
            return
        seen_ids.add(doc_id)
        docs.append(
            {
                "doc_id": doc_id,
                "direction": normalize_direction(direction),
                "genre": genre,
                "text": text,
            }
        )

    if corpus_dir is not None:
        ensure_not_blind_path(corpus_dir)
        if not corpus_dir.is_dir():
            raise DiffToolError(f"corpus directory not found: {corpus_dir}")
        for entry in sorted(corpus_dir.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith("A6"):
                for txt_path in sorted(entry.glob("*.txt")):
                    ensure_not_blind_path(txt_path)
                    direction = "zh->en" if txt_path.name.startswith("zh_") else "en->zh"
                    add_doc(
                        f"A6/{txt_path.stem}",
                        direction,
                        "announcement",
                        txt_path.read_text(encoding="utf-8"),
                    )
            elif entry.name.startswith("A"):
                prefix = entry.name[:2]
                genre = "announcement" if prefix in {"A3", "A4", "A5"} else "guide"
                for lang, direction in (("zh", "zh->en"), ("en", "en->zh")):
                    txt_path = entry / f"{lang}.txt"
                    if txt_path.is_file():
                        ensure_not_blind_path(txt_path)
                        add_doc(
                            f"{prefix}/{lang}",
                            direction,
                            genre,
                            txt_path.read_text(encoding="utf-8"),
                        )
            elif entry.name.startswith("B"):
                prefix = entry.name[:2]
                txt_path = entry / "zh.txt"
                if txt_path.is_file():
                    ensure_not_blind_path(txt_path)
                    genre = "guide" if prefix == "B1" else "story"
                    add_doc(f"{prefix}/zh", "zh->en", genre, txt_path.read_text(encoding="utf-8"))

    if vps_inputs is not None:
        ensure_not_blind_path(vps_inputs)
        if not vps_inputs.is_file():
            raise DiffToolError(f"vps_inputs not found: {vps_inputs}")
        vps_data = json.loads(vps_inputs.read_text(encoding="utf-8"))
        for item in vps_data.get("set2_sentences", []):
            add_doc(
                f"vps/{item['sent_id']}",
                "zh->en",
                "mixed",
                str(item["zh_text"]),
            )
        for item in vps_data.get("set1_docs", []):
            text = str(item["zh_text"])
            # Include only if distinct from A1-B3 already added (e.g. sanitized variants)
            existing_hashes = {sha256_text(d["text"]) for d in docs}
            if sha256_text(text) not in existing_hashes:
                add_doc(f"vps-set1/{item['doc_id']}", "zh->en", "mixed", text)

    if r_inputs is not None:
        ensure_not_blind_path(r_inputs)
        if not r_inputs.is_file():
            raise DiffToolError(f"r_inputs not found: {r_inputs}")
        r_data = json.loads(r_inputs.read_text(encoding="utf-8"))
        for item in r_data.get("documents", []):
            add_doc(
                f"R/{item['doc_id']}",
                str(item["direction"]),
                "mixed",
                str(item["text"]),
            )

    if m_gold is not None:
        ensure_not_blind_path(m_gold)
        if not m_gold.is_file():
            raise DiffToolError(f"m_gold not found: {m_gold}")
        m_data = json.loads(m_gold.read_text(encoding="utf-8"))
        raw_docs = m_data.get("docs") or {}
        if isinstance(raw_docs, Mapping):
            existing_hashes = {sha256_text(d["text"]) for d in docs}
            for key, doc_path_str in sorted(raw_docs.items()):
                doc_path = Path(str(doc_path_str))
                ensure_not_blind_path(doc_path)
                if doc_path.is_file():
                    text = doc_path.read_text(encoding="utf-8")
                    if sha256_text(text) not in existing_hashes:
                        add_doc(f"M-Gold/{key}", "zh->en", "mixed", text)
                        existing_hashes.add(sha256_text(text))

    if include_synthetic:
        for item in build_synthetic_single_char_stress_docs():
            add_doc(item["doc_id"], item["direction"], item["genre"], item["text"])

    return docs


def load_manifest_documents(manifest_path: Path) -> list[dict[str, str]]:
    ensure_not_blind_path(manifest_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != "h3-manifest-v1":
        raise DiffToolError(f"{manifest_path.name}: schema must be h3-manifest-v1")
    docs = data.get("documents")
    if not isinstance(docs, list) or not docs:
        raise DiffToolError(f"{manifest_path.name}: documents must be a non-empty list")
    return [
        {
            "doc_id": str(d["doc_id"]),
            "direction": normalize_direction(str(d["direction"])),
            "genre": str(d["genre"]),
            "text": str(d["text"]),
        }
        for d in docs
    ]


def batch_documents(
    docs: Sequence[Mapping[str, str]],
    *,
    max_docs: int = DEFAULT_BATCH_MAX_DOCS,
    max_chars: int = DEFAULT_BATCH_MAX_CHARS,
) -> list[list[dict[str, str]]]:
    """Split documents into batches so each stays well within ``STAGE_CAPS['full']`` (240)."""
    batches: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    current_chars = 0
    for doc in docs:
        item = dict(doc)
        text_len = len(item["text"])
        if current and (len(current) >= max_docs or current_chars + text_len > max_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += text_len
    if current:
        batches.append(current)
    return batches


# ----------------------------------------------------------- eval_h3.py driver


@dataclass(frozen=True)
class TreeTarget:
    label: str
    bound_tree: Path
    expected_src_tree: str


def run_eval_h3_scenario(
    *,
    target: TreeTarget,
    terms_db: Path,
    expected_terms_sha256: str,
    docs: Sequence[Mapping[str, str]],
    scenario_name: str,
    scenario_rules: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Drive ``scripts/eval_h3.py run --fake-transport`` in batches and collect outputs."""
    batches = batch_documents(docs)
    merged_docs: dict[str, dict[str, Any]] = {}
    total_posts = 0
    total_transport_posts = 0
    batch_count = len(batches)

    with tempfile.TemporaryDirectory(prefix=f"h3-diff-{target.label}-{scenario_name}-") as tmp:
        tmp_path = Path(tmp)
        scenario_path: Path | None = None
        if scenario_rules:
            scenario_path = tmp_path / "scenario.json"
            scenario_path.write_text(
                json.dumps({"schema": "h3-fake-scenario-v1", "rules": list(scenario_rules)}),
                encoding="utf-8",
            )

        for batch_index, batch in enumerate(batches):
            manifest_path = tmp_path / f"manifest-{batch_index}.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "h3-manifest-v1",
                        "set": f"diff-{scenario_name}-b{batch_index}",
                        "documents": batch,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            run_dir = tmp_path / f"run-{batch_index}"
            cmd = [
                sys.executable,
                str(EVAL_H3_SCRIPT),
                "run",
                "--bound-tree",
                str(target.bound_tree),
                "--terms-db",
                str(terms_db),
                "--expected-src-tree",
                target.expected_src_tree,
                "--expected-terms-sha256",
                expected_terms_sha256,
                "--run-dir",
                str(run_dir),
                "--manifest",
                str(manifest_path),
                "--stage",
                "full",
                "--arm",
                "wuwaterm_prod",
                "--max-calls",
                str(STAGE_FULL_MAX_CALLS),
                "--fake-transport",
            ]
            if scenario_path is not None:
                cmd.extend(["--fake-scenario", str(scenario_path)])
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                raise DiffToolError(
                    f"eval_h3.py failed for {target.label} scenario={scenario_name} "
                    f"batch={batch_index} rc={proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
                )
            summary = json.loads(proc.stdout.strip().splitlines()[-1])
            total_posts += int(summary["posts"])
            total_transport_posts += int(summary["transport_posts"])
            outputs_file = run_dir / summary["outputs"]["path"]
            outputs_data = json.loads(outputs_file.read_text(encoding="utf-8"))
            for doc_entry in outputs_data["documents"]:
                merged_docs[doc_entry["doc_id"]] = doc_entry

    return {
        "tree": target.label,
        "src_tree": target.expected_src_tree,
        "scenario": scenario_name,
        "batches": batch_count,
        "posts_total": total_posts,
        "transport_posts_total": total_transport_posts,
        "documents": merged_docs,
    }


# --------------------------------------------------------- stage-level probing


def _serialize_refs(refs: Sequence[Any]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for ref in refs:
        if hasattr(ref, "source"):
            occs = [
                {
                    "start": occ.start,
                    "end": occ.end,
                    "matched_text": occ.matched_text,
                    "segment_index": occ.segment_index,
                    "model_start": occ.model_start,
                    "model_end": occ.model_end,
                }
                for occ in getattr(ref, "occurrences", ())
            ]
            serialized.append(
                {
                    "source": ref.source,
                    "zh": ref.zh,
                    "en": ref.en,
                    "category": ref.category,
                    "origin": getattr(ref, "origin", ""),
                    "reason": getattr(ref, "reason", ""),
                    "occurrences": occs,
                }
            )
        else:
            serialized.append({"tuple": list(ref)})
    return serialized


def _probe_stages_in_process(
    *,
    bound_tree: Path,
    terms_db: Path,
    docs: Sequence[Mapping[str, str]],
    scenario_name: str,
    scenario_rules: Sequence[Mapping[str, Any]],
    fixed_placeholder_prefix: str | None,
) -> dict[str, Any]:
    """Run inside a dedicated subprocess to capture stage digests without raw text."""
    import contextvars
    import httpx

    src_dir = (bound_tree / "src").resolve()
    if "wuwaterm" in sys.modules:
        raise DiffToolError("wuwaterm already imported before stage probe")
    sys.path.insert(0, str(src_dir))

    from wuwaterm import application, cjk_span, request_trace, segmentation, sentence
    from wuwaterm import term_preparation, translation_plan

    current_state: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
        "probe_state", default=None
    )

    if fixed_placeholder_prefix is not None:
        fallback_counter = {"n": 0}

        def deterministic_prefix(source_text: str) -> str:
            state = current_state.get()
            if state is not None:
                seg_rec = state["segments"][state["seg_idx"]]
                seg_rec["prefix_calls"] += 1
                call_idx = seg_rec["prefix_calls"]
                doc_id = state["doc_id"]
                attempt = state["attempt"]
                seg_idx = state["seg_idx"]
                salt = 0
                while True:
                    tag = f"{doc_id}:{attempt}:{seg_idx}:{call_idx}:{salt}"
                    digest = sha256_text(tag)[:12]
                    candidate = f"{fixed_placeholder_prefix}{digest}_"
                    if candidate not in source_text:
                        return candidate
                    salt += 1
            while True:
                fallback_counter["n"] += 1
                candidate = f"{fixed_placeholder_prefix}global_{fallback_counter['n']:04d}_"
                if candidate not in source_text:
                    return candidate

        setattr(term_preparation, "new_placeholder_prefix", deterministic_prefix)
        setattr(sentence, "new_placeholder_prefix", deterministic_prefix)

    doc_stages: dict[str, dict[str, Any]] = {}

    orig_prepare_term_segment = term_preparation.prepare_term_segment
    orig_lock = sentence.SentenceTranslator._lock_terms_with_references
    orig_build_spec = translation_plan.build_translation_request_spec

    def _record_prepared(
        seg_rec: dict[str, Any],
        prepared_text: str,
        locked_text: str,
        locks: Sequence[Any],
        references: Sequence[Any],
        ambiguous: Sequence[Any],
    ) -> None:
        seg_rec["prepared_text_sha256"] = sha256_text(prepared_text)
        seg_rec["locked_text_sha256"] = sha256_text(locked_text)
        seg_rec["locks_count"] = len(locks)
        seg_rec["locks_sha256"] = sha256_bytes(
            canonical_json([list(lock) for lock in locks])
        )
        serialized_refs = _serialize_refs(references)
        seg_rec["references_count"] = len(serialized_refs)
        seg_rec["references_sha256"] = sha256_bytes(canonical_json(serialized_refs))
        seg_rec["ambiguous_count"] = len(ambiguous)

    def wrapped_prepare_term_segment(text: str, *args: Any, **kwargs: Any) -> Any:
        prepared = orig_prepare_term_segment(text, *args, **kwargs)
        state = current_state.get()
        if state is not None:
            seg_rec = state["segments"][state["seg_idx"]]
            _record_prepared(
                seg_rec,
                text,
                prepared.locked_text,
                prepared.locks,
                prepared.references,
                getattr(prepared, "ambiguous", ()),
            )
            seg_rec["saw_prepare_term_segment"] = True
        return prepared

    def wrapped_lock(
        self: Any,
        text: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        state = current_state.get()
        if state is not None:
            state["segments"][state["seg_idx"]]["saw_prepare_term_segment"] = False
        locked = orig_lock(self, text, *args, **kwargs)
        if state is not None:
            seg_rec = state["segments"][state["seg_idx"]]
            ambiguous_count = (
                seg_rec["ambiguous_count"] if seg_rec.get("saw_prepare_term_segment") else 0
            )
            _record_prepared(
                seg_rec,
                text,
                locked.locked_text,
                locked.locks,
                locked.references,
                () if ambiguous_count == 0 else [None] * ambiguous_count,
            )
        return locked

    def wrapped_build_spec(plan: Any, **kwargs: Any) -> Any:
        spec = orig_build_spec(plan, **kwargs)
        state = current_state.get()
        if state is not None:
            seg_rec = state["segments"][state["seg_idx"]]
            ctx_obj = plan.context
            ctx_digest = None
            if ctx_obj is not None:
                before = getattr(ctx_obj, "before", None)
                precedents = getattr(ctx_obj, "precedents", ())
                ctx_digest = sha256_bytes(
                    canonical_json(
                        {
                            "before": (
                                {
                                    "source": getattr(before, "source", None),
                                    "target": getattr(before, "target", None),
                                }
                                if before is not None
                                else None
                            ),
                            "precedents": [
                                {
                                    "source": getattr(p, "source", None),
                                    "choice": getattr(p, "choice", None),
                                    "rendering": getattr(p, "rendering", None),
                                    "snippet": getattr(p, "snippet", None),
                                }
                                for p in precedents
                            ],
                        }
                    )
                )
            plan_core = {
                "source_text_sha256": sha256_text(plan.source_text),
                "prepared_text_sha256": sha256_text(plan.prepared_text),
                "locked_text_sha256": sha256_text(plan.locked_text),
                "locks_sha256": sha256_bytes(canonical_json([list(x) for x in plan.locks])),
                "references_sha256": sha256_bytes(canonical_json(_serialize_refs(plan.references))),
                "to_chinese": plan.to_chinese,
                "html_mode": plan.html_mode,
                "context_sha256": ctx_digest,
                "dictionary_identity": plan.dictionary_identity,
            }
            wire = spec.wire_payload()
            seg_rec["requests"].append(
                {
                    "attempt_kind": spec.attempt_kind,
                    "thinking_mode": spec.thinking_mode,
                    "plan_core_sha256": sha256_bytes(canonical_json(plan_core)),
                    "matcher_revision": plan.matcher_revision,
                    "system_prompt_sha256": sha256_text(spec.system_prompt),
                    "user_prompt_sha256": sha256_text(spec.user_prompt),
                    "payload_sha256": sha256_bytes(canonical_json(wire)),
                    "normalized_payload_sha256": sha256_bytes(
                        canonical_json(spec.normalized_wire_payload())
                    ),
                }
            )
        return spec

    setattr(term_preparation, "prepare_term_segment", wrapped_prepare_term_segment)
    setattr(sentence, "prepare_term_segment", wrapped_prepare_term_segment)
    setattr(sentence.SentenceTranslator, "_lock_terms_with_references", wrapped_lock)
    setattr(translation_plan, "build_translation_request_spec", wrapped_build_spec)
    setattr(sentence, "build_translation_request_spec", wrapped_build_spec)

    def rule_action(doc_id: str, seg_idx: int, doc_attempt: int, post_num: int) -> str | None:
        for rule in scenario_rules:
            arm = rule.get("arm")
            if arm is not None and arm != "wuwaterm_prod":
                continue
            if rule.get("doc_id") is not None and rule.get("doc_id") != doc_id:
                continue
            if rule.get("segment") is not None and rule.get("segment") != seg_idx:
                continue
            if rule.get("doc_attempt") is not None and rule.get("doc_attempt") != doc_attempt:
                continue
            if rule.get("post") is not None and rule.get("post") != post_num:
                continue
            return str(rule["action"])
        return None

    def gateway_handler(request: httpx.Request) -> httpx.Response:
        state = current_state.get()
        assert state is not None
        seg_rec = state["segments"][state["seg_idx"]]
        seg_rec["transport_posts"] += 1
        post_num = seg_rec["transport_posts"]
        action = rule_action(state["doc_id"], state["seg_idx"], state["attempt"], post_num)
        if action == "timeout":
            raise httpx.ReadTimeout("dry-run timeout", request=request)
        if action == "http_500":
            return httpx.Response(500, json={"error": {"message": "dry-run error"}})
        body = json.loads(request.content)
        messages = body["messages"]
        content = str(messages[-1]["content"])
        if action == "empty":
            content = ""
        elif action == "merge_paragraphs":
            content = " ".join(content.split())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    @contextlib.contextmanager
    def probe_env() -> Iterator[None]:
        keys = (
            "WUWATERM_OPENAI_BASE_URL",
            "WUWATERM_OPENAI_API_KEY",
            "WUWATERM_OPENAI_MODEL",
            "OPENAI_BASE_URL",
            "OPENAI_API_KEY",
            *PROD_THINKING_ENV,
        )
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ.pop("OPENAI_BASE_URL", None)
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ["WUWATERM_OPENAI_BASE_URL"] = FAKE_BASE_URL
            os.environ["WUWATERM_OPENAI_API_KEY"] = "dry-run"
            os.environ["WUWATERM_OPENAI_MODEL"] = FAKE_MODEL
            os.environ.update(PROD_THINKING_ENV)
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    async def run_all() -> None:
        transport = httpx.MockTransport(gateway_handler)
        translator = sentence.SentenceTranslator(
            terms_db,
            llm_timeout_seconds=45.0,
            llm_max_concurrency=1,
            llm_transport=transport,
            en_zh_protocol=True,
        )
        try:
            for doc in docs:
                doc_id = doc["doc_id"]
                text = doc["text"]
                to_chinese = doc["direction"] == "en->zh"
                seg_objs = segmentation.segment_text(text)
                non_empty_indices = [
                    i for i, s in enumerate(seg_objs) if s.core_start < s.core_end
                ]
                attempts_out: list[dict[str, Any]] = []
                final_output: str | None = None
                for attempt_num in (1, 2):
                    seg_records: dict[int, dict[str, Any]] = {
                        i: {
                            "index": i,
                            "core_sha256": sha256_text(text[seg_objs[i].core_start : seg_objs[i].core_end]),
                            "prepared_text_sha256": None,
                            "locked_text_sha256": None,
                            "locks_count": 0,
                            "locks_sha256": None,
                            "references_count": 0,
                            "references_sha256": None,
                            "ambiguous_count": 0,
                            "prefix_calls": 0,
                            "transport_posts": 0,
                            "requests": [],
                        }
                        for i in non_empty_indices
                    }
                    pos = {"idx": 0}
                    state: dict[str, Any] = {
                        "doc_id": doc_id,
                        "attempt": attempt_num,
                        "seg_idx": non_empty_indices[0] if non_empty_indices else 0,
                        "segments": seg_records,
                    }

                    def on_segment(_seg: Any, _translated: str) -> None:
                        pos["idx"] += 1
                        if pos["idx"] < len(non_empty_indices):
                            state["seg_idx"] = non_empty_indices[pos["idx"]]

                    token = current_state.set(state)
                    trace = request_trace.start()
                    request_trace.set_deadline(90.0)
                    try:
                        outcome = await application._translate_long_plain_text(
                            translator,
                            text,
                            to_chinese=to_chinese,
                            on_segment=on_segment,
                        )
                    finally:
                        request_trace.reset()
                        current_state.reset(token)
                        _ = trace

                    status = "ok" if outcome.kind != "error" else "failed"
                    attempts_out.append(
                        {
                            "attempt": attempt_num,
                            "status": status,
                            "segments": [seg_records[i] for i in non_empty_indices],
                        }
                    )
                    if status == "ok":
                        final_output = str(outcome.text)
                        break

                doc_stages[doc_id] = {
                    "doc_sha16": sha16_text(text),
                    "output_sha256": sha256_text(final_output) if final_output is not None else None,
                    "attempts": attempts_out,
                }
        finally:
            await translator.aclose()

    try:
        with probe_env():
            asyncio.run(run_all())
    finally:
        setattr(term_preparation, "prepare_term_segment", orig_prepare_term_segment)
        setattr(sentence, "prepare_term_segment", orig_prepare_term_segment)
        setattr(sentence.SentenceTranslator, "_lock_terms_with_references", orig_lock)
        setattr(translation_plan, "build_translation_request_spec", orig_build_spec)
        setattr(sentence, "build_translation_request_spec", orig_build_spec)

    return {
        "schema": "h3-stage-probe-v1",
        "scenario": scenario_name,
        "matcher_revision": str(cjk_span.MATCHER_REVISION),
        "documents": doc_stages,
    }


def run_stage_probe_subprocess(
    *,
    bound_tree: Path,
    terms_db: Path,
    docs: Sequence[Mapping[str, str]],
    scenario_name: str,
    scenario_rules: Sequence[Mapping[str, Any]],
    fixed_placeholder_prefix: str = "__WUWA_TERM_DIFFTEST_",
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="h3-stage-probe-") as tmp:
        tmp_path = Path(tmp)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": "h3-manifest-v1",
                    "set": "stage-probe",
                    "documents": list(docs),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        scenario_path = tmp_path / "scenario.json"
        scenario_path.write_text(
            json.dumps({"schema": "h3-fake-scenario-v1", "rules": list(scenario_rules)}),
            encoding="utf-8",
        )
        out_path = tmp_path / "probe.json"
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "_probe-stages",
            "--bound-tree",
            str(bound_tree),
            "--terms-db",
            str(terms_db),
            "--manifest",
            str(manifest_path),
            "--scenario-name",
            scenario_name,
            "--scenario-file",
            str(scenario_path),
            "--fixed-placeholder-prefix",
            fixed_placeholder_prefix,
            "--json-out",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise DiffToolError(
                f"_probe-stages failed for {bound_tree.name}: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return json.loads(out_path.read_text(encoding="utf-8"))


def localize_document_stages(
    doc_a: Mapping[str, Any],
    doc_b: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare stage digests between tree A and tree B for one document."""
    attempts_a = doc_a.get("attempts") or []
    attempts_b = doc_b.get("attempts") or []
    earliest_stage: str | None = None
    diverging_fields: list[str] = []
    ambiguous_counts_a = 0
    ambiguous_counts_b = 0
    fixed_nonce_request_diffs = 0
    fixed_nonce_requests_compared = 0

    def note(stage: str, field: str) -> None:
        nonlocal earliest_stage
        if earliest_stage is None:
            earliest_stage = stage
        if field not in diverging_fields:
            diverging_fields.append(field)

    if len(attempts_a) != len(attempts_b):
        note("attempt", "attempt_count")
        unmatched_attempts = (
            attempts_a[len(attempts_b) :] if len(attempts_a) > len(attempts_b) else attempts_b[len(attempts_a) :]
        )
        for att in unmatched_attempts:
            for seg in att.get("segments") or []:
                n_reqs = len(seg.get("requests") or [])
                fixed_nonce_requests_compared += n_reqs
                fixed_nonce_request_diffs += n_reqs

    for att_a, att_b in zip(attempts_a, attempts_b):
        segs_a = att_a.get("segments") or []
        segs_b = att_b.get("segments") or []
        if len(segs_a) != len(segs_b):
            note("segmentation", "segment_count")
            unmatched_segs = (
                segs_a[len(segs_b) :] if len(segs_a) > len(segs_b) else segs_b[len(segs_a) :]
            )
            for seg in unmatched_segs:
                n_reqs = len(seg.get("requests") or [])
                fixed_nonce_requests_compared += n_reqs
                fixed_nonce_request_diffs += n_reqs
        for seg_a, seg_b in zip(segs_a, segs_b):
            ambiguous_counts_a += int(seg_a.get("ambiguous_count", 0))
            ambiguous_counts_b += int(seg_b.get("ambiguous_count", 0))
            if seg_a.get("prepared_text_sha256") != seg_b.get("prepared_text_sha256"):
                note("prepare_text", "prepared_text_sha256")
            if (
                seg_a.get("locks_count") != seg_b.get("locks_count")
                or seg_a.get("locks_sha256") != seg_b.get("locks_sha256")
            ):
                note("prepared_terms", "locks")
            if (
                seg_a.get("references_count") != seg_b.get("references_count")
                or seg_a.get("references_sha256") != seg_b.get("references_sha256")
            ):
                note("prepared_terms", "references")
            if seg_a.get("locked_text_sha256") != seg_b.get("locked_text_sha256"):
                note("prepared_terms", "locked_text_sha256")

            reqs_a = seg_a.get("requests") or []
            reqs_b = seg_b.get("requests") or []
            if len(reqs_a) != len(reqs_b):
                note("request_spec", "request_count")
                extra = abs(len(reqs_a) - len(reqs_b))
                fixed_nonce_requests_compared += extra
                fixed_nonce_request_diffs += extra
            for req_a, req_b in zip(reqs_a, reqs_b):
                fixed_nonce_requests_compared += 1
                req_diff = False
                if req_a.get("plan_core_sha256") != req_b.get("plan_core_sha256"):
                    note("translation_plan", "plan_core_sha256")
                    req_diff = True
                if req_a.get("attempt_kind") != req_b.get("attempt_kind"):
                    note("request_spec", "attempt_kind")
                    req_diff = True
                if req_a.get("thinking_mode") != req_b.get("thinking_mode"):
                    note("request_spec", "thinking_mode")
                    req_diff = True
                if req_a.get("system_prompt_sha256") != req_b.get("system_prompt_sha256"):
                    note("request_spec", "system_prompt_sha256")
                    req_diff = True
                if req_a.get("user_prompt_sha256") != req_b.get("user_prompt_sha256"):
                    note("request_spec", "user_prompt_sha256")
                    req_diff = True
                if req_a.get("payload_sha256") != req_b.get("payload_sha256"):
                    note("request_spec", "payload_sha256")
                    req_diff = True
                if req_diff:
                    fixed_nonce_request_diffs += 1

    if doc_a.get("output_sha256") != doc_b.get("output_sha256"):
        note("transport_output", "output_sha256")

    return {
        "fixed_nonce_requests_compared": fixed_nonce_requests_compared,
        "fixed_nonce_request_diffs": fixed_nonce_request_diffs,
        "earliest_diverging_stage": earliest_stage,
        "diverging_fields": diverging_fields,
        "ambiguous_mentions_recorded_a": ambiguous_counts_a,
        "ambiguous_mentions_recorded_b": ambiguous_counts_b,
    }


# --------------------------------------------------- diff comparison & summary


def _doc_post_count(doc_entry: Mapping[str, Any]) -> int:
    return sum(
        len(seg.get("requests") or [])
        for att in (doc_entry.get("attempts") or [])
        for seg in (att.get("segments") or [])
    )


def compare_scenario_outputs(
    *,
    scenario_name: str,
    run_a: Mapping[str, Any],
    run_b: Mapping[str, Any],
    docs_by_id: Mapping[str, Mapping[str, str]],
    target_a: TreeTarget,
    target_b: TreeTarget,
    terms_db: Path,
    scenario_rules: Sequence[Mapping[str, Any]],
    always_verify_stages: bool = False,
) -> dict[str, Any]:
    docs_a: Mapping[str, Any] = run_a["documents"]
    docs_b: Mapping[str, Any] = run_b["documents"]

    all_ids = sorted(set(docs_a) | set(docs_b))
    counts = {
        "documents_total": len(all_ids),
        "documents_matched": 0,
        "documents_inconsistent": 0,
        "posts_a": int(run_a["posts_total"]),
        "posts_b": int(run_b["posts_total"]),
        "post_count_diffs": 0,
        "output_sha256_diffs": 0,
        "requests_compared": 0,
        "attempt_kind_diffs": 0,
        "thinking_mode_diffs": 0,
        "normalized_payload_sha256_diffs": 0,
        "raw_payload_sha256_equal": 0,
        "raw_payload_sha256_nonce_only_diffs": 0,
        "raw_payload_sha256_real_diffs": 0,
        "raw_system_prompt_sha256_equal": 0,
        "raw_system_prompt_sha256_nonce_only_diffs": 0,
        "raw_system_prompt_sha256_real_diffs": 0,
        "raw_user_prompt_sha256_equal": 0,
        "raw_user_prompt_sha256_nonce_only_diffs": 0,
        "raw_user_prompt_sha256_real_diffs": 0,
        "fixed_nonce_requests_verified": 0,
        "fixed_nonce_request_diffs": 0,
    }
    diff_categories: dict[str, int] = {}
    inconsistent_docs: list[dict[str, Any]] = []
    docs_needing_probe: list[str] = []
    doc_eval_issues: dict[str, list[str]] = {}

    def bump(cat: str) -> None:
        diff_categories[cat] = diff_categories.get(cat, 0) + 1

    for doc_id in all_ids:
        entry_a = docs_a.get(doc_id)
        entry_b = docs_b.get(doc_id)
        issues: list[str] = []
        had_nonce_diff = False

        if entry_a is None or entry_b is None:
            issues.append("missing_document")
            bump("missing_document")
            doc_eval_issues[doc_id] = issues
            docs_needing_probe.append(doc_id)
            continue

        if entry_a.get("status") != entry_b.get("status"):
            issues.append("status_diff")
            bump("status_diff")

        out_a = sha256_text(entry_a["output"]) if entry_a.get("output") is not None else None
        out_b = sha256_text(entry_b["output"]) if entry_b.get("output") is not None else None
        if out_a != out_b:
            counts["output_sha256_diffs"] += 1
            issues.append("output_sha256_diff")
            bump("output_sha256_diff")

        posts_a = _doc_post_count(entry_a)
        posts_b = _doc_post_count(entry_b)
        if posts_a != posts_b:
            counts["post_count_diffs"] += 1
            issues.append("post_count_diff")
            bump("post_count_diff")

        attempts_a = entry_a.get("attempts") or []
        attempts_b = entry_b.get("attempts") or []
        if len(attempts_a) != len(attempts_b):
            issues.append("attempt_count_diff")
            bump("attempt_count_diff")

        for att_a, att_b in zip(attempts_a, attempts_b):
            segs_a = att_a.get("segments") or []
            segs_b = att_b.get("segments") or []
            if len(segs_a) != len(segs_b):
                issues.append("segment_count_diff")
                bump("segment_count_diff")
            for seg_a, seg_b in zip(segs_a, segs_b):
                if seg_a.get("status") != seg_b.get("status"):
                    issues.append("segment_status_diff")
                    bump("segment_status_diff")
                reqs_a = seg_a.get("requests") or []
                reqs_b = seg_b.get("requests") or []
                if len(reqs_a) != len(reqs_b):
                    issues.append("segment_request_count_diff")
                    bump("segment_request_count_diff")
                for req_a, req_b in zip(reqs_a, reqs_b):
                    counts["requests_compared"] += 1
                    if req_a.get("attempt_kind") != req_b.get("attempt_kind"):
                        counts["attempt_kind_diffs"] += 1
                        issues.append("attempt_kind_diff")
                        bump("attempt_kind_diff")
                    if req_a.get("thinking_mode") != req_b.get("thinking_mode"):
                        counts["thinking_mode_diffs"] += 1
                        issues.append("thinking_mode_diff")
                        bump("thinking_mode_diff")

                    norm_eq = req_a.get("normalized_payload_sha256") == req_b.get(
                        "normalized_payload_sha256"
                    )
                    if not norm_eq:
                        counts["normalized_payload_sha256_diffs"] += 1
                        issues.append("normalized_payload_sha256_diff")
                        bump("normalized_payload_sha256_diff")

                    raw_payload_eq = req_a.get("payload_sha256") == req_b.get("payload_sha256")
                    if raw_payload_eq:
                        counts["raw_payload_sha256_equal"] += 1
                    elif norm_eq:
                        counts["raw_payload_sha256_nonce_only_diffs"] += 1
                        had_nonce_diff = True
                    else:
                        counts["raw_payload_sha256_real_diffs"] += 1
                        issues.append("raw_payload_sha256_diff")
                        bump("raw_payload_sha256_diff")

                    raw_sys_eq = req_a.get("system_prompt_sha256") == req_b.get(
                        "system_prompt_sha256"
                    )
                    if raw_sys_eq:
                        counts["raw_system_prompt_sha256_equal"] += 1
                    elif norm_eq:
                        counts["raw_system_prompt_sha256_nonce_only_diffs"] += 1
                        had_nonce_diff = True
                    else:
                        counts["raw_system_prompt_sha256_real_diffs"] += 1
                        issues.append("system_prompt_sha256_diff")
                        bump("system_prompt_sha256_diff")

                    raw_usr_eq = req_a.get("user_prompt_sha256") == req_b.get(
                        "user_prompt_sha256"
                    )
                    if raw_usr_eq:
                        counts["raw_user_prompt_sha256_equal"] += 1
                    elif norm_eq:
                        counts["raw_user_prompt_sha256_nonce_only_diffs"] += 1
                        had_nonce_diff = True
                    else:
                        counts["raw_user_prompt_sha256_real_diffs"] += 1
                        issues.append("user_prompt_sha256_diff")
                        bump("user_prompt_sha256_diff")

        if issues or had_nonce_diff or always_verify_stages:
            docs_needing_probe.append(doc_id)
        doc_eval_issues[doc_id] = sorted(set(issues))

    # Stage-level verification & localization for all flagged documents
    stage_localization_by_doc: dict[str, dict[str, Any]] = {}
    probe_matcher_revs: dict[str, str] = {}
    if docs_needing_probe:
        probe_docs = [dict(docs_by_id[doc_id]) for doc_id in docs_needing_probe if doc_id in docs_by_id]
        if probe_docs:
            probe_a = run_stage_probe_subprocess(
                bound_tree=target_a.bound_tree,
                terms_db=terms_db,
                docs=probe_docs,
                scenario_name=scenario_name,
                scenario_rules=scenario_rules,
            )
            probe_b = run_stage_probe_subprocess(
                bound_tree=target_b.bound_tree,
                terms_db=terms_db,
                docs=probe_docs,
                scenario_name=scenario_name,
                scenario_rules=scenario_rules,
            )
            probe_matcher_revs = {
                target_a.label: probe_a["matcher_revision"],
                target_b.label: probe_b["matcher_revision"],
            }
            for doc_id in docs_needing_probe:
                pa = probe_a["documents"].get(doc_id)
                pb = probe_b["documents"].get(doc_id)
                if pa is None or pb is None:
                    continue
                entry_a = docs_a.get(doc_id)
                entry_b = docs_b.get(doc_id)
                probe_posts_a = _doc_post_count(pa)
                probe_posts_b = _doc_post_count(pb)
                if entry_a is not None and probe_posts_a != _doc_post_count(entry_a):
                    doc_eval_issues[doc_id].append("probe_post_count_mismatch_a")
                    bump("probe_post_count_mismatch_a")
                if entry_b is not None and probe_posts_b != _doc_post_count(entry_b):
                    doc_eval_issues[doc_id].append("probe_post_count_mismatch_b")
                    bump("probe_post_count_mismatch_b")
                loc = localize_document_stages(pa, pb)
                stage_localization_by_doc[doc_id] = loc
                counts["fixed_nonce_requests_verified"] += int(
                    loc["fixed_nonce_requests_compared"]
                )
                counts["fixed_nonce_request_diffs"] += int(loc["fixed_nonce_request_diffs"])
                if loc["earliest_diverging_stage"] is not None:
                    for field in loc["diverging_fields"]:
                        issue_name = f"stage_{loc['earliest_diverging_stage']}_{field}"
                        if issue_name not in doc_eval_issues[doc_id]:
                            doc_eval_issues[doc_id].append(issue_name)
                            bump(issue_name)

    for doc_id in all_ids:
        issues = doc_eval_issues.get(doc_id, [])
        if issues:
            counts["documents_inconsistent"] += 1
            src_doc = docs_by_id.get(doc_id, {})
            text = str(src_doc.get("text", ""))
            record: dict[str, Any] = {
                "doc_id": doc_id,
                "doc_sha16": sha16_text(text) if text else None,
                "direction": src_doc.get("direction"),
                "issues": issues,
            }
            if doc_id in stage_localization_by_doc:
                record["stage_localization"] = stage_localization_by_doc[doc_id]
            inconsistent_docs.append(record)
        else:
            counts["documents_matched"] += 1

    ambiguous_total_a = sum(
        v.get("ambiguous_mentions_recorded_a", 0) for v in stage_localization_by_doc.values()
    )
    ambiguous_total_b = sum(
        v.get("ambiguous_mentions_recorded_b", 0) for v in stage_localization_by_doc.values()
    )

    return {
        "scenario": scenario_name,
        "counts": counts,
        "diff_categories": diff_categories,
        "stage_probe": {
            "documents_probed": len(stage_localization_by_doc),
            "matcher_revisions": probe_matcher_revs,
            "ambiguous_mentions_recorded_a": ambiguous_total_a,
            "ambiguous_mentions_recorded_b": ambiguous_total_b,
        },
        "inconsistent_documents": inconsistent_docs,
    }


# ------------------------------------------------ static reachability summary


def _src_tree_file_hashes(src_root: Path) -> dict[str, str]:
    if not src_root.is_dir():
        raise DiffToolError(f"missing src directory: {src_root}")
    hashes: dict[str, str] = {}
    for path in sorted(src_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src_root.parent)
        if "__pycache__" in rel.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        hashes[rel.as_posix()] = sha256_file(path)
    return hashes


def build_static_reachability_summary(
    tree_a: Path,
    tree_b: Path,
    *,
    src_a: str | None = None,
    src_b: str | None = None,
) -> dict[str, Any]:
    """Compute actual ``src/`` file diffs between ``tree_a`` and ``tree_b``.

    The curated ``v0.13.0 -> v0.14.0`` static reachability notes are attached
    only when ``src_a`` and ``src_b`` match the pinned baseline SHAs.
    """
    commit_a = git_rev_parse(tree_a, "HEAD")
    commit_b = git_rev_parse(tree_b, "HEAD")
    resolved_src_a = src_a or git_rev_parse(tree_a, "HEAD:src")
    resolved_src_b = src_b or git_rev_parse(tree_b, "HEAD:src")

    hashes_a = _src_tree_file_hashes(tree_a / "src")
    hashes_b = _src_tree_file_hashes(tree_b / "src")
    changed_files = [
        rel
        for rel in sorted(set(hashes_a) | set(hashes_b))
        if hashes_a.get(rel) != hashes_b.get(rel)
    ]

    pinned_baseline_match = (
        resolved_src_a == PINNED_V013_SRC_TREE
        and resolved_src_b == PINNED_V014_SRC_TREE
    )
    analysis: dict[str, str] | None = None
    if pinned_baseline_match:
        analysis = {
            "cjk_span.py": (
                "Only MATCHER_REVISION bumped (v10 -> v11); matcher functions, _WORDS lexicon, "
                "and single-character name sets are unchanged."
            ),
            "single_char_context.py": (
                "New pure context-evidence module; imported only by term_preparation and review."
            ),
            "term_preparation.py": (
                "Added AmbiguousMention dataclass and PreparedTerms.ambiguous; refactored "
                "_lock_mixed_single_char_names to delegate to _lock_mixed_single_char_names_with_deltas "
                "which runs identical selection/locking logic and also returns placeholder length deltas "
                "to shift PreparedTerms.ambiguous coordinates. prepare_term_segment collects "
                "ambiguous_mentions read-only without mutating selected, locks, references, or locked_text."
            ),
            "translation_plan.py": (
                "Added RequestPolicy.ambiguous_mention_exposure (default 'off') and "
                "TranslationPlan.ambiguous (default ()). While exposure is 'off', "
                "to_identity_dict omits ambiguous_mention_exposure and build_translation_messages "
                "never reads plan.ambiguous, leaving prompt bytes and wire payloads byte-identical."
            ),
            "application.py": (
                "Only review_pair (ambiguous_mentions opt-in), project_review_report sort key, "
                "service_capabilities, and new check_structure added; translation pipeline untouched."
            ),
            "review.py": "Opt-in ambiguous-mention-v1 for review-v2 only; unreachable from translation.",
            "structural_checks.py": "Standalone deterministic layer-1 check module; unreachable from translation.",
            "wuwaterm_api/app.py": (
                "Added ambiguous-mention-v1 review header and POST /v1/structure-checks; "
                "translation routes untouched."
            ),
        }

    return {
        "commit_a": commit_a,
        "commit_b": commit_b,
        "src_tree_a": resolved_src_a,
        "src_tree_b": resolved_src_b,
        "pinned_v013_v014_baseline": pinned_baseline_match,
        "changed_src_files": changed_files,
        "default_translation_path_analysis": analysis,
    }


# ---------------------------------------------- local v0.14.0 API verification


def _verify_v014_api_in_process(*, bound_tree: Path, terms_db: Path) -> dict[str, Any]:
    """Run inside a dedicated subprocess to verify v0.14.0 review-v2 and /v1/meta."""
    import httpx

    src_dir = (bound_tree / "src").resolve()
    if "wuwaterm" in sys.modules:
        raise DiffToolError("wuwaterm already imported before API check")
    sys.path.insert(0, str(src_dir))

    from wuwaterm.application import build_term_service, review_pair, service_metadata
    from wuwaterm_api.app import create_app
    from wuwaterm_api.auth import SCOPE_META, SCOPE_TRANSLATE, DeviceStore
    from wuwaterm_api.settings import ApiSettings

    svc = build_term_service(terms_db)
    meta = service_metadata(svc)
    direct_review = review_pair(
        svc,
        source="今汐和守岸人正在研究声骸。",
        target="Jinhsi and Shorekeeper are studying Echoes.",
        direction="en",
        review_version="review-v2",
    )

    async def call_asgi() -> dict[str, Any]:
        import secrets

        with tempfile.TemporaryDirectory(prefix="h3-api-check-") as tmp:
            device_db = Path(tmp) / "devices.db"
            store = DeviceStore(device_db, guard_legacy_default=False)
            secret = secrets.token_urlsafe(24)
            dev = store.issue(
                "offline-check",
                scopes=(SCOPE_META, SCOPE_TRANSLATE),
                secret=secret,
            )
            token = f"wtd1.{dev.device_id}.{secret}"
            settings = ApiSettings(
                db_path=terms_db,
                device_db_path=device_db,
                device_db_is_default=False,
            )
            app = create_app(settings=settings, device_store=store, term_service=svc)
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1",
            ) as client:
                meta_resp = await client.get("/v1/meta", headers=headers)
                review_resp = await client.post(
                    "/v1/reviews",
                    headers={
                        **headers,
                        "X-WuwaTerm-Review-Features": "reason-v1",
                        "X-WuwaTerm-Matcher-Basis": "1",
                    },
                    json={
                        "review_version": "review-v2",
                        "direction": "en",
                        "source": "今汐和守岸人正在研究声骸。",
                        "target": "Jinhsi and Shorekeeper are studying Echoes.",
                    },
                )
            return {
                "meta_status": meta_resp.status_code,
                "meta_body": meta_resp.json(),
                "review_status": review_resp.status_code,
                "review_body": review_resp.json(),
            }

    asgi = asyncio.run(call_asgi())
    http_meta_term_count = asgi["meta_body"].get("term_count")
    http_review_dict = asgi["review_body"].get("dictionary") or {}
    http_review_dict_rev = http_review_dict.get("revision")
    http_review_dict_term_count = http_review_dict.get("term_count")
    http_review_matcher_rev = asgi["review_body"].get("matcher_revision")

    ok = (
        direct_review.dictionary.revision == EXPECTED_V014_DICTIONARY_REVISION
        and direct_review.dictionary.term_count == EXPECTED_V014_TERM_COUNT
        and direct_review.matcher_revision == EXPECTED_V014_MATCHER_REVISION
        and meta.term_count == EXPECTED_V014_TERM_COUNT
        and asgi["meta_status"] == 200
        and http_meta_term_count == EXPECTED_V014_TERM_COUNT
        and asgi["review_status"] == 200
        and http_review_dict_rev == EXPECTED_V014_DICTIONARY_REVISION
        and http_review_dict_term_count == EXPECTED_V014_TERM_COUNT
        and http_review_matcher_rev == EXPECTED_V014_MATCHER_REVISION
    )

    return {
        "passed": ok,
        "dictionary_revision": http_review_dict_rev,
        "expected_dictionary_revision": EXPECTED_V014_DICTIONARY_REVISION,
        "term_count": http_meta_term_count,
        "expected_term_count": EXPECTED_V014_TERM_COUNT,
        "matcher_revision": http_review_matcher_rev,
        "expected_matcher_revision": EXPECTED_V014_MATCHER_REVISION,
        "http_meta_status": asgi["meta_status"],
        "http_review_status": asgi["review_status"],
    }


def run_v014_api_check_subprocess(*, bound_tree: Path, terms_db: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="h3-api-check-out-") as tmp:
        out_path = Path(tmp) / "api.json"
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "_verify-api",
            "--bound-tree",
            str(bound_tree),
            "--terms-db",
            str(terms_db),
            "--json-out",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode not in (0, 1) or not out_path.is_file():
            raise DiffToolError(
                f"_verify-api failed (rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return json.loads(out_path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ main entry


def execute_differential(
    *,
    tree_a: Path,
    tree_b: Path,
    terms_db: Path,
    expected_tree_a: str | None = None,
    expected_tree_b: str | None = None,
    expected_terms_sha256: str | None = None,
    docs: Sequence[Mapping[str, str]],
    scenarios: Sequence[str] = ("default", "timeout_fallback", "paragraph_repair"),
    verify_v014_api: bool = True,
    always_verify_stages: bool = False,
) -> tuple[int, dict[str, Any]]:
    ensure_not_blind_path(tree_a)
    ensure_not_blind_path(tree_b)
    ensure_not_blind_path(terms_db)
    if not terms_db.is_file():
        raise DiffToolError(f"terms_db is not a file: {terms_db}")

    src_a = expected_tree_a or git_rev_parse(tree_a, "HEAD:src")
    src_b = expected_tree_b or git_rev_parse(tree_b, "HEAD:src")
    terms_sha = expected_terms_sha256 or sha256_file(terms_db)

    target_a = TreeTarget(label="tree_a", bound_tree=tree_a.resolve(), expected_src_tree=src_a)
    target_b = TreeTarget(label="tree_b", bound_tree=tree_b.resolve(), expected_src_tree=src_b)

    docs_list = list(docs)
    if not docs_list:
        raise DiffToolError("no documents provided for differential run")
    docs_by_id = {d["doc_id"]: d for d in docs_list}

    scenario_reports: list[dict[str, Any]] = []
    any_inconsistent = False

    for scenario_name in scenarios:
        if scenario_name not in SCENARIOS:
            raise DiffToolError(f"unknown scenario {scenario_name!r}")
        rules = SCENARIOS[scenario_name]
        run_a = run_eval_h3_scenario(
            target=target_a,
            terms_db=terms_db.resolve(),
            expected_terms_sha256=terms_sha,
            docs=docs_list,
            scenario_name=scenario_name,
            scenario_rules=rules,
        )
        run_b = run_eval_h3_scenario(
            target=target_b,
            terms_db=terms_db.resolve(),
            expected_terms_sha256=terms_sha,
            docs=docs_list,
            scenario_name=scenario_name,
            scenario_rules=rules,
        )
        report = compare_scenario_outputs(
            scenario_name=scenario_name,
            run_a=run_a,
            run_b=run_b,
            docs_by_id=docs_by_id,
            target_a=target_a,
            target_b=target_b,
            terms_db=terms_db.resolve(),
            scenario_rules=rules,
            always_verify_stages=always_verify_stages,
        )
        if report["counts"]["documents_inconsistent"] > 0:
            any_inconsistent = True
        scenario_reports.append(report)

    api_check: dict[str, Any] | None = None
    if verify_v014_api:
        api_check = run_v014_api_check_subprocess(
            bound_tree=target_b.bound_tree,
            terms_db=terms_db.resolve(),
        )
        if not api_check["passed"]:
            any_inconsistent = True

    manifest_digest = sha256_bytes(
        canonical_json(
            [
                {
                    "doc_id": d["doc_id"],
                    "direction": d["direction"],
                    "genre": d["genre"],
                    "text_sha256": sha256_text(d["text"]),
                }
                for d in docs_list
            ]
        )
    )

    direction_counts = {
        "zh->en": sum(1 for d in docs_list if d["direction"] == "zh->en"),
        "en->zh": sum(1 for d in docs_list if d["direction"] == "en->zh"),
    }

    summary = {
        "schema": "h3-tree-diff-v1",
        "bindings": {
            "tree_a_src": src_a,
            "tree_b_src": src_b,
            "terms_db_sha256": terms_sha,
            "corpus_manifest_sha256": manifest_digest,
        },
        "corpus": {
            "documents_total": len(docs_list),
            "directions": direction_counts,
        },
        "scenarios": scenario_reports,
        "v014_api_verification": api_check,
        "static_reachability": build_static_reachability_summary(
            target_a.bound_tree,
            target_b.bound_tree,
            src_a=src_a,
            src_b=src_b,
        ),
        "differential_clean": not any_inconsistent,
    }
    return (0 if not any_inconsistent else 1), summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_h3_tree_diff.py",
        description="Differential harness comparing two Core trees via eval_h3.py",
    )
    sub = parser.add_subparsers(dest="command")

    # Default run subcommand (also supported at top level when command is omitted)
    for target in (parser, sub.add_parser("run", help="run differential comparison")):
        target.add_argument("--tree-a", type=Path, default=None, help="baseline Core checkout")
        target.add_argument("--tree-b", type=Path, default=None, help="candidate Core checkout")
        target.add_argument("--terms-db", type=Path, default=None, help="pinned terms SQLite DB")
        target.add_argument("--expected-tree-a", default=None)
        target.add_argument("--expected-tree-b", default=None)
        target.add_argument("--expected-terms-sha256", default=None)
        target.add_argument("--manifest", type=Path, default=None)
        target.add_argument("--corpus-dir", type=Path, default=None)
        target.add_argument("--vps-inputs", type=Path, default=None)
        target.add_argument("--r-inputs", type=Path, default=None)
        target.add_argument("--m-gold", type=Path, default=None)
        target.add_argument("--no-synthetic", action="store_true")
        target.add_argument(
            "--scenarios",
            default="default,timeout_fallback,paragraph_repair",
            help="comma-separated scenario names",
        )
        target.add_argument("--skip-api-check", action="store_true")
        target.add_argument("--always-verify-stages", action="store_true")
        target.add_argument("--json-out", type=Path, default=None)

    probe = sub.add_parser("_probe-stages", help=argparse.SUPPRESS)
    probe.add_argument("--bound-tree", type=Path, required=True)
    probe.add_argument("--terms-db", type=Path, required=True)
    probe.add_argument("--manifest", type=Path, required=True)
    probe.add_argument("--scenario-name", required=True)
    probe.add_argument("--scenario-file", type=Path, required=True)
    probe.add_argument("--fixed-placeholder-prefix", default=None)
    probe.add_argument("--json-out", type=Path, required=True)

    api_cmd = sub.add_parser("_verify-api", help=argparse.SUPPRESS)
    api_cmd.add_argument("--bound-tree", type=Path, required=True)
    api_cmd.add_argument("--terms-db", type=Path, required=True)
    api_cmd.add_argument("--json-out", type=Path, required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "_probe-stages":
            docs = load_manifest_documents(args.manifest)
            scenario_data = json.loads(args.scenario_file.read_text(encoding="utf-8"))
            report = _probe_stages_in_process(
                bound_tree=args.bound_tree,
                terms_db=args.terms_db,
                docs=docs,
                scenario_name=args.scenario_name,
                scenario_rules=scenario_data.get("rules") or [],
                fixed_placeholder_prefix=args.fixed_placeholder_prefix,
            )
            args.json_out.write_text(
                json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return 0

        if args.command == "_verify-api":
            result = _verify_v014_api_in_process(
                bound_tree=args.bound_tree,
                terms_db=args.terms_db,
            )
            args.json_out.write_text(
                json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return 0 if result["passed"] else 1

        if args.tree_a is None or args.tree_b is None or args.terms_db is None:
            raise DiffToolError("--tree-a, --tree-b, and --terms-db are required")

        if args.manifest is not None:
            docs = load_manifest_documents(args.manifest)
        else:
            docs = load_allowed_corpora(
                corpus_dir=args.corpus_dir,
                vps_inputs=args.vps_inputs,
                r_inputs=args.r_inputs,
                m_gold=args.m_gold,
                include_synthetic=not args.no_synthetic,
            )

        scenarios = [
            s.strip() for s in str(args.scenarios).split(",") if s.strip()
        ]
        code, summary = execute_differential(
            tree_a=args.tree_a,
            tree_b=args.tree_b,
            terms_db=args.terms_db,
            expected_tree_a=args.expected_tree_a,
            expected_tree_b=args.expected_tree_b,
            expected_terms_sha256=args.expected_terms_sha256,
            docs=docs,
            scenarios=scenarios,
            verify_v014_api=not args.skip_api_check,
            always_verify_stages=args.always_verify_stages,
        )
        text = json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return code
    except DiffToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
