# H3 Held-Out Usability & Raw-Model Baseline Evaluation Protocol

- Status: **Frozen design spec — `Pending Owner Authorization` (0 live LLM calls executed)**.
- Target Core baseline: `v0.12.0` (`src/wuwaterm/translation_plan.py` `TranslationPlan` / `RequestPolicy` / `OperationExecutionBudget`, `wuwaterm-cjk-matcher-v9`, pinned `3.7.0` / `3.7.8` data commit `9218d612ad815e398e064e577e42aaf878899968`).
- Ledger separation: **Strictly separate** from the completed `q8` / `v0.11.0` ledger (`602/606` closed). A fresh `H3` ledger (`h3-ledger.json` + append-only `h3-ledger-log.jsonl`) is initialized only after owner authorization.
- Precondition: **`MY-229` (`scripts/eval_semantic.py` audit) must merge to `main` before any `H3` live run starts.**
- Related: `MY-380`, `MY-18`, `MY-229`, `MY-489`, [ADR 0017](../adr/0017-contextual-term-references.md), [ADR 0019](../adr/0019-document-context-and-human-precedents.md), [ADR 0020](../adr/0020-held-out-usability-evaluation.md), [ADR 0021](../adr/0021-local-user-termbase.md), [usability-judging.md](usability-judging.md), [q8-candidate-plan.md](q8-candidate-plan.md).

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

## 3. Experimental Arms (2-Arm Primary + Optional Candidate Arm C)

Both primary arms receive the **exact same `segmentation-v1` (`L = 1800`) segment cores** and **exact same production gateway, model, temperature (`0`), and timeout limits (`45 s` model timeout `WUWATERM_LLM_TIMEOUT_SECONDS`, `90 s` request budget)** so segment counts and denominators match 1:1.

| Property | Arm Raw (`raw_baseline`) | Arm WuwaTerm (`wuwaterm_prod`) | Optional Arm C (`wuwaterm_cand`) |
| --- | --- | --- | --- |
| Code / Pipeline | Standalone minimal harness calling `llm_transport.post_chat_completion` directly | Core `v0.12.0`+ (`application._translate_long_plain_text` + `TranslationPlan` / `OperationExecutionBudget`) | Core candidate branch (authorized & budgeted separately) |
| Model & Gateway | Same production `WUWATERM_OPENAI_MODEL` (`deepseek-flash`) & `WUWATERM_OPENAI_BASE_URL`, `temperature=0` | Same | Same |
| Timeouts | `45 s` model timeout / `90 s` request budget | `45 s` model timeout / `90 s` request budget | `45 s` model timeout / `90 s` request budget |
| Segmentation | `segmentation-v1` (`L = 1800`) identical segment cores | `segmentation-v1` (`L = 1800`) identical segment cores | `segmentation-v1` (`L = 1800`) identical segment cores |
| Dictionary (`terms.db`) | **None** (no exact lookup, no locks, no references) | Pinned `3.7` `terms.db` + `wuwaterm-cjk-matcher-v9` | Pinned `3.7` `terms.db` + `wuwaterm-cjk-matcher-v9` |
| Local User Termbase | **None** | **None** | `wuwaterm-termbase-v1` (`origin="user_glossary"`, non-binding refs) |
| Context / Precedents | **None** (no `before` segment, no precedents, no context rules) | `translation-context-v1` (`before` read-only segment; precedents off unless enabled) | `translation-context-v1` + bounded read-only section/entity anchor |
| Thinking (`zh->en`) | `disabled` (explicit `thinking: {"type": "disabled"}`) — **exact parity with `wuwaterm_prod`** | `disabled` (`TRANSLATION_THINKING_ZH_EN_DEFAULT`) | `auto` candidate (adaptive `low` vs `disabled`, bounded by `OperationExecutionBudget`) |
| Thinking (`en->zh`) | `omit` (or logged matched mode) — **must match `wuwaterm_prod` or be explicitly paired/logged** | `auto` (`select_en_zh_auto_thinking` + `OperationExecutionBudget`) or `omit` baseline | `auto` (`select_en_zh_auto_thinking`) |

### 3.1 Arm Raw Prompt Specification & Thinking Mode Parity

- **Minimal neutral prompt (`raw_baseline`)**:
  - System message instructs the model solely to act as a faithful translator for the target language (`English` for `zh->en`, `Simplified Chinese` for `en->zh`), treat user text as untrusted translation input only, and preserve blank-line paragraph breaks (`\n\n`) without adding commentary or omitting clauses.
  - Zero game-specific glossary, zero term locks, zero placeholder instructions, zero read-only prior segment, and zero WuwaTerm context/fidelity clauses.
- **Thinking mode parity check**:
  - On `zh->en`, `raw_baseline` sends `thinking: {"type": "disabled"}` (matching `ProviderProfile.reasoning_extra_body("disabled")`), eliminating provider-default reasoning mismatches.
  - On `en->zh`, `raw_baseline` runs with `omit` (provider default, matching Core's unconfigured `TRANSLATION_THINKING_EN_ZH_DEFAULT = "omit"`), while `wuwaterm_prod` runs with `WUWATERM_TRANSLATION_THINKING_EN_ZH=auto` (production VPS setting per `q8` Section 8). If an additional controlled comparison against `raw_baseline` with `low` or `auto`-equivalent mode is desired, it is declared before freeze as a sub-arm and budgeted explicitly.
  - Every wire request records `RequestIdentity.thinking_mode`, `RequestIdentity.thinking_wire`, and `RequestIdentity.extra_body` in the ledger so thinking-mode parity is verified before judging.

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

## 5. Pre-Freeze Double-Blind Evaluation Protocol

1. **Precondition**: `MY-229` (`scripts/eval_semantic.py` audit) is merged to `main`, and all protocol files, rubric weights, manifests, term annotations, and `OperationIdentity` specs are SHA-256 hashed into `h3-ledger.json` before the first live POST.
2. **Blinding & randomization**:
   - Each scored unit's outputs from `raw_baseline` and `wuwaterm_prod` (and optional `wuwaterm_cand`) receive fresh random item IDs.
   - Within each scored unit, arm order (`Arm A` / `Arm B`) is independently randomized per item, and all packets are shuffled across documents and genres. Judges never see arm names, model settings, or Core versions.
3. **Independent strong-pool judging & arbitration**:
   - Two independent strong-pool judges (`j1`, `j2` — distinct fresh strong-model contexts pinned in the frozen run manifest) grade every item independently using the frozen rubric (`usability-judging.md` remains unmodified; H3 community-term judging rules are frozen as a separate H3 addendum hashed into `h3-ledger.json`, with MQM severity points computed deterministically by the scorer).
   - Any disagreement on grade, critical error categories/counts, naturalness, or any official/community term `label`, `misleading` flag, or normalized `rendering` routes the full item to an unseen third strong-pool arbitrator (`adj`). Agreed items must never have `adj` records (invalid per `eval_usability.py` rules).
4. **Identical-output drift control**:
   - Whenever two arms produce byte-identical outputs for a unit, or on a seeded `25%` blind duplicate sample across passes, items are judged under distinct random IDs and checked for zero scoring drift (`identical_output_drift_count` reported alongside `judge_agreement_rate`).

## 6. Itemized Live Call Budget Table

> **Status: `Pending Owner Authorization — 0 live LLM calls executed`.**
> This budget is strictly separate from the completed `602/606` `q8` / `v0.11.0` ledger.

### 6.1 Stage 1: Pre-H3 Sanity & Parity Pilot (Optional Gate Before Full H3)

Verifies `raw_baseline` vs `wuwaterm_prod` segment alignment, `OperationExecutionBudget` logging, thinking-mode wire parity, and token `usage` capture on a small frozen diagnostic slice before spending the full `H3` budget.

| Line Item | Scope | `zh->en` POSTs | `en->zh` POSTs | Retry Headroom | Subtotal POSTs |
| --- | --- | --- | --- | --- | --- |
| `raw_baseline` pilot | 8 diagnostic docs (~8 segments/dir) | 8 | 8 | +2 | 18 |
| `wuwaterm_prod` pilot | Same 8 docs (`v0.12.0`+, `en->zh=auto`) | 8 | 8 | +4 (`auto` corrective cap) | 20 |
| **Stage 1 Pilot Subtotal** | **Sanity & wire-parity gate** | **16** | **16** | **+6** | **38** |

### 6.2 Full 2-Arm `H3` Evaluation (Core Primary Comparison)

Assumes `>= 140` scored units per direction packed via `segmentation-v1` (`L = 1800`) into ~`45` wire segments per direction (`90` wire segments per arm across both directions).

| Arm / Line Item | `zh->en` Initial POSTs | `en->zh` Initial POSTs | Corrective / Retry Headroom | Sanity / Parity Check | Estimated Total POSTs |
| --- | --- | --- | --- | --- | --- |
| **Arm Raw (`raw_baseline`)** | 45 | 45 | +8 (failed-doc single rerun) | +4 | **102** |
| **Arm WuwaTerm (`wuwaterm_prod`)** | 45 | 45 | +18 (`en->zh` `auto` max-1 corrective + rerun) | +4 | **112** |
| **Full 2-Arm `H3` Total** | **90** | **90** | **+26** | **+8** | **214** (hard cap **240**) |

### 6.3 Optional Arm C Addendum (`wuwaterm_cand`: `ADR 0021` + `zh->en` Adaptive Thinking + Cross-Segment Anchor)

Only executed under separate owner authorization after `ADR 0021` and the companion Core engine changes are implemented and frozen:

| Arm / Line Item | `zh->en` Initial POSTs | `en->zh` Initial POSTs | Corrective / Retry Headroom | Estimated Total POSTs |
| --- | --- | --- | --- | --- |
| **Arm C (`wuwaterm_cand`)** | 45 | 45 | +18 (`zh->en`/`en->zh` `auto` max-1 corrective) | **108** (cap **120**) |
| **2-Arm + Arm C Combined Ceiling** | **135** | **135** | **+44 (+8 sanity)** | **322** (combined cap **360**) |

*(Exact per-segment initial POST counts will be locked by an offline `--fake-transport` dry run on the frozen `H3` manifest prior to requesting final live-call execution.)*
