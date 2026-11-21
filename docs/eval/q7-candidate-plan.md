# q7 Translation Quality Candidate Plan & Frozen Stage-1 Proposal

- Status: **Candidate Draft PR — 0 live LLM calls executed; awaits owner budget authorization before any live evaluation or merge.**
- Branch: `feat/q7-occurrence-term-admission`
- Development base: `main` (`bf75603`, carrying Core `0.10.2` plus `#289`, `#290`, `#291`, `#292`, `#293`)
- Formal non-regression comparison arm (control): **released Core `0.10.2` (`1d4bcdc`)** (offline-verified byte-identical to `main` `bf75603` on built dictionary `3a5726ad…`, matcher `wuwaterm-cjk-matcher-v8` `49b2c761…`, and 19/19 `D + ordinary` requests). Historical Core `0.9.1` (`5c636cb`) and `q5` (`04094ee`) are historical references only and do not substitute for current formal-release non-regression.
- Matcher revision: `wuwaterm-cjk-matcher-v9` (`30095de7a8e188c41b3536b9cd340df69ffe9890c4392b944198a3014a2da3e7`, bumped cleanly from `wuwaterm-cjk-matcher-v8` `49b2c76152f78f2f0c460e43c9cac91e063568060a312b2e0132f92bfa08ad5a`; `v8` and `v9` never collide)
- Related: MY-18, MY-380, [ADR 0017](../adr/0017-contextual-term-references.md), [ADR 0020](../adr/0020-held-out-usability-evaluation.md), [translation-reference-admission.md](../translation-reference-admission.md), [next-batch.md](next-batch.md), [translation-latency-criteria.md](translation-latency-criteria.md)

## 1. Prior Candidate Conclusion (`q6`)

- `archive/q6-scoped-skill-references-20261121` (`2135005`) is formally concluded **not adopted**: cumulative evaluation spend reached 469 calls and the Stage-1 gate did not pass.
- `q7` is built fresh on formal `main` (`a724c2b` merged with `bf75603`) rather than reviving the archived pre-refactor `q6` branch. It combines the proven mechanisms from PR #276 (`34d056e`), `archive/q5-contextual-names-20261119` (`04094ee`), and the bounded closed-class `…技能` stem rule from `archive/q6-scoped-skill-references-20261121` (`2135005`), refactored cleanly onto `#291`'s `TermReference`/`TermOccurrence` domain model with storage-decoupled `term_classification.py`.

## 2. Candidate Changes Included in `q7`

1. **Pure-domain decoupling (`term_classification.py`, `check_architecture_boundaries.py` Rule #11)**:
   - Extracts `is_derived_name_entry`, `derived_reference_only_sources`, and `select_preferred_exact_entry` into `src/wuwaterm/term_classification.py` (imports only `constants`, `models`, `normalize`; zero `db` or `lookup` dependency).
   - Both `term_matching.py` and `lookup.py` import from `term_classification.py`; `lookup.py` re-exports the predicates for compatibility and exposes `preferred_exact_entry`.
   - `scripts/check_architecture_boundaries.py` Rule #11 enforces that pure terminology modules (`cjk_span`, `english_plural`, `protected_text`, `skill_references`, `term_classification`, `term_matching`, `term_preparation`) never import `db` or `lookup`, preserving Rules #1–#10 intact.
2. **Single-character CJK name right-boundary admission (`cjk_span.py`, `wuwaterm-cjk-matcher-v9`)**:
   - Shared by translation reference admission and `review-v2`.
   - Requires a plausible right boundary (known lexicon word, honorific, one-character particle, punctuation/markup/whitespace, or existing name grammar) and closes both sides at middle-dot compounds (`红椿·蕾`, `心·蕾`, `红·椿`), preventing unknown compounds like `红椿蕊` from admitting an embedded single-character resonator name while keeping `欢迎椿来到`, `椿却没有回应`, `椿大人`, and `心大人`.
   - Exposes public `span_inside_known_word()` so `skill_references.py` complies with architecture boundary Rule #9 (no private cross-module `_name` imports).
   - Bumps `MATCHER_REVISION` from `wuwaterm-cjk-matcher-v8` to `wuwaterm-cjk-matcher-v9` with `_SINGLE_NAME_HONORIFICS` and `_SINGLE_NAME_PARTICLES` included in the revision hash.
3. **Sourced-name recovery in `builder.py` (audited on pinned 3.7 data `9218d612`)**:
   - Imports the complete `Advertising_VersionTitle_<n>` family from `Textmaps/*/multi_text/MultiText.json` (skipping already-curated `CORE_TERM_KEYS`). On pinned `9218d612`, this adds exactly 7 multi-character official release titles (`Advertising_VersionTitle_30` through `Advertising_VersionTitle_36`; `37` is already curated) — zero short or noisy ordinary words. Runtime treats them as contextual references only in both directions (`term_classification.is_derived_name_entry` / `derived_reference_only_sources`).
   - Relaxes `_item_tier_base_records` collision filtering from single-side surface uniqueness to `(zh, en)` pair uniqueness (`existing_pairs`). On pinned `9218d612`, this unlocks exactly 1 additional derived pair: `调谐器 / Tuner` alongside the speaker `调音师 / Tuner`, bringing the built DB from `12,089` rows (`3a5726ad…`) to `12,097` rows (`2bc0154b…`).
   - Preserves established exact-entry ownership across deterministic whole-input paths (`lookup.preferred_exact_entry`, called by `sentence.py`, `application.py`, and `channel.py`) so derived rows never displace an established exact hit while dictionary lookup still exposes all candidates.
4. **Pre-exclusion casefold collision retention & occurrence-scoped references (`term_matching.py`, `term_preparation.py`, `sentence.py`)**:
   - Retains `pre_exclusion_sources` in `model_reference_candidates` (PR #276 `34d056e`) so excluding one cased surface in a shared casefold key continues to block derived soft-spelling synthesis.
   - Collapses multi-table identical `(zh, en)` rows to one reference line (highest `CATEGORY_ORDER`) while exposing competing distinct pairs (such as `Tuner`) as contextual alternatives rather than hard locks.
   - Gates derived soft-casing hints (`origin="derived_soft_casing"`, `reason="soft_casing"`) on actual dictionary term context in the submitted segment, keeping isolated ordinary prose untouched.
   - Carries visible-segment `(start, end)` and post-placeholder `(model_start, model_end)` coordinates directly on `TermOccurrence` inside `TermReference` (plain and protected multi-segment paths), emitting `_SCOPED_REFERENCE_INSTRUCTION` and `Reference occurrence ranges` strictly on demand so plain single-meaning requests without context remain byte-identical.
   - Updates opt-in `translation-context-v1` `_CONTEXT_RULES_INSTRUCTION` (numeric shorthand such as `3+1`/`S0R1`, omitted actors/rhetorical addressees, and translating every clause), active whenever `context is not None` in **both** `zh->en` and `en->zh`.
5. **Bounded closed-class `…技能` 2-character stem reference rule (`skill_references.py`, `term_preparation.py`)**:
   - General rule derived strictly from the five official `BinData/skill/skilltype.json` rows (`SkillType_{2,3,5,6,11}_TypeName`) — no per-word hardcoding.
   - Runs **only on `zh->en`** (`to_chinese is False`). (Note: while `skill_references.py` itself is `zh->en`-only, `q7` as a whole DOES modify `en->zh` prompts via `_CONTEXT_RULES_INSTRUCTION`, `builder.py` sourced names, and `term_matching`/`term_preparation` reference admission; see Sections 4 and 5.)
   - Requires local action syntax plus independent local game-context evidence (or `切<resonator/speaker>`) in the same clause, skips spans inside known lexicon words or covered by longer dictionary matches (`变奏技能`), and requires a unique local sense marker for ambiguous stems (`共鸣` -> `技能`/`解放`/`回路`).
   - Emits `TermReference(..., origin="scoped_skill_stem", reason="local_skill_action")` with exact `TermOccurrence` `(model_start, model_end)` ranges validated fail-closed before any HTTP call.

## 3. Preserved Owner Rulings & Non-Goals

- **No per-word hacks**: `变奏入场` is handled exclusively through the closed-class `BinData/skill/skilltype.json` 2-character stem rule.
- **Thinking policy unchanged**: `zh->en` remains `disabled`, `en->zh` remains `omit`.
- **Timeout constants unchanged**: `45s` model timeout and `90s` request budget remain untouched.
- **No H3**: `H2` remains marked `held-out used`; no new held-out split is created.
- **Zero live LLM calls in this PR**: all verification is offline (`scripts/validate.py` + `scripts/build_q7_eval_manifest.py` + GitHub Actions CI).

## 4. Development Base, Formal Non-Regression Baseline & Offline `reuse_key` Manifest

### 4.1 Arm Definitions (Clearly Separated)

| Role | Commit / Ref | Built DB SHA-256 (pinned `9218d612`) | `MATCHER_REVISION` | Status |
| --- | --- | --- | --- | --- |
| **Development base** | `main` (`bf75603`) | `3a5726ad9355…` (12,089 rows) | `v8` (`49b2c76152f7…`) | Offline-verified identical to `0.10.2` on DB, matcher, and 19/19 `D + ordinary` requests |
| **Formal non-regression control** | Released Core `0.10.2` (`1d4bcdc`) | `3a5726ad9355…` (12,089 rows) | `v8` (`49b2c76152f7…`) | Primary non-regression baseline; requires fresh live run (0/19 strict `reuse_key` match with `0.9.1`) |
| **Candidate (`q7`)** | `feat/q7-occurrence-term-admission` | `2bc0154b667a…` (12,097 rows, +8) | `v9` (`30095de7a8e1…`) | Candidate under test; 0 live calls executed |
| Historical reference (`0.9.1`) | `5c636cb` | `3a5726ad9355…` (12,089 rows) | `2bbeac6dcffc…` | Historical reference only; lacks `raw_content` and `MY-359` regroup equivalence |
| Historical reference (`q5` / `q6`) | `04094ee` / `2135005` | `2bc0154b667a…` (12,097 rows) | `v9` (`30095de7a8e1…`) | `q6` concluded not adopted; `q6` live snapshots retain `raw_content` + `adr0020_reuse_key` on 15/19 segments |

### 4.2 Offline Per-Request `reuse_key` Manifest (`D + ordinary`, 17 docs / 19 segments)

Generated strictly offline (sockets blocked, `httpx.MockTransport` only, **0 live LLM calls**, **0 corpus text emitted**) via `scripts/build_q7_eval_manifest.py` with:
- `model = deepseek-flash`, `temperature = 0`, `timeout_seconds = 45.0`, `en_zh_protocol = True`, `zh-en thinking = disabled`, `en-zh thinking = omit`.
- Manifest `reuse_key` = SHA-256 over canonical JSON `{db_sha256, direction, matcher_revision, model, system_prompt_sha256, thinking, timeout_seconds, user_prompt_sha256}`.
- `adr0020_reuse_key` = exact ADR 0020 / `run_usability.py` / `run_q6.py` SHA-256 over `[sha256(core), direction, sha256(canonical(payload)), db_sha256, matcher_revision, refs, model, temperature, thinking_wire]`.
- Segments with `locks > 0` emit a per-call random placeholder nonce (`__WUWA_TERM_<16-hex>_0000__`), so their exact `system_prompt_sha256`, `user_prompt_sha256`, `reuse_key`, and `adr0020_reuse_key` change on every run and are **always `must_post`** (normalized hashes with fixed sentinel are listed in brackets for prompt-diff auditing only).

| Doc | Seg | Dir | Chars | Paras | Thinking | Timeout | Locks (`0.10.2`→`q7`) | Refs (`0.10.2`→`q7`, `skill`) | `q7` `sys_sha` (12) | `q7` `usr_sha` (12) | `q7` `reuse_key` (12) | `q7` `adr0020_key` (12) | `0.10.2` vs `0.9.1` | `q7` vs `0.10.2`/`0.9.1` | `q7` vs archived `q6` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `D-001` | 1/1 | `zh-en` | 17 | 3 | `disabled` | `45s` | `0→0` | `1→1` (`0`) | `94acbe10e870` | `c5607aa5477b` | `57cedeb71250` | `fa2ed1248725` | `must_post` (`v8`≠`0.9.1`, no `raw_content`) | `must_post` (`sys`+`db`+`v9` changed) | `reusable_exact` (if `q6` reuse approved) |
| `D-002` | 1/1 | `zh-en` | 250 | 13 | `disabled` | `45s` | `0→0` | `3→3` (`0`) | `150cf930d5e9` | `8d4145dca91a` | `9ec284a463d0` | `a15d8d227eae` | `must_post` (`v8`≠`0.9.1`, no `raw_content`) | `must_post` (`sys`+`db`+`v9` changed) | `reusable_exact` (if `q6` reuse approved) |
| `D-003` | 1/1 | `zh-en` | 1476 | 45 | `disabled` | `45s` | `0→0` | `19→21` (`1`) | `e33d56bc0a93` | `20472c8e51eb` | `57f5d4e7eade` | `626ff6c158ab` | `must_post` (`v8`≠`0.9.1`, no `raw_content`) | `must_post` (`sys`+`refs`+`skill`+`v9`) | `reusable_exact` (if `q6` reuse approved) |
| `D-004` | 1/2 | `zh-en` | 1735 | 6 | `disabled` | `45s` | `0→0` | `16→16` (`0`) | `3b03c603015d` | `b32adf07856a` | `56a3fb8fae34` | `1b9b6085a16c` | `must_post` (`v8`≠`0.9.1`, no `raw_content`) | `must_post` (`sys`+`db`+`v9` changed) | `reusable_exact` (if `q6` reuse approved) |
| `D-004` | 2/2 | `zh-en` | 854 | 4 | `disabled` | `45s` | `0→0` | `6→6` (`0`) | `4aa67190590b` | `12cce3bc171b` | `0a067f72c30a` | `01560cde3d06` | `must_post` (`prior_seg` + `v8`≠`0.9.1`) | `must_post` (`sys`+`prior_seg`+`v9`) | `reusable_exact` (if `q6` reuse approved) |
| `D-005` | 1/2 | `zh-en` | 1735 | 35 | `disabled` | `45s` | `0→0` | `7→8` (`0`) | `e9446a5b05a2` | `32d80cb469ee` | `d2d065a5a26e` | `e00f2896f9f3` | `must_post` (`v8`≠`0.9.1`, no `raw_content`) | `must_post` (`sys`+`refs`+`db`+`v9`) | `reusable_exact` (if `q6` reuse approved) |
| `D-005` | 2/2 | `zh-en` | 395 | 9 | `disabled` | `45s` | `0→0` | `2→2` (`0`) | `557015a0d2f9` | `dead0edc849d` | `f342c31042f5` | `4aece23346d4` | `must_post` (`prior_seg` + `v8`≠`0.9.1`) | `must_post` (`sys`+`prior_seg`+`v9`) | `reusable_exact` (if `q6` reuse approved) |
| `D-006` | 1/1 | `en-zh` | 248 | 6 | `omit` | `45s` | `0→0` | `0→0` (`0`) | `5b5e1535b79a` | `cf50e815e870` | `33cb86add213` | `3b6910247fb1` | `must_post` (`v8`≠`0.9.1`, no `raw_content`) | `must_post` (`sys` context rules changed) | `reusable_exact` (if `q6` reuse approved) |
| `D-007` | 1/1 | `en-zh` | 15 | 1 | `omit` | `45s` | `0→0` | `0→0` (`0`) | `5b5e1535b79a` | `f24d933bd6ab` | `a80f2c8d2696` | `9ff5bfa73555` | `must_post` (`v8`≠`0.9.1`, no `raw_content`) | `must_post` (`sys` context rules changed) | `reusable_exact` (if `q6` reuse approved) |
| `D-008` | 1/1 | `en-zh` | 1244 | 17 | `omit` | `45s` | `0→0` | `2→3` (`0`) | `58ce701aecfa` | `63438ed69856` | `ed6c8e479d0d` | `f49deb5ae42a` | `must_post` (`v8`≠`0.9.1`, no `raw_content`) | `must_post` (`sys`+`refs`+`db`+`v9`) | `reusable_exact` (if `q6` reuse approved) |
| `D-009` | 1/1 | `en-zh` | 1542 | 30 | `omit` | `45s` | `11→11` | `1→2` (`0`) | `norm:eca9d3aad11e` | `nonce` | `nonce` | `nonce` | `must_post` (`random_lock_nonce`) | `must_post` (`random_lock_nonce`+`sys`+`refs`) | `must_post` (`random_lock_nonce` + `q6` `run_failed`) |
| `D-010` | 1/1 | `en-zh` | 309 | 2 | `omit` | `45s` | `0→0` | `0→0` (`0`) | `5b5e1535b79a` | `cbc9e9feb10c` | `9be06fc0611d` | `c7c6ba5358ed` | `must_post` (`v8`≠`0.9.1`, no `raw_content`) | `must_post` (`sys` context rules changed) | `reusable_exact` (if `q6` reuse approved) |
| `D-011` | 1/1 | `zh-en` | 324 | 16 | `disabled` | `45s` | `0→0` | `4→4` (`0`) | `d6eb673435cb` | `8ce15bf4372e` | `41ee8938992a` | `c17614419ae2` | `must_post` (`v8`≠`0.9.1`, no `raw_content`) | `must_post` (`sys`+`db`+`v9` changed) | `reusable_exact` (if `q6` reuse approved) |
| `D-012` | 1/1 | `en-zh` | 1402 | 23 | `omit` | `45s` | `2→2` | `0→0` (`0`) | `norm:8135b52cc8c0` | `nonce` | `nonce` | `nonce` | `must_post` (`random_lock_nonce`) | `must_post` (`random_lock_nonce`+`sys`) | `must_post` (`random_lock_nonce`) |
| `D-013` | 1/1 | `en-zh` | 294 | 3 | `omit` | `45s` | `0→0` | `0→0` (`0`) | `5b5e1535b79a` | `15890eb991f0` | `c9ca1d366405` | `cfb2d7414195` | `must_post` (`v8`≠`0.9.1`, no `raw_content`) | `must_post` (`sys` context rules changed) | `reusable_exact` (if `q6` reuse approved) |
| `D-014` | 1/1 | `en-zh` | 920 | 11 | `omit` | `45s` | `2→2` | `0→0` (`0`) | `norm:608ef3abfdff` | `nonce` | `nonce` | `nonce` | `must_post` (`random_lock_nonce`) | `must_post` (`random_lock_nonce`+`sys`) | `must_post` (`random_lock_nonce`) |
| `D-015` | 1/1 | `en-zh` | 367 | 8 | `omit` | `45s` | `0→0` | `0→0` (`0`) | `5b5e1535b79a` | `9ae7742a5061` | `4bb510c76954` | `820d9dfb04fb` | `must_post` (`v8`≠`0.9.1`, no `raw_content`) | `must_post` (`sys` context rules changed) | `reusable_exact` (if `q6` reuse approved) |
| `ordinary-zh-en` | 1/1 | `zh-en` | 120 | 6 | `disabled` | `45s` | `0→0` | `8→8` (`0`) | `100e6e49af01` | `c9dba45dfe71` | `b4aab0cd0c9f` | `e261a57b6b72` | `must_post` (`v8`≠`0.9.1`, no `raw_content`) | `must_post` (`sys`+`db`+`v9` changed) | `reusable_exact` (if `q6` reuse approved) |
| `ordinary-en-zh` | 1/1 | `en-zh` | 262 | 6 | `omit` | `45s` | `2→2` | `2→8` (`0`) | `norm:a33e896c0ca0` | `nonce` | `nonce` | `nonce` | `must_post` (`random_lock_nonce`) | `must_post` (`random_lock_nonce`+`sys`+`refs`) | `must_post` (`random_lock_nonce`) |

### 4.3 Recalculated Real Call Budget (Explicitly Auditing `<= 25`)

Because `0.10.2` has **0/19** strictly reusable segments from `0.9.1` (different `MATCHER_REVISION`, missing `raw_content`, and unresolved `MY-359` postprocessing equivalence on 18/19 segments) and `q7` alters `_CONTEXT_RULES_INSTRUCTION`, DB, and matcher on **all 19/19** segments relative to `0.10.2`, **a `<= 25` hard cap CANNOT cover a full fresh paired `0.10.2` vs `q7` evaluation**:

| Budget Mode | Formal `0.10.2` Baseline POSTs | `q7` Candidate POSTs | Subtotal (0 retries) | With 1 D-009 Retry / Arm | Optional D-009 Diag Split (+3/arm) | Fits `<= 25`? | Trade-off / Requirement |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Mode A (Recommended for clean paired proof)**: Fresh `0.10.2` + Fresh `q7` (zero `q6` reuse) | **19** | **19** | **38** | **40** | `44`–`46` | **No** (exceeds `25`) | Cleanest paired formal non-regression proof; requires owner budget approval of **40 calls** (or **46** with Option B). |
| **Mode B (Fits `<= 25` only with explicit `q6` exact-key reuse)**: Fresh `0.10.2` + `q7` reusing 15 byte-identical `q6` `adr0020_reuse_key` segments + 4 fresh `q7` POSTs | **19** | **4** (`D-009`, `D-012`, `D-014`, `ordinary-en-zh`) | **23** | **25** | `29`–`31` | **Yes** (`23`–`25`, without Option B) | Requires explicit owner sign-off to reuse `q6`'s 15 byte-identical archived outputs even though `q6` was concluded not adopted; adding Option B exceeds `25`. |
| **Mode C (Single-arm only)**: No `0.10.2` baseline run + Fresh `q7` only | **0** | **19** | **19** | **20** | `22`–`23` | **Yes** (`19`–`23`) | **Does NOT establish formal `0.10.2` non-regression** (historical `0.9.1` reference only); cannot authorize release on its own. |

Every attempted POST (including timeouts and retries) counts against the authorized cap. No live call may start until the owner chooses the budget mode and authorizes the cap.

## 5. Accurate D-009 Timeout Status (Overstrong Attribution Retracted)

- **Historical verification (`5c636cb`, Core `0.9.1`)**: Core `0.9.1` ALREADY defined `DEFAULT_LLM_TIMEOUT_SECONDS = 45.0` and passed `httpx.Timeout(self.llm_timeout_seconds)` to `httpx.AsyncClient`. `httpx.Timeout(45.0)` bounds individual socket operations (`connect`/`read`/`write`/`pool`, i.e., per-chunk wait) rather than enforcing a strict wall-clock total ceiling; the outer API 90s request middleware did not run on that historical direct-Core path.
- **Observed timeout recurrence**: In `q5-eval-20261119`, D-009 (`1,542` chars / `30` paragraphs `en->zh`, 1 call under `en->zh` `thinking=omit` + `45s`) timed out in 7 of 8 attempts (`45.3–45.7s`) and completed once at `44.291s`; in `q6-eval-20261120`, D-009 timed out in 2 of 2 attempts (`45.317s`, `45.343s`).
- **Scope of `q7` changes on `en->zh`**: Although `skill_references.py` runs on `zh->en` only, `q7` is **not** zero-change on `en->zh` — it updates `_CONTEXT_RULES_INSTRUCTION` on all context-mode requests, recovers `Tuner` / version-title rows in `builder.py`, and updates reference admission in `term_matching`/`term_preparation` (changing `D-009`'s system prompt and increasing its references from `1` in `0.10.2` to `2` in `q7`).
- **Accurate conclusion**: **D-009 历史超时反复出现，具体成因及 q7 影响待验证。** Timeout settings (`45s` model / `90s` request) and thinking policy (`zh->en` `disabled`, `en->zh` `omit`) remain unchanged.

## 6. Stage-1 Gates, Additive Metrics, Identical-Output Review Rule & Delta Schema

### 6.1 Additive Relation to the Original Frozen Full Gates

The four q7-specific checks below are **strictly additive** and **never replace or weaken** the original frozen full gates ([next-batch.md](next-batch.md), [translation-latency-criteria.md](translation-latency-criteria.md)):

1. **Original Frozen Full Gates (mandatory, evaluated against formal Core `0.10.2` baseline)**:
   - **Official-name accuracy per direction**: does not regress relative to the formal `0.10.2` baseline.
   - **Critical errors & ordinary-word errors**: neither count increases relative to the formal `0.10.2` baseline.
   - **Pre-frozen repair minimum**: at least **2** pre-frozen official-name targets fixed and at least **1** pre-frozen critical semantic error target fixed (no post-hoc cherry-picking).
   - **Full-denominator `A + B` usable rate per direction**: does not regress relative to the formal `0.10.2` baseline, with **`run_failed` paragraphs retained in the full denominator** (never dropped from usability rates).
2. **Additive q7 Checks (must hold in addition to the frozen full gates above)**:
   - `ordinary zh->en` control accuracy `>= 5/6` (preserving `q5`'s ordinary-word improvement without regressing `ordinary en->zh`).
   - Target official-name recovery on `D-003` (`变奏入场` -> `Intro Skill`) via the general `scoped_skill_stem` mechanism (zero per-word hardcoding).
   - `D` critical error historical benchmark `<= 18`: explicitly noted as **`q5`'s historical target reference value only** — it **cannot** substitute for non-regression against the formal `0.10.2` baseline, and **cannot** be satisfied by dropping `D-009` failed paragraphs to make the raw count appear lower.
   - Zero `zh->en` timeouts under `disabled` + `45s`.

### 6.2 Frozen Identical-Output Re-check Rule (Anti-Judge-Drift Guard)

To prevent judge-drift artifacts such as historical `D-011` (where identical translation outputs received divergent scores across arms):

1. Before final scoring, a deterministic pre-check compares the formal `0.10.2` baseline translation and the `q7` candidate translation for every scored `(doc_id, paragraph_index)` after NFKC and edge-whitespace trimming.
2. Whenever two arms emit the **exact same translation text** for a paragraph yet blind judging assigns different `grade`, `critical_errors`, or `names` verdicts, that paragraph is automatically flagged `identical_output_judge_drift` and routed to a **mandatory blinded re-check / adjudication** before computing deltas.
3. Both raw prior judgments remain preserved in the audit log; the adjudicated verdict applies identically to both arms for that exact text so judge variance cannot manufacture a false "fixed" or "regressed" item.

### 6.3 Frozen Paired `Fixed / Added` Delta Table Schema

Every Stage-1 paired report (`0.10.2` vs `q7`) must publish the following per-direction paired delta table (never reporting net totals alone):

| Column | Required Definition |
| --- | --- |
| `direction` | `zh-en` or `en-zh` (never pooled) |
| `metric` | `usable_A_plus_B`, `official_names_correct`, `critical_errors`, `ordinary_word_errors`, `run_failed_paragraphs` |
| `formal_0_10_2` | Exact count & full denominator on formal Core `0.10.2` |
| `q7_candidate` | Exact count & full denominator on `q7` |
| `fixed_items` | Explicit list/count of `(doc_id, para_idx)` that failed on `0.10.2` and passed on `q7` |
| `newly_added_items` | Explicit list/count of `(doc_id, para_idx)` that passed on `0.10.2` and failed on `q7` |
| `identical_output_rechecked` | Count of identical-output items checked under Section 6.2 |
| `run_failed_in_denominator` | Explicit count of `run_failed` paragraphs kept in the full denominator per arm |
| `net_delta` | `q7_candidate - formal_0_10_2` |

## 7. D-009 Execution Options & Locked Option B Boundaries (Owner Decision Required)

- **Option A (Strict single-segment only)**: Run unmodified D-009 as 1 single 1,542-char / 30-paragraph request in each evaluated arm under unchanged `en->zh` `omit` + `45s`, keeping any `run_failed` outcome in the full `D` denominator and bounding retries strictly within the approved budget.
- **Option B (Separate D-009 paragraph-group diagnostic split, subject to three hard boundaries)**:
  1. **Formal `D` unmodified**: The original full-document single-request D-009 still runs in formal `D` evaluation; if it fails, all 30 paragraphs remain `run_failed` in the formal `D` full denominator.
  2. **Identical fixed paragraph-group boundaries**: Both the formal `0.10.2` baseline arm and the `q7` candidate arm use the exact same deterministic 3-group split of D-009's 30 paragraphs (`scripts/build_q7_eval_manifest.py::paragraph_group_ranges(30, 10)`):
     - Group 1: paragraphs `1–10` (`556` chars)
     - Group 2: paragraphs `11–20` (`498` chars)
     - Group 3: paragraphs `21–30` (`484` chars)
  3. **Strict diagnostic isolation**: Diagnostic split translations and grades are recorded in a separate diagnostic report only — they **never** backfill missing formal full-document D-009 results, **never** alter the formal `D` denominator or critical-error count, and **never** establish Stage-1 or Stage-2 gate passage.

## 8. Offline Verification Gate

Before requesting owner budget authorization, this Draft PR passes 100% offline:
- `python scripts/validate.py` (all 6 steps: `hygiene`, `architecture` including Rules #8/#9/#10/#11, `api-contract`, `ruff`, `typing`, `pytest`).
- `scripts/build_q7_eval_manifest.py` offline unit tests + per-request `reuse_key` manifest verification.
- GitHub Actions PR CI checks green on the pushed HEAD SHA.
- **0 live LLM calls executed.**
