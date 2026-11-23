# q8 Translation Quality Candidate Plan & Frozen Offline Scope

- Status: **Stage 3 complete — `q8r1` passed every frozen gate and is the
  adopted candidate.** Stage 1 Run #1 (`515 -> 521`), Phase 1 (`540 -> 545`) and
  the Phase 3 full run (`568/569`, 23 POSTs) have all been executed and read;
  the ledger has `1` POST left and no further run is planned.
- Branch: `feat/q8-quality-candidate`
- Development base: `main` (`0c6fa4b`, Core `v0.10.3`, including PR #299's
  `contracts/products.json` and `docs/products.md`, which this branch leaves
  byte-for-byte intact).
- Carried-forward mechanism commit: `q7` (`8ca82478`, PR #294), merged into this
  branch without rewriting it.
- Matcher revision: `wuwaterm-cjk-matcher-v9`
  (`bcb9fe68b1597a697596e250f969ea39d6ed086523d2b3771859c747d8d2bc8f`), unchanged
  from `q7`.
- Authorized live budget for the next stage: **at most `+54` POSTs** —
  Stage 1 `<= 12`, Stage 2 `<= 42` — against a cumulative ledger cap of **`569`**.
  `H2` stays `held-out used`; no `H3` split is created.
- Related: MY-18, MY-380, [ADR 0017](../adr/0017-contextual-term-references.md),
  [ADR 0019](../adr/0019-document-context-and-human-precedents.md),
  [ADR 0020](../adr/0020-held-out-usability-evaluation.md),
  [translation-reference-admission.md](../translation-reference-admission.md),
  [q7-candidate-plan.md](q7-candidate-plan.md), [next-batch.md](next-batch.md).

## 1. Prior Candidate Conclusion (`q7`)

`q7` (`8ca82478`, PR #294) is formally concluded **`No-Adopt`**. Its Mode A
`ordinary` fail-fast gate stopped the run at `4` POSTs (`469 -> 473`) against the
cumulative ceiling `515`, which the evaluator then closed out to `515/515`. The
two `zh->en` stop conditions held: `ordinary-zh-en` still rendered `心很重要` as
the character name, and `红椿蕊` still rendered with the character name. Its
mechanisms are nevertheless the proven starting point, so they are carried
forward rather than revived as a branch.

PR #294 stays open as the historical `No-Adopt` record. This branch never
pushes to it, never merges it, and never edits it.

## 2. What `q8` Adds On Top Of `q7`

Everything `q7` proved is carried forward unchanged: pure term classification
(`term_classification.py`, Architecture Rules #11/#12), the closed-class `…技能`
stem reference (`skill_references.py`), matcher `v9`'s one-character right
boundary with `"有"` (`cjk_span.py`), paragraph-level derived gating with
`single_newline_paragraphs=False` for context requests, scoped local-snippet
rendering (`term_preparation.py`), and selective in-place locking of mixed
one-character names (`_lock_mixed_single_char_names`).

On top of that, `q8` changes four behaviours: the two below aimed at the first
`D-003` failure, and the two in Section 2.3 aimed at what Stage 1 Run #1 then
showed.

### 2.1 Root cause 1 — `zh->en` never asked for placeholder preservation

`D-003` locks two `心` mentions inside parenthesised team formulas. `q7`'s
`zh->en` system prompt said only
`Keep all placeholders exactly unchanged.` next to a `Locked terms:` line that
spells out the same term's English. `deepseek-flash` under
`thinking: disabled` wrote `Hsin` where the token was, and the fail-closed
`LockedSentence.restore()` rejected the whole 200-OK answer as
`invalid_response / missing_placeholder`, which the pipeline reports as
`llm_unavailable`.

`en->zh` already had a strict protocol sentence for this. `zh->en` now gets the
same treatment **when the request carries at least one lock**:

> Placeholder tokens (such as `__WUWA_TERM_...__`) are mandatory protocol syntax,
> not natural-language output. Copy every placeholder token byte-for-byte
> exactly once (including inside parentheses or formulas like `'(TOKEN+A+B)'`).
> Never replace a placeholder with the official term shown under Locked terms;
> the server validates and restores official terms after your response.

Hard invariants, verified offline in Section 4:

- `zh->en` with **no** locks keeps its system prompt byte-for-byte identical.
- `en->zh` prompts are untouched in both cases.
- HTML markup requests keep their existing structural-placeholder sentence; the
  new sentence is plain-text only.

### 2.2 Root cause 2 — locking was decided by enclosed occurrences

`_lock_mixed_single_char_names` used to count **every** raw occurrence of the
character that was not an admitted mention. In `D-003`, 26 of 29 `心` sit inside
`心月狐`, `应世心`, `照世心`, `霆心域` and `核心` — terms that already own a
prompt line — so the lock decision was driven by 26 occurrences that cannot
prime anything. The function now counts only raw occurrences that **no longer
*global* reference encloses**: `_is_strictly_enclosed` against the occurrences
of every multi-character reference that `partition_references_for_prompt()`
keeps in the global block, the same test that function already uses. A longer
surface routed into the scoped block (a paragraph-gated derived name, a bounded
skill stem) is shown to the model as a local snippet keyed to its own admitted
occurrence, so it does not make the character behind it unambiguous and does
not suppress the lock.

`D-003` still has exactly **1** unenclosed `心`: a mid-document prose sentence
whose right neighbour `基` keeps `single_character_name_context_ok()` false, so
it is never admitted. Its two formula tokens therefore stay locked — and Root
cause 1 now tells the model to hand those tokens back untouched.
`single_character_name_context_ok()` is deliberately **not** relaxed for ordinary
words such as `心很重要` / `因为心很痛`.

This is a decision-basis fix, not a prompt change: on all 50 frozen
`D + ordinary + R + D-009-planB` documents it changes **zero** prompts
(Section 4), because every corpus document that locks a one-character name also
has at least one genuine unenclosed occurrence. Its effect is to stop
hard-locking a name in a document whose only other occurrences are inside longer
official terms.

### 2.3 Root causes 3 and 4 — what Stage-1 Run #1 actually showed

Stage 1 Run #1 spent 6 POSTs (`515 -> 521`) and was read in full. It cleared
the latency and control half of the gate: `D-009` answered in `2.9 s` with
`30/30` on its fixed 3-group split, `ordinary-zh-en` held **`6/6`**, `D-008`
answered in `2.1 s` and `D-012` in `2.6 s`. It did **not** clear the `D-003`
gate: the answer came back `200 OK` and was rejected as
`duplicate_placeholder` with `actual = 7`, and `ordinary-en-zh#p1` regressed
under `en_zh = disabled`.

**Root cause 3 — selective locking fought a longer reference.** `D-003` carries
`心月狐 = Hsin the Moon Fox` as a global reference, and `心 = Hsin` as a mixed
one-character mention. Locking `心` put `Hsin` in the prompt twice: once as a
placeholder's target, once inside `心月狐`'s own line. The model then wrote that
placeholder where `心月狐` belongs, reused it 7 times, and left `p41`
(`因为心基础数值最高`) unprimed enough to come back as `the heart`. `R1-A3` has
the same shape (`「心」 = "Hsin"`, `心团子 = Hsin Cube`).
`_lock_mixed_single_char_names` now leaves such a mention as a reference when a
longer **global** reference satisfies both conditions:

- the character is inside the longer source (`心 in 心月狐`), and
- the mention's official target appears in the longer target as a
  case-insensitive whole word (`\bHsin\b in "Hsin the Moon Fox"`).

`ordinary-zh-en` has no such pair (`心` / `椿` appear alone there), so it still
selectively locks `心` x1 and `椿` x3; the Run #1 `R1-B1` reference set has no
longer `椿` term carrying `Camellya` either, so its `椿` locks stay; unmixed
names are unaffected. `D-003` and `R1-A3` now carry **0 locks**, keep
`心 = Hsin` as an occurrence-scoped reference, and `D-003` keeps
`变奏 = Intro Skill` scoped.

**Root cause 4 — a competing `(speaker)` pair had no label rule.**
`ordinary-en-zh` offers `Tuner = 调音师 (speaker)` and `Tuner = 调谐器 (item)`.
The section `Use a Basic Tuner first, then the Tuner.` came back as
`...然后再用调音师` because `(speaker)` was listed first and nothing said a speaker
label is not an item. When — and only when — one surface carries a `(speaker)`
entry **and** at least one other kind, the prompt now appends, directly after
the competing-pairs instruction:

> When a term has both a (speaker) meaning and another meaning such as (item) or
> (core_term), use the (speaker) translation ONLY for a dialogue speaker label
> (such as 'Name: ...') or a speaking person, never for an item, material or
> gameplay object.

A request whose references carry no such competition keeps its exact prompt
bytes.

### 2.4 Observability (no API change)

`LockedSentence.restore()` already raised with a structured, sanitised
diagnostic (`reason`, `detail`, `expected_count`, `actual_count`). Two gaps on
the way out are closed:

- the swallowed-failure log lines in `sentence.py` / `application.py` printed
  only `reason`; they now carry `detail`, `expected` and `actual` (bounded
  vocabulary, never source or model text), so a rejected 200-OK answer is
  distinguishable from a provider failure after the fact;
- the same diagnostic already rides on `TranslationOutcome.llm_failure`, which
  is what an in-process offline harness reads for
  `llm_unavailable / missing_placeholder`.

No HTTP body, status, error envelope or OpenAPI change.

## 3. Stage Plan

`q8` has two independent stages, each gated on its own evidence. Stage 1 Run #1
has been executed (6 POSTs, `515 -> 521`) and is recorded in Section 2.3; the
Run #1 fixes on this branch are **offline-verified only**, so Stage 1 must run
again before Stage 2 can be judged.

| Stage | Question | POSTs | Contents |
| --- | --- | --- | --- |
| **1** | Does the `D-003` placeholder rejection disappear without regressing the controls? | `<= 12` | `D-003` once; `ordinary-zh-en`; `ordinary-en-zh`; paired `0.10.3` control where the gate needs a baseline |
| **2** | Does `en->zh` `thinking=disabled` beat `omit` on the segments that timed out or mis-scored? | `<= 42` | `D-009` full document plus its fixed 3-group diagnostic split, `ordinary-en-zh`, `D-008`, `D-012`, under `omit` then `disabled` |

Stage 1 acceptance (additive to the frozen full gates in
[next-batch.md](next-batch.md)):

- `D-003` returns a translation instead of `llm_unavailable`
  (`missing_placeholder` count `0` on this document), and its
  `变奏入场 -> Intro Skill` target stays;
- `ordinary zh->en` keeps `>= 5/6` (the same bar `q7` failed);
- `ordinary en->zh` does not regress against the `0.10.3` control.

Stage 1 is judged immediately after its own POSTs; failing it stops the run
before Stage 2 spends anything.

Stage 2 is enabled **only** by the environment variable
`WUWATERM_TRANSLATION_THINKING_EN_ZH`: the production default stayed `"omit"` in
`constants.py` and `settings.py` while Stage 2 ran, and flipping that default
was kept as a separate decision that only a passing Stage 2 could justify. That
decision is now made on the Phase 3 evidence in Section 7, and `q8r1` is the
candidate that carries it.

## 4. Offline Verification (0 Live Calls)

All of the following ran with sockets blocked and `httpx.MockTransport` in
place, over the frozen `D` (15 docs / 17 segments), `ordinary` (2 docs),
`D-009` Plan B (3 groups) and `R` (30 docs / 33 segments) inputs — 50 documents,
56 segments — against the `q7`-built dictionary (`2bc0154b…`, 12,097 rows).

| Check | Result |
| --- | --- |
| Segments whose request changes at all | **5 of 56**, all `zh->en` with non-empty locks (`D-003`, `ordinary-zh-en`, `R1-A3 s2`, `R1-B1 s1`, `R1-B1 s2`) |
| Change in those 5 | the protocol sentence only: locks, references, scoped blocks, `D-003`'s `变奏` coordinates and lock counts are identical |
| `zh->en` with zero locks | **prompt byte-identical** to `q7` (system-prompt SHA-256 unchanged for every such segment) |
| `en->zh` (all segments, both directions of locks) | **prompt byte-identical** to `q7` |
| Root cause 2 effect on the frozen corpora | zero prompt changes (every locking document keeps at least one unenclosed occurrence: `D-003` 1, `R1-A3` 8, `ordinary-zh-en` `心` 2 / `椿` 2, `R1-B1` 5 and 1); `R1-A5`'s unmixed `椿` stays a reference |
| `D-003` failure reproduction | a mock that writes the official English term where the token was yields `missing_placeholder` (expected 1, actual 0) on **both** `q7` and `q8` — the fix is the instruction, not a loosened restore |
| `restore()` round-trip on all 56 segments | unchanged from `q7`; still fail-closed |
| `scripts/validate.py` (6 steps) | green on this HEAD |

Owner-facing gates that stay frozen: model timeout `45 s` and request budget
`90 s` are untouched; `zh->en` thinking stays `disabled`; `en->zh` stays
`omit` unless Stage 2 runs it explicitly; `H2` remains `held-out used`.

### 4.1 Stage-1 Run #1 fix verification

That table describes the q7->q8 comparison made before Run #1. The two Run #1
fixes (Section 2.3) were verified separately, without live calls:

- unit tests over the real dictionary records: `心月狐 = Hsin the Moon Fox`
  skips the `心` lock, `心魔 = Camellya's Alter Ego` does not (its English lacks
  the whole-word target), `「心」 = "Hsin"` skips it, and a `Tuner` speaker+item
  pair emits the label rule while a plain reference set keeps the exact prompt
  bytes;
- a locally rebuilt dictionary (`3a5726ad9355…`, 12,089 rows) over documents
  shaped like the Run #1 inputs: `D-003` **0 locks** with `心 = Hsin` kept as an
  occurrence-scoped reference and `变奏 = Intro Skill` still scoped, `R1-A3`
  **0 locks**, `ordinary-zh-en` **unchanged** (`椿` x3 + `心` x1 locked).

The held-out bundles are not in this repository, so this branch reports the
lock/reference outcome by shape rather than by re-emitting corpus text or
claiming a byte-level manifest diff it cannot recompute here. Re-running the
Stage-1 manifest against the same frozen inputs is the first step of the next
authorized run.

## 5. Non-Goals

- No per-word hardcoding, no new prompt exception for one document, no
  `变奏` special case (it stays the closed-class `skilltype.json` stem rule).
- No timeout relaxation, no concurrency change, no model swap, no new service.
- No edit to `contracts/products.json`'s `web` entry, `docs/products.md`, or the
  external web consumer.
- No relaxation of `restore()`: a rejected answer still fails closed; only its
  diagnostic becomes readable.
- No merge. This branch is opened as a Draft and stays a Draft until a Stage 1
  run earns the next step.

## 6. `q8r1` — General `en->zh` Fast-And-Reliable Strategy (Offline Only)

Phase 1 measured 5 `en->zh` segments against the cumulative ledger (`540 -> 545`
of `569`) and separated one failure shape into two causes that pull in opposite
directions:

| Shape | Measured |
| --- | --- |
| `D-012`: 1,402 chars / 23 paragraphs / 2 locks | answered in `20.7 s` under `low`, `#p12` correct |
| `D-009`: 1,542 chars / 30 paragraphs / 11 locks | ran past the model timeout under thinking, `3.3 s` with it off |
| Plan B `[11-20]`: 498 chars / 10 paragraphs / 5 locks | ran past the model timeout under thinking, `3.3 s` with it off |
| `D-010` | the model merged the title paragraph into the first body paragraph (`2 -> 1`) |

So no single mode is the right answer for every request: thinking improves
quality on a document that answers inside the timeout, and costs the whole
segment on one that does not. `q8r1` therefore adapts per request instead of
picking one mode for the direction, and keeps the two corrections bounded.

### 6.1 Layer 1 — the request picks the mode (`auto`, opt-in)

`WUWATERM_TRANSLATION_THINKING_EN_ZH` gains `auto`. Into Chinese the default
stays `omit` — an unconfigured deployment keeps sending the provider-default
request it always sent, and `auto` is the value an operator sets to have the
process choose. It started as an opt-in while the live evidence was incomplete
(Section 7 is that evidence), because which of the two measured shapes dominates
a real workload could only be settled by running it. It has now been run: the
full held-out set scored at or above every frozen gate, including the long
documents that used to time out. The default stayed `omit` through the cut, on
the owner's direction: an unconfigured deployment is not the place to spend a
known timeout risk.

`select_en_zh_auto_thinking(prepared_text, lock_count=...)` is pure and
request-local:

- `lock_count >= 4` **or** `para_count >= 25` → `disabled`;
- otherwise → `low`.

Both bounds sit on the timeout side of the measurement above: 23 paragraphs with
2 locks answered in 20.7 s under `low`, while 30 paragraphs / 11 locks and 10
paragraphs / 5 locks did not answer at all. Locks count beside paragraphs because
a placeholder-dense prompt is the other shape that reproduces text slowly. An
explicit `omit`, `disabled`, `low` or `high` never reaches the helper — it is
returned unchanged, so any named mode replaces `auto` — and `auto` into English
stays an unknown value (there is no request shape there to adapt).

### 6.2 Layer 2 — at most one paragraph-count repair

`_regroup_lines_into_source_paragraphs` only moves blank lines; it cannot restore
a paragraph the model merged away, because the answer's lines no longer answer
the source's lines. A context-mode request whose answer has a different paragraph
count from its source (`len >= 2` on both sides, so a one-paragraph segment has
nothing to restore) therefore gets exactly one more call with the same prompt,
references and locks plus:

> The source has exactly N paragraphs separated by blank lines. Translate every
> title, heading, and paragraph and return exactly N paragraphs separated by
> blank lines.

The retry starts only with at least `15 s` of request budget left
(`request_trace.remaining_budget_seconds()`), is bounded by
`min(llm_timeout, remaining - 2 s)`, runs the caller's `before_llm_call` guard so
admission accounting and caps see it, and steps `disabled -> low` only under
`auto`, when the first attempt had no reasoning and the document carries fewer
than 4 locks. An explicit `disabled`/`omit`/`low`/`high` keeps its mode on the
retry too — the repair adds the paragraph count, it does not overrule a
configured mode. Anything
else — a matching count, too little budget, a refused guard, an error, or an
answer that still has the wrong count — returns the first attempt's translation.
A successful first call is never degraded into an `llm_unavailable` by the repair
meant to improve it.

Layer 2 and Layer 3 are one budget, not two: a segment gets **at most one
corrective call in total**, so it never costs more than two model calls. When
Layer 3 has already fired, the paragraph repair is skipped even if the fallback's
answer has the wrong count — otherwise a `low -> disabled -> low` chain would
spend the segment's last budget walking back into the exact mode that had just
timed out.

### 6.3 Layer 3 — at most one fast fallback on an `auto` timeout

Only an `en->zh` request whose configured mode is `auto`, that resolved to a
mode which asks for reasoning (`low` or `omit`), and that then raised
`LLMTranslationError(reason="timeout")` gets one shorter retry with thinking off:
the prompt is unchanged, the bound is `min(20 s, remaining - 2 s)` (or
`min(20 s, llm_timeout)` outside a request), it needs at least `10 s` left, and
it runs the same guard. An explicit `omit`/`low`/`high`/`disabled`, a `zh->en`
timeout, any other reason, a too-small budget, a refused guard, or a failed
retry re-raises the original timeout unchanged. `45 s` and `90 s` are both
untouched.

### 6.4 `zh->en` additive fidelity clause

A Chinese qualifier (`以上`, `超过`, `至少`, `约`) or a line that trails off is
meaning the English has to carry: rendered as a bare number, or completed into a
sentence, the answer has changed the claim. `zh->en` context requests therefore
append one sentence after the existing context rules:

> Preserve numeric qualifiers and bounds faithfully (e.g., more than, over, at
> least, about), and keep trailing ellipses and interrupted clauses intact
> without dropping or completing them.

Every existing rule is kept verbatim, and the `en->zh` prompt stays
byte-identical (the Chinese side of that pair is already the numerics rule).

### 6.5 Offline verification

| Check | Result |
| --- | --- |
| default | `omit` into Chinese sends no reasoning field and is byte-identical to the request before this setting existed; `=auto` opts into per-request resolution |
| `auto` on the measured shapes | `D-009` 11 locks/30p → `disabled`, Plan B 5/10 → `disabled`, `D-012` 2/23 → `low`, `D-010` 0/2 → `low`, `ordinary` 2/6 → `low` |
| explicit modes | `omit`/`disabled`/`low`/`high` bypass `auto` on the same shape and send exactly what they say |
| `en->zh` first call | byte-identical prompt, one POST, `extra_body` exactly what the resolved mode sends |
| paragraph repair | a `2 -> 1` answer is retried once with the count sentence, then accepted when it matches; every failure path returns the first answer |
| explicit `disabled` + repair | the retry still sends `thinking: {"type": "disabled"}` and only the count sentence is added |
| budget | `< 15 s` left skips the repair, `< 10 s` left skips the fallback, and both bounds shrink to the remaining budget |
| `zh->en` clause | present into English, absent into Chinese, and `en->zh` `_CONTEXT_RULES_INSTRUCTION` unchanged |
| `scripts/validate.py` (6 steps) | green on this HEAD |

`H2` stays `held-out used`. No live call is made by this commit; Section 7 is
the live evidence the strategy was adopted on.

## 7. Phase 3 — Live Acceptance (`568/569`, 23 POSTs)

`q8r1` ran on `c1bab1aa` with `WUWATERM_TRANSLATION_THINKING_EN_ZH=auto` and a
full-document denominator per arm. Ledger `545 -> 568` of `569`; no run failed
and no arm was re-run to reach a verdict. Scoring was doubled-blind and
independently arbitrated: `95.9%` judge agreement, `0/71` identical-output drift.

The left column of every pair below is the comparison baseline the frozen gate
uses. The `q8` `52042932` scores stay immutable and are **not** the comparison
basis of this table.

| Arm | Before | `q8r1` |
| --- | --- | --- |
| `ordinary` `zh-en` (6) | `3/6`, pollution `3` | **`6/6`, all A, pollution `0`, targets `6/6`** |
| `ordinary` `en-zh` (6) | `5/6`, critical `1` | **`6/6` (`5` A / `1` B), critical `0`, targets `6/6`** |
| `D` `zh-en` usable A+B (131) | `126/131` | **`128/131` (+2)** |
| `D` `zh-en` critical (131) | `7` | **`6` (-1, `0` number errors)** |
| `D` `zh-en` name targets (169) | `150/169` | **`153/169` (+3 fixed / `0` regressed)** |
| `D` `zh-en` semantic targets (6) | `1/6` | **`3/6`** (`D-004#p4:negation`, `D-005#p34:reference`, both mechanically confirmed) |
| `D` `en-zh` paired usable A+B (71) | `65/71` | **`65/71`** |
| `D` `en-zh` paired critical (71) | `6` | **`6`** (zero regression) |
| `D` `en-zh` full denominator (101) | `65/101` | **`93/101` (+28, `0` timeouts)** |
| `D` `en-zh` runtime (17 docs) | `221.4 s` | **`147.7 s`** |

Arm notes from the same run:

- `D-003` returned its full `45/45` in `7.5 s` under `auto`'s `low`, so the
  placeholder-protocol and locking-scope work of Sections 2.1-2.3 keeps holding
  on the arm it was built for.
- `D-010` restored `2/2`, i.e. the paragraph-count repair in Section 6.2 did put
  back the title paragraph that Stage 2 lost.
- `D-012` took `34.3 s` and `#p12` was fixed — the shape that `auto` sends with
  `low` stayed inside the model timeout at the upper end of the `low` band.
- `D-009` answered in `3.7 s` at `30/30` (`0/30` timed out), and both defects
  that arm had carried (`p0:meaning`, `p15:idiom`) passed: `auto` routed it to
  `disabled` on its `30` paragraphs and `11` locks.
- `D` `en-zh` runtime fell by a third for the same denominator because the arm
  that used to burn `45 s` per attempt on `D-009` now answers in `3.7 s`.

Targeted `R` regression check: the `4` of `34` segments whose request actually
changes (`R1-B1` with its `24 + 6` locks, `R1-A3#1`, `R1-A5`) completed `4/4`
with `0` placeholder rejections, `0` `Camellya` leaks, `0` ordinary-`心` leaks
and literal cues unchanged. **This is a targeted `4/34` check, not a rerun of the
full `R` set of `34/34` segments; the remaining `30` are unchanged by this
candidate and were not re-costed.**

### 7.1 Adopted configuration

- `WUWATERM_TRANSLATION_THINKING_EN_ZH` defaults to `omit`; `auto` is the
  opt-in that resolves a mode per request, every explicit
  `omit`/`disabled`/`low`/`high` overrides it, and the paragraph-count repair
  never changes an explicit mode (Section 6.2).
- Model timeout `45 s`, request budget `90 s`, `segmentation-v1` (`L = 1800`),
  the OpenAPI/HTTP contracts and the product contracts are unchanged.
- `H2` stays `held-out used`; the ledger closes at `568/569` with no further run
  planned.
