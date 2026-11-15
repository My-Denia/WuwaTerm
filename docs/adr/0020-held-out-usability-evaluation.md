# ADR 0020: Held-out usability evaluation of translations

- Status: Accepted (evaluation protocol). Frozen before the first evaluation
  model call; not edited afterwards. A change needs a new decision that says
  what changed and why.
- Date: 2026-11-16
- Related: [ADR 0018](0018-semantic-review.md) (frozen-criteria mechanism,
  report states), [ADR 0019](0019-document-context-and-human-precedents.md)
  (the translation path under test), [segmentation-v1](../segmentation-v1.md)

## Problem

The owner's target is that, on new manuscripts never used in tuning, at
least 80% of paragraphs can be used as is or after light polishing (90% is the
stretch goal), reported separately for zh→en and en→zh.

The evidence so far does not measure that. The M-Gold set scores items (an
official term, a number, a negation), not paragraphs, and its texts were used
while the dictionary and prompts were tuned: the frozen score stayed at
670/675 while term coverage improved. Better term coverage and a usable
paragraph are different claims. This ADR fixes how the paragraph claim is
measured, on which texts, by whom, and how the texts are kept out of the
tuning loop.

## Decision

1. Three sets of new texts: **D** (development; the people who change Core may
   read it), **H1** (held-out acceptance) and **H2** (held-out reserve, sealed).
   The texts live in a private evaluation directory outside every repository
   and worktree. Only aggregate counts leave it.
2. Each scored unit is one paragraph of one translation. Blind judges grade it
   A, B or C with the rubric below. Usable = A or B.
3. The primary result is the usable rate per direction on H1, with a Wilson 95%
   interval, plus four separate metrics. The first H1 result of a candidate
   Core is the official number.
4. A leakage scan with frozen parameters runs before every Core quality merge.
5. The rubric, metric definitions, scorer, scan, set manifests and name
   annotations are hashed into the evaluation ledger before the first
   evaluation model call.

## Texts

### Genres and sources

Genres are assigned by source, mechanically:

| Genre | Definition | Source | Reference |
| --- | --- | --- | --- |
| 公告 announcement | an article of the official Wuthering Waves website | the Kuro website article JSON, zh and en editions of the same announcement | the other edition, paragraph-aligned |
| 剧情 story | dialogue and narration lines of story quests new in game data 3.7 | upstream TextMaps at the pinned data commit `9218d612`, keys absent from the 3.6.6 data (upstream `67fec8db`) | the other language of the same key |
| 攻略 guide | community text whose stated purpose (title or the source's own category) is instruction about play: build, team, mechanics, walkthrough, farming, pulling advice | TapTap posts (zh); Steam community guides (en) | none |
| 混合语境 mixed | community text about the game that is not a guide: user reviews, discussion, opinion, story commentary, version impressions | TapTap posts and Steam reviews (zh); Steam reviews (en) | none |

"Mixed" means everyday language, game names and their ordinary-word
homographs in the same text, written by players. This genre tests
the difference between an official name and the same surface in its ordinary
sense.

Directions: a zh source is translated zh→en, an en source en→zh. Each
bilingual unit (an announcement pair, a TextMap key) is used once, in one
direction and one set, so no set contains another set's source as a reference.

### Freshness

- No text from an earlier repair or evaluation: every Kuro
  article id, TextMap key and URL that appears in earlier evaluation material
  (the 2026-11-10 corpus A1–A6 and B1–B3, Set 2 S01–S18 and the quality pilot,
  MY-236, M-Gold, and every other earlier run directory) or in the Core tree at
  the freeze commit is excluded. A story quest with such a key is excluded
  whole; a key that appears only in the zh lock-exclusion census is excluded
  as a line, and no scene spans it.
- Published from 2025-01-01 up to, not including, 2026-11-06 19:45 +08:00 (the
  first article titled for version 3.8), and no mention of version 3.8 or
  later. The dictionary under test is built from 3.7 data; 3.8 names cannot be
  in it, and the batch does not bump game data.

### Document form

- Paragraphs are separated by exactly `\n\n`, so a paragraph is a
  segmentation-v1 paragraph region. Web text: one non-empty line is one
  paragraph. TextMap text: one key is one paragraph; rich-text tags are removed
  and their inner text kept; `{PlayerName}`-style placeholders are kept; a
  line with a gender switch or a placeholder that exists in one language only
  is dropped in both languages.
- A document is a whole text or a contiguous excerpt cut at section
  boundaries (announcements) or a scene of 10–30 consecutive lines of one
  quest (story). Excerpts keep the size of a real manuscript and the call
  budget in proportion.
- Personal identifiers (player UIDs, contact handles) are dropped as whole
  lines; texts that cannot be cleaned that way are not used.

### Scored and unscored paragraphs

Every paragraph is translated as part of its document. A paragraph is not
scored, and is counted separately, when:

1. **Dictionary answer.** Sent alone as its own translation request, the
   paragraph would be answered by the dictionary stage
   (`application._dictionary_stage` at Core `e97b997` returns an exact or fuzzy
   outcome) with the dictionary built from the pinned data by that commit.
   Inside a document the paragraph is merged into a longer segment and almost
   never skips the model, so the test is per paragraph.
2. **No letter.** It contains no Unicode letter (numbers, symbols and
   separators only).
3. **Duplicate.** After NFKC, case folding and removal of whitespace and
   punctuation it equals a paragraph of an earlier evaluation text, or an
   earlier scored paragraph of the same set and direction.

### Sizes and assignment

- H1 and H2: at least 120 scored paragraphs per direction, and at least 25 per
  direction and genre. D: at least 15 per direction and genre, except
  announcements (below). Counts are taken after the exclusions; material is
  added until they hold.
- D announcements fall short. The Chinese site has only two template
  families, and the D pruning below removes their boilerplate where it
  repeats H text, so D keeps only the announcement paragraphs that share no
  window with H. Announcement failures are analysed on D's remainder and on
  the earlier announcement samples (A1–A6), which Q2 already iterates on.
- A series stays inside one set: a story quest line, a community author.
  Announcements come from two template families only, so each announcement
  pair is its own series; their shared boilerplate is handled by the
  duplicate rule and the D pruning below. Material of a series assigned to
  one set that the set does not need is discarded, never moved to another
  set.
- Assignment is deterministic: candidates are ordered by SHA-256 of
  `real-manuscript-eval-20261115:<id>` and filled in that order.
- After assignment, every D paragraph that shares a 12-scalar window with any
  H1 or H2 source or reference paragraph (uncleared, see the leakage scan) is
  removed from its D document. D therefore starts at zero uncleared hits.

## Translation runs

- **Call unit.** One call is one model request of the product's segment path:
  `application._translate_long_plain_text` with an empty
  `TranslationContext` (ADR 0019: the previous segment as read-only context,
  segmentation-v1 with L = 1800), direction forced by the set. A segment whose
  core is wholly a dictionary exact hit makes no call. One call covers many
  paragraphs.
- **Settings.** Production model, temperature and request options as the
  translation path sends them (no extra thinking field today). They go into the
  reuse key, and secrets never go into any file.
- **Paragraph output.** A segment's output is split at segmentation-v1
  paragraph breaks (a whitespace run holding two or more U+000A). When the count
  equals the segment's source paragraph count, paragraph i maps to paragraph i.
  Otherwise judges receive the whole segment output and locate each
  paragraph's translation themselves; a paragraph whose translation cannot be
  found is an omission. For the literal match (metric 5) such a paragraph's
  translation is the whole segment output.
- **Failure.** The product path stops a document at its first failed
  segment, so a failed document is run once more from the start (every call
  counts). If it fails again, its paragraphs count as not usable and are
  listed as `run_failed`. They are left out of metrics 2–5, whose reports
  state how many were left out.
- **Reuse key.** Per call: SHA-256 over the compact JSON array of the source
  segment hash, direction, the request payload hash (model, messages,
  temperature and every other body field, without credentials or URL), the
  dictionary SHA-256, `MATCHER_REVISION`, the selected term references, model,
  temperature and thinking setting. An earlier output is reused only on an
  identical key; any change reruns the call. A prompt change therefore
  invalidates every earlier output.
- **Size.** With the frozen sets, one run of H1 is 31 calls (15 zh→en, 16
  en→zh) and one run of D is 17, counted by a dry run of the runner against an
  echo gateway that sends nothing out.
- **Budget.** Every attempted POST to the product model gateway counts,
  retries and failures included, through the transport's pre-send hook. One
  ledger for the batch: cap 500 calls, stop at 450 and ask the owner. Q0 makes
  no call; Q0 and the Q1 baseline together use at most 200. Judging and
  annotation run on the Claude subscription and are not calls.

## Judging

### Rubric (per paragraph)

- **A** usable as is.
- **B** needs only light polishing: wording, word order, punctuation, style.
- **C** needs substantive change. Always C: a change of meaning; an omission or
  an addition; a wrong subject or reference relation; a wrong number or
  quantity; a lost or added negation; an official-name error that misleads
  the reader.
- **undecidable** the judge cannot decide; the reason is required. In the
  usable rate it counts as not usable, and it is listed for the owner's spot
  check. The spot-check outcome is reported next to the number and does not
  change it.

### Protocol

- Judges are Claude agents in fresh contexts, separate from the people who
  change Core. A judge never sees which arm, round or Core version produced a
  translation: items carry random ids and are shuffled across documents.
  When a candidate is judged, its round also contains a seeded 25% of the
  baseline items, re-judged blind; their results are reported as a drift
  check and do not replace the baseline numbers.
- A judge receives the source document, the item's paragraph and translation,
  the official reference when one exists (aligned paragraph and the whole
  reference document), and the name annotation of the paragraph.
- Two independent judgments per item (`j1`, `j2`). When they differ in
  anything — grade, the critical-error list, naturalness, or any name
  occurrence's label, misleading flag or rendering — a third agent adjudicates
  the whole item (`adj`), sees both judgments and gives a reason. The
  adjudicated record is final; otherwise the agreed record is final. An item
  that needs adjudication and has none is **pending**, not undecidable. An
  adjudication record for an item whose two judgments agree is a protocol
  error; the scorer refuses the file (exit 2).

### Judgment record (schema `usability-judgment-v1`)

```json
{"item": "random id", "grade": "A|B|C|undecidable",
 "critical_errors": [{"type": "omission|addition|meaning_shift|reference|number|negation", "count": 1}],
 "naturalness": "natural|stiff|disfluent|undecidable",
 "names": [{"occ": "occurrence id", "label": "official|not_official|omitted|undecidable",
            "rendering": "base form used, or null", "misleading": false}],
 "reason": "required for C, undecidable and every adj record"}
```

A record is invalid, and the scorer refuses the file, when it lists a critical
error or a misleading name and is not C; when `label` is `official` or
`not_official` without a rendering, or `omitted` or `undecidable` with one;
when `misleading` is true on a label other than `not_official`; when it names
an occurrence the annotation does not have or misses one it has. `rendering` is
the base form (singular, no possessive) so that inflection is not counted as
inconsistency.

### Name annotation

Before the first model call an annotator in a fresh context lists every
official-name occurrence in every scored paragraph of D, H1 and H2: paragraph,
scalar range, surface, a document-local `name_id`, and the accepted official
forms in the target language. Official forms come from the upstream TextMap
pairs at the pinned commit; for a document with an official reference the
form used there is also accepted. The annotator does not use Core's dictionary
or any translation output. The annotation guidelines and files are hashed
into the ledger.

## Metrics

All metrics are reported per direction and per arm, never pooled across
directions. Genre breakdowns are descriptive.

1. **Usable rate.** Unit: scored paragraph. (A + B) / scored paragraphs.
   Undecidable, `run_failed` and C are not usable. 95% interval: Wilson score
   interval, z = 1.959964. With n = 120 and a rate near 80% the interval is
   about ±7 points.
2. **Official names and their consistency.** Unit: annotated occurrence in a
   scored paragraph. Accuracy = occurrences labelled `official` / all
   occurrences (`not_official`, `omitted` and `undecidable` are not
   official). In-document consistency: among names (`name_id`) with at least
   two occurrences in the scored paragraphs of one document, the share whose
   occurrences all have a rendering and the same rendering after NFKC, case
   folding and trimming whitespace and punctuation at both ends. An omitted or
   undecidable occurrence makes its name inconsistent.
3. **Meaning integrity and critical errors.** Unit: scored paragraph. Share of
   paragraphs with at least one critical error (omission, addition, meaning
   shift, subject or reference, number, negation). Also critical errors per
   1,000 source scalars, where the denominator is the Unicode scalar count of
   the scored source paragraphs of that direction (Chinese source for zh→en,
   English source for en→zh; the English side is about three times longer, so
   the two are never compared or pooled).
4. **Naturalness.** Unit: scored paragraph. Distribution over natural,
   readable but stiff, and disfluent; undecidable counted on its own.
5. **Literal sentence match.** Only scored paragraphs with an aligned official
   reference. The reference paragraph is split into sentences with the
   review-v2 rule (a run of `。．.！？!?` or U+000A ends a sentence, except `.`
   and `．` between two digits; a piece of only enders and whitespace stays
   with the sentence before it); the translation is split the same way. Both
   are normalized: NFKC, then every whitespace scalar and every Unicode
   punctuation scalar (category P) is removed; case is kept. A reference
   sentence matches when an unused translation sentence of the same paragraph
   is identical. Rate = matched / reference sentences with a non-empty
   normalized form.

## Acceptance and report states

- The gate is per direction: the usable rate of the first candidate run on H1
  at least 80% (point estimate; the interval, the baseline value and the
  undecidable count are reported with it). 90% is the stretch target.
- Below 80% is reported as it is; the rules do not change. One more repair
  round may be measured only on H2. A rerun on H1 is labelled "held-out used
  in repair" and does not count toward the 80%.
- Report states follow ADR 0018: met, not met, undecidable, pending
  adjudication, not executed. A round with a pending item is not finished.
- The four metrics have no thresholds. They are reported per direction for
  baseline and candidate.
- Regression guards (M-Gold frozen 670/675, Set 1 resident 119/124, Set 2
  terms 38/39) are separate checks with their own frozen data.

## Leakage scan

- **Parameters.** Window: 12 Unicode scalars of the raw text, no
  normalization. A hit is any window of a sealed paragraph (H1 or H2, source
  or reference) that occurs anywhere in the scanned input.
- **Inputs, before every Core quality merge:** the added lines of the PR diff,
  the PR body, the translation prompt templates, new or changed dictionary rows
  (`scripts/diff_terms_db.py` output), and D against H1 and H2. The scan
  prints counts only — never the matched text — and the counts go into the
  ledger.
- **Stop rule.** Any hit stops the merge until it is cleared. A hit is cleared
  when it traces to D, to upstream game data outside the sealed paragraphs
  (for example an official name added as a dictionary row), or to text already
  in the Core tree at the freeze commit. Two kinds are cleared mechanically:
  `--clear-git <core>:<freeze commit>`, and `--clear` with the frozen upstream
  name file (zh and en content, at most 60 scalars, of every pinned TextMap
  key whose name ends in `Name` or `Title`, minus the keys used in D, H1 or
  H2; its SHA-256 is in the ledger). A remaining window may be cleared as
  **upstream data** or as **ordinary wording**: the latter when it holds no
  name, number or other content specific to its sealed paragraph and the
  scanned sentence around it is not a copy of that paragraph. Only the
  evaluator, never the person who changes Core, looks at matched windows; the
  ledger gets the class and the count, never the text. Anything else stops
  the work and goes to the owner.
- **What the clearances cost.** Of the 70,034 distinct 12-scalar windows in
  H1 and H2, the Core tree pre-clears 3.0% and the name file brings that to
  4.6%; 4 sealed source or reference texts are fully pre-cleared either way
  (a copy of them would not show). Clearing against the whole TextMap instead
  would pre-clear 27.8% and hide 74 texts, so the file is limited to names.
- **Measured noise.** Before the freeze, two merged quality PRs were scanned
  against H1 and H2 (H1 / H2 windows). Dictionary PR #257: 49 / 46 hits in
  its added lines, 31 / 29 after clearing against the Core tree before it,
  17 / 16 after also clearing the name file. PR #255: 37 / 47, then 11 / 14,
  then 11 / 14. The residue is classified by hand. A 12-scalar window is about
  two English words, so most of it is common English phrasing; Chinese text
  hits almost only on names. The parameters stay as frozen.
- The D pruning at freeze cleared against the Core tree alone, the stricter of
  the two.

## Consequences

- The quality claim becomes a paragraph claim on texts nobody tuned on, with
  an interval, per direction.
- H1 and H2 cost annotation and judging time on the Claude subscription and
  about one call per 1,800 source scalars per run.
- The genres follow what was obtainable: official announcements on the
  Chinese site after 2024 are maintenance notices and update notes only, so
  announcements are those two families plus two small notices; English
  community text comes from Steam (guides, user reviews).
- The scorer imports nothing from Core, so a later Core change cannot change
  how frozen judgments are scored.

## Evidence

- `scripts/eval_usability.py` (`score`, `scan`), `tests/test_eval_usability.py`
- The evaluation ledger (private) records the SHA-256 of this file, the
  scorer, the set manifests and the annotation files, and the call counter.
