# Usability judging instructions (ADR 0020)

These are the instructions given, unchanged, to every judge and adjudicator
agent. They are frozen with ADR 0020; their SHA-256 is in the evaluation
ledger. Rubric and schema: [ADR 0020](../adr/0020-held-out-usability-evaluation.md).

## For a judge (passes `j1` and `j2`)

You judge machine translations of Wuthering Waves texts, one paragraph per
item. You work alone. You do not know, and must not try to find out, which
system, version or run produced a translation; items carry random ids and are
shuffled. Do not look at other judgments, other files, the internet or any
translation tool. Use only the packet.

Each packet item gives you: the whole source document, the source paragraph
being judged, its translation, whether that translation is the paragraph's own
output (`translation_scope: "paragraph"`) or the output of a whole segment of
several paragraphs (`"segment"`; then find the part that translates this
paragraph yourself — if none does, that is an omission), the official
reference paragraph and reference document when they exist, and the list of
official-name occurrences in the source paragraph with their accepted
official forms.

Judge the paragraph as a reader of the target language who needs it to be
correct:

1. **Critical errors.** List every one with its type and count:
   `omission` (source content missing), `addition` (content not in the
   source), `meaning_shift`, `reference` (wrong subject, object or referent),
   `number` (number, quantity, unit, date, version), `negation` (lost, added
   or moved). The reference translation is evidence of meaning, not the only
   correct wording; a different but faithful rendering is not an error.
2. **Names.** For every listed occurrence give `label`:
   `official` (rendered with an accepted official form; inflection, case and
   surrounding punctuation do not matter), `not_official` (rendered another
   way), `omitted`, or `undecidable`. Give `rendering` = the text the
   translation uses for it, in base form (singular, no possessive), or null
   for `omitted`/`undecidable`. Set `misleading: true` when a
   `not_official` rendering would make a reader believe something wrong
   (another character, item or place; a wrong mechanic).
3. **Naturalness.** `natural`, `stiff` (readable but clearly translated),
   `disfluent` (hard to read, ungrammatical), or `undecidable`.
4. **Grade.**
   - `A`: usable as is. Requires no critical error, no misleading name, and
     naturalness `natural`.
   - `B`: needs only light polishing (wording, word order, punctuation,
     style). No critical error, no misleading name, naturalness not
     `disfluent`.
   - `C`: needs substantive change. Always C with any critical error, any
     misleading name, or disfluent text.
   - `undecidable`: you cannot decide; say why.
5. **Reason.** Required for C and undecidable: name the errors briefly. Do
   not copy long passages.

Output one JSON file:
`{"schema": "usability-judgment-v1", "pass": "<j1|j2>", "records": [...]}`
with one record per item of your packet, exactly in this shape:
`{"item": "...", "grade": "...", "critical_errors": [{"type": "...", "count": 1}], "naturalness": "...", "names": [{"occ": "...", "label": "...", "rendering": "..." , "misleading": false}], "reason": "..."}`.
Every listed occurrence appears once in `names`; none other.

## For an adjudicator (pass `adj`)

You receive items on which the two judges differed, each with both judgments
and their reasons. Apply the same rubric from scratch and write one complete
record per item with pass `adj`, including a reason that says which point of
disagreement you decided and why. You may grade `undecidable` when the
paragraph truly cannot be decided; such items go to the owner's spot check.
Do not write a record for any other item.
