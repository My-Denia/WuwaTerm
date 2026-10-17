# Deployment

> **Scope: this is the author's own production runbook.** It describes one
> specific installation — a transactional updater with rollback, an immutable
> per-commit deployment manifest and pointer, one particular reverse proxy
> configuration, and the readback procedure that proves the promotion took.
> Every path, port and command below is that deployment's, not a general
> recommendation.
>
> **If you are not the author, start with [self-hosting.md](self-hosting.md).**
> Running your own copy — Docker Compose or plain Python, whichever TLS
> terminator you already have, and how to upgrade, back up, restore and
> troubleshoot it — is that document's subject, and none of it requires the
> machinery described here. This page is kept for the operator of this one
> server and as a worked example of what a transactional promotion looks like.

## VPS Docker Compose

The VPS target uses Docker Compose because the current system Python there is
older than the project target. `/opt/wuwaterm/current` must be a clean Git
checkout with an `origin` remote and a `main` ref; the updater fetches and
requires `HEAD == origin/main`, so an exported source copy without `.git` is
intentionally not deployable. Create `/opt/wuwaterm/current/.env` from
`.env.example` or `deploy/env.example`, and set it to mode `600`. These two
template files are intentionally identical and tested for drift.

Both serving services read that one file through Compose `env_file`; the
builder has no `env_file` at all, and `.env` is ignored and excluded from the
image build context. What reaches each serving container is then narrowed by
its own `environment:` block. `wuwaterm-api` has `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_TEST_CHAT_ID`, `OWNER_USER_ID`, `WUWATERM_REDACTION_SECRET` and the
bot's state paths blanked, because none of them are its business. It does
receive the model credentials — `WUWATERM_OPENAI_API_KEY` and the other
`WUWATERM_OPENAI_*` settings — and that sharing is deliberate: both surfaces
translate through the same upstream account, and a second key would carry the
same power while doubling what has to be rotated. So the isolation boundary is
specific, not total: the Telegram identity, the owner identity and the log
redaction key stay with the bot; the model credential is shared.

The Compose file has two image roles across three services:

- `wuwaterm` builds the `runtime` target and runs the Telegram bot. It mounts
  `data/` at `/app/data` read-only and `state/` at `/app/state` writable.
- `wuwaterm-api` runs the SAME runtime image with `command: ["api"]`. It mounts
  the same `data/` read-only and its own `state-api/` at `/app/state-api`
  writable, so the two serving processes never share writable state. That
  directory is a SIBLING of `state/`, not a child: the bot mounts the whole of
  `state/` read-write, so a child directory would have given the bot process
  read-write access to the credential store. The API container also gets the
  bot's credentials blanked, because they are not its business. It binds
  loopback only, and that bind is fixed in the Compose file rather than read
  from `.env`: with host networking, an environment knob would turn a public
  exposure into a one-line edit.
- `wuwaterm-builder` builds the `builder` target. It has the data-build
  toolchain, mounts `data/` writable, and runs `refresh-data`, `build-db`,
  and `verify-db`.

The runtime image refuses every command except `bot`, `api` and `device`
(exit 64 otherwise), and still ships without the data-build toolchain.

Prepare and verify a candidate without changing the serving database:

```bash
cd /opt/wuwaterm/current
docker compose -f deploy/docker-compose.yml run --rm wuwaterm-builder refresh-data
docker compose -f deploy/docker-compose.yml run --rm -e WUWATERM_DB_PATH=/app/data/terms.candidate.db wuwaterm-builder build-db --atomic
docker compose -f deploy/docker-compose.yml run --rm -e WUWATERM_DB_PATH=/app/data/terms.candidate.db wuwaterm-builder verify-db
```

Do not move this candidate over `data/terms.db` manually. The transactional
updater is the production promotion boundary.

The compose service uses long polling (`wuwaterm bot`) and
`restart: unless-stopped`. It does not configure webhook delivery, inline query
handling, or any extra command-routing layer. Starting the service is
owner-gated:

```bash
cd /opt/wuwaterm/current
docker compose -f deploy/docker-compose.yml up -d
```

## HTTP Adapter Service

`wuwaterm-api` serves the versioned HTTP surface documented by
`docs/api/openapi.json`. It opens **no listener of its own that anything
outside the host can reach**: it binds `WUWATERM_API_BIND` (default
`127.0.0.1`) on `WUWATERM_API_PORT` (default `8788`). The service runs with
host networking, so a `ports:` list would have no effect at all and none is
present: what keeps this off the host's public interfaces is the bind address,
hard-coded in `deploy/docker-compose.yml` rather than interpolated from an
environment file.
One other line in that same file can override it — `command:` is passed
through to the server, which accepts `--host` — so both live where they can
only be changed in review.

A desktop client reaches the service at **the configured secure endpoint**:
one stable base address, served over TLS by the reverse proxy that already
fronts the operator's existing sites on this host, routed to the loopback port
above. That selection, its threat model and its rollback are recorded in
[ADR 0012](adr/0012-client-transport-selection.md); the route itself is the
[Publishing the API](#publishing-the-api) section below.

Anything beyond that one route — a second route, an open port, a new name, a
firewall change — is still an owner decision and not a deployment step.

Inventing one here (a forwarded port, an open port, a new route) is exactly the decision this project stopped making by default.

Two properties hold whatever the network arrangement is, and neither is a
consequence of it:

- **The API contract does not encode the network path.** The base address is
  pure client configuration; moving the service from one endpoint to another
  changes no request, response or contract byte.
- **Every `/v1` operation is authenticated at the application layer.**
  Reaching the endpoint is never sufficient: the device credential below is
  required on every `/v1` call, so being on the right network is not an
  authorization. The two probes `GET /healthz` and `GET /readyz` are
  deliberately unauthenticated (they answer `ok`/`ready` and expose nothing
  else), as is `GET /openapi.json`; those three ARE reachable through the
  published route, which is a property of the route and not something the
  application enforces.

The port a running container was actually given is an operations fact, so
read it back from that container rather than assuming the default:

```bash
cd /opt/wuwaterm/current
docker compose -f deploy/docker-compose.yml exec -T wuwaterm-api printenv WUWATERM_API_PORT
```

**Correct the port before publishing anything.** If an existing `.env` sets
`WUWATERM_API_PORT` explicitly it wins over both the Compose default and the
application's, so a changed default does not reach that host on its own — and
the route below would then be pointed at whatever else holds the old port.
Update or remove the line:

```bash
WUWATERM_API_PORT=8788
```

A normal `deploy/vps-update.sh` run recreates both containers from the current
Compose file, so on a host whose `.env` does NOT pin the port there is nothing
to do here. The manual recreate below is only for a host that pinned the old
one, because an `.env` edit does not reach a container that is already running.

**Recreate it on the image it is already on.** The updater creates the serving
containers with an immutable `WUWATERM_RUNTIME_IMAGE=wuwaterm-runtime:<source
commit>` and `--no-build`; a bare `compose up -d` would instead resolve the
Compose default `wuwaterm-runtime:local` and, because the service carries a
`build:` block, BUILD one — an unvalidated image, labelled `unknown`, that
breaks the manifest's image binding and splits the two surfaces apart. Take the
reference from the running container (the deployment manifest's `image_ref` is
the other source for it) and forbid building:

```bash
cd /opt/wuwaterm/current
pinned=""
image="$(docker inspect --format '{{.Config.Image}}' wuwaterm-api 2>/dev/null)"
running_id="$(docker inspect --format '{{.Image}}' wuwaterm-api 2>/dev/null)"
tag_id="$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null)"
[ -n "$tag_id" ] && [ "$tag_id" = "$running_id" ] && pinned="$image"
echo "${pinned:?no usable pin: no wuwaterm-api container, or its reference no longer names the image it runs}"
WUWATERM_RUNTIME_IMAGE="${pinned:?nothing pinned}" docker compose -f deploy/docker-compose.yml up -d --no-build --force-recreate wuwaterm-api &&
  docker compose -f deploy/docker-compose.yml exec -T wuwaterm-api printenv WUWATERM_API_PORT
```

**That block refuses rather than warns, and both refusals matter.** A container
records the reference it was created with (`.Config.Image`) and, separately, the
image it is actually running (`.Image`, an id — which is what this page's
Traceability Readback compares and what the deployment manifest binds). A tag is
mutable, and the updater rebuilds and retags `wuwaterm-runtime:<commit>` before
its rollback trap exists, so a same-commit rerun that aborts in between leaves
the tag pointing at a fresh, unvalidated image while this container still runs
the old one. Nothing here is left to the operator to notice: `pinned` is set
only when the reference resolves AND still names the running id, and every
command that uses it is written so that an unset `pinned` stops it rather than
falling through. `pinned` is emptied on the first line so a value from an
earlier successful run in the same shell cannot stand in for one this run
failed to establish, and `[ -n "$tag_id" ]` is there because two empty strings
compare equal — without it the check would pass on a host with no container at
all, which is the one case it most needs to catch. The `&&` on the recreate is
the same idea for the readback: a port read from a container that was never
recreated is not the port this subsection just set.

There is exactly ONE refusal, and its message names both situations it can
mean, because the block cannot tell an operator which. The lookups run inside
command substitutions, where a failure ends the substitution and not the
sequence, so no earlier line can be the thing that stops the block — their
errors are silenced for that reason, leaving one message rather than a docker
error followed by a claim that contradicts it. A stopped container is not a
failure at all: `docker inspect` answers for it, and pinning off it is
correct.

What `printenv` prints is the port the route below must name. The block also
echoes the reference it is about to use, and the Traceability Readback at the
end of this page requires BOTH running containers to match the manifest — this
step is the one that could break that.

Shell access to the host stays what it has always been: the operator's
administration channel, used for the deployment and credential commands on
this page. It is not a path for the desktop client and is never required for
using it.

### Publishing the API

The endpoint is a **path route on an HTTPS site the host already serves**, not
a new site and not a new listener. Applying it is an owner-gated step, separate
from deploying the service, and it is the only host change the client's
transport needs.

Preconditions, all of which are properties to CHECK on the host rather than
things to create:

- the reverse proxy is already terminating TLS for at least one site on this
  host, and that name already resolves here;
- its certificates are installed from files rather than obtained
  automatically, so adding a route triggers no certificate or account
  activity;
- the site block being extended already routes by path prefix, so the addition
  is purely additive rather than a restructuring of a live route;
- the API is running and answering on loopback (the readback below).

With those true, the route is one block added to the existing site. In Caddy
syntax, inside the site block that already serves the chosen name:

```caddyfile
handle_path /wuwaterm-api/* {
    # The port from the readback above, not the default assumed here.
    reverse_proxy 127.0.0.1:8788
}
```

`handle_path` strips the matched prefix, so the client's base address is
`https://<the-existing-site>/wuwaterm-api` and the service still sees
`/v1/...`: the client's HTTP library merges a relative route onto a base that
carries a path, so `/v1/translations` is sent as
`/wuwaterm-api/v1/translations` (pinned by
`client/tests/test_api.py::test_a_base_address_with_a_path_prefix_keeps_it_on_every_route`).

The trailing `/*` is deliberate. `/wuwaterm-api*` would also match a sibling
path that merely starts with the same characters — `/wuwaterm-api-docs`, say —
and strip the prefix out of it, which is the one thing "purely additive" must
not mean. The bare `/wuwaterm-api` with nothing after it stops matching, and
nothing requests it.

With another proxy, the equivalent is a path-prefix route that strips the
prefix and passes the request to the same loopback address; nothing about the
route may bind or publish the API itself.

Back up the configuration before editing it, and apply the change with the
proxy's own reload rather than a restart, so the sites already being served are
not interrupted:

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date -u +%Y%m%dT%H%M%SZ)
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

**Rollback is deleting that block and reloading again** (or restoring the
backup file). Nothing else is affected: the API keeps running exactly as it did
before, still bound to loopback, and both containers are untouched. Because the
service never binds a public interface, removing the route removes the
exposure — there is no second place it could still be reachable from.

**Readback belongs on the client machine.** A request made on the host proves
the service is up; it does not prove the route works, and it is not evidence of
anything about the path being tested. From the owner's own machine, request
`https://<the-existing-site>/wuwaterm-api/v1/meta` **without** a credential: it
must come back as the API's own `401` envelope. That single answer carries
three facts — the route reaches this service, the prefix is being stripped
(`/v1/meta` was matched by the application, not by the site), and reaching the
endpoint is not an authorization. Ask for a path under the prefix rather than
the base address itself: the base is what the client is configured with, and on
its own it matches no route. Then start the desktop client against that base
address and translate something.

### Device Credentials

There is no registration endpoint. Credentials are issued by the operator on
the host and shown exactly once.

**Run them on the image the service is already on.** These are one-shot
containers of the same Compose service, and `docker compose run` resolves the
image the same way `compose up` does: from `WUWATERM_RUNTIME_IMAGE`, falling
back to the Compose default `wuwaterm-runtime:local`. The updater never uses
that default — it creates the serving containers from an immutable
`wuwaterm-runtime:<source commit>` — so on a host that still carries an older
`wuwaterm-runtime:local` tag from before, an unpinned `run` starts a container
from a *different and stale* image while the deployed one keeps serving. The
property to hold, and the one to check a variant of these commands against, is:
**the one-shot container must be the deployed image, not whatever the default
tag resolves to.** Getting it wrong does not announce itself as the wrong
image: the runtime entrypoint accepts only the commands its own build knows, so
an image older than a subcommand refuses that subcommand, and the refusal reads
like a broken runbook. Resolve the reference exactly as the recreate above does
— the same resolution, the same refusals — and carry it on each command:

```bash
cd /opt/wuwaterm/current
pinned=""
image="$(docker inspect --format '{{.Config.Image}}' wuwaterm-api 2>/dev/null)"
running_id="$(docker inspect --format '{{.Image}}' wuwaterm-api 2>/dev/null)"
tag_id="$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null)"
[ -n "$tag_id" ] && [ "$tag_id" = "$running_id" ] && pinned="$image"
echo "${pinned:?no usable pin: no wuwaterm-api container, or its reference no longer names the image it runs}"
WUWATERM_RUNTIME_IMAGE="${pinned:?nothing pinned}" docker compose -f deploy/docker-compose.yml run --rm wuwaterm-api device issue --name "owner laptop"
WUWATERM_RUNTIME_IMAGE="${pinned:?nothing pinned}" docker compose -f deploy/docker-compose.yml run --rm wuwaterm-api device list
WUWATERM_RUNTIME_IMAGE="${pinned:?nothing pinned}" docker compose -f deploy/docker-compose.yml run --rm wuwaterm-api device revoke --device-id <id>
```

**The pin travels on the command, and refuses rather than warns.** A bare
`export` would report its own success and not the command inside it, so a failed
lookup would leave the variable EMPTY — and Compose's `:-` default treats empty
exactly like unset, which is `wuwaterm-runtime:local`: the failure this section
exists to prevent, reached by following this section. `${pinned:?…}` cannot do
that. A shell where the resolution did not succeed has no usable `pinned` — it
is emptied on the block's first line, so a value left over from an earlier
successful run cannot stand in for one this run failed to establish — and every
one of these commands therefore stops on its own, including in a session where
the block above was never run, and including the stdin form below.

The `echo` is there to say what you are about to run on, not to be validated by
its shape. `wuwaterm-runtime:<commit>` is the usual answer;
`wuwaterm-runtime:rollback-<deployment id>` is the right one on a host that is
currently rolled back, and even `wuwaterm-runtime:local` is right on a host
whose API container really was created from that tag. What makes the reference
trustworthy is the id comparison two lines above it, which has already run by
the time anything is printed — an operator checking the format instead would
reject a correct pin after a rollback.

A resolved reference also happens to keep the one-shot container from being
BUILT — the service carries a `build:` block, and a reference resolving to no
local image is what sends Compose to build an unvalidated one — but that is a
consequence of the pin holding, not a second guard.

What the check establishes is point-in-time: the reference named the deployed
image when it was checked, and Compose resolves that reference again on every
command below. A deployment that retags the same commit **while** a credential
session is open would move it underneath. That is not something a command in
this block can close — it is a reason not to issue credentials during a
deployment, and the reason the Traceability Readback at the end of this page,
which compares image ids rather than names, is what says what actually ran.

`device issue` reads the secret from standard input; the service never prints
credential material. Generate it where it will be stored, then register it:

```bash
WUWATERM_RUNTIME_IMAGE="${pinned:?nothing pinned}" docker compose -f deploy/docker-compose.yml run --rm -T wuwaterm-api   device issue --name "owner laptop" < /path/to/secret
```

The token is `wtd1.<device_id>.<that secret>`. Only a salted scrypt verifier is
stored, in `state-api/devices.db` (created 0600, in a 0700 directory, together
with its write-ahead log). Revoking keeps the row and stamps `revoked_at`, so a
withdrawal stays auditable. Break-glass: deleting `state-api/devices.db`
revokes every device at once, and the next start recreates an empty store.

`state-api/` must exist and be writable by the container user before the first
start; create it on the host rather than letting Docker create it root-owned:

```bash
mkdir -p state-api && chmod 700 state-api
```

### Reading The Request Log

The service writes to its **standard error** — the stream the standard
library's default handler uses, and the one the bot's records already go to. The
container runtime collects both streams, so `docker compose logs` shows them; a
collector that captures only stdout will not:

```bash
cd /opt/wuwaterm/current
docker compose -f deploy/docker-compose.yml logs --since 30m wuwaterm-api
```

At `INFO`, the default, every HTTP request produces exactly one **completion
record**, one line, always the same fields, recognisable by the words `request
complete`:

```
2026-01-01 00:00:00,000 INFO wuwaterm_api request complete request_id=<32 hex> method=POST route=/v1/translations status=200 duration_ms=41.2 device=id:<8 hex>
```

That guarantee is about the completion record and nothing else. At `INFO` the
service also writes the diagnostic lines it has always written — an
authentication refusal, a rate-limit refusal, and for a translation this one:

```
2026-01-01 00:00:00,000 INFO wuwaterm_api translation device=id:<8 hex> kind=exact direction=en request_id=<32 hex>
```

`kind` is the pipeline stage that answered: `exact` or `fuzzy` from the
dictionary, `llm` from the model branch, `error` when the pipeline failed,
`noop` when there was nothing to do. **Read it as the outcome, not as a
billing record.** `exact`, `fuzzy` and `noop` do mean no model was called.
The other two prove nothing in either direction: on a host with no model
configured the pipeline still labels its local fallback `llm` (and this
surface then refuses the request as unavailable rather than pretending), and
`error` can follow a provider call that was made, and charged, before it
failed. What model spend actually is lives with the provider, not here.

When a failed model outcome carries internal diagnostics, the HTTP adapter also
writes a bounded warning tied to the same server-generated request ID:

```text
WARNING wuwaterm_api llm translation diagnostic request_id=<server-id> reason=invalid_response detail=missing_placeholder expected_count=1 actual_count=0
```

Join this line to the completion record by `request_id`. `detail` distinguishes
`empty_output`, `non_text_output`, `missing_placeholder`, and
`duplicate_placeholder`; `unspecified` covers errors without a finer diagnostic.
The count fields describe only the **first** failing known placeholder check,
not total missing terms. They are `-` when unavailable. Unknown categories and
malformed counts are replaced with safe defaults, never stringified.

These fields are internal logs only. They do not contain source text, translated
text, placeholder tokens, credentials, request headers or provider addresses,
and they do not appear in the HTTP response or OpenAPI schema. The existing
`reason` and public error-code mappings remain distinct: a non-text provider
response envelope remains `invalid_api_response`, while a custom translator's
non-text output remains `invalid_response`. No fallback or retry is added.
Failure to write this extra diagnostic cannot replace the request's original
error. A diagnostic identifies a failed check, not model reliability or billing.
Reading the diagnostic attribute is inside the same guard as writing the log.
Business classification uses the original reason's plain string value, not the
diagnostic record: string subclasses retain their meaning without calling custom
hash/equality/string methods, while non-string or unreadable reasons become the
existing `llm_unavailable` error.

A request can therefore produce several lines in total, and a collector must
select on `request complete` rather than assume every line has that record's
fields. Every
request produces the completion record, including the unauthenticated
`/healthz` and `/readyz` probes, so a monitor polling those is visible in the
volume. Raising the level above `INFO` drops all of them — that is the trade
that setting is for, and correlation goes with them.

`request_id` is minted by the service and reaches the caller two ways, neither
of which covers quite everything:

| Response | In the JSON body | In `X-Request-Id` |
|---|---|---|
| a `/v1` answer an endpoint returned normally | yes | yes |
| a handled failure's envelope (`400`, `401`, `403`, `404`, `405`, `413`, `422`, `429`, `503`, `504`) | yes | yes |
| an **unhandled** failure's envelope (`500`) | yes | **no** — assembled after the middleware that attaches the header has unwound |
| `/healthz`, `/readyz` | **no** — those bodies are `status` and nothing else | yes |
| `/openapi.json` | **no** — that body is the schema | yes |
| an automatic trailing-slash `307` | **no** — that response has no body at all | yes |

The rows are exclusive: an unhandled `500` is the third row and not the first
two, even though it came from a `/v1` endpoint and carries an error envelope.

So for the calls an operator correlates, the body is enough; for a probe, a
schema read or a redirect, read the header. Either way the id a client reports
is what finds the request:

```bash
docker compose -f deploy/docker-compose.yml logs --since 24h wuwaterm-api | grep <request id>
```

`device` is the redacted principal — the same helper the chat adapter uses for
its identifiers — and it is `-` when no credential was verified. It is stable
for a given device, so requests can be attributed to one machine without the
log ever holding the device id itself.

What a record deliberately does not contain, of the things **this service
holds**: no credential, no identifier of an authenticated device other than the
redacted `device` field, no submitted or translated text, and no query string.

The unmatched-target case is the one place a caller's own bytes reach a record,
and the guarantee there is about their **form**, not their content: no target
reaches a record able to carry a terminal escape sequence, forge a field, or run
past the line — inert either because it needed no escaping or because it was
escaped. A caller may put anything in a URL, including strings that look like
this service's own identifiers, and recording those discloses nothing: the
reader of the line learns only what the writer of the request already had. What
the guarantee above covers is what the service knows and the caller does not —
the credential it verified, the principal behind it, the text it translated. (A
device id is in any case the non-secret half of a token; `device revoke` takes
one on the command line and `device list` prints them.)
A target that could itself be a credential — a client or proxy that puts a token
in the URL instead of the `Authorization` header — is replaced entirely by the
literal `credential-shaped`, with no digest of it either: the digest would be
unkeyed here (this container blanks the redaction secret, which belongs to the
bot) and a leaked line would then be a cheap way to test guesses at the secret.

`method` is likewise recorded by membership, not content: the standard verb when
it is one, `other` when it is not. This service publishes `GET` and `POST` (plus
the `HEAD` the framework pairs with `GET` on `/openapi.json`) and refuses every
other verb, so the exact spelling of a refused one tells an operator nothing,
and the field cannot be used to write caller-chosen text into the log.

`route` has three cases, in this order:

1. the request matched a route → its **route template**. An unsupported method
   on a known route counts as matched: the framework picks the route and then
   refuses the method, so a `405` is named by its template like any other.
2. it matched nothing and could be a credential → the literal
   `credential-shaped`, as above.
3. it matched nothing else → what arrived, rendered inert and bounded, because
   that value is chosen by an unauthenticated caller and an operator reads it
   in a terminal. A target that needs no escaping is recorded as it stands. An
   automatic trailing-slash `307` lands here, not in case 1, and so does
   `/openapi.json` — whose rendering happens to be identical to its template.

For the same reason the server's own access log stays off: it prints raw
targets.

Two properties a collector's parser can rely on. Every field is **one
whitespace-delimited token** — an escaped target has its spaces and its `=`
escaped precisely so it cannot become two — so splitting on whitespace and then
on the first `=` is a complete parse **of everything after `request complete`**.
(What precedes it is the timestamp, level and logger name from the log format,
plus those two literal words; none of them are fields.) And a trailing `~` on
the `route` value always means it was shortened — either the target itself or
its rendering hit a bound: `~` is outside the character set a plain value may
use, and an escaped one always ends in its closing quote.

One value in the `status` field is not a status this service ever sends: `499`
means the caller went away, or the work was cancelled at shutdown, before there
was a response. Nothing reached the client, so there is no envelope to correlate
with. A `ClientDisconnect` traceback may follow it — that is the hang-up itself,
not a fault to chase.

**A desktop client's Cancel button does not normally produce one.** What the
service can observe is a connection that goes away *while it is still reading
the request*; a translation body is a short piece of text, delivered long
before anyone can press a button, so there is nothing left to interrupt. The
cancel ends the client's wait and closes the client's own connection, and on
this deployment that hang-up did not reach the service: the request ran to
completion and was recorded with its ordinary status and its full duration —
and, where the request needed the model, the model call included, whose cost is
spent whether or not anyone is still waiting for the answer. (A dictionary hit
returns before the model stage and costs nothing either way; a `kind=` of
`exact`, `fuzzy` or `noop` on the translation line rules the model out, with
the caveats above about the two values that do not rule it in.) Measured here: a
translation cancelled in the client 0.4 s after it started was recorded
`status=200 duration_ms=5183.6`, indistinguishable from one whose answer was
read. Treat `499` as what it says — a caller that went away mid-read, or a
shutdown — and not as a count of cancellations. (A cancel that lands before the
service has the whole request sends nothing it can act on, so that case leaves
no record here, or a `499`, rather than a misleading completion.)

Two consequences for reading these records. A cancelled request is not a
distinct state in the log: what is recorded is what the SERVICE did, not what a
user waited for, so log volume and model spend both count requests nobody read.
And such a request has no id the user can quote — the client never reads the
response the id arrives in — so finding it means bracketing it in time on its
route rather than searching for an id.

On a request that failed unexpectedly, the completion record appears **before**
the traceback rather than after it: the exception passes through the recording
middleware on its way to the handler that renders the `500`. The two are tied
together by the same `request_id`.

`WUWATERM_API_LOG_LEVEL` sets how much is written; `INFO` is the default and is
the level these records are emitted at. `WARNING` keeps the failures and drops
the per-request records. Like the chat adapter's own `WUWATERM_LOG_LEVEL` it is
applied as the process level, so it governs third-party loggers too — except the
HTTP client library, which is held at `WARNING` **or at this level, whichever is
stricter**, because at `INFO` it reports the model endpoint this service was
configured with. (The chat adapter pins that one flat at `WARNING`; here a
quieter setting stays quiet.) It is separate from `wuwaterm-api serve
--log-level`, which is the web server's own startup and socket logging and is
not passed by the Compose `command:` at all.

### Cost Topology

The API's budgets are per process and are NOT shared with the bot:

| Process | Model concurrency | Model calls per minute |
|---|---|---|
| `wuwaterm` (bot) | `WUWATERM_LLM_MAX_CONCURRENCY` (default 4) | per-chat request limit only, plus the linked-channel budget |
| `wuwaterm-api` | `WUWATERM_API_LLM_MAX_CONCURRENCY` (default 2) | `WUWATERM_API_LLM_CALLS_PER_MINUTE` (default 30) |

The worst case for the host is the SUM of the two, never one shared ceiling.
Nothing in either process coordinates with the other; if a single global budget
is ever needed, that is a new mechanism, not a configuration change.

## Runtime-only updates

Use the runtime-only transaction when the application changes but the existing
terminology database already matches the reviewed source's schema and data pin:

```bash
sh deploy/vps-update.sh --runtime-only
```

Run this from the normal deployment checkout after reviewing and merging the
change. The updater requires a clean checkout at freshly fetched `origin/main`.
It updates **both** serving containers to one immutable image, so the existing
manifest's single source/image identity continues to describe the bot and API.
Both services are briefly stopped during the transition. It does not change
Telegram configuration or send a Telegram test message.

Admission requires an existing, consistent pointer, immutable manifest, running
bot/API pair, and read-only data mounts. It rejects missing or mismatched state,
an unsupported database, symlinked database paths, and SQLite journal sidecars.
Database validation uses immutable read-only SQLite access, schema/provenance
and integrity checks, and file identity plus SHA-256 comparisons. The same
database must remain unchanged through activation and recovery.

This mode never runs the builder, refreshes source data, creates a candidate
database, promotes a database, migrates legacy state, or creates a new database
backup. It retains old image identities and the old pointer for recovery.
The new manifest records the unchanged database's actual hash and provenance;
its backup field explicitly denotes that no new database backup was created.
An already deployed identical commit is verified without rebuilding or replacing
its immutable image binding.
An existing local tag without an immutable manifest is rebuilt from the clean
reviewed checkout; a revision label alone is not proof of source provenance.

Full and runtime-only updates share one deployment lock. Runtime-only progress
is recorded durably before operations that may partially succeed. Success
requires both containers to match the new image, readiness and database checks
to pass, and the manifest and pointer to pass readback. An ordinary failure
restores the previous pair and pointer. A recovery failure is reported as
unresolved; it is never described as a successful rollback.
Ordinary failures before the first service-stop attempt leave the old pair
running only after its database, pointer, manifest and runtime binding are
reverified unchanged. Partial stop failures still require rollback. A completed
rollback journal cannot be replayed over a later full deployment.

When an orchestration wrapper advances the checkout, it must acquire that same
root's `.deployments/.deployment.lock` **before** admission, fetch or fast-forward,
and retain it through the updater's exit. Pass the locked open-file description
as FD 9 to each source-mutating child and the updater. The supported shell entry
validates an inherited FD 9 against the canonical root, regular-file type and
device/inode, preserves it without reopening, and still performs nonblocking
`flock`. Foreign or replaced descriptors are refused. With no inherited FD 9,
the updater opens and acquires its own lock as usual. Do not release the wrapper's
lock between checkout advancement and activation, or use a flag to skip locking.

After an interrupted runtime-only transaction, use the explicit recovery entry:

```bash
sh deploy/vps-update.sh --recover-runtime
```

Recovery validates the saved transaction, trusted local tool/Compose binding,
old manifest and images, and unchanged database. It can handle stopped or
partially switched containers; it does not require network access or the latest
remote `main`. Keep the trusted deployment checkout and recovery images intact.
Normal deployment refuses an unresolved transaction until recovery succeeds.
Uncatchable termination or host power loss is not an atomic Docker operation:
the durable record makes the unfinished transition explicit and recoverable.

Recovery never restores or rewrites `terms.db`. If another process changed it,
or the journal/image binding cannot be trusted, stop and resolve that condition
before serving. Do not delete the journal, rewrite a manifest, or manually tag
another image merely to make a precondition pass.

This path activates reviewed runtime code; it does not prove a translation
provider is reliable or establish the cause of an earlier LLM failure.

## State Migration

Older deployments stored `chat_settings.json` and `channel_replies.json` in
`data/` beside `terms.db`. The current runtime keeps `terms.db` read-only under
`data/` and writes runtime state under `state/`.

For a normal live upgrade, run `deploy/vps-update.sh` from the deploy root:

```bash
cd /opt/wuwaterm/current
WUWATERM_DEPLOY_ROOT=/opt/wuwaterm/current sh deploy/vps-update.sh
```

`WUWATERM_DEPLOY_ROOT` defaults to `/opt/wuwaterm/current`. Before any stop the
updater:

1. fetches `origin/main`, requires clean `HEAD == origin/main`, and records the
   full source commit;
2. rebuilds the mutable local builder image from that exact clean checkout,
   then proves the builder container has none of the runtime secret variables;
3. refreshes source data, builds a unique candidate, and runs the strong DB
   verifier against that candidate;
4. builds an immutable `wuwaterm-runtime:<source-commit>` image and verifies its
   `org.opencontainers.image.revision` label.

It then snapshots the old database and commit pointer, tags the old image for
rollback, stops the runtime, promotes the candidate, validates/migrates state,
starts the exact validated image, and runs `scripts/deploy_smoke.py` with
diagnostic message sending explicitly disabled. Only after smoke and running
image-ID checks pass does it create and read back
`.deployments/<source-commit>.json`, then atomically publish `.deploy_commit`.
The manifest contains source commit, image ref/ID/digest/revision, DB SHA-256,
DB source profile/commit/game/resource/changelist, deployment UTC, and backup
path. It is mode read-only and never overwritten with different content.
Re-running the same source commit is accepted only when its image and database
binding is byte-for-byte identical; a different rebuild must use a new source
commit instead of deleting or rewriting the historical manifest.

Any post-promotion state/start/smoke/manifest/pointer failure restores the old
DB, recreates the runtime from the tagged old image, and restores or removes
the commit pointer. Timestamped DB and pointer backups and rollback image tags
are retained; no historical snapshot or state file is deleted.

### Interrupted full updates

A full update mutates the database, both serving containers and the pointer,
so the updater durably records the transaction BEFORE the first
serving-binding mutation. The record at `.deployments/.full-transaction.json`
(schema-versioned, mode 0600, written atomically with fsync) binds:

- the old and new database SHA-256 preimages, the backup path, and the
  backup hash recorded in its filename;
- the old commit pointer and its immutable manifest;
- each serving surface's observed image id, reference and running state,
  plus the per-surface rollback tags created for this transaction;
- the target image ref/id/digest/revision after the image was verified;
- a fingerprint over the deployment shell, compose file, manifest helper and
  recovery module, so a journal written by different tool code is not
  actionable by this code.

An ordinary failure still rolls back in-process through the EXIT trap. An
uncatchable termination (SIGKILL, host power loss) inside the window leaves
that durable record behind; the phase marker alone is never proof of the
outcome. While ANY full-transaction record exists — in any shape, including
one whose marker says `committed` — every deployment mode refuses admission:

```bash
sh deploy/vps-update.sh            # refused: a transaction journal requires recovery
sh deploy/vps-update.sh --runtime-only   # refused likewise
```

The explicit recovery entry classifies the host by what is actually observed
— pointer, live database hash, manifest, and both containers' image
ids/references/running states — never by the journal's phase marker alone:

```bash
sh deploy/vps-update.sh --recover-full
```

- Observed NEW binding complete (pointer, database, manifest, both surfaces
  on the target image id and reference): the transaction is confirmed and
  the record is cleared. No container is stopped and nothing is restored.
- Observed OLD binding: the recorded previous binding is restored — known
  surfaces are quiesced and verified stopped first, the recorded backup is
  re-hashed before any copy and re-verified after promotion, only surfaces
  that were recorded running come back, each on its own recorded image,
  and the old binding is verified from observation before the record is
  cleared.
- Anything else — unknown pointer, unknown live database, new pointer with
  old or missing services, missing or modified backup, unconfined recorded
  paths, schema/type drift, tool fingerprint mismatch — is reported
  unresolved, changes nothing further, and KEEPS the record, so recovery is
  reentrant. A missing live database is restored from the recorded verified
  backup; a first deploy (no recorded old binding) restores the recorded
  absence and starts nothing.

Before any of that, recovery establishes an admissibility preflight that
mutates nothing: every journal-referenced path (backup, old manifest) is
confined and consistent with the recorded preimage; every observed surface
must be one of this transaction's known identities — the recorded old
binding, the per-surface rollback binding, or the target binding, in any
running state, with absence only when the daemon positively proves it; and
every recorded rollback reference must resolve to the exact per-surface old
image id recorded for it, re-resolved immediately before use; drift observed
at either check blocks execution. The check and the Docker operation are not
atomic with manual changes outside the deployment lock. An inspection that cannot positively
prove absence (daemon, transport, permission, or malformed output) makes
the host unobservable: recovery refuses, the database does not move, and
the record is kept. The shell's EXIT rollback restores the database through
the same verified restoration as explicit recovery (`restore-db`): the
backup and the copied bytes are hashed BEFORE the live file can be
replaced, so a backup modified after preparation leaves the known live
database untouched.

The EXIT-trap rollback drops the record only through the same observed
old-binding verification, so a rollback that cannot prove its outcome keeps
the record instead of certifying it. Stopped and absent are distinct object
states: explicit recovery recreates a recorded stopped service on its verified
old image without starting it, or removes only a known stopped replacement when
no old object existed (without force or volume removal). Final verification
checks presence, image, reference and running state; an unresolved object keeps
the journal even after an ordinary EXIT rollback. The old manifest is verified
with its own recorded image ID, not an arbitrary first service when images differ.
Recovery never rewrites an unknown database, never starts a surface that was not
recorded running, and never deletes an unconfined journal-referenced file.
Keep the recorded backup,
the old manifests and the rollback image tags until recovery has completed.

Like the runtime-only record, this machinery is exercised against isolated
fake-docker harnesses with real durable file operations and a real
process-group SIGKILL at the promotion window; it is not a claim about real
host power-loss behaviour.

For a state-only migration using an existing `terms.db`, let the new runtime
perform the same validated, atomic one-time migration during startup:

```bash
cd /opt/wuwaterm/current
docker compose -f deploy/docker-compose.yml stop wuwaterm
mkdir -p state
docker compose -f deploy/docker-compose.yml up -d --build wuwaterm
```

Do not manually copy these files while the old bot is running. Do not delete or
pre-create `state/*.json`: an existing target is deliberately never overwritten.

If the existing `.env` explicitly sets either state file path to the old data
directory, remove those lines or update them to:

```bash
WUWATERM_SETTINGS_PATH=state/chat_settings.json
WUWATERM_CHANNEL_REPLY_INDEX_PATH=state/channel_replies.json
```

The Compose runtime also overrides these paths to `/app/state/...` so an old
`.env` cannot make the read-only runtime write under `/app/data`.

Startup performs the one-time copy when `WUWATERM_STATE_DIR` is set,
the new file is missing, and the legacy DB-adjacent file exists. This includes
old explicit settings that still point at the DB-adjacent files. It never
overwrites an existing file in `state/`; copy failure stops startup instead of
silently dropping the allowlist, public-mode state, or channel reply index.

## Deployment Smoke

After the service starts, use `scripts/deploy_smoke.py` as a deployment
reachability check. It verifies `getMe`, and when `TELEGRAM_TEST_CHAT_ID` is set
it sends one diagnostic message without printing the token or chat id. See
[Validation](validation.md) for live smoke caveats.

## Traceability Readback

After an owner-authorized deployment, read the pointer and its immutable
manifest together:

```bash
cd /opt/wuwaterm/current
cat .deploy_commit
python3 -m json.tool ".deployments/$(cat .deploy_commit).json"
docker inspect --format '{{.Image}}' wuwaterm-bot
docker inspect --format '{{.Image}}' wuwaterm-api
sha256sum data/terms.db
docker compose -f deploy/docker-compose.yml exec -T wuwaterm-api \
  python -c "import os, urllib.request; port = os.environ.get('WUWATERM_API_PORT', '8788'); print(urllib.request.urlopen('http://127.0.0.1:' + port + '/readyz', timeout=10).status)"
```

The pointer must equal the intended source SHA exactly; BOTH running image IDs
and the DB hash must match the manifest, and the health check must print
`200`. These are runtime evidence only when read
from the actual VPS after deployment. Local tests and failure injection are
offline/deployment validation, not proof that production changed.
