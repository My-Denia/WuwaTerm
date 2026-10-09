# Validation

## One Command

```bash
python scripts/validate.py            # every gate, in order
python scripts/validate.py --list     # what it would run; runs nothing
python scripts/validate.py --quick    # every gate except the test suite
python scripts/validate.py --only ruff
python scripts/validate.py --client   # additionally: the desktop client suite
```

The steps, in order: `hygiene`, `non-goals`, `architecture`, `api-contract`,
`ruff`, `pytest`. `--list` prints them with the command each one runs and what
its failure means. The run stops at the first failing step and names it, and
the exit status is that step's.

This is the entry point the **server test matrix** uses: the `pytest (py3.x)`
jobs install `.[dev]` and then run `python scripts/validate.py`, so a green run
here and a green matrix job are the same claim, on the same file. Before it
existed, CI carried one list, the README a shorter one, and this page a third,
and a gate added to one of them was invisible in the others.

It is **not** the whole pull request. CI also runs the uv lock-drift check, the
wheel and sdist build with its packaging audit and clean-venv install smoke, the
Windows desktop-client build, the Docker runtime/builder boundary job, and the
`site` job under `site/` — five jobs this command does not contain, needing
`uv`, a build, a Windows runner, Docker, and Node respectively. If a change
touches the lock file, the packaging metadata, `client/`, `site/` or
`deploy/`, a green run here is necessary and not sufficient; the job list is
in [the gate table below](#gates-for-the-http-api-and-the-desktop-client)
and in `.github/workflows/ci.yml`.

Two further workflows run on a pull request, but only when it touches what they
cover. `.github/workflows/release.yml` runs in dry-run mode when the pull
request changes that workflow, `deploy/**`, `client/**` or `pyproject.toml`: it
builds the wheel, the sdist, the Windows client zip, both container images, the
checksums and the release manifest, and publishes nothing.
`.github/workflows/selfhost-smoke.yml` runs when the pull request changes
`deploy/**` or [Self-Hosting](self-hosting.md): it follows that guide's
container path end to end on a clean runner — image build, data refresh,
database build and verification, an API-only start, a device credential, one
exact lookup, one sentence translation against a mock model endpoint, and a
credential-store backup and restore. It is the only gate in this repository
that runs the documented install instead of reading it, which is exactly the
class of defect the text gates cannot see.

The READMEs used to carry their own command block, and it was **not**
equivalent: it predated both the linter and the API-contract gate, so following
it could leave a contributor green locally and red on the pull request. The
READMEs now name only `python scripts/validate.py`, so there is one list of
gates and it lives in the script. The candidate-database checks below are
deliberately not in the entry point, because they need a built
`data/terms.candidate.db`.

The script uses only the standard library and runs each step with the
interpreter that runs the script, so there is no POSIX-only path in it:

```powershell
.venv\Scripts\python scripts\validate.py
```

```bash
.venv/bin/python scripts/validate.py
```

`--client` is opt-in, and it runs under `client/.venv` rather than under the
interpreter that started the script: the client is a separate package needing
Qt, `keyring` and `qasync` and at least Python 3.12, while that environment
carries none of the server's dependencies, so no single interpreter can run
both sides. If `client/.venv` does not exist the step says so and how to make
it, instead of failing in test collection. Everything about the client that can
be checked by reading text is in the main suite instead (see the table below).

The linter is `ruff`, installed by the `dev` extra and bounded to one minor
release. The enabled rules are written out in `pyproject.toml` under
`[tool.ruff.lint]` — `E4`, `E7`, `E9`, `F` — rather than left to the tool's
defaults, so the gate cannot change under the repository when those defaults
move. It is a correctness gate: unused imports, undefined names, broken
f-strings. Formatting is not enforced.

## Offline Validation

The commands below are what the entry point runs, plus the candidate-database
checks that are **not** in it: those need a built `data/terms.candidate.db`,
which only exists during a data refresh, so they belong to that workflow rather
than to every commit. `uv lock --check` is also outside the script (it needs
`uv`, which the `dev` extra does not install) and has its own CI job.

```bash
.venv/bin/python scripts/verify_db.py data/terms.candidate.db --profile arikatsu --require-free-text-exclusions --require-free-text-zh-exclusions
.venv/bin/python scripts/verify_seed_terms.py data/terms.candidate.db --discrepancies goal-runs/wuwaterm-v2-translator/seed-discrepancies.json
.venv/bin/python scripts/verify_exact_hits.py data/terms.candidate.db --sample-size 500
.venv/bin/python scripts/verify_idempotent_build.py --data-dir data/wutheringdata --out-dir goal-runs/wuwaterm-v2-translator --profile arikatsu
.venv/bin/python scripts/check_repo_hygiene.py
.venv/bin/python scripts/check_non_goals.py
.venv/bin/python scripts/check_architecture_boundaries.py
.venv/bin/python scripts/check_api_contract.py
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest
uv lock --check
```

The `goal-runs/` paths above are local working artifacts, ignored by Git; the
scripts create or read them on the machine that runs the validation pass.

`verify_idempotent_build.py` compares SHA256 over LF-normalized SQLite logical
dumps, not raw database bytes, so Windows/Linux SQLite formatting differences
do not create false mismatches.

All database checks above target the candidate. They do not promote or replace
`data/terms.db`; production promotion belongs to the transactional deployment
workflow after every candidate gate passes.

## Live Telegram Smoke

Live Telegram smoke is owner-gated. If `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_TEST_CHAT_ID` are not supplied, only the live smoke criterion is
blocked; offline handler tests still validate the bot code.

`scripts/deploy_smoke.py` is a deployment reachability check, not a polling
handler E2E test. It verifies `getMe`, and when `TELEGRAM_TEST_CHAT_ID` is set
it sends one diagnostic message without printing the token or chat id. A real
handler E2E still requires observing the bot's reply from Telegram.

`check_repo_hygiene.py` and `check_non_goals.py` are each narrower than their
names suggest, and **neither is a secret scanner**.

`check_repo_hygiene.py` reads the git index and the working tree and fails on
four things: a path whose suffix says SQLite (`.db`, `.sqlite`, `.sqlite3`); a
staged blob or an untracked file whose first sixteen bytes are the SQLite
header, whatever the file is called; a runtime-state path (`state/`,
`state-api/`, `chat_settings.json`, `channel_replies.json` and their
dot-prefixed forms); and a bulk game-text path (`TextMap/`, `Textmaps/`,
`ConfigDB/`, `BinData/`, `data/`, `wutheringdata`) over 1 MB.

`check_non_goals.py` reads text files and fails on four product markers — the
chat-side delivery-registration settings, the inline-query handler class, the
keyword of a synonym layer, and the free-text message-handler class — plus a
structural pin that `src/wuwaterm/bot.py` registers exactly one message
listener and that it is the channel auto-forward one.

Neither one looks for tokens, API keys or real Telegram identifiers, and no
gate in this repository does. What keeps credentials out of commits is
elsewhere: GitHub secret scanning with push protection, enabled on the
repository rather than run from `scripts/`; `.gitignore`, which excludes
`.env`, `state/`, `state-api/` and every `*.db` / `*.sqlite` / `*.sqlite3`;
and `scripts/check_package_artifacts.py`, which audits what a built
distribution actually contains.

`check_architecture_boundaries.py` guards forbidden import directions between
presentation, domain, and builder modules, and enforces the HTTP adapter's
four-module import allowlist into `wuwaterm` (see
[Architecture](architecture.md)).

## Gates For The HTTP API And The Desktop Client

| Gate | What it proves | Where it runs |
|---|---|---|
| `scripts/check_api_contract.py` | The committed `docs/api/openapi.json` still equals the document the application generates, and the product token pins hold in that JSON artifact (which the text scanner does not read). A route, model or error code cannot change without the published contract changing in the same commit | `scripts/validate.py`, offline and in every CI matrix job, before the suite |
| `tests/test_client_transport_policy.py` | The desktop client cannot regress into reaching the service through the operator's administration channel, and certificate verification cannot be turned off by an edit anywhere in the client tree, the deploy scripts or this runbook. Text gates over the shipped client surface, `docs/deployment.md` and `deploy/*` | The repository `pytest` run — deliberately here rather than in the client suite, so it runs on every pull request without a Windows runner or the client's dependencies |
| `client/.venv/Scripts/python.exe -m pytest` (in `client/`) | The client's own behaviour: transport refusals, the request-target guard, credential storage, cancellation, error rendering, and that every displayed string comes from the strings module | CI `desktop client build (windows)` job on `windows-latest` |
| `client/build.ps1` | The one-folder PyInstaller artifact builds from the committed spec and passes its own `--self-check`, and the build leaves nothing untracked in the working tree. Not that two builds agree: there is no lock file for the client and no byte comparison, and the script says so | Same CI job; the artifact is uploaded there |
| `tests/test_client_documentation_claims.py` | Documentation does not promise properties the client does not have. The ban on claiming that two builds produce the same bytes is repository-wide, and literally so: the scan covers every file `git ls-files` reports outside `tests/` — extensionless and unfamiliar-extension files included, so `deploy/Dockerfile`, `uv.lock`, `docs/api/openapi.json` and the env examples are in scope, and nothing merely present-but-untracked is — with the two denial sentences exempted by their text rather than by their file, while the build script's own guarantee and the README's account of cancellation are pinned where they live. A text gate, for defects that are sentences rather than code | The repository `pytest` run, beside the other text gates |
| `tests/test_api_request_logging.py` | Every HTTP request leaves exactly one server-side **completion** record — on the authenticated path, on `401`, on a failure that never reaches a handler — carrying the same `request_id` the caller was given; and that no record the deployed process would write holds any of what the service itself knows: a credential, the device id behind an authenticated principal, or submitted text. A caller-supplied target is recorded escaped, bounded, and replaced entirely when it could be a credential. The adapter's other diagnostic lines are outside the one-per-request count and inside the privacy scan. Asserted against captured records rather than by reading the source, because a field added later is invisible to inspection | The repository `pytest` run |
| `cd site && npm test && npm run build && npm run verify:no-client-secret` | The Sites proxy pins host, path, redirects, schema and secret non-reflection; the client bundle has no device token or `WUWATERM_*` names. Mocked upstream only; not a live VPS | CI `site` job (`site feasibility security`) |

The client gates are split on purpose. Everything that can be checked by
reading text runs in the main suite on Linux; only what genuinely needs Qt,
`keyring` and a Windows toolchain runs on the Windows runner.

Neither the API contract gate nor the client build proves anything about a
deployed service. A request answered on the service host is evidence about the
process; evidence about the published endpoint has to come from the client
machine (see [Deployment](deployment.md)).

## Server-Side Request Records

At `INFO`, the default, the HTTP adapter writes one **completion record** per
HTTP request — a line containing `request complete` — to standard error when it
is started by `wuwaterm-api serve`. That is the stream the standard library's
default handler uses, and the one the bot's records already go to. Raising the
level drops those records, and correlation with them.

HTTP is the whole of what this service speaks: it declares no WebSocket route,
and this repository locks no WebSocket library for any purpose, so no build made
from the lock — the runtime image among them — has one to hand (pinned by
`test_no_websocket_library_is_locked_for_any_purpose`). An upgrade attempt is
therefore refused before any such connection exists, and is answered — and
recorded — as the ordinary HTTP request it arrived as.

The adapter's other diagnostic lines are unchanged and are not part of that
guarantee.

`WUWATERM_API_LOG_LEVEL` (`CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG`;
default `INFO`) sets the level; an unusable value stops the serve path with exit
code 2 and never blocks the credential subcommands. Nothing is configured at
import time, so a program that embeds the application keeps its own logging
arrangement.

Correlating a client's report with the server is the point of the record: the
`request_id` the caller was given is the `request_id` in the line. Which
responses carry it in the body and which only in the header, and how to read it
back with `docker logs`, are in
[Deployment](deployment.md#reading-the-request-log). What the records may and
may not contain is a test, not a convention — see the table above.

## Windows Reference

Windows commands are still supported when needed:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install -e ".[dev]"
$env:TELEGRAM_BOT_TOKEN="..."
$env:WUWATERM_DB_PATH="data\terms.db"
```
