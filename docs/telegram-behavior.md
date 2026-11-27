# Telegram Behavior (Retired from WuwaTerm Core)

The legacy inline Telegram bot (`wuwaterm bot`, `src/wuwaterm/bot.py`,
`src/wuwaterm/channel.py`, `src/wuwaterm/telegram_html.py`,
`src/wuwaterm/telegram_text.py`, and the `wuwaterm[telegram]` extra) has been
**retired from WuwaTerm Core**.

## Where Telegram Behavior and Interaction Specs Live Now

All active Telegram bot behavior, commands (`/tr`, `/term`, `/sentence`,
`/review`, `/public`), linked-channel auto-translation, HTML structure
protection, and message splitting rules now live in the standalone repository:

- Repository: [My-Denia/WuwaTerm-Telegram](https://github.com/My-Denia/WuwaTerm-Telegram)
- Deployment from Core: `./deploy/setup.sh --with-telegram` (see
  [Self-Hosting](self-hosting.md) and [Products](products.md))

> **Note (MY-360):** Earlier revisions of this document described an obsolete
> 2,000-character independent chunk-splitting scheme that no longer reflects
> current Telegram adapter behavior. Refer to the standalone
> `My-Denia/WuwaTerm-Telegram` documentation for authoritative message handling
> and chunking specifications.

## Runtime & CLI Retirement Behavior

- Running `wuwaterm bot` or `docker run --rm <runtime-image> bot` exits with
  status `64` and prints migration guidance pointing to
  `My-Denia/WuwaTerm-Telegram`.
- Core no longer ships `python-telegram-bot` or any Telegram presentation
  modules (`src/wuwaterm/protected_text.py` and `src/wuwaterm/normalize.py`
  remain in Core as protocol-neutral text and structure-protection primitives).

## Rollback to the Last Inline-Bot Release

If you still require the retired inline Telegram bot (`wuwaterm-bot`), pin or
roll back to Core release **`v0.16.0`**, which is the final release tag
carrying the legacy inline Telegram adapter and `wuwaterm[telegram]` extra:

- Release tag: [v0.16.0](https://github.com/My-Denia/WuwaTerm/releases/tag/v0.16.0)
