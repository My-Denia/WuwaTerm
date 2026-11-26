# ADR 0022: Deterministic structure checks are a separate review track

- Status: Accepted (contract and engine)
- Date: 2026-11-26
- Related: `MY-490`, `MY-504`,
  [ADR 0018](0018-semantic-review.md),
  [segmentation-v1](../segmentation-v1.md)

## Problem

Some translation breaks are mechanical: a number that changed or went
missing, a placeholder or markup tag that was dropped or translated, two
paragraphs merged into one, a segment left untranslated. `POST /v1/reviews`
checks official dictionary terms only, so it passes all of these.
`POST /v1/semantic-reviews` can notice some of them, but it is a model call:
it costs one, its findings are not reproducible, and it is experimental and off
by default.

These breaks can be found without a model, every time, for free. Adding them to
the `/v1/reviews` response would change a document clients already store under
exact key sets: manuscript v4 saves review reports, and its readers reject a
finding whose key set differs from the published one.

## Decision

Layer 1 deterministic structure checks are a third, separate track:
`POST /v1/structure-checks` with `check_version: "structure-check-v1"`.

1. **Separate tracks.** `/v1/reviews` and `/v1/semantic-reviews` do not change:
   not their requests, not their responses, not their bytes. A test pins the
   `/v1/reviews` schemas and response bytes to what main served before this
   endpoint. A client that wants structure findings calls this endpoint as
   well.
2. **Deterministic and stateless.** No model, no storage, no network. The same
   segments always produce the same findings. Nothing is written into a
   manuscript format; the client decides what to show.
3. **Always listed.** `GET /v1/capabilities` lists `structure-check-v1`
   whether or not a model is configured, because the checks need none. A server
   that does not list it answers 404 on the path; a client then hides the
   feature.
4. **Layer 1 wins an overlap.** Where a model finding and a structure finding
   describe the same break, clients show the structure finding: it is
   reproducible.

## Contract

The request and response shapes are published in `docs/api/openapi.json`. This
section records the rules a schema cannot express.

### Request

- `check_version` is required and is `structure-check-v1`.
- `segments`: 1 to 64 already-paired segments. Each has `source`, `target`,
  and an optional `segment_id` (`^[A-Za-z0-9._:-]{1,128}$`, unique within the
  request) that is echoed back.
- Each side is at most 2,000 Unicode scalars and all sides together at most
  20,000; past either limit the answer is `input_too_long` (422), the same
  answer `/v1/reviews` and `/v1/semantic-reviews` give. Any other malformed
  body is `invalid_request` (400): unknown keys at any level, a wrong
  `check_version`, no segments or more than 64, a repeated `segment_id`, an
  isolated surrogate. The operator's body limit (`WUWATERM_API_MAX_BODY_BYTES`,
  64 KiB by default) applies first (`payload_too_large`, 413), and Chinese text
  at the scalar limits is larger than that, so a client splits its batches by
  serialized request size, not by segment count alone.
- Scope `translate`, counted against the same per-device limit as
  `/v1/reviews`.

### Response

- `checks` lists what ran: `empty_target`, `numbers`, `placeholders`,
  `paragraphs`.
- `segments` has one entry per request segment, in request order, with its
  `index`, its `segment_id` (or null), its `findings`, `omitted_findings` and a
  `layout` of paragraph and non-blank line counts per side.
- A finding is `{rule_id, source_span, target_span}`. Spans are half-open
  Unicode scalar offsets into the side as submitted, before any normalization,
  with the exact text at that range.
- Findings are ordered by rule (`empty_target`, `paragraph_count_mismatch`,
  `line_count_mismatch`, `placeholder_missing`, `placeholder_unexpected`,
  `tag_unbalanced`, `number_missing`, `number_unexpected`) and then by offset.
  At most 16 per segment are returned; `omitted_findings` counts the rest, so a
  response holds at most 1,024 findings. A 1,024-finding response from a
  24 KB request measured 124 KB.

### Rules

- **Blank sides.** A source with no visible scalar (whitespace and invisible
  format characters such as U+200B only) yields no findings. A blank target for
  a non-blank source yields `empty_target` and nothing else, since every other
  check would restate it.
- **Paragraphs.** Paragraph regions are the segmentation-v1 ones (a blank line
  separates two), after `\r\n` and `\r` become `\n`. `paragraph_count_mismatch`
  when the paragraph counts differ; otherwise `line_count_mismatch` when the
  counts of non-blank lines differ.
- **Placeholders and markup.** `{0}`, `{name}`, `{{name}}`, printf
  conversions such as `%s` and `%1$s`, tags such as `<color=Highlight>`,
  `</color>`, `<size=20>`, `<te href=123>`, and WuwaTerm's own opaque tokens.
  A gender switch such as `{Male=him;Female=her}` is compared by its keys only,
  because its values are translated. Each source token the target lacks is
  `placeholder_missing`; each target token the source lacks is
  `placeholder_unexpected`. When both sides carry the same tokens and the
  source tags nest correctly, target tags that do not nest are
  `tag_unbalanced`.
- **Numbers.** Only Arabic-digit numbers are reported. Both sides are read in
  NFKC, digits of any script count as ASCII digits, and characters inside
  placeholders and tags are not read. `1,000` and `1000` are the same number,
  as are `1.50` and `1.5`. A digit number followed by `十百千万亿` or by
  `thousand`, `million` or `billion` also reads as that multiple, so `1.5万`
  matches `15,000`. A source number the target does not account for is
  `number_missing`; a target number the source does not account for is
  `number_unexpected`.
- **Numeric facts.** Two numbers of one value only account for each other
  when they can be the same fact:
  - A percentage (`50%`, `50％`, `50 percent`, `百分之50`) or a multiplier
    (`1.5倍`, `1.5x`, `1.5×`, `2-fold`) is one on both sides: `50%` and `50`
    do not match. `3 times` and `×100` read as a multiplier or as a plain
    count, so `3次` matches `3 times` and `100星声` matches `Astrite ×100`. A
    bare number joined to the next one by a range or list mark takes its
    unit: `30-45%` matches `30%-45%`, and `50/60/70%` matches `50%/60%/70%`.
  - An explicit sign written on both sides agrees: `+20%` and `−20%` do not
    match. `-`, `−` (U+2212) and `–` (U+2013) are minus signs; the em dash
    never is. A dash after a digit, a percent sign or a unit joins a range
    (`100-150`, `100–150`, `3秒-5秒`, `2025-05-22`), after a word it joins a
    compound (`Type-2`), and after an all-capitals stat name it is a sign
    (`ATK-20%`).
  - An explicit comparison written on both sides points the same way:
    `大于`, `超过`, `不低于`, `至少`, `≥`, `>`, `more than`, `at least`,
    `over`, `above`, a trailing `以上` or `or more` against `小于`, `低于`,
    `不超过`, `最多`, `≤`, `<`, `less than`, `at most`, `under`, `below`,
    `up to`, a trailing `以下`, `以内` or `or less`. A leading word sits right
    before the number, with only spaces or tags between them; a trailing one
    follows the number and its unit, with at most one classifier or word
    between (`3层以上`, `3 stacks or more`). Strictness is not compared:
    `大于` matches `at least`.
  - Time and distance units written on both sides agree: `秒`/`s`/`sec`,
    `分钟`/`min`, `小时`/`h`, `米`/`meter`; a bare `m` reads as metres or
    minutes. `20秒` does not match `20m`.

  A sign, unit or comparison written on one side only never decides
  anything: dropping `至少` or writing `+20%` for `提高20%` is a translation
  choice. Among numbers of one value, as many pairs are made as their facts
  allow (a maximum matching), so an unsigned `20%` is never spent on the
  wrong `+20%`. A value written more than eight different ways on one side
  is matched by value alone.
- **Build markers.** Resonance Chain and refinement markers are one fact
  each: `S0R1`, `C0R1` (C reads as S), `S6 R5`, `S6`, `R1`, `0+1`, `3+1`,
  `6链` and `0链1精`. `S0R1` matches `0链1精` and `0+1` but not `S1R0` or
  `S0R5`. A marker the other side spells out as plain numbers (`S3R1` as
  `Sequence 3, Refinement 1`) is accounted for by those numbers. `N+M` is a
  marker only for chain 0 to 6 and refinement 1 to 5 standing alone, so
  `1+1=2` and `3+1+1` stay arithmetic.
- **Spans of number findings.** A number finding's span covers the number as
  written: an explicit sign, an attached percent sign, multiplier or unit,
  and a comparison word, as far as they are contiguous text outside markup
  (`+20%`, `大于 150%`, `less than 150%`, `S0R1`, `3+1`). A unit or
  comparison read across a tag (`<color=Red>1.1</color>倍`) still counts but
  stays out of the span.
- **What is not checked.** Which number goes with which attribute: `攻击力
  20%，暴击率 10%` translated as `ATK 10%, CRIT Rate 20%` raises nothing.
  The two sides name attributes in different words and orders, and natural
  translations reorder them freely, so pairing numbers by position or by
  neighbouring words would raise a false alarm on every reordered sentence.
  This is left to Layer 2 semantic review.
- **What never raises a number finding.** Chinese numerals (`三`, `二〇二四`,
  `百分之五十`), spelled English numbers (`twenty-one`, `two hundred and five`,
  `twice`, `triple`), English month names (`November` for `11月`), list labels
  at the start of a line (`1.`, `2)`, `(3)`, `4、`), compatibility numerals
  (`①`, `²`, `½`, `Ⅳ`) and the `:00` of a clock time are never reported. They
  only account for a digit number of the same value on the other side, and
  they do so whatever its facts. The fullwidth comma `，` separates list
  items, so `100，200` is two numbers, and an ASCII `100,200` also matches the
  list `100、200`.

## Consequences

- Without a model, `GET /v1/capabilities` now answers
  `["ambiguous-mention-v1", "structure-check-v1"]`. ADR 0018's sentence that it
  "lists nothing" describes the state before either feature. The Web client at
  `contracts/web-ref.txt` keeps only the capability names it knows, and the
  Windows client at `contracts/windows-ref.txt` keeps unknown names, so neither
  changes behaviour.
- The number rule is lenient on purpose. A number that a Chinese numeral, a
  spelled word or a list label on the other side could account for is not
  reported, even when that word meant something else. A false alarm on every
  `三个`/`3` pair would teach users to ignore the check.
- The numeric facts (MY-504) add no rule id, no check name and no field:
  they still answer `number_missing` and `number_unexpected`, so the Web and
  Windows allow-lists of `STRUCTURE_RULES` and `STRUCTURE_CHECKS` accept
  every response. What changed for a client is where a number finding fires
  and that its span now includes the sign, unit and comparison written with
  the number.
- Web ([WuwaTerm-Web #68](https://github.com/My-Denia/WuwaTerm-Web/pull/68),
  `ae5b396`) and Windows
  ([WuwaTerm-Windows #53](https://github.com/My-Denia/WuwaTerm-Windows/pull/53),
  `b50abdd`) call the endpoint on their main branches. Web `v0.4.5` and
  Windows `v0.6.6`, the latest releases, were cut before either change.

## Measured false alarms and misses (MY-504)

`python scripts/eval_structure_checks.py` measures the number rule offline
with no model call. Numbers below are for this change's head.

- **Fixture.** `tests/fixtures/structure-numeric-facts.json` holds 96
  hand-written guide, announcement, skill and story pairs, labelled before the
  rules were written: 57 natural translations (27 of them hard negatives
  such as ranges, `3次`/`3 times`, `-` against `−`, a dropped `至少`, and
  `percent` spelled out), 36 pairs with one injected error each (percent or
  multiplier lost, sign flipped, comparison flipped, build marker changed,
  unit changed, number added, dropped or changed), and 3 attribution swaps.
  Every case runs in both directions. A positive counts as found only when a
  number finding overlaps the injected error.

  | | Before (`fe4555f`) | After |
  |---|---|---|
  | False alarms on natural pairs | 2 / 114 (1.8%) | 0 / 114 (0%) |
  | Missed errors | 48 / 72 (66.7%) | 0 / 72 (0%) |
  | Attribution swaps found (not claimed) | 0 / 6 | 0 / 6 |

  The two earlier false alarms were the month name in `11月26日` /
  `November 26`. The labels were committed before any rule, but the fixture
  is not blind: its first run against the new rules found 70 of 72 errors
  with 2 false alarms, and two changes followed from it (the lettered build
  form takes any digit so `S1R0` is one marker, and month names account for
  a month). The official corpus below is the measure the rules were not
  tuned on.
- **Official corpus.** With `--textmap-root data/wutheringdata/Textmaps
  --baseline-ref fe4555f`, the 27,385 zh-Hans/en pairs of the official game
  text (data `9218d61`, 3.7.8) that hold a digit are read in both
  directions. Official translations rarely change a number, so a pair this
  change newly flags is read as a false alarm unless the two texts really
  disagree. Flagged pairs went from 2,337 to 2,270 per direction: 75 stopped
  being flagged, each for a month name (63 dates with `N月` against the
  English month, and the 12 birthday-month labels `1` / `Jan` to `12` /
  `Dec`); 8 are newly flagged, and each of the 8
  is a real difference in the official text: `热熔伤害提升10%` translated as
  `Gain 10 Fusion DMG Bonus` in four item texts, `0%` against `0` or `0.0` in
  two, `0` against `0%` and `+0.02` against `0.02%`. The script prints counts and
  identifiers only; no corpus text is written anywhere.
- **Time.** The slowest of 20 adversarial pairs with both sides at 2,000
  scalars (signs, percents, build markers, comparison words, units, ranges,
  tags, many ways of writing one value, long digit runs) took 11.8 ms on
  Python 3.11 and 10.7 ms on 3.13, best of 5, on the development machine. The test suite bounds each at 100 ms, to
  catch a superlinear path without failing on a slow shared runner.

## Evidence

- `src/wuwaterm/structural_checks.py` (`check_segment_structure`)
- `src/wuwaterm/application.py` (`check_structure`, `service_capabilities`)
- `src/wuwaterm_api/app.py` (`POST /v1/structure-checks`)
- `tests/test_structural_checks.py`, `tests/test_api_structure_checks.py`
- `scripts/eval_structure_checks.py`, `tests/fixtures/structure-numeric-facts.json`,
  `tests/test_eval_structure_checks.py`
