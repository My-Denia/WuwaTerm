# Architecture

Maintainer entry for the **current** system: Core provides the protocol-neutral
application pipeline and its HTTP API adapter (`wuwaterm-api`), plus offline
dictionary/knowledge-base builders. Independent products consume Core:
[WuwaTerm-Telegram](https://github.com/My-Denia/WuwaTerm-Telegram) (standalone
Telegram Bot wheel), [WuwaTerm-Windows](https://github.com/My-Denia/WuwaTerm-Windows)
(desktop client), and [WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web)
(standalone Web workbench and anonymous public beta Site).
Claims map to source, tests, deploy scripts, and Compose topology. This is not an
aspirational redesign.

Operational detail lives in sibling guides. Decision rationale lives in
[ADRs](adr/). Automated import-direction checks run under `scripts/` and CI.

## System context

```
Desktop client / WuwaTerm-Web / standalone WuwaTerm-Telegram (optional external consumers)
                                       |
                                       | https, cert verified (or local core wheel)
                                       v
                     +---------------------------------+
                     | reverse proxy already serving   |
                     | the operator's existing sites   |
                     +-----------------+---------------+
                                       | 127.0.0.1 (loopback)
                                       v
                     +---------------------------------+
                     | wuwaterm-api (Compose service)  |
                     |  presentation: wuwaterm_api/    |
                     +-----------------+---------------+
                                       |
                                       v
      +----------------------------------------------------------------+
      |  application layer   src/wuwaterm/application.py               |
      |  API / CLI / library dictionary-first pipeline, protocol-neutral|
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

One in-repo presentation adapter (`wuwaterm_api`), one protocol-neutral
application layer, and three independent external products:

| Surface | What it is | Where |
|---------|------------|-------|
| HTTP API | Presentation adapter: versioned routes, device authentication, one error envelope, plain text | `src/wuwaterm_api/` |
| Telegram bot (standalone) | Independent presentation product consuming Core's protocol-neutral pipeline and `ProtectedText` (legacy inline `bot.py`/`channel.py` retired from Core after `v0.16.0`) | [WuwaTerm-Telegram](https://github.com/My-Denia/WuwaTerm-Telegram) |
| Desktop client | **Not** an adapter — a consumer of the API's published contract, holding no translation logic | [WuwaTerm-Windows](https://github.com/My-Denia/WuwaTerm-Windows) |
| Web workbench & Public beta Site | **Not** an in-process sub-app — standalone Web workbench and Hosted / Cloudflare Worker BFF consuming `/v1` with a device token (legacy in-process `src/wuwaterm_api/web/` retired from Core after `v0.16.0`) | [WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web) |

Desktop source moved to the independent Windows product after its v0.3.0
release. The original desktop design made dictionary versus model cost and
answer provenance clear, used Chinese and themes, and kept errors inline; the
independent product preserves that work while owning its build and release.
See the [original Core source](https://github.com/My-Denia/WuwaTerm/tree/cc5743df0e807c42e4b669564fd8b4d0a465b79c/client) and the
[Windows migration provenance](https://github.com/My-Denia/WuwaTerm-Windows/blob/cda93f01bf7803efe66ca695701cae678b320ef1/provenance/client-source.json). Core retains the API and checks its producer
contract against a pinned Windows consumer.

The desktop client is deliberately outside the service: it renders answers the
API produced, and every rule the API applies is applied whether the caller is
that client or `curl`. See [ADR 0009](adr/0009-http-api-adapter.md) and
[ADR 0011](adr/0011-pc-client-stack.md).

The legacy in-process private web layer (`src/wuwaterm_api/web/`, previously
mounted at `/wuwaterm-web` behind `WUWATERM_API_WEB_ENABLED`) has been retired
from WuwaTerm Core after release `v0.16.0`. Leaving `WUWATERM_API_WEB_ENABLED`
unset, empty, or falsy (`0`, `false`, `no`, `off`) is silently ignored for
existing `.env` compatibility, while any truthy or unrecognised value fails
closed on `wuwaterm-api serve` and `create_app()` with migration guidance
pointing to [WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web) (and
rollback release `v0.16.0`). See [web-presentation-layer.md](web-presentation-layer.md)
and historical [ADR 0014](adr/0014-private-web-presentation-layer.md).

The public beta Site and standalone Web workbench live in
[WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web): they run outside the
Core API process and call `/v1` over HTTP using a provisioned Core device
credential. The public beta Site holds `WUWATERM_SITE_DEVICE_TOKEN` on the
Worker and has **no visitor account or authentication in application code**. It
currently serves anonymous visitors from the public URL documented in
[sites.md](sites.md), under one site-wide first-come quota pool. Details,
environment, and the operator checklist are in [sites.md](sites.md). Do not add
Sites setup to [web-presentation-layer.md](web-presentation-layer.md).

### Users and actors

| Actor | Role | Evidence |
|-------|------|----------|
| Telegram users / admins / linked channels | Served by standalone [WuwaTerm-Telegram](https://github.com/My-Denia/WuwaTerm-Telegram) consuming Core's library and `ProtectedText` (legacy inline Core bot retired after `v0.16.0`) | [docs/telegram-behavior.md](telegram-behavior.md) |
| API device principal | Any registered, unrevoked device: translate and dictionary reads over HTTP | `src/wuwaterm_api/auth.py`, [ADR 0010](adr/0010-device-principal-authentication.md) |
| Owner at a browser | Standalone Web workbench calling `/v1` with a device credential (legacy in-process `/wuwaterm-web` retired after `v0.16.0`) | [WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web), [docs/web-presentation-layer.md](web-presentation-layer.md) |
| Anonymous public beta visitor | Same-origin `/api/*` on the Hosted origin; the Worker then calls `/v1` as one device | [WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web), [sites.md](sites.md) |
| Operator on VPS | Deploy, data refresh, Compose up/down, device issuance and revocation | `deploy/vps-update.sh`, `docs/deployment.md` |

### External systems

| System | Direction | Purpose |
|--------|-----------|---------|
| OpenAI-compatible HTTP API | Outbound when LLM configured and dictionary miss | Sentence translation with dictionary terms as contextual references |
| Owner's desktop client | **Inbound** over HTTPS through the operator's existing ingress | Published `/v1` contract caller ([WuwaTerm-Windows](https://github.com/My-Denia/WuwaTerm-Windows)) |
| Standalone Web workbench / Sites Worker | **Inbound** to `/v1` over HTTPS using a Core device token | Published `/v1` contract caller ([WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web)); browsers never talk directly to `/v1` on the public beta Site |
| GitHub game-data repos | Builder sparse checkout only | Source for `terms.db` (`data_source.py`, `constants.py` pins) |
| Local Docker Compose host | Runtime (one Core API container, `wuwaterm-api`) + optional builder jobs | Supported single-host topology |

### Trust boundaries

1. **External chat and markup surfaces are untrusted input.** Standalone chat
   adapters (such as [WuwaTerm-Telegram](https://github.com/My-Denia/WuwaTerm-Telegram))
   pass untrusted text through Core's `ProtectedText` boundary so structural
   tokens and prose are treated as data, not instructions
   (`docs/privacy-and-llm.md`; sentence system prompt + protected-text
   protect/restore).
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
4. **Secrets stay outside the image and the builder.** `wuwaterm-api` gets its
   credentials via Compose `env_file`; the builder has no `env_file`
   (`deploy/docker-compose.yml`, `docs/deployment.md`). As defense in depth for
   upgraded `.env` files, `wuwaterm-api` blanks `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_TEST_CHAT_ID`, `OWNER_USER_ID`, `WUWATERM_REDACTION_SECRET`, and
   `WUWATERM_API_DEVICE_DB_PATH`.
5. **Terminology DB is trusted after candidate verification**, then mounted
   read-only at runtime (`/app/data:ro`) by `wuwaterm-api` (and by standalone
   `WuwaTerm-Telegram` when deployed). Promotion is owner-scripted, not
   in-process self-update.
6. **Mutable state is local, private and isolated per surface.** Core API
   writes `state-api/` (a sibling of any legacy or standalone Telegram `state/`
   directory, never a child). Neither is committed
   (`scripts/check_repo_hygiene.py`).
7. **Chat control plane belongs to standalone `WuwaTerm-Telegram`.** Core
   registers no Telegram polling loop or webhook endpoint (legacy inline bot
   retired after `v0.16.0`; standalone `WuwaTerm-Telegram` keeps long polling
   per [ADR 0003](adr/0003-long-polling-not-webhook.md)). Operator shell access
   to the host is an administration channel and is never the application's path
   to the service.
8. **The Sites Worker is a half-trusted hop.** It holds a Bearer token that
   can spend the API's per-device rate limit and model budget. The browser
   is locked down by [the Web client-secret check](https://github.com/My-Denia/WuwaTerm-Web/blob/main/scripts/verify-no-client-secret.mjs); callers of
   the Worker are not. URL secrecy or a platform ACL is an operational
   choice this repository does not encode ([sites.md](sites.md)).

## Identity model: separate controls across Core API and standalone products

Core's HTTP API authenticates device principals; standalone
[WuwaTerm-Telegram](https://github.com/My-Denia/WuwaTerm-Telegram) owns its
own separate Telegram chat authorization (legacy inline bot retired from Core
after `v0.16.0`). They are separate by construction, and **neither can grant,
extend or revoke the other**.

| | Standalone `WuwaTerm-Telegram` (external product) | Core HTTP API (`wuwaterm-api`) |
|---|---|---|
| Principal | A Telegram user id, and a chat id | A device registered by the operator |
| Owner authority | `OWNER_USER_ID` in the Telegram product's environment | none — there is no owner device, only devices |
| Grant | `/grant` adds a chat to the allowlist; `/public` opens a chat to its members | `wuwaterm-api device issue`, reading the secret from standard input |
| Stored where | Telegram product's own `state/chat_settings.json` | `state-api/devices.db` (API's own mount) |
| Stored what | Chat ids and flags | A salted scrypt verifier, scopes, timestamps — never a secret |
| Withdraw | `/revoke` for a chat | `wuwaterm-api device revoke` stamps `revoked_at`; deleting the store file revokes everything |
| Scope model | Per chat, plus owner-only commands | Per device: `translate`, `meta` |
| Failure answer | Silent reject or a chat message | `401` unproven credential, `403` scope not granted, one envelope either way |

Concretely: a Telegram account confers no API access, an API device cannot use
a Telegram command, revoking a device leaves standalone Telegram allowlists
untouched, and removing a chat from a Telegram allowlist leaves every API
device untouched. Details in [ADR 0010](adr/0010-device-principal-authentication.md);
the reason the network layer is not a third control is in
[ADR 0012](adr/0012-client-transport-selection.md).

Standalone [WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web) (which
replaces the retired in-process `/wuwaterm-web` layer after `v0.16.0`) runs in
its own container behind the operator's proxy auth and `X-WuwaTerm-Edge` marker
and calls `/v1` as an ordinary device from the right-hand column, so
`wuwaterm-api device revoke` turns that surface off without touching the edge,
the desktop client, or standalone Telegram.

The public beta Site is another *credential location*, not a separate control
in the table: `WUWATERM_SITE_DEVICE_TOKEN` lives in the Hosted runtime.
Revoking that device stops the Worker the same way it stops any other `/v1`
caller. It does not authenticate the person who opened the Sites URL.

## Cost topology: budgets are per process, and the worst case is their sum

Each runtime process has its OWN limiter objects, in its own memory. Nothing
coordinates across processes, and nothing in `wuwaterm-api` can observe an
external product's spend.

| Process | Model concurrency | Model calls per minute | Per-caller limit |
|---|---|---|---|
| `wuwaterm-api` (Core) | `WUWATERM_API_LLM_MAX_CONCURRENCY` (default 2) | `WUWATERM_API_LLM_CALLS_PER_MINUTE` (default 30) | per-device sliding-window rate limiter (default 30/min) |
| Standalone `WuwaTerm-Telegram` (if deployed separately) | configured in its own container (`WUWATERM_LLM_MAX_CONCURRENCY`) | independent per-process channel/command limits in that product | per-chat request rate limiter in that product |

Read that table as follows, because the wording is the point:

- **The worst case for the host and for the model account is the SUM.** If
  standalone `WuwaTerm-Telegram` shares the same upstream LLM account with
  `wuwaterm-api`, concurrent calls from both processes add up. There is no
  configuration that merges separate processes into one shared ceiling.
- **These counters are per process, never global.** They are in-memory objects
  belonging to one container. A second copy of `wuwaterm-api` would have its
  own counters and would double the ceiling; a restart resets them.
- **No shared budget exists, deliberately.** Building one means a shared
  durable store, a protocol for reserving and releasing a slot, and a failure
  mode when that store is unavailable — real machinery for a single-owner
  deployment with one model account and hand-sized traffic.

What would make it worth building: a second human user or a second client
machine; an observed breach of the model account's own limits or spend
budget; or any topology with more than one instance of the API. Until one of
those is real, the honest description is per-process budgets whose worst case
across multiple processes is their sum.

Sites and standalone Web workbench traffic lands in the API process as device
principals. Each shares its device's sliding window and the API process's model
budget with every other caller of the same token. A dedicated Sites device
keeps the public Site from starving the desktop client; sharing one token does
the opposite. Any standalone `WuwaTerm-Telegram` process sharing the same
upstream model key remains a separate summand.

The API's other limits are its own too, and none of them are model budgets:
a per-request time budget, a request body cap enforced by streaming rather
than by the declared length, and a bounded credential-verification pool with
non-queuing admission (a saturated verifier sheds `429` rather than queueing
work an unauthenticated caller can schedule).

## Telegram as presentation layer (Retired from Core)

Telegram is a separate presentation product ([My-Denia/WuwaTerm-Telegram](https://github.com/My-Denia/WuwaTerm-Telegram)),
not part of the Core domain model. The legacy inline bot modules (`bot.py`,
`channel.py`, `channel_reply_index.py`, `channel_reply_schema.py`,
`channel_runtime.py`, `settings.py`, `runtime_keys.py`, `telegram_html.py`,
`telegram_text.py`) and `wuwaterm[telegram]` extra have been retired from Core
(preserved through release `v0.16.0`). Protocol-neutral structure protection
(`protected_text.py`) and normalization (`normalize.py`) remain in Core. See
[ADR 0001](adr/0001-telegram-as-presentation-layer.md).

## HTTP API as presentation layer

The API adapter is the same kind of thing on the other side of the
application layer: routes, authentication, limits and rendering, with no
translation logic of its own.

| Presentation | Responsibility | Module |
|--------------|----------------|--------|
| Routes and models | `/v1/translations`, `/v1/terms`, `/v1/meta`, `/v1/dictionary`, `/v1/capabilities`, `/v1/knowledge/query`, `/v1/reviews`, `/v1/semantic-reviews`, `/v1/structure-checks`, `/v1/auth/status`, `/healthz`, `/readyz` | `src/wuwaterm_api/app.py` |
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
  |                                  |-> app / auth / errors / settings / cli
  |                                  |     `-> application (allowlisted imports only)
  |                                  |           `-> lookup / review / sentence / normalize / translation_policy   domain
  |                                  |                             |-> term_preparation -> term_matching
  |                                  |                             `-> term_matching
  |-> builder / data_source / db (write) / build_pinyin            builder path
  |     `-> term_matching                                          pure matching
  |-> lookup / sentence / db (read)                                offline CLI

knowledge_builder (offline KB build)   scripts/verify_knowledge_db.py
  `-> knowledge_db (read-only KB) <----'
        |-> entity_graph (read-only 1-hop graph)
        `-> domain_knowledge (pure matching, lock_eligible=False)
knowledge_runtime (thread-safe revision-checked runtime + query)
  |-> knowledge_db / lookup / domain_knowledge
  `<- sentence / application  (HTTP adapter reaches it only via application)
scripts/build_knowledge_vectors.py
  `-> retrieval_vector (optional dense vector backend, fail-closed)
        `-> domain_knowledge (pure matching, lock_eligible=False)
```

| Layer | Modules | Must not import |
|-------|---------|-----------------|
| Domain core | `attribution_candidates`, `domain_knowledge`, `entity_graph`, `fuzzy_index`, `knowledge_db`, `knowledge_runtime`, `lookup`, `native_kernel`, `normalize`, `models`, `retrieval_vector`, `review`, `cjk_span`, `document_anchor`, `english_plural`, `protected_text`, `segmentation`, `semantic_review_contract`, `single_char_context`, `skill_references`, `structural_checks`, `term_classification`, `term_matching`, `term_preparation`, `termbase`, `text_spans` | presentation / Telegram SDK (including under `TYPE_CHECKING`); builder-only modules; `sentence` |
| Pure matching (subset of Domain core) | `attribution_candidates`, `domain_knowledge`, `fuzzy_index`, `native_kernel`, `term_matching`, `term_preparation`, `cjk_span`, `document_anchor`, `protected_text`, `segmentation`, `english_plural`, `single_char_context`, `skill_references`, `structural_checks`, `term_classification`, `termbase`, `text_spans` | `db`, `entity_graph`, `knowledge_db`, `knowledge_runtime`, `lookup`, `retrieval_vector`, `review`, `sqlite3` (including under `TYPE_CHECKING`) |
| Domain + provider | `sentence`, `provider_capabilities`, `translation_plan` (plan, policy, `ProviderProfile`, `SegmentExecutionBudget`, `OperationExecutionBudget`, `TranslationRequestSpec`, request/operation identity), `llm_transport` (chat-completion HTTP transport: env resolution, one POST path, error mapping, transport-level execution-budget choke point, redacted per-attempt log), `semantic_review` (semantic-review-v1 engine: prompt, JSON parsing, anchoring, `term_effect`) | presentation and Telegram SDK (including under `TYPE_CHECKING`); builder-only modules |
| Application | `application` | presentation / Telegram SDK (including under `TYPE_CHECKING`); builder-only modules |
| Shared policy | `translation_policy`, `constants`, `exclusion_metadata` | presentation / Telegram SDK (including under `TYPE_CHECKING`); builder-only modules |
| Presentation (chat) | *(retired from Core; standalone `WuwaTerm-Telegram`)* | — |
| Presentation (HTTP) | `wuwaterm_api.*` (in-process `wuwaterm_api.web` retired; standalone `WuwaTerm-Web`) | everything in `wuwaterm` except the five allowlisted modules below; the Telegram SDK |
| Local state | `logging_utils`, `request_trace` | presentation / Telegram SDK (including under `TYPE_CHECKING`); builder-only modules |
| Storage | `db` | presentation / Telegram SDK; `build_pinyin` only inside write helpers (lazy) |
| Builder | `builder`, `data_source`, `build_pinyin`, `knowledge_builder` | presentation / Telegram SDK |
| Bootstrap | `cli` | Telegram SDK; may wire both runtime and builder entrypoints |

Companion entity knowledge base modules keep soft retrieval strictly isolated
from mandatory placeholder locks: `domain_knowledge` sits in `PURE_MATCHING`
(no SQLite or `knowledge_db` dependency) and enforces `lock_eligible=False`,
joint span-coverage and single-character CJK gating, and budget caps;
`knowledge_db` sits in `DOMAIN_CORE` (forbidden to `PURE_MATCHING`) as the
read-only SQLite companion schema and Lexical RAG reader bound fail-closed to
`terms_db_revision`; `knowledge_runtime` sits in `DOMAIN_CORE` (also forbidden
to `PURE_MATCHING`) as the thread-safe runtime cache that re-validates
`terms_db_revision` on `terms.db` or `knowledge.db` changes, degrades safely
with sanitized warnings (never leaking paths), dynamically controls
`knowledge-retrieval-v1`, backs `POST /v1/knowledge/query`, and feeds both
plain-text and `translate_protected_async` visible-text soft RAG retrieval
through `application` and `sentence`; `retrieval_vector` sits in `DOMAIN_CORE`
(also forbidden to `PURE_MATCHING`) as the optional pluggable vector backend and deterministic RRF
fuser, defaulting to zero third-party ML dependencies (INT8 memmap over stdlib
`mmap` when `numpy` is absent) and degrading fail-closed to pure Lexical RAG on
missing/mismatched `knowledge_db_sha256` or `terms_db_revision`; `knowledge_builder`
sits in `BUILDER` for offline extraction and atomic database generation.

Strict parsers for the builder's versioned free-text lock exclusion
metadata live in `exclusion_metadata` — standard-library modules plus
`constants` only. Sentence locking and the review engine's
automatic-assessment policy both consume that same corpus evidence while
keeping their own policies separate. The bounded English plural rule lives
in `english_plural`, so `sentence` locking (en→zh) and `review-v2` admit the
same inflected spellings instead of two drifting copies.

`term_matching` owns dictionary-surface admission and non-overlapping span
selection. The builder uses it directly with exact matching for both corpus
measurements. Prepared plain translation can opt into the existing admissible
NFKC spellings; protected text retains exact matching. `term_preparation` uses
those spans and the supplied dictionary snapshot to choose locks, contextual
references or unchanged text, then merges visible segments in order with one
placeholder namespace. `sentence` retains the database snapshots, model calls
and restoration.

**The API import allowlist.** `src/wuwaterm_api` may import exactly five
modules from `wuwaterm`: `application`, `models`, `translation_policy`,
`logging_utils`, `request_trace`. Anything else — `bot`, `channel`, `lookup`,
`sentence`, `db`, the markup helpers, the builder modules, the `wuwaterm`
package root — fails `scripts/check_architecture_boundaries.py`. That list is
the machine-checkable form of "the API cannot bypass the shared pipeline": an
adapter that could reach `lookup` or `sentence` directly could grow a second,
divergent pipeline without anyone noticing, which is exactly what a second
surface is most likely to do.

The fifth entry is an instrument, not a way in. `request_trace` holds the
per-request stage timings (`llm_slot_wait`, `llm_post`), strategy/thinking/budget
counters (`llm strategy` summary logs), and the in-process `RuntimeMetrics`
aggregator read by `app.state.metrics`; the stages are opened by `sentence` and
`llm_transport` CORE, which the adapter may not import. Offline log aggregation
lives in `scripts/summarize_runtime_metrics.py`. It therefore has to be
reachable from both sides, and it is the smallest thing that can be: a
`ContextVar`, a clock, and bounded fixed-vocabulary counters, importing nothing
from any sibling module, holding no dictionary, no pipeline, and no transport.
Adding a name to this list is still a deliberate act — the guard and this
paragraph are what make it one.

The shared execution-budget boundary lives in `translation_plan.OperationExecutionBudget`:
single-segment `translate_request_async` defaults to at most 2 upstream calls
and 1 shared corrective slot across protected-text/HTML->plain fallback, timeout
fallback, and paragraph repair (closing MY-483); multi-segment long-text
operations size calls to non-empty segments plus at most one corrective call per
segment; external chat adapters (such as standalone `WuwaTerm-Telegram`) can
align `OperationExecutionBudget` with their own admission guards; and
`llm_transport.post_chat_completion` enforces active cancellation, deadline, and
total-call bounds at the HTTP choke point.

Evidence boundary for `translation_plan.OperationIdentity`: `OperationIdentity`
identifies single-segment wire request, `RequestPolicy`, `ProviderProfile`,
dictionary identity, matcher revision, postprocess version, and corrective prompt
hash, and does not encode outer `OperationExecutionBudget` caps
(`max_total_calls`, `max_corrective_calls`, `max_corrective_calls_per_segment`,
`timeout_seconds`, and cooperative cancellation) or multi-segment/channel
orchestration envelopes. Identical single-segment
`OperationIdentity` therefore does not by itself prove execution equivalence
across different outer `OperationExecutionBudget` caps.

**The deployment-tool import direction.** The flat `scripts/` deployment tools
are a second graph, and the same script checks it for cycles over real AST
imports — a reverse edge written inside a function body counts, so keeping it
lazy does not hide it. `deployment_admission.py` is the read-only layer every
tool calls, so it necessarily calls tools back to verify them; what it must not
become is a cycle. `component_handoff.py` therefore holds only the predecessor
primitives, and `component_handoff_finish.py` — above both modules — owns the
admission-dependent removal. Neither is in a cycle.

The check walks strongly connected components rather than mutual pairs,
because a pair test only sees cycles of two: the builder graph
(`component_build` → `component_build_isolation` → `component_wheel_build`) and
the transaction graph (`component_deployment` → `component_full` →
`component_runtime`) are already three-module cycles. Both predate the rule,
belong to other subsystems, and are recorded in `KNOWN_SCRIPTS_CYCLES`. Naming
a component is what keeps the gate honest in both directions: anything this set
does not name fails, and an entry whose cycle is gone fails until it is
deleted.

Because `deployment_admission.py` loads verified tools dynamically via
`_tool_module` / `_tool_modules` (including `_ATTEMPT_PRODUCERS`), those
dynamic edges are pinned by `DECLARED_DYNAMIC_TOOL_DEPS` in
`scripts/deployment_admission.py`. `scripts/check_architecture_boundaries.py`
parses `_tool_module` / `_tool_modules` and `_ATTEMPT_PRODUCERS` via AST and
rejects any undeclared or non-discoverable dynamic load, any stale manifest
entry, any declared tool missing from `scripts/` or
`component_journal_runtime.TOOL_PATHS` (outside the legacy `runtime_update`
exception), and any dynamic dependency on higher-layer modules such as
`component_handoff_finish`.

The application layer holds the shared API/CLI/library pipeline (prepare →
direction → exact hit → trusted fuzzy hit → length gate → chunked
LLM call with contextual term references) and knows nothing about Telegram or HTTP. Its adapter-shaped steps
are injected by the caller: external chat adapters (such as standalone `WuwaTerm-Telegram`) can inject a markup translator and
UTF-16-aware splitter; the HTTP API injects neither, which is why its answers are
plain text by construction. Callers receive a `TranslationOutcome` and
own their wording.

Adapters can construct `ProtectedText` and `ProtectedStructure` through
`application` and call the translator's `translate_protected` or
`translate_protected_async`. Each opaque structure carries its own visible
context; Core uses that context for term boundaries, keeps dictionary terms as
contextual references within each visible segment, validates structural
placeholders and returns translated segments with their original structures. The adapter owns parsing and output validation.
A guarded markup hook must
forward `before_llm_call` to the translator; the guard runs after acquiring the
shared model slot. Unguarded legacy hooks remain supported.

Observed edges (static imports among shipped modules):

- `wuwaterm_api` → `wuwaterm.application` (pipeline, translator factory,
  lookup entry points, error codes) from `app.py` and `errors.py`;
  `wuwaterm.logging_utils` (`redact_id`) from `app.py`;
  `wuwaterm.request_trace` (start/reset the per-request stage trace, render its
  fields) from `app.py`; and `wuwaterm.translation_policy`
  (`LLM_INPUT_CHAR_LIMIT`) from `app.py`. The boundary guard permits those four
  modules plus `models`, and nothing beyond the five.
- `sentence` → `request_trace` (open the model-slot stage and record the
  direction), so the slot wait and the POST are timed by the layer that runs
  them; `llm_transport` → `request_trace` for the POST stage. Both edges are
  no-ops when no HTTP request is being traced (such as offline CLI runs or
  external library callers).
- `sentence` → `translation_plan` → `term_preparation`, `text_spans`, `cjk_span`, `semantic_review_contract`, `llm_transport`;
- `sentence` → `lookup`, `normalize`, `protected_text`, `term_matching`,
  `term_preparation`, `httpx` (with zero presentation or Telegram SDK imports).
- `term_preparation` → `term_matching`, `cjk_span`, `protected_text`,
  `single_char_context`, `skill_references`; `review` → `single_char_context`
  (only for the opt-in `review-v2` feature `ambiguous-mention-v1`);
  `single_char_context` → `cjk_span` (request-local evidence for ambiguous
  one-character mentions; standard library otherwise); `term_matching` → `cjk_span`, `constants`,
  `english_plural`, `models`, `term_classification` (no `lookup` or `db`);
  `term_classification` → `constants`, `models`, `normalize`;
  `skill_references` → `cjk_span`, `models`;
  `builder` → `term_matching` for both free-text exclusion measurements.
- `lookup` → `db` (read helpers), `models`, `normalize`, `constants`,
  `term_classification`.
- `db.insert_records` lazily imports `build_pinyin` (builder-only dependency).
- Runtime image refuses `bot` and `build-db` and excludes builder tooling,
  serving `api` and `device` commands only
  (`tests/test_runtime_imports.py`, CI `deploy-boundary` job).

Automated enforcement: `scripts/check_architecture_boundaries.py`,
`scripts/check_api_contract.py` and `tests/test_runtime_imports.py`.

### Current coupling (honest)

| Coupling | Status | Notes |
|----------|--------|-------|
| Legacy inline `bot.py` / `channel.py` | Retired | Removed from Core after `v0.16.0`; chat presentation lives in standalone `WuwaTerm-Telegram` |
| `sentence` → `telegram_html` | Resolved | Legacy HTML forwarding bridges and `telegram_html.py` retired from Core; `sentence.py` has zero presentation dependencies |
| `db` top-level imports `data_source.SourceProvenance` | Mild storage/builder type coupling | Provenance metadata is shared with build path |
| `segmentation`/`translation_plan`/`sentence` imported pure text helpers from `review` | Resolved | Pure Unicode-scalar span helpers (`WHITESPACE`, `sentence_ranges`, `sentence_ranges_v2`, `review_sentence_ranges_v2`, `segment_core`, `paragraph_ranges`) live in stdlib-only `text_spans`; Rule #11 forbids `db`, `entity_graph`, `knowledge_db`, `lookup`, `retrieval_vector`, `review`, and `sqlite3` across all pure matching/text modules (checked transitively by `tests/test_architecture_boundaries.py`), while `review` re-exports compatible public/private aliases |
| Core API and external consumers share one upstream LLM credential | Deliberate | When standalone `WuwaTerm-Telegram` or `WuwaTerm-Web` runs alongside `wuwaterm-api`, each container keeps its own state/identity isolation while upstream model keys remain operator-managed |

## Request flows

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
6. The shared pipeline runs: exact hit → fuzzy hit → length gate → model call
   with contextual term references, with the dictionary stage offloaded to a
   worker thread.
7. With no model configured, this surface refuses (`llm_unavailable`) rather
   than returning source text as though it had been translated — deterministic
   local substitution would be misleading over HTTP.
8. Re-check the device again before returning; the work is already paid for
   here, so a transient store error serves the answer while a definitive
   revocation still refuses.
9. Render `kind`, `text`, `direction`, `dictionary_miss`, `request_id` — plain
   text, no markup.

### Telegram commands and linked-channel auto-translation (Retired from Core)

Telegram command handling (`/tr`, `/term`, `/sentence`) and linked-channel
auto-translation are no longer implemented in WuwaTerm Core (removed after
`v0.16.0`); they live in standalone
[My-Denia/WuwaTerm-Telegram](https://github.com/My-Denia/WuwaTerm-Telegram) on
top of Core's protocol-neutral `application` and `ProtectedText` interfaces. See
[docs/telegram-behavior.md](telegram-behavior.md).
7. Deliver reply or edit existing chunks with `send_with_flood_retry`.
8. Telemetry outcomes via channel runtime counters.

Channel path is always auto-detected direction; it does not accept command
`--to` flags (`docs/telegram-behavior.md`).

### Paths that never call the LLM

| Path | Why |
|------|-----|
| Exact dictionary hit (`lookup_exact` + early returns in the shared pipeline) | Official string from SQLite |
| Fuzzy dictionary short answers (`_fuzzy_dictionary_answer`) | DB-only |
| `GET /v1/terms`, `GET /v1/meta`, `GET /v1/dictionary`, `GET /v1/capabilities`, `POST /v1/knowledge/query`, `POST /v1/reviews`, `POST /v1/structure-checks`, `/healthz`, `/readyz` | Dictionary reads, read-only companion knowledge retrieval (`knowledge-retrieval-v1`), deterministic pair review and structure checks ([ADR 0022](adr/0022-deterministic-structure-checks.md)), and probes only |
| Unauthorized / rate-limited / malformed HTTP requests | Rejected before translation work |
| LLM env incomplete | Core HTTP API refuses with `llm_unavailable` (`503`); offline `SentenceTranslator` library/CLI callers fall back to deterministic term substitution without inventing terms |

### Paths that call the LLM

| Path | What leaves the process | Notes |
|------|-------------------------|-------|
| `POST /v1/translations` (and standalone `WuwaTerm-Telegram` / CLI callers invoking Core's `translate_request_async`) | The source text (up to 2,000 characters per request) with official terms as contextual references | One shared pipeline, only after a dictionary miss |
| `POST /v1/semantic-reviews` (`semantic-review-v1`) | Both the source segment **and** the translation, plus read-only context and human precedents | Experimental and off by default (`WUWATERM_SEMANTIC_REVIEW_ENABLED`). At most one model call per request (none for two blank sides), sharing the translation budget, concurrency limit and per-device limit. Listed in `GET /v1/capabilities` only when switched on and a model is configured. See [ADR 0018](adr/0018-semantic-review.md) |
| `POST /v1/translations` with `context` (`translation-context-v1`) | The text, plus the previous segment and human precedents the request names | Opt-in per request; without `context` the request and prompt are unchanged. Core's own long-text path sends the previous segment the same way. See [ADR 0019](adr/0019-document-context-and-human-precedents.md) |
| `POST /v1/translations` with a `document_anchor` or `user_glossary_references` in `context` | The bounded document anchor, and matched `wuwaterm-termbase-v1` entries | Not on the wire: the two members exist on Core's `TranslationContext` for opt-in clients and offline evaluation, hold at most 280 scalars and 16 entries, and render only when set. A user entry never becomes a placeholder lock and loses to an official surface in the same segment. See [ADR 0021](adr/0021-local-user-termbase.md) |

`review-v1` and `review-v2` stay deterministic: `POST /v1/reviews` never calls
the model, costs nothing per request, and returns the same report for the same
input and dictionary. Model findings never become a `verified_constraint`; the
two kinds of result are separate tracks that a client requests separately.
Structure checks (`POST /v1/structure-checks`, `structure-check-v1`) are a
third deterministic track for numbers, placeholders and tags, paragraph layout
and empty targets; they leave both review responses unchanged
([ADR 0022](adr/0022-deterministic-structure-checks.md)).

The response contract of the model path lives in
`semantic_review_contract` (domain core, no model code): field limits, display
excerpts, withheld suggestions, ordering and the 56 KiB byte budget. Long-text
segmentation and bilingual pairing for clients follow
[segmentation-v1](segmentation-v1.md), implemented in `segmentation`.

## Data model: immutable vs mutable

| Kind | Location | Mutability | Notes |
|------|----------|------------|-------|
| Terminology SQLite | `data/terms.db` (Compose: `/app/data` **ro** in `wuwaterm-api`) | Immutable at runtime | Built offline; promoted by `deploy/vps-update.sh` ([ADR 0004](adr/0004-sqlite-terminology-data.md), [ADR 0008](adr/0008-candidate-verification-and-transactional-deployment.md)) |
| Device credential store | `state-api/devices.db` | Mutable, operator-written | Sibling of any legacy or standalone Telegram `state/` directory; created by the operator path only, never by a request ([ADR 0010](adr/0010-device-principal-authentication.md)) |
| Legacy / standalone Telegram chat state | `state/chat_settings.json`, `state/channel_replies.json` (owned by standalone `WuwaTerm-Telegram`; legacy Core `v0.16.0` layout preserved for migration/rollback) | Mutable | Not mounted or accessed by `wuwaterm-api` ([ADR 0005](adr/0005-file-backed-single-instance-state.md)) |
| Rate limiters, model budgets, credential pool | Process memory, inside `wuwaterm-api` | Mutable, lost on restart | Per-process only; never shared with external products |
| Source pins / profile | `constants.py` + DB metadata | Immutable until rebuild | |
| Secrets | Host `.env` mode `600` | Operator-managed | Not in image; not in builder; narrowed per container |
| Client base address | `%APPDATA%/WuwaTerm/config.json` on the owner's PC | Mutable, non-secret | The device token is never written here — it lives in the OS credential store |

## Single-instance assumptions

Supported topology (also [supported vs unsupported](#supported-vs-unsupported-topology)):

- One Core API container (`container_name: wuwaterm-api`) bound to loopback.
- One Compose host; `network_mode: host`.
- Builder is profile-gated one-shot work (`wuwaterm-builder`), not a second always-on replica.
- Standalone `WuwaTerm-Telegram` (when deployed) owns its own single-poller and single-token assumptions in its own container.

### Dual-instance failure modes (unsupported)

If two API processes share the same credential store and port:

1. **Doubled budgets** — model concurrency, per-minute caps, per-device rate
   limits and the credential pool are per process, so two instances double
   every ceiling this document states.
2. **Port ownership** — both bind the same loopback port; the second fails to
   start, or an operator "fixes" it by binding something else, which is the
   one thing the bind guard exists to prevent.

Docs and ops must not treat multi-instance as HA. SQLite WAL and local state
locks are durability aids for a single serving instance, not a cluster
protocol.

## Failure propagation

| Dependency | Failure mode | User-visible / recovery |
|------------|--------------|-------------------------|
| LLM HTTP | Timeout, 4xx/5xx, empty content | API: enumerated code (`llm_unavailable`, `llm_budget_exhausted`) in the standard envelope. Core library callers receive `LLMTranslationError` |
| SQLite terms DB | Missing/corrupt file | Startup/use fails when `TermService` cannot read; `/readyz` answers `503`; deploy keeps previous DB on failed promotion |
| Credential store | Missing, deleted, locked, corrupt | `503` (store unusable), never `401` — a valid device is never told to re-pair because of a database hiccup |
| Ingress route | Proxy down, route removed | Clients render an offline or timeout state; `wuwaterm-api` keeps running on loopback |
| Docker / Compose | Image/build failure, wrong revision | `vps-update.sh` aborts before stop (and refuses preflight if legacy `wuwaterm-bot` is still running; a stopped `wuwaterm-bot` is left untouched with a manual `docker rm` hint), or rolls back image + DB + pointer for `wuwaterm-api` after failed post-promote steps |

No automatic cross-region failover. Recovery is restart, restore from
timestamped backups under `data/deployment-backups` (the newest five snapshots;
every successful full deploy prunes older ones, see
[Deployment](deployment.md)), or re-run transactional deploy after fixing the
root cause.

## Data refresh and transactional deploy

From repo scripts and docs only (no live VPS mutation required to understand):

1. **Refresh** — `wuwaterm-builder refresh-data` sparse-checkouts pinned source
   (`data_source.refresh_data`, `docs/data-refresh.md`).
2. **Build candidate** — `build-db --atomic` → unique path under
   `data/candidates/` (updater), not live `terms.db`.
3. **Verify** — `verify-db`, seed/exact/idempotent scripts as wired in
   `deploy/vps-update.sh` and `docs/validation.md`.
4. **Build immutable runtime image** labeled with source commit revision.
5. **Promote** — stop `wuwaterm-api`, swap DB, start `wuwaterm-api` on the exact
   image, verify in-container `/readyz` readiness, write immutable
   `.deployments/<commit>.json`, atomic `.deploy_commit`.
6. **Rollback** — on post-promote failure restore previous DB, rollback image
   tag, and pointer (`deploy/vps-update.sh` `rollback_on_failure`), restarting
   `wuwaterm-api`.

Runtime vs builder separation: [ADR 0007](adr/0007-runtime-builder-separation.md).
Dictionary-before-LLM: [ADR 0006](adr/0006-dictionary-first-before-llm.md).

## Conditions that would justify later extensions

These are **not** current capabilities. They would need a new goal, ADRs, and
likely product/ops changes:

| Extension | Real trigger (examples) |
|-----------|-------------------------|
| Private overlay network instead of the public route | Evidence of unwanted traffic on the route; a decision that the surface should not be public; a second machine that also needs access ([ADR 0012](adr/0012-client-transport-selection.md)) |
| Multi-user / a principals table | A second human user, or per-user quotas. Today the device id IS the principal id |
| Shared cross-process LLM budget | A second client machine, an observed breach of the model account's limits, or more than one instance of the API — see [cost topology](#cost-topology-budgets-are-per-process-and-the-worst-case-is-their-sum) |
| Web admin console | Operators need bulk allowlist/audit or device management over HTTP; multi-owner RBAC. Standalone `WuwaTerm-Web` (which replaced the retired in-process private web layer) holds no administration capability and cannot issue/revoke devices or authorize Telegram chats |
| Multi-instance / external state | Sustained load exceeds one process **and** the delivery model is redesigned |
| Postgres / Redis / queue | State or job volume exceeds single-host SQLite operational comfort; multi-host deploy becomes a requirement |
| Public or third-party API clients | Anyone but the owner needs access, which changes rate limits, abuse handling and the identity model together |

Until those triggers are real, adding the machinery would be ceremony.

## Supported vs unsupported topology

| Supported | Unsupported |
|-----------|-------------|
| Single VPS, Compose, one Core API container (`wuwaterm-api`) bound to loopback, published by a path route on the operator's existing HTTPS ingress | The API binding a public interface, a second open port, multi-replica HA, or answering `/v1` without a device credential |
| Standalone [WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web) (deployed via `./deploy/setup.sh --with-web` or hosted separately) calling `/v1` with a Core device token | Re-enabling the retired in-process `/wuwaterm-web` sub-app on Core `main` (roll back to `v0.16.0` if needed) |
| Standalone [WuwaTerm-Telegram](https://github.com/My-Denia/WuwaTerm-Telegram) (deployed via `./deploy/setup.sh --with-telegram`) consuming Core's pinned wheel | Running the retired inline Core `wuwaterm-bot` alongside current Core `main`, multi-token sharding, or active-active bot polling |
| `data/terms.db` RO in `wuwaterm-api` + `state-api/` RW to `wuwaterm-api` only | Runtime writing game TextMaps or self-promoting DB; cross-surface state sharing |
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
