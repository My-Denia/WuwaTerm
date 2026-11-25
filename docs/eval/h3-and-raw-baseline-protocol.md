# H3 Held-Out Usability & Raw-Model Baseline Evaluation Protocol

- Status: **Frozen design spec — `Pending Owner Authorization` (0 live LLM calls executed)**.
- Revision 2026-11-26 (before any `H3` call): pins Core `v0.13.0`, `wuwaterm-cjk-matcher-v10`, the production dictionary and the frozen evaluation tools (§0); adds the `en->zh` `raw_matched` thinking ablation (§3.2), the paired comparison metrics (§4.1) and a pre-registered decision rule (§4.2); re-itemizes the call budget and defers Arm C at `0` POSTs (§6).
- Target Core under test: `v0.13.0` (tag commit `e5b7f1a84d04deb5795296b1a652e13c4e9fd1a8`, `src` tree `9e23fd5975caeab024dcb00267e20a6266f4678e`), `wuwaterm-cjk-matcher-v10`, pinned `3.7.0` / `3.7.8` data commit `9218d612ad815e398e064e577e42aaf878899968`. Full bindings in §0.
- Ledger separation: **Strictly separate** from the completed `q8` / `v0.11.0` ledger (`602/606` closed). A fresh `H3` ledger (`h3-ledger.json` + append-only `h3-ledger-log.jsonl`) is initialized only after owner authorization.
- Precondition: `MY-229` (`scripts/eval_semantic.py` audit) — **satisfied** by #316 (`5bc809c`, merged to `main` 2026-11-25).
- Related: `MY-380`, `MY-18`, `MY-229`, `MY-489`, [ADR 0017](../adr/0017-contextual-term-references.md), [ADR 0019](../adr/0019-document-context-and-human-precedents.md), [ADR 0020](../adr/0020-held-out-usability-evaluation.md), [ADR 0021](../adr/0021-local-user-termbase.md), [usability-judging.md](usability-judging.md), [q8-candidate-plan.md](q8-candidate-plan.md).

## 0. Pinned Bindings

Every value below is hashed into `h3-ledger.json` before the first live POST. Any change to the Core tree, the dictionary or a frozen tool after that point makes a new candidate: it needs a new `--fake-transport` dry run and a protocol revision before any further live call.

| Binding | Value | Where it comes from |
| --- | --- | --- |
| Core release under test | `v0.13.0` ([release](https://github.com/My-Denia/WuwaTerm/releases/tag/v0.13.0); lightweight tag) | GitHub release, published 2026-11-25 |
| Tag commit | `e5b7f1a84d04deb5795296b1a652e13c4e9fd1a8` | `git rev-parse 'v0.13.0^{commit}'` |
| `src` tree under test | `9e23fd5975caeab024dcb00267e20a6266f4678e` | `git rev-parse 'v0.13.0:src'`; the runner refuses to start unless the bound tree's `HEAD:src` equals this value |
| Matcher | `wuwaterm-cjk-matcher-v10`, `MATCHER_REVISION = 57428b88978597d95840f56ba0080a4f8fb11d4860fe8ea5532d3c1e7c6a918a` | `wuwaterm.cjk_span.MATCHER_REVISION`, identical at the tag and at `main` `486312d` |
| Game data | `3.7.0` / `3.7.8`, data commit `9218d612ad815e398e064e577e42aaf878899968` | dictionary build pin |
| Production `terms.db` file | SHA-256 `9ddee8197fbd01ed283f948e6a77e5067485d47daba24553582dcf309fba1f18` | `sha256sum` of the production `data/terms.db` |
| Dictionary revision | `d9f5e4ae93755b1479943dfff8adf176c461a23bd0d8a337d39d28ea7051f2a5`, with `term_count = 12097`, `source_commit = 9218d612ad815e398e064e577e42aaf878899968`, `schema_version = 2` | the `dictionary` object of a production `/v1/reviews` `review-v2` response (`review._dictionary_revision`, a `wuwaterm-review-dictionary-v1` hash over the term rows); `/v1/meta` does not carry this value |
| Semantic evaluation fingerprint | `FINGERPRINT_VERSION = 4` | `scripts/eval_semantic.py` |
| Scorer | `scripts/eval_usability.py` SHA-256 `8b17d3ae75f7aafebaf537246fcaa1e56c1e52cb1a4bf5c875c599c42622d796` | `main` `486312df532a3757f367d523d6f7a7cdd5735f96` (includes `paired-compare`, #321) |
| Semantic evaluation runner | `scripts/eval_semantic.py` SHA-256 `e0677dc797b5dfa3a23c336b56f95408a1a3127333462a8032a03b3983a21c91` | `main` `486312d` (includes the `MY-229` hardening, #316); byte-identical at the tag |

Notes on the bindings:

- The Core tree under test and the evaluation tools come from different commits on purpose. At the tag, `scripts/eval_usability.py` is `7fee9b04e0de22b65112dca2d1b9581f6b6331cc10ee3bef0c30f48f3d5b9614` and has no `paired-compare`; the scorer is therefore taken from `main` `486312d`. That commit's `src` tree (`49cc2670fd36df715b449ad85f8063adecd60e5c`) adds #323 (request-local evidence for ambiguous single-character mentions) and is **not** the tree under test.
- The q7 and q8 plans cite `2bc0154b…` for the dictionary. That value is the SHA-256 of the `terms.db` file built locally in q7, not a dictionary revision. `H3` binds the production file and the `review-v2` revision in the table above.

Pre-freeze deliverables that are not in the repository at `486312d`; each is written, dry-run and SHA-256 hashed into `h3-ledger.json` before the first live POST:

1. **`H3` runner**: the `raw_baseline` minimal harness (§3.1), the `wuwaterm_prod` segment path bound to the tree above, the `raw_matched` mode copy (§3.2), an offline `--fake-transport` dry run, a `--max-calls` stage cap, and POST counting through the transport pre-send hook.
2. **Arm-scores exporter**: turns resolved final judgments and per-direction telemetry into one `usability-arm-scores-v1` file per arm, plus `en->zh`-only files for the `raw_matched` comparisons (§4.1). The scorer only consumes this schema; nothing produces it yet.
3. **`H3` judging addendum**: the community-term judging rules of §5.3.

## 1. Status of Prior Held-Out Corpora (`H1` and `H2`)

- **`H1` and `H2` remain permanently marked `held-out used`.**
- Both sets remain useful as frozen diagnostic and regression corpora only; neither may ever be re-claimed as a blind acceptance test.
- Because `H1` and `H2` share `1,841` uncleared 12-scalar windows (predominantly `1,623` windows in announcement templates per [ADR 0020](../adr/0020-held-out-usability-evaluation.md#acceptance-and-report-states)), `H3` introduces a clean, strictly non-overlapping held-out corpus designed to measure both direct-publishable quality (`A`-rate) and real-world value over a bare LLM baseline.

## 2. Fresh Held-Out Corpus `H3`

### 2.1 Storage and Access Control

- All raw `H3` documents, manifests, name/term annotations, arm outputs, and judge packets live in a private evaluation directory **outside every repository and worktree**.
- Only frozen protocol specifications, aggregate metric tables, non-text window-scan counts, and SHA-256 hashes enter this repository or the evaluation ledger.
- Fixers and engine contributors never read `H3` source texts, references, or raw judge packets prior to freeze sign-off.

### 2.2 Genres and Sample Weighting

`H3` retains the four mechanical genre definitions from [ADR 0020](../adr/0020-held-out-usability-evaluation.md#genres-and-sources), weighted to address the real user bottlenecks identified in `MY-380` and `H1` (where `zh->en` guides scored `71/87`):

| Genre | Code | Source & Scope | Weighting Rationale |
| --- | --- | --- | --- |
| 攻略 Guide | `guide` | Community builds, rotations, mechanics, walkthroughs, farming, pulling advice (TapTap `zh`, Steam guides `en`), rich in shorthand (`3+1`, `S0R1`), skill stems, and community jargon | **Weighted highest on `zh->en`** (`>= 50` scored units) to target the `71/87` `H1` bottleneck |
| 公告 Announcement | `announcement` | Official Kuro website announcement JSON (`zh`/`en` editions), using distinct non-boilerplate sections where possible | Measures official terminology and formatting fidelity |
| 剧情 Story | `story` | Dialogue and narration scenes from pinned `3.7` TextMaps (`9218d612`) disjoint from all earlier sets | Measures pronoun/elided-subject continuity, speaker disambiguation, and tone |
| 混合语境 Mixed | `mixed` | Community discussion, reviews, UI/event notices mixing official names with everyday words (`心`, `椿`, `tuner`, `energy core`) | Measures homograph isolation and zero false-positive priming |

### 2.3 Target Sample Counts and Scored Units

Every document is segmented identically by `segmentation-v1` (`L = 1800`, `wuwaterm.segmentation.segment_text`) before any model call. Scoring operates on individual **scored units** (natural paragraphs / standalone single-paragraph segments after `ADR 0020` exclusions: dictionary-answered alone, no-letter, or duplicate), packed into `L = 1800` wire segments so `Raw` and `WuwaTerm` share exact 1:1 segment and unit denominators:

- **Per direction (`zh->en` and `en->zh`)**: **`>= 140` scored units** per direction.
- **Per genre in each direction**: **`>= 30` scored units** per genre (`announcement`, `story`, `guide`, `mixed`).
- **`zh->en` guide floor**: **`>= 50` scored units** in `guide` (leaving `>= 90` across the remaining three genres, e.g., `50` guide + `30` announcement + `30` story + `30` mixed = `140+`).
- Target document packing: ~`45`–`50` `L = 1800` wire segments per direction (~`90`–`100` wire segments per full 2-direction arm).

### 2.4 Pre-Freeze Deduplication and Freshness Rules

Before the **first** live model call of `H3`, an automated pre-freeze check must pass with **zero** violations:

1. **Zero shared 12-scalar windows**: Run the 12-Unicode-scalar sliding window check (`scripts/eval_usability.py` `scan` semantics, raw text without normalization) of every `H3` candidate source and reference paragraph against **all** prior evaluation corpora: `H1`, `H2`, `D`, `R`, `A1–A6`, `B1–B3`, `S01–S18`, `M-Gold`, and the Core source tree at the freeze commit. **Requirement: `0` shared uncleared windows** (candidates with shared boilerplate or overlapping windows are pruned or replaced before freeze, never carried forward like `H1`/`H2` announcement templates).
2. **Source ID and key disjointness**: Every Kuro article ID, TextMap key, story quest line, and community URL/author series appearing in `H1`, `H2`, `D`, `R`, `A1–A6`, `B1–B3`, `S01–S18`, or `M-Gold` is excluded.
3. **Cutoff and version isolation**: Community texts and announcements must satisfy the pre-`3.8` window (before `2026-11-06 19:45 +08:00`) and contain **no mention of game version `3.8` or later**, matching the pinned `3.7.0 / 3.7.8` dictionary (`9218d612`).
4. **Intra-set deduplication**: Zero normalized duplicate paragraphs within `H3` (NFKC + casefold + strip whitespace/punctuation), and each bilingual pair appears in one direction only.
5. **PII / ID scrubbing**: Player UIDs, contact handles, and personal identifiers are removed before freeze; any document that cannot be cleaned cleanly is discarded.

## 3. Experimental Arms (2-Arm Primary + `en->zh` `raw_matched` Ablation; Arm C Deferred)

All arms run in this revision (`raw_baseline`, `raw_matched`, `wuwaterm_prod`) receive the **exact same `segmentation-v1` (`L = 1800`) segment cores** and **exact same production gateway, model, temperature (`0`), and timeout limits (`45 s` model timeout `WUWATERM_LLM_TIMEOUT_SECONDS`, `90 s` request budget)** so segment counts and denominators match 1:1.

| Property | Arm Raw (`raw_baseline`) | Ablation Arm (`raw_matched`, `en->zh` only) | Arm WuwaTerm (`wuwaterm_prod`) |
| --- | --- | --- | --- |
| Code / Pipeline | Standalone minimal harness calling `llm_transport.post_chat_completion` directly | Same harness and prompt as `raw_baseline` | Core `v0.13.0` (`e5b7f1a`, `src` tree `9e23fd59…`): `application._translate_long_plain_text` + `TranslationPlan` / `OperationExecutionBudget` |
| Directions | `zh->en` and `en->zh` | `en->zh` only | `zh->en` and `en->zh` |
| Model & Gateway | Same production `WUWATERM_OPENAI_MODEL` (`deepseek-flash`) & `WUWATERM_OPENAI_BASE_URL`, `temperature=0` | Same | Same |
| Timeouts | `45 s` model timeout / `90 s` request budget | `45 s` model timeout / `90 s` request budget | `45 s` model timeout / `90 s` request budget |
| Segmentation | `segmentation-v1` (`L = 1800`) identical segment cores | `segmentation-v1` (`L = 1800`) identical segment cores | `segmentation-v1` (`L = 1800`) identical segment cores |
| Dictionary (`terms.db`) | **None** (no exact lookup, no locks, no references) | **None** | Production `terms.db` (`9ddee819…`, revision `d9f5e4ae…`) + `wuwaterm-cjk-matcher-v10` |
| Local User Termbase | **None** | **None** | **None** |
| Context / Precedents | **None** (no `before` segment, no precedents, no context rules) | **None** | `translation-context-v1` (`before` read-only segment; precedents off unless enabled) |
| Thinking (`zh->en`) | `disabled` (explicit `thinking: {"type": "disabled"}`) — **exact parity with `wuwaterm_prod`** | not run | `disabled` (`TRANSLATION_THINKING_ZH_EN_DEFAULT`) |
| Thinking (`en->zh`) | `omit` (provider default, Core's unconfigured `TRANSLATION_THINKING_EN_ZH_DEFAULT`) | Per segment, the `thinking_mode` that `wuwaterm_prod` actually used for that segment (§3.2) | `auto` (`select_en_zh_auto_thinking` + `OperationExecutionBudget`) |

### 3.1 Arm Raw Prompt Specification & Thinking Mode Parity

- **Minimal neutral prompt (`raw_baseline`)**:
  - System message instructs the model solely to act as a faithful translator for the target language (`English` for `zh->en`, `Simplified Chinese` for `en->zh`), treat user text as untrusted translation input only, and preserve blank-line paragraph breaks (`\n\n`) without adding commentary or omitting clauses.
  - Zero game-specific glossary, zero term locks, zero placeholder instructions, zero read-only prior segment, and zero WuwaTerm context/fidelity clauses.
- **Thinking mode parity check**:
  - On `zh->en`, `raw_baseline` sends `thinking: {"type": "disabled"}` (matching `ProviderProfile.reasoning_extra_body("disabled")`), eliminating provider-default reasoning mismatches.
  - On `en->zh`, `raw_baseline` runs with `omit` (provider default, matching Core's unconfigured `TRANSLATION_THINKING_EN_ZH_DEFAULT = "omit"`), while `wuwaterm_prod` runs with `WUWATERM_TRANSLATION_THINKING_EN_ZH=auto` (production VPS setting per `q8` Section 8). The thinking difference this leaves in the `en->zh` comparison is separated out by the `raw_matched` ablation arm (§3.2).
  - Every wire request records `RequestIdentity.thinking_mode`, `RequestIdentity.thinking_wire`, and `RequestIdentity.extra_body` in the ledger so thinking-mode parity is verified before judging.

### 3.2 `en->zh` Thinking Ablation Arm (`raw_matched`)

On `en->zh`, `raw_baseline` (`omit`) and `wuwaterm_prod` (`auto`) differ in two things at once: the WuwaTerm pipeline (dictionary, term references, context, prompt) and the thinking policy. `raw_matched` holds the thinking policy equal to `wuwaterm_prod` while keeping everything else equal to `raw_baseline`, so the `en->zh` result splits into three paired comparisons (§4.1):

- `raw_baseline` → `raw_matched`: the thinking-policy effect on the bare model.
- `raw_matched` → `wuwaterm_prod`: the WuwaTerm pipeline net effect at equal thinking.
- `raw_baseline` → `wuwaterm_prod`: the end-to-end production effect.

Frozen rules:

1. **Coverage**: every `en->zh` segment of the frozen manifest, so `raw_matched` has the same `en->zh` scored units as the other two arms (`>= 140`). `raw_matched` sends no `zh->en` request.
2. **Prompt and harness**: byte-identical to `raw_baseline` (§3.1). Only the thinking field differs.
3. **Mode source**: for each segment, `raw_matched` sends the `RequestIdentity.thinking_mode` that the `wuwaterm_prod` ledger records for the request whose output `wuwaterm_prod` returned for that segment: the initial request, or the corrective request when an `OperationExecutionBudget` corrective call (timeout fallback `low` → `disabled`, or paragraph repair `disabled` → `low`) produced the returned output. The wire field is built by the same `ProviderProfile.reasoning_extra_body(mode)` call the product path uses. If `wuwaterm_prod` sent a request for a segment but returned no output for it, even after its document's single rerun, that segment's initial request mode is used.
4. **No re-selection**: `raw_matched` never calls `select_en_zh_auto_thinking` on the raw text. Raw requests carry no term locks, so the lock count, and with it the selected mode, would differ from what `wuwaterm_prod` used.
5. **Order**: the `en->zh` `wuwaterm_prod` run finishes and its ledger entries are hashed before the first `raw_matched` POST.
6. **No mirrored corrective calls**: `raw_matched` makes no corrective call of its own; like `raw_baseline`, a failed document is rerun once from the start (§6.3).
7. **Unmatched segments**: a segment for which `wuwaterm_prod` made no model request (wholly a dictionary exact hit, or never reached because its document stopped at an earlier failed segment) is sent with `omit` and marked `unmatched` in the ledger.
8. **Parity check before judging**: every `raw_matched` request's recorded `thinking_mode` equals the copied mode (`0` mismatches required); the report lists the per-mode segment counts (`low`, `disabled`, `unmatched`) and the number of segments whose initial and returned `wuwaterm_prod` modes differ.

### 3.3 Arm C (`wuwaterm_cand`) — Deferred

Arm C (the `ADR 0021` `user_glossary` termbase references, `zh->en` adaptive thinking, and a bounded read-only section/entity anchor) is **deferred: `0` POSTs in this revision**. Under [ADR 0021](../adr/0021-local-user-termbase.md#consequences), Core engine adoption of any of these three stays blocked until Arm C is evaluated under frozen `H3`; that evaluation needs its own protocol revision, `--fake-transport` dry run, budget and owner authorization, after the 2-arm and ablation results are reported.

## 4. Pre-Freeze Scoring Metrics

All metrics are computed per direction (`zh->en` and `en->zh`) and per arm, never pooled across directions. Genre breakdowns are reported alongside totals.

1. **Direct-publishable `A`-rate (primary quality differentiation metric) & `A+B` usable rate**:
   - Report **`A`-rate** (`A / N_scored`) **separately** from **`A+B` usable rate** (`(A + B) / N_scored`), each with **Wilson 95% confidence intervals** (`z = 1.959964`), document count, and genre breakdown.
   - `undecidable` and `run_failed` remain in the denominator `N_scored` and count as neither `A` nor `usable`.
2. **Critical error rate & breakdown**:
   - Share of scored units with `>= 1` critical error, AND critical errors per **1,000 source Unicode scalars**.
   - Broken down across six categories:
     - `accuracy` (`meaning_shift` + `addition`),
     - `negation`,
     - `number` (quantities, units, dates, versions, numeric shorthands like `3+1` / `S0R1`),
     - `reference` (pronouns, elided subjects, speaker/actor inversion),
     - `omission`,
     - `term` (misleading official or community term error).
3. **Official term accuracy vs. community/guide term accuracy (scored separately)**:
   - Pre-freeze annotators tag each scored unit's occurrences into two disjoint annotation sets:
     - **Official terms** (grounded in pinned `3.7` TextMap pairs / `ADR 0020` rules): accuracy (`official / total_official`) and in-document consistency.
     - **Community / guide terms** (pre-frozen community lexicon / mechanics terms in `guide` and `mixed`, e.g., shorthand, slang, combat roles): accuracy (`accepted / total_community`) and misleading-error rate, reported **separately** from official terms.
4. **Estimated human revision cost**:
   - **MQM-weighted severity points** (computed deterministically by the scorer per unit and per 1,000 source scalars from judged items, frozen prior to live calls):
     - Grade `A` (natural, 0 errors): `0` points;
     - Grade `B` / `stiff` style note (`1` point, applied at most once per Grade `B` or Grade `C` unit; subsumed when severe `disfluent`);
     - Non-misleading `not_official` / minor term mismatch: `2` points;
     - `disfluent` or `misleading` term error: `5` points;
     - Critical error (`omission`, `addition`, `meaning_shift`, `reference`, `number`, `negation`): `10` points per critical error;
     - `run_failed`: scored separately and noted as full re-translation cost (`max_unit_penalty`).
   - **Normalized post-edit distance proxy**: character/token Levenshtein edit ratio against minimal post-edited publishable references on a pre-frozen post-edit audit subset (or against aligned official reference where applicable, explicitly labeled as a proxy).
5. **End-to-end latency**:
   - `p50`, `p95`, and `max` latency in seconds per wire segment and per document (from request start to completed outcome, including any `OperationExecutionBudget` corrective retry).
6. **Actual model POST count & token cost**:
   - Total gateway HTTP POST attempts (logged by transport pre-send hook, including corrective calls and failures) and cumulative prompt / completion / reasoning tokens per arm and direction (reported from gateway `usage` fields; marked `unknown` if omitted by the gateway).

### 4.1 Paired Comparison Metrics (`paired-compare`)

Arm-to-arm comparisons are computed by the frozen scorer (§0), never by hand:

```text
python scripts/eval_usability.py paired-compare \
  --arm-a <baseline-side arm>.json --arm-b <treatment-side arm>.json \
  --seed 0 --bootstrap-samples 10000 --json-out <comparison>.json
```

Inputs are `usability-arm-scores-v1` files from the arm-scores exporter (§0); the output schema is `usability-paired-compare-v1`. Every delta is `arm_b − arm_a`, so `--arm-a` is always the baseline-side arm.

| Comparison | Direction | `--arm-a` | `--arm-b` | Role |
| --- | --- | --- | --- | --- |
| WuwaTerm vs raw | `zh->en` | `raw_baseline` | `wuwaterm_prod` | Primary (`zh->en`) |
| Pipeline net effect at equal thinking | `en->zh` | `raw_matched` | `wuwaterm_prod` | Primary (`en->zh`) |
| End-to-end production effect | `en->zh` | `raw_baseline` | `wuwaterm_prod` | Secondary |
| Thinking-policy effect | `en->zh` | `raw_baseline` | `raw_matched` | Secondary |

- **Pairing**: units are paired by `unit_id`. The scorer exits `2` when the two files' unit ids do not match 1:1, when a paired unit's `doc_id`, direction, genre or `source_scalars` differ, when a unit is still pending, or when a pair whose two units both ran has a post-edit distance in one arm only. The first and third comparisons come from one invocation over the full two-direction `raw_baseline` and `wuwaterm_prod` files (each direction is reported separately). `raw_matched` exists only for `en->zh`, so the comparisons that involve it use `en->zh`-only files of the other arm.
- **Seed and replicates**: seed `0` (the scorer's `DEFAULT_SEED`), frozen here and recorded in `h3-ledger.json`; `B = 10,000` replicates. Inside the scorer, `zh->en` draws from `seed` and `en->zh` from `seed + 1,000,003`; each direction's post-edit bootstrap draws from that direction's seed `XOR 0x517CC1B7`.
- **Bootstrap**: per direction, stratified by genre; within each genre stratum whole `doc_id` clusters are resampled with replacement (as many draws as the stratum has documents); 95% percentile interval (2.5th and 97.5th percentiles, linear interpolation).

The four metric families, per direction:

1. **Paired `A`-rate difference**: `ΔA = A_rate(arm_b) − A_rate(arm_a)` over the paired units (`undecidable` and `run_failed` stay in the denominator as not `A`), with its cluster-bootstrap 95% CI, `net_units`, and `wins` (unit is `A` in `arm_b` only) / `losses` (`A` in `arm_a` only) / `ties` (split into `ties_both_a` and `ties_both_not_a`). The same block is reported for the `A+B` usable rate. Per-arm genre breakdowns with Wilson intervals come with it.
2. **Critical-error net reduction**: total critical errors `arm_a − arm_b` with CI, the reduction per 1,000 source Unicode scalars with CI, and the six-category breakdown of §4 item 2 (`term` counts misleading term errors). `run_failed` units contribute neither errors nor scalars.
3. **MQM revision cost and optional edit distance**: MQM points per §4 item 4 (scorer `MQM_WEIGHTS`): total delta, and per-1,000-scalar delta and net reduction with CI; the `run_failed` count difference is reported separately (`max_unit_penalty`). Normalized post-edit distance (character Levenshtein distance divided by the longer string's length) is computed only on units with a post-edited reference in both arms; mean difference and net reduction with CI, `null` when no unit has one.
4. **Unit improvement cost**: the extra telemetry of `arm_b` over `arm_a` per direction (POSTs, tokens, latency seconds, cost) divided by (a) the net number of `A` units gained and (b) the net total of critical errors removed. Each is computable only when its CI lower bound is `> 0` and its net count is `> 0`: (a) uses the `ΔA` CI; (b) uses the CI of the per-1,000-scalar critical-error reduction while dividing by the total net reduction. Otherwise the entry's status and its POST, token, latency and cost fields read `not_computable (delta_not_significant)`, which covers a CI that crosses `0` and a `Δ <= 0`. Cost is gateway `usage` priced with the official price page at report time; a missing telemetry field gives `null`.

### 4.2 Pre-Registered Decision Rule

> **Plan default, for the owner to adjust at approval.** The rule, the seed and the primary comparisons are hashed into `h3-ledger.json` before the first live POST; after that they change only through an append-only ledger-log entry written before any `H3` judgment is read.

- Each direction's **primary** comparison (§4.1) decides that direction's claim; the two directions are separate claims and are never pooled.
- **Significant gain** only when the lower bound of the `ΔA` 95% cluster-bootstrap CI is `> 0` (scorer `verdict = "gain"`).
- **Worse than the raw model** when the upper bound is `< 0` (`verdict = "worse"`).
- Otherwise **indistinguishable**, whatever the point estimate.
- The secondary comparisons, the usable-rate difference, the critical-error, MQM and edit-distance families, and the per-arm Wilson intervals are reported with the scorer's verdicts and do not change the primary verdict.

## 5. Pre-Freeze Double-Blind Evaluation Protocol

1. **Precondition**: `MY-229` (`scripts/eval_semantic.py` audit) is satisfied (#316, merged to `main`), and all protocol files, rubric weights, manifests, term annotations, `OperationIdentity` specs, the pinned bindings and frozen tools of §0 (including the `H3` runner and the arm-scores exporter), and the decision rule and seed of §4.2 are SHA-256 hashed into `h3-ledger.json` before the first live POST.
2. **Blinding & randomization**:
   - Each scored unit's outputs from `raw_baseline`, `wuwaterm_prod` and, on `en->zh`, `raw_matched` receive fresh random item IDs.
   - Within each scored unit, the order of its arms' outputs is independently randomized per item, and all packets are shuffled across documents and genres. Judges never see arm names, model settings, or Core versions.
3. **Independent strong-pool judging & arbitration**:
   - Two independent strong-pool judges (`j1`, `j2` — distinct fresh strong-model contexts pinned in the frozen run manifest) grade every item independently using the frozen rubric (`usability-judging.md` remains unmodified; H3 community-term judging rules are frozen as a separate H3 addendum hashed into `h3-ledger.json`, with MQM severity points computed deterministically by the scorer).
   - Any disagreement on grade, critical error categories/counts, naturalness, or any official/community term `label`, `misleading` flag, or normalized `rendering` routes the full item to an unseen third strong-pool arbitrator (`adj`). Agreed items must never have `adj` records (invalid per `eval_usability.py` rules).
4. **Identical-output drift control**:
   - Whenever two arms produce byte-identical outputs for a unit, or on a seeded `25%` blind duplicate sample across passes, items are judged under distinct random IDs and checked for zero scoring drift (`identical_output_drift_count` reported alongside `judge_agreement_rate`).

## 6. Itemized Live Call Budget Table

> **Status: `Pending Owner Authorization — 0 live LLM calls executed`.**
> This budget is strictly separate from the completed `602/606` `q8` / `v0.11.0` ledger.

Every attempted POST to the product model gateway counts, retries and failures included, through the transport pre-send hook. Hard caps are per stage: the runner's `--max-calls` stops a stage at its cap, and the whole `H3` translation budget stops at `354`.

### 6.1 Stage 1: Pre-H3 Sanity & Parity Pilot (Optional Gate Before Full H3)

Verifies `raw_baseline` vs `wuwaterm_prod` segment alignment, `OperationExecutionBudget` logging, thinking-mode wire parity (including the `raw_matched` mode copy of §3.2), and token `usage` capture on a small frozen diagnostic slice before spending the full `H3` budget.

| Line Item | Scope | `zh->en` POSTs | `en->zh` POSTs | Retry Headroom | Subtotal POSTs |
| --- | --- | --- | --- | --- | --- |
| `raw_baseline` pilot | 8 diagnostic docs (~8 segments/dir) | 8 | 8 | +2 | 18 |
| `wuwaterm_prod` pilot | Same 8 docs (`v0.13.0`, `en->zh=auto`) | 8 | 8 | +4 (`auto` corrective cap) | 20 |
| `raw_matched` pilot | Same 8 docs, `en->zh` only, modes copied from the `wuwaterm_prod` pilot | 0 | 8 | +0 | 8 |
| **Stage 1 Pilot Subtotal** | **Sanity & wire-parity gate** | **16** | **24** | **+6** | **46** (hard cap **56**) |

### 6.2 Full 2-Arm `H3` Evaluation (Core Primary Comparison)

Assumes `>= 140` scored units per direction packed via `segmentation-v1` (`L = 1800`) into ~`45` wire segments per direction (`90` wire segments per arm across both directions).

| Arm / Line Item | `zh->en` Initial POSTs | `en->zh` Initial POSTs | Corrective / Retry Headroom | Sanity / Parity Check | Estimated Total POSTs |
| --- | --- | --- | --- | --- | --- |
| **Arm Raw (`raw_baseline`)** | 45 | 45 | +8 (failed-doc single rerun) | +4 | **102** |
| **Arm WuwaTerm (`wuwaterm_prod`)** | 45 | 45 | +18 (`en->zh` `auto` max-1 corrective + rerun) | +4 | **112** |
| **Full 2-Arm `H3` Total** | **90** | **90** | **+26** | **+8** | **214** (hard cap **240**) |

### 6.3 `en->zh` Thinking Ablation (`raw_matched`)

Runs after the `en->zh` `wuwaterm_prod` run of §6.2 (§3.2 rule 5), on the same ~`45` `en->zh` wire segments.

| Arm / Line Item | `zh->en` Initial POSTs | `en->zh` Initial POSTs | Corrective / Retry Headroom | Sanity / Parity Check | Estimated Total POSTs |
| --- | --- | --- | --- | --- | --- |
| **Ablation Arm (`raw_matched`)** | 0 | 45 | +5 (failed-doc single rerun) | +0 | **50** (hard cap **58**) |

### 6.4 Arm C (`wuwaterm_cand`) — Deferred

**`0` POSTs, hard cap `0`** in this revision (§3.3). The earlier Arm C estimate (`108`, cap `120`; combined ceiling `322`, cap `360`) is withdrawn; an Arm C run is budgeted in its own protocol revision.

### 6.5 Translation POST Total

| Stage | Estimated POSTs | Hard Cap |
| --- | --- | --- |
| Stage 1 pilot (§6.1) | 46 | 56 |
| Full 2-arm `H3` (§6.2) | 214 | 240 |
| `raw_matched` ablation (§6.3) | 50 | 58 |
| Arm C (§6.4) | 0 | 0 |
| **Total** | **310** | **354** |

*(Exact per-segment initial POST counts will be locked by an offline `--fake-transport` dry run on the frozen `H3` manifest prior to requesting final live-call execution.)*

### 6.6 Judging Budget (Subscription Agents, `0` API Dollars)

Following [ADR 0020](../adr/0020-held-out-usability-evaluation.md#translation-runs), judging and adjudication run as fresh agent contexts on the subscription. They are not gateway calls and cost `0` API dollars; they consume subscription quota.

Items at the scored-unit floor of §2.3 (more scored units scale every line in proportion):

| Line Item | Items |
| --- | --- |
| `raw_baseline` + `wuwaterm_prod`: `140` units × 2 directions × 2 arms | 560 |
| `raw_matched`: `140` `en->zh` units | 140 |
| Seeded `25%` blind drift re-judge of the above (§5.4) | 175 |
| **Total items** | **~875** |

- `j1` and `j2` each judge every item (`~1,750` judgment records); `adj` judges only the items they disagree on, whole item.
- Packing: at most `20` items per packet and within the packet size limit, so each judging pass needs at least `44` packets (`⌈875 / 20⌉`); the size limit can only raise that count.
- Token use is measured on the frozen packets, which are built offline with no model call, and goes into the approval request as the subscription quota estimate.

### 6.7 Hard Stop and Approval Request

> **Hard stop: 0 live paid calls have been made under `H3`. No paid online POST — the Stage 1 pilot included — is sent until the owner approves.**

The approval request carries:

1. The POST itemization of §6.1–§6.5 (`310` POSTs, hard cap `354`).
2. The amount computed from the official price page at the time of the request.
3. The subscription judging quota estimate of §6.6.
4. The exact per-stage POST counts from an offline `--fake-transport` dry run on the frozen `H3` manifest.
