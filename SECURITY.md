# Security Policy

WuwaTerm is a personal, MIT-licensed hobby project maintained by one person.
The project provides a no-account public beta at
<https://wuwaterm.denia-official.chatgpt.site>; it uses one shared, first-come
quota pool and has no SLA. Self-hosted instances still belong to whoever
deployed them. Security reports are read and handled best-effort — please read
[What To Expect](#what-to-expect) before depending on a reply.

## Supported Versions

Fixes are made on `main` and ship in the next release. Only the latest published
release line receives them; earlier tags are historical and get nothing.

The table below applies when v0.6.0 is published. Until then, 0.5.x remains
the latest supported published line.

| Version | Supported |
|---|---|
| 0.6.x | Yes |
| 0.5.x | No |
| 0.4.x | No |
| 0.3.x | No |
| 0.2.x | No |
| 0.1.x | No |
| `main` | Fixes land here first, but `main` is not a release |

If you are running anything older than the latest release, upgrading is the
first step. Nothing is back-ported to an earlier tag.

## Reporting A Vulnerability

Report privately through GitHub. Do not open a public issue for a
vulnerability.

**Private vulnerability reporting is enabled on this repository**, so the
private path exists and is the one to use:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Fill in the form. What you write is visible to the maintainer, not to the
   public.

Direct link:
<https://github.com/My-Denia/wuwa-translate-bot/security/advisories/new>

There is no security mailbox and no other private channel — no address is
published here, deliberately, because a published address is a thing to
maintain and to leak.

If that form is unavailable to you for any reason, do **not** improvise in a
public issue. Open a **Security contact request** from the issue chooser
instead: it exists only to ask the maintainer to open a private channel, it
takes no details, and it carries a required acknowledgement that you have put
none there. Everything about the finding waits for the private channel.

A useful report says what an attacker gains, what access they need to start,
which surface it goes through (public beta Site under `site/`, Telegram bot,
HTTP API, private web layer, desktop client, or the data build), and the
smallest sequence that shows it.

## What Not To Put In A Report

Sanitize before you paste. None of the following belongs in a report, an issue,
a screenshot, or an attachment:

- Telegram bot tokens.
- OpenAI-compatible API keys, or any other provider credential.
- Device credentials — the bearer tokens the API issues, which begin `wtd1.`,
  including `WUWATERM_SITE_DEVICE_TOKEN` on the Sites Worker.
- Raw production logs. Excerpt the lines that matter and redact the rest.
- Real Telegram chat ids, user ids, or channel ids.
- The contents of a `.env` file.
- Host names, addresses, or paths that identify somebody's deployment.

Replace each with a placeholder. A report that demonstrates the problem with
invented values is a better report, not a weaker one. Secret scanning and push
protection are enabled on this repository, but neither one reads an issue body
for you — the redaction is yours to do.

## Scope

In scope:

- The code in this repository: the Telegram bot, the HTTP API under `/v1`, the
  owner-private web presentation layer, the public beta Site under `site/`, the
  desktop client under `client/`, the data-build scripts, and the packaging
  and deployment material under `deploy/`.
- The deployment guidance this project publishes, when following it as written
  leaves an instance exposed. A wrong instruction is a defect worth reporting.

Out of scope:

- Wuthering Waves game data and terminology, which are copyright Kuro Games.
  This project reads a pinned upstream data repository and redistributes none of
  it. Questions about the data itself belong upstream.
- Telegram as a platform, and anything about how the Telegram service handles a
  message once it has left this code.
- Whichever OpenAI-compatible provider an operator configures. Free-text
  sentence translation calls that endpoint only when an operator sets one up;
  the provider is theirs, not this project's.
- Somebody else's self-hosted deployment. If you found a problem in a running
  instance you do not operate, tell the operator. Only report it here if the
  cause is this project's code or its documented guidance.
- Findings that require an attacker to already hold the operator's credentials,
  the host, or the machine the desktop client runs on.

## What To Expect

This is one person's spare-time project. There is no service-level commitment,
no acknowledgement deadline, and no bounty. Reports are looked at when the
maintainer has time. A fix, when there is one, lands on `main` and goes out in
the next release, and the release notes say what changed. If a report turns out
to be out of scope or not a vulnerability, you will be told that rather than
left waiting — again, best-effort.

Public disclosure timing is not dictated here. Please give a self-hosted,
single-maintainer project a reasonable interval before publishing, and say in
the report what interval you have in mind.

## Hardening Notes For Operators

Most of what determines whether a deployment is safe is deployment-side, and it
is documented rather than repeated here:

- [Self-hosting](docs/self-hosting.md) — what an instance needs, what it must
  not be given, and how to stand one up.
- [Deployment](docs/deployment.md) — the shipped Compose files, the
  runtime/builder split, and reading the request log.
- [Privacy and the LLM path](docs/privacy-and-llm.md) — what leaves the host,
  when, and to whom.

Four properties of the shipped configuration are worth knowing before you change
it:

- The API service binds loopback under the shipped Compose file. It is meant to
  be published, if at all, over HTTPS by a reverse proxy in front of it — not by
  moving the bind address.
- The credential store keeps only a salted scrypt verifier, and the service
  never prints credential material at all. The operator supplies the secret on
  standard input, `device issue` prints only the device id and its metadata, and
  the token is `wtd1.` plus that device id plus the secret the operator supplied
  — so the secret has to be kept at the moment it is generated. Nothing recovers
  it from the store afterwards.
- The web presentation layer is off by default and is owner-private when on. It
  is not a public front end and turning it on does not make it one.
- The public beta Site under `site/` is a separate Hosted proxy. It is not the
  in-process web layer. Application code does not authenticate the visitor;
  the Worker holds a device token that can spend `/v1` quota. See
  [Public Beta Site](docs/sites.md).
- Nothing in this repository distributes generated databases, upstream game
  text, or runtime state. If your deployment is committing any of those, that is
  a local misconfiguration. `python scripts/validate.py` catches most of it —
  a database by its content as well as its name, and the runtime state paths by
  name — but its upstream-game-text check is by SIZE, above 1 MB, so a small
  upstream file passes it. A green run is not a licence to commit game data.

## Related Documents

- [SUPPORT.md](SUPPORT.md) — where non-security questions go.
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to send a change, including a fix.
