# Number attribution: an offline candidate signal for Layer 2

- Status: offline prototype, measured. No runtime code, contract or finding
  category changes; nothing in this document is wired into the server.
- Date: 2026-11-26
- Related: [ADR 0022](../adr/0022-deterministic-structure-checks.md),
  [ADR 0018](../adr/0018-semantic-review.md),
  `scripts/eval_structure_checks.py` (mode `attribution`),
  `tests/fixtures/structure-attribution.json`,
  `tests/test_eval_structure_checks.py`.

## Problem

Layer 1 decides that every number on one side exists on the other side with the
same value, unit, sign and comparison. It deliberately does not decide which
number belongs to which stat: `攻击力20%，暴击率10%` translated as `ATK 10%,
CRIT Rate 20%` raises nothing, because pairing numbers by position or by
neighbouring words would raise a false alarm on every sentence a translator
reorders. ADR 0022 leaves that question to Layer 2 semantic review, and on the
committed number fixture Layer 1 finds 0 of 6 attribution swaps.

Layer 2 sees the pair, but it has no mechanical evidence about which value
moved: by the time a model reads two percentages and two stat names, the swap
is one plausible reading among several. A deterministic reader that names the
slots and the values it paired, and says which pair disagrees, turns "look
closely at these percentages" into "`atk` reads 20% here and 10% there".

This document measures such a reader as an offline candidate signal, and
records how it would reach Layer 2 without changing the API, the schemas or the
finding categories.

## The signal

A **candidate** is a pair where both sides name the same two or more stat
slots, both sides write the same set of numbers, and the numbers sit on
different slots. It is not a finding: it carries no rule id, no span and no
claim that the translation is wrong. It carries the pairing evidence.

### Masking

The reader blanks, with offsets preserved, everything that is markup rather
than text: WuwaTerm and HTML placeholders read by the server's own
`extract_placeholders`, `{Cus:...}` client directives whose arguments
(`SapTag=0`) are not quantities, and Resonance Chain / refinement markers
(`S0R1`, `0+1`, `6链`). A masked number can be neither paired nor counted, so a
tag argument never becomes a stat value.

### Slots

One language-neutral name per stat a sentence attributes a number to. The
written form is matched longest-first at each position, so `暴击伤害` reads as
`crit_dmg` and never as `crit_rate` plus prose, and `CRIT DMG` never falls back
to `dmg`.

| Slot | Chinese forms | English forms |
| --- | --- | --- |
| `crit_dmg` | 暴击伤害, 暴伤 | CRIT DMG, Crit. DMG, CRIT Damage |
| `crit_rate` | 暴击率, 暴击几率, 暴击概率, 暴击 | CRIT Rate, Crit. Rate |
| `energy_regen` | 共鸣效率 | Energy Regen |
| `cooldown` | 冷却时间, 冷却 | Cooldown |
| `duration` | 持续时间, 持续 | Duration, Lasts |
| `heal` | 治疗加成, 治疗效果, 治疗, 回复, 回血 | Healing Bonus, Heal, Restores |
| `shield` | 护盾值, 护盾效果, 护盾 | Shield |
| `dmg` | 伤害倍率, 伤害加深, 伤害 | DMG, Damage |
| `hp` | 生命值上限, 生命值, 生命, 血量 | HP, Health |
| `def` | 防御力, 防御 | DEF, Defense |
| `atk` | 攻击力, 攻击 | ATK |
| `stamina` | 耐力值, 耐力 | Stamina |
| `energy` | 共鸣能量, 能量 | Energy |
| `chance` | 概率, 几率 | Chance, Probability |
| `range` | 范围 | Range |
| `resist` | 抗性 | RES, Resistance |
| `cost` | 消耗 | Consume, Cost |
| `stacks` | 层数 | Stack, Stacks |

A slot names a stat a number is the value of. Mechanism words that count things
(`每层`, `每 2 秒`, `叠加`) are not slots: they sit beside a number without
owning it, and pairing them would turn every stacking description into a fake
swap.

### Pairing

1. Split each side into clauses at `，。；！？、` and at sentence punctuation,
   never at an ASCII `.` or `,` between digits (`0.8%`, `1,000`) and never
   through a slot name (`Crit. Rate`).
2. In a clause, a slot reads the **first number written after it**; only when
   the clause has none does it read the last number before it. Both languages
   write the value after the name it belongs to (`攻击力提升20%`, `ATK +20%`),
   and a value earlier in the same clause usually belongs to a slot already
   passed.
3. Drop the slot when the clause leaves no single closest number (two numbers
   the same distance away) or when two different slots claim the same number.
   Declining to pair is what keeps natural reordering from producing evidence.

### Candidate test

With `source_slots` and `target_slots` the per-slot value sets:

1. at least two slots are named on both sides;
2. the values written on those slots are the same multiset on both sides — a
   value only one side accounts for is Layer 1's `number_missing`, not an
   attribution question;
3. at least one slot carries a different value than its counterpart.

The hint is the differing slots, source values and target values:

```json
{
  "version": "attribution-candidates-v1",
  "shared_slots": 2,
  "slots": [
    {"slot": "atk", "source": ["20"], "target": ["10"]},
    {"slot": "crit_rate", "source": ["10"], "target": ["20"]}
  ]
}
```

## Why a candidate and not a finding

- **The slot table is a vocabulary, not a rule.** A stat outside it is
  invisible; a stat spelled differently is a miss. Layer 1's contract is
  "reproducible finding with a span"; a wrong attribution would be a new class
  of false alarm under that promise, on exactly the reordered sentences ADR
  0022 calls out as the reason not to pair.
- **The evidence needs meaning to become a claim.** The reader knows `atk`
  reads 20% here and 10% there. It does not know whether the translator
  restructured a sentence, whether one side names a stat the other expresses as
  a mechanism, or whether the source itself is ambiguous. Only review of the
  meaning can decide that.
- **One measured noise shape stays.** A single official pair (in both
  directions) raises a candidate in which the two sides break a multi-level
  ability block into sentences differently, so a value lands in a different
  clause. That is not a swap, and it is the shape a deterministic finding could
  not survive.

## Measured

All numbers come from `python scripts/eval_structure_checks.py attribution`
(and with `--textmap-root data/wutheringdata/Textmaps`). No model is called;
nothing leaves the machine; no corpus text is printed.

### Synthetic pairs

`tests/fixtures/structure-attribution.json` holds 54 hand-written pairs.
Each runs as written, in the direction it is written: 11 zh→en and 10 en→zh
positives, 11 zh→en and 5 en→zh natural reorderings, 11 zh→en and 6 en→zh
clean multi-stat segments. The gates for this mode were fixed before the run
(80% swapped recall, zero candidates on both negative buckets). The pairs were
written against the same slot vocabulary the reader uses, so the numbers below
measure the reader against its own table, not against an unknown text: the
corpus section is what measures the table against text nobody wrote for it.

| Bucket | Cases | Candidates | Intended |
| --- | --- | --- | --- |
| `swapped` | 21 | 21 (100%) | one value moved onto another stat, same numbers on both sides |
| `natural_reorder` | 16 | 0 | clauses reordered, voice changed, same facts |
| `clean_multi_stat` | 17 | 0 | multi-stat sentences with consistent values, repeated values, single-stat segments |

Every hint names the slots the case moved. The recall bar for this mode was
80%; the measurement is 21/21.
### Official corpus

The 27,385 zh-Hans/en pairs of the official 3.7.8 game text (data `9218d61`)
that hold a digit, read in both directions: **54,770 runs, 2 candidates**
(1 zh→en, 1 en→zh, the same pair read both ways), a candidate rate of
**0.0037%** against a 0.5% ceiling. Neither run raises any structure finding, so
the candidate never restates a Layer 1 result.

The pair is the multi-level ability block described above. Its hint is advisory
by construction, and reading it is the Layer 2 step.

### Cost

The adversarial pairs of the number rule (23 inputs, each side at the 2,000
scalar limit, aimed at every reader of the number rule) run the attribution
reader in **under 3.1 ms** at worst, best of five runs, across repeated runs of
the suite. The masking, slot and number patterns are single-pass and bounded;
no input in that set costs more than a few milliseconds, so there is no
backtracking shape to exploit.

## Where it would go in Layer 2 (design only)

Nothing below exists in the code today. It is the shape a follow-up change
would take, kept separate so this prototype can be measured without touching
the server.

1. **The reader moves into the package.** `scripts/eval_structure_checks.py`
   keeps the benchmark; the reader itself becomes a pure function beside
   `structural_checks.py` with the same no-model, no-storage, no-network
   properties.
2. **The prompt gains one optional payload key.** `build_messages` in
   `semantic_review.py` builds a JSON payload today (`term_facts`,
   `precedents`, `context_before`). When a gate is on and the pair has a
   candidate, the payload gains `numeric_attribution` with the hint, plus one
   wording constant in the style of `TERM_FACT_WORDING` saying it is
   program-computed surface evidence, not a verdict. The gate is a
   server-side environment flag like the existing semantic options, off by
   default, so the prompt, the bytes and the cost of a normal request do not
   change.
3. **Nothing in the wire contract changes.** No request field, no response
   field, no OpenAPI change, no new finding category and no new rule id: a
   confirmed attribution problem is reported as the existing
   `quantity_change`, which both clients already accept. The hint never appears
   in a response body, only in the prompt.
4. **Layer 1 keeps its answer.** The structure-check endpoint does not gain an
   attribution rule; a client that only calls it sees exactly what it sees
   today.

### Gates before any rollout

Measured offline, on frozen sets, before the gate is turned on anywhere:

1. **Real-model attribution.** On the 21 synthetic swaps plus a sample of
   official pairs read as swaps, the model must report a `quantity_change` on
   the pairs the hint names, at a rate stated before the run; a model that
   ignores the hint is a failed gate, not a passed one.
2. **No new alarm on natural pairs.** On a frozen set of natural translations
   (the `natural_reorder` and `clean_multi_stat` buckets, plus official pairs
   with no candidate), the number of findings must not rise against the same
   model with the gate off. The hint must never be the stated basis for a
   finding on a pair where the reader found no candidate.
3. **No finding outside `quantity_change`.** An attribution hint that produces
   `meaning_shift`, `unsupported_addition` or `omission` findings has changed
   the category semantics and is out of scope.
4. **Bounded cost.** Prompt tokens and latency per request stay inside the
   existing semantic-review budget; the receipt records the flag state so a
   run with and without the hint is reproducible.
5. **Silence when off.** With the flag off, the request bytes, the response
   bytes and the findings equal today's for the committed semantic-review
   fixtures.

## Reproduce

```bash
python scripts/eval_structure_checks.py attribution
python scripts/eval_structure_checks.py attribution --textmap-root data/wutheringdata/Textmaps
python -m pytest tests/test_eval_structure_checks.py -q
```
