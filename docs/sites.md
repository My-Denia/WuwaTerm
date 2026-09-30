# WuwaTerm anonymous public beta Site

The current public entry is
<https://wuwaterm.denia-official.chatgpt.site>. It is anonymously reachable,
requires no WuwaTerm account, and offers Chinese-English official-term lookup,
bidirectional term-locked sentence translation, and a review workbench that
checks an existing translation against the dictionary and saves resumable local
manuscript files, all through one shared public beta pool. It is a best-effort
hobby service: one visitor can exhaust the pool, requests can be busy or fail,
and there is no SLA.

## Evidence boundaries

- **Repository fact:** `site/` is a separately built Hosted / Cloudflare Worker
  BFF. The browser calls only same-origin `/api/pool`, `/api/terms`,
  `/api/translations` and `/api/reviews`; the server-side proxy holds the device credential and
  calls the published `/v1` contract. There is no visitor account system in
  this application code.
- **Hosted platform control:** the hosting platform owns deployment versions,
  resources and any platform-level access control. Those values are not encoded
  by the application tree. Anonymous reachability does not prove a particular
  ACL value or immutable deployed-version identity.
- **VPS fact:** the supported production topology keeps dictionary lookup,
  ranking, direction detection, term locking and model calls on the authoritative
  VPS API. The Site contains no second translation pipeline. A documentation
  check or public response is not a fresh attestation of a particular VPS
  process, container or commit.
- **Current production observation (2026-09-06):** cookie-free requests reached
  the public pages and same-origin API; exact lookup and one tested sentence in
  each direction succeeded, while another admitted translation first returned a
  busy response and later succeeded after the suggested wait. This establishes
  the tested behavior, not universal model accuracy or future availability.
- **Current production observation (2026-09-13):** fresh, cookie-free browser
  sessions showed the review workbench on the public page. Exact lookups,
  Chinese-to-English sentence translations and one review check succeeded; the
  first lookup attempt returned an upstream timeout and later attempts
  succeeded. The same limits apply: this is tested behavior, not an availability
  promise.

Optional `/api/meta` exposes only term count, data schema version and the existing
request correlation ID. The Telegram bot, Windows client and API contract are
separate surfaces and are unchanged by public Site access.

## Resumable bilingual manuscripts

The review workbench saves a user-initiated `wuwaterm-manuscript-v1` JSON file.
It contains source, target, direction, up to 32 occurrence-specific decisions,
their dictionary/rule/candidate basis, explicit scalar correspondences and the
last two reports. Import is local and bounded to 1 MiB; it never sends a request.
There are no accounts, cloud manuscripts, automatic uploads or browser-storage
history. The user must download and later import the file to resume.

Sending a translation to review accepts an empty workbench directly. Sending
the same source, translation and direction again preserves choices, ranges,
reports, undo history and any running check. A different manuscript asks for
confirmation, defaulting to keeping the current work. Cancelling leaves the
current check running; confirming preserves a snapshot before replacing the
draft and stopping the old check. Undo restores the prior work and historical
reports, but a new explicit check is still required. Handoff and undo do not
send review requests automatically.

File reports are untrusted historical records, regardless of hashes or verdicts.
The first explicit check after import obtains fresh authoritative candidates.
Unchanged official choices can then be staged for a second explicit check;
an imported `not_a_term` decision always needs user confirmation. A source edit
can recover an occurrence only within an identical, unique whole source block;
changed, deleted or duplicate blocks cannot be guessed into new positions.
Direction, dictionary/rule basis or correspondence changes require confirmation.
Text edits invalidate reports and clear previously explicit correspondences.
Partial candidate/source display does not prevent reusing a visible specific
candidate whose complete identity and fresh basis match. The next request still
validates that identity against the server's full snapshot; globally truncated
reports cannot restore choices automatically.

Users may confirm selected source/target ranges, including merged or split
sentences, or explicitly leave a source region unevaluated. Correspondences are
bounded non-overlapping scalar ranges with exact text. Without explicit ranges,
v2 uses decimal-aware sentence boundaries only when both sentence counts match.
Positional correspondence is a terminology-check scope, not proof of semantic
equivalence. Target occurrences cannot satisfy multiple source mentions.

Report comparison distinguishes new findings, positively resolved terminology
constraints, still-pending items and incomparable history. Only a fresh,
comparable positive check can resolve a prior conflict; disappearance,
global report truncation, omission and changed basis never mean resolved. Imported history
cannot establish resolution. Export includes current text and explicitly states
whether its report is current; no export certifies sentence meaning.
Each runtime report also records the decisions actually submitted. A changed
decision is incomparable, not a resolved old constraint. For a partially shown
candidate/source list, only the identical explicitly selected, visible candidate
under the same full basis can support a resolved terminology constraint;
automatic partial evidence and globally truncated reports remain incomparable.

The endpoint remains `/v1/reviews`: absent `review_version` retains the original
v1 request/response and segmentation behavior. The Site opts into `review-v2`,
which adds candidate identities and a content-derived dictionary revision from
one read snapshot. Nonempty choices require current source/rule/dictionary
context and are checked against the authoritative snapshot. The Site performs
one upstream call per admitted explicit check; save/import/edit/select/compare
and export perform none. Deploy the dual-protocol API before the v2 Site;
roll back the Site to its v1 version before reverting the API.

## Shared pool contract

There is no per-visitor, per-IP or personal fair-use allowance. A single caller
can exhaust the entire pool. D1 stores one singleton row of aggregate counters:
second/minute/day window numbers and admitted request/input counts. It never
stores or derives visitor identifiers, IPs, hashes, input, output, request IDs,
accounts or history. No cleanup scheduler is required: the row is overwritten
as active windows advance; inactive old aggregate windows can remain until
another admission. This is not a time-based deletion promise.

| Boundary | Public beta ceiling |
| --- | --- |
| All upstream routes combined | 6 admissions / fixed UTC minute; 1 / UTC second |
| Translation | 1 admission / fixed UTC minute |
| Terms | 240 admissions / UTC day |
| Translation | 30 admissions and 12,000 raw Unicode characters / UTC day |
| Optional meta | 60 admissions / UTC day |
| Review | 60 admissions / UTC day; does not increment translation or character counters |
| Inputs | query <=200 trimmed Unicode characters; translation <=2,000 raw characters; review source and target each <=2,000 raw characters; streamed body <=32,768 bytes |

Terms, translations and reviews have independent daily counters. Disabling translation,
exhausting its count or character pool, or VPS model unavailability does not
disable terms or reviews. The public `/api/pool` JSON does not include a `reviews`
key. Terms can still be busy under the total short window, its own
daily cap or infrastructure failure.

One atomic conditional SQLite UPSERT checks and increments all applicable
counters. No read-then-write race or partial multi-counter reservation.
Database clock determines the windows. Admission precedes exactly one BFF
fetch. There is no automatic retry or refund on error, cancellation, ambiguous
DB response or upstream timeout. Missing/invalid settings, missing D1, failed
queries and acquisition exceeding 1s fail closed, with no upstream call.
Rejected requests do not count as admitted requests.

Public beta ceilings reserve headroom against the upstream
device contract, including adjacent fixed-window bursts and dispatch delay.
The extra second gate reduces bursts. Network delay and other users of the
same credential can still produce upstream 429; upstream admission remains
authoritative. The values shown by the live Site are a changing snapshot, not
demonstrated sustainable throughput or an availability promise.

The pool limits admitted upstream work and translation input. It does not
guarantee a currency bill, fairness, availability, DDoS resistance, bounded
inbound Worker/D1 requests, or a shared budget across the API and Telegram
processes. A <=2,000-character client request uses the existing single model
attempt path, when dictionary-first processing requires it.

## Runtime configuration

Existing secret values stay server-only in the Sites environment. Never put
them in hosting metadata, browser code, source control, evidence or URLs.

| Variable | Contract |
| --- | --- |
| `WUWATERM_API_BASE_URL` | Existing pinned HTTPS mount; unchanged |
| `WUWATERM_API_ALLOWED_HOST` | Existing exact lower-case hostname; unchanged |
| `WUWATERM_SITE_DEVICE_TOKEN` | Existing dedicated device; unchanged |
| `WUWATERM_SHARED_POOL_ENABLED` | Must explicitly equal `true`; otherwise all upstream requests fail closed |
| `WUWATERM_TRANSLATION_ENABLED` | `true` enables the current public translation surface; `false` pauses translations while preserving the independent lookup pool |
| `WUWATERM_PUBLIC_ORIGIN` | Exact approved HTTPS Sites origin with no trailing slash, credentials, path, port or query; enables canonical/OG/robots/sitemap metadata |

`.openai/hosting.json` declares logical `DB` only; no real database ID or
secret. `db/schema.ts`, `drizzle.config.ts` and generated `drizzle/` migrations
own schema changes. Runtime never creates/alters tables. Sites owns production
resources and applies migrations. A failed deployment can leave migrations
applied: inspect the applied boundary before retrying and never edit applied
migration history.

The current public origin publishes canonical, robots and sitemap metadata. The
origin never derives from request or forwarded hosts. All API responses remain
no-store/noindex and public UI errors use fixed copy.

## Validation and operations

From `site/`:

~~~sh
npm ci
npm test
npm run typecheck
npm run lint
npm run build
npm run verify:no-client-secret
~~~

Tests include proxy failure/secret contracts, real SQLite boundary fixtures and
a genuine local Cloudflare D1/Worker concurrency test using a synthetic upstream.
They do not prove a current Hosted deployment, physical VPS binding, model
reliability or public uptime. Browser acceptance and live probes are separate
evidence and must be labelled with their observation time.

Any later change to the public slug, platform access control, D1/environment,
saved version, credentials, translation switch or deployment remains a separate
owner-authorized action. Read the exact current object before changing it and
retain rollback/readback evidence. Turning off translation preserves the
independent lookup pool; never clear live counters as a recovery shortcut.

Historical owner-only, M0, runner-blocked and private-acceptance records remain
historical evidence. The current public beta does not rewrite them into success
or prove steps that those runs left unresolved. See the original
[shared-only design](superpowers/specs/2026-09-02-wuwaterm-shared-beta.md).
