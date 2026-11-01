# Support

WuwaTerm is a personal, MIT-licensed hobby project with a single maintainer. It
is offered as-is, and help is best-effort: questions are answered when there is
time to answer them, and some will not be answered at all. That is the honest
version, and it is better to say it here than to leave you waiting on a promise
that was never made.

The project provides a no-account, shared public beta at
<https://wuwaterm.denia-official.chatgpt.site>. It is a best-effort entry for
term lookup, bidirectional sentence translation and translation review, not a
managed commercial service: everyone shares the same first-come daily limits,
one visitor can exhaust them, requests may be busy or fail, and there is
**no SLA**. Self-hosted deployments
remain separately operated by whoever runs them.

There is **no private support channel**: no support mailbox, no chat, no direct
messages. Everything happens in public issues, with one exception, which is
security.

## Where To Ask

Everything starts here:
<https://github.com/My-Denia/wuwa-translate-bot/issues/new/choose>

| If | Use |
|---|---|
| The public beta is reachable but behaves wrongly — a wrong lookup/translation path, a broken page, or a repeatable error | **Bug report**; choose **Public beta Site** and include the on-screen request ID if one was shown, but never paste input containing personal or sensitive data |
| A self-hosted surface behaves wrongly — a wrong response, a crash, or a surface that misbehaves | **Bug report** |
| You cannot install it, build the dictionary, start a service, issue a device credential, or get the desktop client to talk to the API | **Installation or setup problem** |
| You want the project to do something it does not do | **Feature request** — read the non-goals in [CONTRIBUTING.md](CONTRIBUTING.md#non-goals) first |
| You found a vulnerability | **Not an issue.** Follow [SECURITY.md](SECURITY.md) — report it privately through the Security tab, which is enabled. If that form is unavailable to you, the **Security contact request** form asks the maintainer to open a private channel and deliberately takes no details |

GitHub Discussions is not enabled on this repository, so a question is an issue
too. Use the bug-report form and say plainly that it is a question; a question
answered in public is a question the next person can find.

There is deliberately no blank-issue option. Each form carries a required
acknowledgement that credentials, real Telegram identifiers and anything that
identifies your deployment — a host name, an address, a path — have been
removed from whatever you paste, and an empty issue would be a way past it —
a token published in a public issue has to be revoked, and cannot be unpublished.
Fields that do not apply to you can be answered with "not applicable".

Before opening anything, please check the existing open and closed issues. Many
of the sharp edges are already written down, some with a decision attached.

## Read First

A good share of what gets asked is already documented:

- [README.md](README.md) — what the project is, in English, and
  [README.zh-CN.md](README.zh-CN.md) in Chinese.
- [docs/README.md](docs/README.md) — the index of every guide in the
  repository.
- [docs/self-hosting.md](docs/self-hosting.md) — standing up your own instance.
- [docs/support-matrix.md](docs/support-matrix.md) — which operating systems,
  Python versions and surfaces are supported, and which are merely known to
  exist.
- [docs/deployment.md](docs/deployment.md) — the shipped Compose setup, the
  runtime and builder split, reading the request log.
- [docs/validation.md](docs/validation.md) — how to check an installation with
  `python scripts/validate.py`.
- [docs/telegram-behavior.md](docs/telegram-behavior.md) — what the bot does and
  does not respond to.
- [docs/privacy-and-llm.md](docs/privacy-and-llm.md) — what leaves the host and
  when.
- [docs/sites.md](docs/sites.md) — the anonymous public beta, shared quota,
  Hosted/VPS trust boundary, privacy limits and current service caveats.
- [docs/web-presentation-layer.md](docs/web-presentation-layer.md) — the
  owner-private web layer, which is off by default.
- [docs/data-refresh.md](docs/data-refresh.md) — refreshing the dictionary from
  upstream game data.
- [client/README.md](client/README.md) — the Windows desktop client.

## What To Include In A Report

The issue forms ask for these because a report without them usually cannot be
acted on:

- **Version, tag, or observation time** — for the public beta Site, give the
  date and approximate UTC time you observed the problem; for self-hosting,
  give the release tag or commit you are running.
- **Which surface**: the public beta Site, Telegram bot, HTTP API, private web
  layer, desktop client, or data build.
- **Environment or browser**: for the public beta Site, give the operating
  system and browser version; for self-hosting, give the operating system,
  Python version, and whether you installed from source or run the containers.
- **Steps** that lead to it, in order, small enough to follow.
- **Expected versus actual** — both, explicitly. "It does not work" names
  neither.
- **Logs or error output**, trimmed to the part that matters.

**Redact before you paste.** Remove Telegram bot tokens, OpenAI-compatible API
keys, device credentials (the bearer tokens beginning `wtd1.`), real Telegram
chat or user ids, `.env` contents, and anything that identifies your host.
Replace each with a placeholder. Every issue form carries a required checkbox
about this, because a token pasted into a public issue is a token that has to be
rotated.

## Out Of Scope

These are not things this project can help with:

- **Game data and terminology questions.** Wuthering Waves data and terms are
  copyright Kuro Games. This project reads a pinned upstream data repository and
  redistributes none of it; if a term is wrong or missing upstream, it is wrong
  or missing here, and the fix belongs upstream. What this project can fix is a
  wrong *lookup*, which is a bug report.
- **Telegram platform problems.** Rate limits, delivery, account or channel
  restrictions, and anything about how Telegram itself behaves are between you
  and Telegram.
- **LLM provider problems.** Free-text sentence translation calls an
  OpenAI-compatible endpoint only if an operator configures one. Its quota,
  billing, latency, model behaviour and outages belong to that provider.
- **Guaranteed public-beta capacity or correctness.** Shared limits bound
  aggregate use, not individual access. They do not promise fairness, uptime,
  latency or that every model translation is correct; report a repeatable
  project defect, but do not treat the beta as an SLA-backed service.
- **Operating somebody's deployment for them.** Nobody here has access to your
  host, your tokens or your logs, and no one will ask for them. Help looks like
  a documented answer, not a remote hand.
- **Requests that cross a non-goal.** Four product decisions are settled; see [Non-Goals](CONTRIBUTING.md#non-goals).

## If You Want To Help

Reporting a defect well is help. So is fixing one — see
[CONTRIBUTING.md](CONTRIBUTING.md) for the setup and the single validation
command.
