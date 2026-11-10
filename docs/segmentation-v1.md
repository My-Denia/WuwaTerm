# segmentation-v1

`segmentation-v1` is the rule every client uses to cut a long text into pieces
that fit one model request, and to pair a source text with its translation
for semantic review. Core ships the reference implementation
`wuwaterm.segmentation` and the shared fixtures
[`contracts/segmentation-v1.json`](../contracts/segmentation-v1.json). The Web,
Windows and Telegram implementations run the same fixtures. Where this page
and the reference implementation disagree, that is a bug in one of them; the
fixtures decide which until it is fixed.

Background and the reason for each rule:
[ADR 0019](adr/0019-document-context-and-human-precedents.md).

## Definitions

- **Offsets.** Every offset is an index in Unicode scalar values (code
  points). Python `str` indices are scalar indices. JavaScript must count
  with `Array.from(text)`, Qt must convert from UTF-16. A cut never falls
  inside a surrogate pair because a scalar is never split.
- **L.** The limit is `L = 1800` scalars. The request limit is 2000; the
  remaining 200 is headroom.
- **Whitespace (WS).** Exactly these scalars: U+0009, U+000A, U+000B, U+000C,
  U+000D, U+0020, U+00A0, U+1680, U+2000–U+200A, U+2028, U+2029, U+202F,
  U+205F, U+3000. Nothing else counts, so the rule does not depend on a
  language runtime's idea of whitespace.
- **Boundary punctuation (P).** Exactly `，、；：,;:`.
- **Region.** A half-open range `[start, end)` of one side.
- **Core.** The core of a region is the region with leading and trailing WS
  removed: `[core_start, core_end)`. A region that is all WS has an empty core
  with `core_start = core_end = start`.
- **Length.** "Length" of a region always means its core length,
  `core_end - core_start`. Every "≤ L" in this document is a core length. The
  core is what is sent to the model; the WS around it is a separator that is
  kept verbatim and never sent.
- **Partition.** The regions of one side, in order, cover `[0, len)` without
  gaps or overlap: the first starts at 0, each starts where the previous one
  ends, the last ends at `len`. Concatenating them reproduces the text byte for
  byte. Every cut described below only splits a region at an offset or joins
  two adjacent regions, so the partition always holds.
- **Separator ownership.** When a region is cut at offset `p`, the scalars
  before `p` stay in the left region. Cut points are chosen so that the WS
  between two pieces ends up at the end of the left piece: leading WS of the
  whole text belongs to the first region, and every other separator belongs to
  the region before it.

## Building blocks

### Paragraph regions

A paragraph break is any maximal run of WS scalars that contains at least two
U+000A. (`\r\n\r\n`, `\n  \n` and `\n\u3000\n` are all breaks; a single
`\r\n` is not.) The text is cut at the end of every paragraph break that has
a non-WS scalar both before and after it, that is at the first non-WS scalar
after the break. A break at the very start stays in the first region and a
break at the very end stays in the last one, so no region is all WS. A text
with no non-WS scalar has no paragraph regions.

### Sentence regions

Sentence boundaries are the review-v2 boundaries (`review._sentence_ranges_v2`,
applied to the core of the region being split): a run of the scalars
`。．.！？!?` and U+000A ends a sentence, except that `.` and `．` do not end
one when both neighbours are digits (Python `str.isdigit()`; this keeps `3.7`
inside one sentence). A candidate sentence made only of those ending scalars
and of scalars for which Python `str.isspace()` is true is not a sentence of
its own; it stays with the sentence before it. That one test uses Python's
`isspace()`, which is WS plus U+001C–U+001F and U+0085, because it is the
review-v2 rule; every other whitespace test in this document uses WS. The region is cut at the first non-WS
scalar of every sentence after the first, so WS between sentences stays with
the previous sentence. A region with a single sentence is not cut.

### Natural boundary

An offset `p` is a natural boundary when `text[p-1]` is WS or P.

## Single side (translation path)

`segment_text(text, L)` returns segments
`{start, end, core_start, core_end, hard_cut}`.

1. Empty text: no segments. A text that is all WS: one segment covering it,
   with an empty core and `hard_cut: false`. The caller sends nothing for a
   segment whose core is empty.
2. Cut into paragraph regions.
3. A paragraph region longer than `L` is replaced by its sentence regions.
4. A sentence region longer than `L` is hard-cut. Let its core be `[s, e)` and
   `pos = s`. While `e - pos > L`, choose the cut `p` in the window
   `pos + L - 200 ≤ p ≤ pos + L`:
   1. the largest `p` in the window with `text[p-1]` in WS;
   2. otherwise the largest `p` in the window with `text[p-1]` in P;
   3. otherwise `p = pos + L`.

   Then cut at `p` and continue with `pos = p`. Every piece produced from such
   a region has `hard_cut: true`. A piece whose core is empty (only possible
   with a long run of internal WS) is joined to the piece before it as a
   separator.
5. Greedy merge, left to right: keep a current segment; join the next one to
   it when neither of the two is `hard_cut` and the core of the joined region
   is still ≤ `L`; otherwise emit the current one and start a new one with the
   next. A `hard_cut` segment is never joined.

The translation output is `Σ (text[start:core_start] + translate(core) +
text[core_end:end])`: separators are copied, never regenerated.

## Bilingual pairing (review path only)

`pair_segments(source, target, L, alignments=None)` returns pairs
`{index, status, hard_cut, source: region, target: region}` where each region
is `{start, end, core_start, core_end}`. The source regions partition the
source and the target regions partition the target. `status` is one of:

| status | meaning | sent to the model |
| --- | --- | --- |
| `ready` | both cores non-empty and ≤ `L` | yes |
| `needs_manual_split` | no trustworthy pairing; the UI shows both lengths and asks the user to insert a blank line at corresponding places | no |
| `untranslated` | source core non-empty, target core empty | no (the UI offers "translate this segment") |
| `unpaired_target` | source core empty, target core non-empty: an extra target paragraph, listed as a deterministic hint | no |

Only `ready` pairs are bounded by `L`; the other statuses carry their region
as is. If neither side has any non-WS scalar the result is empty.

1. **Region pairs.**
   1. When `alignments` is a non-empty list (the review-v2 alignment shape:
      ordered `{source: {start, end, text}, target: {start, end, text} | null}`),
      the alignments define the pairs. With `n` alignments, alignment `i` owns
      the source region `[s_i, s_{i+1})` and the target region
      `[c_i, c_{i+1})`, where `s_0 = c_0 = 0`, `s_n = len(source)`,
      `c_n = len(target)`, and for `0 < i < n`: `s_i` is alignment `i`'s
      `source.start`, and `c_i` is alignment `i`'s `target.start`, or
      `c_{i+1}` when its target is `null` (resolved from the right). So an
      alignment with `target: null` owns a target region with an empty core
      (for the first alignment that region is `[0, c_1)`, which can hold
      leading WS). Alignment spans may touch but not overlap or go backwards;
      a malformed list is a caller error. If any non-WS scalar lies outside
      every alignment span on either side, the whole document is one
      `needs_manual_split` pair instead, whatever either side contains. A
      region pair whose two cores are both empty (only possible when an
      alignment span is all WS) is joined to the pair before it, or to the
      pair after it when it is first.
   2. Otherwise, when both sides have the same number `n ≥ 1` of paragraph
      regions, paragraph `i` pairs with paragraph `i`.
   3. Otherwise the whole document is one region pair.

   Each region pair is then classified: `untranslated` or `unpaired_target`
   as in the table, or a normal pair when both cores are non-empty. In case
   1.iii a normal pair with either core longer than `L` is
   `needs_manual_split` and is not split further: without matching paragraph
   counts there is nothing to align on.
2. **Sentence pairs.** A normal pair with either core longer than `L` is cut
   into sentence regions on both sides. If both sides have the same number of
   sentences, sentence `i` pairs with sentence `i`. If the counts differ, the
   pair becomes `needs_manual_split`.
3. **Fallback cut.** A sentence pair with either core still longer than `L`
   (however short the other side is) is cut proportionally:
   1. `k = max(⌈ls / L⌉, ⌈lt / L⌉)` where `ls`, `lt` are the core lengths.
   2. On each side with core `[a, b)` and length `n`, for `i = 1 … k-1` the
      target offset is `t_i = a + ⌊i · n / k⌋`. The cut `p_i` is the natural
      boundary with `a < p_i < b`, `p_i > p_{i-1}` and `|p_i - t_i| ≤ 200`
      that is nearest to `t_i`; on a tie the smaller offset wins. WS and P are
      equal here: nearest wins.
   3. If every cut on both sides exists and every resulting piece has a
      non-empty core of length ≤ `L`, the result is `k` pairs, all `ready`
      with `hard_cut: true`.
   4. Otherwise the sentence pair is `needs_manual_split`. There is no cut at
      the `L`-th scalar on this path: such an offset has no trustworthy
      counterpart on the other side.
4. **Greedy merge.** Left to right over all pairs, join two adjacent pairs when
   both are `ready`, neither is `hard_cut`, and both joined cores are ≤ `L`.
   Pairs of any other status are never joined and end a run.
5. **Final assertion.** Before returning, every `ready` pair has both cores
   non-empty and ≤ `L`, and both sides are partitions. The reference
   implementation raises instead of returning a violating result. Clients
   check again immediately before each request and turn a violating pair into
   `needs_manual_split`; an oversized request is never sent.

`index` numbers the final pairs from 0.

## Accepted risk

A hard cut can split a dictionary surface: the client has no dictionary and
cannot avoid it. It happens only when the 200-scalar window holds neither WS
nor P. Every such segment is marked `hard_cut`, and the UI says the boundary
may be judged wrongly.
