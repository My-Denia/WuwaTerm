# ADR 0019: Document context and human precedents for model calls

- Status: Accepted (contract); the translation-context engine is not part of
  this change
- Date: 2026-11-10
- Supplements: [ADR 0017](0017-contextual-term-references.md). The reference
  mechanism of ADR 0017 is unchanged.
- Related: [ADR 0018](0018-semantic-review.md),
  [segmentation-v1](../segmentation-v1.md)

## Problem

Long texts are translated in independent chunks today. Each chunk is sent
without the text before it, so a pronoun or an elided subject loses its
referent. When a line is longer than the chunk limit it is cut hard and the
pieces are joined with `\n`, so the output gains a line break the source never
had (`application.split_plain_text` and the final `"\n".join`).

Users also make decisions while reviewing a manuscript: "this `椿` is the
character", "this `Encore` is applause, not the resonator", "render this
name as X". Those decisions are lost on the next model call.

## Decision

1. **Segment, keep separators.** Long texts are cut with the single-side
   [segmentation-v1](../segmentation-v1.md) rule: paragraphs first, then
   review-v2 sentences, then a marked hard cut. Each segment's leading and
   trailing whitespace is a separator copied verbatim; only the core is
   translated. The output is `Σ (leading_i + translate(core_i) + trailing_i)`,
   which also removes the extra line break of the current hard-cut path.
2. **Read-only context.** Each segment carries the previous segment's source
   and translation, at most 400 scalars each, marked read-only. Nothing else
   crosses segments.
3. **Human precedents, scoped to one occurrence.** A precedent is
   `{surface, choice, rendering, snippet, anchor, scope}`:
   - its source is a human action only: an `official_pair` or `not_a_term`
     choice the user made in the manuscript, or a rendering the user accepted
     or adjusted (`user_rendering`);
   - `anchor` names where it was made: the segment index and the source range
     in that segment;
   - `scope` has exactly one value, `occurrence`, matching what the Web
     manuscript's `makeChoice` already records. There is no "whole document"
     option;
   - at most 12 per request; a client prefers the same surface and the
     nearest occurrence;
   - the prompt wording is fixed: "The following are human judgments made at
     one earlier place. They state the meaning and rendering at that place
     only. The same surface may mean something else in this segment; judge it
     from this segment's context, and reuse the rendering when the meaning is
     the same."
4. **No consistency memory from automatic matches.** Core does not turn
   dictionary matches or review results into precedents, and does not build a
   cross-segment sense memory. An automatic match is a surface match; carried
   forward it would spread one place's name-versus-ordinary-word decision into
   contexts where it is wrong.
5. **Opt-in on the wire.** `POST /v1/translations` gains an optional
   `context: {before, precedents}` with the same shapes and limits as
   semantic review, and an unchanged response. Older servers silently ignore
   unknown translation fields, so a client sends `context` only when
   `GET /v1/capabilities` lists `translation-context-v1`. The `context` model
   refuses unknown keys. This field ships with the engine, not with the
   contract change that introduces this ADR.
6. **Precedents are off by default until the M′ gates pass** (below). A client
   keeps the option; by default it sends no precedents.

## Re-review of manuscript decisions

A decision on a semantic issue is bound to an occurrence and a text version,
not to a fingerprint. The fingerprint only finds candidates. A previous
`ignored` decision carries over automatically only when the source span and
the target span can both be recovered uniquely with the manuscript's existing
`recoverSpan` rule and exactly one new issue with the same fingerprint sits at
the recovered spans. `accepted` and `adjusted` decisions already changed the
text; they move to history and never carry over. Anything else is shown as
"needs reconfirmation" with the previous decision as reference.

## Pre-frozen acceptance criteria (translation context)

Frozen before the first call to the product model gateway, under the shared
rules of [ADR 0018](0018-semantic-review.md#pre-frozen-acceptance-criteria)
(three states, reruns, budget, scope of the conclusion). The M layer compares
the current and the new translation path on the owner's private monolingual
texts (B1–B3, plus the Chinese side of A1 and A3 translated as monolingual
manuscripts, with the official English as a reference, not as gold). A blind
annotator labels every occurrence that matters: official concept or ordinary
sense, negation, number, reference.

- **Official terms, per occurrence:** on occurrences labelled "official
  concept", the new path is correct at least as often as the old one, and at
  most 1 occurrence is right in the old path and wrong in the new one.
- **Ordinary sense, scored separately:** on occurrences labelled "ordinary
  sense", the new path's errors drop by at least 50% against the old path, or
  to at most 1 in absolute terms.
- **Minimum samples:** at least 30 "official concept" and at least 10
  "ordinary sense" occurrences. If short, monolingual paragraphs from the
  Chinese side of the A groups are added before the first call and frozen; if
  still short, the gate is undecidable. Thresholds are not loosened.
- **Reference, negation and number:** errors in total not more than the old
  path.
- **M′ precedents:** from M-layer segments where a surface occurs at least
  twice, the gold judgment of an earlier occurrence becomes a precedent
  (`scope: occurrence`), and the later segments run with and without it. Only
  later occurrences are scored, with two denominators reported separately:
  N_same (gold sense equals the precedent's) and N_diff (it differs).
  - Gates: on same-sense occurrences, errors with the precedent are not more
    than without; on different-sense occurrences, occurrences led astray by
    the precedent are 0.
  - Minimum samples: N_same ≥ 10 and N_diff ≥ 5, assembled and frozen from
    the M layer and the Chinese side of the A groups before the first call
    (homographs such as `椿` and `安可` are different-sense sources).
  - If either is undecidable, or either gate is not met, precedents stay off
    by default (decision 6) with no owner acceptance needed. Only when both
    have enough samples and both pass are precedents switched on by default.
- **Separator fidelity:** every M-layer output rebuilt with segmentation-v1
  has the source's whitespace structure; the automatic check passes 100%.
- `verified_constraint` counts are reported as a descriptive diagnostic only,
  never as a gate.
- **G:** every regression case stays correct.

## Consequences

- Telegram long texts follow the same core path and gain the previous-segment
  context; nothing else changes for them.
- Clients without a dictionary cannot avoid a hard cut through a term surface;
  such segments are marked `hard_cut` and the UI says the boundary may be
  judged wrongly ([segmentation-v1](../segmentation-v1.md#accepted-risk)).
- A precedent can still mislead the model on a homograph. The fixed wording,
  the occurrence scope and the default-off rule until N_diff passes are the
  three guards.

## Evidence

- `src/wuwaterm/segmentation.py`, `tests/test_segmentation.py`,
  `scripts/build_segmentation_fixtures.py`,
  `contracts/segmentation-v1.json`
- Precedent request shape: `SemanticPrecedentBody` in
  `src/wuwaterm_api/app.py`, `docs/api/openapi.json`
