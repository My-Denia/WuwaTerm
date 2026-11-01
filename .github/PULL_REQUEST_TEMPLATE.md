<!--
Thanks for sending a change. Small and focused merges fastest.
Read CONTRIBUTING.md, including the project non-goals.
Never put tokens, API keys, device credentials, real Telegram ids or .env
contents in this description or in the diff.
-->

## What

<!-- What this change does, in a sentence or two. -->

## Why

<!--
The problem it solves. Link the issue if there is one (Fixes #123).
If this changes behaviour somebody could be relying on, say so here.
-->

## How It Was Validated

<!-- Describe the checks relevant to this change: existing or new tests, builds,
manual verification, or another suitable check. Include the command, environment,
and result where useful. For server changes, run `python scripts/validate.py`
on Linux. Explain any check that could not run. -->


## Checklist

- [ ] One concern. This change does one thing; anything else is a separate pull request.
- [ ] Validation matches the change and is recorded above.
- [ ] If this changes user-visible behaviour, `CHANGELOG.md` describes it under `## Unreleased`.
- [ ] If either README was touched, `README.md` and `README.zh-CN.md` were both updated and still carry the same sections, facts and images.
- [ ] Documentation was updated wherever this change made an existing statement untrue.
- [ ] If behaviour changed, existing or new tests cover it, or the validation above explains the alternative.
- [ ] No secrets and no generated data: no `.env`, no tokens or API keys, no generated SQLite database, no upstream TextMap or game data, no runtime state (`state/`, `state-api/`, `chat_settings.json`, `channel_replies.json`).
- [ ] This change does not cross a non-goal (see `CONTRIBUTING.md`).

## Notes For The Reviewer

<!--
Anything worth knowing: a decision you were unsure about, a part that deserves
a closer look, a follow-up you deliberately left out of scope.
Automated reviewers run on this pull request. If one of them is wrong, say why
in the thread rather than changing the code to satisfy it.
-->
