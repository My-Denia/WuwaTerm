#!/usr/bin/env python3
"""H3 corpus acceptance gate (C1-C10) and ``h3-acceptance-v1`` receipt validator.

Subcommands
-----------

``accept``          run mechanical checks C1-C10 over a candidate H3 corpus
                    (``h3-manifest-v1``, ``usability-sample-v1``, metadata,
                    official/community annotations, prior texts/IDs, bound Core
                    tree and production ``terms.db``) and emit a structured
                    ``h3-acceptance-v1`` receipt JSON with zero corpus text.
``verify-receipt``  validate an existing ``h3-acceptance-v1`` receipt file using
                    ``validate_acceptance_receipt``.

Exit codes:
  0  all checks C1-C10 passed (``verdict == "pass"``) / valid receipt
  1  one or more checks failed (``verdict == "fail"``) / invalid receipt
  2  CLI usage error
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCORER_PATH = ROOT / "scripts" / "eval_usability.py"
SELF_PATH = Path(__file__).resolve()

# Pinned v0.14.0 bindings and frozen scorer hash.
PINNED_CORE_COMMIT = "2737530187f1e1c3a6b8758de357c8d32d21dfc7"
PINNED_SRC_TREE = "8e6bd426bfc12625f050c03ed14e419d3dba145f"
PINNED_MATCHER_REVISION = "286d29e8f25b1ee53d0634c89bf6fb8a83ef950888c21f15d485413cb25d0664"
PINNED_TERMS_DB_SHA256 = "9ddee8197fbd01ed283f948e6a77e5067485d47daba24553582dcf309fba1f18"
PINNED_SCORER_SHA256 = "8b17d3ae75f7aafebaf537246fcaa1e56c1e52cb1a4bf5c875c599c42622d796"

# Schemas.
RECEIPT_SCHEMA = "h3-acceptance-v1"
MANIFEST_SCHEMA = "h3-manifest-v1"
SAMPLE_SCHEMA = "usability-sample-v1"
OFFICIAL_NAMES_SCHEMA = "usability-names-v1"
COMMUNITY_NAMES_SCHEMA = "h3-community-names-v1"
METADATA_SCHEMA = "h3-corpus-metadata-v1"
PRIOR_TEXTS_SCHEMA = "h3-prior-texts-v1"
PRIOR_IDS_SCHEMA = "h3-prior-ids-v1"
UPSTREAM_CLEARANCES_SCHEMA = "h3-upstream-clearances-v1"

# Rule IDs and domain vocabularies.
ALL_RULES = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10")
DIRECTIONS = ("zh->en", "en->zh")
DIRECTION_ALIASES = {
    "zh->en": "zh->en",
    "zh-en": "zh->en",
    "en->zh": "en->zh",
    "en-zh": "en->zh",
}
GENRES = ("announcement", "story", "guide", "mixed")
EXCLUSIONS = ("dictionary", "no_letter", "duplicate")

# C3 default thresholds.
DEFAULT_PER_DIRECTION_FLOOR = 140
DEFAULT_PER_GENRE_FLOOR = 30
DEFAULT_ZH_EN_GUIDE_FLOOR = 50

# C4 defaults.
WINDOW_SIZE = 12
DEFAULT_M2_MAX_SHARED_SPAN = 24

# C7 cutoff.
DEFAULT_CUTOFF_ISO = "2026-11-06T19:45:00+08:00"
MIN_FORBIDDEN_VERSION = (3, 8)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")

# C7 version detection patterns (applied to NFKC-normalized text).
_TAGGED_VERSION_BEFORE_RE = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9_])(?:ver\.?|version|patch|update)\s*|(?<![A-Za-z0-9_])v(?=\d)|(?:版本|前瞻|更新|补丁|公测|测试服)\s*)"
    r"(\d+)\.(\d+)(?:\.(\d+))?(?![\d.])"
)
_TAGGED_VERSION_AFTER_RE = re.compile(
    r"(?i)(?<![\d.])(\d+)\.(\d+)(?:\.(\d+))?\s*"
    r"(?:版本|版(?![权本])|前瞻|更新|补丁|卡池|下半|上半|新角色|新地图|剧情|主线|活动|测试服|实装|上线|直播|前瞻直播"
    r"|(?-i:PV)|(?:pv|banner|patch|update|version|beta|livestream|preview)(?![A-Za-z0-9_]))"
)
_SEMVER_THREE_PART_RE = re.compile(r"(?<![\d.])(\d+)\.(\d+)\.(\d+)(?![\d.])")
_BARE_VERSION_RE = re.compile(r"(?<![\d.$￥¥+\-×])(\d+)\.(\d+)(?![\d.])")
_UNIT_AFTER_NUMBER_RE = re.compile(
    r"^\s*(?:%|‰|°|/|(?i:s|sec|secs|second|seconds|ms|min|mins|hr|hrs|h|m|cm|mm|km|px|gb|mb|kb|tb|x|k|w|pts|pt|hp|atk|def|cr|cd|er|dps|fps|hz|usd|eur|cny|rmb)(?![A-Za-z])"
    r"|(?:秒|分|分钟|小时|时|米|厘米|公里|倍|万|亿|点|层|级|星|颗|抽|元|美元|人民币|次|天|日|月|年|帧|度|折|斤|克|千克|公斤))"
)

# C8 PII detection patterns (applied to raw and NFKC-normalized text).
_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"(?i)(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9-])")),
    ("player_uid", re.compile(r"(?i)\buid\s*[:：#]?\s*\d{6,12}\b")),
    ("player_uid", re.compile(r"(?<!\d)\d{9}(?!\d)")),
    (
        "contact_or_group",
        re.compile(
            r"(?i)(?:(?<![A-Za-z])(?:qq|vx|wx|wechat)(?![A-Za-z])|微信|微信号|企鹅|扣扣|群号?|交流群|水友群|公会群|频道号?)"
            r"\s*[:：#号]?\s*[A-Za-z0-9_-]{5,24}"
        ),
    ),
    (
        "contact_or_group",
        re.compile(r"(?i)(?:discord\.gg/|discord\.com/invite/|t\.me/|telegram\.me/)[A-Za-z0-9_/+=-]{3,}"),
    ),
    (
        "contact_or_group",
        re.compile(r"(?i)(?:(?<![A-Za-z])(?:discord|telegram|tg)(?![A-Za-z])|联系我|私信|加我)\s*[:：]\s*@?[A-Za-z0-9_.-]{3,32}"),
    ),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("phone", re.compile(r"(?<!\d)\+\d{1,3}[-\s]?\d{3,4}[-\s]?\d{3,4}[-\s]?\d{3,4}(?!\d)")),
    ("social_handle", re.compile(r"(?<![A-Za-z0-9._%+-])@[A-Za-z][A-Za-z0-9_]{4,31}\b")),
)


class ReceiptValidationError(ValueError):
    """Raised by ``validate_acceptance_receipt`` when a receipt is invalid."""


class _CheckError(Exception):
    """Internal non-text error raised while loading or validating an artifact."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------- helpers


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def load_json_strict(path: Path, *, label: str) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _CheckError("unreadable_file", f"{label}: cannot read file") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _CheckError("invalid_utf8", f"{label}: not valid UTF-8") from exc

    def no_dup(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({k for k, _ in pairs}) != len(pairs):
            raise ValueError("duplicate object key")
        return dict(pairs)

    def no_const(_name: str) -> Any:
        raise ValueError("NaN/Infinity not valid JSON")

    try:
        return json.loads(text, object_pairs_hook=no_dup, parse_constant=no_const)
    except (ValueError, RecursionError) as exc:
        raise _CheckError("invalid_json", f"{label}: not valid JSON") from exc


def normalize_direction(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return DIRECTION_ALIASES.get(value)


def normalize_dedup(text: str) -> str:
    """NFKC + casefold + remove all whitespace and Unicode punctuation."""
    return "".join(
        ch
        for ch in unicodedata.normalize("NFKC", text).casefold()
        if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )


def has_unicode_letter(text: str) -> bool:
    return any(unicodedata.category(ch).startswith("L") for ch in text)


def _has_unicode_nd_digit(text: str) -> bool:
    return any(unicodedata.category(ch) == "Nd" for ch in text)


def _normalize_id_token(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


# --------------------------------------------------------------- git & Core


def _git(cwd: Path, *args: str) -> tuple[int, bytes, bytes]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return 1, b"", b"git cannot be started"
    return proc.returncode, proc.stdout, proc.stderr


def _read_until_nul(stream: Any) -> bytes | None:
    buf = bytearray()
    while True:
        ch = stream.read(1)
        if not ch:
            return None if not buf else bytes(buf)
        if ch == b"\0":
            return bytes(buf)
        buf.extend(ch)


def iter_git_commit_text_blobs(repo: Path, commit: str) -> tuple[str, list[str]]:
    """Resolve ``commit`` and read all tracked UTF-8 non-binary blobs in one cat-file batch."""
    rc, out, _ = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if rc != 0:
        raise _CheckError("git_commit_invalid", "cannot resolve core commit")
    resolved = out.decode("utf-8").strip()

    rc, listing, _ = _git(repo, "ls-tree", "-r", "-z", resolved)
    if rc != 0:
        raise _CheckError("git_ls_tree_failed", "git ls-tree failed")

    oids: list[bytes] = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, _, _path = entry.partition(b"\t")
        fields = meta.split()
        if len(fields) != 3 or fields[1] != b"blob" or fields[0] == b"120000":
            continue
        oids.append(fields[2])

    texts: list[str] = []
    if not oids:
        return resolved, texts

    proc = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "--batch", "-Z"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    try:
        payload = b"".join(oid + b"\0" for oid in oids)
        proc.stdin.write(payload)
        proc.stdin.close()
        for _ in oids:
            header = _read_until_nul(proc.stdout)
            if header is None:
                raise _CheckError("git_cat_file_failed", "cat-file stream truncated")
            if header.endswith(b" missing"):
                continue
            parts = header.split()
            if len(parts) < 3:
                raise _CheckError("git_cat_file_failed", "malformed cat-file header")
            size = int(parts[-1])
            body = proc.stdout.read(size)
            if len(body) < size:
                raise _CheckError("git_cat_file_failed", "cat-file body truncated")
            delim = proc.stdout.read(1)
            if delim != b"\0":
                raise _CheckError("git_cat_file_failed", "cat-file delimiter missing")
            if b"\0" in body:
                continue
            try:
                texts.append(body.decode("utf-8"))
            except UnicodeDecodeError:
                continue
    finally:
        proc.stdout.close()
        proc.stderr.close()
        rc = proc.wait()
    if rc != 0:
        raise _CheckError("git_cat_file_failed", "git cat-file exited non-zero")
    return resolved, texts


@dataclass
class BoundCore:
    application: Any
    cjk_span: Any
    lookup: Any
    segmentation: Any
    sentence: Any
    text_spans: Any
    matcher_revision: str


def import_bound_core(bound_tree: Path) -> BoundCore:
    src = (bound_tree / "src").resolve()
    if not src.is_dir():
        raise _CheckError("bound_tree_missing_src", "bound tree has no src directory")
    loaded = sys.modules.get("wuwaterm")
    if loaded is None:
        sys.path.insert(0, str(src))
    import wuwaterm
    from wuwaterm import application, cjk_span, lookup, segmentation, sentence, text_spans

    for mod in (wuwaterm, application, cjk_span, lookup, segmentation, sentence, text_spans):
        origin = Path(str(mod.__file__)).resolve()
        if not origin.is_relative_to(src):
            raise _CheckError(
                "core_import_origin_mismatch",
                "wuwaterm is already imported from another tree; run in a fresh process",
            )
    return BoundCore(
        application=application,
        cjk_span=cjk_span,
        lookup=lookup,
        segmentation=segmentation,
        sentence=sentence,
        text_spans=text_spans,
        matcher_revision=str(cjk_span.MATCHER_REVISION),
    )


def load_verified_scorer(scorer_path: Path, expected_sha256: str) -> tuple[Any, str]:
    if not scorer_path.is_file():
        raise _CheckError("scorer_missing", "eval_usability.py not found")
    actual = sha256_file(scorer_path)
    if actual != expected_sha256:
        raise _CheckError("scorer_sha256_mismatch", "eval_usability.py SHA-256 mismatch")
    mod_name = "_h3_corpus_eval_usability"
    loaded = sys.modules.get(mod_name)
    if loaded is not None and Path(str(loaded.__file__)).resolve() == scorer_path.resolve():
        return loaded, actual
    spec = importlib.util.spec_from_file_location(mod_name, scorer_path)
    if spec is None or spec.loader is None:
        raise _CheckError("scorer_load_failed", "cannot load eval_usability.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "windows"):
        raise _CheckError("scorer_missing_windows", "eval_usability.py lacks windows()")
    return module, actual


# --------------------------------------------------------------- data models


@dataclass(frozen=True)
class ManifestDoc:
    doc_id: str
    direction: str
    genre: str
    text: str


@dataclass(frozen=True)
class SampleParagraph:
    pid: str
    doc_id: str
    direction: str
    genre: str
    scored: bool
    exclusion: str | None
    source: str
    reference: str | None
    declared_source_sha256: str | None = None


@dataclass(frozen=True)
class DocMetadata:
    doc_id: str
    source_id: str | None
    textmap_keys: tuple[str, ...]
    series: str | None
    author: str | None
    bilingual_pair_id: str | None
    published: str | None


# ------------------------------------------------------------ loaders (C1-C9)


def _as_str_tuple(raw: Any, *, field_name: str, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return tuple(x for x in raw if x.strip())
    raise _CheckError("invalid_metadata", f"{where}: {field_name} must be string, array of strings, or null")


def _load_manifest_data(path: Path) -> tuple[str, list[ManifestDoc], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    try:
        data = load_json_strict(path, label=path.name)
    except _CheckError as exc:
        issues.append({"code": exc.code, "artifact": "manifest"})
        return "", [], issues
    if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
        issues.append({"code": "invalid_manifest_schema", "artifact": "manifest"})
        return "", [], issues
    raw_docs = data.get("documents")
    if not isinstance(raw_docs, list) or not raw_docs:
        issues.append({"code": "empty_or_invalid_documents", "artifact": "manifest"})
        return "", [], issues

    docs: list[ManifestDoc] = []
    seen: set[str] = set()
    for idx, raw in enumerate(raw_docs):
        if not isinstance(raw, dict) or set(raw) != {"doc_id", "direction", "genre", "text"}:
            issues.append({"code": "invalid_manifest_doc_keys", "index": idx})
            continue
        doc_id = raw["doc_id"]
        direction = normalize_direction(raw["direction"])
        genre = raw["genre"]
        text = raw["text"]
        if not isinstance(doc_id, str) or not doc_id or doc_id in seen:
            issues.append({"code": "invalid_or_duplicate_doc_id", "index": idx})
            continue
        if direction is None:
            issues.append({"code": "invalid_direction", "doc_id": doc_id})
            continue
        if not isinstance(genre, str) or genre not in GENRES:
            issues.append({"code": "invalid_genre", "doc_id": doc_id})
            continue
        if not isinstance(text, str) or not text.strip():
            issues.append({"code": "blank_document_text", "doc_id": doc_id})
            continue
        seen.add(doc_id)
        docs.append(ManifestDoc(doc_id=doc_id, direction=direction, genre=genre, text=text))
    label = data.get("set") if isinstance(data.get("set"), str) else ""
    return label, docs, issues


def _load_sample_data(
    path: Path,
) -> tuple[str, str | None, list[SampleParagraph], dict[str, Any], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    try:
        data = load_json_strict(path, label=path.name)
    except _CheckError as exc:
        issues.append({"code": exc.code, "artifact": "sample"})
        return "", None, [], {}, issues
    if not isinstance(data, dict) or data.get("schema") != SAMPLE_SCHEMA:
        issues.append({"code": "invalid_sample_schema", "artifact": "sample"})
        return "", None, [], {}, issues
    label = data.get("set")
    if not isinstance(label, str) or not label:
        issues.append({"code": "missing_sample_set", "artifact": "sample"})
        label = ""
    raw_paras = data.get("paragraphs")
    if not isinstance(raw_paras, list) or not raw_paras:
        issues.append({"code": "empty_or_invalid_paragraphs", "artifact": "sample"})
        return label, None, [], {}, issues

    declared_manifest_sha = data.get("manifest_sha256")
    if declared_manifest_sha is not None and not isinstance(declared_manifest_sha, str):
        issues.append({"code": "invalid_manifest_sha256_field", "artifact": "sample"})
        declared_manifest_sha = None

    paragraphs: list[SampleParagraph] = []
    seen_pids: set[str] = set()
    required = ("id", "doc", "direction", "genre", "scored", "exclusion", "source", "reference")
    for idx, raw in enumerate(raw_paras):
        if not isinstance(raw, dict) or any(k not in raw for k in required):
            issues.append({"code": "invalid_paragraph_keys", "index": idx})
            continue
        pid = raw["id"]
        doc_id = raw["doc"]
        direction = normalize_direction(raw["direction"])
        genre = raw["genre"]
        scored = raw["scored"]
        exclusion = raw["exclusion"]
        source = raw["source"]
        reference = raw["reference"]
        if not isinstance(pid, str) or not pid or pid in seen_pids:
            issues.append({"code": "invalid_or_duplicate_paragraph_id", "index": idx})
            continue
        if not isinstance(doc_id, str) or not doc_id:
            issues.append({"code": "invalid_paragraph_doc_id", "paragraph_id": pid})
            continue
        if direction is None:
            issues.append({"code": "invalid_paragraph_direction", "paragraph_id": pid})
            continue
        if not isinstance(genre, str) or genre not in GENRES:
            issues.append({"code": "invalid_paragraph_genre", "paragraph_id": pid})
            continue
        if type(scored) is not bool:
            issues.append({"code": "invalid_scored_flag", "paragraph_id": pid})
            continue
        if exclusion is not None and (not isinstance(exclusion, str) or exclusion not in EXCLUSIONS):
            issues.append({"code": "invalid_exclusion_label", "paragraph_id": pid})
            continue
        if scored != (exclusion is None):
            issues.append({"code": "scored_exclusion_inconsistent", "paragraph_id": pid})
            continue
        if not isinstance(source, str):
            issues.append({"code": "invalid_source_type", "paragraph_id": pid})
            continue
        if reference is not None and not isinstance(reference, str):
            issues.append({"code": "invalid_reference_type", "paragraph_id": pid})
            continue
        decl_src_sha = raw.get("source_sha256")
        if decl_src_sha is not None and not isinstance(decl_src_sha, str):
            issues.append({"code": "invalid_source_sha256_field", "paragraph_id": pid})
            decl_src_sha = None
        seen_pids.add(pid)
        paragraphs.append(
            SampleParagraph(
                pid=pid,
                doc_id=doc_id,
                direction=direction,
                genre=genre,
                scored=scored,
                exclusion=exclusion,
                source=source,
                reference=reference,
                declared_source_sha256=decl_src_sha,
            )
        )
    return label, declared_manifest_sha, paragraphs, data, issues


def _load_metadata(
    metadata_path: Path | None,
    sample_raw: Mapping[str, Any],
) -> tuple[dict[str, DocMetadata], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    meta_by_doc: dict[str, DocMetadata] = {}

    def _parse_doc_entry(raw: Any, where: str) -> None:
        if not isinstance(raw, dict):
            issues.append({"code": "invalid_metadata_entry", "where": where})
            return
        doc_id = raw.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id:
            issues.append({"code": "missing_metadata_doc_id", "where": where})
            return
        if doc_id in meta_by_doc:
            issues.append({"code": "duplicate_metadata_doc_id", "doc_id": doc_id})
            return
        try:
            source_id_raw = raw.get("source_id")
            if source_id_raw is not None and not isinstance(source_id_raw, str):
                raise _CheckError("invalid_metadata", "source_id")
            series_raw = raw.get("series")
            if series_raw is not None and not isinstance(series_raw, str):
                raise _CheckError("invalid_metadata", "series")
            author_raw = raw.get("author")
            if author_raw is not None and not isinstance(author_raw, str):
                raise _CheckError("invalid_metadata", "author")
            pair_raw = raw.get("bilingual_pair_id", raw.get("pair_id"))
            if pair_raw is not None and not isinstance(pair_raw, str):
                raise _CheckError("invalid_metadata", "bilingual_pair_id")
            published_raw = raw.get("published")
            if published_raw is not None and not isinstance(published_raw, str):
                raise _CheckError("invalid_metadata", "published")
            tm_keys = (
                _as_str_tuple(raw.get("textmap_keys"), field_name="textmap_keys", where=where)
                + _as_str_tuple(raw.get("textmap_key"), field_name="textmap_key", where=where)
            )
        except _CheckError as exc:
            issues.append({"code": exc.code, "doc_id": doc_id})
            return
        meta_by_doc[doc_id] = DocMetadata(
            doc_id=doc_id,
            source_id=source_id_raw,
            textmap_keys=tuple(dict.fromkeys(tm_keys)),
            series=series_raw,
            author=author_raw,
            bilingual_pair_id=pair_raw,
            published=published_raw,
        )

    if metadata_path is not None:
        try:
            data = load_json_strict(metadata_path, label=metadata_path.name)
        except _CheckError as exc:
            issues.append({"code": exc.code, "artifact": "metadata"})
            return {}, issues
        if not isinstance(data, dict) or data.get("schema") != METADATA_SCHEMA:
            issues.append({"code": "invalid_metadata_schema", "artifact": "metadata"})
            return {}, issues
        raw_docs = data.get("documents")
        if not isinstance(raw_docs, list):
            issues.append({"code": "invalid_metadata_documents", "artifact": "metadata"})
            return {}, issues
        for idx, raw in enumerate(raw_docs):
            _parse_doc_entry(raw, f"documents[{idx}]")
    elif isinstance(sample_raw.get("documents"), list):
        for idx, raw in enumerate(sample_raw["documents"]):
            _parse_doc_entry(raw, f"sample.documents[{idx}]")
    return meta_by_doc, issues


# --------------------------------------------------------------- check C1


def check_c1(
    *,
    manifest_path: Path,
    manifest_sha256: str,
    manifest_label: str,
    manifest_docs: Sequence[ManifestDoc],
    manifest_issues: Sequence[dict[str, Any]],
    sample_path: Path,
    sample_sha256: str,
    sample_label: str,
    declared_manifest_sha256: str | None,
    sample_paragraphs: Sequence[SampleParagraph],
    sample_issues: Sequence[dict[str, Any]],
    metadata_by_doc: Mapping[str, DocMetadata],
    metadata_issues: Sequence[dict[str, Any]],
    has_metadata_file: bool,
    core: BoundCore | None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = [*manifest_issues, *sample_issues, *metadata_issues]

    if manifest_label and sample_label and manifest_label != sample_label:
        issues.append({"code": "set_label_mismatch"})
    if declared_manifest_sha256 is not None and declared_manifest_sha256 != manifest_sha256:
        issues.append({"code": "manifest_sha256_mismatch"})

    by_doc_manifest = {d.doc_id: d for d in manifest_docs}
    paras_by_doc: dict[str, list[SampleParagraph]] = {}
    for para in sample_paragraphs:
        paras_by_doc.setdefault(para.doc_id, []).append(para)

    manifest_ids = set(by_doc_manifest)
    sample_ids = set(paras_by_doc)
    for missing in sorted(manifest_ids - sample_ids):
        issues.append({"code": "doc_missing_in_sample", "doc_id": missing})
    for extra in sorted(sample_ids - manifest_ids):
        issues.append({"code": "doc_missing_in_manifest", "doc_id": extra})

    if has_metadata_file:
        meta_ids = set(metadata_by_doc)
        for missing in sorted(manifest_ids - meta_ids):
            issues.append({"code": "doc_missing_in_metadata", "doc_id": missing})
        for extra in sorted(meta_ids - manifest_ids):
            issues.append({"code": "extra_doc_in_metadata", "doc_id": extra})

    wire_segments_by_dir: dict[str, int] = {d: 0 for d in DIRECTIONS}
    doc_hashes_checked = 0
    if core is None:
        issues.append({"code": "bound_core_unavailable"})
    else:
        for doc in manifest_docs:
            segments = core.segmentation.segment_text(doc.text)
            non_empty_segs = [s for s in segments if s.core_start < s.core_end]
            wire_segments_by_dir[doc.direction] += len(non_empty_segs)
            if not non_empty_segs:
                issues.append({"code": "empty_segmentation", "doc_id": doc.doc_id})

            raw_ranges = core.text_spans.paragraph_ranges(doc.text)
            expected_paras = [
                doc.text[cs:ce]
                for ps, pe in raw_ranges
                for cs, ce in [core.text_spans.segment_core(doc.text, ps, pe)]
                if cs < ce
            ]
            actual_paras = paras_by_doc.get(doc.doc_id, [])
            if len(actual_paras) != len(expected_paras):
                issues.append(
                    {
                        "code": "paragraph_count_mismatch",
                        "doc_id": doc.doc_id,
                        "manifest_paragraphs": len(expected_paras),
                        "sample_paragraphs": len(actual_paras),
                    }
                )
                continue
            for idx, (exp_text, para) in enumerate(zip(expected_paras, actual_paras, strict=True)):
                if para.direction != doc.direction:
                    issues.append({"code": "direction_mismatch", "doc_id": doc.doc_id, "paragraph_id": para.pid})
                if para.genre != doc.genre:
                    issues.append({"code": "genre_mismatch", "doc_id": doc.doc_id, "paragraph_id": para.pid})
                exp_sha = sha256_text(exp_text)
                act_sha = sha256_text(para.source)
                if exp_sha != act_sha:
                    issues.append(
                        {
                            "code": "paragraph_text_hash_mismatch",
                            "doc_id": doc.doc_id,
                            "paragraph_id": para.pid,
                            "index": idx,
                            "expected_sha256": exp_sha,
                            "actual_sha256": act_sha,
                        }
                    )
                if para.declared_source_sha256 is not None and para.declared_source_sha256 != act_sha:
                    issues.append(
                        {
                            "code": "declared_source_sha256_mismatch",
                            "doc_id": doc.doc_id,
                            "paragraph_id": para.pid,
                        }
                    )
            doc_hashes_checked += 1

    scored_count = sum(1 for p in sample_paragraphs if p.scored)
    return {
        "status": "pass" if not issues else "fail",
        "manifest_name": manifest_path.name,
        "manifest_sha256": manifest_sha256,
        "sample_name": sample_path.name,
        "sample_sha256": sample_sha256,
        "documents_count": len(manifest_docs),
        "documents_aligned": doc_hashes_checked,
        "paragraphs_count": len(sample_paragraphs),
        "scored_paragraphs_count": scored_count,
        "excluded_paragraphs_count": len(sample_paragraphs) - scored_count,
        "wire_segments_by_direction": wire_segments_by_dir,
        "issues_count": len(issues),
        "issues": issues,
    }


# --------------------------------------------------------------- check C2


def check_c2(
    *,
    sample_paragraphs: Sequence[SampleParagraph],
    prior_normalized_paragraphs: set[str],
    core: BoundCore | None,
    terms_db: Path,
) -> dict[str, Any]:
    if core is None or not terms_db.is_file() or not sample_paragraphs:
        return {
            "status": "fail",
            "checked_paragraphs": 0,
            "mismatches_count": 1,
            "mismatches": [
                {
                    "code": "no_paragraphs_to_check"
                    if not sample_paragraphs
                    else "bound_core_or_terms_db_unavailable"
                }
            ],
            "computed_exclusion_counts": {k: 0 for k in ("scored", *EXCLUSIONS)},
            "declared_exclusion_counts": {k: 0 for k in ("scored", *EXCLUSIONS)},
        }

    service = core.lookup.TermService(terms_db)
    translator = core.sentence.SentenceTranslator(terms_db)
    input_limit = int(getattr(core.application, "LLM_INPUT_CHAR_LIMIT", 1800))

    seen_scored_by_dir: dict[str, set[str]] = {d: set() for d in DIRECTIONS}
    computed_counts: Counter[str] = Counter()
    declared_counts: Counter[str] = Counter()
    mismatches: list[dict[str, Any]] = []

    for para in sample_paragraphs:
        declared_label = "scored" if para.scored else str(para.exclusion)
        declared_counts[declared_label] += 1

        # Verify segmentation-v1 core matches the standalone paragraph unit.
        seg_units = core.segmentation.segment_text(para.source)
        seg_cores = [para.source[s.core_start : s.core_end] for s in seg_units if s.core_start < s.core_end]
        unit_text = seg_cores[0] if len(seg_cores) == 1 else para.source

        # ADR 0020 order: 1. dictionary, 2. no_letter, 3. duplicate.
        stage = core.application._dictionary_stage(
            service,
            translator,
            core.application.TranslationJob(
                text=unit_text,
                forced_to_chinese=(para.direction == "en->zh"),
            ),
            input_limit,
        )
        is_dict = stage.outcome is not None and stage.outcome.kind in (
            core.application.KIND_EXACT,
            core.application.KIND_FUZZY,
        )

        norm = normalize_dedup(para.source)
        if is_dict:
            computed_exclusion: str | None = "dictionary"
        elif not has_unicode_letter(para.source):
            computed_exclusion = "no_letter"
        elif norm and (norm in prior_normalized_paragraphs or norm in seen_scored_by_dir[para.direction]):
            computed_exclusion = "duplicate"
        else:
            computed_exclusion = None
            if norm:
                seen_scored_by_dir[para.direction].add(norm)

        computed_label = "scored" if computed_exclusion is None else computed_exclusion
        computed_counts[computed_label] += 1

        expected_scored = computed_exclusion is None
        if para.scored != expected_scored or para.exclusion != computed_exclusion:
            mismatches.append(
                {
                    "code": "exclusion_label_mismatch",
                    "doc_id": para.doc_id,
                    "paragraph_id": para.pid,
                    "declared_scored": para.scored,
                    "declared_exclusion": para.exclusion,
                    "computed_scored": expected_scored,
                    "computed_exclusion": computed_exclusion,
                }
            )

    return {
        "status": "pass" if not mismatches else "fail",
        "checked_paragraphs": len(sample_paragraphs),
        "mismatches_count": len(mismatches),
        "mismatches": mismatches,
        "computed_exclusion_counts": {k: computed_counts[k] for k in ("scored", *EXCLUSIONS)},
        "declared_exclusion_counts": {k: declared_counts[k] for k in ("scored", *EXCLUSIONS)},
    }


# --------------------------------------------------------------- check C3


def check_c3(
    *,
    sample_paragraphs: Sequence[SampleParagraph],
    announcement_floor: int | None,
) -> dict[str, Any]:
    if announcement_floor is not None and announcement_floor < 0:
        return {
            "status": "fail",
            "floor_profile": f"invalid:announcement={announcement_floor}",
            "non_default_floor": True,
            "shortfalls": [{"code": "invalid_announcement_floor"}],
        }

    eff_ann = DEFAULT_PER_GENRE_FLOOR if announcement_floor is None else announcement_floor
    non_default = announcement_floor is not None and announcement_floor != DEFAULT_PER_GENRE_FLOOR
    floor_profile = f"custom:announcement={eff_ann}" if non_default else "default"

    per_genre_floors: dict[str, dict[str, int]] = {
        "zh->en": {
            "announcement": eff_ann,
            "story": DEFAULT_PER_GENRE_FLOOR,
            "guide": DEFAULT_ZH_EN_GUIDE_FLOOR,
            "mixed": DEFAULT_PER_GENRE_FLOOR,
        },
        "en->zh": {
            "announcement": eff_ann,
            "story": DEFAULT_PER_GENRE_FLOOR,
            "guide": DEFAULT_PER_GENRE_FLOOR,
            "mixed": DEFAULT_PER_GENRE_FLOOR,
        },
    }

    observed_dir: dict[str, int] = {d: 0 for d in DIRECTIONS}
    observed_genre: dict[str, dict[str, int]] = {d: {g: 0 for g in GENRES} for d in DIRECTIONS}
    for para in sample_paragraphs:
        if not para.scored:
            continue
        observed_dir[para.direction] += 1
        observed_genre[para.direction][para.genre] += 1

    shortfalls: list[dict[str, Any]] = []
    for direction in DIRECTIONS:
        if observed_dir[direction] < DEFAULT_PER_DIRECTION_FLOOR:
            shortfalls.append(
                {
                    "code": "direction_floor_shortfall",
                    "direction": direction,
                    "observed": observed_dir[direction],
                    "required": DEFAULT_PER_DIRECTION_FLOOR,
                }
            )
        for genre in GENRES:
            req = per_genre_floors[direction][genre]
            obs = observed_genre[direction][genre]
            if obs < req:
                shortfalls.append(
                    {
                        "code": "genre_floor_shortfall",
                        "direction": direction,
                        "genre": genre,
                        "observed": obs,
                        "required": req,
                    }
                )

    return {
        "status": "pass" if not shortfalls else "fail",
        "floor_profile": floor_profile,
        "non_default_floor": non_default,
        "effective_floors": {
            "per_direction": DEFAULT_PER_DIRECTION_FLOOR,
            "per_genre": per_genre_floors,
        },
        "observed": {
            "per_direction": observed_dir,
            "per_genre": observed_genre,
        },
        "shortfalls_count": len(shortfalls),
        "shortfalls": shortfalls,
    }


# --------------------------------------------------------------- check C4


class _DictionarySurfaceMatcher:
    """Fast matcher over all ``zh`` and ``en`` dictionary surfaces of length >= 2."""

    def __init__(self, terms_db: Path) -> None:
        self._en_by_first_lower: dict[str, list[tuple[str, ...]]] = {}
        self._substr_by_prefix2: dict[str, list[str]] = {}
        if not terms_db.is_file():
            return
        conn = sqlite3.connect(str(terms_db))
        try:
            rows = conn.execute("SELECT DISTINCT zh, en FROM terms").fetchall()
        finally:
            conn.close()
        seen_substr: set[str] = set()
        for zh, en in rows:
            if isinstance(zh, str):
                zh_s = zh.strip()
                if len(zh_s) >= 2 and zh_s not in seen_substr:
                    seen_substr.add(zh_s)
                    self._substr_by_prefix2.setdefault(zh_s[:2], []).append(zh_s)
            if isinstance(en, str):
                en_s = en.strip()
                if len(en_s) < 2:
                    continue
                words = _LATIN_WORD_RE.findall(en_s)
                if (
                    words
                    and sum(len(w) for w in words) >= 2
                    and all(ch.isascii() and (ch.isalpha() or ch.isspace() or ch in "-'") for ch in en_s)
                ):
                    low = tuple(w.casefold() for w in words)
                    bucket = self._en_by_first_lower.setdefault(low[0], [])
                    if low not in bucket:
                        bucket.append(low)
                elif en_s not in seen_substr:
                    seen_substr.add(en_s)
                    self._substr_by_prefix2.setdefault(en_s[:2], []).append(en_s)
        for bucket in self._en_by_first_lower.values():
            bucket.sort(key=len, reverse=True)
        for bucket in self._substr_by_prefix2.values():
            bucket.sort(key=len, reverse=True)

    def term_spans(self, text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        # 1) Substring matches (zh terms and non-Latin terms of length >= 2).
        text_len = len(text)
        for idx in range(text_len - 1):
            candidates = self._substr_by_prefix2.get(text[idx : idx + 2])
            if not candidates:
                continue
            for surface in candidates:
                if text.startswith(surface, idx):
                    spans.append((idx, idx + len(surface)))
                    break
        # 2) English word-boundary matches (length >= 2).
        tokens = [(m.start(), m.end(), m.group().casefold()) for m in _LATIN_WORD_RE.finditer(text)]
        n = len(tokens)
        for i in range(n):
            start_i, _end_i, low_i = tokens[i]
            candidates = self._en_by_first_lower.get(low_i)
            if not candidates:
                continue
            for seq in candidates:
                seq_len = len(seq)
                if i + seq_len > n:
                    continue
                if all(tokens[i + j][2] == seq[j] for j in range(seq_len)):
                    spans.append((start_i, tokens[i + seq_len - 1][1]))
                    break
        return spans


def _spans_overlap(spans: Sequence[tuple[int, int]], start: int, end: int) -> bool:
    for s, e in spans:
        if s < end and e > start:
            return True
    return False


def _load_prior_texts(paths: Sequence[Path]) -> tuple[list[tuple[str, str]], list[str], list[dict[str, Any]]]:
    """Return ([(set_label, text), ...], distinct_set_labels, issues)."""
    entries: list[tuple[str, str]] = []
    sets_seen: list[str] = []
    issues: list[dict[str, Any]] = []
    for path in paths:
        try:
            data = load_json_strict(path, label=path.name)
        except _CheckError as exc:
            issues.append({"code": exc.code, "artifact": path.name})
            continue
        if not isinstance(data, dict) or data.get("schema") != PRIOR_TEXTS_SCHEMA:
            issues.append({"code": "invalid_prior_texts_schema", "artifact": path.name})
            continue
        raw_sets = data.get("sets")
        if not isinstance(raw_sets, list) or not raw_sets:
            issues.append({"code": "empty_prior_texts_sets", "artifact": path.name})
            continue
        for idx, raw_set in enumerate(raw_sets):
            if not isinstance(raw_set, dict):
                issues.append({"code": "invalid_prior_set_entry", "artifact": path.name, "index": idx})
                continue
            set_label = raw_set.get("set")
            raw_texts = raw_set.get("texts")
            if not isinstance(set_label, str) or not set_label or not isinstance(raw_texts, list):
                issues.append({"code": "invalid_prior_set_fields", "artifact": path.name, "index": idx})
                continue
            if set_label not in sets_seen:
                sets_seen.append(set_label)
            for t_idx, item in enumerate(raw_texts):
                if isinstance(item, str):
                    entries.append((set_label, item))
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    entries.append((set_label, item["text"]))
                else:
                    issues.append(
                        {"code": "invalid_prior_text_item", "artifact": path.name, "set": set_label, "index": t_idx}
                    )
    return entries, sets_seen, issues


def _load_m1_name_strings(
    clear_name_paths: Sequence[Path],
    official_names_raw: Any,
    community_names_raw: Any,
) -> tuple[list[str], list[dict[str, Any]]]:
    names: list[str] = []
    issues: list[dict[str, Any]] = []
    for path in clear_name_paths:
        try:
            raw_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            issues.append({"code": "unreadable_clear_names_file", "artifact": path.name})
            continue
        if path.suffix.lower() == ".json":
            try:
                parsed = json.loads(raw_text)
            except ValueError:
                issues.append({"code": "invalid_clear_names_json", "artifact": path.name})
                continue
            items = parsed
            if isinstance(parsed, dict):
                items = parsed.get("names", parsed.get("terms"))
            if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
                issues.append({"code": "invalid_clear_names_structure", "artifact": path.name})
                continue
            names.extend(x for x in items if x)
        else:
            names.extend(line for line in raw_text.splitlines() if line)

    if isinstance(official_names_raw, dict) and isinstance(official_names_raw.get("occurrences"), list):
        for occ in official_names_raw["occurrences"]:
            if not isinstance(occ, dict):
                continue
            if isinstance(occ.get("surface"), str) and occ["surface"]:
                names.append(occ["surface"])
            if isinstance(occ.get("official_forms"), list):
                names.extend(f for f in occ["official_forms"] if isinstance(f, str) and f)

    if isinstance(community_names_raw, dict) and isinstance(community_names_raw.get("occurrences"), list):
        for occ in community_names_raw["occurrences"]:
            if not isinstance(occ, dict):
                continue
            if isinstance(occ.get("surface"), str) and occ["surface"]:
                names.append(occ["surface"])
            for key in ("accepted_forms", "official_forms"):
                if isinstance(occ.get(key), list):
                    names.extend(f for f in occ[key] if isinstance(f, str) and f)
    return names, issues


def _load_m3_upstream_clearances(
    path: Path | None,
    windows_fn: Any,
) -> tuple[set[str], list[dict[str, Any]]]:
    """Return (set of cleared 12-scalar window SHA-256 digests, issues)."""
    cleared_digests: set[str] = set()
    issues: list[dict[str, Any]] = []
    if path is None:
        return cleared_digests, issues
    try:
        data = load_json_strict(path, label=path.name)
    except _CheckError as exc:
        issues.append({"code": exc.code, "artifact": path.name})
        return cleared_digests, issues
    if not isinstance(data, dict) or data.get("schema") != UPSTREAM_CLEARANCES_SCHEMA:
        issues.append({"code": "invalid_upstream_clearances_schema", "artifact": path.name})
        return cleared_digests, issues
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        issues.append({"code": "invalid_upstream_clearances_entries", "artifact": path.name})
        return cleared_digests, issues
    for idx, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            issues.append({"code": "invalid_upstream_clearance_item", "index": idx})
            continue
        classification = entry.get("classification", entry.get("reason"))
        if classification != "upstream_data":
            issues.append({"code": "non_upstream_data_clearance_rejected", "index": idx})
            continue
        win_sha = entry.get("window_sha256")
        win_text = entry.get("text")
        if isinstance(win_sha, str):
            if not _HEX64_RE.match(win_sha):
                issues.append({"code": "invalid_window_sha256", "index": idx})
                continue
            cleared_digests.add(win_sha)
        elif isinstance(win_text, str) and len(win_text) >= WINDOW_SIZE:
            for w in windows_fn(win_text, WINDOW_SIZE):
                cleared_digests.add(sha256_text(w))
        else:
            issues.append({"code": "missing_window_sha256_or_text", "index": idx})
    return cleared_digests, issues


def _extend_shared_span(h3_text: str, i: int, target_text: str, j: int, size: int) -> int:
    """Exact maximal contiguous shared substring length covering ``h3_text[i:i+size] == target_text[j:j+size]``."""
    left = 0
    while i - left > 0 and j - left > 0 and h3_text[i - left - 1] == target_text[j - left - 1]:
        left += 1
    right = size
    len_h3 = len(h3_text)
    len_tgt = len(target_text)
    while i + right < len_h3 and j + right < len_tgt and h3_text[i + right] == target_text[j + right]:
        right += 1
    return left + right


def check_c4(
    *,
    sample_paragraphs: Sequence[SampleParagraph],
    scorer_path: Path,
    expected_scorer_sha256: str,
    prior_entries: Sequence[tuple[str, str]],
    prior_sets: Sequence[str],
    prior_issues: Sequence[dict[str, Any]],
    core_repo: Path,
    core_commit: str,
    clear_name_paths: Sequence[Path],
    official_names_raw: Any,
    community_names_raw: Any,
    upstream_clearances_path: Path | None,
    terms_db: Path,
    m2_threshold: int,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = list(prior_issues)
    actual_scorer_sha = sha256_file(scorer_path) if scorer_path.is_file() else None
    if m2_threshold < WINDOW_SIZE:
        issues.append({"code": "invalid_m2_threshold"})

    try:
        ev, verified_sha = load_verified_scorer(scorer_path, expected_scorer_sha256)
    except _CheckError as exc:
        issues.append({"code": exc.code})
        return {
            "status": "fail",
            "window_size": WINDOW_SIZE,
            "scorer_sha256": actual_scorer_sha,
            "scorer_sha256_expected": expected_scorer_sha256,
            "scorer_sha256_ok": False,
            "core_commit": None,
            "core_blobs_scanned": 0,
            "prior_sets_scanned": list(prior_sets),
            "m2_max_shared_span_threshold": m2_threshold,
            "windows_hit": 0,
            "cleared_counts": {"M1": 0, "M2": 0, "M3": 0},
            "windows_uncleared": 0,
            "max_shared_span_length": 0,
            "uncleared_windows": [],
            "issues_count": len(issues),
            "issues": issues,
        }

    windows_fn = ev.windows
    if getattr(ev, "DEFAULT_WINDOW", WINDOW_SIZE) != WINDOW_SIZE:
        issues.append({"code": "scorer_window_size_mismatch"})

    try:
        resolved_commit, core_blobs = iter_git_commit_text_blobs(core_repo, core_commit)
    except _CheckError as exc:
        issues.append({"code": exc.code})
        resolved_commit, core_blobs = "", []

    m1_names, m1_issues = _load_m1_name_strings(clear_name_paths, official_names_raw, community_names_raw)
    issues.extend(m1_issues)
    m3_digests, m3_issues = _load_m3_upstream_clearances(upstream_clearances_path, windows_fn)
    issues.extend(m3_issues)

    # Index H3 units: (pid, field, text)
    h3_units: list[tuple[str, str, str]] = []
    h3_window_locs: dict[str, list[tuple[int, int]]] = {}
    for para in sample_paragraphs:
        for field_name, text in (("source", para.source), ("reference", para.reference)):
            if not text or len(text) < WINDOW_SIZE:
                continue
            unit_idx = len(h3_units)
            h3_units.append((para.pid, field_name, text))
            for pos, w in enumerate(windows_fn(text, WINDOW_SIZE)):
                h3_window_locs.setdefault(w, []).append((unit_idx, pos))

    h3_windows = set(h3_window_locs)

    # Scan prior texts and Core commit tracked text blobs.
    hit_targets: dict[str, set[str]] = {}
    max_span_by_window: dict[str, int] = {}

    def _scan_target(target_label: str, target_text: str) -> None:
        if len(target_text) < WINDOW_SIZE or not h3_windows:
            return
        # Fast filter: only examine positions whose 12-scalar window exists in H3.
        for j, w in enumerate(windows_fn(target_text, WINDOW_SIZE)):
            if w not in h3_windows:
                continue
            hit_targets.setdefault(w, set()).add(target_label)
            best = max_span_by_window.get(w, WINDOW_SIZE)
            for unit_idx, i in h3_window_locs[w]:
                h3_text = h3_units[unit_idx][2]
                span_len = _extend_shared_span(h3_text, i, target_text, j, WINDOW_SIZE)
                if span_len > best:
                    best = span_len
            max_span_by_window[w] = best

    for set_label, text in prior_entries:
        _scan_target(f"prior:{set_label}", text)
    for blob_text in core_blobs:
        _scan_target("core_tree", blob_text)

    hit_windows = set(hit_targets)
    global_max_span = max(max_span_by_window.values()) if max_span_by_window else 0

    # Build M1 cleared window set (only for hit windows).
    m1_cleared_windows: set[str] = set()
    if hit_windows:
        for name_str in m1_names:
            if len(name_str) < WINDOW_SIZE:
                continue
            for w in windows_fn(name_str, WINDOW_SIZE):
                if w in hit_windows:
                    m1_cleared_windows.add(w)

    # Build dictionary-surface spans (zh and en, length >= 2) only for H3 units with hits.
    dict_matcher = _DictionarySurfaceMatcher(terms_db) if hit_windows else None
    unit_dict_spans: dict[int, list[tuple[int, int]]] = {}

    def _overlaps_dict_surface(window: str) -> bool:
        assert dict_matcher is not None
        if dict_matcher.term_spans(window):
            return True
        for unit_idx, pos in h3_window_locs[window]:
            spans = unit_dict_spans.get(unit_idx)
            if spans is None:
                spans = dict_matcher.term_spans(h3_units[unit_idx][2])
                unit_dict_spans[unit_idx] = spans
            if _spans_overlap(spans, pos, pos + WINDOW_SIZE):
                return True
        return False

    m1_count = 0
    m2_count = 0
    m3_count = 0
    uncleared_records: list[dict[str, Any]] = []

    # Clearance order: M1 (name/term list) -> M2 (generic phrase auto-clear) -> M3 (upstream_data).
    for w in sorted(hit_windows, key=sha256_text):
        digest = sha256_text(w)
        span_len = max_span_by_window[w]
        if w in m1_cleared_windows:
            m1_count += 1
            continue
        # M2: no Unicode Nd digit, no dictionary surface (len >= 2), contiguous max shared span < T.
        m2_ok = (
            span_len < m2_threshold
            and not _has_unicode_nd_digit(w)
            and not _overlaps_dict_surface(w)
        )
        if m2_ok:
            m2_count += 1
            continue
        if digest in m3_digests:
            m3_count += 1
            continue
        pids = sorted({h3_units[u_idx][0] for u_idx, _ in h3_window_locs[w]})
        uncleared_records.append(
            {
                "window_sha256": digest,
                "max_shared_span_length": span_len,
                "targets": sorted(hit_targets[w]),
                "paragraph_ids": pids,
            }
        )

    passed = not issues and len(uncleared_records) == 0
    return {
        "status": "pass" if passed else "fail",
        "window_size": WINDOW_SIZE,
        "scorer_sha256": verified_sha,
        "scorer_sha256_expected": expected_scorer_sha256,
        "scorer_sha256_ok": True,
        "core_commit": resolved_commit or None,
        "core_blobs_scanned": len(core_blobs),
        "prior_sets_scanned": list(prior_sets),
        "m2_max_shared_span_threshold": m2_threshold,
        "windows_hit": len(hit_windows),
        "cleared_counts": {"M1": m1_count, "M2": m2_count, "M3": m3_count},
        "windows_uncleared": len(uncleared_records),
        "max_shared_span_length": global_max_span,
        "uncleared_windows": uncleared_records,
        "issues_count": len(issues),
        "issues": issues,
    }


# --------------------------------------------------------------- check C5


def check_c5(
    *,
    manifest_docs: Sequence[ManifestDoc],
    sample_paragraphs: Sequence[SampleParagraph],
    metadata_by_doc: Mapping[str, DocMetadata],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []

    # 1. Intra-set normalized paragraph deduplication.
    seen_norm: dict[str, str] = {}
    for para in sample_paragraphs:
        norm = normalize_dedup(para.source)
        if not norm:
            continue
        first = seen_norm.get(norm)
        if first is not None:
            issues.append(
                {
                    "code": "duplicate_paragraph",
                    "paragraph_id": para.pid,
                    "first_paragraph_id": first,
                    "norm_sha256": sha256_text(norm),
                }
            )
        else:
            seen_norm[norm] = para.pid

    # 2. Each bilingual pair appears in a single direction only.
    doc_dir = {d.doc_id: d.direction for d in manifest_docs}
    pair_dirs: dict[tuple[str, str], dict[str, list[str]]] = {}

    def _record_pair_key(kind: str, raw_key: str, doc_id: str) -> None:
        norm_key = _normalize_id_token(raw_key)
        if not norm_key:
            return
        direction = doc_dir.get(doc_id)
        if direction is None:
            return
        pair_dirs.setdefault((kind, norm_key), {}).setdefault(direction, []).append(doc_id)

    for doc_id, meta in metadata_by_doc.items():
        if meta.bilingual_pair_id:
            _record_pair_key("bilingual_pair_id", meta.bilingual_pair_id, doc_id)
        if meta.source_id:
            _record_pair_key("source_id", meta.source_id, doc_id)
        for tm_key in meta.textmap_keys:
            _record_pair_key("textmap_key", tm_key, doc_id)

    for (kind, norm_key), by_dir in sorted(pair_dirs.items()):
        if len(by_dir) > 1:
            issues.append(
                {
                    "code": "bilingual_pair_both_directions",
                    "key_kind": kind,
                    "key_sha256": sha256_text(norm_key),
                    "doc_ids_by_direction": {d: sorted(ids) for d, ids in sorted(by_dir.items())},
                }
            )

    # Cross-direction text-level reference == source collision check.
    source_by_dir: dict[str, dict[str, str]] = {d: {} for d in DIRECTIONS}
    for para in sample_paragraphs:
        norm = normalize_dedup(para.source)
        if norm:
            source_by_dir[para.direction].setdefault(norm, para.pid)
    for para in sample_paragraphs:
        if not para.reference:
            continue
        ref_norm = normalize_dedup(para.reference)
        if not ref_norm:
            continue
        other_dir = "en->zh" if para.direction == "zh->en" else "zh->en"
        other_pid = source_by_dir[other_dir].get(ref_norm)
        if other_pid is not None:
            issues.append(
                {
                    "code": "bilingual_reference_source_collision",
                    "paragraph_id": para.pid,
                    "other_paragraph_id": other_pid,
                    "norm_sha256": sha256_text(ref_norm),
                }
            )

    return {
        "status": "pass" if not issues else "fail",
        "paragraphs_checked": len(sample_paragraphs),
        "issues_count": len(issues),
        "issues": issues,
    }


# --------------------------------------------------------------- check C6


def check_c6(
    *,
    manifest_docs: Sequence[ManifestDoc],
    metadata_by_doc: Mapping[str, DocMetadata],
    prior_ids_path: Path,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    try:
        data = load_json_strict(prior_ids_path, label=prior_ids_path.name)
    except _CheckError as exc:
        issues.append({"code": exc.code, "artifact": prior_ids_path.name})
        return {"status": "fail", "documents_checked": 0, "issues_count": len(issues), "issues": issues}

    if not isinstance(data, dict) or data.get("schema") != PRIOR_IDS_SCHEMA:
        issues.append({"code": "invalid_prior_ids_schema", "artifact": prior_ids_path.name})
        return {"status": "fail", "documents_checked": 0, "issues_count": len(issues), "issues": issues}

    prior_sets: dict[str, set[str]] = {}
    for field in ("source_ids", "textmap_keys", "series", "authors"):
        raw_list = data.get(field)
        if not isinstance(raw_list, list) or not all(isinstance(x, str) for x in raw_list):
            issues.append({"code": "invalid_prior_ids_field", "field": field})
            prior_sets[field] = set()
        else:
            prior_sets[field] = {_normalize_id_token(x) for x in raw_list if x.strip()}

    if not manifest_docs:
        issues.append({"code": "empty_manifest_docs"})
    for doc in manifest_docs:
        meta = metadata_by_doc.get(doc.doc_id)
        if meta is None:
            issues.append({"code": "missing_doc_metadata", "doc_id": doc.doc_id})
            continue
        checks: list[tuple[str, str, tuple[str, ...]]] = [
            ("source_id", "source_ids", (meta.source_id,) if meta.source_id else ()),
            ("textmap_key", "textmap_keys", meta.textmap_keys),
            ("series", "series", (meta.series,) if meta.series else ()),
            ("author", "authors", (meta.author,) if meta.author else ()),
        ]
        for singular, plural, values in checks:
            for val in values:
                norm = _normalize_id_token(val)
                if norm and norm in prior_sets[plural]:
                    issues.append(
                        {
                            "code": "prior_id_overlap",
                            "doc_id": doc.doc_id,
                            "field": singular,
                            "value_sha256": sha256_text(norm),
                        }
                    )

    return {
        "status": "pass" if not issues else "fail",
        "documents_checked": len(manifest_docs),
        "prior_counts": {k: len(v) for k, v in prior_sets.items()},
        "issues_count": len(issues),
        "issues": issues,
    }


# --------------------------------------------------------------- check C7


def _has_version_ge_3_8(text: str) -> bool:
    norm = unicodedata.normalize("NFKC", text)

    def _is_forbidden(major: int, minor: int) -> bool:
        return (major, minor) >= MIN_FORBIDDEN_VERSION

    for pattern in (_TAGGED_VERSION_BEFORE_RE, _TAGGED_VERSION_AFTER_RE):
        for match in pattern.finditer(norm):
            major, minor = int(match.group(1)), int(match.group(2))
            if _is_forbidden(major, minor):
                return True

    for match in _SEMVER_THREE_PART_RE.finditer(norm):
        major, minor = int(match.group(1)), int(match.group(2))
        if major in (3, 4, 5, 6, 7, 8, 9) and _is_forbidden(major, minor):
            return True

    for match in _BARE_VERSION_RE.finditer(norm):
        major, minor = int(match.group(1)), int(match.group(2))
        if not (3 <= major <= 9 and _is_forbidden(major, minor)):
            continue
        tail = norm[match.end() :]
        if _UNIT_AFTER_NUMBER_RE.match(tail):
            continue
        return True

    return False


def check_c7(
    *,
    manifest_docs: Sequence[ManifestDoc],
    sample_paragraphs: Sequence[SampleParagraph],
    metadata_by_doc: Mapping[str, DocMetadata],
    cutoff_iso: str,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    try:
        cutoff_dt = _dt.datetime.fromisoformat(cutoff_iso)
        if cutoff_dt.tzinfo is None:
            raise ValueError("naive cutoff")
    except ValueError:
        issues.append({"code": "invalid_cutoff_timestamp"})
        return {"status": "fail", "cutoff": cutoff_iso, "issues_count": len(issues), "issues": issues}

    if not manifest_docs:
        issues.append({"code": "empty_manifest_docs"})
    for doc in manifest_docs:
        meta = metadata_by_doc.get(doc.doc_id)
        if meta is None or not meta.published:
            issues.append({"code": "missing_published_timestamp", "doc_id": doc.doc_id})
        else:
            try:
                pub_dt = _dt.datetime.fromisoformat(meta.published)
            except ValueError:
                issues.append({"code": "invalid_published_timestamp", "doc_id": doc.doc_id})
            else:
                if pub_dt.tzinfo is None:
                    issues.append({"code": "naive_published_timestamp", "doc_id": doc.doc_id})
                elif pub_dt > cutoff_dt:
                    issues.append(
                        {
                            "code": "published_after_cutoff",
                            "doc_id": doc.doc_id,
                            "published": meta.published,
                        }
                    )

        if _has_version_ge_3_8(doc.text):
            issues.append({"code": "version_ge_3_8_in_document", "doc_id": doc.doc_id})

    for para in sample_paragraphs:
        if _has_version_ge_3_8(para.source):
            issues.append({"code": "version_ge_3_8_in_source", "doc_id": para.doc_id, "paragraph_id": para.pid})
        if para.reference and _has_version_ge_3_8(para.reference):
            issues.append({"code": "version_ge_3_8_in_reference", "doc_id": para.doc_id, "paragraph_id": para.pid})

    return {
        "status": "pass" if not issues else "fail",
        "cutoff": cutoff_iso,
        "min_forbidden_version": "3.8",
        "documents_checked": len(manifest_docs),
        "paragraphs_checked": len(sample_paragraphs),
        "issues_count": len(issues),
        "issues": issues,
    }


# --------------------------------------------------------------- check C8


def _scan_pii_categories(text: str) -> list[str]:
    norm = unicodedata.normalize("NFKC", text)
    found: list[str] = []
    for category, pattern in _PII_PATTERNS:
        if pattern.search(text) or pattern.search(norm):
            if category not in found:
                found.append(category)
    return found


def check_c8(
    *,
    manifest_docs: Sequence[ManifestDoc],
    sample_paragraphs: Sequence[SampleParagraph],
) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    by_category: Counter[str] = Counter()

    for doc in manifest_docs:
        for cat in _scan_pii_categories(doc.text):
            by_category[cat] += 1
            hits.append({"code": "pii_detected", "category": cat, "scope": "manifest", "doc_id": doc.doc_id})

    for para in sample_paragraphs:
        for field_name, text in (("source", para.source), ("reference", para.reference)):
            if not text:
                continue
            for cat in _scan_pii_categories(text):
                by_category[cat] += 1
                hits.append(
                    {
                        "code": "pii_detected",
                        "category": cat,
                        "scope": field_name,
                        "doc_id": para.doc_id,
                        "paragraph_id": para.pid,
                    }
                )

    return {
        "status": "pass" if not hits else "fail",
        "hits_count": len(hits),
        "hits_by_category": dict(sorted(by_category.items())),
        "hits": hits,
    }


# --------------------------------------------------------------- check C9


def _validate_annotation_file(
    *,
    raw_data: Any,
    expected_schema: str,
    kind: str,
    paragraphs_by_id: Mapping[str, SampleParagraph],
    scored_ids: set[str],
    seen_occs: set[str],
    seen_spans: dict[tuple[str, int, int], str],
) -> tuple[int, int, list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(raw_data, dict) or raw_data.get("schema") != expected_schema:
        issues.append({"code": "invalid_annotation_schema", "kind": kind})
        return 0, 0, issues

    raw_occs = raw_data.get("occurrences")
    if not isinstance(raw_occs, list):
        issues.append({"code": "invalid_occurrences_array", "kind": kind})
        return 0, 0, issues

    occ_paragraphs: set[str] = set()
    valid_occs = 0
    for idx, raw in enumerate(raw_occs):
        if not isinstance(raw, dict):
            issues.append({"code": "invalid_occurrence_object", "kind": kind, "index": idx})
            continue
        if kind == "official":
            req_keys = ("occ", "paragraph", "start", "end", "surface", "name_id", "official_forms")
            if any(k not in raw for k in req_keys):
                issues.append({"code": "missing_official_occurrence_key", "kind": kind, "index": idx})
                continue
        else:
            req_keys = ("occ", "paragraph", "start", "end", "surface", "term_id", "accepted_forms")
            allowed_keys = set(req_keys) | {"official_forms"}
            if any(k not in raw for k in req_keys) or not set(raw) <= allowed_keys:
                issues.append({"code": "invalid_community_occurrence_keys", "kind": kind, "index": idx})
                continue

        occ_id = raw["occ"]
        pid = raw["paragraph"]
        start = raw["start"]
        end = raw["end"]
        surface = raw["surface"]

        if not isinstance(occ_id, str) or not occ_id:
            issues.append({"code": "invalid_occ_id", "kind": kind, "index": idx})
            continue
        if occ_id in seen_occs:
            issues.append({"code": "duplicate_occ_id", "kind": kind, "occ": occ_id})
            continue
        seen_occs.add(occ_id)

        if not isinstance(pid, str) or pid not in paragraphs_by_id:
            issues.append({"code": "unknown_paragraph", "kind": kind, "occ": occ_id})
            continue
        para = paragraphs_by_id[pid]
        if not para.scored:
            issues.append({"code": "unscored_paragraph_annotated", "kind": kind, "occ": occ_id, "paragraph_id": pid})
            continue

        if type(start) is not int or type(end) is not int or not (0 <= start < end <= len(para.source)):
            issues.append({"code": "invalid_span_bounds", "kind": kind, "occ": occ_id, "paragraph_id": pid})
            continue
        if not isinstance(surface, str) or surface != para.source[start:end]:
            issues.append({"code": "surface_span_mismatch", "kind": kind, "occ": occ_id, "paragraph_id": pid})
            continue

        if kind == "official":
            if not isinstance(raw["name_id"], str) or not raw["name_id"]:
                issues.append({"code": "invalid_name_id", "kind": kind, "occ": occ_id})
                continue
            forms = raw["official_forms"]
            if not isinstance(forms, list) or not forms or not all(isinstance(f, str) and f for f in forms):
                issues.append({"code": "invalid_official_forms", "kind": kind, "occ": occ_id})
                continue
        else:
            if not isinstance(raw["term_id"], str) or not raw["term_id"]:
                issues.append({"code": "invalid_term_id", "kind": kind, "occ": occ_id})
                continue
            forms_ok = True
            for key in ("accepted_forms", "official_forms"):
                if key in raw:
                    forms = raw[key]
                    if not isinstance(forms, list) or not forms or not all(isinstance(f, str) and f for f in forms):
                        issues.append({"code": f"invalid_{key}", "kind": kind, "occ": occ_id})
                        forms_ok = False
            if not forms_ok:
                continue

        span_key = (pid, start, end)
        prev_occ = seen_spans.get(span_key)
        if prev_occ is not None:
            issues.append(
                {
                    "code": "duplicate_span_across_annotations",
                    "kind": kind,
                    "occ": occ_id,
                    "previous_occ": prev_occ,
                    "paragraph_id": pid,
                }
            )
            continue
        seen_spans[span_key] = occ_id
        occ_paragraphs.add(pid)
        valid_occs += 1

    # Coverage of all scored units.
    if "annotated_paragraphs" in raw_data:
        raw_cov = raw_data["annotated_paragraphs"]
        if not isinstance(raw_cov, list) or not all(isinstance(x, str) for x in raw_cov):
            issues.append({"code": "invalid_annotated_paragraphs", "kind": kind})
            covered: set[str] = set()
        else:
            if len(set(raw_cov)) != len(raw_cov):
                issues.append({"code": "duplicate_annotated_paragraph", "kind": kind})
            covered = set(raw_cov)
            for pid in sorted(covered - scored_ids):
                issues.append({"code": "non_scored_in_annotated_paragraphs", "kind": kind, "paragraph_id": pid})
            for pid in sorted(occ_paragraphs - covered):
                issues.append({"code": "occurrence_outside_annotated_paragraphs", "kind": kind, "paragraph_id": pid})
    else:
        covered = occ_paragraphs

    missing_scored = sorted(scored_ids - covered)
    if missing_scored:
        issues.append(
            {
                "code": "incomplete_scored_unit_coverage",
                "kind": kind,
                "missing_count": len(missing_scored),
                "missing_paragraph_ids_sample": missing_scored[:20],
            }
        )
    return valid_occs, len(covered & scored_ids), issues


def check_c9(
    *,
    sample_paragraphs: Sequence[SampleParagraph],
    official_names_raw: Any,
    official_load_error: str | None,
    community_names_raw: Any,
    community_load_error: str | None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if official_load_error is not None:
        issues.append({"code": official_load_error, "kind": "official"})
    if community_load_error is not None:
        issues.append({"code": community_load_error, "kind": "community"})

    paragraphs_by_id = {p.pid: p for p in sample_paragraphs}
    scored_ids = {p.pid for p in sample_paragraphs if p.scored}
    seen_occs: set[str] = set()
    seen_spans: dict[tuple[str, int, int], str] = {}

    off_occs = off_cov = 0
    if official_load_error is None:
        off_occs, off_cov, off_issues = _validate_annotation_file(
            raw_data=official_names_raw,
            expected_schema=OFFICIAL_NAMES_SCHEMA,
            kind="official",
            paragraphs_by_id=paragraphs_by_id,
            scored_ids=scored_ids,
            seen_occs=seen_occs,
            seen_spans=seen_spans,
        )
        issues.extend(off_issues)

    com_occs = com_cov = 0
    if community_load_error is None:
        com_occs, com_cov, com_issues = _validate_annotation_file(
            raw_data=community_names_raw,
            expected_schema=COMMUNITY_NAMES_SCHEMA,
            kind="community",
            paragraphs_by_id=paragraphs_by_id,
            scored_ids=scored_ids,
            seen_occs=seen_occs,
            seen_spans=seen_spans,
        )
        issues.extend(com_issues)

    return {
        "status": "pass" if not issues else "fail",
        "scored_units_total": len(scored_ids),
        "official_occurrences": off_occs,
        "official_covered_scored_units": off_cov,
        "community_occurrences": com_occs,
        "community_covered_scored_units": com_cov,
        "issues_count": len(issues),
        "issues": issues,
    }


# --------------------------------------------------------------- check C10


def _file_binding(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"name": path.name, "sha256": None}
    return {"name": path.name, "sha256": sha256_file(path)}


def build_bindings_and_c10(
    *,
    bound_tree: Path,
    core_repo: Path,
    core_commit: str,
    expected_core_commit: str,
    expected_src_tree: str,
    expected_matcher_revision: str,
    terms_db: Path,
    expected_terms_sha256: str,
    scorer_path: Path,
    expected_scorer_sha256: str,
    manifest_path: Path,
    sample_path: Path,
    names_path: Path,
    community_names_path: Path,
    metadata_path: Path | None,
    prior_ids_path: Path,
    prior_text_paths: Sequence[Path],
    clear_name_paths: Sequence[Path],
    upstream_clearances_path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any], BoundCore | None]:
    issues: list[dict[str, Any]] = []

    # Bound tree git verification.
    rc_head, out_head, _ = _git(bound_tree, "rev-parse", "HEAD")
    bound_head = out_head.decode("utf-8").strip() if rc_head == 0 else None
    if bound_head is None:
        issues.append({"code": "bound_tree_head_failed"})

    rc_src, out_src, _ = _git(bound_tree, "rev-parse", "HEAD:src")
    bound_src_sha = out_src.decode("utf-8").strip() if rc_src == 0 else None
    if bound_src_sha is None:
        issues.append({"code": "bound_tree_src_rev_parse_failed"})
    elif bound_src_sha != expected_src_tree:
        issues.append(
            {
                "code": "bound_tree_src_sha_mismatch",
                "actual": bound_src_sha,
                "expected": expected_src_tree,
            }
        )

    rc_st, out_st, _ = _git(bound_tree, "status", "--porcelain", "--untracked-files=all", "--", "src", "pyproject.toml")
    if rc_st != 0 or out_st.strip():
        issues.append({"code": "bound_tree_dirty"})

    # Core commit verification.
    rc_cc, out_cc, _ = _git(core_repo, "rev-parse", "--verify", f"{core_commit}^{{commit}}")
    resolved_core_commit = out_cc.decode("utf-8").strip() if rc_cc == 0 else None
    if resolved_core_commit is None:
        issues.append({"code": "core_commit_unresolvable"})
    elif resolved_core_commit != expected_core_commit:
        issues.append(
            {
                "code": "core_commit_mismatch",
                "actual": resolved_core_commit,
                "expected": expected_core_commit,
            }
        )

    # Import bound Core (only if src dir exists).
    core: BoundCore | None = None
    actual_matcher_rev: str | None = None
    try:
        core = import_bound_core(bound_tree)
        actual_matcher_rev = core.matcher_revision
        if actual_matcher_rev != expected_matcher_revision:
            issues.append(
                {
                    "code": "matcher_revision_mismatch",
                    "actual": actual_matcher_rev,
                    "expected": expected_matcher_revision,
                }
            )
    except _CheckError as exc:
        issues.append({"code": exc.code})

    # terms.db SHA-256 verification.
    terms_sha = sha256_file(terms_db) if terms_db.is_file() else None
    if terms_sha is None:
        issues.append({"code": "terms_db_missing"})
    elif terms_sha != expected_terms_sha256:
        issues.append(
            {
                "code": "terms_db_sha256_mismatch",
                "actual": terms_sha,
                "expected": expected_terms_sha256,
            }
        )

    # Tool files SHA-256.
    scorer_sha = sha256_file(scorer_path) if scorer_path.is_file() else None
    if scorer_sha is None:
        issues.append({"code": "scorer_file_missing"})
    elif scorer_sha != expected_scorer_sha256:
        issues.append(
            {
                "code": "scorer_sha256_mismatch",
                "actual": scorer_sha,
                "expected": expected_scorer_sha256,
            }
        )

    tools_sha256: dict[str, str | None] = {
        "scripts/eval_h3_corpus.py": sha256_file(SELF_PATH),
        "scripts/eval_usability.py": scorer_sha,
        "scripts/eval_h3.py": sha256_file(ROOT / "scripts" / "eval_h3.py")
        if (ROOT / "scripts" / "eval_h3.py").is_file()
        else None,
        "scripts/eval_h3_export.py": sha256_file(ROOT / "scripts" / "eval_h3_export.py")
        if (ROOT / "scripts" / "eval_h3_export.py").is_file()
        else None,
    }

    inputs_sha256: dict[str, Any] = {
        "manifest": _file_binding(manifest_path),
        "sample": _file_binding(sample_path),
        "names": _file_binding(names_path),
        "community_names": _file_binding(community_names_path),
        "metadata": _file_binding(metadata_path) if metadata_path is not None else None,
        "prior_ids": _file_binding(prior_ids_path),
        "prior_texts": [_file_binding(p) for p in prior_text_paths],
        "clear_names": [_file_binding(p) for p in clear_name_paths],
        "upstream_clearances": (
            _file_binding(upstream_clearances_path) if upstream_clearances_path is not None else None
        ),
    }

    for key in ("manifest", "sample", "names", "community_names", "prior_ids"):
        if inputs_sha256[key]["sha256"] is None:
            issues.append({"code": "missing_input_file", "input": key})
    if not inputs_sha256["prior_texts"] or any(x["sha256"] is None for x in inputs_sha256["prior_texts"]):
        issues.append({"code": "missing_input_file", "input": "prior_texts"})
    if metadata_path is not None and inputs_sha256["metadata"]["sha256"] is None:
        issues.append({"code": "missing_input_file", "input": "metadata"})
    if any(x["sha256"] is None for x in inputs_sha256["clear_names"]):
        issues.append({"code": "missing_input_file", "input": "clear_names"})
    if upstream_clearances_path is not None and inputs_sha256["upstream_clearances"]["sha256"] is None:
        issues.append({"code": "missing_input_file", "input": "upstream_clearances"})

    bindings: dict[str, Any] = {
        "core_commit": resolved_core_commit,
        "expected_core_commit": expected_core_commit,
        "bound_tree_commit": bound_head,
        "bound_tree_src_sha": bound_src_sha,
        "expected_src_tree": expected_src_tree,
        "matcher_revision": actual_matcher_rev,
        "expected_matcher_revision": expected_matcher_revision,
        "terms_db_sha256": terms_sha,
        "expected_terms_sha256": expected_terms_sha256,
        "tools_sha256": tools_sha256,
        "inputs_sha256": inputs_sha256,
    }
    c10 = {
        "status": "pass" if not issues else "fail",
        "bindings_complete": not issues,
        "issues_count": len(issues),
        "issues": issues,
    }
    return bindings, c10, core


# ---------------------------------------------- public receipt validator API


def validate_acceptance_receipt(
    receipt_dict: Any,
    *,
    expected_manifest_sha256: str | None = None,
    expected_sample_sha256: str | None = None,
    expected_names_sha256: str | None = None,
    expected_community_names_sha256: str | None = None,
    expected_metadata_sha256: str | None = None,
    expected_src_tree: str | None = None,
    expected_core_commit: str | None = None,
    expected_terms_sha256: str | None = None,
    expected_matcher_revision: str | None = None,
    expected_scorer_sha256: str | None = PINNED_SCORER_SHA256,
    allow_non_default_floor: bool = False,
    require_pass: bool = True,
) -> dict[str, Any]:
    """Validate an ``h3-acceptance-v1`` receipt dict for reuse by ``eval_h3.py``.

    Raises ``ReceiptValidationError`` (a ``ValueError`` subclass) when any
    structural, integrity, hash, floor-profile, or verdict constraint fails.
    Returns ``receipt_dict`` unchanged on success.
    """
    if not isinstance(receipt_dict, dict):
        raise ReceiptValidationError("receipt must be a JSON object")
    if receipt_dict.get("schema") != RECEIPT_SCHEMA:
        raise ReceiptValidationError(f"receipt schema must be {RECEIPT_SCHEMA!r}")

    verdict = receipt_dict.get("verdict")
    if verdict not in ("pass", "fail"):
        raise ReceiptValidationError("receipt verdict must be 'pass' or 'fail'")

    failed_rules = receipt_dict.get("failed_rules")
    if (
        not isinstance(failed_rules, list)
        or not all(isinstance(r, str) and r in ALL_RULES for r in failed_rules)
        or len(set(failed_rules)) != len(failed_rules)
    ):
        raise ReceiptValidationError("failed_rules must be a list of unique rule ids C1..C10")

    if (verdict == "pass") != (len(failed_rules) == 0):
        raise ReceiptValidationError("verdict disagrees with failed_rules")

    checks = receipt_dict.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(ALL_RULES):
        raise ReceiptValidationError("checks must contain exact keys C1..C10")

    derived_failed: list[str] = []
    for rule in ALL_RULES:
        block = checks[rule]
        if not isinstance(block, dict) or block.get("status") not in ("pass", "fail"):
            raise ReceiptValidationError(f"checks[{rule!r}].status must be 'pass' or 'fail'")
        if block["status"] != "pass":
            derived_failed.append(rule)
    if sorted(failed_rules) != sorted(derived_failed):
        raise ReceiptValidationError("failed_rules does not match per-check statuses")

    bindings = receipt_dict.get("bindings")
    if not isinstance(bindings, dict):
        raise ReceiptValidationError("bindings must be an object")
    declared_bindings_sha = receipt_dict.get("bindings_sha256")
    computed_bindings_sha = sha256_bytes(canonical(bindings))
    if not isinstance(declared_bindings_sha, str) or declared_bindings_sha != computed_bindings_sha:
        raise ReceiptValidationError("bindings_sha256 does not match canonical bindings")

    for sha40_key in ("core_commit", "bound_tree_src_sha"):
        val = bindings.get(sha40_key)
        if require_pass and (not isinstance(val, str) or not _HEX40_RE.match(val)):
            raise ReceiptValidationError(f"bindings.{sha40_key} must be a 40-hex git SHA")
    for sha64_key in ("terms_db_sha256", "matcher_revision"):
        val = bindings.get(sha64_key)
        if require_pass and (not isinstance(val, str) or not _HEX64_RE.match(val)):
            raise ReceiptValidationError(f"bindings.{sha64_key} must be a 64-hex SHA-256")

    tools_sha = bindings.get("tools_sha256")
    if not isinstance(tools_sha, dict):
        raise ReceiptValidationError("bindings.tools_sha256 must be an object")
    for tool_key in ("scripts/eval_h3_corpus.py", "scripts/eval_usability.py"):
        tval = tools_sha.get(tool_key)
        if require_pass and (not isinstance(tval, str) or not _HEX64_RE.match(tval)):
            raise ReceiptValidationError(f"bindings.tools_sha256[{tool_key!r}] must be a 64-hex SHA-256")

    inputs_sha = bindings.get("inputs_sha256")
    if not isinstance(inputs_sha, dict):
        raise ReceiptValidationError("bindings.inputs_sha256 must be an object")
    for inp_key in ("manifest", "sample", "names", "community_names", "prior_ids"):
        entry = inputs_sha.get(inp_key)
        if not isinstance(entry, dict):
            raise ReceiptValidationError(f"bindings.inputs_sha256[{inp_key!r}] must be an object")
        if require_pass and (not isinstance(entry.get("sha256"), str) or not _HEX64_RE.match(entry["sha256"])):
            raise ReceiptValidationError(f"bindings.inputs_sha256[{inp_key!r}].sha256 must be a 64-hex SHA-256")
    prior_texts_list = inputs_sha.get("prior_texts")
    if not isinstance(prior_texts_list, list) or (require_pass and not prior_texts_list):
        raise ReceiptValidationError("bindings.inputs_sha256.prior_texts must be a non-empty list")
    for idx, entry in enumerate(prior_texts_list):
        if not isinstance(entry, dict) or (
            require_pass and (not isinstance(entry.get("sha256"), str) or not _HEX64_RE.match(entry["sha256"]))
        ):
            raise ReceiptValidationError(f"bindings.inputs_sha256.prior_texts[{idx}] has invalid sha256")

    # C4 invariants.
    c4 = checks["C4"]
    if require_pass:
        if c4.get("windows_uncleared") != 0:
            raise ReceiptValidationError("C4 windows_uncleared must be 0 for a passing receipt")
        if c4.get("scorer_sha256_ok") is not True:
            raise ReceiptValidationError("C4 scorer_sha256_ok must be true for a passing receipt")

    # C3 floor-profile check.
    c3 = checks["C3"]
    if require_pass and not allow_non_default_floor:
        if c3.get("non_default_floor") is not False or c3.get("floor_profile") != "default":
            raise ReceiptValidationError("non-default C3 floor_profile requires allow_non_default_floor=True")

    if require_pass and verdict != "pass":
        raise ReceiptValidationError(f"receipt verdict is 'fail' (failed_rules={failed_rules})")

    # Expected binding checks.
    def _check_expected(label: str, actual: Any, expected: str | None) -> None:
        if expected is not None and actual != expected:
            raise ReceiptValidationError(f"{label} mismatch")

    _check_expected("manifest_sha256", inputs_sha["manifest"].get("sha256"), expected_manifest_sha256)
    _check_expected("sample_sha256", inputs_sha["sample"].get("sha256"), expected_sample_sha256)
    _check_expected("names_sha256", inputs_sha["names"].get("sha256"), expected_names_sha256)
    _check_expected(
        "community_names_sha256", inputs_sha["community_names"].get("sha256"), expected_community_names_sha256
    )
    if expected_metadata_sha256 is not None:
        meta_entry = inputs_sha.get("metadata")
        actual_meta = meta_entry.get("sha256") if isinstance(meta_entry, dict) else None
        _check_expected("metadata_sha256", actual_meta, expected_metadata_sha256)
    _check_expected("bound_tree_src_sha", bindings.get("bound_tree_src_sha"), expected_src_tree)
    _check_expected("core_commit", bindings.get("core_commit"), expected_core_commit)
    _check_expected("terms_db_sha256", bindings.get("terms_db_sha256"), expected_terms_sha256)
    _check_expected("matcher_revision", bindings.get("matcher_revision"), expected_matcher_revision)
    _check_expected("scorer_sha256", tools_sha.get("scripts/eval_usability.py"), expected_scorer_sha256)

    return receipt_dict


# --------------------------------------------------------------- orchestrator


def run_acceptance(
    *,
    manifest_path: Path,
    sample_path: Path,
    names_path: Path,
    community_names_path: Path,
    metadata_path: Path | None,
    prior_text_paths: Sequence[Path],
    prior_ids_path: Path,
    bound_tree: Path,
    core_repo: Path,
    core_commit: str,
    terms_db: Path,
    expected_core_commit: str = PINNED_CORE_COMMIT,
    expected_src_tree: str = PINNED_SRC_TREE,
    expected_matcher_revision: str = PINNED_MATCHER_REVISION,
    expected_terms_sha256: str = PINNED_TERMS_DB_SHA256,
    scorer_path: Path = SCORER_PATH,
    expected_scorer_sha256: str = PINNED_SCORER_SHA256,
    clear_name_paths: Sequence[Path] = (),
    upstream_clearances_path: Path | None = None,
    announcement_floor: int | None = None,
    m2_max_shared_span: int = DEFAULT_M2_MAX_SHARED_SPAN,
    cutoff_iso: str = DEFAULT_CUTOFF_ISO,
) -> dict[str, Any]:
    bindings, c10, core = build_bindings_and_c10(
        bound_tree=bound_tree,
        core_repo=core_repo,
        core_commit=core_commit,
        expected_core_commit=expected_core_commit,
        expected_src_tree=expected_src_tree,
        expected_matcher_revision=expected_matcher_revision,
        terms_db=terms_db,
        expected_terms_sha256=expected_terms_sha256,
        scorer_path=scorer_path,
        expected_scorer_sha256=expected_scorer_sha256,
        manifest_path=manifest_path,
        sample_path=sample_path,
        names_path=names_path,
        community_names_path=community_names_path,
        metadata_path=metadata_path,
        prior_ids_path=prior_ids_path,
        prior_text_paths=prior_text_paths,
        clear_name_paths=clear_name_paths,
        upstream_clearances_path=upstream_clearances_path,
    )

    manifest_label, manifest_docs, manifest_issues = _load_manifest_data(manifest_path)
    sample_label, declared_manifest_sha, sample_paragraphs, sample_raw, sample_issues = _load_sample_data(sample_path)
    metadata_by_doc, metadata_issues = _load_metadata(metadata_path, sample_raw)

    prior_entries, prior_sets, prior_issues = _load_prior_texts(prior_text_paths)
    prior_norm_set = {norm for _, text in prior_entries if (norm := normalize_dedup(text))}

    official_raw: Any = None
    official_err: str | None = None
    try:
        official_raw = load_json_strict(names_path, label=names_path.name)
    except _CheckError as exc:
        official_err = exc.code

    community_raw: Any = None
    community_err: str | None = None
    try:
        community_raw = load_json_strict(community_names_path, label=community_names_path.name)
    except _CheckError as exc:
        community_err = exc.code

    manifest_sha = bindings["inputs_sha256"]["manifest"]["sha256"] or ""
    sample_sha = bindings["inputs_sha256"]["sample"]["sha256"] or ""

    c1 = check_c1(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        manifest_label=manifest_label,
        manifest_docs=manifest_docs,
        manifest_issues=manifest_issues,
        sample_path=sample_path,
        sample_sha256=sample_sha,
        sample_label=sample_label,
        declared_manifest_sha256=declared_manifest_sha,
        sample_paragraphs=sample_paragraphs,
        sample_issues=sample_issues,
        metadata_by_doc=metadata_by_doc,
        metadata_issues=metadata_issues,
        has_metadata_file=metadata_path is not None,
        core=core,
    )
    c2 = check_c2(
        sample_paragraphs=sample_paragraphs,
        prior_normalized_paragraphs=prior_norm_set,
        core=core,
        terms_db=terms_db,
    )
    c3 = check_c3(
        sample_paragraphs=sample_paragraphs,
        announcement_floor=announcement_floor,
    )
    c4 = check_c4(
        sample_paragraphs=sample_paragraphs,
        scorer_path=scorer_path,
        expected_scorer_sha256=expected_scorer_sha256,
        prior_entries=prior_entries,
        prior_sets=prior_sets,
        prior_issues=prior_issues,
        core_repo=core_repo,
        core_commit=core_commit,
        clear_name_paths=clear_name_paths,
        official_names_raw=official_raw,
        community_names_raw=community_raw,
        upstream_clearances_path=upstream_clearances_path,
        terms_db=terms_db,
        m2_threshold=m2_max_shared_span,
    )
    c5 = check_c5(
        manifest_docs=manifest_docs,
        sample_paragraphs=sample_paragraphs,
        metadata_by_doc=metadata_by_doc,
    )
    c6 = check_c6(
        manifest_docs=manifest_docs,
        metadata_by_doc=metadata_by_doc,
        prior_ids_path=prior_ids_path,
    )
    c7 = check_c7(
        manifest_docs=manifest_docs,
        sample_paragraphs=sample_paragraphs,
        metadata_by_doc=metadata_by_doc,
        cutoff_iso=cutoff_iso,
    )
    c8 = check_c8(
        manifest_docs=manifest_docs,
        sample_paragraphs=sample_paragraphs,
    )
    c9 = check_c9(
        sample_paragraphs=sample_paragraphs,
        official_names_raw=official_raw,
        official_load_error=official_err,
        community_names_raw=community_raw,
        community_load_error=community_err,
    )

    checks = {
        "C1": c1,
        "C2": c2,
        "C3": c3,
        "C4": c4,
        "C5": c5,
        "C6": c6,
        "C7": c7,
        "C8": c8,
        "C9": c9,
        "C10": c10,
    }
    failed_rules = [rule for rule in ALL_RULES if checks[rule]["status"] != "pass"]
    verdict = "pass" if not failed_rules else "fail"
    return {
        "schema": RECEIPT_SCHEMA,
        "created_at": now_iso(),
        "verdict": verdict,
        "failed_rules": failed_rules,
        "bindings": bindings,
        "bindings_sha256": sha256_bytes(canonical(bindings)),
        "checks": checks,
    }


# ------------------------------------------------------------------------ cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_h3_corpus.py",
        description="H3 corpus acceptance gate (C1-C10) and receipt validator.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    acc = sub.add_parser("accept", help="run mechanical checks C1-C10 and emit h3-acceptance-v1 receipt")
    acc.add_argument("--manifest", required=True, metavar="MANIFEST.json", help="h3-manifest-v1 file")
    acc.add_argument("--sample", required=True, metavar="SAMPLE.json", help="usability-sample-v1 file")
    acc.add_argument("--names", required=True, metavar="NAMES.json", help="usability-names-v1 file")
    acc.add_argument(
        "--community-names", required=True, metavar="COMMUNITY.json", help="h3-community-names-v1 file"
    )
    acc.add_argument("--metadata", metavar="METADATA.json", help="h3-corpus-metadata-v1 file")
    acc.add_argument(
        "--prior-texts",
        action="append",
        required=True,
        metavar="PRIOR_TEXTS.json",
        help="h3-prior-texts-v1 file (repeatable)",
    )
    acc.add_argument("--prior-ids", required=True, metavar="PRIOR_IDS.json", help="h3-prior-ids-v1 file")
    acc.add_argument("--bound-tree", required=True, metavar="DIR", help="bound Core checkout")
    acc.add_argument(
        "--core-repo",
        metavar="DIR",
        help="Core git repository for C4 tracked-blob scan (default: --bound-tree)",
    )
    acc.add_argument(
        "--core-commit",
        default=PINNED_CORE_COMMIT,
        metavar="SHA",
        help=f"Core commit scanned by C4 (default: {PINNED_CORE_COMMIT})",
    )
    acc.add_argument("--terms-db", required=True, metavar="TERMS.db", help="bound production terms.db")
    acc.add_argument(
        "--expected-core-commit",
        default=PINNED_CORE_COMMIT,
        metavar="SHA",
        help="expected Core commit SHA for C10",
    )
    acc.add_argument(
        "--expected-src-tree",
        default=PINNED_SRC_TREE,
        metavar="SHA",
        help="expected bound_tree HEAD:src SHA for C10",
    )
    acc.add_argument(
        "--expected-matcher-revision",
        default=PINNED_MATCHER_REVISION,
        metavar="SHA",
        help="expected MATCHER_REVISION for C10",
    )
    acc.add_argument(
        "--expected-terms-sha256",
        default=PINNED_TERMS_DB_SHA256,
        metavar="SHA",
        help="expected terms.db SHA-256 for C10",
    )
    acc.add_argument(
        "--scorer-path",
        default=str(SCORER_PATH),
        metavar="PATH",
        help="path to frozen scripts/eval_usability.py",
    )
    acc.add_argument(
        "--expected-scorer-sha256",
        default=PINNED_SCORER_SHA256,
        metavar="SHA",
        help="expected scripts/eval_usability.py SHA-256",
    )
    acc.add_argument(
        "--clear-names",
        action="append",
        default=[],
        metavar="PATH",
        help="M1 declared official/community name/term list file (repeatable)",
    )
    acc.add_argument(
        "--upstream-clearances",
        metavar="UPSTREAM.json",
        help="M3 explicit upstream_data clearance manifest (h3-upstream-clearances-v1)",
    )
    acc.add_argument(
        "--announcement-floor",
        type=int,
        default=None,
        metavar="N",
        help="explicit override for C3 announcement floor (recorded as non-default in receipt)",
    )
    acc.add_argument(
        "--m2-max-shared-span",
        type=int,
        default=DEFAULT_M2_MAX_SHARED_SPAN,
        metavar="T",
        help=f"C4 M2 max contiguous shared span threshold (strictly < T; default: {DEFAULT_M2_MAX_SHARED_SPAN})",
    )
    acc.add_argument(
        "--cutoff",
        default=DEFAULT_CUTOFF_ISO,
        metavar="ISO8601",
        help=f"C7 publication cutoff (default: {DEFAULT_CUTOFF_ISO})",
    )
    acc.add_argument("--json-out", metavar="RECEIPT.json", help="write h3-acceptance-v1 receipt to file")

    ver = sub.add_parser("verify-receipt", help="validate an h3-acceptance-v1 receipt file")
    ver.add_argument("--receipt", required=True, metavar="RECEIPT.json")
    ver.add_argument("--expected-manifest-sha256", metavar="SHA")
    ver.add_argument("--expected-sample-sha256", metavar="SHA")
    ver.add_argument("--expected-names-sha256", metavar="SHA")
    ver.add_argument("--expected-community-names-sha256", metavar="SHA")
    ver.add_argument("--expected-src-tree", metavar="SHA")
    ver.add_argument("--expected-core-commit", metavar="SHA")
    ver.add_argument("--expected-terms-sha256", metavar="SHA")
    ver.add_argument("--expected-matcher-revision", metavar="SHA")
    ver.add_argument("--expected-scorer-sha256", default=PINNED_SCORER_SHA256, metavar="SHA")
    ver.add_argument("--allow-non-default-floor", action="store_true")
    return parser


def cmd_accept(args: argparse.Namespace) -> int:
    bound_tree = Path(args.bound_tree).resolve()
    core_repo = Path(args.core_repo).resolve() if args.core_repo else bound_tree
    receipt = run_acceptance(
        manifest_path=Path(args.manifest).resolve(),
        sample_path=Path(args.sample).resolve(),
        names_path=Path(args.names).resolve(),
        community_names_path=Path(args.community_names).resolve(),
        metadata_path=Path(args.metadata).resolve() if args.metadata else None,
        prior_text_paths=[Path(p).resolve() for p in args.prior_texts],
        prior_ids_path=Path(args.prior_ids).resolve(),
        bound_tree=bound_tree,
        core_repo=core_repo,
        core_commit=args.core_commit,
        terms_db=Path(args.terms_db).resolve(),
        expected_core_commit=args.expected_core_commit,
        expected_src_tree=args.expected_src_tree,
        expected_matcher_revision=args.expected_matcher_revision,
        expected_terms_sha256=args.expected_terms_sha256,
        scorer_path=Path(args.scorer_path).resolve(),
        expected_scorer_sha256=args.expected_scorer_sha256,
        clear_name_paths=[Path(p).resolve() for p in args.clear_names],
        upstream_clearances_path=Path(args.upstream_clearances).resolve() if args.upstream_clearances else None,
        announcement_floor=args.announcement_floor,
        m2_max_shared_span=args.m2_max_shared_span,
        cutoff_iso=args.cutoff,
    )
    if args.json_out:
        write_json_atomic(Path(args.json_out), receipt)
    sys.stdout.write(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return EXIT_OK if receipt["verdict"] == "pass" else EXIT_FAIL


def cmd_verify_receipt(args: argparse.Namespace) -> int:
    path = Path(args.receipt)
    try:
        data = load_json_strict(path, label=path.name)
        validate_acceptance_receipt(
            data,
            expected_manifest_sha256=args.expected_manifest_sha256,
            expected_sample_sha256=args.expected_sample_sha256,
            expected_names_sha256=args.expected_names_sha256,
            expected_community_names_sha256=args.expected_community_names_sha256,
            expected_src_tree=args.expected_src_tree,
            expected_core_commit=args.expected_core_commit,
            expected_terms_sha256=args.expected_terms_sha256,
            expected_matcher_revision=args.expected_matcher_revision,
            expected_scorer_sha256=args.expected_scorer_sha256,
            allow_non_default_floor=args.allow_non_default_floor,
        )
    except (_CheckError, ReceiptValidationError) as exc:
        print(f"invalid receipt: {exc}", file=sys.stderr)
        return EXIT_FAIL
    print(json.dumps({"valid": True, "schema": RECEIPT_SCHEMA, "verdict": data["verdict"]}, sort_keys=True))
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "accept":
        return cmd_accept(args)
    return cmd_verify_receipt(args)


if __name__ == "__main__":
    sys.exit(main())
