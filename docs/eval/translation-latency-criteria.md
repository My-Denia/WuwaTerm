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
