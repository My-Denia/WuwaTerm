# Translation latency and official names: frozen success criteria

- Status: frozen before any code change and before any candidate model call
  (MY-230, MY-18, MY-16 Q2/Q4). Not edited afterwards; a change needs a new
  entry that says what changed and why.
- Date: 2026-11-16
- Related: [ADR 0020](../adr/0020-held-out-usability-evaluation.md) (D set,
  runner, judging), [ADR 0018](../adr/0018-semantic-review.md) (thinking
  setting of the semantic review path)

## Problem

Translation requests carry no provider reasoning field, so the provider
default decides. The production model is DeepSeek `deepseek-flash`, whose
documented default is thinking mode at high effort. Long segments then run
past the 45 s model timeout:

- D baseline (Core `e97b997`, production settings): 5 of 19 attempted calls
  timed out, all zh→en; 89 of 128 scored zh→en paragraphs are `run_failed`.
  The judged zh→en paragraphs are 38 of 39 usable; en→zh is 97 of 99.
- H1 baseline (aggregate from the evaluator): 12 of 35 calls timed out, all in
  community guide or mixed documents with segments near 1,800 scalars.
- Earlier runs of the same segment path on the probe documents below (120 s
  timeout, 2026-11-13 and 2026-11-15 ledgers): 12 of 26 calls took longer than
  45 s, up to 81.6 s, including a 466-scalar segment at 46 s and 52 s.

The cause is not asserted here. These criteria decide whether a change fixes
the waiting and timeouts without lowering translation quality.

## Settings under test

Every run uses the production model, the production API timeout (45 s, from
the production environment), the production LLM concurrency, and the ADR 0020
runner (`_translate_long_plain_text`, empty `TranslationContext`,
segmentation-v1 with L = 1800). Every attempted POST counts against the Q2
budget (at most 150 calls for Q2 and Q4 together).

Arms for the translation request:

- `disabled`: `"thinking": {"type": "disabled"}`.
- `low`: `"thinking": {"type": "enabled"}` and `"reasoning_effort": "low"`.
- `omit`: no field (the current behaviour).

The semantic review path keeps its own setting and is not part of this change.

### Arm selection rule

1. Measure `disabled` first, on D and on the probe set.
2. If `disabled` meets every latency and quality criterion, it becomes the
   default.
3. If `disabled` meets the latency criteria but fails a quality criterion,
   measure `low`. If `low` meets every criterion, it becomes the default.
4. Otherwise nothing ships as a default change: MY-230 is reported as
   needs-replan with the measurements. Thresholds are not changed.

## Probe set

R1 is the Set 1 documents A1–A6 and B1–B3 of the 2026-11-13 evaluation
(Chinese source, translated zh→en; earlier tuning material, not held out):
16 segments, 7 of them between 1,000 and 1,800 scalars (1,797, 1,789, 1,800,
1,624, 1,508, 1,354, 1,023). R2 is the Set 2 sentences S01–S18, one document
each, used only for the regression guard. The run inputs are built
mechanically from `translation-eval-20261113/work/vps_inputs.json`
(SHA-256 `88f4d377…56128`), text unchanged, as `usability-run-inputs-v1`
with SHA-256 `506a4f0c2f080f7aa7ecb10ca96516dc58bee17ab63223230f4200463e320aaf`.
English long-segment timing comes from the D en→zh documents (segments of
1,244, 1,402 and 1,542 scalars).

## Latency criteria

- L1: 0 timed-out calls on the D documents.
- L2: 0 timed-out calls on R1.
- L3: per-call p95 at most 30 s over all candidate calls of the D run and
  the R1 calls together (nearest rank; a failed call counts with its elapsed
  time). p50, p95 and the maximum are also reported per direction.
- L4: a timeout still answers a retryable reason. The API returns
  `llm_unavailable` with HTTP 503 (documented as retryable). A public request
  is limited to 2,000 input scalars, so it makes one model call, and the 45 s
  model timeout fires before the 90 s request budget. Web and Windows already
  keep finished segments. A test asserts the mapping; no new state is added.

## Quality criteria

D is judged with [usability-judging](usability-judging.md) unchanged, two
judgments per item, and an adjudication where they differ. The candidate is
compared with the baseline per direction.

- Q1: the usable rate over all scored D paragraphs is at least the baseline
  rate (zh→en 38/128, en→zh 97/99).
- Q2: on the paragraphs the baseline judged (zh→en 39, en→zh 99), the
  candidate's usable count is at least the baseline's minus 2 (zh→en ≥ 36,
  en→zh ≥ 95).
- Q3: paragraphs with a critical error, on those same paragraphs, are at most
  the baseline's plus 2 (zh→en ≤ 3, en→zh ≤ 3).
- Q4 (regression guard, frozen scorer and old literal cue list, unchanged):
  M-Gold at least 670/675, Set 1 standing terms at least 119/124, Set 2 terms
  at least 38/39. R2 runs through the ADR 0020 runner, which adds the
  empty-context rules to each sentence's prompt; the earlier Set 2 runs used
  the short-request path without them. If R2 falls below 38/39, the
  difference is examined before it is attributed to the change.

The tolerance in Q2 and Q3 exists because the baseline is itself one sample.
In thinking mode the provider ignores `temperature` (DeepSeek thinking-mode
guide), so the baseline outputs were sampled at the provider's own
temperature, and a rerun of the unchanged baseline would also move some
paragraph grades. Two paragraphs is 2% of the en→zh paragraphs. The tolerance
is set before any candidate output exists.

## Official names (the change after the latency change)

A dictionary or matcher change must be general: it adds or matches a class of
upstream names, never a per-word list. It is accepted when:

- N1: D official-name accuracy per direction is at least the baseline
  (zh→en 42/46, en→zh 15/28), and each D miss the change targets is listed
  with the change that explains it.
- N2: Q1, Q2, Q3 and Q4 above hold for the candidate that carries both
  changes, measured against the same baseline.
- N3: every matcher change has a regression test where the same surface is an
  ordinary word (MY-18), on synthetic or pre-3.7 text.
- N4: the dictionary diff (`scripts/diff_terms_db.py`) is reported with its
  row counts per category.

## Replan, 2026-11-16: per-direction arm

Written after the rule above was applied and before any call of the new arm.
The sections above are unchanged.

### What the rule found

All calls went through the ADR 0020 runner at production settings (45 s
timeout) and are in the evaluation ledger log.

| Criterion | `disabled` (D 17 + R 34 calls) | `low` (R 34 calls) |
| --- | --- | --- |
| L1, L2 timeouts | 0 | 0 on R1 |
| L3 pooled p95 | 8.6 s (max 9.2 s) | not met: 2 of 16 R1 calls over 30 s (40.8 s, 33.5 s) |
| Q1 zh→en | 122/128 (baseline 38/128) | not run |
| Q1 en→zh | not met: 94/99 (baseline 97/99) | not run |
| Q2 zh→en / en→zh | 36/39 / not met: 94/99 | not run |
| Q3 | 3 / 1 | not run |
| Q4 M-Gold, Set 1, Set 2 | not met: 664/675; 120/124; 38/39 | 671/675; 119/124; 38/39 |

The 35 baseline items re-judged blind in the same round were all usable, so
judge drift does not explain the en→zh loss. Rule step 4 applied: the shipped
default stays `omit`.

### What changes and why

- Thinking is set per direction. Chinese→English requests use `disabled`;
  English→Chinese requests stay `omit`, byte-identical to today. The timeouts
  happen only on long zh→en segments, where `disabled` removed them; en→zh had
  no timeout problem and lost quality under `disabled`.
- In zh→en prompts, a reference whose source is one Chinese character says
  that it applies only where the character alone names the term, not where
  the character is part of another word. Under `disabled`, three M-Gold
  losses were the ordinary 椿 inside a plant-derived mechanic name rendered as
  the resonator Camellya (MY-18); thinking used to absorb that ambiguity. The
  matcher, its revision and the plant-compound and lowercase rules are
  unchanged; no word list is added.

### Frozen criteria for the per-direction arm

- P-L1: 0 timeouts on every zh→en call of D and R1 at production settings. A
  call answered from an earlier output under the full ADR 0020 reuse key
  counts with the outcome and elapsed time of the call that produced it (the
  same request in the `disabled` run).
- P-L2: pooled p95 at most 30 s over those calls (nearest rank).
- P-Q1: D zh→en usable rate on the full D zh→en set (128 scored paragraphs;
  `run_failed` counts as not usable) at least the baseline's 38/128. The
  39-paragraph baseline-judged subset is reported beside it as information,
  not as a gate.
- P-Q2: no new C-grade class, meaning no systematic error class introduced
  by the arm (for example an ordinary word rendered as an official name, or
  clauses dropped across many segments). Single C grades on resampled outputs
  are reported item by item and already count in the usable rate.
- P-Q3: en→zh requests are byte-identical to the baseline: every en→zh call
  of the D run is answered from the baseline output under the full reuse key
  (0 calls), so en→zh results equal the baseline's.
- P-Q4: the M-Gold score under the frozen cue list is reported exactly. The
  guard passes if that score is at least 670/675, or if a blind adjudication
  finds that the candidate's count of real meaning errors on M-Gold items is
  at most the baseline's. The adjudication takes every M-Gold miss of the
  baseline (the five misses of 670/675) and of the candidate, hides which
  arm each came from, and has independent judges in fresh contexts classify
  each as a real error (an ADR 0020 C trigger) or as meaning kept with
  different cue wording; a disagreement gets a third judgment with a reason.
  Set 1 at least 119/124 and Set 2 at least 38/39 as before.

Why the M-Gold alternative: changing the thinking setting resamples every
output, and literal cue matching counts reworded but correct negations as
losses (3 of the 9 `disabled` losses). A symmetric blind comparison keeps
the guard about real regressions. The cue list is not changed.

Decided by the batch planner on 2026-11-16: the per-direction arm, the
reading of "C-grade class" (a systematic class, not single resampled C
grades, because the 89 formerly failed paragraphs have no baseline output to
compare against), and the M-Gold alternative.

Projection, written before the run: only segments with a one-character name
reference change (R1-A3 segments 2–3, R1-A5, R1-B1 both segments, D-003);
every other call is reused. The literal M-Gold score is expected at 667–669:
the A1 Rover loss sits in a reused segment, and 670 needs every B1 loss back,
including three reworded negations.

### Official names, measured on their own

The dictionary change is a separate branch on `main` with the shipped
default. Comparing the request bodies against an echo gateway, old and new
dictionary, it changes only D-004 segment 1 and D-009 segment 1 and no R
request. So N2 is measured on D-004 and D-009 only: their usable count is at
least the baseline's minus 2, and R is unaffected by construction.

### Correction before the first call of the per-direction arm

A dry run against the echo gateway showed that three en→zh D segments
(D-009, D-012, D-014) cannot be answered from the baseline under the full
reuse key, although their code path is unchanged: every request with a hard
lock gets a fresh random placeholder prefix (`_new_placeholder_prefix`), so
its body never repeats. P-Q3 is therefore shown in two parts. The six en→zh
segments without a lock are answered from the baseline output (0 calls). For
all nine en→zh segments, the request bodies of the baseline code (`e97b997`)
and of the candidate, with the placeholder prefix fixed, are byte-identical
(9 of 9 SHA-256 equal). The en→zh documents are not sent again, and en→zh
results stay the baseline's. Only the zh→en D documents run.
