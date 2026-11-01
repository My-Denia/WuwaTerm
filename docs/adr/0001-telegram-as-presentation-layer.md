# ADR 0001: Telegram as presentation layer

- Status: Accepted
- Date: 2026-08-05
- Context amended by [ADR 0009](0009-http-api-adapter.md): an HTTP adapter
  now shares the application pipeline with Telegram command routes;
  linked-channel posts retain specialized orchestration. The layering rule
  below is unchanged and is what made a second adapter possible.

## Context

The product is a self-hosted translator for Wuthering Waves official terms.
Operators and community users already live in Telegram groups and linked
channels. A separate web UI or generic HTTP API would add auth, hosting, and
client surface without improving dictionary fidelity.

## Decision

Treat Telegram (via `python-telegram-bot`) as the presentation and transport
edge only. Domain work — normalization, SQLite lookup, term locking, optional
LLM calls — lives in modules that CLI and tests can invoke without a live Bot
session.

Presentation modules:

- `src/wuwaterm/bot.py` — commands, auth, rate limits, reply I/O, polling
- `src/wuwaterm/channel.py` — linked-channel admission/delivery
- `src/wuwaterm/telegram_html.py`, `telegram_text.py` — HTML/text helpers

## Consequences

- Positive: dictionary and sentence behavior are testable offline; CLI
  `lookup` / `sentence` share domain code with the bot.
- Positive: trust boundary is explicit — Telegram text is untrusted input to
  the domain/LLM path (`docs/privacy-and-llm.md`).
- Negative: presentation modules are large and hold orchestration that could
  eventually move deeper; accepted concentration for a single-operator bot.
- Constraint: no webhook server, inline mode, or free-text DM listener as
  alternate UIs. `tests/test_bot.py` pins the registered handlers; the webhook
  part rests on code review.

## Evidence

- `src/wuwaterm/bot.py` `create_application`, `run_bot`, `translate_query*`
- `src/wuwaterm/channel.py` `channel_post_handler`
- `src/wuwaterm/cli.py` wires `run_bot` and domain commands separately
- `docs/telegram-behavior.md`, `docs/architecture.md`
