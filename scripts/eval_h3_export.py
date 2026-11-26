#!/usr/bin/env python3
"""H3 arm-scores exporter and pre-registered decision rule.

Protocol: ``docs/eval/h3-and-raw-baseline-protocol.md`` (§4, §4.1, §4.2, §5.3);
judge-facing community-term rules: ``docs/eval/h3-judging-addendum.md``.

``export-scores``  resolve every H3 item to its final judgment (``j1`` and ``j2``
                   agreeing, otherwise ``adj``), check each judged item against
                   the arm outputs whose SHA-256 ``h3-ledger.json`` records, and
                   write one ``usability-arm-scores-v1`` file per arm, ``en->zh``-only
                   files of ``raw_baseline`` and ``wuwaterm_prod`` for the
                   ``raw_matched`` comparisons, and ``export-summary.json``.
``decide``         apply the §4.2 rule (``A``-rate verdict plus the critical-error
                   non-worsening gate, parameters read from the ledger's hashed
                   bindings) to the primary ``usability-paired-compare-v1`` outputs.

Standard library only, like the scorer. The frozen scorer
(``scripts/eval_usability.py``) is loaded from this directory after its SHA-256
is checked, and its loaders, consistency rules and MQM weights are the ones
used here, so an exported file scores exactly as ``paired-compare`` reads it.

Exit codes: 0 written; 2 invalid input (the message names the file and the
rule, never evaluation text); 3 at least one item still pending (nothing is
written).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCORER_PATH = HERE / "eval_usability.py"
PINNED_SCORER_SHA256 = "8b17d3ae75f7aafebaf537246fcaa1e56c1e52cb1a4bf5c875c599c42622d796"

LEDGER_SCHEMA = "h3-ledger-v1"
OUTPUTS_SCHEMA = "h3-arm-outputs-v1"
COMMUNITY_SCHEMA = "h3-community-names-v1"
H3_JUDGMENT_SCHEMA = "h3-judgment-v1"
SUMMARY_SCHEMA = "h3-export-summary-v1"
DECISION_SCHEMA = "h3-decision-v1"
LOG_NAME = "h3-ledger-log.jsonl"

ARM_DIRECTIONS = {
    "raw_baseline": ("zh->en", "en->zh"),
    "wuwaterm_prod": ("zh->en", "en->zh"),
    "raw_matched": ("en->zh",),
}
EN_ZH_ONLY_COPIES = ("raw_baseline", "wuwaterm_prod")
# The full evaluation's raw_matched runs in its own budget stage (protocol §6.3).
RECORD_STAGE = {("full", "raw_matched"): "ablation"}
SAMPLE_DIRECTION = {"zh-en": "zh->en", "en-zh": "en->zh"}

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_PENDING = 3


class InputError(Exception):
    """Invalid input; names the file and the rule, never evaluation text."""


class Pending(Exception):
    pass


def fail(where: str, rule: str) -> Any:
    raise InputError(f"{where}: {rule}")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_scorer() -> Any:
    if not SCORER_PATH.is_file() or sha256_file(SCORER_PATH) != PINNED_SCORER_SHA256:
        fail(str(SCORER_PATH.name), "the frozen scorer's SHA-256 differs from the pinned value")
    name = "eval_usability"
    loaded = sys.modules.get(name)
    if loaded is not None and Path(str(loaded.__file__)).resolve() == SCORER_PATH:
        return loaded
    spec = importlib.util.spec_from_file_location(name, SCORER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail(path.name, "not a readable JSON file")


def verify_ledger(path: Path) -> dict[str, Any]:
    """Load ``h3-ledger.json`` and check its hash-chained log against the recorded head."""
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema") != LEDGER_SCHEMA:
        fail(path.name, f"schema must be {LEDGER_SCHEMA}")
    head: str | None = None
    count = 0
    log_path = path.parent / LOG_NAME
    if log_path.is_file():
        for raw in log_path.read_bytes().splitlines():
            count += 1
            try:
                entry = json.loads(raw)
            except ValueError:
                fail(LOG_NAME, f"line {count} is not JSON")
            if entry.get("seq") != count or entry.get("prev_sha256") != head:
                fail(LOG_NAME, f"hash chain broken at line {count}")
            head = hashlib.sha256(raw).hexdigest()
    state = data.get("log") or {}
    if count != state.get("lines") or head != state.get("head_sha256"):
        fail(LOG_NAME, "does not match the head recorded in the ledger")
    return data


# ------------------------------------------------------------------ judgments


@dataclass(frozen=True)
class CommunityJudgment:
    label: str
    rendering: str | None
    misleading: bool


@dataclass(frozen=True)
class H3Judgment:
    base: Any  # eval_usability.Judgment
    community: dict[str, CommunityJudgment]


def load_community(ev: Any, path: Path, paragraphs: dict[str, Any], seen: set[str]) -> dict[str, list[str]]:
    data = ev.obj(ev.load_json(str(path)), path.name)
    ev.check_schema(data, COMMUNITY_SCHEMA, path.name)
    ev.need_keys(data, path.name, ("occurrences",))
    found: dict[str, list[str]] = {}
    for index, raw in enumerate(ev.arr(data["occurrences"], f"{path.name}: occurrences")):
        where = f"{path.name}: occurrences[{index}]"
        row = ev.exact_keys(
            ev.obj(raw, where), where,
            ("occ", "paragraph", "start", "end", "surface", "term_id", "accepted_forms"),
            optional=("official_forms",),
        )
        occ = ev.get_str(row, "occ", where)
        if occ in seen:
            fail(where, "occ id repeated")
        seen.add(occ)
        paragraph = paragraphs.get(ev.get_str(row, "paragraph", where))
        if paragraph is None or not paragraph.scored:
            fail(where, "paragraph is not a scored paragraph of the sample files")
        start, end = ev.get_int(row, "start", where), ev.get_int(row, "end", where)
        if not start < end <= len(paragraph.source):
            fail(where, "start and end must be a non-empty range inside the paragraph source")
        if ev.get_str(row, "surface", where) != paragraph.source[start:end]:
            fail(where, "surface does not equal source[start:end]")
        for key in ("accepted_forms", "official_forms"):
            if key in row:
                forms = ev.arr(row[key], f"{where}: {key}")
                if not forms or not all(isinstance(f, str) and f for f in forms):
                    fail(where, f"{key} must be a non-empty array of non-empty strings")
        ev.get_str(row, "term_id", where)
        found.setdefault(paragraph.id, []).append(occ)
    return found


def parse_record(
    ev: Any, raw: Any, where: str, pass_name: str, items: dict[str, Any],
    occurrences: dict[str, list[Any]], community: dict[str, list[str]], *, h3: bool,
) -> H3Judgment:
    row = dict(ev.obj(raw, where))
    listed = row.pop("community_names", None)
    if listed is not None and not h3:
        fail(where, "community_names needs schema h3-judgment-v1")
    base = ev.parse_judgment(row, where, pass_name, items, occurrences)
    where = f"{where} (item {base.item!r})"
    expected = set(community.get(items[base.item].paragraph, ()))
    names: dict[str, CommunityJudgment] = {}
    for index, entry_raw in enumerate(ev.arr(listed or [], f"{where}: community_names")):
        spot = f"{where}: community_names[{index}]"
        entry = ev.exact_keys(ev.obj(entry_raw, spot), spot, ("occ", "label", "rendering", "misleading"))
        occ = ev.get_str(entry, "occ", spot)
        if occ in names:
            fail(spot, "occ listed twice")
        label = ev.get_enum(entry, "label", ev.COMMUNITY_LABELS, spot)
        rendering = ev.get_opt_str(entry, "rendering", spot)
        misleading = ev.get_bool(entry, "misleading", spot)
        if label in ("official", "accepted", "not_official"):
            if rendering is None or not ev.normalize_rendering(rendering):
                fail(spot, "a rendered label needs a non-empty rendering")
        elif rendering is not None:
            fail(spot, "label omitted or undecidable needs rendering null")
        if misleading and label != "not_official":
            fail(spot, "misleading is only allowed with label not_official")
        names[occ] = CommunityJudgment(label, rendering, misleading)
    if set(names) != expected:
        fail(where, "community_names must list exactly the community occurrences of the item's paragraph")
    if any(n.misleading for n in names.values()) and base.grade != "C":
        fail(where, "a misleading community term requires grade C")
    return H3Judgment(base, names)


def load_judgments(
    ev: Any, paths: Sequence[Path], items: dict[str, Any], occurrences: dict[str, list[Any]],
    community: dict[str, list[str]],
) -> dict[str, dict[str, H3Judgment]]:
    by_pass: dict[str, dict[str, H3Judgment]] = {name: {} for name in ev.PASSES}

    def add(pass_name: str, judgment: H3Judgment, where: str) -> None:
        if judgment.base.item in by_pass[pass_name]:
            fail(where, f"item judged twice in pass {pass_name}")
        by_pass[pass_name][judgment.base.item] = judgment

    for path in paths:
        if path.suffix == ".jsonl":
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                fail(path.name, "not readable")
            for number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                where = f"{path.name}: line {number}"
                try:
                    raw = json.loads(line)
                except ValueError:
                    fail(where, "not JSON")
                row = dict(ev.need_keys(ev.obj(raw, where), where, ("pass",)))
                pass_name = ev.get_enum(row, "pass", ev.PASSES, where)
                del row["pass"]
                judgment = parse_record(ev, row, where, pass_name, items, occurrences, community, h3=True)
                add(pass_name, judgment, where)
            continue
        data = ev.exact_keys(ev.obj(ev.load_json(str(path)), path.name), path.name, ("schema", "pass", "records"))
        schema = data["schema"]
        if schema not in (ev.JUDGMENT_SCHEMA, H3_JUDGMENT_SCHEMA):
            fail(path.name, f"schema must be {ev.JUDGMENT_SCHEMA} or {H3_JUDGMENT_SCHEMA}")
        pass_name = ev.get_enum(data, "pass", ev.PASSES, path.name)
        for index, raw in enumerate(ev.arr(data["records"], f"{path.name}: records")):
            where = f"{path.name}: records[{index}]"
            judgment = parse_record(ev, raw, where, pass_name, items, occurrences, community,
                                    h3=schema == H3_JUDGMENT_SCHEMA)
            add(pass_name, judgment, where)
    return by_pass


def signature(ev: Any, judgment: H3Judgment) -> tuple[Any, ...]:
    """What the two judges must agree on: the scorer's signature plus community terms."""
    community = sorted(
        (occ, n.label, n.misleading, None if n.rendering is None else ev.normalize_rendering(n.rendering))
        for occ, n in judgment.community.items()
    )
    return (*ev.signature(judgment.base), community)


def resolve(
    ev: Any, items: dict[str, Any], by_pass: dict[str, dict[str, H3Judgment]]
) -> tuple[dict[str, H3Judgment | None], dict[str, int]]:
    j1, j2, adj = by_pass["j1"], by_pass["j2"], by_pass["adj"]
    for item_id in adj:
        first, second = j1.get(item_id), j2.get(item_id)
        if first is None or second is None or signature(ev, first) == signature(ev, second):
            fail(adj[item_id].base.origin, "adj record without two differing j1 and j2 judgments")
    finals: dict[str, H3Judgment | None] = {}
    stats = {"judged_by_both": 0, "agreed": 0, "adjudicated": 0}
    for item_id, item in items.items():
        if item.run_failed:
            continue
        first, second = j1.get(item_id), j2.get(item_id)
        if first is None or second is None:
            finals[item_id] = None
            continue
        stats["judged_by_both"] += 1
        if signature(ev, first) == signature(ev, second):
            stats["agreed"] += 1
            finals[item_id] = first
        else:
            finals[item_id] = adj.get(item_id)
            stats["adjudicated"] += item_id in adj
    return finals, stats


# --------------------------------------------------------------- arm outputs


def arm_outputs(ledger_path: Path, ledger: dict[str, Any], stage: str, arm: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Per doc the arm's final output, and per direction the arm's telemetry, from complete records."""
    records = [r for r in ledger["records"] if r["stage"] == stage and r["arm"] == arm and r["status"] == "complete"]
    if not records:
        fail(ledger_path.name, f"no complete {arm} record in stage {stage}")
    docs: dict[str, Any] = {}
    tel: dict[str, dict[str, Any]] = {}
    for record in records:
        path = ledger_path.parent / record["outputs"]["path"]
        if not path.is_file() or sha256_file(path) != record["outputs"]["sha256"]:
            fail(record["outputs"]["path"], "outputs do not match the SHA-256 recorded in the ledger")
        outputs = read_json(path)
        if outputs.get("schema") != OUTPUTS_SCHEMA:
            fail(path.name, f"schema must be {OUTPUTS_SCHEMA}")
        for doc in outputs["documents"]:
            if doc["doc_id"] in docs:
                fail(path.name, "a document appears in two complete records of this arm")
            docs[doc["doc_id"]] = doc
        for direction, entry in (record.get("telemetry") or {}).items():
            acc = tel.setdefault(direction, {"posts": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                             "total_tokens": 0, "latency_seconds": 0.0})
            acc["posts"] += int(record["posts_by_direction"].get(direction, 0))
            acc["latency_seconds"] += float(entry["latency_seconds"])
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                acc[key] = "unknown" if "unknown" in (acc[key], entry[key]) else acc[key] + int(entry[key])
    return docs, tel


def telemetry_block(tel: dict[str, Any], directions: Sequence[str], prices: tuple[float, float] | None) -> dict[str, Any]:
    block: dict[str, Any] = {}
    for direction in directions:
        entry = tel.get(direction)
        if entry is None:
            continue
        # total_tokens is written explicitly: most gateways count reasoning inside
        # completion_tokens, and the scorer would add the parts otherwise.
        out: dict[str, Any] = {
            "posts": entry["posts"],
            "total_tokens": entry["total_tokens"],
            "latency_seconds": entry["latency_seconds"],
        }
        if prices is not None and "unknown" not in (entry["prompt_tokens"], entry["completion_tokens"]):
            out["cost_usd"] = entry["prompt_tokens"] * prices[0] / 1e6 + entry["completion_tokens"] * prices[1] / 1e6
        block[direction] = out
    return block


# -------------------------------------------------------------- export-scores


def unit_row(paragraph: Any, direction: str, item: Any, final: H3Judgment | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "unit_id": paragraph.id,
        "doc_id": paragraph.doc,
        "direction": direction,
        "genre": paragraph.genre,
        "source_scalars": len(paragraph.source),
        "run_failed": item.run_failed,
    }
    if item.run_failed:
        return row
    assert final is not None
    base = final.base
    row.update(
        grade=base.grade,
        naturalness=base.naturalness,
        critical_errors=[{"type": kind, "count": count} for kind, count in sorted(base.critical.items())],
        names=[
            {"occ": occ, "label": n.label, "rendering": n.rendering, "misleading": n.misleading}
            for occ, n in sorted(base.names.items())
        ],
        community_names=[
            {"occ": occ, "label": n.label, "rendering": n.rendering, "misleading": n.misleading}
            for occ, n in sorted(final.community.items())
        ],
    )
    return row


def direction_summary(ev: Any, units: list[Any]) -> dict[str, Any]:
    summary = ev.single_arm_summary(units)
    return {
        "n": summary["n"],
        "documents": summary["documents"],
        "run_failed": summary["run_failed"],
        "a_rate": summary["a_rate"],
        "a_wilson95": summary["a_wilson95"],
        "usable_rate": summary["usable_rate"],
        "critical_errors": summary["critical_errors"],
        "mqm": summary["mqm"],
    }


def term_summary(ev: Any, judged: list[tuple[Any, H3Judgment]], occurrences: dict[str, list[Any]]) -> dict[str, Any]:
    """§4 item 3: official-name and community-term accuracy, reported separately."""
    count = official = 0
    groups: dict[tuple[str, str], list[Any]] = {}
    c_count = c_accepted = c_misleading = 0
    for paragraph, final in judged:
        name_ids = {o.occ: o.name_id for o in occurrences.get(paragraph.id, ())}
        for occ, name in final.base.names.items():
            count += 1
            official += name.label == "official"
            groups.setdefault((paragraph.doc, name_ids[occ]), []).append(name)
        for name in final.community.values():
            c_count += 1
            c_accepted += name.label in ("official", "accepted")
            c_misleading += name.misleading
    multi = [g for g in groups.values() if len(g) >= 2]
    consistent = sum(
        1 for g in multi
        if all(n.label in ev.RENDERED_LABELS for n in g)
        and len({ev.normalize_rendering(n.rendering or "") for n in g}) == 1
    )
    return {
        "official": {
            "occurrences": count, "official": official, "accuracy": ev.ratio(official, count),
            "multi_occurrence_names": len(multi), "consistent_names": consistent,
            "consistency": ev.ratio(consistent, len(multi)),
        },
        "community": {
            "occurrences": c_count, "accepted": c_accepted, "accuracy": ev.ratio(c_accepted, c_count),
            "misleading": c_misleading, "misleading_rate": ev.ratio(c_misleading, c_count),
        },
    }


def cmd_export(args: argparse.Namespace) -> int:
    ev = load_scorer()
    out_dir = Path(args.out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        fail(str(out_dir), "--out-dir must be empty")
    ledger_path = Path(args.ledger)
    ledger = verify_ledger(ledger_path)
    frozen = ledger["bindings"].get("frozen_inputs") or {}
    for text in [*args.sample, *args.names, *(args.community_names or [])]:
        path = Path(text)
        if frozen and frozen.get(path.name) != sha256_file(path):
            fail(path.name, "differs from the SHA-256 frozen in the ledger bindings (--freeze)")
    paragraphs: dict[str, Any] = {}
    for path in args.sample:
        for paragraph in ev.load_sample(path).paragraphs:
            if paragraph.id in paragraphs:
                fail(path, "paragraph id repeated across sample files")
            paragraphs[paragraph.id] = paragraph
    seen_occ: set[str] = set()
    occurrences: dict[str, list[Any]] = {}
    for path in args.names:
        for occ in ev.load_names(path, paragraphs, seen_occ):
            occurrences.setdefault(occ.paragraph, []).append(occ)
    community: dict[str, list[str]] = {}
    for path in args.community_names or []:
        for pid, occs in load_community(ev, Path(path), paragraphs, seen_occ).items():
            community.setdefault(pid, []).extend(occs)
    items: dict[str, Any] = {}
    partial: set[str] = set()
    seen_items: set[str] = set()
    for path in args.items:
        found, listed = ev.load_items(path, paragraphs, seen_items)
        partial |= listed
        items.update({item.item: item for item in found})
    by_pass = load_judgments(ev, [Path(p) for p in args.judgments], items, occurrences, community)
    finals, stats = resolve(ev, items, by_pass)

    prices = None
    if (args.price_input_per_1m is None) != (args.price_output_per_1m is None):
        fail("--price-*-per-1m", "give both prices or neither")
    if args.price_input_per_1m is not None:
        prices = (float(args.price_input_per_1m), float(args.price_output_per_1m))

    arms = sorted({item.arm for item in items.values()} - partial)
    unknown = [arm for arm in arms if arm not in ARM_DIRECTIONS]
    if unknown:
        fail("--items", f"unknown arm {unknown[0]!r} (list drift samples in partial_arms)")
    files: dict[str, dict[str, Any]] = {}
    judged: dict[tuple[str, str], list[tuple[Any, H3Judgment]]] = {}
    pending = 0
    for arm in arms:
        docs, tel = arm_outputs(ledger_path, ledger, RECORD_STAGE.get((args.stage, arm), args.stage), arm)
        by_paragraph: dict[str, Any] = {}
        for item in items.values():
            if item.arm != arm:
                continue
            if SAMPLE_DIRECTION[paragraphs[item.paragraph].direction] not in ARM_DIRECTIONS[arm]:
                fail("--items", f"item {item.item!r}: arm {arm} has no item in that direction")
            if item.paragraph in by_paragraph:
                fail("--items", f"two items of arm {arm} for one paragraph")
            by_paragraph[item.paragraph] = item
        units: list[dict[str, Any]] = []
        for pid, paragraph in sorted(paragraphs.items()):
            direction = SAMPLE_DIRECTION[paragraph.direction]
            if not paragraph.scored or direction not in ARM_DIRECTIONS[arm]:
                continue
            item = by_paragraph.get(pid)
            if item is None:
                fail("--items", f"scored paragraph {pid!r} has no item of arm {arm}")
            doc = docs.get(paragraph.doc)
            if doc is None or doc["direction"] != direction:
                fail(ledger_path.name, f"document {paragraph.doc!r} is not in the {arm} outputs")
            if item.run_failed != (doc["status"] == "run_failed"):
                fail("--items", f"item {item.item!r}: run_failed disagrees with the {arm} outputs")
            if not item.run_failed and item.translation not in doc["output"]:
                fail("--items", f"item {item.item!r}: translation is not part of the {arm} output")
            final = finals.get(item.item)
            if not item.run_failed and final is None:
                pending += 1
                continue
            if final is not None:
                judged.setdefault((arm, direction), []).append((paragraph, final))
            units.append(unit_row(paragraph, direction, item, final))
        files[f"{arm}.json"] = {"arm": arm, "units": units, "telemetry": telemetry_block(tel, ARM_DIRECTIONS[arm], prices)}
        if arm in EN_ZH_ONLY_COPIES:
            files[f"{arm}.en-zh.json"] = {
                "arm": arm,
                "units": [u for u in units if u["direction"] == "en->zh"],
                "telemetry": telemetry_block(tel, ("en->zh",), prices),
            }
    if pending:
        raise Pending(f"{pending} item(s) still pending; nothing was written")

    out_dir.mkdir(parents=True, exist_ok=True)
    summary_files: dict[str, Any] = {}
    for name, body in files.items():
        path = out_dir / name
        path.write_text(
            json.dumps({"schema": ev.ARM_SCORES_SCHEMA, **body}, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        loaded = ev.load_arm_scores(str(path))  # the scorer's own reading of the file
        by_direction: dict[str, list[Any]] = {}
        for unit in loaded.units.values():
            by_direction.setdefault(unit.direction, []).append(unit)
        summary_files[name] = {
            "sha256": sha256_file(path),
            "arm": body["arm"],
            "directions": {
                d: {**direction_summary(ev, u), "terms": term_summary(ev, judged.get((body["arm"], d), []), occurrences)}
                for d, u in sorted(by_direction.items())
            },
            "telemetry": body["telemetry"],
        }
    inputs = {
        "ledger": sha256_file(ledger_path),
        **{f"sample:{Path(p).name}": sha256_file(Path(p)) for p in args.sample},
        **{f"names:{Path(p).name}": sha256_file(Path(p)) for p in args.names},
        **{f"community:{Path(p).name}": sha256_file(Path(p)) for p in args.community_names or []},
        **{f"items:{Path(p).name}": sha256_file(Path(p)) for p in args.items},
        **{f"judgments:{Path(p).name}": sha256_file(Path(p)) for p in args.judgments},
    }
    summary = {
        "schema": SUMMARY_SCHEMA,
        "stage": args.stage,
        "ledger_mode": ledger["mode"],
        "scorer_sha256": PINNED_SCORER_SHA256,
        "mqm_weights": dict(ev.MQM_WEIGHTS),
        "judging": {**stats, "agreement_rate": (stats["agreed"] / stats["judged_by_both"]) if stats["judged_by_both"] else None},
        "inputs": inputs,
        "files": summary_files,
    }
    (out_dir / "export-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"written": sorted([*files, "export-summary.json"]), "out_dir": str(out_dir)}))
    return EXIT_OK


# --------------------------------------------------------------------- decide


def critical_gate(block: dict[str, Any], max_relative_increase: float) -> dict[str, Any]:
    crit = block.get("critical_errors") or {}
    rate_a = (crit.get("arm_a") or {}).get("per_1000_source_scalars")
    rate_b = (crit.get("arm_b") or {}).get("per_1000_source_scalars")
    verdict = crit.get("verdict")
    significant = verdict == "worse"
    ceiling = None if rate_a is None else rate_a * (1.0 + max_relative_increase)
    holds = None if ceiling is None or rate_b is None else rate_b <= ceiling + 1e-12
    return {
        "arm_a_per_1000": rate_a,
        "arm_b_per_1000": rate_b,
        "net_reduction_per_1000_ci95": crit.get("per_1000_source_scalars_net_reduction_ci95"),
        "scorer_verdict": verdict,
        "significant_worsening": significant,
        "max_relative_increase": max_relative_increase,
        "ceiling_per_1000": ceiling,
        "ceiling_holds": holds,
        # A missing rate or verdict (no scored scalars) fails closed.
        "passed": verdict is not None and not significant and holds is True,
    }


def cmd_decide(args: argparse.Namespace) -> int:
    ledger = verify_ledger(Path(args.ledger))
    decision = ledger["bindings"]["decision"]
    dry_run = ledger["mode"] != "live"
    max_increase = float(decision["critical_error_gate"]["max_relative_increase"])
    given = {"zh->en": args.zh_en, "en->zh": args.en_zh}
    if not any(given.values()):
        fail("decide", "give --zh-en and/or --en-zh")
    report: dict[str, Any] = {
        "schema": DECISION_SCHEMA,
        "dry_run": dry_run,
        "ledger_bindings_sha256": ledger.get("bindings_sha256"),
        "rule": decision,
        "directions": {},
    }
    for direction, path_text in given.items():
        if not path_text:
            continue
        path = Path(path_text)
        data = read_json(path)
        if not isinstance(data, dict) or data.get("schema") != "usability-paired-compare-v1":
            fail(path.name, "schema must be usability-paired-compare-v1")
        expected = decision["primary"][direction]
        if [data.get("arm_a"), data.get("arm_b")] != expected:
            fail(path.name, f"the {direction} primary comparison is --arm-a {expected[0]} --arm-b {expected[1]}")
        if not dry_run and (data.get("seed") != decision["seed"]
                            or data.get("bootstrap_samples") != decision["bootstrap_samples"]):
            fail(path.name, "seed and bootstrap samples must be the frozen values")
        block = (data.get("directions") or {}).get(direction) or {}
        if not block.get("n"):
            fail(path.name, f"no paired {direction} units")
        a_rate = block["a_rate"]
        gate = critical_gate(block, max_increase)
        if a_rate["verdict"] == "worse" or gate["significant_worsening"]:
            claim = "worse"
        elif a_rate["verdict"] == "gain" and gate["passed"]:
            claim = "gain"
        else:
            claim = "indistinguishable"
        report["directions"][direction] = {
            "arm_a": data["arm_a"],
            "arm_b": data["arm_b"],
            "compare_sha256": sha256_file(path),
            "seed": data.get("seed"),
            "bootstrap_samples": data.get("bootstrap_samples"),
            "n": block["n"],
            "a_rate": {"delta": a_rate["delta"], "ci95": a_rate["ci95"], "verdict": a_rate["verdict"]},
            "critical_error_gate": gate,
            "claim": claim,
            "gain_blocked_by_critical_gate": a_rate["verdict"] == "gain" and not gate["passed"],
        }
    text = json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return EXIT_OK


# ------------------------------------------------------------------------ cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval_h3_export.py", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export-scores", help="write usability-arm-scores-v1 files per arm")
    export.add_argument("--sample", action="append", required=True, metavar="SAMPLE.json")
    export.add_argument("--names", action="append", required=True, metavar="NAMES.json")
    export.add_argument("--community-names", action="append", metavar="COMMUNITY.json")
    export.add_argument("--items", action="append", required=True, metavar="ITEMS.json")
    export.add_argument("--judgments", action="extend", nargs="+", required=True, metavar="FILE")
    export.add_argument("--ledger", required=True, metavar="h3-ledger.json")
    export.add_argument("--stage", required=True, choices=("pilot", "full"))
    export.add_argument("--price-input-per-1m", type=float)
    export.add_argument("--price-output-per-1m", type=float)
    export.add_argument("--out-dir", required=True, metavar="DIR")
    export.set_defaults(func=cmd_export)

    decide = commands.add_parser("decide", help="apply the pre-registered §4.2 decision rule")
    decide.add_argument("--ledger", required=True, metavar="h3-ledger.json")
    decide.add_argument("--zh-en", metavar="COMPARE.json", help="raw_baseline vs wuwaterm_prod paired-compare")
    decide.add_argument("--en-zh", metavar="COMPARE.json", help="raw_matched vs wuwaterm_prod paired-compare")
    decide.add_argument("--json-out", metavar="DECISION.json")
    decide.set_defaults(func=cmd_decide)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Pending as exc:
        print(f"pending: {exc}", file=sys.stderr)
        return EXIT_PENDING
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except Exception as exc:
        scorer = sys.modules.get("eval_usability")
        if scorer is not None and isinstance(exc, scorer.InputError):
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_INVALID
        raise


if __name__ == "__main__":
    sys.exit(main())
