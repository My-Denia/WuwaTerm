# Telegram Behavior

## Telegram Bot

Create the bot and token in BotFather yourself, then run:

```bash
export TELEGRAM_BOT_TOKEN="..."
export WUWATERM_DB_PATH="data/terms.db"
.venv/bin/python -m wuwaterm.cli bot
```

Commands:

- `/tr 声骸` returns `Echo`
- `/tr Echo` returns `声骸`
- `/tr 今汐装备了声骸` translates the sentence after locking `Jinhsi` and `Echo`
- `/tr --to en 今汐装备了声骸` and `/tr -to en 今汐装备了声骸`
  force English output
- `/tr --to zh Jinhsi equipped an Echo` and
  `/tr -to zh Jinhsi equipped an Echo` force Chinese output
- `/term 今汐` returns `Jinhsi`
- `/sentence 今汐装备了声骸` locks known DB terms before translation
- `/sentence --to en 今汐装备了声骸` and `/sent --to en 今汐装备了声骸`
  force English sentence translation
- `/sentence --to zh Jinhsi equipped an Echo` and
  `/sent --to zh Jinhsi equipped an Echo` force Chinese sentence translation

The default remains auto-detected when no direction flag is supplied. The
direction flag may be written as `--to en`, `-to en`, `--to zh`, or `-to zh`,
and is recognized only at the START of the command text: once the first
non-flag word is seen, everything after it — including a later `--to en` —
is translated as literal text instead of being treated as a flag (so
"/tr how --to convert files" translates instead of erroring). A leading
invalid `--to` value returns usage and does not call the LLM; exact
dictionary hits do not call the LLM.

Short ASCII queries may also be answered from the dictionary by pinyin:
full pinyin (`shenghai`) and pinyin abbreviations (`sh`) answer directly;
pinyin prefix/substring matches require a 4+ letter query so common short
English words ("he", "an") are machine-translated instead of hijacked by an
unrelated term.

Screenshot-noise stripping only removes MARKER forms: bracketed or hashtag
spoiler/version tags anywhere (`[spoiler]`, `【剧透】`, `#spoiler`,
`(WW 3.4)`), a line-leading `spoiler:` / `剧透：` label, or a whole
decorated line. Bare "spoiler"/"剧透"/"Wuthering Waves 2.1" inside prose is
kept verbatim, and URLs like `example.com/ww2.0` are never touched.

LLM configuration is documented in [Privacy And LLM](privacy-and-llm.md).

Configuration parsing is fail-fast for non-empty invalid values. Booleans accept
only `1/true/yes/on` and `0/false/no/off` (case-insensitive); unset or empty
values keep the documented default. Numeric values must parse cleanly and stay
inside the ranges below, and floating-point values must be finite (`nan` and
`inf` are rejected). A configuration error names the variable and expected
range/type but never echoes the raw value.

Optional Telegram/runtime environment variables:

- `WUWATERM_RATE_LIMIT_PER_MINUTE`, default `10`; range `1..10000`
- `WUWATERM_GROUP_TR_REJECT_TEXT`, default is the bilingual two-line reply
  `仅群管理员可用 /tr` then `Only group admins can use /tr`
- `WUWATERM_PRIVATE_TR_REJECT_TEXT`, default is the bilingual two-line reply
  `此 bot 仅限群内由管理员使用` then `This bot can only be used by admins inside a group.`
- `WUWATERM_TR_REJECT_SILENT`, default `0`; any true token
  (`1/true/yes/on`) drops unauthorized `/tr` calls without replying; false
  tokens are `0/false/no/off`
- `OWNER_USER_ID`, no default; the only Telegram user id allowed to use
  `/tr` in private chat — missing or empty means private `/tr` rejects
  everyone (fail-closed) and a startup warning is logged
- `WUWATERM_CHANNEL_AUTOTRANSLATE`, default on; true tokens are
  `1/true/yes/on`; `0/false/no/off` disables the linked-channel
  auto-translation listener (kill switch)
- `WUWATERM_CHANNEL_MIN_CJK`, default `1`, range `1..4096`; minimum number
  of CJK ideographs a channel post needs to be auto-translated Chinese ->
  English
- `WUWATERM_CHANNEL_MIN_LATIN`, default `2`, range `1..4096`; for a channel
  post with no CJK, the minimum number of Latin letters it needs to be
  auto-translated English -> Chinese (below both thresholds the post is skipped)
- `WUWATERM_CHANNEL_TEXT_LIMIT`, default `4096`, range `1..4096`; max
  text-post length for auto-translation
- `WUWATERM_CHANNEL_CAPTION_LIMIT`, default `1024`, range `1..1024`; max
  caption length for auto-translation
- `WUWATERM_CHANNEL_MAX_AGE_SECONDS`, default `86400`, range `1..2592000`;
  channel posts older than this are never auto-translated. The default is broad
  because linked-channel content is trusted, while still bounding Telegram
  update replays (restart backlog, bot admin promotion) from translating
  old history.
- `WUWATERM_CHANNEL_REPLY_INDEX_PATH`, optional; defaults to
  `WUWATERM_STATE_DIR/channel_replies.json` when `WUWATERM_STATE_DIR` is set,
  otherwise `<db parent>/channel_replies.json` for local compatibility. This
  file contains Telegram
  chat/message ids and must stay in the ignored runtime data volume, not in
  commits or public logs.
- `WUWATERM_SOURCE_PROFILE`, default `arikatsu`; supported profiles are listed
  by `refresh-data --help` and `build-db --help`

No Telegram token, LLM key, endpoint, or model is hardcoded.

### Group Chats

In groups, slash commands work with Telegram privacy mode left on. The bot
does not listen to free-text messages from members; the only passive listener
is the linked-channel auto-translation described below — note that receiving
those channel auto-forwards at all requires the bot to be a discussion-group
admin (see that section).

- All translate commands — `/tr`, `/term`, `/sentence`, `/sent` — share one
  authorization gate. In a group the chat must first be on the authorization
  allowlist (see below); commands in a non-authorized group are rejected
  outright, even for admins and even in public mode. Within an authorized group
  they are admin-only by default: each call resolves the sender via
  `getChatMember`, and only `creator`/`administrator` may use them. Anonymous
  group admins (posting as the group itself) are allowed. Membership verdicts
  are cached about 5 minutes per (chat, user). An admin may open an authorized
  chat to all members with `/public on` (see below).
- Authorization has explicit tiers. Owner and group admins are trusted
  callers in allowlisted groups; ordinary members are authorized only when
  `/public on` is enabled. Unauthorized callers get a short bilingual reply
  (Chinese line then English), default `仅群管理员可用 /tr` then
  `Only group admins can use /tr`; the wording is configurable, and a config
  flag switches to silent ignore (default replies). Rejected calls never invoke
  the LLM and use a separate rejection-reply budget.
- Authorized `/tr 声骸` and `/tr@<botusername> 声骸` return dictionary hits;
  `/tr <sentence>` translates with DB terms locked. Direction is auto-detected:
  Chinese input -> English, English input -> Chinese (`/tr Echo` returns `声骸`).
  A resonator name typed all-lowercase or all-caps inside a sentence (`jinhsi`, `JINHSI`) is locked
  like its official casing; mixed casing and other term categories still need the exact official form.
  An English dictionary surface that official text almost always uses as an ordinary word (`It`, `Do`,
  `Fish`) is not locked inside a sentence once a dictionary rebuilt with its free-text exclusion list is
  promoted; `/tr It` alone still returns the dictionary hit.
- Explicit direction flags override that default for commands only:
  `/tr --to en ...`, `/tr -to en ...`, `/tr --to zh ...`, `/tr -to zh ...`,
  `/sentence --to en ...`, `/sentence --to zh ...`, `/sent --to en ...`, and
  `/sent --to zh ...` use the requested target direction.
- When you reply to a message with `/tr --to en`, `/tr -to en`,
  `/sentence --to zh`, or `/sent --to zh`, the bot uses the replied-to text
  with the requested direction.
  Replying to a formatted text or caption preserves Telegram HTML formatting
  through the LLM path. Tags, attributes, links, custom emoji ids, and entities
  are replaced with opaque structural placeholders before the model sees only
  visible text. Missing, duplicated, reordered, or newly injected structure
  discards the HTML attempt and retries once as plain text, so the caller
  still gets an unformatted translation instead of an error notice
  (transport/quota errors keep the notice). Dictionary exact and fuzzy hits
  remain official plain text.
- Formatting typed directly in the command (`/tr <b>bold</b> sentence`, via
  Telegram's formatting UI) is preserved the same way: the command prefix and
  leading direction flags are cut off and the remaining tail's entities go
  through the same HTML pipeline. Fail-safe: multiline inline text, repeated
  spaces, mid-text direction flags, or an entity straddling the command prefix
  fall back to today's plain-text behavior rather than guessing.
- Group replies quote the asking message.
- Private chat: all translate commands answer only the configured owner
  user id; everyone else gets a short bilingual reply, default
  `此 bot 仅限群内由管理员使用` then `This bot can only be used by admins inside a group.`
  With the owner id unset, private chat rejects everyone (fail-closed).
  Channel-type chats are rejected entirely.
- Ordinary public members are rate-limited per chat, default 10 lookups per
  minute, and keep the 2000-character LLM input limit. Trusted callers (owner
  and group admins) do not use the ordinary public-member throttle and may use
  the channel text/caption limits (4096 text / 1024 caption); long trusted
  inputs are split internally into independent 2000-character LLM chunks and
  joined with newlines, so long-input translation favors delivery over perfect
  cross-chunk context or HTML preservation. Replies longer than Telegram's
  4096-character text message limit are split before sending.
- `/status` is owner-only and reports operational counts and flags only:
  dictionary term count, data profile/short commit, LLM configured yes/no,
  channel auto-translation on/off, tracked channel-post count, allowlist/public
  counts, channel reply persistence health, channel active/pending/high-water
  counts, aggregate outcome counters, admission caps, and message limits. It
  does not print secrets, storage paths, chat ids, message bodies, or URLs.
  Channel reply load/save failures are cumulative since process start; the
  last-load and last-save fields show whether the most recent persistence read
  or write succeeded.

### Group Authorization / auto-leave (`/grant`, `/revoke`)

The bot only stays in groups the owner has authorized. When it is added to a
chat that is not on the allowlist, it posts a short bilingual notice and then
leaves automatically — this keeps a public bot from being pulled into arbitrary
groups and abused. The allowlist is also the serving gate: translate commands
only run in a group that is on it, so even if the auto-leave or the
persisted-write fails, an unauthorized group still gets no translations
(fail-closed).

- The owner adding the bot to a group auto-authorizes that group (its id goes on
  the allowlist), so the owner can drop the bot into their own groups with no
  extra step.
- `/grant` (owner only) — in a group, authorizes the current chat; in
  private chat, `/grant <chat_id>` authorizes by id and `/grant list`
  shows the allowlist.
- `/revoke` (owner only) — removes a chat from the allowlist (current chat in a
  group, or `/revoke <chat_id>` in private).
- Only the genuine "added" event triggers the leave. A promotion, demotion, or
  any status change inside a group the bot already belongs to never makes it
  leave, so an existing authorized group is safe.
- Requires `OWNER_USER_ID`. With it unset (fail-closed) the bot is not
  authorized to stay in any newly-added group.
- The allowlist is persisted in the same file as the `/public` state
  (`WUWATERM_SETTINGS_PATH`).

> First deploy into an existing group: the bot is already a member there, so it
> does not auto-leave (no "added" event fires) — but because serving is gated on
> the allowlist, `/tr` and the other commands will NOT respond there until the
> group is authorized. Run `/grant` once inside that group (or
> `/grant <chat_id>` from a private chat with the owner) to add it to the
> allowlist; translations resume immediately.

### Opening a Group to Non-Admins (`/public`)

Admins can open translate commands to everyone in a specific group with
`/public on`, and restrict them back to admins with `/public off`. The default
for every new chat is admin-only (no behavior change for groups that don't
opt in).

- `/public on` — open `/tr`, `/term`, `/sentence`, `/sent` to all members.
- `/public off` — restrict back to admins (default).
- `/public` or `/public status` — report the current state.
- `/public` is ALWAYS admin-only — public mode never unlocks the toggle
  itself, so a non-admin can never flip a public chat back off or on.
- Public mode only applies inside an authorized group; it never bypasses the
  authorization allowlist (an un-authorized group serves no one, public or not).
- Per-chat throttling and the 2000-char LLM cap still apply.
- State is persisted to `WUWATERM_SETTINGS_PATH`. When unset, the default is
  `WUWATERM_STATE_DIR/chat_settings.json` if `WUWATERM_STATE_DIR` is set, or
  `<db parent>/chat_settings.json` for local compatibility. On the supported
  Docker layout this lives in the writable `state/` volume and survives image
  rebuilds while `data/terms.db` stays read-only.

### Linked-Channel Auto-Translation

When the channel linked to a group posts, Telegram auto-forwards the post
into the group. The bot auto-detects the post's language and replies in-thread
with a translation that preserves the original Telegram formatting (bold,
links, spoilers, ...): a Chinese post is translated to English, an English
post to Chinese. No command is involved.

- Trigger (hard boundary): only automatic forwards whose sender is a
  channel (`is_automatic_forward` + channel `sender_chat`). Ordinary member
  messages, manual forwards of channel posts, and anonymous-admin posts
  never trigger it.
- Authorization: the discussion group must be on the same authorization
  allowlist as the slash commands. An unauthorized or revoked group — including
  one the bot has not yet managed to leave (e.g. a `leave_chat` that failed) —
  gets no auto-translations and burns no LLM budget. (Posting rights in the
  linked channel are owner-controlled, but the allowlist is what bounds where
  the bot will spend tokens.)
- Direction by script: a post with enough Chinese (`WUWATERM_CHANNEL_MIN_CJK`,
  default 1) is translated to English; a post with no Chinese but enough Latin
  letters (`WUWATERM_CHANNEL_MIN_LATIN`, default 2) is translated to Chinese; a
  post with neither (emoji / links / numbers only) is skipped silently with
  zero LLM calls and zero throttle consumption.
- The reply uses Telegram HTML (`parse_mode=HTML`) rendered from the post's
  entities. Every tag, attribute, link, custom emoji id, and entity is replaced
  by an opaque placeholder before the LLM sees only visible text; DB terms in
  visible text are locked separately. Placeholder count/order changes, unknown
  tags, raw structural injection, and blank output discard the HTML attempt
  and retry once as plain text (one extra LLM call reserved up front when
  `WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE` is at least 2; a budget of 1 skips
  the retry rather than blocking all short posts), so the post still gets an
  unformatted in-thread translation; transport/quota failures — including a
  malformed gateway/API response envelope — stay silent with no retry. If Telegram
  rejects byte-preserved, locally validated HTML at delivery time, the bot may
  retry the same translation as plain text. If the translated output exceeds Telegram's
  4096-character text message limit, the bot strips HTML formatting and sends
  the full translation as tracked plain-text chunks.
- Delivery reliability: a Telegram 429 flood-wait (`RetryAfter`) on any single
  send or edit is waited out once (capped at 60s) and retried — a flood-wait
  guarantees the call did not execute, so the retry cannot duplicate; retries
  are per API call, never per multi-chunk delivery. Before the retry actually
  sends, the volatile delivery gates (freshness, authorization) are re-checked:
  a post revoked or aged out during the wait is dropped, not delivered late.
  Timeout/network errors are
  deliberately not retried (the send may have succeeded server-side).
  Admin checks retry once on a transient network error before failing closed.
  A post whose text exists but whose rendered HTML is unavailable degrades to
  the plain pipeline instead of being dropped.
- Dictionary-first still applies: a post that is exactly one official term
  gets the official string byte-for-byte (English for a Chinese term, Chinese
  for an English term), plain, without the LLM.
- Channel auto-translation remains auto-detected for linked-channel posts;
  command direction flags such as `--to en` and `--to zh` do not apply.
- Caption posts (photo/video announcements) are handled the same as text
  posts. Length caps are Telegram's own limits (4096 text / 1024 caption)
  instead of the 2000-char command cap.
- Channel auto-translation is a trusted publisher path. It does not share the
  ordinary public-member command throttle; it is bounded by allowlist,
  text/caption limits, direction checks, the LLM configuration/budget, and the
  freshness gate below. `WUWATERM_CHANNEL_MAX_PENDING` (default 16) bounds the
  waiting queue on top of `WUWATERM_LLM_MAX_CONCURRENCY`; queue-full posts are
  skipped before an LLM call. `WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE` (default
  60) is a rolling process-local call budget. A multi-chunk post atomically
  reserves every required call before the first call, so it is either admitted
  in full or skipped. Authorization, freshness, and edit currency are checked
  again after a queue wait. Budget exhaustion skips silently with one
  privacy-safe log line — no notice comment under the post.
- Kill switch: `WUWATERM_CHANNEL_AUTOTRANSLATE`, default on.
- Freshness gate: posts older than `WUWATERM_CHANNEL_MAX_AGE_SECONDS`
  (default 86400) are skipped silently. Linked-channel content is trusted, so
  this default allows delayed posts and late edits while still bounding
  Telegram replays — restart backlog, or a burst of recent group history when
  the bot is promoted to admin — from translating old history. Operationally,
  restarting after downtime or promoting the bot to admin can translate
  queued/replayed trusted channel posts that are still inside this 24-hour
  window; lower `WUWATERM_CHANNEL_MAX_AGE_SECONDS` if that burst is not wanted.
- Edited posts update the existing tracked reply chunks in place: when the
  linked channel edits a post, the bot re-translates and edits existing chunks,
  adds continuation chunks, or deletes stale extras instead of adding untracked
  duplicates. The post-to-reply map is persisted to
  `WUWATERM_STATE_DIR/channel_replies.json` by default in Docker; for local
  compatibility without `WUWATERM_STATE_DIR`, it falls back to
  `<db parent>/channel_replies.json`. Recent posts can still be reconciled
  after a container rebuild or restart while they remain inside
  `WUWATERM_CHANNEL_MAX_AGE_SECONDS`. If no tracked reply exists — for a post
  that was never translated, a corrupt/missing persistence file, or an edit made
  after the freshness window — the edit is skipped silently; an edit never
  produces a duplicate reply.
- Delivery precondition: Telegram privacy mode withholds channel
  auto-forwards from non-admin bots (slash commands still arrive). Make
  the bot an admin of the discussion group (any single right suffices);
  the alternative — disabling privacy mode via BotFather — also requires
  removing and re-adding the bot to the group.
