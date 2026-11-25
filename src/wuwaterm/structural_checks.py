"""Pure stdlib post-translation structural verification and regrouping helpers.

These checks operate purely on Unicode scalars, sentence/paragraph ranges from
``text_spans``, numeric tokens, and template/protocol placeholders. Keeping
them outside ``sentence`` and ``translation_plan`` ensures both pure-matching
validation and LLM execution share one deterministic implementation without
touching storage or network layers. The same helpers back the public Layer 1
structure checks (``check_segment_structure``, structure-check-v1, ADR 0022).
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation
import re
import statistics
from typing import Literal
import unicodedata

from .text_spans import WHITESPACE, paragraph_ranges, segment_core

# Absorbing one line into another and giving a third away as two leaves the
# count where it was, and shifts the content: from the absorbing line to the
# giving one, each answer line answers the source line before it, and after that
# they agree again. Regrouping such an answer would carry text across a break.
#
# Both events are read from a per-line share ratio,
# ``(answer line / answer total) / (source line / source total)``. The ratio
# cancels the translation's overall expansion and each source line's own
# length, so a line that absorbed a naturally long neighbour still reads as two
# lines' worth of text rather than as a local peak against short ones. It also
# does not dilute as the segment grows, which is why this guard is a per-line
# test rather than a check on the two sides' running shares: a one-line shift
# moves the running share by 1/N (5% at 20 lines, 2.2% at 45), while a faithful
# answer at +/-35% per-line length noise already reaches a 5% running-share gap
# at p99, so that signal cannot separate the two at any N.
_MERGED_LINE_SHARE = 1.4
# A merged line also stands this clear of the segment's median line, so a
# whole-answer length bias cannot lift an ordinary line over the bound above.
# The two bounds work together: the first keeps an ordinary line out, the second
# keeps a line out whose segment runs long or short as a whole. 1.3 rather than
# a rounder 1.5 because a merge of two unequal lines lands lower: a line of 40
# with a neighbour of 30 holds 1.75 lines' worth, and a 20% noise draw that
# comes out short leaves 1.42, which a 1.5 peer bound on a median of 1.0 denies,
# while the split bounds below are what keep faithful answers out.
_MERGED_LINE_PEER = 1.3
# The neighbour a merged line absorbed, as a fraction of its own length: a merge
# of two very unequal lines moves too little text to tell from variation.
_ABSORBED_NEIGHBOUR_SHARE = 0.75
# One line left with well under its own text. 0.60 rather than a tighter 0.55:
# a split whose two halves both come out long leaves the shorter half at about
# 0.59 (40, 30, 20, 30 answered as 56, 16, 18, 18 has one at 0.593), which a 0.55
# bound misses while 0.60 catches it, and no faithful line reaches 0.60 at the
# +/-35% per-line noise this guard is calibrated against.
_SPLIT_LINE_SHARE = 0.60
# Or the two halves of one line split in two: each half this small, and the two
# together holding one line's worth (a split conserves the line's text, so their
# ratios sum to about 1 however unevenly the model cut it).
_SPLIT_PAIR_LINE_SHARE = 0.65
_SPLIT_PAIR_TOTAL = (0.75, 1.25)

_OPAQUE_HTML_PLACEHOLDER_RE = re.compile(r"__WUWA_HTML_[0-9a-f]{16}_[0-9]{4,}__")


@dataclass(frozen=True)
class ParagraphStructureReport:
    """Comparison of paragraph and line layout between source and translation."""

    source_counts: tuple[int, ...]
    answer_counts: tuple[int, ...]
    source_paragraphs: int
    answer_paragraphs: int
    source_lines: int
    answer_lines: int
    paragraphs_match: bool
    lines_match: bool
    crossing_cuts: bool
    merged_and_split: bool
    regroupable: bool


def has_text(line: str) -> bool:
    """Whether ``line`` contains at least one non-WHITESPACE scalar."""
    return any(char not in WHITESPACE for char in line)


def paragraph_line_counts(text: str) -> list[int]:
    """Non-empty lines in each segmentation-v1 paragraph region of ``text``."""
    return [
        sum(1 for line in text[start:end].split("\n") if has_text(line))
        for start, end in paragraph_ranges(text)
    ]


def line_scalar_counts(text: str) -> list[int]:
    """Non-whitespace scalars of each non-empty line, in reading order."""
    return [
        sum(1 for char in line if char not in WHITESPACE)
        for line in text.split("\n")
        if has_text(line)
    ]


def cumulative_cuts(layout: Sequence[int]) -> frozenset[int]:
    """Where each paragraph ends, counted in non-empty lines."""
    total = 0
    cuts: set[int] = set()
    for count in layout[:-1]:
        total += count
        cuts.add(total)
    return frozenset(cuts)


def line_share_ratios(
    source_counts: Sequence[int], answer_counts: Sequence[int]
) -> list[float]:
    """How many source lines' worth of text each answer line carries."""
    source_total = sum(source_counts)
    answer_total = sum(answer_counts)
    if source_total <= 0 or answer_total <= 0:
        return []
    return [
        (answer / answer_total) / (source / source_total)
        for source, answer in zip(source_counts, answer_counts, strict=True)
    ]


def merged_line_indices(
    source_counts: Sequence[int],
    answer_counts: Sequence[int],
    ratios: Sequence[float],
) -> list[int]:
    """Answer lines that carry their own source line and a neighbour."""
    source_total = sum(source_counts)
    answer_total = sum(answer_counts)
    if source_total <= 0 or answer_total <= 0 or not ratios:
        return []
    source_shares = [count / source_total for count in source_counts]
    answer_shares = [count / answer_total for count in answer_counts]
    peer = statistics.median(ratios)
    merged: list[int] = []
    for index, ratio in enumerate(ratios):
        if ratio >= _MERGED_LINE_SHARE and ratio >= peer * _MERGED_LINE_PEER:
            merged.append(index)
            continue
        own = source_shares[index]
        excess = abs(answer_shares[index] - own)
        if excess <= 0:
            continue
        for other in (index + 1, index - 1):
            if not 0 <= other < len(source_counts):
                continue
            if source_shares[other] < own * _ABSORBED_NEIGHBOUR_SHARE:
                continue
            if abs(answer_shares[index] - (own + source_shares[other])) <= excess / 2:
                merged.append(index)
                break
    return merged


def split_line_indices(ratios: Sequence[float]) -> list[int]:
    """Answer lines that gave text away, on their own or as two halves."""
    split = [index for index, ratio in enumerate(ratios) if ratio <= _SPLIT_LINE_SHARE]
    for index in range(len(ratios) - 1):
        left, right = ratios[index], ratios[index + 1]
        if (
            left <= _SPLIT_PAIR_LINE_SHARE
            and right <= _SPLIT_PAIR_LINE_SHARE
            and _SPLIT_PAIR_TOTAL[0] <= left + right <= _SPLIT_PAIR_TOTAL[1]
        ):
            split.append(index)
    return split


def lines_merged_and_split(source: str, answer: str) -> bool:
    """Whether ``answer`` merged one line and split another."""
    source_counts = line_scalar_counts(source)
    answer_counts = line_scalar_counts(answer)
    if len(source_counts) != len(answer_counts) or len(source_counts) < 2:
        return False
    ratios = line_share_ratios(source_counts, answer_counts)
    if not ratios:
        return False
    if not merged_line_indices(source_counts, answer_counts, ratios):
        return False
    return bool(split_line_indices(ratios))


def check_paragraph_structure(source: str, answer: str) -> ParagraphStructureReport:
    """Analyze paragraph counts, line counts, crossing breaks, and merge/splits."""
    source_counts = tuple(paragraph_line_counts(source))
    answer_counts = tuple(paragraph_line_counts(answer))
    source_paragraphs = len(source_counts)
    answer_paragraphs = len(answer_counts)
    source_lines = sum(source_counts)
    answer_lines = sum(answer_counts)
    paragraphs_match = source_paragraphs == answer_paragraphs
    lines_match = source_lines == answer_lines
    source_cuts = cumulative_cuts(source_counts)
    answer_cuts = cumulative_cuts(answer_counts)
    crossing_cuts = (
        not paragraphs_match
        and lines_match
        and not (answer_cuts <= source_cuts or source_cuts <= answer_cuts)
    )
    merged_split = (
        not paragraphs_match
        and lines_match
        and not crossing_cuts
        and lines_merged_and_split(source, answer)
    )
    regroupable = (
        not paragraphs_match
        and lines_match
        and not crossing_cuts
        and not merged_split
    )
    return ParagraphStructureReport(
        source_counts=source_counts,
        answer_counts=answer_counts,
        source_paragraphs=source_paragraphs,
        answer_paragraphs=answer_paragraphs,
        source_lines=source_lines,
        answer_lines=answer_lines,
        paragraphs_match=paragraphs_match,
        lines_match=lines_match,
        crossing_cuts=crossing_cuts,
        merged_and_split=merged_split,
        regroupable=regroupable,
    )


def regroup_lines_into_source_paragraphs(source: str, answer: str) -> str:
    """Put the lines of ``answer`` back into the paragraphs of ``source`` when safe."""
    report = check_paragraph_structure(source, answer)
    if not report.regroupable:
        return answer
    start, end = segment_core(answer, 0, len(answer))
    lines = iter(line for line in answer[start:end].split("\n") if has_text(line))
    paragraphs = (
        "\n".join(next(lines) for _ in range(count))
        for count in report.source_counts
    )
    return answer[:start] + "\n\n".join(paragraphs) + answer[end:]


def extract_numbers(text: str) -> tuple[str, ...]:
    """Return Arabic-digit numeric tokens of the NFKC view of ``text`` in reading order."""
    view = _numeric_view(text, ())[0]
    return tuple(match.group() for match in _DIGIT_NUMBER_RE.finditer(view))


def missing_numbers(source: str, answer: str) -> tuple[str, ...]:
    """Return digit numbers of ``source`` that ``answer`` does not account for.

    The structure-check number rule, source side only: see
    :func:`check_segment_structure`.
    """
    return tuple(
        finding.source_span.text
        for finding in _number_findings(source, answer, (), ())
        if finding.rule_id == "number_missing" and finding.source_span is not None
    )


def extract_placeholders(text: str) -> tuple[str, ...]:
    """Return opaque WuwaTerm, template and markup placeholders in reading order."""
    return tuple(match.group() for match in _MARKUP_RE.finditer(text))


def check_placeholder_integrity(source: str, answer: str) -> bool:
    """Check whether ``answer`` preserves all placeholders from ``source``.

    All placeholder tokens must match in multiset multiplicity, and opaque HTML
    structural placeholders must also retain their exact relative order.
    """
    src_tokens = extract_placeholders(source)
    ans_tokens = extract_placeholders(answer)
    if Counter(src_tokens) != Counter(ans_tokens):
        return False
    src_html = [t for t in src_tokens if _OPAQUE_HTML_PLACEHOLDER_RE.fullmatch(t)]
    ans_html = [t for t in ans_tokens if _OPAQUE_HTML_PLACEHOLDER_RE.fullmatch(t)]
    return src_html == ans_html


# --------------------------------------------------------------------------
# structure-check-v1: Layer 1 deterministic pair checks
#
# One paired segment in, a list of findings out. Nothing here calls a model,
# reads storage or keeps state, so the same pair always yields the same
# findings. Offsets are Unicode scalar offsets into the text as submitted, before
# any normalization, so a client can highlight exactly what was flagged.
#
# The number check reports only Arabic-digit numbers. Chinese numerals, spelled
# English numbers, list labels and enumerated symbols are never reported on
# their own; they only excuse a digit number with the same value on the other
# side. A number that only some reading of the other side accounts for is
# treated as accounted for: a missed error is cheaper here than a false alarm.
# --------------------------------------------------------------------------

STRUCTURE_CHECK_VERSION: Literal["structure-check-v1"] = "structure-check-v1"
STRUCTURE_CHECKS = ("empty_target", "numbers", "placeholders", "paragraphs")
# Wire order of findings inside one segment; truncation keeps the first ones.
STRUCTURE_RULES = (
    "empty_target",
    "paragraph_count_mismatch",
    "line_count_mismatch",
    "placeholder_missing",
    "placeholder_unexpected",
    "tag_unbalanced",
    "number_missing",
    "number_unexpected",
)
_RULE_ORDER = {rule: index for index, rule in enumerate(STRUCTURE_RULES)}

# Markup a translation must carry over unchanged. Real game text uses
# `{0}`, `{PlayerName}`, `{Male=him;Female=her}` (whose values are translated),
# `<color=Highlight>…</color>`, `<size=20>`, `<te href=123>` and similar tags;
# WuwaTerm's own opaque tokens and printf conversions are kept from the
# earlier helpers. Order of alternatives matters: the gender switch is tried
# before the plain brace form, which would otherwise take it literally.
_GENDER_SWITCH_RE = re.compile(
    r"\{[A-Za-z_][A-Za-z0-9_]*=[^{};<>\n]{0,64}"
    r"(?:;[A-Za-z_][A-Za-z0-9_]*=[^{};<>\n]{0,64})+\}"
)
_SWITCH_KEY_RE = re.compile(r"(?:^\{|;)([A-Za-z_][A-Za-z0-9_]*)=")
# One bounded attribute run and no separate trailing `\s*`/`/?`, so nothing in
# the pattern can match the same character two ways; `/>` is read from the text.
_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9_-]{0,31})(?:[ =/][^<>\n]{0,96})?>")
_MARKUP_RE = re.compile(
    r"__WUWA_(?:TERM|HTML)_[0-9a-f]{16}_[0-9]{4,}__"
    r"|\{\{[^{}\s]{1,64}\}\}"
    rf"|{_GENDER_SWITCH_RE.pattern}"
    r"|\{[^{}\s]{1,64}\}"
    rf"|{_TAG_RE.pattern}"
    # printf: position, flags, zero padding, width, precision, conversion. Each
    # part takes characters no neighbour can, so a long run of `0` after a `%`
    # fails in linear time instead of trying every way to split it.
    r"|(?<![A-Za-z0-9])%(?:\d+\$)?[+\-#]*0*(?:[1-9]\d*)?(?:\.\d+)?"
    r"[diouxXeEfFgGcrs](?![A-Za-z0-9])"
)

_DIGIT_NUMBER_RE = re.compile(r"(?:[0-9]{1,3}(?:,[0-9]{3})+(?![0-9])|[0-9]+)(?:\.[0-9]+)*")
_EN_SCALE_AFTER_RE = re.compile(r"[ \t]*(thousand|million|billion)\b", re.IGNORECASE)
# Spelled numbers past this are not quantities a translation carries; skipping
# them also keeps `亿亿亿…` from building an integer too long to print.
_MAX_WORD_NUMBER = 10**20
# Exact decimal arithmetic for every digit count a 2,000-scalar side can hold.
_EXACT = Context(prec=10_000)
_LIST_LABEL_OPENERS = frozenset({"", "(", "[", "【"})
_LIST_LABEL_CLOSERS = frozenset({".", ")", "]", "、", "】"})
_HORIZONTAL_SPACE = " \t\u3000"

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "貳": 2, "两": 2, "兩": 2,
    "三": 3, "叁": 3, "參": 3, "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6,
    "陸": 6, "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9,
}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_MYRIADS = {"万": 10**4, "萬": 10**4, "亿": 10**8, "億": 10**8}
_CN_NUMERAL_CHARS = "".join((*_CN_DIGITS, *_CN_UNITS, *_CN_MYRIADS))
_CN_NUMERAL_RE = re.compile(
    rf"[{_CN_NUMERAL_CHARS}]+(?:点[{''.join(_CN_DIGITS)}]+)?"
)
# A digit number followed by one of these also reads as that multiple:
# `1.5万` is 15,000 and `3千` is 3,000.
_CN_MULTIPLIERS = {**_CN_UNITS, **_CN_MYRIADS}
_CN_MULTIPLIER_AFTER_RE = re.compile(rf"[ \t]*([{''.join(_CN_MULTIPLIERS)}])")

_EN_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20, "thirtieth": 30, "fortieth": 40,
    "fiftieth": 50, "sixtieth": 60, "seventieth": 70, "eightieth": 80,
    "ninetieth": 90, "once": 1, "twice": 2, "thrice": 3, "dozen": 12,
}
_EN_SCALES = {
    "hundred": 100, "hundredth": 100, "thousand": 10**3, "thousandth": 10**3,
    "million": 10**6, "millionth": 10**6, "billion": 10**9, "billionth": 10**9,
}
_EN_WORD_RE = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True)
class StructureSpan:
    """Half-open Unicode scalar range of the submitted side, with its text."""

    start: int
    end: int
    text: str


@dataclass(frozen=True)
class StructureFinding:
    rule_id: str
    source_span: StructureSpan | None = None
    target_span: StructureSpan | None = None


@dataclass(frozen=True)
class StructureLayout:
    source_paragraphs: int
    target_paragraphs: int
    source_lines: int
    target_lines: int


@dataclass(frozen=True)
class SegmentStructureReport:
    findings: tuple[StructureFinding, ...]
    layout: StructureLayout


@dataclass(frozen=True)
class _Token:
    span: StructureSpan
    values: frozenset[str]
    key: str = ""
    # A digit number as read in the NFKC view (`１００,２００．５` → `100,200.5`).
    folded: str = ""


def is_blank(text: str) -> bool:
    """No visible scalar: only WHITESPACE and invisible format characters."""
    return all(char in WHITESPACE or unicodedata.category(char) == "Cf" for char in text)


def _canonical(value: Decimal | int | float) -> str:
    """Plain decimal text: no exponent, no trailing zeros, no leading zeros.

    Formatted by ``decimal`` alone. Converting through ``int`` would hit
    Python's integer-to-string digit limit on a long enough digit run.
    """
    number = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    return format(_EXACT.normalize(number), "f")


def _digit_values(token: str) -> tuple[str, Decimal | None]:
    plain = token.replace(",", "")
    if plain.count(".") > 1:
        return plain, None
    try:
        number = Decimal(plain)
    except InvalidOperation:  # pragma: no cover - the pattern admits digits only
        return plain, None
    return _canonical(number), number


def _chinese_numeral_value(run: str) -> str | None:
    """Value of a Chinese numeral run, or None past :data:`_MAX_WORD_NUMBER`."""
    integer, _, fraction = run.partition("点")
    if integer and all(char in _CN_DIGITS for char in integer) and len(integer) > 1:
        # 二〇二四: a digit-by-digit reading, as years and codes are written.
        if len(integer) > len(str(_MAX_WORD_NUMBER)):
            return None
        value = Decimal("".join(str(_CN_DIGITS[char]) for char in integer))
    else:
        total = section = number = 0
        for char in integer:
            if char in _CN_DIGITS:
                number = _CN_DIGITS[char]
            elif char in _CN_UNITS:
                section += (number or 1) * _CN_UNITS[char]
                number = 0
            elif char in ("万", "萬"):
                total += (section + number or 1) * _CN_MYRIADS[char]
                section = number = 0
            else:
                total = (total + section + number or 1) * _CN_MYRIADS[char]
                section = number = 0
            if max(total, section) > _MAX_WORD_NUMBER:
                return None
        value = Decimal(total + section + number)
    if fraction:
        value += Decimal("0." + "".join(str(_CN_DIGITS[char]) for char in fraction))
    return _canonical(value)


def _english_number_runs(text: str) -> list[str]:
    """Values of spelled English numbers such as `twenty-one` or `two hundred`."""
    values: list[str] = []
    total = current = 0
    active = False
    previous_end = 0

    def close() -> None:
        nonlocal total, current, active
        if active and total + current <= _MAX_WORD_NUMBER:
            values.append(_canonical(total + current))
        total = current = 0
        active = False

    for match in _EN_WORD_RE.finditer(text):
        word = match.group().casefold()
        gap = text[previous_end : match.start()]
        joined = active and gap.strip(" \t-") == ""
        if word == "and" and joined:
            previous_end = match.end()
            continue
        if word in _EN_NUMBER_WORDS or word in _EN_SCALES:
            if not joined:
                close()
            active = True
            # Clamped just past the limit so `hundred hundred …` stays small;
            # close() then drops the run.
            if word in _EN_NUMBER_WORDS:
                current += _EN_NUMBER_WORDS[word]
            elif _EN_SCALES[word] == 100:
                current = min((current or 1) * 100, _MAX_WORD_NUMBER + 1)
            else:
                total = min(
                    total + (current or 1) * _EN_SCALES[word], _MAX_WORD_NUMBER + 1
                )
                current = 0
        else:
            close()
        previous_end = match.end()
    close()
    return values


def _numeric_view(
    text: str, masked: Sequence[tuple[int, int]]
) -> tuple[str, list[int], list[frozenset[str]]]:
    """NFKC view of ``text`` for number reading, with an offset map back.

    Masked ranges (markup) become spaces. Decimal digits of any script become
    ASCII digits. A character that is a number only by compatibility (①, ²,
    ½, Ⅳ, ㋀) becomes a space and is kept as an excuse value instead: it is
    an enumerator or an exponent far more often than a quantity. The
    fullwidth comma stays as it is, because Chinese text uses it between list
    items (`100，200`) and never inside one number.
    """
    hidden = [False] * len(text)
    for start, end in masked:
        hidden[start:end] = [True] * (end - start)
    chars: list[str] = []
    origin: list[int] = []
    specials: list[frozenset[str]] = []
    for index, char in enumerate(text):
        if hidden[index]:
            chars.append(" ")
            origin.append(index)
            continue
        if char == "，" or char in _CN_NUMERAL_CHARS:
            chars.append(char)
            origin.append(index)
            continue
        category = unicodedata.category(char)
        if category == "Nd":
            chars.append(str(unicodedata.decimal(char)))
            origin.append(index)
            continue
        folded = unicodedata.normalize("NFKC", char)
        digits = re.findall(r"[0-9]+", folded)
        if category in ("No", "Nl") or digits:
            values = {_canonical(int(run)) for run in digits}
            numeric = unicodedata.numeric(char, None)
            if numeric is not None:
                values.add(_canonical(numeric))
            if values:
                specials.append(frozenset(values))
            chars.append(" ")
            origin.append(index)
            continue
        for piece in folded:
            chars.append(piece)
            origin.append(index)
    return "".join(chars), origin, specials


def _is_list_label(view: str, start: int, end: int, token: str) -> bool:
    """`1.`, `2)`, `(3)` or `4、` opening a line, not followed by another number."""
    if not token.isdigit() or len(token) > 3:
        return False
    line_start = view.rfind("\n", 0, start) + 1
    if view[line_start:start].strip(_HORIZONTAL_SPACE) not in _LIST_LABEL_OPENERS:
        return False
    if end >= len(view) or view[end] not in _LIST_LABEL_CLOSERS:
        return False
    return not view[end + 1 :].lstrip(_HORIZONTAL_SPACE)[:1].isdigit()


def _number_tokens(
    text: str, masked: Sequence[tuple[int, int]]
) -> tuple[list[_Token], list[frozenset[str]]]:
    """Reportable digit numbers, and the values that only excuse the other side."""
    view, origin, excuses = _numeric_view(text, masked)
    reportable: list[_Token] = []
    for match in _DIGIT_NUMBER_RE.finditer(view):
        start, end = match.span()
        token = match.group()
        canonical, number = _digit_values(token)
        values = {canonical}
        if number is not None:
            multiplier = _CN_MULTIPLIER_AFTER_RE.match(view, end)
            if multiplier:
                factor = _CN_MULTIPLIERS[multiplier.group(1)]
                values.add(_canonical(_EXACT.multiply(number, Decimal(factor))))
            scale = _EN_SCALE_AFTER_RE.match(view, end)
            if scale:
                factor = _EN_SCALES[scale.group(1).casefold()]
                values.add(_canonical(_EXACT.multiply(number, Decimal(factor))))
        source_start = origin[start]
        source_end = origin[end - 1] + 1
        span = StructureSpan(source_start, source_end, text[source_start:source_end])
        # A list label, or the `:00` of a clock time that the other language
        # writes as `4点`, is never reported on its own.
        label = _is_list_label(view, start, end, token)
        clock_zero = start > 0 and view[start - 1] == ":" and set(token) == {"0"}
        if label or clock_zero:
            excuses.append(frozenset(values))
            continue
        reportable.append(
            _Token(span=span, values=frozenset(values), key=canonical, folded=token)
        )
    for match in _CN_NUMERAL_RE.finditer(view):
        value = _chinese_numeral_value(match.group())
        if value is not None:
            excuses.append(frozenset({value}))
    excuses.extend(frozenset({value}) for value in _english_number_runs(view))
    return reportable, [values for values in excuses if values]


class _Pool:
    """Unclaimed positions per value, taken in reading order."""

    def __init__(self, entries: Sequence[frozenset[str]], used: list[bool]):
        self.used = used
        self.positions: dict[str, deque[int]] = {}
        for position, values in enumerate(entries):
            if not used[position]:
                for value in sorted(values):
                    self.positions.setdefault(value, deque()).append(position)

    def take(self, value: str, held: frozenset[int] = frozenset()) -> int | None:
        queue = self.positions.get(value)
        if not queue:
            return None
        while queue and self.used[queue[0]]:
            queue.popleft()
        for position in queue:
            if not self.used[position] and position not in held:
                return position
        return None


def _pair_tokens(left: list[_Token], right: list[_Token]) -> tuple[list[bool], list[bool]]:
    """Match digit numbers across the two sides, exact values first."""
    left_used = [False] * len(left)
    right_used = [False] * len(right)
    for exact in (True, False):
        pool = _Pool(
            [frozenset({token.key}) if exact else token.values for token in right],
            right_used,
        )
        for position, token in enumerate(left):
            if left_used[position]:
                continue
            for value in sorted({token.key} if exact else token.values):
                match = pool.take(value)
                if match is not None:
                    right_used[match] = left_used[position] = True
                    break
    # `100,200` on one side can be the list `100、200` on the other.
    for grouped, other, grouped_used, other_used in (
        (left, right, left_used, right_used),
        (right, left, right_used, left_used),
    ):
        pool = _Pool([token.values for token in other], other_used)
        for position, token in enumerate(grouped):
            if grouped_used[position] or "," not in token.folded:
                continue
            held: set[int] = set()
            for part in token.folded.split(","):
                match = pool.take(_digit_values(part)[0], frozenset(held))
                if match is None:
                    break
                held.add(match)
            else:
                grouped_used[position] = True
                for match in held:
                    other_used[match] = True
    return left_used, right_used


def _excuse(tokens: list[_Token], used: list[bool], excuses: list[frozenset[str]]) -> None:
    pool = _Pool(excuses, [False] * len(excuses))
    for position, token in enumerate(tokens):
        if used[position]:
            continue
        for value in sorted(token.values):
            match = pool.take(value)
            if match is not None:
                pool.used[match] = used[position] = True
                break


def _number_findings(
    source: str,
    target: str,
    source_masked: Sequence[tuple[int, int]],
    target_masked: Sequence[tuple[int, int]],
) -> list[StructureFinding]:
    source_numbers, source_excuses = _number_tokens(source, source_masked)
    target_numbers, target_excuses = _number_tokens(target, target_masked)
    source_used, target_used = _pair_tokens(source_numbers, target_numbers)
    _excuse(source_numbers, source_used, target_excuses)
    _excuse(target_numbers, target_used, source_excuses)
    findings = [
        StructureFinding("number_missing", source_span=token.span)
        for token, used in zip(source_numbers, source_used, strict=True)
        if not used
    ]
    findings += [
        StructureFinding("number_unexpected", target_span=token.span)
        for token, used in zip(target_numbers, target_used, strict=True)
        if not used
    ]
    return findings


def _markup_key(token: str) -> str:
    if _GENDER_SWITCH_RE.fullmatch(token):
        return "{" + ";".join(f"{key}=" for key in _SWITCH_KEY_RE.findall(token)) + "}"
    return token


def _markup_tokens(text: str) -> list[_Token]:
    return [
        _Token(
            span=StructureSpan(match.start(), match.end(), match.group()),
            values=frozenset(),
            key=_markup_key(match.group()),
        )
        for match in _MARKUP_RE.finditer(text)
    ]


def _unbalanced_tags(tokens: list[_Token], closing_names: frozenset[str]) -> list[_Token]:
    """Tags that open or close out of nesting order, among names that close."""
    stack: list[tuple[str, _Token]] = []
    offending: list[_Token] = []
    for token in tokens:
        tag = _TAG_RE.fullmatch(token.span.text)
        if (
            tag is None
            or token.span.text.endswith("/>")
            or tag.group(2) not in closing_names
        ):
            continue
        name = tag.group(2)
        if not tag.group(1):
            stack.append((name, token))
        elif stack and stack[-1][0] == name:
            stack.pop()
        else:
            offending.append(token)
    offending.extend(token for _, token in stack)
    return sorted(offending, key=lambda token: token.span.start)


def _markup_findings(
    source_tokens: list[_Token], target_tokens: list[_Token]
) -> list[StructureFinding]:
    available = Counter(token.key for token in target_tokens)
    findings: list[StructureFinding] = []
    for token in source_tokens:
        if available[token.key] > 0:
            available[token.key] -= 1
        else:
            findings.append(StructureFinding("placeholder_missing", source_span=token.span))
    expected = Counter(token.key for token in source_tokens)
    for token in target_tokens:
        if expected[token.key] > 0:
            expected[token.key] -= 1
        else:
            findings.append(
                StructureFinding("placeholder_unexpected", target_span=token.span)
            )
    if findings:
        return findings
    # Same tags on both sides, so only their order can still be wrong.
    closing = frozenset(
        tag.group(2)
        for token in source_tokens
        if (tag := _TAG_RE.fullmatch(token.span.text)) is not None and tag.group(1)
    )
    if _unbalanced_tags(source_tokens, closing):
        return []
    return [
        StructureFinding("tag_unbalanced", target_span=token.span)
        for token in _unbalanced_tags(target_tokens, closing)
    ]


def _layout(source: str, target: str) -> StructureLayout:
    def unify(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n")

    source_counts = paragraph_line_counts(unify(source))
    target_counts = paragraph_line_counts(unify(target))
    return StructureLayout(
        source_paragraphs=len(source_counts),
        target_paragraphs=len(target_counts),
        source_lines=sum(source_counts),
        target_lines=sum(target_counts),
    )


def _finding_order(finding: StructureFinding) -> tuple[int, int]:
    span = finding.source_span or finding.target_span
    return _RULE_ORDER[finding.rule_id], -1 if span is None else span.start


def check_segment_structure(source: str, target: str) -> SegmentStructureReport:
    """Layer 1 deterministic checks for one paired segment (structure-check-v1).

    A blank source has nothing to compare and yields no finding. A blank
    target for a non-blank source yields only ``empty_target``: every other
    check would restate it.
    """
    layout = _layout(source, target)
    if is_blank(source):
        return SegmentStructureReport(findings=(), layout=layout)
    if is_blank(target):
        return SegmentStructureReport(
            findings=(StructureFinding("empty_target"),), layout=layout
        )
    findings: list[StructureFinding] = []
    if layout.source_paragraphs != layout.target_paragraphs:
        findings.append(StructureFinding("paragraph_count_mismatch"))
    elif layout.source_lines != layout.target_lines:
        findings.append(StructureFinding("line_count_mismatch"))
    source_markup = _markup_tokens(source)
    target_markup = _markup_tokens(target)
    findings += _markup_findings(source_markup, target_markup)
    findings += _number_findings(
        source,
        target,
        [(token.span.start, token.span.end) for token in source_markup],
        [(token.span.start, token.span.end) for token in target_markup],
    )
    return SegmentStructureReport(
        findings=tuple(sorted(findings, key=_finding_order)), layout=layout
    )
