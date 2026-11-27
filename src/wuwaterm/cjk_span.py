"""Veto a multi-character CJK term when ordinary words cross both edges.

The lexicon is a derived slice of the jieba dictionary, not a copy of the
whole file and not part of the terms database.

Upstream: https://github.com/fxsjy/jieba
Commit: 67fa2e36e72f69d9134b8a1037b83fbb070b9775
File: jieba/dict.txt
This module does not import jieba. The upstream license notice follows:

The MIT License (MIT)

Copyright (c) 2013 Sun Junyi

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

Filter, applied to that commit's dict.txt: keep a word only when every
character is in U+3400–U+9FFF, its length is 2 through 8, and its frequency
is at least 24. The slice has 66315 words. MATCHER_REVISION is the SHA-256
of the canonical filter, commit, and sorted word list.

A term is removed only when one listed word crosses its start and one crosses
its end. Touching just one side is not enough, and the term's own exact span
is not a veto.

wuwaterm-cjk-matcher-v2 keeps the v1 slice and also gates review-v2 target
occurrences; review-v2 source mentions already did.

wuwaterm-cjk-matcher-v3 adds target-side compound qualification for actual
single-character resonator candidates. The pinned jieba slice and existing
crosses_cjk_word_boundary policy stay unchanged; source mention and sentence
locking admission retain their prior conservative policy.

wuwaterm-cjk-matcher-v4 adds bounded English plural admission to review-v2: an
en->zh source mention may appear as its orthodox plural, and a zh->en target
may answer with one. The lexicon slice, the CJK boundary policy and every
other MATCHER_REVISION input are unchanged; stored resolution contexts
carrying an older matcher hash become stale by the existing freshness rule.

wuwaterm-cjk-matcher-v5 aligns review-v2 source admission for single-character
resonator names with the reference set the translator is given: a character
counts as a mention unless an ordinary word covers it, instead of also
requiring the narrow sentence lock whitelist. 欢迎椿 and 快找椿 are referenced
in translation and were dropped from review, so a mistranslation there produced
no finding; 香椿, 臭椿, 椿树, 椿芽, 春椿, 椿木 and 椿皮 stay ordinary words on
both sides.

That widening holds only where the character is not an everyday standalone
noun. The 3.7 dictionary ships two single-character resonators, 椿 (Camellya)
and 心 (Hsin), and 心 is also the ordinary word for heart, mind or core. It
keeps the narrow name-context rule on both paths, so 心很重要 and 她的心很痛 are
neither referenced to the model nor reported by review. The everyday-character
set joins the compound supplement in the MATCHER_REVISION payload; the lexicon
slice is unchanged, and stored resolution contexts carrying v4 go stale by the
existing freshness rule.

wuwaterm-cjk-matcher-v6 lets a zh->en target answer with the singular of a
plural dictionary entry (海蚀 -> "Waveworn Phenomenon", 潮汐任务 -> "Main Quest")
in every category except resonator and speaker names, which keep the plural only
so the speaker Mars never answers to the month Mar. number_variants() strips
only a suffix that cannot belong to the stem, so Prizes becomes Prize and Toes
becomes Toe instead of Priz and To. Source admission is unchanged at the
sentence lock rule (one orthodox trailing s/es): review reads exactly the
spellings sentence.py references, and a reversed plural there would invent
mentions the translator never saw (Mar for Mars, Message for Messages). The
lexicon slice, the CJK boundary policy and the everyday-character set are
unchanged; resolution contexts carrying v5 go stale by the existing freshness
rule.

wuwaterm-cjk-matcher-v7 changes no matcher input. review-v2 now judges terms
through paragraph pairing when the source and target sentence counts differ
(a translation that writes the list marker "I." where the source has "一、"
no longer leaves every term unassessed), and a target occurrence inside an
occurrence of a longer form some mention still asserts, or of a longer form
among the mention's own candidates, no longer counts for the shorter one; a
dismissed term, an unresolved ordinary word and another mention's unselected
candidates reserve nothing. The same input gave a different report under v6,
so the basis must change. Stored resolution contexts and saved manuscript-v3
segment results carrying v6 go stale by the existing freshness rule and are
re-reviewed.

wuwaterm-cjk-matcher-v8 changes no matcher input. Every matcher function, the
lexicon slice and the single-character name rules are the v7 ones; what
changed is review-v2's attribution, so the basis label a stored resolution
context carries has to change with it. review-v2 now gives each mapped region
its own claim and allocation step, so a shortage credits no mention instead of
the first one a sweep reaches, and an inflection never answers a competing
official form; a numbering dot ("1.", "IV.") no longer ends a sentence; an
occurrence enclosed by a longer dictionary form is not a standalone hit for
the shorter one; a lowercase spelling of a capitalized official term is an
ordinary word; and a derived name surface asserts no constraint. The same
input gave a different report under v7, so stored resolution contexts and
saved manuscript-v3 segment results carrying v7 go stale by the existing
freshness rule and are re-reviewed.

wuwaterm-cjk-matcher-v9 also requires a plausible right word boundary for a
single-character source name. An unrecognized CJK continuation or a middle-dot
compound (红椿·蕾, 心·蕾, 红·椿) is not evidence of a standalone name. A
following known word, an honorific, a one-character particle, or the existing
name grammar still admits 欢迎椿来到, 椿却没有回应, 椿大人 and 心大人,
whereas 红椿蕊 and 红椿·蕾 do not yield the character inside them. Both
translation reference admission and review-v2 consult the same helper, and the
honorific and particle sets join the MATCHER_REVISION payload so stored v8
resolution contexts go stale and are re-reviewed.

wuwaterm-cjk-matcher-v10 refines single-character CJK mention judgment into an
explicit three-way classifier (``classify_single_character_mention`` returning
``name_mention``, ``ambiguous_mention``, or ``ordinary_word``). Both translation
reference admission and review-v2 keep exact ``name_mention`` gating while
distinguishing settled compounds and middle-dot terms from uncertain everyday or
unknown continuations; ``single_character_name_mention_at`` remains as a
compatibility predicate.

wuwaterm-cjk-matcher-v11 changes no matcher input. Every matcher function, the
lexicon slice and the single-character name sets are the v10 ones. review-v2
gains the opt-in ``ambiguous-mention-v1`` feature: an ``ambiguous_mention``
one-character resonator name whose request-local evidence meets the
``single_char_context`` threshold is reported as a ``not_evaluated`` finding,
and a resolution may now name it. A review that does not ask for the feature
gives the v10 report, but the same request can now give a different report, so
stored resolution contexts and saved manuscript segment results carrying v10
go stale by the existing freshness rule and are re-reviewed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    _WORDS: frozenset[str]

MAX_WORD_LEN = 8
LEXICON_COUNT = 66315
JIEBA_COMMIT = "67fa2e36e72f69d9134b8a1037b83fbb070b9775"
JIEBA_DICT_PATH = "jieba/dict.txt"
LEXICON_FILTER = "U+3400-U+9FFF; length 2-8; frequency >= 24"


# The frequency-filtered slice omits ordinary plant and material names that
# end in the resonator character 椿: 香椿 (toon), 臭椿 (tree of heaven), 椿芽,
# 春椿, 椿木 and 椿皮. This explicit supplement is hashed with the target
# matcher contract; it does not change the pinned lexicon or source/sentence
# locking.
_SINGLE_NAME_COMPOUNDS = frozenset({"香椿", "臭椿", "椿芽", "春椿", "椿木", "椿皮"})

# A single-character dictionary surface that is also an everyday standalone
# noun. 心 (Hsin) names a resonator and is also the ordinary word for heart,
# mind or core, so it keeps the narrow name-context rule that the sentence
# lock has always applied; 心情 is an ordinary compound and 心很重要 is not a
# mention. Hashed with the compound supplement because it decides the same
# admission question.
_EVERYDAY_STANDALONE_CHARS = frozenset({"心"})

# Honorifics a speaker or narrator can put after a one-character name. They are
# grammar, not part of the name: 心大人 addresses the resonator Hsin exactly as
# 椿大人 addresses Camellya, while 心很重要 and 心情 remain ordinary words.
_SINGLE_NAME_HONORIFICS = (
    "大人",
    "小姐",
    "先生",
    "女士",
    "前辈",
    "阁下",
    "老师",
    "队长",
    "长官",
)

# One-character function words that can follow a name inside a clause: 椿却没有
# 回应, 椿等共鸣者, 欢迎椿来到, 椿有极其优秀的. They are particles, adverbs,
# prepositions and light verbs, not evidence that the character is part of a
# longer noun compound; the ordinary compound check above still runs first, so
# 心情, 椿树 and 香椿 stay excluded.
_SINGLE_NAME_PARTICLES = frozenset(
    "的和与在是也会要能都有却只又还已曾将正就才更很不没把被对向给跟同比让说问看等们呢吗吧啊"
)


def _load_words() -> frozenset[str]:
    import importlib.resources

    package = __package__ or "wuwaterm"
    text = (
        importlib.resources.files(package)
        .joinpath("cjk_words.txt")
        .read_text(encoding="utf-8")
    )
    return frozenset(text.splitlines())


def _get_words() -> frozenset[str]:
    words = globals().get("_WORDS")
    if words is not None:
        return words
    loaded = _load_words()
    globals()["_WORDS"] = loaded
    return loaded


def __getattr__(name: str) -> object:
    if name == "_WORDS":
        return _get_words()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | {"_WORDS"})


def _is_cjk_character(char: str) -> bool:
    return "\u3400" <= char <= "\u9fff"


def single_character_name_context_ok(text: str, start: int, end: int) -> bool:
    """Whether a one-character span sits in a bounded Chinese name context.

    Only newly eligible one-character surfaces use this conservative rule.
    Unlisted contexts (for example 欢迎椿) may remain unlocked; multi-character
    names retain the existing matching policy.

    A middle dot on either side closes the question before any other rule: it
    is not a CJK character, so without this check the ordinary punctuation
    branch would count it as a boundary and admit 红·椿, 心·蕾 or the 椿 of
    「椿·蕾」 as a standalone name.
    """
    if (start > 0 and text[start - 1] == "·") or (
        end < len(text) and text[end] == "·"
    ):
        return False
    if start and _is_cjk_character(text[start - 1]):
        if text[start - 1] not in "和与让给由是用有":
            return False
    return (
        end == len(text)
        or not _is_cjk_character(text[end])
        or text.startswith(_SINGLE_NAME_HONORIFICS, end)
        or text.startswith(("的", "和", "与", "在", "是", "也", "会", "要", "能",
                            "加入", "离开", "登场", "使用", "说"), end)
    )


def single_character_compound_at(text: str, start: int, end: int) -> bool:
    """Whether an ordinary word covers this single-character target form.

    Called only for one-character resonator candidates, never as a global
    ban on one-character official forms. A 2..8-char ordinary word covering
    this span extends at least one edge (香椿, 臭椿, 椿芽 and 春椿 end with the
    character; 椿树, 椿木 and 椿皮 begin with it). It is not a general Chinese
    word segmenter or a sentence-meaning check.
    """
    if not (0 <= start < end <= len(text) and end - start == 1):
        return False
    if not "\u3400" <= text[start] <= "\u9fff":
        return False
    words = _get_words()
    for lo in range(max(0, end - MAX_WORD_LEN), start + 1):
        for hi in range(max(end, lo + 2), min(len(text), lo + MAX_WORD_LEN) + 1):
            word = text[lo:hi]
            if word in words or word in _SINGLE_NAME_COMPOUNDS:
                return True
    return False


def span_inside_known_word(text: str, start: int, end: int) -> bool:
    """Whether a longer ordinary lexicon word covers text[start:end].

    Returns True only when a listed word spans [start, end) and extends past
    at least one edge (e.g. 变奏 inside 变奏曲). The span's own exact text is
    not a veto.
    """
    if not (0 <= start < end <= len(text)):
        return False
    words = _get_words()
    for left in range(max(0, start - MAX_WORD_LEN + 1), start + 1):
        for right in range(end, min(len(text), left + MAX_WORD_LEN) + 1):
            if (left < start or right > end) and text[left:right] in words:
                return True
    return False


SINGLE_CHAR_NAME_MENTION: Literal["name_mention"] = "name_mention"
SINGLE_CHAR_AMBIGUOUS_MENTION: Literal["ambiguous_mention"] = "ambiguous_mention"
SINGLE_CHAR_ORDINARY_WORD: Literal["ordinary_word"] = "ordinary_word"
SingleCharMentionKind = Literal["name_mention", "ambiguous_mention", "ordinary_word"]


def classify_single_character_mention(
    text: str, start: int, end: int
) -> SingleCharMentionKind:
    """Classify a one-character CJK span as name, ambiguous, or ordinary word.

    Returns ``ordinary_word`` when the span is invalid, joined by a middle dot
    compound marker (``·``), or covered by a known 2..8-character compound.
    Returns ``name_mention`` when the character appears in an admitted name
    context or has a clear right word/particle/honorific boundary. Otherwise
    returns ``ambiguous_mention`` (for everyday standalone characters outside
    explicit name grammar, or unrecognized CJK continuations).
    """
    if (
        end - start != 1
        or start < 0
        or end > len(text)
        or not _is_cjk_character(text[start])
    ):
        return SINGLE_CHAR_ORDINARY_WORD
    if (start > 0 and text[start - 1] == "·") or (
        end < len(text) and text[end] == "·"
    ):
        # A middle dot welds the halves of one invented compound name, on
        # either side of this character, so it is not a standalone mention.
        return SINGLE_CHAR_ORDINARY_WORD
    if single_character_compound_at(text, start, end):
        return SINGLE_CHAR_ORDINARY_WORD
    if text[start:end] in _EVERYDAY_STANDALONE_CHARS:
        if single_character_name_context_ok(text, start, end):
            return SINGLE_CHAR_NAME_MENTION
        return SINGLE_CHAR_AMBIGUOUS_MENTION
    if end < len(text) and _is_cjk_character(text[end]):
        words = _get_words()
        if (
            single_character_name_context_ok(text, start, end)
            or text[end] in _SINGLE_NAME_PARTICLES
            or text.startswith(_SINGLE_NAME_HONORIFICS, end)
            or any(
                text[end:hi] in words
                for hi in range(end + 2, min(len(text), end + MAX_WORD_LEN) + 1)
            )
        ):
            return SINGLE_CHAR_NAME_MENTION
        return SINGLE_CHAR_AMBIGUOUS_MENTION
    return SINGLE_CHAR_NAME_MENTION


def single_character_name_mention_at(text: str, start: int, end: int) -> bool:
    """Whether a one-character official name at this span is a name mention.

    Both callers ask the same question: term preparation before it offers the
    character to the model as a reference, review.py before it turns the
    mention into a finding. An ordinary word covering the character (香椿,
    臭椿, 椿芽, 春椿, 椿木, 椿皮, 椿树) makes it that word, not a name. A
    character that is also an everyday standalone noun (心) additionally needs
    the narrow name context the sentence lock has always required, so 心很重要
    and 她的心很痛 are neither references nor findings.

    A plausible right boundary is required as well. The lexicon cannot list
    every novel mechanic name, so an unrecognized CJK continuation, or a
    middle-dot compound (红椿·蕾), means the character is a component of a
    longer word rather than a standalone mention (红椿蕊). A known two-to-eight
    character word, an honorific, a one-character particle, punctuation, HTML
    markup or whitespace is a boundary; 欢迎椿来到, 快找椿商量对策 and 椿却没有
    回应 stay admitted. This is not a general Chinese word segmenter or a
    sentence-meaning check.
    """
    return classify_single_character_mention(text, start, end) == SINGLE_CHAR_NAME_MENTION


def crosses_cjk_word_boundary(text: str, start: int, end: int) -> bool:
    """Return whether ordinary words cross both edges of text[start:end]."""
    if start < 0 or end > len(text) or end - start < 2:
        return False
    for char in text[start:end]:
        if not "\u3400" <= char <= "\u9fff":
            return False
    words = _get_words()
    left = False
    right = False
    window_lo = max(0, start - MAX_WORD_LEN)
    window_hi = min(len(text), end + MAX_WORD_LEN)
    for index in range(window_lo, end):
        limit = min(MAX_WORD_LEN, window_hi - index)
        for length in range(2, limit + 1):
            piece = text[index : index + length]
            if piece not in words:
                continue
            bound = index + length
            if index < start < bound:
                left = True
            if index < end < bound:
                right = True
            if left and right:
                return True
    return False


def _matcher_revision(words: frozenset[str]) -> str:
    import hashlib
    import json

    payload = json.dumps(
        [
            "wuwaterm-cjk-matcher-v11",
            JIEBA_COMMIT,
            LEXICON_FILTER,
            sorted(words),
            sorted(_SINGLE_NAME_COMPOUNDS),
            sorted(_EVERYDAY_STANDALONE_CHARS),
            sorted(_SINGLE_NAME_HONORIFICS),
            sorted(_SINGLE_NAME_PARTICLES),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


MATCHER_REVISION = "286d29e8f25b1ee53d0634c89bf6fb8a83ef950888c21f15d485413cb25d0664"
