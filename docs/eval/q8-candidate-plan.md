# q8 Translation Quality Candidate Plan & Frozen Offline Scope

- Status: **Draft candidate, offline verification only (0 live LLM calls in this
  PR).** No Stage-1 or Stage-2 budget has been spent on this branch.
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

On top of that, `q8` changes exactly two behaviours, both aimed at the same
`D-003` failure.

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

### 2.3 Observability (no API change)

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

## 3. Stage Plan (Not Executed Here)

`q8` is proposed with two independent stages, each gated on its own evidence.

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
`WUWATERM_TRANSLATION_THINKING_EN_ZH`: the production default stays `"omit"` in
`constants.py` and `settings.py`. Flipping that default is a separate decision
that only a passing Stage 2 can justify.

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
