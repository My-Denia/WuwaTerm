"""Application-layer pair review: coordinates, coverage, and no LLM."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from wuwaterm.application import (
    ReviewRequestError,
    review_pair,
)
from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.lookup import TermService
from wuwaterm.models import TermRecord
from wuwaterm.review import (
    CHOICE_NOT_A_TERM,
    CHOICE_OFFICIAL_PAIR,
    RULE_SENTENCE_MEANING,
    RULE_TERM_PAIR,
    VERDICT_CONFLICT,
    VERDICT_NEEDS_REVIEW,
    VERDICT_NOT_EVALUATED,
    VERDICT_VERIFIED,
    _next_unused,
    _sentence_ranges,
    mention_id,
    text_revision,
)


ROOT = Path(__file__).resolve().parents[1]


def _service(sample_db) -> TermService:
    return TermService(sample_db)


def _finding(report, text: str):
    matches = [item for item in report.findings if item.source_span.text == text]
    assert matches, f"no finding for {text!r}: {[item.source_span.text for item in report.findings]}"
    return matches[0]


def test_review_module_source_does_not_import_sentence():
    source = (ROOT / "src" / "wuwaterm" / "review.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "sentence" or module.endswith(".sentence"):
                imported.append(module)
            for imported_name in node.names:
                if imported_name.name == "sentence":
                    imported.append(imported_name.name)
        elif isinstance(node, ast.Import):
            for imported_name in node.names:
                if imported_name.name == "sentence" or imported_name.name.endswith(".sentence"):
                    imported.append(imported_name.name)
    assert imported == []
    assert "from .sentence" not in source
    assert "wuwaterm.sentence" not in source
    assert "SentenceTranslator" not in source


def test_domain_core_sentence_import_machine_check_fails(tmp_path, monkeypatch):
    from scripts import check_architecture_boundaries as cab

    review = tmp_path / "review.py"
    review.write_text("from .sentence import SentenceTranslator\n", encoding="utf-8")
    sentence = tmp_path / "sentence.py"
    sentence.write_text("", encoding="utf-8")
    lookup = tmp_path / "lookup.py"
    lookup.write_text("", encoding="utf-8")

    def fake_discover():
        return {"review": review, "sentence": sentence, "lookup": lookup}, []

    monkeypatch.setattr(cab, "_discover_modules", fake_discover)
    failures = cab.check()
    assert any("domain core must not import domain+LLM" in item for item in failures)


def test_review_classified_as_domain_core():
    from scripts import check_architecture_boundaries as cab

    assert "review" in cab.DOMAIN_CORE
    assert "review" not in cab.DOMAIN_LLM


def test_dictionary_fields_come_from_the_same_term_service_read(sample_db):
    inner = _service(sample_db)
    calls: list[str] = []

    class Probe:
        def metadata(self):
            calls.append("metadata")
            data = dict(inner.metadata())
            data["schema_version"] = "snap-schema"
            data["source_commit"] = "snap-commit"
            return data

        def term_count(self):
            calls.append("term_count")
            return 4242

        def entries(self):
            calls.append("entries")
            return inner.entries()

    report = review_pair(Probe(), "今汐拿到了声骸。", "Jinhsi got an Echo.", "en")
    assert report.dictionary.schema_version == "snap-schema"
    assert report.dictionary.source_commit == "snap-commit"
    assert report.dictionary.term_count == 4242
    assert calls[0] == "metadata"
    assert calls[1] == "term_count"
    assert "entries" in calls


def test_revisions_are_utf8_sha256_of_submitted_strings(sample_db):
    source = "今汐"
    target = "Jinhsi"
    report = review_pair(_service(sample_db), source, target, "en")
    assert report.source_revision == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert report.target_revision == hashlib.sha256(target.encode("utf-8")).hexdigest()
    assert report.source_revision == text_revision(source)
    assert len(report.source_revision) == 64
    assert report.rule_version == "review-v1"


def test_empty_source_is_invalid_request(sample_db):
    with pytest.raises(ReviewRequestError) as caught:
        review_pair(_service(sample_db), "", "Jinhsi", "en")
    assert caught.value.code == "invalid_request"


def test_empty_target_is_invalid_request(sample_db):
    with pytest.raises(ReviewRequestError) as caught:
        review_pair(_service(sample_db), "今汐", "", "en")
    assert caught.value.code == "invalid_request"


def test_isolated_surrogate_is_invalid_request(sample_db):
    with pytest.raises(ReviewRequestError) as caught:
        review_pair(_service(sample_db), "今汐\ud800", "Jinhsi", "en")
    assert caught.value.code == "invalid_request"


def test_overlong_side_is_input_too_long(sample_db):
    with pytest.raises(ReviewRequestError) as caught:
        review_pair(_service(sample_db), "今" * 2001, "Jinhsi", "en")
    assert caught.value.code == "input_too_long"


def test_bad_direction_is_invalid_request(sample_db):
    with pytest.raises(ReviewRequestError) as caught:
        review_pair(_service(sample_db), "今汐", "Jinhsi", "jp")
    assert caught.value.code == "invalid_request"


def test_spans_use_unicode_scalar_offsets_and_include_text(sample_db):
    source = "今汐😀声骸"
    target = "Jinhsi 😀 Echo"
    report = review_pair(_service(sample_db), source, target, "en")
    jinhsi = _finding(report, "今汐")
    echo = _finding(report, "声骸")
    assert jinhsi.source_span.start == 0
    assert jinhsi.source_span.end == 2
    assert jinhsi.source_span.text == "今汐"
    assert echo.source_span.start == 3
    assert echo.source_span.end == 5
    assert echo.source_span.text == source[echo.source_span.start : echo.source_span.end]
    assert echo.id == mention_id(3, 5, "声骸")
    assert echo.target_span is not None
    assert echo.target_span.text == "Echo"
    assert target[echo.target_span.start : echo.target_span.end] == "Echo"


def test_supplementary_plane_scalar_does_not_shift_later_spans(sample_db):
    source = "今汐\U0001f600声骸"
    report = review_pair(_service(sample_db), source, "Jinhsi \U0001f600 Echo", "en")
    echo = _finding(report, "声骸")
    assert echo.source_span.start == 3
    assert len(source) == 5


def test_mention_id_stable_when_only_target_changes(sample_db):
    service = _service(sample_db)
    first = review_pair(service, "今汐拿到了声骸。", "Jinhsi got an Echo.", "en")
    second = review_pair(service, "今汐拿到了声骸。", "Jinhsi got a thing.", "en")
    assert {item.id for item in first.findings} == {item.id for item in second.findings}


def test_no_confirmed_conflict_without_resolutions(sample_db):
    report = review_pair(
        _service(sample_db),
        "今汐拿到了声骸。",
        "Jinhsi got a phantom.",
        "en",
    )
    assert all(item.verdict != VERDICT_CONFLICT for item in report.findings)


def test_coverage_never_treats_unevaluated_sentence_meaning_as_pass(sample_db):
    report = review_pair(_service(sample_db), "今汐拿到了声骸。", "Jinhsi got an Echo.", "en")
    assert RULE_SENTENCE_MEANING in report.coverage.rules
    assert RULE_TERM_PAIR in report.coverage.rules
    assert report.coverage.not_evaluated > 0
    assert all(item.rule_id != RULE_SENTENCE_MEANING for item in report.findings)


def test_synthetic_aligned_official_pair_is_constraint_not_meaning(sample_db):
    report = review_pair(_service(sample_db), "今汐拿到了声骸。", "Jinhsi got an Echo.", "en")
    assert _finding(report, "今汐").verdict == VERDICT_VERIFIED
    assert _finding(report, "声骸").verdict == VERDICT_VERIFIED
    assert _finding(report, "今汐").target_span is not None
    assert _finding(report, "声骸").target_span is not None
    assert report.coverage.not_evaluated > 0


def test_synthetic_repeated_mentions_need_own_aligned_spans(sample_db):
    report = review_pair(
        _service(sample_db),
        "声骸与声骸。",
        "Echo and Echo.",
        "en",
    )
    echoes = [item for item in report.findings if item.source_span.text == "声骸"]
    assert len(echoes) == 2
    assert echoes[0].source_span.start != echoes[1].source_span.start
    assert echoes[0].id != echoes[1].id
    assert all(item.verdict == VERDICT_VERIFIED for item in echoes)
    assert echoes[0].target_span is not None and echoes[1].target_span is not None
    assert echoes[0].target_span.start != echoes[1].target_span.start
    assert report.coverage.not_evaluated > 0


def test_synthetic_passive_reorder_still_aligns_in_same_sentence(sample_db):
    report = review_pair(
        _service(sample_db),
        "今汐击败了先锋幼岩。",
        "Vanguard Junrock was defeated by Jinhsi.",
        "en",
    )
    assert _finding(report, "今汐").verdict == VERDICT_VERIFIED
    assert _finding(report, "先锋幼岩").verdict == VERDICT_VERIFIED
    assert report.coverage.not_evaluated > 0


def test_synthetic_placeholder_exact_once_does_not_verify_sentence_meaning(sample_db):
    report = review_pair(
        _service(sample_db),
        "今汐不是共鸣者。",
        "Jinhsi is not a Resonator.",
        "en",
    )
    resonator = _finding(report, "共鸣者")
    assert resonator.verdict == VERDICT_VERIFIED
    assert resonator.target_span is not None
    assert resonator.target_span.text == "Resonator"
    assert report.coverage.not_evaluated > 0
    assert RULE_SENTENCE_MEANING in report.coverage.rules


def test_synthetic_missing_official_form_is_needs_review_not_conflict(sample_db):
    report = review_pair(
        _service(sample_db),
        "今汐拿到了声骸。",
        "Jinhsi got something.",
        "en",
    )
    echo = _finding(report, "声骸")
    assert echo.verdict == VERDICT_NEEDS_REVIEW
    assert echo.target_span is None
    assert all(item.verdict != VERDICT_CONFLICT for item in report.findings)
    assert report.coverage.not_evaluated > 0


def test_consecutive_sentence_terminators_do_not_shift_alignment(sample_db):
    assert _sentence_ranges("今汐？！声骸。") == [(0, 4), (4, 7)]
    report = review_pair(
        _service(sample_db),
        "今汐？！声骸。",
        "Jinhsi! Echo.",
        "en",
    )
    assert _finding(report, "今汐").verdict == VERDICT_VERIFIED
    assert _finding(report, "声骸").verdict == VERDICT_VERIFIED


def test_leading_sentence_terminators_do_not_shift_alignment(sample_db):
    assert _sentence_ranges("\n今汐。") == [(1, 4)]
    assert _sentence_ranges("Jinhsi.") == [(0, 7)]
    report = review_pair(
        _service(sample_db),
        "\n今汐。",
        "Jinhsi.",
        "en",
    )
    assert _finding(report, "今汐").verdict == VERDICT_VERIFIED


def test_whitespace_between_sentences_does_not_shift_alignment(sample_db):
    assert _sentence_ranges("前言。\r\n今汐。") == [(0, 3), (5, 8)]
    assert _sentence_ranges("Preface.\nJinhsi.") == [(0, 9), (9, 16)]
    report = review_pair(
        _service(sample_db),
        "前言。\r\n今汐。",
        "Preface.\nJinhsi.",
        "en",
    )
    assert _finding(report, "今汐").verdict == VERDICT_VERIFIED


def test_separator_only_text_has_no_sentence_range():
    assert _sentence_ranges("...") == []
    assert _sentence_ranges("？！") == []
    assert _sentence_ranges("hello") == [(0, 5)]


def test_overlapping_target_spans_are_not_reused():
    used = {(0, 14)}
    assert _next_unused([(0, 5)], used) is None
    assert _next_unused([(5, 14)], used) is None
    assert _next_unused([(14, 20)], used) == (14, 20)


def test_official_form_only_elsewhere_is_not_verified(sample_db):
    report = review_pair(
        _service(sample_db),
        "今汐拿到了声骸。后面没有了。",
        "Jinhsi got something. Later an Echo appeared.",
        "en",
    )
    echo = _finding(report, "声骸")
    assert echo.verdict == VERDICT_NOT_EVALUATED
    assert echo.target_span is None
    assert echo.verdict != VERDICT_VERIFIED


def test_official_pair_elsewhere_stays_not_evaluated(sample_db):
    service = _service(sample_db)
    source = "前言。声骸。"
    target = "Preface. More. Echo."
    baseline = review_pair(service, source, target, "en")
    echo = _finding(baseline, "声骸")
    assert echo.verdict == VERDICT_NOT_EVALUATED
    report = review_pair(
        service,
        source,
        target,
        "en",
        resolutions=(
            {
                "mention_id": echo.id,
                "choice": CHOICE_OFFICIAL_PAIR,
                "zh": "声骸",
                "en": "Echo",
            },
        ),
    )
    resolved = _finding(report, "声骸")
    assert resolved.verdict == VERDICT_NOT_EVALUATED
    assert resolved.target_span is None
    assert resolved.verdict != VERDICT_CONFLICT


def test_official_pair_resolution_conflict_when_aligned_region_fails(sample_db):
    service = _service(sample_db)
    source = "今汐拿到了声骸。"
    target = "Jinhsi got something."
    baseline = review_pair(service, source, target, "en")
    echo_id = _finding(baseline, "声骸").id
    report = review_pair(
        service,
        source,
        target,
        "en",
        resolutions=(
            {
                "mention_id": echo_id,
                "choice": CHOICE_OFFICIAL_PAIR,
                "zh": "声骸",
                "en": "Echo",
            },
        ),
    )
    echo = _finding(report, "声骸")
    assert echo.verdict == VERDICT_CONFLICT
    assert echo.target_span is None


def test_official_pair_must_be_a_candidate(sample_db):
    service = _service(sample_db)
    baseline = review_pair(service, "今汐拿到了声骸。", "Jinhsi got an Echo.", "en")
    echo_id = _finding(baseline, "声骸").id
    with pytest.raises(ReviewRequestError) as caught:
        review_pair(
            service,
            "今汐拿到了声骸。",
            "Jinhsi got an Echo.",
            "en",
            resolutions=(
                {
                    "mention_id": echo_id,
                    "choice": CHOICE_OFFICIAL_PAIR,
                    "zh": "声骸",
                    "en": "NotARealGloss",
                },
            ),
        )
    assert caught.value.code == "invalid_request"


def test_unknown_mention_id_is_invalid_request(sample_db):
    with pytest.raises(ReviewRequestError) as caught:
        review_pair(
            _service(sample_db),
            "今汐",
            "Jinhsi",
            "en",
            resolutions=(
                {"mention_id": "0:2:nope", "choice": CHOICE_NOT_A_TERM},
            ),
        )
    assert caught.value.code == "invalid_request"


def test_not_a_term_is_not_needs_review_or_conflict(sample_db):
    service = _service(sample_db)
    baseline = review_pair(service, "今汐拿到了声骸。", "Jinhsi got something.", "en")
    echo_id = _finding(baseline, "声骸").id
    report = review_pair(
        service,
        "今汐拿到了声骸。",
        "Jinhsi got something.",
        "en",
        resolutions=({"mention_id": echo_id, "choice": CHOICE_NOT_A_TERM},),
    )
    echo = _finding(report, "声骸")
    assert echo.verdict == VERDICT_NOT_EVALUATED
    assert echo.verdict not in {VERDICT_NEEDS_REVIEW, VERDICT_CONFLICT}


def test_unaligned_official_pair_cannot_confirm_conflict(sample_db):
    service = _service(sample_db)
    source = "前言在此。今汐拿到了声骸。"
    target = "Only one target sentence."
    baseline = review_pair(service, source, target, "en")
    echo_id = _finding(baseline, "声骸").id
    report = review_pair(
        service,
        source,
        target,
        "en",
        resolutions=(
            {
                "mention_id": echo_id,
                "choice": CHOICE_OFFICIAL_PAIR,
                "zh": "声骸",
                "en": "Echo",
            },
        ),
    )
    echo = _finding(report, "声骸")
    assert echo.verdict == VERDICT_NOT_EVALUATED
    assert echo.target_span is None
    assert echo.verdict != VERDICT_CONFLICT


def test_adjacent_long_term_wins_over_short(sample_db):
    report = review_pair(_service(sample_db), "先锋幼岩来了。", "Vanguard Junrock arrived.", "en")
    texts = [item.source_span.text for item in report.findings]
    assert "先锋幼岩" in texts
    assert "幼岩" not in texts


def test_ascii_boundaries_do_not_lock_glued_echo(sample_db):
    report = review_pair(_service(sample_db), "New Echoes drop", "新的掉落", "zh")
    assert all(item.source_span.text != "Echo" for item in report.findings)


def test_same_surface_keeps_all_sources(sample_db, tmp_path):
    from wuwaterm.db import connect, insert_records
    from wuwaterm.models import TermRecord

    with connect(sample_db) as conn:
        insert_records(
            conn,
            [
                TermRecord(
                    category="echo",
                    source_file="ExtraA.json",
                    source_id="extra-a",
                    text_key="Term850082_Title",
                    zh="声骸",
                    en="Echo",
                ),
                TermRecord(
                    category="echo",
                    source_file="ExtraB.json",
                    source_id="extra-b",
                    text_key="Term850082_Title",
                    zh="声骸",
                    en="Echo",
                ),
            ],
        )
        conn.commit()
    report = review_pair(_service(sample_db), "声骸", "Echo", "en")
    echo = _finding(report, "声骸")
    files = {
        source.source_file
        for candidate in echo.candidates
        if candidate.zh == "声骸" and candidate.en == "Echo"
        for source in candidate.sources
    }
    assert "ExtraA.json" in files
    assert "ExtraB.json" in files


def test_review_pair_does_not_call_translate_request(monkeypatch, sample_db):
    import wuwaterm.application as application

    def boom(*_args, **_kwargs):
        raise AssertionError("review must not call translate_request")

    monkeypatch.setattr(application, "translate_request", boom)
    monkeypatch.setattr(application, "translate_request_async", boom)
    report = review_pair(_service(sample_db), "今汐", "Jinhsi", "en")
    assert report.findings


def test_zero_coverage_pair_still_counts_unevaluated_sentence_meaning(sample_db):
    report = review_pair(_service(sample_db), "没有术语的句子。", "A sentence without terms.", "en")
    assert report.findings == ()
    assert report.coverage.evaluated == 0
    assert report.coverage.not_evaluated > 0


def test_proper_name_and_common_term_are_separate_mentions(sample_db):
    report = review_pair(
        _service(sample_db),
        "今汐是共鸣者。",
        "Jinhsi is a Resonator.",
        "en",
    )
    assert _finding(report, "今汐").verdict == VERDICT_VERIFIED
    assert _finding(report, "共鸣者").verdict == VERDICT_VERIFIED


def test_true_ambiguity_in_same_sentence_needs_review(sample_db):
    report = review_pair(
        _service(sample_db),
        "巧手烹调很重要。",
        "Life Skill and Skillful Cooking both appear.",
        "en",
    )
    cooking = _finding(report, "巧手烹调")
    assert cooking.verdict == VERDICT_NEEDS_REVIEW
    assert len({(c.zh, c.en, c.category) for c in cooking.candidates}) >= 2


def test_punctuation_does_not_shift_scalar_span_text(sample_db):
    report = review_pair(_service(sample_db), "「今汐」。", '"Jinhsi".', "en")
    jinhsi = _finding(report, "今汐")
    assert jinhsi.source_span.text == "今汐"
    assert jinhsi.source_span.start == 1


def test_numeric_unit_same_inventory_different_relation_is_not_meaning_proof(sample_db):
    report = review_pair(
        _service(sample_db),
        "联觉经验增加了。",
        "Union EXP decreased.",
        "en",
    )
    exp = _finding(report, "联觉经验")
    assert exp.verdict == VERDICT_VERIFIED
    assert report.coverage.not_evaluated > 0


def test_development_and_validation_templates_are_not_renames(sample_db):
    first = review_pair(_service(sample_db), "守岸人看见声骸。", "Shorekeeper saw an Echo.", "en")
    second = review_pair(
        _service(sample_db),
        "卡卡罗在云陵谷。",
        "Calcharo is in Gorges of Spirits.",
        "en",
    )
    assert {item.source_span.text for item in first.findings} != {
        item.source_span.text for item in second.findings
    }
    assert _finding(first, "守岸人").verdict == VERDICT_VERIFIED
    assert _finding(second, "云陵谷").verdict == VERDICT_VERIFIED


def _one_character_service(tmp_path: Path) -> TermService:
    path = tmp_path / "one-character-v1.db"
    with connect(path) as conn:
        initialize(conn)
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (("schema_version", "synthetic-v1"), ("source_commit", "fixture-commit")),
        )
        insert_records(conn, _ONE_CHARACTER_RECORDS)
        conn.commit()
    return TermService(path)


_ONE_CHARACTER_RECORDS = (
    TermRecord("speaker", "fixture", "wo", "wo", "我", "Rover"),
    TermRecord("core_term", "fixture", "rover", "rover", "漂泊者", "Rover"),
    TermRecord("resonator", "fixture", "xin", "xin", "心", "Hsin"),
    TermRecord("speaker", "fixture", "xin-speaker", "xin-speaker", "心", "Hsin"),
    TermRecord("resonator", "fixture", "chun", "chun", "椿", "Camellya"),
    TermRecord("echo", "fixture", "jiao", "jiao", "角", "Jué"),
    TermRecord("item", "fixture", "yan", "yan", "盐", "Salt"),
    TermRecord("item", "fixture", "mi", "mi", "米", "Rice"),
    TermRecord("resonator", "fixture", "jinhsi", "jinhsi", "今汐", "Jinhsi"),
    TermRecord("core_term", "fixture", "echo", "echo", "声骸", "Echo"),
    TermRecord("resonator", "fixture", "jia", "jia", "甲", "A"),
)


def test_review_v1_still_reports_one_character_dictionary_collisions(tmp_path):
    # v1-collision: the frozen collector still reports every one-character
    # surface that review-v2 now drops, including both surfaces in one sentence.
    service = _one_character_service(tmp_path)
    expected = {
        "盐和米不是一句术语。": {"盐", "米"},
        "我是漂泊者。": {"我", "漂泊者"},
        "心里想着今汐。": {"心", "今汐"},
        "角色提到香椿和声骸。": {"角", "椿", "声骸"},
        "香椿": {"椿"},
        "椿树": {"椿"},
        "欢迎椿": {"椿"},
        "A joins the party.": {"A"},
    }
    for source, surfaces in expected.items():
        report = review_pair(service, source, "placeholder", "en")
        assert report.rule_version == "review-v1"
        assert {item.source_span.text for item in report.findings} == surfaces
