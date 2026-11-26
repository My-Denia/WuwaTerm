#!/usr/bin/env python3
"""H3 corpus acceptance gate (C1-C10) and ``h3-acceptance-v1`` receipt validator.

Subcommands
-----------

``accept``          run mechanical checks C1-C10 over a candidate H3 corpus
                    (``h3-manifest-v1``, ``usability-sample-v1``, metadata,
                    official/community annotations + adjudication, prior
                    texts/IDs, bound Core tree and production ``terms.db``) and
                    emit a structured ``h3-acceptance-v1`` receipt JSON with
                    zero corpus text.
``verify-receipt``  validate an existing ``h3-acceptance-v1`` receipt file using
                    ``validate_acceptance_receipt`` (pinned defaults, fail-closed
                    on any non-default parameter unless explicitly allowed).

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
import math
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
ANNOTATION_ADJUDICATION_SCHEMA = "h3-annotation-adjudication-v1"
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
COMMUNITY_GENRES = ("guide", "mixed")
ANNOUNCEMENT_LEVELS = ("L0", "L1", "L2")
EXCLUSIONS = ("dictionary", "no_letter", "duplicate")

# C3 default thresholds.
DEFAULT_PER_DIRECTION_FLOOR = 140
DEFAULT_PER_GENRE_FLOOR = 30
DEFAULT_ZH_EN_GUIDE_FLOOR = 50

# C4 defaults & required prior sets (§2.4 rule 1).
WINDOW_SIZE = 12
DEFAULT_M2_MAX_SHARED_SPAN = 24
REQUIRED_PRIOR_SETS = (
    "H1",
    "H2",
    "D",
    "R",
    "A1-A6",
    "B1-B3",
    "S01-S18",
    "M-Gold",
)
_GROUPED_PRIOR_SET_MEMBERS: dict[str, tuple[str, ...]] = {
    "A1-A6": tuple(f"A{i}" for i in range(1, 7)),
    "B1-B3": tuple(f"B{i}" for i in range(1, 4)),
    "S01-S18": tuple(f"S{i:02d}" for i in range(1, 19)),
}

# C7 cutoff (strictly < cutoff per ADR 0020).
DEFAULT_CUTOFF_ISO = "2026-11-06T19:45:00+08:00"
MIN_FORBIDDEN_VERSION = (3, 8)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

_DEFAULT_GATE_SHA = object()
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


def _is_cjk_char(ch: str) -> bool:
    """Unicode Han (incl. non-BMP ext + 々/〇), Hiragana, Katakana (incl. halfwidth), Hangul."""
    code = ord(ch)
    return (
        # Han: CJK Unified Ideographs, Ext A, Compatibility, Radicals, 々/〇/Hangzhou numerals, Ext B-I
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
        or 0x2E80 <= code <= 0x2EFF
        or 0x2F00 <= code <= 0x2FDF
        or code in (0x3005, 0x3007)
        or 0x3021 <= code <= 0x3029
        or 0x3038 <= code <= 0x303B
        or 0x20000 <= code <= 0x323AF
        # Hiragana
        or 0x3040 <= code <= 0x309F
        or 0x1B001 <= code <= 0x1B11F
        or code in (0x1B132, 0x1B150, 0x1B151, 0x1B152)
        # Katakana (incl. Phonetic Extensions, Halfwidth Katakana, Kana Extended)
        or 0x30A0 <= code <= 0x30FF
        or 0x31F0 <= code <= 0x31FF
        or 0xFF65 <= code <= 0xFF9F
        or code in (0x1B000, 0x1B120, 0x1B121, 0x1B122, 0x1B155, 0x1B164, 0x1B165, 0x1B166, 0x1B167)
        or 0x1AFF0 <= code <= 0x1AFFF
        # Hangul (Jamo, Compatibility Jamo, Ext-A, Syllables, Ext-B, Halfwidth Hangul)
        or 0x1100 <= code <= 0x11FF
        or 0x3130 <= code <= 0x318F
        or 0xA960 <= code <= 0xA97F
        or 0xAC00 <= code <= 0xD7AF
        or 0xD7B0 <= code <= 0xD7FF
        or 0xFFA0 <= code <= 0xFFDF
    )


def _has_cjk_char(text: str) -> bool:
    return any(_is_cjk_char(ch) for ch in text)


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


def iter_git_commits_text_blobs(repo: Path, commits: Sequence[str]) -> tuple[list[str], list[str], int]:
    """Resolve one or more commits and read the union of tracked UTF-8 non-binary blobs.

    Uses the exact blob-filtering rule of ``eval_usability.git_blob_texts``:
    tracked regular blobs (not symlinks ``120000`` or submodules), no NUL byte,
    valid UTF-8. Duplicate blob OIDs across commits are read once.

    Returns ``(resolved_commits, unique_text_blobs, text_blob_entries_count)``.
    """
    resolved_list: list[str] = []
    unique_resolved: list[str] = []
    for commit in commits:
        rc, out, _ = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
        if rc != 0:
            raise _CheckError("git_commit_invalid", "cannot resolve core commit")
        resolved = out.decode("utf-8").strip()
        resolved_list.append(resolved)
        if resolved not in unique_resolved:
            unique_resolved.append(resolved)

    oids: list[bytes] = []
    oid_entry_counts: dict[bytes, int] = {}
    for resolved in unique_resolved:
        rc, listing, _ = _git(repo, "ls-tree", "-r", "-z", resolved)
        if rc != 0:
            raise _CheckError("git_ls_tree_failed", "git ls-tree failed")
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            meta, _, _path = entry.partition(b"\t")
            fields = meta.split()
            if len(fields) != 3 or fields[1] != b"blob" or fields[0] == b"120000":
                continue
            oid = fields[2]
            if oid not in oid_entry_counts:
                oid_entry_counts[oid] = 1
                oids.append(oid)
            else:
                oid_entry_counts[oid] += 1

    texts: list[str] = []
    text_entries_total = 0
    if not oids:
        return resolved_list, texts, 0

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
        for oid in oids:
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
                text_entries_total += oid_entry_counts[oid]
            except UnicodeDecodeError:
                continue
    finally:
        proc.stdout.close()
        proc.stderr.close()
        rc = proc.wait()
    if rc != 0:
        raise _CheckError("git_cat_file_failed", "git cat-file exited non-zero")
    return resolved_list, texts, text_entries_total


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
    announcement_level: str | None = None


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
            ann_level_raw = raw.get("announcement_level")
            if ann_level_raw is not None and not isinstance(ann_level_raw, str):
                raise _CheckError("invalid_announcement_level_type", "announcement_level")
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
            announcement_level=ann_level_raw,
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


def resolve_announcement_floors(
    *,
    announcement_floor: int | None,
    announcement_floor_zh_en: int | None,
    announcement_floor_en_zh: int | None,
) -> tuple[int, int, str | None]:
    """Resolve effective per-direction announcement floors or return an error code."""
    eff_zh = announcement_floor_zh_en if announcement_floor_zh_en is not None else announcement_floor
    eff_en = announcement_floor_en_zh if announcement_floor_en_zh is not None else announcement_floor
    if announcement_floor is not None:
        if announcement_floor_zh_en is not None and announcement_floor_zh_en != announcement_floor:
            return DEFAULT_PER_GENRE_FLOOR, DEFAULT_PER_GENRE_FLOOR, "conflicting_announcement_floor_flags"
        if announcement_floor_en_zh is not None and announcement_floor_en_zh != announcement_floor:
            return DEFAULT_PER_GENRE_FLOOR, DEFAULT_PER_GENRE_FLOOR, "conflicting_announcement_floor_flags"
    val_zh = DEFAULT_PER_GENRE_FLOOR if eff_zh is None else eff_zh
    val_en = DEFAULT_PER_GENRE_FLOOR if eff_en is None else eff_en
    if val_zh < 0 or val_en < 0:
        return val_zh, val_en, "invalid_announcement_floor"
    return val_zh, val_en, None


def check_c3(
    *,
    manifest_docs: Sequence[ManifestDoc],
    sample_paragraphs: Sequence[SampleParagraph],
    metadata_by_doc: Mapping[str, DocMetadata],
    announcement_floor_zh_en: int,
    announcement_floor_en_zh: int,
    floor_config_error: str | None = None,
) -> dict[str, Any]:
    if floor_config_error is not None:
        return {
            "status": "fail",
            "floor_profile": f"invalid:{floor_config_error}",
            "non_default_floor": True,
            "announcement_units_by_level": {d: {lvl: 0 for lvl in ANNOUNCEMENT_LEVELS} for d in DIRECTIONS},
            "announcement_level_issues_count": 0,
            "announcement_level_issues": [],
            "shortfalls_count": 1,
            "shortfalls": [{"code": floor_config_error}],
        }

    custom_parts: list[str] = []
    if announcement_floor_zh_en != DEFAULT_PER_GENRE_FLOOR:
        custom_parts.append(f"announcement[zh->en]={announcement_floor_zh_en}")
    if announcement_floor_en_zh != DEFAULT_PER_GENRE_FLOOR:
        custom_parts.append(f"announcement[en->zh]={announcement_floor_en_zh}")
    non_default = bool(custom_parts)
    floor_profile = f"custom:{','.join(custom_parts)}" if non_default else "default"

    per_genre_floors: dict[str, dict[str, int]] = {
        "zh->en": {
            "announcement": announcement_floor_zh_en,
            "story": DEFAULT_PER_GENRE_FLOOR,
            "guide": DEFAULT_ZH_EN_GUIDE_FLOOR,
            "mixed": DEFAULT_PER_GENRE_FLOOR,
        },
        "en->zh": {
            "announcement": announcement_floor_en_zh,
            "story": DEFAULT_PER_GENRE_FLOOR,
            "guide": DEFAULT_PER_GENRE_FLOOR,
            "mixed": DEFAULT_PER_GENRE_FLOOR,
        },
    }

    # Validate announcement_level (§2.5 & §6.7: required L0/L1/L2 on announcements, forbidden on non-announcements).
    level_issues: list[dict[str, Any]] = []
    doc_ann_level: dict[str, str] = {}
    for doc in manifest_docs:
        meta = metadata_by_doc.get(doc.doc_id)
        level = meta.announcement_level if meta is not None else None
        if doc.genre == "announcement":
            if level is None:
                level_issues.append({"code": "missing_announcement_level", "doc_id": doc.doc_id})
            elif level not in ANNOUNCEMENT_LEVELS:
                level_issues.append({"code": "invalid_announcement_level", "doc_id": doc.doc_id})
            else:
                doc_ann_level[doc.doc_id] = level
        elif level is not None:
            level_issues.append(
                {
                    "code": "unexpected_announcement_level_on_non_announcement",
                    "doc_id": doc.doc_id,
                    "genre": doc.genre,
                }
            )

    observed_dir: dict[str, int] = {d: 0 for d in DIRECTIONS}
    observed_genre: dict[str, dict[str, int]] = {d: {g: 0 for g in GENRES} for d in DIRECTIONS}
    announcement_units_by_level: dict[str, dict[str, int]] = {
        d: {lvl: 0 for lvl in ANNOUNCEMENT_LEVELS} for d in DIRECTIONS
    }
    for para in sample_paragraphs:
        if not para.scored:
            continue
        observed_dir[para.direction] += 1
        observed_genre[para.direction][para.genre] += 1
        if para.genre == "announcement":
            lvl = doc_ann_level.get(para.doc_id)
            if lvl is not None:
                announcement_units_by_level[para.direction][lvl] += 1

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
        "status": "pass" if (not shortfalls and not level_issues) else "fail",
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
        "announcement_units_by_level": announcement_units_by_level,
        "announcement_level_issues_count": len(level_issues),
        "announcement_level_issues": level_issues,
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
        text_len = len(text)
        for idx in range(text_len - 1):
            candidates = self._substr_by_prefix2.get(text[idx : idx + 2])
            if not candidates:
                continue
            for surface in candidates:
                if text.startswith(surface, idx):
                    spans.append((idx, idx + len(surface)))
                    break
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


def _missing_required_prior_sets(prior_sets: Sequence[str]) -> list[str]:
    present = set(prior_sets)
    missing: list[str] = []
    for req in REQUIRED_PRIOR_SETS:
        if req in present:
            continue
        members = _GROUPED_PRIOR_SET_MEMBERS.get(req)
        if members is not None and all(m in present for m in members):
            continue
        missing.append(req)
    return missing


def _extract_lexicon_strings(parsed: Any) -> list[str] | None:
    items = parsed
    if isinstance(parsed, dict):
        items = parsed.get("entries", parsed.get("terms", parsed.get("names")))
    if not isinstance(items, list):
        return None
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            if item:
                out.append(item)
        elif isinstance(item, dict):
            found_any = False
            for key in ("surface", "term", "name"):
                val = item.get(key)
                if isinstance(val, str) and val:
                    out.append(val)
                    found_any = True
            for key in ("forms", "accepted_forms", "official_forms", "aliases"):
                vals = item.get(key)
                if isinstance(vals, list) and all(isinstance(v, str) for v in vals):
                    out.extend(v for v in vals if v)
                    found_any = True
            if not found_any:
                return None
        else:
            return None
    return out


def _load_m1_name_strings(
    clear_name_paths: Sequence[Path],
    community_lexicon_paths: Sequence[Path],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Load M1 declared names strictly from frozen upstream name files and explicit community lexicons.

    H3's own ``names.json`` / ``community_names.json`` occurrences are never
    accepted as M1 sources so candidate annotations cannot self-exempt hits.
    """
    names: list[str] = []
    issues: list[dict[str, Any]] = []
    for kind, paths in (("clear_names", clear_name_paths), ("community_lexicon", community_lexicon_paths)):
        for path in paths:
            try:
                raw_text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                issues.append({"code": f"unreadable_{kind}_file", "artifact": path.name})
                continue
            if path.suffix.lower() == ".json":
                try:
                    parsed = json.loads(raw_text)
                except ValueError:
                    issues.append({"code": f"invalid_{kind}_json", "artifact": path.name})
                    continue
                extracted = _extract_lexicon_strings(parsed)
                if extracted is None:
                    issues.append({"code": f"invalid_{kind}_structure", "artifact": path.name})
                    continue
                names.extend(extracted)
            else:
                names.extend(line for line in raw_text.splitlines() if line)
    return names, issues


def _load_m3_upstream_clearances(path: Path | None) -> tuple[set[str], list[dict[str, Any]]]:
    """Return (set of cleared 12-scalar window SHA-256 digests, issues).

    Strict §2.4.1 M3 rules: every entry must carry ``classification == "upstream_data"``,
    a 64-hex ``window_sha256``, and a non-empty ``location``. ``text`` entries and
    ``reason`` aliases are rejected.
    """
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

    allowed_keys = {"classification", "window_sha256", "location", "source_ref"}
    for idx, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            issues.append({"code": "invalid_upstream_clearance_item", "index": idx})
            continue
        if "text" in entry:
            issues.append({"code": "upstream_clearance_text_forbidden", "index": idx})
            continue
        if "reason" in entry:
            issues.append({"code": "upstream_clearance_reason_alias_forbidden", "index": idx})
            continue
        if not set(entry) <= allowed_keys:
            issues.append({"code": "unknown_upstream_clearance_key", "index": idx})
            continue
        if entry.get("classification") != "upstream_data":
            issues.append({"code": "non_upstream_data_clearance_rejected", "index": idx})
            continue
        win_sha = entry.get("window_sha256")
        if not isinstance(win_sha, str) or not _HEX64_RE.match(win_sha):
            issues.append({"code": "invalid_window_sha256", "index": idx})
            continue
        location = entry.get("location")
        if not isinstance(location, str) or not location.strip():
            issues.append({"code": "missing_upstream_clearance_location", "index": idx})
            continue
        if "source_ref" in entry and (not isinstance(entry["source_ref"], str) or not entry["source_ref"].strip()):
            issues.append({"code": "invalid_upstream_clearance_source_ref", "index": idx})
            continue
        cleared_digests.add(win_sha)
    return cleared_digests, issues


def _compute_fragment_lengths(
    h3_units: Sequence[tuple[str, str, str]],
    hit_windows: set[str],
    windows_fn: Any,
) -> dict[str, int]:
    """§2.4.1 shared fragment length per hit window: ``consecutive_hit_windows + 11``.

    Computed over each candidate paragraph's hit positions across ALL sealed sets
    combined, so adjacent/overlapping hits from distinct sealed sets merge into
    one contiguous shared fragment.
    """
    max_frag_by_window: dict[str, int] = {}
    if not hit_windows:
        return max_frag_by_window
    for _pid, _field, text in h3_units:
        hit_positions: list[tuple[int, str]] = [
            (pos, w) for pos, w in enumerate(windows_fn(text, WINDOW_SIZE)) if w in hit_windows
        ]
        if not hit_positions:
            continue
        run_start = 0
        n = len(hit_positions)
        while run_start < n:
            run_end = run_start
            while run_end + 1 < n and hit_positions[run_end + 1][0] == hit_positions[run_end][0] + 1:
                run_end += 1
            frag_len = (run_end - run_start + 1) + (WINDOW_SIZE - 1)
            for idx in range(run_start, run_end + 1):
                w = hit_positions[idx][1]
                prev = max_frag_by_window.get(w, 0)
                if frag_len > prev:
                    max_frag_by_window[w] = frag_len
            run_start = run_end + 1
    return max_frag_by_window


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
    freeze_core_commit: str | None,
    clear_name_paths: Sequence[Path],
    community_lexicon_paths: Sequence[Path],
    upstream_clearances_path: Path | None,
    terms_db: Path,
    m2_threshold: int,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = list(prior_issues)
    missing_prior = _missing_required_prior_sets(prior_sets)
    if missing_prior:
        issues.append({"code": "missing_required_prior_sets", "missing": missing_prior})

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
            "core_tag_commit": None,
            "freeze_core_commit": None,
            "core_blob_entries_scanned": 0,
            "core_unique_blobs_scanned": 0,
            "prior_sets_scanned": list(prior_sets),
            "required_prior_sets_complete": not missing_prior,
            "m2_max_shared_span_threshold": m2_threshold,
            "windows_hit": 0,
            "cleared_counts": {"M1": 0, "M2": 0, "M3": 0},
            "windows_uncleared": 0,
            "max_shared_span_length": 0,
            "hit_windows": [],
            "uncleared_windows": [],
            "issues_count": len(issues),
            "issues": issues,
        }

    windows_fn = ev.windows
    if getattr(ev, "DEFAULT_WINDOW", WINDOW_SIZE) != WINDOW_SIZE:
        issues.append({"code": "scorer_window_size_mismatch"})

    resolved_core = ""
    resolved_freeze = ""
    core_blobs: list[str] = []
    core_blob_entries = 0
    if not freeze_core_commit or not freeze_core_commit.strip():
        issues.append({"code": "missing_freeze_core_commit"})
        try:
            resolved_commits, core_blobs, core_blob_entries = iter_git_commits_text_blobs(core_repo, [core_commit])
            resolved_core = resolved_commits[0]
        except _CheckError as exc:
            issues.append({"code": exc.code})
    else:
        try:
            resolved_commits, core_blobs, core_blob_entries = iter_git_commits_text_blobs(
                core_repo, [core_commit, freeze_core_commit]
            )
            resolved_core, resolved_freeze = resolved_commits[0], resolved_commits[1]
            if resolved_freeze == resolved_core:
                issues.append({"code": "freeze_core_commit_equals_tag_commit"})
            else:
                rc_anc, _, _ = _git(core_repo, "merge-base", "--is-ancestor", resolved_core, resolved_freeze)
                if rc_anc != 0:
                    issues.append({"code": "core_tag_not_ancestor_of_freeze_commit"})
        except _CheckError as exc:
            issues.append({"code": exc.code})

    m1_names, m1_issues = _load_m1_name_strings(clear_name_paths, community_lexicon_paths)
    issues.extend(m1_issues)
    m3_digests, m3_issues = _load_m3_upstream_clearances(upstream_clearances_path)
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

    # Scan prior texts and Core commit(s) tracked text blobs.
    hit_targets: dict[str, set[str]] = {}

    def _scan_target(target_label: str, target_text: str) -> None:
        if len(target_text) < WINDOW_SIZE or not h3_windows:
            return
        for w in windows_fn(target_text, WINDOW_SIZE):
            if w in h3_windows:
                hit_targets.setdefault(w, set()).add(target_label)

    for set_label, text in prior_entries:
        _scan_target(f"prior:{set_label}", text)
    for blob_text in core_blobs:
        _scan_target("core_tree", blob_text)

    hit_windows = set(hit_targets)
    max_frag_by_window = _compute_fragment_lengths(h3_units, hit_windows, windows_fn)
    global_max_frag = max(max_frag_by_window.values()) if max_frag_by_window else 0

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
    hit_windows_audit: list[dict[str, Any]] = []
    uncleared_records: list[dict[str, Any]] = []

    # Clearance order (§2.4.1): M1 -> M2 -> M3 -> uncleared.
    for w in sorted(hit_windows, key=sha256_text):
        digest = sha256_text(w)
        frag_len = max_frag_by_window[w]
        pids = sorted({h3_units[u_idx][0] for u_idx, _ in h3_window_locs[w]})
        targets = sorted(hit_targets[w])
        if w in m1_cleared_windows:
            cls = "M1"
            m1_count += 1
        elif (
            frag_len < m2_threshold
            and not _has_cjk_char(w)
            and not _has_unicode_nd_digit(w)
            and not _overlaps_dict_surface(w)
        ):
            cls = "M2"
            m2_count += 1
        elif digest in m3_digests:
            cls = "M3"
            m3_count += 1
        else:
            cls = "uncleared"
        record = {
            "sha256": digest,
            "class": cls,
            "paragraph_ids": pids,
            "targets": targets,
            "fragment_len": frag_len,
        }
        hit_windows_audit.append(record)
        if cls == "uncleared":
            uncleared_records.append(record)

    passed = not issues and len(uncleared_records) == 0
    return {
        "status": "pass" if passed else "fail",
        "window_size": WINDOW_SIZE,
        "scorer_sha256": verified_sha,
        "scorer_sha256_expected": expected_scorer_sha256,
        "scorer_sha256_ok": True,
        "core_tag_commit": resolved_core or None,
        "freeze_core_commit": resolved_freeze or None,
        "core_blob_entries_scanned": core_blob_entries,
        "core_unique_blobs_scanned": len(core_blobs),
        "prior_sets_scanned": list(prior_sets),
        "required_prior_sets_complete": not missing_prior,
        "m2_max_shared_span_threshold": m2_threshold,
        "windows_hit": len(hit_windows),
        "cleared_counts": {"M1": m1_count, "M2": m2_count, "M3": m3_count},
        "windows_uncleared": len(uncleared_records),
        "max_shared_span_length": global_max_frag,
        "hit_windows": hit_windows_audit,
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
        non_empty_id_count = 0
        for singular, plural, values in checks:
            for val in values:
                norm = _normalize_id_token(val)
                if not norm:
                    continue
                non_empty_id_count += 1
                if norm in prior_sets[plural]:
                    issues.append(
                        {
                            "code": "prior_id_overlap",
                            "doc_id": doc.doc_id,
                            "field": singular,
                            "value_sha256": sha256_text(norm),
                        }
                    )
        if non_empty_id_count == 0:
            issues.append({"code": "missing_doc_identifiers", "doc_id": doc.doc_id})

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
                elif pub_dt >= cutoff_dt:
                    # Strictly < cutoff per ADR 0020 ("up to, not including, 2026-11-06 19:45 +08:00").
                    issues.append(
                        {
                            "code": "published_at_or_after_cutoff",
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
        "cutoff_comparison": "strictly_before",
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


def _validate_adjudication_block(
    *,
    block: Any,
    kind: str,
    target_unit_count: int,
    units_with_occurrences: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(block, dict):
        issues.append({"code": "missing_or_invalid_adjudication", "kind": kind})
        return None, issues

    annotators = block.get("annotators")
    if (
        not isinstance(annotators, list)
        or len(annotators) != 2
        or not all(isinstance(a, str) and a.strip() for a in annotators)
        or len({a.strip() for a in annotators}) != 2
    ):
        issues.append({"code": "invalid_adjudication_annotators", "kind": kind})
        annotators_clean: list[str] = []
    else:
        annotators_clean = [a.strip() for a in annotators]

    adjudicator = block.get("adjudicator")
    if not isinstance(adjudicator, str) or not adjudicator.strip():
        issues.append({"code": "invalid_adjudication_adjudicator", "kind": kind})
        adj_clean = ""
    else:
        adj_clean = adjudicator.strip()
        if adj_clean in annotators_clean:
            issues.append({"code": "adjudicator_not_independent", "kind": kind})

    units_reviewed = block.get("units_reviewed")
    if type(units_reviewed) is not int or units_reviewed != target_unit_count:
        issues.append(
            {
                "code": "adjudication_units_reviewed_mismatch",
                "kind": kind,
                "observed": units_reviewed if type(units_reviewed) is int else None,
                "expected": target_unit_count,
            }
        )

    agreement_rate = block.get("agreement_rate")
    if (
        type(agreement_rate) is bool
        or not isinstance(agreement_rate, (int, float))
        or not math.isfinite(float(agreement_rate))
        or not (0.0 <= float(agreement_rate) <= 1.0)
    ):
        issues.append({"code": "invalid_adjudication_agreement_rate", "kind": kind})
        agreement_val: float | None = None
    else:
        agreement_val = float(agreement_rate)

    adjudicated_count = block.get("adjudicated_count")
    if type(adjudicated_count) is not int or adjudicated_count < 0 or adjudicated_count > target_unit_count:
        issues.append({"code": "invalid_adjudication_count", "kind": kind})
        adj_count_val: int | None = None
    else:
        adj_count_val = adjudicated_count

    unresolved = block.get("unresolved_disagreements")
    if type(unresolved) is not int or unresolved != 0:
        issues.append(
            {
                "code": "unresolved_annotation_disagreements",
                "kind": kind,
                "unresolved_disagreements": unresolved if type(unresolved) is int else None,
            }
        )

    if "zero_occurrence_units" in block:
        zero_units = block["zero_occurrence_units"]
        if type(zero_units) is not int or zero_units < 0 or units_with_occurrences + zero_units != target_unit_count:
            issues.append({"code": "invalid_zero_occurrence_units_count", "kind": kind})

    summary = {
        "annotators_count": len(annotators_clean),
        "adjudicator_present": bool(adj_clean),
        "units_reviewed": units_reviewed if type(units_reviewed) is int else None,
        "units_with_occurrences": units_with_occurrences,
        "zero_occurrence_units": max(target_unit_count - units_with_occurrences, 0),
        "agreement_rate": agreement_val,
        "adjudicated_count": adj_count_val,
        "unresolved_disagreements": unresolved if type(unresolved) is int else None,
    }
    return summary, issues


def _validate_annotation_file(
    *,
    raw_data: Any,
    expected_schema: str,
    kind: str,
    paragraphs_by_id: Mapping[str, SampleParagraph],
    target_ids: set[str],
    seen_occs: set[str],
    seen_spans: dict[tuple[str, int, int], str],
) -> tuple[int, set[str], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(raw_data, dict) or raw_data.get("schema") != expected_schema:
        issues.append({"code": "invalid_annotation_schema", "kind": kind})
        return 0, set(), issues

    raw_occs = raw_data.get("occurrences")
    if not isinstance(raw_occs, list):
        issues.append({"code": "invalid_occurrences_array", "kind": kind})
        return 0, set(), issues

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
        if pid not in target_ids:
            issues.append(
                {
                    "code": "community_occurrence_outside_guide_mixed",
                    "kind": kind,
                    "occ": occ_id,
                    "paragraph_id": pid,
                    "genre": para.genre,
                }
            )
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

    # Optional explicit zero-occurrence coverage list if present in external files: validate if given, never required.
    if "zero_occurrence_paragraphs" in raw_data:
        raw_zeros = raw_data["zero_occurrence_paragraphs"]
        if not isinstance(raw_zeros, list) or not all(isinstance(x, str) for x in raw_zeros):
            issues.append({"code": "invalid_zero_occurrence_paragraphs", "kind": kind})
        else:
            zero_set = set(raw_zeros)
            for pid in sorted(zero_set - target_ids):
                issues.append({"code": "out_of_scope_zero_occurrence_paragraph", "kind": kind, "paragraph_id": pid})
            for pid in sorted(zero_set & occ_paragraphs):
                issues.append({"code": "paragraph_in_both_occurrences_and_zero_list", "kind": kind, "paragraph_id": pid})

    return valid_occs, occ_paragraphs, issues


def check_c9(
    *,
    sample_paragraphs: Sequence[SampleParagraph],
    official_names_raw: Any,
    official_load_error: str | None,
    community_names_raw: Any,
    community_load_error: str | None,
    adjudication_raw: Any,
    adjudication_load_error: str | None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if official_load_error is not None:
        issues.append({"code": official_load_error, "kind": "official"})
    if community_load_error is not None:
        issues.append({"code": community_load_error, "kind": "community"})
    if adjudication_load_error is not None:
        issues.append({"code": adjudication_load_error, "kind": "adjudication"})

    paragraphs_by_id = {p.pid: p for p in sample_paragraphs}
    scored_ids = {p.pid for p in sample_paragraphs if p.scored}
    community_target_ids = {p.pid for p in sample_paragraphs if p.scored and p.genre in COMMUNITY_GENRES}

    if not scored_ids:
        issues.append({"code": "no_scored_units"})

    seen_occs: set[str] = set()
    seen_spans: dict[tuple[str, int, int], str] = {}

    off_occs = 0
    off_occ_pids: set[str] = set()
    if official_load_error is None:
        off_occs, off_occ_pids, off_issues = _validate_annotation_file(
            raw_data=official_names_raw,
            expected_schema=OFFICIAL_NAMES_SCHEMA,
            kind="official",
            paragraphs_by_id=paragraphs_by_id,
            target_ids=scored_ids,
            seen_occs=seen_occs,
            seen_spans=seen_spans,
        )
        issues.extend(off_issues)

    com_occs = 0
    com_occ_pids: set[str] = set()
    if community_load_error is None:
        com_occs, com_occ_pids, com_issues = _validate_annotation_file(
            raw_data=community_names_raw,
            expected_schema=COMMUNITY_NAMES_SCHEMA,
            kind="community",
            paragraphs_by_id=paragraphs_by_id,
            target_ids=community_target_ids,
            seen_occs=seen_occs,
            seen_spans=seen_spans,
        )
        issues.extend(com_issues)

    # Resolve dual-annotation adjudication blocks (explicit file OR embedded in annotation files).
    off_adj_raw: Any = None
    com_adj_raw: Any = None
    if adjudication_raw is not None:
        if not isinstance(adjudication_raw, dict) or adjudication_raw.get("schema") != ANNOTATION_ADJUDICATION_SCHEMA:
            issues.append({"code": "invalid_adjudication_schema", "kind": "adjudication"})
        else:
            off_adj_raw = adjudication_raw.get("official")
            com_adj_raw = adjudication_raw.get("community")
    else:
        if isinstance(official_names_raw, dict):
            off_adj_raw = official_names_raw.get("adjudication")
        if isinstance(community_names_raw, dict):
            com_adj_raw = community_names_raw.get("adjudication")

    off_adj_summary, off_adj_issues = _validate_adjudication_block(
        block=off_adj_raw,
        kind="official",
        target_unit_count=len(scored_ids),
        units_with_occurrences=len(off_occ_pids),
    )
    issues.extend(off_adj_issues)

    com_adj_summary, com_adj_issues = _validate_adjudication_block(
        block=com_adj_raw,
        kind="community",
        target_unit_count=len(community_target_ids),
        units_with_occurrences=len(com_occ_pids),
    )
    issues.extend(com_adj_issues)

    return {
        "status": "pass" if not issues else "fail",
        "scored_units_total": len(scored_ids),
        "community_target_genres": list(COMMUNITY_GENRES),
        "community_target_scored_units": len(community_target_ids),
        "official_occurrences": off_occs,
        "official_units_with_occurrences": len(off_occ_pids),
        "community_occurrences": com_occs,
        "community_units_with_occurrences": len(com_occ_pids),
        "adjudication": {
            "official": off_adj_summary,
            "community": com_adj_summary,
        },
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
    freeze_core_commit: str | None,
    expected_core_commit: str,
    expected_freeze_core_commit: str | None,
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
    annotation_adjudication_path: Path | None,
    metadata_path: Path | None,
    prior_ids_path: Path,
    prior_text_paths: Sequence[Path],
    clear_name_paths: Sequence[Path],
    community_lexicon_paths: Sequence[Path],
    upstream_clearances_path: Path | None,
    non_default_parameters: Mapping[str, Any],
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

    # Core tag commit + mandatory distinct descendant freeze Core commit verification.
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

    resolved_freeze_commit: str | None = None
    if not freeze_core_commit or not freeze_core_commit.strip():
        issues.append({"code": "missing_freeze_core_commit"})
    else:
        rc_fc, out_fc, _ = _git(core_repo, "rev-parse", "--verify", f"{freeze_core_commit}^{{commit}}")
        resolved_freeze_commit = out_fc.decode("utf-8").strip() if rc_fc == 0 else None
        if resolved_freeze_commit is None:
            issues.append({"code": "freeze_core_commit_unresolvable"})
        else:
            if expected_freeze_core_commit is not None and resolved_freeze_commit != expected_freeze_core_commit:
                issues.append(
                    {
                        "code": "freeze_core_commit_mismatch",
                        "actual": resolved_freeze_commit,
                        "expected": expected_freeze_core_commit,
                    }
                )
            if resolved_core_commit is not None:
                if resolved_freeze_commit == resolved_core_commit:
                    issues.append({"code": "freeze_core_commit_equals_tag_commit"})
                else:
                    rc_anc, _, _ = _git(
                        core_repo, "merge-base", "--is-ancestor", resolved_core_commit, resolved_freeze_commit
                    )
                    if rc_anc != 0:
                        issues.append({"code": "core_tag_not_ancestor_of_freeze_commit"})

    # Import bound Core.
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
        "annotation_adjudication": (
            _file_binding(annotation_adjudication_path) if annotation_adjudication_path is not None else None
        ),
        "metadata": _file_binding(metadata_path) if metadata_path is not None else None,
        "prior_ids": _file_binding(prior_ids_path),
        "prior_texts": [_file_binding(p) for p in prior_text_paths],
        "clear_names": [_file_binding(p) for p in clear_name_paths],
        "community_lexicon": [_file_binding(p) for p in community_lexicon_paths],
        "upstream_clearances": (
            _file_binding(upstream_clearances_path) if upstream_clearances_path is not None else None
        ),
    }

    for key in ("manifest", "sample", "names", "community_names", "prior_ids"):
        if inputs_sha256[key]["sha256"] is None:
            issues.append({"code": "missing_input_file", "input": key})
    if not inputs_sha256["prior_texts"] or any(x["sha256"] is None for x in inputs_sha256["prior_texts"]):
        issues.append({"code": "missing_input_file", "input": "prior_texts"})
    if annotation_adjudication_path is not None and inputs_sha256["annotation_adjudication"]["sha256"] is None:
        issues.append({"code": "missing_input_file", "input": "annotation_adjudication"})
    if metadata_path is not None and inputs_sha256["metadata"]["sha256"] is None:
        issues.append({"code": "missing_input_file", "input": "metadata"})
    if any(x["sha256"] is None for x in inputs_sha256["clear_names"]):
        issues.append({"code": "missing_input_file", "input": "clear_names"})
    if any(x["sha256"] is None for x in inputs_sha256["community_lexicon"]):
        issues.append({"code": "missing_input_file", "input": "community_lexicon"})
    if upstream_clearances_path is not None and inputs_sha256["upstream_clearances"]["sha256"] is None:
        issues.append({"code": "missing_input_file", "input": "upstream_clearances"})

    bindings: dict[str, Any] = {
        "core_tag_commit": resolved_core_commit,
        "core_commit": resolved_core_commit,
        "freeze_core_commit": resolved_freeze_commit,
        "expected_core_commit": expected_core_commit,
        "expected_freeze_core_commit": expected_freeze_core_commit,
        "bound_tree_commit": bound_head,
        "bound_tree_src_sha": bound_src_sha,
        "expected_src_tree": expected_src_tree,
        "matcher_revision": actual_matcher_rev,
        "expected_matcher_revision": expected_matcher_revision,
        "terms_db_sha256": terms_sha,
        "expected_terms_sha256": expected_terms_sha256,
        "non_default_parameters": dict(sorted(non_default_parameters.items())),
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


_NON_DEFAULT_FLOOR_KEYS = frozenset({"announcement_floor_zh_en", "announcement_floor_en_zh"})
_NON_DEFAULT_M2_KEYS = frozenset({"m2_max_shared_span"})
_NON_DEFAULT_CUTOFF_KEYS = frozenset({"cutoff"})
_NON_DEFAULT_BINDING_KEYS = frozenset(
    {
        "expected_core_commit",
        "expected_src_tree",
        "expected_matcher_revision",
        "expected_terms_sha256",
        "expected_scorer_sha256",
    }
)


def validate_acceptance_receipt(
    receipt_dict: Any,
    *,
    expected_manifest_sha256: str | None = None,
    expected_sample_sha256: str | None = None,
    expected_names_sha256: str | None = None,
    expected_community_names_sha256: str | None = None,
    expected_metadata_sha256: str | None = None,
    expected_src_tree: str | None = PINNED_SRC_TREE,
    expected_core_commit: str | None = PINNED_CORE_COMMIT,
    expected_freeze_core_commit: str | None = None,
    expected_terms_sha256: str | None = PINNED_TERMS_DB_SHA256,
    expected_matcher_revision: str | None = PINNED_MATCHER_REVISION,
    expected_scorer_sha256: str | None = PINNED_SCORER_SHA256,
    expected_gate_sha256: str | None | object = _DEFAULT_GATE_SHA,
    allow_non_default_floor: bool = False,
    allow_non_default_m2_threshold: bool = False,
    allow_non_default_cutoff: bool = False,
    allow_non_default_bindings: bool = False,
    require_pass: bool = True,
) -> dict[str, Any]:
    """Validate an ``h3-acceptance-v1`` receipt dict for reuse by ``eval_h3.py``.

    Defaults bind to the pinned v0.14.0 Core/dictionary/scorer/gate constants and
    fail closed on any non-empty ``non_default_parameters`` unless the matching
    ``allow_*`` flag is explicitly True.
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

    non_default_top = receipt_dict.get("non_default_parameters")
    if not isinstance(non_default_top, dict):
        raise ReceiptValidationError("non_default_parameters must be an object")
    if bindings.get("non_default_parameters") != non_default_top:
        raise ReceiptValidationError("non_default_parameters disagrees with hashed bindings")

    for sha40_key in ("core_tag_commit", "freeze_core_commit", "bound_tree_src_sha"):
        val = bindings.get(sha40_key)
        if require_pass and (not isinstance(val, str) or not _HEX40_RE.match(val)):
            raise ReceiptValidationError(f"bindings.{sha40_key} must be a 40-hex git SHA")
    if require_pass:
        if "core_commit" in bindings and bindings.get("core_commit") != bindings.get("core_tag_commit"):
            raise ReceiptValidationError("bindings.core_commit disagrees with bindings.core_tag_commit")
        if bindings.get("freeze_core_commit") == bindings.get("core_tag_commit"):
            raise ReceiptValidationError("bindings.freeze_core_commit must differ from bindings.core_tag_commit")
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

    c3 = checks["C3"]
    c4 = checks["C4"]
    c7 = checks["C7"]
    if require_pass:
        if c4.get("windows_uncleared") != 0:
            raise ReceiptValidationError("C4 windows_uncleared must be 0 for a passing receipt")
        if c4.get("scorer_sha256_ok") is not True:
            raise ReceiptValidationError("C4 scorer_sha256_ok must be true for a passing receipt")
        if c4.get("required_prior_sets_complete") is not True:
            raise ReceiptValidationError("C4 required_prior_sets_complete must be true for a passing receipt")

    # Fail closed on non-default parameters (check both recorded dict and actual check fields).
    if require_pass:
        allowed_keys: set[str] = set()
        if allow_non_default_floor:
            allowed_keys |= _NON_DEFAULT_FLOOR_KEYS
        if allow_non_default_m2_threshold:
            allowed_keys |= _NON_DEFAULT_M2_KEYS
        if allow_non_default_cutoff:
            allowed_keys |= _NON_DEFAULT_CUTOFF_KEYS
        if allow_non_default_bindings:
            allowed_keys |= _NON_DEFAULT_BINDING_KEYS

        disallowed = sorted(set(non_default_top) - allowed_keys)
        if disallowed:
            raise ReceiptValidationError(
                f"non_default_parameters not explicitly allowed: {', '.join(disallowed)}"
            )
        if not allow_non_default_floor and (
            c3.get("non_default_floor") is not False or c3.get("floor_profile") != "default"
        ):
            raise ReceiptValidationError("non-default C3 floor_profile requires allow_non_default_floor=True")
        if not allow_non_default_m2_threshold and c4.get("m2_max_shared_span_threshold") != DEFAULT_M2_MAX_SHARED_SPAN:
            raise ReceiptValidationError(
                "non-default C4 m2_max_shared_span_threshold requires allow_non_default_m2_threshold=True"
            )
        if not allow_non_default_cutoff and c7.get("cutoff") != DEFAULT_CUTOFF_ISO:
            raise ReceiptValidationError("non-default C7 cutoff requires allow_non_default_cutoff=True")

    if require_pass and verdict != "pass":
        raise ReceiptValidationError(f"receipt verdict is 'fail' (failed_rules={failed_rules})")

    # Expected binding checks.
    def _check_expected(label: str, actual: Any, expected: str | None) -> None:
        if expected is not None and actual != expected:
            raise ReceiptValidationError(f"{label} mismatch")

    eff_gate_sha: str | None = (
        sha256_file(SELF_PATH) if expected_gate_sha256 is _DEFAULT_GATE_SHA else expected_gate_sha256  # type: ignore[assignment]
    )

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
    _check_expected("core_tag_commit", bindings.get("core_tag_commit"), expected_core_commit)
    _check_expected("freeze_core_commit", bindings.get("freeze_core_commit"), expected_freeze_core_commit)
    _check_expected("terms_db_sha256", bindings.get("terms_db_sha256"), expected_terms_sha256)
    _check_expected("matcher_revision", bindings.get("matcher_revision"), expected_matcher_revision)
    _check_expected("scorer_sha256", tools_sha.get("scripts/eval_usability.py"), expected_scorer_sha256)
    _check_expected("gate_sha256", tools_sha.get("scripts/eval_h3_corpus.py"), eff_gate_sha)

    return receipt_dict


# --------------------------------------------------------------- orchestrator


def run_acceptance(
    *,
    manifest_path: Path,
    sample_path: Path,
    names_path: Path,
    community_names_path: Path,
    annotation_adjudication_path: Path | None = None,
    metadata_path: Path | None = None,
    prior_text_paths: Sequence[Path],
    prior_ids_path: Path,
    bound_tree: Path,
    core_repo: Path,
    core_commit: str,
    freeze_core_commit: str | None = None,
    terms_db: Path,
    expected_core_commit: str = PINNED_CORE_COMMIT,
    expected_freeze_core_commit: str | None = None,
    expected_src_tree: str = PINNED_SRC_TREE,
    expected_matcher_revision: str = PINNED_MATCHER_REVISION,
    expected_terms_sha256: str = PINNED_TERMS_DB_SHA256,
    scorer_path: Path = SCORER_PATH,
    expected_scorer_sha256: str = PINNED_SCORER_SHA256,
    clear_name_paths: Sequence[Path] = (),
    community_lexicon_paths: Sequence[Path] = (),
    upstream_clearances_path: Path | None = None,
    announcement_floor: int | None = None,
    announcement_floor_zh_en: int | None = None,
    announcement_floor_en_zh: int | None = None,
    m2_max_shared_span: int = DEFAULT_M2_MAX_SHARED_SPAN,
    cutoff_iso: str = DEFAULT_CUTOFF_ISO,
) -> dict[str, Any]:
    eff_ann_zh, eff_ann_en, floor_err = resolve_announcement_floors(
        announcement_floor=announcement_floor,
        announcement_floor_zh_en=announcement_floor_zh_en,
        announcement_floor_en_zh=announcement_floor_en_zh,
    )

    non_default_parameters: dict[str, Any] = {}
    if m2_max_shared_span != DEFAULT_M2_MAX_SHARED_SPAN:
        non_default_parameters["m2_max_shared_span"] = m2_max_shared_span
    if cutoff_iso != DEFAULT_CUTOFF_ISO:
        non_default_parameters["cutoff"] = cutoff_iso
    if eff_ann_zh != DEFAULT_PER_GENRE_FLOOR:
        non_default_parameters["announcement_floor_zh_en"] = eff_ann_zh
    if eff_ann_en != DEFAULT_PER_GENRE_FLOOR:
        non_default_parameters["announcement_floor_en_zh"] = eff_ann_en
    if expected_core_commit != PINNED_CORE_COMMIT:
        non_default_parameters["expected_core_commit"] = expected_core_commit
    if expected_src_tree != PINNED_SRC_TREE:
        non_default_parameters["expected_src_tree"] = expected_src_tree
    if expected_matcher_revision != PINNED_MATCHER_REVISION:
        non_default_parameters["expected_matcher_revision"] = expected_matcher_revision
    if expected_terms_sha256 != PINNED_TERMS_DB_SHA256:
        non_default_parameters["expected_terms_sha256"] = expected_terms_sha256
    if expected_scorer_sha256 != PINNED_SCORER_SHA256:
        non_default_parameters["expected_scorer_sha256"] = expected_scorer_sha256

    bindings, c10, core = build_bindings_and_c10(
        bound_tree=bound_tree,
        core_repo=core_repo,
        core_commit=core_commit,
        freeze_core_commit=freeze_core_commit,
        expected_core_commit=expected_core_commit,
        expected_freeze_core_commit=expected_freeze_core_commit,
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
        annotation_adjudication_path=annotation_adjudication_path,
        metadata_path=metadata_path,
        prior_ids_path=prior_ids_path,
        prior_text_paths=prior_text_paths,
        clear_name_paths=clear_name_paths,
        community_lexicon_paths=community_lexicon_paths,
        upstream_clearances_path=upstream_clearances_path,
        non_default_parameters=non_default_parameters,
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

    adjudication_raw: Any = None
    adjudication_err: str | None = None
    if annotation_adjudication_path is not None:
        try:
            adjudication_raw = load_json_strict(
                annotation_adjudication_path, label=annotation_adjudication_path.name
            )
        except _CheckError as exc:
            adjudication_err = exc.code

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
        manifest_docs=manifest_docs,
        sample_paragraphs=sample_paragraphs,
        metadata_by_doc=metadata_by_doc,
        announcement_floor_zh_en=eff_ann_zh,
        announcement_floor_en_zh=eff_ann_en,
        floor_config_error=floor_err,
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
        freeze_core_commit=freeze_core_commit,
        clear_name_paths=clear_name_paths,
        community_lexicon_paths=community_lexicon_paths,
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
        adjudication_raw=adjudication_raw,
        adjudication_load_error=adjudication_err,
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
        "non_default_parameters": dict(sorted(non_default_parameters.items())),
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
    acc.add_argument(
        "--annotation-adjudication",
        metavar="ADJUDICATION.json",
        help="h3-annotation-adjudication-v1 dual-annotation adjudication summary (or embed in annotation files)",
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
        help=f"Core tag commit scanned by C4 (default: {PINNED_CORE_COMMIT})",
    )
    acc.add_argument(
        "--freeze-core-commit",
        required=True,
        metavar="SHA",
        help="mandatory freeze-time main commit (strict descendant of --core-commit); C4 scans the union of tracked text blobs",
    )
    acc.add_argument("--terms-db", required=True, metavar="TERMS.db", help="bound production terms.db")
    acc.add_argument(
        "--expected-core-commit",
        default=PINNED_CORE_COMMIT,
        metavar="SHA",
        help="expected Core commit SHA for C10",
    )
    acc.add_argument(
        "--expected-freeze-core-commit",
        default=None,
        metavar="SHA",
        help="optional expected freeze-time Core commit SHA for C10",
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
        help="M1 frozen upstream official name file (repeatable)",
    )
    acc.add_argument(
        "--community-lexicon",
        action="append",
        default=[],
        metavar="PATH",
        help="M1 explicit frozen H3 community lexicon file (repeatable)",
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
        help="explicit override for both directions' C3 announcement floor (recorded in non_default_parameters)",
    )
    acc.add_argument(
        "--announcement-floor-zh-en",
        type=int,
        default=None,
        metavar="N",
        help="explicit L3 override for zh->en C3 announcement floor",
    )
    acc.add_argument(
        "--announcement-floor-en-zh",
        type=int,
        default=None,
        metavar="N",
        help="explicit L3 override for en->zh C3 announcement floor",
    )
    acc.add_argument(
        "--m2-max-shared-span",
        type=int,
        default=DEFAULT_M2_MAX_SHARED_SPAN,
        metavar="T",
        help=f"C4 M2 max shared fragment length threshold (strictly < T; default: {DEFAULT_M2_MAX_SHARED_SPAN})",
    )
    acc.add_argument(
        "--cutoff",
        default=DEFAULT_CUTOFF_ISO,
        metavar="ISO8601",
        help=f"C7 strict publication cutoff (< cutoff; default: {DEFAULT_CUTOFF_ISO})",
    )
    acc.add_argument("--json-out", metavar="RECEIPT.json", help="write h3-acceptance-v1 receipt to file")

    ver = sub.add_parser("verify-receipt", help="validate an h3-acceptance-v1 receipt file")
    ver.add_argument("--receipt", required=True, metavar="RECEIPT.json")
    ver.add_argument("--expected-manifest-sha256", metavar="SHA")
    ver.add_argument("--expected-sample-sha256", metavar="SHA")
    ver.add_argument("--expected-names-sha256", metavar="SHA")
    ver.add_argument("--expected-community-names-sha256", metavar="SHA")
    ver.add_argument("--expected-metadata-sha256", metavar="SHA")
    ver.add_argument("--expected-src-tree", default=PINNED_SRC_TREE, metavar="SHA")
    ver.add_argument("--expected-core-commit", default=PINNED_CORE_COMMIT, metavar="SHA")
    ver.add_argument("--expected-freeze-core-commit", default=None, metavar="SHA")
    ver.add_argument("--expected-terms-sha256", default=PINNED_TERMS_DB_SHA256, metavar="SHA")
    ver.add_argument("--expected-matcher-revision", default=PINNED_MATCHER_REVISION, metavar="SHA")
    ver.add_argument("--expected-scorer-sha256", default=PINNED_SCORER_SHA256, metavar="SHA")
    ver.add_argument("--expected-gate-sha256", default=None, metavar="SHA")
    ver.add_argument("--allow-non-default-floor", action="store_true")
    ver.add_argument("--allow-non-default-m2-threshold", action="store_true")
    ver.add_argument("--allow-non-default-cutoff", action="store_true")
    ver.add_argument("--allow-non-default-bindings", action="store_true")
    return parser


def cmd_accept(args: argparse.Namespace) -> int:
    bound_tree = Path(args.bound_tree).resolve()
    core_repo = Path(args.core_repo).resolve() if args.core_repo else bound_tree
    receipt = run_acceptance(
        manifest_path=Path(args.manifest).resolve(),
        sample_path=Path(args.sample).resolve(),
        names_path=Path(args.names).resolve(),
        community_names_path=Path(args.community_names).resolve(),
        annotation_adjudication_path=(
            Path(args.annotation_adjudication).resolve() if args.annotation_adjudication else None
        ),
        metadata_path=Path(args.metadata).resolve() if args.metadata else None,
        prior_text_paths=[Path(p).resolve() for p in args.prior_texts],
        prior_ids_path=Path(args.prior_ids).resolve(),
        bound_tree=bound_tree,
        core_repo=core_repo,
        core_commit=args.core_commit,
        freeze_core_commit=args.freeze_core_commit,
        terms_db=Path(args.terms_db).resolve(),
        expected_core_commit=args.expected_core_commit,
        expected_freeze_core_commit=args.expected_freeze_core_commit,
        expected_src_tree=args.expected_src_tree,
        expected_matcher_revision=args.expected_matcher_revision,
        expected_terms_sha256=args.expected_terms_sha256,
        scorer_path=Path(args.scorer_path).resolve(),
        expected_scorer_sha256=args.expected_scorer_sha256,
        clear_name_paths=[Path(p).resolve() for p in args.clear_names],
        community_lexicon_paths=[Path(p).resolve() for p in args.community_lexicon],
        upstream_clearances_path=Path(args.upstream_clearances).resolve() if args.upstream_clearances else None,
        announcement_floor=args.announcement_floor,
        announcement_floor_zh_en=args.announcement_floor_zh_en,
        announcement_floor_en_zh=args.announcement_floor_en_zh,
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
        gate_sha = _DEFAULT_GATE_SHA if args.expected_gate_sha256 is None else args.expected_gate_sha256
        validate_acceptance_receipt(
            data,
            expected_manifest_sha256=args.expected_manifest_sha256,
            expected_sample_sha256=args.expected_sample_sha256,
            expected_names_sha256=args.expected_names_sha256,
            expected_community_names_sha256=args.expected_community_names_sha256,
            expected_metadata_sha256=args.expected_metadata_sha256,
            expected_src_tree=args.expected_src_tree,
            expected_core_commit=args.expected_core_commit,
            expected_freeze_core_commit=args.expected_freeze_core_commit,
            expected_terms_sha256=args.expected_terms_sha256,
            expected_matcher_revision=args.expected_matcher_revision,
            expected_scorer_sha256=args.expected_scorer_sha256,
            expected_gate_sha256=gate_sha,
            allow_non_default_floor=args.allow_non_default_floor,
            allow_non_default_m2_threshold=args.allow_non_default_m2_threshold,
            allow_non_default_cutoff=args.allow_non_default_cutoff,
            allow_non_default_bindings=args.allow_non_default_bindings,
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
