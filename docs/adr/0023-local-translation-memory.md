# ADR 0023: Local sentence translation memory (`wuwaterm-tm-v1`) and incremental re-translation

- Status: **Proposed (architecture and format spec; not implemented)**
- Date: 2026-11-26
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

Nothing today keeps a human-confirmed segment reusable across documents or
across edits of the same document.

## Decision

A local sentence translation memory: `wuwaterm-tm-v1`. User-local, exact-match
only, manual-confirmed only, provenance-checked, and never a shared service.

1. **Only manual-confirmed pairs enter.** An entry exists because a human saved
   and confirmed that target for that source (ADR 0015 manuscript confirmation,
   or the same confirmation reached through the desktop or self-hosted
   surface). A model translation never becomes an entry by being produced,
   reviewed, or used. This is the same rule
   [ADR 0021](0021-local-user-termbase.md) applies to termbases, and the lesson
   `MY-228` states for review output: model output is a candidate, not
   knowledge.
2. **Exact match only.** An entry is looked up by the hash of its normalized
   source plus direction. No embeddings, no fuzzy ranking, no semantic
   similarity. A sentence is reused or it is not; anything in between is left
   to the model and the human.
3. **Two match strengths.** A `context_exact` match (source *and* both
   neighbours unchanged) is reusable without a model call. A `source_exact`
   match (the source is the same, a neighbour changed) is a read-only
   reference for that call, never applied silently — an inserted line moves the
   surrounding sentences into different company.
4. **Provenance decides freshness.** Every entry records the dictionary
   revision and game version it was confirmed under. An entry whose provenance
   no longer matches the running instance is `stale`: it is read, reported as
   stale, and never auto-applied.
5. **Storage is local and separate.** Windows desktop and self-hosted
   instances keep a SQLite file next to their own data; the public Web keeps
   zero automatic storage and only offers explicit
   `wuwaterm-tm-v1` JSON import/export held in page memory.
6. **Incremental re-translation is the point.** Editing a document sends the
   edited segments and their boundary neighbours to the model; the untouched
   interior is served from the memory, and the receipt says how much of the
   document was reused.

## Format: `wuwaterm-tm-v1`

The same JSON document is what the SQLite store exports and what a user can
import; there is no second format.

```json
{
  "format": "wuwaterm-tm-v1",
  "schema_version": 1,
  "exported_at": "2026-11-26T00:00:00Z",
  "entries": [
    {
      "id": "tm-001",
      "direction": "en",
      "source": "攻击力提升20%，暴击率提升10%。",
      "target": "ATK +20%, CRIT Rate +10%.",
      "source_hash": "9f2c…",
      "prev_source_hash": "1b7e…",
      "next_source_hash": null,
      "origin": "manual_confirmed",
      "document_kind": "guide",
      "terms_db_revision": "3.7.8-9218d61",
      "game_version": "3.7.8",
      "confirmed_at": "2026-11-25T09:12:00Z"
    }
  ]
}
```

### Fields and bounds (`additionalProperties: false` at both levels)

- `format`: literal `"wuwaterm-tm-v1"`.
- `schema_version`: literal integer `1`.
- `exported_at`: RFC 3339 UTC timestamp.
- `entries`: 0 to 20,000 entry objects per document; a larger memory exports
  in several documents rather than one unbounded file.
- `id`: unique within the document, `1..64` printable characters.
- `direction`: `"en"` (`zh→en`) or `"zh"` (`en→zh`), matching the runtime
  direction of the confirmed translation.
- `source`: `1..2000` Unicode scalars, the same side limit the API applies.
- `target`: `1..2000` Unicode scalars.
- `source_hash`: 64 lowercase hex characters — SHA-256 over
  `normalize_text(source)` with the direction prepended, so `zh→en` and
  `en→zh` never collide.
- `prev_source_hash` / `next_source_hash`: the same hash of the neighbouring
  source segments in the document the entry was confirmed in, or `null` at a
  document edge. They are what makes `context_exact` checkable.
- `origin`: literal `"manual_confirmed"`. The field exists so an imported file
  that claims anything else is rejected rather than silently trusted.
- `document_kind`: `"guide" | "announcement" | "manuscript" | "other"`.
  Reporting only; it never changes matching.
- `terms_db_revision`, `game_version`: the provenance the entry was confirmed
  under, from the running instance at confirmation time.
- `confirmed_at`: RFC 3339 UTC timestamp of the human confirmation.

### SQLite on Windows desktop and self-hosted

- Windows: `%APPDATA%/WuwaTerm/translation-memory.db`, written with the same
  atomic replace discipline as the manuscript (ADR 0015), in its own file —
  never inside a manuscript, never beside credentials.
- Self-hosted: the instance's local data directory, same schema, one file per
  instance. `.env` and `data/` handling follows the deploy rules the project
  already has; the memory is user data, not shared dictionary data.
- Schema: `PRAGMA user_version = 1`, one `tm_entries` table with the fields
  above and indexes on `(direction, source_hash)` and
  `(direction, source_hash, prev_source_hash, next_source_hash)`.
- The memory is not part of `terms.db` and is never written into it: official
  data stays reproducible from pinned TextMaps
  ([ADR 0004](0004-sqlite-terminology-data.md),
  [ADR 0007](0007-runtime-builder-separation.md)).

### Public Web

- Zero automatic browser storage: `localStorage` and `IndexedDB` stay empty,
  matching ADR 0021 and [Privacy and LLM](../privacy-and-llm.md).
- Explicit import and export of `wuwaterm-tm-v1` JSON are the only paths, and
  the content lives in the current page memory only.
- Imported entries are request-scoped for that page; nothing is written to the
  server, aggregated, or kept after the tab closes.

## Matching and staleness

| Match | Key | Effect |
| --- | --- | --- |
| `context_exact` | `direction` + `source_hash` + both neighbour hashes | Reused without a model call; the reuse is reported |
| `source_exact` | `direction` + `source_hash`, neighbours differ | Read-only reference for that call; never applied silently |
| `stale` | any of the above, and provenance ≠ current runtime | Reported as stale, never auto-applied |
| none | — | The segment takes the normal translation path |

Rules that follow from this:

1. **Layer 1 still runs on a reused pair.** A `context_exact` reuse is passed
   through `structure-check-v1`
   ([ADR 0022](0022-deterministic-structure-checks.md)) like any translated
   pair; a structural finding demotes it to `needs_review` instead of serving
   it as settled.
2. **Provenance mismatch is staleness, not deletion.** A dictionary revision
   or game-version change can invalidate an official rendering or an entity
   name. The entry stays, is reported stale, and earns its place again when a
   human confirms it under the current provenance.
3. **Latest confirmation wins, history stays.** Two confirmations for the same
   key keep both rows; matching reads the newest by `confirmed_at`. Nothing is
   rewritten in place.
4. **No cross-document admission.** An entry born in one document is usable in
   another only under the same rule that admitted it (manual confirmation) and
   only for the same direction; `document_kind` is reporting, not permission.
5. **Bounded work per lookup.** Exact hash lookups only, at most two memory
   reads per segment (context, then source); no scan of the whole memory per
   segment.

## Incremental re-translation

For an edited guide or announcement:

1. Segment the document with segmentation-v1 (the same ranges the structure
   checks use) and hash every source segment plus its neighbours.
2. Solve each segment: `context_exact` is filled from the memory with no model
   call; `source_exact` and `stale` go to the model with the stored pair as a
   bounded read-only reference; everything else takes the normal path.
3. An insertion or deletion changes the neighbour hashes of at most the
   adjacent segments, so the untouched interior of a long document stays
   `context_exact` — this is the saving the memory exists for.
4. The run receipt reports counts: reused (`context_exact`), referenced
   (`source_exact`), stale, re-translated; plus `terms_db_revision` and
   `game_version`. Counts and provenance only, never segment text.
5. A human confirmation of a re-translated segment writes or refreshes its
   entry with the neighbours it was confirmed under, which is what restores
   `context_exact` after an edit settles.

## Non-goals

- **No vector store, no embeddings, no fuzzy or semantic ranking.** Exact
  hashes only; the moment matching becomes a score, the "reused" claim stops
  being checkable.
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
  schema, limits and privacy review.
- **Not a backup mechanism.** The memory is derived, user-local data; losing it
  costs re-translation, not the manuscript.

## Gates before implementation is accepted

Frozen before any measurement, on fixed documents rather than ad-hoc samples:

1. **Reuse correctness.** On a set of real edited guides, exactly the segments
   whose source or neighbours changed are sent to the model; every untouched
   interior segment is reused with zero model calls.
2. **Staleness.** 100% of entries whose `terms_db_revision` or `game_version`
   differs from the running instance are reported stale and none is
   auto-applied.
3. **Manual-confirmed-only.** A test proves no code path creates an entry from
   model output, review output, or an imported file that claims another
   `origin`.
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
- The user owns their memory: it is one portable JSON document, it can be
  exported, imported, and inspected without the service.
- The service gains no shared knowledge from it. Whatever the memory learns
  stays on the machine that confirmed it, which is the constraint that keeps
  the official dictionary and the public Web unchanged.
- Implementation is not started: `MY-491` is the work item, and this ADR
  fixes the format and the invariants before any code exists.
