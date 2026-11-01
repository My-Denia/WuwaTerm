# translatebot Project Rules

Project-scoped addendum to the owner's shared rules; angle-bracket paths are
placeholders resolved from local Git configuration and host bindings. Deploy
needs separate current authorization. A fast-forward push on the same PR branch
that only fixes a confirmed in-scope review finding is already authorized; that
never covers force-push, history rewrite, a wider scope, deploy, or a production
change.

## Code Review Rules

Severity and scope are separate questions.

Severity asks how bad a finding is if it is true. Scope asks whether
this pull request has to fix it now. A severity label does not decide
scope. A finding can be real and important and still be a follow-up.

Treat a finding as a blocker for the current pull request only when one
of these is true:

- this pull request introduced the defect
- this pull request caused a regression
- a behavior this pull request states does not hold
- this pull request caused a correctness, security, data-integrity, or
  destructive-risk problem
- the fix is required by the contract this pull request already states

When a problem was not introduced here and does not break that contract,
record it as a follow-up. That includes a pre-existing defect, an
adjacent problem that already existed, theoretical hardening, a deeper
architecture invariant, a new capability, a broader refactor, unrelated
cleanup, polish, and work justified only by already being nearby.

If the same pull request keeps producing many findings of one kind that
are inside the current scope, stop the one-by-one patch treadmill. Say
that the implementation, plan, abstraction, or verification may be
failing as a whole, and ask for one systematic audit of that concern
before more patches. That is not permission to add unrelated scope.

A wrong comment, or a comment outside this pull request's scope, is not
an instruction to change the pull request. Answer it in the thread.

## Development Host

- The only local checkout is this WSL tree, `<LOCAL_CHECKOUT>`, on the Linux
  filesystem. Do not develop via `/mnt/c/...` and do not open a second Windows
  checkout.
- Windows client work is paused: `client/` stays in the repo and CI still
  builds it, but local Qt, Credential Manager and `build.ps1` are out of scope
  until the owner resumes it.
- The server test suite is unsupported on a native Windows host (directory
  `fsync` and subprocess handle failures). Local gate:
  `python scripts/validate.py` (optional `--client`). The server needs
  Python >= 3.11; this checkout's `.venv` is POSIX (`bin/`).

## Product Decisions

- Stay on Python; no full Go/Rust rewrite. A narrow native extract needs
  profiling evidence.
- Do not optimize without a fresh production baseline and pre-frozen success
  criteria. If WuwaTerm is not the bottleneck, close with no-change instead of
  shipping low-value diffs.
- Keep bot and API as separate processes. Do not add microservices,
  Kubernetes, Redis, Kafka, or PostgreSQL. Do not redo the v0.4.0 open-source
  productization campaign. Do not bump game data unless production provenance
  disagrees with the release.
- Do not send test messages to Telegram chats, groups, or channels; API/Web
  probes, `getMe`, and natural telemetry are allowed.
- Do not assume a large Telegram channel fails translation because of rate
  limits; inspect silent skip, capacity, and budget-exhaustion paths (channel
  LLM admission can skip posts on `llm_budget` / `queue_full`). Fuzzy lookup
  scoring the full terms table is an unproven CPU backlog; idle bot+api use
  about 61 MiB (`goal-runs/vps-mem-opt-20260821`, closed no-change).
- Owner-private web UI is intentionally simple; do not over-invest in visual
  redesign.
- CI and tests should catch real defects, not grow for volume. When CI is
  already red, fix it immediately.
- After an authorized review-fix commit is on the PR, resolve the GitHub review
  threads that change actually fixed. Do not resolve skipped, frozen, or
  still-disputed threads. Do not @ bots.
- Do not commit generated term DBs, upstream TextMaps, secrets, raw tokens,
  real Telegram identifiers, or unsanitized production logs. Swap/zram is only
  an OOM cushion, not a way to hide resident-memory problems.
- This repo squash-merges with `(#NN)`. Do not force-push main or use admin
  bypass. Codex PR review is auto-triggered; do not comment `@codex review`.

## Project Facts

- GitHub repo `My-Denia/WuwaTerm`. WuwaTerm is a dictionary-first official
  Wuthering Waves term service: one protocol-neutral application layer,
  Telegram and versioned HTTP API adapters, an owner-private in-process web UI
  (off by default), and a paused Windows desktop client that consumes the API
  with no translation logic.
- Since 2026-11-01 the public Web lives in `My-Denia/WuwaTerm-Web`; this
  repository no longer tracks `site/`. `contracts/web-ref.txt` pins the
  external consumer for metadata and label contract CI. Windows packaging uses
  `client-release.yml`, independent of backend `release.yml`. This is source
  organization, not evidence of a new release or production deployment.
- Long-running services are `wuwaterm-bot` and `wuwaterm-api`; the builder is
  on-demand. Production uses a locally built runtime image, not GHCR. Reverify
  live release and deploy state before acting.

## Git Branches

- Use conventional branch prefixes (`fix/`, `feat/`, `chore/`, `docs/`) named
  after the work, e.g. `fix/translation-limit-2000`. Do not create branches
  named after the agent or tool, including `codex/*`. Project rules override
  generic publish skills that suggest `codex/*` branches or `[codex]` titles.

## Workspace cleanup when finishing

When local agents finish work, they clean up the workspace: archive reusable content first, then delete merged or obsolete local branches and worktrees. A branch is obsolete when it is an ancestor of `origin/main`, `git cherry origin/main <branch>` prints only `-` lines, or its pull request is closed. Disk space is shared by several projects, so leftovers fill it up. (Owner ruling, 2026-10-15: 「删分支是项目设置，规则有没有不重要，本地agent整理工作区删分支工作树归档可复用内容很重要，因为我的储存就这么点…不是只有一个项目在占用资源」)

## Main, PRs, and Merge Semantics

- This is a public repository: change `main` through PRs.
- Do not confuse patch equivalence with commit reachability. A GitHub squash
  merge puts equivalent content on `main` but does not make the feature branch
  commits ancestors of `main`. If asked whether work is "merged to main",
  report both when they differ.
- Signed commits are preferred. Do not point the signing key at a Windows path
  via `/mnt/c`: DrvFs shows mode 0777 and `ssh-keygen -Y sign` refuses it.
- When merging by API/connector, pass the expected head SHA so a moved PR head
  cannot be merged accidentally.

## PR Review

- Do not claim a reviewer approved unless there is a returned bot comment,
  review, or successful review workflow for the current head SHA. If a trigger
  comment (e.g. `@claude review`) gets no response, report that absence instead
  of waiting indefinitely.

## VPS Deployments

- Use the saved `<SAVED_VPS_PROFILE_SKILL>` workflow/helper for this server.
  Before deploying to `<REMOTE_DEPLOY_ROOT>`, verify the source commit, current
  `origin/main`, and whether the commit is already on `main`. Deployable code
  should be on `origin/main` before the task is complete; state any exception.
- Preserve the remote `.env` and `data/` during deploys. Never print secret
  values.
- After writing `.deploy_commit`, read it back with `cat .deploy_commit` and
  compare it to the intended SHA exactly; helper quoting mistakes can produce a
  marker that only looks close.
- The deploy reachability smoke is `python scripts/deploy_smoke.py` inside the
  compose service, not a `deploy-smoke` entrypoint command. A missing
  `TELEGRAM_TEST_CHAT_ID` means `sendMessage` is skipped, not that `getMe`
  failed.
- After deploy, verify at minimum: container status, restart count, recent
  sanitized error-log scan, DB hash, `.env` mode, `.deploy_commit`, and absence
  of leftover staging/archive/run containers. Keep the remote backup path and
  deployed commit SHA in the closeout.

## Privacy and Secret Checks

- Inspect the outgoing diff before commit, push, PR, or deploy. If a secret or
  private identifier appears, remove it before committing; if it was already
  pushed, report the exact commit/ref and assume rotation or history repair may
  be required.
- Do not commit or expose real Telegram bot tokens, API keys, `.env` contents,
  owner/user ids, private chat ids, credentials, local credential paths,
  passwords, cookies, or full private endpoint URLs. Use placeholders in docs
  and tests.
- Treat numeric Telegram chat ids, user ids, hostnames, IPs, local Windows/WSL
  user paths, and deployment logs as sensitive unless they are already
  intentionally public in this repository.
