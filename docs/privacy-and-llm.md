# Privacy And LLM

This guide describes the privacy boundary around dictionary lookups, LLM calls,
runtime state, and logs.

## Dictionary-First Boundary

An exact database hit returns the official string byte-for-byte from the local
SQLite database and does not call the LLM. This applies to command paths and
linked-channel auto-translation. Free-text sentence translation supplies known
DB terms to the model as contextual references together with their official
translations, so an in-game term keeps the official string while a word that is
also an ordinary word keeps its sentence meaning. Without a configured model the
translator substitutes the official terms locally instead.

Generated TextMap data, SQLite DBs, runtime settings, channel reply indexes,
tokens, API keys, and real Telegram chat/user/message ids must stay out of
commits and public logs.

## Anonymous Public Beta Site

The browser and proxy are maintained in [WuwaTerm-Web](https://github.com/My-Denia/WuwaTerm-Web).
Its [privacy guide](https://github.com/My-Denia/WuwaTerm-Web/blob/main/docs/privacy-and-llm.md) records Web-specific behavior and limitations.

The no-account public beta is
<https://wuwaterm.denia-official.chatgpt.site>. Its browser sends terms and
sentences only to same-origin `/api/*`; the Hosted server-side proxy forwards
them to the authoritative VPS `/v1` API with a credential that never enters the
browser. Dictionary-first processing and term selection remain backend behavior,
not a second implementation in the Site.

The WuwaTerm application does not create visitor profiles or history and does
not read, parse, store or infer visitor IP addresses. Its shared-pool row stores
aggregate time windows, admitted request counts and translation input-character
totals — not input, output, request IDs, browser identifiers or per-person
allowances. Results remain in the current page. The review workbench asks before
an ordinary leave or refresh when that page cannot restore the work from the
manuscript imported in the visit. The site cannot confirm a manuscript
download reached disk, and a crash or forced quit can still discard the page.

That application boundary is not a promise that every infrastructure layer has
zero logs or retention. Hosting, network transport and the configured model
provider may process technical data for operation, security or fault diagnosis.
Do not submit personal information, passwords, tokens or other sensitive text.
The shared beta is first-come, has no per-visitor fairness guarantee and no SLA;
see [Public Beta Site](sites.md) and the live
[privacy notice](https://wuwaterm.denia-official.chatgpt.site/privacy).

## Semantic Review Sends Both Texts

Pair review (`POST /v1/reviews`, `review-v1`/`review-v2`) is dictionary-only
and structure checks (`POST /v1/structure-checks`, `structure-check-v1`) read
only the submitted segments: for both, nothing leaves the process and nothing is
stored. The structure-check log line carries counts only, never text. Semantic review (`POST /v1/semantic-reviews`,
`semantic-review-v1`, [ADR 0018](adr/0018-semantic-review.md)) is a separate,
model-backed path, and it sends more than translation does:

- the source segment **and** the submitted translation of that segment, each
  at most 2,000 Unicode scalars;
- optional read-only context: the previous segment's source and translation,
  each at most 400 scalars;
- optional human precedents: up to 12 earlier choices the user made in the
  same manuscript, each with a surface, a rendering and an 80-scalar snippet of
  the sentence it came from.

The server does not write any of these texts, the model output, or the issues
to disk, and never logs them. Its log line for a semantic review carries the
redacted device, the request id, the two input lengths, the issue count, the
unanchored-drop count and the status. A client decides per segment whether to
send; segments that are blank, unpaired or need a manual split are never sent
([segmentation-v1](segmentation-v1.md)).

Semantic review is experimental and off by default
(`WUWATERM_SEMANTIC_REVIEW_ENABLED`). A server lists `semantic-review-v1` in
`GET /v1/capabilities` only when it is switched on and a model is configured;
`translation-context-v1` needs only a model, and `structure-check-v1` is always
listed. While semantic review is off the
endpoint validates the request and answers `llm_unavailable` without calling a
model or sending any text anywhere.

Translation context (`translation-context-v1`, [ADR 0019](adr/0019-document-context-and-human-precedents.md))
is opt-in per request: a `POST /v1/translations` body without `context` sends
exactly what it sent before. With `context`, the previous segment's source and
translation (at most 400 scalars each) and the listed precedents go into the
same model request. Long texts that Core cuts itself (Telegram channel posts
over the per-call limit) carry the previous segment the same way.

Every HTTP attempt to the model gateway, from any path, writes one log line
with its purpose (`translation` or `semantic_review`), outcome class, HTTP
status and elapsed time: no URL, key, model name, prompt or output. It exists
so that call counts can be reconciled against a budget ledger.

Evaluation corpora used to accept this feature stay outside every repository.
Contract fixtures are synthetic and quote no corpus text.

## LLM Configuration

Optional LLM environment variables:

- `WUWATERM_OPENAI_BASE_URL`, set explicitly to your OpenAI-compatible or
  LiteLLM gateway URL.
- `WUWATERM_OPENAI_API_KEY`.
- `WUWATERM_OPENAI_MODEL`.
- `WUWATERM_LLM_TIMEOUT_SECONDS`, default `45`, finite range `0.1..300`.
  Timeout ladder across components: Core single-call LLM timeout `45 s`, Core
  semantic review timeout `75 s` (`SEMANTIC_TIMEOUT_SECONDS`), Core HTTP API
  request deadline `90 s` (`WUWATERM_API_REQUEST_TIMEOUT_SECONDS`), Web BFF
  proxy `100 s` for translation/semantic (`30 s` for review, `8 s` for
  lookup/health/capabilities), and Windows client `100 s`
  (`DEFAULT_TRANSLATE_TIMEOUT_SECONDS`; `10 s` for lookup/status).
- `WUWATERM_LLM_MAX_CONCURRENCY`, default `4`, range `1..64`; max
  in-flight LLM calls.
- `WUWATERM_TRANSLATION_THINKING_ZH_EN` (requests translating Chinese into
  English) and `WUWATERM_TRANSLATION_THINKING_EN_ZH` (English into Chinese),
  default `disabled` into English and `omit` into Chinese; `disabled`, `low`,
  `high` and `omit` are accepted in both directions and `auto` only into
  Chinese. `omit` sends neither field, so the provider default applies, which on
  DeepSeek is high-effort thinking and can outlast
  `WUWATERM_LLM_TIMEOUT_SECONDS` on a long segment. `disabled` sends
  `thinking: {"type": "disabled"}`. `low` and `high` turn thinking on at that
  `reasoning_effort`. Measured into English: `disabled` removed the long-segment
  timeouts, which occur only into English, and it is the default there. Into
  Chinese `omit` is the default and names nothing, so an unconfigured
  deployment sends exactly the request it always sent. `auto` is the opt-in that
  picks one of the modes per request from the request's own shape: 25 or more
  paragraphs or 4 or more locked official terms is sent `disabled`, anything
  shorter as `low`. That split follows the measurement behind it — long or
  lock-dense documents ran past the model timeout with thinking on and answered
  in about 3 s with it off, while a 23-paragraph document answered in 20.7 s
  under `low` — and a full held-out run scored it at or above every frozen gate,
  including the long documents that used to time out. Setting any explicit
  `omit`, `disabled`, `low` or `high` replaces `auto` in that direction, so a
  deployment that opted in can pin one mode without unsetting `auto`. Known
  regression with `disabled` into English: a character-mechanic name that
  contains a one-character resonator name may be rendered with the resonator
  name; set `WUWATERM_TRANSLATION_THINKING_ZH_EN=omit` to restore the previous
  behaviour.
  Translation requests only; a blank value is that direction's default and an
  unknown value logs a warning naming the variable and uses that default. Read
  on every request.

### Provider Compatibility

Provider-specific `thinking` and `reasoning_effort` fields require support from
the configured endpoint. An HTTP 400 whose `error` object has
`code: "unsupported_parameter"` and `param: "thinking"` or
`param: "reasoning_effort"` gets diagnostic detail `unsupported_thinking` or
`unsupported_reasoning_effort`. The reason remains `http`. A fixed warning
names only the allowed parameter and the configuration options; it never
copies the provider's message, source text, secrets, or URL. An error message
merely mentioning thinking does not trigger this diagnostic.

For translation, explicitly set `WUWATERM_TRANSLATION_THINKING_ZH_EN=omit`
(Chinese into English) or `WUWATERM_TRANSLATION_THINKING_EN_ZH=omit` (English
into Chinese) for the affected direction, or select a provider supporting the
requested fields. `omit` sends neither reasoning field and uses the provider
default; it does not disable thinking. These direction variables affect
translation only.

The failed request still makes one POST. WuwaTerm does not retry it, remove
fields automatically, or infer support from the model name. The existing
user-facing unavailable notice and HTTP 503 `llm_unavailable` envelope stay the
same; the specific detail is available in server diagnostics. Ordinary HTTP
errors retain their existing classification.

A strict endpoint is served by the directions' defaults: `disabled` into English
and `omit` into Chinese both send only the fields the endpoint accepts. Into
Chinese `auto` is the opt-in that sends the reasoning fields (a short request as
`low`, a long or lock-dense one as `disabled`), so a deployment that opts in
there needs `WUWATERM_TRANSLATION_THINKING_EN_ZH=omit` or a provider that
supports them. The two corrective calls described under
[translation strategy](#llm-configuration) — one paragraph-count repair and one
fast fallback on an `auto` timeout — are the only retries in the translation
path, each at most one attempt, and a request rejected by the provider is still
one POST because neither applies to an HTTP failure.

## Configuration Validation

Non-empty invalid numeric or boolean settings fail startup instead of being
silently coerced. Numeric settings must be inside their documented ranges;
LLM timeouts must also be finite, so `nan` and `inf` are rejected. Runtime
booleans accept only `1/true/yes/on` and `0/false/no/off`
(case-insensitive); unset or empty values use their defaults. Validation errors
name the variable and expected type/range but never echo its raw value, which
may contain operational or sensitive material.

`OPENAI_BASE_URL` and `OPENAI_API_KEY` are accepted fallbacks for the endpoint
and key; `WUWATERM_OPENAI_MODEL` is still required for LLM use.

No Telegram token, LLM key, endpoint, or model is hardcoded.

## Source Text Is Untrusted

The LLM system prompt treats user/channel text as untrusted source text: it is
translation input only, and instructions inside that source text must not be
followed. The prompt stays short and requires structural placeholders exactly
unchanged.

This is a prompt-level guard. The stronger protection is structural: in HTML
mode every tag, attribute, link, custom emoji id, and entity is replaced before
the model sees only visible text, and restore rejects raw or unknown structure.
Hard-locked name/term placeholders must also be copied exactly once.

## Placeholder Integrity

Placeholders use per-request nonce shapes: `__WUWA_TERM_...__` for terms that are
still hard-locked (names translating into Chinese, and the no-model path) and
`__WUWA_HTML_...__` for structure. User literal placeholders such as `__TERM_0__`
and strings that only look like internal placeholders are not restored unless
they were generated for that request.

After the LLM returns, restore performs integrity checks. Missing, duplicated,
or malformed hard-locked term placeholders fail closed instead of producing a
translation that silently corrupts an official name; structural placeholders
fail closed on any drift. A contextual term reference has no placeholder to
restore, so the model's sentence is returned as written and a review check is
what reports the official term it used.

## Settings And Authorization

Private `/tr` is owner-only. Missing or empty `OWNER_USER_ID` means private
`/tr` rejects everyone and logs a startup warning. Group serving is gated on the
authorization allowlist, and public mode never bypasses that allowlist.

The allowlist and `/public` state are persisted to `WUWATERM_SETTINGS_PATH`.
The linked-channel reply index is persisted to `WUWATERM_CHANNEL_REPLY_INDEX_PATH`.
When those explicit paths are unset and `WUWATERM_STATE_DIR` is set, the files
default to that writable state directory. Otherwise they default alongside the
DB for local compatibility. Both are runtime data files and should stay in an
ignored local volume or directory, not in commits.

## Logging And Status Output

`/status` is owner-only and reports operational counts and flags only:
dictionary term count, data profile/short commit, LLM configured yes/no,
channel auto-translation on/off, tracked channel-post count, allowlist/public
counts, channel reply persistence health, channel active/pending/high-water
counts, aggregate outcome counters, admission caps, and message limits. It does
not print secrets, storage paths, chat ids, message bodies, or URLs. Channel
event logs likewise contain only stage/reason, redacted identifiers, lengths,
mode, and aggregate admission counts; they never include source or translated
content.

Deployment smoke checks do not print Telegram tokens or chat ids.
