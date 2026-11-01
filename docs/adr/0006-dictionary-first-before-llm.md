# ADR 0006: Dictionary-first before LLM

- Status: Accepted
- Date: 2026-08-05

## Context

Official localization strings must not be paraphrased when the local dictionary
already contains them. LLM calls cost money, add latency, and expand the privacy
surface (source text leaves the host).

## Decision

Always attempt structured dictionary work before any model call:

1. Normalize / prepare text.
2. Exact SQLite lookup (`TermService.lookup_exact`) — on hit, return official
   text and **do not** call the LLM.
3. For free text, lock known DB terms to opaque placeholders, then call an
   OpenAI-compatible endpoint only if configured and needed.
4. Restore placeholders verbatim; reject broken model output rather than
   shipping raw placeholders or invented official terms.

Fuzzy short dictionary answers on the command path also short-circuit without
LLM (`bot._fuzzy_dictionary_answer`).

## Consequences

- Positive: exact hits are deterministic and offline.
- Positive: privacy boundary is structural, not only prompt text
  (`docs/privacy-and-llm.md`).
- Negative: dictionary coverage and build quality bound result quality;
  missing terms still require LLM or fail closed.
- Constraint: no secondary name-map layer or inline-mode glossary rewrite.

## Evidence

- `src/wuwaterm/bot.py` `translate_query`, `translate_query_async`,
  `translate_request_async`
- `src/wuwaterm/sentence.py` `lock_terms`, `translate_async`, `_llm_configured`
- `src/wuwaterm/channel.py` exact-hit branch before LLM
- `docs/privacy-and-llm.md`, `tests/test_bot.py`, `tests/test_sentence.py`
