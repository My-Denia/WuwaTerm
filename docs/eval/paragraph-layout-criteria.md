# Paragraph layout of context-mode translations: frozen criteria

- Status: frozen before any code change (MY-231). Not edited afterwards; a
  change needs a new entry that says what changed and why.
- Date: 2026-11-16
- Related: [ADR 0019](../adr/0019-document-context-and-human-precedents.md)
  (context mode keeps blank lines in the text sent),
  [segmentation-v1](../segmentation-v1.md),
  [translation-latency-criteria](translation-latency-criteria.md)

## Problem

A context-mode request (`translation-context-v1`) sends a segment with its
blank lines kept, but the model decides the layout of the answer. In the
second end-to-end run, a 45-paragraph Chinese guide (D-003, one segment)
came back with all 45 lines but only 31 paragraphs, so the Web manuscript
fell back to one manually split pair. An earlier run returned 8 paragraphs.

Stored runner outputs of the earlier sample documents show the same pattern:

| Run | Segments | Paragraph count differs | Of those, same line count |
| --- | --- | --- | --- |
| D baseline (`omit`) | 14 | 0 | – |
| D `disabled` | 17 | 0 | – |
| R `disabled` | 34 | 3 | 2 |
| R `low` | 34 | 4 | 2 |
| R per-direction | 34 | 3 | 2 |

Here a paragraph is a region between blank lines (segmentation-v1), and a
line is a non-empty line.

## Change under test

A post-check after the model answer, on the context path only: when the
answer's paragraph count differs from the source's, but its non-empty line
count equals the source's, the answer's lines are regrouped into the
source's paragraph layout. The lines and their order are kept; only the
blank lines between them change. When the line counts differ, the answer is
returned unchanged, with no second call, because a retry would double the
wait on the path that already runs into the model timeout. The request sent
to the model does not change.

## Criteria

- L1 request identity: with the change, every request body for the D and R
  inputs is byte-identical to `main` (`d54957c`), compared with an echo
  gateway and a fixed placeholder prefix. Request size and latency are
  therefore unchanged (bound: 0 bytes).
- L2 preservation: replaying the post-check on every stored runner output
  above and on the end-to-end D-003 answers (31 and 8 paragraphs), the share
  of segments whose answer has the source's paragraph count is at least the
  share without it, in every run, and the 31-paragraph D-003 answer comes out
  with 45 paragraphs.
- L3 content: in every regrouped answer, the sequence of non-empty lines,
  with surrounding whitespace removed, is identical before and after.
- L4 unchanged otherwise: an answer whose paragraph count already matches,
  or whose line count differs, is returned byte-identical.
- L5 guards: M-Gold under the frozen cue list, Set 1 standing terms and Set 2
  terms, rescored on the R outputs with the post-check applied, equal the
  scores of the same outputs without it. D usable cannot change: no D answer
  is regrouped (L2's table shows 0 paragraph-count differences on D).
- L6 tests: unit tests for the regrouping (equal line counts, unequal line
  counts, already matching, single-paragraph source with an answer split
  into paragraphs) and one test through `translate_async` with a mock model
  that drops blank lines, on synthetic text.

No model call is needed: the requests are unchanged (L1), so the stored
answers are what the gateway returned for them, and the post-check is a
deterministic function of each answer and its source.

## Results and one premise that holds only on this data, 2026-11-16

All of L1–L6 hold. No model call was made: on these inputs the requests are
unchanged (51/51 bodies; 0 of 5 next-segment contexts change), so the stored
answers are what the gateway returned.

Scope: the post-check runs wherever `translate_async` runs with a context.
That covers API requests that send `context`, and every segment of the
long-text path (`_translate_long_plain_text`), which always passes a
context, including Telegram trusted messages over 2,000 characters. Calls
without a context are unchanged: short Telegram, API and Web requests,
channel posts, HTML and no-model translation.

- L1: 51 of 51 request bodies (D 17, R 34) are identical to `d54957c`. This
  holds by construction for each segment's own request: the post-check runs
  after the answer, and the echo gateway's answers never trigger it.
- L2: paragraph-count matches go from 31 to 33 of 34 segments in the
  `disabled` and per-direction R runs and from 30 to 32 in the `low` run.
  They stay at 14/14 (D baseline), 17/17 (D `disabled`) and 8/8 (D
  per-direction, zh→en documents only, a run not in the Problem table: 8
  segments, 0 paragraph-count differences). Both end-to-end D-003 answers
  (31 and 8 paragraphs, 45 lines each) come out with 45 paragraphs.
- L3 and L4 hold for all 8 regrouped answers and every other segment.
- L5: M-Gold, Set 1 and Set 2 are equal before and after on all three R runs
  (664/120/38, 671/119/38, 667/120/38). This holds by construction: the
  regression scorer maps translations by non-empty lines, which the
  post-check never changes.

"The request sent to the model does not change" holds for each segment's own
text but not in general for the next segment: `_translate_long_plain_text`
gives the next segment the end of this segment's translation as read-only
context, so a regrouped answer whose changed blank lines fall within that
tail changes the next request. On the D and R inputs none did: 0 of the 5
regrouped segments that have a following segment changed its context
(evidence: the local `downstream_context.py` replay, hashed on MY-231). The
change is blank lines only.

## Amendment: conservative regrouping guard, 2026-11-19 (MY-359)

The post-check above is unchanged in what it publishes for a dropped blank
line. Two guards now decide when *not* to regroup, because an answer can keep
the source's line count and still no longer answer line for line.

- A line whose text merged with its neighbour and another line that split in
  two produce equal line counts. Grouping such an answer into the source's
  paragraphs moves real content across a paragraph boundary. The answer is
  therefore left alone when the two sides' running shares of the text diverge
  by more than 7.5%, or when a line whose share grew by half (a local peak its
  source line was not) is followed within six lines by one whose share fell by a
  third (a local trough its source line was not). Both bounds sit far outside
  ordinary variation: on synthetic faithful line-by-line answers with ±35%
  per-line length noise the drift stays under 5% at p99 and neither signal
  fires, while a merge+split reaches 8% or more. `source = "a1\na2\n\nb1\nb2"`
  against `answer = "A1 A2\nB1\nB2x\nB2y"` is rejected by that rule.
- Two layouts whose paragraph breaks cross — one keeps a break the other drops
  before dropping one of its own — cannot be reconciled by any grouping of the
  answer's lines, so the answer is left alone too.

Every guard returns the answer byte-identical, so a rejected regrouping can
only leave the model's own blank lines in place; it never moves text.

Replay (`python scripts/verify_paragraph_regroup.py`), on stored artifacts
under `goal-runs/`, none of them sealed:

- L4: all 15 stored D-003 answers in
  `names-critical-batch-20261116/evaluator/*/raw_outputs.json` already have the
  source's 45 paragraphs and come back byte-identical, and so do the 30 stored
  R answers in the same tree (their paragraph counts match their source's, or
  their line counts differ, which the post-check already leaves alone).
- L2/L3: the stored 31-paragraph and 8-paragraph D-003 answers the frozen
  criteria describe are not present in this workspace (the recorded runs are
  `run_failed`, and every stored D-003 answer has 45 paragraphs), so their
  layout is reproduced from a stored 45-paragraph answer by removing blank
  lines: 45 lines, 31 and 8 paragraphs. Both regroup to the source's 45
  paragraphs with the identical line sequence.
- Guards: the merge+split and crossing-break cases are returned unchanged.

The reproduction is not a stored answer: only the blank-line layout is
reproduced, from real stored translation text. A future run that stores a
31-paragraph or 8-paragraph answer should replace the reproduction, and the
script will pick it up as an L4 case only if it is stored with its own layout.
The six regrouped R segments recorded in the original criteria table are not in
this workspace either; the 30 stored R answers here need no regrouping, so the
replay can only hold them to L4.
