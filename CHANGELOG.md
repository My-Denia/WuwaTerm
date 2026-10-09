# Changelog

All notable source changes for this project are tracked here. This repository
does not distribute generated game data or generated SQLite databases.

## Unreleased

- Review: `review-v2` no longer treats a mention of a surface the official text uses as an ordinary word as an asserted terminology constraint. The two free-text lock exclusion metadata keys (`free_text_lock_exclusions`, `free_text_lock_exclusions_zh`, built by #148/#149 from official-corpus alignment) now also inform review: an unresolved mention of such a surface — `It`, `Fish`, `Tea`, `Discord`, `大人`, `索拉` and 241 more on the 3.7 data — keeps its finding and every candidate but is returned `not_evaluated` with no target span, so `It is raining today.` no longer demands a check against 祂. Finding a sentence that happens to contain the official form no longer auto-verifies such a mention; an explicit `official_pair` choice still runs the authoritative check (verified with the exact span, `confirmed_conflict`, or `not_evaluated` per the existing rules), and `not_a_term` still records user intent. Mentions, candidate identities, dictionary/matcher revisions, saved choices and manuscripts are unchanged: a historical downgrade is classified incomparable, never resolved, and a report whose findings are all such mentions honestly reports zero evaluated coverage — zero findings never means the translation passed. A database without these keys keeps the previous behavior; an invalid key is ignored with one warning naming it while the other key still applies. `review-v1` is unchanged. The strict parsers for both keys moved to the shared `wuwaterm.exclusion_metadata` module, re-exported unchanged from `wuwaterm.sentence`. On the frozen 3.7 measurement (production-identical dictionary rows, 946-row official-corpus sample both directions, handcrafted controls): automatic `needs_review` findings fall 322→80 with all 242 eliminated findings on excluded surfaces, every nonexcluded control stays discovered and verified, no unconfirmed occurrence becomes verified, and 27 previously auto-verified excluded-surface findings (genuine item/weapon discussion such as Discord→异响空灵) now need one explicit choice to verify — kept, selectable and reversible.
- Terms: sentence translation no longer forces an official English name onto a Chinese dictionary surface that official text overwhelmingly uses as an ordinary word or as part of other names, such as the speaker labels `大人`, `索拉`, `少女` and `通讯` (`他已经是大人了` no longer becomes `他已经是Adult了`; `索拉里斯山脉的风景` no longer becomes `Serra里斯山脉的风景`). A full `build-db` measures every Chinese surface over the official bilingual text — a line counts as aligned when the parallel English, casefolded and with surrounding quote characters stripped, carries the surface's official English — and records in the new `free_text_lock_exclusions_zh` metadata key (121 surfaces on the 3.7 data: 92 speakers, 18 skills, 9 items, 1 weapon, 1 resonator) the surfaces locked at least 10 times whose aligned share is below one in five; free-text translation applies the union of the English and Chinese exclusion lists, each failing open per key. The official-corpus trade-off: 9,433 mislocked official-line locks are removed for 477 aligned locks lost, 0.42% of all aligned locks, dominated by generic dialogue labels; the felt losses are `岁光` (Jué) and the weapon `凌空`, and a stricter one-in-ten cut was measured and declined (it would keep `岁光` and `月相` locking while leaving about 1,042 mislocks in place). Whole-input lookup (`/tr 大人` still returns `Adult`), speaker-label resolution, the English exclusion list and review are unchanged. `verify_db.py` checks the new key and `--require-free-text-zh-exclusions` (always passed by the builder image's `verify-db`) rejects a candidate without it. Activation matches the English list: the next owner-authorized full deploy, which rebuilds the dictionary, turns this on; a database without the key keeps every surface lockable, and a runtime-only update changes nothing.
- Terms: sentence translation no longer forces an official Chinese name onto an English dictionary surface that official text uses as an ordinary word, such as the speaker labels `It`, `Do` and `Will` or the items `Fish` and `Tea` (`It is raining today.` no longer becomes `祂 is raining today.`). A full `build-db` measures every dictionary surface over the official bilingual text and records in the database the surfaces locked at least 10 times whose official Chinese line carries the official name in fewer than one in five of those lines; free-text translation on every entry point then leaves them to the model. The trade-off is deliberate: a real use of an excluded name in a sentence, including six weapon names, is also no longer protected. Whole-input lookup (`/tr It`), speaker-label resolution, longer terms containing an excluded surface and review are unchanged, and an excluded resonator name gains no lowercase or all-caps variant; Chinese surfaces were untouched by that English-side entry (the separate `free_text_lock_exclusions_zh` entry above changes them with its own key). `verify_db.py` checks the recorded list, and `--require-free-text-exclusions` (always passed by the builder image's `verify-db`) rejects a candidate without it. Activation: the next owner-authorized full deploy, which rebuilds the dictionary, turns this on; a runtime-only update keeps the current database and changes nothing, because a database without the list keeps every surface lockable.
- Terms: sentence translation now locks a resonator-category English name typed all-lowercase or all-caps, so `jinhsi joins` and `JINHSI joins` keep the official name just like `Jinhsi joins`. Only exact Unicode lowercase or uppercase forms count, glued to no Latin letter or digit; mixed casing such as `jinHsi`, plurals, other categories (speakers, items, skills, core terms), and a name whose other casing is a separate dictionary surface for a different record keep exact-case matching. A variant restores the same record as the official casing, and an exact dictionary entry always wins. The resonator category also holds some non-playable roleinfo names. Lookup, review and properly cased text are unchanged.
- Site: undoing a manuscript import restores the prior draft, choices, alignments and both historical reports, including a check that finished while the file was being read. Restored reports still require an explicit fresh check.

- Site: the pinned `next` dependency moves from 16.3.6 to 16.3.8, clearing the newly published draft-mode content-leak and SSG/ISR cache-poisoning advisories (`GHSA-3w37-wq28-93x7`, `GHSA-4jqv-mc3x-m676`) that turned the site feasibility security check red. `npm audit` now reports no known vulnerabilities; the vendored `braces` entry is only re-sorted alphabetically. No application code changes.
- Review: a Chinese substring assembled across two ordinary words no longer verifies a `review-v2` term constraint. `声骸` inside `回声骸骨` and `安可` inside `平安可贵` are not counted as the official form, cannot satisfy a repeated mention, and can no longer make a finding `not_evaluated` from outside the aligned sentence; a user-chosen official pair with no remaining valid hit falls to the existing `confirmed_conflict` outcome instead of being verified. Continuous hits such as `使用声骸`, `一声骸` and `强大声骸` stay verified, and `review-v1` judgments are unchanged. The canonical matcher label becomes `wuwaterm-cjk-matcher-v2` on the same lexicon slice, so stored opt-in resolution contexts carrying the old matcher hash become stale by the existing freshness rule; legacy three-key contexts and `rule_version` are unchanged.
- Site: review findings without a reliable translation span now offer a user-confirmed single-occurrence revision. Enter an exact fragment, select one occurrence by position and context, then preview and replace it with the chosen official form. The edit preserves existing undo, manuscript and stale-report semantics; adopting a pair and requesting a fresh check remain separate actions.
- Review: `review-v2` remains the request protocol. Default responses omit `matcher_revision` and legacy three-key resolution contexts remain fresh when source, rule, and dictionary revisions match. The new Site opts in with `X-WuwaTerm-Matcher-Basis: 1`: reports then carry a separate matcher identity and nonempty resolutions require it. Deploy API before Site; roll back Site before API. Old manuscripts still import; a saved choice without the new matcher basis requires confirmation even when its span remains. `review-v1` is unchanged.
- Terms: a multi-character Chinese term is no longer locked, and review-v2 no longer reports it, when ordinary words cross both of its edges. `安可` inside `随遇而安可以`, `祝你平安可以吗` or `平安可贵` is not a resonator mention, and `声骸` inside `回声骸骨` is not an Echo mention. Real terms whose neighbors only touch the boundary stay, including `今汐装备了声骸` and `给安可装备声骸`. A one-sided overlap also stays, including `声骸` in `香椿和声骸`. One-character resonator filtering and review-v1 spans are unchanged. A saved manuscript is not rewritten; submitting a resolution for a span this rule dropped is still rejected.
- Site: update the pinned sharp native dependency to 0.35.5 for the librsvg security fix, and correct its optional native packages' classification so production installs can load the patched library.
- Site: the review workbench shows whether the current manuscript still matches the file imported this visit or the last manuscript download handed to the browser. That download is not treated as a confirmed disk write. Translation TXT, result JSON and Markdown exports do not update it. Opening an in-site note, refreshing, or otherwise leaving asks before dropping work that the imported file cannot restore; staying keeps the manuscript on the page. A crash or a forced quit can still discard the page.
- Repository: review rules now separate severity from pull-request scope. A confirmed in-scope finding may be fast-forward pushed on that same branch. After an authorized merge, the same turn updates local main and deletes that merged feature branch locally. GitHub already deletes its remote head; the tidy deletes that remote head only when it is still there. That cleanup needs no second authorization. Merge, deploy, production changes, and deletion of any other branch still need their own authorization.
- Review: `review-v2` now ignores the same one-character dictionary collisions that sentence locking already refuses. Ordinary one-character items, echoes, and speakers such as `我`, `角`, `盐`, and `米` are not findings, and a one-character resonator is a finding only in a name-like context (`椿加入队伍`, not `香椿` or `心里`). That one-character filter did not change longer official terms. `review-v1` still reports the old spans.
- Site: lookup results, review candidates, and the Markdown review report show dictionary categories and lookup match types in the active interface language. Unknown values stay as returned. The API and the JSON exports still carry the original keys.
- Site: the home quota strip and the review entrance show shared review remaining before submit. When that remaining count is zero, the two buttons that spend a review admission are disabled.

## 0.6.0 - 2026-10-05

### Highlights

- Site: Chinese and English interface support, plus a full Markdown review-report export.
- Site: filter and navigate findings, then jump to exact source or target spans.
- Data: the Arikatsu 3.7.0 candidate contains 11,329 extracted records.

### Added

- Site: the whole interface renders in Chinese and English. A topbar toggle
  switches in place without reloading, keeps the manuscript, choices, active
  finding, expansion state and report currency, and sends no request. The
  preference persists in one `wuwaterm-lang` cookie read server-side, so the
  first paint matches the choice with no hydration mismatch; first visits
  default to Chinese and missing translations fall back to Chinese copy.
  Interface language never changes the manuscript, translation direction,
  terms or evidence. The storage scanner allows exactly that one
  content-pinned cookie write and rejects everything else.
- Site: the review workbench exports a readable Markdown snapshot of the
  full report (`wuwaterm-report.md`) beside the manuscript JSON, result JSON
  and translation TXT. It is generated locally without requests, is never
  clipped by the findings filter, and states the report's own rule version,
  dictionary revision, source commit, revisions and request id — never the
  app version. Current, stale-edited and imported-untrusted reports carry
  distinct wording; zero findings and zero coverage keep their true meaning
  and nothing certifies sentence meaning. User text is rendered as literals
  inside code spans and fences, so report structure, links and HTML cannot
  be forged.
- Site: `npm run test:built` runs the landing, review-v2 journey,
  shared-quota accounting, parser-rejection, 404, redaction and fail-closed
  checks against the real production build artifact (`dist/`) through the
  wrangler test harness with a synthetic upstream and local D1 — no real
  model calls and no beta quota. `WUWATERM_BROWSER_BUILT=1 npm run
  test:browser` serves the built bundle to the Playwright suite instead of
  the dev server, and CI runs both.
- Site: a review report with several findings can be filtered by verdict,
  stepped through with previous/next, and jumped to an exact source or target
  span. Candidate source file and id expand locally. Filtering, navigation,
  jumps, and that expansion do not send a review request or change the
  report, choices, manuscript, or export.
- Site: the public page can show the dictionary version reported by the
  service that is actually running. The panel stays unread until the visitor
  opens it, so a page view does not spend a meta admission. The site accepts
  the current `/v1/meta` body and a later body that adds game version,
  resource version, and changelist. Absent version fields and a failed read
  stay unknown; the page does not fill them from the repository pin.
- API: `GET /v1/meta` adds `source_game_version`, `source_resource_version`,
  and `source_changelist` from the open database. A database that never
  recorded those keys returns null. The route does not copy the active source
  profile.

### Fixed

- Site: imported CRLF manuscripts now locate the exact source and translation
  terms and record manually selected alignment ranges in the report's original
  coordinates. Saved manuscript and translation bytes retain their line endings.
  The English header also wraps within narrow content widths, including a
  390-pixel viewport with a classic scrollbar.
- Site: a finding displayed after a verdict filter hides the previous active
  finding now becomes the navigation selection. Returning to a broader filter
  preserves it; an empty filter retains the last selection without displaying
  a finding. Navigation remains local and does not change report exports.
- Site: replace the vulnerable `braces` resolution with a transparent,
  repository-owned 3.0.3 security patch that bounds nested pattern and direct
  AST traversal. Both build/lint dependency paths use it; the audit policy is
  unchanged. Keep the temporary fork until a verified upstream fix can replace it.
- Site: pin Next.js to 16.3.6, the patched release for
  GHSA-vcvr-r3jv-pc5j in Node.js `next/og` image generation. Refresh only
  its locked runtime packages and platform compilers; the site continues to
  use its static Open Graph image.
- Site: the locked `brace-expansion` copies move to 1.1.21 and 5.0.12, the
  patched releases for GHSA-6j4f-fj2g-mc7p, GHSA-qhr7-859c-m2p7, and
  GHSA-q2hr-2g5m-vwhr. The two copies stay on their existing major lines, and
  no direct site dependency changes.
- Site: sending a translation to review now preserves an identical manuscript,
  asks before replacing existing work, and keeps a recoverable undo snapshot.
  Cancelling a handoff leaves any running check intact; replacing a manuscript
  rejects late results from its previous check.

### Game Data

- data: pin Arikatsu 3.7.0 — the active source profile moves to Wuthering
  Waves 3.7.0 / resource 3.7.8 / changelist 8975829 at exact upstream commit
  `9218d612ad815e398e064e577e42aaf878899968` (previously 3.6.0 / 3.6.4 /
  8464573 at `6ce8d5eda49f2930da84d8846c144432142c7465`). The upstream README
  at that commit labels the checkout Global / Release; that label is the data
  repository's own status line. The candidate built from that checkout carries
  11329 extracted records, up from 10951, with 378 added terms, 0 removed and
  1 changed zh/en pair. The removed set is empty because the diff matches
  source keys: nothing present at 3.6 was dropped. The one change is an
  English spelling correction on the existing speaker row
  `Speaker_800543_Name`: `Panicked Lolo Logistics Staff` becomes
  `Panicked Lollo Logistics Staff`. Category deltas are echo +26, item +60,
  location +17, resonator +5, skill +20, sonata +3, speaker +245 and weapon +2.
  New resonator source rows are not all new names: `心 / Hsin` and
  `锁暝 / Suoming` already occurred in 3.6, `维里奈 / Verina` gains another
  source key, and the new strings are `棠宁 / Tangning` and
  `心狐·漂泊者 / Moon Fox: Rover`. Offline gates (`verify_db.py`,
  `verify_seed_terms.py`, `verify_exact_hits.py`, `verify_idempotent_build.py`)
  pass on this candidate.
- The required representative exact pair stays `景燃 -> Jingran`. It was
  re-measured on the 3.7 candidate and is still single-valued in both
  directions. `穗穗 -> Suisui` stays retired for the same reason as in 3.6.
  `棠宁 -> Tangning` is a new pair that is also single-valued both ways; it
  is the measured new-term sample, not a second required check.
- A 3.6 review manuscript does not keep its certification under this
  dictionary. The content-derived dictionary revisions differ, a saved 3.6
  resolution context is rejected as stale by `review-v2`, and the site
  manuscript reconciler marks the imported choice pending until the user
  confirms it against the fresh basis. A fresh check on the 3.7 candidate
  can accept the same sentence once its context carries the new revision.
- Generated databases and raw game data are excluded from release artifacts.
  Operators build `terms.db` locally from the pinned source.

## 0.5.0 - 2026-09-13

The public product and `main` catch up to a tagged release. WuwaTerm is an
unofficial, independent fan project: look up official Chinese and English
Wuthering Waves terms, translate a sentence with those names locked, or review
a translation you already have.

Try it: <https://wuwaterm.denia-official.chatgpt.site> (anonymous public beta,
one shared first-come pool, no SLA). Windows client:
`WuwaTerm-0.2.0-windows-x64.zip` on this release (unsigned). Self-host and
docs: [docs/self-hosting.md](docs/self-hosting.md) and
[docs/README.md](docs/README.md).

### Highlights

- **Public beta.** No-account lookup, term-locked translation, and review on
  the shared site above (#96, #98, #104, #105, #109).
- **Review workbench.** Paste a source string and an existing translation;
  dictionary-first findings, no model call (`POST /v1/reviews`; default
  `review-v1`, opt-in `review-v2`) (#110).
- **Resumable manuscripts.** Save or import a local bilingual draft and recheck
  current dictionary evidence before reusing saved choices (#111).
- **Bidirectional term locks.** English-to-Chinese placeholder instructions
  keep locked tokens for official-term restoration (#108).
- **API and deploy.** `/v1/terms` returns the backend-ranked exact-to-fuzzy
  list (the unpublished 0.4.1 package line). Transactional runtime-only deploys
  can update bot and API without rebuilding the terminology database (#97,
  #107).
- **Repository front door.** Bilingual landing READMEs and a docs index (#112).

### Also in this release

- Owner chat allowlisting is `/grant`; `/authorize` is gone (#95).
- The desktop client's CJK-literal gate covers the whole `ui/` package (#93,
  #94).
- GHCR `vX.Y.Z` and `X.Y` image tags are applied only after this GitHub
  Release is published; a discarded draft leaves only `sha-<7>` registry tags
  (#88).

## 0.4.0 - 2026-08-19

Presentation, distribution and data release. A third presentation layer — an
owner-private web interface running inside the API process and off by default —
joins the Telegram bot and the HTTP API; the game-data pin moves to Wuthering
Waves 3.6.0 / resource 3.6.4 / changelist 8464573 at upstream commit
`6ce8d5eda49f2930da84d8846c144432142c7465`; the desktop client becomes 0.2.0 and
is published as a release asset for the first time; and every release asset is
now built by a workflow from one reviewed commit rather than by hand. The
project also gains its governance entries, a generic self-hosting guide separate
from the owner's own runbook, and one command that runs every offline gate.

### Game Data

- data: pin Arikatsu 3.6.0 — the active source profile moves to Wuthering
  Waves 3.6.0 / resource 3.6.4 / changelist 8464573 at exact upstream commit
  `6ce8d5eda49f2930da84d8846c144432142c7465` (previously 3.5.0 / 3.5.5 /
  8059200 at `dae29691c04ef0f48d0810b5d244fb0b37288c60`). The candidate built
  from that checkout carries 10951 extracted records, up from 10691, with 260
  added terms, 0 removed and 6 changed zh/en pairs; the offline gates
  (`verify_db.py`, `verify_seed_terms.py`, `verify_exact_hits.py`,
  `verify_idempotent_build.py`) all pass on it and `diff_terms_db.py` reports
  no removed term.
- The required representative exact pair is now `景燃 -> Jingran`, a resonator
  that is new at 3.6 and single-valued in both directions in the built
  database. The 3.5 pair `穗穗 -> Suisui` was retired because 3.6 adds a second
  speaker row `穗穗（通讯中） -> Suisui`, which makes the reverse direction
  ambiguous and would fail the check on a correct build. The verifier's tests
  gained a case for that failure shape (a second zh row carrying the same en).
- No production data is shipped by this change: deployments pick the new pin
  up through `deploy/vps-update.sh`, which refreshes the checkout and rebuilds
  and re-verifies the candidate on the target host.

### HTTP API / Web Presentation Layer

- **New: an owner-private web presentation layer.** A mobile-first browser
  interface for dictionary lookup and sentence translation, mounted inside the
  API process as a sub-application over the same protocol-neutral pipeline the
  Telegram bot and the HTTP API already use. It is **off by default**: with
  `WUWATERM_API_WEB_ENABLED` unset there is no route, no sub-application and no
  entry in the published API document, and the process behaves exactly as it
  did before the layer existed. When enabled it requires a device credential
  held server-side (`WUWATERM_API_WEB_DEVICE_TOKEN`) and a marker the reverse
  proxy injects on every proxied request (`WUWATERM_API_WEB_EDGE_SECRET`);
  without that marker it refuses everything, so reaching the loopback port
  directly does not get past the front door. Session lifetime and the ceiling
  on live sessions are `WUWATERM_API_WEB_SESSION_TTL_SECONDS` and
  `WUWATERM_API_WEB_MAX_SESSIONS`. The surface ships no page scripts and the
  browser holds only an opaque HttpOnly session cookie. Decision and cost:
  [ADR 0014](docs/adr/0014-private-web-presentation-layer.md); operation:
  [docs/web-presentation-layer.md](docs/web-presentation-layer.md). The layer
  landed in an earlier pull request without an entry that introduced it; this
  is that entry, written where the surface belongs rather than backdated.
- Malformed serve-only numeric settings no longer block operator credential
  commands such as `device revoke`: `from_env()` now retains their raw forms
  while falling back safely, and `serve` validates all eight values strictly
  before logging, credential-store initialization, app construction or socket
  serving.
- `TERM_QUERY_MAX_LENGTH` is defined once in `wuwaterm_api` and imported by
  both the JSON routes and the web views (previously 200 vs a local 120).
- The web surface envelope now matches the mount path exactly or its
  children only, so a future sibling like `/wuwaterm-webhooks` is not
  rewritten as a web page.
- Restyled the owner-private web surface (markup unchanged): ink-and-gold
  palette with a sharp 3px geometry, underline tabs, gold provenance heading
  on result cards, gold focus rings, and a brand bar across the viewport top.
  All previous functional rules stand: system fonts only, zero scripts, one
  round trip, 16px inputs, `pre-wrap` results.

### Channel Adapter

- Capacity-driven channel skips (`queue_full`, `llm_budget`) now DM the bot
  owner, rate-limited to one notice per 10-minute window with the suppressed
  count folded in. Content gates stay silent by design; only degradation the
  owner could not otherwise see is reported. The notice carries counts and
  internal reason vocabulary only, never post text, and its own delivery
  failure is swallowed into the log.
- Edits now yield to new posts near LLM-budget exhaustion: an edit is
  admitted only if the remaining per-minute budget covers its calls plus a
  two-call headroom (`skipped:edit_yield`). A yielded edit delays refreshing
  an already-delivered translation; a rejected new post would mean no
  translation at all. `ChannelRuntime.budget_remaining` exposes the read-only
  reading the gate uses; admission itself still goes through `reserve`.
- Edit-token bookkeeping is now bounded: tokens share the entry TTL and are
  pruned on `begin_edit` itself, so edits skipped before any remember
  (content gates) no longer accumulate in-process forever.
- Reply-index persistence no longer runs on the event loop. Payloads are
  snapshotted on the loop thread and the write (tmp file + fsync + replace +
  directory fsync) is offloaded, single-flight, coalescing multi-chunk bursts
  into the latest snapshot. Sync callers keep the original inline semantics.
- An edit whose tracked reply still exists but rejects the in-place edit
  ("uneditable", distinct from "gone") now deletes that reply instead of
  leaving it visible but untracked — an orphan that no later edit could ever
  update again. The no-repost-on-gone policy is unchanged.
- Review follow-ups (PR #76): the capacity-notice cooldown and pending count
  are now committed only after the owner DM actually sends; a transient
  failure keeps the count and re-arms after a 60-second retry delay instead
  of silencing alerts for the whole 10-minute window. A successful notice
  clears only the skips it reported, so skips that arrive while the DM is in
  flight survive into the next notice instead of being silently zeroed.
- Edit-token registration is deferred to the moment an edit actually starts
  its first LLM call (the dictionary fast path registers just before its
  emit), so an edit rejected by any bail path — `edit_yield`, `queue_full`,
  `llm_budget`, staleness or authorization rechecks — no longer supersedes an
  admitted in-flight edit whose completed translation was then dropped as
  stale with nothing replacing it.
- The reply index gains `aflush()`, wired to the application's
  `post_shutdown` hook: an offloaded save still queued at shutdown is drained
  (and a cancelled one rewritten inline from memory), and the flush waits out
  an executor write already in flight so an older snapshot cannot replace the
  final one on disk. Replies remembered just before exit survive the restart
  instead of causing duplicate translations. The flush also runs when the
  translator close raises (independent `finally`), and the offloaded writer
  now uses a dedicated single-worker executor whose futures can be awaited
  safely — a job cancelled while still queued raises immediately instead of
  hanging the shutdown flush.
- The edit budget-yield gate is capped by the configured per-minute
  capacity: at `WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE` of 1-3 the
  required+headroom sum is unreachable even on a fresh window, which would
  have yielded every edit forever; a completely unused window now always
  admits one edit.
- A multi-chunk edit whose later tracked reply rejects edits ("uneditable")
  now deletes that reply instead of dropping it from the rebuilt index while
  leaving it visible — the same orphan the first-chunk path already handles.
  When that delete itself fails, the id stays tracked as a trailing stale
  extra rather than at the chunk position, so the next edit does not map a
  fresh chunk onto the still-uneditable reply.
- The capacity owner notice now reports per-reason counts
  (e.g. `llm_budget ×2、queue_full ×1`) instead of labelling the whole
  aggregated count with whichever reason happened to trigger the notice.
- Web stylesheet: decorative glyphs are literal Unicode again — CSS
  code-point escapes like `\25C6` in a plain Python string parse as octal
  control characters, so the heading diamonds and em-dashes rendered as
  mojibake since the restyle.

### Telegram Bot

- The dictionary stage (exact + full-table fuzzy over sqlite) now runs off
  the event loop via the pipeline's existing `offload` seam, matching what
  the HTTP adapter already did; a fuzzy lookup can no longer stall every
  concurrent handler.

### Desktop Client

- The client is **0.2.0**, the first version distributed as a release asset:
  `WuwaTerm-<version>-windows-x64.zip`, the one-folder build, listed in
  `SHA256SUMS`. It is **not code-signed** — SmartScreen will warn on first run,
  and `client/README.md` says so and shows what to do.
- Compatibility contract: client 0.2.x speaks HTTP API `v1`, served by wuwaterm
  0.3.0 or newer. The client checks it on the `/v1/meta` reply the status view
  already fetches when the owner presses 刷新 — **no new request, and none at
  startup**: an unconfigured client still sends nothing. A server reporting an
  API version this client does not support gets a warning naming both versions,
  while the service facts stay on screen and the client keeps working.

### Distribution And Release Pipeline

- New `.github/workflows/release.yml` builds every release asset from one
  reviewed commit: the wheel and the sdist (with `twine check --strict`, the
  packaging audit and a clean-environment install smoke), the Windows client
  zip (built on a Windows runner through `client/build.ps1` and started with
  `--self-check` before it is packaged), a `SHA256SUMS` covering all three, and
  a `release-manifest.json` recording the tag, both versions, the source
  commit, the build time, the image tags and digests, and the game-data pin.
  It has **no tag trigger**: publishing stays a human step, and a draft
  release still creates no tag, so a discarded draft leaves nothing behind.
- The workflow runs in two modes. `workflow_dispatch` with
  `dry_run=false` is the real one and is the only mode that logs in to the
  container registry, pushes an image, or creates the draft release; every
  other run — including the `pull_request` run that fires when this workflow,
  `deploy/**`, `client/**` or `pyproject.toml` changes — is a dry run that
  builds and checks everything and publishes nothing. Write permissions match:
  the workflow default is read-only, `packages: write` exists only on the
  image job, and `contents: write` only on the job that creates the draft.
- Release-pinned container images: `ghcr.io/my-denia/wuwaterm` (runtime) and
  `ghcr.io/my-denia/wuwaterm-builder` (builder), tagged `vX.Y.Z`, `X.Y` and
  `sha-<7>`, carrying the source, version, revision and created OCI labels.
  Both are published because the runtime image is useless without a terminology
  database, which is built by the builder image and is never distributed. The
  images save the local image build and nothing else: the generic path still
  needs a source checkout at the release tag for the Compose files, the
  entrypoints, the data build and the verification scripts. Whether an
  anonymous pull is permitted is a registry-side setting and is stated as
  something to verify, not as something this project has measured.
- New `.github/workflows/selfhost-smoke.yml` follows the container path of
  `docs/self-hosting.md` on a clean runner — build, `refresh-data`,
  `build-db --atomic`, `verify-db`, promote, start the API alone, issue a
  device credential, one exact lookup, one sentence translation against a mock
  model endpoint started in the job, and a credential-store backup and restore
  — and reports per-step timings. It runs on pull requests that touch
  `deploy/**` or that guide, and can be dispatched at any ref.
- `deploy/docker-compose.yml` reads the builder image reference from
  `WUWATERM_BUILDER_IMAGE`, defaulting to the previous fixed local tag, so a
  self-hoster can point it at a pulled image; `deploy/vps-update.sh` never sets
  it and its behaviour is unchanged. `deploy/docker-compose.ghcr.yml` is a thin
  overlay that sets only the three `image:` fields from `WUWATERM_IMAGE_TAG`.
- `docs/release-checklist.md` is rewritten around the new asset policy and the
  new flow: five assets, images on the registry, the client binary stated as
  unsigned, and a readback performed while the release is still a draft.

### Documentation And Project Governance

- Both READMEs open with an audience router — desktop user, Telegram group
  admin, self-hoster, contributor, owner operations — and now name all four
  surfaces, the owner-private web layer among them. Their two validation
  command blocks route to `python scripts/validate.py` instead of listing
  individual gates, so a contributor runs the same list CI's server matrix
  runs — that one job, on Linux, and no more. A pull request is also checked by
  the uv lock-drift job, the packaging audit, the Windows desktop-client build
  and the Docker runtime/builder boundary job, none of which this entry point
  runs; a green local run is evidence about the server gates, not a prediction
  about the pull request. The candidate-database checks stay listed separately,
  because the entry point deliberately does not run them either.
- New `docs/self-hosting.md`: the generic path for someone who is not the
  author — requirements, a source checkout at a release tag, configuration,
  the data build on both the container and the source path, starting the
  surfaces, issuing the first device credential, a first exact lookup and a
  first sentence translation over HTTP, publishing over HTTPS through any TLS
  terminator, upgrade, backup and restore, rollback, and a troubleshooting
  table. `docs/deployment.md` keeps every word it had and gains a scope banner
  saying it is the author's own production runbook, not the general path.
- New `docs/support-matrix.md`: which Python versions are tested where, why the
  server suite is not supported on a Windows host, what the desktop client
  needs, and the client-to-API compatibility contract.
- New `SECURITY.md` (private reporting through GitHub, what never to paste into
  a report, what is in and out of scope), `CONTRIBUTING.md` (setup, the one
  validation command, what each gate is for, what makes a change easy to
  accept) and `SUPPORT.md` (where to ask, and the boundary of a best-effort
  hobby project); four GitHub issue forms with a redaction acknowledgement,
  their `config.yml`, and a pull-request template. The acknowledgement covers
  host names, addresses and paths that identify a deployment as well as
  credentials and Telegram identifiers, which is what `SECURITY.md` asks a
  reporter to remove and what `SUPPORT.md` says the forms cover.
- `docs/architecture.md` describes the private web layer as the third
  presentation layer inside the API process, including how it reaches the
  pipeline (in process, not over HTTP) and what it deliberately is not; the
  ADR index lists 0013 and 0014; ADR 0014 moves from Proposed to Accepted with
  its basis written out.
- Two corrections of claims that had drifted from the code: `docs/validation.md`
  no longer says the hygiene and non-goal gates catch tokens, API keys or
  Telegram identifiers — it now says what each one actually checks and that no
  gate in this repository scans for secrets — and
  `docs/web-presentation-layer.md` no longer says the edge block has never been
  validated on the target host.
- `deploy/env.example` and `.env.example` document the five
  `WUWATERM_API_WEB_*` keys, commented out, with the note that they belong to
  the API process only. `client/README.md` gains the user path: download the
  release zip, check it against `SHA256SUMS`, unpack it anywhere, and what an
  unsigned build looks like on first run.

### Developer Experience

- `scripts/validate.py` is now the single offline validation entry point:
  hygiene, non-goals, architecture boundaries, the API contract, ruff and the
  test suite, in that order, with `--list` to print what each gate fails on and
  `--quick` to stop before the suite. CI's server matrix runs that one command
  instead of listing the gates itself, so a contributor and a pull request run
  the same list. It is standard library only, and every step runs under the
  interpreter that ran the script, so there is no second Windows form to keep in
  step. Its `--client` step is the exception and says so: no single interpreter
  runs both sides, so that step runs the client's own environment or explains
  how to create it.
- Ruff joins the dev extra, bounded to one minor, with the enabled rule set
  written out (`E4`, `E7`, `E9`, `F`) instead of left to the tool's defaults — a
  wider range would silently change what the gate enforces. The unused imports
  it reported are removed.
- The CI test matrix widens to Python **3.11, 3.12, 3.13 and 3.14** on
  `ubuntu-latest`, so no version inside the declared `requires-python >=3.11`
  floor is untested; `docs/support-matrix.md` records what runs where and what
  the Windows-host reds are.
- `.github/dependabot.yml` proposes GitHub Actions and uv updates monthly, in
  groups rather than one pull request per bump.
- The four hygiene-test defects of issue #75 are fixed and each fix is pinned by
  a test that fails when the handled branch is removed: the Git 2.51+
  `<oid> submodule` response line and the broken-pipe branch gain discriminating
  cases; the missing-response test is rebuilt on a response stream written out
  in the test, because as written it had stopped measuring the marker rule and
  started measuring which git the machine happens to have; and the
  newline-in-path test is skip-guarded on Windows, where its own fixture cannot
  run.

### Upgrading From 0.3.0

- **The terminology database must be rebuilt.** The game-data pin moves from
  3.5.0 to 3.6.0, and no release ships a database. A production host picks the
  pin up through `deploy/vps-update.sh`, which refreshes the checkout, rebuilds
  the candidate, verifies it and promotes it transactionally. A source install
  reruns `refresh-data` and `build-db --atomic` and re-verifies with
  `scripts/verify_db.py` (see [docs/data-refresh.md](docs/data-refresh.md)).
- **Nothing else has to change.** There is no state-directory migration, no
  removed setting, and no change to the Telegram or HTTP surfaces a 0.3.0
  deployment already serves. The owner-private web layer is new and **off by
  default**: with `WUWATERM_API_WEB_ENABLED` unset the process behaves exactly
  as 0.3.0 did.
- **The desktop client is 0.2.0** and is distributed as an unsigned portable zip
  on the release page. A 0.1.x client keeps working against this server; what
  0.2.x adds is the compatibility check, not a new requirement.
- **Release assets moved.** Releases now carry the wheel, the sdist, the client
  zip, `SHA256SUMS` and `release-manifest.json`, all built by
  `.github/workflows/release.yml` from one reviewed commit; container images are
  published to the registry rather than to the release page. Verify that an
  image pull works for you before relying on it; if it is denied, build from
  source ([docs/self-hosting.md](docs/self-hosting.md)).

## 0.3.0 - 2026-08-12

API-first release: the Telegram-only bot becomes a multi-adapter system. One
protocol-neutral application layer now serves two presentation adapters — the
existing Telegram bot and a new versioned HTTP API with revocable
device-principal authentication — plus a Windows desktop client that consumes
the API. Game-data pin is unchanged from 0.2.x (Wuthering Waves 3.5.0 /
resource 3.5.5 / changelist 8059200 at
`dae29691c04ef0f48d0810b5d244fb0b37288c60`).

### Maintainer Architecture

- New protocol-neutral application layer (`src/wuwaterm/application.py`) owns
  the dictionary-first translation pipeline exactly once. `bot.py` keeps the
  Telegram wording, HTML parse mode and UTF-16 splitter and delegates the
  pipeline; no intentional Telegram behaviour change.
- Boundary guard gains an `Application` layer that may not import presentation
  modules or the Telegram SDK, even under `TYPE_CHECKING`.

### HTTP Adapter (new)

- New `wuwaterm_api` package: a versioned, plain-text HTTP surface
  (`POST /v1/translations`, `GET /v1/terms`, `GET /v1/meta`, `GET /healthz`,
  `GET /readyz`) served by the same dictionary-first pipeline as the bot.
- Revocable device credentials in their own store (`state-api/devices.db`),
  registered and withdrawn by an operator through the new `wuwaterm-api device`
  commands. The operator supplies the secret on standard input and only a
  salted scrypt verifier is stored: the service never produces or prints
  credential material, so none can reach a log, a terminal recording or a
  captured command output through it.
- Stable error envelope with enumerated codes, request ids, per-device request
  limits, a streaming body-size cap, and a time budget applied to both the body
  read and the handler. Credential verification is itself bounded, so the
  deliberately expensive check cannot become the load.
- Committed contract snapshot `docs/api/openapi.json` with a drift gate
  (`scripts/check_api_contract.py`), which also re-applies the repo's product
  token bans to that JSON artifact.
- The published wheel now ships the `wuwaterm_api` package and a
  `wuwaterm-api` entry point; `scripts/check_package_artifacts.py` requires
  both, so packaging drift stays caught. The `api` extra carries FastAPI and
  uvicorn; the core dependency set is unchanged.
- API budgets are per process and separate from the bot's: its own translator
  instance, its own model concurrency cap (default 2) and its own per-minute
  call budget (default 30).
- The service now writes one structured completion record per request —
  correlation id, method, route, status, duration, and the redacted device
  principal when one authenticated — and `wuwaterm-api serve` installs the
  handler that emits it, on standard error (`WUWATERM_API_LOG_LEVEL`, default
  `INFO`). It had none: every `LOGGER` call in the adapter went nowhere in a
  deployed process, so a request id handed to a client matched nothing on the
  server. The adapter's other diagnostic lines are unchanged and are not part of
  the one-per-request guarantee. The operator subcommands and any program
  importing the application still configure no logging at all.
- Records name the matched route TEMPLATE, or an escaped, bounded target when
  nothing matched, instead of the decoded request path: that value is chosen by
  an unauthenticated caller and is read in a terminal. A target that could be a
  credential is replaced entirely. Nothing the service itself knows — the
  credential it verified, the device id behind the principal, the text it
  translated — appears in any record, which is asserted against captured
  records rather than by inspection.

### Desktop Client (new)

- New `client/` tree: a Windows desktop client for the HTTP adapter, with its
  own `pyproject.toml` outside `src/` so it is never part of the `wuwaterm`
  wheel. `scripts/check_package_artifacts.py` fails if `wuwaterm_client` ever
  appears in a built distribution.
- It calls the published contract and renders what comes back: plain-text
  translation with an automatic or forced direction and mid-flight
  cancellation, dictionary lookup, service status, and the error envelope's
  stable codes plus the transport states only a client can know (offline,
  timed out, cancelled). It contains no translation logic and no Telegram
  concept.
- One device credential, held only in the Windows Credential Manager, never
  written to a config file.
- `client/build.ps1` produces a one-folder PyInstaller build and then runs the
  artifact's own start-up self-check, so a build that cannot start fails the
  build. A `windows-latest` CI job runs the client suite, builds through the
  same script and uploads the artifact.
- A missing, unreadable, malformed or unusable `config.json` now leaves the
  client in an explicit unconfigured state instead of silently substituting a
  local development address: the main window states the configured server
  address (or that none is configured, and where to set one), and every
  request path refuses with the stable code `not_configured` before the
  credential store is read. The settings field opens empty when unconfigured
  and never invents an address (#59).
- Settings saves are atomic (temporary file, fsync, `os.replace`) with
  descriptor-safe failure cleanup, so an interrupted save cannot leave a
  truncated settings file; changing the server address cancels in-flight
  requests and clears answers derived from the previous endpoint (#59).

### Deployment

- New Compose service `wuwaterm-api` runs the same runtime image with
  `command: ["api"]`, mounts the terminology database read-only and keeps its
  own writable `state-api/`, a sibling of the bot's state directory rather than
  a child of it, so the bot's read-write state mount cannot reach the
  credential store. It binds loopback only and publishes no ports, so the
  service opens no listener anything outside the host can reach; a desktop
  client reaches it through a path route on an HTTPS site the host already
  serves, and authenticates every call with its own device credential.
- Documented operator commands read `WUWATERM_API_PORT` from the serving
  container instead of assuming the default, so a deployment that configured
  another port is not read back against a closed one.
- The runtime entry point accepts `bot`, `api` and `device` and still exits 64
  for every data-build command. CI asserts both halves.
- `deploy/vps-update.sh` now stops, restarts, smokes and reads back BOTH
  serving containers. The API smoke runs inside its own container over
  loopback, so nothing has to be exposed to run it.
- Rollback restores what was RUNNING, not what merely existed: both surfaces
  record their running state before the deployment, both go down before the
  database is rolled back, and a stop that fails aborts the restoration
  entirely rather than reverting a database underneath a container that may
  still be serving it.
- The device store refuses to start an empty store at the new
  `state-api/devices.db` while an older `state/api/devices.db` still holds the
  verifiers; that would have looked like a clean start while every registered
  device stopped authenticating.
- `scripts/check_non_goals.py` skips nested virtual environments and build
  output at any depth, so a per-component venv cannot drown the product gate in
  third-party matches.
- The API's default port is now **8788**. The previous default was already
  bound on the deployment target by an unrelated service and was the upstream
  of that host's existing routes, so the old default would have taken over a
  running service rather than adding one. The port remains a setting; nothing
  in the contract or the client depends on the number. An existing `.env` that
  sets `WUWATERM_API_PORT` explicitly still wins over both defaults, so the
  deployment guide now says to update that line and read the port back from the
  recreated container: a changed default does not reach a host that pinned the
  old one.

### Documentation

- `docs/architecture.md` rewritten for the system that now exists: two
  presentation adapters over one application layer plus a client that consumes
  the API, the component map including the adapter's import allowlist, the
  trust boundaries including the public HTTPS edge, the two separate identity
  models and the explicit statement that neither can grant the other, and a
  cost-topology section stating that the per-process budgets are never global
  and that the worst case is their sum.
- Four new decision records: [0009](docs/adr/0009-http-api-adapter.md) the HTTP
  adapter (amending the context of 0001 and 0003, whose long-polling decision
  is unchanged), [0010](docs/adr/0010-device-principal-authentication.md)
  device-principal authentication,
  [0011](docs/adr/0011-pc-client-stack.md) the PC client stack and its
  transport policy, and
  [0012](docs/adr/0012-client-transport-selection.md) the transport selection
  itself, with its trust boundary, threat model, credential lifecycle,
  endpoint configuration, verification, deployment and rollback, and the
  documented future migration path.
- `docs/deployment.md` gains the route that publishes the API, its backup,
  reload and one-block rollback, and the readback that has to happen on the
  client machine to mean anything. `docs/validation.md` gains the contract,
  client-transport and client-build gates and where each runs.

### Upgrading From 0.2.1

- Deploy only through `deploy/vps-update.sh` on a clean Git checkout where
  `HEAD == origin/main` (see `docs/deployment.md`). The updater now manages
  BOTH serving containers; rollback covers both.
- The HTTP API is a second, loopback-only container. Its credential store
  lives in a new `state-api/` directory, a sibling of the bot's `state/`.
  Devices are registered by an operator over SSH with
  `wuwaterm-api device issue` (secret supplied on standard input) and revoked
  with `wuwaterm-api device revoke`. Nothing is exposed publicly by default;
  publication happens through a reverse-proxy path route the host already
  serves (see `docs/deployment.md` and ADR 0012).
- The API's default port is 8788; a `.env` that pins `WUWATERM_API_PORT`
  wins over the default.
- The desktop client is built from source with `client/build.ps1` (or taken
  from the CI artifact); it stores its device credential only in the OS
  credential manager and starts unconfigured until a server address is set
  in Settings.
- No bot state-directory migration and no game-data pin change from 0.2.1.
  Telegram behaviour is unchanged apart from the shared application layer
  refactor, which is covered by the existing test suite.

## 0.2.1 - 2026-08-06

Maintenance release packaging post-v0.2.0 production hardening already merged
as PRs #41–#45. Game-data pin stays Wuthering Waves 3.5.0 / resource 3.5.5 /
changelist 8059200 at `dae29691c04ef0f48d0810b5d244fb0b37288c60`.

### Telegram Runtime (#41–#43)

- Channel LLM content-shape failures (`invalid_response` / `html_integrity`)
  now retry once as plain text instead of dropping the post, within the
  per-minute call budget (including correct behaviour when the budget is 1).
- Malformed API envelopes are reported as `invalid_api_response`, separate
  from content-shape `invalid_response`.
- Inline `/tr <text>` preserves Telegram rich-text entities; PTB error
  handling covers NetworkError-style update failures.
- Normalization fixes: spoiler markers and version tags no longer eat
  mid-sentence prose or mangle URLs; term-lock / fuzzy design flaws that
  produced wrong translations are repaired with regression tests.
- Delivery and observability hardening for linked-channel paths.

### Maintainer Architecture (#44–#45)

- Formal architecture map (`docs/architecture.md`) and ADRs 0001–0008.
- Fail-closed import-boundary guard (`scripts/check_architecture_boundaries.py`)
  wired into local validation and CI, with Codex P2 follow-ups for nested
  packages and TYPE_CHECKING / presentation import rules.
- No intentional runtime behaviour change in #44–#45.

### Upgrading From 0.2.0

- Deploy only through `deploy/vps-update.sh` on a clean Git checkout where
  `HEAD == origin/main` (see `docs/deployment.md`). Runtime fixes from
  #41–#43 may already be live on long-running hosts; this release still
  advances package version metadata and ships the architecture guard.
- No state-directory migration and no game-data pin change from 0.2.0.

## 0.2.0 - 2026-07-17

Production hardening release: Wuthering Waves 3.5 data pin, transactional VPS
deployment, Telegram structural safety, and default CI packaging gates.

### Upgrading From 0.1.0

- The deployment target must be a clean Git checkout with an `origin` remote
  and a `main` ref; exported non-Git source copies are intentionally not
  deployable. Live upgrades go through `deploy/vps-update.sh`; see
  `docs/deployment.md`.
- Runtime state (`chat_settings.json`, `channel_replies.json`) moved from
  `data/` to `state/`. The updater and runtime perform a validated one-time
  migration and never overwrite existing state files.
- The runtime image only runs the bot; data refresh/build/verify commands
  moved to the builder image. Rebuild the terms database with the 3.5 pin via
  the transactional updater (`docs/data-refresh.md`).

### CI And Packaging

- Added `scripts/check_package_artifacts.py`, a wheel/sdist content audit that
  fails on generated databases, game data, runtime state, environment files,
  deployment internals, secret-looking files, missing package members, and
  version-metadata drift against `pyproject.toml`.
- Extended default CI with lockfile drift checking (`uv lock --check` pinned to
  the deploy image's uv version), wheel/sdist build with strict metadata
  validation and clean-venv install/import/CLI smoke for both artifacts,
  deploy shell script syntax checks, compose config validation, and Docker
  runtime/builder boundary assertions (runtime refuses non-bot commands and
  ships without git/uv).

### Data And Deployment

- Updated the active source profile to Wuthering Waves 3.5.0 / resource 3.5.5
  / changelist 8059200 at exact upstream commit
  `dae29691c04ef0f48d0810b5d244fb0b37288c60`, with observed checkout and
  README provenance recorded in generated DB metadata.
- Added read-only strong candidate verification, including schema, integrity,
  metadata, category, and `穗穗 -> Suisui` exact-hit gates.
- Made VPS updates transactional around a separately verified candidate and
  immutable revision-labelled image, with DB/image/pointer rollback and an
  immutable deployment manifest. Builder containers no longer receive the
  runtime `.env`.

### Telegram Runtime

- Protected every Telegram HTML tag, attribute, link, custom emoji id, and
  entity with opaque structural placeholders so only visible text reaches the
  translator; structural drift now fails closed.
- Added bounded linked-channel admission, atomic multi-chunk LLM call budgets,
  post-queue freshness/authorization checks, privacy-safe outcome telemetry,
  and owner status counters.
- Hardened linked-channel reply-index schema validation and persistence
  diagnostics, including explicit reporting when directory durability is
  uncertain.
- Avoided fuzzy dictionary scans for long translation inputs while retaining
  exact and short ASCII/pinyin lookup behavior.

## 0.1.0 - 2026-07-04

Initial release preparation for the self-hosted WuWa Term Bot.

### Added

- Dictionary-first Chinese to official English and English to Chinese term
  lookup from a locally built SQLite database.
- Term-locked sentence translation through an OpenAI-compatible endpoint, with
  exact database hits returned before any LLM call.
- Telegram bot commands for private and group use, including group allowlists,
  public-mode controls, linked-channel auto-translation, rate limits, and
  privacy-safe operational logging.
- Local validation and hygiene scripts that guard against committing generated
  game data, generated databases, and unsupported runtime surfaces.

### Data Source

- Supported source profile: `arikatsu`
- Supported game data version: `GameVer 3.4.0 | ResVer 3.4.13`
- Pinned source repository: `https://github.com/Arikatsu/WutheringWaves_Data`
- Pinned source commit: `58ec43698d2b4e188cb285467ce1ae887612dd92`

### Known Limitations

- The bot is self-hosted; no public hosted service is provided by this
  repository.
- Live Telegram behavior still requires owner-provided bot credentials and
  chat configuration.
- Free-text sentence translation requires a separately configured
  OpenAI-compatible endpoint.
- Generated TextMap data and generated term databases are local artifacts and
  are not published in this source repository.
