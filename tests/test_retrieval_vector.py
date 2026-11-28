"""Tests for optional pluggable vector retrieval, fail-closed validation, and RRF fusion."""

from __future__ import annotations

import json
import logging
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

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

    # Mixed CJK + Latin sentences must not bypass ordinary Latin word gating.
    mixed_encore_hits = [
        VectorHit(entity_id="resonator:1203", score=0.93, source_surface="安可", rank=1)
    ]
    mixed_rows = {
        "resonator:1203": {
            "entity_type": "resonator",
            "zh": "安可",
            "en": "Encore",
            "trust_tier": "official_authoritative",
            "context_gloss": "黑海岸成员安可 | Encore of the Black Shores",
        },
        "term:850082": {
            "entity_type": "term",
            "zh": "声骸",
            "en": "Echo",
            "trust_tier": "official_authoritative",
            "context_gloss": "Echo system",
        },
    }
    mixed_encore_text = "观众大喊 encore 要求再来一首"
    mixed_encore_cands = vector_hits_to_candidates(
        mixed_encore_hits, mixed_rows, text=mixed_encore_text
    )
    assert mixed_encore_cands[0].source_surface == "Encore"
    assert hybrid_fuse_candidates([], mixed_encore_cands, text=mixed_encore_text) == ()
    # Even if a caller passes a pre-built candidate with zh surface ("安可"),
    # hybrid_fuse_candidates realigns and rejects lowercase "encore".
    assert hybrid_fuse_candidates([], [DomainKnowledgeCandidate(
        entity_id="resonator:1203",
        source_surface="安可",
        zh="安可",
        en="Encore",
        entity_type="resonator",
        trust_tier="official_authoritative",
        retrieval_channel="dense_semantic",
        score=0.93,
        lock_eligible=False,
    )], text=mixed_encore_text) == ()

    mixed_echo_text = "山谷里传来 echo 回声"
    mixed_echo_cands = vector_hits_to_candidates(hits, mixed_rows, text=mixed_echo_text)
    assert mixed_echo_cands[0].source_surface == "Echo"
    assert hybrid_fuse_candidates([], mixed_echo_cands, text=mixed_echo_text) == ()

    # Capitalized Encore in a mixed CJK + Latin sentence is preserved with lock_eligible=False.
    mixed_proper_text = "黑海岸的 Encore 和 Aalto"
    mixed_proper_cands = vector_hits_to_candidates(
        mixed_encore_hits, mixed_rows, text=mixed_proper_text
    )
    fused_mixed_proper = hybrid_fuse_candidates(
        [], mixed_proper_cands, text=mixed_proper_text
    )
    assert len(fused_mixed_proper) == 1
    assert fused_mixed_proper[0].entity_id == "resonator:1203"
    assert fused_mixed_proper[0].source_surface == "Encore"
    assert fused_mixed_proper[0].lock_eligible is False


def test_sqlite_vec_rejects_int8_quantization_and_faiss_corrupt_index_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import types
    import wuwaterm.retrieval_vector as rv

    with pytest.raises(ValueError, match="fp32"):
        SqliteVecBackend.build(
            tmp_path,
            vectors=[[1.0, 0.0]],
            entity_ids=["e1"],
            knowledge_db_sha256="a" * 64,
            terms_db_revision="b" * 64,
            model_id="m",
            dim=2,
            quantization="int8",
        )

    terms_db, kb_db = _build_synthetic_fixture(tmp_path / "fx")
    del terms_db
    rc = build_knowledge_vectors.main(
        [
            "--knowledge-db",
            str(kb_db),
            "--out-dir",
            str(tmp_path / "vec"),
            "--backend",
            "sqlite-vec",
            "--encoder",
            "fake",
            "--quantization",
            "int8",
        ]
    )
    assert rc == 2

    # Corrupted .faiss file raising RuntimeError converts to VectorIndexMetadataError
    # and open_vector_backend degrades fail-closed to None.
    def _raise_runtime(_path: str) -> object:
        raise RuntimeError("corrupt faiss index")

    fake_faiss = types.SimpleNamespace(read_index=_raise_runtime)
    fake_np = types.SimpleNamespace()
    real_import = rv.importlib.import_module

    def patched_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "faiss":
            return fake_faiss
        if name == "numpy":
            return fake_np
        return real_import(name)

    monkeypatch.setattr(rv.importlib, "import_module", patched_import)
    faiss_dir = tmp_path / "faiss_corrupt"
    faiss_dir.mkdir()
    (faiss_dir / "knowledge-vec.faiss").write_bytes(b"corrupt")
    (faiss_dir / "knowledge-vec.meta.json").write_text(
        json.dumps(
            {
                "vec_schema_version": VEC_SCHEMA_VERSION,
                "knowledge_db_sha256": "a" * 64,
                "terms_db_revision": "b" * 64,
                "model_id": "m",
                "dim": 4,
                "quantization": "fp32",
                "record_count": 1,
                "backend": "faiss",
                "entity_ids": ["e1"],
                "surfaces": ["s1"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(VectorIndexMetadataError, match="corrupted faiss"):
        FaissVectorBackend(
            faiss_dir,
            expected_knowledge_db_sha256="a" * 64,
            expected_terms_db_revision="b" * 64,
        )
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        degraded = open_vector_backend(
            faiss_dir,
            backend="faiss",
            expected_knowledge_db_sha256="a" * 64,
            expected_terms_db_revision="b" * 64,
        )
    assert degraded is None
    assert caplog.messages
    for msg in caplog.messages:
        assert str(faiss_dir) not in msg


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


class _StubNdArray:
    """Minimal pure-Python ndarray stub for testing OnnxInt8TextEncoder without numpy."""

    def __init__(self, data: object) -> None:
        self._data = data

    @property
    def shape(self) -> tuple[int, ...]:
        dims: list[int] = []
        cur = self._data
        while isinstance(cur, list):
            dims.append(len(cur))
            cur = cur[0] if cur else None
        return tuple(dims)

    @property
    def ndim(self) -> int:
        return len(self.shape)

    def tolist(self) -> Any:
        if isinstance(self._data, list):
            return [
                _StubNdArray(x).tolist() if isinstance(x, list) else x
                for x in self._data
            ]
        return self._data

    def _map(self, fn: object, other: object = None) -> _StubNdArray:
        def _apply(a: Any, b: Any = None) -> Any:
            if isinstance(a, list):
                if isinstance(b, list):
                    if len(b) == 1 and len(a) > 1:
                        return [_apply(ai, b[0]) for ai in a]
                    return [_apply(ai, bi) for ai, bi in zip(a, b)]
                return [_apply(ai, b) for ai in a]
            return fn(a) if b is None else fn(a, b)  # type: ignore[operator]

        rhs = other._data if isinstance(other, _StubNdArray) else other
        return _StubNdArray(_apply(self._data, rhs))

    def astype(self, dtype: object) -> _StubNdArray:
        cast = float if dtype is float else int
        return self._map(cast)

    def __gt__(self, other: object) -> _StubNdArray:
        return self._map(lambda a, b: a > b, other)

    def __sub__(self, other: object) -> _StubNdArray:
        return self._map(lambda a, b: a - b, other)

    def __rsub__(self, other: object) -> _StubNdArray:
        return self._map(lambda a, b: b - a, other)

    def __mul__(self, other: object) -> _StubNdArray:
        return self._map(lambda a, b: a * b, other)

    def __truediv__(self, other: object) -> _StubNdArray:
        return self._map(lambda a, b: a / b, other)

    def sum(self, axis: int = 1) -> _StubNdArray:
        assert axis == 1
        assert isinstance(self._data, list)
        out: list[Any] = []
        for batch_item in self._data:
            if batch_item and isinstance(batch_item[0], list):
                width = len(batch_item[0])
                out.append([sum(tok[d] for tok in batch_item) for d in range(width)])
            else:
                out.append(sum(batch_item))
        return _StubNdArray(out)

    def any(self, axis: int = 1) -> _StubNdArray:
        assert axis == 1
        assert isinstance(self._data, list)
        return _StubNdArray([any(row) for row in self._data])

    def __getitem__(self, key: object) -> _StubNdArray:
        assert isinstance(self._data, list)
        if isinstance(key, int):
            return _StubNdArray(self._data[key])
        if isinstance(key, tuple):
            if (
                len(key) == 2
                and key[0] == slice(None)
                and key[1] is None
                and self.ndim == 1
            ):
                return _StubNdArray([[x] for x in self._data])
            if (
                len(key) == 2
                and key[0] == slice(None)
                and isinstance(key[1], slice)
            ):
                s = key[1]
                return _StubNdArray([row[s] for row in self._data])
            if (
                len(key) == 3
                and key[0] == slice(None)
                and key[1] == slice(None)
                and key[2] is None
            ):
                return _StubNdArray([[[x] for x in row] for row in self._data])
            if (
                len(key) == 3
                and key[0] == slice(None)
                and isinstance(key[1], int)
                and key[2] == slice(None)
            ):
                pos = key[1]
                return _StubNdArray([row[pos] for row in self._data])
            if (
                len(key) == 3
                and isinstance(key[0], _StubNdArray)
                and isinstance(key[1], _StubNdArray)
                and key[2] == slice(None)
            ):
                return _StubNdArray(
                    [
                        self._data[b][t]
                        for b, t in zip(key[0].tolist(), key[1].tolist())
                    ]
                )
        raise NotImplementedError(f"unsupported stub slice: {key!r}")


class _StubNumpy:
    int64 = int
    float32 = float

    @staticmethod
    def asarray(data: object, dtype: object = None) -> _StubNdArray:
        arr = _StubNdArray(data)
        return arr.astype(dtype) if dtype is not None else arr

    @staticmethod
    def arange(n: int) -> _StubNdArray:
        return _StubNdArray(list(range(n)))

    @staticmethod
    def zeros(shape: tuple[int, ...], dtype: object = float) -> _StubNdArray:
        val = 0.0 if dtype is float else 0

        def _build(dims: tuple[int, ...]) -> Any:
            if len(dims) == 1:
                return [val] * dims[0]
            return [_build(dims[1:]) for _ in range(dims[0])]

        return _StubNdArray(_build(shape))

    @staticmethod
    def argmax(arr: _StubNdArray, axis: int = 1) -> _StubNdArray:
        assert axis == 1
        return _StubNdArray([row.index(max(row)) for row in arr._data])

    @staticmethod
    def maximum(arr: _StubNdArray, val: float) -> _StubNdArray:
        return arr._map(lambda x: x if x >= val else val)

    @staticmethod
    def cumsum(arr: _StubNdArray, axis: int = 1) -> _StubNdArray:
        assert axis == 1
        out: list[list[int]] = []
        for row in arr._data:
            running = 0
            acc: list[int] = []
            for v in row:
                running += v
                acc.append(running)
            out.append(acc)
        return _StubNdArray(out)

    @staticmethod
    def where(
        cond: _StubNdArray, x: _StubNdArray, y: float
    ) -> _StubNdArray:
        out: list[list[float]] = []
        for c_row, x_row in zip(cond._data, x._data):
            flag = c_row[0] if isinstance(c_row, list) else c_row
            out.append(list(x_row) if flag else [float(y)] * len(x_row))
        return _StubNdArray(out)


def test_onnx_encoder_pad_resolution_and_pooling_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import types
    import wuwaterm.retrieval_vector as rv

    model_file = tmp_path / "model.int8.onnx"
    tok_file = tmp_path / "tokenizer.json"
    model_file.write_bytes(b"stub-onnx")
    tok_file.write_text("{}", encoding="utf-8")

    class _FakeEncoding:
        def __init__(self, ids: list[int], mask: list[int], types_list: list[int]) -> None:
            self.ids = ids
            self.attention_mask = mask
            self.type_ids = types_list

    recorded: dict[str, Any] = {}

    class _FakeTokenizer:
        def __init__(
            self,
            *,
            vocab: dict[str, int],
            padding: dict[str, Any] | None = None,
            batch_rows: list[tuple[list[int], list[int], list[int]]] | None = None,
        ) -> None:
            self._vocab = vocab
            self._id_to_tok = {v: k for k, v in vocab.items()}
            self.padding = padding
            self._batch_rows = batch_rows or []
            self.pad_calls: list[dict[str, Any]] = []

        def token_to_id(self, token: str) -> int | None:
            return self._vocab.get(token)

        def id_to_token(self, idx: int) -> str | None:
            return self._id_to_tok.get(idx)

        def enable_truncation(self, *, max_length: int) -> None:
            recorded["max_length"] = max_length

        def enable_padding(self, **kwargs: Any) -> None:
            self.pad_calls.append(kwargs)
            self.padding = dict(kwargs)

        def encode_batch(self, texts: list[str]) -> list[_FakeEncoding]:
            recorded["encoded_texts"] = list(texts)
            return [_FakeEncoding(ids, mask, tids) for ids, mask, tids in self._batch_rows]

    # Hidden states: batch=2, seq=4, native_dim=4; pad positions hold large junk
    # values (999.0) so any pooling mistake immediately fails assertions.
    hidden_tensor = [
        [
            [1.0, 2.0, 3.0, 9.0],   # tok 0 (valid)
            [3.0, 6.0, 9.0, 9.0],   # tok 1 (valid; last valid for row 0)
            [999.0, 999.0, 999.0, 999.0],  # pad
            [999.0, 999.0, 999.0, 999.0],  # pad
        ],
        [
            [2.0, 0.0, 0.0, 5.0],   # tok 0 (valid)
            [0.0, 4.0, 0.0, 5.0],   # tok 1 (valid)
            [1.0, 2.0, 2.0, 5.0],   # tok 2 (valid; last valid for row 1)
            [999.0, 999.0, 999.0, 999.0],  # pad
        ],
    ]
    batch_rows = [
        ([10, 11, 151643, 151643], [1, 1, 0, 0], [0, 0, 0, 0]),
        ([20, 21, 22, 151643], [1, 1, 1, 0], [0, 0, 0, 0]),
    ]
    active_tok: list[_FakeTokenizer] = []

    class _FakeSession:
        def __init__(self, path: str, providers: list[str]) -> None:
            recorded["session_path"] = path
            recorded["providers"] = providers

        def get_inputs(self) -> list[Any]:
            return [
                types.SimpleNamespace(name="input_ids"),
                types.SimpleNamespace(name="attention_mask"),
                types.SimpleNamespace(name="position_ids"),
            ]

        def get_outputs(self) -> list[Any]:
            return [types.SimpleNamespace(shape=["batch", "seq", 4])]

        def run(self, _out: object, feeds: dict[str, Any]) -> list[Any]:
            recorded["feeds"] = {k: v.tolist() for k, v in feeds.items()}
            return [_StubNdArray(hidden_tensor)]

    fake_ort = types.SimpleNamespace(InferenceSession=_FakeSession)
    fake_tok_mod = types.SimpleNamespace(
        Tokenizer=types.SimpleNamespace(from_file=lambda _p: active_tok[0])
    )
    real_import = rv.importlib.import_module

    def patched_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "onnxruntime":
            return fake_ort
        if name == "tokenizers":
            return fake_tok_mod
        if name == "numpy":
            return _StubNumpy
        return real_import(name)

    monkeypatch.setattr(rv.importlib, "import_module", patched_import)

    # 1) Auto-resolve pad_id=1 / <pad> from tokenizer.padding (XLM-R / BGE-M3 style)
    xlmr_tok = _FakeTokenizer(
        vocab={"<pad>": 1, "<s>": 0},
        padding={"pad_id": 1, "pad_token": "<pad>", "direction": "right"},
        batch_rows=batch_rows,
    )
    active_tok[:] = [xlmr_tok]
    enc_cls = OnnxInt8TextEncoder(
        model_path=model_file, dim=3, pooling="cls", query_prefix="q: "
    )
    assert enc_cls.pad_id == 1
    assert enc_cls.pad_token == "<pad>"
    assert xlmr_tok.pad_calls[-1] == {
        "pad_id": 1,
        "pad_token": "<pad>",
        "direction": "right",
    }
    cls_vecs = enc_cls.encode_queries(["a", "b"])
    assert recorded["encoded_texts"] == ["q: a", "q: b"]
    # row 0 tok 0[:3] = [1, 2, 3], row 1 tok 0[:3] = [2, 0, 0] -> [1, 0, 0]
    norm0 = math.sqrt(1.0 + 4.0 + 9.0)
    assert cls_vecs[0] == pytest.approx([1.0 / norm0, 2.0 / norm0, 3.0 / norm0])
    assert cls_vecs[1] == pytest.approx([1.0, 0.0, 0.0])

    # 2) Auto-resolve Qwen3 <|endoftext|> = 151643 when padding is None + last_token pooling
    qwen_tok = _FakeTokenizer(
        vocab={"<|endoftext|>": 151643},
        padding=None,
        batch_rows=batch_rows,
    )
    active_tok[:] = [qwen_tok]
    enc_last = OnnxInt8TextEncoder(
        model_path=model_file, dim=3, pooling="last_token"
    )
    assert enc_last.pad_id == 151643
    assert enc_last.pad_token == "<|endoftext|>"
    assert qwen_tok.pad_calls[-1] == {"pad_id": 151643, "pad_token": "<|endoftext|>"}
    last_vecs = enc_last.encode_passages(["a", "b"])
    # Check position_ids were derived from attention_mask
    assert recorded["feeds"]["position_ids"] == [[0, 1, 1, 1], [0, 1, 2, 2]]
    # row 0 last valid (idx 1)[:3] = [3, 6, 9]; row 1 last valid (idx 2)[:3] = [1, 2, 2]
    norm_r0 = math.sqrt(9.0 + 36.0 + 81.0)
    assert last_vecs[0] == pytest.approx([3.0 / norm_r0, 6.0 / norm_r0, 9.0 / norm_r0])
    assert last_vecs[1] == pytest.approx([1.0 / 3.0, 2.0 / 3.0, 2.0 / 3.0])

    # 3) Mean pooling ignores pad positions and preserves MRL truncation + L2 norm
    enc_mean = OnnxInt8TextEncoder(
        model_path=model_file, dim=3, pooling="mean", pad_token="<|endoftext|>"
    )
    assert enc_mean.pad_id == 151643
    mean_vecs = enc_mean.encode_passages(["a", "b"])
    # row 0 mean over idx 0,1 = [2, 4, 6]; row 1 mean over idx 0,1,2 = [1, 2, 2/3]
    m0 = math.sqrt(4.0 + 16.0 + 36.0)
    m1 = math.sqrt(1.0 + 4.0 + (2.0 / 3.0) ** 2)
    assert mean_vecs[0] == pytest.approx([2.0 / m0, 4.0 / m0, 6.0 / m0])
    assert mean_vecs[1] == pytest.approx([1.0 / m1, 2.0 / m1, (2.0 / 3.0) / m1])

    # 4) Left-padded sequence + all-masked row with last_token pooling
    left_tok = _FakeTokenizer(
        vocab={"<|pad|>": 77},
        padding=None,
        batch_rows=[
            ([77, 77, 10, 11], [0, 0, 1, 1], [0, 0, 0, 0]),
            ([77, 77, 77, 77], [0, 0, 0, 0], [0, 0, 0, 0]),
        ],
    )
    active_tok[:] = [left_tok]
    hidden_left = [
        [
            [999.0, 999.0, 999.0, 9.0],
            [999.0, 999.0, 999.0, 9.0],
            [1.0, 0.0, 0.0, 9.0],
            [0.0, 3.0, 4.0, 9.0],  # last valid for left-padded row 0 -> [0, 3, 4]
        ],
        [
            [999.0, 999.0, 999.0, 9.0],
            [999.0, 999.0, 999.0, 9.0],
            [999.0, 999.0, 999.0, 9.0],
            [999.0, 999.0, 999.0, 9.0],  # all masked -> zero vector
        ],
    ]
    left_sess = types.SimpleNamespace(
        get_inputs=lambda: [
            types.SimpleNamespace(name="input_ids"),
            types.SimpleNamespace(name="attention_mask"),
        ],
        get_outputs=lambda: [types.SimpleNamespace(shape=[2, 4, 4])],
        run=lambda _o, _f: [_StubNdArray(hidden_left)],
    )
    monkeypatch.setattr(fake_ort, "InferenceSession", lambda _p, providers=None: left_sess)
    enc_left = OnnxInt8TextEncoder(model_path=model_file, dim=3, pooling="last_token")
    assert enc_left.pad_id == 77
    assert enc_left.pad_token == "<|pad|>"
    left_vecs = enc_left.encode_queries(["x", "y"])
    assert left_vecs[0] == pytest.approx([0.0, 0.6, 0.8])
    assert left_vecs[1] == [0.0, 0.0, 0.0]

    # 5) Explicit pad_id override + fallback to (0, "[PAD]") when unknown
    fallback_tok = _FakeTokenizer(vocab={}, padding=None, batch_rows=batch_rows)
    active_tok[:] = [fallback_tok]
    enc_fb = OnnxInt8TextEncoder(model_path=model_file, dim=3)
    assert enc_fb.pad_id == 0
    assert enc_fb.pad_token == "[PAD]"

    xlmr_no_pad = _FakeTokenizer(vocab={"<pad>": 1}, padding=None)
    active_tok[:] = [xlmr_no_pad]
    enc_explicit = OnnxInt8TextEncoder(model_path=model_file, dim=3, pad_id=1)
    assert enc_explicit.pad_id == 1
    assert enc_explicit.pad_token == "<pad>"

    with pytest.raises(ValueError, match="pooling"):
        OnnxInt8TextEncoder(model_path=model_file, pooling="max")  # type: ignore[arg-type]


def test_build_knowledge_vectors_cli_forwards_onnx_pooling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _terms_db, kb_db = _build_synthetic_fixture(tmp_path / "fx")
    captured: dict[str, Any] = {}

    class _StubOnnxEncoder:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.model_id = str(kwargs.get("model_id", "stub"))
            self.dim = int(kwargs.get("dim") or 8)

        def encode_queries(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]

        def encode_passages(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]

    monkeypatch.setattr(build_knowledge_vectors, "OnnxInt8TextEncoder", _StubOnnxEncoder)
    onnx_model = tmp_path / "model.int8.onnx"
    onnx_model.write_bytes(b"stub")
    rc = build_knowledge_vectors.main(
        [
            "--knowledge-db",
            str(kb_db),
            "--out-dir",
            str(tmp_path / "vec_out"),
            "--encoder",
            "onnx",
            "--onnx-model",
            str(onnx_model),
            "--onnx-pooling",
            "last_token",
            "--dim",
            "8",
        ]
    )
    assert rc == 0
    assert captured["pooling"] == "last_token"
    assert captured["dim"] == 8
