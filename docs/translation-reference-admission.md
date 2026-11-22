# Source coverage and occurrence-scoped translation references

This supplement explains the candidate mechanism following
[ADR 0017](adr/0017-contextual-term-references.md) and
[ADR 0019](adr/0019-document-context-and-human-precedents.md), and it does not
set aside the rulings already recorded in them. Only a frozen real-model
comparison can say whether quality improves; the offline suite settles which
source rows exist, which occurrences are admitted, and what request reaches the
model.

## Missing source records

At the pinned WutheringData commit
`9218d612ad815e398e064e577e42aaf878899968`, the Chinese and English
`Textmaps/*/multi_text/MultiText.json` files hold a family of eight
`Advertising_VersionTitle_<number>` keys (`30`–`37`). Earlier the dictionary
carried only the one curated member (`37`). The builder now takes every member
whose key has that shape (adding 7 multi-character release titles on the pinned
3.7 data), keeps curated keys untouched, and refuses a member whose side is
missing or blank. Such a row is a contextual reference in either direction,
never a fixed substitution: it names a release, and the same wording can appear
in prose. Provenance stays the original source key.

The four-tier material extractor used to skip a whole family as soon as one of
its two base surfaces was already taken by a different meaning. That is what
dropped `调谐器 / Tuner` because `Speaker_1513_Name` already supplies
`调音师 / Tuner`. On the pinned 3.7 data, switching the guard to exact pair
uniqueness (`existing_pairs`) unlocks only `调谐器 / Tuner` with zero noisy
additions. The derived row keeps its `tier-base:<smallest Basic row id>`
provenance.

An established record remains the deterministic whole-input primary when a
derived record shares its surface (`lookup.preferred_exact_entry`, backed by
pure `term_classification.select_preferred_exact_entry`). On the
model path, competing pairs are alternatives supplied together as
`TermReference` items (`reason="ambiguous_alternative"`), never a forced
replacement, and a surface with more than one distinct pair is never
hard-locked. Dictionary lookup still exposes all records.

## One reference line per meaning

Several candidate tables can list one name, always pairing the same two
strings and differing only in the category column. `model_reference_candidates`
collapses those rows per surface by `(zh, en)`, keeping the highest-priority
`CATEGORY_ORDER` category, so the model receives one line per meaning rather
than one per row. Only more than one distinct `(zh, en)` pair makes a surface
ambiguous.

## Casing and paragraph-level context for derived common nouns

The prior hard-lock spelling set remains separate. Derived common-noun rows
(`Area_<n>_Title` such as `教学楼` and `tier-base:<id>` such as `能源核心` /
`能量袋`, via `term_classification.derived_context_gated_sources`) and derived
English soft-casing spellings (`origin="derived_soft_casing"`,
`reason="soft_casing"`, such as `sealed tubes`) are admitted as model
references only when their **natural paragraph** (separated by blank lines
`\n\n`) already contains at least one non-derived official dictionary hit
(including admitted resonator/speaker names). This keeps isolated ordinary
paragraphs such as `我们学校的教学楼很旧。`, `the energy core of a star`, or
`a guitar tuner` free of game hints even inside a multi-paragraph segment,
while preserving in-context derived terms (`拉贝尔学部` in a game paragraph,
`sealed tubes` alongside `Sealed Tubes`) and official `Advertising_VersionTitle_<n>`
release titles. The existing exact/plural owner wins, pre-exclusion casefold
collisions block synthesis even if one member is later excluded, and both
free-text exclusion lists still apply.

## A one-character name does not name every compound containing it

ADR 0017 widened single-character references so `欢迎椿` could identify
Camellya. Source admission now also requires a plausible right word boundary:
the existing name grammar, a following known lexicon word, an honorific
(`大人`, `小姐`, `先生`, `女士`, `前辈`, `阁下`, `老师`, `队长`, `长官`), a
one-character particle (`的`, `却`, `等`, `已`, …), or a non-CJK boundary. An
unrecognized CJK continuation or a middle-dot continuation on either side does
not establish a standalone name. Thus `欢迎椿来到`, `快找椿商量对策`,
`椿却没有回应`, `椿等共鸣者` and `心大人` remain admitted, while the character
inside `红椿蕊`, `红椿·蕾`, `心·蕾` or `红·椿` does not. The ordinary-compound
and everyday-character checks still run first, so `心情`, `心很重要` and
`香椿拌豆腐` stay ordinary words.

Translation (`term_preparation`) and `review-v2` call the same helper in
`cjk_span.py`, so the same character is admitted or withheld in both.
`MATCHER_REVISION` therefore advances cleanly from `wuwaterm-cjk-matcher-v8`
to `wuwaterm-cjk-matcher-v9`, hashing `_SINGLE_NAME_HONORIFICS` and
`_SINGLE_NAME_PARTICLES` alongside the existing inputs so stored `v8`
resolution contexts go stale by the existing freshness contract.

## Closed-class skill stem references (`zh->en` only)

For Chinese-to-English requests only (`to_chinese is False`),
`skill_references.py` derives two-character stems strictly from the five
documented official `BinData/skill/skilltype.json` rows (`SkillType_2_TypeName`,
`SkillType_3_TypeName`, `SkillType_5_TypeName`, `SkillType_6_TypeName`,
`SkillType_11_TypeName`). An occurrence is admitted only when:

- it is not covered by a longer selected dictionary span (such as `变奏技能`)
  and not embedded inside a longer known lexicon word (such as `变奏曲`, checked
  via `cjk_span.span_inside_known_word`);
- local action syntax and independent local game-context evidence (or
  `切<resonator/speaker>`) both appear in the same clause of the submitted
  segment (never read-only previous context);
- for ambiguous stems (`共鸣`), a unique local sense marker (`技能`, `解放`,
  `回路`) disambiguates the target.

Each admitted occurrence is carried as a `TermReference` with
`origin="scoped_skill_stem"`, `reason="local_skill_action"`, and exact
`TermOccurrence` coordinates (`model_start`, `model_end`) in the final model
input. English-to-Chinese requests never invoke this rule.

## On-demand request scope and local context snippets

The prompt is unchanged unless the request actually needs the extra
explanation:

- an admitted one-character CJK name mention (`single_char_name_mention`);
- a bounded `scoped_skill_stem` reference (`local_skill_action`);
- a selected surface with more than one distinct official pair;
- a soft-cased spelling of a derived name.

Any request that needs none of these arrives at the model with the exact bytes
it carried before this mechanism existed. `validated_reference_ranges` verifies
every `(model_start, model_end)` coordinate against `locked_text` (after term
and structural placeholders) fail-closed before any HTTP request leaves the
process. Occurrence-scoped references (`single_char_name_mention` and
`scoped_skill_stem`) are separated from `Dictionary term references:` into a
`Scoped term references:` block rendered with deduplicated single-line local
context snippets (`ONLY at:` / `NOT at:`, collapsing >4 admitted occurrences to
`(all occurrences in this text)`) rather than bare numeric offsets, so
`thinking: disabled` models do not have to count character indices.

## Context fidelity

When a request opts into `translation-context-v1` (`context is not None`), the
context rules also preserve numeric shorthand such as `3+1` or `S0R1`, resolve
omitted actors from the clause without turning rhetorical addressees into
actors, and require every clause to be translated. Previous-segment context
remains read-only and human precedents retain occurrence scope.
