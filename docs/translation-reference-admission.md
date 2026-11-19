# Source coverage and occurrence-scoped translation references

This supplement explains the candidate mechanism following
[ADR 0017](adr/0017-contextual-term-references.md) and
[ADR 0019](adr/0019-document-context-and-human-precedents.md), and it does not
set aside the rulings already recorded in them. Only the frozen real-model
comparison can say whether quality improves; the offline suite settles which
source rows exist, which occurrences are admitted, and what request reaches the
model.

## Missing source records

At the pinned WutheringData commit
`9218d612ad815e398e064e577e42aaf878899968`, the Chinese and English
`Textmaps/*/multi_text/MultiText.json` files hold a whole family of
`Advertising_VersionTitle_<number>` keys. Earlier the dictionary carried just
the one hand-picked member. The builder now takes every member whose key has
that shape, keeps curated keys untouched, and refuses a member whose side is
missing or blank. Such a row is a reference whichever direction a request
goes, never a fixed substitution: it names a release, and the same wording turns
up in prose. Provenance stays the original source key.

The four-tier material extractor used to skip a whole family as soon as one of
its two base surfaces was already taken by a different meaning. That is what
dropped
`调谐器 / Tuner` because `Speaker_1513_Name` already supplies `调音师 / Tuner`.
The item rows 36000011–36000014 independently establish the four material tiers.
A complete family now loses only an identical pair, not a different meaning of
one surface. The derived row keeps its `tier-base:<smallest Basic row id>`
provenance.

An established record remains the deterministic translation primary when a
derived record shares its surface. On the model path, competing pairs are
alternatives supplied together as references, never a forced replacement, and
a surface with more than one distinct pair is never hard-locked. Dictionary
lookup can expose both records. No new pair is inferred from a manuscript's
expected translation.

## One reference line per meaning

Several candidate tables can list one name, always pairing the same two
strings and differing only in the category column. The reference list collapses
those rows per surface by `(zh, en)`, keeping the highest-priority category, so
the model gets one line per meaning rather than one per row. Only more than one
distinct `(zh, en)` pair makes a surface ambiguous; three rows of a single pair
stay one meaning.

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

Source admission now requires a plausible right word boundary as well: the
existing name grammar, a following known lexicon word, an honorific
(`大人`, `小姐`, `先生`, `女士`, `前辈`, `阁下`, `老师`, `队长`, `长官`), a
one-character function word (`的`, `却`, `等`, `已`, …), or a non-CJK boundary.
An unknown CJK continuation or a middle-dot continuation does not establish a
standalone name. Thus `欢迎椿来到`, `快找椿商量对策`, `椿却没有回应`, `椿等共鸣者`
and `心大人` remain admitted, while the component in `红椿蕊` or `红椿·蕾` does
not. The existing ordinary-compound and everyday-character checks still run
first, so `心情`, `心很重要` and `香椿拌豆腐` stay ordinary words. This is a
boundary rule for all single-character candidates, not a list of mechanic
exceptions. Rare legitimate continuations may also be withheld; that is a
regression risk to measure rather than assume away.

Translation and review call the same helper, so the same character is admitted
or withheld in both. `MATCHER_REVISION` therefore advances from v7 to v8, and
old saved manuscript-v3 results become stale through the existing matcher
freshness contract. `RULE_VERSION_V2` remains the wire literal `review-v2`.
Evaluation must use the new matcher key; semantic similarity does not authorize
reusing outputs from an older key.

## On-demand request scope

The prompt is unchanged unless the request actually needs the extra
explanation. Three things ask for it:

- an admitted one-character name mention, whose reference is limited to listed
  occurrence ranges;
- a selected surface with more than one distinct official pair, which the model
  must choose between by context;
- a soft-cased spelling of a derived name, which is a hint rather than an
  official sense.

A request that needs none of the three — any ordinary sentence, including one
drawing terms from several tables, or naming a derived surface in its official
casing — arrives at the model with the very bytes it carried before this
mechanism existed. `Reference occurrence ranges (JSON; zero-based,
end-exclusive):` is rendered only when an admitted one-character mention
produced ranges.

Ranges use zero-based, end-exclusive scalar coordinates in the actual user
message, after term and structural placeholders, on the plain, synchronous,
asynchronous and protected-text paths. A valid character mention in the segment
therefore does not grant the hint to every embedded component, and the
compound's official full translation is not invented when no independent pair
is established.

## Context fidelity

The opt-in document-context prompt already asked for preserved negation,
numbers and referents. It now states how to handle omitted actors and rhetorical
addressees, keeps numeric shorthand such as `3+1` or `S0R1` verbatim, and
requires every clause to be translated. It does not add the idiom-and-irony
sentence an earlier candidate carried. Previous-segment context remains
read-only and human precedents retain occurrence scope.

## Review attribution

Review-v2 reads the same admission and the same enclosing-form rule, so a
reference the translator withheld is not reported as a missing term either.
Three attribution rules changed with the same batch:

- a derived reference-only surface (教学楼, 能量袋) is `not_evaluated` with
  reason `ordinary_word` instead of a missing-term finding, and therefore
  contributes no semantic-review term fact;
- an inner occurrence of a shorter official form inside a longer official
  dictionary form is not credited to the shorter term, even when the longer
  surface never appeared in the source or resolved to a different pair;
- a mention is credited with an occurrence only when a region-wide assignment
  can place every competing mention on its own non-overlapping occurrence.
  A region that cannot do that marks the competing mentions
  `not_evaluated`/`ambiguous_repeat` instead of crediting whichever mention a
  left-to-right sweep reached first.

Real-model evaluation of this candidate has not been run.
