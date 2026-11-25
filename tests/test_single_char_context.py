"""Ambiguous one-character name mentions: request-local evidence (MY-18 B2a).

``single_char_context`` records evidence for ``ambiguous_mention`` occurrences
of one-character resonator/speaker surfaces. The default translation path must
stay byte-identical to the pre-B2a ``main`` snapshot frozen below; the
evaluation is state only, never a lock and never a reference. review-v2 runs
its own evaluation only for the opt-in ``ambiguous-mention-v1`` feature.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import itertools
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from wuwaterm import term_preparation
from wuwaterm.application import ContextBefore, TranslationContext
from wuwaterm.cjk_span import (
    MATCHER_REVISION,
    SINGLE_CHAR_AMBIGUOUS_MENTION,
    SINGLE_CHAR_ORDINARY_WORD,
    classify_single_character_mention,
)
from wuwaterm.protected_text import ProtectedStructure, ProtectedText
from wuwaterm.sentence import SentenceTranslator
from wuwaterm.single_char_context import (
    ENTITY_EVIDENCE_KINDS,
    EVIDENCE_KINDS,
    ContextEntity,
    ContextEvaluation,
    EvidenceHit,
    RequestContextIndex,
    evaluate_ambiguous_mention,
)
from wuwaterm.term_preparation import (
    AmbiguousMention,
    PreparedTerms,
    _paragraph_spans,
    _paragraphs_with_official_terms,
    format_ambiguous_mention_lines,
    prepare_protected_terms,
    prepare_term_segment,
)
from wuwaterm.translation_plan import (
    AMBIGUOUS_MENTION_HEADER,
    AMBIGUOUS_MENTION_INSTRUCTION,
    RequestPolicy,
    TranslationPlan,
    build_translation_messages,
    build_translation_request_spec,
)

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "__WUWA_TERM_0123456789abcdef_"

LOCKABLE = (
    ("共鸣者", ("共鸣者", "Resonator")),
    ("心月狐", ("心月狐", "Hsin the Moon Fox")),
    ("今汐", ("今汐", "Jinhsi")),
    ("声骸", ("声骸", "Echo")),
    ("心", ("心", "Hsin")),
    ("椿", ("椿", "Camellya")),
)
CATEGORIES = {
    "共鸣者": "core_term",
    "心月狐": "resonator",
    "今汐": "resonator",
    "声骸": "core_term",
    "心": "resonator",
    "椿": "resonator",
}
DIRECT_TEXT = "心很重要。\n\n心加入队伍，今汐和椿姐也在。\n\n她的心很痛，红椿蕊开了。"


def _prepare(
    text: str,
    *,
    to_chinese: bool | None = False,
    lockable=LOCKABLE,
    categories=CATEGORIES,
    reference_only: frozenset[str] = frozenset(),
    context_gated_reference_only: frozenset[str] = frozenset(),
) -> PreparedTerms:
    return prepare_term_segment(
        text,
        lockable=lockable,
        context_text=text,
        context_offset=0,
        categories=categories if to_chinese is not None else {},
        reference_only=reference_only,
        to_chinese=to_chinese,
        allow_plural_inflection=False,
        nfkc_spelling_sources=frozenset(),
        placeholder_prefix=PREFIX,
        context_gated_reference_only=context_gated_reference_only,
    )


def _mention_at(prepared: PreparedTerms, start: int) -> AmbiguousMention:
    return next(item for item in prepared.ambiguous if item.occurrence.start == start)


def _assert_model_coordinates(prepared: PreparedTerms) -> None:
    """Every ambiguous occurrence still names its own raw character."""
    placeholder_spans = [
        (prepared.locked_text.index(lock[0]), prepared.locked_text.index(lock[0]) + len(lock[0]))
        for lock in prepared.locks
    ]
    for mention in prepared.ambiguous:
        start = mention.occurrence.model_start
        end = mention.occurrence.model_end
        assert isinstance(start, int) and isinstance(end, int)
        assert prepared.locked_text[start:end] == mention.source
        assert not any(lo < end and start < hi for lo, hi in placeholder_spans)


def _evaluate(text: str, start: int, *, entities=(), paragraphs=None, source=None, en="Camellya"):
    index = RequestContextIndex(
        text,
        paragraphs=paragraphs if paragraphs is not None else _paragraph_spans(text),
        entities=entities,
    )
    return evaluate_ambiguous_mention(
        index, start, start + 1, source=source or text[start], en=en
    )


def _entity(text: str, surface: str, category: str, en: str, *, nth: int = 0) -> ContextEntity:
    start = -1
    for _ in range(nth + 1):
        start = text.index(surface, start + 1)
    return ContextEntity(start, start + len(surface), surface, surface, en, category)


# ---------------------------------------------------------------------------
# Frozen pre-B2a snapshot of the default (exposure "off") path.
#
# Values were produced on origin/main 25ce314 before any B2a change, with the
# fixtures in ``_default_path_snapshot``. Equality proves locked_text, locks,
# references, prompt bytes and OperationIdentity are unchanged (I2).
#
# OperationIdentity hashes the matcher label, which has since moved from v10
# to v11 without any matcher input changing. The snapshot therefore recomputes
# the operation hash under the v10 label it was frozen with, and checks the
# live label separately.
# ---------------------------------------------------------------------------

MATCHER_V10_REVISION = "57428b88978597d95840f56ba0080a4f8fb11d4860fe8ea5532d3c1e7c6a918a"

PLAIN_CASES = (
    ("p1_zh_en_compound", "红椿蕊很漂亮。", False, None),
    ("p2_zh_en_speaker_like", "椿姐：你好", False, None),
    ("p3_zh_en_enumeration", "今汐、椿姐、守岸人一起出发。", False, None),
    ("p4_zh_en_mixed_lock", "欢迎椿来到今州。\n红椿蕊开了。", False, None),
    ("p5_zh_en_context_before", "红椿蕊开了。\n\n欢迎椿。", False, "before"),
    ("p6_zh_en_config", "0+1 椿姐 配今汐，椿姐和声骸。", False, None),
    ("p7_en_zh", "Camellya and Jinhsi saw 红椿蕊 and an Echo.", True, None),
    ("p8_zh_en_ordinary", "红椿·蕾和香椿拌豆腐", False, None),
    ("p9_zh_en_topic", "椿姐配队攻略\n\n共鸣者椿姐和今汐一起出发，椿姐很强。", False, "empty"),
)
HTML_CASES = (
    ("h1_zh_en", "<b>欢迎椿</b>来到今州，红椿蕊开了。椿姐和声骸", False),
    ("h2_en_zh", "Camellya <b>Jinhsi</b> 红椿蕊", True),
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _terms_repr(locked_text, locks, references) -> str:
    return repr((locked_text, tuple(locks), tuple(references)))


def _fixed_llm_environment(monkeypatch) -> None:
    import secrets

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "fixture")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "fixture")
    monkeypatch.setattr(secrets, "token_hex", lambda _size: "0123456789abcdef")


def _default_path_snapshot(sample_db, monkeypatch) -> dict[str, object]:
    _fixed_llm_environment(monkeypatch)
    out: dict[str, object] = {}
    translator = SentenceTranslator(sample_db)
    policy = RequestPolicy()
    for name, text, to_chinese, ctx in PLAIN_CASES:
        context = None
        if ctx == "before":
            context = TranslationContext(
                before=ContextBefore(source="椿大人来了。", target="Lady Camellya came.")
            )
        elif ctx == "empty":
            context = TranslationContext()
        exact, plan = translator.plan_translation(
            text, to_chinese=to_chinese, context=context, dictionary_identity="fixture"
        )
        assert exact is None and plan is not None, name
        spec = translator.build_request_spec(plan, policy=policy, model="fixture")
        op = spec.operation_identity(policy)
        assert op.matcher_revision == MATCHER_REVISION, name
        op = replace(spec, matcher_revision=MATCHER_V10_REVISION).operation_identity(policy)
        out[name] = {
            "terms": _terms_repr(plan.locked_text, plan.locks, plan.references),
            "system_sha256": _sha(spec.system_prompt),
            "user": spec.user_prompt,
            "request_sha256": op.request.request_sha256,
            "operation_sha256": op.operation_sha256,
        }

    for name, html, to_chinese in HTML_CASES:
        calls: list[dict] = []

        def echo(request, calls=calls):
            body = json.loads(request.content)
            calls.append(body)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": body["messages"][-1]["content"]}}]},
            )

        html_translator = SentenceTranslator(sample_db, llm_transport=httpx.MockTransport(echo))

        async def run(html_translator=html_translator, html=html, to_chinese=to_chinese):
            try:
                return await html_translator.translate_html_async(html, to_chinese=to_chinese)
            finally:
                await html_translator.aclose()

        asyncio.run(run())
        assert len(calls) == 1, name
        system, user = calls[0]["messages"]
        out[name] = {"system_sha256": _sha(system["content"]), "user": user["content"]}

    locked = translator.lock_terms("欢迎椿来到今州，红椿蕊开了。椿姐和声骸")
    out["d0_deterministic"] = _terms_repr(locked.locked_text, locked.locks, locked.references)

    for name, to_chinese in (
        ("d1_direct_zh_en", False),
        ("d2_direct_en_zh", True),
        ("d3_direct_none", None),
    ):
        prepared = _prepare(DIRECT_TEXT, to_chinese=to_chinese)
        out[name] = _terms_repr(prepared.locked_text, prepared.locks, prepared.references)

    protected = ProtectedText(
        visible_segments=("心加入队伍，", "红椿蕊和椿姐", "，欢迎椿。"),
        structures=(ProtectedStructure("<b>"), ProtectedStructure("</b>")),
    )
    for name, to_chinese in (("d4_protected_zh_en", False), ("d5_protected_en_zh", True)):
        prepared = prepare_protected_terms(
            protected,
            lockable=LOCKABLE,
            categories=CATEGORIES,
            reference_only=frozenset(),
            to_chinese=to_chinese,
            allow_plural_inflection=False,
            placeholder_prefix=PREFIX,
        )
        out[name] = _terms_repr(prepared.locked_text, prepared.locks, prepared.references)

    out["policy_identity"] = {
        "none": policy.to_identity_dict(),
        "en_zh": policy.to_identity_dict(to_chinese=True),
        "zh_en": policy.to_identity_dict(to_chinese=False),
    }
    return out


# fmt: off
MAIN_DEFAULT_PATH_SNAPSHOT: dict[str, object] = {'d0_deterministic': "('欢迎椿来到今州,红椿蕊开了。椿姐和__WUWA_TERM_0123456789abcdef_0000__', "
                     "(('__WUWA_TERM_0123456789abcdef_0000__', '声骸', 'Echo'),), ())",
 'd1_direct_zh_en': "('心很重要。\\n\\n__WUWA_TERM_0123456789abcdef_0000__加入队伍，今汐和椿姐也在。\\n\\n她的心很痛，红椿蕊开了。', "
                    "(('__WUWA_TERM_0123456789abcdef_0000__', '心', 'Hsin'),), "
                    "(TermReference(source='今汐', zh='今汐', en='Jinhsi', "
                    "category='resonator', occurrences=(TermOccurrence(start=13, "
                    "end=15, matched_text='今汐', segment_index=0, model_start=47, "
                    "model_end=49),), origin='dictionary', reason='contextual'),))",
 'd2_direct_en_zh': "('心很重要。\\n\\n心加入队伍，今汐和椿姐也在。\\n\\n她的心很痛，红椿蕊开了。', (), "
                    "(TermReference(source='心', zh='心', en='Hsin', "
                    "category='resonator', occurrences=(TermOccurrence(start=7, end=8, "
                    "matched_text='心', segment_index=0, model_start=7, model_end=8),), "
                    "origin='dictionary', reason='single_char_name_mention'), "
                    "TermReference(source='今汐', zh='今汐', en='Jinhsi', "
                    "category='resonator', occurrences=(TermOccurrence(start=13, "
                    "end=15, matched_text='今汐', segment_index=0, model_start=13, "
                    "model_end=15),), origin='dictionary', reason='contextual')))",
 'd3_direct_none': "('心很重要。\\n\\n__WUWA_TERM_0123456789abcdef_0000__加入队伍，__WUWA_TERM_0123456789abcdef_0001__和椿姐也在。\\n\\n她的心很痛，红椿蕊开了。', "
                   "(('__WUWA_TERM_0123456789abcdef_0000__', '心', 'Hsin'), "
                   "('__WUWA_TERM_0123456789abcdef_0001__', '今汐', 'Jinhsi')), ())",
 'd4_protected_zh_en': "('心加入队伍，__WUWA_HTML_0123456789abcdef_0000__红椿蕊和椿姐__WUWA_HTML_0123456789abcdef_0001__，欢迎__WUWA_TERM_0123456789abcdef_0000__。', "
                       "(('__WUWA_TERM_0123456789abcdef_0000__', '椿', 'Camellya'),), "
                       "(TermReference(source='心', zh='心', en='Hsin', "
                       "category='resonator', occurrences=(TermOccurrence(start=0, "
                       "end=1, matched_text='心', segment_index=0, model_start=0, "
                       "model_end=1),), origin='dictionary', "
                       "reason='single_char_name_mention'),))",
 'd5_protected_en_zh': "('心加入队伍，__WUWA_HTML_0123456789abcdef_0000__红椿蕊和椿姐__WUWA_HTML_0123456789abcdef_0001__，欢迎椿。', "
                       "(), (TermReference(source='心', zh='心', en='Hsin', "
                       "category='resonator', occurrences=(TermOccurrence(start=0, "
                       "end=1, matched_text='心', segment_index=0, model_start=0, "
                       "model_end=1),), origin='dictionary', "
                       "reason='single_char_name_mention'), TermReference(source='椿', "
                       "zh='椿', en='Camellya', category='resonator', "
                       "occurrences=(TermOccurrence(start=3, end=4, matched_text='椿', "
                       'segment_index=2, model_start=85, model_end=86),), '
                       "origin='dictionary', reason='single_char_name_mention')))",
 'h1_zh_en': {'system_sha256': '95a174ebee89fd09a32e1eb6d4088e8bee467dd7b831946bf4167ce6eab124f2',
              'user': '__WUWA_HTML_0123456789abcdef_0000__欢迎__WUWA_TERM_0123456789abcdef_0000____WUWA_HTML_0123456789abcdef_0001__来到今州，红椿蕊开了。椿姐和声骸'},
 'h2_en_zh': {'system_sha256': 'd9f827678fd1d4ff96bdf3ec2f6969c81768f6704c18b9c65d4a18508b2b0f89',
              'user': 'Camellya '
                      '__WUWA_HTML_0123456789abcdef_0000__Jinhsi__WUWA_HTML_0123456789abcdef_0001__ '
                      '红椿蕊'},
 'p1_zh_en_compound': {'operation_sha256': '45967b9f96a5b43b3985494dfe293ea732ed15871c3083ed4e9bbccb30c00493',
                       'request_sha256': '4443b77ecfce2755e1765060156b0914266b007864a6aaa84e92b5f0d4106398',
                       'system_sha256': '2b1325160b643d222920eb7dba52a6084bd1b8da9a09fae288cbc4cd80969c8b',
                       'terms': "('红椿蕊很漂亮。', (), ())",
                       'user': '红椿蕊很漂亮。'},
 'p2_zh_en_speaker_like': {'operation_sha256': '60a4dbd547ffceeb73d0d0af0466132d45166b3bec5094f109868fee096fe99a',
                           'request_sha256': 'b52867accc7d80244867b3d2f08f0ab3c887c0eef0711330ae0177475cb415e2',
                           'system_sha256': '2b1325160b643d222920eb7dba52a6084bd1b8da9a09fae288cbc4cd80969c8b',
                           'terms': "('椿姐:你好', (), ())",
                           'user': '椿姐:你好'},
 'p3_zh_en_enumeration': {'operation_sha256': 'fae91f0543391c9cfcd9584f5e0bf6f4a3d0fc9e4b7f7320ad94d2c06f62b64c',
                          'request_sha256': '30cac6a6520d1c5e063497b3227ad3d5195704cd7895b40a818d930ab64c48b6',
                          'system_sha256': '75978fabd8aed184852914ae4ff8a369cec2e7a74c7ebf2d0b79e147f707c8ca',
                          'terms': "('今汐、椿姐、守岸人一起出发。', (), (TermReference(source='今汐', "
                                   "zh='今汐', en='Jinhsi', category='resonator', "
                                   'occurrences=(TermOccurrence(start=0, end=2, '
                                   "matched_text='今汐', segment_index=0, model_start=0, "
                                   "model_end=2),), origin='dictionary', "
                                   "reason='contextual'), TermReference(source='守岸人', "
                                   "zh='守岸人', en='Shorekeeper', category='resonator', "
                                   'occurrences=(TermOccurrence(start=6, end=9, '
                                   "matched_text='守岸人', segment_index=0, "
                                   'model_start=6, model_end=9),), '
                                   "origin='dictionary', reason='contextual')))",
                          'user': '今汐、椿姐、守岸人一起出发。'},
 'p4_zh_en_mixed_lock': {'operation_sha256': '2d2abaadd0e58da6593321bc7d3e1ebc03cf4ee96d74cc7723cd4537ed149662',
                         'request_sha256': '70a0cbca48a89494e706df34d797d8fb051a46372b9cea00df2ee338cd33f343',
                         'system_sha256': '3bdebc9714301f9f99a7fe19aa4037dbe99bc79f8c583b1275ded217b0392063',
                         'terms': "('欢迎__WUWA_TERM_0123456789abcdef_0000__来到今州。\\n红椿蕊开了。', "
                                  "(('__WUWA_TERM_0123456789abcdef_0000__', '椿', "
                                  "'Camellya'),), ())",
                         'user': '欢迎__WUWA_TERM_0123456789abcdef_0000__来到今州。\n红椿蕊开了。'},
 'p5_zh_en_context_before': {'operation_sha256': '49b9f595100861687912b73128aeab94449401f117e600cc0b974a62d09598ac',
                             'request_sha256': '211efa58cf3ae8c96fb20fa04200279de3a060c309c356243095df8a3a988ed4',
                             'system_sha256': '3d1b8f1043b34d14e90b32e345d8800603cd444edc7544a3597dddbc9d137bc8',
                             'terms': "('红椿蕊开了。\\n\\n欢迎__WUWA_TERM_0123456789abcdef_0000__。', "
                                      "(('__WUWA_TERM_0123456789abcdef_0000__', '椿', "
                                      "'Camellya'),), ())",
                             'user': '红椿蕊开了。\n'
                                     '\n'
                                     '欢迎__WUWA_TERM_0123456789abcdef_0000__。'},
 'p6_zh_en_config': {'operation_sha256': '801335dbf38dad35ea0bb100a2a45988317c83cafdd4613ec2fa70074b3761d5',
                     'request_sha256': 'd0ad2567e665232f9f128a539e2b6d984e84bc34d5407c9bd59221ff09ef0a38',
                     'system_sha256': 'e06f156754cf45f87e62a450e80af6302fbbe06ce8b10f547d60eb69e3096b36',
                     'terms': "('0+1 椿姐 配今汐,椿姐和声骸。', (), (TermReference(source='今汐', "
                              "zh='今汐', en='Jinhsi', category='resonator', "
                              'occurrences=(TermOccurrence(start=8, end=10, '
                              "matched_text='今汐', segment_index=0, model_start=8, "
                              "model_end=10),), origin='dictionary', "
                              "reason='contextual'), TermReference(source='声骸', "
                              "zh='声骸', en='Echo', category='core_term', "
                              'occurrences=(TermOccurrence(start=14, end=16, '
                              "matched_text='声骸', segment_index=0, model_start=14, "
                              "model_end=16),), origin='dictionary', "
                              "reason='contextual')))",
                     'user': '0+1 椿姐 配今汐,椿姐和声骸。'},
 'p7_en_zh': {'operation_sha256': '39c55b19b46216e9a4197083ef6b6f488289b706b5bb2af0adbb28fdbfbec1e9',
              'request_sha256': '6ad6dcb70e54a2ebc87ee0309788b2bb948b95c5e7d46566e574f41860deb463',
              'system_sha256': '54bf522aae0716aa333cefff3bac46a77235d698b935f83848eeeaf76c66eed5',
              'terms': "('Camellya and Jinhsi saw 红椿蕊 and an "
                       "__WUWA_TERM_0123456789abcdef_0000__.', "
                       "(('__WUWA_TERM_0123456789abcdef_0000__', '声骸', 'Echo'),), "
                       "(TermReference(source='Camellya', zh='椿', en='Camellya', "
                       "category='resonator', occurrences=(TermOccurrence(start=0, "
                       "end=8, matched_text='Camellya', segment_index=0, "
                       "model_start=0, model_end=8),), origin='dictionary', "
                       "reason='contextual'), TermReference(source='Jinhsi', zh='今汐', "
                       "en='Jinhsi', category='resonator', "
                       'occurrences=(TermOccurrence(start=13, end=19, '
                       "matched_text='Jinhsi', segment_index=0, model_start=13, "
                       "model_end=19),), origin='dictionary', reason='contextual')))",
              'user': 'Camellya and Jinhsi saw 红椿蕊 and an '
                      '__WUWA_TERM_0123456789abcdef_0000__.'},
 'p8_zh_en_ordinary': {'operation_sha256': '515f4023d9c5903faa473bf7cf9232558a4a0d75c687ae2bd051f305d0796ef0',
                       'request_sha256': 'e68c1e8eeae1e1eba0e61eff15b4932d4c60fe36b4413e4671bd6dd172ac0ca0',
                       'system_sha256': '2b1325160b643d222920eb7dba52a6084bd1b8da9a09fae288cbc4cd80969c8b',
                       'terms': "('红椿·蕾和香椿拌豆腐', (), ())",
                       'user': '红椿·蕾和香椿拌豆腐'},
 'p9_zh_en_topic': {'operation_sha256': 'a2371097398b0b76f494d03a2caa5e92481a5a4830875096fb32b52bdec3fdba',
                    'request_sha256': 'cc7cf4c949850766d47f2a6465db4b64ecb986bc34ce0d3fbcf28a5da081b529',
                    'system_sha256': '747fe798e7a0d589d923bcb598ee8d9fb2cd30e391e64c1585f126e181e45b23',
                    'terms': "('椿姐配队攻略\\n\\n共鸣者椿姐和今汐一起出发,椿姐很强。', (), "
                             "(TermReference(source='共鸣者', zh='共鸣者', en='Resonator', "
                             "category='core_term', "
                             'occurrences=(TermOccurrence(start=8, end=11, '
                             "matched_text='共鸣者', segment_index=0, model_start=8, "
                             "model_end=11),), origin='dictionary', "
                             "reason='contextual'), TermReference(source='今汐', "
                             "zh='今汐', en='Jinhsi', category='resonator', "
                             'occurrences=(TermOccurrence(start=14, end=16, '
                             "matched_text='今汐', segment_index=0, model_start=14, "
                             "model_end=16),), origin='dictionary', "
                             "reason='contextual')))",
                    'user': '椿姐配队攻略\n\n共鸣者椿姐和今汐一起出发,椿姐很强。'},
 'policy_identity': {'en_zh': {'auto_lock_limit': 4,
                               'auto_paragraph_limit': 25,
                               'configured_thinking': 'omit',
                               'direction': 'en->zh',
                               'en_zh_protocol': False,
                               'max_corrective_calls': 1,
                               'paragraph_regroup_enabled': True,
                               'paragraph_repair_enabled': True,
                               'retry_budget_headroom_seconds': 2.0,
                               'structural_retry_min_remaining_seconds': 15.0,
                               'timeout_fallback_min_remaining_seconds': 10.0,
                               'timeout_fallback_timeout_seconds': 20.0,
                               'timeout_seconds': 45.0},
                     'none': {'auto_lock_limit': 4,
                              'auto_paragraph_limit': 25,
                              'configured_thinking_en_zh': 'omit',
                              'configured_thinking_zh_en': 'disabled',
                              'en_zh_protocol': False,
                              'max_corrective_calls': 1,
                              'paragraph_regroup_enabled': True,
                              'paragraph_repair_enabled': True,
                              'retry_budget_headroom_seconds': 2.0,
                              'structural_retry_min_remaining_seconds': 15.0,
                              'timeout_fallback_min_remaining_seconds': 10.0,
                              'timeout_fallback_timeout_seconds': 20.0,
                              'timeout_seconds': 45.0},
                     'zh_en': {'configured_thinking': 'disabled',
                               'direction': 'zh->en',
                               'max_corrective_calls': 1,
                               'paragraph_regroup_enabled': True,
                               'paragraph_repair_enabled': True,
                               'retry_budget_headroom_seconds': 2.0,
                               'structural_retry_min_remaining_seconds': 15.0,
                               'timeout_seconds': 45.0}}}
# fmt: on


def test_default_path_is_byte_identical_to_frozen_main_snapshot(sample_db, monkeypatch):
    """I2: exposure "off" keeps locks, references, prompt bytes and identity."""
    snapshot = _default_path_snapshot(sample_db, monkeypatch)
    assert snapshot.keys() == MAIN_DEFAULT_PATH_SNAPSHOT.keys()
    for key in sorted(snapshot):
        assert snapshot[key] == MAIN_DEFAULT_PATH_SNAPSHOT[key], key


def test_default_policy_identity_has_no_exposure_key_and_explicit_mode_does():
    """I2: the field joins the policy identity only when it is not "off"."""
    off = RequestPolicy()
    on = RequestPolicy(ambiguous_mention_exposure="scoped_alternative")
    for to_chinese in (None, True, False):
        off_payload = off.to_identity_dict(to_chinese=to_chinese)
        on_payload = on.to_identity_dict(to_chinese=to_chinese)
        assert "ambiguous_mention_exposure" not in off_payload
        assert on_payload.pop("ambiguous_mention_exposure") == "scoped_alternative"
        assert on_payload == off_payload
    assert RequestPolicy(ambiguous_mention_exposure="off") == off
    with pytest.raises(ValueError):
        RequestPolicy(ambiguous_mention_exposure="on")  # type: ignore[arg-type]


def _plan(text: str, prepared: PreparedTerms, *, to_chinese: bool = False, context=None):
    return TranslationPlan(
        source_text=text,
        prepared_text=text,
        locked_text=prepared.locked_text,
        locks=prepared.locks,
        references=prepared.references,
        to_chinese=to_chinese,
        context=context,
        dictionary_identity="fixture",
        ambiguous=prepared.ambiguous,
    )


def test_off_exposure_ignores_recorded_ambiguous_mentions():
    """I2: a plan that carries ambiguous state renders exactly like one without."""
    text = "0+1 椿姐 S0R1\n\n欢迎椿。"
    prepared = _prepare(text)
    assert prepared.ambiguous and prepared.ambiguous[0].evaluation.admitted_as_low_constraint
    with_state = _plan(text, prepared)
    without_state = replace(with_state, ambiguous=())
    policy = RequestPolicy()
    spec_with = build_translation_request_spec(with_state, policy=policy, model="fixture")
    spec_without = build_translation_request_spec(without_state, policy=policy, model="fixture")
    assert spec_with.messages == spec_without.messages
    assert (
        spec_with.operation_identity(policy).operation_sha256
        == spec_without.operation_identity(policy).operation_sha256
    )
    assert build_translation_messages(with_state) == build_translation_messages(without_state)
    with pytest.raises(ValueError):
        build_translation_messages(with_state, ambiguous_mention_exposure="on")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# scoped_alternative: offline only, low-constraint wording, never a lock.
# ---------------------------------------------------------------------------


def test_scoped_alternative_renders_only_admitted_mentions_without_locks():
    text = "0+1 椿姐 S0R1\n\n欢迎椿。\n\n红椿蕊开了。"
    prepared = _prepare(text)
    admitted = [m for m in prepared.ambiguous if m.evaluation.admitted_as_low_constraint]
    rejected = [m for m in prepared.ambiguous if not m.evaluation.admitted_as_low_constraint]
    assert [m.occurrence.start for m in admitted] == [4]
    assert [text[m.occurrence.start - 1 : m.occurrence.end + 1] for m in rejected] == ["红椿蕊"]
    _assert_model_coordinates(prepared)

    plan = _plan(text, prepared)
    off_policy = RequestPolicy()
    on_policy = RequestPolicy(ambiguous_mention_exposure="scoped_alternative")
    off_system = build_translation_messages(plan)[0]["content"]
    on_spec = build_translation_request_spec(plan, policy=on_policy, model="fixture")
    off_spec = build_translation_request_spec(plan, policy=off_policy, model="fixture")
    on_system = on_spec.system_prompt
    assert on_system == (
        off_system
        + "\n"
        + AMBIGUOUS_MENTION_INSTRUCTION
        + AMBIGUOUS_MENTION_HEADER
        + "椿 = Camellya (resonator) — POSSIBLY at: 0+1 「椿」姐 S0R1"
    )
    assert on_spec.user_prompt == off_spec.user_prompt == prepared.locked_text
    # The admitted 欢迎椿 is the only lock; no placeholder was added for 椿姐.
    assert [lock[1:] for lock in plan.locks] == [("椿", "Camellya")]
    assert (
        on_spec.operation_identity(on_policy).operation_sha256
        != off_spec.operation_identity(off_policy).operation_sha256
    )


def test_scoped_alternative_fails_closed_on_tampered_coordinates():
    text = "0+1 椿姐 S0R1\n\n欢迎椿。"
    prepared = _prepare(text)
    mention = prepared.ambiguous[0]
    shifted = replace(mention, occurrence=mention.occurrence.shifted_model(1))
    with pytest.raises(ValueError):
        format_ambiguous_mention_lines(prepared.locked_text, (shifted,), to_chinese=False)
    plan = replace(_plan(text, prepared), ambiguous=(shifted,))
    with pytest.raises(ValueError):
        build_translation_messages(plan, ambiguous_mention_exposure="scoped_alternative")
    # A tampered entry that never met the threshold is not rendered, so it
    # cannot fail the request either.
    rejected = replace(shifted, evaluation=ContextEvaluation())
    assert format_ambiguous_mention_lines(prepared.locked_text, (rejected,), to_chinese=False) == ()


# ---------------------------------------------------------------------------
# I1: never a lock, never admitted evidence for B1b locking or B1 gating.
# ---------------------------------------------------------------------------


def test_ambiguous_mentions_are_never_locked_or_references():
    """I1: ambiguous occurrences stay raw text in every direction and path."""
    for to_chinese in (False, True):
        prepared = _prepare(DIRECT_TEXT, to_chinese=to_chinese)
        assert {m.occurrence.start for m in prepared.ambiguous} == {0, 16, 25, 30}
        for mention in prepared.ambiguous:
            assert classify_single_character_mention(
                DIRECT_TEXT, mention.occurrence.start, mention.occurrence.end
            ) == SINGLE_CHAR_AMBIGUOUS_MENTION
        reference_spans = {
            (occ.start, occ.end)
            for ref in prepared.references
            for occ in ref.occurrences
        }
        assert not reference_spans & {
            (m.occurrence.start, m.occurrence.end) for m in prepared.ambiguous
        }
        _assert_model_coordinates(prepared)
    # The direction-less deterministic path records nothing.
    assert _prepare(DIRECT_TEXT, to_chinese=None).ambiguous == ()


def test_admitted_ambiguous_mention_still_counts_as_unadmitted_for_selective_locking():
    """I1: B1b's raw_other_count is unchanged by an admitted ambiguous mention."""
    text = "欢迎椿来到这里，今汐和椿姐也在。"
    prepared = _prepare(text)
    mention = _mention_at(prepared, text.index("椿姐"))
    assert mention.evaluation.admitted_as_low_constraint
    # 欢迎椿 is locked exactly as before because 椿姐 is still an unadmitted
    # raw occurrence; 椿姐 itself keeps its raw character.
    assert [lock[1:] for lock in prepared.locks] == [("椿", "Camellya")]
    assert prepared.locked_text == f"欢迎{PREFIX}0000__来到这里，今汐和椿姐也在。"
    assert [ref.source for ref in prepared.references] == ["今汐"]
    _assert_model_coordinates(prepared)


def test_admitted_ambiguous_mention_is_not_official_paragraph_evidence():
    """I1: an admitted ambiguous mention does not open derived gating."""
    lockable = (*LOCKABLE, ("教学楼", ("学部教学楼", "Academia Hall")))
    categories = {**CATEGORIES, "教学楼": "location"}
    text = "0+1 椿姐 S0R1，教学楼很旧。\n\n欢迎椿。"
    gated = frozenset({"教学楼"})
    prepared = _prepare(
        text,
        lockable=lockable,
        categories=categories,
        reference_only=gated,
        context_gated_reference_only=gated,
    )
    mention = _mention_at(prepared, text.index("椿姐"))
    assert mention.evaluation.admitted_as_low_constraint
    assert "教学楼" not in {ref.source for ref in prepared.references}
    paragraphs = _paragraph_spans(text)
    assert _paragraphs_with_official_terms(
        text,
        paragraphs,
        lockable=lockable,
        categories=categories,
        reference_only=gated,
        allow_plural_inflection=False,
        nfkc_spelling_sources=frozenset(),
    ) == frozenset({1})


def test_other_ambiguous_mentions_never_vouch_for_each_other():
    """I1: no fixed point -- two uncertain characters give each other nothing."""
    text = "椿姐和椿妹，心很重要"
    prepared = _prepare(text)
    assert len(prepared.ambiguous) == 3
    for mention in prepared.ambiguous:
        assert not mention.evaluation.same_paragraph_entity
        assert not mention.evaluation.resonator_cooccurrence
        assert not mention.evaluation.admitted_as_low_constraint


# ---------------------------------------------------------------------------
# I3: only the visible text of the current request is read.
# ---------------------------------------------------------------------------


def test_previous_context_never_supplies_evidence():
    """I3: 椿大人 in context.before gives 红椿蕊 nothing; in the request it does."""
    context = TranslationContext(
        before=ContextBefore(source="椿大人来了。", target="Lady Camellya came.")
    )
    current = "红椿蕊开了。"
    prepared = _prepare(current)
    assert len(prepared.ambiguous) == 1
    evaluation = prepared.ambiguous[0].evaluation
    assert evaluation.hits == ()
    assert not any(getattr(evaluation, kind) for kind in EVIDENCE_KINDS)
    assert not evaluation.admitted_as_low_constraint

    plan = _plan(current, prepared, context=context)
    system = build_translation_messages(
        plan, ambiguous_mention_exposure="scoped_alternative"
    )[0]["content"]
    assert AMBIGUOUS_MENTION_HEADER not in system
    assert "椿大人来了。" in system.split("Read-only previous segment", 1)[1]

    # Control: the same name inside the request is evidence.
    control = "椿大人来了。红椿蕊开了。"
    control_prepared = _prepare(control)
    control_eval = _mention_at(control_prepared, control.index("红椿蕊") + 1).evaluation
    assert control_eval.resonator_cooccurrence
    assert control_eval.same_paragraph_entity
    # Both kinds rest on the one 椿大人 span, so they are not independent.
    assert not control_eval.admitted_as_low_constraint


def test_single_char_context_has_no_path_to_translation_context():
    """I3: the module cannot reach TranslationContext or its read-only before."""
    tree = ast.parse((ROOT / "src/wuwaterm/single_char_context.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        assert not (isinstance(node, ast.Attribute) and node.attr == "before")
    assert imported <= {"__future__", "bisect", "re", "collections.abc", "dataclasses", "typing", "cjk_span"}


# ---------------------------------------------------------------------------
# I4, I5, I6
# ---------------------------------------------------------------------------


def test_review_does_not_consume_translation_ambiguous_state():
    """I4: review never reads the translation side's ambiguous state.

    review-v2 runs its own request-local evaluation for ambiguous-mention-v1
    (see tests/test_review_ambiguous_mention.py); it imports the evaluation
    module, never ``term_preparation`` or its ``AmbiguousMention``, and neither
    evidence module can produce a verified or conflicting judgment.
    """
    review_tree = ast.parse((ROOT / "src/wuwaterm/review.py").read_text(encoding="utf-8"))
    for node in ast.walk(review_tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in {"term_preparation", "wuwaterm.term_preparation"}
            assert not any(alias.name == "AmbiguousMention" for alias in node.names)
    for name in ("single_char_context.py", "term_preparation.py"):
        source = (ROOT / "src/wuwaterm" / name).read_text(encoding="utf-8")
        assert "verified_constraint" not in source
        assert "confirmed_conflict" not in source


def test_matcher_revision_is_v11_with_v10_matcher_inputs():
    """I5: the classifier is unchanged; only the basis label moved to v11.

    The label moved because review-v2's opt-in ambiguous-mention-v1 lets the
    same request give a different report; no matcher input changed.
    """
    assert MATCHER_REVISION == "286d29e8f25b1ee53d0634c89bf6fb8a83ef950888c21f15d485413cb25d0664"
    source = (ROOT / "src/wuwaterm/cjk_span.py").read_text(encoding="utf-8")
    assert '"wuwaterm-cjk-matcher-v11",' in source


@pytest.mark.parametrize("text", ["红椿·蕾", "香椿拌豆腐", "红椿·蕾和香椿拌豆腐"])
def test_middle_dot_and_compound_words_stay_ordinary(text):
    """I6: 红椿·蕾 and 香椿拌豆腐 are ordinary words, never ambiguous mentions."""
    for position, char in enumerate(text):
        if char == "椿":
            assert (
                classify_single_character_mention(text, position, position + 1)
                == SINGLE_CHAR_ORDINARY_WORD
            )
    for to_chinese in (False, True):
        prepared = _prepare(text, to_chinese=to_chinese)
        assert prepared.ambiguous == ()
        assert prepared.locks == ()
        assert "椿" not in {ref.source for ref in prepared.references}


# ---------------------------------------------------------------------------
# Evidence kinds and the threshold.
# ---------------------------------------------------------------------------


def test_threshold_truth_table_with_distinct_supports():
    """admitted iff >= 2 kinds hold and one of them is an entity kind."""
    for flags in itertools.product((False, True), repeat=len(EVIDENCE_KINDS)):
        hits = tuple(
            EvidenceHit(kind, ((index * 10, index * 10 + 1),))
            for index, (kind, on) in enumerate(zip(EVIDENCE_KINDS, flags))
            if on
        )
        evaluation = ContextEvaluation(hits)
        kinds = {kind for kind, on in zip(EVIDENCE_KINDS, flags) if on}
        assert set(evaluation.evidence) == kinds
        expected = len(kinds) >= 2 and bool(kinds & ENTITY_EVIDENCE_KINDS)
        assert evaluation.admitted_as_low_constraint is expected, kinds


def test_one_span_cannot_satisfy_two_kinds():
    shared = ((3, 5),)
    single = ContextEvaluation(
        (EvidenceHit("document_topic", shared), EvidenceHit("same_paragraph_entity", shared))
    )
    assert single.document_topic and single.same_paragraph_entity
    assert not single.admitted_as_low_constraint
    second = ContextEvaluation(
        (
            EvidenceHit("document_topic", shared),
            EvidenceHit("same_paragraph_entity", ((3, 5), (8, 10))),
        )
    )
    assert second.admitted_as_low_constraint
    # The real path: 声骸 is both the request topic and the paragraph's entity.
    text = "椿姐和声骸"
    evaluation = _prepare(text).ambiguous[0].evaluation
    assert evaluation.evidence == ("document_topic", "same_paragraph_entity")
    assert not evaluation.admitted_as_low_constraint


def test_document_topic_reads_the_request_head():
    text = "共鸣者攻略\n\n今天红椿蕊开了。"
    start = text.index("椿")
    assert classify_single_character_mention(text, start, start + 1) == SINGLE_CHAR_AMBIGUOUS_MENTION
    topic = _entity(text, "共鸣者", "core_term", "Resonator")
    assert _evaluate(text, start, entities=(topic,)).document_topic
    # A game-system term outside the head is not the topic.
    late = "今天红椿蕊开了。\n\n共鸣者攻略"
    late_start = late.index("椿")
    assert not _evaluate(
        late, late_start, entities=(_entity(late, "共鸣者", "core_term", "Resonator"),)
    ).document_topic
    # A longer official name of the same character is a topic signal.
    hsin = "心月狐的故事\n\n她说心很重要。"
    hsin_start = hsin.index("心很")
    assert classify_single_character_mention(hsin, hsin_start, hsin_start + 1) == SINGLE_CHAR_AMBIGUOUS_MENTION
    fox = _entity(hsin, "心月狐", "resonator", "Hsin the Moon Fox")
    assert _evaluate(hsin, hsin_start, entities=(fox,), en="Hsin").document_topic
    # A resonator with an unrelated name in the head is not the topic.
    other = ContextEntity(0, 3, "心月狐", "心月狐", "Moon Fox", "resonator")
    assert not _evaluate(hsin, hsin_start, entities=(other,), en="Hsin").document_topic


def test_same_paragraph_entity_stays_in_its_paragraph():
    text = "今汐来了。\n\n红椿蕊开了。"
    start = text.index("椿")
    jinhsi = _entity(text, "今汐", "resonator", "Jinhsi")
    assert not _evaluate(text, start, entities=(jinhsi,)).same_paragraph_entity
    same = "今汐来了，红椿蕊开了。"
    assert _evaluate(
        same, same.index("椿"), entities=(_entity(same, "今汐", "resonator", "Jinhsi"),)
    ).same_paragraph_entity


def test_resonator_cooccurrence_sentence_window_and_same_character():
    filler = "然后大家一起走了很久很久很久很久很久很久"  # 20 scalars, no punctuation
    text = f"今汐说{filler}红椿蕊开了。"
    start = text.index("椿")
    assert classify_single_character_mention(text, start, start + 1) == SINGLE_CHAR_AMBIGUOUS_MENTION
    jinhsi = _entity(text, "今汐", "resonator", "Jinhsi")
    assert start - jinhsi.end > 20
    # Same sentence, outside the 24-scalar window: still co-occurrence.
    assert _evaluate(text, start, entities=(jinhsi,)).resonator_cooccurrence
    # Another sentence but inside the window.
    near = "今汐来了。红椿蕊开了。"
    assert _evaluate(
        near, near.index("椿"), entities=(_entity(near, "今汐", "resonator", "Jinhsi"),)
    ).resonator_cooccurrence
    # Another sentence and outside the window.
    far = f"今汐来了。{filler}。红椿蕊开了。"
    assert not _evaluate(
        far, far.index("椿"), entities=(_entity(far, "今汐", "resonator", "Jinhsi"),)
    ).resonator_cooccurrence
    # The window never crosses a paragraph break.
    split = "今汐来了。\n\n红椿蕊开了。"
    assert not _evaluate(
        split, split.index("椿"), entities=(_entity(split, "今汐", "resonator", "Jinhsi"),)
    ).resonator_cooccurrence
    # A non-resonator entity and the same character's Latin name are not another resonator.
    echo = "声骸和红椿蕊。"
    assert not _evaluate(
        echo, echo.index("椿"), entities=(_entity(echo, "声骸", "core_term", "Echo"),)
    ).resonator_cooccurrence
    latin = "Camellya和红椿蕊。"
    assert not _evaluate(
        latin, latin.index("椿"), entities=(_entity(latin, "Camellya", "resonator", "Camellya"),)
    ).resonator_cooccurrence
    # The same character admitted as a name anywhere in the request.
    mention = "欢迎椿。\n\n很多段以后\n\n红椿蕊开了。"
    prepared = _prepare(mention)
    assert _mention_at(prepared, mention.index("红椿蕊") + 1).evaluation.resonator_cooccurrence


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("椿姐：今天去哪？", "椿"),
        ("  椿姐: 走吧", "椿"),
        ("今汐、椿姐、守岸人", "椿"),
        ("0+1 椿姐 配队", "椿"),
        ("0+1椿姐", "椿"),
        ("椿姐 S0R1 很强", "椿"),
        ("6链椿姐", "椿"),
        ("他说「椿姐来了」", "椿"),
        ("# 椿姐配队", "椿"),
        ("椿姐配队攻略\n\n正文在这里", "椿"),
    ],
)
def test_position_context_kinds(text, needle):
    start = text.index(needle)
    assert classify_single_character_mention(text, start, start + 1) == SINGLE_CHAR_AMBIGUOUS_MENTION
    evaluation = _evaluate(text, start)
    assert evaluation.position_context
    assert evaluation.evidence == ("position_context",)
    assert not evaluation.admitted_as_low_constraint


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("心很重要。\n\n心加入队伍。", "心"),
        ("她说椿姐：好", "椿"),
        ("红椿蕊开了", "椿"),
        ("今天的红椿蕊开了，很美", "椿"),
        ("A1椿姐", "椿"),
    ],
)
def test_position_context_negatives(text, needle):
    start = text.index(needle)
    assert classify_single_character_mention(text, start, start + 1) == SINGLE_CHAR_AMBIGUOUS_MENTION
    assert not _evaluate(text, start).position_context


def test_invalid_coordinates_give_no_evidence():
    text = "今汐、椿姐"
    index = RequestContextIndex(text, paragraphs=((0, len(text)),), entities=())
    assert evaluate_ambiguous_mention(index, 0, 1, source="椿", en="Camellya") == ContextEvaluation()
    assert evaluate_ambiguous_mention(index, 9, 10, source="椿", en="Camellya") == ContextEvaluation()


# ---------------------------------------------------------------------------
# Coordinates through placeholders and protected segments; one index per request.
# ---------------------------------------------------------------------------


def test_protected_model_coordinates_survive_segments_and_selective_locks():
    protected = ProtectedText(
        visible_segments=("欢迎椿，", "红椿蕊和椿姐", "，今汐。"),
        structures=(ProtectedStructure("<b>"), ProtectedStructure("</b>")),
    )
    for to_chinese in (False, True):
        prepared = prepare_protected_terms(
            protected,
            lockable=LOCKABLE,
            categories=CATEGORIES,
            reference_only=frozenset(),
            to_chinese=to_chinese,
            allow_plural_inflection=False,
            placeholder_prefix=PREFIX,
        )
        assert [(m.occurrence.segment_index, m.occurrence.start) for m in prepared.ambiguous] == [
            (1, 1),
            (1, 4),
        ]
        _assert_model_coordinates(prepared)
        if not to_chinese:
            # 欢迎椿 was locked before the segment that holds the mentions.
            assert prepared.locks and prepared.locked_text.index(PREFIX) < (
                prepared.ambiguous[0].occurrence.model_start
            )
        for mention in prepared.ambiguous:
            assert mention.evaluation.resonator_cooccurrence


def test_plain_model_coordinates_follow_earlier_placeholders():
    text = "声骸和欢迎椿，今汐和椿姐、心很重要"
    for to_chinese in (False, True):
        prepared = _prepare(text, to_chinese=to_chinese)
        assert len(prepared.ambiguous) == 2
        _assert_model_coordinates(prepared)


def test_context_index_is_built_once_per_request(monkeypatch):
    built: list[int] = []
    original = term_preparation.RequestContextIndex

    class CountingIndex(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            built.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(term_preparation, "RequestContextIndex", CountingIndex)
    text = "今汐和椿姐。\n\n" + "她的心很痛。" * 200
    prepared = _prepare(text)
    assert len(prepared.ambiguous) == 201
    assert len(built) == 1

    built.clear()
    assert _prepare("今汐和声骸。").ambiguous == ()
    assert built == []

    built.clear()
    protected = ProtectedText(
        visible_segments=("心很重要，", "红椿蕊", "，今汐。"),
        structures=(ProtectedStructure("<b>"), ProtectedStructure("</b>")),
    )
    prepare_protected_terms(
        protected,
        lockable=LOCKABLE,
        categories=CATEGORIES,
        reference_only=frozenset(),
        to_chinese=False,
        allow_plural_inflection=False,
        placeholder_prefix=PREFIX,
    )
    assert len(built) == 1
