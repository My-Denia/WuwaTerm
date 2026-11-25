"""Normalization helpers for term matching."""

from __future__ import annotations

import re
import unicodedata

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
# Marker-form matching only. Both patterns once matched bare words anywhere,
# which deleted real prose ("Wuthering Waves 2.1 brings..." lost its subject,
# "Major spoilers ahead" became "Majors ahead") and mangled URLs like
# example.com/ww2.0. Bracketed/hashtag tags still strip anywhere in a line;
# bare word forms only as a line-leading label or a whole decorated line.
_VERSION_TAG_CORE = r"(?:ww|wuthering\s*waves)\s*\d+(?:\.\d+){1,2}"
VERSION_TAG_RE = re.compile(
    rf"(?im)(?:[\[(（【]\s*{_VERSION_TAG_CORE}\s*[\])）】])|(?:^\s*{_VERSION_TAG_CORE}\s*$)"
)
_SPOILER_WORDS = r"(?:spoilers|spoiler|spoliers|spolier|剧透)"
SPOILER_RE = re.compile(
    rf"(?im)(?:[\[(（【]\s*{_SPOILER_WORDS}\s*[:：-]?\s*[\])）】])"
    # Horizontal whitespace only ([^\S\n]): a plain \s* here would consume the
    # newline after a line-ending "#spoiler" and merge adjacent content lines.
    # Require a boundary after the keyword (no word character, and no hyphen
    # followed by non-whitespace) so "#spoilersafe", "#spoiler-free", and
    # "#剧透警告" are preserved while "#spoiler- 今汐" and "#剧透：今汐" strip.
    rf"|(?:#{_SPOILER_WORDS}(?!\w|-(?=\S))[^\S\n]*(?:[:：]|-(?=\s|$))?[^\S\n]*)"
    rf"|(?:^\s*{_SPOILER_WORDS}\s*[:：-]\s*)"
    rf"|(?:^[\s*_#-]*{_SPOILER_WORDS}[\s*_#:：-]*$)"
)
# Horizontal whitespace only ([^\S\n]) so blank paragraph separators before a
# quoted line are preserved rather than swallowed across lines.
QUOTE_BAR_RE = re.compile(
    r"(?m)^[^\S\n]*(?:[|｜│┃▌▍▏]+[^\S\n]*|>{1,}(?:[^\S\n]+|$))+"
)
BLANK_LINES_RE = re.compile(r"\n{3,}")
_PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t]*\n+")
# CJK Ext A, CJK Unified, CJK Compatibility Ideographs, CJK Ext B-F.
CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿\U00020000-\U0002ebef]")
LATIN_RE = re.compile(r"[A-Za-z]")


def strip_markup(value: str) -> str:
    value = TAG_RE.sub("", value)
    value = value.replace("\\n", "\n")
    return value.strip()


def clean_source_text(value: str) -> str:
    value = strip_markup(value)
    if value.startswith("dnt/"):
        value = value[4:]
    return value.strip()


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = SPACE_RE.sub("", value)
    return value.casefold()


def normalize_prose_text(value: str) -> str:
    """Normalize line endings and spacing while preserving user prose verbatim.

    Generic Core translation uses this path so line-leading comparators
    (``>50%``), hashtags (``#spoilersafe``), and version headers
    (``[Wuthering Waves 3.7]``) are never silently deleted outside explicit
    Telegram wrapper cleanup.
    """
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [SPACE_RE.sub(" ", line).strip() for line in value.splitlines()]
    value = "\n".join(line for line in lines if line)
    value = BLANK_LINES_RE.sub("\n\n", value)
    return value.strip()


def strip_telegram_wrapper_noise(value: str, *, keep_paragraphs: bool = False) -> str:
    """Strip explicit Telegram screenshot/wrapper markers before translation."""

    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = VERSION_TAG_RE.sub("", value)
    value = SPOILER_RE.sub("", value)
    value = QUOTE_BAR_RE.sub("", value)
    if not keep_paragraphs:
        return normalize_prose_text(value)
    parts = (normalize_prose_text(part) for part in _PARAGRAPH_BREAK_RE.split(value))
    return "\n\n".join(part for part in parts if part)


def normalize_user_text(value: str) -> str:
    """Telegram-adapter alias for :func:`strip_telegram_wrapper_noise`."""
    return strip_telegram_wrapper_noise(value)


def normalize_ascii(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^0-9A-Za-z]+", "", value)
    return value.casefold()


def count_cjk(text: str) -> int:
    return len(CJK_RE.findall(text))


def count_latin(text: str) -> int:
    return len(LATIN_RE.findall(text))


def has_cjk(text: str) -> bool:
    """True when the text contains at least one CJK ideograph.

    Used to pick translation direction: Chinese source -> English (the
    default), English/Latin source -> Chinese.
    """
    return CJK_RE.search(text) is not None
