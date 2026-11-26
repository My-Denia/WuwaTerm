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
`\n\n`, or by non-empty lines `\n` when a non-context request has no blank
lines) already contains at least one non-derived official dictionary hit
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
one-character particle (`的`, `有`, `却`, `等`, `已`, …), or a non-CJK boundary. An
unrecognized CJK continuation or a middle-dot continuation on either side does
not establish a standalone name. Thus `欢迎椿来到`, `快找椿商量对策`,
`椿却没有回应`, `椿有极其优秀的`, `椿等共鸣者` and `心大人` remain admitted, while the character
inside `红椿蕊`, `红椿·蕾`, `心·蕾` or `红·椿` does not. The ordinary-compound
and everyday-character checks still run first, so `心情`, `心很重要` and
`香椿拌豆腐` stay ordinary words.

Translation (`term_preparation`) and `review-v2` call the same helper in
`cjk_span.py`, so the same character is admitted or withheld in both.
`MATCHER_REVISION` therefore advances cleanly from `wuwaterm-cjk-matcher-v8`
to `wuwaterm-cjk-matcher-v9`, hashing `_SINGLE_NAME_HONORIFICS` and
`_SINGLE_NAME_PARTICLES` alongside the existing inputs so stored `v8`
resolution contexts go stale by the existing freshness contract.

`wuwaterm-cjk-matcher-v10` (#319) put the same right-boundary rule behind
`classify_single_character_mention`, which answers `name_mention`,
`ambiguous_mention` or `ordinary_word`. Translation reference admission
(`term_matching`, `term_preparation`) and `review-v2` admit a one-character
name only on `name_mention`, so an `ambiguous_mention` is withheld exactly as
the boolean predicate `single_character_name_mention_at` (kept for
compatibility) withheld it under `v9`. No admission outcome changed from `v9`;
the `MATCHER_REVISION` label moved to `57428b88…`, and stored `v9` resolution
contexts and saved manuscript results went stale by the same freshness
contract and were re-reviewed with the same outcome.

The current matcher is `wuwaterm-cjk-matcher-v11` (`286d29e8…`). Its matcher
inputs are the `v10` ones; the label moves because `review-v2` gained the
opt-in `ambiguous-mention-v1` feature (`X-WuwaTerm-Review-Features`, together
with `reason-v1`, and listed by `GET /v1/capabilities`). With it, a
one-character resonator name that the classifier calls `ambiguous_mention`, and
whose evidence from the submitted source alone meets the `single_char_context`
threshold (two independent kinds, one of them an entity kind), is reported as
a `not_evaluated` finding with reason `ambiguous_mention` and its official
candidates. It becomes `verified_constraint` or `confirmed_conflict` only after
the user resolves it with `official_pair`. While unresolved it changes no
other finding; it never counts as evaluated, sorts after every other finding
and is the first to be dropped when the finding page or the byte ceiling is
reached. A review
that does not ask for the feature returns the `v10` report; the one difference
is that a response carrying the matcher revision (`review-v2` sent with
`X-WuwaTerm-Matcher-Basis: 1`) now carries the `v11` value instead of the `v10`
one, so only responses without it keep the `v10` bytes. The
translation side still admits only `name_mention`, and its ambiguous-mention
exposure stays off by default. Because the same request can now yield a
different report, stored `v10` resolution contexts and saved manuscript
results go stale by the same freshness contract and are re-reviewed.

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
process. Occurrence-scoped references (`single_char_name_mention` when unmixed,
`scoped_skill_stem`, and any paragraph-gated derived reference whose surface
also appears unadmitted elsewhere in `locked_text`) are separated from
`Dictionary term references:` into a `Scoped term references:` block rendered
with deduplicated single-line local context snippets (`ONLY at:` / `NOT at:`).
On `zh->en`, when an admitted single-character CJK proper name
(`resonator`/`speaker`) coexists in `locked_text` with at least one unadmitted
raw occurrence of the same character (`raw_other_count > 0`), the admitted
spans are instead selectively locked in place with opaque placeholders (so
`Locked terms:` exposes only the English proper name, never the Chinese source
character) and removed from reference tables to prevent negative-priming
leakage onto ordinary prose or compounds. One exception keeps the mention as an
occurrence-scoped reference: a longer **global** reference that contains the
same source character and whose target contains the mention's official target
as a case-insensitive whole word (`心` beside `心月狐 = Hsin the Moon Fox`,
`「心」 = "Hsin"`, `心团子 = Hsin Cube`). That reference already shows the model
the same official word, so locking would only add a second copy for the model to
reproduce (see ADR 0017's Step B1b refinement 2 and the `duplicate_placeholder`
evidence from `q8` Stage 1 Run #1). Remaining references (including
`scoped_skill_stem`) have their `(model_start, model_end)` coordinates
re-aligned after the inserted placeholders and validated fail-closed. Any
colliding `ONLY at:` / `NOT at:` snippets automatically escalate through wider
same-line windows, full natural paragraph, `[P{n}]` paragraph prefixes, and
`[start,end)` scalar coordinates until strictly disjoint. Compression to
`(all occurrences in this text)` is applied only when zero other raw
occurrences of the surface exist anywhere in `locked_text` (including spans
covered by longer global terms).

## Context fidelity

When a request opts into `translation-context-v1` (`context is not None`), the
context rules also preserve numeric shorthand such as `3+1` or `S0R1`, resolve
omitted actors from the clause without turning rhetorical addressees into
actors, and require every clause to be translated. Previous-segment context
remains read-only and human precedents retain occurrence scope.
