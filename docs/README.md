# WuwaTerm documentation

Every guide in this repository, grouped by what you are trying to do. The
[project README](../README.md) ([简体中文](../README.zh-CN.md)) is the short
introduction; the pages below carry the detail. Most are in English; the
desktop client guide and a few design and audit records are in Chinese.

## Using WuwaTerm

- [Public Beta Site](sites.md): links to the independent Web repository, product
  guide, privacy boundary, cross-repository contracts and deployment receipts.
- [Windows client](https://github.com/My-Denia/WuwaTerm-Windows): the independent desktop app, release, build and support guide.
- [Telegram Behavior](telegram-behavior.md): commands, direction flags, group
  authorization, public mode, linked-channel auto-translation and
  Telegram-specific limits.
- [Privacy and LLM](privacy-and-llm.md): what leaves the host and when, model
  configuration, the prompt-injection guard, placeholder integrity and secret
  handling.

## Running your own

- [Self-Hosting](self-hosting.md): the generic path from a checkout at a release
  tag to a first lookup, containers or source, the data build, device
  credentials, publishing over HTTPS, upgrade, backup, restore and rollback.
- [Support Matrix](support-matrix.md): tested Python versions and platforms,
  the client-to-API compatibility contract, and what "supported" means here.
- [Data Refresh](data-refresh.md): the pinned game-data source, building and
  verifying the terminology database, and the data licence boundary.
- [HTTP API contract](api/openapi.json): the committed OpenAPI snapshot for the
  versioned `/v1` routes.

## Contributing and maintaining

- [Contributing](../CONTRIBUTING.md): setting up, the single validation
  command, and what makes a pull request easy to accept.
- [Architecture](architecture.md): modules, request flows, trust boundaries and
  the single-instance topology.
- [Architecture Decision Records](adr/README.md): why the system is shaped the
  way it is.
- [Validation](validation.md): what `python scripts/validate.py` runs, the
  candidate-database checks, and live smoke caveats.
- [Release Checklist](release-checklist.md): what a release carries and how a
  draft is built, read back and published.
- [Changelog](../CHANGELOG.md): notable changes by release, including what is
  on `main` but unreleased.
- [Support](../SUPPORT.md) and [Security](../SECURITY.md): where to ask, what to
  expect, and how to report a vulnerability privately.

## Maintainer operations and records

- [Deployment](deployment.md): the maintainer's own production runbook for one
  specific host, with the transactional updater, state migration and smoke
  checks. Self-hosters should start from [Self-Hosting](self-hosting.md)
  instead.
- [Web Presentation Layer](web-presentation-layer.md): the owner-private
  browser interface inside the API process. Off by default, not the public
  beta, and an extension of the [Deployment](deployment.md) runbook.
- [Desktop client UI redesign](client-ui-redesign.md): the historical design proposal
  for the client interface. In Chinese.
- [Repository audit](repo-audit.md): a dated, read-only inventory of the tree at
  one commit. In Chinese.
- [Shared public beta design](superpowers/specs/2026-09-02-wuwaterm-shared-beta.md):
  the design behind the anonymous, shared-pool public beta.
- [Repository assets](assets/README.md): where the README images come from and
  how to render them again.
