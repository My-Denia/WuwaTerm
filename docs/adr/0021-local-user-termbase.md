# ADR 0021: Local user termbase (`wuwaterm-termbase-v1`) and Core engine next-step research

- Status: **Proposed (architecture and format spec; pending implementation)**
- Date: 2026-11-25
- Related: `MY-380`, `MY-18`, `MY-489`, `MY-491`,
  [ADR 0006](0006-dictionary-first-before-llm.md),
  [ADR 0015](0015-desktop-manuscript-workflow.md),
  [ADR 0017](0017-contextual-term-references.md),
  [ADR 0018](0018-semantic-review.md),
  [ADR 0019](0019-document-context-and-human-precedents.md),
  [ADR 0020](0020-held-out-usability-evaluation.md),
  [H3 & Raw Baseline Protocol](../eval/h3-and-raw-baseline-protocol.md),
  [Privacy and LLM](../privacy-and-llm.md)

## Problem

Official game data (`terms.db`) covers canonical TextMap nouns, resonators,
weapons, echoes, skills, and locations, while [ADR 0019](0019-document-context-and-human-precedents.md)
captures occurrence-scoped manuscript choices (`scope: "occurrence"`). Neither
layer addresses two recurring gaps observed in `MY-380` and `H1`:

1. **Community / guide terminology and user-preferred spellings.** Guides and
   player discussions (`攻略`, `混合语境`) rely heavily on community shorthand,
   team nicknames, mechanics slang, and user-preferred or do-not-translate
   tokens that do not exist in official TextMaps and should never contaminate the
   shared official `terms.db`.
2. **Core engine bottlenecks on `zh->en` guides and multi-segment cohesion.**
   `zh->en` currently runs with `WUWATERM_TRANSLATION_THINKING_ZH_EN=disabled`,
   while `en->zh` already benefits from `select_en_zh_auto_thinking` (`auto`
   in `src/wuwaterm/translation_plan.py`). In addition, `TranslationContext`
   carries only `before` (up to `400` scalars of the immediately preceding
   segment), so longer guides lose track of section headings or active
   resonator/entity mentions established earlier in the manuscript.

Solving these without compromising `ADR 0017`, `ADR 0018`, `ADR 0019`, or
[Privacy and LLM](../privacy-and-llm.md) requires separating local user
terminology from official game data and sentence translation memory, and gating
any Core engine changes on the frozen [H3 evaluation protocol](../eval/h3-and-raw-baseline-protocol.md).

## Decision

### 1. Four Strictly Separated Knowledge Layers

| Layer | Name & Ownership | Scope & Persistence | Model-Path Authority |
| --- | --- | --- | --- |
| **1. Official Dictionary** | `data/terms.db` (Core builder from pinned TextMaps) | Global read-only SQLite; version-pinned | Owns deterministic exact/fuzzy lookup, `review-v1`/`review-v2`, and official hard locks / references (`ADR 0006`, `ADR 0017`) |
| **2. In-Manuscript Context & Precedents** | `TranslationContext` (`before`, `precedents`) (`ADR 0019`) | Single manuscript / request (`scope: "occurrence"`) | Read-only previous context + explicit human occurrence choices (`scope: "occurrence"`, off by default until M′ gates pass) |
| **3. Local User / Community Termbase (`TB`)** | `wuwaterm-termbase-v1` (this ADR) | User-local file or explicit import/export JSON; never shared globally | Enters model calls **only** as non-binding contextual references (`origin: "user_glossary"`); never creates hard locks |
| **4. Future Local Sentence Translation Memory (`TM`)** | Deferred to `MY-491` (out of scope here) | User-local segment/sentence pairs with provenance & staleness | Reserved for future whole-segment TM; distinct from term-level `TB` |

### 2. Portable JSON Format: `wuwaterm-termbase-v1`

A minimal, portable UTF-8 JSON format shared across `WuwaTerm-Windows`,
`WuwaTerm-Web` explicit import/export, and future Core request/matching adapters:

```json
{
  "format": "wuwaterm-termbase-v1",
  "schema_version": 1,
  "updated_at": "2026-11-25T00:00:00Z",
  "entries": [
    {
      "id": "tb-001",
      "source": "主C",
      "target": "main DPS",
      "direction": "en",
      "kind": "community_term",
      "note": "Community team role shorthand in guides.",
      "updated_at": "2026-11-25T00:00:00Z"
    }
  ]
}
```

#### Schema & Validation Constraints

- **Top-level fields** (`additionalProperties: false`):
  - `format`: literal string `"wuwaterm-termbase-v1"`.
  - `schema_version`: literal integer `1`.
  - `updated_at`: RFC 3339 / ISO-8601 UTC timestamp string.
  - `entries`: JSON array of entry objects.
- **Entry fields** (`additionalProperties: false`):
  - `id`: unique non-empty ASCII/UTF-8 identifier string (`1..64` scalars).
  - `source`: non-empty source surface string (`1..120` scalars).
  - `target`: non-empty target rendering string (`1..160` scalars; for `do_not_translate`, holds the preserved surface/casing).
  - `direction`: `"en" | "zh"` — matching Core `TranslationDirection` (`"en"` = `zh->en`, `"zh"` = `en->zh`).
  - `kind`: `"community_term" | "preferred_term" | "do_not_translate"`.
  - `note`: string (`0..160` Unicode scalars; contextual hint only, never executable instructions).
  - `updated_at`: RFC 3339 / ISO-8601 UTC timestamp string.
- **Hard size and security bounds**:
  - Maximum **`2,000` entries** per termbase file.
  - Maximum **`1 MiB` (`1,048,576` bytes)** serialized UTF-8 file size.
  - Reject unknown keys (`additionalProperties: false`), duplicate object keys, `NaN`/`Infinity`, isolated UTF-16 surrogates, NUL bytes, and prototype-pollution keys (`"__proto__"`, `"constructor"`, `"prototype"`) at any level.

### 3. Safety Invariants (`ADR 0017`, `ADR 0018`, `ADR 0019`)

1. **Contextual reference only — never a hard lock.** Matched `wuwaterm-termbase-v1` entries enter model prompts **only** as bounded, non-binding contextual references (`origin="user_glossary"`), explicitly labeled as user preference references for the model to judge by context. They **never** generate `__WUWA_TERM_...__` placeholder locks (`ProtectedText` / `restore()`) and never trigger `missing_placeholder` failures.
2. **Official `terms.db` precedence.** Official `terms.db` exact hits, hard locks, and official reference rows **always** take precedence over `wuwaterm-termbase-v1` on any overlapping surface, unless the user explicitly records an occurrence-scoped manuscript choice (`ADR 0019` `precedents`, `scope: "occurrence"`).
3. **No automatic certification or cross-document sense locking.** Automatic user-glossary matches never modify `/v1/reviews` (`review-v1`/`review-v2`), never emit `verified_constraint`, and never create cross-document or whole-manuscript sense locks (`ADR 0018`, `ADR 0019`).
4. **Prompt-injection isolation.** Entry `source`, `target`, and `note` fields are untrusted data passed in bounded structured form (never raw prompt instructions) and subject to `UNTRUSTED_SOURCE_INSTRUCTION` and per-request scalar/count caps.
5. **Deterministic paths unchanged.** Whole-input `/v1/terms`, exact dictionary hits without an LLM call, and the no-model local fallback path remain governed solely by `terms.db`.

### 4. Privacy & Storage Boundaries

- **Windows desktop (`WuwaTerm-Windows`)**:
  - Stores the user's termbase locally at `%APPDATA%/WuwaTerm/termbase.json` using atomic writes (write temporary file in the same directory, flush, replace — matching [ADR 0015](0015-desktop-manuscript-workflow.md)).
  - Excluded from `.wuwaterm` manuscript exports by default unless explicitly bundled or referenced by user action.
  - Strictly isolated from device credentials/tokens.
- **Public Web (`WuwaTerm-Web`)**:
  - Maintains **zero automatic browser storage**: `localStorage` and `IndexedDB` remain empty by default, preserving [Privacy and LLM](../privacy-and-llm.md).
  - Supports explicit user **import and export** of `wuwaterm-termbase-v1` JSON held in current page memory only (any opt-in browser persistence would require separate authorization).
- **No shared global dictionary mixing**:
  - Public site user inputs or imported termbases are **never** written to, aggregated into, or promoted into any shared server dictionary or `terms.db`.
  - When sent per request to Core `/v1/translations` (future opt-in API extension), matched entries are request-scoped, never persisted to disk, and never logged.

## Companion Core Engine Next-Step Research Design

*(Cross-references `MY-380`, `MY-18`, `MY-489`; gated on [H3 protocol](../eval/h3-and-raw-baseline-protocol.md) budget approval — **0 live LLM calls executed**.)*

1. **`zh->en` adaptive thinking (`WUWATERM_TRANSLATION_THINKING_ZH_EN=auto` candidate)**:
   - Mirror `select_en_zh_auto_thinking` and `RequestPolicy` in `src/wuwaterm/translation_plan.py`:
     - Route short or guide/term-dense `zh->en` segments (where `A`-rate, numeric qualifiers, and homograph/skill-stem disambiguation dominate) to `low` thinking.
     - Route long or lock-dense `zh->en` segments (where thinking latency risks the `45 s` `DEFAULT_LLM_TIMEOUT_SECONDS`) to `disabled`.
   - Governed by `OperationExecutionBudget`: **at most 1 corrective call per segment** (shared with HTML fallback and timeout fallback), preserving `45 s` model timeout and `90 s` request budget.
   - `disabled` remains the unconfigured default (`TRANSLATION_THINKING_ZH_EN_DEFAULT`) until `H3` demonstrates statistically clear `A`-rate / critical-error gain without timeout regressions.
2. **Bounded read-only cross-segment section/entity anchor**:
   - Extend `TranslationContext` read-only metadata beyond the single `before` (`400`-scalar previous segment) with a strictly bounded read-only anchor carrying the active Markdown heading and recently established resonator/entity mentions from earlier segments (capped at a fixed scalar budget, e.g., `<= 240`–`320` scalars).
   - **Strict invariants**:
     - Read-only context **never** participates in `term_preparation` or `skill_references` admission (`ADR 0017` rule: read-only previous context never enters admission).
     - **Never** creates automatic placeholder locks or cross-segment sense locks (`ADR 0019` decision 4).
     - Requests with `context=None` keep byte-identical prompts (`test_translation_plan` / `OperationIdentity` invariants).

## Consequences

- Community and personal terms gain a portable, schema-validated `wuwaterm-termbase-v1` representation without polluting `terms.db`, leaking across anonymous Web visits, or weakening `ADR 0017`/`ADR 0019` guards.
- `WuwaTerm-Windows`, `WuwaTerm-Web`, and Core `/v1` can adopt `wuwaterm-termbase-v1` incrementally behind explicit capability/schema checks without breaking existing `.wuwaterm` manuscripts or `/v1/translations` callers.
- Any Core engine adoption of `zh->en` adaptive thinking, `user_glossary` prompt injection, or read-only section/entity anchors remains blocked until evaluated under frozen `H3` (`Arm C` vs `wuwaterm_prod` and `raw_baseline`) after `MY-229` merges.
