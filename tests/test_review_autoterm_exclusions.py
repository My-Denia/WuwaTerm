"""F10 fixture regressions: review-v2 forks automatic term discovery from
free-text lock exclusion metadata.

These tests own a private temporary SQLite dictionary (same connect /
initialize / insert_records / TermService pattern as tests/test_review_v2.py)
whose exclusion metadata rows are written into the metadata table. Every
judgment-matrix cell is pinned end-to-end through ``review_pair`` with
``review_version='review-v2'`` — request validation is never bypassed: an
automatically discovered mention whose exact source surface appears in either
exclusion key asserts no terminology constraint (``not_evaluated`` with no
target span, every candidate preserved, no target span reserved) unless the
request carries an explicit resolution, which keeps the existing
authoritative verification, conflict, elsewhere, and rejection behavior.
Nonexcluded controls and the review-v1 path are pinned unchanged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from wuwaterm.constants import (
    FREE_TEXT_LOCK_EXCLUSIONS_KEY,
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY,
)
from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.lookup import TermService
from wuwaterm.models import TermRecord
from wuwaterm.review import (
    RULE_SENTENCE_MEANING,
    RULE_TERM_PAIR,
    RULE_VERSION,
    RULE_VERSION_V2,
    VERDICT_CONFLICT,
    VERDICT_NEEDS_REVIEW,
    VERDICT_NOT_EVALUATED,
    VERDICT_VERIFIED,
    ReviewRequestError,
    review_pair,
    text_revision,
)

# EN-excluded ordinary surfaces: It/Fish/Tea/Discord (synthetic fixtures).
# Fish is deliberately recorded with two official forms (鱼肉 and 鱼), and the
# 茶汤/Tea record shares the excluded "Tea" surface's official target form.
# ZH-excluded ordinary surface: 大人. Sorted ascending per the parser contract.
EN_EXCLUSION_ROWS = (("Discord", 12, 2), ("Fish", 12, 2), ("It", 12, 2), ("Tea", 12, 2))
ZH_EXCLUSION_ROWS = (("大人", 12, 2),)

TERMS = (
    TermRecord("speaker", "SyntheticSpeakers.json", "speaker-it", "speaker-it", "祂", "It"),
    TermRecord("item", "SyntheticItems.json", "item-fish-meat", "item-fish-meat", "鱼肉", "Fish"),
    TermRecord("monster", "SyntheticMonsters.json", "monster-fish", "monster-fish", "鱼", "Fish"),
    TermRecord("item", "SyntheticItems.json", "item-tea-leaf", "item-tea-leaf", "茶叶", "Tea"),
    TermRecord("item", "SyntheticItems.json", "item-tea-soup", "item-tea-soup", "茶汤", "Tea"),
    TermRecord("weapon", "SyntheticWeapons.json", "weapon-discord", "weapon-discord", "异响空灵", "Discord"),
    TermRecord("speaker", "SyntheticSpeakers.json", "speaker-adult", "speaker-adult", "大人", "Adult"),
    TermRecord("character", "SyntheticCharacters.json", "character-jinhsi", "character-jinhsi", "今汐", "Jinhsi"),
    TermRecord("item", "SyntheticItems.json", "item-echo", "item-echo", "声骸", "Echo"),
    # A longer surface whose text contains the excluded substring "Fish".
    TermRecord("item", "SyntheticItems.json", "item-starfish", "item-starfish", "海星", "Starfish"),
)


def _exclusion_value(rows) -> str:
    return json.dumps(
        {
            "version": 1,
            "min_occurrences": 10,
            "max_aligned_ratio": [1, 5],
            "surfaces": [list(row) for row in rows],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _build_service(
    path: Path,
    *,
    en_value: str | None = None,
    zh_value: str | None = None,
) -> TermService:
    with connect(path) as conn:
        initialize(conn)
        metadata = [
            ("schema_version", "synthetic-v1"),
            ("source_commit", "fixture-commit"),
        ]
        if en_value is not None:
            metadata.append((FREE_TEXT_LOCK_EXCLUSIONS_KEY, en_value))
        if zh_value is not None:
            metadata.append((FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY, zh_value))
        conn.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", metadata)
        insert_records(conn, TERMS)
        conn.commit()
    return TermService(path)


@pytest.fixture()
def excluded_service(tmp_path: Path) -> TermService:
    return _build_service(
        tmp_path / "exclusions.db",
        en_value=_exclusion_value(EN_EXCLUSION_ROWS),
        zh_value=_exclusion_value(ZH_EXCLUSION_ROWS),
    )


@pytest.fixture()
def plain_service(tmp_path: Path) -> TermService:
    """Same dictionary rows with both exclusion keys absent (pre-F10)."""
    return _build_service(tmp_path / "plain.db")


@pytest.fixture()
def partial_service(tmp_path: Path) -> TermService:
    """Invalid English key alongside a valid zh key; each parses independently."""
    return _build_service(
        tmp_path / "partial.db",
        en_value="not json",
        zh_value=_exclusion_value(ZH_EXCLUSION_ROWS),
    )


@pytest.fixture()
def invalid_service(tmp_path: Path) -> TermService:
    """Both keys present but invalid."""
    return _build_service(tmp_path / "invalid.db", en_value="not json", zh_value="[]")


def _v2(service: TermService, source: str, target: str, direction: str, **overrides):
    request = {
        "source": source,
        "target": target,
        "direction": direction,
        "review_version": RULE_VERSION_V2,
    }
    request.update(overrides)
    return review_pair(service, **request)


def _context(report, source: str) -> dict[str, str]:
    return {
        "source_revision": text_revision(source),
        "rule_version": report.rule_version,
        "dictionary_revision": report.dictionary.revision,
        "matcher_revision": report.matcher_revision,
    }


def _official_pair(finding, candidate) -> dict[str, str]:
    return {
        "mention_id": finding.id,
        "choice": "official_pair",
        "candidate_id": candidate.candidate_id,
    }


def _candidate(finding, zh: str, en: str):
    for candidate in finding.candidates:
        if candidate.zh == zh and candidate.en == en:
            return candidate
    raise AssertionError(f"no ({zh}, {en}) candidate on finding {finding.id}")


def _finding_identity(finding):
    """Everything about a finding except the verdict and target span."""
    return (
        finding.id,
        finding.rule_id,
        (finding.source_span.start, finding.source_span.end, finding.source_span.text),
        finding.candidates_truncated,
        tuple(
            (
                candidate.candidate_id,
                candidate.zh,
                candidate.en,
                candidate.category,
                tuple(
                    (source.source_file, source.source_id)
                    for source in candidate.sources
                ),
            )
            for candidate in finding.candidates
        ),
    )


def test_excluded_automatic_without_target_form_is_not_evaluated(excluded_service, plain_service):
    report = _v2(excluded_service, "Fish is food.", "鱼肉是食物。", "en")
    finding = report.findings[0]
    assert finding.id == "0:4:Fish"
    assert finding.verdict == VERDICT_NOT_EVALUATED
    assert finding.rule_id == RULE_TERM_PAIR
    assert finding.target_span is None
    assert [c.en for c in finding.candidates] == ["Fish", "Fish"]
    assert all(c.candidate_id for c in finding.candidates)

    # Pre-F10 the same automatic mention asserted a constraint (needs_review).
    plain = _v2(plain_service, "Fish is food.", "鱼肉是食物。", "en")
    assert plain.findings[0].verdict == VERDICT_NEEDS_REVIEW
    assert _finding_identity(plain.findings[0]) == _finding_identity(finding)


def test_excluded_automatic_with_present_form_is_not_evaluated(excluded_service, plain_service):
    # The official zh form 祂 sits in the mapped region, but target-form
    # presence alone must not verify an ambiguous ordinary word (D3).
    report = _v2(excluded_service, "It matters.", "祂很重要。", "zh")
    finding = report.findings[0]
    assert finding.id == "0:2:It"
    assert finding.verdict == VERDICT_NOT_EVALUATED
    assert finding.target_span is None
    assert [(c.zh, c.en, c.category) for c in finding.candidates] == [("祂", "It", "speaker")]

    plain = _v2(plain_service, "It matters.", "祂很重要。", "zh")
    assert plain.findings[0].verdict == VERDICT_VERIFIED
    assert plain.findings[0].target_span is not None
    assert plain.findings[0].target_span.text == "祂"
    assert _finding_identity(plain.findings[0]) == _finding_identity(finding)


def test_excluded_automatic_with_multiple_official_forms_is_not_evaluated(excluded_service, plain_service):
    # Surface "Fish" carries two official zh forms; the zh direction target
    # contains both. Pre-F10 this is the needs_review branch.
    source, target = "Fish swims.", "鱼肉和鱼都很新鲜。"
    plain = _v2(plain_service, source, target, "zh")
    assert plain.findings[0].verdict == VERDICT_NEEDS_REVIEW
    assert plain.findings[0].target_span is not None
    assert plain.findings[0].target_span.text == "鱼肉"
    assert len(plain.findings[0].candidates) == 2

    report = _v2(excluded_service, source, target, "zh")
    finding = report.findings[0]
    assert finding.verdict == VERDICT_NOT_EVALUATED
    assert finding.target_span is None
    assert _finding_identity(plain.findings[0]) == _finding_identity(finding)


def test_excluded_automatic_with_elsewhere_only_form_is_not_evaluated(excluded_service, plain_service):
    source, target = "Fish swims. Birds sing.", "Birds sing. Fish swim."
    for service in (excluded_service, plain_service):
        report = _v2(service, source, target, "en")
        finding = report.findings[0]
        assert finding.id == "0:4:Fish"
        assert finding.verdict == VERDICT_NOT_EVALUATED
        assert finding.target_span is None
        assert len(finding.candidates) == 2


def test_excluded_automatic_without_mapping_is_not_evaluated(excluded_service):
    # Mismatched sentence counts produce no mapping at all.
    report = _v2(excluded_service, "Fish.", "No fish here. None.", "en")
    assert report.findings[0].verdict == VERDICT_NOT_EVALUATED
    assert report.findings[0].target_span is None

    # Explicitly empty mappings map nothing.
    report = _v2(excluded_service, "Tea.", "Tea.", "en", alignments=[])
    assert report.findings[0].verdict == VERDICT_NOT_EVALUATED
    assert report.findings[0].target_span is None

    # An explicit null target mapping leaves the mention unmapped.
    report = _v2(
        excluded_service,
        "Tea.",
        "Tea.",
        "en",
        alignments=[
            {
                "source": {"start": 0, "end": 4, "text": "Tea."},
                "target": None,
            }
        ],
    )
    assert report.findings[0].verdict == VERDICT_NOT_EVALUATED
    assert report.findings[0].target_span is None


def test_excluded_not_a_term_is_not_evaluated(excluded_service, plain_service):
    source, target = "It matters.", "祂很重要。"
    baseline = _v2(excluded_service, source, target, "zh")
    resolutions = ({"mention_id": baseline.findings[0].id, "choice": "not_a_term"},)
    report = _v2(
        excluded_service,
        source,
        target,
        "zh",
        resolutions=resolutions,
        resolution_context=_context(baseline, source),
    )
    finding = report.findings[0]
    assert finding.verdict == VERDICT_NOT_EVALUATED
    assert finding.target_span is None
    assert _finding_identity(finding) == _finding_identity(baseline.findings[0])

    plain = _v2(
        plain_service,
        source,
        target,
        "zh",
        resolutions=resolutions,
        resolution_context=_context(baseline, source),
    )
    assert plain.findings[0].verdict == VERDICT_NOT_EVALUATED


def test_excluded_official_pair_verifies_with_exact_target_span(excluded_service):
    # The genuine weapon case: automatic discovery is unassessed, the explicit
    # authoritative check still verifies with the exact official span.
    source, target = "Discord.", "异响空灵。"
    baseline = _v2(excluded_service, source, target, "zh")
    finding = baseline.findings[0]
    assert finding.verdict == VERDICT_NOT_EVALUATED
    assert finding.target_span is None
    assert [(c.zh, c.en, c.category) for c in finding.candidates] == [
        ("异响空灵", "Discord", "weapon")
    ]

    report = _v2(
        excluded_service,
        source,
        target,
        "zh",
        resolutions=(_official_pair(finding, finding.candidates[0]),),
        resolution_context=_context(baseline, source),
    )
    resolved = report.findings[0]
    assert resolved.id == finding.id
    assert resolved.verdict == VERDICT_VERIFIED
    assert (resolved.target_span.start, resolved.target_span.end, resolved.target_span.text) == (
        0,
        4,
        "异响空灵",
    )


def test_excluded_official_pair_conflicts_when_the_form_is_absent(excluded_service):
    source, target = "Tea is ready.", "Taste is ready."
    baseline = _v2(excluded_service, source, target, "en")
    finding = baseline.findings[0]
    assert finding.verdict == VERDICT_NOT_EVALUATED

    report = _v2(
        excluded_service,
        source,
        target,
        "en",
        resolutions=(_official_pair(finding, finding.candidates[0]),),
        resolution_context=_context(baseline, source),
    )
    resolved = report.findings[0]
    assert resolved.verdict == VERDICT_CONFLICT
    assert resolved.target_span is None


def test_excluded_official_pair_with_elsewhere_only_form_is_not_evaluated(excluded_service):
    source, target = "Fish swims. Birds sing.", "Birds sing. Fish swim."
    baseline = _v2(excluded_service, source, target, "en")
    finding = baseline.findings[0]
    report = _v2(
        excluded_service,
        source,
        target,
        "en",
        resolutions=(_official_pair(finding, finding.candidates[0]),),
        resolution_context=_context(baseline, source),
    )
    resolved = report.findings[0]
    assert resolved.verdict == VERDICT_NOT_EVALUATED
    assert resolved.target_span is None


def test_excluded_official_pair_without_mapping_is_not_evaluated(excluded_service):
    source, target = "Tea.", "Tea or coffee. Hmm."
    baseline = _v2(excluded_service, source, target, "en")
    finding = baseline.findings[0]
    assert baseline.dictionary.term_count == len(TERMS)
    report = _v2(
        excluded_service,
        source,
        target,
        "en",
        resolutions=(_official_pair(finding, finding.candidates[0]),),
        resolution_context=_context(baseline, source),
    )
    resolved = report.findings[0]
    assert resolved.verdict == VERDICT_NOT_EVALUATED
    assert resolved.target_span is None


def test_excluded_official_pair_with_stale_candidate_id_is_rejected(excluded_service):
    # A wrong candidate stays a request error even though the mention is
    # excluded; gating never neutralizes request validation.
    source, target = "Discord.", "异响空灵。"
    baseline = _v2(excluded_service, source, target, "zh")
    resolutions = (
        {
            "mention_id": baseline.findings[0].id,
            "choice": "official_pair",
            "candidate_id": "f" * 64,
        },
    )
    with pytest.raises(ReviewRequestError) as error:
        _v2(
            excluded_service,
            source,
            target,
            "zh",
            resolutions=resolutions,
            resolution_context=_context(baseline, source),
        )
    assert error.value.code == "invalid_request"
    assert "not current" in error.value.message


def test_repeated_explicit_selections_do_not_reuse_the_target_span(excluded_service):
    # Both "Tea" mentions are explicitly selected; the target holds one valid
    # occurrence, so exactly one verifies and the other conflicts without
    # reusing the span.
    source, target = "Tea and Tea.", "Tea is here."
    baseline = _v2(excluded_service, source, target, "en")
    candidate = baseline.findings[0].candidates[0]
    resolutions = tuple(
        _official_pair(finding, candidate) for finding in baseline.findings
    )
    report = _v2(
        excluded_service,
        source,
        target,
        "en",
        resolutions=resolutions,
        resolution_context=_context(baseline, source),
    )
    assert [f.verdict for f in report.findings] == [VERDICT_VERIFIED, VERDICT_CONFLICT]
    first, second = report.findings
    assert (first.target_span.start, first.target_span.end, first.target_span.text) == (
        0,
        3,
        "Tea",
    )
    assert second.target_span is None
    assert second.target_span is not first.target_span


def test_nonexcluded_controls_keep_the_existing_automatic_verdicts(excluded_service):
    # Genuine control pair still auto-verifies.
    report = _v2(excluded_service, "今汐登场。", "Jinhsi appears.", "en")
    assert [f.verdict for f in report.findings] == [VERDICT_VERIFIED]
    assert (report.findings[0].target_span.start, report.findings[0].target_span.text) == (
        0,
        "Jinhsi",
    )

    # Repeated control with a short target keeps verified + needs_review.
    report = _v2(excluded_service, "今汐与今汐。", "Jinhsi.", "en")
    assert [f.verdict for f in report.findings] == [VERDICT_VERIFIED, VERDICT_NEEDS_REVIEW]
    assert report.findings[1].target_span is None

    # A nonexcluded formless target keeps needs_review: no new false pass.
    report = _v2(excluded_service, "茶汤很好。", "Nothing relevant.", "en")
    assert [f.verdict for f in report.findings] == [VERDICT_NEEDS_REVIEW]

    # Both genuine controls verify within their mapped sentences.
    report = _v2(excluded_service, "今汐登场。声骸出现。", "Jinhsi appears. An Echo appears.", "en")
    assert [f.verdict for f in report.findings] == [VERDICT_VERIFIED, VERDICT_VERIFIED]
    assert [f.target_span.text for f in report.findings] == ["Jinhsi", "Echo"]


def test_invalid_english_key_does_not_disable_valid_zh_evidence(excluded_service, partial_service, caplog):
    with caplog.at_level(logging.WARNING, logger="wuwaterm.review"):
        # The valid zh key still gates its surface.
        report = _v2(partial_service, "大人说话。", "The Adult speaks.", "zh")
        assert [f.verdict for f in report.findings] == [VERDICT_NOT_EVALUATED]
        assert report.findings[0].target_span is None

        # The invalid English key contributes nothing: "It" keeps pre-F10
        # automatic behavior, so one bad key does not disable the other.
        report = _v2(partial_service, "It matters.", "祂很重要。", "zh")
        assert [f.verdict for f in report.findings] == [VERDICT_VERIFIED]
        assert report.findings[0].target_span.text == "祂"

    zh_warnings = [r for r in caplog.records if FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY in r.getMessage()]
    assert zh_warnings == []
    en_warnings = [
        r for r in caplog.records if FREE_TEXT_LOCK_EXCLUSIONS_KEY in r.getMessage()
    ]
    assert len(en_warnings) == 2  # one per review_pair call, never for the zh key

    # Sanity: the fully-excluded dictionary does gate both surfaces.
    assert _v2(excluded_service, "It matters.", "祂很重要。", "zh").findings[0].verdict == (
        VERDICT_NOT_EVALUATED
    )


def test_absent_and_invalid_keys_reproduce_pre_f10_behavior(excluded_service, plain_service, invalid_service, caplog):
    # Absent keys: the formless excluded surface stays needs_review.
    plain = _v2(plain_service, "Fish is food.", "鱼肉是食物。", "en")
    assert [f.verdict for f in plain.findings] == [VERDICT_NEEDS_REVIEW]

    # Invalid keys fail open the same way, with one warning per key.
    with caplog.at_level(logging.WARNING, logger="wuwaterm.review"):
        report = _v2(invalid_service, "Fish is food.", "鱼肉是食物。", "en")
    assert [f.verdict for f in report.findings] == [VERDICT_NEEDS_REVIEW]
    # The zh key name contains the en key name; count the records precisely.
    zh_records = [r for r in caplog.records if FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY in r.getMessage()]
    en_only_records = [
        r
        for r in caplog.records
        if FREE_TEXT_LOCK_EXCLUSIONS_KEY in r.getMessage()
        and FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY not in r.getMessage()
    ]
    assert len(zh_records) == 1
    assert len(en_only_records) == 1

    # The excluded dictionary gates the same surface, proving the contrast.
    assert _v2(excluded_service, "Fish is food.", "鱼肉是食物。", "en").findings[0].verdict == (
        VERDICT_NOT_EVALUATED
    )


def test_english_exclusion_does_not_gate_the_chinese_surface(excluded_service, plain_service):
    # Exclusion membership is exact-surface: the EN key excludes "Fish", not
    # 鱼肉, and a zh-side 鱼肉 mention keeps its automatic verification.
    source, target = "鱼肉很新鲜。", "Fresh Fish is tasty."
    report = _v2(excluded_service, source, target, "en")
    assert [f.verdict for f in report.findings] == [VERDICT_VERIFIED]
    assert (report.findings[0].target_span.start, report.findings[0].target_span.text) == (
        6,
        "Fish",
    )
    plain = _v2(plain_service, source, target, "en")
    assert [f.verdict for f in plain.findings] == [VERDICT_VERIFIED]
    assert _finding_identity(plain.findings[0]) == _finding_identity(report.findings[0])


def test_longer_surface_is_not_gated_by_its_excluded_substring(excluded_service):
    report = _v2(excluded_service, "Starfish glows.", "A Starfish glows.", "en")
    assert [f.id for f in report.findings] == ["0:8:Starfish"]
    assert [f.verdict for f in report.findings] == [VERDICT_VERIFIED]
    assert (report.findings[0].target_span.start, report.findings[0].target_span.text) == (
        2,
        "Starfish",
    )


@pytest.mark.parametrize(
    ("source", "target", "direction"),
    (
        ("Fish is food.", "鱼肉是食物。", "en"),
        ("Tea and 茶汤。", "Tea is ready.", "en"),
        ("大人说话。", "The Adult speaks.", "zh"),
    ),
)
def test_identity_is_stable_when_only_exclusion_metadata_changes(
    excluded_service, plain_service, source, target, direction
):
    gated = _v2(excluded_service, source, target, direction)
    plain = _v2(plain_service, source, target, direction)
    assert gated.rule_version == plain.rule_version == RULE_VERSION_V2
    assert gated.matcher_revision == plain.matcher_revision
    assert gated.dictionary.revision == plain.dictionary.revision
    assert gated.dictionary.term_count == plain.dictionary.term_count == len(TERMS)
    assert gated.source_revision == plain.source_revision
    assert gated.target_revision == plain.target_revision

    assert len(gated.findings) == len(plain.findings)
    for gated_finding, plain_finding in zip(gated.findings, plain.findings, strict=True):
        assert _finding_identity(gated_finding) == _finding_identity(plain_finding)
    # Only the verdict and target span may differ; every scenario here does.
    assert any(
        g.verdict != p.verdict or g.target_span != p.target_span
        for g, p in zip(gated.findings, plain.findings, strict=True)
    )


def test_gated_finding_reserves_no_target_span(excluded_service, plain_service):
    # The excluded "Tea" mention comes first and the nonexcluded 茶汤 record
    # shares its official target form, so the single "Tea" span in the mapped
    # region is exactly the evidence the gated mention must not claim.
    source, target = "Tea and 茶汤。", "Tea is ready."
    report = _v2(excluded_service, source, target, "en")
    gated_tea, soup = report.findings
    assert (gated_tea.id, soup.id) == ("0:3:Tea", "8:10:茶汤")
    assert gated_tea.verdict == VERDICT_NOT_EVALUATED
    assert gated_tea.target_span is None
    assert soup.verdict == VERDICT_VERIFIED
    assert (soup.target_span.start, soup.target_span.end, soup.target_span.text) == (
        0,
        3,
        "Tea",
    )

    # Without the metadata the excluded mention claims the span first and the
    # shared-form control is left unassessed — the pre-F10 allocation.
    plain = _v2(plain_service, source, target, "en")
    assert plain.findings[0].verdict == VERDICT_VERIFIED
    assert (plain.findings[0].target_span.start, plain.findings[0].target_span.text) == (
        0,
        "Tea",
    )
    assert plain.findings[1].verdict == VERDICT_NEEDS_REVIEW
    assert plain.findings[1].target_span is None


def test_v1_findings_are_unchanged_by_exclusion_metadata(excluded_service, plain_service):
    cases = (
        ("Fish is food.", "鱼肉是食物。", "en"),
        ("今汐登场。", "Jinhsi appears.", "en"),
        ("It matters.", "祂很重要。", "zh"),
    )
    for source, target, direction in cases:
        kwargs = {"source": source, "target": target, "direction": direction}
        with_exclusions = review_pair(excluded_service, review_version=RULE_VERSION, **kwargs)
        without = review_pair(plain_service, review_version=RULE_VERSION, **kwargs)
        assert with_exclusions.rule_version == RULE_VERSION
        assert with_exclusions.findings == without.findings
        assert with_exclusions.coverage == without.coverage


@pytest.mark.parametrize(
    ("source", "target", "direction"),
    (
        ("Fish is food.", "鱼肉是食物。", "en"),
        ("Tea and 茶汤。", "Tea is ready.", "en"),
        ("Fish swims. Birds sing.", "Birds sing. Fish swim.", "en"),
        ("今汐登场。", "Jinhsi appears.", "en"),
    ),
)
def test_coverage_formula_counts_gated_findings(excluded_service, source, target, direction):
    report = _v2(excluded_service, source, target, direction)
    findings_not_evaluated = sum(
        1 for finding in report.findings if finding.verdict == VERDICT_NOT_EVALUATED
    )
    evaluated = sum(
        1
        for finding in report.findings
        if finding.verdict in {VERDICT_VERIFIED, VERDICT_CONFLICT, VERDICT_NEEDS_REVIEW}
    )
    assert report.coverage.evaluated == evaluated
    assert report.coverage.not_evaluated == 1 + findings_not_evaluated
    assert report.coverage.rules == (RULE_TERM_PAIR, RULE_SENTENCE_MEANING)


def test_exclusion_metadata_is_read_through_the_bounded_snapshot(excluded_service, monkeypatch):
    # Exclusions must come from the existing snapshot metadata; a separate
    # metadata/entries/count read fails the test, as in test_review_v2.py.
    monkeypatch.setattr(
        excluded_service,
        "metadata",
        lambda: pytest.fail("v2 must not make a separate metadata read"),
    )
    monkeypatch.setattr(
        excluded_service,
        "term_count",
        lambda: pytest.fail("v2 must not make a separate count read"),
    )
    monkeypatch.setattr(
        excluded_service,
        "entries",
        lambda: pytest.fail("v2 must not make a separate entries read"),
    )
    report = _v2(excluded_service, "Fish is food.", "鱼肉是食物。", "en")
    assert report.findings[0].verdict == VERDICT_NOT_EVALUATED
    assert report.dictionary.term_count == len(TERMS)
