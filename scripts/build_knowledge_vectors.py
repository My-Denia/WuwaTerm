#!/usr/bin/env python3
"""Offline companion vector index builder from read-only ``knowledge.db``.

Reads ``kb_entities``, ``kb_aliases``, and ``kb_contexts`` from a verified
read-only ``knowledge.db``, encodes entity passages via either the zero-dep
``DeterministicHashEncoder`` (``--encoder fake``) or ``OnnxInt8TextEncoder``
(``--encoder onnx``), and atomically writes a version-bound vector index
(``memmap`` or ``sqlite-vec``) tied to ``knowledge_db_sha256`` and
``terms_db_revision``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wuwaterm.knowledge_db import (  # noqa: E402
    KB_SCHEMA_VERSION,
    KnowledgeBaseError,
    connect_knowledge_db_read_only,
)
from wuwaterm.retrieval_vector import (  # noqa: E402
    DEFAULT_VECTOR_DIM,
    SUPPORTED_ONNX_POOLINGS,
    DeterministicHashEncoder,
    Int8MemmapVectorBackend,
    OnnxInt8TextEncoder,
    OnnxPoolingMode,
    QuantizationMode,
    SqliteVecBackend,
    VectorBackendUnavailableError,
    VectorEncoder,
    VectorIndexMetadata,
    VectorRetrievalError,
    compute_file_sha256,
)

_MAX_ALIASES_PER_ENTITY = 12
_MAX_CONTEXT_SNIPPETS = 2
_MAX_PASSAGE_CHARS = 512


def _load_kb_passages(
    kb_path: Path,
) -> tuple[str, str, list[str], list[str], list[str]]:
    """Extract deterministic entity_ids, preferred surfaces, and passages from ``knowledge.db``."""
    kb_sha256 = compute_file_sha256(kb_path)
    with closing(connect_knowledge_db_read_only(kb_path)) as conn:
        meta_rows = conn.execute("SELECT key, value FROM kb_metadata").fetchall()
        meta = {str(r["key"]): str(r["value"]) for r in meta_rows}
        if meta.get("kb_schema_version") != KB_SCHEMA_VERSION:
            raise KnowledgeBaseError(
                f"unsupported kb_schema_version: {meta.get('kb_schema_version')!r}"
            )
        terms_rev = meta.get("terms_db_revision", "").strip()
        if not terms_rev:
            raise KnowledgeBaseError("knowledge.db missing terms_db_revision metadata")

        entities = conn.execute(
            """
            SELECT entity_id, entity_type, zh, en, context_gloss
            FROM kb_entities
            ORDER BY entity_id ASC
            """
        ).fetchall()

        aliases_by_entity: dict[str, list[str]] = {}
        for row in conn.execute(
            """
            SELECT entity_id, alias_surface
            FROM kb_aliases
            WHERE lock_eligible = 0
            ORDER BY entity_id ASC, id ASC
            """
        ):
            eid = str(row["entity_id"])
            bucket = aliases_by_entity.setdefault(eid, [])
            surf = str(row["alias_surface"]).strip()
            if surf and surf not in bucket and len(bucket) < _MAX_ALIASES_PER_ENTITY:
                bucket.append(surf)

        contexts_by_entity: dict[str, list[str]] = {}
        for row in conn.execute(
            """
            SELECT entity_id, zh_text, en_text
            FROM kb_contexts
            ORDER BY entity_id ASC, id ASC
            """
        ):
            eid = str(row["entity_id"])
            bucket = contexts_by_entity.setdefault(eid, [])
            if len(bucket) >= _MAX_CONTEXT_SNIPPETS:
                continue
            parts = [str(row["zh_text"]).strip(), str(row["en_text"]).strip()]
            snippet = " | ".join(p for p in parts if p)
            if snippet:
                bucket.append(snippet)

    entity_ids: list[str] = []
    surfaces: list[str] = []
    passages: list[str] = []
    for row in entities:
        eid = str(row["entity_id"])
        etype = str(row["entity_type"])
        zh = str(row["zh"]).strip()
        en = str(row["en"]).strip()
        gloss = str(row["context_gloss"]).strip()
        aliases = [
            a
            for a in aliases_by_entity.get(eid, ())
            if a != zh and a != en
        ]
        ctx_list = contexts_by_entity.get(eid, [])

        parts: list[str] = []
        if zh:
            parts.append(zh)
        if en:
            parts.append(en)
        if etype:
            parts.append(f"[{etype}]")
        if aliases:
            parts.append("aliases: " + ", ".join(aliases))
        if gloss:
            parts.append(gloss)
        if ctx_list:
            parts.append(" ".join(ctx_list))
        passage = " | ".join(parts)
        if len(passage) > _MAX_PASSAGE_CHARS:
            passage = passage[:_MAX_PASSAGE_CHARS].rstrip()

        entity_ids.append(eid)
        surfaces.append(zh or en or eid)
        passages.append(passage)

    return kb_sha256, terms_rev, entity_ids, surfaces, passages


def build_knowledge_vectors(
    knowledge_db_path: str | Path,
    *,
    out_dir: str | Path | None = None,
    backend: str = "memmap",
    encoder: VectorEncoder | None = None,
    encoder_kind: str = "fake",
    dim: int = DEFAULT_VECTOR_DIM,
    quantization: QuantizationMode | None = None,
    model_id: str | None = None,
    onnx_model_path: str | Path | None = None,
    onnx_tokenizer_path: str | Path | None = None,
    onnx_pooling: OnnxPoolingMode = "mean",
    query_prefix: str = "",
    passage_prefix: str = "",
    batch_size: int = 64,
) -> VectorIndexMetadata:
    """Build an atomic companion vector index from read-only ``knowledge.db``."""
    resolved_quant: QuantizationMode
    if quantization is None:
        resolved_quant = "fp32" if backend == "sqlite-vec" else "int8"
    else:
        resolved_quant = quantization

    if backend == "sqlite-vec" and resolved_quant != "fp32":
        raise ValueError(
            f"backend='sqlite-vec' only supports quantization='fp32' (got {resolved_quant!r}); "
            "use backend='memmap' for int8"
        )

    kb_path = Path(knowledge_db_path)
    target_dir = Path(out_dir) if out_dir is not None else kb_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)

    kb_sha256, terms_rev, entity_ids, surfaces, passages = _load_kb_passages(kb_path)

    active_encoder: VectorEncoder
    if encoder is not None:
        active_encoder = encoder
    elif encoder_kind == "fake":
        active_encoder = DeterministicHashEncoder(
            dim=dim,
            model_id=model_id or "fake-hash-ngram-v1",
        )
    elif encoder_kind == "onnx":
        if onnx_model_path is None:
            raise ValueError("--onnx-model is required when --encoder onnx")
        if onnx_pooling not in SUPPORTED_ONNX_POOLINGS:
            raise ValueError(f"unsupported onnx_pooling: {onnx_pooling!r}")
        active_encoder = OnnxInt8TextEncoder(
            model_path=onnx_model_path,
            tokenizer_path=onnx_tokenizer_path,
            model_id=model_id or Path(onnx_model_path).stem,
            dim=dim,
            query_prefix=query_prefix,
            passage_prefix=passage_prefix,
            pooling=onnx_pooling,
        )
    else:
        raise ValueError(f"unsupported encoder kind: {encoder_kind!r}")

    vectors: list[list[float]] = []
    step = max(1, int(batch_size))
    for start in range(0, len(passages), step):
        batch = passages[start : start + step]
        vectors.extend(active_encoder.encode_passages(batch))

    if backend == "memmap":
        return Int8MemmapVectorBackend.build(
            target_dir,
            vectors=vectors,
            entity_ids=entity_ids,
            surfaces=surfaces,
            knowledge_db_sha256=kb_sha256,
            terms_db_revision=terms_rev,
            model_id=active_encoder.model_id,
            dim=active_encoder.dim,
            quantization=resolved_quant,
        )
    if backend == "sqlite-vec":
        return SqliteVecBackend.build(
            target_dir,
            vectors=vectors,
            entity_ids=entity_ids,
            surfaces=surfaces,
            knowledge_db_sha256=kb_sha256,
            terms_db_revision=terms_rev,
            model_id=active_encoder.model_id,
            dim=active_encoder.dim,
            quantization=resolved_quant,
        )
    raise ValueError(f"unsupported vector backend: {backend!r}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_knowledge_vectors",
        description="Build companion vector index artifacts from read-only knowledge.db.",
    )
    parser.add_argument(
        "--knowledge-db",
        type=Path,
        default=ROOT / "data" / "knowledge.db",
        help="Path to read-only knowledge.db (default: data/knowledge.db).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory for companion vector artifacts (default: knowledge-db parent).",
    )
    parser.add_argument(
        "--backend",
        choices=("memmap", "sqlite-vec"),
        default="memmap",
        help="Vector index artifact backend (default: memmap).",
    )
    parser.add_argument(
        "--encoder",
        choices=("fake", "onnx"),
        default="onnx",
        help="Embedding encoder backend (default: onnx).",
    )
    parser.add_argument(
        "--dim",
        type=int,
        default=DEFAULT_VECTOR_DIM,
        help=f"Embedding dimension (default: {DEFAULT_VECTOR_DIM}).",
    )
    parser.add_argument(
        "--quantization",
        choices=("int8", "fp32"),
        default=None,
        help="Vector storage quantization mode (default: int8 for memmap, fp32 for sqlite-vec).",
    )
    parser.add_argument(
        "--model-id",
        type=str,
        default=None,
        help="Explicit model identifier recorded in vector index metadata.",
    )
    parser.add_argument(
        "--onnx-model",
        type=Path,
        default=None,
        help="Path to ONNX model file (required when --encoder onnx).",
    )
    parser.add_argument(
        "--onnx-tokenizer",
        type=Path,
        default=None,
        help="Path to HuggingFace tokenizers tokenizer.json (default: sibling of --onnx-model).",
    )
    parser.add_argument(
        "--onnx-pooling",
        choices=("mean", "cls", "last_token"),
        default="mean",
        help="Token pooling strategy for --encoder onnx (default: mean).",
    )
    parser.add_argument(
        "--query-prefix",
        type=str,
        default="",
        help="Optional query prefix (e.g. 'query: ' for E5 models).",
    )
    parser.add_argument(
        "--passage-prefix",
        type=str,
        default="",
        help="Optional passage prefix (e.g. 'passage: ' for E5 models).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="Encoding batch size (default: 64).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print built metadata as JSON.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.knowledge_db.is_file() or args.knowledge_db.stat().st_size == 0:
        print(
            f"error: knowledge database missing or empty: {args.knowledge_db.name}",
            file=sys.stderr,
        )
        return 2
    raw_quant = (
        args.quantization
        if args.quantization is not None
        else ("fp32" if args.backend == "sqlite-vec" else "int8")
    )
    quant: QuantizationMode = "int8" if raw_quant == "int8" else "fp32"
    try:
        meta = build_knowledge_vectors(
            args.knowledge_db,
            out_dir=args.out_dir,
            backend=args.backend,
            encoder_kind=args.encoder,
            dim=args.dim,
            quantization=quant,
            model_id=args.model_id,
            onnx_model_path=args.onnx_model,
            onnx_tokenizer_path=args.onnx_tokenizer,
            onnx_pooling=args.onnx_pooling,
            query_prefix=args.query_prefix,
            passage_prefix=args.passage_prefix,
            batch_size=args.batch_size,
        )
    except (
        KnowledgeBaseError,
        VectorBackendUnavailableError,
        VectorRetrievalError,
        ValueError,
        sqlite3.Error,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload: dict[str, Any] = meta.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"built {meta.backend} vector index: records={meta.record_count} "
            f"dim={meta.dim} quant={meta.quantization} model={meta.model_id}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
