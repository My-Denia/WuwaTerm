# Contributing

Thanks for looking. WuwaTerm is a personal, MIT-licensed hobby project with one
maintainer, so contributions are welcome but reviewed best-effort — there is no
promised turnaround. Small, focused changes are the ones that get merged.

Before you build something, read [Non-Goals](#non-goals). These product
boundaries guide what the project accepts.

- Bug reports and questions: [SUPPORT.md](SUPPORT.md).
- Vulnerabilities: [SECURITY.md](SECURITY.md) — never a public issue.
- Try the current no-account public beta:
  <https://wuwaterm.denia-official.chatgpt.site>. It is best-effort, uses one
  shared pool, and is a useful reproduction surface but not an SLA-backed test
  environment.

## What This Repository Contains

Five surfaces sit on one application layer, except that linked-channel
auto-translation keeps its own orchestration over the same lookup and
term-locking primitives (the last two surfaces are API consumers, not adapters):

- a Telegram bot,
- an HTTP API under `/v1`,
- an owner-private web presentation layer that lives inside the API process and
  is off by default,
- a Windows desktop client in [WuwaTerm-Windows](https://github.com/My-Denia/WuwaTerm-Windows), which only calls the API,
- an anonymous public beta in [WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web), a separately hosted same-origin proxy.
  It is not the in-process `/wuwaterm-web` layer; see
  [Public Beta Site](docs/sites.md).

Terminology comes from a pinned upstream game-data repository and is built into
a local SQLite dictionary. Wuthering Waves game data and terminology are
copyright Kuro Games; this project never redistributes them, and no generated
database or upstream text may be committed.

[Architecture](docs/architecture.md) is the map, and
[docs/adr/README.md](docs/adr/README.md) records why the shape is what it is.

### API / public Web contract

The default Python suite excludes `cross_component` tests and needs no Node.js
for the meta projection contract. CI runs these tests in a dedicated job with
Python 3.11 and Node.js 24. Run them explicitly with:

```sh
python -m pytest -m cross_component tests/test_meta_site_contract.py
```

`WUWATERM_WEB_ROOT` must select the external Web source root. It must
contain `tests/helpers/project-live-meta.mjs` and the BFF implementation it
imports. An explicit contract run fails if Node or the helper is missing.
For a separate Web checkout, set this variable to its absolute path and record
both repository commit SHAs with the result. The test passes real API response
bytes through the consumer; strict field rejection remains part of the contract.
CI pins that checkout using `contracts/web-ref.txt`. To reproduce the pair locally:

```sh
git clone https://github.com/My-Denia/WuwaTerm-Web.git web
git -C web checkout --detach "$(cat contracts/web-ref.txt)"
WUWATERM_WEB_ROOT="$PWD/web" python -m pytest -m cross_component tests/test_meta_site_contract.py
WUWATERM_BACKEND_ROOT="$PWD" WUWATERM_WINDOWS_ROOT=/path/to/windows npm --prefix web run test:contract
```

The Web contract requires Node 24, Python 3.12 or newer and its pinned Windows
checkout, but no npm dependency installation. Full Web validation belongs to the
Web repository. Keep its pin immutable and update it
through a reviewed PR when the producer/consumer contract changes.

## Getting The Code

```bash
git clone https://github.com/My-Denia/wuwa-translate-bot.git
cd wuwa-translate-bot
```

Work on a branch and open a pull request against `main`.

## Setting Up

The server needs Python 3.11 or newer. Either path works; use the first if you
have [uv](https://docs.astral.sh/uv/).

With uv:

```bash
uv venv .venv
uv sync --locked --extra dev --extra telegram
```

With a plain virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev,telegram]"
```

`--locked` makes uv honour `uv.lock` rather than re-resolving it; CI has a job
that fails when the lock has drifted from `pyproject.toml`, so do not edit one
without the other.

**Host support.** The server test suite is green on Linux and is not supported
on a Windows host — several tests exercise POSIX-only behaviour and fail there
for platform reasons rather than code reasons. If you develop on Windows, run
the suite under WSL or a Linux container. See
[docs/support-matrix.md](docs/support-matrix.md) for what is supported where.

## The One Command

```bash
python scripts/validate.py
```

That is the whole local gate, and it is exactly what the server test matrix
runs in CI — the same file, not a second list that drifts from this one. It is
not the whole pull request, though: CI also runs the lock-drift check, the
wheel and sdist build with its install audit, the Docker runtime and builder boundary, and the cross-repository Web and Windows contract jobs,
and none of those is in this command. If your change touches the lock file,
the packaging metadata, the consumer contracts, or anything under `deploy/`, a
green run here is necessary and not sufficient — see the job list further down.

```bash
python scripts/validate.py --list        # what it would run; runs nothing
python scripts/validate.py --quick       # everything except the test suite
python scripts/validate.py --cov         # run tests with coverage and floor gate
python scripts/validate.py --only ruff   # a single step by name
```

The run stops at the first failing step, names it, and exits with that step's
status. The steps, in order:

| Step | What it is for | What a failure means |
|---|---|---|
| `hygiene` | Keeps generated databases and runtime state out of the index, and bulk upstream game text — the game-text paths are checked by SIZE, above 1 MB, so a small upstream file passes this gate and is still not welcome | A generated database, a runtime state file, or a large upstream data file would be committed |
| `architecture` | Guards import directions between layers | An import crosses a boundary the architecture forbids |
| `api-contract` | Ties the published contract to the code | The committed `docs/api/openapi.json` no longer equals the document the application generates |
| `ruff` | Correctness linting only — rules `E4`, `E7`, `E9`, `F` | An unused import, an undefined name, a broken f-string, or similar. Formatting is not enforced, so do not send a formatting sweep |
| `typing` | Static type checking of `src/wuwaterm` against the baseline in `pyproject.toml` | The type checker reports an error outside the baseline in `pyproject.toml` |
| `pytest` | The repository test suite | The suite is red (or under `--cov`, coverage falls below the floor) |

`--cov` appends `--cov=src --cov-report=term-missing` to `pytest` and enforces
the regression floor configured in `pyproject.toml` (`fail_under = 88`, measured
baseline 91.37% combined on 2026-11-11, #211). Because coverage requires
running the suite, `--cov` cannot be combined with `--quick`.

[docs/validation.md](docs/validation.md) has the longer account, including the
candidate-database checks that belong to a data refresh rather than to every
commit.

Continuous integration runs `python scripts/validate.py` across the supported
Python versions (3.11, 3.12, 3.13 and 3.14 on `ubuntu-latest`; pull requests
run 3.11 and 3.14). The Python 3.11 leg runs `python scripts/validate.py --cov`
to enforce the coverage floor without duplicating runtime overhead across every
matrix leg. CI also runs a uv lock-drift check, package audit, Docker boundary
check, and contracts against pinned Web and Windows checkouts.
The full Site suite runs in [WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web).
CI publishes nothing.

## Windows consumer contract

Windows source, tests, builds, releases and issues live in [WuwaTerm-Windows](https://github.com/My-Denia/WuwaTerm-Windows).
Core CI checks the exact revision in `contracts/windows-ref.txt` without Qt or
keyring. With that revision checked out at `WUWATERM_WINDOWS_ROOT`, run:

```sh
WUWATERM_WINDOWS_ROOT=/path/to/windows python -m pytest -m cross_component tests/test_windows_contract.py
```

The test reads the consumer's API versions and sends a synthetic review through
the current Core HTTP app to the Windows manuscript validator. Web and Windows
CI own the full manuscript file interoperability checks.

## What Makes A Pull Request Easy To Accept

- **One concern.** A small change that does one thing is reviewed in one pass. A
  large change that does four is reviewed in four, if at all.
- **Evidence for the change.** Use existing or new tests for changed behaviour,
  or explain the build, manual check or other validation used. Prose-only
  changes do not require new tests.
- **Behaviour, not wiring** ([issue #66](https://github.com/My-Denia/wuwa-translate-bot/issues/66)).
  When you add an assertion that a signal is connected, a callback was injected
  or a dependency was passed in, add a second assertion that actually triggers
  that slot or callback at least once, with its external dependencies (dialogs,
  network, keyring) replaced by stand-ins, and asserts the observable outcome.
  A wiring assertion stays green when the slot itself raises on its first line.
  Two things make such a test red for reasons that are not the code's fault, so
  write it around them: assert focus as `page.focusWidget() is widget` rather
  than `widget.hasFocus()`, because the offscreen platform the suites run on
  never activates a window; and build the case on a **configured** window,
  because a disabled widget correctly refuses focus.
- **A changelog entry for user-visible changes.** Under `## Unreleased` in
  [CHANGELOG.md](CHANGELOG.md), say what changed and why.
- **README parity.** `README.md` (English, the front page) and
  `README.zh-CN.md` (Chinese) are kept aligned: if you touch one, touch the
  other, and keep the same sections, facts and images in both. The READMEs are
  a short introduction; detail belongs in the guides listed in
  [docs/README.md](docs/README.md), not in the front page.
- **Documentation updated when behaviour changed.** A document that promises
  something the code does not do is a defect here.
- **No secrets and no generated data.** Never commit a generated SQLite
  database, upstream TextMap or game data, runtime state (`state/`,
  `state-api/`, `chat_settings.json`, `channel_replies.json`), or a `.env` file.
  The `hygiene` gate catches most of this; it is not a reason to stop looking.
- **Validation recorded in the pull request.** Run `python scripts/validate.py`
  locally for server changes; describe suitable checks for other changes.
- Signed commits are welcome. The maintainer signs; you are not required to.

## Non-Goals

Four product decisions are settled. Please do not send a change that adds any
of them without discussing the decision first:

1. **No callback-URL registration for Telegram.** The bot uses long polling; see
   the decision records in [docs/adr/README.md](docs/adr/README.md).
2. **No inline-query surface.** The bot answers commands and one channel
   listener, not inline queries.
3. **No synonym layer over the dictionary.** Lookups resolve against official
   terminology, and adding a second, unofficial naming layer defeats the point
   of the project.
4. **No free-text listener beyond the single linked-channel one.** Exactly one
   listener exists — automatic forwards from the linked channel.

If you believe one of these decisions should change, open an issue and discuss
it before writing code.

## How Review Works

Pull requests are reviewed by automated reviewers and by the maintainer:

- A Claude code-review workflow runs on every pull request, alongside
  repository-level review bots.
- CodeQL default setup scans the repository, weekly and on pull requests.
- The maintainer reads it when they can. This is a spare-time project; a quiet
  week is not a rejection, and a nudge on the pull request after a while is
  fine.

Automated review is advisory. A bot comment that is wrong should be answered,
not silently obeyed — say why in the thread.

Severity and scope are different questions. A true comment can still sit
outside this pull request. [AGENTS.md](AGENTS.md) says what the current pull
request must fix, what is a follow-up, and when a confirmed in-scope fix may
be pushed on the same branch. Merge, deploy, and production changes still
need their own authorization.

## Reporting Instead Of Fixing

Reporting is a contribution. The issue forms ask for the fields that make a
report actionable — version, surface, environment, steps, expected versus
actual, and logs with the secrets removed:
<https://github.com/My-Denia/wuwa-translate-bot/issues/new/choose>

## Code Of Conduct

This project has no code of conduct as of v0.4.0. It is a single-maintainer,
best-effort hobby project whose community surface is this file plus
[SUPPORT.md](SUPPORT.md), [SECURITY.md](SECURITY.md), the issue forms and the
pull request template; with no community yet and one person to act on a
report, a conduct document here would be a promise rather than a policy. The
decision is meant to be revisited the moment there is outside contribution or
community activity to govern, and at that point the standard
[Contributor Covenant](https://www.contributor-covenant.org/) is what would be
adopted rather than something written here from scratch.

## Licence

By contributing you agree that your contribution is licensed under this
project's [MIT licence](LICENSE).
