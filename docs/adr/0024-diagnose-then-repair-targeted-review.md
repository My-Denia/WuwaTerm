# ADR 0024: Diagnose-then-repair layered review with targeted second-pass verification

- Status: **Proposed (Layer 1 accepted under ADR 0022; candidate reader and the
  opt-in hint gate implemented and off by default)**
- Date: 2026-11-26
- Related: `MY-490`, `MY-228`, `MY-504`,
  [ADR 0018](0018-semantic-review.md),
  [ADR 0019](0019-document-context-and-human-precedents.md),
  [ADR 0020](0020-held-out-usability-evaluation.md),
  [ADR 0021](0021-local-user-termbase.md),
  [ADR 0022](0022-deterministic-structure-checks.md),
  [ADR 0023](0023-local-translation-memory.md),
  [Number attribution candidates](../eval/structure-attribution-candidates.md)

## Problem

`MY-228` measured what whole-segment semantic review costs and returns on
official pairs: medium/high false positives went 39 → 31 per direction (zh→en)
and 32 → 26 (en→zh) when the prompt and thinking effort were already tuned,
against a gate of ≤ 8.6 per direction. Two runs of tuning moved the number by
the same amount as one rerun's noise, so more tuning was not the answer, and the
owner accepted semantic review as experimental and off by default (ADR 0018).
Critical-error recall met its gates; the noise did not.

Meanwhile Layer 1 (ADR 0022) answers the mechanical questions for free and
misses exactly one class: which number belongs to which stat. On the committed
number fixture it finds 0 of 6 attribution swaps, because pairing by position
would raise a false alarm on every reordered sentence.

So the choice is not "review everything with a model, or nothing". A model is
needed only where a deterministic reader can point at something specific, and
what the model is asked for should be diagnosis, not a rewrite.

## Decision

Review becomes five stages, each answering a narrower question than the one
before it. A response the client shows is still assembled from the existing
`semantic-review-v1` finding categories; this ADR adds no category and no rule
id.

### Stage 1 — L1 deterministic checks

`POST /v1/structure-checks` (ADR 0022) runs with no model, no storage and no
network: numbers present, placeholders and markup intact, paragraphs and lines
not merged or dropped, target not left untranslated. Its findings are
reproducible and are shown as they are today.

### Stage 2 — risk candidates

Deterministic readers that produce *candidates*, not findings: "look here",
with the evidence that made them say so. `attribution_candidates` is the first
of them: it names the stat slots both sides write, the values each side puts on
them, and only when both sides name the same two or more slots with the same
set of values does it report a slot whose value disagrees
([measurement](../eval/structure-attribution-candidates.md)).

Candidate properties, all enforced in code:

- **No claim.** A candidate has no rule id, no span and no verdict. It cannot
  appear in a structure-check response or in a response body at all.
- **Bounded.** Both sides are capped at 2,000 scalars
  (`MAX_SIDE_SCALARS`), and the hint carries at most eight slots with four
  values each (`MAX_SLOTS`, `MAX_VALUES_PER_SLOT`), so it can be put in a
  prompt without growing with the input.
- **Pure.** No model, no storage, no network
  (`src/wuwaterm/attribution_candidates.py`).

### Stage 3 — targeted L2 verification

One model call, and only for a segment that has at least one candidate. The
prompt carries the candidate as read-only evidence with fixed wording
(`ATTRIBUTION_HINT_WORDING` in `src/wuwaterm/semantic_review.py`), which says
it is program-computed surface evidence, that a restructured sentence can raise
it while the translation is right, and that reporting nothing is correct when
the attribution reads correctly. Diagnose first, report second: the model is
never asked to fix anything at this stage.

A segment with no candidate produces no call from this stage. That is the
saving: the model sees the percentages that were probably swapped, not every
segment of every document.

### Stage 4 — bounded minimal repair proposal

When the model does report a `quantity_change`, the repair is the smallest edit
that resolves the quoted span: `target_span` plus `replacement`, at most 100
characters (`semantic-review-v1`'s existing suggestion contract), with
`term_effect` computed by rerunning `review-v2` on the applied copy. The client
previews it and can undo it; nothing is applied server-side, and the proposal
never rewrites a whole segment.

### Stage 5 — human confirmation and incremental recheck

A human accepts, edits or rejects the proposal. Accepting a segment is a
confirmation and feeds ADR 0023's memory; the recheck after a repair runs only
on the touched segment and its neighbours, not on the document. Rejections are
recorded as precedents (ADR 0019) rather than re-proposed on the next pass.

### Full semantic review stays available

The whole-segment path (`POST /v1/semantic-reviews`, ADR 0018) is unchanged and
stays experimental and off by default. The layered path is an addition for
operators who turn it on; it never replaces, hides, or alters that endpoint.

## Invariants

1. **Default-off, byte-identical when off.** The hint is gated by
   `WUWATERM_SEMANTIC_REVIEW_ATTRIBUTION_HINTS`; only `1`, `true` or `yes`
   turn it on. With it off, `build_messages` produces exactly the messages it
   produced before the gate existed — pinned by a SHA-256 test over both
   messages.
2. **No wire change.** No request field, no response field, no OpenAPI change,
   no new category. A confirmed attribution problem is reported as the
   existing `quantity_change`.
3. **Layer 1 keeps its answer.** The structure-check endpoint gains no
   attribution rule; a client that only calls it sees exactly what it sees
   today.
4. **A hint is never a finding.** Nothing in the pipeline turns a candidate
   into a finding without a model and a human; the hint cannot carry an
   instruction because its values are numbers and its slot names come from a
   fixed table.
5. **Bounded cost.** One model call per candidate-bearing segment, no retry,
   inside the existing semantic-review timeout and budget.

## Implemented today

| Piece | Where | State |
| --- | --- | --- |
| Candidate reader | `src/wuwaterm/attribution_candidates.py` | Implemented, pure, typed, bounded |
| Benchmark | `scripts/eval_structure_checks.py attribution`, `tests/fixtures/structure-attribution.json` | 21/21 swap recall, 0/33 negative-bucket candidates, slowest adversarial input 2.35 ms |
| Official corpus | 27,385 zh-Hans/en pairs of `data/9218d61`, both directions | 54,770 runs, 2 candidates (one pair), 0.0037% |
| Opt-in prompt hint | `WUWATERM_SEMANTIC_REVIEW_ATTRIBUTION_HINTS` in `src/wuwaterm/semantic_review.py` | Implemented, default off, byte-identical off, prompt-injection tests |
| Tests | `tests/test_attribution_candidates.py` | 24 tests over the reader and the gate |
| Target selection, repair preview, incremental recheck | — | Not implemented |

## Pre-registered acceptance metrics

Fixed before any run, measured per direction on frozen sets, each compared with
the same model doing today's full review on the same segments. No live run has
been made: the numbers below are the gates a run must meet, not results.

| # | Metric | Definition | Gate |
| --- | --- | --- | --- |
| 1 | Critical-error recall | Share of injected critical errors (numbers, negation, placeholder, term sense) the layered path reports, on the frozen injection set | ≥ the full-review baseline on the same set; a drop fails the design |
| 2 | Precision on official pairs | Medium/high findings on official translations per direction, on the `MY-228` official sample | > full review, and inside the `MY-228` gate of ≤ 8.6 per direction |
| 3 | Repair-introduced error rate | Share of accepted proposals that a blind second reviewer marks as a new error (wrong value, broken markup, changed term) | ≤ 2% of accepted proposals |
| 4 | Issues per 1,000 characters | Reported issues divided by submitted characters on the same corpus | < full review, with metric 1 held |
| 5 | Human edit time per 1,000 characters | Wall-clock time for one editor to clear a fixed sample, with and without the hint | ≤ full review's time on the same sample |
| 6 | Latency and model calls per 1,000 characters | Calls issued and p50/p95 latency per 1,000 characters | Fewer calls than full review; per-call latency inside the existing `semantic-review-v1` timeout |

The five gates recorded in the
[attribution measurement](../eval/structure-attribution-candidates.md#gates-before-any-rollout)
— real-model attribution, no new alarm on natural pairs, no finding outside
`quantity_change`, bounded cost, silence when off — stay as the stage-specific
version of metrics 1, 2, 4, 6 and the invariants above. A metric that is not
measured is reported as unmeasured, not as passed.

## Non-goals

- **Not a new finding category.** Attribution problems stay `quantity_change`;
  a new category would break clients that validate the published key sets.
- **Not a rewrite engine.** Stage 4 proposes a bounded replacement of a quoted
  span; it never regenerates a segment or a document.
- **Not "review everything with the strongest model".** The point of the
  layered path is that a segment with no candidate needs no call.
- **Not a replacement for Layer 1 or for full review.** Both stay, unchanged
  and independently switchable.
- **Not automatic.** No proposal is applied, saved, or memorized without a
  human confirming it.

## Consequences

- The model's job shrinks from "read everything and judge" to "given these two
  percentages and the stat names they sit on, is one on the wrong stat". That is
  the shape `MY-228`'s noise came from the absence of.
- The prompt, the response and the cost of a default request do not change,
  which is what lets the layered path be developed and measured while semantic
  review stays off.
- A wrong candidate costs one model call and one human look, not a wrong fix:
  the hint says it may be a false alarm, and the repair still needs a human.
- The gates above are the acceptance test for turning the flag on anywhere; a
  default-on change needs its own decision, not this ADR.
