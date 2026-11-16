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

All of L1–L6 hold. No model call was made.

- L1: 51 of 51 request bodies (D 17, R 34) are identical to `d54957c`.
- L2: paragraph-count matches go from 31 to 33 of 34 segments in each of the
  three R runs, and stay at 14/14, 17/17 and 8/8 on D. Both end-to-end D-003
  answers (31 and 8 paragraphs, 45 lines each) come out with 45 paragraphs.
- L3 and L4 hold for all 8 regrouped answers and every other segment.
- L5: M-Gold, Set 1 and Set 2 are equal before and after on all three R runs
  (664/120/38, 671/119/38, 667/120/38).

"The request sent to the model does not change" holds for each segment's own
text but not in general for the next segment: `_translate_long_plain_text`
gives the next segment the end of this segment's translation as read-only
context, so a regrouped answer whose changed blank lines fall within that
tail changes the next request. On the D and R inputs none did (0 of 5
following contexts changed). The change is blank lines only.
