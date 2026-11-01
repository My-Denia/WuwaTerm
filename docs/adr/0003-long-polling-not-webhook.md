# ADR 0003: Long polling (not webhook)

- Status: Accepted
- Date: 2026-08-05
- Still in force. [ADR 0009](0009-http-api-adapter.md) amends one CONTEXT
  sentence only: the deployment does now have an inbound HTTP surface, the
  API adapter's. Chat updates are still received by long polling, and this
  decision is not affected by it.

## Context

Telegram bots can receive updates via long polling (`getUpdates`) or webhook
HTTPS endpoints. Webhooks need a public URL, TLS, reverse proxy, and careful
multi-instance semantics. This deployment is a single VPS hobby service with
host networking and no public application HTTP surface.

## Decision

Use long polling only: `Application.run_polling()` after handler registration.
Do not implement webhook delivery or document webhook HA.

## Consequences

- Positive: no inbound HTTP listener for Telegram; simpler firewall and image.
- Positive: matches single active consumer assumption (one token, one process).
- Negative: two concurrent pollers on the same token are unsafe (update races);
  multi-instance is unsupported rather than “scale out with sticky webhooks”.
- Enforcement: `tests/test_bot.py` pins the registered handlers. The former
  wording scanner `scripts/check_non_goals.py` was retired in 2026-11.

## Evidence

- `src/wuwaterm/bot.py` `run_bot` → `app.run_polling()`
- `deploy/docker-compose.yml` `command: ["bot"]`, no webhook env
- `docs/deployment.md` (“uses long polling … does not configure webhook”)
- `tests/test_bot.py`
