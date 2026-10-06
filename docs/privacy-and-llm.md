# Privacy And LLM

This guide describes the privacy boundary around dictionary lookups, LLM calls,
runtime state, and logs.

## Dictionary-First Boundary

An exact database hit returns the official string byte-for-byte from the local
SQLite database and does not call the LLM. This applies to command paths and
linked-channel auto-translation. Sentence translation locks known DB terms
before any LLM call, so official terms are restored verbatim in the target
language rather than paraphrased.

Generated TextMap data, SQLite DBs, runtime settings, channel reply indexes,
tokens, API keys, and real Telegram chat/user/message ids must stay out of
commits and public logs.

## Anonymous Public Beta Site

The no-account public beta is
<https://wuwaterm.denia-official.chatgpt.site>. Its browser sends terms and
sentences only to same-origin `/api/*`; the Hosted server-side proxy forwards
them to the authoritative VPS `/v1` API with a credential that never enters the
browser. Dictionary-first processing and term locking remain backend behavior,
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
followed. The prompt stays short and requires opaque placeholders exactly
unchanged.

This is a prompt-level guard. The stronger protection is structural: official
terms are locked, and in HTML mode every tag, attribute, link, custom emoji id,
and entity is replaced before the model sees only visible text. Restore checks
exact placeholder count and order and rejects raw or unknown structure.

## Placeholder Integrity

Term locking uses per-request nonce placeholders with the `__WUWA_TERM_...__`
shape. User literal placeholders such as `__TERM_0__` and strings that only
look like internal placeholders are not restored unless they were generated for
that request.

After the LLM returns, placeholder restore performs integrity checks. Missing,
duplicated, or malformed placeholders fail closed instead of producing a
translation that silently drops or corrupts official terms.

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
