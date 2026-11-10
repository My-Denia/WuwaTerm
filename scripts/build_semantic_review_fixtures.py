#!/usr/bin/env python3
"""Generate the semantic-review-v1 contract fixtures from the real serializer.

Every response fixture is the exact byte string `wuwaterm.semantic_review_contract`
produces for a synthetic, already-anchored model output, so a client that
replays it is testing against what the server would send, not against a
hand-written approximation. Request fixtures are compact UTF-8 JSON, encoded
the way the server encodes responses, so their byte length is meaningful.

All text is synthetic. No corpus, game or personal text appears here.

Usage:
    python scripts/build_semantic_review_fixtures.py            # verify
    python scripts/build_semantic_review_fixtures.py --write    # regenerate
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "contracts" / "semantic-review-v1"
MANIFEST = "cases.json"

sys.path.insert(0, str(ROOT / "src"))

from wuwaterm.semantic_review_contract import (  # noqa: E402
    REQUEST_BYTE_LIMIT,
    REVIEW_VERSION,
    SEMANTIC_RESPONSE_BYTE_BUDGET,
    Alternative,
    AnchoredIssue,
    FittedResponse,
    Span,
    Suggestion,
    build_not_reviewed,
    build_response,
    serialize,
)

TERM_BASIS = {
    "dictionary_revision": hashlib.sha256(b"fixture dictionary").hexdigest(),
    "matcher_revision": hashlib.sha256(b"fixture matcher").hexdigest(),
}
# Synthetic filler: the ten heavenly stems, repeated. Not a sentence of anything.
STEMS = "甲乙丙丁戊己庚辛壬癸"
# One scalar each; JSON escapes them to 2 (quote, backslash, tab) or 6 (U+0001)
# bytes, which is what makes byte measurement differ from scalar counting.
ESCAPED = '"\\\t\u0001'


@dataclass(frozen=True)
class Case:
    name: str
    description: str
    request: dict[str, Any]
    response: FittedResponse | None


def stems(count: int, offset: int = 0) -> str:
    return "".join(STEMS[(offset + index) % len(STEMS)] for index in range(count))


def mixed(count: int, pattern: str) -> str:
    """``count`` scalars cycling through ``pattern``."""
    return "".join(pattern[index % len(pattern)] for index in range(count))


def request(
    source: str,
    target: str,
    *,
    direction: str = "en",
    explain_language: str = "zh",
    context: dict[str, Any] | None = None,
    precedents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source": source,
        "target": target,
        "direction": direction,
        "review_version": REVIEW_VERSION,
        "explain_language": explain_language,
    }
    if context is not None:
        body["context"] = context
    body["precedents"] = precedents or []
    return body


def respond(name: str, body: dict[str, Any], issues, dropped: int = 0) -> FittedResponse:
    return build_response(
        request_id=f"fixture-{name}",
        source=body["source"],
        target=body["target"],
        term_basis=TERM_BASIS,
        issues=issues,
        dropped_unanchored=dropped,
    )


def find(text: str, quote: str) -> Span:
    start = text.index(quote)
    if text.find(quote, start + 1) != -1:
        raise ValueError(f"quote is not unique: {quote!r}")
    return Span(start, start + len(quote))


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------

SMALL_SOURCE = "甲乙在第三天不会出发，丙丁随后到达。"
SMALL_TARGET = "Jia and Yi will leave on the second day; Bing and Ding arrive later."


def case_minimal() -> Case:
    body = request(
        SMALL_SOURCE,
        SMALL_TARGET,
        context={"before": {"source": "甲乙丙丁已经集合。", "target": "Jia, Yi, Bing and Ding have gathered."}},
        precedents=[
            {
                "surface": "甲乙",
                "choice": "user_rendering",
                "rendering": "Jia and Yi",
                "snippet": "甲乙丙丁已经集合。",
                "anchor": {"segment_index": 0, "source_start": 0, "source_end": 2},
                "scope": "occurrence",
            }
        ],
    )
    issues = [
        AnchoredIssue(
            category="quantity_change",
            confidence="high",
            source_span=find(SMALL_SOURCE, "第三天"),
            target_span=find(SMALL_TARGET, "second day"),
            rationale="原文是第三天，译文写成了第二天。",
            suggestion=Suggestion(find(SMALL_TARGET, "second"), "third", "none"),
            alternatives=(Alternative("3rd", "none"),),
        ),
        AnchoredIssue(
            category="negation_change",
            confidence="medium",
            source_span=find(SMALL_SOURCE, "不会出发"),
            target_span=find(SMALL_TARGET, "will leave"),
            rationale="原文是否定，译文丢了否定。",
            suggestion=Suggestion(find(SMALL_TARGET, "will leave"), "will not leave", "none"),
        ),
        AnchoredIssue(
            category="omission",
            confidence="low",
            source_span=find(SMALL_SOURCE, "随后"),
            target_span=None,
            rationale="Only the source side is anchored; there is nothing to replace.",
        ),
    ]
    return Case(
        "minimal",
        "Two anchored issues with suggestions and one source-only issue; context and one precedent in the request.",
        body,
        respond("minimal", body, issues),
    )


def case_dropped_unanchored() -> Case:
    body = request(SMALL_SOURCE, SMALL_TARGET)
    issues = [
        AnchoredIssue(
            category="meaning_shift",
            confidence="medium",
            source_span=find(SMALL_SOURCE, "到达"),
            target_span=find(SMALL_TARGET, "arrive"),
            rationale="示例理由。",
        )
    ]
    return Case(
        "dropped-unanchored",
        "Complete review where three model issues could not be anchored and were dropped.",
        body,
        respond("dropped-unanchored", body, issues, dropped=3),
    )


def case_not_reviewed() -> Case:
    body = request(SMALL_SOURCE, SMALL_TARGET)
    response = build_not_reviewed(
        request_id="fixture-not-reviewed",
        source=body["source"],
        target=body["target"],
        term_basis=TERM_BASIS,
    )
    return Case(
        "not-reviewed",
        "The model output failed validation: status not_reviewed, nothing checked, no issues.",
        body,
        response,
    )


def case_issue_limit() -> Case:
    source = "".join(f"第{index}项。" for index in range(1, 21))
    target = " ".join(f"Item {index:02d}." for index in range(1, 21))
    confidences = ("low", "medium", "high")
    issues = [
        AnchoredIssue(
            category="quantity_change",
            confidence=confidences[index % 3],
            source_span=find(source, f"第{index}项"),
            target_span=find(target, f"Item {index:02d}"),
            rationale=f"示例 {index}。",
        )
        for index in range(1, 21)
    ]
    body = request(source, target)
    return Case(
        "issue-limit",
        "Twenty small issues: sorted by confidence then position, sixteen kept, four omitted (issue_limit).",
        body,
        respond("issue-limit", body, issues),
    )


def case_suggestion_span_too_long() -> Case:
    target = mixed(400, "abcdefghij ")
    source = stems(120)
    span = Span(50, 350)
    issues = [
        AnchoredIssue(
            category="meaning_shift",
            confidence="high",
            source_span=Span(10, 40),
            target_span=span,
            rationale="可执行范围有 300 个标量，超过 200，建议整体移除。",
            suggestion=Suggestion(span, "short replacement", "none"),
            alternatives=(Alternative("other", "none"),),
        )
    ]
    body = request(source, target)
    return Case(
        "suggestion-span-too-long",
        "A 300-scalar executable range: the issue and its complete span stay, the suggestion and alternatives are withheld (span_too_long).",
        body,
        respond("suggestion-span-too-long", body, issues),
    )


def case_suggestion_replacement_too_long() -> Case:
    target = mixed(300, "klmnopqrst ")
    source = stems(80)
    span = Span(20, 70)
    issues = [
        AnchoredIssue(
            category="unsupported_addition",
            confidence="medium",
            source_span=Span(5, 25),
            target_span=span,
            rationale="替换文本有 241 个标量，超过 240，建议整体移除。",
            suggestion=Suggestion(span, mixed(241, "uvwxyz "), "not_checked"),
        )
    ]
    body = request(source, target)
    return Case(
        "suggestion-replacement-too-long",
        "A 241-scalar replacement: the suggestion is withheld (replacement_too_long); the span is complete.",
        body,
        respond("suggestion-replacement-too-long", body, issues),
    )


def case_suggestion_boundary() -> Case:
    target = mixed(500, "ABCDEFGHIJ ")
    source = stems(150)
    span = Span(100, 300)
    issues = [
        AnchoredIssue(
            category="term_sense",
            confidence="high",
            source_span=Span(0, 12),
            target_span=span,
            rationale="边界通过例：范围恰好 200，替换恰好 240。",
            suggestion=Suggestion(span, mixed(240, "replace "), "alters_official_match"),
            alternatives=(Alternative(mixed(240, "other "), "introduces_official_form"),),
        )
    ]
    body = request(source, target)
    return Case(
        "suggestion-boundary",
        "Exactly 200 scalars of range with exactly 240 scalars of replacement and alternative: kept.",
        body,
        respond("suggestion-boundary", body, issues),
    )


def case_byte_limit() -> Case:
    # Mostly 6-byte escapes: each full-segment issue serializes to over 20 KB.
    source = mixed(2000, "\u0001\u0002甲\"\u0003\\")
    target = mixed(2000, "\u0004a\u0005\"\u0006\\")
    categories = ("omission", "meaning_shift", "reference_inconsistency", "term_sense")
    issues = [
        AnchoredIssue(
            category=category,
            confidence="high",
            source_span=Span(0, 2000),
            target_span=Span(0, 2000),
            rationale=mixed(240, "理由\u0001\""),
        )
        for category in categories
    ]
    body = request(source, target, direction="zh", explain_language="en")
    return Case(
        "byte-limit",
        "Four issues spanning both whole 2,000-scalar sides with escape-heavy text: three fit the byte budget, the fourth is omitted whole (byte_limit). The spans are never shortened.",
        body,
        respond("byte-limit", body, issues),
    )


def case_max() -> Case:
    source = mixed(2000, "甲乙丙\"丁戊\\己庚\t辛壬\u0001癸")
    target = mixed(2000, "子丑寅\"卯辰\\巳午\t未申\u0002酉")
    issues: list[AnchoredIssue] = []
    # Four large issues first (high confidence sorts first).
    for index, (start, end) in enumerate(((0, 1990), (5, 1500), (10, 1900), (15, 600))):
        issues.append(
            AnchoredIssue(
                category="meaning_shift",
                confidence="high",
                source_span=Span(start, end),
                target_span=Span(start + index, end - index),
                rationale=mixed(240, "大段理由\""),
                suggestion=Suggestion(Span(start, start + 200), mixed(240, "替换\\"), "none"),
                alternatives=(Alternative(mixed(240, "备选\t"), "not_checked"),),
            )
        )
    # Then many small issues, each well under 1 KiB, so the fit stops within
    # 1 KiB of the budget and leaves a two-digit omitted count.
    for index in range(24):
        start = 100 + index * 60
        issues.append(
            AnchoredIssue(
                category="reference_inconsistency",
                confidence="medium",
                source_span=Span(start, start + 12),
                target_span=Span(start + 3, start + 15),
                rationale=mixed(60, "小段理由"),
                suggestion=Suggestion(Span(start + 3, start + 9), mixed(20, "改"), "none"),
            )
        )
    body = request(source, target)
    return Case(
        "max",
        "The largest legal response the fixtures carry: its final serialized size, counters included, is within 1 KiB below the 57,344-byte budget, one more issue would exceed it, and omitted_issues has two digits.",
        body,
        respond("max", body, issues),
    )


def case_near_limit_request() -> Case:
    context = {"before": {"source": stems(400, 3), "target": stems(400, 7)}}
    precedents = [
        {
            "surface": stems(32, index),
            "choice": "official_pair" if index % 3 == 0 else ("not_a_term" if index % 3 == 1 else "user_rendering"),
            "rendering": None if index % 3 == 1 else stems(48, index + 1),
            "snippet": stems(80, index + 2),
            "anchor": {"segment_index": index, "source_start": 0, "source_end": 32},
            "scope": "occurrence",
        }
        for index in range(12)
    ]
    # Every field at its scalar maximum in CJK is only about 23 KB, so the
    # rest of the bytes come from 6-byte escapes: the target is all escapes and
    # the source trades CJK for escapes until one more would cross the limit.
    target = mixed(2000, "\u0001")
    for control_count in range(2000, -1, -1):
        source = mixed(control_count, "\u0002") + stems(2000 - control_count)
        body = request(source, target, context=context, precedents=precedents)
        if len(serialize(body)) <= REQUEST_BYTE_LIMIT:
            break
    return Case(
        "near-limit-request",
        "A valid request at every scalar limit whose compact UTF-8 encoding is just under the 32 KiB request limit.",
        body,
        None,
    )


def cases() -> list[Case]:
    return [
        case_minimal(),
        case_dropped_unanchored(),
        case_not_reviewed(),
        case_issue_limit(),
        case_suggestion_span_too_long(),
        case_suggestion_replacement_too_long(),
        case_suggestion_boundary(),
        case_byte_limit(),
        case_max(),
        case_near_limit_request(),
    ]


def _check_case(case: Case) -> None:
    """Fail generation when a case stops proving what its description says."""
    request_bytes = len(serialize(case.request))
    if request_bytes > REQUEST_BYTE_LIMIT:
        raise SystemExit(f"{case.name}: request is {request_bytes} bytes")
    response = case.response
    if case.name == "near-limit-request" and request_bytes < REQUEST_BYTE_LIMIT - 64:
        raise SystemExit(f"{case.name}: request is not near the limit ({request_bytes})")
    if response is None:
        return
    size = len(response.data)
    if size > SEMANTIC_RESPONSE_BYTE_BUDGET:
        raise SystemExit(f"{case.name}: response is {size} bytes")
    body = response.body
    if case.name == "max":
        if not SEMANTIC_RESPONSE_BYTE_BUDGET - 1024 <= size:
            raise SystemExit(f"max: {size} bytes is not within 1 KiB of the budget")
        if body["truncated_reason"] != "byte_limit" or body["omitted_issues"] < 10:
            raise SystemExit(f"max: unexpected truncation {body['truncated_reason']} {body['omitted_issues']}")
        if response.next_size is None or response.next_size <= SEMANTIC_RESPONSE_BYTE_BUDGET:
            raise SystemExit("max: one more issue would still fit")
    if case.name == "byte-limit" and (
        body["truncated_reason"] != "byte_limit" or response.kept < 1
    ):
        raise SystemExit("byte-limit: did not truncate on bytes")
    if case.name == "issue-limit" and (
        body["truncated_reason"] != "issue_limit" or response.kept != 16
    ):
        raise SystemExit("issue-limit: did not truncate at sixteen issues")


def render_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    manifest_cases = []
    for case in cases():
        _check_case(case)
        request_name = (
            f"{case.name}.json" if case.response is None else f"{case.name}-request.json"
        )
        request_data = serialize(case.request)
        files[request_name] = request_data
        entry: dict[str, Any] = {
            "name": case.name,
            "description": case.description,
            "request": request_name,
            "request_bytes": len(request_data),
            "response": None,
        }
        if case.response is not None:
            response_name = f"{case.name}-response.json"
            files[response_name] = case.response.data
            body = case.response.body
            entry.update(
                response=response_name,
                response_bytes=len(case.response.data),
                status=body["status"],
                issues=len(body["issues"]),
                truncated_reason=body["truncated_reason"],
                omitted_issues=body["omitted_issues"],
            )
        manifest_cases.append(entry)
    manifest = {
        "version": REVIEW_VERSION,
        "generator": "scripts/build_semantic_review_fixtures.py",
        "request_byte_limit": REQUEST_BYTE_LIMIT,
        "response_byte_budget": SEMANTIC_RESPONSE_BYTE_BUDGET,
        "encoding": "UTF-8 JSON, ensure_ascii=false, separators (',', ':'), no trailing newline; the response files are the exact bytes the server sends",
        "cases": manifest_cases,
    }
    files[MANIFEST] = (
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the fixtures")
    args = parser.parse_args(argv)

    files = render_files()
    if args.write:
        OUT.mkdir(parents=True, exist_ok=True)
        for stale in OUT.glob("*.json"):
            if stale.name not in files:
                stale.unlink()
        for name, data in files.items():
            (OUT / name).write_bytes(data)
        print(f"wrote {len(files)} files to {OUT.relative_to(ROOT).as_posix()}")
        return 0

    problems = []
    present = {path.name for path in OUT.glob("*.json")} if OUT.is_dir() else set()
    for name, data in files.items():
        path = OUT / name
        if not path.is_file() or path.read_bytes() != data:
            problems.append(name)
    problems += sorted(present - set(files))
    if problems:
        print(
            "semantic-review-v1 fixture drift: "
            + ", ".join(problems)
            + ". Run `python scripts/build_semantic_review_fixtures.py --write`.",
            file=sys.stderr,
        )
        return 1
    print("semantic-review-v1 fixtures ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
