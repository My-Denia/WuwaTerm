# Architecture

Maintainer entry for the **current** system: Telegram command routes, an HTTP
API and its in-process web layer use the shared application pipeline;
linked-channel posts retain specialized channel orchestration. A desktop
client and a separately hosted anonymous public beta Site consume the API.
Claims map to source, tests, deploy scripts, and Compose topology. This is not an
aspirational redesign.

Operational detail lives in sibling guides. Decision rationale lives in
[ADRs](adr/). Automated import-direction and non-goal gates live under
`scripts/` and CI.

## System context

```
Telegram users / groups / linked discussion groups        owner's PC and browser
                  |                                      |
                  | Bot API (long polling)               | https, cert verified
                  v                                      v
+-----------------------------------+  +---------------------------------+
| wuwaterm-bot (Compose service)    |  | reverse proxy already serving   |
|  presentation: bot.py, channel.py |  | the operator's existing sites   |
+-----------------+-----------------+  +-----------------+---------------+
                  | command routes                       | 127.0.0.1 (loopback)
                  |                                      v
                  |                    +---------------------------------+
                  |                    | wuwaterm-api (Compose service)  |
                  |                    |  presentation: wuwaterm_api/    |
                  |                    |  + web/ sub-app, OFF by default |
                  |                    +-----------------+---------------+
                  |                                      |
                  v                                      v
      +----------------------------------------------------------------+
      |  application layer   src/wuwaterm/application.py               |
      |  command/API/web dictionary-first pipeline, protocol-neutral   |
      +-----------------------------+----------------------------------+
                                    |
                  +-----------------+--------------------+
                  v                                      v
          data/terms.db (read-only)            OpenAI-compatible LLM
                  ^
                  | promote only via deploy/vps-update.sh
      +------------+---------------------------------------------------+
      |  Builder jobs (Compose profile: builder)                       |
      |  refresh-data -> build-db -> verify-*                          |
      |  never holds runtime secrets (no env_file)                     |
      +----------------------------------------------------------------+
```

Three presentation adapters, one application layer, two consumers:

| Surface | What it is | Where |
|---------|------------|-------|
| Telegram bot | Presentation adapter: commands, chat authorization, chat wording, markup | `src/wuwaterm/bot.py`, `channel.py` |
| HTTP API | Presentation adapter: versioned routes, device authentication, one error envelope, plain text | `src/wuwaterm_api/` |
| Private web layer | Presentation adapter: two owner-facing HTML views, rendered server-side, mounted inside the API process | `src/wuwaterm_api/web/` |
| Desktop client | **Not** an adapter — a consumer of the API's published contract, holding no translation logic | `client/` |
| Public beta Site | **Not** an adapter and **not** `/wuwaterm-web` — a Hosted / Cloudflare Worker BFF that anonymously exposes same-origin `/api/*` and proxies `/v1` with a server-side device token | `site/` |

The desktop client is deliberately outside the service: it renders answers the
API produced, and every rule the API applies is applied whether the caller is
that client or `curl`. See [ADR 0009](adr/0009-http-api-adapter.md) and
[ADR 0011](adr/0011-pc-client-stack.md).

The private web layer is inside the service, and inside the API's own process:
it is mounted as a sub-application at `/wuwaterm-web`, and only when
`WUWATERM_API_WEB_ENABLED` is set — the default is off
(`src/wuwaterm_api/settings.py`, `DEFAULT_WEB_ENABLED = False`), and with the
switch off there is no route, no middleware and no sub-application. It is
owner-private: the reverse proxy authenticates the request and injects the edge
marker `X-Wuwaterm-Edge`, and the layer refuses anything arriving without that
marker, so reaching the loopback port directly gets nothing. It appears in no
published contract — the mount and the bare-mount guard are plain routes, not
API routes, so `docs/api/openapi.json` lists exactly the paths it listed before
the layer existed. Details in [web-presentation-layer.md](web-presentation-layer.md);
the decision, its cost and what it does not cover are in
[ADR 0014](adr/0014-private-web-presentation-layer.md).

How that layer reaches the pipeline is worth being exact about, because it is
the opposite of how the desktop client does: **not over HTTP**. It imports
`translate_request_async` and `lookup_exact_terms` from `wuwaterm.application`
and calls them in process, and `create_web_app` hands it the parent's `State`
OBJECT — the same rate limiter, the same model-call budget, the same credential
pool instance, not copies (`src/wuwaterm_api/web/app.py`). So it is a second
caller of the application layer rather than a second consumer of the published
contract, and the API's aggregate budgets are unchanged by it. It is not an
exemption either: a browser request is still resolved to a device principal and
still passes the same scope check, per-device rate limit and revocation
re-checks the `/v1` routes apply.

The public beta Site is the other consumer, and it is the opposite of the
in-process web layer in every way that matters for trust: it runs outside
this Compose topology, holds `WUWATERM_SITE_DEVICE_TOKEN` on the Worker, and
has **no visitor account or authentication in application code**. It currently
serves anonymous visitors from the public URL documented in [sites.md](sites.md),
under one site-wide first-come quota pool. Details, environment, and the
operator checklist are in [sites.md](sites.md). Do not add Sites setup to
[web-presentation-layer.md](web-presentation-layer.md).

### Users and actors

| Actor | Role | Evidence |
|-------|------|----------|
| Owner (`OWNER_USER_ID`) | Private `/tr`, `/grant` / `/revoke`, `/public`, `/status` | `bot.py` `_is_owner`, `grant_command`, `public_command` |
| Group admins | `/tr` when chat is allowlisted (unless public mode) | `bot.py` `_is_authorized_group_sender`, `ChatSettings` |
| Group members | `/tr` only when public mode is on for that chat | `settings.py` public map; `docs/telegram-behavior.md` |
| Linked channel posts | Auto-translated into the discussion group when gated | `channel.py` `channel_post_handler`; single channel auto-forward listener pin in `bot.py` |
| API device principal | Any registered, unrevoked device: translate and dictionary reads over HTTP | `src/wuwaterm_api/auth.py`, [ADR 0010](adr/0010-device-principal-authentication.md) |
| Owner at a browser | The private web layer's two views (lookup, sentence translation), only when it is switched on and only through the proxy | `src/wuwaterm_api/web/app.py`, [ADR 0014](adr/0014-private-web-presentation-layer.md) |
| Anonymous public beta visitor | Same-origin `/api/*` on the Hosted origin; the Worker then calls `/v1` as one device | `site/`, [sites.md](sites.md) |
| Operator on VPS | Deploy, data refresh, Compose up/down, device issuance and revocation | `deploy/vps-update.sh`, `docs/deployment.md` |

### External systems

| System | Direction | Purpose |
|--------|-----------|---------|
| Telegram Bot API | Outbound long polling + send/edit/delete | Chat presentation only |
| OpenAI-compatible HTTP API | Outbound when LLM configured and dictionary miss | Sentence translation after term lock |
| Owner's desktop client | **Inbound** over HTTPS through the operator's existing ingress | One intended caller of the published `/v1` contract |
| Sites Worker | **Inbound** to `/v1` over HTTPS from the Hosted runtime, using `WUWATERM_SITE_DEVICE_TOKEN` | The other published-contract caller; the browser never talks to `/v1` |
| Owner's browser | **Inbound** over HTTPS to `/wuwaterm-web` through the same ingress, when that layer is switched on | Renders the private web layer's pages; it never calls `/v1` |
| GitHub game-data repos | Builder sparse checkout only | Source for `terms.db` (`data_source.py`, `constants.py` pins) |
| Local Docker Compose host | Runtime (two serving containers) + optional builder jobs | Supported single-host topology |

### Trust boundaries

1. **Telegram update surface is untrusted input.** Command text, replied
   message HTML, and channel posts are treated as data, not instructions
   (`docs/privacy-and-llm.md`; sentence system prompt + HTML protect/restore).
2. **The HTTP surface is untrusted input, and so is anything that has already
   passed TLS.** The API's boundary is the API process, not the proxy in front
   of it: the proxy proves the SERVER's identity to the client and proves
   nothing about the client to the server. Every `/v1` route therefore requires
   a device credential, bodies are size- and time-capped before a handler sees
   them, and the request id is always minted server-side so a caller cannot
   route its own input into the logs or a response
   ([ADR 0012](adr/0012-client-transport-selection.md)).
3. **The public HTTPS edge terminates outside this application.** A path route
   on the operator's existing ingress forwards to the API's loopback port; the
   API itself binds a numeric loopback literal and refuses anything else on the
   only path that binds a socket (`validate_loopback_bind`, `cli._serve`). The
   exposure is therefore removable by deleting one route, with the service
   untouched.
4. **Secrets stay outside the image and the builder.** Serving containers get
   credentials via Compose `env_file`; the builder has no `env_file`
   (`deploy/docker-compose.yml`, `docs/deployment.md`). The API container has
   the bot's chat token, owner id and log-redaction key blanked; the model
   credential is deliberately shared by both surfaces.
5. **Terminology DB is trusted after candidate verification**, then mounted
   read-only at runtime (`/app/data:ro`) by BOTH serving containers. Promotion
   is owner-scripted, not in-process self-update.
6. **Mutable state is local, private and not shared between the surfaces.**
   The bot writes `state/`; the API writes `state-api/`, a SIBLING of it, so
   the bot's read-write mount never covers the credential store. Neither is
   committed (`scripts/check_repo_hygiene.py`).
7. **The chat control plane is still the Bot API only.** Chat updates arrive
   by long polling; nothing here registers a chat-side delivery endpoint
   ([ADR 0003](adr/0003-long-polling-not-webhook.md), unchanged). Operator
   shell access to the host is an administration channel and is never the
   application's path to the service.
8. **The Sites Worker is a half-trusted hop.** It holds a Bearer token that
   can spend the API's per-device rate limit and model budget. The browser
   is locked down by `site/scripts/verify-no-client-secret.mjs`; callers of
   the Worker are not. URL secrecy or a platform ACL is an operational
   choice this repository does not encode ([sites.md](sites.md)).

## Identity model: two separate controls

The system has two authorization mechanisms. They are separate by
construction, and **neither can grant, extend or revoke the other**.

| | Telegram | HTTP API |
|---|---|---|
| Principal | A Telegram user id, and a chat id | A device registered by the operator |
| Owner authority | `OWNER_USER_ID` from the environment | none — there is no owner device, only devices |
| Grant | `/grant` adds a chat to the allowlist; `/public` opens a chat to its members | `wuwaterm-api device issue`, reading the secret from standard input |
| Stored where | `state/chat_settings.json` (bot's writable mount) | `state-api/devices.db` (API's own mount) |
| Stored what | Chat ids and flags | A salted scrypt verifier, scopes, timestamps — never a secret |
| Withdraw | `/revoke` for a chat | `wuwaterm-api device revoke` stamps `revoked_at`; deleting the store file revokes everything |
| Scope model | Per chat, plus owner-only commands | Per device: `translate`, `meta` |
| Failure answer | Silent reject or a chat message | `401` unproven credential, `403` scope not granted, one envelope either way |

Concretely: the owner's Telegram account confers no API access, an API device
cannot use a Telegram command, revoking a device leaves every chat exactly as
it was, and removing a chat from the allowlist leaves every device exactly as
it was. Details in [ADR 0010](adr/0010-device-principal-authentication.md);
the reason the network layer is not a third control is in
[ADR 0012](adr/0012-client-transport-selection.md).

When the private web layer is switched on, a third secret has to be safeguarded
and rotated — the proxy's own credential for `/wuwaterm-web`, and the edge
marker it injects — and it is deliberately **not** a third control in the table
above. Neither confers a principal: together they decide whether a request
reaches the mount at all. What the layer then acts as is an ordinary device
from the store in the right-hand column, so `wuwaterm-api device revoke` turns
that surface off without touching the edge, the desktop client or any chat
([ADR 0014](adr/0014-private-web-presentation-layer.md)).

The public beta Site is a third *credential location*, not a third control in
the table: `WUWATERM_SITE_DEVICE_TOKEN` lives in the Hosted runtime. Revoking
that device stops the Worker the same way it stops any other `/v1` caller.
It does not authenticate the person who opened the Sites URL.

## Cost topology: budgets are per process, and the worst case is their sum

Each serving process has its OWN limiter objects, in its own memory. Nothing
coordinates between them, and nothing in either process can observe the
other's spend.

| Process | Model concurrency | Model calls per minute | Per-caller limit |
|---|---|---|---|
| `wuwaterm-bot` | `WUWATERM_LLM_MAX_CONCURRENCY` (default 4) | `WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE` (default 60) bounds linked-channel work only; commands have no per-minute cap | per-chat request rate limiter |
| `wuwaterm-api` | `WUWATERM_API_LLM_MAX_CONCURRENCY` (default 2) | `WUWATERM_API_LLM_CALLS_PER_MINUTE` (default 30) | per-device sliding-window rate limiter (default 30/min) |

Read that table as follows, because the wording is the point:

- **The worst case for the host and for the model account is the SUM.** With
  the defaults, up to six model calls can be in flight at once — four from the
  bot and two from the API. There is no configuration that makes six into
  four.
- **These counters are per process, never global.** They are in-memory objects
  belonging to one container. A second copy of either process would have its
  own counters and would double the ceiling; a restart resets them.
- **No shared budget exists, deliberately.** Building one means a shared
  durable store, a protocol for reserving and releasing a slot, and a failure
  mode when that store is unavailable — real machinery for a single-owner
  deployment with one model account and hand-sized traffic.

What would make it worth building: a second human user or a second client
machine; an observed breach of the model account's own limits or spend
budget; or any topology with more than one instance of either surface. Until
one of those is real, the honest description is the one above — two
independent budgets whose worst case is their sum.

Sites traffic lands in the API process as one device principal. It shares
that device's sliding window and the API process's model budget with every
other caller of the same token. A dedicated Sites device keeps the
public Site from starving the desktop client; sharing one token does the
opposite. The bot process is still a separate summand.

The API's other limits are its own too, and none of them are model budgets:
a per-request time budget, a request body cap enforced by streaming rather
than by the declared length, and a bounded credential-verification pool with
non-queuing admission (a saturated verifier sheds `429` rather than queueing
work an unauthenticated caller can schedule).

## Telegram as presentation layer

Telegram is one UI and transport, not the domain model.

| Presentation | Responsibility | Module |
|--------------|----------------|--------|
| Command bot | Handlers, auth, rate limits, replies, polling bootstrap | `src/wuwaterm/bot.py` |
| Linked channel | Admission, translate, deliver/edit, flood retry | `src/wuwaterm/channel.py` |
| HTML/text helpers | Protect/validate Telegram HTML; chunk limits | `telegram_html.py`, `telegram_text.py` |

Domain (`lookup`, `normalize`, `models`, `review`, term-lock policy in `sentence`) and
storage (`db` reads, `settings`, `channel_reply_index`) must remain usable from
CLI and tests without a live Telegram session. See [ADR 0001](adr/0001-telegram-as-presentation-layer.md).

## HTTP API as presentation layer

The API adapter is the same kind of thing on the other side of the
application layer: routes, authentication, limits and rendering, with no
translation logic of its own.

| Presentation | Responsibility | Module |
|--------------|----------------|--------|
| Routes and models | `/v1/translations`, `/v1/terms`, `/v1/meta`, `/v1/reviews`, `/healthz`, `/readyz` | `src/wuwaterm_api/app.py` |
| Middleware | Server-minted request id, body size/arrival cap, request time budget | `src/wuwaterm_api/app.py` |
| Identity | Device store, scopes, revocation, credential pool | `src/wuwaterm_api/auth.py` |
| Error vocabulary | Enumerated codes and their HTTP statuses | `src/wuwaterm_api/errors.py` |
| Operator entry | `serve`, and `device issue` / `list` / `revoke` | `src/wuwaterm_api/cli.py` |

The contract is published at `docs/api/openapi.json` and drift-gated by
`scripts/check_api_contract.py`. Answers are plain text: the adapter injects
no markup translator, so chat markup cannot reach this contract even by
accident.

## Component map and dependency direction

Intended layers (modular monolith — [ADR 0002](adr/0002-modular-monolith.md)):

```
cli (bootstrap)                    wuwaterm_api (separate top-level package)
  |-> bot / channel                  |-> app / auth / errors / settings / cli / web
  |     |-> application  <-----------'        (allowlisted imports only)
  |     |     |-> lookup / review / sentence / normalize / translation_policy   domain
  |     |-> settings / channel_reply_* / channel_runtime         local infra
  |     |-> telegram_html / telegram_text                 presentation helpers
  |-> builder / data_source / db (write) / build_pinyin          builder path
  |-> lookup / sentence / db (read)                              offline CLI
```

| Layer | Modules | Must not import |
|-------|---------|-----------------|
| Domain core | `lookup`, `normalize`, `models`, `review`, `cjk_span` | presentation / Telegram SDK (including under `TYPE_CHECKING`); builder-only modules; `sentence` |
| Domain + provider | `sentence` | `bot`, `channel` (may use `telegram_html` for HTML term-lock); builder-only modules |
| Application | `application` | presentation / Telegram SDK (including under `TYPE_CHECKING`); builder-only modules |
| Shared policy | `translation_policy`, `runtime_keys`, `constants`, `exclusion_metadata` | presentation / Telegram SDK (including under `TYPE_CHECKING`); builder-only modules |
| Presentation (chat) | `bot`, `channel`, `telegram_html`, `telegram_text` | `builder`, `data_source`, `build_pinyin`, bootstrap `cli` |
| Presentation (HTTP) | `wuwaterm_api.*` | everything in `wuwaterm` except the four allowlisted modules below; the Telegram SDK |
| Local state | `settings`, `channel_reply_index`, `channel_reply_schema`, `channel_runtime` | presentation / Telegram SDK (including under `TYPE_CHECKING`); builder-only modules |
| Storage | `db` | presentation / Telegram SDK; `build_pinyin` only inside write helpers (lazy) |
| Builder | `builder`, `data_source`, `build_pinyin` | `bot`, `channel` |
| Bootstrap | `cli` | may wire both runtime and builder entrypoints |

Strict parsers for the builder's versioned free-text lock exclusion
metadata live in `exclusion_metadata` — standard-library modules plus
`constants` only. Sentence locking and the review engine's
automatic-assessment policy both consume that same corpus evidence while
keeping their own policies separate.

**The API import allowlist.** `src/wuwaterm_api` may import exactly four
modules from `wuwaterm`: `application`, `models`, `translation_policy`,
`logging_utils`. Anything else — `bot`, `channel`, `lookup`, `sentence`, `db`,
the markup helpers, the builder modules, the `wuwaterm` package root — fails
`scripts/check_architecture_boundaries.py`. That list is the machine-checkable
form of "the API cannot bypass the shared pipeline": an adapter that could
reach `lookup` or `sentence` directly could grow a second, divergent pipeline
without anyone noticing, which is exactly what a second surface is most likely
to do.

The application layer holds the shared command/API/web pipeline (prepare →
direction → exact hit → trusted fuzzy hit → length gate → chunked, term-locked
LLM call) and knows nothing about Telegram or HTTP. Its adapter-shaped steps
are injected by the caller: Telegram commands inject a markup translator and
UTF-16-aware splitter; the API injects neither, which is why its answers are
plain text by construction. Those callers receive a `TranslationOutcome` and
own their wording. Linked-channel posts deliberately do not consume that
outcome: `channel.py` owns channel admission, freshness, edits, delivery and
its exact-first path while sharing protocol-neutral term and sentence
primitives. This is a contract distinction, not a request to merge the flows.

Observed edges (static imports among shipped modules):

- `bot` → `channel` (handler registration + flood retry); `channel` → `bot`
  only under `TYPE_CHECKING` for `BotConfig` (no runtime cycle).
- `bot` → `application` (shared pipeline + rate limiter); `application` imports
  no presentation module and no Telegram SDK, enforced by the boundary guard.
- `wuwaterm_api` → `wuwaterm.application` (pipeline, translator factory,
  lookup entry points, error codes) from `app.py`, `errors.py`, `web/app.py`
  and `web/render.py`; `wuwaterm.logging_utils` (`redact_id`) from `app.py`;
  and `wuwaterm.translation_policy` (`LLM_INPUT_CHAR_LIMIT`) from `web/app.py`,
  which imports the shared input ceiling rather than restating it. Those three
  modules are the whole of what the package imports from `wuwaterm` today; the
  guard permits one more (`models`) and nothing beyond the four.
- `sentence` → `lookup`, `normalize`, `telegram_html`, `httpx`.
- `lookup` → `db` (read helpers), `models`, `normalize`, `constants`.
- `db.insert_records` lazily imports `build_pinyin` (builder-only dependency).
- Runtime image refuses `build-db` and excludes builder tooling, while
  serving BOTH surfaces from the same image
  (`tests/test_runtime_imports.py`, CI `deploy-boundary` job).

Automated enforcement: `scripts/check_architecture_boundaries.py` plus
`scripts/check_non_goals.py`, `scripts/check_api_contract.py` and
`tests/test_runtime_imports.py`.

### Current coupling (honest)

| Coupling | Status | Notes |
|----------|--------|-------|
| Large `bot.py` (config, auth, rate limit, translate orchestration, migration, polling) | Accepted concentration | Documented; not mass-split in this formalization |
| `translate_query*` in `bot.py` are thin wrappers over `application` | Resolved | The pipeline itself lives in `application.py`; `bot.py` only adds Telegram wording, HTML parse mode and the UTF-16 splitter |
| `channel` TYPE_CHECKING-imports `BotConfig` from `bot` | Low aesthetic cycle | Runtime import graph is one-way; extraction deferred unless a check requires it |
| `sentence` → `telegram_html` | Accepted | HTML term-lock is part of Telegram-HTML translation fidelity |
| `db` top-level imports `data_source.SourceProvenance` | Mild storage/builder type coupling | Provenance metadata is shared with build path |
| Both serving processes share one model credential | Deliberate | A second key would carry the same power and double what must be rotated; the isolation that matters (chat identity, owner id, redaction key) is enforced per container |

## Request flows

### Command `/tr` (and `/term`, `/sentence` / `/sent`)

Evidence: `bot.py` `create_application`, `_translation_command`,
`translate_request_async` / `translate_query_async`; tests under `tests/test_bot.py`.

1. `run_bot` → `create_application` → `app.run_polling()` ([ADR 0003](adr/0003-long-polling-not-webhook.md)).
2. `CommandHandler` for `tr`/`term` or `sentence`/`sent` → `_translation_command`.
3. **Auth**: owner private chat, or group admin (or public mode) + allowlist
   (`_translation_actor_or_reject`, `ChatSettings`).
4. **Rate limit**: per-chat `PerChatRateLimiter`.
5. Parse args / optional `--to` / replied text; invalid `--to` → usage, **no LLM**.
6. `prepare_text` → `lookup_exact`:
   - exact hit → official string, **no LLM**;
   - short ASCII fuzzy dictionary short-circuit → **no LLM** (`_fuzzy_dictionary_answer`).
7. Length gate (`translation_policy` / config limits).
8. `SentenceTranslator`: lock DB terms → optional LLM → restore placeholders.
9. `reply_to_user` (HTML with plain fallback; flood retry via channel helper).

### `POST /v1/translations`

Evidence: `src/wuwaterm_api/app.py` `create_translation`,
`authenticated_device`, `_require_active_device`; `tests/test_api.py`.

1. Request id minted server-side; an inbound `X-Request-Id` is ignored.
2. Body read incrementally against the size cap and its own arrival deadline;
   the whole request runs under the request time budget.
3. **Admission**: non-queuing slot acquire (full → `429`), credential verified
   on the dedicated credential pool, per-device rate limit, then `record_use`
   — whose row count is itself the check that the device is still live.
4. **Scope**: `translate` required, else `403`.
5. Re-check the device before the model call, so a revocation since admission
   spends no budget.
6. The shared pipeline runs: exact hit → fuzzy hit → length gate → term-locked
   model call, with the dictionary stage offloaded to a worker thread.
7. With no model configured, this surface refuses (`llm_unavailable`) rather
   than returning source text as though it had been translated — the chat
   fallback would be misleading over HTTP.
8. Re-check the device again before returning; the work is already paid for
   here, so a transient store error serves the answer while a definitive
   revocation still refuses.
9. Render `kind`, `text`, `direction`, `dictionary_miss`, `request_id` — plain
   text, no markup.

### Linked-channel auto-translation

Evidence: `channel.py` `channel_post_handler`; single listener pin in
`scripts/check_non_goals.py` / `bot.py`.

1. Update matches `filters.IS_AUTOMATIC_FORWARD & filters.SenderChat.CHANNEL`.
2. Age / size / CJK-Latin thresholds; kill switch `WUWATERM_CHANNEL_AUTOTRANSLATE`.
3. Auth: discussion group must still satisfy allowlist gate.
4. `ChannelRuntime.reserve` (process-local admission / budget).
5. Claim original in `ChannelReplyIndex` (file-backed).
6. Translate: exact DB / term lock / LLM with re-gates before send.
7. Deliver reply or edit existing chunks with `send_with_flood_retry`.
8. Telemetry outcomes via channel runtime counters.

Channel path is always auto-detected direction; it does not accept command
`--to` flags (`docs/telegram-behavior.md`).

### Paths that never call the LLM

| Path | Why |
|------|-----|
| Exact dictionary hit (`lookup_exact` + early returns in the shared pipeline, channel exact branch) | Official string from SQLite |
| Fuzzy dictionary short answers (`_fuzzy_dictionary_answer`) | DB-only |
| `GET /v1/terms`, `GET /v1/meta`, `POST /v1/reviews`, `/healthz`, `/readyz` | Dictionary reads, pair review, and probes only |
| Invalid leading `--to` | Usage reply only |
| Unauthorized / rate-limited / silent reject (either surface) | No translation work |
| Channel below CJK/Latin thresholds, stale posts, admission reject, kill switch off | Skipped before translate |
| LLM env incomplete | The bot restores locked placeholders without inventing terms; the API refuses with `llm_unavailable` |
| `/about`, `/status`, grant/revoke/public membership housekeeping | No translation |

## Data model: immutable vs mutable

| Kind | Location | Mutability | Notes |
|------|----------|------------|-------|
| Terminology SQLite | `data/terms.db` (Compose: `/app/data` **ro** in both serving containers) | Immutable at runtime | Built offline; promoted by `deploy/vps-update.sh` ([ADR 0004](adr/0004-sqlite-terminology-data.md), [ADR 0008](adr/0008-candidate-verification-and-transactional-deployment.md)) |
| Chat allowlist + public mode | `state/chat_settings.json` | Mutable | Process `RLock` + `fcntl`/`msvcrt` file lock ([ADR 0005](adr/0005-file-backed-single-instance-state.md)) |
| Channel reply index | `state/channel_replies.json` | Mutable | Atomic replace + asyncio edit locks |
| Device credential store | `state-api/devices.db` | Mutable, operator-written | Sibling of `state/`; created by the operator path only, never by a request ([ADR 0010](adr/0010-device-principal-authentication.md)) |
| Channel admission / budgets | `ChannelRuntime` in process memory | Mutable, lost on restart | Not shared across processes |
| Rate limiters, model budgets, credential pool | Process memory, per serving process | Mutable, lost on restart | Never shared between bot and API |
| Source pins / profile | `constants.py` + DB metadata | Immutable until rebuild | |
| Secrets | Host `.env` mode `600` | Operator-managed | Not in image; not in builder; narrowed per container |
| Client base address | `%APPDATA%/WuwaTerm/config.json` on the owner's PC | Mutable, non-secret | The device token is never written here — it lives in the OS credential store |

## Single-instance assumptions

Supported topology (also [supported vs unsupported](#supported-vs-unsupported-topology)):

- One Bot token (`TELEGRAM_BOT_TOKEN`).
- One active long-polling runtime container (`container_name: wuwaterm-bot`).
- One API container (`container_name: wuwaterm-api`) bound to loopback.
- One Compose host; `network_mode: host`.
- Builder is profile-gated one-shot work, not a second always-on replica.

### Dual-instance failure modes (unsupported)

If two bot processes share the same token and state files:

1. **getUpdates contention** — Telegram effectively serves one consumer;
   offsets race and updates are lost or double-handled.
2. **Allowlist / public split-brain** — file locks reduce corrupt JSON writes
   but not logical divergence between two writers with interleaved intent.
3. **Double channel work** — `ChannelRuntime` budgets and in-memory dedup are
   process-local → duplicate LLM spend and duplicate replies.
4. **Reply-index races** — claim/edit windows can conflict across processes even
   when individual file replaces are atomic.

If two API processes share the same credential store and port:

5. **Doubled budgets** — model concurrency, per-minute caps, per-device rate
   limits and the credential pool are per process, so two instances double
   every ceiling this document states.
6. **Port ownership** — both bind the same loopback port; the second fails to
   start, or an operator "fixes" it by binding something else, which is the
   one thing the bind guard exists to prevent.

Docs and ops must not treat multi-instance as HA. File locks are durability
aids for a single writer, not a cluster protocol.

## Failure propagation

| Dependency | Failure mode | User-visible / recovery |
|------------|--------------|-------------------------|
| Telegram API | NetworkError, RetryAfter, BadRequest | Flood retry helper; HTML parse fallback to plain; missing reply target handling; process keeps polling (`restart: unless-stopped`) |
| LLM HTTP | Timeout, 4xx/5xx, empty content | Bot: `LLMTranslationError` mapped to a safe user message. API: enumerated code (`llm_unavailable`, `llm_budget_exhausted`) in the standard envelope |
| SQLite terms DB | Missing/corrupt file | Startup/use fails when `TermService` cannot read; `/readyz` answers `503`; deploy keeps previous DB on failed promotion |
| Credential store | Missing, deleted, locked, corrupt | `503` (store unusable), never `401` — a valid device is never told to re-pair because of a database hiccup |
| Filesystem state | Disk full, lock errors, durability errors | Settings/reply-index raise typed errors; startup migration refuses overwrite of existing targets |
| Ingress route | Proxy down, route removed | The client renders an offline or timeout state; the service keeps running on loopback and the bot is unaffected |
| Docker / Compose | Image/build failure, wrong revision | `vps-update.sh` aborts before stop, or rolls back image + DB + pointer after failed post-promote steps, for both serving containers |

No automatic cross-region failover. Recovery is restart, restore from
timestamped backups under `data/deployment-backups`, or re-run transactional
deploy after fixing the root cause.

## Data refresh and transactional deploy

From repo scripts and docs only (no live VPS mutation required to understand):

1. **Refresh** — `wuwaterm-builder refresh-data` sparse-checkouts pinned source
   (`data_source.refresh_data`, `docs/data-refresh.md`).
2. **Build candidate** — `build-db --atomic` → unique path under
   `data/candidates/` (updater), not live `terms.db`.
3. **Verify** — `verify-db`, seed/exact/idempotent scripts as wired in
   `deploy/vps-update.sh` and `docs/validation.md`.
4. **Build immutable runtime image** labeled with source commit revision.
5. **Promote** — stop both serving containers, swap DB, start the exact image,
   smoke (`scripts/deploy_smoke.py` with diagnostic send disabled in updater
   path, plus an in-container readiness probe for the API), write immutable
   `.deployments/<commit>.json`, atomic `.deploy_commit`.
6. **Rollback** — on post-promote failure restore previous DB, rollback image
   tag, and pointer (`deploy/vps-update.sh` `rollback_on_failure`), restarting
   only the surfaces that were running before.

Runtime vs builder separation: [ADR 0007](adr/0007-runtime-builder-separation.md).
Dictionary-before-LLM: [ADR 0006](adr/0006-dictionary-first-before-llm.md).

## Conditions that would justify later extensions

These are **not** current capabilities. They would need a new goal, ADRs, and
likely product/ops changes:

| Extension | Real trigger (examples) |
|-----------|-------------------------|
| Private overlay network instead of the public route | Evidence of unwanted traffic on the route; a decision that the surface should not be public; a second machine that also needs access ([ADR 0012](adr/0012-client-transport-selection.md)) |
| Multi-user / a principals table | A second human user, or per-user quotas. Today the device id IS the principal id |
| Shared cross-process LLM budget | A second client machine, an observed breach of the model account's limits, or more than one instance of either surface — see [cost topology](#cost-topology-budgets-are-per-process-and-the-worst-case-is-their-sum) |
| Web admin console | Operators need bulk allowlist/audit without Telegram commands; multi-owner RBAC. The private web layer that now exists is **not** this and does not satisfy this row: it is single-owner, it has two views — dictionary lookup and sentence translation — and it holds no administration capability at all, so it cannot authorize a chat, issue or list a device, or read an audit trail |
| Multi-instance / external state | Sustained load exceeds one process **and** the delivery model is redesigned; shared durable admission for channels |
| Postgres / Redis / queue | State or job volume exceeds single-host JSON/SQLite operational comfort; multi-host deploy becomes a requirement |
| Public or third-party API clients | Anyone but the owner needs access, which changes rate limits, abuse handling and the identity model together |

Until those triggers are real, adding the machinery would be ceremony.

## Supported vs unsupported topology

| Supported | Unsupported |
|-----------|-------------|
| Single VPS, Compose, bot long polling | Chat delivery endpoints, multi-replica polling, HA |
| One API container bound to loopback, published by a path route on the operator's existing HTTPS ingress | The API binding a public interface, a second open port, or answering `/v1` without a device credential |
| The private web layer mounted inside that same API process, off unless switched on, published by a second path route behind the proxy's own authentication and the injected edge marker | A separate web process or container for it; the mount answering anything that did not arrive through the edge; any administration capability on that surface |
| Single bot token, one active runtime | Multi-token shard, active-active |
| `data/terms.db` RO in both serving containers + `state/`, `state-api/` RW to their own owners | Runtime writing game TextMaps or self-promoting DB; either surface writing the other's state |
| One owner, devices registered by the operator | Multi-tenant accounts, self-service registration, public sign-up |
| Dictionary-first + optional LLM, plain text over HTTP | LLM-only glossary, secondary name-map layer, chat markup in the HTTP contract |
| Owner-gated deploy from clean `HEAD == origin/main` | Unverified candidate copy over live DB |

## Related documents

- [Deployment](deployment.md)
- [The private web presentation layer](web-presentation-layer.md)
- [Telegram Behavior](telegram-behavior.md)
- [Privacy And LLM](privacy-and-llm.md)
- [Data Refresh](data-refresh.md)
- [Validation](validation.md)
- [ADR index](adr/README.md)
