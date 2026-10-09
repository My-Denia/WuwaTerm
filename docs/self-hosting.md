# Self-Hosting

This is the generic path: how a stranger runs this service on their own machine.
It makes no assumption about the operating system beyond Linux with a container
runtime, and it names no host, path or reverse proxy that belongs to anyone in
particular. The owner's own production runbook — a specific host, a specific
directory, a transactional updater — is [Deployment](deployment.md), and it is
not a prerequisite for anything below.

## What You Get, And What You Still Have To Do

You get a dictionary-first Wuthering Waves terminology service: exact
Chinese-to-English term lookup and the reverse, plus term-locked sentence
translation in both directions, reachable through a Telegram bot, a versioned
HTTP API, or both. An exact dictionary hit returns the official string
byte-for-byte and never calls a model.

You still have to do these things yourself, because this project cannot do them
for you:

- **Build the terminology database.** It is derived locally from a public
  upstream data repository pinned in `src/wuwaterm/constants.py`. It is never
  distributed — not in a release, not in a container image. A fresh install has
  no database until you build one.
- **Bring your own Telegram bot token**, if you want the bot surface.
- **Bring your own OpenAI-compatible endpoint**, if you want sentence
  translation. Exact dictionary lookups work without it; see
  [With no model configured](#with-no-model-configured) for exactly what a
  request that would have needed a model does instead.
- **Terminate TLS yourself.** The API binds a loopback address and refuses to
  bind anything else. Publishing it is your reverse proxy's job.
- **Issue your own device credentials.** There is no registration endpoint and
  no sign-up.

**How long this takes.** A first install, from the checkout to the first exact
lookup, is a few minutes, and most of that is the upstream data fetch rather
than anything this project does — roughly 1.2 GB over the network against a
handful of seconds of local work. A clean-room run of this document on a fast
link spent about a minute and a half of machine time on the whole path; the same
run took about eight minutes of wall time because the fetch failed twice and was
re-run. Budget for the link, not for the build.

## Requirements

Either path works; pick one.

| | Container path | Source path |
| --- | --- | --- |
| Host | Linux with Docker and Compose v2 | Linux or macOS with Python 3.11 or newer; on Windows, inside WSL |
| Python | supplied by the image (`python:3.11-slim`) | 3.11+ (`requires-python >=3.11`), **plus a way to create a virtual environment**: either the standard library `venv` module with pip, which on Debian and Ubuntu is the separate `python3-venv` package rather than part of `python3`, or [uv](https://docs.astral.sh/uv/), which needs neither. Both forms are written out below. |
| Disk | about 2 GB for the upstream data checkout, plus images | about 2 GB for the upstream data checkout |
| Git | needed for the source checkout | needed for the checkout and for the data refresh |

Optional, for either path:

- A **Telegram bot token** if you want the bot. Create the bot with Telegram's
  own BotFather; this project has no part in that step.
- An **OpenAI-compatible endpoint** (base URL, API key, model name) if you want
  sentence translation.

Every command in this document is written for a POSIX shell, and the source
path assumes one: the virtual environment's interpreter is at
`.venv/bin/python`, and `chmod` decides who can read your credentials. On
Windows that path is WSL, not the Windows interpreter — which is also where the
server's own test suite is supported. See [Support Matrix](support-matrix.md)
for what is tested where.

## Get The Source At A Release Tag

Both paths need the source, because the Compose files, the entrypoints, the
data-build commands and the verification scripts all live in it.

```bash
git clone --branch vX.Y.Z --depth 1 https://github.com/My-Denia/wuwa-translate-bot.git
cd wuwa-translate-bot
```

Replace `vX.Y.Z` with a published tag from
[Releases](https://github.com/My-Denia/wuwa-translate-bot/releases). Deploying
from a moving branch is possible and is a worse idea than it looks: the data pin
and the schema version travel with the source, so a tag is what makes a working
install describable later.

For the source path, create the environment and install the API extra:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e ".[api,build]"
```

If the first line fails with `ensurepip is not available`, this host's Python
has no `venv` module — a stock Ubuntu or Debian install does not carry one, and
that message is what it looks like. Either install it
(`sudo apt install python3-venv`, or the version-specific package the message
names), or use uv, which supplies its own interpreter and pip and needs no
system package at all:

```bash
uv venv --seed .venv
.venv/bin/python -m pip install -e ".[api,build]"
```

Either way the environment ends up at `.venv/` and every later command in this
document works unchanged.

`api` carries FastAPI and uvicorn; `build` carries what the data build needs.
The Telegram bot needs neither extra beyond the base install.

## Configure

```bash
cp deploy/env.example .env
chmod 600 .env
```

`.env` is git-ignored and is excluded from the container build context. Edit it
before the first start. The variables are documented inline in the file; these
are the ones that decide whether each surface works at all.

**Nothing in this project reads that file for you on the source path.** Compose
loads it for the container services through its own `env_file` directive, but
the command-line entry points read the process environment directly — there is
no dotenv loader anywhere in this codebase. So on the source path, export it
into the shell you start a service from, or the bot will fail to start for want
of a token and the API will quietly run on its defaults:

```bash
set -a
. ./.env
set +a
```

**That line evaluates the file as shell**, which is the catch: a value
containing a space, a dollar sign, a backtick, a semicolon or a quote is not
read as data, and a command substitution in it would run. So either quote every
value in single quotes, or do not load it this way. A process supervisor is the
better answer for a real installation — systemd's `EnvironmentFile=` and the
equivalents elsewhere parse the file as data rather than executing it, which is
also how Compose reads it for the container path. Either way, the variables have
to reach the process; nothing in this project puts them there.

If you run both surfaces on the source path, do not export one file into both:
[Start The Service](#start-the-service) shows how to give each process only its
own secrets, which is what the Compose file already does for the container path.

**For the Telegram bot:**

| variable | why it matters |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Without it the bot cannot start. |
| `OWNER_USER_ID` | Who may use the bot in a private chat. Empty means private chat rejects everyone, which is the fail-closed default. |
| `WUWATERM_STATE_DIR` | Where `chat_settings.json` and the channel reply index live. The example file sets it to `state`, and that is what the rest of this document assumes. It is **not** what an unset variable does: with nothing set, those two files fall back to sitting beside the database instead, which is the pre-`state/` layout and would put them where the backup section does not look. Leave the example's value in place. |

**For the HTTP API:**

| variable | why it matters |
| --- | --- |
| `WUWATERM_DB_PATH` | The terminology database the service reads. Default `data/terms.db`. |
| `WUWATERM_API_PORT` | The loopback port. Default `8788`. Under Compose the bind address itself is fixed in the Compose file and is deliberately not an environment knob. |
| `WUWATERM_API_STATE_DIR` | Where the device credential store lives. Default `state-api`, a **sibling** of the bot's state directory and never a child of it, because the bot mounts its own state directory writable. |
| `WUWATERM_API_RATE_LIMIT_PER_MINUTE`, `WUWATERM_API_LLM_CALLS_PER_MINUTE`, `WUWATERM_API_MAX_BODY_BYTES`, `WUWATERM_API_REQUEST_TIMEOUT_SECONDS` | Per-process admission and spending bounds. They are not shared with the bot; the documented worst case is the sum of the two processes. |

**For sentence translation (optional, shared by both surfaces):**
`WUWATERM_OPENAI_BASE_URL`, `WUWATERM_OPENAI_API_KEY`, `WUWATERM_OPENAI_MODEL`,
plus the timeout and concurrency bounds beside them.

**For the owner-private web layer (optional, off by default):**
`WUWATERM_API_WEB_ENABLED`, `WUWATERM_API_WEB_DEVICE_TOKEN`,
`WUWATERM_API_WEB_EDGE_SECRET`, `WUWATERM_API_WEB_SESSION_TTL_SECONDS`,
`WUWATERM_API_WEB_MAX_SESSIONS`. Leave `WUWATERM_API_WEB_ENABLED` unset unless
you have read [The owner-private web presentation layer](web-presentation-layer.md):
the layer expects a reverse proxy in front of it that both authenticates the
visitor and injects the edge marker, and it refuses every request that arrives
without that marker. With the switch off there is no route and no
sub-application at all.

Note where those values land: if one environment file feeds several processes,
the web variables reach processes that have no web surface. The Compose file in
this repository blanks all three in the bot service for exactly that reason. If
you deploy some other way, reproduce that.

## Build The Terminology Database

Three steps, always in this order: fetch the pinned upstream data, build a
candidate database, verify it. The refresh is fail-closed by design — it checks
that the checkout is the pinned commit of the pinned repository and that the
upstream version-provenance file says the version the profile expects, and it
stops rather than building from something else.

**Container path.** Create the bind-mounted directories yourself first, before
any Compose command touches them. Docker creates a missing bind source as root,
and the builder then fills a root-owned `data/` with root-owned artifacts —
after which the promotion step below, run as you, cannot rename a file in it.
Both directories are git-ignored, so a fresh clone has neither:

```bash
mkdir -p data state state-api
chmod 700 state-api
```

Then build:

```bash
docker compose -f deploy/docker-compose.yml run --rm wuwaterm-builder refresh-data
docker compose -f deploy/docker-compose.yml run --rm -e WUWATERM_DB_PATH=/app/data/terms.candidate.db wuwaterm-builder build-db --atomic
docker compose -f deploy/docker-compose.yml run --rm -e WUWATERM_DB_PATH=/app/data/terms.candidate.db wuwaterm-builder verify-db
```

**Source path:**

```bash
.venv/bin/python -m wuwaterm.cli refresh-data --dest data/wutheringdata --profile arikatsu
.venv/bin/python -m wuwaterm.cli build-db --data-dir data/wutheringdata --db data/terms.candidate.db --profile arikatsu --atomic
.venv/bin/python scripts/verify_db.py data/terms.candidate.db --profile arikatsu --require-free-text-exclusions --require-free-text-zh-exclusions
```

On either path the refresh fetches roughly a gigabyte over the network and can
fail part-way on a slow or unreliable link. Re-run the same command if it does:
an existing
checkout is resumed rather than discarded, so a retry after a transient failure
is usually much faster than the first attempt.

`verify_db.py` checks integrity, the schema, the recorded source provenance
against the profile, the required categories, representative exact hits, and
the free-text lock exclusion lists the build records — the English
`free_text_lock_exclusions` and Chinese `free_text_lock_exclusions_zh` keys
(`--require-free-text-exclusions` and `--require-free-text-zh-exclusions`
make a candidate without them fail; the builder image's `verify-db` always
passes both). It prints the category counts, the recorded provenance and the
number of excluded surfaces and nothing else — there is **no explicit PASS
line**, so the exit status is the verdict: zero means verified. On a first install, once it passes, put the candidate in place:

```bash
mv data/terms.candidate.db data/terms.db
```

On an install that is already serving, do not do that by hand while the service
is running — stop it first, or use the transactional updater described in
[Deployment](deployment.md).

## Start The Service

**Container path.** Name the service you want. Both are ordinary services in
the Compose file, so a bare `up -d` starts both — and a bot with no token would
then fail and be restarted forever by its restart policy on an API-only
installation:

```bash
docker compose -f deploy/docker-compose.yml up -d wuwaterm-api
docker compose -f deploy/docker-compose.yml up -d wuwaterm
docker compose -f deploy/docker-compose.yml up -d wuwaterm wuwaterm-api
```

The three lines are alternatives: the API alone, the bot alone, or both.

**Source path**, one process each — and they are two long-running processes,
not two lines of one script. Start each in its own terminal, or put each under
a process supervisor; run in one shell, the second command waits for the first
to exit.

```bash
.venv/bin/python -m wuwaterm.cli bot --db data/terms.db
.venv/bin/python -m wuwaterm_api.cli serve
```

**Give each process only its own secrets.** One `.env` exported into one shell
puts every value into both processes: the bot would hold the web layer's device
token and edge marker, and the API would hold the Telegram token, the owner id
and the bot's log-redaction key — none of which either one uses. That is not a
detail of taste; it is what the Compose file already does for you, blanking
three variables in the bot service and five in the API service, and a source
installation has to reproduce it. Two ways, both fine:

Separate files, one per process — `.env.bot` and `.env.api`, each at mode 600,
each holding only what that process needs (the shared ones — the database path,
the data directory, the model settings — appear in both):

```bash
( set -a; . ./.env.bot; set +a; exec .venv/bin/python -m wuwaterm.cli bot --db data/terms.db )
( set -a; . ./.env.api; set +a; exec .venv/bin/python -m wuwaterm_api.cli serve )
```

Or one file, with the other process's variables unset at the point of launch —
the same list Compose blanks:

```bash
( set -a; . ./.env; set +a
  unset WUWATERM_API_WEB_ENABLED WUWATERM_API_WEB_DEVICE_TOKEN WUWATERM_API_WEB_EDGE_SECRET
  exec .venv/bin/python -m wuwaterm.cli bot --db data/terms.db )

( set -a; . ./.env; set +a
  unset TELEGRAM_BOT_TOKEN TELEGRAM_TEST_CHAT_ID OWNER_USER_ID WUWATERM_REDACTION_SECRET WUWATERM_API_DEVICE_DB_PATH
  exec .venv/bin/python -m wuwaterm_api.cli serve )
```

A supervisor unit does the same with its own environment-file directive: point
each unit at its own file, or at the shared one plus that unit's unsets.

The API binds `127.0.0.1` and port `8788` unless you say otherwise, and it
refuses to bind a non-loopback address. Check it:

```bash
curl -sS http://127.0.0.1:8788/healthz
curl -sS http://127.0.0.1:8788/readyz
```

`/healthz` answers `{"status":"ok"}` as soon as the process is up. `/readyz`
answers `{"status":"ready"}` only when the terminology database is readable, and
`503` with an `internal` error code when it is not. Neither endpoint needs a
credential; every `/v1` route does.

## Issue The First Device Credential

There is no registration endpoint. The operator issues credentials, and the
secret is read from standard input so it is never printed and never appears in
a process listing.

**You create the secret, and the store has rules for it.** It has to be at
least **32 characters** and printable ASCII with no spaces and no control
characters, because it is presented as an HTTP header value and a credential
that cannot be sent is worse than none; anything shorter is refused as too
little material. Generate it into a file that only you can read, rather than
typing it or pasting it into a shell:

```bash
( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > /path/to/secret )
```

That gives 43 URL-safe characters. Delete the file once the token is in the
desktop client's credential store: the service keeps only a salted scrypt
verifier and cannot give the secret back to you or to anyone else.

**Container path.** A one-shot container of the API service. On a fresh
install with one runtime image this is enough; on a host that carries several
runtime image tags, pin the reference to the image the service is actually
running, because an unpinned run can start a stale image whose refusal reads
like a broken runbook — the resolution is written out in
[Deployment](deployment.md).

```bash
docker compose -f deploy/docker-compose.yml run --rm -T wuwaterm-api device issue --name "my desktop" < /path/to/secret
```

**Source path:**

```bash
.venv/bin/python -m wuwaterm_api.cli device issue --name "my desktop" < /path/to/secret
```

Both accept `--scopes translate,meta`, which is also the default: `translate`
admits `POST /v1/translations` and `POST /v1/reviews`, `meta` admits
`GET /v1/terms` and `GET /v1/meta`. The command prints the device id, the name, the scopes and the
creation time — nothing secret. The token you hand to the desktop client is:

```
wtd1.<device id>.<the secret you supplied>
```

Only a salted scrypt verifier is stored, in `state-api/devices.db` — a `0600`
file in a `0700` directory, together with its write-ahead log. The other two
subcommands are `device list` and `device revoke --device-id <id>`; revoking
keeps the row and stamps a revocation time, so a withdrawal stays auditable.
Deleting `state-api/devices.db` revokes every device at once and the next start
recreates an empty store.

Where `state-api/` comes from differs by path, and only one of them asks
anything of you:

- **Container path.** It is the directory you created in
  [Build The Terminology Database](#build-the-terminology-database) — before any
  Compose command ran, so that it belongs to you and not to root. If you skipped
  that step, the directory Docker created is root-owned and mode 0700, and no
  host-side backup or restore below can read it; fix the ownership before going
  on.
- **Source path.** There is no such step and none is needed: the service creates
  `state-api/` itself at first start, mode `0700`, owned by whoever runs it.

## First Lookup, First Translation

Both `/v1` routes take the token as a bearer credential. `request_id` is a fresh
hex identifier on every response and is also returned in the `X-Request-Id`
header; the values below are illustrative.

An exact dictionary lookup — no model involved, at any setting:

```bash
curl -sS "http://127.0.0.1:8788/v1/terms?q=%E4%BB%8A%E6%B1%90" \
  -H "Authorization: Bearer wtd1.<device id>.<secret>"
```

```json
{
  "query": "今汐",
  "matches": [
    {"zh": "今汐", "en": "Jinhsi", "category": "resonator", "score": 100.0, "reason": "exact"},
    {"zh": "今汐", "en": "Jinhsi", "category": "echo", "score": 100.0, "reason": "exact"},
    {"zh": "今汐", "en": "Jinhsi", "category": "speaker", "score": 100.0, "reason": "exact"}
  ],
  "request_id": "3f7c1a9e5b2d4c8a9e0f1b2c3d4e5f60"
}
```

**Several matches for one exact term is normal, not a duplicate.** Matches are
distinct on the triple of Chinese string, English string and category, so one
query can legitimately return several. The response above is one shape of that:
this particular term exists as a resonator, an echo and a speaker, and its
English string happens to be the same in all three. It is not the only shape.
When the upstream data records more than one official English string for a term,
those come back as separate matches **inside one category** — at the 3.7 pin,
`守岸人` returns both `Shorekeeper` and `The Shorekeeper` in `resonator`. A reverse
(English) query can likewise return several Chinese strings — `Suisui` is both
`穗穗` and `穗穗（通讯中）`, both `speaker`. So read `zh`, `en` and `category`
together, and do not treat a second match as a duplicate of the first: on an
ambiguous term it is the other official answer.

When there is no exact candidate, the same endpoint returns the dictionary
service's backend-ranked fuzzy candidates (up to five) with their scores and
reasons. For example, pinyin `jinxi` can return `今汐 -> Jinhsi` with reason
`pinyin`. Clients must preserve the returned order and must not independently
rescore or deduplicate the list.

A sentence, which reaches the model only after the known terms in it have been
locked:

```bash
curl -sS http://127.0.0.1:8788/v1/translations \
  -H "Authorization: Bearer wtd1.<device id>.<secret>" \
  -H "Content-Type: application/json" \
  -d '{"text": "今汐装备了声骸", "to": "en"}'
```

```json
{
  "kind": "llm",
  "text": "Jinhsi equipped an Echo.",
  "direction": "en",
  "dictionary_miss": false,
  "request_id": "b41d9e07c3a24f15ae6d8c2b0f739e5a"
}
```

`kind` says which stage answered: `noop` (nothing translatable), `exact` (an
official dictionary hit), `fuzzy` (a trusted pinyin hit) or `llm`. `to` may be
`"en"`, `"zh"`, or omitted to auto-detect from the source text.

Every failure uses one envelope:

```json
{"error": {"code": "unauthorized", "message": "..."}, "request_id": "..."}
```

The codes are `unauthorized`, `forbidden`, `rate_limited`, `payload_too_large`,
`invalid_request`, `input_too_long`, `llm_unavailable`, `llm_budget_exhausted`
and `internal`.

### With no model configured

Leaving `WUWATERM_OPENAI_BASE_URL` and its siblings empty is a supported way to
run this. What changes is narrow:

- An **exact dictionary hit still answers**, on every surface. That path never
  calls a model.
- A request that would have needed the model is **refused rather than
  answered**: `POST /v1/translations` returns HTTP `503` with the error code
  `llm_unavailable` and the message `no translation model is configured`. The
  pipeline could have returned the source text with official terms substituted,
  which is a reasonable fallback in a chat window but over HTTP would look like
  a successful translation, so this surface declines to pretend.

## Publish It Over HTTPS

**The requirement, not the recipe:** the API binds a loopback address and
refuses anything else, so nothing outside the host can reach it until you put a
TLS terminator in front of it. The desktop client verifies certificates and
cannot be told not to; plaintext `http://` is accepted by it only for loopback.
So the terminator must serve a valid certificate for a name you control, and it
must pass the request through to the loopback port unchanged, including the
`Authorization` header.

Any of the usual terminators does this. Below is **one minimal example**, not a
recommendation and not the only shape that works:

```nginx
location /wuwaterm-api/ {
    proxy_pass http://127.0.0.1:8788/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Two things to get right whatever you use:

- Decide whether the public prefix is stripped before the request reaches the
  service, and make the client's configured base address agree with that
  decision. A base address that resolves to no route answers `404`, which reads
  like a broken deployment and is not one.
- Do not publish anything the surface does not need. The service declares only
  the paths in `docs/api/openapi.json` at your checkout; a rule that forwards a
  whole host to it publishes more than that.

The owner's own installation uses one particular terminator with a path route,
and the exact block, the validation command and the rollback for it are in
[Deployment](deployment.md). Treat that as one worked example rather than as the
supported configuration.

## Optional: Container Images From GHCR

From v0.4.0 onward there are two published images:
`ghcr.io/my-denia/wuwaterm` (runtime) and `ghcr.io/my-denia/wuwaterm-builder`
(builder). They exist to save the local image build; they do not replace the
source checkout, because the Compose files, the entrypoints, the data build and
the verification scripts are in the source.

Verify that the pull succeeds before you plan around it. If it is denied, build
from source — that is the path the rest of this document already describes, and
nothing here depends on the images.

```bash
docker pull ghcr.io/my-denia/wuwaterm:v0.4.0
docker pull ghcr.io/my-denia/wuwaterm-builder:v0.4.0
```

To use them, point the runtime services at the pulled image instead of the
locally built one. The Compose file already reads the runtime reference from an
environment variable:

```bash
WUWATERM_RUNTIME_IMAGE=ghcr.io/my-denia/wuwaterm:v0.4.0 \
  docker compose -f deploy/docker-compose.yml up -d --no-build wuwaterm-api
```

Name the service here for the same reason as above: without it, an API-only
installation starts the bot too, and a bot with no token fails and is restarted
forever. Substitute `wuwaterm` for the bot alone, or list both.

**That variable has to survive the command.** Set only on that one line, it is
gone from the next one — and every later Compose command, including a restart
you run months from now, falls back to the local tag and quietly starts a
different image from the one you meant to deploy. Put it in `.env` instead, so
Compose reads it every time, and update it deliberately at each upgrade:

```bash
WUWATERM_RUNTIME_IMAGE=ghcr.io/my-denia/wuwaterm:v0.4.0
```

If you would rather not keep it there, then carry the prefix on *every* Compose
command that starts, recreates or runs a container — not only the first one.

**The builder image has its own variable.** It is
`WUWATERM_BUILDER_IMAGE`, and it defaults to the locally built
`wuwaterm-builder:local`, so nothing changes for an installation that builds
its own images. Put it in `.env` next to the other one when you want the
pulled builder instead:

```bash
WUWATERM_BUILDER_IMAGE=ghcr.io/my-denia/wuwaterm-builder:v0.4.0
```

**Or set both at once with the shipped overlay.**
`deploy/docker-compose.ghcr.yml` sets nothing but the three `image:` fields,
from one variable:

```bash
WUWATERM_IMAGE_TAG=v0.4.0 docker compose \
  -f deploy/docker-compose.yml -f deploy/docker-compose.ghcr.yml \
  up -d --no-build wuwaterm-api
```

Pass the base file first; the overlay's values win. It does not remove the
base file's `build:` sections — an overlay cannot — so what keeps Compose from
building is not asking it to: `--no-build` on `up`, or simply never running
`build`. It does not repeat `profiles:` either, so the builder service stays
behind the `builder` profile and still has to be named or profiled to run:

```bash
WUWATERM_IMAGE_TAG=v0.4.0 docker compose \
  -f deploy/docker-compose.yml -f deploy/docker-compose.ghcr.yml \
  run --rm wuwaterm-builder refresh-data
```

The overlay refuses to expand an unset `WUWATERM_IMAGE_TAG` rather than
defaulting to something, because a default here would be a silently wrong
image; and the same warning as above applies to it, so put the tag in `.env`
if you do not want to carry it on every command.

## Upgrade

Move the source first, on either path:

```bash
git fetch --tags
git checkout vX.Y.Z
```

Then bring the runtime up to that source. **Container path** — name the builder
too. It sits behind a Compose profile, so a bare build skips it, and the data
commands below would then refresh and verify the new release's data with the
previous release's builder code, which is exactly what a release that moves the
pin or the schema version breaks:

```bash
docker compose -f deploy/docker-compose.yml build wuwaterm wuwaterm-api
docker compose -f deploy/docker-compose.yml --profile builder build wuwaterm-builder
```

**Source path** — the dependencies move with a release too, so reinstall them
into the virtual environment rather than only restarting:

```bash
.venv/bin/python -m pip install -e ".[api,build]"
```

Then rebuild and re-verify the data before restarting, because a release can
move the data pin or the schema version:

```bash
docker compose -f deploy/docker-compose.yml run --rm wuwaterm-builder refresh-data
docker compose -f deploy/docker-compose.yml run --rm -e WUWATERM_DB_PATH=/app/data/terms.candidate.db wuwaterm-builder build-db --atomic
docker compose -f deploy/docker-compose.yml run --rm -e WUWATERM_DB_PATH=/app/data/terms.candidate.db wuwaterm-builder verify-db
```

On the source path the same three steps are the source-path build commands from
[Build The Terminology Database](#build-the-terminology-database).

Stop the services, promote the verified candidate over `data/terms.db`, start
again — on the source path that means starting the two processes again, in
their own terminals or under their supervisor — and re-check `/readyz`. Take
the backup below first: promotion is the one step in this document that
overwrites something.

An installation old enough to predate the separate `state/` and `state-api/`
directories needs a one-time state migration rather than a restart; that path,
including the requirement to stop the old runtime first, is the state-migration
section of [Deployment](deployment.md).

## Backup And Restore

Three things are worth backing up, and one is not.

| what | how |
| --- | --- |
| `.env` | Copy it. It holds every credential the service is given. |
| `state/` | Bot settings and the channel reply index. Plain JSON; copy it while the bot is stopped. On the container path these files are written by the bot process, which runs as root in the shipped image and restricts them, so the copy needs the same privileges the credential store below does — `sudo` on the host, not a one-shot container, whose entrypoint takes only the three service commands. Pre-creating `state/` keeps the directory yours and not the files in it. |
| `state-api/` | The device credential store. **A SQLite database in write-ahead-log mode.** Stop the API first, or take an online backup through SQLite's own backup API. Copying the file while the process holds it open can silently lose the most recent commits. |
| `data/terms.db` | **Not worth backing up.** It is regenerable from the pinned upstream source with the build commands above, and rebuilding it is the only way to be sure of what is in it. |

An online backup of the credential store, without stopping anything, using
SQLite's own backup call — here through the `sqlite3` command-line shell, which
is not a dependency of this project and may need installing:

A backup of the credential store is as sensitive as the store: it carries the
same verifiers, and a copy left world-readable in a working directory undoes
the 0600 the service was careful about. Create the destination closed, and
close the file itself:

```bash
mkdir -p backup
chmod 700 backup
rm -f backup/devices.db.new
( umask 077 && sqlite3 -readonly state-api/devices.db ".backup 'backup/devices.db.new'" ) \
  && chmod 600 backup/devices.db.new \
  && mv backup/devices.db.new backup/devices.db
```

**Without the `sqlite3` shell**, the same backup call is in the Python standard
library — `sqlite3.Connection.backup` — so no package has to be installed for
it. **This is the source-path form**, and it uses the environment you built in
[Get The Source At A Release Tag](#get-the-source-at-a-release-tag) rather than
a system interpreter, because that environment is the one this document
guarantees: the uv route supplies its own interpreter and a host may have no
`python3` at all.

```bash
mkdir -p backup
chmod 700 backup
rm -f backup/devices.db.new
( umask 077 && .venv/bin/python -c "import sqlite3; src=sqlite3.connect('file:state-api/devices.db?mode=ro', uri=True); dst=sqlite3.connect('backup/devices.db.new'); src.backup(dst); dst.close(); src.close()" ) \
  && chmod 600 backup/devices.db.new \
  && mv backup/devices.db.new backup/devices.db
```

**The container path offers no interpreter of its own for this.** Its
requirements promise Docker and Compose and nothing else; the Python that runs
this service lives inside the image, and the runtime entrypoint accepts only
`bot`, `api` and `device` — it refuses an arbitrary command by design. A
one-shot container can still reach that interpreter if you override the
entrypoint and mount a destination yourself (`docker compose run --rm
--entrypoint python -v "$PWD/backup:/backup" wuwaterm-api -c ...` with the
standard-library backup call above), but that is a recipe you own, not one the
Compose file provides. The path that needs no improvising: stop the API
(`docker compose -f deploy/docker-compose.yml down`) and copy `state-api/`,
which is exactly what the table above allows. A stopped database has nothing
in flight to lose, and that is the whole reason the online form exists in the
first place.

**Both forms open the source read-only, and that is not decoration.** SQLite
CREATES a database file it is given and does not find. Aim either command at a
store that does not exist yet — before the first credential is issued — or at
the wrong state directory, and without that flag it would succeed, leaving a
valid-looking backup of an empty database and a loss you would meet during a
restore. Opened read-only, a missing source is an error at once.

**Both blocks write to a new name and rename only on success, chained with
`&&`.** That is not tidiness either. A backup that fails must leave the
PREVIOUS backup untouched and must exit non-zero, so that whatever runs this —
you, or a scheduler — cannot mistake a stale file for a fresh one. An earlier
revision of this page reported the failure with a message instead, which turned
a failed backup into an exit status of zero; a message a human might read is
not a status a machine can act on.

The Python form was **measured** both ways here, including against a live
database with an uncheckpointed write-ahead log, where the read-only open still
returns the newest commit. The `-readonly` flag on the shell form is the same
protection expressed in that program's own option; this document's clean room
did not have the `sqlite3` shell installed, so treat that half as reasoned
rather than measured, and check the exit status the first time you run it.

Both forms take the same online backup and need the same privileges. Use
whichever is available; do not substitute a plain file copy for either.

The `umask` makes the new file 0600 as it is created; the explicit `chmod`
after it is there because a shell that inherits a different umask, or a
`sqlite3` build that copies the source file's mode, would otherwise decide
that for you. The same applies to whatever you copy `state/` and `.env` into.

**On the container path that command needs the file's own privileges.** The
runtime image selects no unprivileged user, so the API process creates
`devices.db` as root and then restricts it to mode 0600 — pre-creating the
directory keeps the DIRECTORY yours, but not the database inside it. Run the
backup with the same privileges the container has — `sudo` on the host — or
give the store a matching non-root owner. Reaching for a one-shot container of
the API service instead does not work: its entrypoint accepts `bot`, `api` and
`device` and refuses anything else, on purpose. The source path, where the API
runs as you, needs none of that.

Verify a restored credential store by a business count — `device list` should
show the devices you expect — rather than by an integrity check alone: an
integrity check passes on a database that is intact but stale.

To restore: stop the services, put `.env`, `state/` and `state-api/` back with
their original permissions (`.env` at `600`, `state-api/` at `700`), rebuild
`data/terms.db` from the pinned source if it is missing, and start again. On an
API-only install there is nothing in `state/` to restore: that directory belongs
to the bot process, and a bot that never ran wrote nothing into it. On the
source path it will usually not exist at all; on the container path it exists
and is **empty**, because the build step above pre-creates it along with the
other bind mounts. Either way, restore `.env` and `state-api/` and skip it — an
absent or empty `state/` on an API-only install is the expected state, not a
missing backup.

## Rollback

Stop what is running — `docker compose -f deploy/docker-compose.yml down` on
the container path, or stop the two processes on the source path — then take
the source back and rebuild the runtime for the path you are on:

```bash
git checkout vPREVIOUS
docker compose -f deploy/docker-compose.yml build wuwaterm wuwaterm-api
docker compose -f deploy/docker-compose.yml --profile builder build wuwaterm-builder
```

```bash
.venv/bin/python -m pip install -e ".[api,build]"
```

The first block is the container path, the second the source path. The builder
is named here for the same reason as in the upgrade above: it is behind a
profile, a bare build skips it, and the database you are about to rebuild at
the previous tag's pin has to be built by that tag's builder code.
Restore `state/` and `state-api/` from the backup taken before the upgrade,
rebuild and verify the database at that tag's pin, then start and re-check
`/readyz`. Rolling the source back without rolling the database back is the
mistake to avoid: the schema version travels with the source.

## Troubleshooting

| symptom | likely cause | what to do |
| --- | --- | --- |
| Bot starts and answers nothing | `TELEGRAM_BOT_TOKEN` empty, wrong, or revoked | Check the process log for the startup failure. The token is read from `.env`; a wrong token fails at startup rather than silently. |
| Bot answers in a private chat to nobody | `OWNER_USER_ID` unset | Empty means private chat rejects everyone, on purpose. Set it, or use the bot in a group. |
| `/readyz` returns `503` | The terminology database is missing or unreadable | Confirm `WUWATERM_DB_PATH` points at a file that exists and that the process can read; build it if it does not. |
| `verify-db` fails on a fresh build | The upstream checkout is not the pinned commit, or its version-provenance file does not say the expected version | This is fail-closed by design. Re-run `refresh-data`; if it still fails, the profile in `src/wuwaterm/constants.py` and the upstream repository have diverged and the pin has to be updated deliberately, not worked around. |
| `refresh-data` fails | Same fail-closed check, or the fetch from the upstream repository failed or was interrupted | Read the message: it names which check failed. A network failure part-way through **does** leave a partial `data/wutheringdata/` behind, and re-running the same command **resumes** that checkout instead of starting over — that is deliberate, and it is why a retry after a transient failure is usually fast. A partial directory is still never consumed as data: `build-db` re-runs the same provenance inspection and refuses a checkout that is not at the pinned commit with the expected version file. |
| `401` with `unauthorized` from `/v1` | No token, a malformed token, or a revoked device | The header is `Authorization: Bearer wtd1.<device id>.<secret>`. Run `device list` to see whether that device is still active. |
| `403` with `forbidden` from `/v1` | The device has the wrong scopes | `translate` admits `POST /v1/translations` and `POST /v1/reviews`; `meta` admits `GET /v1/terms` and `GET /v1/meta`. Issue a device with both. |
| `429` with `rate_limited` | Two different bounds answer with this code. One is the per-device request limiter. The other is credential-verification admission: verification is deliberately expensive, so a bounded number of them may run at once and a request that finds every slot taken is refused **before** it is authenticated | If the completion record for that request carries no device principal, it was refused at verification admission and raising `WUWATERM_API_RATE_LIMIT_PER_MINUTE` will not help — raise `WUWATERM_API_AUTH_MAX_CONCURRENCY`, or reduce how many unauthenticated requests arrive at once. If it does name a device, it is the per-device limiter. |
| `503` with `llm_unavailable` | No model configured, or the model call failed or timed out | If you configured none, this is the documented behaviour. If you did, check the endpoint, the key and `WUWATERM_API_LLM_TIMEOUT_SECONDS`. |
| `503` with `llm_budget_exhausted` | The per-minute model call budget for that process | Raise `WUWATERM_API_LLM_CALLS_PER_MINUTE`, or wait. Budgets are per process and are not shared between the bot and the API. |
| The API will not start: bind error | `WUWATERM_API_BIND` set to something that is not a numeric loopback address | It is refused on purpose. Bind loopback and publish through a TLS terminator. |
| The API will not start: port in use | Something else holds the port | Change `WUWATERM_API_PORT`, and change the reverse proxy target with it. |
| The desktop client says the endpoint is unreachable | The base address, the certificate, or the proxy route | The client verifies certificates and that cannot be turned off. Check the base address resolves to a real route under the prefix, not to the site root. |

Server-side, every HTTP request leaves exactly one completion record carrying
the same `request_id` the caller was given, written to standard error at `INFO`.
Correlating a client report with that record is the fastest way to tell a
request that never arrived from one that arrived and failed. See
[Validation](validation.md) for what those records may and may not contain.
