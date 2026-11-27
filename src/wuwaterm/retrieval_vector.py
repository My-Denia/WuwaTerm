"""Optional pluggable local vector retrieval abstraction and fail-closed backends.

This module sits in ``DOMAIN_CORE`` (outside ``PURE_MATCHING``) and keeps all
heavy ML dependencies (``onnxruntime``, ``tokenizers``, ``sqlite_vec``,
``faiss``, ``numpy``) strictly optional via lazy imports. In a standard runtime
environment with zero extra ML packages installed:

- ``Int8MemmapVectorBackend`` runs using standard-library ``mmap`` + ``struct``
  (accelerating automatically with ``numpy.memmap`` when ``numpy`` is present).
- ``SqliteVecBackend`` and ``FaissVectorBackend`` raise
  ``VectorBackendUnavailableError`` when their optional C extensions or SQLite
  extension loading are absent.
- ``open_vector_backend`` enforces version-bound metadata verification
  (``vec_schema_version == "1"``, ``knowledge_db_sha256``,
  ``terms_db_revision``, ``model_id``, ``dim``, ``quantization``) and degrades
  fail-closed to ``None`` (pure Lexical RAG) with a sanitized warning.
- ``hybrid_fuse_candidates`` performs deterministic Reciprocal Rank Fusion
  (RRF) and passes every candidate through ``domain_knowledge`` hard isolation
  so ``lock_eligible`` is always ``False`` and ordinary words / locked spans are
  never injected.
"""

from __future__ import annotations

import hashlib
import heapq
import importlib
import json
import logging
import math
import mmap
import os
import re
import sqlite3
import struct
import tempfile
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from .domain_knowledge import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MAX_SCALARS,
    DomainKnowledgeCandidate,
    TRUST_TIERS,
    TrustTier,
    prepare_prompt_domain_knowledge_candidates,
    select_domain_knowledge_candidates,
)
from .normalize import has_cjk, normalize_text


LOGGER = logging.getLogger(__name__)

VEC_SCHEMA_VERSION = "1"
DEFAULT_VECTOR_DIM = 512
DEFAULT_RRF_K = 60
SUPPORTED_QUANTIZATIONS: frozenset[str] = frozenset({"int8", "fp32"})
QuantizationMode = Literal["int8", "fp32"]

_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'_-]*")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_SHA256_CHUNK_BYTES = 1 << 20


class VectorRetrievalError(RuntimeError):
    """Base exception for vector index loading, validation, or encoding."""


class VectorBackendUnavailableError(VectorRetrievalError):
    """Raised when an optional vector backend or encoder dependency is absent."""


class VectorIndexMetadataError(VectorRetrievalError):
    """Raised fail-closed when vector index metadata is missing or mismatched."""


@runtime_checkable
class VectorEncoder(Protocol):
    """Protocol for query/passage embedding encoders."""

    @property
    def model_id(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def encode_queries(self, texts: Sequence[str]) -> list[list[float]]: ...

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class VectorHit:
    """Single nearest-neighbor hit returned by a vector backend."""

    entity_id: str
    score: float
    source_surface: str = ""
    rank: int = 0


@dataclass(frozen=True)
class VectorIndexMetadata:
    """Strong version-binding metadata stored alongside every vector index."""

    vec_schema_version: str
    knowledge_db_sha256: str
    terms_db_revision: str
    model_id: str
    dim: int
    quantization: QuantizationMode
    record_count: int = 0
    backend: str = "memmap"

    def to_dict(self) -> dict[str, object]:
        return {
            "vec_schema_version": self.vec_schema_version,
            "knowledge_db_sha256": self.knowledge_db_sha256,
            "terms_db_revision": self.terms_db_revision,
            "model_id": self.model_id,
            "dim": int(self.dim),
            "quantization": self.quantization,
            "record_count": int(self.record_count),
            "backend": self.backend,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> VectorIndexMetadata:
        schema = raw.get("vec_schema_version")
        kb_sha = raw.get("knowledge_db_sha256")
        terms_rev = raw.get("terms_db_revision")
        model_id = raw.get("model_id")
        dim_raw = raw.get("dim")
        quant_raw = raw.get("quantization")
        count_raw = raw.get("record_count", 0)
        backend_raw = raw.get("backend", "memmap")

        if not isinstance(schema, str) or not schema:
            raise VectorIndexMetadataError("missing vec_schema_version")
        if not isinstance(kb_sha, str) or not kb_sha.strip():
            raise VectorIndexMetadataError("missing knowledge_db_sha256")
        if not isinstance(terms_rev, str) or not terms_rev.strip():
            raise VectorIndexMetadataError("missing terms_db_revision")
        if not isinstance(model_id, str) or not model_id.strip():
            raise VectorIndexMetadataError("missing model_id")
        if isinstance(dim_raw, bool) or not isinstance(dim_raw, int) or dim_raw <= 0:
            raise VectorIndexMetadataError("invalid vector dim")
        if quant_raw not in SUPPORTED_QUANTIZATIONS:
            raise VectorIndexMetadataError(f"unsupported quantization: {quant_raw!r}")
        if (
            isinstance(count_raw, bool)
            or not isinstance(count_raw, int)
            or count_raw < 0
        ):
            raise VectorIndexMetadataError("invalid record_count")
        if not isinstance(backend_raw, str) or not backend_raw.strip():
            raise VectorIndexMetadataError("invalid backend")

        quant: QuantizationMode = "int8" if quant_raw == "int8" else "fp32"
        return cls(
            vec_schema_version=schema,
            knowledge_db_sha256=kb_sha.strip(),
            terms_db_revision=terms_rev.strip(),
            model_id=model_id.strip(),
            dim=dim_raw,
            quantization=quant,
            record_count=count_raw,
            backend=backend_raw.strip(),
        )

    def validate(
        self,
        *,
        expected_knowledge_db_sha256: str | None = None,
        expected_terms_db_revision: str | None = None,
        expected_model_id: str | None = None,
        expected_dim: int | None = None,
        expected_quantization: str | None = None,
        expected_backend: str | None = None,
    ) -> None:
        if self.vec_schema_version != VEC_SCHEMA_VERSION:
            raise VectorIndexMetadataError(
                f"vec_schema_version mismatch: expected {VEC_SCHEMA_VERSION!r}, "
                f"got {self.vec_schema_version!r}"
            )
        if not self.knowledge_db_sha256 or not self.terms_db_revision:
            raise VectorIndexMetadataError("incomplete provenance binding in vector metadata")
        if self.dim <= 0:
            raise VectorIndexMetadataError("vector dim must be positive")
        if self.quantization not in SUPPORTED_QUANTIZATIONS:
            raise VectorIndexMetadataError(
                f"unsupported quantization {self.quantization!r}"
            )
        if (
            expected_knowledge_db_sha256 is not None
            and self.knowledge_db_sha256 != expected_knowledge_db_sha256
        ):
            raise VectorIndexMetadataError("knowledge_db_sha256 mismatch")
        if (
            expected_terms_db_revision is not None
            and self.terms_db_revision != expected_terms_db_revision
        ):
            raise VectorIndexMetadataError("terms_db_revision mismatch")
        if expected_model_id is not None and self.model_id != expected_model_id:
            raise VectorIndexMetadataError("model_id mismatch")
        if expected_dim is not None and self.dim != expected_dim:
            raise VectorIndexMetadataError("dim mismatch")
        if (
            expected_quantization is not None
            and self.quantization != expected_quantization
        ):
            raise VectorIndexMetadataError("quantization mismatch")
        if expected_backend is not None and self.backend != expected_backend:
            raise VectorIndexMetadataError("backend mismatch")


@runtime_checkable
class VectorIndexBackend(Protocol):
    """Protocol for read-only nearest-neighbor vector search backends."""

    @property
    def metadata(self) -> VectorIndexMetadata: ...

    def search(
        self, query_vector: Sequence[float], *, top_k: int = 16
    ) -> list[VectorHit]: ...

    def close(self) -> None: ...


def compute_file_sha256(path: str | Path) -> str:
    """Compute hex SHA-256 of ``path`` in bounded chunks."""
    file_path = Path(path)
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while chunk := handle.read(_SHA256_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _l2_normalize(vector: Sequence[float]) -> list[float]:
    values = [float(x) for x in vector]
    norm_sq = math.fsum(x * x for x in values)
    if norm_sq <= 0.0:
        return [0.0] * len(values)
    inv_norm = 1.0 / math.sqrt(norm_sq)
    return [x * inv_norm for x in values]


def _quantize_int8(vector: Sequence[float]) -> tuple[float, list[int]]:
    normed = _l2_normalize(vector)
    max_abs = max((abs(x) for x in normed), default=0.0)
    if max_abs <= 0.0:
        return 0.0, [0] * len(normed)
    scale = max_abs / 127.0
    quantized = [max(-127, min(127, int(round(x / scale)))) for x in normed]
    return scale, quantized


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            view = memoryview(payload)
            while view:
                written = handle.write(view)
                view = view[written:]
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_write_text(target: Path, text: str) -> None:
    _atomic_write_bytes(target, text.encode("utf-8"))


def _optional_numpy() -> Any | None:
    try:
        return importlib.import_module("numpy")
    except ImportError:
        return None


def _resolve_memmap_paths(index_path: str | Path) -> tuple[Path, Path]:
    path = Path(index_path)
    if path.name.endswith(".meta.json"):
        stem = path.name[: -len(".meta.json")]
        return path.with_name(f"{stem}.bin"), path
    if path.suffix == ".bin":
        return path, path.with_suffix(".meta.json")
    return path / "knowledge-vec.bin", path / "knowledge-vec.meta.json"


def _row_stride_bytes(dim: int, quantization: QuantizationMode) -> int:
    if quantization == "int8":
        return 4 + dim
    return 4 * dim


class Int8MemmapVectorBackend:
    """Compact zero-dependency INT8/FP32 memmap vector backend.

    Uses ``numpy.memmap`` when ``numpy`` is available and falls back to
    standard-library ``mmap`` + ``struct`` when ``numpy`` is absent.
    """

    def __init__(
        self,
        index_path: str | Path,
        *,
        expected_knowledge_db_sha256: str | None = None,
        expected_terms_db_revision: str | None = None,
        expected_model_id: str | None = None,
        expected_dim: int | None = None,
        expected_quantization: str | None = None,
    ) -> None:
        self._bin_path, self._meta_path = _resolve_memmap_paths(index_path)
        if not self._meta_path.is_file():
            raise VectorIndexMetadataError("vector metadata file is missing")
        if not self._bin_path.is_file():
            raise VectorIndexMetadataError("vector binary file is missing")

        try:
            raw_meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VectorIndexMetadataError("unreadable vector metadata JSON") from exc
        if not isinstance(raw_meta, dict):
            raise VectorIndexMetadataError("vector metadata must be a JSON object")

        meta = VectorIndexMetadata.from_dict(raw_meta)
        meta.validate(
            expected_knowledge_db_sha256=expected_knowledge_db_sha256,
            expected_terms_db_revision=expected_terms_db_revision,
            expected_model_id=expected_model_id,
            expected_dim=expected_dim,
            expected_quantization=expected_quantization,
            expected_backend="memmap",
        )

        entity_ids_raw = raw_meta.get("entity_ids")
        surfaces_raw = raw_meta.get("surfaces")
        if not isinstance(entity_ids_raw, list) or not all(
            isinstance(item, str) and item for item in entity_ids_raw
        ):
            raise VectorIndexMetadataError("invalid entity_ids in vector metadata")
        if len(entity_ids_raw) != meta.record_count:
            raise VectorIndexMetadataError("entity_ids count mismatch")

        if surfaces_raw is None:
            surfaces = ("",) * meta.record_count
        else:
            if not isinstance(surfaces_raw, list) or not all(
                isinstance(item, str) for item in surfaces_raw
            ):
                raise VectorIndexMetadataError("invalid surfaces in vector metadata")
            if len(surfaces_raw) != meta.record_count:
                raise VectorIndexMetadataError("surfaces count mismatch")
            surfaces = tuple(surfaces_raw)

        stride = _row_stride_bytes(meta.dim, meta.quantization)
        expected_size = meta.record_count * stride
        actual_size = self._bin_path.stat().st_size
        if actual_size != expected_size:
            raise VectorIndexMetadataError(
                f"vector binary size mismatch: expected {expected_size}, got {actual_size}"
            )

        self._metadata = meta
        self._entity_ids: tuple[str, ...] = tuple(entity_ids_raw)
        self._surfaces: tuple[str, ...] = surfaces
        self._stride = stride
        self._file: Any | None = None
        self._mmap: mmap.mmap | None = None
        self._np_memmap: Any | None = None
        self._row_struct = struct.Struct(
            f"<f{meta.dim}b" if meta.quantization == "int8" else f"<{meta.dim}f"
        )

        if meta.record_count > 0:
            np_mod = _optional_numpy()
            if np_mod is not None:
                dtype = (
                    np_mod.dtype([("scale", "<f4"), ("vec", "<i1", (meta.dim,))])
                    if meta.quantization == "int8"
                    else np_mod.dtype(("<f4", (meta.dim,)))
                )
                self._np_memmap = np_mod.memmap(
                    self._bin_path,
                    dtype=dtype,
                    mode="r",
                    shape=(meta.record_count,),
                )
            else:
                handle = self._bin_path.open("rb")
                try:
                    self._mmap = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
                    self._file = handle
                except Exception:
                    handle.close()
                    raise

    @classmethod
    def open(
        cls,
        index_path: str | Path,
        *,
        expected_knowledge_db_sha256: str | None = None,
        expected_terms_db_revision: str | None = None,
        expected_model_id: str | None = None,
        expected_dim: int | None = None,
        expected_quantization: str | None = None,
    ) -> Int8MemmapVectorBackend:
        return cls(
            index_path,
            expected_knowledge_db_sha256=expected_knowledge_db_sha256,
            expected_terms_db_revision=expected_terms_db_revision,
            expected_model_id=expected_model_id,
            expected_dim=expected_dim,
            expected_quantization=expected_quantization,
        )

    @classmethod
    def build(
        cls,
        output_path: str | Path,
        *,
        vectors: Sequence[Sequence[float]],
        entity_ids: Sequence[str],
        surfaces: Sequence[str] | None = None,
        knowledge_db_sha256: str,
        terms_db_revision: str,
        model_id: str,
        dim: int,
        quantization: QuantizationMode = "int8",
    ) -> VectorIndexMetadata:
        if len(vectors) != len(entity_ids):
            raise ValueError("vectors and entity_ids lengths must match")
        if surfaces is not None and len(surfaces) != len(entity_ids):
            raise ValueError("surfaces and entity_ids lengths must match")
        if dim <= 0:
            raise ValueError("dim must be positive")
        if quantization not in SUPPORTED_QUANTIZATIONS:
            raise ValueError(f"unsupported quantization: {quantization!r}")

        bin_path, meta_path = _resolve_memmap_paths(output_path)
        row_struct = struct.Struct(
            f"<f{dim}b" if quantization == "int8" else f"<{dim}f"
        )
        stride = _row_stride_bytes(dim, quantization)
        buffer = bytearray(len(vectors) * stride)

        for idx, vec in enumerate(vectors):
            if len(vec) != dim:
                raise ValueError(
                    f"vector at index {idx} has dim {len(vec)}, expected {dim}"
                )
            offset = idx * stride
            if quantization == "int8":
                scale, q_vals = _quantize_int8(vec)
                row_struct.pack_into(buffer, offset, scale, *q_vals)
            else:
                normed = _l2_normalize(vec)
                row_struct.pack_into(buffer, offset, *normed)

        meta = VectorIndexMetadata(
            vec_schema_version=VEC_SCHEMA_VERSION,
            knowledge_db_sha256=knowledge_db_sha256,
            terms_db_revision=terms_db_revision,
            model_id=model_id,
            dim=dim,
            quantization=quantization,
            record_count=len(entity_ids),
            backend="memmap",
        )
        meta_payload = meta.to_dict()
        meta_payload["entity_ids"] = list(entity_ids)
        meta_payload["surfaces"] = (
            list(surfaces) if surfaces is not None else [""] * len(entity_ids)
        )

        # Write binary matrix first, then publish metadata atomically so readers
        # never observe metadata pointing to a partial binary file.
        _atomic_write_bytes(bin_path, bytes(buffer))
        _atomic_write_text(
            meta_path,
            json.dumps(meta_payload, ensure_ascii=False, indent=2) + "\n",
        )
        return meta

    @property
    def metadata(self) -> VectorIndexMetadata:
        return self._metadata

    @property
    def bin_path(self) -> Path:
        return self._bin_path

    @property
    def meta_path(self) -> Path:
        return self._meta_path

    def close(self) -> None:
        if self._mmap is not None:
            try:
                self._mmap.close()
            finally:
                self._mmap = None
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None
        if self._np_memmap is not None:
            mm = getattr(self._np_memmap, "_mmap", None)
            self._np_memmap = None
            if mm is not None:
                try:
                    mm.close()
                except Exception:
                    pass

    def __enter__(self) -> Int8MemmapVectorBackend:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def search(
        self, query_vector: Sequence[float], *, top_k: int = 16
    ) -> list[VectorHit]:
        dim = self._metadata.dim
        if len(query_vector) != dim:
            raise ValueError(
                f"query_vector length {len(query_vector)} does not match index dim {dim}"
            )
        if top_k <= 0 or self._metadata.record_count == 0:
            return []

        q_norm = _l2_normalize(query_vector)
        if not any(x != 0.0 for x in q_norm):
            return []

        limit = min(int(top_k), self._metadata.record_count)
        scores_list: list[float]

        if self._np_memmap is not None:
            np_mod = _optional_numpy()
            assert np_mod is not None
            q_arr = np_mod.asarray(q_norm, dtype=np_mod.float32)
            if self._metadata.quantization == "int8":
                raw_dots = self._np_memmap["vec"].astype(np_mod.float32) @ q_arr
                scores_arr = raw_dots * self._np_memmap["scale"]
            else:
                scores_arr = self._np_memmap @ q_arr
            scores_list = [float(val) for val in scores_arr.tolist()]
        else:
            mm = self._mmap
            if mm is None:
                return []
            scores_list = []
            unpack_from = self._row_struct.unpack_from
            stride = self._stride
            q_tuple = tuple(q_norm)
            is_int8 = self._metadata.quantization == "int8"
            for idx in range(self._metadata.record_count):
                unpacked = unpack_from(mm, idx * stride)
                if is_int8:
                    scale = float(unpacked[0])
                    if scale == 0.0:
                        scores_list.append(0.0)
                    else:
                        dot = math.fsum(
                            w * q_val for w, q_val in zip(unpacked[1:], q_tuple)
                        )
                        scores_list.append(scale * dot)
                else:
                    scores_list.append(
                        math.fsum(w * q_val for w, q_val in zip(unpacked, q_tuple))
                    )

        ranked_indices = heapq.nsmallest(
            limit,
            range(self._metadata.record_count),
            key=lambda idx: (-scores_list[idx], self._entity_ids[idx], idx),
        )
        return [
            VectorHit(
                entity_id=self._entity_ids[idx],
                score=float(scores_list[idx]),
                source_surface=self._surfaces[idx],
                rank=rank,
            )
            for rank, idx in enumerate(ranked_indices, start=1)
        ]


def _load_sqlite_vec_module() -> Any:
    try:
        return importlib.import_module("sqlite_vec")
    except ImportError as exc:
        raise VectorBackendUnavailableError(
            "sqlite_vec is not installed; install wuwaterm[rag-vec] to use SqliteVecBackend"
        ) from exc


def _connect_sqlite_vec(db_path: Path, *, read_only: bool) -> sqlite3.Connection:
    sqlite_vec = _load_sqlite_vec_module()
    if read_only:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_path)

    enable_ext = getattr(conn, "enable_load_extension", None)
    if enable_ext is None:
        conn.close()
        raise VectorBackendUnavailableError(
            "sqlite3.Connection.enable_load_extension is disabled in this Python build"
        )
    try:
        enable_ext(True)
        sqlite_vec.load(conn)
    except (AttributeError, sqlite3.Error) as exc:
        conn.close()
        raise VectorBackendUnavailableError(
            "failed to load sqlite_vec SQLite extension"
        ) from exc
    finally:
        try:
            enable_ext(False)
        except Exception:
            pass

    if read_only:
        conn.execute("PRAGMA query_only = ON;")
    return conn


def _resolve_sqlite_vec_path(index_path: str | Path) -> Path:
    path = Path(index_path)
    if path.suffix in (".sqlite3", ".sqlite", ".db"):
        return path
    return path / "knowledge-vec.sqlite3"


class SqliteVecBackend:
    """Optional read-only ``sqlite-vec`` backend reading ``knowledge-vec.sqlite3``."""

    def __init__(
        self,
        index_path: str | Path,
        *,
        expected_knowledge_db_sha256: str | None = None,
        expected_terms_db_revision: str | None = None,
        expected_model_id: str | None = None,
        expected_dim: int | None = None,
        expected_quantization: str | None = None,
    ) -> None:
        self._sqlite_vec = _load_sqlite_vec_module()
        self._db_path = _resolve_sqlite_vec_path(index_path)
        if not self._db_path.is_file():
            raise VectorIndexMetadataError("sqlite-vec index file is missing")

        self._conn = _connect_sqlite_vec(self._db_path, read_only=True)
        try:
            rows = self._conn.execute(
                "SELECT key, value FROM vec_metadata ORDER BY key"
            ).fetchall()
            raw_meta: dict[str, object] = {str(k): str(v) for k, v in rows}
            for int_key in ("dim", "record_count"):
                if int_key in raw_meta:
                    raw_meta[int_key] = int(str(raw_meta[int_key]))
            meta = VectorIndexMetadata.from_dict(raw_meta)
            meta.validate(
                expected_knowledge_db_sha256=expected_knowledge_db_sha256,
                expected_terms_db_revision=expected_terms_db_revision,
                expected_model_id=expected_model_id,
                expected_dim=expected_dim,
                expected_quantization=expected_quantization,
                expected_backend="sqlite-vec",
            )
            self._metadata = meta
        except Exception:
            self._conn.close()
            raise

    @classmethod
    def open(
        cls,
        index_path: str | Path,
        *,
        expected_knowledge_db_sha256: str | None = None,
        expected_terms_db_revision: str | None = None,
        expected_model_id: str | None = None,
        expected_dim: int | None = None,
        expected_quantization: str | None = None,
    ) -> SqliteVecBackend:
        return cls(
            index_path,
            expected_knowledge_db_sha256=expected_knowledge_db_sha256,
            expected_terms_db_revision=expected_terms_db_revision,
            expected_model_id=expected_model_id,
            expected_dim=expected_dim,
            expected_quantization=expected_quantization,
        )

    @classmethod
    def build(
        cls,
        output_path: str | Path,
        *,
        vectors: Sequence[Sequence[float]],
        entity_ids: Sequence[str],
        surfaces: Sequence[str] | None = None,
        knowledge_db_sha256: str,
        terms_db_revision: str,
        model_id: str,
        dim: int,
        quantization: QuantizationMode = "int8",
    ) -> VectorIndexMetadata:
        if len(vectors) != len(entity_ids):
            raise ValueError("vectors and entity_ids lengths must match")
        if surfaces is not None and len(surfaces) != len(entity_ids):
            raise ValueError("surfaces and entity_ids lengths must match")
        if dim <= 0:
            raise ValueError("dim must be positive")
        if quantization not in SUPPORTED_QUANTIZATIONS:
            raise ValueError(f"unsupported quantization: {quantization!r}")

        sqlite_vec = _load_sqlite_vec_module()
        db_path = _resolve_sqlite_vec_path(output_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{db_path.name}.", suffix=".tmp", dir=str(db_path.parent)
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        meta = VectorIndexMetadata(
            vec_schema_version=VEC_SCHEMA_VERSION,
            knowledge_db_sha256=knowledge_db_sha256,
            terms_db_revision=terms_db_revision,
            model_id=model_id,
            dim=dim,
            quantization=quantization,
            record_count=len(entity_ids),
            backend="sqlite-vec",
        )
        try:
            with closing(_connect_sqlite_vec(tmp_path, read_only=False)) as conn:
                conn.execute(
                    "CREATE TABLE vec_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE vec_entities ("
                    "rowid INTEGER PRIMARY KEY, "
                    "entity_id TEXT NOT NULL, "
                    "source_surface TEXT NOT NULL DEFAULT ''"
                    ")"
                )
                conn.execute(
                    f"CREATE VIRTUAL TABLE vec_items USING vec0(embedding float[{dim}])"
                )
                meta_items = [
                    (key, str(val)) for key, val in meta.to_dict().items()
                ]
                conn.executemany(
                    "INSERT INTO vec_metadata(key, value) VALUES (?, ?)",
                    meta_items,
                )
                resolved_surfaces = (
                    surfaces if surfaces is not None else [""] * len(entity_ids)
                )
                for row_id, (entity_id, surface, vec) in enumerate(
                    zip(entity_ids, resolved_surfaces, vectors), start=1
                ):
                    if len(vec) != dim:
                        raise ValueError(
                            f"vector at index {row_id - 1} has dim {len(vec)}, expected {dim}"
                        )
                    normed = _l2_normalize(vec)
                    conn.execute(
                        "INSERT INTO vec_entities(rowid, entity_id, source_surface) "
                        "VALUES (?, ?, ?)",
                        (row_id, entity_id, surface),
                    )
                    conn.execute(
                        "INSERT INTO vec_items(rowid, embedding) VALUES (?, ?)",
                        (row_id, sqlite_vec.serialize_float32(normed)),
                    )
                conn.commit()
            os.replace(tmp_path, db_path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return meta

    @property
    def metadata(self) -> VectorIndexMetadata:
        return self._metadata

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteVecBackend:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def search(
        self, query_vector: Sequence[float], *, top_k: int = 16
    ) -> list[VectorHit]:
        if len(query_vector) != self._metadata.dim:
            raise ValueError(
                f"query_vector length {len(query_vector)} does not match index dim "
                f"{self._metadata.dim}"
            )
        if top_k <= 0 or self._metadata.record_count == 0:
            return []

        q_norm = _l2_normalize(query_vector)
        if not any(x != 0.0 for x in q_norm):
            return []

        rows = self._conn.execute(
            """
            SELECT e.entity_id, e.source_surface, v.distance
            FROM vec_items v
            JOIN vec_entities e ON e.rowid = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance ASC, e.entity_id ASC
            """,
            (self._sqlite_vec.serialize_float32(q_norm), min(int(top_k), self._metadata.record_count)),
        ).fetchall()
        # vec0 default L2 distance on unit-normalized vectors: cos = 1 - d^2 / 2
        hits: list[VectorHit] = []
        for rank, (entity_id, surface, dist) in enumerate(rows, start=1):
            d = float(dist)
            cos_score = max(-1.0, min(1.0, 1.0 - 0.5 * d * d))
            hits.append(
                VectorHit(
                    entity_id=str(entity_id),
                    score=cos_score,
                    source_surface=str(surface),
                    rank=rank,
                )
            )
        return hits


def _load_faiss_modules() -> tuple[Any, Any]:
    try:
        faiss_mod = importlib.import_module("faiss")
    except ImportError as exc:
        raise VectorBackendUnavailableError(
            "faiss is not installed; install faiss-cpu to use FaissVectorBackend"
        ) from exc
    np_mod = _optional_numpy()
    if np_mod is None:
        raise VectorBackendUnavailableError(
            "numpy is required for FaissVectorBackend"
        )
    return faiss_mod, np_mod


def _resolve_faiss_paths(index_path: str | Path) -> tuple[Path, Path]:
    path = Path(index_path)
    if path.name.endswith(".meta.json"):
        stem = path.name[: -len(".meta.json")]
        return path.with_name(f"{stem}.faiss"), path
    if path.suffix == ".faiss":
        return path, path.with_suffix(".meta.json")
    return path / "knowledge-vec.faiss", path / "knowledge-vec.meta.json"


class FaissVectorBackend:
    """Optional FAISS inner-product vector backend."""

    def __init__(
        self,
        index_path: str | Path,
        *,
        expected_knowledge_db_sha256: str | None = None,
        expected_terms_db_revision: str | None = None,
        expected_model_id: str | None = None,
        expected_dim: int | None = None,
        expected_quantization: str | None = None,
    ) -> None:
        faiss_mod, np_mod = _load_faiss_modules()
        self._faiss = faiss_mod
        self._np = np_mod
        self._faiss_path, self._meta_path = _resolve_faiss_paths(index_path)
        if not self._meta_path.is_file() or not self._faiss_path.is_file():
            raise VectorIndexMetadataError("faiss index or metadata file is missing")

        raw_meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
        if not isinstance(raw_meta, dict):
            raise VectorIndexMetadataError("vector metadata must be a JSON object")
        meta = VectorIndexMetadata.from_dict(raw_meta)
        meta.validate(
            expected_knowledge_db_sha256=expected_knowledge_db_sha256,
            expected_terms_db_revision=expected_terms_db_revision,
            expected_model_id=expected_model_id,
            expected_dim=expected_dim,
            expected_quantization=expected_quantization,
            expected_backend="faiss",
        )
        entity_ids_raw = raw_meta.get("entity_ids")
        surfaces_raw = raw_meta.get("surfaces")
        if not isinstance(entity_ids_raw, list) or len(entity_ids_raw) != meta.record_count:
            raise VectorIndexMetadataError("entity_ids count mismatch")
        if surfaces_raw is None:
            surfaces = ("",) * meta.record_count
        elif isinstance(surfaces_raw, list) and len(surfaces_raw) == meta.record_count:
            surfaces = tuple(str(s) for s in surfaces_raw)
        else:
            raise VectorIndexMetadataError("surfaces count mismatch")

        self._index = faiss_mod.read_index(str(self._faiss_path))
        if int(self._index.d) != meta.dim or int(self._index.ntotal) != meta.record_count:
            raise VectorIndexMetadataError("faiss index geometry mismatches metadata")

        self._metadata = meta
        self._entity_ids = tuple(str(e) for e in entity_ids_raw)
        self._surfaces = surfaces

    @property
    def metadata(self) -> VectorIndexMetadata:
        return self._metadata

    def close(self) -> None:
        self._index = None

    def search(
        self, query_vector: Sequence[float], *, top_k: int = 16
    ) -> list[VectorHit]:
        if self._index is None:
            return []
        if len(query_vector) != self._metadata.dim:
            raise ValueError(
                f"query_vector length {len(query_vector)} does not match index dim "
                f"{self._metadata.dim}"
            )
        if top_k <= 0 or self._metadata.record_count == 0:
            return []

        q_norm = _l2_normalize(query_vector)
        if not any(x != 0.0 for x in q_norm):
            return []
        k = min(int(top_k), self._metadata.record_count)
        q_arr = self._np.asarray([q_norm], dtype=self._np.float32)
        scores, indices = self._index.search(q_arr, k)
        hits: list[VectorHit] = []
        for rank, (idx_raw, score_raw) in enumerate(
            zip(indices[0].tolist(), scores[0].tolist()), start=1
        ):
            idx = int(idx_raw)
            if idx < 0 or idx >= len(self._entity_ids):
                continue
            hits.append(
                VectorHit(
                    entity_id=self._entity_ids[idx],
                    score=float(score_raw),
                    source_surface=self._surfaces[idx],
                    rank=rank,
                )
            )
        return hits


class DeterministicHashEncoder:
    """Zero-dependency deterministic character n-gram hash encoder.

    Intended strictly for offline ``--fake-encoder`` testing and regression
    harnesses. Uses BLAKE2b feature hashing + L2 normalization with zero network
    access and zero third-party dependencies.
    """

    def __init__(
        self,
        *,
        dim: int = DEFAULT_VECTOR_DIM,
        model_id: str = "fake-hash-ngram-v1",
    ) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        if not model_id or not model_id.strip():
            raise ValueError("model_id must be non-empty")
        self._dim = int(dim)
        self._model_id = model_id.strip()

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def _encode_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        norm_text = normalize_text(text)
        if not norm_text:
            return vec

        features: list[tuple[str, float]] = []
        for run in _CJK_RUN_RE.findall(norm_text):
            for ch in run:
                features.append((f"c1:{ch}", 1.0))
            for idx in range(len(run) - 1):
                features.append((f"c2:{run[idx : idx + 2]}", 1.8))
            for idx in range(len(run) - 2):
                features.append((f"c3:{run[idx : idx + 3]}", 2.2))

        for tok in _LATIN_WORD_RE.findall(norm_text):
            features.append((f"lw:{tok}", 2.2))
            for n in (3, 4):
                if len(tok) >= n:
                    for idx in range(len(tok) - n + 1):
                        features.append((f"ln{n}:{tok[idx : idx + n]}", 1.0))

        if not features:
            for idx, ch in enumerate(norm_text):
                features.append((f"ch:{ch}", 1.0))
                if idx + 1 < len(norm_text):
                    features.append((f"bg:{norm_text[idx : idx + 2]}", 1.4))

        for feat, weight in features:
            digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            slot = int.from_bytes(digest[:4], "little") % self._dim
            sign = 1.0 if (digest[4] & 1) == 0 else -1.0
            vec[slot] += sign * weight

        return _l2_normalize(vec)

    def encode_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._encode_one(text) for text in texts]

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._encode_one(text) for text in texts]


class OnnxInt8TextEncoder:
    """Optional CPU ONNX Runtime text encoder with MRL truncation & L2 norm.

    Lazily imports ``onnxruntime``, ``tokenizers``, and ``numpy`` so standard
    runtime deployments never require ML packages.
    """

    def __init__(
        self,
        *,
        model_path: str | Path,
        tokenizer_path: str | Path | None = None,
        model_id: str = "onnx-int8-encoder",
        dim: int | None = None,
        max_length: int = 256,
        query_prefix: str = "",
        passage_prefix: str = "",
        pooling: Literal["mean", "cls"] = "mean",
        providers: Sequence[str] = ("CPUExecutionProvider",),
    ) -> None:
        try:
            ort_mod = importlib.import_module("onnxruntime")
            tok_mod = importlib.import_module("tokenizers")
        except ImportError as exc:
            raise VectorBackendUnavailableError(
                "onnxruntime and tokenizers are required for OnnxInt8TextEncoder; "
                "install wuwaterm[rag-onnx] or pass --fake-encoder"
            ) from exc

        np_mod = _optional_numpy()
        if np_mod is None:
            raise VectorBackendUnavailableError(
                "numpy is required for OnnxInt8TextEncoder; install wuwaterm[rag-onnx]"
            )

        model_file = Path(model_path)
        tok_file = (
            Path(tokenizer_path)
            if tokenizer_path is not None
            else model_file.with_name("tokenizer.json")
        )
        if not model_file.is_file():
            raise VectorRetrievalError(f"ONNX model file not found: {model_file.name}")
        if not tok_file.is_file():
            raise VectorRetrievalError(f"Tokenizer file not found: {tok_file.name}")

        self._ort = ort_mod
        self._np = np_mod
        self._tokenizer = tok_mod.Tokenizer.from_file(str(tok_file))
        self._tokenizer.enable_truncation(max_length=max(8, int(max_length)))
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self._session = ort_mod.InferenceSession(
            str(model_file), providers=list(providers)
        )
        self._input_names = {item.name for item in self._session.get_inputs()}

        output_shape = self._session.get_outputs()[0].shape
        native_dim = output_shape[-1] if output_shape and isinstance(output_shape[-1], int) else None
        if dim is not None:
            if dim <= 0:
                raise ValueError("dim must be positive")
            if native_dim is not None and dim > native_dim:
                raise ValueError(f"requested dim {dim} exceeds model dim {native_dim}")
            self._dim = int(dim)
        elif native_dim is not None:
            self._dim = int(native_dim)
        else:
            raise VectorRetrievalError(
                "cannot infer embedding dim from ONNX output shape; pass explicit dim"
            )

        self._model_id = model_id
        self._query_prefix = query_prefix
        self._passage_prefix = passage_prefix
        self._pooling = pooling

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def _encode_batch(self, texts: Sequence[str], prefix: str) -> list[list[float]]:
        if not texts:
            return []
        prepared = [f"{prefix}{t}" if prefix else t for t in texts]
        encodings = self._tokenizer.encode_batch(prepared)
        input_ids = self._np.asarray([enc.ids for enc in encodings], dtype=self._np.int64)
        attention_mask = self._np.asarray(
            [enc.attention_mask for enc in encodings], dtype=self._np.int64
        )
        feeds: dict[str, Any] = {}
        if "input_ids" in self._input_names:
            feeds["input_ids"] = input_ids
        if "attention_mask" in self._input_names:
            feeds["attention_mask"] = attention_mask
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = self._np.asarray(
                [enc.type_ids for enc in encodings], dtype=self._np.int64
            )

        outputs = self._session.run(None, feeds)
        hidden = outputs[0]
        if hidden.ndim == 2:
            pooled = hidden
        elif hidden.ndim == 3:
            if self._pooling == "cls":
                pooled = hidden[:, 0, :]
            else:
                mask = attention_mask[:, :, None].astype(self._np.float32)
                denom = self._np.maximum(mask.sum(axis=1), 1e-9)
                pooled = (hidden.astype(self._np.float32) * mask).sum(axis=1) / denom
        else:
            raise VectorRetrievalError(f"unexpected ONNX output rank: {hidden.ndim}")

        sliced = pooled[:, : self._dim]
        return [_l2_normalize(row.tolist()) for row in sliced]

    def encode_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode_batch(texts, self._query_prefix)

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode_batch(texts, self._passage_prefix)


def open_vector_backend(
    index_path: str | Path,
    *,
    backend: str = "memmap",
    expected_knowledge_db_sha256: str,
    expected_terms_db_revision: str,
    expected_model_id: str | None = None,
    expected_dim: int | None = None,
    expected_quantization: str | None = None,
) -> VectorIndexBackend | None:
    """Open a companion vector index with strict fail-closed degradation.

    Any missing file, unavailable optional C extension, corrupted binary/JSON,
    or metadata mismatch (schema, ``knowledge_db_sha256``, ``terms_db_revision``,
    ``model_id``, ``dim``, ``quantization``) logs a sanitized warning without
    exposing local absolute paths and returns ``None`` so callers degrade to pure
    Lexical RAG.
    """
    try:
        if backend == "memmap":
            return Int8MemmapVectorBackend.open(
                index_path,
                expected_knowledge_db_sha256=expected_knowledge_db_sha256,
                expected_terms_db_revision=expected_terms_db_revision,
                expected_model_id=expected_model_id,
                expected_dim=expected_dim,
                expected_quantization=expected_quantization,
            )
        if backend == "sqlite-vec":
            return SqliteVecBackend.open(
                index_path,
                expected_knowledge_db_sha256=expected_knowledge_db_sha256,
                expected_terms_db_revision=expected_terms_db_revision,
                expected_model_id=expected_model_id,
                expected_dim=expected_dim,
                expected_quantization=expected_quantization,
            )
        if backend == "faiss":
            return FaissVectorBackend(
                index_path,
                expected_knowledge_db_sha256=expected_knowledge_db_sha256,
                expected_terms_db_revision=expected_terms_db_revision,
                expected_model_id=expected_model_id,
                expected_dim=expected_dim,
                expected_quantization=expected_quantization,
            )
        LOGGER.warning(
            "unsupported vector backend %r; degrading to pure lexical RAG",
            backend,
        )
        return None
    except VectorBackendUnavailableError:
        LOGGER.warning(
            "optional vector backend %s is unavailable; degrading to pure lexical RAG",
            backend,
        )
        return None
    except VectorIndexMetadataError as exc:
        LOGGER.warning(
            "companion vector index failed validation (%s); degrading to pure lexical RAG",
            exc,
        )
        return None
    except (OSError, ValueError, sqlite3.Error):
        LOGGER.warning(
            "companion vector index could not be loaded (%s); degrading to pure lexical RAG",
            backend,
        )
        return None


def vector_hits_to_candidates(
    hits: Sequence[VectorHit],
    entity_rows: Mapping[str, Mapping[str, object]],
    *,
    text: str = "",
    min_score: float = 0.0,
) -> tuple[DomainKnowledgeCandidate, ...]:
    """Convert raw ``VectorHit`` items into isolated ``dense_semantic`` candidates."""
    cjk_query = has_cjk(text) if text else True
    candidates: list[DomainKnowledgeCandidate] = []
    for hit in hits:
        if hit.score < min_score:
            continue
        row = entity_rows.get(hit.entity_id)
        if row is None:
            continue
        zh = str(row.get("zh", ""))
        en = str(row.get("en", ""))
        entity_type = str(row.get("entity_type", "term"))
        trust_raw = str(row.get("trust_tier", "official_authoritative"))
        if trust_raw not in TRUST_TIERS:
            continue
        trust_tier: TrustTier = (
            "official_authoritative"
            if trust_raw == "official_authoritative"
            else (
                "official_derived"
                if trust_raw == "official_derived"
                else "community_curated"
            )
        )
        gloss = str(row.get("context_gloss", ""))
        preferred_surface = (
            (zh or en or hit.source_surface)
            if cjk_query
            else (en or zh or hit.source_surface)
        )
        if not preferred_surface:
            continue
        candidates.append(
            DomainKnowledgeCandidate(
                entity_id=hit.entity_id,
                source_surface=preferred_surface,
                zh=zh,
                en=en,
                entity_type=entity_type,
                trust_tier=trust_tier,
                retrieval_channel="dense_semantic",
                score=float(hit.score),
                context_gloss=gloss,
                lock_eligible=False,
                matched_source_spans=(),
            )
        )
    return tuple(candidates)


def hybrid_fuse_candidates(
    lexical_candidates: Iterable[DomainKnowledgeCandidate],
    vector_candidates: Iterable[DomainKnowledgeCandidate],
    *,
    text: str = "",
    locked_surfaces: Collection[str] = (),
    reference_surfaces: Collection[str] = (),
    k: int = DEFAULT_RRF_K,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    max_scalars: int = DEFAULT_MAX_SCALARS,
    lexical_weight: float = 1.0,
    vector_weight: float = 0.85,
) -> tuple[DomainKnowledgeCandidate, ...]:
    """Deterministic RRF fusion of lexical and vector candidates.

    Strictly preserves ``lock_eligible=False`` and delegates final isolation,
    ordinary-word (single-char CJK + lowercase Latin overlap), lock-collision,
    and scalar-budget enforcement to ``domain_knowledge``.
    """
    if k <= 0:
        raise ValueError("RRF k must be positive")
    if max_candidates <= 0 or max_scalars <= 0:
        return ()

    valid_lex = [
        cand
        for cand in lexical_candidates
        if isinstance(cand, DomainKnowledgeCandidate) and cand.lock_eligible is False
    ]
    valid_vec = [
        cand
        for cand in vector_candidates
        if isinstance(cand, DomainKnowledgeCandidate) and cand.lock_eligible is False
    ]

    valid_lex.sort(
        key=lambda item: (-float(item.score), item.entity_id, item.source_surface)
    )
    valid_vec.sort(
        key=lambda item: (-float(item.score), item.entity_id, item.source_surface)
    )

    rrf_scores: dict[str, float] = {}
    chosen_base: dict[str, DomainKnowledgeCandidate] = {}

    for rank, cand in enumerate(valid_lex, start=1):
        rrf_scores[cand.entity_id] = rrf_scores.get(cand.entity_id, 0.0) + (
            lexical_weight / (k + rank)
        )
        if cand.entity_id not in chosen_base:
            chosen_base[cand.entity_id] = cand

    for rank, cand in enumerate(valid_vec, start=1):
        rrf_scores[cand.entity_id] = rrf_scores.get(cand.entity_id, 0.0) + (
            vector_weight / (k + rank)
        )
        if cand.entity_id not in chosen_base:
            chosen_base[cand.entity_id] = replace(
                cand,
                retrieval_channel="dense_semantic",
                lock_eligible=False,
            )

    fused: list[DomainKnowledgeCandidate] = []
    cjk_query = has_cjk(text) if text else None
    for entity_id, base in chosen_base.items():
        # For dense-only candidates with source text, keep surface aligned with
        # the query script so Latin ordinary-word gating checks the English term.
        surface = base.source_surface
        if (
            cjk_query is False
            and base.retrieval_channel == "dense_semantic"
            and base.en
            and normalize_text(surface) != normalize_text(base.en)
        ):
            surface = base.en
        fused.append(
            replace(
                base,
                source_surface=surface,
                score=rrf_scores[entity_id],
                lock_eligible=False,
            )
        )

    if text:
        return select_domain_knowledge_candidates(
            text,
            fused,
            locked_surfaces=locked_surfaces,
            reference_surfaces=reference_surfaces,
            max_candidates=max_candidates,
            max_scalars=max_scalars,
        )
    return prepare_prompt_domain_knowledge_candidates(
        "",
        fused,
        locked_surfaces=locked_surfaces,
        reference_surfaces=reference_surfaces,
        max_candidates=max_candidates,
        max_scalars=max_scalars,
    )
