"""Deterministic number-attribution candidates for Layer 2 review (ADR 0022, ADR 0024).

Layer 1 decides that every number on one side exists on the other side with the
same value, unit, sign and comparison. It deliberately does not decide which
number belongs to which stat: ``攻击力20%，暴击率10%`` translated as ``ATK 10%,
CRIT Rate 20%`` passes it, because pairing numbers by position or by
neighbouring words raises a false alarm on every sentence a translator reorders
(ADR 0022).

This module answers the narrower mechanical question that is left. When both
sides name the same stat slots, both sides write the same set of values, and
one slot carries a different value than its counterpart, it returns a
*candidate* carrying the pairing evidence: ``atk`` reads 20 here and 10 there.
A candidate is not a finding. It has no rule id, no span and no verdict, it
never appears in a structure-check response, and by itself it claims nothing
about the translation; only review of the meaning can decide that (ADR 0024).

Pure: no model, no storage, no network. Both sides are bounded (a side longer
than :data:`MAX_SIDE_SCALARS` yields no candidate) and so is the output (at most
:data:`MAX_SLOTS` differing slots with :data:`MAX_VALUES_PER_SLOT` values each),
because the hint is meant to be small enough to put in a prompt.

The slot table and the pairing rule are fixed by the offline measurement in
``docs/eval/structure-attribution-candidates.md``; ``mode='attribution'`` of
``scripts/eval_structure_checks.py`` reproduces it.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import TypedDict

from .structural_checks import extract_placeholders

CANDIDATE_VERSION = "attribution-candidates-v1"
# One number per stat name is the shape being read; a single named slot has no
# counterpart to disagree with.
MIN_SHARED_SLOTS = 2
# The semantic-review side limit. A longer side is out of scope for every rule
# this package runs, and returning early keeps the reader's cost bounded.
MAX_SIDE_SCALARS = 2000
# Output caps: the hint goes into a prompt, so it must not grow with the input.
MAX_SLOTS = 8
MAX_VALUES_PER_SLOT = 4


class AttributionSlot(TypedDict):
    """One slot whose value differs, with the values each side writes on it."""

    slot: str
    source: list[str]
    target: list[str]


class AttributionCandidate(TypedDict):
    """The whole hint: version, how many slots both sides name, the differing ones."""

    version: str
    shared_slots: int
    slots: list[AttributionSlot]


# One language-neutral slot per quantity a game sentence attributes a number
# to: ``(slot, zh pattern, en pattern)``. Order is precedence, longest form
# first within a slot, so `暴击伤害` reads as `crit_dmg` and never as the
# `crit_rate` word plus prose, and `CRIT DMG` never falls back to `dmg`.
# Slots name a stat a number is the value of, not a mechanism word that counts
# things (`每层`, `每 2 秒`): those words sit beside a number without owning it.
SLOTS: tuple[tuple[str, str, str], ...] = (
    ("crit_dmg", "暴击伤害|暴伤", r"CRIT\s*DMG|Crit\.?\s*DMG|CRIT\s*Damage|Crit\.?\s*Damage"),
    ("crit_rate", "暴击率|暴击几率|暴击概率|暴击", r"CRIT\s*Rate|Crit\.?\s*Rate|CRIT\s*Hit"),
    ("energy_regen", "共鸣效率", r"Energy\s+Regen(?:eration)?"),
    ("cooldown", "冷却时间|冷却", r"Cooldown"),
    ("duration", "持续时间|持续", r"Duration|Lasts|Lasting"),
    ("heal", "治疗加成|治疗效果|治疗量|治疗|回复|回血",
     r"Healing(?:\s+Bonus)?|Heal|Restores?|Restoring|Restoration"),
    ("shield", "护盾值|护盾效果|护盾", r"Shield(?:s|ing)?"),
    ("dmg", "伤害倍率|伤害加深|伤害", r"DMG|Damage"),
    ("hp", "生命值上限|生命值|生命|血量", r"HP|Health"),
    ("def", "防御力|防御", r"DEF|Defen[cs]e"),
    ("atk", "攻击力|攻击", r"ATK"),
    ("stamina", "耐力值|耐力", r"Stamina"),
    ("energy", "共鸣能量|能量", r"Energy"),
    ("chance", "概率|几率", r"Chance|Probability"),
    ("range", "范围", r"Range"),
    ("resist", "抗性", r"RES|Resistance"),
    ("cost", "消耗", r"Consum(?:e|es|ed|ing|ption)|Cost"),
    ("stacks", "层数", r"Stacks?"),
)


def _slot_reader(patterns: tuple[tuple[str, str], ...], *, words: bool) -> re.Pattern[str]:
    """One pass over a masked side, yielding the slot of each written match."""
    body = "|".join(f"(?P<{slot}>{pattern})" for slot, pattern in patterns)
    if words:
        body = rf"(?<![A-Za-z0-9])(?:{body})(?![A-Za-z0-9])"
        return re.compile(body, re.IGNORECASE)
    return re.compile(body)


_SLOT_READERS = (
    _slot_reader(tuple((slot, zh) for slot, zh, _ in SLOTS), words=False),
    _slot_reader(tuple((slot, en) for slot, _, en in SLOTS), words=True),
)
# `{Cus:Sap,S=stack P=stacks SapTag=0}` carries digits that are arguments, not
# quantities. The shape mirrors ``structural_checks._CUS_TEMPLATE_RE``; the
# pattern stays a local copy so that a change to the server's number rule
# cannot silently move what this reader pairs.
_TEMPLATE_RE = re.compile(r"\{Cus:[A-Za-z]{1,16},[^{}<>{}\n]{0,160}\}")
# Resonance Chain / refinement markers (ADR 0022): one fact each, never a
# quantity a stat slot owns. The shapes follow ``structural_checks._BUILD_RE``.
_BUILD_RE = re.compile(
    r"(?<![A-Za-z0-9])[SsCc][0-9](?:[ \t]?[Rr][0-9])?(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])[Rr][0-9](?![A-Za-z0-9])"
    r"|(?<![0-9.+\-×*/=<>])[0-6]\+[1-5](?![0-9.+\-×*/=<>%倍xX])"
    r"|(?<![0-9.])[0-6]链(?:[0-9]精)?(?![接条])"
)
# Digits as the number rule reads them, minus the letter-glued ones (`SOL3`,
# `T-G0NG`, `HP50%`): this reader only pairs a number with a slot, so it can
# afford to skip the ambiguous spellings.
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[0-9]{1,3}(?:,[0-9]{3})+(?![0-9])|[0-9]+)(?:\.[0-9]+)*"
)
# Clause ends. An ASCII `.` or `,` between digits is a decimal point or a
# thousands separator, not a clause end.
_CLAUSE_END_RE = re.compile(r"(?<![0-9])[.,](?![0-9])|[，;；。！!？?、\n\r]")


def _hidden_ranges(text: str) -> list[tuple[int, int]]:
    """Ranges of ``text`` that no slot or number may be read from."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for token in extract_placeholders(text):
        start = text.find(token, cursor)
        if start < 0:
            continue
        cursor = start + len(token)
        spans.append((start, cursor))
    spans += [match.span() for match in _TEMPLATE_RE.finditer(text)]
    spans += [match.span() for match in _BUILD_RE.finditer(text)]
    return spans


def _masked(text: str) -> str:
    """``text`` with masked ranges blanked, offsets preserved."""
    hidden = [False] * len(text)
    for start, end in _hidden_ranges(text):
        for index in range(start, end):
            hidden[index] = True
    return "".join(" " if hidden[index] else char for index, char in enumerate(text))


def _value(token: str) -> str:
    """One value per number, whichever way it is written: `0.80` and `0.8` agree."""
    try:
        return format(Decimal(token.replace(",", "")).normalize(), "f")
    except (ArithmeticError, ValueError):
        return token


def _clauses(masked: str, slots: tuple[tuple[str, int, int], ...]) -> list[tuple[int, int]]:
    """Clause bounds, never cutting through a slot name (`Crit. Rate`)."""
    bounds: list[tuple[int, int]] = []
    start = 0
    for match in _CLAUSE_END_RE.finditer(masked):
        position = match.start()
        if any(slot_start <= position < slot_end for _, slot_start, slot_end in slots):
            continue
        bounds.append((start, position))
        start = match.end()
    bounds.append((start, len(masked)))
    return bounds


def _slots_of(masked: str) -> list[tuple[str, int, int]]:
    """Every written slot occurrence from either reader, left to right."""
    found: list[tuple[str, int, int]] = []
    for reader in _SLOT_READERS:
        for match in reader.finditer(masked):
            slot = match.lastgroup
            if slot is not None:
                found.append((slot, match.start(), match.end()))
    return sorted(found, key=lambda entry: (entry[1], -entry[2]))


def _rank(slot_start: int, slot_end: int, number_start: int, number_end: int) -> tuple[int, int]:
    """Reading order of a number for a slot: the one written after it, else the one before.

    Both languages write the value after the name it belongs to (`攻击力提升
    20%`, `ATK +20%`), and a clause keeps several stats in one sentence, so a
    number further on is a better reading than a closer one already passed.
    The second element breaks ties among numbers on the same side.
    """
    if number_start >= slot_end:
        return 0, number_start - slot_end
    return 1, slot_start - number_end


def _pairing(text: str) -> dict[str, set[str]]:
    """The ``slot -> values`` reading of one side, clause by clause.

    A slot is dropped when its own clause leaves no single closest number (two
    numbers the same distance apart) or when two different slots claim the
    same number. Guessing there is what makes positional pairing raise a false
    alarm on natural translations, so the reader declines to pair.
    """
    masked = _masked(text)
    numbers = [
        (match.start(), match.end(), _value(match.group()))
        for match in _NUMBER_RE.finditer(masked)
    ]
    slots = tuple(_slots_of(masked))
    pairing: dict[str, set[str]] = {}
    for clause_start, clause_end in _clauses(masked, slots):
        clause_numbers = [n for n in numbers if clause_start <= n[0] and n[1] <= clause_end]
        clause_slots = [s for s in slots if clause_start <= s[1] and s[2] <= clause_end]
        if not clause_numbers or not clause_slots:
            continue
        chosen: list[tuple[str, int]] = []
        for slot, slot_start, slot_end in clause_slots:
            ranked = sorted(
                (_rank(slot_start, slot_end, n[0], n[1]), index)
                for index, n in enumerate(clause_numbers)
            )
            if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
                continue
            chosen.append((slot, ranked[0][1]))
        owners: dict[int, set[str]] = {}
        for slot, index in chosen:
            owners.setdefault(index, set()).add(slot)
        for slot, index in chosen:
            if len(owners[index]) > 1:
                continue
            pairing.setdefault(slot, set()).add(clause_numbers[index][2])
    return pairing


def detect_attribution_candidates(source: str, target: str) -> AttributionCandidate | None:
    """A Layer 2 hint when both sides write the same values on different slots.

    ``None`` unless all of: neither side is longer than
    :data:`MAX_SIDE_SCALARS`, at least two stat slots are named on both sides,
    the values written on those slots are the same set, and at least one slot
    carries a different value than its counterpart. A number the other side
    does not account for at all is Layer 1's ``number_missing`` rule's, so it
    is not a candidate here.
    """
    if len(source) > MAX_SIDE_SCALARS or len(target) > MAX_SIDE_SCALARS:
        return None
    source_slots = _pairing(source)
    target_slots = _pairing(target)
    shared = sorted(set(source_slots) & set(target_slots))
    if len(shared) < MIN_SHARED_SLOTS:
        return None
    source_values = sorted(value for slot in shared for value in source_slots[slot])
    target_values = sorted(value for slot in shared for value in target_slots[slot])
    if source_values != target_values:
        return None
    differing = [slot for slot in shared if source_slots[slot] != target_slots[slot]]
    if not differing:
        return None
    slots: list[AttributionSlot] = [
        AttributionSlot(
            slot=slot,
            source=sorted(source_slots[slot])[:MAX_VALUES_PER_SLOT],
            target=sorted(target_slots[slot])[:MAX_VALUES_PER_SLOT],
        )
        for slot in differing[:MAX_SLOTS]
    ]
    return AttributionCandidate(
        version=CANDIDATE_VERSION, shared_slots=len(shared), slots=slots
    )
