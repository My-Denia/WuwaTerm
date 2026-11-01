# ADR 0009: HTTP API as a second presentation adapter

- Status: Accepted
- Date: 2026-08-11
- Amends the context of [ADR 0001](0001-telegram-as-presentation-layer.md)
  and [ADR 0003](0003-long-polling-not-webhook.md)

## Context

[ADR 0001](0001-telegram-as-presentation-layer.md) recorded that a generic
HTTP API "would add auth, hosting, and client surface without improving
dictionary fidelity". That was true while Telegram was the only surface
anyone needed. It stopped being true when the owner needed a desktop
application on their own machine: the alternative to an API was a second copy
of the dictionary-first pipeline inside a desktop program, which is the one
outcome this project's layering exists to prevent.

Two things had to be settled before an API could be added without damaging
the property ADR 0001 protects:

1. Where the shared behaviour lives, so that two adapters cannot drift.
2. What "a second inbound surface" means for a deployment whose whole
   security posture was "there is no inbound HTTP listener".

## Decision

### One shared application pipeline, one specialized channel path, one more consumer

`src/wuwaterm/application.py` holds the dictionary-first pipeline used by
Telegram command handlers, the HTTP API and the API process's private web
layer: prepare, direction resolution, exact dictionary hit, trusted fuzzy hit,
length gate, chunked term-locked model call, and the stable error vocabulary.
It is protocol-neutral: it imports no presentation module and no chat SDK.

Current callers and the intentional exception are:

- `src/wuwaterm/bot.py` command handlers inject the two adapter-shaped steps
  the pipeline takes as parameters: a markup-aware translator and a
  markup-aware text splitter.
- `src/wuwaterm/channel.py` linked-channel auto-translation intentionally owns
  channel admission, freshness, edit, delivery and exact-first orchestration;
  it shares protocol-neutral term and sentence primitives rather than calling
  the application pipeline.
- `src/wuwaterm_api/` — the HTTP adapter. It injects neither, so its answers
  are plain text by construction rather than by convention.

This qualification records the existing implementation; it does not change
linked-channel behavior or turn the channel path into another API contract.

The desktop client (`client/`, [ADR 0011](0011-pc-client-stack.md)) is not a
third adapter: it is a consumer of the HTTP adapter's published contract, and
holds no translation logic of its own.

### The API package may not reach past the application layer

`src/wuwaterm_api` is a separate top-level package, and
`scripts/check_architecture_boundaries.py` allows it to import exactly four
modules from `wuwaterm`: `application`, `models`, `translation_policy` and
`logging_utils`. It may not import `bot`, `channel`, `lookup`, `sentence`,
`db`, the markup helpers, the builder modules, the `wuwaterm` package root,
or the chat SDK. That allowlist is the machine-checkable form of "the API
cannot bypass the shared pipeline": if the adapter could reach `lookup` or
`sentence` directly, a second, divergent pipeline could grow there without
anyone noticing.

### Framework and contract

FastAPI with uvicorn, confined to the optional `api` extra. The choice is
about the contract, not about the framework's features: the generated
OpenAPI document is committed at `docs/api/openapi.json`, and
`scripts/check_api_contract.py` regenerates it and compares, so a change to
a route, a model or an error code cannot merge without the published contract
changing in the same commit.

The surface is versioned under `/v1`:

| Route | Scope | Answer |
|---|---|---|
| `POST /v1/translations` | `translate` | `kind` (`noop`/`exact`/`fuzzy`/`llm`), `text`, `direction`, `dictionary_miss`, `request_id` |
| `GET /v1/terms?q=` | `meta` | Backend-ranked exact or fuzzy dictionary candidates with official strings, scores and reasons |
| `GET /v1/meta` | `meta` | Service and data provenance: no paths, no secrets, no chat identifiers |
| `GET /healthz` | none | Liveness |
| `GET /readyz` | none | Readiness: the terminology database is readable right now |
| `GET /openapi.json` | none | The contract itself |

Every failure renders one envelope — `{"error": {"code", "message"},
"request_id"}` — with an enumerated `code` from a closed set, so a client
branches on `code` and never on prose. Where the code set cannot express a
distinction, the HTTP status carries it: `504` for the server time budget and
`503` for a dependency that is temporarily unavailable are both classified
`internal`, and the status is what separates them from a genuine `500`.

Middleware, outermost first: a server-generated request id (an inbound
`X-Request-Id` is ignored entirely, so a caller cannot route its own
credential into the logs or a response body), a body size and arrival-time
cap, and a request time budget.

### The API is not chat delivery

[ADR 0003](0003-long-polling-not-webhook.md) decided that chat updates arrive
by long polling. **That decision is unchanged.** The bot still polls; nothing
in this repository registers a chat-side delivery endpoint. What ADR 0003 also said — that this deployment has "no public
application HTTP surface" — is what this record amends: there is now one
inbound HTTP surface, it belongs to the API adapter, it is authenticated per
device ([ADR 0010](0010-device-principal-authentication.md)), and it has
nothing to do with how chat updates are received.

### Separate process, separate budgets, separate state

The adapter runs as its own container from the same image
(`command: ["api"]`), reads the same terminology database read-only, and
writes only its own credential store under a state directory that is a
sibling of the bot's, never a child of it. It has its own model concurrency
limit and its own per-minute call budget. Those budgets are per process: the
worst case for the host is the SUM of the two surfaces, never one shared
ceiling. See the cost topology section of
[the architecture map](../architecture.md).

## Consequences

- Positive: a non-chat client is possible without a second pipeline, and the
  boundary guard makes that structural rather than a review promise.
- Positive: the contract is machine-readable and drift-gated, so a client can
  be generated or hand-written against a document that cannot silently rot.
- Positive: the chat surface is untouched. The bot container's runtime
  configuration, behaviour and state are the same as before the adapter
  existed.
- Negative: the deployment now has an inbound listener where it previously
  had none. It is bound to loopback, and everything under `/v1` requires a
  device credential, but the class of risk is new and is the reason
  [ADR 0012](0012-client-transport-selection.md) exists.
- Negative: the public wheel of this public repository now ships API server
  code, and `scripts/check_package_artifacts.py` requires it. The
  distribution boundary that matters — no database, no game data — is
  unaffected.
- Constraint: three routes answer without a credential (`/healthz`,
  `/readyz`, `/openapi.json`). Whether they are reachable from outside the
  host is an ingress decision, not something the application enforces.
- Constraint: the adapter renders plain text. Chat markup is an adapter
  extension and may not appear in this contract.

## Evidence

- `src/wuwaterm/application.py` — the single pipeline and its injection points
- `src/wuwaterm_api/app.py` — routes, middleware, error envelope, dependencies
- `src/wuwaterm_api/errors.py`, `src/wuwaterm_api/settings.py`
- `scripts/check_architecture_boundaries.py` — the API import allowlist
- `scripts/check_api_contract.py`, `docs/api/openapi.json` — contract drift
- `deploy/docker-compose.yml` — the second service, its mounts and its bind
- `tests/test_api.py`, `tests/test_architecture_boundaries.py`
