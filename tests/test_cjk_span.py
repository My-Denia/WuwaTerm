"""Both-edge CJK veto: lexicon pin, window, and lock/review agreement."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from wuwaterm.application import review_pair
from wuwaterm.cjk_span import (
    JIEBA_COMMIT,
    LEXICON_COUNT,
    LEXICON_FILTER,
    MATCHER_REVISION,
    MAX_WORD_LEN,
    _WORDS,
    crosses_cjk_word_boundary,
)
from wuwaterm.lookup import TermService
from wuwaterm.review import RULE_VERSION_V2
from wuwaterm.sentence import SentenceTranslator

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_WORDS = (
    "随遇而安",
    "可以",
    "平安",
    "可贵",
    "回声",
    "骸骨",
    "装备",
    "加入",
    "队伍",
    "使用",
    "中国",
    "和声",
)

# term, expected veto. None means the span stays: one-sided overlap is unresolved.
CASES = (
    ("随遇而安可以，但别放弃。", "安可", True),
    ("祝你平安可以吗？", "安可", True),
    ("平安可贵。", "安可", True),
    ("回声骸骨", "声骸", True),
    ("一声骸", "声骸", False),
    ("安可加入队伍。", "安可", False),
    ("今汐装备了声骸。", "今汐", False),
    ("今汐装备了声骸。", "声骸", False),
    ("给安可装备声骸。", "安可", False),
    ("给安可装备声骸。", "声骸", False),
    ("使用声骸。", "声骸", False),
    ("今汐的声骸很强。", "今汐", False),
    ("今汐的声骸很强。", "声骸", False),
    ("和安可一起。", "安可", False),
    ("角色提到香椿和声骸。", "声骸", False),
    ("没有声骸。", "声骸", False),
    ("她有声骸。", "声骸", False),
    ("强大声骸。", "声骸", False),
)


def test_lexicon_is_the_pinned_jieba_slice():
    assert MAX_WORD_LEN == 8
    assert len(_WORDS) == LEXICON_COUNT == 66315
    assert JIEBA_COMMIT == "67fa2e36e72f69d9134b8a1037b83fbb070b9775"
    assert LEXICON_FILTER == "U+3400-U+9FFF; length 2-8; frequency >= 24"
    assert "安可" not in _WORDS
    for word in REQUIRED_WORDS:
        assert word in _WORDS
    payload = json.dumps(
        ["wuwaterm-cjk-matcher-v2", JIEBA_COMMIT, LEXICON_FILTER, sorted(_WORDS)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    assert MATCHER_REVISION == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(("text", "term", "veto"), CASES)
def test_both_edges_are_required(text, term, veto):
    start = text.find(term)
    assert start >= 0
    assert crosses_cjk_word_boundary(text, start, start + len(term)) is veto


def test_words_longer_than_the_window_do_not_veto(monkeypatch):
    long_text = "甲乙丙丁戊己庚辛壬癸"
    assert len(long_text) > MAX_WORD_LEN
    monkeypatch.setattr("wuwaterm.cjk_span._WORDS", frozenset({long_text}))
    assert crosses_cjk_word_boundary(long_text, 4, 6) is False

    covered = "甲乙丙丁戊己庚辛"
    assert len(covered) == MAX_WORD_LEN
    monkeypatch.setattr("wuwaterm.cjk_span._WORDS", frozenset({covered}))
    assert crosses_cjk_word_boundary(covered, 3, 5) is True


def test_review_does_not_import_sentence():
    source = (ROOT / "src" / "wuwaterm" / "review.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(item.name for item in node.names)
    assert "wuwaterm.sentence" not in imported
    assert "sentence" not in imported
    assert "cjk_span" in imported


def _locked_zh(translator: SentenceTranslator, text: str) -> set[str]:
    return {zh for _placeholder, zh, _en in translator.lock_terms(text).locks}


def _surfaces(service: TermService, text: str, version: str) -> set[str]:
    report = review_pair(service, text, "placeholder", "en", review_version=version)
    return {item.source_span.text for item in report.findings}


def test_locking_and_review_v2_agree_on_the_registered_sentences(sample_db):
    translator = SentenceTranslator(sample_db)
    service = TermService(sample_db)
    expected: dict[str, set[str]] = {}
    for text, term, veto in CASES:
        wanted = expected.setdefault(text, set())
        if not veto:
            wanted.add(term)
    for text, wanted in expected.items():
        locked = _locked_zh(translator, text)
        reviewed = _surfaces(service, text, RULE_VERSION_V2)
        assert locked == reviewed == wanted
    assert _locked_zh(translator, "今天天气不错。") == set()
    assert _surfaces(service, "今天天气不错。", "review-v1") == set()


def test_review_v1_still_reports_the_cross_word_spans(sample_db):
    service = TermService(sample_db)
    for text, term, veto in CASES:
        if not veto:
            continue
        assert term in _surfaces(service, text, "review-v1")


def test_derived_lexicon_retains_full_upstream_mit_notice():
    notice = (ROOT / "src/wuwaterm/cjk_span.py").read_text()
    assert "Copyright (c) 2013 Sun Junyi" in notice
    assert "Permission is hereby granted, free of charge" in notice
    assert "copies or substantial portions of the Software" in notice
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in notice
