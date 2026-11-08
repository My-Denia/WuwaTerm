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

**Amendment (2026-11-08, [ADR 0017](0017-contextual-term-references.md)).** On
the model path, free-text terms are supplied to the model as contextual
references instead of forced placeholders, so a word that is also an ordinary
word keeps its sentence meaning and an English term can take the number and
article it needs. Exact lookup (step 2), the no-model substitution path, the
strict `restore()` contract for the placeholders that are still produced, and
review are unchanged.

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
