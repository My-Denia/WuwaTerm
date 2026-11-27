# ADR 0023: Local sentence translation memory (`wuwaterm-tm-v1`) and incremental re-translation

- Status: **Partially implemented (Windows SQLite TM, Public Web in-memory `wuwaterm-tm-v1` import/export, three-state freshness, trusted-reuse boundary, incremental skip, Core `GET /v1/dictionary`, and self-hosted Web SQLite TM (`state-web/data/tm.sqlite3`) merged on `main`; self-hosted Web SQLite TM awaits next self-hosted release)**
- Date: 2026-11-26 (synced with shipped `wuwaterm-tm-v1` schema and three-state freshness on 2026-11-27)
- Related: `MY-491`, `MY-490`,
  [ADR 0004](0004-sqlite-terminology-data.md),
  [ADR 0006](0006-dictionary-first-before-llm.md),
  [ADR 0015](0015-desktop-manuscript-workflow.md),
  [ADR 0017](0017-contextual-term-references.md),
  [ADR 0018](0018-semantic-review.md),
  [ADR 0019](0019-document-context-and-human-precedents.md),
  [ADR 0021](0021-local-user-termbase.md),
  [ADR 0024](0024-diagnose-then-repair-targeted-review.md),
  [Privacy and LLM](../privacy-and-llm.md)

## Problem

Guides and patch announcements are edited, not rewritten. A guide gains one
line after a balance change, a patch note corrects one percentage; the rest of
the document is already translated and already confirmed by a human. Today
every save re-translates the whole document through the model, which costs
again for text nobody changed and — worse — comes back with different
renderings for sentences a human already settled, so the manuscript's
consistency drifts away from its own confirmed text.

Three existing layers do not cover this:

- `terms.db` ([ADR 0006](0006-dictionary-first-before-llm.md),
  [ADR 0004](0004-sqlite-terminology-data.md)) holds official terms, not
  sentences.
- `TranslationContext` precedents
  ([ADR 0019](0019-document-context-and-human-precedents.md)) hold
  occurrence-scoped human choices for the current request.
- `wuwaterm-termbase-v1` ([ADR 0021](0021-local-user-termbase.md)) holds user
  and community *terms*; its own section 4 table defers whole-segment memory to
  `MY-491`.

Nothing before `MY-491` kept a human-confirmed segment reusable across documents
or across edits of the same document.

## Decision

A local sentence translation memory: `wuwaterm-tm-v1`. User-local, deterministic
matching only (no embeddings or fuzzy scoring), confirmed-only,
provenance-checked, and never a shared service.

1. **Only confirmed pairs enter.** An entry enters only with `origin` in
   `{"human_confirmed", "imported_confirmed"}` — a human accepted that target for
   that source (or imported a confirmed entry from another machine). Machine
   output, drafts, and rejected candidates are refused at both the reader and the
   database constraint level. This is the same rule
   [ADR 0021](0021-local-user-termbase.md) applies to termbases, and the lesson
   `MY-228` states for review output: model output is a candidate, not
   knowledge.
2. **Deterministic matching, no semantic guessing.** Lookup ranks exact context
   matches first (`context_exact`), then exact source (`exact`), then ASCII
   digit-template variants (`numeric_variant`) and whitespace-collapsed matches
   (`normalized_exact`). No embeddings, no fuzzy similarity.
3. **Trusted reuse boundary (`context_exact` + `verified_fresh` only).** Only a
   `context_exact` match — where at least one neighbour hash is present
   (`prev_source_hash !== null || next_source_hash !== null`), `prev_source_hash`
   and `next_source_hash` match on both sides (`null` at a document edge), and
   any neighbour segment exceeding the `2,000`-scalar bound cannot be hashed or
   certified as `context_exact` — whose freshness is `verified_fresh` may enter
   batch / incremental skip (reuse without a model call). Strict two-sided
   comparison (downgrading to `exact` when either neighbour is omitted or
   changed) is merged on `main` across Windows and Web. Every other match — `exact`,
   `numeric_variant`, `normalized_exact`, `known_stale`, and
   `provenance_unknown` — is a read-only reference only and is never
   auto-applied or skipped silently.
4. **Three-state freshness decides trust.** Every entry records the dictionary
   revision (`terms_db_revision`) and game version (`game_version`) it was
   confirmed under. Lookup classifies every match into one of three states:
   `verified_fresh`, `known_stale`, or `provenance_unknown`.
5. **Storage is local and separate.** Windows desktop keeps a local SQLite file
   (`%APPDATA%/WuwaTerm/tm.sqlite3`); the public Web keeps zero automatic
   storage (`localStorage`, `sessionStorage`, and `IndexedDB` stay empty) and
   only offers explicit `wuwaterm-tm-v1` JSON import/export held in React page
   memory. Self-hosted Web persistence is served by its same-origin backend
   SQLite store (`state-web/data/tm.sqlite3`, separate from Core `terms.db`),
   merged on `WuwaTerm-Web` `main` (PR #81, `129f6a3`, awaiting the next
   self-hosted release).
6. **Incremental re-translation is the point.** Editing a document sends the
   edited segments and their boundary neighbours to the model; the untouched
   interior that qualifies as `context_exact` + `verified_fresh` can be served
   from memory without a model call, and the receipt says how much of the
   document was reused.

## Format: `wuwaterm-tm-v1`

The same JSON document is what the Windows SQLite store exports/imports and
what the Web panel exports/imports; there is no second format.

```json
{
  "format": "wuwaterm-tm-v1",
  "schema_version": 1,
  "exported_at": "2026-11-26T00:00:00Z",
  "entries": [
    {
      "direction": "en",
      "source_text": "攻击力提升20%，暴击率提升10%。",
      "target_text": "ATK +20%, CRIT Rate +10%.",
      "prev_source_hash": "1b7e000000000000000000000000000000000000000000000000000000000000",
      "next_source_hash": null,
      "origin": "human_confirmed",
      "terms_db_revision": "3.7.8-9218d61",
      "game_version": "3.7.8",
      "note": "",
      "use_count": 1,
      "created_at": "2026-11-25T09:12:00Z",
      "updated_at": "2026-11-25T09:12:00Z"
    }
  ]
}
```

### Fields and bounds (`additionalProperties: false` at both levels)

Top-level keys (canonical order):

- `format`: literal `"wuwaterm-tm-v1"`.
- `schema_version`: literal integer `1`.
- `exported_at`: RFC 3339 UTC timestamp.
- `entries`: `0` to `10,000` entry objects per document; serialized canonical
  UTF-8 JSON (2-space indent, LF, trailing newline, no BOM) must not exceed
  `8 MiB` (`8,388,608` bytes).

Entry keys (canonical order; uniqueness identity is `(direction, sha256(source_text), prev_source_hash ?? "", next_source_hash ?? "", target_text)`):

- `direction`: `"en"` (`zh→en`) or `"zh"` (`en→zh`), matching the runtime
  direction of the confirmed translation.
- `source_text`: `1..2000` Unicode scalars, non-blank, matching the API segment
  bound.
- `target_text`: `1..2000` Unicode scalars, non-blank.
- `prev_source_hash` / `next_source_hash`: 64 lowercase hex characters (SHA-256
  of the exact UTF-8 neighbouring source segment) or `null` at a document edge
  or when the neighbour is blank/whitespace-only. Both sides are checked for
  `context_exact`.
- `origin`: `"human_confirmed" | "imported_confirmed"`. Any other value is
  refused (`tm_unconfirmed_origin`). Repeated confirmation or merge never
  downgrades `"human_confirmed"` to `"imported_confirmed"`.
- `terms_db_revision`: `1..128` Unicode scalars or `null`.
- `game_version`: `1..128` Unicode scalars or `null`.
- `note`: `0..160` Unicode scalars (`""` when empty).
- `use_count`: integer in `0..2,147,483,647`.
- `created_at`: RFC 3339 UTC timestamp of initial confirmation.
- `updated_at`: RFC 3339 UTC timestamp of most recent confirmation/update.

### SQLite on Windows desktop and self-hosted

- Windows (implemented): `%APPDATA%/WuwaTerm/tm.sqlite3` beside `config.json`,
  opened with WAL journal mode and `PRAGMA synchronous = FULL`, and exported
  atomically via temporary file + `fsync` + `os.replace` — never inside a
  `.wuwaterm` manuscript, never beside device credentials.
- Schema: `tm_schema` metadata table (`format = "wuwaterm-tm-v1"`,
  `schema_version = "1"`) and `tm_entries` table with unique index
  `tm_entries_candidate` on
  `(direction, source_hash, COALESCE(prev_source_hash, ''), COALESCE(next_source_hash, ''), target_text)`,
  plus `tm_entries_norm` (`(direction, source_norm)`) and `tm_entries_masked`
  (`(direction, source_masked)`).
- Self-hosted Web (merged on `WuwaTerm-Web` `main` in PR #81 / `129f6a3`; awaiting
  next self-hosted release): served by the self-hosted Web same-origin backend
  at `state-web/data/tm.sqlite3` (in the self-hosted Web data directory) with
  the same schema and `.env` / `data/` deploy rules; user memory stays separate
  from Core `terms.db` and shared dictionary data.
- The memory is not part of `terms.db` and is never written into it: official
  data stays reproducible from pinned TextMaps
  ([ADR 0004](0004-sqlite-terminology-data.md),
  [ADR 0007](0007-runtime-builder-separation.md)).

### Public Web

- Implemented in React page state only: zero automatic browser storage
  (`localStorage`, `sessionStorage`, `IndexedDB`, and cookies stay untouched),
  matching ADR 0021 and [Privacy and LLM](../privacy-and-llm.md).
- Explicit import and export of `wuwaterm-tm-v1` JSON are the only persistence
  paths; the contract caps capacity at `10,000` entries and `8 MiB` UTF-8 across
  import, merge, confirmation, and export, alongside three-state freshness
  alignment (`verified_fresh`, `known_stale`, `provenance_unknown`), strict
  two-sided `context_exact`, and incremental skip.
- Unreused imported entries stay in React page memory only — nothing is written
  to the server, uploaded to any API, or embedded into `.wuwaterm` manuscripts,
  and closing or refreshing the tab clears them. Once a user reuses an entry in
  the current manuscript, the target text placed into the manuscript
  participates in Layer 1 structure checks and adjacent-segment translation
  context under the normal manuscript flow.

## Matching, three-state freshness, and reuse boundary

Match kinds (ranked `0..3`, ties broken by `use_count` desc, then parsed
`updated_at` instant desc):

| Match kind | Condition | Reusable for skip? |
| --- | --- | --- |
| `context_exact` | Exact `source_text`, request supplies at least one neighbour hash (`prev_source_hash !== null \|\| next_source_hash !== null`, document edges `null`, neighbours > `2,000` scalars cannot be hashed/certified), and **both** `prev_source_hash` and `next_source_hash` equal the entry | Only when `freshness === "verified_fresh"` |
| `exact` | Exact `source_text` that does not pass `context_exact` (a neighbour differs or is missing, both neighbours are `null` in a single-segment document, or an adjacent neighbour exceeds `2,000` scalars and cannot be hashed) | Read-only reference only |
| `numeric_variant` | `numeric_mask(source_text)` matches (ASCII digit runs differ) | Read-only reference only (`numeric_changed`) |
| `normalized_exact` | Whitespace/line-ending normalized source matches | Read-only reference only |

Deleting or changing either neighbour (`prev_source` or `next_source`) downgrades
`context_exact` to `exact`. Strict two-sided comparison and disqualifying
neighbours over `2,000` scalars from `context_exact` are merged on `main`
across both Windows and Web.

Three-state freshness (`freshness`), evaluated with a single cross-client
precedence rule:

1. Collect all applicable `stale_reasons` independently without masking missing
   evidence (`terms_revision_changed` when both `terms_db_revision` values are
   non-null and unequal; `game_version_changed` when both `game_version` values
   are non-null and unequal; `numeric_changed` for `numeric_variant`; and
   `provenance_unknown` whenever `current_terms_db_revision === null`,
   `entry.terms_db_revision === null`, or `game_version` is `null` on one side
   only).
2. Assign `freshness` strictly in precedence order:
   **`known_stale`** (`terms_revision_changed` or `game_version_changed` or
   `numeric_changed`) **>** **`provenance_unknown`** (no explicit mismatch,
   but missing `terms_db_revision` or one-sided `game_version`) **>**
   **`verified_fresh`**.

| Freshness | Condition | `stale_reasons` / effect |
| --- | --- | --- |
| `verified_fresh` | `current_terms_db_revision !== null` and `entry.terms_db_revision === current_terms_db_revision`, `game_version` sides are both `null` or equal, and not `numeric_variant` | `stale_reasons = []`, `needs_recheck = false`; eligible for skip only if also `context_exact` |
| `known_stale` | Both `terms_db_revision` values non-null and unequal (`terms_revision_changed`), or both `game_version` values non-null and unequal (`game_version_changed`), or `numeric_variant` (`numeric_changed`) | Carries all matching stale reasons (plus `provenance_unknown` if the other provenance field is missing); `needs_recheck = true`; read-only reference only, never auto-applied |
| `provenance_unknown` | Not `known_stale`, and `current_terms_db_revision === null`, `entry.terms_db_revision === null`, or `game_version` is `null` on one side only | `stale_reasons` contains `provenance_unknown`; `needs_recheck = true`; read-only reference only, never auto-applied |

**Trusted reuse invariant:** Only `context_exact` + `verified_fresh` may enter
batch / incremental skip (served without a model call). All other matches —
including `exact`, `numeric_variant`, `normalized_exact`, `known_stale`, and
`provenance_unknown` — remain read-only suggestions requiring human review and
never bypass translation or silently overwrite text.

Rules that follow from this:

1. **Layer 1 still runs on a reused pair.** A `context_exact` +
   `verified_fresh` reuse is passed through `structure-check-v1`
   ([ADR 0022](0022-deterministic-structure-checks.md)) like any translated
   pair; a structural finding demotes it to `needs_review` instead of serving
   it as settled.
2. **Provenance mismatch is staleness, not deletion.** A dictionary revision
   or game-version change can invalidate an official rendering or an entity
   name. The entry stays, is reported `known_stale` (or `provenance_unknown`
   when provenance cannot be verified), and earns `verified_fresh` again when a
   human confirms it under the current provenance.
3. **Multi-candidate coexistence; latest confirmation wins tie-breaks.** Distinct
   `(direction, source_hash, prev_source_hash, next_source_hash, target_text)`
   candidates coexist without overwriting each other; repeat confirmations
   increment `use_count` and update `updated_at`, while ranking prefers higher
   `use_count` then newer `updated_at`.
4. **Cross-document reuse requires a trusted match.** An entry born in one
   document only auto-reuses in another when exact text and both neighbours
   match (`context_exact`) under `verified_fresh` provenance; otherwise it is a
   read-only reference only.
5. **Bounded work per lookup.** Indexed exact/normalized/masked lookups with
   `MAX_LOOKUP_CANDIDATES` / `MAX_LOOKUP_LIMIT` caps; no unbounded full-table
   scoring scan.

## Incremental re-translation

For an edited guide or announcement:

1. Segment the document with segmentation-v1 (the same ranges the structure
   checks use) and hash every source segment plus its neighbours.
2. Classify each segment against memory: only `context_exact` +
   `verified_fresh` is eligible for zero-model-call reuse; `exact`,
   `numeric_variant`, `normalized_exact`, `known_stale`, and
   `provenance_unknown` go through translation with the stored pair available
   at most as a bounded read-only reference; unmatched segments take the normal
   path.
3. An insertion or deletion changes the neighbour hashes of at most the
   adjacent segments, so the untouched interior of a long document stays
   `context_exact` (and qualifies for skip when `verified_fresh`) — this is the
   saving the memory exists for. Untouched segments whose match is `known_stale`
   or `provenance_unknown` still go to the model rather than being silently
   reused.
4. The run receipt reports counts (reused, referenced, stale/unknown,
   re-translated) plus `terms_db_revision` and `game_version` — counts and
   provenance only, never segment text.
5. A human confirmation of a re-translated segment writes or refreshes its
   entry with the neighbours and provenance it was confirmed under, which is
   what restores `context_exact` + `verified_fresh` after an edit settles.

## Non-goals

- **No vector store, no embeddings, no fuzzy or semantic ranking.** Exact,
  whitespace-normalized, and ASCII-numeric-masked deterministic keys only; the
  moment matching becomes a similarity score, the "reused" claim stops being
  checkable.
- **No global or shared server translation memory.** No cross-user
  aggregation, no "community memory", no public endpoint that serves other
  users' confirmed text.
- **No automatic certification.** Model output, review output, and structure
  findings never create an entry.
- **No promotion into `terms.db` or `wuwaterm-termbase-v1`.** A confirmed
  sentence is not a term; promoting one would quietly change official
  dictionary behaviour (ADR 0006).
- **No new API resource in this ADR.** If a future `/v1/translations` request
  gains a memory field, that is a separate contract change with its own
  schema, limits and privacy review. (Note: `GET /v1/dictionary` and capability
  `dictionary-identity-v1` were added separately under `MY-491` as a read-only
  dictionary identity endpoint so clients do not have to guess dictionary
  freshness from `/v1/meta`; see below.)
- **Not a backup mechanism.** The memory is derived, user-local data; losing it
  costs re-translation, not the manuscript.

## Authoritative dictionary identity (`GET /v1/dictionary` and `dictionary-identity-v1`)

To let clients verify `terms_db_revision` directly without relying on `/v1/meta`
provenance tuples (`(schema_version, source_commit, term_count)`), Core exposes
read-only `GET /v1/dictionary` (scoped to `SCOPE_META`) and advertises
`"dictionary-identity-v1"` in `GET /v1/capabilities`.

- `GET /v1/dictionary` returns `revision` (the 64-character lowercase hex
  SHA-256 computed by the same deterministic `wuwaterm-review-dictionary-v1`
  algorithm as `POST /v1/reviews` `review-v2` `dictionary.revision` for the
  same dictionary state; default `review-v1` omits `revision`),
  `schema_version`, `source_commit`, `source_game_version`, `term_count`, and
  `request_id`.
- Any change to dictionary entries alters `revision` even when `schema_version`,
  `source_commit`, and `term_count` remain unchanged.
- `/v1/meta` and `/v1/reviews` response shapes remain unchanged; clients talking
  to older servers that do not advertise `dictionary-identity-v1` continue to
  fall back to `/v1/meta` for provenance.

## Gates before implementation is accepted

Frozen before any measurement, on fixed documents rather than ad-hoc samples:

1. **Reuse correctness.** On a fixed set of real edited guides exercised
   separately against three memory fixture sets — (a) entries confirmed under
   the running dictionary revision and game version, (b) `known_stale` entries,
   and (c) `provenance_unknown` entries — every qualifying (`context_exact` +
   `verified_fresh`) untouched interior segment in (a) is reused with zero
   model calls; every segment whose source or neighbours changed, as well as
   every untouched segment in (b) `known_stale` and (c) `provenance_unknown`,
   goes to the model rather than being silently reused; and deleting, changing,
   or exceeding `2,000` scalars on either neighbour downgrades to `exact`.
2. **Staleness & unknown provenance.** 100% of entries with a `known_stale`
   cause (`terms_revision_changed`, `game_version_changed`, or
   `numeric_changed`) are classified `known_stale`, and 100% of entries with
   missing or one-sided `null` provenance and no `known_stale` cause are
   classified `provenance_unknown`. As an explicit gate case (aligned on
   Windows and Web), a `numeric_variant` entry with missing or one-sided
   `null` provenance must be classified `known_stale` while retaining both
   `numeric_changed` and `provenance_unknown` in `stale_reasons`. Neither
   `known_stale` nor `provenance_unknown` may ever be auto-applied or skipped.
3. **Manual-confirmed-only.** A test proves no code path creates an entry from
   model output, review output, or an imported file that claims an `origin`
   outside `"human_confirmed" | "imported_confirmed"`.
4. **Structure checks still run.** Every reused pair passes through
   `check_segment_structure`, and a finding demotes it.
5. **Privacy.** No entry text in logs, receipts, or error messages; the Web
   path stores nothing after the tab closes.
6. **Measured saving.** Model calls and latency per 1,000 characters on an
   unchanged document and on an edited one, reported as the ratio the memory
   claims, before the feature is called done.

## Consequences

- Editing a confirmed document costs model calls only for what changed, and
  the settled parts of the document stop drifting between runs.
- The user owns their memory: it is one portable `wuwaterm-tm-v1` JSON document
  shared byte-for-byte between Windows and Web, inspectable without the
  service.
- The service gains no shared knowledge from it. Whatever the memory learns
  stays on the machine (or current Web page session) that confirmed it, keeping
  the official dictionary and public Web stateless.
