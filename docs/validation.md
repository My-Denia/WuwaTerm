# Validation

## One Command

```bash
python scripts/validate.py            # every gate, in order
python scripts/validate.py --list     # what it would run; runs nothing
python scripts/validate.py --quick    # every gate except the test suite
python scripts/validate.py --only ruff
```

The steps, in order: `hygiene`, `architecture`, `api-contract`,
`ruff`, `pytest`. `--list` prints them with the command each one runs and what
its failure means. The run stops at the first failing step and names it, and
the exit status is that step's.

This is the entry point the **server test matrix** uses: the `pytest (py3.x)`
jobs install the source-appropriate development dependencies and then run `python scripts/validate.py`, so a green run
here and a green matrix job are the same claim, on the same file. Before it
existed, CI carried one list, the README a shorter one, and this page a third,
and a gate added to one of them was invisible in the others.

It is **not** the whole pull request. CI separately checks the lock, package,
Docker boundary, and contracts against pinned Web and Windows checkouts. Windows
build and release validation runs in [WuwaTerm-Windows](https://github.com/My-Denia/WuwaTerm-Windows).

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

`check_repo_hygiene.py` is narrower than its name suggests and **is not a secret scanner**.

`check_repo_hygiene.py` reads the git index and the working tree and fails on
four things: a path whose suffix says SQLite (`.db`, `.sqlite`, `.sqlite3`); a
staged blob or an untracked file whose first sixteen bytes are the SQLite
header, whatever the file is called; a runtime-state path (`state/`,
`state-api/`, `chat_settings.json`, `channel_replies.json` and their
dot-prefixed forms); and a bulk game-text path (`TextMap/`, `Textmaps/`,
`ConfigDB/`, `BinData/`, `data/`, `wutheringdata`) over 1 MB.

The hygiene check does not look for tokens, API keys or real Telegram
identifiers. Other protections include GitHub secret scanning with push
protection, enabled on the repository rather than run from `scripts/`; `.gitignore`, which excludes
`.env`, `state/`, `state-api/` and every `*.db` / `*.sqlite` / `*.sqlite3`;
and `scripts/check_package_artifacts.py`, which audits what a built
distribution actually contains.

`check_architecture_boundaries.py` guards forbidden import directions between
presentation, domain, and builder modules, and enforces the HTTP adapter's
four-module import allowlist into `wuwaterm` (see
[Architecture](architecture.md)).

## Gates For The HTTP API And Consumers

| Gate | What it proves | Where it runs |
|---|---|---|
| `scripts/check_api_contract.py` | The committed `docs/api/openapi.json` still equals the document the application generates. A route, model or error code cannot change without the published contract changing in the same commit | `scripts/validate.py`, offline and in every CI matrix job, before the suite |
| `tests/test_api_request_logging.py` | Every HTTP request leaves exactly one server-side **completion** record — on the authenticated path, on `401`, on a failure that never reaches a handler — carrying the same `request_id` the caller was given; and that no record the deployed process would write holds any of what the service itself knows: a credential, the device id behind an authenticated principal, or submitted text. A caller-supplied target is recorded escaped, bounded, and replaced entirely when it could be a credential. The adapter's other diagnostic lines are outside the one-per-request count and inside the privacy scan. Asserted against captured records rather than by reading the source, because a field added later is invisible to inspection | The repository `pytest` run |
| `WUWATERM_WEB_ROOT=... python -m pytest -m cross_component tests/test_meta_site_contract.py`; Web `npm run test:contract` | Real API metadata through the pinned BFF plus category/reason/client-label compatibility; strict response projection retained | Backend CI `web-contract`; Web CI also tests a pinned backend |

`WUWATERM_WINDOWS_ROOT=... python -m pytest -m cross_component tests/test_windows_contract.py`
checks the pinned Windows version declaration and pure manuscript validator against
actual Core API responses, including a nonempty review. Web and Windows product CI
check their full file interoperability.

## Historical CI sources

The running workflow revision supplies the epoch policy; the exact source
commit determines which checks apply. The supported bundled-client interval
starts at v0.6.1 and ends at the last bundled main commit, inclusive. Its Web
contract uses the Web revision recorded by that source and its bundled client
labels. Sources containing manuscript support also run the original real
Web/client file round trip. The product-owned Windows workflow builds and
tests the historical client. Sources after the interval instead run the pure
Core-to-Windows contract; normal Core CI does not build the Windows product.

CI retains the Python matrix, locked runtime, package and container checks for
both epochs. The earliest supported sources include Telegram in their base
dependencies; later ones check the SDK-free core and builder. A source that is
not connected to the trusted main ancestry or lacks its epoch's required files
fails rather than falling back to fewer checks. Equal trees do not establish
ancestry.

A release receipt binds its exact source, control revision and policy digest to
a completed successful CI attempt and all selected jobs. Old schema receipts
are not accepted. For a historical source without a matching receipt, dispatch
current CI on main for that exact SHA, wait until the whole run succeeds, and
separately verify that attempt as described in the release checklist. A release
dry run does not test the receipt gate.

Neither the API contract gate nor consumer tests prove anything about a
deployed service. A request answered on the service host is evidence about the
process; evidence about the published endpoint has to come from the client
machine (see [Deployment](deployment.md)).

## Server-Side Request Records

At `INFO`, the default, the HTTP adapter writes one **completion record** per
HTTP request — a line containing `request complete` — to standard error when it
is started by `wuwaterm-api serve`. That is the stream the standard library's
default handler uses, and the one the bot's records already go to. Raising the
level drops those records, and correlation with them.

HTTP is the whole of what this service speaks: it declares no WebSocket route.
CI checks that its runtime image has neither `websockets` nor `wsproto` installed.
An upgrade attempt is refused and recorded as an HTTP request.

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

The server test suite is unsupported on a native Windows host. The following commands show environment setup only; use Linux for the server validation gate:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install -e ".[dev,telegram]"
$env:TELEGRAM_BOT_TOKEN="..."
$env:WUWATERM_DB_PATH="data\terms.db"
```
