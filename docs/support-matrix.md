# Support Matrix

What is actually tested, on what, and what "supported" means for a project of
this size. Every row here is a measured fact about this repository rather than
an intention.

## Server

| | |
| --- | --- |
| Declared range | `requires-python >=3.11`, no upper bound (`pyproject.toml`) |
| Tested in CI | Python **3.11, 3.12, 3.13 and 3.14**, on `ubuntu-latest`, through the `pytest (py3.x)` matrix. `main` pushes and release dispatches execute every version; a pull request runs 3.11 and 3.14 to shorten feedback, and the intermediate versions stay supported and are covered by the main push that follows a merge. |
| Entry point the matrix runs | `python scripts/validate.py` — the same command a contributor runs locally, so a green local run and a green matrix job are the same claim. It is not the whole pull request: the lock-drift check, the packaging build and audit, the Docker boundary job and the Web and Windows contract jobs run separately ([Validation](validation.md)) |
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

## Desktop client and API compatibility

[WuwaTerm-Windows](https://github.com/My-Denia/WuwaTerm-Windows) owns the desktop source, builds, support and
release process. The independent [v0.3.0 release](https://github.com/My-Denia/WuwaTerm-Windows/releases/tag/v0.3.0) is unsigned.
Core CI tests the current `/v1` API and a real nonempty review report against
the pinned Windows consumer, without importing Qt or the client transport.
The `api_version` in `/v1/meta` identifies the protocol; `service_version`
identifies the Core release. The desktop warns about an unsupported API version
on a status refresh rather than refusing the response. The API snapshot is [OpenAPI](api/openapi.json).
Windows and Web product CI own full manuscript file interoperability.

The owner-private Web and public beta have separate presentation boundaries;
see [Web Presentation Layer](web-presentation-layer.md) and [Public Beta Site](sites.md).

## Public beta Site

| | |
| --- | --- |
| Tree | [WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web) — Next.js / vinext on Cloudflare Workers, package `sites-project` 0.1.0 |
| Tested in CI | Web repository job `web` on `ubuntu-latest` with Node 24: `npm ci`, the full and production-only npm audits, `npm test`, typecheck, lint, production build, `verify:no-client-secret`, the built-artifact suite (`test:built`), a Playwright Chromium install, and the browser suite against the emitted bundle (`WUWATERM_BROWSER_BUILT=1 npm run test:browser`) |
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
