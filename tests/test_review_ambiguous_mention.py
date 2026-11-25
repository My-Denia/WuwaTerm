"""Ambiguous one-character name mentions in review-v2 (``ambiguous-mention-v1``).

A review request that does not ask for ``ambiguous-mention-v1`` must keep the
report and the HTTP bytes it had before the feature existed. The snapshot
below was produced on ``main`` 486312d, before any review change, from the
cases in ``_review_snapshot``. Only the matcher revision value is normalized:
the label is expected to move, nothing else is.

With the feature, a one-character resonator name the matcher calls
``ambiguous_mention`` and whose request-local evidence meets the
``single_char_context`` threshold is reported as ``not_evaluated`` with reason
``ambiguous_mention``. It is judged only when the caller resolves it, it never
changes another finding, and it is the first to go when a page is full.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from tests.test_api import bearer, build_client_app, call, issue_device, run
from wuwaterm.application import (
    REVIEW_FEATURE_AMBIGUOUS_MENTION,
    REVIEW_MAX_JSON_BYTES,
    ReviewRequestError,
    project_review_report,
    review_pair,
    service_capabilities,
)
from wuwaterm.cjk_span import MATCHER_REVISION
from wuwaterm.lookup import TermService
from wuwaterm.review import (
    REASON_AMBIGUOUS_MENTION,
    RULE_TERM_PAIR,
    VERDICT_CONFLICT,
    VERDICT_NEEDS_REVIEW,
    VERDICT_NOT_EVALUATED,
    VERDICT_VERIFIED,
    ReviewCandidate,
    ReviewCoverage,
    ReviewDictionary,
    ReviewFinding,
    ReviewReport,
    ReviewSource,
    ReviewSpan,
)

# The v10 label's revision. A resolution context that still carries it is
# stale once the label moves.
MATCHER_V10 = "57428b88978597d95840f56ba0080a4f8fb11d4860fe8ea5532d3c1e7c6a918a"

MATCHER_PLACEHOLDER = "<matcher-revision>"
REQUEST_ID = "req-snapshot"

# (name, source, target, direction). Every Chinese case holds 椿 occurrences
# that the classifier calls ambiguous_mention; several of them carry enough
# request-local evidence that the feature, when asked for, reports them.
REVIEW_CASES: tuple[tuple[str, str, str, str], ...] = (
    (
        "enumeration",
        "今汐、椿姐、守岸人一起出发。",
        "Jinhsi, Camellya and the Shorekeeper set out.",
        "en",
    ),
    (
        "compound_alone",
        "红椿蕊开了。",
        "The red camellia bloomed.",
        "en",
    ),
    (
        "topic_title",
        "椿姐配队攻略\n\n共鸣者椿姐和今汐一起出发，椿姐很强。",
        "Camellya team guide\n\nResonator Camellya set out with Jinhsi; Camellya is strong.",
        "en",
    ),
    (
        "speaker_labels",
        "椿姐：你好。\n今汐：你好。",
        "Camellya: Hello.\nJinhsi: Hello.",
        "en",
    ),
    (
        "build_tokens",
        "0+1 椿姐 配今汐，椿姐和声骸。",
        "S0R1 Camellya with Jinhsi, Camellya and an Echo.",
        "en",
    ),
    (
        "name_and_compound",
        "椿加入队伍。红椿蕊开了，今汐、椿姐也在。",
        "Camellya joined the team. The red camellia bloomed; Jinhsi and Camellya are here too.",
        "en",
    ),
    (
        "mismatch",
        "今汐、椿姐、守岸人一起出发。",
        "Jinhsi, Changli and the Shorekeeper set out.",
        "en",
    ),
    (
        "repeat_shortage",
        "今汐和今汐、椿姐一起出发。",
        "Jinhsi set out with Camellya.",
        "en",
    ),
    (
        "no_target_region",
        "今汐出发。椿姐也在。\n\n守岸人、椿姐来了。",
        "Jinhsi set out.",
        "en",
    ),
    (
        "en_source",
        "Camellya and Jinhsi met an Echo.",
        "椿和今汐遇到了声骸。",
        "zh",
    ),
    (
        "page_overflow",
        "今汐，" * 30 + "守岸人、椿姐、今汐一起出发。" + "今汐、椿姐。" * 4,
        "Jinhsi, " * 30 + "The Shorekeeper, Camellya and Jinhsi set out. " + "Jinhsi, Camellya. " * 4,
        "en",
    ),
)

# Header combinations a client can send today.
FEATURE_HEADERS: tuple[str | None, ...] = (None, "dense-128", "reason-v1", "dense-128,reason-v1")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalized(data: bytes) -> bytes:
    return data.replace(MATCHER_REVISION.encode("ascii"), MATCHER_PLACEHOLDER.encode("ascii"))


def _report_bytes(report) -> bytes:
    return _normalized(
        json.dumps(
            dataclasses.asdict(report), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
    )


def _projection_bytes(body: dict[str, object]) -> bytes:
    return _normalized(
        json.dumps(
            body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    )


def _ambiguous_mention_id(source: str) -> str:
    """The mention id an ambiguous 椿 occurrence would have (``start:end:椿``)."""
    start = source.index("椿姐")
    return f"{start}:{start + 1}:椿"


def _response_entry(response) -> dict[str, object]:
    content = response.content
    request_id = response.headers.get("X-Request-Id")
    if request_id:
        content = content.replace(request_id.encode("utf-8"), REQUEST_ID.encode("utf-8"))
    return {
        "status": response.status_code,
        "features": response.headers.get("X-WuwaTerm-Review-Features"),
        "sha256": _sha(_normalized(content)),
    }


def _review_snapshot(tmp_path: Path, sample_db: Path) -> dict[str, object]:
    service = TermService(sample_db)
    out: dict[str, object] = {}

    for name, source, target, direction in REVIEW_CASES:
        v1 = review_pair(service, source, target, direction, review_version="review-v1")
        v2 = review_pair(service, source, target, direction, review_version="review-v2")
        entry: dict[str, object] = {
            "v1_report": _sha(_report_bytes(v1)),
            "v2_report": _sha(_report_bytes(v2)),
            "v2_findings": len(v2.findings),
        }
        for max_findings in (32, 128):
            for include_reason in (False, True):
                body = project_review_report(
                    v2,
                    REQUEST_ID,
                    max_findings=max_findings,
                    include_reason=include_reason,
                )
                key = f"v2_projection_{max_findings}_{'reason' if include_reason else 'plain'}"
                entry[key] = _sha(_projection_bytes(body))
        out[name] = entry

    # A resolved pair: the official_pair resolution names the 今汐 mention.
    source, target = "今汐、椿姐、守岸人一起出发。", "Jinhsi, Camellya and the Shorekeeper set out."
    base = review_pair(service, source, target, "en", review_version="review-v2")
    jinhsi = next(f for f in base.findings if f.source_span.text == "今汐")
    resolution = {
        "mention_id": jinhsi.id,
        "choice": "official_pair",
        "candidate_id": jinhsi.candidates[0].candidate_id,
    }
    context = {
        "source_revision": base.source_revision,
        "rule_version": "review-v2",
        "dictionary_revision": base.dictionary.revision,
        "matcher_revision": MATCHER_REVISION,
    }
    resolved = review_pair(
        service,
        source,
        target,
        "en",
        (resolution,),
        review_version="review-v2",
        resolution_context=context,
    )
    out["resolved_official_pair"] = _sha(_report_bytes(resolved))

    # Alignment input: one whole-text region.
    aligned = review_pair(
        service,
        source,
        target,
        "en",
        review_version="review-v2",
        alignments=[
            {
                "source": {"start": 0, "end": len(source), "text": source},
                "target": {"start": 0, "end": len(target), "text": target},
            }
        ],
    )
    out["aligned"] = _sha(_report_bytes(aligned))

    # HTTP: every published header combination, with and without the matcher
    # basis, plus the refusal of a resolution aimed at an ambiguous 椿.
    app, store = build_client_app(tmp_path, sample_db, max_body_bytes=64 * 1024)
    _, token = issue_device(store, "snapshot reviewer")
    http: dict[str, object] = {}
    for name, source, target, direction in REVIEW_CASES:
        for basis in (False, True):
            for features in FEATURE_HEADERS:
                headers = {**bearer(token)}
                if basis:
                    headers["X-WuwaTerm-Matcher-Basis"] = "1"
                if features is not None:
                    headers["X-WuwaTerm-Review-Features"] = features
                response = run(
                    call(
                        app,
                        "POST",
                        "/v1/reviews",
                        json={
                            "review_version": "review-v2",
                            "source": source,
                            "target": target,
                            "direction": direction,
                        },
                        headers=headers,
                    )
                )
                key = f"{name}|basis={int(basis)}|features={features or '-'}"
                http[key] = _response_entry(response)
        response = run(
            call(
                app,
                "POST",
                "/v1/reviews",
                json={
                    "review_version": "review-v1",
                    "source": source,
                    "target": target,
                    "direction": direction,
                },
                headers=bearer(token),
            )
        )
        http[f"{name}|review-v1"] = _response_entry(response)

    source = "今汐、椿姐、守岸人一起出发。"
    target = "Jinhsi, Camellya and the Shorekeeper set out."
    base = review_pair(service, source, target, "en", review_version="review-v2")
    stray = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={
                "review_version": "review-v2",
                "source": source,
                "target": target,
                "direction": "en",
                "resolutions": [
                    {"mention_id": _ambiguous_mention_id(source), "choice": "not_a_term"}
                ],
                "resolution_context": {
                    "source_revision": base.source_revision,
                    "rule_version": "review-v2",
                    "dictionary_revision": base.dictionary.revision,
                    "matcher_revision": MATCHER_REVISION,
                },
            },
            headers={
                **bearer(token),
                "X-WuwaTerm-Matcher-Basis": "1",
                "X-WuwaTerm-Review-Features": "dense-128,reason-v1",
            },
        )
    )
    http["stray_ambiguous_resolution"] = _response_entry(stray)
    out["http"] = http
    return out


def test_review_without_the_feature_matches_the_frozen_main_snapshot(tmp_path, sample_db):
    """No ambiguous-mention-v1: reports and HTTP bytes equal main 486312d's."""
    snapshot = _review_snapshot(tmp_path, sample_db)
    assert snapshot.keys() == MAIN_REVIEW_SNAPSHOT.keys()
    for key in sorted(snapshot):
        if key == "http":
            continue
        assert snapshot[key] == MAIN_REVIEW_SNAPSHOT[key], key
    http = snapshot["http"]
    frozen = MAIN_REVIEW_SNAPSHOT["http"]
    assert isinstance(http, dict) and isinstance(frozen, dict)
    assert http.keys() == frozen.keys()
    for key in sorted(http):
        assert http[key] == frozen[key], key


# ---------------------------------------------------------------------------
# The feature itself
# ---------------------------------------------------------------------------


def _case(name: str) -> tuple[str, str, str]:
    for case_name, source, target, direction in REVIEW_CASES:
        if case_name == name:
            return source, target, direction
    raise KeyError(name)


def _v2(service, source, target, direction="en", resolutions=(), **kwargs):
    return review_pair(
        service, source, target, direction, resolutions, review_version="review-v2", **kwargs
    )


def _ambiguous(report) -> list[ReviewFinding]:
    return [f for f in report.findings if f.reason == REASON_AMBIGUOUS_MENTION]


def _context(report) -> dict[str, object]:
    return {
        "source_revision": report.source_revision,
        "rule_version": "review-v2",
        "dictionary_revision": report.dictionary.revision,
        "matcher_revision": MATCHER_REVISION,
    }


def test_matcher_revision_is_v11():
    assert MATCHER_REVISION == "286d29e8f25b1ee53d0634c89bf6fb8a83ef950888c21f15d485413cb25d0664"
    assert MATCHER_REVISION != MATCHER_V10


def test_a_v10_resolution_context_is_stale(sample_db):
    service = TermService(sample_db)
    source, target, direction = _case("enumeration")
    base = _v2(service, source, target, direction)
    jinhsi = next(f for f in base.findings if f.source_span.text == "今汐")
    resolution = {
        "mention_id": jinhsi.id,
        "choice": "official_pair",
        "candidate_id": jinhsi.candidates[0].candidate_id,
    }
    with pytest.raises(ReviewRequestError, match="resolution_context is stale"):
        _v2(
            service,
            source,
            target,
            direction,
            (resolution,),
            resolution_context={**_context(base), "matcher_revision": MATCHER_V10},
        )


def test_supported_ambiguous_name_is_reported_unassessed_after_judged_findings(sample_db):
    service = TermService(sample_db)
    source, target, direction = _case("enumeration")
    off = _v2(service, source, target, direction)
    on = _v2(service, source, target, direction, ambiguous_mentions=True)

    assert [f.source_span.text for f in off.findings] == ["今汐", "守岸人"]
    assert on.findings[: len(off.findings)] == off.findings
    [finding] = _ambiguous(on)
    assert on.findings[-1] is finding
    assert finding.id == f"{source.index('椿姐')}:{source.index('椿姐') + 1}:椿"
    assert finding.verdict == VERDICT_NOT_EVALUATED
    assert finding.rule_id == RULE_TERM_PAIR
    assert finding.target_span is None
    assert [(c.zh, c.en, c.category) for c in finding.candidates] == [
        ("椿", "Camellya", "resonator")
    ]
    assert finding.candidates[0].candidate_id is not None
    # Not counted as a problem: evaluated is unchanged, not_evaluated grows.
    assert on.coverage.evaluated == off.coverage.evaluated == 2
    assert on.coverage.not_evaluated == off.coverage.not_evaluated + 1


def test_feature_never_judges_and_never_moves_another_finding(sample_db):
    service = TermService(sample_db)
    seen = 0
    for name, source, target, direction in REVIEW_CASES:
        off = _v2(service, source, target, direction)
        on = _v2(service, source, target, direction, ambiguous_mentions=True)
        extra = _ambiguous(on)
        seen += len(extra)
        assert on.findings == off.findings + tuple(extra), name
        assert all(f.verdict == VERDICT_NOT_EVALUATED for f in extra), name
        assert all(f.target_span is None for f in extra), name
        assert on.coverage.evaluated == off.coverage.evaluated, name
        assert on.coverage.not_evaluated == off.coverage.not_evaluated + len(extra), name
        assert on.matcher_revision == off.matcher_revision == MATCHER_REVISION, name
    assert seen == 16


@pytest.mark.parametrize(
    ("name", "unreported"),
    (
        # 红椿蕊 alone: no evidence at all.
        ("compound_alone", "红椿蕊"),
        # The title 椿姐 has only its title position; 今汐 is in another paragraph.
        ("topic_title", "椿姐配队攻略"),
        # 今汐 is both the paragraph entity and the co-occurring resonator:
        # one span, so one piece of evidence.
        ("no_target_region", "今汐出发。椿姐也在。"),
    ),
)
def test_ambiguous_name_below_the_threshold_is_not_reported(sample_db, name, unreported):
    service = TermService(sample_db)
    source, target, direction = _case(name)
    on = _v2(service, source, target, direction, ambiguous_mentions=True)
    lo = source.index(unreported)
    hi = lo + len(unreported)
    assert not any(lo <= f.source_span.start < hi for f in _ambiguous(on))


def _resolved(service, name, choice):
    source, target, direction = _case(name)
    base = _v2(service, source, target, direction, ambiguous_mentions=True)
    [finding] = _ambiguous(base)
    resolution: dict[str, object] = {"mention_id": finding.id, "choice": choice}
    if choice == "official_pair":
        resolution["candidate_id"] = finding.candidates[0].candidate_id
    return finding, _v2(
        service,
        source,
        target,
        direction,
        (resolution,),
        resolution_context=_context(base),
        ambiguous_mentions=True,
    )


@pytest.mark.parametrize(
    ("name", "choice", "verdict", "reason"),
    (
        ("enumeration", "official_pair", VERDICT_VERIFIED, None),
        ("mismatch", "official_pair", VERDICT_CONFLICT, None),
        ("enumeration", "not_a_term", VERDICT_NOT_EVALUATED, "not_a_term"),
    ),
)
def test_only_a_resolution_judges_an_ambiguous_name(sample_db, name, choice, verdict, reason):
    service = TermService(sample_db)
    finding, report = _resolved(service, name, choice)
    [judged] = [f for f in report.findings if f.id == finding.id]
    assert judged.verdict == verdict
    assert judged.reason == reason
    assert _ambiguous(report) == []
    # A resolved mention is an ordinary finding again and sorts by position.
    assert [f.source_span.start for f in report.findings] == sorted(
        f.source_span.start for f in report.findings
    )


def test_resolution_aimed_at_an_ambiguous_name_needs_the_feature(sample_db):
    service = TermService(sample_db)
    source, target, direction = _case("enumeration")
    base = _v2(service, source, target, direction)
    resolution = {"mention_id": _ambiguous_mention_id(source), "choice": "not_a_term"}
    with pytest.raises(ReviewRequestError, match="does not match a finding"):
        _v2(service, source, target, direction, (resolution,), resolution_context=_context(base))


def test_resolution_aimed_at_an_unreported_ambiguous_name_is_refused(sample_db):
    service = TermService(sample_db)
    source, target, direction = _case("compound_alone")
    base = _v2(service, source, target, direction, ambiguous_mentions=True)
    resolution = {"mention_id": f"{source.index('椿')}:{source.index('椿') + 1}:椿", "choice": "not_a_term"}
    with pytest.raises(ReviewRequestError, match="does not match a finding"):
        _v2(
            service,
            source,
            target,
            direction,
            (resolution,),
            resolution_context=_context(base),
            ambiguous_mentions=True,
        )


def test_review_v1_has_no_ambiguous_mention_feature(sample_db):
    service = TermService(sample_db)
    with pytest.raises(ReviewRequestError, match="review-v1 does not accept"):
        review_pair(
            service, "今汐、椿姐。", "Jinhsi, Camellya.", "en", ambiguous_mentions=True
        )


# ---------------------------------------------------------------------------
# Projection: order and truncation
# ---------------------------------------------------------------------------


def _synthetic_finding(
    index: int, verdict: str, reason: str | None, *, sources: int = 1, pad: int = 0
):
    start = index * 2
    candidate = ReviewCandidate(
        zh="今汐",
        en="Jinhsi",
        category="resonator",
        sources=tuple(
            ReviewSource(source_file=f"RoleInfo.json/{'N' * pad}{n}", source_id=f"Role_{index}_{n}")
            for n in range(sources)
        ),
        candidate_id="ab" * 32,
    )
    return ReviewFinding(
        id=f"{start}:{start + 1}:x{index}",
        verdict=verdict,
        rule_id=RULE_TERM_PAIR,
        source_span=ReviewSpan(start=start, end=start + 1, text="x"),
        target_span=None,
        candidates=(candidate,),
        reason=reason,
    )


def _synthetic_report(findings) -> ReviewReport:
    evaluated = sum(1 for f in findings if f.verdict != VERDICT_NOT_EVALUATED)
    return ReviewReport(
        source_revision="0" * 64,
        target_revision="1" * 64,
        rule_version="review-v2",
        matcher_revision=MATCHER_REVISION,
        dictionary=ReviewDictionary(
            schema_version="2", source_commit="fixture", term_count=1, revision="2" * 64
        ),
        coverage=ReviewCoverage(
            evaluated=evaluated,
            not_evaluated=1 + len(findings) - evaluated,
            rules=("review.term_pair", "review.sentence_meaning"),
        ),
        findings=tuple(findings),
    )


def test_truncation_keeps_all_128_judged_findings_and_drops_the_5_ambiguous_ones():
    """128 judged + 5 ambiguous on a 128-finding page: exactly the 128 judged stay."""
    verdicts = (VERDICT_VERIFIED, VERDICT_CONFLICT, VERDICT_NEEDS_REVIEW)
    findings = []
    # The ambiguous findings come first in the source, where a positional sort
    # alone would put them on the page and push judged terms off it.
    for index in range(5):
        findings.append(_synthetic_finding(index, VERDICT_NOT_EVALUATED, REASON_AMBIGUOUS_MENTION))
    judged = [
        _synthetic_finding(5 + index, verdicts[index % 3], None) for index in range(128)
    ]
    findings.extend(judged)
    report = _synthetic_report(findings)

    body = project_review_report(report, REQUEST_ID, max_findings=128, include_reason=True)

    assert len(_projection_bytes(body)) <= REVIEW_MAX_JSON_BYTES
    page = body["findings"]
    assert isinstance(page, list)
    assert len(page) == 128
    assert [item["id"] for item in page] == [f.id for f in judged]
    assert all("reason" not in item for item in page)
    assert body["truncated"] is True
    assert body["coverage"] == {
        "evaluated": 128,
        "not_evaluated": 6,
        "rules": ["review.term_pair", "review.sentence_meaning"],
    }


def test_ambiguous_findings_follow_every_other_finding_on_the_page():
    findings = [
        _synthetic_finding(0, VERDICT_NOT_EVALUATED, REASON_AMBIGUOUS_MENTION),
        _synthetic_finding(1, VERDICT_NOT_EVALUATED, "ambiguous_repeat"),
        _synthetic_finding(2, VERDICT_VERIFIED, None),
        _synthetic_finding(3, VERDICT_NOT_EVALUATED, REASON_AMBIGUOUS_MENTION),
        _synthetic_finding(4, VERDICT_NOT_EVALUATED, "no_target_region"),
    ]
    body = project_review_report(_synthetic_report(findings), REQUEST_ID, include_reason=True)
    page = body["findings"]
    assert isinstance(page, list)
    assert [item.get("reason") for item in page] == [
        "ambiguous_repeat",
        None,
        "no_target_region",
        REASON_AMBIGUOUS_MENTION,
        REASON_AMBIGUOUS_MENTION,
    ]
    assert body["truncated"] is False


def test_the_byte_ceiling_drops_ambiguous_findings_before_judged_ones():
    judged = [
        _synthetic_finding(10 + index, VERDICT_VERIFIED, None, sources=8, pad=200)
        for index in range(20)
    ]
    ambiguous = [
        _synthetic_finding(
            index, VERDICT_NOT_EVALUATED, REASON_AMBIGUOUS_MENTION, sources=8, pad=200
        )
        for index in range(10)
    ]
    report = _synthetic_report([*ambiguous, *judged])
    body = project_review_report(report, REQUEST_ID, max_findings=128, include_reason=True)
    assert len(_projection_bytes(body)) <= REVIEW_MAX_JSON_BYTES
    page = body["findings"]
    assert isinstance(page, list)
    # The ceiling bites: not every finding fits, and every judged one does.
    assert len(page) < len(judged) + len(ambiguous)
    assert [item["id"] for item in page[: len(judged)]] == [f.id for f in judged]
    assert body["truncated"] is True


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_review(app, token, source, target, *, features, direction="en", extra=None):
    headers = {**bearer(token), "X-WuwaTerm-Matcher-Basis": "1"}
    if features is not None:
        headers["X-WuwaTerm-Review-Features"] = features
    return run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={
                "review_version": "review-v2",
                "source": source,
                "target": target,
                "direction": direction,
                **(extra or {}),
            },
            headers=headers,
        )
    )


@pytest.mark.parametrize(
    "features",
    ("ambiguous-mention-v1", "dense-128,ambiguous-mention-v1"),
)
def test_http_ambiguous_mention_requires_reason_v1(tmp_path, sample_db, features):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    source, target, _direction = _case("enumeration")
    response = _http_review(app, token, source, target, features=features)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"
    assert "X-WuwaTerm-Review-Features" not in response.headers


def test_http_ambiguous_mention_on_review_v1_is_invalid_request(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={"review_version": "review-v1", "source": "今汐。", "target": "Jinhsi.", "direction": "en"},
            headers={**bearer(token), "X-WuwaTerm-Review-Features": "ambiguous-mention-v1,reason-v1"},
        )
    )
    assert response.status_code == 400, response.text


def test_http_full_default_page_keeps_judged_findings_and_drops_ambiguous_ones(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db, max_body_bytes=64 * 1024)
    _, token = issue_device(store, "owner desktop")
    source, target, _direction = _case("page_overflow")

    without = _http_review(app, token, source, target, features="reason-v1")
    page = _http_review(app, token, source, target, features="ambiguous-mention-v1,reason-v1")
    assert page.status_code == 200, page.text
    assert page.headers["X-WuwaTerm-Review-Features"] == "ambiguous-mention-v1,reason-v1"
    body = page.json()
    # 36 judged findings fill the 32-finding page before any ambiguous one.
    assert [item["id"] for item in body["findings"]] == [
        item["id"] for item in without.json()["findings"]
    ]
    assert body["truncated"] is True
    assert body["coverage"]["evaluated"] == without.json()["coverage"]["evaluated"] == 36
    assert body["coverage"]["not_evaluated"] == without.json()["coverage"]["not_evaluated"] + 5

    dense = _http_review(
        app, token, source, target, features="dense-128,reason-v1,ambiguous-mention-v1"
    )
    assert dense.status_code == 200, dense.text
    assert dense.headers["X-WuwaTerm-Review-Features"] == "ambiguous-mention-v1,dense-128,reason-v1"
    findings = dense.json()["findings"]
    assert len(findings) == 41
    assert [item.get("reason") for item in findings[36:]] == [REASON_AMBIGUOUS_MENTION] * 5
    assert all(item.get("reason") != REASON_AMBIGUOUS_MENTION for item in findings[:36])
    assert all(item["verdict"] == "not_evaluated" for item in findings[36:])
    assert dense.json()["truncated"] is False


def test_http_resolution_judges_an_ambiguous_name(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    source, target, _direction = _case("mismatch")
    features = "ambiguous-mention-v1,reason-v1"
    first = _http_review(app, token, source, target, features=features).json()
    [finding] = [f for f in first["findings"] if f.get("reason") == REASON_AMBIGUOUS_MENTION]
    resolved = _http_review(
        app,
        token,
        source,
        target,
        features=features,
        extra={
            "resolutions": [
                {
                    "mention_id": finding["id"],
                    "choice": "official_pair",
                    "candidate_id": finding["candidates"][0]["candidate_id"],
                }
            ],
            "resolution_context": {
                "source_revision": first["source_revision"],
                "rule_version": "review-v2",
                "dictionary_revision": first["dictionary"]["revision"],
                "matcher_revision": first["matcher_revision"],
            },
        },
    )
    assert resolved.status_code == 200, resolved.text
    [judged] = [f for f in resolved.json()["findings"] if f["id"] == finding["id"]]
    assert judged["verdict"] == "confirmed_conflict"
    assert "reason" not in judged


def test_capabilities_always_list_ambiguous_mention(tmp_path, sample_db, monkeypatch):
    for name in ("WUWATERM_OPENAI_BASE_URL", "WUWATERM_OPENAI_API_KEY", "WUWATERM_OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert service_capabilities() == (REVIEW_FEATURE_AMBIGUOUS_MENTION,)
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    response = run(call(app, "GET", "/v1/capabilities", headers=bearer(token)))
    assert response.status_code == 200, response.text
    assert response.json()["capabilities"] == ["ambiguous-mention-v1"]


# fmt: off
MAIN_REVIEW_SNAPSHOT: dict[str, object] = {'aligned': '4d0e15c76edb4b1b15b1859b7f6ed763418f1236d8cd12b6ff3fe4adc2597726',
 'build_tokens': {'v1_report': '7a1178544759f8e743c53909064832220dec633410a7f449914eb27fb0bb758d',
                  'v2_findings': 2,
                  'v2_projection_128_plain': '0b891ecb0a7c5cb3eaad043eaf4bb080a090fd32bea58f2cd7c44c1ea016de52',
                  'v2_projection_128_reason': '0b891ecb0a7c5cb3eaad043eaf4bb080a090fd32bea58f2cd7c44c1ea016de52',
                  'v2_projection_32_plain': '0b891ecb0a7c5cb3eaad043eaf4bb080a090fd32bea58f2cd7c44c1ea016de52',
                  'v2_projection_32_reason': '0b891ecb0a7c5cb3eaad043eaf4bb080a090fd32bea58f2cd7c44c1ea016de52',
                  'v2_report': '2cfaa97386e3ab86aa89b43d5065de5b253edaedf44bf7b742f0ded5c45d5e35'},
 'compound_alone': {'v1_report': '7c99c1d230d075e47b3d6c0019d662237e1b0bdf4e49af0d6eb786e4d71ca76f',
                    'v2_findings': 0,
                    'v2_projection_128_plain': '62ca91987f704fe3c5436810b58e7d378dff76e386888e6c7593ed11bcb35690',
                    'v2_projection_128_reason': '62ca91987f704fe3c5436810b58e7d378dff76e386888e6c7593ed11bcb35690',
                    'v2_projection_32_plain': '62ca91987f704fe3c5436810b58e7d378dff76e386888e6c7593ed11bcb35690',
                    'v2_projection_32_reason': '62ca91987f704fe3c5436810b58e7d378dff76e386888e6c7593ed11bcb35690',
                    'v2_report': '2f5a7b42f602c5171f2fe87aafbeb04a26d77647a9e2511f5da6309c35e144cf'},
 'en_source': {'v1_report': '8cfa6e3f289f9662f02a88440c1adcfabfb55260c94c61f2394fb4deb595bcf5',
               'v2_findings': 3,
               'v2_projection_128_plain': 'f7c28b85bc5059df167b1f299823679531435f0f76f025eb6442b913cb159f80',
               'v2_projection_128_reason': 'f7c28b85bc5059df167b1f299823679531435f0f76f025eb6442b913cb159f80',
               'v2_projection_32_plain': 'f7c28b85bc5059df167b1f299823679531435f0f76f025eb6442b913cb159f80',
               'v2_projection_32_reason': 'f7c28b85bc5059df167b1f299823679531435f0f76f025eb6442b913cb159f80',
               'v2_report': '10aaf1ca1669a488903801b942d7d67f3a5b61247ae6753bcca1384d0ee19e49'},
 'enumeration': {'v1_report': 'b3f101edaad72465d32dcb5b40206dd16ef4aacaf5698b853f43093273788b52',
                 'v2_findings': 2,
                 'v2_projection_128_plain': '6440f3e1e6108f1701709753c9383cfe7c8724d840c1d5a251bc82a114c0e46d',
                 'v2_projection_128_reason': '6440f3e1e6108f1701709753c9383cfe7c8724d840c1d5a251bc82a114c0e46d',
                 'v2_projection_32_plain': '6440f3e1e6108f1701709753c9383cfe7c8724d840c1d5a251bc82a114c0e46d',
                 'v2_projection_32_reason': '6440f3e1e6108f1701709753c9383cfe7c8724d840c1d5a251bc82a114c0e46d',
                 'v2_report': '4d0e15c76edb4b1b15b1859b7f6ed763418f1236d8cd12b6ff3fe4adc2597726'},
 'http': {'build_tokens|basis=0|features=-': {'features': None,
                                              'sha256': '6e55800e0b084ecf215de19a7640748a82441bd7b6f566d1f80941bc4495ce43',
                                              'status': 200},
          'build_tokens|basis=0|features=dense-128': {'features': 'dense-128',
                                                      'sha256': '6e55800e0b084ecf215de19a7640748a82441bd7b6f566d1f80941bc4495ce43',
                                                      'status': 200},
          'build_tokens|basis=0|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                'sha256': '6e55800e0b084ecf215de19a7640748a82441bd7b6f566d1f80941bc4495ce43',
                                                                'status': 200},
          'build_tokens|basis=0|features=reason-v1': {'features': 'reason-v1',
                                                      'sha256': '6e55800e0b084ecf215de19a7640748a82441bd7b6f566d1f80941bc4495ce43',
                                                      'status': 200},
          'build_tokens|basis=1|features=-': {'features': None,
                                              'sha256': '0b891ecb0a7c5cb3eaad043eaf4bb080a090fd32bea58f2cd7c44c1ea016de52',
                                              'status': 200},
          'build_tokens|basis=1|features=dense-128': {'features': 'dense-128',
                                                      'sha256': '0b891ecb0a7c5cb3eaad043eaf4bb080a090fd32bea58f2cd7c44c1ea016de52',
                                                      'status': 200},
          'build_tokens|basis=1|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                'sha256': '0b891ecb0a7c5cb3eaad043eaf4bb080a090fd32bea58f2cd7c44c1ea016de52',
                                                                'status': 200},
          'build_tokens|basis=1|features=reason-v1': {'features': 'reason-v1',
                                                      'sha256': '0b891ecb0a7c5cb3eaad043eaf4bb080a090fd32bea58f2cd7c44c1ea016de52',
                                                      'status': 200},
          'build_tokens|review-v1': {'features': None,
                                     'sha256': '7f65a652ec2488bae527cbbe2081e4ba39566bac0f84b3293b8ac1af72195a84',
                                     'status': 200},
          'compound_alone|basis=0|features=-': {'features': None,
                                                'sha256': 'f28df030652135ce766117003f83c430646f7e840bf368e6c0fd86b47c8d36a5',
                                                'status': 200},
          'compound_alone|basis=0|features=dense-128': {'features': 'dense-128',
                                                        'sha256': 'f28df030652135ce766117003f83c430646f7e840bf368e6c0fd86b47c8d36a5',
                                                        'status': 200},
          'compound_alone|basis=0|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                  'sha256': 'f28df030652135ce766117003f83c430646f7e840bf368e6c0fd86b47c8d36a5',
                                                                  'status': 200},
          'compound_alone|basis=0|features=reason-v1': {'features': 'reason-v1',
                                                        'sha256': 'f28df030652135ce766117003f83c430646f7e840bf368e6c0fd86b47c8d36a5',
                                                        'status': 200},
          'compound_alone|basis=1|features=-': {'features': None,
                                                'sha256': '62ca91987f704fe3c5436810b58e7d378dff76e386888e6c7593ed11bcb35690',
                                                'status': 200},
          'compound_alone|basis=1|features=dense-128': {'features': 'dense-128',
                                                        'sha256': '62ca91987f704fe3c5436810b58e7d378dff76e386888e6c7593ed11bcb35690',
                                                        'status': 200},
          'compound_alone|basis=1|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                  'sha256': '62ca91987f704fe3c5436810b58e7d378dff76e386888e6c7593ed11bcb35690',
                                                                  'status': 200},
          'compound_alone|basis=1|features=reason-v1': {'features': 'reason-v1',
                                                        'sha256': '62ca91987f704fe3c5436810b58e7d378dff76e386888e6c7593ed11bcb35690',
                                                        'status': 200},
          'compound_alone|review-v1': {'features': None,
                                       'sha256': 'd31fe3f1ff916e100913d1cc1ca534041d20edd69e14937431350dc4f7e64250',
                                       'status': 200},
          'en_source|basis=0|features=-': {'features': None,
                                           'sha256': 'b1d7105659c88f39fb2467149d437779c0b496c65baad5d0f022fdf548ba5f02',
                                           'status': 200},
          'en_source|basis=0|features=dense-128': {'features': 'dense-128',
                                                   'sha256': 'b1d7105659c88f39fb2467149d437779c0b496c65baad5d0f022fdf548ba5f02',
                                                   'status': 200},
          'en_source|basis=0|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                             'sha256': 'b1d7105659c88f39fb2467149d437779c0b496c65baad5d0f022fdf548ba5f02',
                                                             'status': 200},
          'en_source|basis=0|features=reason-v1': {'features': 'reason-v1',
                                                   'sha256': 'b1d7105659c88f39fb2467149d437779c0b496c65baad5d0f022fdf548ba5f02',
                                                   'status': 200},
          'en_source|basis=1|features=-': {'features': None,
                                           'sha256': 'f7c28b85bc5059df167b1f299823679531435f0f76f025eb6442b913cb159f80',
                                           'status': 200},
          'en_source|basis=1|features=dense-128': {'features': 'dense-128',
                                                   'sha256': 'f7c28b85bc5059df167b1f299823679531435f0f76f025eb6442b913cb159f80',
                                                   'status': 200},
          'en_source|basis=1|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                             'sha256': 'f7c28b85bc5059df167b1f299823679531435f0f76f025eb6442b913cb159f80',
                                                             'status': 200},
          'en_source|basis=1|features=reason-v1': {'features': 'reason-v1',
                                                   'sha256': 'f7c28b85bc5059df167b1f299823679531435f0f76f025eb6442b913cb159f80',
                                                   'status': 200},
          'en_source|review-v1': {'features': None,
                                  'sha256': '8740b2da4a7c1a2ddb95be7033eba4362fbcee5669517f8cc9b4da92df789029',
                                  'status': 200},
          'enumeration|basis=0|features=-': {'features': None,
                                             'sha256': 'c13738a482225dab818378014c8b00178ef06686e1ea9fb6902322e5dc28123d',
                                             'status': 200},
          'enumeration|basis=0|features=dense-128': {'features': 'dense-128',
                                                     'sha256': 'c13738a482225dab818378014c8b00178ef06686e1ea9fb6902322e5dc28123d',
                                                     'status': 200},
          'enumeration|basis=0|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                               'sha256': 'c13738a482225dab818378014c8b00178ef06686e1ea9fb6902322e5dc28123d',
                                                               'status': 200},
          'enumeration|basis=0|features=reason-v1': {'features': 'reason-v1',
                                                     'sha256': 'c13738a482225dab818378014c8b00178ef06686e1ea9fb6902322e5dc28123d',
                                                     'status': 200},
          'enumeration|basis=1|features=-': {'features': None,
                                             'sha256': '6440f3e1e6108f1701709753c9383cfe7c8724d840c1d5a251bc82a114c0e46d',
                                             'status': 200},
          'enumeration|basis=1|features=dense-128': {'features': 'dense-128',
                                                     'sha256': '6440f3e1e6108f1701709753c9383cfe7c8724d840c1d5a251bc82a114c0e46d',
                                                     'status': 200},
          'enumeration|basis=1|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                               'sha256': '6440f3e1e6108f1701709753c9383cfe7c8724d840c1d5a251bc82a114c0e46d',
                                                               'status': 200},
          'enumeration|basis=1|features=reason-v1': {'features': 'reason-v1',
                                                     'sha256': '6440f3e1e6108f1701709753c9383cfe7c8724d840c1d5a251bc82a114c0e46d',
                                                     'status': 200},
          'enumeration|review-v1': {'features': None,
                                    'sha256': 'efc8ec9299344ab1fb870bc50875c2bca8d1aed68ee6d8ea97cd44f34962b09d',
                                    'status': 200},
          'mismatch|basis=0|features=-': {'features': None,
                                          'sha256': '46f285e205165670722727721218d291ca62a29b9942679395c67902e2e0cd26',
                                          'status': 200},
          'mismatch|basis=0|features=dense-128': {'features': 'dense-128',
                                                  'sha256': '46f285e205165670722727721218d291ca62a29b9942679395c67902e2e0cd26',
                                                  'status': 200},
          'mismatch|basis=0|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                            'sha256': '46f285e205165670722727721218d291ca62a29b9942679395c67902e2e0cd26',
                                                            'status': 200},
          'mismatch|basis=0|features=reason-v1': {'features': 'reason-v1',
                                                  'sha256': '46f285e205165670722727721218d291ca62a29b9942679395c67902e2e0cd26',
                                                  'status': 200},
          'mismatch|basis=1|features=-': {'features': None,
                                          'sha256': 'd69d7247358734c0ef976753738ac3388fac016dc4c542fc24480f2db64ec4e6',
                                          'status': 200},
          'mismatch|basis=1|features=dense-128': {'features': 'dense-128',
                                                  'sha256': 'd69d7247358734c0ef976753738ac3388fac016dc4c542fc24480f2db64ec4e6',
                                                  'status': 200},
          'mismatch|basis=1|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                            'sha256': 'd69d7247358734c0ef976753738ac3388fac016dc4c542fc24480f2db64ec4e6',
                                                            'status': 200},
          'mismatch|basis=1|features=reason-v1': {'features': 'reason-v1',
                                                  'sha256': 'd69d7247358734c0ef976753738ac3388fac016dc4c542fc24480f2db64ec4e6',
                                                  'status': 200},
          'mismatch|review-v1': {'features': None,
                                 'sha256': 'a6416a55be5f9e69bb9f5c07fc41cdb7373b69c1492b6302da1a4fd621e5d160',
                                 'status': 200},
          'name_and_compound|basis=0|features=-': {'features': None,
                                                   'sha256': 'd89dc071394717b97833003fdc42d9c3716643ef1e3002482ed31ee15b36dfae',
                                                   'status': 200},
          'name_and_compound|basis=0|features=dense-128': {'features': 'dense-128',
                                                           'sha256': 'd89dc071394717b97833003fdc42d9c3716643ef1e3002482ed31ee15b36dfae',
                                                           'status': 200},
          'name_and_compound|basis=0|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                     'sha256': 'd89dc071394717b97833003fdc42d9c3716643ef1e3002482ed31ee15b36dfae',
                                                                     'status': 200},
          'name_and_compound|basis=0|features=reason-v1': {'features': 'reason-v1',
                                                           'sha256': 'd89dc071394717b97833003fdc42d9c3716643ef1e3002482ed31ee15b36dfae',
                                                           'status': 200},
          'name_and_compound|basis=1|features=-': {'features': None,
                                                   'sha256': '3c25691d880a2304c528eebad146ca2509ee8133caf4bb8fcab7dee252a21bee',
                                                   'status': 200},
          'name_and_compound|basis=1|features=dense-128': {'features': 'dense-128',
                                                           'sha256': '3c25691d880a2304c528eebad146ca2509ee8133caf4bb8fcab7dee252a21bee',
                                                           'status': 200},
          'name_and_compound|basis=1|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                     'sha256': '3c25691d880a2304c528eebad146ca2509ee8133caf4bb8fcab7dee252a21bee',
                                                                     'status': 200},
          'name_and_compound|basis=1|features=reason-v1': {'features': 'reason-v1',
                                                           'sha256': '3c25691d880a2304c528eebad146ca2509ee8133caf4bb8fcab7dee252a21bee',
                                                           'status': 200},
          'name_and_compound|review-v1': {'features': None,
                                          'sha256': 'ce64704e5307edb83bfad533fda85c60bf38353c9a2f08d2406f5dbf6c592c55',
                                          'status': 200},
          'no_target_region|basis=0|features=-': {'features': None,
                                                  'sha256': 'a2d96fa394ba22573a7f5f239a94edea92cf890fba277fe18dd971af9e9e490a',
                                                  'status': 200},
          'no_target_region|basis=0|features=dense-128': {'features': 'dense-128',
                                                          'sha256': 'a2d96fa394ba22573a7f5f239a94edea92cf890fba277fe18dd971af9e9e490a',
                                                          'status': 200},
          'no_target_region|basis=0|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                    'sha256': 'f076ab0f96e18a85deca7eb0c629c237b1b841e2d0878cd08041f44fdf3d073d',
                                                                    'status': 200},
          'no_target_region|basis=0|features=reason-v1': {'features': 'reason-v1',
                                                          'sha256': 'f076ab0f96e18a85deca7eb0c629c237b1b841e2d0878cd08041f44fdf3d073d',
                                                          'status': 200},
          'no_target_region|basis=1|features=-': {'features': None,
                                                  'sha256': '1156d2c30b8d0a8d0432da11c8225e1b9e682f83f57d13ffb81b0f6e5ce52562',
                                                  'status': 200},
          'no_target_region|basis=1|features=dense-128': {'features': 'dense-128',
                                                          'sha256': '1156d2c30b8d0a8d0432da11c8225e1b9e682f83f57d13ffb81b0f6e5ce52562',
                                                          'status': 200},
          'no_target_region|basis=1|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                    'sha256': 'c4d8216d56e013be60a8b1c1adb1adfa646fa38abab13516e98c42085a80f82b',
                                                                    'status': 200},
          'no_target_region|basis=1|features=reason-v1': {'features': 'reason-v1',
                                                          'sha256': 'c4d8216d56e013be60a8b1c1adb1adfa646fa38abab13516e98c42085a80f82b',
                                                          'status': 200},
          'no_target_region|review-v1': {'features': None,
                                         'sha256': '78448b7bb126913c929a1ee2095f45878814b001e6934cea2b10fbd761ab65b5',
                                         'status': 200},
          'page_overflow|basis=0|features=-': {'features': None,
                                               'sha256': '529b9cad55bdea1ac72d0cb9e42c76ba56a5da6eb275e53e87a43f6412e92ae8',
                                               'status': 200},
          'page_overflow|basis=0|features=dense-128': {'features': 'dense-128',
                                                       'sha256': 'd52e29241c9bbb1a3eb25f5967720f17591045f4be5d87d37dd20d909c449e75',
                                                       'status': 200},
          'page_overflow|basis=0|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                 'sha256': 'd52e29241c9bbb1a3eb25f5967720f17591045f4be5d87d37dd20d909c449e75',
                                                                 'status': 200},
          'page_overflow|basis=0|features=reason-v1': {'features': 'reason-v1',
                                                       'sha256': '529b9cad55bdea1ac72d0cb9e42c76ba56a5da6eb275e53e87a43f6412e92ae8',
                                                       'status': 200},
          'page_overflow|basis=1|features=-': {'features': None,
                                               'sha256': '06d0706b3285d7b2e872b0380e9d7d8418edf4e3403c9404a64a7725061adb4a',
                                               'status': 200},
          'page_overflow|basis=1|features=dense-128': {'features': 'dense-128',
                                                       'sha256': 'cf91faa75913fc0c2b6c54bd946211bb0e0ece965421bf5bf16411c6f7debcd7',
                                                       'status': 200},
          'page_overflow|basis=1|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                 'sha256': 'cf91faa75913fc0c2b6c54bd946211bb0e0ece965421bf5bf16411c6f7debcd7',
                                                                 'status': 200},
          'page_overflow|basis=1|features=reason-v1': {'features': 'reason-v1',
                                                       'sha256': '06d0706b3285d7b2e872b0380e9d7d8418edf4e3403c9404a64a7725061adb4a',
                                                       'status': 200},
          'page_overflow|review-v1': {'features': None,
                                      'sha256': '098799a043298d25d1f9f4f771bbd281da3da9c74a524109614d1e88195b9a80',
                                      'status': 200},
          'repeat_shortage|basis=0|features=-': {'features': None,
                                                 'sha256': '5e157fbdc73ba23b3fc9fa69e4125c319850bdb5bba7d0830eb92be76d6b71da',
                                                 'status': 200},
          'repeat_shortage|basis=0|features=dense-128': {'features': 'dense-128',
                                                         'sha256': '5e157fbdc73ba23b3fc9fa69e4125c319850bdb5bba7d0830eb92be76d6b71da',
                                                         'status': 200},
          'repeat_shortage|basis=0|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                   'sha256': '1fea63b0e6d75d5e103e535bb87864d55122887e87c9118d5e0816ae6bafbca1',
                                                                   'status': 200},
          'repeat_shortage|basis=0|features=reason-v1': {'features': 'reason-v1',
                                                         'sha256': '1fea63b0e6d75d5e103e535bb87864d55122887e87c9118d5e0816ae6bafbca1',
                                                         'status': 200},
          'repeat_shortage|basis=1|features=-': {'features': None,
                                                 'sha256': '82bbcb7f792cc91d882cec4daf3b7dbeef3bb5d5221ce870cdb747fd23869e44',
                                                 'status': 200},
          'repeat_shortage|basis=1|features=dense-128': {'features': 'dense-128',
                                                         'sha256': '82bbcb7f792cc91d882cec4daf3b7dbeef3bb5d5221ce870cdb747fd23869e44',
                                                         'status': 200},
          'repeat_shortage|basis=1|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                   'sha256': '268ef4c4d0f41d76f5f04de21697bdfa129aa4dfac2623041a5c507ae3908d79',
                                                                   'status': 200},
          'repeat_shortage|basis=1|features=reason-v1': {'features': 'reason-v1',
                                                         'sha256': '268ef4c4d0f41d76f5f04de21697bdfa129aa4dfac2623041a5c507ae3908d79',
                                                         'status': 200},
          'repeat_shortage|review-v1': {'features': None,
                                        'sha256': 'c3e2648442eb5c77d374bfe17105d4b9ae941fa2f915ee5bc223a4638ef78a62',
                                        'status': 200},
          'speaker_labels|basis=0|features=-': {'features': None,
                                                'sha256': '49870760786d1df7c515b8b28d7c7810517b68f3c9c9c69886cf2d3e72674a1c',
                                                'status': 200},
          'speaker_labels|basis=0|features=dense-128': {'features': 'dense-128',
                                                        'sha256': '49870760786d1df7c515b8b28d7c7810517b68f3c9c9c69886cf2d3e72674a1c',
                                                        'status': 200},
          'speaker_labels|basis=0|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                  'sha256': '49870760786d1df7c515b8b28d7c7810517b68f3c9c9c69886cf2d3e72674a1c',
                                                                  'status': 200},
          'speaker_labels|basis=0|features=reason-v1': {'features': 'reason-v1',
                                                        'sha256': '49870760786d1df7c515b8b28d7c7810517b68f3c9c9c69886cf2d3e72674a1c',
                                                        'status': 200},
          'speaker_labels|basis=1|features=-': {'features': None,
                                                'sha256': '9f08837efa72c2118e68c632af4cfee681f7251dcd5b29cea20e0709e324e4f2',
                                                'status': 200},
          'speaker_labels|basis=1|features=dense-128': {'features': 'dense-128',
                                                        'sha256': '9f08837efa72c2118e68c632af4cfee681f7251dcd5b29cea20e0709e324e4f2',
                                                        'status': 200},
          'speaker_labels|basis=1|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                                  'sha256': '9f08837efa72c2118e68c632af4cfee681f7251dcd5b29cea20e0709e324e4f2',
                                                                  'status': 200},
          'speaker_labels|basis=1|features=reason-v1': {'features': 'reason-v1',
                                                        'sha256': '9f08837efa72c2118e68c632af4cfee681f7251dcd5b29cea20e0709e324e4f2',
                                                        'status': 200},
          'speaker_labels|review-v1': {'features': None,
                                       'sha256': '66832ab35ed21b3e1499b43e1716a76cbe35f17fc90ccbcfb4a4dc2d2530fdda',
                                       'status': 200},
          'stray_ambiguous_resolution': {'features': None,
                                         'sha256': '46986250912bfa978fe270252b704f85122d2713eb4c7452e1520fbb38c4dd23',
                                         'status': 400},
          'topic_title|basis=0|features=-': {'features': None,
                                             'sha256': '9b91b25794addd1fc4611032ca3e1cfc2b45c7c0c02e797f77323397a673b612',
                                             'status': 200},
          'topic_title|basis=0|features=dense-128': {'features': 'dense-128',
                                                     'sha256': '9b91b25794addd1fc4611032ca3e1cfc2b45c7c0c02e797f77323397a673b612',
                                                     'status': 200},
          'topic_title|basis=0|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                               'sha256': '9b91b25794addd1fc4611032ca3e1cfc2b45c7c0c02e797f77323397a673b612',
                                                               'status': 200},
          'topic_title|basis=0|features=reason-v1': {'features': 'reason-v1',
                                                     'sha256': '9b91b25794addd1fc4611032ca3e1cfc2b45c7c0c02e797f77323397a673b612',
                                                     'status': 200},
          'topic_title|basis=1|features=-': {'features': None,
                                             'sha256': 'f20b028b5fdbb17e0e78c58071085ca1c514d3459f5d06f4f956ca95ad4e0801',
                                             'status': 200},
          'topic_title|basis=1|features=dense-128': {'features': 'dense-128',
                                                     'sha256': 'f20b028b5fdbb17e0e78c58071085ca1c514d3459f5d06f4f956ca95ad4e0801',
                                                     'status': 200},
          'topic_title|basis=1|features=dense-128,reason-v1': {'features': 'dense-128,reason-v1',
                                                               'sha256': 'f20b028b5fdbb17e0e78c58071085ca1c514d3459f5d06f4f956ca95ad4e0801',
                                                               'status': 200},
          'topic_title|basis=1|features=reason-v1': {'features': 'reason-v1',
                                                     'sha256': 'f20b028b5fdbb17e0e78c58071085ca1c514d3459f5d06f4f956ca95ad4e0801',
                                                     'status': 200},
          'topic_title|review-v1': {'features': None,
                                    'sha256': 'c5597a2d7e40796b850c772804dcbb2f6e97d29d7c21b0f7135b27e7089b77cb',
                                    'status': 200}},
 'mismatch': {'v1_report': 'e94ab9785d10e5289beca08ddb8feee86aa0c6bd8d2abe2c029d62a36c6b2c69',
              'v2_findings': 2,
              'v2_projection_128_plain': 'd69d7247358734c0ef976753738ac3388fac016dc4c542fc24480f2db64ec4e6',
              'v2_projection_128_reason': 'd69d7247358734c0ef976753738ac3388fac016dc4c542fc24480f2db64ec4e6',
              'v2_projection_32_plain': 'd69d7247358734c0ef976753738ac3388fac016dc4c542fc24480f2db64ec4e6',
              'v2_projection_32_reason': 'd69d7247358734c0ef976753738ac3388fac016dc4c542fc24480f2db64ec4e6',
              'v2_report': 'c4e3b9d4a2a160342fedbccb6e9a18a3ceef7087529c86b19c0cff191605d833'},
 'name_and_compound': {'v1_report': '6bbdb3ccc0ee8bac34cd52b4852f807783b910c3ddfdbb2ba785dc8402b09288',
                       'v2_findings': 2,
                       'v2_projection_128_plain': '3c25691d880a2304c528eebad146ca2509ee8133caf4bb8fcab7dee252a21bee',
                       'v2_projection_128_reason': '3c25691d880a2304c528eebad146ca2509ee8133caf4bb8fcab7dee252a21bee',
                       'v2_projection_32_plain': '3c25691d880a2304c528eebad146ca2509ee8133caf4bb8fcab7dee252a21bee',
                       'v2_projection_32_reason': '3c25691d880a2304c528eebad146ca2509ee8133caf4bb8fcab7dee252a21bee',
                       'v2_report': 'a924b37ca8277df9596bf2ef08a9aaddf67a639b5326fb467f6348f7be82027e'},
 'no_target_region': {'v1_report': 'e4e6ee2eda3210a7f3ffec76655aa10509ad72dae9f0f21b9cdd3177d31f0d61',
                      'v2_findings': 2,
                      'v2_projection_128_plain': '1156d2c30b8d0a8d0432da11c8225e1b9e682f83f57d13ffb81b0f6e5ce52562',
                      'v2_projection_128_reason': 'c4d8216d56e013be60a8b1c1adb1adfa646fa38abab13516e98c42085a80f82b',
                      'v2_projection_32_plain': '1156d2c30b8d0a8d0432da11c8225e1b9e682f83f57d13ffb81b0f6e5ce52562',
                      'v2_projection_32_reason': 'c4d8216d56e013be60a8b1c1adb1adfa646fa38abab13516e98c42085a80f82b',
                      'v2_report': 'c642a2d8a831bacd5d3d2c9e855026a587fa4d46e5c572c8447bcc0717c13be7'},
 'page_overflow': {'v1_report': '827198febf3348605378a08e500d8810282dd009c22a0679c1c7efcdb0d050b9',
                   'v2_findings': 36,
                   'v2_projection_128_plain': 'cf91faa75913fc0c2b6c54bd946211bb0e0ece965421bf5bf16411c6f7debcd7',
                   'v2_projection_128_reason': 'cf91faa75913fc0c2b6c54bd946211bb0e0ece965421bf5bf16411c6f7debcd7',
                   'v2_projection_32_plain': '06d0706b3285d7b2e872b0380e9d7d8418edf4e3403c9404a64a7725061adb4a',
                   'v2_projection_32_reason': '06d0706b3285d7b2e872b0380e9d7d8418edf4e3403c9404a64a7725061adb4a',
                   'v2_report': 'bd62035432df08fd151631aa824b97d679fc1713e392adc9b4ab4e7382b6d107'},
 'repeat_shortage': {'v1_report': '470109c0cc0a478e302aba5e148010ea44af27f6f561a05eb644f729ad9b4976',
                     'v2_findings': 2,
                     'v2_projection_128_plain': '82bbcb7f792cc91d882cec4daf3b7dbeef3bb5d5221ce870cdb747fd23869e44',
                     'v2_projection_128_reason': '268ef4c4d0f41d76f5f04de21697bdfa129aa4dfac2623041a5c507ae3908d79',
                     'v2_projection_32_plain': '82bbcb7f792cc91d882cec4daf3b7dbeef3bb5d5221ce870cdb747fd23869e44',
                     'v2_projection_32_reason': '268ef4c4d0f41d76f5f04de21697bdfa129aa4dfac2623041a5c507ae3908d79',
                     'v2_report': 'eb4dfbf7c8259b26fb5e22dae1fd8f617081c594e299cdcea887de2b04a4e6f4'},
 'resolved_official_pair': '4d0e15c76edb4b1b15b1859b7f6ed763418f1236d8cd12b6ff3fe4adc2597726',
 'speaker_labels': {'v1_report': 'ffab92114a98644d3bf0c939748fdda93fa071417fde0bd6734e95e275217737',
                    'v2_findings': 1,
                    'v2_projection_128_plain': '9f08837efa72c2118e68c632af4cfee681f7251dcd5b29cea20e0709e324e4f2',
                    'v2_projection_128_reason': '9f08837efa72c2118e68c632af4cfee681f7251dcd5b29cea20e0709e324e4f2',
                    'v2_projection_32_plain': '9f08837efa72c2118e68c632af4cfee681f7251dcd5b29cea20e0709e324e4f2',
                    'v2_projection_32_reason': '9f08837efa72c2118e68c632af4cfee681f7251dcd5b29cea20e0709e324e4f2',
                    'v2_report': 'd1e664d9de8a91530b4bbe160881a959302081431cf6fcf9efa36b41f8775bfe'},
 'topic_title': {'v1_report': 'b939f88d3f29a8bfa2443a5225e7a0a9f7752b2dddf23e72c1760d9e464d1c9b',
                 'v2_findings': 2,
                 'v2_projection_128_plain': 'f20b028b5fdbb17e0e78c58071085ca1c514d3459f5d06f4f956ca95ad4e0801',
                 'v2_projection_128_reason': 'f20b028b5fdbb17e0e78c58071085ca1c514d3459f5d06f4f956ca95ad4e0801',
                 'v2_projection_32_plain': 'f20b028b5fdbb17e0e78c58071085ca1c514d3459f5d06f4f956ca95ad4e0801',
                 'v2_projection_32_reason': 'f20b028b5fdbb17e0e78c58071085ca1c514d3459f5d06f4f956ca95ad4e0801',
                 'v2_report': '2ced56e4b8a344ed8657e99a01f178d51e5f7b3822b9ee60c2927f5d95a46dff'}}
# fmt: on
