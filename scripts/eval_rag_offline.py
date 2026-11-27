#!/usr/bin/env python3
"""Zero-cost offline RAG evaluation harness (official dictionary locking vs +knowledge.db).

Compares two deterministic offline arms with zero LLM calls and zero network access:
  - Baseline: official ``terms.db`` locking & contextual references only
  - RAG arm: official ``terms.db`` + companion read-only ``knowledge.db`` soft recall
    (exact aliases + short-term n-gram index + FTS5 trigram + 1-hop EntityGraph)

Reports 6 deterministic metrics in JSON and Markdown:
  1. Ordinary-word false-lock & false-injection rate (must be 0.0%, incl. 香椿/臭椿/心情/encore)
  2. Domain entity & short-term recall / coverage lift (+ 1-hop graph disambiguation)
  3. Lock-isolation integrity (100% ``lock_eligible == False``, zero lock-span conflicts,
     and byte-identical default ``OperationIdentity``)
  4. Query latency P50 / P95 (ms)
  5. Companion KB + EntityGraph RSS / tracemalloc delta (MiB)
  6. Structured JSON + Markdown summary output (``--synthetic`` or real DB paths)
"""

from __future__ import annotations

import argparse
import gc
import json
import resource
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from wuwaterm.constants import (  # noqa: E402
    FREE_TEXT_LOCK_EXCLUSIONS_KEY,
    FREE_TEXT_LOCK_EXCLUSIONS_VERSION,
    FREE_TEXT_LOCK_MAX_ALIGNED_RATIO,
    FREE_TEXT_LOCK_MIN_OCCURRENCES,
)
from wuwaterm.data_source import SourceProvenance  # noqa: E402
from wuwaterm.db import connect, initialize, insert_records  # noqa: E402
from wuwaterm.domain_knowledge import (  # noqa: E402
    build_normalized_index,
    find_normalized_spans,
)
from wuwaterm.knowledge_builder import (  # noqa: E402
    AliasSpec,
    EntitySpec,
    RelationSpec,
    create_knowledge_database,
)
from wuwaterm.knowledge_db import (  # noqa: E402
    KnowledgeBaseReader,
    compute_terms_db_revision,
)
from wuwaterm.models import TermRecord  # noqa: E402
from wuwaterm.normalize import normalize_text  # noqa: E402
from wuwaterm.retrieval_vector import (  # noqa: E402
    DEFAULT_VECTOR_DIM,
    DeterministicHashEncoder,
    OnnxInt8TextEncoder,
    QuantizationMode,
    VectorEncoder,
    VectorIndexBackend,
    compute_file_sha256,
    hybrid_fuse_candidates,
    open_vector_backend,
    vector_hits_to_candidates,
)
from wuwaterm.sentence import SentenceTranslator  # noqa: E402
from wuwaterm.translation_plan import (  # noqa: E402
    DEFAULT_PROVIDER_PROFILE,
    RequestPolicy,
    build_translation_request_spec,
    official_source_surfaces,
)

SCHEMA_VERSION = "wuwaterm-rag-offline-eval-v1"
MAX_GRAPH_MEMORY_MIB = 3.0


@dataclass(frozen=True)
class NegativeCase:
    case_id: str
    text: str
    to_chinese: bool
    forbidden_surfaces: tuple[str, ...]


@dataclass(frozen=True)
class PositiveCase:
    case_id: str
    category: str
    text: str
    to_chinese: bool
    expected_surfaces: tuple[str, ...]
    short_term: bool = False


@dataclass(frozen=True)
class GraphDisambiguationCase:
    case_id: str
    text: str
    to_chinese: bool
    target_surface: str
    expect_boost: bool


SYNTHETIC_NEGATIVE_CASES: tuple[NegativeCase, ...] = (
    NegativeCase(
        "neg_xiangchun",
        "今天晚饭吃香椿炒蛋。",
        False,
        ("椿", "香椿", "Camellya"),
    ),
    NegativeCase(
        "neg_chouchun",
        "院子里的臭椿树发芽了。",
        False,
        ("椿", "臭椿", "椿树", "Camellya"),
    ),
    NegativeCase(
        "neg_chunya",
        "春天采摘新鲜的椿芽做菜。",
        False,
        ("椿", "椿芽", "Camellya"),
    ),
    NegativeCase(
        "neg_xinqing",
        "今天天气很好，心情也特别轻松。",
        False,
        ("心", "心情", "Hsin"),
    ),
    NegativeCase(
        "neg_encore",
        "The audience applauded loudly and asked for an encore.",
        True,
        ("encore", "Encore", "安可"),
    ),
    NegativeCase(
        "neg_tuner",
        "He borrowed a clip-on guitar tuner before rehearsal.",
        True,
        ("tuner", "Tuner", "调谐器", "调音师"),
    ),
    NegativeCase(
        "neg_echo",
        "We heard a faint echo across the quiet mountain valley.",
        True,
        ("echo", "Echo", "声骸"),
    ),
)

SYNTHETIC_POSITIVE_CASES: tuple[PositiveCase, ...] = (
    PositiveCase(
        "pos_short_glacio",
        "short_term",
        "这套声骸可以大幅提高冷凝伤害。",
        False,
        ("冷凝", "Glacio"),
        short_term=True,
    ),
    PositiveCase(
        "pos_short_midnight_rangers",
        "short_term",
        "夜归长期驻守在今州边疆。",
        False,
        ("夜归", "Midnight Rangers"),
        short_term=True,
    ),
    PositiveCase(
        "pos_short_havoc",
        "short_term",
        "湮灭属性适合应对高抗性敌人。",
        False,
        ("湮灭", "Havoc"),
        short_term=True,
    ),
    PositiveCase(
        "pos_alias_te_anchor",
        "alias",
        "夜归军团的先锋负责侦察前线。",
        False,
        ("夜归", "夜归军团", "Midnight Rangers"),
        short_term=False,
    ),
    PositiveCase(
        "pos_weapon_and_passive",
        "weapon",
        "装备千古洑流并触发流光洄溯效果。",
        False,
        ("千古洑流", "Emerald of Genesis"),
        short_term=False,
    ),
    PositiveCase(
        "pos_resonant_chain",
        "resonant_chain",
        "解锁青蓝澄澈的初风节点后强化输出。",
        False,
        ("青蓝澄澈的初风", "Azure Breeze"),
        short_term=False,
    ),
    PositiveCase(
        "pos_monster",
        "monster",
        "前方区域出现了啸叫种残象。",
        False,
        ("啸叫种残象", "Whisperin TD"),
        short_term=False,
    ),
    PositiveCase(
        "pos_area",
        "area",
        "任务指引要求返回今州城中心广场。",
        False,
        ("今州城", "Jinzhou"),
        short_term=False,
    ),
    PositiveCase(
        "pos_en_short_glacio",
        "short_term",
        "Equip this set for extra Glacio bonus.",
        True,
        ("冷凝", "Glacio"),
        short_term=True,
    ),
    PositiveCase(
        "pos_en_substring_weapon",
        "weapon",
        "Emerald of Genesis increases energy regen.",
        True,
        ("千古洑流", "Emerald of Genesis"),
        short_term=False,
    ),
    PositiveCase(
        "pos_en_capitalized_encore",
        "resonator",
        "Encore and Aalto report to the Black Shores.",
        True,
        ("安可", "Encore"),
        short_term=False,
    ),
)

SYNTHETIC_GRAPH_CASES: tuple[GraphDisambiguationCase, ...] = (
    GraphDisambiguationCase(
        "graph_pos_black_shores_camellya",
        "黑海岸的椿施放了执花。",
        False,
        "椿",
        expect_boost=True,
    ),
    GraphDisambiguationCase(
        "graph_neg_xiangchun_everyday",
        "今天晚饭吃香椿炒蛋。",
        False,
        "椿",
        expect_boost=False,
    ),
)


def _current_rss_mib() -> float:
    """Return current process RSS in MiB (Linux /proc/self/status with ru_maxrss fallback)."""
    status_path = Path("/proc/self/status")
    if status_path.is_file():
        try:
            for line in status_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return float(int(parts[1])) / 1024.0
        except OSError:
            pass
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return float(usage) / divisor


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _build_synthetic_fixture(root: Path) -> tuple[Path, Path]:
    """Create a deterministic synthetic ``terms.db`` + ``knowledge.db`` pair."""
    root.mkdir(parents=True, exist_ok=True)
    terms_path = root / "terms.db"
    kb_path = root / "knowledge.db"

    with closing(connect(terms_path)) as conn:
        initialize(conn)
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (
                ("schema_version", "2"),
                ("source_commit", "synthetic-eval-commit"),
                ("source_game_version", "3.7.0"),
                (
                    FREE_TEXT_LOCK_EXCLUSIONS_KEY,
                    json.dumps(
                        {
                            "version": FREE_TEXT_LOCK_EXCLUSIONS_VERSION,
                            "min_occurrences": FREE_TEXT_LOCK_MIN_OCCURRENCES,
                            "max_aligned_ratio": list(FREE_TEXT_LOCK_MAX_ALIGNED_RATIO),
                            "surfaces": [
                                ["Echo", max(100, FREE_TEXT_LOCK_MIN_OCCURRENCES), 0],
                                ["Encore", max(100, FREE_TEXT_LOCK_MIN_OCCURRENCES), 0],
                                ["Tuner", max(100, FREE_TEXT_LOCK_MIN_OCCURRENCES), 0],
                            ],
                        },
                        ensure_ascii=False,
                    ),
                ),
            ),
        )
        # Keep terms.db focused on a core subset (resonator/core_term/location)
        # so the companion knowledge.db demonstrates real recall lift on elements,
        # short terms, aliases, weapons, monsters, resonant chains, and 1-hop graph.
        insert_records(
            conn,
            [
                TermRecord("resonator", "fixture", "1603", "RoleInfo_1603_Name", "椿", "Camellya"),
                TermRecord("resonator", "fixture", "1699", "RoleInfo_1699_Name", "心", "Hsin"),
                TermRecord("resonator", "fixture", "1203", "RoleInfo_1203_Name", "安可", "Encore"),
                TermRecord("core_term", "fixture", "1", "Term_Echo", "声骸", "Echo"),
                TermRecord("item", "fixture", "2", "Item_Tuner", "调谐器", "Tuner"),
                TermRecord("location", "fixture", "10", "Area_BlackShores", "黑海岸", "Black Shores"),
            ],
        )
        conn.commit()

    revision = compute_terms_db_revision(terms_path)
    provenance = SourceProvenance(
        profile="arikatsu",
        repo_url="https://example.invalid/wutheringdata.git",
        commit="synthetic-eval-commit",
        game_version="3.7.0",
        resource_version="3.7.8",
        changelist="8975829",
    )
    entities = [
        EntitySpec(
            "resonator:1603",
            "resonator",
            "BinData/role/roleinfo.json",
            "1603",
            "椿",
            "Camellya",
            context_gloss="黑海岸执花 | Bloom Bearer of the Black Shores",
        ),
        EntitySpec(
            "resonator:1699",
            "resonator",
            "BinData/role/roleinfo.json",
            "1699",
            "心",
            "Hsin",
        ),
        EntitySpec(
            "resonator:1203",
            "resonator",
            "BinData/role/roleinfo.json",
            "1203",
            "安可",
            "Encore",
            context_gloss="黑海岸成员安可 | Encore of the Black Shores",
        ),
        EntitySpec(
            "term:850082",
            "term",
            "BinData/Term/termconfig.json",
            "850082",
            "声骸",
            "Echo",
            context_gloss="残象留下的声骸频率 | Echo left by a Tacet Discord",
        ),
        EntitySpec(
            "item:tuner",
            "item",
            "BinData/item/iteminfo.json",
            "500001",
            "调谐器",
            "Tuner",
            context_gloss="用于声骸副词条调谐的材料 | Material used to tune Echoes",
        ),
        EntitySpec(
            "influence:black_shores",
            "influence",
            "BinData/influence/influence.json",
            "10",
            "黑海岸",
            "Black Shores",
            context_gloss="神秘组织黑海岸 | The Black Shores organization",
        ),
        EntitySpec(
            "skill:bloom_bearer",
            "skill",
            "BinData/skill/skill.json",
            "160301",
            "执花",
            "Bloom Bearer",
            context_gloss="黑海岸执花专属称号与技能 | Signature title and skill",
        ),
        EntitySpec(
            "element:1",
            "element",
            "BinData/element_info/elementinfo.json",
            "1",
            "冷凝",
            "Glacio",
            context_gloss="冷凝属性伤害 | Glacio elemental attribute",
        ),
        EntitySpec(
            "element:6",
            "element",
            "BinData/element_info/elementinfo.json",
            "6",
            "湮灭",
            "Havoc",
            context_gloss="湮灭属性伤害 | Havoc elemental attribute",
        ),
        EntitySpec(
            "term:850086",
            "term",
            "BinData/Term/termconfig.json",
            "850086",
            "夜归",
            "Midnight Rangers",
            context_gloss="守卫今州边疆的夜归军 | Midnight Rangers guarding Jinzhou",
        ),
        EntitySpec(
            "weapon:21020011",
            "weapon",
            "BinData/weapon/weaponconf.json",
            "21020011",
            "千古洑流",
            "Emerald of Genesis",
            context_gloss="五星迅刀千古洑流 | Five-star sword Emerald of Genesis",
        ),
        EntitySpec(
            "weapon_reson:1",
            "weapon_passive",
            "BinData/weapon/weaponreson.json",
            "1",
            "流光洄溯",
            "Stormy Resolution",
            context_gloss="千古洑流谐振被动 | Weapon passive effect",
        ),
        EntitySpec(
            "resonant_chain:1",
            "resonant_chain",
            "BinData/resonate_chain/resonantchain.json",
            "1",
            "青蓝澄澈的初风",
            "Azure Breeze",
            context_gloss="共鸣链节点 | Resonance Chain node",
        ),
        EntitySpec(
            "monster:310000010",
            "monster",
            "BinData/monster_Info/monsterinfo.json",
            "310000010",
            "啸叫种残象",
            "Whisperin TD",
            context_gloss="今州外围常见残象 | Common Tacet Discord near Jinzhou",
        ),
        EntitySpec(
            "area:3",
            "area",
            "BinData/area/area.json",
            "3",
            "今州城",
            "Jinzhou",
            context_gloss="瑝珑边城今州城 | Border city of Huanglong",
        ),
    ]
    aliases = [
        AliasSpec("term:850086", "夜归军团", "zh", "te_anchor", "official_derived"),
        AliasSpec("term:850086", "夜归先锋", "zh", "te_anchor", "official_derived"),
        AliasSpec("resonator:1203", "Encore", "en", "nickname", "official_derived"),
        AliasSpec("term:850082", "Echo", "en", "te_anchor", "official_derived"),
        AliasSpec("item:tuner", "Tuner", "en", "te_anchor", "official_derived"),
    ]
    relations = [
        RelationSpec(
            "resonator:1603",
            "belongs_to_influence",
            "influence:black_shores",
            "BinData/role/roleinfo.json",
            "PartyId:10",
        ),
        RelationSpec(
            "resonator:1203",
            "belongs_to_influence",
            "influence:black_shores",
            "BinData/role/roleinfo.json",
            "PartyId:10",
        ),
        RelationSpec(
            "term:850082",
            "references_term",
            "item:tuner",
            "BinData/Term/termconfig.json",
            "Term:850082",
        ),
        RelationSpec(
            "resonator:1603",
            "has_skill",
            "skill:bloom_bearer",
            "BinData/skill/skill.json",
            "SkillId:160301",
        ),
        RelationSpec(
            "weapon:21020011",
            "has_weapon_passive",
            "weapon_reson:1",
            "BinData/weapon/weaponconf.json",
            "ResonId:21020011",
        ),
        RelationSpec(
            "monster:310000010",
            "inhabits_area",
            "area:3",
            "BinData/monster_Info/monsterinfo.json",
            "PerchId:3",
        ),
    ]
    create_knowledge_database(
        kb_path,
        entities=entities,
        aliases=aliases,
        contexts=[],
        relations=relations,
        terms_db_revision=revision,
        source_provenance=provenance,
    )
    return terms_path, kb_path


def _discover_real_cases(
    kb_path: Path,
) -> tuple[tuple[NegativeCase, ...], tuple[PositiveCase, ...], tuple[GraphDisambiguationCase, ...]]:
    """Derive deterministic positive, negative, and 1-hop probes from a real ``knowledge.db``."""
    conn = sqlite3.connect(f"{kb_path.resolve().as_uri()}?mode=ro", uri=True)
    with closing(conn):
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON;")

        pos_cases: list[PositiveCase] = []
        # 1. Sample deterministic short terms (2-char CJK) across key entity types
        short_rows = conn.execute(
            """
            SELECT entity_id, entity_type, zh, en
            FROM kb_entities
            WHERE LENGTH(zh) = 2 AND zh != en AND zh != '' AND en != ''
            ORDER BY entity_type, entity_id
            LIMIT 8
            """
        ).fetchall()
        for idx, row in enumerate(short_rows):
            zh = str(row["zh"])
            en = str(row["en"])
            pos_cases.append(
                PositiveCase(
                    case_id=f"real_short_{idx:02d}",
                    category=str(row["entity_type"]),
                    text=f"这段战斗说明提到了{zh}相关机制。",
                    to_chinese=False,
                    expected_surfaces=(zh, en),
                    short_term=True,
                )
            )

        # 2. Sample domain entities (weapon, monster, area, resonant_chain, element)
        for etype in ("weapon", "monster", "area", "resonant_chain", "element"):
            rows = conn.execute(
                """
                SELECT entity_id, zh, en
                FROM kb_entities
                WHERE entity_type = ? AND LENGTH(zh) >= 2 AND zh != '' AND en != ''
                ORDER BY entity_id
                LIMIT 2
                """,
                (etype,),
            ).fetchall()
            for idx, row in enumerate(rows):
                zh = str(row["zh"])
                en = str(row["en"])
                pos_cases.append(
                    PositiveCase(
                        case_id=f"real_{etype}_{idx:02d}",
                        category=etype,
                        text=f"档案记录中包含{zh}的详细信息。",
                        to_chinese=False,
                        expected_surfaces=(zh, en),
                        short_term=len(zh) <= 2,
                    )
                )

        # 3. Sample 1-hop graph pair from kb_relations
        graph_cases: list[GraphDisambiguationCase] = []
        rel_row = conn.execute(
            """
            SELECT s.zh AS src_zh, d.zh AS dst_zh
            FROM kb_relations r
            JOIN kb_entities s ON s.entity_id = r.src_entity_id
            JOIN kb_entities d ON d.entity_id = r.dst_entity_id
            WHERE LENGTH(s.zh) >= 2 AND LENGTH(d.zh) >= 2 AND s.zh != d.zh
            ORDER BY r.id
            LIMIT 1
            """
        ).fetchone()
        if rel_row is not None:
            src_zh = str(rel_row["src_zh"])
            dst_zh = str(rel_row["dst_zh"])
            graph_cases.append(
                GraphDisambiguationCase(
                    case_id="real_graph_pos_00",
                    text=f"关于{src_zh}与{dst_zh}的关联说明。",
                    to_chinese=False,
                    target_surface=dst_zh,
                    expect_boost=True,
                )
            )
        graph_cases.append(
            GraphDisambiguationCase(
                case_id="real_graph_neg_xiangchun",
                text="今天晚饭吃香椿炒蛋。",
                to_chinese=False,
                target_surface="椿",
                expect_boost=False,
            )
        )

    return SYNTHETIC_NEGATIVE_CASES, tuple(pos_cases), tuple(graph_cases)


def _official_matched_surfaces(
    locks: tuple[tuple[str, str, str], ...],
    references: tuple[Any, ...],
) -> set[str]:
    matched: set[str] = set()
    for _placeholder, zh, en in locks:
        if zh:
            matched.add(normalize_text(zh))
        if en:
            matched.add(normalize_text(en))
    for ref in references:
        for val in (ref.source, ref.zh, ref.en):
            if val:
                matched.add(normalize_text(val))
    return matched


def _check_lock_span_conflict(
    text: str,
    locked_surfaces: tuple[str, ...],
    candidate: Any,
) -> bool:
    """Return True if ``candidate`` conflicts with ``locked_surfaces`` in ``text``."""
    if not locked_surfaces:
        return False
    blocked_norm = {normalize_text(s) for s in locked_surfaces if s}
    for field in (candidate.source_surface, candidate.zh, candidate.en):
        if field and normalize_text(field) in blocked_norm:
            return True
    index = build_normalized_index(text)
    occupied: list[tuple[int, int]] = []
    for s in locked_surfaces:
        occupied.extend(find_normalized_spans(index, s))
    if not occupied:
        return False
    cand_spans = (
        candidate.matched_source_spans
        if candidate.matched_source_spans
        else find_normalized_spans(index, candidate.source_surface)
    )
    if not cand_spans:
        return False
    return all(
        any(occ_s <= s and e <= occ_e for occ_s, occ_e in occupied)
        for s, e in cand_spans
    )


def _load_entity_rows_map(kb_path: Path) -> dict[str, dict[str, object]]:
    """Load read-only entity metadata keyed by ``entity_id`` for dense hit hydration."""
    conn = sqlite3.connect(f"{kb_path.resolve().as_uri()}?mode=ro", uri=True)
    with closing(conn):
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON;")
        rows = conn.execute(
            "SELECT entity_id, entity_type, zh, en, trust_tier, context_gloss FROM kb_entities"
        ).fetchall()
    return {
        str(row["entity_id"]): {
            "entity_type": str(row["entity_type"]),
            "zh": str(row["zh"]),
            "en": str(row["en"]),
            "trust_tier": str(row["trust_tier"]),
            "context_gloss": str(row["context_gloss"]),
        }
        for row in rows
    }


def _vector_artifact_size_bytes(index_dir: Path, backend: str) -> int:
    if backend == "memmap":
        total = 0
        for name in ("knowledge-vec.bin", "knowledge-vec.meta.json"):
            p = index_dir / name
            if p.is_file():
                total += p.stat().st_size
        return total
    if backend == "sqlite-vec":
        p = index_dir / "knowledge-vec.sqlite3"
        return p.stat().st_size if p.is_file() else 0
    return 0


def evaluate_offline_rag(
    *,
    terms_db_path: Path,
    knowledge_db_path: Path,
    mode: str,
    iterations: int = 5,
    retrieval_mode: str = "lexical",
    encoder: VectorEncoder | None = None,
    vector_backend_name: str = "memmap",
    vector_dim: int = DEFAULT_VECTOR_DIM,
    vector_quantization: QuantizationMode = "int8",
    vector_index_dir: Path | None = None,
    min_vector_score: float = 0.15,
    negative_cases: tuple[NegativeCase, ...] | None = None,
    positive_cases: tuple[PositiveCase, ...] | None = None,
    graph_cases: tuple[GraphDisambiguationCase, ...] | None = None,
) -> dict[str, Any]:
    """Run the full 6-metric deterministic offline RAG evaluation."""
    if retrieval_mode not in ("lexical", "hybrid"):
        raise ValueError(f"unsupported retrieval_mode: {retrieval_mode!r}")
    iterations = max(1, int(iterations))
    if mode == "synthetic":
        neg_cases = negative_cases or SYNTHETIC_NEGATIVE_CASES
        pos_cases = positive_cases or SYNTHETIC_POSITIVE_CASES
        gr_cases = graph_cases or SYNTHETIC_GRAPH_CASES
    else:
        auto_neg, auto_pos, auto_gr = _discover_real_cases(knowledge_db_path)
        neg_cases = negative_cases or auto_neg
        pos_cases = positive_cases or auto_pos
        gr_cases = graph_cases or auto_gr

    baseline_translator = SentenceTranslator(terms_db_path)
    expected_revision = baseline_translator.service.snapshot().dictionary_identity.revision
    # Warm baseline snapshot before measuring companion KB + graph delta.
    _ = baseline_translator.service.snapshot().lockable_snapshot()

    rss_before_mib = _current_rss_mib()
    tracemalloc.start()
    try:
        kb_reader = KnowledgeBaseReader(
            knowledge_db_path, expected_terms_db_revision=expected_revision
        )
        gc.collect()
        graph_snap_before = tracemalloc.take_snapshot()
        graph = kb_reader.entity_graph
        gc.collect()
        graph_snap_after = tracemalloc.take_snapshot()
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    rss_after_mib = _current_rss_mib()

    graph_diff = graph_snap_after.compare_to(graph_snap_before, "lineno")
    graph_alloc_bytes = sum(max(0, stat.size_diff) for stat in graph_diff)
    graph_tracemalloc_mib = round(graph_alloc_bytes / (1024.0 * 1024.0), 4)
    kb_and_graph_rss_delta_mib = round(max(0.0, rss_after_mib - rss_before_mib), 4)
    kb_and_graph_tracemalloc_peak_mib = round(peak_bytes / (1024.0 * 1024.0), 4)
    kb_file_size_mib = round(
        knowledge_db_path.stat().st_size / (1024.0 * 1024.0), 4
    )

    rag_translator = SentenceTranslator(
        terms_db_path,
        service=baseline_translator.service,
        knowledge_db_path=knowledge_db_path,
    )

    vec_temp_dir: tempfile.TemporaryDirectory[str] | None = None
    vec_backend: VectorIndexBackend | None = None
    active_encoder: VectorEncoder | None = encoder
    entity_rows_map: dict[str, dict[str, object]] = {}
    vec_index_size_bytes = 0
    vec_build_ms = 0.0
    vec_meta_dict: dict[str, Any] | None = None

    if retrieval_mode == "hybrid":
        from scripts.build_knowledge_vectors import build_knowledge_vectors

        if active_encoder is None:
            active_encoder = DeterministicHashEncoder(dim=vector_dim)
        kb_sha256 = compute_file_sha256(knowledge_db_path)
        if vector_index_dir is None:
            vec_temp_dir = tempfile.TemporaryDirectory(prefix="wuwaterm-eval-vec-")
            resolved_vec_dir = Path(vec_temp_dir.name)
            t_vec0 = time.perf_counter()
            build_knowledge_vectors(
                knowledge_db_path,
                out_dir=resolved_vec_dir,
                backend=vector_backend_name,
                encoder=active_encoder,
                dim=active_encoder.dim,
                quantization=vector_quantization,
            )
            vec_build_ms = round((time.perf_counter() - t_vec0) * 1000.0, 3)
        else:
            resolved_vec_dir = vector_index_dir

        vec_backend = open_vector_backend(
            resolved_vec_dir,
            backend=vector_backend_name,
            expected_knowledge_db_sha256=kb_sha256,
            expected_terms_db_revision=expected_revision,
            expected_model_id=active_encoder.model_id,
            expected_dim=active_encoder.dim,
            expected_quantization=vector_quantization,
        )
        if vec_backend is None:
            if vec_temp_dir is not None:
                vec_temp_dir.cleanup()
            raise RuntimeError(
                "hybrid evaluation requested but companion vector backend failed validation or is unavailable"
            )
        vec_meta_dict = vec_backend.metadata.to_dict()
        vec_index_size_bytes = _vector_artifact_size_bytes(
            resolved_vec_dir, vector_backend_name
        )
        entity_rows_map = _load_entity_rows_map(knowledge_db_path)

    def retrieve_with_diagnostics(
        prepared_text: str,
        *,
        locked_surfaces: tuple[str, ...],
        reference_surfaces: tuple[str, ...],
    ) -> tuple[tuple[Any, ...], Any]:
        lex_cands, graph_outcome = kb_reader.query_with_diagnostics(
            prepared_text,
            locked_surfaces=locked_surfaces,
            reference_surfaces=reference_surfaces,
            use_graph=True,
        )
        if retrieval_mode != "hybrid" or vec_backend is None or active_encoder is None:
            return lex_cands, graph_outcome
        q_vecs = active_encoder.encode_queries([prepared_text])
        hits = vec_backend.search(q_vecs[0], top_k=16) if q_vecs else []
        vec_cands = vector_hits_to_candidates(
            hits,
            entity_rows_map,
            text=prepared_text,
            min_score=min_vector_score,
        )
        fused = hybrid_fuse_candidates(
            lex_cands,
            vec_cands,
            text=prepared_text,
            locked_surfaces=locked_surfaces,
            reference_surfaces=reference_surfaces,
        )
        return fused, graph_outcome

    try:
        # ------------------------------------------------------------------
        # Metric 1: Ordinary-word false-lock & false-injection rate
        # ------------------------------------------------------------------
        neg_results: list[dict[str, Any]] = []
        false_lock_count = 0
        false_injection_count = 0
        for case in neg_cases:
            prepared = baseline_translator.prepare_text(case.text)
            locked = baseline_translator._lock_terms_with_references(
                prepared,
                to_chinese=case.to_chinese,
                allow_plural_inflection=case.to_chinese,
                allow_nfkc_spelling=True,
                single_newline_paragraphs=True,
            )
            lock_idx = 2 if case.to_chinese else 1
            locked_surfaces = tuple(
                lock[lock_idx] for lock in locked.locks if len(lock) > lock_idx
            )
            ref_surfaces = official_source_surfaces(
                locked.locks, locked.references, to_chinese=case.to_chinese
            )
            candidates, graph_outcome = retrieve_with_diagnostics(
                prepared,
                locked_surfaces=locked_surfaces,
                reference_surfaces=ref_surfaces,
            )
            forbidden_norm = {normalize_text(s) for s in case.forbidden_surfaces}
            locked_hit = any(
                normalize_text(val) in forbidden_norm
                for _p, zh, en in locked.locks
                for val in (zh, en)
            )
            injected_hit = any(
                normalize_text(val) in forbidden_norm
                for cand in candidates
                for val in (cand.source_surface, cand.zh, cand.en)
            )
            if locked_hit:
                false_lock_count += 1
            if injected_hit:
                false_injection_count += 1
            neg_results.append(
                {
                    "case_id": case.case_id,
                    "locked_count": len(locked.locks),
                    "candidate_count": len(candidates),
                    "graph_triggers": graph_outcome.triggered_count,
                    "false_lock": locked_hit,
                    "false_injection": injected_hit,
                }
            )

        neg_total = max(1, len(neg_cases))
        ordinary_word_false_lock_rate = round(false_lock_count / neg_total, 6)
        ordinary_word_false_injection_rate = round(false_injection_count / neg_total, 6)

        # ------------------------------------------------------------------
        # Metric 2 & 3: Domain recall, coverage lift & lock-isolation checks
        # ------------------------------------------------------------------
        baseline_hits = 0
        rag_hits = 0
        short_total = 0
        short_baseline_hits = 0
        short_rag_hits = 0

        total_candidates_checked = 0
        lock_eligible_violations = 0
        lock_span_conflicts = 0
        pos_breakdown: list[dict[str, Any]] = []

        for case in pos_cases:
            prepared = baseline_translator.prepare_text(case.text)
            locked = baseline_translator._lock_terms_with_references(
                prepared,
                to_chinese=case.to_chinese,
                allow_plural_inflection=case.to_chinese,
                allow_nfkc_spelling=True,
                single_newline_paragraphs=True,
            )
            lock_idx = 2 if case.to_chinese else 1
            locked_surfaces = tuple(
                lock[lock_idx] for lock in locked.locks if len(lock) > lock_idx
            )
            ref_surfaces = official_source_surfaces(
                locked.locks, locked.references, to_chinese=case.to_chinese
            )
            candidates, _graph_out = retrieve_with_diagnostics(
                prepared,
                locked_surfaces=locked_surfaces,
                reference_surfaces=ref_surfaces,
            )

            expected_norm = {normalize_text(s) for s in case.expected_surfaces}
            official_norm = _official_matched_surfaces(locked.locks, locked.references)
            kb_norm = {
                normalize_text(val)
                for cand in candidates
                for val in (cand.source_surface, cand.zh, cand.en)
                if val
            }

            hit_baseline = bool(official_norm & expected_norm)
            hit_rag = bool((official_norm | kb_norm) & expected_norm)
            if hit_baseline:
                baseline_hits += 1
            if hit_rag:
                rag_hits += 1
            if case.short_term:
                short_total += 1
                if hit_baseline:
                    short_baseline_hits += 1
                if hit_rag:
                    short_rag_hits += 1

            for cand in candidates:
                total_candidates_checked += 1
                if cand.lock_eligible is not False:
                    lock_eligible_violations += 1
                if _check_lock_span_conflict(prepared, locked_surfaces, cand):
                    lock_span_conflicts += 1

            pos_breakdown.append(
                {
                    "case_id": case.case_id,
                    "category": case.category,
                    "short_term": case.short_term,
                    "baseline_hit": hit_baseline,
                    "rag_hit": hit_rag,
                    "candidate_count": len(candidates),
                }
            )

        # 1-hop graph disambiguation verification
        graph_results: list[dict[str, Any]] = []
        graph_positive_passed = True
        graph_negative_passed = True
        for gcase in gr_cases:
            prepared = baseline_translator.prepare_text(gcase.text)
            locked = baseline_translator._lock_terms_with_references(
                prepared,
                to_chinese=gcase.to_chinese,
                allow_plural_inflection=gcase.to_chinese,
                allow_nfkc_spelling=True,
                single_newline_paragraphs=True,
            )
            lock_idx = 2 if gcase.to_chinese else 1
            locked_surfaces = tuple(
                lock[lock_idx] for lock in locked.locks if len(lock) > lock_idx
            )
            ref_surfaces = official_source_surfaces(
                locked.locks, locked.references, to_chinese=gcase.to_chinese
            )
            # Evaluate pure KB graph disambiguation with explicit anchor surfaces
            # (exclude target from locked/reference so we directly observe score lift).
            target_norm = normalize_text(gcase.target_surface)
            non_target_locks = tuple(
                s for s in locked_surfaces if normalize_text(s) != target_norm
            )
            non_target_refs = tuple(
                s for s in ref_surfaces if normalize_text(s) != target_norm
            )
            no_graph_cands, _ = kb_reader.query_with_diagnostics(
                prepared,
                locked_surfaces=non_target_locks,
                reference_surfaces=non_target_refs,
                use_graph=False,
            )
            with_graph_cands, graph_out = kb_reader.query_with_diagnostics(
                prepared,
                locked_surfaces=non_target_locks,
                reference_surfaces=non_target_refs,
                use_graph=True,
            )

            score_without = max(
                (
                    c.score
                    for c in no_graph_cands
                    if normalize_text(c.source_surface) == target_norm
                    or normalize_text(c.zh) == target_norm
                ),
                default=0.0,
            )
            score_with = max(
                (
                    c.score
                    for c in with_graph_cands
                    if normalize_text(c.source_surface) == target_norm
                    or normalize_text(c.zh) == target_norm
                ),
                default=0.0,
            )

            if gcase.expect_boost:
                passed = graph_out.triggered_count > 0 and score_with > score_without
                if not passed:
                    graph_positive_passed = False
            else:
                passed = (
                    graph_out.triggered_count == 0 and len(with_graph_cands) == 0
                )
                if not passed:
                    graph_negative_passed = False

            graph_results.append(
                {
                    "case_id": gcase.case_id,
                    "expect_boost": gcase.expect_boost,
                    "score_without_graph": round(score_without, 6),
                    "score_with_graph": round(score_with, 6),
                    "graph_triggers": graph_out.triggered_count,
                    "candidate_count": len(with_graph_cands),
                    "passed": passed,
                }
            )

        # Verify default-off OperationIdentity byte invariance & prompt lock isolation
        probe_text = pos_cases[0].text if pos_cases else "夜归长期驻守在今州边疆。"
        _exact_a, plan_default_a = baseline_translator.plan_translation(
            probe_text, to_chinese=False
        )
        _exact_b, plan_default_b = SentenceTranslator(
            terms_db_path, service=baseline_translator.service
        ).plan_translation(probe_text, to_chinese=False)
        policy = RequestPolicy()
        default_op_identity_unchanged = False
        if plan_default_a is not None and plan_default_b is not None:
            spec_a = build_translation_request_spec(
                plan_default_a, policy=policy, model="offline-eval-model"
            )
            spec_b = build_translation_request_spec(
                plan_default_b, policy=policy, model="offline-eval-model"
            )
            op_a = spec_a.operation_identity(policy, DEFAULT_PROVIDER_PROFILE)
            op_b = spec_b.operation_identity(policy, DEFAULT_PROVIDER_PROFILE)
            default_op_identity_unchanged = (
                spec_a.system_prompt == spec_b.system_prompt
                and op_a.operation_sha256 == op_b.operation_sha256
            )

        _exact_rag, plan_rag = rag_translator.plan_translation(
            probe_text, to_chinese=False
        )
        rag_locks_isolated = (
            plan_default_a is not None
            and plan_rag is not None
            and len(plan_rag.locks) == len(plan_default_a.locks)
            and all(
                c.lock_eligible is False for c in plan_rag.domain_knowledge_candidates
            )
        )

        # ------------------------------------------------------------------
        # Metric 4: Query latency P50 / P95 (ms)
        # ------------------------------------------------------------------
        latency_texts = [
            (case.text, case.to_chinese) for case in (*pos_cases, *neg_cases)
        ]
        baseline_latencies_ms: list[float] = []
        rag_latencies_ms: list[float] = []
        for _ in range(iterations):
            for text, to_chinese in latency_texts:
                t0 = time.perf_counter()
                prepared = baseline_translator.prepare_text(text)
                locked = baseline_translator._lock_terms_with_references(
                    prepared,
                    to_chinese=to_chinese,
                    allow_plural_inflection=to_chinese,
                    allow_nfkc_spelling=True,
                    single_newline_paragraphs=True,
                )
                t1 = time.perf_counter()
                baseline_latencies_ms.append((t1 - t0) * 1000.0)

                lock_idx = 2 if to_chinese else 1
                locked_surfaces = tuple(
                    lock[lock_idx] for lock in locked.locks if len(lock) > lock_idx
                )
                ref_surfaces = official_source_surfaces(
                    locked.locks, locked.references, to_chinese=to_chinese
                )
                t2 = time.perf_counter()
                _ = retrieve_with_diagnostics(
                    prepared,
                    locked_surfaces=locked_surfaces,
                    reference_surfaces=ref_surfaces,
                )
                t3 = time.perf_counter()
                rag_latencies_ms.append(((t1 - t0) + (t3 - t2)) * 1000.0)

        pos_total = max(1, len(pos_cases))
        baseline_recall = round(baseline_hits / pos_total, 4)
        rag_recall = round(rag_hits / pos_total, 4)
        coverage_lift = round(rag_recall - baseline_recall, 4)

        short_denom = max(1, short_total)
        short_baseline_recall = round(short_baseline_hits / short_denom, 4)
        short_rag_recall = round(short_rag_hits / short_denom, 4)

        integrity_violations = lock_eligible_violations + lock_span_conflicts
        integrity_rate = (
            1.0
            if total_candidates_checked == 0
            else round(
                (total_candidates_checked - integrity_violations)
                / total_candidates_checked,
                6,
            )
        )
        all_lock_eligible_false = lock_eligible_violations == 0
        zero_lock_span_conflicts = lock_span_conflicts == 0

        gates_passed = (
            ordinary_word_false_lock_rate == 0.0
            and ordinary_word_false_injection_rate == 0.0
            and all_lock_eligible_false
            and zero_lock_span_conflicts
            and default_op_identity_unchanged
            and rag_locks_isolated
            and graph_positive_passed
            and graph_negative_passed
            and graph_tracemalloc_mib < MAX_GRAPH_MEMORY_MIB
        )

        vec_index_size_mib = round(vec_index_size_bytes / (1024.0 * 1024.0), 4)
        report: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "mode": mode,
            "retrieval_mode": retrieval_mode,
            "gates_passed": gates_passed,
            "ordinary_word_false_lock": {
                "false_lock_rate": ordinary_word_false_lock_rate,
                "false_injection_rate": ordinary_word_false_injection_rate,
                "false_locks": false_lock_count,
                "false_injections": false_injection_count,
                "total_negative_cases": len(neg_cases),
                "cases": neg_results,
            },
            "domain_recall": {
                "baseline_official_only": {
                    "hits": baseline_hits,
                    "total": len(pos_cases),
                    "recall_rate": baseline_recall,
                },
                "with_knowledge_rag": {
                    "hits": rag_hits,
                    "total": len(pos_cases),
                    "recall_rate": rag_recall,
                },
                "coverage_lift": coverage_lift,
                "short_term_recall": {
                    "total": short_total,
                    "baseline_hits": short_baseline_hits,
                    "baseline_rate": short_baseline_recall,
                    "rag_hits": short_rag_hits,
                    "rag_rate": short_rag_recall,
                    "lift": round(short_rag_recall - short_baseline_recall, 4),
                },
                "graph_disambiguation": {
                    "positive_passed": graph_positive_passed,
                    "negative_passed": graph_negative_passed,
                    "cases": graph_results,
                },
                "cases": pos_breakdown,
            },
            "lock_isolation_integrity": {
                "total_candidates_checked": total_candidates_checked,
                "lock_eligible_violations": lock_eligible_violations,
                "lock_span_conflicts": lock_span_conflicts,
                "all_lock_eligible_false": all_lock_eligible_false,
                "zero_lock_span_conflicts": zero_lock_span_conflicts,
                "integrity_rate": integrity_rate,
                "default_operation_identity_unchanged": default_op_identity_unchanged,
                "rag_locks_isolated": rag_locks_isolated,
            },
            "latency_ms": {
                "samples": len(rag_latencies_ms),
                "baseline_p50_ms": round(_percentile(baseline_latencies_ms, 50.0), 3),
                "baseline_p95_ms": round(_percentile(baseline_latencies_ms, 95.0), 3),
                "rag_p50_ms": round(_percentile(rag_latencies_ms, 50.0), 3),
                "rag_p95_ms": round(_percentile(rag_latencies_ms, 95.0), 3),
            },
            "memory_mib": {
                "kb_and_graph_rss_delta_mib": kb_and_graph_rss_delta_mib,
                "kb_and_graph_tracemalloc_peak_mib": kb_and_graph_tracemalloc_peak_mib,
                "graph_tracemalloc_mib": graph_tracemalloc_mib,
                "graph_memory_limit_mib": MAX_GRAPH_MEMORY_MIB,
                "kb_file_size_mib": kb_file_size_mib,
                "vector_index_file_size_mib": vec_index_size_mib,
                "graph_node_count": graph.node_count,
                "graph_edge_count": graph.edge_count,
            },
        }
        if retrieval_mode == "hybrid" and vec_meta_dict is not None:
            report["vector_index"] = {
                "metadata": vec_meta_dict,
                "index_size_bytes": vec_index_size_bytes,
                "index_size_mib": vec_index_size_mib,
                "build_ms": vec_build_ms,
            }
        return report
    finally:
        if vec_backend is not None:
            vec_backend.close()
        if vec_temp_dir is not None:
            vec_temp_dir.cleanup()
        rag_translator.close_knowledge_reader()
        kb_reader.close()


def run_synthetic_eval(
    *,
    iterations: int = 5,
    retrieval_mode: str = "lexical",
    encoder: VectorEncoder | None = None,
    vector_backend_name: str = "memmap",
    vector_dim: int = DEFAULT_VECTOR_DIM,
    vector_quantization: QuantizationMode = "int8",
) -> dict[str, Any]:
    """Run the self-contained synthetic offline RAG evaluation benchmark."""
    with tempfile.TemporaryDirectory(prefix="wuwaterm-eval-rag-") as tmp:
        terms_path, kb_path = _build_synthetic_fixture(Path(tmp))
        return evaluate_offline_rag(
            terms_db_path=terms_path,
            knowledge_db_path=kb_path,
            mode="synthetic",
            iterations=iterations,
            retrieval_mode=retrieval_mode,
            encoder=encoder,
            vector_backend_name=vector_backend_name,
            vector_dim=vector_dim,
            vector_quantization=vector_quantization,
        )


def format_markdown_report(report: dict[str, Any]) -> str:
    """Format an offline RAG evaluation report as a concise Markdown summary."""
    neg = report["ordinary_word_false_lock"]
    rec = report["domain_recall"]
    iso = report["lock_isolation_integrity"]
    lat = report["latency_ms"]
    mem = report["memory_mib"]
    short = rec["short_term_recall"]
    gr = rec["graph_disambiguation"]
    status = "PASS" if report["gates_passed"] else "FAIL"
    retrieval_mode = report.get("retrieval_mode", "lexical")
    arm_col = (
        "Official Lock + `knowledge.db` + Vector RRF"
        if retrieval_mode == "hybrid"
        else "Official Lock + `knowledge.db` RAG"
    )

    lines = [
        f"# WuwaTerm Offline RAG Evaluation ({report['mode']} — {status})",
        "",
        f"| Metric | Official Lock Only | {arm_col} | Gate / Notes |",
        "| --- | --- | --- | --- |",
        (
            f"| 1. Ordinary-word false-lock rate | "
            f"{neg['false_lock_rate'] * 100:.2f}% | "
            f"{neg['false_lock_rate'] * 100:.2f}% "
            f"(false-injection: {neg['false_injection_rate'] * 100:.2f}%) | "
            f"Hard gate: 0.00% ({neg['total_negative_cases']} negative cases) |"
        ),
        (
            f"| 2. Domain entity recall (short-term) | "
            f"{rec['baseline_official_only']['recall_rate'] * 100:.1f}% "
            f"({short['baseline_rate'] * 100:.1f}%) | "
            f"{rec['with_knowledge_rag']['recall_rate'] * 100:.1f}% "
            f"({short['rag_rate'] * 100:.1f}%) | "
            f"Coverage lift: +{rec['coverage_lift'] * 100:.1f}pp "
            f"(short-term: +{short['lift'] * 100:.1f}pp) |"
        ),
        (
            f"| 3. Lock-isolation integrity | N/A | "
            f"{iso['integrity_rate'] * 100:.2f}% "
            f"({iso['total_candidates_checked']} checked) | "
            f"lock_eligible=False: {iso['all_lock_eligible_false']}, "
            f"zero_conflicts: {iso['zero_lock_span_conflicts']}, "
            f"default_op_id_unchanged: {iso['default_operation_identity_unchanged']} |"
        ),
        (
            f"| 4. Query latency P50 / P95 | "
            f"{lat['baseline_p50_ms']:.3f} / {lat['baseline_p95_ms']:.3f} ms | "
            f"{lat['rag_p50_ms']:.3f} / {lat['rag_p95_ms']:.3f} ms | "
            f"{lat['samples']} samples (mode={retrieval_mode}) |"
        ),
        (
            f"| 5. KB & 1-hop graph memory | 0.00 MiB | "
            f"RSS delta {mem['kb_and_graph_rss_delta_mib']:.3f} MiB "
            f"(graph tracemalloc {mem['graph_tracemalloc_mib']:.4f} MiB, "
            f"< {mem['graph_memory_limit_mib']:.1f} MiB limit) | "
            f"nodes={mem['graph_node_count']}, edges={mem['graph_edge_count']}, "
            f"file={mem['kb_file_size_mib']:.3f} MiB |"
        ),
        (
            f"| 6. 1-hop graph disambiguation | N/A | "
            f"positive={gr['positive_passed']}, negative_zero_trigger={gr['negative_passed']} | "
            f"schema={report['schema_version']} |"
        ),
    ]
    vec_info = report.get("vector_index")
    if isinstance(vec_info, dict):
        vmeta = vec_info.get("metadata", {})
        lines.append(
            f"| 7. Vector index artifact ({vmeta.get('backend', 'memmap')}) | N/A | "
            f"{vec_info.get('index_size_bytes', 0)} bytes ({vec_info.get('index_size_mib', 0.0):.4f} MiB) | "
            f"model={vmeta.get('model_id', '')}, dim={vmeta.get('dim', 0)}, "
            f"quant={vmeta.get('quantization', '')}, build={vec_info.get('build_ms', 0.0):.2f} ms |"
        )
    lines.append("")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eval_rag_offline",
        description="Zero-cost offline RAG evaluation harness for WuwaTerm.",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Run self-contained synthetic benchmark (zero external DBs required).",
    )
    parser.add_argument(
        "--terms-db",
        type=Path,
        default=None,
        help="Path to terms.db (default: data/terms.db when not --synthetic).",
    )
    parser.add_argument(
        "--knowledge-db",
        type=Path,
        default=None,
        help="Path to companion knowledge.db (default: sibling knowledge.db of terms-db).",
    )
    parser.add_argument(
        "--mode",
        choices=("lexical", "hybrid"),
        default="lexical",
        help="Retrieval mode: pure lexical RAG (default) or lexical+vector RRF hybrid.",
    )
    parser.add_argument(
        "--fake-encoder",
        action="store_true",
        help="Use deterministic zero-dep hash encoder for --mode hybrid offline regression.",
    )
    parser.add_argument(
        "--vec-backend",
        choices=("memmap", "sqlite-vec"),
        default="memmap",
        help="Vector index backend for --mode hybrid (default: memmap).",
    )
    parser.add_argument(
        "--vec-dim",
        type=int,
        default=DEFAULT_VECTOR_DIM,
        help=f"Vector embedding dimension for --mode hybrid (default: {DEFAULT_VECTOR_DIM}).",
    )
    parser.add_argument(
        "--vec-quantization",
        choices=("int8", "fp32"),
        default="int8",
        help="Vector quantization for --mode hybrid (default: int8).",
    )
    parser.add_argument(
        "--onnx-model",
        type=Path,
        default=None,
        help="Optional CPU ONNX model path for --mode hybrid.",
    )
    parser.add_argument(
        "--onnx-tokenizer",
        type=Path,
        default=None,
        help="Optional HuggingFace tokenizers JSON path for --onnx-model.",
    )
    parser.add_argument(
        "--model-id",
        type=str,
        default=None,
        help="Optional explicit model identifier for vector metadata.",
    )
    parser.add_argument(
        "--query-prefix",
        type=str,
        default="",
        help="Optional query prefix for ONNX encoder (e.g. 'query: ').",
    )
    parser.add_argument(
        "--passage-prefix",
        type=str,
        default="",
        help="Optional passage prefix for ONNX encoder (e.g. 'passage: ').",
    )
    parser.add_argument(
        "--vector-index-dir",
        type=Path,
        default=None,
        help="Optional pre-built vector index directory (otherwise built temporarily).",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=5,
        help="Number of timed iterations for latency P50/P95 sampling (default: 5).",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json", "both"),
        default="markdown",
        help="Stdout report format (default: markdown).",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Optional path to write full JSON evaluation report.",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=None,
        help="Optional path to write Markdown summary report.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with non-zero status if any evaluation gate fails.",
    )
    return parser.parse_args(argv)


def _resolve_encoder_from_args(args: argparse.Namespace) -> VectorEncoder | None:
    if args.mode != "hybrid":
        return None
    if args.fake_encoder:
        return DeterministicHashEncoder(
            dim=args.vec_dim,
            model_id=args.model_id or "fake-hash-ngram-v1",
        )
    if args.onnx_model is not None:
        return OnnxInt8TextEncoder(
            model_path=args.onnx_model,
            tokenizer_path=args.onnx_tokenizer,
            model_id=args.model_id or args.onnx_model.stem,
            dim=args.vec_dim,
            query_prefix=args.query_prefix,
            passage_prefix=args.passage_prefix,
        )
    raise ValueError(
        "--mode hybrid requires either --fake-encoder or --onnx-model"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    quant: QuantizationMode = "int8" if args.vec_quantization == "int8" else "fp32"
    try:
        encoder = _resolve_encoder_from_args(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.synthetic:
        report = run_synthetic_eval(
            iterations=args.iterations,
            retrieval_mode=args.mode,
            encoder=encoder,
            vector_backend_name=args.vec_backend,
            vector_dim=args.vec_dim,
            vector_quantization=quant,
        )
    else:
        terms_path = args.terms_db or (ROOT / "data" / "terms.db")
        kb_path = args.knowledge_db or terms_path.with_name("knowledge.db")
        if not terms_path.is_file() or terms_path.stat().st_size == 0:
            print(
                f"error: terms database missing or empty: {terms_path.name}",
                file=sys.stderr,
            )
            return 2
        if not kb_path.is_file() or kb_path.stat().st_size == 0:
            print(
                f"error: companion knowledge database missing or empty: {kb_path.name}",
                file=sys.stderr,
            )
            return 2
        report = evaluate_offline_rag(
            terms_db_path=terms_path,
            knowledge_db_path=kb_path,
            mode="real",
            iterations=args.iterations,
            retrieval_mode=args.mode,
            encoder=encoder,
            vector_backend_name=args.vec_backend,
            vector_dim=args.vec_dim,
            vector_quantization=quant,
            vector_index_dir=args.vector_index_dir,
        )

    json_text = json.dumps(report, ensure_ascii=False, indent=2)
    md_text = format_markdown_report(report)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json_text + "\n", encoding="utf-8")
    if args.markdown_out is not None:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(md_text + "\n", encoding="utf-8")

    if args.format == "json":
        print(json_text)
    elif args.format == "both":
        print(md_text)
        print(json_text)
    else:
        print(md_text)

    if args.strict and not report["gates_passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
