# Support Matrix

What is actually tested, on what, and what "supported" means for a project of
this size. Every row here is a measured fact about this repository rather than
an intention.

## Server

| | |
| --- | --- |
| Declared range | `requires-python >=3.11`, no upper bound (`pyproject.toml`) |
| Tested in CI | Python **3.11, 3.12, 3.13 and 3.14**, on `ubuntu-latest` — the `pytest (py3.x)` matrix, added in pull request #82. No version in that range is skipped. |
| Entry point the matrix runs | `python scripts/validate.py` — the same command a contributor runs locally, so a green local run and a green matrix job are the same claim. It is not the whole pull request: the lock-drift check, the packaging build and audit, the Windows client build, the Docker boundary job and the `site` job run separately ([Validation](validation.md)) |
| Container image | `python:3.11-slim` (`deploy/Dockerfile`). The image pins the low end of the range; the matrix covers the rest. |
| Published images from v0.4.0 onward | `ghcr.io/my-denia/wuwaterm` (runtime) and `ghcr.io/my-denia/wuwaterm-builder` (builder), tagged `vX.Y.Z`, `X.Y` and `sha-<7>`. They save the local image build and nothing else — the generic path still needs a source checkout at the release tag. Verify that the pull succeeds before planning around it; if it is denied, build from source. |
| Supported development platform | **Linux.** The suite is green there. |

### The server test suite on a Windows host is not supported

This is a platform limitation, not a preference, and it is worth naming
precisely so nobody spends an afternoon on it:

- **Directory `fsync`.** `scripts/deployment_manifest.py` makes a manifest
  replacement durable by opening the containing *directory* with `os.open` and
  syncing it. Windows does not allow a directory to be opened that way, so the
  call raises before the test body runs. That accounts for about 29 errors in
  `tests/test_deploy_scripts.py`, all through the same fixture.
- **Invalid handle when tests spawn subprocesses.** Tests that shell out to
  `git` or `python` fail nondeterministically with
  `OSError: [WinError 6] The handle is invalid`, depending on how the parent
  shell's standard handles are arranged. The same tests pass when run another
  way in the same checkout, which is what makes it a host property rather than
  a repository defect.

Neither is guarded by a platform skip today, so a Windows contributor running
`python -m pytest` sees red on a tree that CI reports green. Run the server
suite on Linux — a WSL checkout is enough, and a Linux run of the suite is
green at roughly 1028 tests.

The *client* suite is the opposite case: it runs on Windows, in the client's own
virtual environment, and everything about the client that can be checked by
reading text is in the server suite instead so that it runs on every pull
request without a Windows runner.

## Desktop client

| | |
| --- | --- |
| Operating system | Windows 10 and Windows 11, x64 — the **intended targets**, not a measured claim. CI builds and tests the client on GitHub's `windows-latest` runner image, which is not pinned to either desktop release, so a defect specific to one of them would not turn CI red. |
| Python to build | 3.12 or newer (`client/pyproject.toml` declares `requires-python >=3.12`); the client keeps its own virtual environment, separate from the server's |
| GUI toolkit | PySide6 (`>=6.7,<7`) |
| Build | one-folder PyInstaller build from the committed spec, via `client/build.ps1` |
| Signing | **None.** The build is unsigned and there is no installer, so Windows SmartScreen shows a warning the user has to click through. |
| Byte-for-byte equality between two builds | Not claimed and not checked. There is no lock file for the client and no comparison of build outputs. |
| Distribution between releases | a CI workflow artifact from the `desktop client build (windows)` job, retained for 90 days and requiring a GitHub login. The release workflow builds the same zip on every run, including its dry runs, so the artifact exists before any release does. |
| Distribution from v0.4.0 onward | a release asset, `WuwaTerm-0.2.0-windows-x64.zip`, listed in `SHA256SUMS` |
| Tested in CI | the client's own suite on `windows-latest` with Python 3.12, followed by the build; the release workflow repeats the suite, the build and the `--self-check` start-up rehearsal before it packages the zip |

## Compatibility contract

| | |
| --- | --- |
| Client 0.1.x — every build before this change | speaks HTTP API `v1`, served by wuwaterm **>= 0.3.0**. It parsed `api_version` and did nothing with it: pointed at a server speaking something else, it would have failed one request at a time with no explanation. |
| Client 0.2.x — what the tree carries now (`client/pyproject.toml`, `client/src/wuwaterm_client/__init__.py`), and the first publicly distributed build | the same contract, now stated in code as `SUPPORTED_API_VERSIONS` in `client/src/wuwaterm_client/api.py` and **checked**. A reply whose `api_version` is outside that tuple raises a warning naming the version the server reported and the version this client speaks. It is a warning, not a refusal: the service facts stay on screen and nothing is blocked, because the client cannot know that every route it needs has changed. |
| When the check runs | on the `/v1/meta` reply the status view already fetches when the owner presses 刷新. **No request was added, and none happens at startup** — an unconfigured or freshly started client still sends nothing, which is a tested invariant (issues #68 and #80), not a side effect. |
| How the client learns which PROTOCOL the server speaks | `api_version` in the body of `GET /v1/meta`. It is the contract identifier — `"v1"` — and it is stable across server releases, so it answers "can I talk to this at all", not "which release is this" |
| How the client learns which RELEASE is running | `service_version` in the same body, which is the installed package version and is what the desktop status view shows. That is the field to read against the `>= 0.3.0` boundary above |
| Where the contract lives | [`docs/api/openapi.json`](api/openapi.json), committed and drift-gated by `scripts/check_api_contract.py`, so a route, model or error code cannot change without the published contract changing in the same commit |

The owner-private web presentation layer is deliberately outside this contract:
it is off by default and has no entry in the published API document
([Web Presentation Layer](web-presentation-layer.md)).

The anonymous public beta Site is also outside this contract: it is a separately
hosted proxy with its own error envelope, checked by the CI `site` job, not by
the desktop client ([Public Beta Site](sites.md)).

## Public beta Site

| | |
| --- | --- |
| Tree | `site/` — Next.js / vinext on Cloudflare Workers, package `sites-project` 0.1.0 |
| Tested in CI | job `site` (`site feasibility security`) on `ubuntu-latest` with Node 24: `npm ci`, the full and production-only npm audits, `npm test`, typecheck, lint, production build, `verify:no-client-secret`, the built-artifact suite (`test:built`), a Playwright Chromium install, and the browser suite against the emitted bundle (`WUWATERM_BROWSER_BUILT=1 npm run test:browser`) |
| Public service status | Best-effort anonymous beta at <https://wuwaterm.denia-official.chatgpt.site>; no account and no SLA, fixed-availability promise or per-visitor fairness guarantee |
| Not in `scripts/validate.py` | by design; the server matrix does not install Node |
| Visitor authentication | **none in application code.** Crawler `noindex` is not an ACL |
| Relation to `/wuwaterm-web` | different process, different auth, different docs |

## Telegram

| | |
| --- | --- |
| Library | `python-telegram-bot` 22.x (`>=22.7,<23`) |
| Delivery | long polling |

Long polling is a decision with a record, not an accident of setup:
[ADR 0003](adr/0003-long-polling-not-webhook.md).

## Game data

- Upstream: `https://github.com/Arikatsu/WutheringWaves_Data`
- Pinned commit: `9218d612ad815e398e064e577e42aaf878899968`
- Pinned version: `GameVer 3.7.0 / ResVer 3.7.8 / Changelist 8975829`

The pin is enforced, not documented: the refresh checks the remote, the commit
and the upstream version-provenance file and stops rather than building from
anything else. The database itself is built locally and is never distributed.
Details, including the licence boundary, are in
[Data Refresh](data-refresh.md#data-source).

## What "supported" means here

This is a personal hobby project maintained on a best-effort basis. "Supported"
on this page means one thing only: **there is a test, running in CI, that would
go red if it broke.** It does not mean a response time, a maintenance window, or
a commitment to keep anything working for anyone in particular. There is no
service-level guarantee and no guarantee of a reply to an issue or a pull
request.

Issues are welcome anyway, and a good one is much more likely to get an answer
than a vague one. Where to send what — a question, a bug, a security report, a
change — is in [SUPPORT.md](../SUPPORT.md).
