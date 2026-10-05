# Contributing

Thanks for looking. WuwaTerm is a personal, MIT-licensed hobby project with one
maintainer, so contributions are welcome but reviewed best-effort — there is no
promised turnaround. Small, focused changes are the ones that get merged.

Before you build something, read [Non-Goals](#non-goals). Some things this
project has deliberately refused are enforced by a gate, and a pull request that
adds one will be rejected no matter how well it is written.

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
- a Windows desktop client under `client/`, which only calls the API,
- an anonymous public beta under `site/`, a separately hosted same-origin proxy.
  It is not the in-process `/wuwaterm-web` layer; see
  [Public Beta Site](docs/sites.md).

Terminology comes from a pinned upstream game-data repository and is built into
a local SQLite dictionary. Wuthering Waves game data and terminology are
copyright Kuro Games; this project never redistributes them, and no generated
database or upstream text may be committed.

[Architecture](docs/architecture.md) is the map, and
[docs/adr/README.md](docs/adr/README.md) records why the shape is what it is.

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
uv sync --locked --extra dev
```

With a plain virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
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
wheel and sdist build with its install audit, the Windows desktop-client build,
the Docker runtime and builder boundary, and the `site` job under `site/`,
and none of those is in this command. If your change touches the lock file,
the packaging metadata, the client, `site/`, or anything under `deploy/`, a
green run here is necessary and not sufficient — see the job list further down.

```bash
python scripts/validate.py --list        # what it would run; runs nothing
python scripts/validate.py --quick       # everything except the test suites
python scripts/validate.py --only ruff   # a single step by name
python scripts/validate.py --client      # additionally: the desktop client suite
```

The run stops at the first failing step, names it, and exits with that step's
status. The steps, in order:

| Step | What it is for | What a failure means |
|---|---|---|
| `hygiene` | Keeps generated databases and runtime state out of the index, and bulk upstream game text — the game-text paths are checked by SIZE, above 1 MB, so a small upstream file passes this gate and is still not welcome | A generated database, a runtime state file, or a large upstream data file would be committed |
| `non-goals` | Pins four product decisions as text | A boundary this project has refused was crossed in a file |
| `architecture` | Guards import directions between layers | An import crosses a boundary the architecture forbids |
| `api-contract` | Ties the published contract to the code | The committed `docs/api/openapi.json` no longer equals the document the application generates |
| `ruff` | Correctness linting only — rules `E4`, `E7`, `E9`, `F` | An unused import, an undefined name, a broken f-string, or similar. Formatting is not enforced, so do not send a formatting sweep |
| `pytest` | The repository test suite | The suite is red |

[docs/validation.md](docs/validation.md) has the longer account, including the
candidate-database checks that belong to a data refresh rather than to every
commit.

Continuous integration runs `python scripts/validate.py` on Python 3.11, 3.12,
3.13 and 3.14 on `ubuntu-latest`, plus five more jobs: a uv lock-drift check, a
wheel and sdist packaging audit with a clean-environment install smoke, a
Windows desktop-client build, a Docker runtime/builder boundary check, and
the Site suite (`npm test`, typecheck, lint, build,
`verify:no-client-secret`, and the browser handoff regression in `site/`).
CI publishes nothing.

## Working On The Desktop Client

The client is a separate package with its own virtual environment. It needs
Python 3.12 or newer, and it pulls in PySide6, `keyring` and `qasync` — none of
which the server environment carries, and it carries none of the server's
dependencies either. No single interpreter can run both suites, which is why
`--client` runs under `client/.venv`.

On Windows:

```powershell
py -3.12 -m venv client\.venv
client\.venv\Scripts\python.exe -m pip install -e "client[dev,build]"
Push-Location client; .venv\Scripts\python.exe -m pytest; Pop-Location
client\build.ps1
```

The third line changes directory on purpose. `pytest` reads its configuration
from the working directory, and the repository root's `testpaths` points at the
SERVER suite — so running it from the root with the client interpreter collects
the wrong suite and fails on the first import. Measured, not guessed.

Any interpreter that satisfies the declared range will do — `py -3.12` picks a
registered 3.12 whoever shipped it, `py -3.13` is equally fine, and on a
machine without the launcher, `python3.12 -m venv client\.venv` or the full
path to the interpreter works the same way. The version is a requirement; the
vendor is not.

`client/build.ps1` produces the one-folder PyInstaller artifact, runs its
start-up self-check, and packages the folder as
`client/dist/WuwaTerm-<version>-windows-x64.zip` — the same name the release
carries.

**On Windows, run the client suite directly, as the block above does.** From
the repository root, `python scripts/validate.py --client` APPENDS the client
suite to the six server steps rather than replacing them, and the runner stops
at the first failing step — so on a Windows host the server steps fail for the
platform reasons [docs/support-matrix.md](docs/support-matrix.md) records, and
the client suite you were trying to run never starts. Run it directly, from
inside `client/`, as the third line of the block above does:
`.venv\Scripts\python.exe -m pytest`. If you want the entry point's reporting,
`python scripts/validate.py --client --only client-tests` selects that one step
and skips the server ones; run it from the repository root — it changes into
`client/` itself and finds the client environment itself. On Linux
or macOS the plain `--client` form runs everything in order and is the simpler
choice.

Everything about the client that can be checked by reading text — its transport
policy, its documentation claims — is in the main suite instead, so it runs on
every pull request without a Windows runner.
[client/README.md](client/README.md) is the client's own document.

## What Makes A Pull Request Easy To Accept

- **One concern.** A small change that does one thing is reviewed in one pass. A
  large change that does four is reviewed in four, if at all.
- **Tests that would fail without it.** For a fix, the test should be red on the
  code as it stands and green with the change. A test that passes either way
  documents nothing.
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
- **An entry under `## Unreleased` in [CHANGELOG.md](CHANGELOG.md).** Say what
  changed and why, in the voice of the surrounding entries.
- **README parity.** `README.md` (English, the front page) and
  `README.zh-CN.md` (Chinese) are kept aligned: if you touch one, touch the
  other, and keep the same sections, facts and images in both. The READMEs are
  a short introduction; detail belongs in the guides listed in
  [docs/README.md](docs/README.md), not in the front page.
- **Documentation updated when behaviour changed.** A document that promises
  something the code does not do is a defect here, and there are text gates that
  say so.
- **No secrets and no generated data.** Never commit a generated SQLite
  database, upstream TextMap or game data, runtime state (`state/`,
  `state-api/`, `chat_settings.json`, `channel_replies.json`), or a `.env` file.
  The `hygiene` gate catches most of this; it is not a reason to stop looking.
- **`python scripts/validate.py` run locally**, with the result in the pull
  request description.
- Signed commits are welcome. The maintainer signs; you are not required to.

## Non-Goals

Four product decisions are settled and enforced as text by
[`scripts/check_non_goals.py`](scripts/check_non_goals.py). Please do not send a
change that adds any of them — the gate goes red and the pull request cannot
merge:

1. **No callback-URL registration for Telegram.** The bot uses long polling; see
   the decision records in [docs/adr/README.md](docs/adr/README.md).
2. **No inline-query surface.** The bot answers commands and one channel
   listener, not inline queries.
3. **No synonym layer over the dictionary.** Lookups resolve against official
   terminology, and adding a second, unofficial naming layer defeats the point
   of the project.
4. **No free-text listener beyond the single linked-channel one.** Exactly one
   listener exists — automatic forwards from the linked channel — and the gate
   asserts that it is that one and nothing else.

The gate reads the whole tree, so the banned identifiers cannot appear in prose
either. If you believe one of these decisions should change, open an issue and
argue it before writing code.

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
