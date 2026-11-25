# ADR 0022: Deterministic structure checks are a separate review track

- Status: Accepted (contract and engine)
- Date: 2026-11-26
- Related: `MY-490`,
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
   `/v1/reviews` schemas and response bytes to what Core 0.13.0 served. A
   client that wants structure findings calls this endpoint as well.
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
- **What never raises a number finding.** Chinese numerals (`三`, `二〇二四`,
  `百分之五十`), spelled English numbers (`twenty-one`, `two hundred and five`,
  `twice`), list labels at the start of a line (`1.`, `2)`, `(3)`, `4、`),
  compatibility numerals (`①`, `²`, `½`, `Ⅳ`) and the `:00` of a clock time
  are never reported. They only account for a digit number of the same value
  on the other side. The fullwidth comma `，` separates list items, so
  `100，200` is two numbers, and an ASCII `100,200` also matches the list
  `100、200`.

## Consequences

- Without a model, `GET /v1/capabilities` now answers
  `["structure-check-v1"]` instead of `[]`. ADR 0018's sentence that it "lists
  nothing" describes the state before this ADR. The Web client at
  `contracts/web-ref.txt` keeps only the capability names it knows, and the
  Windows client at `contracts/windows-ref.txt` keeps unknown names, so neither
  changes behaviour.
- The number rule is lenient on purpose. A number that a Chinese numeral, a
  spelled word or a list label on the other side could account for is not
  reported, even when that word meant something else. A false alarm on every
  `三个`/`3` pair would teach users to ignore the check.
- The false-positive rate is not measured yet. It can be measured offline on
  existing private evaluation outputs with no model call.
- Web and Windows do not call the endpoint yet; adopting it is client work.

## Evidence

- `src/wuwaterm/structural_checks.py` (`check_segment_structure`)
- `src/wuwaterm/application.py` (`check_structure`, `service_capabilities`)
- `src/wuwaterm_api/app.py` (`POST /v1/structure-checks`)
- `tests/test_structural_checks.py`, `tests/test_api_structure_checks.py`
