<p align="center">
  <a href="https://wuwaterm.denia-official.chatgpt.site"><img src="docs/assets/readme/hero.png" alt="WuwaTerm: get Wuthering Waves terms right, in Chinese and English"></a>
</p>

<p align="center">
  Official Chinese and English terminology for the game Wuthering Waves (鸣潮): look up a term, translate a sentence without the names getting paraphrased, or check a translation you already have.
  <br>
  查询《鸣潮》中英官方术语，整句翻译时保留官方译名，并用词典核对已有译文。
</p>

<p align="center">
  <a href="https://wuwaterm.denia-official.chatgpt.site"><b>Try&nbsp;the&nbsp;public&nbsp;beta</b></a>&nbsp;·
  <a href="#run-your-own"><b>Self-host</b></a>&nbsp;·
  <a href="#windows-client">Windows&nbsp;client</a>&nbsp;·
  <a href="#http-api">HTTP&nbsp;API</a>&nbsp;·
  <a href="docs/README.md">Docs</a>
  <br>
  <b>English</b> · <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/My-Denia/WuwaTerm/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/My-Denia/WuwaTerm?style=flat-square&label=release&labelColor=30363d&color=b1e8c6"></a>
  <a href="https://github.com/My-Denia/WuwaTerm/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/My-Denia/WuwaTerm/ci.yml?branch=main&style=flat-square&label=CI&labelColor=30363d"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-b1e8c6?style=flat-square&labelColor=30363d">
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-b1e8c6?style=flat-square&labelColor=30363d"></a>
</p>

> [!NOTE]
> WuwaTerm is an unofficial, independent fan project. It is not affiliated with, authorized by, or endorsed by Kuro Games, and it uses no official game art. Wuthering Waves game data and in-game terminology are © Kuro Games.

## Why WuwaTerm

WuwaTerm is for anyone who reads or writes about Wuthering Waves across Chinese and English: fan translators, guide and wiki writers, and community admins.

A general-purpose translator sees game names as ordinary words, so an official name can come back paraphrased, or rendered differently from one sentence to the next. WuwaTerm checks a dictionary built from the game's own text data first. When the whole query is a term, you get the dictionary's official string back byte for byte, with no model involved. When it is a sentence, the matching terms are locked before any text reaches a language model and put back unchanged afterwards.

<p align="center">
  <a href="https://wuwaterm.denia-official.chatgpt.site"><img src="docs/assets/readme/screenshot-workbench.png" alt="The WuwaTerm public beta: looking up 声骸 returns the official English Echo, and translating 今汐装备了声骸 keeps both official names"></a>
  <br>
  <sub>The public beta, captured 2026-09-13 (the interface then offered Chinese only; it now offers Chinese and English). You can type in either language. 声骸 is listed twice because the game uses the term in two categories.</sub>
</p>

## What you can do

- **Look up a term in either direction.** Type Chinese or English and get the official pair. Short romanised queries such as `shenghai` can also match by pinyin.
- **Translate a sentence with the dictionary's terms.** The direction is detected from the script, or you choose it. Official terms are supplied to the model with their translations; a word that is also an ordinary word keeps its sentence meaning, and English output takes the number and article the sentence needs.
- **Review a translation you already have.** Paste the source and your translation; WuwaTerm shows which official terms are confirmed and which need a look, and offers the official pair for each. It checks terminology only, not sentence meaning, and never calls a model.
- **Pick up where you left off.** The review workbench downloads your draft as a manuscript file on your own computer, and you import it later to continue. The page cannot confirm the download reached disk. Ordinary leave and refresh ask first when the work cannot be restored from the manuscript imported in this visit. A crash or forced quit can still discard the page. There are no accounts and no cloud copy of your manuscript.

<p align="center">
  <img src="docs/assets/readme/screenshot-review.png" alt="The review workbench: 今汐 is confirmed as Jinhsi, while 声骸 needs a look and the official Echo is offered">
  <br>
  <sub>Review on the public beta: “Jinhsi” is confirmed; no official rendering of 声骸 is found in the translation, so the workbench offers “Echo”. Where it had to shorten a candidate list, the report says so instead of claiming a complete check. The image is cropped.</sub>
</p>

### Where it runs

| Surface | What it does | What you need |
| --- | --- | --- |
| Public web beta | Lookup, translation, review, manuscript files | A browser |
| Telegram bot | Lookup, translation, auto-translating a linked channel | Your own server and bot token |
| Windows client | Lookup, translation, review, manuscript files | A server address and device token |
| HTTP API | Lookup, translation, review | Your own server |

<sub>Outside the public beta, sentence translation also needs an OpenAI-compatible model configured on the server.</sub>

## How it works

<img src="docs/assets/readme/how-it-works-en.png" alt="How WuwaTerm works: dictionary hits return official strings without a model; free text is translated by an OpenAI-compatible model with the dictionary's terms supplied; the dictionary is built locally from pinned public game-text data">

Telegram commands and the HTTP API share one dictionary-first application layer, and the public beta and the Windows client reach it through the API. Linked-channel auto-translation has its own orchestration over the same dictionary lookup and term selection. The terminology database is a local SQLite file you build from a pinned public copy of the game's text data; it is never shipped in a release or container image. The model is optional: without one, dictionary lookups still work. Modules, trust boundaries and request flows are mapped in [Architecture](docs/architecture.md).

## Get started

### Try it now

Open the [public beta](https://wuwaterm.denia-official.chatgpt.site). There is no sign-up. The interface offers Chinese and English, first visits start in Chinese, and you can type English; lookups show both languages. Everyone shares the same first-come daily limits, counted separately for lookups, translations and reviews and reset at 00:00 UTC, so it can be busy or run out, and there is no SLA. Please do not paste personal or sensitive text. Limits are in [Public Beta Site](docs/sites.md) and privacy details in [Privacy and LLM](docs/privacy-and-llm.md#anonymous-public-beta-site).

### Run your own

One command does the first install and each later addition:

```bash
./deploy/setup.sh                  # shared translation service (Core API)
./deploy/setup.sh --with-telegram  # hand the bot to its own product
./deploy/setup.sh --with-web --web-origin https://wuwaterm.example
                                   # add WuwaTerm-Web behind your proxy
./deploy/setup.sh --status         # versions, components and health
```

The Telegram bot is a separate product, [WuwaTerm-Telegram](https://github.com/My-Denia/WuwaTerm-Telegram), with
its own release, verified artifacts and Compose file. `--with-telegram` checks
this installation, gives the bot a copy of the database and writes its `.env`
when you point it at that checkout (`--telegram-dir`), then prints the
product's own install steps. This checkout no longer starts the inline bot it
still carries; that became an explicit `--with-legacy-bot`.

A clone of the Core v0.9.0 tag prints the install steps of Telegram v0.2.10
and a clone of the v0.9.1 tag those of Telegram v0.3.0 (which pins Core
v0.9.0), the release each tag records. `main` records Telegram v0.6.1, and the
`v0.15.0`, `v0.14.1` and `v0.14.0` tags record Telegram v0.6.0 (both pin Core
v0.13.0); the `v0.13.0` tag records Telegram v0.5.0 (pinning Core v0.12.0); the
historical `v0.10.2`, `v0.10.3`, `v0.11.0` and `v0.12.0` tags record Telegram
v0.4.1 (pinning Core v0.10.1), while the `v0.10.1` tag was
frozen before v0.4.1 shipped and still records v0.4.0 (pinning Core v0.10.0).
A checkout of those earlier tags can either run from `main` or pass the newer
release explicitly:

```bash
WUWATERM_TELEGRAM_VERSION=v0.6.1 ./deploy/setup.sh --with-telegram
```

The v0.10.0 tag predates both and still names v0.3.1, which is what the v0.9.2
tag records as well and pins Core v0.9.1.

The variable always names a published tag, never `latest`.

The Web option also defaults to the release the checkout records: a clone of
the Core v0.9.0 tag installs Web v0.2.0, which rejects manuscript v3 files, and
the v0.9.1, v0.9.2 and v0.10.0 tags record Web v0.3.1. `main` records Web
v0.6.0, the v0.15.0 tag records Web v0.5.1, the v0.14.1 tag records Web v0.5.0,
the v0.14.0 tag records Web v0.4.5, the v0.13.0 tag records Web v0.4.4, the
v0.10.3, v0.11.0 and v0.12.0 tags record Web v0.4.3, and the v0.10.1 and
v0.10.2 tags record Web v0.4.1, so a checkout of any earlier tag
passes the release it lacks explicitly:

```bash
WUWATERM_WEB_VERSION=v0.6.0 ./deploy/setup.sh --with-web --web-origin https://wuwaterm.example
```

The [self-hosting guide](docs/self-hosting.md) explains what each step does and
lists the explicit commands: Docker Compose on Linux (or Python 3.11+ from
source), about 2 GB of disk for the upstream data, and your own Telegram bot
token and OpenAI-compatible endpoint if you want those features. The Web
option installs the published
[WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web/releases) package this
checkout records (v0.6.0 on `main`, v0.5.1 in the v0.15.0 tag, v0.5.0 in the
v0.14.1 tag, v0.4.5 in the v0.14.0 tag, v0.4.4 in the v0.13.0 tag, v0.4.3 in the
v0.10.3, v0.11.0 and v0.12.0 tags, v0.4.1 on the v0.10.1 and v0.10.2 tags, v0.3.1 in the v0.10.0, v0.9.1 and
v0.9.2 tags, v0.2.0 on the v0.9.0 tag), verifies it against the release checksums,
and needs the HTTPS origin your proxy serves (`--web-origin`).

### Windows client

Download the portable package from [WuwaTerm-Windows](https://github.com/My-Denia/WuwaTerm-Windows/releases/latest) (`v0.8.0` is the latest release). The [Windows repository](https://github.com/My-Denia/WuwaTerm-Windows) owns the guide, source, builds, releases and desktop issues. The unsigned package can trigger Windows SmartScreen. It connects to a server through the device-token-protected API.

### Telegram bot

There is no shared public bot. The bot is its own product in [WuwaTerm-Telegram](https://github.com/My-Denia/WuwaTerm-Telegram): it runs on your server with your own BotFather token, installs from that repository's verified release artifacts, and reads a terminology database this repository builds. Its README is the install authority; [self-hosting](docs/self-hosting.md) explains how the two fit together and what `--with-telegram` prepares. Then authorize the groups it may serve.

| You send | The bot replies with |
| --- | --- |
| `/tr 声骸` | `Echo` |
| `/tr Echo` | `声骸` |
| `/tr --to en 今汐装备了声骸` | the sentence in English with official terms |

Commands, group authorization and linked-channel auto-translation are covered in [Telegram Behavior](docs/telegram-behavior.md).

### HTTP API

Versioned `/v1` routes cover terms, translations and reviews, each authenticated with a revocable device token. The committed contract is [`docs/api/openapi.json`](docs/api/openapi.json), and the self-hosting guide walks through a [first lookup and translation](docs/self-hosting.md#first-lookup-first-translation).

## Documentation

| For users | For self-hosters | For contributors |
| --- | --- | --- |
| [Public Beta Site](docs/sites.md) | [Self-Hosting](docs/self-hosting.md) | [Contributing](CONTRIBUTING.md) |
| [Windows client](https://github.com/My-Denia/WuwaTerm-Windows) (in Chinese) | [Support Matrix](docs/support-matrix.md) | [Architecture](docs/architecture.md) |
| [Telegram Behavior](docs/telegram-behavior.md) | [Data Refresh](docs/data-refresh.md) | [Decision records](docs/adr/README.md) |
| [Privacy and LLM](docs/privacy-and-llm.md) | [HTTP API contract](docs/api/openapi.json) | [Validation](docs/validation.md) |

Every guide is listed in the [documentation index](docs/README.md).

## Under the hood

Public Web source and its Node CI live in [WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web). This repository keeps the Python service, the deprecated inline bot, the dictionary builder, and deployment tools; [WuwaTerm-Windows](https://github.com/My-Denia/WuwaTerm-Windows) owns the desktop product, and [WuwaTerm-Telegram](https://github.com/My-Denia/WuwaTerm-Telegram) owns the supported Telegram bot.

- **Python 3.11+** with python-telegram-bot (long polling) and FastAPI, over a SQLite dictionary that the serving containers mount read-only.
- **Architecture rules that CI enforces**: the application layer imports no presentation code, and the API contract is drift-checked against a committed OpenAPI snapshot ([ADR 0009](docs/adr/0009-http-api-adapter.md)).
- **Revocable device credentials** for the API, stored only as salted scrypt verifiers ([ADR 0010](docs/adr/0010-device-principal-authentication.md)).
- **Transactional updates** in the maintainer's deployment tooling: the new image, and for data updates a candidate database, is verified before the running service is touched, and an ordinary failure restores the previous image, database and commit pointer ([ADR 0008](docs/adr/0008-candidate-verification-and-transactional-deployment.md)).
- **A Windows client** in PySide6 that holds no translation logic ([ADR 0011](docs/adr/0011-pc-client-stack.md)), and **a public beta** built as a Cloudflare Worker that proxies the same API.

## Project status

WuwaTerm is a personal hobby project, maintained on a best-effort basis. The latest tagged release is on the [Releases page](https://github.com/My-Denia/WuwaTerm/releases); `main` moves ahead of it, and [CHANGELOG.md](CHANGELOG.md) marks what is unreleased. The public beta promises no fixed availability or response time. Where to ask questions and what to expect is in [SUPPORT.md](SUPPORT.md).

## Contributing

- **Getting a wrong result?** Open a [bug report](https://github.com/My-Denia/WuwaTerm/issues/new/choose) with your query, what WuwaTerm returned and what the game shows. A term that is wrong in the game data itself has to be fixed upstream ([SUPPORT.md](SUPPORT.md)).
- **Want to change code or docs?** Read [CONTRIBUTING.md](CONTRIBUTING.md). Local validation has one entry point: `python scripts/validate.py`.
- **Found a security problem?** Report it privately as described in [SECURITY.md](SECURITY.md).

## License and data

The source code is released under the [MIT License](LICENSE), © 2026 My-Denia. The license covers this project's code only. Wuthering Waves game data and in-game terminology are © Kuro Games and are not redistributed here: the dictionary is built on your own machine from a pinned public data source, as described in [Data Refresh](docs/data-refresh.md). The WuwaTerm logo and the images in this README are original to this project or screenshots of its own interface, and contain no game art ([asset notes](docs/assets/README.md)).
