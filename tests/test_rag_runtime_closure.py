"""Runtime closure tests for P0-2: companion knowledge.db wiring,
protected-text soft RAG retrieval, /v1/knowledge/query, dynamic capability
negotiation, and zero-cost Core in-place upgrade replacement.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from tests.test_api import (
    _META_EXACT_11_KEYS,
    _TRANSLATION_EXACT_5_KEYS,
    bearer,
    build_settings,
    call,
    disable_llm,
    issue_device,
    run,
)
from wuwaterm.application import (
    CAPABILITY_KNOWLEDGE_RETRIEVAL,
    ERROR_INVALID_REQUEST,
    ERROR_KNOWLEDGE_UNAVAILABLE,
    KIND_ERROR,
    KIND_LLM,
    ProtectedStructure,
    ProtectedTranslationJob,
    TranslationJob,
    build_translator,
    query_domain_knowledge,
    service_capabilities,
    translate_protected_request_async,
    translate_request_async,
)
from wuwaterm.data_source import SourceProvenance
from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.knowledge_builder import (
    AliasSpec,
    EntitySpec,
    RelationSpec,
    create_knowledge_database,
)
from wuwaterm.knowledge_db import compute_terms_db_revision
from wuwaterm.lookup import TermService
from wuwaterm.models import TermRecord
from wuwaterm.protected_text import ProtectedText
from wuwaterm.sentence import SentenceTranslator
from wuwaterm.translation_plan import DOMAIN_KNOWLEDGE_HEADER
from wuwaterm_api.app import create_app
from wuwaterm_api.auth import SCOPE_META, SCOPE_TRANSLATE, DeviceStore
from wuwaterm_api.settings import ApiSettings


def _test_provenance(commit: str = "commit-v1", game_version: str = "2.4") -> SourceProvenance:
    return SourceProvenance(
        profile="arikatsu",
        repo_url="https://example.invalid/wutheringdata",
        commit=commit,
        game_version=game_version,
        resource_version=f"{game_version}.0",
        changelist="100001",
    )


def _create_terms_db(
    db_path: Path,
    *,
    extra_records: tuple[TermRecord, ...] = (),
    commit: str = "commit-v1",
    game_version: str = "2.4",
) -> str:
    records = [
        TermRecord("resonator", "fixture", "101", "Role_101", "今汐", "Jinhsi"),
        TermRecord("echo", "fixture", "102", "Echo_102", "声骸", "Echo"),
        TermRecord("location", "fixture", "103", "Area_103", "黑海岸", "Black Shores"),
        *extra_records,
    ]
    with closing(connect(db_path)) as conn:
        initialize(conn)
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (
                ("schema_version", "2"),
                ("source_profile", "arikatsu"),
                ("source_commit", commit),
                ("source_game_version", game_version),
                ("source_resource_version", f"{game_version}.0"),
                ("source_changelist", "100001"),
            ),
        )
        insert_records(conn, records)
        conn.commit()
    return compute_terms_db_revision(db_path)


def _create_knowledge_db(
    kb_path: Path,
    *,
    terms_db_revision: str,
    commit: str = "commit-v1",
    game_version: str = "2.4",
    extra_entities: tuple[EntitySpec, ...] = (),
) -> None:
    entities = [
        EntitySpec(
            "element:glacio",
            "element",
            "BinData/element.json",
            "1",
            "冷凝",
            "Glacio",
            "official_authoritative",
            "Ice-aligned Resonator damage attribute.",
        ),
        EntitySpec(
            "faction:midnight_rangers",
            "faction",
            "BinData/faction.json",
            "2",
            "夜归",
            "Midnight Rangers",
            "official_authoritative",
            "Huanglong military defense corps.",
        ),
        EntitySpec(
            "resonator:camellya",
            "resonator",
            "BinData/role/camellya.json",
            "1603",
            "椿",
            "Camellya",
            "official_authoritative",
            "Black Shores Bloom Bearer resonator.",
        ),
        EntitySpec(
            "resonator:encore",
            "resonator",
            "BinData/role/encore.json",
            "1203",
            "安可",
            "Encore",
            "official_authoritative",
            "Black Shores Fusion resonator.",
        ),
        EntitySpec(
            "mechanic:echo_system",
            "mechanic",
            "BinData/echo.json",
            "3001",
            "声骸系统",
            "Echo",
            "official_derived",
            "Equipable Tacet Discord echo system.",
        ),
        EntitySpec(
            "location:black_shores",
            "location",
            "BinData/area.json",
            "103",
            "黑海岸",
            "Black Shores",
            "official_authoritative",
            "Secretive island organization.",
        ),
        *extra_entities,
    ]
    aliases = [
        AliasSpec("faction:midnight_rangers", "夜归军", "zh", "short_alias", "official_derived"),
        AliasSpec("resonator:camellya", "椿", "zh", "canonical", "official_authoritative"),
        AliasSpec("resonator:encore", "Encore", "en", "canonical", "official_authoritative"),
        AliasSpec("mechanic:echo_system", "Echo", "en", "canonical", "official_derived"),
    ]
    relations = [
        RelationSpec(
            "resonator:camellya",
            "belongs_to",
            "location:black_shores",
            "BinData/role/camellya.json",
            "Role_1603_Faction",
        ),
    ]
    create_knowledge_database(
        kb_path,
        entities=entities,
        aliases=aliases,
        contexts=[],
        relations=relations,
        terms_db_revision=terms_db_revision,
        source_provenance=_test_provenance(commit=commit, game_version=game_version),
    )


def _enable_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")


def _mock_llm_app(
    tmp_path: Path,
    terms_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    knowledge_db: Path | None = None,
    captured_payloads: list[dict] | None = None,
    response_fn=None,
):
    _enable_llm(monkeypatch)
    seen = captured_payloads if captured_payloads is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        user_msg = payload["messages"][1]["content"]
        content = response_fn(payload) if response_fn is not None else user_msg
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    settings = build_settings(
        tmp_path,
        terms_db,
        knowledge_db_path=knowledge_db,
        max_body_bytes=64 * 1024,
    )
    store = DeviceStore(settings.device_db_path)
    store.initialize()
    translator = SentenceTranslator(
        terms_db,
        en_zh_protocol=True,
        llm_transport=httpx.MockTransport(handler),
        knowledge_db_path=knowledge_db,
    )
    app = create_app(settings, device_store=store, translator=translator)
    return app, store, seen


def test_settings_knowledge_db_path_env_parsing(monkeypatch, tmp_path):
    monkeypatch.delenv("WUWATERM_API_KNOWLEDGE_DB_PATH", raising=False)
    assert ApiSettings.from_env().knowledge_db_path is None

    monkeypatch.setenv("WUWATERM_API_KNOWLEDGE_DB_PATH", "   ")
    assert ApiSettings.from_env().knowledge_db_path is None

    target = tmp_path / "data" / "knowledge.db"
    monkeypatch.setenv("WUWATERM_API_KNOWLEDGE_DB_PATH", f"  {target}  ")
    assert ApiSettings.from_env().knowledge_db_path == target


@pytest.mark.parametrize("failure_kind", ["missing", "corrupt", "mismatch"])
def test_api_startup_degrades_safely_without_leaking_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
):
    disable_llm(monkeypatch)
    secret_dir = tmp_path / "private_operator_vault_9912"
    secret_dir.mkdir()
    terms_db = secret_dir / "terms.db"
    _create_terms_db(terms_db)
    kb_path = secret_dir / "knowledge.db"

    if failure_kind == "corrupt":
        kb_path.write_bytes(b"not-a-valid-sqlite-database")
    elif failure_kind == "mismatch":
        _create_knowledge_db(kb_path, terms_db_revision="0" * 64)

    settings = build_settings(tmp_path, terms_db, knowledge_db_path=kb_path)
    store = DeviceStore(settings.device_db_path)
    store.initialize()
    _, meta_token = issue_device(store, "meta reader", [SCOPE_META])

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="wuwaterm_api"):
        app = create_app(settings, device_store=store)

    assert any(
        "degrading to dictionary-only mode" in rec.message for rec in caplog.records
    )
    assert all("private_operator_vault_9912" not in rec.message for rec in caplog.records)
    assert all(str(kb_path) not in rec.message for rec in caplog.records)

    # API stays healthy and ready, capability is omitted, and /v1/knowledge/query fails closed.
    ready = run(call(app, "GET", "/readyz"))
    assert ready.status_code == 200

    caps = run(call(app, "GET", "/v1/capabilities", headers=bearer(meta_token)))
    assert caps.status_code == 200
    assert CAPABILITY_KNOWLEDGE_RETRIEVAL not in caps.json()["capabilities"]

    query_resp = run(
        call(
            app,
            "POST",
            "/v1/knowledge/query",
            json={"query": "夜归冷凝"},
            headers=bearer(meta_token),
        )
    )
    assert query_resp.status_code == 503
    body = query_resp.json()
    assert body["error"]["code"] == ERROR_KNOWLEDGE_UNAVAILABLE
    assert "private_operator_vault_9912" not in query_resp.text
    assert str(kb_path) not in query_resp.text


def test_capabilities_meta_and_knowledge_query_dynamic_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    disable_llm(monkeypatch)
    terms_db = tmp_path / "terms.db"
    kb_path = tmp_path / "knowledge.db"
    rev1 = _create_terms_db(terms_db, commit="commit-v1", game_version="2.4")
    _create_knowledge_db(kb_path, terms_db_revision=rev1, commit="commit-v1", game_version="2.4")

    settings = build_settings(tmp_path, terms_db, knowledge_db_path=kb_path)
    store = DeviceStore(settings.device_db_path)
    store.initialize()
    _, meta_token = issue_device(store, "meta reader", [SCOPE_META])
    _, translate_only_token = issue_device(store, "translator", [SCOPE_TRANSLATE])

    app = create_app(settings, device_store=store)

    # Zero-argument service_capabilities stays backward compatible (no KB advertised).
    assert CAPABILITY_KNOWLEDGE_RETRIEVAL not in service_capabilities()
    assert CAPABILITY_KNOWLEDGE_RETRIEVAL in service_capabilities(
        knowledge_retrieval_available=True
    )

    # Default /v1/meta keeps exact 11 keys even when knowledge.db is mounted.
    default_meta = run(call(app, "GET", "/v1/meta", headers=bearer(meta_token)))
    assert default_meta.status_code == 200
    assert set(default_meta.json()) == _META_EXACT_11_KEYS

    # /v1/capabilities and opt-in /v1/meta both advertise knowledge-retrieval-v1.
    caps_resp = run(call(app, "GET", "/v1/capabilities", headers=bearer(meta_token)))
    assert caps_resp.status_code == 200
    caps_list = caps_resp.json()["capabilities"]
    assert CAPABILITY_KNOWLEDGE_RETRIEVAL in caps_list
    assert len(caps_list) <= 32
    assert all(re.fullmatch(r"^[a-z0-9][a-z0-9.-]{0,63}$", item) for item in caps_list)

    optin_meta = run(
        call(
            app,
            "GET",
            "/v1/meta",
            headers={**bearer(meta_token), "X-WuwaTerm-Meta-Features": "capabilities-v1"},
        )
    )
    assert optin_meta.status_code == 200
    assert optin_meta.json()["capabilities"] == caps_list

    # Scope enforcement: translate-only token is rejected with 403.
    forbidden = run(
        call(
            app,
            "POST",
            "/v1/knowledge/query",
            json={"query": "夜归"},
            headers=bearer(translate_only_token),
        )
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"

    # Successful lexical knowledge query + category filter + strict lock_eligible=False.
    ok_resp = run(
        call(
            app,
            "POST",
            "/v1/knowledge/query",
            json={"query": "夜归冷凝", "top_k": 5, "mode": "lexical"},
            headers=bearer(meta_token),
        )
    )
    assert ok_resp.status_code == 200, ok_resp.text
    ok_body = ok_resp.json()
    assert set(ok_body) == {"request_id", "knowledge_revision", "mode_used", "hits"}
    assert re.fullmatch(r"^[0-9a-f]{64}$", ok_body["knowledge_revision"])
    assert ok_body["mode_used"] == "lexical"
    assert len(ok_body["hits"]) >= 2
    assert {hit["en"] for hit in ok_body["hits"]} >= {"Glacio", "Midnight Rangers"}
    for hit in ok_body["hits"]:
        assert set(hit) == {
            "entity_id",
            "source_surface",
            "zh",
            "en",
            "entity_type",
            "trust_tier",
            "retrieval_channel",
            "score",
            "context_gloss",
            "lock_eligible",
        }
        assert hit["lock_eligible"] is False

    filtered_resp = run(
        call(
            app,
            "POST",
            "/v1/knowledge/query",
            json={"query": "夜归冷凝", "top_k": 5, "categories": ["element"]},
            headers=bearer(meta_token),
        )
    )
    assert filtered_resp.status_code == 200
    filtered_hits = filtered_resp.json()["hits"]
    assert len(filtered_hits) == 1
    assert filtered_hits[0]["entity_type"] == "element"
    assert filtered_hits[0]["en"] == "Glacio"

    # Hybrid mode fails closed with 400 invalid_request while hybrid-rag-v1 is absent.
    hybrid_resp = run(
        call(
            app,
            "POST",
            "/v1/knowledge/query",
            json={"query": "夜归", "mode": "hybrid"},
            headers=bearer(meta_token),
        )
    )
    assert hybrid_resp.status_code == 400
    assert hybrid_resp.json()["error"]["code"] == ERROR_INVALID_REQUEST

    # Simulate terms.db updating to rev2 while knowledge.db still points to rev1:
    # capability must be withdrawn automatically and query must fail closed with 503.
    rev2_tmp = tmp_path / "terms.v2.db"
    rev2 = _create_terms_db(
        rev2_tmp,
        extra_records=(TermRecord("weapon", "fixture", "201", "Wpn_201", "岁华", "Ages of Harvest"),),
        commit="commit-v2",
        game_version="2.5",
    )
    assert rev2 != rev1
    os.replace(rev2_tmp, terms_db)

    caps_drift = run(call(app, "GET", "/v1/capabilities", headers=bearer(meta_token)))
    assert caps_drift.status_code == 200
    assert CAPABILITY_KNOWLEDGE_RETRIEVAL not in caps_drift.json()["capabilities"]

    meta_drift = run(
        call(
            app,
            "GET",
            "/v1/meta",
            headers={**bearer(meta_token), "X-WuwaTerm-Meta-Features": "capabilities-v1"},
        )
    )
    assert CAPABILITY_KNOWLEDGE_RETRIEVAL not in meta_drift.json()["capabilities"]

    query_drift = run(
        call(
            app,
            "POST",
            "/v1/knowledge/query",
            json={"query": "夜归"},
            headers=bearer(meta_token),
        )
    )
    assert query_drift.status_code == 503
    assert query_drift.json()["error"]["code"] == ERROR_KNOWLEDGE_UNAVAILABLE

    # Updating knowledge.db to match rev2 restores knowledge-retrieval-v1 automatically.
    _create_knowledge_db(
        kb_path,
        terms_db_revision=rev2,
        commit="commit-v2",
        game_version="2.5",
        extra_entities=(
            EntitySpec(
                "location:rinascita",
                "location",
                "BinData/area.json",
                "204",
                "黎那汐塔",
                "Rinascita",
                "official_authoritative",
                "Island nation of Resonators.",
            ),
        ),
    )
    caps_restored = run(call(app, "GET", "/v1/capabilities", headers=bearer(meta_token)))
    assert CAPABILITY_KNOWLEDGE_RETRIEVAL in caps_restored.json()["capabilities"]

    query_restored = run(
        call(
            app,
            "POST",
            "/v1/knowledge/query",
            json={"query": "黎那汐塔"},
            headers=bearer(meta_token),
        )
    )
    assert query_restored.status_code == 200
    restored_body = query_restored.json()
    assert restored_body["knowledge_revision"] != ok_body["knowledge_revision"]
    assert any(hit["en"] == "Rinascita" for hit in restored_body["hits"])

    # Direct application-layer read-only query helper works with Path as well.
    direct = query_domain_knowledge(TermService(terms_db), kb_path, "黎那汐塔", top_k=3)
    assert direct.mode_used == "lexical"
    assert any(hit.en == "Rinascita" and hit.lock_eligible is False for hit in direct.hits)


def test_protected_async_rag_injection_isolation_and_ordinary_word_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    terms_db = tmp_path / "terms.db"
    kb_path = tmp_path / "knowledge.db"
    rev = _create_terms_db(terms_db)
    _create_knowledge_db(kb_path, terms_db_revision=rev)

    # 1. Baseline without knowledge_db_path: prompt and OperationIdentity are byte-for-byte identical.
    base_captured: list[dict] = []
    app_no_kb, store_no_kb, _ = _mock_llm_app(
        tmp_path / "no_kb",
        terms_db,
        monkeypatch,
        knowledge_db=None,
        captured_payloads=base_captured,
    )
    _, token_no_kb = issue_device(store_no_kb, "tg-no-kb")

    resp_no_kb = run(
        call(
            app_no_kb,
            "POST",
            "/v1/translations",
            json={
                "to": "en",
                "protected": {
                    "visible_segments": ["", "夜归", "释放了", "冷凝", "技能。"],
                    "structures": [
                        {"visible_context": ""},
                        {"visible_context": ""},
                        {"visible_context": ""},
                        {"visible_context": ""},
                    ],
                },
            },
            headers=bearer(token_no_kb),
        )
    )
    assert resp_no_kb.status_code == 200, resp_no_kb.text
    assert len(base_captured) == 1
    assert DOMAIN_KNOWLEDGE_HEADER not in base_captured[0]["messages"][0]["content"]

    # Verify plan_translation OperationIdentity is byte-identical when knowledge_db_path=None.
    tr_a = SentenceTranslator(terms_db)
    tr_b = SentenceTranslator(terms_db, knowledge_db_path=None)
    policy_a = tr_a.request_policy(to_chinese=False)
    policy_b = tr_b.request_policy(to_chinese=False)
    _, plan_a = tr_a.plan_translation("夜归释放了冷凝技能。", to_chinese=False)
    _, plan_b = tr_b.plan_translation("夜归释放了冷凝技能。", to_chinese=False)
    assert plan_a is not None and plan_b is not None
    assert tr_a.build_request_spec(plan_a, policy=policy_a).operation_identity(
        policy_a
    ) == tr_b.build_request_spec(plan_b, policy=policy_b).operation_identity(policy_b)

    # 2. Mounted knowledge.db: protected HTML path injects soft KB from visible text only,
    # preserves all structural placeholders, and never exposes raw markup to retrieval.
    kb_captured: list[dict] = []
    app_kb, store_kb, _ = _mock_llm_app(
        tmp_path / "with_kb",
        terms_db,
        monkeypatch,
        knowledge_db=kb_path,
        captured_payloads=kb_captured,
    )
    _, token_kb = issue_device(store_kb, "tg-kb")

    prot_resp = run(
        call(
            app_kb,
            "POST",
            "/v1/translations",
            json={
                "to": "en",
                "protected_text": {
                    "visible_segments": ["", "夜归", "释放了", "冷凝", "技能。"],
                    "structures": [
                        {"original": "<b data-tag='椿'>", "visible_context": ""},
                        {"original": "</b>", "visible_context": ""},
                        {"original": "<i>", "visible_context": ""},
                        {"original": "</i>", "visible_context": ""},
                    ],
                },
            },
            headers=bearer(token_kb),
        )
    )
    assert prot_resp.status_code == 200, prot_resp.text
    prot_body = prot_resp.json()
    assert prot_body["protected_used"] is True
    assert prot_body["visible_segments"] == ["", "夜归", "释放了", "冷凝", "技能。"]
    sys_prompt = kb_captured[-1]["messages"][0]["content"]
    user_prompt = kb_captured[-1]["messages"][1]["content"]
    assert DOMAIN_KNOWLEDGE_HEADER in sys_prompt
    assert "Midnight Rangers" in sys_prompt
    assert "Glacio" in sys_prompt
    # Raw HTML tag text containing '椿' must NEVER reach retrieval or prompt.
    assert "Camellya" not in sys_prompt
    assert "data-tag" not in sys_prompt
    assert "data-tag" not in user_prompt
    # Soft KB never creates term placeholders in Locked terms.
    locked_section = sys_prompt.split("Locked terms:\n", 1)[1].split("\n", 1)[0]
    assert "Glacio" not in locked_section
    assert "Midnight Rangers" not in locked_section

    # 3. Ordinary-word guards inside protected HTML path: 香椿 / encore / echo -> ZERO injection.
    for body_case in (
        {
            "to": "en",
            "protected": {
                "visible_segments": ["今天吃了", "椿", "炒鸡蛋。"],
                "structures": [{"visible_context": "香"}, {"visible_context": ""}],
            },
        },
        {
            "to": "en",
            "protected": {
                "visible_segments": ["今天吃了香", "椿", "炒鸡蛋。"],
                "structures": [{"visible_context": ""}, {"visible_context": ""}],
            },
        },
        {
            "to": "zh",
            "protected": {
                "visible_segments": ["The audience cheered for an ", "encore", " tonight."],
                "structures": [{"visible_context": ""}, {"visible_context": ""}],
            },
        },
        {
            "to": "zh",
            "protected": {
                "visible_segments": ["We heard a faint ", "echo", " in the cave."],
                "structures": [{"visible_context": ""}, {"visible_context": ""}],
            },
        },
    ):
        before_count = len(kb_captured)
        resp = run(
            call(app_kb, "POST", "/v1/translations", json=body_case, headers=bearer(token_kb))
        )
        assert resp.status_code == 200, (body_case, resp.text)
        assert len(kb_captured) == before_count + 1
        prompt = kb_captured[-1]["messages"][0]["content"]
        assert DOMAIN_KNOWLEDGE_HEADER not in prompt, (body_case, prompt)
        assert "Camellya" not in prompt
        assert "安可" not in prompt

    # 4. retrieval_mode="off" disables KB retrieval even when KB is mounted;
    # retrieval_mode="lexical" works when mounted and fails closed when unmounted;
    # retrieval_mode="hybrid" always fails closed with 400 invalid_request.
    off_resp = run(
        call(
            app_kb,
            "POST",
            "/v1/translations",
            json={
                "to": "en",
                "context": {"retrieval_mode": "off"},
                "protected": {
                    "visible_segments": ["", "夜归", "释放了冷凝。"],
                    "structures": [{"visible_context": ""}, {"visible_context": ""}],
                },
            },
            headers=bearer(token_kb),
        )
    )
    assert off_resp.status_code == 200, off_resp.text
    assert DOMAIN_KNOWLEDGE_HEADER not in kb_captured[-1]["messages"][0]["content"]

    lex_resp = run(
        call(
            app_kb,
            "POST",
            "/v1/translations",
            json={"text": "夜归释放了冷凝技能。", "context": {"retrieval_mode": "lexical"}},
            headers=bearer(token_kb),
        )
    )
    assert lex_resp.status_code == 200, lex_resp.text
    assert set(lex_resp.json()) == _TRANSLATION_EXACT_5_KEYS
    assert DOMAIN_KNOWLEDGE_HEADER in kb_captured[-1]["messages"][0]["content"]

    hybrid_rejected = run(
        call(
            app_kb,
            "POST",
            "/v1/translations",
            json={"text": "夜归释放了冷凝技能。", "context": {"retrieval_mode": "hybrid"}},
            headers=bearer(token_kb),
        )
    )
    assert hybrid_rejected.status_code == 400
    assert hybrid_rejected.json()["error"]["code"] == ERROR_INVALID_REQUEST

    # Application-layer retrieval_mode checks for both plain and protected functions.
    service = TermService(terms_db)
    app_captured: list[dict] = []

    def app_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        app_captured.append(payload)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": payload["messages"][1]["content"]}}]}
        )

    tr_no_kb_app = SentenceTranslator(terms_db, llm_transport=httpx.MockTransport(app_handler))
    tr_kb_app = build_translator(terms_db, knowledge_db_path=kb_path)
    tr_kb_app._llm_transport = httpx.MockTransport(app_handler)
    try:
        err_plain = asyncio.run(
            translate_request_async(
                service,
                tr_no_kb_app,
                TranslationJob(text="夜归释放了冷凝技能。"),
                retrieval_mode="lexical",
            )
        )
        assert err_plain.kind == KIND_ERROR
        assert err_plain.error_code == ERROR_INVALID_REQUEST

        err_prot = asyncio.run(
            translate_protected_request_async(
                service,
                tr_no_kb_app,
                ProtectedTranslationJob(
                    visible_segments=("", "夜归", "。"),
                    structures=(ProtectedStructure(""), ProtectedStructure("")),
                ),
                retrieval_mode="lexical",
            )
        )
        assert err_prot.kind == KIND_ERROR
        assert err_prot.error_code == ERROR_INVALID_REQUEST

        ok_prot_off = asyncio.run(
            translate_protected_request_async(
                service,
                tr_kb_app,
                ProtectedTranslationJob(
                    visible_segments=("", "夜归", "释放了冷凝。"),
                    structures=(ProtectedStructure(""), ProtectedStructure("")),
                ),
                retrieval_mode="off",
            )
        )
        assert ok_prot_off.kind == KIND_LLM
        assert DOMAIN_KNOWLEDGE_HEADER not in app_captured[-1]["messages"][0]["content"]

        # Direct translate_protected_async byte-equality check when knowledge_db_path=None.
        protected_obj = ProtectedText(
            ("", "夜归释放了冷凝。", ""),
            (ProtectedStructure("<b>"), ProtectedStructure("</b>")),
        )
        app_captured.clear()
        asyncio.run(tr_no_kb_app.translate_protected_async(protected_obj, to_chinese=False))
        prompt_without_kb = app_captured[-1]["messages"][0]["content"]
        assert DOMAIN_KNOWLEDGE_HEADER not in prompt_without_kb
        asyncio.run(
            tr_kb_app.translate_protected_async(
                protected_obj, to_chinese=False, retrieval_mode="off"
            )
        )
        assert app_captured[-1]["messages"][0]["content"] == prompt_without_kb
        asyncio.run(
            tr_kb_app.translate_protected_async(
                protected_obj, to_chinese=False, retrieval_mode="auto"
            )
        )
        assert DOMAIN_KNOWLEDGE_HEADER in app_captured[-1]["messages"][0]["content"]

        # Direct core-layer check: hit-capable CJK terms inside ProtectedStructure.original
        # must NEVER enter retrieval or prompt even when calling translate_protected_async directly.
        markup_only_hit = ProtectedText(
            ("", "今天天气不错。", ""),
            (
                ProtectedStructure(original="<b data-tag='椿' title='夜归'>", visible_context=""),
                ProtectedStructure(original="</b>", visible_context=""),
            ),
        )
        app_captured.clear()
        asyncio.run(
            tr_kb_app.translate_protected_async(
                markup_only_hit, to_chinese=False, retrieval_mode="auto"
            )
        )
        direct_sys = app_captured[-1]["messages"][0]["content"]
        direct_user = app_captured[-1]["messages"][1]["content"]
        assert DOMAIN_KNOWLEDGE_HEADER not in direct_sys
        assert "Camellya" not in direct_sys
        assert "Midnight Rangers" not in direct_sys
        assert "data-tag" not in direct_sys
        assert "data-tag" not in direct_user
    finally:
        asyncio.run(tr_no_kb_app.aclose())
        asyncio.run(tr_kb_app.aclose())


def test_zero_cost_core_inplace_upgrade_replacement_across_three_client_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Simulate unchanged Web / Windows / Telegram HTTP clients switching from
    Core v1 (terms rev1, no KB) to Core v2 (terms rev2 + matching knowledge.db)
    with a local mock LLM transport (zero external calls).
    """
    # Core v1: terms.db rev1, no knowledge.db.
    v1_dir = tmp_path / "core_v1"
    v1_dir.mkdir()
    terms_v1 = v1_dir / "terms.db"
    rev1 = _create_terms_db(terms_v1, commit="core-v1-sha", game_version="2.4")
    v1_prompts: list[dict] = []
    app_v1, store_v1, _ = _mock_llm_app(
        v1_dir,
        terms_v1,
        monkeypatch,
        knowledge_db=None,
        captured_payloads=v1_prompts,
    )
    _, web_token_v1 = issue_device(store_v1, "web-bff", [SCOPE_META, SCOPE_TRANSLATE])
    _, win_token_v1 = issue_device(store_v1, "windows-client", [SCOPE_META, SCOPE_TRANSLATE])
    _, tg_token_v1 = issue_device(store_v1, "telegram-bot", [SCOPE_META, SCOPE_TRANSLATE])

    # Fixed client request shapes (zero client changes between v1 and v2).
    web_translate_req = {"text": "夜归释放了冷凝技能。", "to": "en"}
    win_translate_req = {
        "text": "夜归释放了冷凝技能。",
        "to": "en",
        "context": {"before": {"source": "黑海岸简报", "target": "Black Shores Briefing"}},
    }
    tg_protected_req = {
        "to": "en",
        "protected": {
            "visible_segments": ["", "夜归", "在", "黑海岸", "释放了冷凝。"],
            "structures": [
                {"visible_context": ""},
                {"visible_context": ""},
                {"visible_context": ""},
                {"visible_context": ""},
            ],
        },
    }

    # --- Phase 1: clients talk to Core v1 ---
    v1_meta = run(call(app_v1, "GET", "/v1/meta", headers=bearer(web_token_v1)))
    assert v1_meta.status_code == 200
    assert set(v1_meta.json()) == _META_EXACT_11_KEYS

    v1_dict = run(call(app_v1, "GET", "/v1/dictionary", headers=bearer(win_token_v1)))
    assert v1_dict.status_code == 200
    assert v1_dict.json()["revision"] == rev1

    v1_caps = run(call(app_v1, "GET", "/v1/capabilities", headers=bearer(win_token_v1)))
    assert CAPABILITY_KNOWLEDGE_RETRIEVAL not in v1_caps.json()["capabilities"]

    v1_kb = run(
        call(
            app_v1,
            "POST",
            "/v1/knowledge/query",
            json={"query": "夜归冷凝"},
            headers=bearer(win_token_v1),
        )
    )
    assert v1_kb.status_code == 503
    assert v1_kb.json()["error"]["code"] == ERROR_KNOWLEDGE_UNAVAILABLE

    v1_web_tr = run(
        call(app_v1, "POST", "/v1/translations", json=web_translate_req, headers=bearer(web_token_v1))
    )
    assert v1_web_tr.status_code == 200
    assert set(v1_web_tr.json()) == _TRANSLATION_EXACT_5_KEYS

    v1_win_tr = run(
        call(app_v1, "POST", "/v1/translations", json=win_translate_req, headers=bearer(win_token_v1))
    )
    assert v1_win_tr.status_code == 200
    assert set(v1_win_tr.json()) == _TRANSLATION_EXACT_5_KEYS

    v1_tg_tr = run(
        call(app_v1, "POST", "/v1/translations", json=tg_protected_req, headers=bearer(tg_token_v1))
    )
    assert v1_tg_tr.status_code == 200
    assert v1_tg_tr.json()["protected_used"] is True
    assert all(
        DOMAIN_KNOWLEDGE_HEADER not in item["messages"][0]["content"] for item in v1_prompts
    )

    # --- Phase 2: Core upgraded in-place to v2 (terms rev2 + matching knowledge.db) ---
    v2_dir = tmp_path / "core_v2"
    v2_dir.mkdir()
    terms_v2 = v2_dir / "terms.db"
    kb_v2 = v2_dir / "knowledge.db"
    rev2 = _create_terms_db(
        terms_v2,
        extra_records=(TermRecord("location", "fixture", "205", "Area_205", "拉古那", "Ragunna"),),
        commit="core-v2-sha",
        game_version="2.5",
    )
    assert rev2 != rev1
    _create_knowledge_db(kb_v2, terms_db_revision=rev2, commit="core-v2-sha", game_version="2.5")

    v2_prompts: list[dict] = []
    app_v2, store_v2, _ = _mock_llm_app(
        v2_dir,
        terms_v2,
        monkeypatch,
        knowledge_db=kb_v2,
        captured_payloads=v2_prompts,
    )
    _, web_token_v2 = issue_device(store_v2, "web-bff", [SCOPE_META, SCOPE_TRANSLATE])
    _, win_token_v2 = issue_device(store_v2, "windows-client", [SCOPE_META, SCOPE_TRANSLATE])
    _, tg_token_v2 = issue_device(store_v2, "telegram-bot", [SCOPE_META, SCOPE_TRANSLATE])

    # Web default /v1/meta keeps exact 11 keys and surfaces new game_version.
    v2_meta = run(call(app_v2, "GET", "/v1/meta", headers=bearer(web_token_v2)))
    assert v2_meta.status_code == 200
    assert set(v2_meta.json()) == _META_EXACT_11_KEYS
    assert v2_meta.json()["source_game_version"] == "2.5"

    # Windows observes new dictionary_revision + knowledge-retrieval-v1 automatically.
    v2_dict = run(call(app_v2, "GET", "/v1/dictionary", headers=bearer(win_token_v2)))
    assert v2_dict.status_code == 200
    assert v2_dict.json()["revision"] == rev2
    assert v2_dict.json()["revision"] != rev1

    v2_caps = run(call(app_v2, "GET", "/v1/capabilities", headers=bearer(win_token_v2)))
    assert CAPABILITY_KNOWLEDGE_RETRIEVAL in v2_caps.json()["capabilities"]

    v2_kb = run(
        call(
            app_v2,
            "POST",
            "/v1/knowledge/query",
            json={"query": "夜归冷凝"},
            headers=bearer(win_token_v2),
        )
    )
    assert v2_kb.status_code == 200
    assert any(hit["en"] == "Midnight Rangers" for hit in v2_kb.json()["hits"])
    assert all(hit["lock_eligible"] is False for hit in v2_kb.json()["hits"])

    # Exact same unchanged Web / Windows / Telegram translation requests now automatically
    # receive soft RAG background in plain, context, and protected-HTML flows.
    v2_web_tr = run(
        call(app_v2, "POST", "/v1/translations", json=web_translate_req, headers=bearer(web_token_v2))
    )
    assert v2_web_tr.status_code == 200
    assert set(v2_web_tr.json()) == _TRANSLATION_EXACT_5_KEYS

    v2_win_tr = run(
        call(app_v2, "POST", "/v1/translations", json=win_translate_req, headers=bearer(win_token_v2))
    )
    assert v2_win_tr.status_code == 200
    assert set(v2_win_tr.json()) == _TRANSLATION_EXACT_5_KEYS

    v2_tg_tr = run(
        call(app_v2, "POST", "/v1/translations", json=tg_protected_req, headers=bearer(tg_token_v2))
    )
    assert v2_tg_tr.status_code == 200
    assert v2_tg_tr.json()["protected_used"] is True
    assert len(v2_prompts) == 3
    for idx, prompt_payload in enumerate(v2_prompts):
        sys_text = prompt_payload["messages"][0]["content"]
        assert DOMAIN_KNOWLEDGE_HEADER in sys_text, f"client #{idx} missing KB block"
        assert "Midnight Rangers" in sys_text
        assert "Glacio" in sys_text
