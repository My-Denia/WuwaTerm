# H3 judging addendum: community terms

These instructions are given, unchanged, to every `H3` judge and adjudicator
together with [usability-judging.md](usability-judging.md), which stays as it
is. Where this addendum says nothing, `usability-judging.md` applies. The
addendum is frozen with the [H3 protocol](h3-and-raw-baseline-protocol.md)
(§5.3); its SHA-256 is in the protocol's §0 and in `h3-ledger.json`.

## What a packet item adds

An item of a `guide` or `mixed` paragraph may list **community-term
occurrences** next to its official-name occurrences. A community term is an
entry of the frozen `H3` community lexicon: build and rotation shorthand
(`3+1`, `S0R1`, `2命`), mechanics and stat words, combat roles, and player
slang. Each listed occurrence gives its source surface, its accepted
renderings in the target language (`accepted_forms`) and, when the term also
has an official game form, that form (`official_forms`). Only listed
occurrences are labelled; any other word is judged as ordinary text.

## For a judge (passes `j1` and `j2`)

Judge the paragraph exactly as `usability-judging.md` says. In addition, give
every listed community occurrence a `label`:

- `official`: rendered with one of its official forms.
- `accepted`: rendered with one of its accepted forms, or with another wording
  that a player reading the target language takes for the same term (the same
  mechanic, role, unit or count). The accepted list is evidence, not the only
  correct wording.
- `not_official`: rendered another way, for example a literal translation
  that loses the game meaning, a different community word, or an unexplained
  transliteration. The label keeps the scorer's name; for a community term it
  means "not accepted".
- `omitted`: the occurrence is not translated.
- `undecidable`: you cannot decide; say why in `reason`.

Give `rendering`, the text the translation uses for the occurrence in base
form, or null for `omitted` and `undecidable`. Set `misleading: true` only
with `not_official`, and only when the rendering would make a reader believe
something wrong: another character, item or role, a different mechanic, or a
different count or level (`S0R1` read as a different investment level, `3+1`
read as a different team shape). A misleading community term makes the grade
`C`, like a misleading official name.

A number inside shorthand that comes out wrong is also a `number` critical
error; record both. An omitted occurrence whose content is missing from the
translation is also an `omission` critical error.

## Output

One JSON file per pass:
`{"schema": "h3-judgment-v1", "pass": "<j1|j2>", "records": [...]}`. Each
record has the shape `usability-judging.md` gives, plus
`"community_names": [{"occ": "...", "label": "...", "rendering": "...", "misleading": false}]`.
Every listed community occurrence appears once in `community_names`; none
other. An item without community occurrences may leave the field out.

## For an adjudicator (pass `adj`)

As in `usability-judging.md`. A disagreement on any community `label`,
`misleading` flag or normalized `rendering` also sends the whole item to you;
your record includes `community_names` like a judge's.
