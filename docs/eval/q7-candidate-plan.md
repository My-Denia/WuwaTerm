# q7 Translation Quality Candidate Plan & Frozen Stage-1 Proposal

- Status: **Candidate Draft PR — 0 live LLM calls executed; awaits owner budget authorization before any live evaluation or merge.**
- Branch: `feat/q7-occurrence-term-admission`
- Base: `main` (`a724c2b`, including Web `0.4.2` default from #290 and structured `TermReference`/`TermOccurrence` + private-import guard from #291)
- Matcher revision: `wuwaterm-cjk-matcher-v9` (bumped cleanly from `wuwaterm-cjk-matcher-v8` on `main`; `v8` and `v9` never collide)
- Related: MY-18, MY-380, [ADR 0017](../adr/0017-contextual-term-references.md), [ADR 0020](../adr/0020-held-out-usability-evaluation.md), [translation-reference-admission.md](../translation-reference-admission.md), [next-batch.md](next-batch.md)

## 1. Prior Candidate Conclusion (`q6`)

- `archive/q6-scoped-skill-references-20261121` (`2135005`) is formally concluded **not adopted**: cumulative evaluation spend reached 469 calls and the Stage-1 gate did not pass.
- `q7` is built fresh on formal `main` (`a724c2b`) rather than reviving the archived pre-refactor `q6` branch. It combines the proven mechanisms from PR #276 (`34d056e`), `archive/q5-contextual-names-20261119` (`04094ee`), and the bounded closed-class `…技能` stem rule from `archive/q6-scoped-skill-references-20261121` (`2135005`), refactored cleanly onto `#291`'s `TermReference`/`TermOccurrence` domain model.

## 2. Candidate Changes Included in `q7`

1. **Single-character CJK name right-boundary admission (`cjk_span.py`, `wuwaterm-cjk-matcher-v9`)**:
   - Shared by translation reference admission and `review-v2`.
   - Requires a plausible right boundary (known lexicon word, honorific, one-character particle, punctuation/markup/whitespace, or existing name grammar) and closes both sides at middle-dot compounds (`红椿·蕾`, `心·蕾`, `红·椿`), preventing unknown compounds like `红椿蕊` from admitting an embedded single-character resonator name while keeping `欢迎椿来到`, `椿却没有回应`, `椿大人`, and `心大人`.
   - Exposes public `span_inside_known_word()` so `skill_references.py` complies with architecture boundary Rule #9 (no private cross-module `_name` imports).
   - Bumps `MATCHER_REVISION` from `wuwaterm-cjk-matcher-v8` to `wuwaterm-cjk-matcher-v9` with `_SINGLE_NAME_HONORIFICS` and `_SINGLE_NAME_PARTICLES` included in the revision hash.
2. **Sourced-name recovery in `builder.py` (audited on pinned 3.7 data `9218d612`)**:
   - Imports the complete `Advertising_VersionTitle_<n>` family from `Textmaps/*/multi_text/MultiText.json` (skipping already-curated `CORE_TERM_KEYS`). On pinned `9218d612`, this adds exactly 7 multi-character official release titles (`Advertising_VersionTitle_30` through `Advertising_VersionTitle_36`; `37` is already curated) — zero short or noisy ordinary words. Runtime treats them as contextual references only on both directions (`lookup.is_derived_name_entry` / `derived_reference_only_sources`).
   - Relaxes `_item_tier_base_records` collision filtering from single-side surface uniqueness to `(zh, en)` pair uniqueness (`existing_pairs`). On pinned `9218d612`, this unlocks exactly 1 additional derived pair: `调谐器 / Tuner` alongside the speaker `调音师 / Tuner`, with zero noisy additions.
   - Preserves established exact-entry ownership across deterministic whole-input paths (`lookup.preferred_exact_entry`, called by `sentence.py`, `application.py`, and `channel.py`) so derived rows never displace an established exact hit while dictionary lookup still exposes all candidates.
3. **Pre-exclusion casefold collision retention & occurrence-scoped references (`term_matching.py`, `term_preparation.py`, `sentence.py`)**:
   - Retains `pre_exclusion_sources` in `model_reference_candidates` (PR #276 `34d056e`) so excluding one cased surface in a shared casefold key continues to block derived soft-spelling synthesis.
   - Collapses multi-table identical `(zh, en)` rows to one reference line (highest `CATEGORY_ORDER`) while exposing competing distinct pairs (such as `Tuner`) as contextual alternatives rather than hard locks.
   - Gates derived soft-casing hints (`origin="derived_soft_casing"`, `reason="soft_casing"`) on actual dictionary term context in the submitted segment, keeping isolated ordinary prose untouched.
   - Carries visible-segment `(start, end)` and post-placeholder `(model_start, model_end)` coordinates directly on `TermOccurrence` inside `TermReference` (plain and protected multi-segment paths), emitting `_SCOPED_REFERENCE_INSTRUCTION` and `Reference occurrence ranges` strictly on demand so plain single-meaning requests remain byte-identical.
   - Carries `q5`'s context-mode fidelity rules in `_CONTEXT_RULES_INSTRUCTION` (numeric shorthand such as `3+1`/`S0R1`, omitted actors/rhetorical addressees, and translating every clause), active only when `context is not None`.
4. **Bounded closed-class `…技能` 2-character stem reference rule (`skill_references.py`, `term_preparation.py`)**:
   - General rule derived strictly from the five official `BinData/skill/skilltype.json` rows (`SkillType_{2,3,5,6,11}_TypeName`) — no per-word hardcoding.
   - Runs **only on `zh->en`** (`to_chinese is False`), adding zero prompt bytes or behavior changes on `en->zh`.
   - Requires local action syntax plus independent local game-context evidence (or `切<resonator/speaker>`) in the same clause, skips spans inside known lexicon words or covered by longer dictionary matches (`变奏技能`), and requires a unique local sense marker for ambiguous stems (`共鸣` -> `技能`/`解放`/`回路`).
   - Emits `TermReference(..., origin="scoped_skill_stem", reason="local_skill_action")` with exact `TermOccurrence` `(model_start, model_end)` ranges validated fail-closed before any HTTP call, addressing `q5`'s single `D-003` (`变奏入场`) regression without bloating ordinary prose.

## 3. Preserved Owner Rulings & Non-Goals

- **No per-word hacks**: `变奏入场` is handled exclusively through the closed-class `BinData/skill/skilltype.json` 2-character stem rule.
- **Thinking policy unchanged**: `zh->en` remains `disabled`, `en->zh` remains `omit`.
- **Timeout constants unchanged**: `45s` model timeout and `90s` request budget remain untouched.
- **No H3**: `H2` remains marked `held-out used`; no new held-out split is created.
- **Zero live LLM calls in this PR**: all verification is offline (`scripts/validate.py` + GitHub Actions CI).

## 4. Verified D-009 Timeout Root-Cause Diagnosis

Cross-checking the recorded `q5-eval-20261119` and `q6-eval-20261120` evaluation ledgers establishes that D-009 timeouts are a pre-existing single-call latency characteristic of D-009 under `en->zh` `thinking=omit` + `45s` timeout, **not** a regression introduced by `q5`, `q6`, or `q7`:

| Source / Ledger | D-009 Request Shape | Settings | Observed Outcome |
| --- | --- | --- | --- |
| Historical Core `0.9.1` | 1,542 chars / 30 paragraphs `en->zh`, 1 call | No API timeout middleware on that path | Completed without 45s timeout termination |
| `q5-eval-20261119` | 1,542 chars / 30 paragraphs `en->zh`, 1 call | `en->zh` `thinking=omit`, `45s` timeout | Timed out **7 of 8** attempts (`45.3–45.7s`); barely succeeded once at `44.291s` |
| `q6-eval-20261120` | 1,542 chars / 30 paragraphs `en->zh`, 1 call | `en->zh` `thinking=omit`, `45s` timeout | Timed out **2 of 2** attempts (`45.317s`, `45.343s`) |

Key factual consequences:
- `D-009` sits right at the `45s` boundary when sent as a single 1,542-scalar / 30-paragraph `en->zh` call under provider-default `omit` thinking.
- `q7`'s `skill_references` rule is `zh->en` only and adds zero prompt tokens or logic to `en->zh`.
- Re-running D-009 unchanged as 1 call under `45s` + `omit` should be expected to reproduce the same ~7/8 timeout rate unless an explicit owner-approved diagnostic option is selected below.

## 5. Frozen Stage-1 Validation Proposal (Awaiting Owner Budget Approval)

No live call may run until the owner explicitly authorizes budget for `q7`.

- **Proposed Stage-1 scope**: `D + ordinary` control set, strictly bounded to **`<= 25` live calls** total (reusing verified baseline records where reuse keys match; every attempted POST counted against the cap).
- **Stage-1 pass criteria (frozen before first live call)**:
  - `zh->en` ordinary-word controls: preserve `q5`'s `>= 5/6` (no regression on ordinary prose such as plant/music words).
  - `D` official names: retain `q5` gains and recover `D-003` (`变奏入场` -> `Intro Skill`) via the general `scoped_skill_stem` rule.
  - `D` critical errors: non-regression against baseline (`<= 18` critical errors on judged segments, no new systematic C-grade class).
  - Zero `zh->en` timeouts under `disabled` + `45s`.
- **Owner decision needed for D-009 before Stage-1 execution**:
  - **Option A (strict single-segment gate)**: Run D-009 as 1 single 1,542-scalar segment under the unchanged `en->zh` `omit` + `45s` settings, accepting its verified ~7/8 historical timeout risk and capping retries strictly within the `<= 25` budget.
  - **Option B (separate diagnostic paragraph-group split for D-009)**: Keep the frozen `45s`/`90s` timeouts and `omit` policy unchanged, run D-009 once under the standard single-segment contract for ledger parity, and separately record a bounded diagnostic paragraph-group split of D-009 (reported outside the single-segment gate) to evaluate `q7`'s `en->zh` term/name quality on D-009's 30 paragraphs without conflating latency timeout noise with translation quality.

## 6. Offline Verification Gate

Before requesting Stage-1 budget authorization, this Draft PR must pass 100% offline:
- `python scripts/validate.py` (all 6 steps: `hygiene`, `architecture`, `api-contract`, `ruff`, `typing`, `pytest`).
- GitHub Actions PR CI checks green on the pushed HEAD SHA.
- **0 live LLM calls executed.**
