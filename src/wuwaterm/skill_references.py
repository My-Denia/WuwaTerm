"""Derive occurrence-scoped skill hints from the official skill-type rows."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import re

from .cjk_span import MAX_WORD_LEN, _WORDS
from .models import TermEntry


_SKILL_TYPE_IDS = frozenset({"2", "3", "5", "6", "11"})
_AMBIGUOUS_SKILL_TYPE_IDS = frozenset({"2", "3", "6"})
_SKILL_SOURCE = "BinData/skill/skilltype.json"
_CLAUSE_BREAK = re.compile(r"[。！？；，\n.!?;,]")
_ACTION_BEFORE = re.compile(r"(?:释放|施放|使用|发动|触发|打出|施展|衔接|切人|换人|切换|切|打).{0,3}$")
_ACTION_AFTER = re.compile(r"^(?:技能|伤害|入场|出场|切入|切出)")
_GAME_CONTEXT = re.compile(r"共鸣者|声骸|技能|伤害|战斗|攻击|普攻|切人|换人|切换角色")


@dataclass(frozen=True)
class SkillReference:
    surface: str
    zh: str
    en: str
    category: str
    sense_marker: str
    needs_disambiguation: bool


@dataclass(frozen=True)
class SkillOccurrence:
    reference: SkillReference
    start: int
    end: int


def _inside_known_word(text: str, start: int, end: int) -> bool:
    """A longer ordinary word owns its characters, including at either edge."""
    for left in range(max(0, start - MAX_WORD_LEN + 1), start + 1):
        for right in range(end, min(len(text), left + MAX_WORD_LEN) + 1):
            if (left < start or right > end) and text[left:right] in _WORDS:
                return True
    return False


def skill_references(
    entries: Iterable[TermEntry],
    existing_sources: set[str],
    excluded: frozenset[str],
) -> tuple[SkillReference, ...]:
    """Build short forms from the five documented official skill-type rows."""
    references = []
    for entry in entries:
        if (
            entry.source_file != _SKILL_SOURCE
            or entry.source_id not in _SKILL_TYPE_IDS
            or entry.text_key != f"SkillType_{entry.source_id}_TypeName"
            or entry.category != "core_term"
            or len(entry.zh) <= 2
            or not entry.en
            or "\n" in entry.zh
            or "\n" in entry.en
            or any(not "\u3400" <= char <= "\u9fff" for char in entry.zh[:2])
        ):
            continue
        surface = entry.zh[:2]
        if surface in existing_sources or {surface, entry.zh, entry.en} & excluded:
            continue
        references.append(
            SkillReference(
                surface=surface,
                zh=entry.zh,
                en=entry.en,
                category=entry.category,
                sense_marker=entry.zh[2:],
                needs_disambiguation=entry.source_id in _AMBIGUOUS_SKILL_TYPE_IDS,
            )
        )
    return tuple(references)


def skill_occurrences(
    text: str,
    context: str,
    offset: int,
    references: tuple[SkillReference, ...],
    game_actions: tuple[tuple[int, int], ...] = (),
) -> tuple[SkillOccurrence, ...]:
    """Find local action-context occurrences in this submitted segment.

    ``context`` is the same segment's visible text, including visible text
    around protected structures. It is never a separately scanned document.
    """
    by_surface: dict[str, list[SkillReference]] = {}
    for reference in references:
        by_surface.setdefault(reference.surface, []).append(reference)
    occurrences = []
    for surface, choices in by_surface.items():
        start = text.find(surface)
        while start >= 0:
            end = start + len(surface)
            context_start = offset + start
            context_end = offset + end
            if _inside_known_word(context, context_start, context_end):
                start = text.find(surface, start + 1)
                continue
            before = context[max(0, context_start - 16) : context_start]
            after = context[context_end : context_end + 16]
            breaks_before = list(_CLAUSE_BREAK.finditer(before))
            if breaks_before:
                before = before[breaks_before[-1].end() :]
            match_after = _CLAUSE_BREAK.search(after)
            if match_after:
                after = after[: match_after.start()]
            local = before + surface + after
            action_before = _ACTION_BEFORE.search(before)
            action_after = _ACTION_AFTER.match(after)
            if action_before:
                action_range = (action_before.start(), action_before.end())
            elif action_after:
                after_start = len(before) + len(surface)
                action_range = (after_start, after_start + action_after.end())
            else:
                start = text.find(surface, start + 1)
                continue
            game_context = any(
                marker.end() <= action_range[0] or marker.start() >= action_range[1]
                for marker in _GAME_CONTEXT.finditer(local)
            )
            game_action = any(
                context_start - len(before) <= start < end <= context_start
                for start, end in game_actions
            )
            if game_context or game_action:
                senses = [
                    choice
                    for choice in choices
                    if not choice.needs_disambiguation
                    or choice.sense_marker in before + after
                ]
                if len(senses) == 1:
                    occurrences.append(SkillOccurrence(senses[0], start, end))
            start = text.find(surface, start + 1)
    return tuple(occurrences)
