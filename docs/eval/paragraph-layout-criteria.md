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
  source line was not) sits within six lines of one whose share fell by a third
  (a local trough its source line was not), in either order. Both bounds sit far
  outside ordinary variation: on synthetic faithful line-by-line answers with ±35%
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

## Amendment: full-segment merge+split guard, 2026-11-20 (MY-359)

The 2026-11-19 amendment's guard has two bounds, and both were defeated by a
merge and a split that sit far apart. This amendment replaces them; it does not
change what the post-check publishes for a dropped blank line, and every guard
still returns the answer byte-identical, so a rejected regrouping can only
leave the model's own blank lines in place.

What was wrong:

- The running-share drift bound (7.5%) was compared across the whole segment,
  and a merge plus a split move the boundaries between them by exactly one
  source line's share, which is `1/N`. On 20 equal lines that is 5%, under the
  bound; on 40 lines, 2.5%. The signal shrinks as the input grows while the
  noise it competes with does not: at +/-35% per-line length noise a faithful
  answer already reaches a 5.0% running-share gap at p99 on 45 lines and 9.9%
  on 10. Measured over 10,000 faithful answers per line count, that
  bound alone rejects 7.6% at 10 lines, 0.7% at 20 and 0.01% at 45. A bound
  that low is unusable at either end.
- The split half of the guard was restricted to `index ± 6`. A merge in the
  first line and a split in the last sit 19 rows apart on a 20-row input and
  39 on a 40-row one, out past that window.
- Even within the window, a split in two equal halves produced no local trough
  (each half's neighbour is the other half), and a peak test failed as soon as
  the merged line sat next to a naturally long source line.
- Consequence: a 20-line two-paragraph source whose answer merged lines 1 and 2
  and split line 20 came back regrouped into `[10, 10]`, putting source line 11
  at the end of the first paragraph, and the document reported `ready` in both
  views instead of the manual split the user should have been offered. That is
  the MY-359 report.

What replaced it. Both events are now read from a per-line share ratio,
`(answer line / answer total) / (source line / source total)`, which cancels the
translation's overall expansion and each source line's own length, and which
does not dilute as the segment grows. A merge is a line holding at least 1.4
lines' worth of text and standing clear of the segment's median line by a factor
of 1.3, or a line whose text matches its own source line plus a neighbour's much
more closely (half the gap or less) than its own source line by itself, where
the neighbour is at least three quarters of its length. A split is a line left
with at most 0.55 of its own text, or the two halves of one line split in two,
each at most 0.65 and the two together holding one line's worth (sum within
0.75-1.25). The two are looked for over the whole segment, at any distance and
in either order; the guard refuses when both appear, and only when both appear,
because either one alone is ordinary line-to-line variation.

Bounds, measured rather than asserted. On synthetic faithful answers (10, 20
and 45 lines, source lines 6-40 scalars, +/-35% per-line length noise) the pair
never occurs: no rejection in 50,000 trials at each line count, and the same
0.0000 that `scripts/verify_paragraph_regroup.py` prints from its own 10,000 per
count. A merge plus a split is recognised with the two 19 or 39 lines apart, in
equal halves and in 60/40 halves, with and without 20% translation noise, in
either order, with the merged line next to a source line three times its length
or having absorbed one, and with the merged line a 40/30 pair drawing 20% short.

The split bounds carry the precision, and that is what pays for the loose merge
bound. Lowering the merge bound from 1.5 to 1.4 on its own, against the 0.65
lone-line bound this guard first shipped with, would have refused 0.3% of
faithful 45-line answers: a faithful line at -35% reaches 0.65, so one unlucky
short line plus one unlucky long one satisfied both sides by chance. A line
counts as the halves of a split now only when each half is at most 0.65 and the
two together hold one line's worth, and a lone shrunk line has to reach 0.55,
which no faithful line does at +/-35%.

The residual bound is narrower than "a merge of two very unequal lines", and
worth stating exactly. When the absorbed line is under roughly half the
absorber's length, so that the merged line holds under 1.4 lines' worth, and the
lines after it are of similar length, the merge leaves no signature in the line
lengths at all: no per-line ratio is outside what variation alone produces, and
the content that moved is under a third of a line. On the 40/30 shape roughly
one 20% noise draw in a hundred leaves the merged line under that bound, and an
answer drawn that way is regrouped. The mirror case, a split whose halves are both above 0.65
and together hold more than 1.25 lines' worth, is missed the same way, for the
same reason: nothing in the lengths says a line was cut.

+/-35% is used because it matches the stored evidence rather than a convenient
round number: the two real D-003 answers give 90 line ratios spanning 0.77-1.32
with median 0.99, so a faithful line stays inside about +/-30%. At wider noise
the two-sided test loosens with it - false rejection is still 0.0000 at 10 and
20 lines at +/-40% and reaches about 1.5% of 45-line trials - which is the price
of refusing a merge whose line a noisy model drew 20% short.

Each case above is replayed by `scripts/verify_paragraph_regroup.py` (`--trials`,
default 10,000), and the counterexamples are in `tests/test_sentence.py`; the
parametrised merge+split cases fail on the 2026-11-19 guard and pass here.

The 4-line sample the first amendment quoted
(`source = "a1\na2\n\nb1\nb2"` against `answer = "A1 A2\nB1\nB2x\nB2y"`) no
longer demonstrates the guard and is replaced in the replay by a case with
realistic counts: its "split" halves carry 3 + 3 scalars against a 2-scalar
source line, a 1.33x merge and a 0.67x split, which a faithful answer can show
on its own. The replay's merge+split sample is now `alpha one / beta two /
gamma e / delta f` answered as `alpha one beta two / gamma e / delta / f`
(8, 7, 6, 6 against 15, 6, 5, 1). The crossing-break sample is unchanged: it is
decided by the paragraph cuts alone.

Evidence boundary, unchanged by this amendment: the 31-paragraph and
8-paragraph D-003 answers the frozen criteria describe are **not** stored in
this workspace, and the ones the replay uses are synthesized by removing blank
lines from a stored 45-paragraph translation (the text and the 45-line sequence
are real, the layout is not). The six regrouped R segments in the original
criteria table are also absent from this workspace; the 30 stored R answers
here need no regrouping, so the replay can only hold them to L4. A future run
that stores a 31-paragraph, 8-paragraph or regrouped answer should replace the
synthesis, and these results should then be re-read rather than reused.

### Revision, 2026-11-20, after PR #282 review

The paragraphs above first shipped with a 1.5 merge bound, a 1.4 peer factor, a
0.65 lone-line bound and a 0.85-1.35 two-half sum. Review found the regression
that shape leaves: a 4-line source of 40, 30, 30, 30 scalars, answered as 56,
36, 18, 18 (the first two lines merged, the last split, 20% noise), gives ratios
1.42, 1.22, 0.61, 0.61 against a median of 0.91. The merged line missed 1.5, the
halves missed the lone-line bound, and the answer was regrouped, putting source
line 3 at the end of paragraph 1. The 2026-11-19 guard did refuse this answer -
with four lines the absorbed line is 23% of the text, so the running-share gap
reaches 18%, four times that bound - which is the dilution argument working in
reverse: a short segment is where that bound is at its strongest. The four
bounds above are the corrected ones, and the replayed cases now include this
answer, the same shape at 20 and 40 lines with 20% noise, and a 40/30 merge next
to a long line.
