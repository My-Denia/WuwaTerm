# translatebot Project Rules

This file augments the user's global agent rules for this repository. It is
project-scoped: do not promote these repo-specific facts into global memory
unless the user explicitly asks.

This tracked file is the project-rules surface for this repository:
product facts plus the WSL Git/VPS host adapter. Paths in angle brackets are
placeholders; resolve their actual values from local Git configuration, host
bindings, and installed profile skills before use. Keep private values local.
As of 2026-09-11 the owner
deleted the Windows checkout `<FORMER_WINDOWS_CHECKOUT>`. SUPERSEDED: that
path's `AGENTS.md` as a separate canonical product-fact layer this adapter
must not absorb. Do not recreate the Windows tree unless the owner asks.
PR creation, merge, deploy, and local or remote branch deletion each
require separate current authorization for that exact action. After the
owner has authorized a pull request, a later fast-forward push on that
same branch is already authorized when it only fixes a confirmed
in-scope review finding, as defined in Code Review Rules. The procedures
below never authorize force-push, history rewrite, a wider scope, a new
pull request, merge, deploy, or a production change.

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

## Pull request fix push

After the owner has authorized a pull request, a later fast-forward push
on that same branch is already authorized when all of these hold:

- the review finding is confirmed
- the finding is inside the current pull request's scope, as defined above
- the push only fixes that finding
- the push does not widen the pull request's concern

That continuation does not authorize force-push, history rewrite, a
wider scope, unrelated cleanup, a new pull request, merge, deploy,
release or tag, registry promotion, production mutation, a backend
restart, a production database refresh, or env, proxy, or secrets
changes.

A finding that reaches outside the original scope is not a reason to do
that work under the existing authorization.

## Development Host

- The only local checkout is this WSL tree:
  `<LOCAL_CHECKOUT>` on the Linux filesystem.
- As of 2026-09-11 the owner paused Windows client work and deleted the
  Windows checkout. Daily work is bot, API, application layer, dictionary,
  `site/`, and deploy here. Do not develop via `/mnt/c/...`.
- The server test suite is unsupported on a native Windows host (directory
  `fsync` and subprocess handle failures). Local gate:
  `python scripts/validate.py`.
- `client/` remains in the repo and CI still builds it on `windows-latest`.
  Local Qt, Credential Manager, and `build.ps1` are out of scope until the
  owner resumes client work. Text-only client gates still run in the server
  suite.

## Learned User Preferences

- Default stay on Python; forbid a full Go/Rust rewrite. A narrow native
  extract needs profiling proof, failed Python/SQLite optimization against a
  frozen budget, independent plan audit, a pure-Python fallback, and
  differential tests — and must not move Telegram, HTTP, auth, deploy, or
  business contracts.
- Do not optimize without a fresh production baseline and pre-frozen success
  criteria. If WuwaTerm is not the bottleneck, close with no-change instead
  of shipping low-value diffs.
- Keep bot and API as separate processes. Do not add microservices,
  Kubernetes, Redis, Kafka, or PostgreSQL. Do not redo the v0.4.0
  open-source productization campaign. Do not bump game data unless
  production provenance disagrees with the release.
- VPS operations require a current sanctioned deploy-gate grant; do not use
  `GAH_DEPLOY_OFF` or other bypasses. Do not send test messages to Telegram
  chats, groups, or channels; API/Web probes, `getMe`, and natural telemetry
  are allowed.
- Merge only when explicitly authorized; this repo squash-merges with
  `(#NN)`. Do not force-push main or use admin bypass. Codex PR review is
  auto-triggered; do not comment `@codex review`.
- After an authorized merge is on `origin/main`, tidy the workspace in that
  same turn: update local `main` to `origin/main`; delete the merged feature
  branch locally and on origin if it remains; classify leftover dirty files;
  archive reusable run evidence under a named `goal-runs/` path. This tidy
  does not authorize a new PR, deploy, or force-push.
- PR creation, merge, deploy, and deleting any branch other than the
  just-merged feature branch must each be independently authorized in the
  current turn. A fast-forward fix push on an already authorized pull
  request is the exception in Pull request fix push, and only for a
  confirmed in-scope finding.
- CI and tests should catch real defects, not grow for volume. When CI is
  already red, fix immediately. Non-blocking review notes go to the backlog.
- After an authorized review-fix commit is on the PR, mark GitHub review
  threads that this change actually fixed as resolved. Do not resolve
  skipped, frozen, or still-disputed threads. Do not @ bots.
- Do not assume a large Telegram channel fails translation because of rate
  limits; inspect silent skip, capacity, and budget-exhaustion paths.
  Owner-private web UI is intentionally simple — do not over-invest in
  visual redesign.
- Do not commit generated term DBs, upstream TextMaps, secrets, raw tokens,
  real Telegram identifiers, or unsanitized production logs. Swap/zram is
  only an OOM cushion, not a way to hide resident-memory problems.
- As of 2026-09-11, Windows client development is paused; do not open a
  second Windows checkout for daily work.

## Learned Workspace Facts

- 2026-10-04/05 **PR #131 MERGED** (squash `e9fe6d527ca5ef540f348756c114e7868213a9b8`
  on `origin/main`, parent `592a03d`; branch `feat/site-review-deliverables`
  deleted local+remote, restore SHA `11de612`): site zh/en bilingual
  UI with one content-pinned `wuwaterm-lang` cookie, Markdown review-report
  export (`wuwaterm-report.md`, literal user text incl. CR/edge-space
  hardening), real-build-artifact E2E (`npm run test:built` +
  `WUWATERM_BROWSER_BUILT=1 npm run test:browser`, CI site job wired), plus
  review fixes folded from Codex (8 P2s resolved) and two CI flake root
  causes (d1 minute-window asserts; browser warm-up gate). 18 commits,
  final head `11de612` (owner-authorized exact-head merge). Main CI +
  CodeQL green on `e9fe6d5`; exact-head GAH execution audit pass in
  `goal-runs/pr131-merge-ready-closeout-20261004`; merge evidence in
  `goal-runs/pr131-merge-20261005`. DEPLOY STILL NEEDS SEPARATE
  AUTHORIZATION: v15 production
  acceptance evidence (390×844 reachability, zero /api/reviews on
  navigation, byte-identical downloads) is in the run folder. Vendored
  braces fork still temporary (upstream 3.0.3 unpatched as of 2026-10-04).
  Known flake (backlog, outside #131): deploy-lock test
  `test_runtime_only_sigkill_child_keeps_lock_until_explicit_release`
  raced once on a PR push run; rerun green.
- GitHub `My-Denia/wuwa-translate-bot`. Sole local checkout:
  `<LOCAL_CHECKOUT>`. WuwaTerm is a dictionary-first
  official Wuthering Waves term service: one protocol-neutral application
  layer, Telegram and versioned HTTP API adapters, an owner-private
  in-process web UI (off by default), a Windows desktop client that consumes
  the API with no translation logic, and `site/` as a separately hosted
  anonymous public beta. SUPERSEDED: `<FORMER_WINDOWS_CHECKOUT>` as the
  live checkout or product-fact path — owner deleted it 2026-09-11.
- This WSL `AGENTS.md` holds both product facts and Git/VPS adapter rules.
  Product bullets above were migrated from the deleted Windows `AGENTS.md`
  (read 2026-09-11 before the tree was removed). SUPERSEDED: a two-file
  split where Windows held product facts and WSL held only the adapter.
- As of the 2026-08-29 project-rule snapshot, formal production release is
  `v0.4.0` at `ad26b56`. Long-running services are `wuwaterm-bot` and
  `wuwaterm-api`; the builder is on-demand. Production uses a locally built
  runtime image, not GHCR. Game data is Arikatsu 3.6.x, built locally.
  Reverify live release/deploy state before acting. Local `main` after the
  2026-09-11 fast-forward was `b2586e5` (snapshot, not a deploy claim).
- Local contributor check is `python scripts/validate.py` (optional
  `--client`). Server requires Python ≥3.11; the Windows-host server suite
  is unsupported. This checkout `.venv` is POSIX (`bin/`).
- Claude project memory for this cwd is
  `<LOCAL_PROJECT_MEMORY_FILE>`.
  Continual-learning writes this repo `AGENTS.md` only (tracked), not the
  Claude MEMORY tree. SUPERSEDED as current: Windows-encoded
  `<FORMER_WINDOWS_MEMORY_ID>` memory (path absent after the checkout
  deletion).
- Goal-run `goal-runs/vps-mem-opt-20260821` closed
  `no-change-idle-resident`: idle bot+api ≈ 61 MiB docker / 59 MiB PSS.
  CPU-under-load, deploy peaks, and natural Telegram traffic remain
  unproven or insufficient.
- Deploy grants live in `<LOCAL_DEPLOY_GRANT_STORE>`. Cursor
  Shell is gated by `~/.cursor/hooks/`; run VPS work from the parent
  context under a grant (subagent shells have been observed to skip the
  gate).
- Channel LLM admission can silently skip posts on `llm_budget` /
  `queue_full`. Fuzzy lookup scoring the full terms table remains an
  unproven CPU backlog after the idle-resident no-change close.
- As of 2026-09-11, WSL Git signs with the file-based SSH key
  `<LOCAL_SIGNING_PRIVATE_KEY>` (`git commit -S`). SUPERSEDED:
  Windows Git/1Password as the only GitHub-accepted signing path.

## Git Branches

- Use conventional branch prefixes such as `fix/`, `feat/`, `chore/`, or
  `docs/`. Do not create repo branches named after the agent/tool, including
  `codex/*`.
- Name branches after the work, for example `fix/translation-limit-2000`, not
  after the executor.
- Before changing branches or deleting a branch, record the current branch,
  `git status --short --branch`, and the branch tip SHA.
- After work is merged to `main`, and only when the corresponding local and
  remote deletion actions are separately authorized, clean up the source
  branch locally and on GitHub unless the user explicitly asks to keep it.
- Before deleting a branch, verify one of these is true:
  - The branch is an ancestor of `main`:
    `git merge-base --is-ancestor <branch> main`.
  - The branch was squash-merged or otherwise patch-equivalent:
    `git cherry main <branch>` prints only `-` lines.
- Record the restore command before deletion:
  `git branch <branch> <sha>` and, for remote restore,
  `git push origin <sha>:refs/heads/<branch>`.
- Verify cleanup with:
  - `git branch -vv`
  - `git branch -r`
  - `git ls-remote --heads origin`

## Main, PRs, and Merge Semantics

- This section describes repository mechanics after authorization. It does not
  authorize branch creation, commit, push, PR creation, merge, or deletion.

- This is a public repository. Do not push directly to `main` or locally merge
  into `main` for normal work. Put changes on a conventionally named branch,
  open a PR, and let `main` advance through the PR workflow. Only bypass this
  when the user explicitly authorizes a direct `main` push for that exact
  operation.
- Before opening or updating a PR, confirm the branch is based on current
  `origin/main` or explain why it is not.
- Deployable code should be on `origin/main` before the task is considered
  complete. If an emergency deploy uses a non-main commit, reconcile `main`
  before closeout and state the exact exception.
- Do not confuse patch equivalence with commit reachability. A GitHub squash
  merge puts equivalent content on `main` but does not make the feature branch
  commits ancestors of `main`.
- If the user asks whether work is "merged to main", report both facts when
  they differ:
  - content is present or absent on `main`;
  - original branch commit SHA is or is not reachable from `main`.
- Avoid merging an already squash-merged branch back into `main` solely to
  chase SHA reachability unless the user explicitly requires that graph shape.
  Prefer a clean follow-up branch from current `main` for additional fixes.
- Do not rewrite `main` history or force-push just to fix branch names or merge
  messages. Fix branch refs and document the historical message instead.
- Project rules override generic GitHub/publish skills. If a skill suggests
  `codex/*` branches or `[codex]` PR titles, keep this repo's conventional
  work-based names instead.
- Prefer signed commits for PR branches. As of 2026-09-11, WSL Git on this
  machine has its own working SSH signing chain: `gpg.format=ssh`,
  `user.signingkey=<LOCAL_SIGNING_PUBLIC_KEY>`, file-based
  key (no agent). Sign with `git commit -S` and verify with
  `git log --show-signature -1` (expect Good, fingerprint
  `<EXPECTED_SIGNING_FINGERPRINT>`). Do not point the
  signing key at the Windows path via `/mnt/c` — DrvFs looks mode 0777 and
  `ssh-keygen -Y sign` refuses it. SUPERSEDED: commit through Windows
  Git/1Password because WSL could not produce a GitHub-accepted signature.
  The 1Password `op-ssh-sign` path is dead.
- Do not add AI/tool co-author trailers such as `Co-authored-by: Codex`,
  `Co-authored-by: Claude`, or `Co-authored-by: Copilot` unless the user
  explicitly asks for that attribution. Before merging, inspect the final PR
  commit message or merge message for unwanted trailers.
- If GitHub CLI auth is stale, do not treat that alone as a blocker. Use local
  `git` for branch/commit/push and the GitHub connector for PR metadata,
  comments, PR creation, merge, and CI/status reads when available.
- When merging by API/connector, pass the expected head SHA so a moved PR head
  cannot be merged accidentally.

## PR Review and CI Gates

- For public PRs, wait for GitHub CI to reach a terminal state and inspect job
  details, not only the PR summary.
- Codex PR review in this repo is auto-triggered; do not comment
  `@codex review`. When another reviewer bot still needs a trigger (for
  example `@claude review`), post that comment, then read the PR
  conversation and workflow runs for actual feedback.
- Do not claim a reviewer approved unless there is a returned bot comment,
  review, or successful review workflow for the current head SHA. If a trigger
  comment receives no bot response, report that exact absence instead of
  waiting indefinitely.
- Judge severity and scope separately, using Code Review Rules. Fix a
  confirmed in-scope finding on the same branch. Record an out-of-scope
  finding as a follow-up. Do not treat a severity label as a reason to
  widen the pull request.

## VPS Deployments

- A deployment requires separate current authorization for the exact source
  revision and target, plus any required deploy-gate grant. This section does
  not authorize deployment or credential use.

- Before deploying to `<REMOTE_DEPLOY_ROOT>`, verify the deployment source
  commit, current `origin/main`, and whether the source commit is already on
  `main`.
- A VPS deployment is not complete until the repo state is also clean:
  - local worktree clean;
  - intended commit pushed;
  - `main` contains the deployed code or an explicit exception is recorded;
  - obsolete local and remote branches are deleted or intentionally retained
    with a reason.
- Continue preserving remote `.env` and `data/` during deploys. Never print
  secret values.
- Keep the remote backup path and deployed commit SHA in the closeout.
- Use the saved `<SAVED_VPS_PROFILE_SKILL>` workflow/helper for this server.
  If direct WSL `ssh` lacks credentials, switch to the helper path instead of
  asking for credentials or retrying blind.
- Read remote global/project rules before remote writes. If
  `<REMOTE_DEPLOY_ROOT>/AGENTS.md` is absent, record that fact and rely on the
  remote global rules plus this project file.
- Deployment marker writes are part of verification. After writing
  `.deploy_commit`, read it back with `cat .deploy_commit` and compare it to
  the intended SHA exactly; helper quoting mistakes can produce a marker that
  only looks close.
- The deploy reachability smoke is `python scripts/deploy_smoke.py` inside the
  compose service, not a `deploy-smoke` entrypoint command. A missing
  `TELEGRAM_TEST_CHAT_ID` means `sendMessage` is skipped, not that `getMe`
  failed.
- After deploy, verify at minimum: container status, restart count, recent
  sanitized error-log scan, DB hash, `.env` mode, `.deploy_commit`, and absence
  of leftover staging/archive/run containers created by the deployment.

## Privacy and Secret Checks

- Before any commit, push, PR, or VPS deployment, inspect the exact outgoing
  diff for secrets and personal or operational identifiers. At minimum review:
  - `git diff --cached` before commit, or `git diff origin/main...HEAD` before
    push/PR/deploy;
  - `git status --short --branch`;
  - `git ls-files --others --exclude-standard` for accidental untracked files.
- Do not commit or expose real Telegram bot tokens, API keys, `.env` contents,
  owner/user ids, private chat ids, credentials, local credential paths,
  passwords, cookies, or full private endpoint URLs. Use placeholders in docs
  and tests.
- Treat numeric Telegram chat ids, user ids, hostnames, IPs, local Windows/WSL
  user paths, and deployment logs as sensitive unless they are already
  intentionally public in this repository.
- If a secret or private identifier appears in a diff, stop and remove it
  before committing. If it was already pushed, report the exact commit/ref and
  assume rotation or history repair may be required.
- Mention the privacy/secret check in closeout for any public PR, push, or
  deployment task.

## Closeout Checks

- Closeout reports remaining branches and cleanup candidates. It deletes no
  local or remote branch unless that deletion is separately authorized.

- Include branch state in final delivery after any Git/PR/deploy task:
  - current branch and tracking status;
  - `origin/main` commit;
  - remaining local branches;
  - remaining remote heads.
- If source branches remain after merge, explain why. Otherwise delete them
  and verify GitHub no longer lists them.
- If a branch was renamed, verify old remote refs are gone and new refs use
  conventional names.
