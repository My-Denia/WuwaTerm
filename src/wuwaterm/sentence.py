"""Dictionary terms for sentence translation.

Exact hits and the no-model path restore official terms deterministically. The
model path supplies the same terms as contextual references, so a word keeps
its ordinary meaning when the sentence uses it that way and an English term can
take the number and article the sentence needs.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import inspect
import logging
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import llm_transport
from . import request_trace
from .cjk_span import MATCHER_REVISION
from .constants import (
    FREE_TEXT_LOCK_EXCLUSIONS_KEY as FREE_TEXT_LOCK_EXCLUSIONS_KEY,
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY as FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY,
)
from .document_anchor import bounded_anchor_fields
from .domain_knowledge import (
    DomainKnowledgeCandidate,
    prepare_prompt_domain_knowledge_candidates,
)
from .exclusion_metadata import (
    parse_free_text_lock_exclusions as parse_free_text_lock_exclusions,
    parse_free_text_zh_lock_exclusions as parse_free_text_zh_lock_exclusions,
)
from .knowledge_runtime import KnowledgeBaseStatus, KnowledgeRuntime
from .llm_transport import (  # noqa: F401  (re-exported; same objects as llm_transport.X)
    BUDGET_EXHAUSTED_NOTICE,
    TRANSLATION_UNAVAILABLE_NOTICE,
    LLMFailureDiagnostic,
    LLMTranslationError,
    extract_llm_content as _extract_llm_content,
    has_structured_budget_signal as _has_structured_budget_signal,
    llm_error_from_response as _llm_error_from_response,
    log_llm_failure,
    require_nonblank_llm_output as _require_nonblank_llm_output,
    safe_llm_failure_diagnostic,
    structured_llm_error_text as _structured_llm_error_text,
)
from .lookup import (
    LockableSnapshot,
    TermService,
    build_lockable_snapshot,
    free_text_exclusions,
    preferred_exact_entry,
)
from .models import TermEntry
from .normalize import normalize_prose_text, normalize_text
from .protected_text import ProtectedText, ProtectedTextIntegrityError
from .semantic_review_contract import PRECEDENT_WORDING
from .structural_checks import (
    paragraph_line_counts as _paragraph_line_counts,
    regroup_lines_into_source_paragraphs as _regroup_lines_into_source_paragraphs,
)
from .skill_references import SkillReference
from .term_matching import (
    CASE_VARIANT_CATEGORIES as CASE_VARIANT_CATEGORIES,
    ReferenceCandidates,
    exact_lockable_sources as exact_lockable_sources,
    is_cjk_character,
    is_eligible_lockable,
    select_term_spans as select_term_spans,
)
from .term_preparation import (
    CONTEXTUAL_REFERENCE_CATEGORIES as CONTEXTUAL_REFERENCE_CATEGORIES,
    TermOccurrence as TermOccurrence,
    TermReference as TermReference,
    new_placeholder_prefix,
    prepare_protected_terms,
    prepare_term_segment,
)
from .translation_plan import (
    CONTEXT_RULES_INSTRUCTION,
    CONTEXTUAL_REFERENCE_INSTRUCTION,
    DEFAULT_LLM_TIMEOUT_SECONDS as DEFAULT_LLM_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_PROFILE as DEFAULT_PROVIDER_PROFILE,
    DOCUMENT_ANCHOR_INSTRUCTION,
    DOMAIN_KNOWLEDGE_HEADER,
    DOMAIN_KNOWLEDGE_INSTRUCTION,
    EN_ZH_AUTO_LOCK_LIMIT as EN_ZH_AUTO_LOCK_LIMIT,
    EN_ZH_AUTO_PARAGRAPH_LIMIT as EN_ZH_AUTO_PARAGRAPH_LIMIT,
    HTML_MODE_INSTRUCTION,
    HTML_MODE_INSTRUCTION_ZH,
    PARAGRAPH_REPAIR_INSTRUCTION_TEMPLATE,
    RETRY_BUDGET_HEADROOM_SECONDS as RETRY_BUDGET_HEADROOM_SECONDS,
    SCOPED_REFERENCE_HEADER,
    SCOPED_REFERENCE_INSTRUCTION,
    SPEAKER_LABEL_DISAMBIGUATION_INSTRUCTION,
    STRUCTURAL_RETRY_MIN_REMAINING_SECONDS as STRUCTURAL_RETRY_MIN_REMAINING_SECONDS,
    TIMEOUT_FALLBACK_MIN_REMAINING_SECONDS as TIMEOUT_FALLBACK_MIN_REMAINING_SECONDS,
    TIMEOUT_FALLBACK_TIMEOUT_SECONDS as TIMEOUT_FALLBACK_TIMEOUT_SECONDS,
    TRANSLATION_THINKING_AUTO as TRANSLATION_THINKING_AUTO,
    TRANSLATION_THINKING_EN_ZH_DEFAULT as TRANSLATION_THINKING_EN_ZH_DEFAULT,
    TRANSLATION_THINKING_EN_ZH_ENV as TRANSLATION_THINKING_EN_ZH_ENV,
    TRANSLATION_THINKING_EN_ZH_MODES as TRANSLATION_THINKING_EN_ZH_MODES,
    TRANSLATION_THINKING_MODES as TRANSLATION_THINKING_MODES,
    TRANSLATION_THINKING_REASONING_MODES as TRANSLATION_THINKING_REASONING_MODES,
    TRANSLATION_THINKING_ZH_EN_DEFAULT as TRANSLATION_THINKING_ZH_EN_DEFAULT,
    TRANSLATION_THINKING_ZH_EN_ENV as TRANSLATION_THINKING_ZH_EN_ENV,
    TRANSLATION_THINKING_ZH_EN_MODES as TRANSLATION_THINKING_ZH_EN_MODES,
    UNTRUSTED_SOURCE_INSTRUCTION,
    USER_GLOSSARY_HEADER,
    USER_GLOSSARY_INSTRUCTION,
    ZH_EN_FIDELITY_INSTRUCTION,
    AttemptKind,
    ConcreteThinkingMode,
    EffortResolution as EffortResolution,
    OperationExecutionBudget as OperationExecutionBudget,
    OperationIdentity as OperationIdentity,
    PromptTemplates as PromptTemplates,
    ProviderProfile as ProviderProfile,
    ReasoningDecision as ReasoningDecision,
    RequestIdentity as RequestIdentity,
    RequestPolicy as RequestPolicy,
    SegmentExecutionBudget as SegmentExecutionBudget,
    TermPlanningMetadata as TermPlanningMetadata,
    TranslationContextProtocol,
    TranslationPlan as TranslationPlan,
    TranslationRequestSpec as TranslationRequestSpec,
    build_paragraph_repair_request_spec as build_paragraph_repair_request_spec,
    build_timeout_fallback_request_spec as build_timeout_fallback_request_spec,
    build_translation_request_spec as build_translation_request_spec,
    canonical_json,
    coerce_term_reference as _coerce_term_reference,
    official_source_surfaces,
    redact_effective_effort,
    redact_provider_profile_name,
    render_context_instruction,
    resolve_configured_thinking_mode,
    resolve_provider_profile_from_env as resolve_provider_profile_from_env,
    resolve_request_policy_from_env,
    select_en_zh_auto_thinking as select_en_zh_auto_thinking,
    select_zh_en_auto_thinking as select_zh_en_auto_thinking,
    sha256_bytes,
)

_safe_llm_failure_diagnostic = safe_llm_failure_diagnostic


LOGGER = logging.getLogger(__name__)
# A blank line: two line breaks with only non-newline whitespace between.
_PARAGRAPH_BREAK_RE = re.compile(r"\n[^\S\n]*\n\s*")
SPEAKER_PREFIX_RE = re.compile(r"^(?P<speaker>[^:：\n]{1,40})\s*[:：]\s*(?P<body>.*)$")
DEFAULT_LLM_MAX_CONCURRENCY = 4
SYNC_TRANSLATION_RUNNING_LOOP_ERROR = (
    "Synchronous sentence translation cannot call the LLM from a running "
    "event loop; use translate_async() or translate_protected_async() instead."
)

LlmCallGuard = Callable[[], object]


def _resolve_operation_budget(
    explicit: OperationExecutionBudget | None,
) -> OperationExecutionBudget | None:
    if explicit is not None:
        return explicit
    active = llm_transport.current_execution_budget()
    return active if isinstance(active, OperationExecutionBudget) else None


@contextmanager
def _maybe_activate_budget(
    budget: OperationExecutionBudget | None,
) -> Iterator[None]:
    if budget is None or llm_transport.current_execution_budget() is budget:
        yield
        return
    with budget.activate():
        yield


_CJK_PUNCTUATION = "，。！？、；：（）《》【】“”‘’·…—"
_CJK_NEIGHBOR_CLASS = rf"[\u3400-\u9fff{_CJK_PUNCTUATION}]"

_SourceSnapshot = tuple[
    tuple[tuple[str, tuple[str, str, str]], ...],
    frozenset[str],
    frozenset[str],
    frozenset[str],
    ReferenceCandidates,
    tuple[SkillReference, ...],
]


@dataclass(frozen=True)
class LockedSentence:
    locked_text: str
    locks: tuple[tuple[str, str, str], ...]
    # Model-facing dictionary references: structured TermReference items.
    # A reference stays visible source text, never a placeholder, so the model
    # can judge the word by its own context and inflect it naturally. The
    # default keeps every existing LockedSentence call site and every
    # deterministic restore bit-identical.
    references: tuple[TermReference, ...] = ()

    def restore(self, translated: str, *, to_en: bool = True) -> str:
        result = translated
        for placeholder, zh, en in self.locks:
            actual_count = result.count(placeholder)
            if actual_count != 1:
                detail = (
                    "missing_placeholder"
                    if actual_count == 0
                    else "duplicate_placeholder"
                )
                raise LLMTranslationError(
                    TRANSLATION_UNAVAILABLE_NOTICE,
                    reason="invalid_response",
                    detail=detail,
                    expected_count=1,
                    actual_count=actual_count,
                )
            if not to_en and zh:
                # Collapse stray ASCII spaces adjacent to restored Chinese terms and CJK text.
                if is_cjk_character(zh[0]) or zh[0] in _CJK_PUNCTUATION:
                    result = re.sub(
                        rf"({_CJK_NEIGHBOR_CLASS}) " + re.escape(placeholder),
                        r"\1" + placeholder,
                        result,
                    )
                if is_cjk_character(zh[-1]) or zh[-1] in _CJK_PUNCTUATION:
                    result = re.sub(
                        re.escape(placeholder) + rf" ({_CJK_NEIGHBOR_CLASS})",
                        placeholder + r"\1",
                        result,
                    )
            result = result.replace(placeholder, en if to_en else zh)
        return result


def _protected_visible_query_text(protected: ProtectedText) -> str:
    """Concatenate visible text of ``protected`` without ever exposing raw markup."""
    parts: list[str] = []
    for segment, structure in zip(
        protected.visible_segments, protected.structures, strict=False
    ):
        if segment:
            parts.append(segment)
        if structure.visible_context:
            parts.append(structure.visible_context)
    if protected.visible_segments:
        tail = protected.visible_segments[-1]
        if tail:
            parts.append(tail)
    return "".join(parts)


def _free_text_exclusions(metadata: Mapping[str, str]) -> frozenset[str]:
    return free_text_exclusions(metadata, logger=LOGGER)


class SentenceTranslator:
    def __init__(
        self,
        db_path: str | Path,
        *,
        service: TermService | None = None,
        llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
        llm_max_concurrency: int = DEFAULT_LLM_MAX_CONCURRENCY,
        llm_transport: httpx.AsyncBaseTransport | None = None,
        en_zh_protocol: bool = False,
        provider_profile: ProviderProfile | None = None,
        knowledge_db_path: str | Path | None = None,
    ):
        self.service = TermService(db_path) if service is None else service
        self.llm_timeout_seconds = max(0.1, float(llm_timeout_seconds))
        self.llm_max_concurrency = max(1, int(llm_max_concurrency))
        self._llm_transport = llm_transport
        self._en_zh_protocol = en_zh_protocol
        self._provider_profile = (
            DEFAULT_PROVIDER_PROFILE
            if provider_profile is None
            else provider_profile
        )
        self._knowledge_db_path: Path | None = (
            Path(knowledge_db_path) if knowledge_db_path is not None else None
        )
        self._knowledge_runtime = KnowledgeRuntime(
            self._knowledge_db_path, logger=LOGGER
        )
        self._llm_slots: asyncio.Semaphore | None = None
        self._llm_slots_loop: asyncio.AbstractEventLoop | None = None
        self._llm_client: httpx.AsyncClient | None = None
        self._llm_client_loop: asyncio.AbstractEventLoop | None = None
        self._patched_lockable_cache_key: tuple[object, ...] | None = None
        self._patched_lockable_cache: LockableSnapshot | None = None

    @property
    def knowledge_db_path(self) -> Path | None:
        return self._knowledge_db_path

    @property
    def knowledge_runtime(self) -> KnowledgeRuntime:
        return self._knowledge_runtime

    def knowledge_base_status(self) -> KnowledgeBaseStatus:
        """Return live revision-verified companion ``knowledge.db`` status."""
        return self._knowledge_runtime.status(self.service)

    def knowledge_retrieval_available(self) -> bool:
        """Whether a revision-matched companion ``knowledge.db`` is ready right now."""
        return self._knowledge_runtime.status(self.service).available

    def close_knowledge_reader(self) -> None:
        """Close any cached read-only companion ``knowledge.db`` reader."""
        self._knowledge_runtime.close()

    def __del__(self) -> None:
        runtime = getattr(self, "_knowledge_runtime", None)
        if runtime is not None:
            try:
                runtime.close()
            except Exception:
                pass

    def _effective_retrieval_mode(
        self,
        context: TranslationContextProtocol | None = None,
        retrieval_mode: str | None = None,
    ) -> str:
        if retrieval_mode is not None:
            return retrieval_mode
        if context is not None:
            mode = getattr(context, "retrieval_mode", "auto")
            if isinstance(mode, str) and mode:
                return mode
        return "auto"

    def retrieve_domain_knowledge(
        self,
        prepared_text: str,
        *,
        locked: LockedSentence,
        to_chinese: bool,
        context: TranslationContextProtocol | None = None,
        retrieval_mode: str | None = None,
    ) -> tuple[DomainKnowledgeCandidate, ...]:
        """Retrieve soft read-only KB candidates when ``knowledge_db_path`` is enabled.

        Strictly returns ``()`` when disabled, explicitly turned off
        (``retrieval_mode == "off"``), or degraded; never raises to block normal
        translation and never mutates ``locked.locks``.
        """
        if self._knowledge_db_path is None or not prepared_text:
            return ()
        if self._effective_retrieval_mode(context, retrieval_mode) == "off":
            return ()
        lock_source_index = 2 if to_chinese else 1
        locked_surfaces = tuple(
            lock[lock_source_index]
            for lock in locked.locks
            if len(lock) > lock_source_index and lock[lock_source_index]
        )
        reference_surfaces = official_source_surfaces(
            locked.locks, locked.references, to_chinese=to_chinese
        )
        anchor_surfaces: tuple[str, ...] = ()
        if context is not None:
            doc_anchor = getattr(context, "document_anchor", None)
            if doc_anchor is not None:
                _topic, _heading, entities = bounded_anchor_fields(doc_anchor)
                anchor_surfaces = entities
        candidates = self._knowledge_runtime.query_candidates(
            self.service,
            prepared_text,
            locked_surfaces=locked_surfaces,
            reference_surfaces=reference_surfaces,
            anchor_surfaces=anchor_surfaces,
            use_graph=True,
        )
        if not candidates:
            return ()
        return prepare_prompt_domain_knowledge_candidates(
            prepared_text,
            candidates,
            locked_surfaces=locked_surfaces,
            reference_surfaces=reference_surfaces,
        )

    def request_policy(self, *, to_chinese: bool | None = None) -> RequestPolicy:
        """Resolve active execution policy for this translator."""
        return resolve_request_policy_from_env(
            timeout_seconds=self.llm_timeout_seconds,
            en_zh_protocol=self._en_zh_protocol,
            to_chinese=to_chinese,
            logger=LOGGER,
        )

    def dictionary_identity_digest(self) -> str:
        """Stable, path-free digest identifying the current dictionary snapshot."""
        snapshot = self.service.snapshot()
        payload: dict[str, object] = {
            "revision": snapshot.dictionary_identity.revision,
            "metadata": tuple(sorted(snapshot.metadata.items())),
        }
        return sha256_bytes(canonical_json(payload))

    def plan_translation(
        self,
        text: str,
        *,
        to_chinese: bool = False,
        context: TranslationContextProtocol | None = None,
        dictionary_identity: str | None = None,
        retrieval_mode: str | None = None,
    ) -> tuple[str | None, TranslationPlan | None]:
        """Build an immutable ``TranslationPlan`` or return a deterministic answer."""
        prepared = (
            self.prepare_text(text)
            if context is None
            else self.prepare_text_keeping_paragraphs(text)
        )
        if not prepared:
            return "", None
        exact = self.preferred_exact_entry(prepared)
        if exact is not None:
            official = exact.zh if to_chinese else exact.en
            if official:
                return official, None
        locked = self._lock_terms_with_references(
            prepared,
            to_chinese=to_chinese,
            allow_plural_inflection=to_chinese,
            allow_nfkc_spelling=True,
            single_newline_paragraphs=(context is None),
        )
        kb_candidates = self.retrieve_domain_knowledge(
            prepared,
            locked=locked,
            to_chinese=to_chinese,
            context=context,
            retrieval_mode=retrieval_mode,
        )
        plan = TranslationPlan(
            source_text=text,
            prepared_text=prepared,
            locked_text=locked.locked_text,
            locks=locked.locks,
            references=locked.references,
            to_chinese=to_chinese,
            html_mode=False,
            context=context,
            dictionary_identity=(
                self.dictionary_identity_digest()
                if dictionary_identity is None
                else dictionary_identity
            ),
            matcher_revision=MATCHER_REVISION,
            prompt_templates=current_prompt_templates(),
            domain_knowledge_candidates=kb_candidates,
        )
        return None, plan

    def build_request_spec(
        self,
        plan: TranslationPlan,
        *,
        policy: RequestPolicy | None = None,
        model: str | None = None,
        thinking_mode_override: ConcreteThinkingMode | None = None,
        prompt_suffix: str = "",
        timeout_override: float | None = None,
        attempt_kind: AttemptKind = "initial",
    ) -> TranslationRequestSpec:
        """Build the final wire request specification for ``plan``."""
        active_policy = (
            self.request_policy(to_chinese=plan.to_chinese)
            if policy is None
            else policy
        )
        return build_translation_request_spec(
            plan,
            policy=active_policy,
            provider_profile=self._provider_profile,
            model=model,
            thinking_mode_override=thinking_mode_override,
            prompt_suffix=prompt_suffix,
            timeout_override=timeout_override,
            attempt_kind=attempt_kind,
        )

    def prepare_text(self, text: str) -> str:
        text = normalize_prose_text(text)
        if not text:
            return ""
        return "\n".join(self._resolve_speaker_prefix(line) for line in text.splitlines())

    def prepare_text_keeping_paragraphs(self, text: str) -> str:
        """``prepare_text`` per paragraph, keeping one blank line between them.

        ``prepare_text`` folds blank lines away. A context-mode request
        (translation-context-v1) is one segment of a document, where a blank
        line is structure the translation should keep, so each paragraph is
        prepared on its own and the paragraphs are joined with a blank line.
        """
        unified = text.replace("\r\n", "\n").replace("\r", "\n")
        parts = (self.prepare_text(part) for part in _PARAGRAPH_BREAK_RE.split(unified))
        return "\n\n".join(part for part in parts if part)

    def lock_terms(
        self, text: str, *, allow_plural_inflection: bool = False
    ) -> LockedSentence:
        """Lock terms in prepared ``text``.

        ``allow_plural_inflection`` mirrors the translation paths' C1 opt-in
        (en→zh plural locking); direction-less callers keep the exact default.
        Plain text is prepared here, so a raw dictionary surface may also be
        compared through its pure NFKC spelling, exactly as the translation
        paths do; the selector and the builder keep their exact default.
        """
        return self._lock_terms(
            self.prepare_text(text),
            allow_plural_inflection=allow_plural_inflection,
            allow_nfkc_spelling=True,
        )

    def _lock_terms(
        self,
        text: str,
        *,
        lockable: tuple[tuple[str, tuple[str, str]], ...] | None = None,
        single_character_context: tuple[str, int] | None = None,
        allow_plural_inflection: bool = False,
        allow_nfkc_spelling: bool = False,
        placeholder_prefix: str | None = None,
        placeholder_offset: int = 0,
    ) -> LockedSentence:
        return self._lock_terms_selected(
            text,
            to_chinese=None,
            lockable=lockable,
            single_character_context=single_character_context,
            allow_plural_inflection=allow_plural_inflection,
            allow_nfkc_spelling=allow_nfkc_spelling,
            placeholder_prefix=placeholder_prefix,
            placeholder_offset=placeholder_offset,
        )

    def _lock_terms_with_references(
        self,
        text: str,
        *,
        to_chinese: bool,
        lockable: tuple[tuple[str, tuple[str, str]], ...] | None = None,
        single_character_context: tuple[str, int] | None = None,
        allow_plural_inflection: bool = False,
        allow_nfkc_spelling: bool = False,
        placeholder_prefix: str | None = None,
        placeholder_offset: int = 0,
        single_newline_paragraphs: bool = False,
    ) -> LockedSentence:
        """Lock the same spans, but hand contextual ones to the model as text.

        ``_lock_terms`` keeps its deterministic placeholder behavior for the
        no-model path and the public ``lock_terms`` API. The builder's corpus
        measurement uses the shared matcher directly with its exact default.
        Only model-facing translation paths call this, so a term whose meaning
        depends on the sentence, or whose English form has to inflect, stays
        visible instead of being replaced.

        ``allow_nfkc_spelling`` is enabled only by the plain prepared-text
        paths; protected text is not prepared and must not use the candidate.
        """
        return self._lock_terms_selected(
            text,
            to_chinese=to_chinese,
            lockable=lockable,
            single_character_context=single_character_context,
            allow_plural_inflection=allow_plural_inflection,
            allow_nfkc_spelling=allow_nfkc_spelling,
            placeholder_prefix=placeholder_prefix,
            placeholder_offset=placeholder_offset,
            single_newline_paragraphs=single_newline_paragraphs,
        )

    def _lock_terms_selected(
        self,
        text: str,
        *,
        to_chinese: bool | None,
        lockable: tuple[tuple[str, tuple[str, str]], ...] | None,
        single_character_context: tuple[str, int] | None,
        allow_plural_inflection: bool,
        allow_nfkc_spelling: bool,
        placeholder_prefix: str | None,
        placeholder_offset: int,
        single_newline_paragraphs: bool = False,
    ) -> LockedSentence:
        context_text, context_offset = single_character_context or (text, 0)
        bundle = self._lockable_bundle()
        uses_bundle_lockable = lockable is None
        active_lockable = bundle.eligible_lockable if uses_bundle_lockable else lockable
        assert active_lockable is not None
        nfkc_sources = (
            bundle.nfkc_spelling_sources if allow_nfkc_spelling else frozenset()
        )
        prepared = prepare_term_segment(
            text,
            lockable=active_lockable,
            context_text=context_text,
            context_offset=context_offset,
            categories=bundle.source_categories if to_chinese is not None else {},
            reference_only=bundle.reference_only_sources,
            to_chinese=to_chinese,
            allow_plural_inflection=allow_plural_inflection,
            nfkc_spelling_sources=nfkc_sources,
            placeholder_prefix=placeholder_prefix or new_placeholder_prefix(text),
            placeholder_offset=placeholder_offset,
            reference_candidates=(
                bundle.reference_candidates if to_chinese is not None else None
            ),
            soft_sources=(
                bundle.soft_sources
                if (to_chinese is not None and uses_bundle_lockable)
                else None
            ),
            context_gated_reference_only=(
                bundle.context_gated_reference_only_sources
                if to_chinese is not None
                else frozenset()
            ),
            single_newline_paragraphs=single_newline_paragraphs,
            skill_refs=bundle.skill_references if to_chinese is False else (),
            matcher=bundle.matcher if uses_bundle_lockable else None,
            extended_lockable=(
                bundle.extended_lockable if uses_bundle_lockable else None
            ),
            extended_matcher_factory=(
                bundle.extended_matcher if uses_bundle_lockable else None
            ),
        )
        return LockedSentence(prepared.locked_text, prepared.locks, prepared.references)

    def _lock_protected_terms(
        self,
        protected: ProtectedText,
        *,
        allow_plural_inflection: bool = False,
    ) -> LockedSentence:
        """Select terms per visible segment with the complete visible context."""
        return self._lock_protected_selected(
            protected, to_chinese=None, allow_plural_inflection=allow_plural_inflection
        )

    def _lock_protected_terms_with_references(
        self,
        protected: ProtectedText,
        *,
        to_chinese: bool,
        allow_plural_inflection: bool = False,
    ) -> LockedSentence:
        """Protected-text variant of ``_lock_terms_with_references``."""
        return self._lock_protected_selected(
            protected, to_chinese=to_chinese, allow_plural_inflection=allow_plural_inflection
        )

    def _lock_protected_selected(
        self,
        protected: ProtectedText,
        *,
        to_chinese: bool | None,
        allow_plural_inflection: bool,
    ) -> LockedSentence:
        bundle = self._lockable_bundle()
        prepared = prepare_protected_terms(
            protected,
            lockable=bundle.eligible_lockable,
            categories=bundle.source_categories if to_chinese is not None else {},
            reference_only=bundle.reference_only_sources,
            to_chinese=to_chinese,
            allow_plural_inflection=allow_plural_inflection,
            placeholder_prefix=new_placeholder_prefix(protected.for_model()),
            reference_candidates=(
                bundle.reference_candidates if to_chinese is not None else None
            ),
            soft_sources=bundle.soft_sources if to_chinese is not None else None,
            context_gated_reference_only=(
                bundle.context_gated_reference_only_sources
                if to_chinese is not None
                else frozenset()
            ),
            skill_refs=bundle.skill_references if to_chinese is False else (),
            matcher=bundle.matcher,
            extended_lockable=bundle.extended_lockable,
            extended_matcher_factory=bundle.extended_matcher,
        )
        return LockedSentence(prepared.locked_text, prepared.locks, prepared.references)

    @staticmethod
    def _restore_protected(
        protected: ProtectedText,
        locked: LockedSentence,
        translated: str,
        *,
        to_en: bool,
    ) -> ProtectedText:
        restored_terms = locked.restore(translated, to_en=to_en)
        try:
            return protected.restore(restored_terms)
        except ProtectedTextIntegrityError as exc:
            raise LLMTranslationError(
                TRANSLATION_UNAVAILABLE_NOTICE, reason="protected_text_integrity"
            ) from exc

    def translate(self, text: str, *, to_chinese: bool = False) -> str:
        """Translate plain text through the synchronous API.

        Async callers should use ``translate_async`` when LLM translation is
        configured; the sync API cannot run an LLM request inside an existing
        event loop.
        """
        prepared = self.prepare_text(text)
        if not prepared:
            return ""
        exact = self.preferred_exact_entry(prepared)
        if exact is not None:
            official = exact.zh if to_chinese else exact.en
            if official:
                return official
        # C1: plural inflection is admitted only when the target is Chinese
        # (Chinese has no plural marking, so restoring the official zh term
        # is well-formed); zh->en and en->en keep exact-surface matching.
        if not llm_configured():
            locked = self._lock_terms(
                prepared,
                allow_plural_inflection=to_chinese,
                allow_nfkc_spelling=True,
            )
            return locked.restore(locked.locked_text, to_en=not to_chinese)
        # The model gets contextual references for the same spans (see
        # _lock_terms_with_references), so a term whose meaning or English
        # word form depends on the sentence is not overwritten.
        locked = self._lock_terms_with_references(
            prepared,
            to_chinese=to_chinese,
            allow_plural_inflection=to_chinese,
            allow_nfkc_spelling=True,
            single_newline_paragraphs=True,
        )
        kb_candidates = self.retrieve_domain_knowledge(
            prepared,
            locked=locked,
            to_chinese=to_chinese,
        )
        try:
            if to_chinese:
                translated = self._call_llm_sync(
                    locked.locked_text,
                    locked.locks,
                    to_chinese=True,
                    references=locked.references,
                    domain_knowledge_candidates=kb_candidates,
                )
            else:
                translated = self._call_llm_sync(
                    locked.locked_text,
                    locked.locks,
                    references=locked.references,
                    domain_knowledge_candidates=kb_candidates,
                )
            translated = _require_nonblank_llm_output(translated)
            return locked.restore(translated, to_en=not to_chinese)
        except LLMTranslationError as exc:
            diagnostic = safe_llm_failure_diagnostic(exc)
            log_llm_failure(diagnostic)
            return exc.user_message

    async def translate_async(
        self,
        text: str,
        *,
        to_chinese: bool = False,
        before_llm_call: LlmCallGuard | None = None,
        propagate_errors: bool = False,
        context: TranslationContextProtocol | None = None,
        execution_budget: OperationExecutionBudget | None = None,
        retrieval_mode: str | None = None,
    ) -> str:
        """Translate plain text; ``context`` opts into translation-context-v1.

        ``context`` is any object with ``before`` (``source``/``target`` or
        None) and ``precedents``. ``None`` keeps the original preparation and
        prompt; a context also keeps paragraph breaks in the text sent.
        """
        prepared = (
            self.prepare_text(text)
            if context is None
            else self.prepare_text_keeping_paragraphs(text)
        )
        if not prepared:
            return ""
        exact = self.preferred_exact_entry(prepared)
        if exact is not None:
            official = exact.zh if to_chinese else exact.en
            if official:
                return official
        if not llm_configured():
            locked = self._lock_terms(
                prepared,
                allow_plural_inflection=to_chinese,
                allow_nfkc_spelling=True,
            )
            return locked.restore(locked.locked_text, to_en=not to_chinese)
        op_budget = _resolve_operation_budget(execution_budget)
        locked = self._lock_terms_with_references(
            prepared,
            to_chinese=to_chinese,
            allow_plural_inflection=to_chinese,
            allow_nfkc_spelling=True,
            single_newline_paragraphs=(context is None),
        )
        kb_candidates = self.retrieve_domain_knowledge(
            prepared,
            locked=locked,
            to_chinese=to_chinese,
            context=context,
            retrieval_mode=retrieval_mode,
        )
        policy = self.request_policy(to_chinese=to_chinese)
        # Resolved once after acquiring the concurrency slot (against the real
        # post-queue remaining budget/timeout), so the two corrective calls
        # below know the exact mode the first attempt actually ran under.
        resolved_initial_modes: list[ConcreteThinkingMode] = []
        budget = (
            op_budget.segment_budget(
                policy_max_corrective_calls=policy.max_corrective_calls
            )
            if op_budget is not None
            else SegmentExecutionBudget(
                max_corrective_calls=policy.max_corrective_calls
            )
        )
        try:
            # One segment gets at most one corrective call on top of its first
            # attempt, whichever kind it is (see ``_retry_without_thinking`` and
            # ``_repaired_paragraph_translation``). The budget is what keeps a
            # timeout fallback from chaining into a paragraph repair: after a
            # ``low`` attempt has already timed out, that repair would spend the
            # segment's last budget on a third call that walks back into the
            # exact mode that just overran, the one call least likely to answer.
            try:
                translated = await self._call_llm_async_limited(
                    locked.locked_text,
                    locked.locks,
                    to_chinese=to_chinese,
                    references=locked.references,
                    before_llm_call=before_llm_call,
                    context=context,
                    thinking_mode=None,
                    execution_budget=op_budget,
                    policy=policy,
                    on_thinking_resolved=resolved_initial_modes.append,
                    domain_knowledge_candidates=kb_candidates,
                )
            except LLMTranslationError as exc:
                initial_mode = (
                    resolved_initial_modes[0]
                    if resolved_initial_modes
                    else policy.resolve_initial_thinking_mode(
                        to_chinese=to_chinese,
                        locked_text=locked.locked_text,
                        lock_count=len(locked.locks),
                        references=locked.references,
                        context=context,
                        remaining_budget_seconds=(
                            op_budget.remaining_seconds()
                            if op_budget is not None
                            else request_trace.remaining_budget_seconds()
                        ),
                    )
                )
                # The one failure ``auto`` in either direction may retry: it
                # picked thinking on for a segment that then ran out of time.
                translated = await self._retry_without_thinking(
                    exc,
                    locked,
                    to_chinese=to_chinese,
                    context=context,
                    before_llm_call=before_llm_call,
                    thinking_mode=initial_mode,
                    policy=policy,
                    budget=budget,
                    execution_budget=op_budget,
                    domain_knowledge_candidates=kb_candidates,
                )
            thinking_mode = (
                resolved_initial_modes[0]
                if resolved_initial_modes
                else policy.resolve_initial_thinking_mode(
                    to_chinese=to_chinese,
                    locked_text=locked.locked_text,
                    lock_count=len(locked.locks),
                    references=locked.references,
                    context=context,
                    remaining_budget_seconds=(
                        op_budget.remaining_seconds()
                        if op_budget is not None
                        else request_trace.remaining_budget_seconds()
                    ),
                )
            )
            translated = _require_nonblank_llm_output(translated)
            restored = locked.restore(translated, to_en=not to_chinese)
            if context is None:
                return restored
            if policy.paragraph_regroup_enabled:
                regrouped = _regroup_lines_into_source_paragraphs(prepared, restored)
                if regrouped != restored:
                    request_trace.note_paragraph_regroup()
            else:
                regrouped = restored
            if not policy.paragraph_repair_enabled:
                return regrouped
            repaired = await self._repaired_paragraph_translation(
                locked,
                prepared,
                regrouped,
                to_chinese=to_chinese,
                context=context,
                before_llm_call=before_llm_call,
                thinking_mode=thinking_mode,
                policy=policy,
                budget=budget,
                execution_budget=op_budget,
                domain_knowledge_candidates=kb_candidates,
            )
            return regrouped if repaired is None else repaired
        except LLMTranslationError as exc:
            if propagate_errors:
                raise
            # Swallowed here by contract (caller gets the notice text), so
            # this is the only place the failure reason can reach the logs.
            log_llm_failure(safe_llm_failure_diagnostic(exc))
            return exc.user_message

    async def _retry_without_thinking(
        self,
        failure: LLMTranslationError,
        locked: LockedSentence,
        *,
        to_chinese: bool,
        context: TranslationContextProtocol | None,
        before_llm_call: LlmCallGuard | None,
        thinking_mode: ConcreteThinkingMode,
        policy: RequestPolicy | None = None,
        budget: SegmentExecutionBudget | None = None,
        execution_budget: OperationExecutionBudget | None = None,
        domain_knowledge_candidates: Sequence[DomainKnowledgeCandidate] = (),
    ) -> str:
        """One faster retry of a timed-out ``auto`` call that chose to think.

        The case this answers: a request whose mode came from ``auto`` in that
        request's direction, which chose to think, and which then ran out of the
        model timeout. Thinking is the part that overran (see
        ``EN_ZH_AUTO_LOCK_LIMIT`` and ``ZH_EN_AUTO_SCALAR_LIMIT``), so the retry
        keeps the prompt, the references and the locks byte-for-byte and changes
        two things only — thinking off, and a bound short enough that the request
        still has time to answer with the result. Measured into Chinese: the two
        shapes ``auto`` sends with thinking off answered in 3.3 s.

        Every other outcome re-raises the original failure unchanged: a different
        reason, a mode an operator named explicitly (never second-guessed), a
        direction that did not opt into ``auto``, no mode that was reasoning to
        begin with, too little budget left, a guard that refuses the second call,
        or a retry that fails on its own. A request that already timed out cannot
        be made worse by trying, and it is not made slower than the budget it has
        left.

        This call is the segment's one corrective call: the budget consumes it
        and skips the paragraph repair, so a segment whose ``low`` attempt timed
        out never spends a third call stepping back up into ``low``.
        """
        op_budget = _resolve_operation_budget(execution_budget)
        if op_budget is not None and op_budget.is_cancelled:
            request_trace.note_cancelled()
            op_budget.check_can_continue()
        active_policy = (
            self.request_policy(to_chinese=to_chinese)
            if policy is None
            else policy
        )
        if not active_policy.allows_timeout_fallback(
            to_chinese=to_chinese,
            initial_thinking_mode=thinking_mode,
            provider_profile=self._provider_profile,
        ):
            raise failure
        if not _is_timeout_failure(failure):
            raise failure
        if budget is not None and not budget.can_use_corrective_call:
            request_trace.note_budget_exhausted()
            raise failure
        remaining = (
            budget.remaining_seconds()
            if budget is not None
            else request_trace.remaining_budget_seconds()
        )
        timeout_seconds = active_policy.timeout_fallback_seconds(remaining)
        if timeout_seconds is None:
            raise failure
        if budget is not None:
            budget.consume_corrective_call()
        try:
            result = await self._call_llm_async_limited(
                locked.locked_text,
                locked.locks,
                to_chinese=to_chinese,
                references=locked.references,
                # The guard runs inside the slot, so a refused retry spends no
                # model call and the caller still gets the first attempt's
                # diagnostic.
                before_llm_call=before_llm_call,
                context=context,
                thinking_mode="disabled",
                timeout_seconds=timeout_seconds,
                attempt_kind="timeout_fallback",
                execution_budget=op_budget,
                domain_knowledge_candidates=domain_knowledge_candidates,
            )
            request_trace.note_timeout_fallback(succeeded=True)
            return result
        except LLMTranslationError as exc:
            if exc.reason == "cancelled":
                raise
            request_trace.note_timeout_fallback(succeeded=False)
            raise failure from exc

    async def _repaired_paragraph_translation(
        self,
        locked: LockedSentence,
        prepared: str,
        regrouped: str,
        *,
        to_chinese: bool,
        context: TranslationContextProtocol | None,
        before_llm_call: LlmCallGuard | None,
        thinking_mode: ConcreteThinkingMode,
        policy: RequestPolicy | None = None,
        budget: SegmentExecutionBudget | None = None,
        execution_budget: OperationExecutionBudget | None = None,
        domain_knowledge_candidates: Sequence[DomainKnowledgeCandidate] = (),
    ) -> str | None:
        """One retry that asks for the source's paragraph count, or ``None``.

        ``_regroup_lines_into_source_paragraphs`` can only move blank lines; it
        cannot put back a paragraph the model merged away, because the answer's
        lines no longer answer the source's lines. A title folded into the first
        body paragraph is that case (a two-paragraph source answered as one), and
        the only repair is to say the count out loud. So a context-mode request
        whose answer lost or gained a paragraph gets exactly one more call: the
        same prompt, the same references, the same locks, plus one sentence
        naming the count. Everything else is untouched — a matching count, a
        one-paragraph source (no structure to restore), a request with too little
        budget left, a guard that refuses the call, an answer that still has the
        wrong count, or any failure on the retry.

        Returns the retry's translation when it arrives with the source's
        paragraph count, and ``None`` in every other case, which leaves the first
        attempt's translation in place: a successful first call is never turned
        into a failure by the repair that was meant to improve it.

        This is a segment's one corrective call, and it runs at most once: the
        caller reaches it only when the first attempt answered and no timeout
        fallback was spent (see ``translate_async``).
        """
        op_budget = _resolve_operation_budget(execution_budget)
        if op_budget is not None and op_budget.is_cancelled:
            request_trace.note_cancelled()
            op_budget.check_can_continue()
        active_policy = (
            self.request_policy(to_chinese=to_chinese)
            if policy is None
            else policy
        )
        if not active_policy.paragraph_repair_enabled:
            return None
        source_layout = _paragraph_line_counts(prepared)
        if len(source_layout) < 2:
            return None
        if len(_paragraph_line_counts(regrouped)) == len(source_layout):
            return None
        if budget is not None and not budget.can_use_corrective_call:
            request_trace.note_budget_exhausted()
            return None
        remaining = (
            budget.remaining_seconds()
            if budget is not None
            else request_trace.remaining_budget_seconds()
        )
        allowed, timeout_seconds = active_policy.paragraph_repair_timeout_seconds(
            remaining
        )
        if not allowed:
            return None
        paragraphs = len(source_layout)
        constraint = current_prompt_templates().format_paragraph_repair_instruction(
            paragraphs
        )
        # Resolve the paragraph-repair mode inside the concurrency slot against
        # the repair attempt's actual remaining timeout window, applying both
        # the auto ceiling and timeout budget down-shift consistently.
        def _resolve_repair_mode(effective_window: float) -> ConcreteThinkingMode:
            return active_policy.paragraph_repair_thinking_mode(
                to_chinese=to_chinese,
                initial_thinking_mode=thinking_mode,
                lock_count=len(locked.locks),
                provider_profile=self._provider_profile,
                prepared_scalars=len(locked.locked_text),
                remaining_budget_seconds=effective_window,
            )

        if budget is not None:
            budget.consume_corrective_call()
        try:
            retried = await self._call_llm_async_limited(
                locked.locked_text,
                locked.locks,
                to_chinese=to_chinese,
                references=locked.references,
                before_llm_call=before_llm_call,
                context=context,
                thinking_mode=None,
                prompt_suffix=constraint,
                timeout_seconds=timeout_seconds,
                attempt_kind="paragraph_repair",
                execution_budget=op_budget,
                thinking_mode_resolver=_resolve_repair_mode,
                domain_knowledge_candidates=domain_knowledge_candidates,
            )
            retried = _require_nonblank_llm_output(retried)
            restored = locked.restore(retried, to_en=not to_chinese)
        except LLMTranslationError as exc:
            if exc.reason == "cancelled":
                raise
            request_trace.note_paragraph_repair(succeeded=False)
            return None
        if active_policy.paragraph_regroup_enabled:
            retry_regrouped = _regroup_lines_into_source_paragraphs(prepared, restored)
            if retry_regrouped != restored:
                request_trace.note_paragraph_regroup()
        else:
            retry_regrouped = restored
        if len(_paragraph_line_counts(retry_regrouped)) != len(source_layout):
            request_trace.note_paragraph_repair(succeeded=False)
            return None
        request_trace.note_paragraph_repair(succeeded=True)
        return retry_regrouped

    def translate_protected(
        self,
        protected: ProtectedText,
        *,
        to_chinese: bool = False,
        retrieval_mode: str | None = None,
    ) -> ProtectedText:
        """Translate visible segments, preserving opaque originals and context.

        As with the historic markup path, failures propagate and this skips
        plain-text preparation; the adapter decides how to handle failures.
        """
        locked = self._lock_protected_terms_with_references(
            protected, to_chinese=to_chinese, allow_plural_inflection=to_chinese
        )
        visible_text = _protected_visible_query_text(protected)
        kb_candidates = self.retrieve_domain_knowledge(
            visible_text,
            locked=locked,
            to_chinese=to_chinese,
            retrieval_mode=retrieval_mode,
        )
        translated = self._call_llm_sync(
            locked.locked_text,
            locked.locks,
            html_mode=True,
            to_chinese=to_chinese,
            references=locked.references,
            domain_knowledge_candidates=kb_candidates,
            kb_source_text=visible_text if kb_candidates else None,
        )
        return self._restore_protected(
            protected, locked, _require_nonblank_llm_output(translated),
            to_en=not to_chinese,
        )

    async def translate_protected_async(
        self,
        protected: ProtectedText,
        *,
        to_chinese: bool = False,
        context: TranslationContextProtocol | None = None,
        before_llm_call: LlmCallGuard | None = None,
        execution_budget: OperationExecutionBudget | None = None,
        retrieval_mode: str | None = None,
    ) -> ProtectedText:
        """Async protected translation, with the guard inside the shared slot."""
        op_budget = _resolve_operation_budget(execution_budget)
        locked = self._lock_protected_terms_with_references(
            protected, to_chinese=to_chinese, allow_plural_inflection=to_chinese
        )
        visible_text = _protected_visible_query_text(protected)
        kb_candidates = self.retrieve_domain_knowledge(
            visible_text,
            locked=locked,
            to_chinese=to_chinese,
            context=context,
            retrieval_mode=retrieval_mode,
        )
        has_kb = bool(kb_candidates) or bool(
            context is not None and getattr(context, "domain_knowledge_candidates", ())
        )
        policy = self.request_policy(to_chinese=to_chinese)
        translated = await self._call_llm_async_limited(
            locked.locked_text,
            locked.locks,
            html_mode=True,
            to_chinese=to_chinese,
            references=locked.references,
            context=context,
            before_llm_call=before_llm_call,
            execution_budget=op_budget,
            policy=policy,
            domain_knowledge_candidates=kb_candidates,
            kb_source_text=visible_text if has_kb else None,
        )
        return self._restore_protected(
            protected, locked, _require_nonblank_llm_output(translated),
            to_en=not to_chinese,
        )

    async def _call_llm_async_limited(
        self,
        locked_text: str,
        locks: tuple[tuple[str, str, str], ...],
        html_mode: bool = False,
        to_chinese: bool = False,
        references: Sequence[TermReference] = (),
        before_llm_call: LlmCallGuard | None = None,
        context: TranslationContextProtocol | None = None,
        thinking_mode: ConcreteThinkingMode | None = None,
        prompt_suffix: str = "",
        timeout_seconds: float | None = None,
        attempt_kind: AttemptKind = "initial",
        execution_budget: OperationExecutionBudget | None = None,
        policy: RequestPolicy | None = None,
        on_thinking_resolved: Callable[[ConcreteThinkingMode], None] | None = None,
        thinking_mode_resolver: Callable[[float], ConcreteThinkingMode] | None = None,
        domain_knowledge_candidates: Sequence[DomainKnowledgeCandidate] = (),
        kb_source_text: str | None = None,
    ) -> str:
        """One model call under the shared slot and the caller's guard.

        ``thinking_mode`` and ``timeout_seconds`` are the two knobs a corrective
        call needs: the retry that turns thinking off, and the retry that has to
        fit inside what is left of the request's budget. ``None`` keeps what the
        environment and the translator's own timeout say, which is every ordinary
        call. ``prompt_suffix`` appends one extra instruction to the system
        prompt; empty keeps the prompt byte-for-byte what it was.
        """
        op_budget = _resolve_operation_budget(execution_budget)
        if op_budget is not None:
            op_budget.check_can_continue()
        # Recorded before the queue, not after it: a request refused at the time
        # budget while still waiting for a slot should still say which pair it
        # was translating. No-op outside an HTTP request (bot, channel, CLI).
        request_trace.set_direction(
            request_trace.DIRECTION_EN_TO_ZH
            if to_chinese
            else request_trace.DIRECTION_ZH_TO_EN
        )
        async with request_trace.llm_slot(self._current_llm_slots()):
            if op_budget is not None:
                op_budget.check_can_continue()
            client = await self._current_llm_client()
            if before_llm_call is not None:
                guarded = before_llm_call()
                if inspect.isawaitable(guarded):
                    await guarded
            attempt_timeout = (
                self.llm_timeout_seconds
                if timeout_seconds is None
                else timeout_seconds
            )
            if op_budget is not None:
                own_rem = op_budget.own_remaining_seconds()
                if own_rem is not None:
                    attempt_timeout = min(attempt_timeout, max(0.05, own_rem))
            remaining_budget = (
                op_budget.remaining_seconds()
                if op_budget is not None
                else request_trace.remaining_budget_seconds()
            )
            effective_window = (
                attempt_timeout
                if remaining_budget is None
                else min(attempt_timeout, remaining_budget)
            )
            resolved_thinking_mode = thinking_mode
            if resolved_thinking_mode is None and thinking_mode_resolver is not None:
                resolved_thinking_mode = thinking_mode_resolver(effective_window)
                effort_resolution = self._provider_profile.resolve_effort(
                    resolved_thinking_mode
                )
                request_trace.note_effort_resolution(
                    requested_effort=effort_resolution.requested_effort,
                    effective_effort=redact_effective_effort(
                        effort_resolution.effective_effort
                    ),
                    provider_profile=redact_provider_profile_name(
                        effort_resolution.provider_profile
                    ),
                )
                if on_thinking_resolved is not None:
                    on_thinking_resolved(resolved_thinking_mode)
            elif resolved_thinking_mode is None and policy is not None:
                resolved_thinking_mode = policy.resolve_initial_thinking_mode(
                    to_chinese=to_chinese,
                    locked_text=locked_text,
                    lock_count=len(locks),
                    references=references,
                    context=context,
                    remaining_budget_seconds=effective_window,
                )
                effort_resolution = self._provider_profile.resolve_effort(
                    resolved_thinking_mode
                )
                request_trace.note_thinking(
                    policy.configured_mode(to_chinese=to_chinese),
                    resolved_thinking_mode,
                )
                request_trace.note_effort_resolution(
                    requested_effort=effort_resolution.requested_effort,
                    effective_effort=redact_effective_effort(
                        effort_resolution.effective_effort
                    ),
                    provider_profile=redact_provider_profile_name(
                        effort_resolution.provider_profile
                    ),
                )
                if on_thinking_resolved is not None:
                    on_thinking_resolved(resolved_thinking_mode)
            extra_kb_kwargs: dict[str, Any] = {}
            if domain_knowledge_candidates:
                extra_kb_kwargs["domain_knowledge_candidates"] = tuple(
                    domain_knowledge_candidates
                )
            if kb_source_text is not None:
                extra_kb_kwargs["kb_source_text"] = kb_source_text
            if op_budget is None:
                return await _call_llm_async(
                    locked_text,
                    locks,
                    html_mode=html_mode,
                    to_chinese=to_chinese,
                    timeout_seconds=attempt_timeout,
                    client=client,
                    en_zh_protocol=self._en_zh_protocol,
                    references=references,
                    context=context,
                    thinking_mode=resolved_thinking_mode,
                    prompt_suffix=prompt_suffix,
                    provider_profile=self._provider_profile,
                    attempt_kind=attempt_kind,
                    **extra_kb_kwargs,
                )
            with _maybe_activate_budget(op_budget), op_budget.call_attempt():
                return await _call_llm_async(
                    locked_text,
                    locks,
                    html_mode=html_mode,
                    to_chinese=to_chinese,
                    timeout_seconds=attempt_timeout,
                    client=client,
                    en_zh_protocol=self._en_zh_protocol,
                    references=references,
                    context=context,
                    thinking_mode=resolved_thinking_mode,
                    prompt_suffix=prompt_suffix,
                    provider_profile=self._provider_profile,
                    attempt_kind=attempt_kind,
                    **extra_kb_kwargs,
                )

    async def complete_chat_async(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        max_tokens: int | None = None,
        response_format: dict[str, str] | None = None,
        model_env: str | None = None,
        before_llm_call: LlmCallGuard | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        execution_budget: OperationExecutionBudget | None = None,
    ) -> str:
        """Run one chat completion under the shared LLM slot and client.

        ``timeout_seconds`` replaces the translator's own timeout for this
        request only.
        """
        op_budget = _resolve_operation_budget(execution_budget)
        if op_budget is not None:
            op_budget.check_can_continue()
        async with request_trace.llm_slot(self._current_llm_slots()):
            if op_budget is not None:
                op_budget.check_can_continue()
            client = await self._current_llm_client()
            if before_llm_call is not None:
                guarded = before_llm_call()
                if inspect.isawaitable(guarded):
                    await guarded
            if op_budget is None:
                return await llm_transport.post_chat_completion(
                    client,
                    messages,
                    purpose=purpose,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    model_env=model_env,
                    extra_body=extra_body,
                    timeout_seconds=timeout_seconds,
                )
            effective_timeout = timeout_seconds
            own_rem = op_budget.own_remaining_seconds()
            if own_rem is not None:
                base = (
                    self.llm_timeout_seconds
                    if timeout_seconds is None
                    else timeout_seconds
                )
                effective_timeout = min(base, max(0.05, own_rem))
            with _maybe_activate_budget(op_budget), op_budget.call_attempt():
                return await llm_transport.post_chat_completion(
                    client,
                    messages,
                    purpose=purpose,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    model_env=model_env,
                    extra_body=extra_body,
                    timeout_seconds=effective_timeout,
                )

    def _call_llm_sync(
        self,
        locked_text: str,
        locks: tuple[tuple[str, str, str], ...],
        html_mode: bool = False,
        to_chinese: bool = False,
        references: Sequence[TermReference] = (),
        domain_knowledge_candidates: Sequence[DomainKnowledgeCandidate] = (),
        kb_source_text: str | None = None,
    ) -> str:
        extra_kb_kwargs: dict[str, Any] = {}
        if domain_knowledge_candidates:
            extra_kb_kwargs["domain_knowledge_candidates"] = tuple(
                domain_knowledge_candidates
            )
        if kb_source_text is not None:
            extra_kb_kwargs["kb_source_text"] = kb_source_text
        return _call_llm(
            locked_text,
            locks,
            html_mode=html_mode,
            to_chinese=to_chinese,
            timeout_seconds=self.llm_timeout_seconds,
            en_zh_protocol=self._en_zh_protocol,
            references=references,
            provider_profile=self._provider_profile,
            transport=self._llm_transport,
            **extra_kb_kwargs,
        )

    def _current_llm_slots(self) -> asyncio.Semaphore:
        # Tests may create a translator once and drive it through multiple
        # asyncio.run loops; bind the semaphore lazily to the active loop.
        loop = asyncio.get_running_loop()
        if self._llm_slots is None or self._llm_slots_loop is not loop:
            self._llm_slots = asyncio.Semaphore(self.llm_max_concurrency)
            self._llm_slots_loop = loop
        return self._llm_slots

    async def _current_llm_client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if (
            self._llm_client is not None
            and self._llm_client_loop is loop
            and not self._llm_client.is_closed
        ):
            return self._llm_client
        await self._close_llm_client()
        self._llm_client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.llm_timeout_seconds),
            transport=self._llm_transport,
        )
        self._llm_client_loop = loop
        return self._llm_client

    async def _close_llm_client(self) -> None:
        client = self._llm_client
        self._llm_client = None
        self._llm_client_loop = None
        if client is None or client.is_closed:
            return
        await client.aclose()

    async def aclose(self) -> None:
        self.close_knowledge_reader()
        await self._close_llm_client()

    def _resolve_speaker_prefix(self, line: str) -> str:
        match = SPEAKER_PREFIX_RE.match(line)
        if not match:
            return line
        speaker = match.group("speaker").strip()
        body = match.group("body").strip()
        official = self._exact_official(speaker)
        if not official:
            return line
        # A line that starts with a derived name's Chinese surface ("教学楼：")
        # is not rewritten to the name either.
        norm = normalize_text(speaker)
        if any(normalize_text(source) == norm for source in self._reference_only_sources()):
            return line
        return f"{official}: {body}" if body else f"{official}:"

    def _exact_official(self, query: str) -> str | None:
        entry = self.preferred_exact_entry(query)
        if entry is None:
            return None
        official = entry.en
        if not official or "\n" in official:
            return None
        return official

    def preferred_exact_entry(
        self, query: str, *, service: TermService | None = None
    ) -> TermEntry | None:
        """Return the exact hit a fixed substitution may restore, if any."""
        return preferred_exact_entry(
            self.service if service is None else service, query
        )

    def _eligible_lockable_sources(self) -> tuple[tuple[str, tuple[str, str]], ...]:
        return self._lockable_bundle().eligible_lockable

    @staticmethod
    def _eligible_rows(
        rows: tuple[tuple[str, tuple[str, str, str]], ...],
    ) -> tuple[tuple[str, tuple[str, str]], ...]:
        return tuple(
            (source, (official[0], official[1]))
            for source, official in rows
            if is_eligible_lockable(source, official)
        )

    def _eligible_lockable_snapshot(
        self,
    ) -> tuple[
        tuple[tuple[str, tuple[str, str]], ...], frozenset[str]
    ]:
        bundle = self._lockable_bundle()
        return bundle.eligible_lockable, bundle.nfkc_spelling_sources

    def _lockable_sources_identity(self) -> tuple[object, ...]:
        provenance = tuple(sorted(self.service.metadata().items()))
        return (self.service._identity(), provenance)

    def _read_lockable_sources(self) -> _SourceSnapshot:
        return build_lockable_snapshot(
            self.service.metadata(), self.service.entries(), logger=LOGGER
        ).as_source_tuple()

    def _source_categories(self) -> Mapping[str, str]:
        """Surface -> category, backed by the unified immutable snapshot."""
        return self._lockable_bundle().source_categories

    def _lockable_sources(self) -> tuple[tuple[str, tuple[str, str, str]], ...]:
        return self._lockable_bundle().rows

    def _nfkc_spelling_sources(self) -> frozenset[str]:
        """Raw surfaces the prepared plain-text paths may also match by NFKC."""
        return self._lockable_bundle().nfkc_spelling_sources

    def _reference_only_sources(self) -> frozenset[str]:
        """Surfaces that are references, never locks (derived names/titles)."""
        return self._lockable_bundle().reference_only_sources

    def _context_gated_reference_only_sources(self) -> frozenset[str]:
        """Derived common-noun surfaces gated by paragraph-level official context."""
        return self._lockable_bundle().context_gated_reference_only_sources

    def _reference_candidates(self) -> ReferenceCandidates:
        """Surface -> every official pair the model path may offer for it."""
        return self._lockable_bundle().reference_candidates

    def _skill_references(self) -> tuple[SkillReference, ...]:
        """Bounded closed-class 2-char skill stems for zh->en references."""
        return self._lockable_bundle().skill_references

    def _lockable_bundle(self) -> LockableSnapshot:
        # In normal execution, share the unified immutable DictionarySnapshot
        # owned by TermService (which tracks SQLite WAL data_version and atomic
        # file replacement). When tests monkeypatch service.entries/metadata on
        # the instance, honor those overrides via an identity-keyed bundle cache.
        service_dict = getattr(self.service, "__dict__", {})
        if "entries" not in service_dict and "metadata" not in service_dict:
            return self.service.snapshot().lockable_snapshot(logger=LOGGER)
        for _attempt in range(2):
            identity_before = self._lockable_sources_identity()
            if (
                self._patched_lockable_cache is not None
                and identity_before == self._patched_lockable_cache_key
            ):
                return self._patched_lockable_cache
            bundle = build_lockable_snapshot(
                self.service.metadata(), self.service.entries(), logger=LOGGER
            )
            identity_after = self._lockable_sources_identity()
            if identity_before == identity_after:
                self._patched_lockable_cache_key = identity_after
                self._patched_lockable_cache = bundle
                return bundle
        return build_lockable_snapshot(
            self.service.metadata(), self.service.entries(), logger=LOGGER
        )

    def _lockable_snapshot(self) -> _SourceSnapshot:
        return self._lockable_bundle().as_source_tuple()


def llm_configured() -> bool:
    return llm_transport.llm_configured()


_llm_configured = llm_configured


_HTML_MODE_INSTRUCTION = HTML_MODE_INSTRUCTION
_HTML_MODE_INSTRUCTION_ZH = HTML_MODE_INSTRUCTION_ZH
_UNTRUSTED_SOURCE_INSTRUCTION = UNTRUSTED_SOURCE_INSTRUCTION
_CONTEXTUAL_REFERENCE_INSTRUCTION = CONTEXTUAL_REFERENCE_INSTRUCTION
# Appended only when references need scope: an admitted one-character name
# mention, one surface carrying competing official pairs, a soft-cased derived
# spelling, or a bounded local skill stem. Plain single-meaning requests keep
# the exact prompt bytes they carried before this mechanism existed.
_SCOPED_REFERENCE_INSTRUCTION = SCOPED_REFERENCE_INSTRUCTION
_SCOPED_REFERENCE_HEADER = SCOPED_REFERENCE_HEADER
# Appended only when one source surface reaches the model carrying a (speaker)
# entry and at least one other kind of entry: an English source such as "Tuner"
# is both the dialogue speaker 调音师 and the gameplay item 调谐器, and nothing in
# the reference lines says which one a given occurrence is. Requests without
# that competition keep their exact prompt bytes.
_SPEAKER_LABEL_DISAMBIGUATION_INSTRUCTION = SPEAKER_LABEL_DISAMBIGUATION_INSTRUCTION

# translation-context-v1 (ADR 0019): added only when a request opts in, so a
# request without context keeps the original prompt byte for byte.
_CONTEXT_RULES_INSTRUCTION = CONTEXT_RULES_INSTRUCTION

# Chinese into English only, appended after the rules above. A Chinese qualifier
# (以上, 超过, 至少, 约) or a line that trails off is meaning the English has to
# carry: rendered as a bare number, or completed into a sentence, the answer has
# silently changed the claim. Into Chinese the prompt stays byte-for-byte what it
# was — the Chinese side of that pair (3+1, S0R1, a bare bound) is already the
# numerics rule above, and nothing measured there asks for more.
_ZH_EN_FIDELITY_INSTRUCTION = ZH_EN_FIDELITY_INSTRUCTION
_PARAGRAPH_REPAIR_INSTRUCTION_TEMPLATE = PARAGRAPH_REPAIR_INSTRUCTION_TEMPLATE

# ADR 0021 opt-in context blocks: a bounded document anchor and bounded
# `user_glossary` preference references. Both render only when the request
# carries them, so a context with neither keeps the bytes it had before they
# existed.
_DOCUMENT_ANCHOR_INSTRUCTION = DOCUMENT_ANCHOR_INSTRUCTION
_USER_GLOSSARY_INSTRUCTION = USER_GLOSSARY_INSTRUCTION
_USER_GLOSSARY_HEADER = USER_GLOSSARY_HEADER
_DOMAIN_KNOWLEDGE_INSTRUCTION = DOMAIN_KNOWLEDGE_INSTRUCTION
_DOMAIN_KNOWLEDGE_HEADER = DOMAIN_KNOWLEDGE_HEADER


def current_prompt_templates() -> PromptTemplates:
    """Return active prompt templates, honoring monkeypatched module constants."""
    return PromptTemplates(
        html_mode_en=_HTML_MODE_INSTRUCTION,
        html_mode_zh=_HTML_MODE_INSTRUCTION_ZH,
        untrusted_source=_UNTRUSTED_SOURCE_INSTRUCTION,
        contextual_reference=_CONTEXTUAL_REFERENCE_INSTRUCTION,
        scoped_reference=_SCOPED_REFERENCE_INSTRUCTION,
        scoped_reference_header=_SCOPED_REFERENCE_HEADER,
        speaker_label_disambiguation=_SPEAKER_LABEL_DISAMBIGUATION_INSTRUCTION,
        context_rules=_CONTEXT_RULES_INSTRUCTION,
        zh_en_fidelity=_ZH_EN_FIDELITY_INSTRUCTION,
        precedent_wording=PRECEDENT_WORDING,
        paragraph_repair_template=_PARAGRAPH_REPAIR_INSTRUCTION_TEMPLATE,
        document_background=_DOCUMENT_ANCHOR_INSTRUCTION,
        user_glossary=_USER_GLOSSARY_INSTRUCTION,
        user_glossary_header=_USER_GLOSSARY_HEADER,
        domain_knowledge=_DOMAIN_KNOWLEDGE_INSTRUCTION,
        domain_knowledge_header=_DOMAIN_KNOWLEDGE_HEADER,
    )


def _context_instruction(
    context: TranslationContextProtocol, *, to_chinese: bool
) -> str:
    """Render a TranslationContext-like object; texts go in as JSON strings."""
    return render_context_instruction(
        context, to_chinese=to_chinese, templates=current_prompt_templates()
    )


def _ensure_sync_llm_outside_running_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(SYNC_TRANSLATION_RUNNING_LOOP_ERROR)


def _call_llm(
    locked_text: str,
    locks: tuple[tuple[str, str, str], ...],
    html_mode: bool = False,
    to_chinese: bool = False,
    timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    en_zh_protocol: bool = False,
    references: Sequence[TermReference] = (),
    provider_profile: ProviderProfile = DEFAULT_PROVIDER_PROFILE,
    transport: httpx.AsyncBaseTransport | None = None,
    domain_knowledge_candidates: Sequence[DomainKnowledgeCandidate] = (),
    kb_source_text: str | None = None,
) -> str:
    _ensure_sync_llm_outside_running_loop()
    extra_kb_kwargs: dict[str, Any] = {}
    if domain_knowledge_candidates:
        extra_kb_kwargs["domain_knowledge_candidates"] = tuple(
            domain_knowledge_candidates
        )
    if kb_source_text is not None:
        extra_kb_kwargs["kb_source_text"] = kb_source_text
    return asyncio.run(
        _call_llm_async(
            locked_text,
            locks,
            html_mode=html_mode,
            to_chinese=to_chinese,
            timeout_seconds=timeout_seconds,
            transport=transport,
            en_zh_protocol=en_zh_protocol,
            references=references,
            provider_profile=provider_profile,
            **extra_kb_kwargs,
        )
    )


async def _call_llm_async(
    locked_text: str,
    locks: tuple[tuple[str, str, str], ...],
    html_mode: bool = False,
    to_chinese: bool = False,
    timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
    client: httpx.AsyncClient | None = None,
    en_zh_protocol: bool = False,
    references: Sequence[TermReference] = (),
    context: TranslationContextProtocol | None = None,
    thinking_mode: ConcreteThinkingMode | None = None,
    prompt_suffix: str = "",
    provider_profile: ProviderProfile = DEFAULT_PROVIDER_PROFILE,
    attempt_kind: AttemptKind = "initial",
    domain_knowledge_candidates: Sequence[DomainKnowledgeCandidate] = (),
    kb_source_text: str | None = None,
) -> str:
    if client is not None:
        # The shared client already carries the translator's own timeout, so the
        # per-request override is sent only when this attempt's bound differs
        # from it: an ordinary POST then keeps the exact call it always made,
        # and a corrective call can still be shorter than the client's bound.
        attempt_timeout = (
            None if timeout_seconds == client.timeout.read else timeout_seconds
        )
        return await _call_llm_async_with_client(
            client,
            locked_text,
            locks,
            html_mode=html_mode,
            to_chinese=to_chinese,
            references=references,
            en_zh_protocol=en_zh_protocol,
            context=context,
            timeout_seconds=attempt_timeout,
            thinking_mode=thinking_mode,
            prompt_suffix=prompt_suffix,
            provider_profile=provider_profile,
            attempt_kind=attempt_kind,
            domain_knowledge_candidates=domain_knowledge_candidates,
            kb_source_text=kb_source_text,
        )
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds), transport=transport
    ) as scoped_client:
        return await _call_llm_async_with_client(
            scoped_client,
            locked_text,
            locks,
            html_mode=html_mode,
            to_chinese=to_chinese,
            references=references,
            en_zh_protocol=en_zh_protocol,
            context=context,
            # This client already holds exactly this timeout, so the request
            # keeps the fields it always sent.
            thinking_mode=thinking_mode,
            prompt_suffix=prompt_suffix,
            provider_profile=provider_profile,
            attempt_kind=attempt_kind,
            domain_knowledge_candidates=domain_knowledge_candidates,
            kb_source_text=kb_source_text,
        )


def _is_timeout_failure(failure: LLMTranslationError) -> bool:
    """Return whether ``failure`` has an exact ``\"timeout\"`` reason without magic calls."""
    try:
        raw_reason = getattr(failure, "reason", None)
    except Exception:
        return False
    return type(raw_reason) is str and raw_reason == "timeout"


def _translation_thinking_mode(to_chinese: bool) -> str:
    """The thinking mode one translation request is configured to run under."""
    return resolve_configured_thinking_mode(to_chinese=to_chinese, logger=LOGGER)


def _resolved_translation_thinking_mode(
    to_chinese: bool, prepared_text: str, *, lock_count: int
) -> ConcreteThinkingMode:
    """The mode one request actually sends, ``auto`` resolved if it says so."""
    policy = resolve_request_policy_from_env(
        to_chinese=to_chinese, logger=LOGGER
    )
    return policy.resolve_initial_thinking_mode(
        to_chinese=to_chinese,
        locked_text=prepared_text,
        lock_count=lock_count,
    )


def _translation_thinking_body(
    mode: ConcreteThinkingMode,
    *,
    provider_profile: ProviderProfile = DEFAULT_PROVIDER_PROFILE,
) -> dict[str, Any]:
    """Provider reasoning fields for a translation request under ``mode``."""
    return provider_profile.reasoning_extra_body(mode)


async def _call_llm_async_with_client(
    client: httpx.AsyncClient,
    locked_text: str,
    locks: tuple[tuple[str, str, str], ...],
    html_mode: bool = False,
    to_chinese: bool = False,
    en_zh_protocol: bool = False,
    references: Sequence[TermReference | tuple[str, str, str, str]] = (),
    context: TranslationContextProtocol | None = None,
    timeout_seconds: float | None = None,
    thinking_mode: ConcreteThinkingMode | None = None,
    prompt_suffix: str = "",
    provider_profile: ProviderProfile = DEFAULT_PROVIDER_PROFILE,
    attempt_kind: AttemptKind = "initial",
    domain_knowledge_candidates: Sequence[DomainKnowledgeCandidate] = (),
    kb_source_text: str | None = None,
) -> str:
    """One translation POST, with the prompt this request's references need.

    The three optional arguments belong to a corrective call and are all
    ``None``/empty on the ordinary path, which is what keeps an ordinary request
    byte-for-byte what it was: ``timeout_seconds`` bounds this attempt alone,
    ``thinking_mode`` replaces the mode the environment would resolve, and
    ``prompt_suffix`` is one instruction appended after the prompt.
    """
    coerced_refs = tuple(_coerce_term_reference(item) for item in references)
    kb_tuple = tuple(domain_knowledge_candidates)
    has_kb = bool(kb_tuple) or bool(
        context is not None and getattr(context, "domain_knowledge_candidates", ())
    )
    if has_kb and kb_source_text is not None:
        prepared_for_plan = kb_source_text
    else:
        prepared_for_plan = locked_text
        if has_kb and locks:
            source_idx = 2 if to_chinese else 1
            for placeholder, zh, en in locks:
                prepared_for_plan = prepared_for_plan.replace(
                    placeholder, en if source_idx == 2 else zh
                )
    plan = TranslationPlan(
        source_text=prepared_for_plan,
        prepared_text=prepared_for_plan,
        locked_text=locked_text,
        locks=locks,
        references=coerced_refs,
        to_chinese=to_chinese,
        html_mode=html_mode,
        context=context,
        prompt_templates=current_prompt_templates(),
        domain_knowledge_candidates=kb_tuple,
    )
    client_read_timeout = getattr(getattr(client, "timeout", None), "read", None)
    effective_timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None
        else (
            float(client_read_timeout)
            if isinstance(client_read_timeout, (int, float))
            and not isinstance(client_read_timeout, bool)
            else DEFAULT_LLM_TIMEOUT_SECONDS
        )
    )
    policy = (
        resolve_request_policy_from_env(
            timeout_seconds=effective_timeout,
            en_zh_protocol=en_zh_protocol,
            to_chinese=to_chinese,
            logger=LOGGER,
        )
        if thinking_mode is None
        else RequestPolicy(
            timeout_seconds=effective_timeout,
            en_zh_protocol=en_zh_protocol,
        )
    )
    spec = build_translation_request_spec(
        plan,
        policy=policy,
        provider_profile=provider_profile,
        thinking_mode_override=thinking_mode,
        prompt_suffix=prompt_suffix,
        timeout_override=timeout_seconds,
        attempt_kind=attempt_kind,
    )
    with llm_transport.activate_call_metadata(
        spec.call_metadata(effective_timeout_seconds=effective_timeout)
    ):
        return await llm_transport.post_chat_completion(
            client,
            [dict(message) for message in spec.messages],
            purpose=spec.purpose,
            **spec.transport_options(),
        )
