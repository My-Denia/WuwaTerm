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
   (`欢迎椿`, `院子里的椿`) is also offered as a reference unless an ordinary
   word covers the character (`香椿`, `臭椿`, `椿芽`, `春椿`, `椿木`, `椿皮`,
   `椿树`). A character that is also an everyday standalone noun keeps the
   narrow name rule: `心` (`Hsin`) is the 3.7 dictionary's other
   single-character resonator and also the ordinary word for heart, mind or
   core, so `心很重要` is neither referenced to the model nor reviewed. Review
   asks the same question before it reports a one-character finding, so the set
   the translator was given and the set review constrains cannot drift apart.
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

## Amendment: everyday standalone characters (2026-11-09)

Review asks the same single-character question as #3 before it reports a
finding; that alignment is the point of the amendment, not a new substitute.
The generalization in #3 was measured on the 3.7 official Chinese text map
before it shipped: of 13,605 `心` occurrences, 3,978 are not covered by an
ordinary word, but only 159 of those also sit in a name context. The compound
test alone would therefore have turned ordinary prose such as `心很重要` and
`她的心很痛` into resonator findings about `Hsin`, and into `心 -> Hsin` hints
the model could follow. The narrow context rule applies to `心` only; `椿`
keeps the widened reference admission this record describes. The plant
supplement covers seven surfaces (`香椿`, `臭椿`, `椿树`, `椿芽`, `春椿`,
`椿木`, `椿皮`); none of them occurs in the official 3.7 text map, so it
guards free user text, not official lines. This is a narrower reference and
reporting set with measured evidence, which the Consequences above name as the
permitted response, not a per-word exception to forced substitution.

## Candidate supplement: occurrence-scoped references (2026-11-22, `q7`)

The `q7` candidate (`feat/q7-occurrence-term-admission`) retains the decisions
above and builds occurrence-scoped references directly on the structured
`TermReference`/`TermOccurrence` model. A surface with competing official pairs
supplies alternatives to the model rather than a hard lock. Deterministic
whole-input translation prefers an established dictionary row over a derived
row (`lookup.preferred_exact_entry`), while dictionary lookup exposes every
candidate. A derived reference remains guidance to the model; it does not
create a human precedent or a verified review finding.

Single-character CJK name references require a plausible right boundary as well
as the existing ordinary-word and name-context checks. An unrecognized CJK
continuation or a middle dot on either side does not establish a standalone
name mention. Translation and `review-v2` share this admission rule, advancing
`MATCHER_REVISION` to `wuwaterm-cjk-matcher-v9`.

For Chinese-to-English requests only, `skill_references.py` derives
two-character skill stems from official `BinData/skill/skilltype.json` rows 2,
3, 5, 6, and 11 (`origin="scoped_skill_stem"`). Admission requires local action
and independent game-context evidence within the same clause of the submitted
segment, excludes spans inside known lexicon words or longer selected terms,
and requires a unique local sense marker when a stem has several meanings.
Read-only previous context never enters admission.

Each occurrence-scoped reference supplies zero-based, end-exclusive
`model_start`/`model_end` ranges in the actual model input after placeholder
replacement, validated fail-closed before any HTTP call. Skill-stem ranges also
carry their target string so two meanings of one stem keep distinct positions.
Plain single-meaning requests keep their pre-admission prompt bytes unchanged.
These rules describe the `q7` Draft PR candidate before owner-authorized
Stage-1 evaluation has been run.

### Step B1 refinement: local context snippets and paragraph-level derived gating (2026-11-22)

Offline root-cause analysis of `q5`/`q6` Stage-1 failures (`C-heart-ordinary`
2/2 failures on `心很重要`/`她的心很痛`, `G-M0439` ordinary-word contamination,
`C-derived-ordinary` on `教学楼`, and `D-003` `变奏入场` scoping) established
two general presentation and admission flaws without any per-word exception:

1. **Local context snippets replace bare numeric offsets on the model prompt**:
   Under `zh->en` `thinking: disabled`, models cannot reliably count Unicode
   scalar indices against JSON offset arrays, so placing `心 = Hsin` or
   `椿 = Camellya` in the global reference table leaked the name into ordinary
   occurrences even when `cjk_span.py` (v9) had excluded them. Occurrence-scoped
   references (`single_char_name_mention`, `scoped_skill_stem`) now stay
   coordinate-validated fail-closed via `validated_reference_ranges()`, but
   render in a separate `Scoped term references:` block using deduplicated
   single-line local snippets (`ONLY at:` / `NOT at:`, collapsing to
   `(all occurrences in this text)` when >4 occurrences are admitted) instead
   of bare integer offsets.
2. **Paragraph-level official-term context gating for derived common nouns**:
   Segment-wide (1,800-scalar) gating let an official term in one paragraph
   inject derived common-noun hints (`Area_<n>_Title` such as `教学楼`,
   `tier-base:<id>` soft casings such as `energy core` / `tuner`) into an
   unrelated ordinary-prose paragraph in the same segment. Gating both derived
   common-noun `reference_only` rows (`derived_context_gated_sources`) and
   `derived_soft_casing` rows at natural-paragraph (`\n\n`) granularity keeps
   ordinary paragraphs clean (`C-derived-ordinary`, `energy core`, `tuner`)
   while preserving in-context derived terms (`D-004` `拉贝尔学部`,
   `ordinary-en-zh` `sealed tubes`) and official version titles (`D-005`
   `未选择的梦`).

Both mechanisms strictly preserve ADR 0017's core principle: dictionary terms
remain contextual guidance to the model, never hard-coded word exceptions or
forced mechanical replacements.
