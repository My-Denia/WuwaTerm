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

Pair review (`POST /v1/reviews`, `review-v1`/`review-v2`) is dictionary-only:
nothing leaves the process. Semantic review (`POST /v1/semantic-reviews`,
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

A server lists `semantic-review-v1` and `translation-context-v1` in
`GET /v1/capabilities` only when a model is configured. Without one the
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
- `WUWATERM_LLM_MAX_CONCURRENCY`, default `4`, range `1..64`; max
  in-flight LLM calls.

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
