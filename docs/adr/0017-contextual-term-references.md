# ADR 0017: Contextual term references on the model path

- Status: Accepted
- Date: 2026-11-08
- Amends: [ADR 0006](0006-dictionary-first-before-llm.md) steps 3–4 for the
  model path. Exact lookup, the no-model path, whole-input lookup and review are
  unchanged.

## What the placeholder design solved

[ADR 0006](0006-dictionary-first-before-llm.md) replaced every dictionary
surface in the source text with an opaque `__WUWA_TERM_...__` token before the
model saw it, and restored the official term verbatim afterwards. That decision
solved a real problem: a model could not paraphrase, mistranslate or invent an
official name, and broken output failed closed instead of shipping a wrong
term. It is why `/tr Jinhsi` and a sentence containing `今汐` both preserve the
official pair, and why a dropped token returns an error rather than a guess.

## Why it changed

A forced token hides the very word that decides the meaning, and fixes the
target word form before the sentence exists:

- `Encore! The crowd cheered for another song.` matched the resonator `安可`
  and became a character name. `白芷` (a herb), `椿` (a tree or a plant name),
  `秋水` (autumn water) and `散华` have the same shape.
- P22, a real saved API response, read `You said before that Resonator can
  activate ...` because the Chinese `共鸣者` had no number and `restore`
  substituted singular `Resonator`; `三位共鸣者` would produce
  `three Resonator`, and no article could be chosen for `Echo` because the
  article precedes a token whose expansion the model cannot see.
- The owner ruling of 2026-11-09 is to use terms as references and let the
  sentence decide, not to keep adding per-word exceptions (MY-18).

Recorded evidence: the MY-18 and MY-16 descriptions and comments, the P22
archive note referenced there, and a real-model probe of this decision on
2026-11-08. With Gemini 4 Argon, P22 produced `Resonators` with no article
error, `Encore!` in prose produced 返场, a paragraph with both meanings produced
安可 and 返场, `白芷` in a prescription produced "angelica root", and `欢迎椿`
produced Camellya. With DeepSeek V4.1 Flash the same P22 plural was correct and
the mixed paragraph separated the two meanings, but the standalone exclamation
still became 安可. The change removes the forced substitution, so that remaining
choice is the model's own and is visible to review; it is not a pre-lock error,
and the owner's ruling does not accept hiding it behind per-word exceptions or a
model swap.

## Decision

The model path keeps dictionary terms as **contextual references**:

1. Every term the sentence layer selects is passed to the model as
   `source = official (category)`, with instructions to use the official term
   when the word names an in-game concept, to translate its real meaning when
   the same surface is an ordinary word, a plant or medicine, and to apply each
   meaning separately when one surface occurs with two meanings. English output
   may carry the plural or article the sentence needs (`Resonators`, `Echoes`,
   `an Echo`).
2. Translating **into English**, every selected term is a reference, because
   number and articles cannot be chosen by a fixed substitution.
   Translating **into Chinese**, only name categories (`resonator`, `speaker`)
   are references; other terms keep the hard placeholder, so ordinary official
   nouns still cannot be paraphrased.
3. A single-character resonator name the narrow lock whitelist skips
   (`欢迎椿`, `院子里的椿`) is still offered as a reference unless an ordinary
   word covers the character (`香椿`, `臭椿`, `椿树`).
4. Deterministic paths are untouched: `lock_terms`/`_lock_terms` still produce
   placeholders, `restore()` is still strict for the locks it receives, the
   no-model path still substitutes official terms locally, and exact hits
   still never call a model. Structural markup placeholders remain mandatory.

## Consequences

- A contextual choice no longer fails with `missing_placeholder`; the model's
  sentence is accepted. Structured HTML still fails closed on structural drift.
- Term fidelity on the model path is carried by the prompt and by `review-v2`
  coverage reports, not by forced substitution. A term the model paraphrases is
  visible in review, not blocked in translation.
- The API `/v1` protocol, the `translate`/`review` response shapes, whole-input
  lookup, speaker-prefix resolution, the free-text exclusion lists and
  `review-v1`/`review-v2` judgments are unchanged.
- If a later regression shows terms drifting on the model path, the response is
  a coverage signal or a narrower reference set with evidence, not a return to
  unconditional substitution; the owner's 2026-11-09 ruling rejects per-word
  exceptions as the mechanism.
