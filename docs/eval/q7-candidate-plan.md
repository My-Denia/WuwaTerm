# q7 Translation Quality Candidate Plan & Frozen Stage-1 Proposal

- Status: **Step B1b Offline Upgrade Complete (0 live LLM calls executed in this PR update; 4-POST Fail-Fast probe used `469 -> 473`, 42 calls remaining of owner-authorized Mode A ceiling `515`).**
- Branch: `feat/q7-occurrence-term-admission`
- Development base: `main` (`b22b50d`, Core `v0.10.3` release snapshot including `#289`–`#298`)
- Formal non-regression comparison arm (control): **released Core `0.10.2` (`1d4bcdc`)** (and `0.10.3` / `main` `b22b50d`, which share identical built DB `3a5726ad…`, matcher `wuwaterm-cjk-matcher-v8` `49b2c761…`, and 19/19 `D + ordinary` translation prompts). Historical Core `0.9.1` (`5c636cb`) and `q5` (`04094ee`) are historical references only and do not substitute for current formal-release non-regression.
- Matcher revision: `wuwaterm-cjk-matcher-v9` (`bcb9fe68b1597a697596e250f969ea39d6ed086523d2b3771859c747d8d2bc8f`, including `"有"` in `_SINGLE_NAME_PARTICLES`; bumped cleanly from `wuwaterm-cjk-matcher-v8` `49b2c76152f78f2f0c460e43c9cac91e063568060a312b2e0132f92bfa08ad5a`; `v8` and `v9` never collide)
- Owner-authorized live budget: **Mode A — up to `+46` live POSTs (cumulative hard cap `515` from `469`; 4 POSTs spent on Fail-Fast #1 -> `473`, `42` remaining)**. **0 live calls have been made in Step B1b.**
- Related: MY-18, MY-380, [ADR 0017](../adr/0017-contextual-term-references.md), [ADR 0020](../adr/0020-held-out-usability-evaluation.md), [translation-reference-admission.md](../translation-reference-admission.md), [next-batch.md](next-batch.md), [translation-latency-criteria.md](translation-latency-criteria.md)

## 1. Prior Candidate Conclusion (`q6`)

- `archive/q6-scoped-skill-references-20261121` (`2135005`) is formally concluded **not adopted**: cumulative evaluation spend reached 469 calls and the Stage-1 gate did not pass.
- `q7` is built fresh on formal `main` (`b22b50d`) rather than reviving the archived pre-refactor `q6` branch. It combines the proven mechanisms from PR #276 (`34d056e`), `archive/q5-contextual-names-20261119` (`04094ee`), and the bounded closed-class `…技能` stem rule from `archive/q6-scoped-skill-references-20261121` (`2135005`), refactored cleanly onto `#291`'s `TermReference`/`TermOccurrence` domain model with storage-decoupled `term_classification.py`, and adds Step B1's two general root-cause fixes targeting `q5`/`q6`'s verified Stage-1 failure modes.

## 2. Candidate Changes Included in `q7` (Including Step B1 Root-Cause Upgrades)

1. **Pure-domain decoupling & Architecture Rules #11/#12 (`term_classification.py`, `check_architecture_boundaries.py`)**:
   - Extracts `is_derived_name_entry`, `derived_reference_only_sources`, `derived_context_gated_sources`, and `select_preferred_exact_entry` into `src/wuwaterm/term_classification.py` (imports only `constants`, `models`, `normalize`; zero `db`/`lookup`/`sqlite3` dependency).
   - Preserves `main` (`#295`)'s **Rule #11** (`PURE_MATCHING` forbidden from `db`, `lookup`, `sqlite3`, including under `TYPE_CHECKING`, expanded to all 8 pure matching modules including `skill_references` and `term_classification`) and **Rule #12** (`check_deployment_admission_manifest`), plus the transitive closure test over the 7 pure terminology modules.
2. **Single-character CJK name right-boundary admission (`cjk_span.py`, `wuwaterm-cjk-matcher-v9`)**:
   - Shared by translation reference admission and `review-v2`.
   - Requires a plausible right boundary (known lexicon word, honorific, one-character particle including `"有"`, punctuation/markup/whitespace, or existing name grammar) and closes both sides at middle-dot compounds (`红椿·蕾`, `心·蕾`, `红·椿`), preventing unknown compounds like `红椿蕊` from admitting an embedded single-character resonator name while keeping `欢迎椿来到`, `椿却没有回应`, `椿有极其优秀的`, `椿大人`, and `心大人`.
   - Exposes public `span_inside_known_word()` for Rule #9 compliance; hashes `_SINGLE_NAME_HONORIFICS` and `_SINGLE_NAME_PARTICLES` into `wuwaterm-cjk-matcher-v9` (`bcb9fe68b159…`).
3. **Sourced-name recovery in `builder.py` (audited on pinned 3.7 data `9218d612`)**:
   - Imports the complete `Advertising_VersionTitle_<n>` family from `Textmaps/*/multi_text/MultiText.json` (+7 multi-character official release titles `30`–`36`; `37` already curated; reference-only in both directions).
   - Relaxes `_item_tier_base_records` to `(zh, en)` pair uniqueness (`existing_pairs`), unlocking only `调谐器 / Tuner` alongside speaker `调音师 / Tuner` (`12,089` -> `12,097` rows, DB SHA-256 `2bc0154b…`).
   - Preserves established exact-entry ownership across deterministic whole-input paths (`lookup.preferred_exact_entry` in `sentence.py`, `application.py`, `channel.py`).
4. **Step B1 / B1b Root-Cause Fix 1 — Selective in-place locking for mixed single-char proper names & local context snippets for scoped references (`term_preparation.py`, `sentence.py`)**:
   - **Step B1b selective locking (from 4-POST Fail-Fast evidence)**: on `zh->en`, when an admitted single-character CJK proper name (`resonator`/`speaker`, `reason == "single_char_name_mention"`) coexists in `locked_text` with at least one unadmitted raw occurrence (`raw_other_count > 0`, e.g., `心`/`椿` in `ordinary-zh-en`, `心` in `D-003` & `R1-A3`, `椿` in `R1-B1`), only the admitted spans are locked in place with opaque placeholders (`__WUWA_TERM_...__ = Hsin` / `Camellya`) and removed from reference tables. Because `Locked terms:` never shows the Chinese source character, unadmitted ordinary/compound occurrences (`心很重要`, `她的心很痛`, `红椿蕊`, `香椿拌豆腐`) receive zero prompt priming. Remaining references (including `scoped_skill_stem` `变奏` in `D-003`) have their `(model_start, model_end)` coordinates re-aligned after the inserted placeholders and validated fail-closed. Unmixed single-char names (`raw_other_count == 0`, e.g. `R1-A5`) remain contextual references with zero extra locks.
   - Keeps fail-closed coordinate validation (`validated_reference_ranges()`) before any HTTP call.
   - Separates remaining occurrence-scoped references (unmixed `single_char_name_mention`, `scoped_skill_stem`, and paragraph-gated derived references with unadmitted occurrences) into a dedicated `Scoped term references:` block using deduplicated single-line local context snippets (`ONLY at:` / `NOT at:`, escalating colliding snippets through wider line -> full paragraph -> `[P{n}]` -> `[start,end)`, and compressing to `(all occurrences in this text)` only when `raw_other_count == 0` and admitted count `> 4`).
5. **Step B1 / B1b Root-Cause Fix 2 — Paragraph-level official-term context gating for derived common nouns & soft casing (`term_classification.py`, `term_preparation.py`)**:
   - Tightens context gating for derived common-noun `reference_only` rows (`Area_<n>_Title`, `tier-base:<id>`, via `derived_context_gated_sources`) and `derived_soft_casing` hints from 1,800-char segment level to **natural-paragraph (`\n\n`) level** (and falls back to non-empty lines `\n` when a non-context request has no `\n\n`): admitted only when the same paragraph contains at least one non-derived official dictionary hit (including admitted resonator/speaker names).
   - Keeps `review-v2` and `MATCHER_REVISION` (`wuwaterm-cjk-matcher-v9`) unchanged; keeps `Advertising_VersionTitle_<n>` official release titles reference-only (never hard-locked) without dropping them in standalone paragraphs (`D-005`).
   - Verified offline across all three datasets (0 POSTs):
     - `ordinary-zh-en`: drops paragraph 6 ordinary `教学楼` (directly addressing `C-derived-ordinary`), retains paragraphs 1–5 game terms;
     - `ordinary-en-zh`: drops paragraph 3 ordinary `tuner` and paragraph 4 ordinary `energy core`, retains paragraph 5 `sealed tubes` (`refs` `8 -> 5`);
     - `full-D.json`: retains all legitimate derived terms (`D-004#p5` `拉贝尔学部`, `D-005#p3` `未选择的梦`);
     - `full-R.json`: zero legitimate derived terms dropped.
6. **Bounded closed-class `…技能` 2-character stem reference rule (`skill_references.py`, `term_preparation.py`)**:
   - Derived strictly from official `BinData/skill/skilltype.json` rows 2, 3, 5, 6, and 11 (no per-word hacks); runs on `zh->en` only (`to_chinese is False`); integrated into `Scoped term references:` via `origin="scoped_skill_stem"`.

## 3. Preserved Owner Rulings & Non-Goals

- **No per-word hacks**: `变奏入场` uses the closed-class `BinData/skill/skilltype.json` 2-char stem rule; no idiom/slang prompt hacks for `D-004`/`D-005#p34`.
- **Thinking policy unchanged**: `zh->en` remains `disabled`, `en->zh` remains `omit`.
- **Timeout constants unchanged**: `45s` model timeout and `90s` request budget remain untouched.
- **No H3**: `H2` remains marked `held-out used`; no new held-out split is created.
- **Zero live LLM calls in Step B1 PR**: all verification in this Draft PR is 100% offline.

## 4. Development Base, Formal Non-Regression Baseline & Offline `reuse_key` Manifest

### 4.1 Arm Definitions (Clearly Separated)

| Role | Commit / Ref | Built DB SHA-256 (pinned `9218d612`) | `MATCHER_REVISION` | Status |
| --- | --- | --- | --- | --- |
| **Development base** | `main` (`b22b50d`, Core `v0.10.3`) | `3a5726ad9355…` (12,089 rows) | `v8` (`49b2c76152f7…`) | Offline-verified identical to `0.10.2` on DB, matcher, and 19/19 `D + ordinary` prompts |
| **Formal non-regression control** | Released Core `0.10.2` (`1d4bcdc`) | `3a5726ad9355…` (12,089 rows) | `v8` (`49b2c76152f7…`) | Primary non-regression baseline; requires fresh live run (0/19 strict `reuse_key` match with `0.9.1`) |
| **Candidate (`q7` Step B1b)** | `feat/q7-occurrence-term-admission` | `2bc0154b667a…` (12,097 rows, +8) | `v9` (`bcb9fe68b159…`) | Candidate under test; 0 live calls executed in Step B1b |
| Historical reference (`0.9.1`) | `5c636cb` | `3a5726ad9355…` (12,089 rows) | `2bbeac6dcffc…` | Historical reference only; lacks `raw_content` and `MY-359` regroup equivalence |
| Historical reference (`q5` / `q6`) | `04094ee` / `2135005` | `2bc0154b667a…` (12,097 rows) | `v9` (`30095de7a8e1…`) | `q6` concluded not adopted; Step B1b updates matcher v9 (`"有"`) and selectively locks mixed single-char names on `ordinary-zh-en`, `D-003`, `R1-A3`, `R1-B1` |

### 4.2 Offline Per-Request `reuse_key` Manifest (`D + ordinary`, 17 docs / 19 segments, Step B1b Refreshed)

Generated strictly offline (sockets blocked, `httpx.MockTransport` only, **0 live LLM calls**, **0 corpus text emitted**) via `scripts/build_q7_eval_manifest.py` (`model = deepseek-flash`, `temperature = 0`, `timeout_seconds = 45.0`, `en_zh_protocol = True`, `zh-en thinking = disabled`, `en-zh thinking = omit`).

**Verified Step B1b offline prompt state (zero negative priming on `ordinary-zh-en`):**
- `ordinary-zh-en`: **`locks = 4`** (`椿 -> Camellya` x3, `心 -> Hsin` x1), **`refs = 5`** (`Forte Circuit`, `共鸣回路`, `含苞状态`, `黑海岸`, `豆腐`), **`scoped = []`** — system prompt contains **ZERO `心 = Hsin`**, **ZERO `椿 = Camellya`**, and **ZERO `教学楼 = Academia Hall`** (`norm_sys_sha = a7e9d9aa5c61`).
- `ordinary-en-zh`: `locks = 2`, `refs = 5` (drops ordinary `tuner` & `energy core`, keeps `sealed tubes`), `norm_sys_sha = 45f7e9ffeea8`.
- `D-003`: **`locks = 2`** (`心 -> Hsin` x2 selectively locked; zero `心 = Hsin` in prompt), `refs = 20` (19 global + 1 `scoped_skill_stem` `变奏 = Intro Skill` with re-aligned coordinates), `norm_sys_sha = 5c68d41372ef`.
- `R` (`full-R.json`): `R1-A3 s2` (`心 -> Hsin` x2 locked, `scoped = []`), `R1-A5 s1` (`locks = 0`, unmixed `椿` stays reference), `R1-B1 s1` (`椿 -> Camellya` x24 locked including `椿有极其优秀的`, `scoped = []`), `R1-B1 s2` (`椿 -> Camellya` x6 locked, `scoped = []`). All 3 datasets pass `restore()` round-trip and have 0 snippet collisions.

| Doc | Seg | Dir | Chars | Paras | Thinking | Timeout | Locks (`0.10.2`→`q7`) | Refs (`0.10.2`→`q7`, `skill`) | `q7` `sys_sha` (12) | `q7` `usr_sha` (12) | `q7` `reuse_key` (12) | `q7` `adr0020_key` (12) | `0.10.2` vs `0.9.1` | `q7` vs `0.10.2`/`0.9.1` | `q7` Step B1b vs archived `q6` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `D-001` | 1/1 | `zh-en` | 17 | 3 | `disabled` | `45s` | `0→0` | `1→1` (`0`) | `94acbe10e870` | `c5607aa5477b` | `dc75fc991480` | `2a5e8c14ca42` | `must_post` (`v8`≠`0.9.1`) | `must_post` (`sys`+`db`+`v9`) | `must_post` (matcher hash updated for `"有"`) |
| `D-002` | 1/1 | `zh-en` | 250 | 13 | `disabled` | `45s` | `0→0` | `3→3` (`0`) | `150cf930d5e9` | `8d4145dca91a` | `bdc4c3b818e0` | `1e1b731b5499` | `must_post` (`v8`≠`0.9.1`) | `must_post` (`sys`+`db`+`v9`) | `must_post` (matcher hash updated for `"有"`) |
| `D-003` | 1/1 | `zh-en` | 1476 | 45 | `disabled` | `45s` | `0→2` | `19→20` (`1`) | `norm:5c68d41372ef` | `nonce` | `nonce` | `nonce` | `must_post` (`v8`≠`0.9.1`) | `must_post` (`心` locked + `变奏` scoped + `v9`) | **`must_post` (`心` x2 locked + `变奏` scoped snippets)** |
| `D-004` | 1/2 | `zh-en` | 1735 | 6 | `disabled` | `45s` | `0→0` | `16→16` (`0`) | `3b03c603015d` | `b32adf07856a` | `797b025f830f` | `331d5b3b94f0` | `must_post` (`v8`≠`0.9.1`) | `must_post` (`sys`+`db`+`v9`) | `must_post` (`拉贝尔学部` retained; matcher updated) |
| `D-004` | 2/2 | `zh-en` | 854 | 4 | `disabled` | `45s` | `0→0` | `6→6` (`0`) | `4aa67190590b` | `12cce3bc171b` | `764f9a05296d` | `b4bdcd469734` | `must_post` (`prior_seg`+`v8`) | `must_post` (`sys`+`prior_seg`+`v9`) | `must_post` (`prior_segment_not_reused`) |
| `D-005` | 1/2 | `zh-en` | 1735 | 35 | `disabled` | `45s` | `0→0` | `7→8` (`0`) | `e9446a5b05a2` | `32d80cb469ee` | `136d40a3e1a8` | `c89837c990f3` | `must_post` (`v8`≠`0.9.1`) | `must_post` (`sys`+`refs`+`db`+`v9`) | `must_post` (`未选择的梦` retained; matcher updated) |
| `D-005` | 2/2 | `zh-en` | 395 | 9 | `disabled` | `45s` | `0→0` | `2→2` (`0`) | `557015a0d2f9` | `dead0edc849d` | `53449825bfea` | `72f76e5b9d18` | `must_post` (`prior_seg`+`v8`) | `must_post` (`sys`+`prior_seg`+`v9`) | `must_post` (`prior_segment_not_reused`) |
| `D-006` | 1/1 | `en-zh` | 248 | 6 | `omit` | `45s` | `0→0` | `0→0` (`0`) | `5b5e1535b79a` | `cf50e815e870` | `8193cc31b99f` | `f80eb0264135` | `must_post` (`v8`≠`0.9.1`) | `must_post` (`sys` context rules) | `must_post` (matcher updated) |
| `D-007` | 1/1 | `en-zh` | 15 | 1 | `omit` | `45s` | `0→0` | `0→0` (`0`) | `5b5e1535b79a` | `f24d933bd6ab` | `87899a1b1341` | `81fbec05d8de` | `must_post` (`v8`≠`0.9.1`) | `must_post` (`sys` context rules) | `must_post` (matcher updated) |
| `D-008` | 1/1 | `en-zh` | 1244 | 17 | `omit` | `45s` | `0→0` | `2→3` (`0`) | `7d7fa111b87c` | `63438ed69856` | `0d39860eb41f` | `cf63984543bb` | `must_post` (`v8`≠`0.9.1`) | `must_post` (`sys`+`refs`+`db`+`v9`) | **`must_post` (updated scope instr + matcher)** |
| `D-009` | 1/1 | `en-zh` | 1542 | 30 | `omit` | `45s` | `11→11` | `1→1` (`0`) | `norm:9d72f50c02ad` | `nonce` | `nonce` | `nonce` | `must_post` (`random_lock_nonce`) | `must_post` (`random_lock_nonce`+`sys`) | **`must_post` (`random_lock_nonce` + Fix 2 `refs 2->1`)** |
| `D-010` | 1/1 | `en-zh` | 309 | 2 | `omit` | `45s` | `0→0` | `0→0` (`0`) | `5b5e1535b79a` | `cbc9e9feb10c` | `0b61d0921db8` | `b4ac51f853f4` | `must_post` (`v8`≠`0.9.1`) | `must_post` (`sys` context rules) | `must_post` (matcher updated) |
| `D-011` | 1/1 | `zh-en` | 324 | 16 | `disabled` | `45s` | `0→0` | `4→4` (`0`) | `d6eb673435cb` | `8ce15bf4372e` | `df207c4893a9` | `ed7a1a9699c2` | `must_post` (`v8`≠`0.9.1`) | `must_post` (`sys`+`db`+`v9`) | `must_post` (matcher updated) |
| `D-012` | 1/1 | `en-zh` | 1402 | 23 | `omit` | `45s` | `2→2` | `0→0` (`0`) | `norm:8135b52cc8c0` | `nonce` | `nonce` | `nonce` | `must_post` (`random_lock_nonce`) | `must_post` (`random_lock_nonce`+`sys`) | `must_post` (`random_lock_nonce`) |
| `D-013` | 1/1 | `en-zh` | 294 | 3 | `omit` | `45s` | `0→0` | `0→0` (`0`) | `5b5e1535b79a` | `15890eb991f0` | `a68359b95ab6` | `12e88d342061` | `must_post` (`v8`≠`0.9.1`) | `must_post` (`sys` context rules) | `must_post` (matcher updated) |
| `D-014` | 1/1 | `en-zh` | 920 | 11 | `omit` | `45s` | `2→2` | `0→0` (`0`) | `norm:608ef3abfdff` | `nonce` | `nonce` | `nonce` | `must_post` (`random_lock_nonce`) | `must_post` (`random_lock_nonce`+`sys`) | `must_post` (`random_lock_nonce`) |
| `D-015` | 1/1 | `en-zh` | 367 | 8 | `omit` | `45s` | `0→0` | `0→0` (`0`) | `5b5e1535b79a` | `9ae7742a5061` | `1c5785f4ffc2` | `185489967e34` | `must_post` (`v8`≠`0.9.1`) | `must_post` (`sys` context rules) | `must_post` (matcher updated) |
| `ordinary-zh-en` | 1/1 | `zh-en` | 120 | 6 | `disabled` | `45s` | `0→4` | `8→5` (`0`) | `norm:a7e9d9aa5c61` | `nonce` | `nonce` | `nonce` | `must_post` (`v8`≠`0.9.1`) | `must_post` (`心`x1+`椿`x3 locked + Fix 2) | **`must_post` (ZERO `心`/`椿`/`教学楼` in system prompt)** |
| `ordinary-en-zh` | 1/1 | `en-zh` | 262 | 6 | `omit` | `45s` | `2→2` | `2→5` (`0`) | `norm:45f7e9ffeea8` | `nonce` | `nonce` | `nonce` | `must_post` (`random_lock_nonce`) | `must_post` (`random_lock_nonce`+`sys`+`refs`) | **`must_post` (`random_lock_nonce` + Fix 2 `refs 8->5`)** |

### 4.3 Owner-Authorized Mode A Budget & 4-POST `ordinary` Fail-Fast Execution Order

Owner has authorized **Mode A** (fresh paired formal `0.10.2` vs fresh `q7`, zero `q6` reuse, **maximum `+46` live POSTs**, cumulative ledger hard cap **`515`** from `469`):

| Execution Phase | Formal `0.10.2` POSTs | `q7` Candidate POSTs | Subtotal | Gate / Stop Rule |
| --- | --- | --- | --- | --- |
| **Step 1 — `ordinary` Fail-Fast Gate** (`ordinary-zh-en` + `ordinary-en-zh`) | **2** | **2** | **4** | **Mandatory stop-loss gate**: judge `ordinary` immediately after these 4 POSTs. If `q7` fails `ordinary zh-en >= 5/6` or regresses `ordinary en-zh`, **STOP immediately** (saving up to 42 POSTs). |
| **Step 2 — Full `D` Paired Run** (`D-001`..`D-015`, 17 segments/arm) | **17** | **17** | **34** | Runs only after Step 1 passes. Zero `q6` reuse; every segment fresh on both arms. |
| **Step 3 — D-009 Option B Diagnostic Split** (fixed 3 groups `[1–10]`, `[11–20]`, `[21–30]` per arm) | **3** | **3** | **6** | Strictly diagnostic (Section 7 hard boundaries); never backfills formal `D`. |
| **Retry Reserve** (e.g. up to 1 D-009 timeout retry per arm) | `<= 1` | `<= 1` | **`<= 2`** | Every attempted POST (including timeouts/retries) counts against the `+46` / `515` ceiling. |
| **Total Authorized Mode A Ceiling** | **`19` + `3` + `<=1`** | **`19` + `3` + `<=1`** | **`<= 46`** | Cumulative ledger total `469 + 46 = 515` hard cap. |

## 5. Accurate D-009 Timeout Status (Overstrong Attribution Retracted)

- **Historical verification (`5c636cb`, Core `0.9.1`)**: Core `0.9.1` ALREADY defined `DEFAULT_LLM_TIMEOUT_SECONDS = 45.0` and passed `httpx.Timeout(self.llm_timeout_seconds)` to `httpx.AsyncClient`. `httpx.Timeout(45.0)` bounds individual socket operations (`connect`/`read`/`write`/`pool`, i.e., per-chunk wait) rather than enforcing a strict wall-clock total ceiling; the outer API 90s request middleware did not run on that historical direct-Core path.
- **Observed timeout recurrence**: In `q5-eval-20261119`, D-009 (`1,542` chars / `30` paragraphs `en->zh`, 1 call under `en->zh` `thinking=omit` + `45s`) timed out in 7 of 8 attempts (`45.3–45.7s`) and completed once at `44.291s`; in `q6-eval-20261120`, D-009 timed out in 2 of 2 attempts (`45.317s`, `45.343s`).
- **Scope of `q7` changes on `en->zh`**: Although `skill_references.py` runs on `zh->en` only, `q7` is **not** zero-change on `en->zh` — it updates `_CONTEXT_RULES_INSTRUCTION` on all context-mode requests, recovers `Tuner` / version-title rows in `builder.py`, and applies paragraph-level derived gating in `term_preparation.py` (changing `D-009`'s system prompt).
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

## 7. D-009 Option B Locked Hard Boundaries

In Mode A, Option B (separate D-009 paragraph-group diagnostic split) is locked to three hard boundaries:
1. **Formal `D` unmodified**: The original full-document single-request D-009 still runs in formal `D` evaluation; if it fails, all 30 paragraphs remain `run_failed` in the formal `D` full denominator.
2. **Identical fixed paragraph-group boundaries**: Both the formal `0.10.2` baseline arm and the `q7` candidate arm use the exact same deterministic 3-group split of D-009's 30 paragraphs (`scripts/build_q7_eval_manifest.py::paragraph_group_ranges(30, 10)`):
   - Group 1: paragraphs `1–10` (`556` chars)
   - Group 2: paragraphs `11–20` (`498` chars)
   - Group 3: paragraphs `21–30` (`484` chars)
3. **Strict diagnostic isolation**: Diagnostic split translations and grades are recorded in a separate diagnostic report only — they **never** backfill missing formal full-document D-009 results, **never** alter the formal `D` denominator or critical-error count, and **never** establish Stage-1 or Stage-2 gate passage.

## 8. Offline Verification Gate

Before Step B2 live execution starts, Step B1 passes 100% offline:
- `python scripts/validate.py` (all 6 steps: `hygiene`, `architecture` Rules #1–#12, `api-contract`, `ruff`, `typing`, `pytest`).
- `scripts/build_q7_eval_manifest.py` offline unit tests + refreshed per-request `reuse_key` manifest verification.
- GitHub Actions PR CI checks green on the pushed HEAD SHA.
- **0 live LLM calls executed in Step B1.**
