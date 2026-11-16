# Source coverage and occurrence-scoped translation references

This supplement explains the candidate mechanism following
[ADR 0017](adr/0017-contextual-term-references.md) and
[ADR 0019](adr/0019-document-context-and-human-precedents.md). It does not
replace their recorded decisions. Model translation quality still requires
the frozen real-model comparison; offline tests establish source extraction,
admission and the request the model receives.

## Missing source records

At the pinned WutheringData commit
`9218d612ad815e398e064e577e42aaf878899968`, the Chinese and English
`Textmaps/*/multi_text/MultiText.json` files contain the complete
`Advertising_VersionTitle_<number>` family. Only the currently curated title
entered the dictionary. The builder now imports the family by its key shape,
skips already curated keys, and rejects missing or empty sides. For example,
`Advertising_VersionTitle_34` is `未选择的梦 / The Dream Not Dreamed`.
These additional titles are contextual references in either direction, not
sentence replacements. The original source keys remain their provenance.

The four-tier material extractor previously dropped a complete family when
either base surface already named something else. This lost
`调谐器 / Tuner` because `Speaker_1513_Name` already supplies
`调音师 / Tuner`. The item rows 36000011–36000014 independently establish
the four material tiers. A complete family now loses only an identical pair,
not a different meaning of one surface. The derived row retains its
`tier-base:<smallest Basic row id>` provenance.

An established record remains the deterministic translation primary when a
derived record shares its surface. On the model path, competing pairs are
alternatives supplied together as references, never a forced replacement.
Dictionary lookup can expose both records. No new pair is inferred from a
manuscript's expected translation.

## Casing is evidence for a candidate, not a forced meaning

The prior hard-lock spelling set remains separate. Derived names may gain
lowercase and uppercase soft spellings on the model path only when the
submitted segment already contains dictionary term context. This supplies
`sealed tubes` in a game-material guide without adding hints to isolated
ordinary prose such as `the energy core of a star` or `a guitar tuner`.
The existing exact/plural owner wins, different exact spellings sharing a
folded key block synthesis, and both free-text exclusion lists still apply.
These hints still require a semantic comparison with ordinary-word controls:
co-occurrence with a game term alone cannot establish an official sense.

## A one-character name does not name every compound containing it

ADR 0017 widened single-character references so `欢迎椿` could identify
Camellya. The ordinary-word lexicon veto alone also admitted the character
inside novel compounds absent from that lexicon. In addition, one valid
character mention created an unscoped `椿 = Camellya` hint for the whole
segment, including other occurrences with a different meaning.

Source admission now requires a plausible right word boundary: the existing
name grammar, a following known lexicon word, or a non-CJK boundary. An
unknown CJK continuation or a middle-dot continuation does not establish a
standalone name. Thus `欢迎椿来到` and `快找椿商量` remain admitted, while
the component in `红椿蕊` or `红椿·蕾` does not. The existing ordinary
compound and everyday-character checks still run first. This is a boundary
rule for all single-character candidates, not a list of mechanic exceptions.
Rare legitimate continuations may also be withheld; that is a regression
risk to measure rather than assume away.

References now carry occurrence ranges through the plain, synchronous,
asynchronous and protected-text paths. Ranges use zero-based, end-exclusive
scalar coordinates in the actual user message, after term and structural
placeholders. The prompt limits a single-character reference to those
ranges. A valid character mention in the same segment therefore does not
grant the hint to every embedded component. The compound's official full
translation is not invented when no independent pair is established.

Translation and review use the same source admission helper, so
`MATCHER_REVISION` advances from v7 to v8. Old saved manuscript-v3 results
become stale through the existing matcher freshness contract.
`RULE_VERSION_V2` remains the wire literal `review-v2`. Evaluation must use
the new matcher key; semantic similarity does not authorize reusing outputs
from an older key.

## Context fidelity

The opt-in document-context prompt already asked for preserved negation,
numbers and referents. It now states how to handle omitted actors and
rhetorical addressees, asks for idiomatic and ironic meaning in full-clause
context, and requires modifiers and clauses to be translated completely.
This is a general instruction change, not a canned translation of an
evaluation paragraph. Previous-segment context remains read-only and human
precedents retain occurrence scope. Its semantic benefit is unconfirmed
until the independent evaluator runs the frozen comparison.
