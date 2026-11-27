"""Tests for optional pluggable vector retrieval, fail-closed validation, and RRF fusion."""

from __future__ import annotations

import json
import logging
import math
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import build_knowledge_vectors
from scripts.eval_rag_offline import _build_synthetic_fixture
from wuwaterm.domain_knowledge import DomainKnowledgeCandidate
from wuwaterm.knowledge_db import compute_terms_db_revision
from wuwaterm.retrieval_vector import (
    VEC_SCHEMA_VERSION,
    DeterministicHashEncoder,
    FaissVectorBackend,
    Int8MemmapVectorBackend,
    OnnxInt8TextEncoder,
    SqliteVecBackend,
    VectorBackendUnavailableError,
    VectorHit,
    VectorIndexMetadataError,
    compute_file_sha256,
    hybrid_fuse_candidates,
    open_vector_backend,
    vector_hits_to_candidates,
)

ROOT = Path(__file__).resolve().parents[1]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def test_deterministic_hash_encoder_normalized_and_repeatable() -> None:
    enc = DeterministicHashEncoder(dim=256)
    assert enc.dim == 256
    assert enc.model_id == "fake-hash-ngram-v1"
    q1 = enc.encode_queries(["千古洑流 Emerald of Genesis"])[0]
    q2 = enc.encode_queries(["千古洑流 Emerald of Genesis"])[0]
    p_rel = enc.encode_passages(["千古洑流 | Emerald of Genesis | 五星迅刀"])[0]
    p_unrel = enc.encode_passages(["今天天气很好，心情也特别轻松。"])[0]
    assert q1 == q2
    assert len(q1) == 256
    assert math.isclose(math.sqrt(sum(x * x for x in q1)), 1.0, rel_tol=1e-6)
    assert _cosine(q1, p_rel) > _cosine(q1, p_unrel)


def test_int8_memmap_backend_roundtrip_atomic_and_metadata_fail_closed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    enc = DeterministicHashEncoder(dim=64)
    texts = [
        "冷凝 Glacio 冷凝属性伤害",
        "湮灭 Havoc 湮灭属性伤害",
        "千古洑流 Emerald of Genesis 五星迅刀",
    ]
    vecs = enc.encode_passages(texts)
    entity_ids = ["element:1", "element:6", "weapon:21020011"]
    surfaces = ["冷凝", "湮灭", "千古洑流"]
    kb_sha = "a" * 64
    terms_rev = "b" * 64

    meta = Int8MemmapVectorBackend.build(
        tmp_path,
        vectors=vecs,
        entity_ids=entity_ids,
        surfaces=surfaces,
        knowledge_db_sha256=kb_sha,
        terms_db_revision=terms_rev,
        model_id=enc.model_id,
        dim=64,
        quantization="int8",
    )
    assert meta.vec_schema_version == VEC_SCHEMA_VERSION
    assert meta.quantization == "int8"
    assert meta.record_count == 3
    assert (tmp_path / "knowledge-vec.bin").stat().st_size == 3 * (4 + 64)

    with Int8MemmapVectorBackend.open(
        tmp_path,
        expected_knowledge_db_sha256=kb_sha,
        expected_terms_db_revision=terms_rev,
        expected_model_id=enc.model_id,
        expected_dim=64,
        expected_quantization="int8",
    ) as backend:
        q_vec = enc.encode_queries(["装备千古洑流提升伤害"])[0]
        hits = backend.search(q_vec, top_k=3)
        assert len(hits) == 3
        assert hits[0].entity_id == "weapon:21020011"
        assert hits[0].source_surface == "千古洑流"
        assert hits[0].rank == 1
        assert hits[0].score > hits[1].score

    # Atomic replacement preserves prior valid index if a rebuild fails mid-way.
    with pytest.raises(ValueError, match="dim"):
        Int8MemmapVectorBackend.build(
            tmp_path,
            vectors=[[1.0, 2.0]],
            entity_ids=["bad"],
            knowledge_db_sha256=kb_sha,
            terms_db_revision=terms_rev,
            model_id=enc.model_id,
            dim=64,
            quantization="int8",
        )
    assert not list(tmp_path.glob(".*.tmp"))

    # Empty index edge case (0 records) opens and searches cleanly without mmap error.
    empty_dir = tmp_path / "empty"
    Int8MemmapVectorBackend.build(
        empty_dir,
        vectors=[],
        entity_ids=[],
        knowledge_db_sha256=kb_sha,
        terms_db_revision=terms_rev,
        model_id=enc.model_id,
        dim=64,
        quantization="fp32",
    )
    with Int8MemmapVectorBackend.open(
        empty_dir,
        expected_knowledge_db_sha256=kb_sha,
        expected_terms_db_revision=terms_rev,
    ) as empty_backend:
        assert empty_backend.search([0.1] * 64, top_k=5) == []

    # Metadata tampering fails closed and open_vector_backend degrades without leaking path.
    meta_path = tmp_path / "knowledge-vec.meta.json"
    original_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for field, bad_val in (
        ("vec_schema_version", "99"),
        ("knowledge_db_sha256", "0" * 64),
        ("terms_db_revision", "1" * 64),
        ("model_id", "wrong-model"),
        ("dim", 128),
        ("quantization", "fp32"),
    ):
        tampered = dict(original_meta)
        tampered[field] = bad_val
        meta_path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(VectorIndexMetadataError):
            Int8MemmapVectorBackend.open(
                tmp_path,
                expected_knowledge_db_sha256=kb_sha,
                expected_terms_db_revision=terms_rev,
                expected_model_id=enc.model_id,
                expected_dim=64,
                expected_quantization="int8",
            )
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            degraded = open_vector_backend(
                tmp_path,
                backend="memmap",
                expected_knowledge_db_sha256=kb_sha,
                expected_terms_db_revision=terms_rev,
                expected_model_id=enc.model_id,
                expected_dim=64,
                expected_quantization="int8",
            )
        assert degraded is None
        assert caplog.messages
        for msg in caplog.messages:
            assert str(tmp_path) not in msg


def test_optional_backends_and_onnx_encoder_degrade_gracefully_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import wuwaterm.retrieval_vector as rv

    def missing_import(name: str, *args: object, **kwargs: object) -> object:
        if name in {"sqlite_vec", "faiss", "onnxruntime", "tokenizers"}:
            raise ImportError(f"missing {name}")
        return rv.importlib.import_module(name)  # pragma: no cover

    monkeypatch.setattr(rv.importlib, "import_module", missing_import)

    with pytest.raises(VectorBackendUnavailableError, match="sqlite_vec"):
        SqliteVecBackend(tmp_path)
    with pytest.raises(VectorBackendUnavailableError, match="faiss"):
        FaissVectorBackend(tmp_path)
    with pytest.raises(VectorBackendUnavailableError, match="onnxruntime"):
        OnnxInt8TextEncoder(model_path=tmp_path / "m.onnx")

    with caplog.at_level(logging.WARNING):
        assert (
            open_vector_backend(
                tmp_path,
                backend="sqlite-vec",
                expected_knowledge_db_sha256="a" * 64,
                expected_terms_db_revision="b" * 64,
            )
            is None
        )
        assert (
            open_vector_backend(
                tmp_path,
                backend="faiss",
                expected_knowledge_db_sha256="a" * 64,
                expected_terms_db_revision="b" * 64,
            )
            is None
        )
    assert len(caplog.messages) == 2
    for msg in caplog.messages:
        assert str(tmp_path) not in msg


def test_sqlite_vec_extension_disabled_raises_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import types
    import wuwaterm.retrieval_vector as rv

    fake_sqlite_vec = types.SimpleNamespace(
        load=lambda _conn: None,
        serialize_float32=lambda v: bytes(len(v) * 4),
    )
    real_import = rv.importlib.import_module

    def patched_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sqlite_vec":
            return fake_sqlite_vec
        return real_import(name)

    class _NoExtConn:
        def execute(self, *_args: object, **_kwargs: object) -> object:
            return self

        def close(self) -> None:
            pass

    monkeypatch.setattr(rv.importlib, "import_module", patched_import)
    monkeypatch.setattr(rv.sqlite3, "connect", lambda *_a, **_kw: _NoExtConn())

    db_file = tmp_path / "knowledge-vec.sqlite3"
    db_file.write_bytes(b"SQLite format 3\x00")
    with pytest.raises(VectorBackendUnavailableError, match="enable_load_extension"):
        SqliteVecBackend(db_file)


def test_hybrid_fuse_candidates_enforces_lock_isolation_and_ordinary_word_gates() -> None:
    lex_glacio = DomainKnowledgeCandidate(
        entity_id="element:1",
        source_surface="冷凝",
        zh="冷凝",
        en="Glacio",
        entity_type="element",
        trust_tier="official_authoritative",
        retrieval_channel="short_ngram",
        score=0.02,
        context_gloss="冷凝属性伤害",
        lock_eligible=False,
    )
    vec_camellya = DomainKnowledgeCandidate(
        entity_id="resonator:1603",
        source_surface="椿",
        zh="椿",
        en="Camellya",
        entity_type="resonator",
        trust_tier="official_authoritative",
        retrieval_channel="dense_semantic",
        score=0.92,
        lock_eligible=False,
    )
    vec_encore = DomainKnowledgeCandidate(
        entity_id="resonator:1203",
        source_surface="Encore",
        zh="安可",
        en="Encore",
        entity_type="resonator",
        trust_tier="official_authoritative",
        retrieval_channel="dense_semantic",
        score=0.95,
        lock_eligible=False,
    )
    vec_echo = DomainKnowledgeCandidate(
        entity_id="term:850082",
        source_surface="Echo",
        zh="声骸",
        en="Echo",
        entity_type="term",
        trust_tier="official_authoritative",
        retrieval_channel="dense_semantic",
        score=0.91,
        lock_eligible=False,
    )

    # Ordinary CJK compound "香椿炒蛋" blocks "椿".
    fused_zh = hybrid_fuse_candidates(
        [lex_glacio],
        [vec_camellya],
        text="今天晚饭吃香椿炒蛋，加一点冷凝效果。",
    )
    assert [c.entity_id for c in fused_zh] == ["element:1"]
    assert all(c.lock_eligible is False for c in fused_zh)

    # Locked surface blocks candidate even if vector score is high.
    fused_locked = hybrid_fuse_candidates(
        [lex_glacio],
        [],
        text="冷凝伤害提升。",
        locked_surfaces=("冷凝",),
    )
    assert fused_locked == ()

    # Ordinary lowercase English words "encore" / "echo" are blocked.
    fused_en_neg = hybrid_fuse_candidates(
        [],
        [vec_encore, vec_echo],
        text="The audience asked for an encore after the echo faded.",
    )
    assert fused_en_neg == ()

    # Capitalized proper noun "Encore" passes and retains lock_eligible=False.
    fused_en_pos = hybrid_fuse_candidates(
        [],
        [vec_encore],
        text="Encore enters the field.",
    )
    assert len(fused_en_pos) == 1
    assert fused_en_pos[0].retrieval_channel == "dense_semantic"
    assert fused_en_pos[0].lock_eligible is False

    # vector_hits_to_candidates respects script alignment on English text.
    hits = [VectorHit(entity_id="term:850082", score=0.88, source_surface="声骸", rank=1)]
    rows = {
        "term:850082": {
            "entity_type": "term",
            "zh": "声骸",
            "en": "Echo",
            "trust_tier": "official_authoritative",
            "context_gloss": "Echo system",
        }
    }
    converted = vector_hits_to_candidates(hits, rows, text="A faint echo.")
    assert len(converted) == 1
    assert converted[0].source_surface == "Echo"
    assert converted[0].retrieval_channel == "dense_semantic"
    assert converted[0].lock_eligible is False
    assert hybrid_fuse_candidates([], converted, text="A faint echo.") == ()


def test_build_knowledge_vectors_cli_end_to_end_with_fake_encoder(
    tmp_path: Path,
) -> None:
    terms_db, kb_db = _build_synthetic_fixture(tmp_path / "fixture")
    out_dir = tmp_path / "vec_out"

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_knowledge_vectors.py"),
            "--knowledge-db",
            str(kb_db),
            "--out-dir",
            str(out_dir),
            "--backend",
            "memmap",
            "--encoder",
            "fake",
            "--dim",
            "512",
            "--quantization",
            "int8",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    built_meta = json.loads(proc.stdout)
    assert built_meta["vec_schema_version"] == VEC_SCHEMA_VERSION
    assert built_meta["knowledge_db_sha256"] == compute_file_sha256(kb_db)
    assert built_meta["terms_db_revision"] == compute_terms_db_revision(terms_db)
    assert built_meta["dim"] == 512
    assert built_meta["quantization"] == "int8"
    assert built_meta["record_count"] >= 15

    backend = open_vector_backend(
        out_dir,
        backend="memmap",
        expected_knowledge_db_sha256=compute_file_sha256(kb_db),
        expected_terms_db_revision=compute_terms_db_revision(terms_db),
        expected_dim=512,
        expected_quantization="int8",
    )
    assert backend is not None
    try:
        enc = DeterministicHashEncoder(dim=512)
        hits = backend.search(enc.encode_queries(["千古洑流 Emerald of Genesis"])[0], top_k=3)
        assert any(h.entity_id == "weapon:21020011" for h in hits)
    finally:
        backend.close()

    # Also verify Python helper API works directly.
    meta2 = build_knowledge_vectors.build_knowledge_vectors(
        kb_db, out_dir=tmp_path / "vec_out_py", backend="memmap", encoder_kind="fake"
    )
    assert meta2.record_count == built_meta["record_count"]


def test_dockerfile_and_core_dependencies_stay_free_of_ml_bloat() -> None:
    import tomllib

    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8").casefold()
    for forbidden in ("onnxruntime", "sqlite-vec", "sqlite_vec", "faiss", "rag-onnx", "rag-vec"):
        assert forbidden not in dockerfile

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["dependencies"]
    api_deps = pyproject["project"]["optional-dependencies"]["api"]
    for item in (*deps, *api_deps):
        lowered = item.casefold()
        for forbidden in ("onnxruntime", "sqlite-vec", "faiss", "numpy", "tokenizers"):
            assert forbidden not in lowered
