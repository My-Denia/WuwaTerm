"""HTTP adapter tests: contract, credentials, limits and budgets.

Everything runs in-process against the ASGI app (no socket, no uvicorn), in the
same duck-typed style as the bot tests.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from wuwaterm.application import ERROR_INPUT_TOO_LONG
from wuwaterm.db import connect, insert_records
from wuwaterm.models import TermRecord
from wuwaterm.review import MAX_SIDE_SCALARS
from wuwaterm.translation_policy import LLM_INPUT_CHAR_LIMIT
from wuwaterm_api import TERM_QUERY_MAX_LENGTH
from wuwaterm_api.app import create_app
from wuwaterm_api.auth import (
    MIN_SECRET_LENGTH,
    SCOPE_META,
    SCOPE_TRANSLATE,
    TOKEN_SCHEME,
    DeviceStore,
    DeviceStoreError,
    parse_token,
)
from wuwaterm_api.errors import MESSAGE_BY_CODE, STATUS_BY_CODE
from wuwaterm_api.settings import ApiConfigError, ApiSettings

ROOT = Path(__file__).resolve().parents[1]


def build_settings(tmp_path: Path, db_path: Path, **overrides) -> ApiSettings:
    defaults = dict(
        db_path=db_path,
        device_db_path=tmp_path / "api-state" / "devices.db",
        rate_limit_per_minute=100,
        llm_calls_per_minute=100,
        llm_max_concurrency=2,
        max_body_bytes=2048,
        request_timeout_seconds=30.0,
    )
    defaults.update(overrides)
    return ApiSettings(**defaults)


def build_client_app(tmp_path: Path, db_path: Path, **overrides):
    settings = build_settings(tmp_path, db_path, **overrides)
    store = DeviceStore(settings.device_db_path)
    store.initialize()
    app = create_app(settings, device_store=store)
    return app, store



# The service never mints a secret; the operator supplies one. Tests do the
# same, so what they exercise is the real registration path.
TEST_SECRET_BASE = "unguessable-material-for-tests-0123456789abcdef"


def issue_device(store, name: str, scopes=None, secret: str | None = None):
    material = (
        secret
        if secret is not None
        else f"{TEST_SECRET_BASE}-{name.replace(' ', '-')}"
    )
    device = store.issue(name, scopes, secret=material)
    return device, f"{TOKEN_SCHEME}.{device.device_id}.{material}"


def run(coro):
    return asyncio.run(coro)


async def call(app, method: str, url: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        return await client.request(method, url, **kwargs)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def enable_mock_llm(monkeypatch, calls, response_factory) -> None:
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def fake_call(
        locked_text,
        locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=30.0,
        transport=None,
    ):
        calls.append((locked_text, locks))
        return await response_factory(locked_text, locks)

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)


def disable_llm(monkeypatch) -> None:
    for name in (
        "WUWATERM_OPENAI_BASE_URL",
        "WUWATERM_OPENAI_API_KEY",
        "WUWATERM_OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


def test_healthz_needs_no_credential(tmp_path, sample_db):
    app, _ = build_client_app(tmp_path, sample_db)

    response = run(call(app, "GET", "/healthz"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_dictionary_readability(tmp_path, sample_db):
    app, _ = build_client_app(tmp_path, sample_db)
    broken_app, _ = build_client_app(
        tmp_path / "broken", tmp_path / "missing" / "terms.db"
    )

    ok = run(call(app, "GET", "/readyz"))
    bad = run(call(broken_app, "GET", "/readyz"))

    assert ok.status_code == 200
    assert ok.json()["status"] == "ready"
    assert bad.status_code == 503
    assert bad.json()["error"]["code"] == "internal"


# --------------------------------------------------------------------------
# Device credentials
# --------------------------------------------------------------------------


def test_translation_requires_a_credential(tmp_path, sample_db):
    app, _ = build_client_app(tmp_path, sample_db)

    response = run(call(app, "POST", "/v1/translations", json={"text": "声骸"}))

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert set(body) == {"error", "request_id"}


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": "Bearer wtd1.deadbeef.wrongsecret"},
        {"Authorization": "Token wtd1.a.b"},
        {"Authorization": ""},
    ],
)
def test_bad_credentials_are_indistinguishable(tmp_path, sample_db, header):
    app, store = build_client_app(tmp_path, sample_db)
    issue_device(store, "owner desktop")

    response = run(
        call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=header)
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_revoked_device_is_rejected(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    device, token = issue_device(store, "owner desktop")

    before = run(
        call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=bearer(token))
    )
    store.revoke(device.device_id)
    after = run(
        call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=bearer(token))
    )

    assert before.status_code == 200
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "unauthorized"


def test_scope_is_enforced(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, meta_only = issue_device(store, "reader", [SCOPE_META])
    _, translate_only = issue_device(store, "translator", [SCOPE_TRANSLATE])

    forbidden = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "声骸"},
            headers=bearer(meta_only),
        )
    )
    also_forbidden = run(call(app, "GET", "/v1/meta", headers=bearer(translate_only)))
    allowed = run(call(app, "GET", "/v1/meta", headers=bearer(meta_only)))

    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"
    assert also_forbidden.status_code == 403
    assert allowed.status_code == 200


def test_only_a_hash_of_the_secret_is_stored(tmp_path):
    store = DeviceStore(tmp_path / "devices.db")
    device, token = issue_device(store, "owner desktop")
    parsed = parse_token(token)

    assert parsed is not None
    assert parsed[0] == device.device_id
    raw_secret = parsed[1]
    on_disk = (tmp_path / "devices.db").read_bytes()
    assert raw_secret.encode("utf-8") not in on_disk
    assert token.encode("utf-8") not in on_disk
    # listing never reveals credential material
    listed = store.list_devices()
    assert [item.device_id for item in listed] == [device.device_id]
    assert raw_secret not in json.dumps([item.__dict__ for item in listed], default=str)


def test_the_store_never_returns_credential_material(tmp_path):
    """issue() hands back a Device and nothing else, by construction."""
    store = DeviceStore(tmp_path / "devices.db")

    result = store.issue("owner desktop", None, secret="x" * MIN_SECRET_LENGTH)

    assert not isinstance(result, tuple)
    assert "x" * MIN_SECRET_LENGTH not in json.dumps(result.__dict__, default=str)


def test_a_weak_supplied_secret_is_refused(tmp_path):
    store = DeviceStore(tmp_path / "devices.db")

    with pytest.raises(DeviceStoreError):
        store.issue("owner desktop", None, secret="short")
    with pytest.raises(DeviceStoreError):
        store.issue("owner desktop", None, secret="   ")
    assert store.list_devices() == []


def test_unknown_scope_is_refused(tmp_path):
    store = DeviceStore(tmp_path / "devices.db")

    with pytest.raises(DeviceStoreError):
        issue_device(store, "owner desktop", ["translate", "root"])


def test_revoking_an_unknown_device_fails(tmp_path):
    store = DeviceStore(tmp_path / "devices.db")

    with pytest.raises(DeviceStoreError):
        store.revoke("does-not-exist")


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------


def test_rate_limit_returns_the_stable_code(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db, rate_limit_per_minute=2)
    _, token = issue_device(store, "owner desktop")

    statuses = [
        run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers=bearer(token),
            )
        )
        for _ in range(3)
    ]

    assert [item.status_code for item in statuses] == [200, 200, 429]
    assert statuses[-1].json()["error"]["code"] == "rate_limited"


def test_rate_limit_is_per_device(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db, rate_limit_per_minute=1)
    _, first = issue_device(store, "device one")
    _, second = issue_device(store, "device two")

    run(call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=bearer(first)))
    blocked = run(
        call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=bearer(first))
    )
    other = run(
        call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=bearer(second))
    )

    assert blocked.status_code == 429
    assert other.status_code == 200


def test_oversized_body_is_refused_before_parsing(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db, max_body_bytes=256)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "x" * 4000},
            headers=bearer(token),
        )
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_translation_limit_keeps_stable_input_too_long_error(
    monkeypatch, tmp_path, sample_db
):
    app, store = build_client_app(tmp_path, sample_db, max_body_bytes=64 * 1024)
    _, token = issue_device(store, "owner desktop")
    disable_llm(monkeypatch)

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "中" * (LLM_INPUT_CHAR_LIMIT + 1)},
            headers=bearer(token),
        )
    )

    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": ERROR_INPUT_TOO_LONG,
        "message": MESSAGE_BY_CODE[ERROR_INPUT_TOO_LONG],
    }


def test_request_timeout_returns_the_envelope(monkeypatch, tmp_path, sample_db):
    app, store = build_client_app(
        tmp_path, sample_db, request_timeout_seconds=0.05, llm_max_concurrency=1
    )
    _, token = issue_device(store, "owner desktop")
    calls: list[tuple[str, object]] = []

    async def slow(locked_text, locks):
        await asyncio.sleep(5)
        return locked_text

    enable_mock_llm(monkeypatch, calls, slow)

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "需要翻译的一个句子"},
            headers=bearer(token),
        )
    )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "internal"


def test_invalid_payload_returns_invalid_request(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    missing_text = run(
        call(app, "POST", "/v1/translations", json={}, headers=bearer(token))
    )
    bad_direction = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "声骸", "to": "fr"},
            headers=bearer(token),
        )
    )

    assert missing_text.status_code == 400
    assert missing_text.json()["error"]["code"] == "invalid_request"
    assert bad_direction.status_code == 400
    assert bad_direction.json()["error"]["code"] == "invalid_request"
    # the rejected text is never echoed back
    assert "声骸" not in bad_direction.text


# --------------------------------------------------------------------------
# Translation paths
# --------------------------------------------------------------------------


def test_exact_dictionary_hit_uses_no_model_call(monkeypatch, tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    calls: list[tuple[str, object]] = []

    async def unused(locked_text, locks):  # pragma: no cover - must not run
        raise AssertionError("dictionary hit must not call the model")

    enable_mock_llm(monkeypatch, calls, unused)

    response = run(
        call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=bearer(token))
    )

    body = response.json()
    assert response.status_code == 200
    assert body["kind"] == "exact"
    assert body["text"] == "Echo"
    assert body["direction"] == "en"
    assert body["dictionary_miss"] is False
    assert body["request_id"]
    assert not calls


def test_forced_direction_is_honored(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "Echo", "to": "zh"},
            headers=bearer(token),
        )
    )

    body = response.json()
    assert body["direction"] == "zh"
    assert body["text"] == "声骸"


def test_fuzzy_hit_is_reported_as_fuzzy(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "shenghai"},
            headers=bearer(token),
        )
    )

    assert response.json()["kind"] == "fuzzy"


def test_model_path_reports_kind_and_dictionary_miss(monkeypatch, tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    calls: list[tuple[str, object]] = []

    async def answer(locked_text, locks):
        return "widget"

    enable_mock_llm(monkeypatch, calls, answer)

    response = run(
        call(
            app, "POST", "/v1/translations", json={"text": "foobar"}, headers=bearer(token)
        )
    )

    body = response.json()
    assert body["kind"] == "llm"
    assert body["text"] == "widget"
    assert body["dictionary_miss"] is True
    assert len(calls) == 1


def test_model_failure_maps_to_llm_unavailable(monkeypatch, tmp_path, sample_db):
    from wuwaterm.sentence import (
        TRANSLATION_UNAVAILABLE_NOTICE,
        LLMTranslationError,
    )

    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    calls: list[tuple[str, object]] = []

    async def boom(locked_text, locks):
        raise LLMTranslationError(TRANSLATION_UNAVAILABLE_NOTICE, reason="upstream")

    enable_mock_llm(monkeypatch, calls, boom)

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "需要翻译的一个句子"},
            headers=bearer(token),
        )
    )

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "llm_unavailable"
    # the Telegram-worded notice never reaches the HTTP contract
    assert TRANSLATION_UNAVAILABLE_NOTICE not in response.text


def test_request_id_is_always_server_generated(tmp_path, sample_db, caplog):
    """The correlation id is minted server-side; an inbound X-Request-Id is
    ignored, never echoed and never logged.

    Trusting the header let a caller put its own token (which fits the shape
    wtd1.<id>.<secret>) into the id, which was then written to the auth-reject
    log and the error envelope. AC16 forbids a raw credential reaching logs or
    telemetry, so nothing the caller sends may become the id.
    """
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    generated = run(
        call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=bearer(token))
    )
    # Shaped exactly like a device token, and matched the old accept charset.
    token_shaped = "wtd1.deadbeef.%s" % ("s" * 40)
    with caplog.at_level("INFO", logger="wuwaterm_api"):
        supplied = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers={**bearer(token), "X-Request-Id": token_shaped},
            )
        )

    # The server's generated id is what is returned and echoed in the header.
    assert generated.headers["X-Request-Id"] == generated.json()["request_id"]
    assert supplied.headers["X-Request-Id"] == supplied.json()["request_id"]
    # The client's value did not become the id, on the wire or in a log.
    assert supplied.json()["request_id"] != token_shaped
    assert token_shaped not in supplied.text
    assert token_shaped not in caplog.text


# --------------------------------------------------------------------------
# Dictionary and metadata
# --------------------------------------------------------------------------


def test_terms_returns_official_strings(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    hit = run(call(app, "GET", "/v1/terms", params={"q": "声骸"}, headers=bearer(token)))
    miss = run(
        call(app, "GET", "/v1/terms", params={"q": "not-a-term"}, headers=bearer(token))
    )
    empty = run(call(app, "GET", "/v1/terms", params={"q": "  "}, headers=bearer(token)))

    assert hit.status_code == 200
    assert hit.json()["matches"][0]["en"] == "Echo"
    assert miss.json()["matches"] == []
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "invalid_request"


def test_terms_returns_backend_ranked_pinyin_match(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(app, "GET", "/v1/terms", params={"q": "jinxi"}, headers=bearer(token))
    )

    assert response.status_code == 200
    assert response.json()["query"] == "jinxi"
    assert response.json()["matches"] == [
        {
            "zh": "今汐",
            "en": "Jinhsi",
            "category": "resonator",
            "score": 100.0,
            "reason": "pinyin",
        }
    ]
    assert response.headers["X-Request-Id"] == response.json()["request_id"]


def test_term_query_limit_keeps_stable_invalid_request_error(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "GET",
            "/v1/terms",
            params={"q": "x" * (TERM_QUERY_MAX_LENGTH + 1)},
            headers=bearer(token),
        )
    )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "invalid_request",
        "message": "query parameter q is too long",
    }


def test_schema_only_limits_do_not_preempt_authentication(tmp_path, sample_db):
    app, _store = build_client_app(tmp_path, sample_db, max_body_bytes=64 * 1024)

    translation = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "中" * (LLM_INPUT_CHAR_LIMIT + 1)},
        )
    )
    terms = run(
        call(
            app,
            "GET",
            "/v1/terms",
            params={"q": "x" * (TERM_QUERY_MAX_LENGTH + 1)},
        )
    )

    assert translation.status_code == 401
    assert translation.json()["error"]["code"] == "unauthorized"
    assert terms.status_code == 401
    assert terms.json()["error"]["code"] == "unauthorized"


def test_schema_only_limits_preserve_preparation_and_strip_order(
    tmp_path, sample_db
):
    app, store = build_client_app(tmp_path, sample_db, max_body_bytes=64 * 1024)
    _, translate_token = issue_device(store, "owner translate")
    _, meta_token = issue_device(store, "owner meta", [SCOPE_META])
    source = "[spoiler]\n" * (LLM_INPUT_CHAR_LIMIT // len("[spoiler]\n") + 1)
    source += "声骸"
    padded_query = f"  {'x' * TERM_QUERY_MAX_LENGTH}  "

    assert len(source) > LLM_INPUT_CHAR_LIMIT
    assert len(padded_query) > TERM_QUERY_MAX_LENGTH
    translation = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": source},
            headers=bearer(translate_token),
        )
    )
    terms = run(
        call(
            app,
            "GET",
            "/v1/terms",
            params={"q": padded_query},
            headers=bearer(meta_token),
        )
    )

    assert translation.status_code == 200
    assert translation.json()["kind"] == "exact"
    assert translation.json()["text"] == "Echo"
    assert terms.status_code == 200
    assert terms.json()["query"] == "x" * TERM_QUERY_MAX_LENGTH
    assert terms.json()["matches"] == []


def test_terms_preserves_backend_category_order_and_limit(tmp_path, sample_db):
    rows = [
        ("core_term", "Shared Official"),
        ("resonator", "Shared Official"),
        ("weapon", "Weapon Official"),
        ("echo", "Echo Official"),
        ("skill", "Skill Official"),
        ("location", "Location Official"),
        ("item", "Item Official"),
        ("speaker", "Speaker Official"),
    ]
    with connect(sample_db) as conn:
        insert_records(
            conn,
            [
                TermRecord(
                    category=category,
                    source_file=f"{category}.json",
                    source_id=str(index),
                    text_key=f"Ambiguous_{index}",
                    zh="多义测试词",
                    en=english,
                )
                for index, (category, english) in enumerate(rows)
            ],
        )
        conn.commit()
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "GET",
            "/v1/terms",
            params={"q": "多义测试词"},
            headers=bearer(token),
        )
    )

    assert response.status_code == 200
    assert [
        (match["category"], match["en"], match["score"], match["reason"])
        for match in response.json()["matches"]
    ] == [
        ("core_term", "Shared Official", 100.0, "exact"),
        ("resonator", "Shared Official", 100.0, "exact"),
        ("weapon", "Weapon Official", 100.0, "exact"),
        ("echo", "Echo Official", 100.0, "exact"),
        ("skill", "Skill Official", 100.0, "exact"),
    ]
    assert response.headers["X-Request-Id"] == response.json()["request_id"]


def test_meta_exposes_provenance_without_paths_or_secrets(
    monkeypatch, tmp_path, sample_db
):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    disable_llm(monkeypatch)

    response = run(call(app, "GET", "/v1/meta", headers=bearer(token)))

    body = response.json()
    assert body["term_count"] > 0
    assert body["source_profile"] == "dimbreath_legacy"
    assert body["api_version"] == "v1"
    assert body["llm_configured"] is False
    assert body["source_game_version"] == "fixture-unavailable"
    assert body["source_resource_version"] == "fixture-unavailable"
    assert body["source_changelist"] == "fixture-unavailable"
    text = response.text
    assert "3.7.0" not in text
    assert "3.7.8" not in text
    assert "8975829" not in text
    assert str(sample_db) not in text
    assert "terms.db" not in text
    assert "devices.db" not in text
    assert token not in text


def test_meta_versions_stay_null_when_the_database_did_not_record_them(
    monkeypatch, tmp_path, sample_db
):
    """An arikatsu profile name must not backfill versions the DB never stored."""
    bare = tmp_path / "bare.db"
    bare.write_bytes(sample_db.read_bytes())
    with connect(bare) as conn:
        conn.execute(
            """
            DELETE FROM metadata
            WHERE key IN (
                'source_game_version',
                'source_resource_version',
                'source_changelist'
            )
            """
        )
        conn.execute(
            "UPDATE metadata SET value = 'arikatsu' WHERE key = 'source_profile'"
        )

    app, store = build_client_app(tmp_path, bare)
    _, token = issue_device(store, "owner desktop")
    disable_llm(monkeypatch)

    response = run(call(app, "GET", "/v1/meta", headers=bearer(token)))

    body = response.json()
    assert response.status_code == 200
    assert body["source_profile"] == "arikatsu"
    assert body["source_game_version"] is None
    assert body["source_resource_version"] is None
    assert body["source_changelist"] is None
    text = response.text
    assert "3.7.0" not in text
    assert "3.7.8" not in text
    assert "8975829" not in text
    assert "9218d612ad815e398e064e577e42aaf878899968" not in text


def test_meta_versions_round_trip_the_strings_stored_in_the_open_database(
    monkeypatch, tmp_path, sample_db
):
    recorded = tmp_path / "recorded.db"
    recorded.write_bytes(sample_db.read_bytes())
    with connect(recorded) as conn:
        stored_commit = conn.execute(
            "SELECT value FROM metadata WHERE key = 'source_commit'"
        ).fetchone()[0]
        conn.executemany(
            "UPDATE metadata SET value = ? WHERE key = ?",
            (
                ("recorded-game-9.9.9", "source_game_version"),
                ("recorded-resource-8.8.8", "source_resource_version"),
                ("recorded-changelist-111", "source_changelist"),
            ),
        )

    app, store = build_client_app(tmp_path, recorded)
    _, token = issue_device(store, "owner desktop")
    disable_llm(monkeypatch)

    response = run(call(app, "GET", "/v1/meta", headers=bearer(token)))

    body = response.json()
    assert response.status_code == 200
    assert body["source_commit"] == stored_commit
    assert body["source_game_version"] == "recorded-game-9.9.9"
    assert body["source_resource_version"] == "recorded-resource-8.8.8"
    assert body["source_changelist"] == "recorded-changelist-111"
    text = response.text
    assert "fixture-unavailable" not in text
    assert "3.7.0" not in text
    assert "3.7.8" not in text
    assert "8975829" not in text


# --------------------------------------------------------------------------
# Budgets (AC7): per-process concurrency cap and per-minute call cap
# --------------------------------------------------------------------------


def test_model_concurrency_never_exceeds_the_configured_cap(
    monkeypatch, tmp_path, sample_db
):
    cap = 2
    app, store = build_client_app(
        tmp_path,
        sample_db,
        llm_max_concurrency=cap,
        rate_limit_per_minute=100,
        llm_calls_per_minute=100,
        request_timeout_seconds=30.0,
        # This test measures the MODEL concurrency cap, which is a later gate
        # than credential admission. Give it enough auth slots to admit its
        # whole burst, so the non-queuing auth admission does not shed requests
        # before they reach the model cap being measured here.
        auth_max_concurrency=8,
    )
    _, token = issue_device(store, "owner desktop")
    calls: list[tuple[str, object]] = []
    state = {"in_flight": 0, "peak": 0}

    async def slow(locked_text, locks):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        try:
            await asyncio.sleep(0.05)
            return "done"
        finally:
            state["in_flight"] -= 1

    enable_mock_llm(monkeypatch, calls, slow)

    async def hammer():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test", timeout=30.0
        ) as client:
            return await asyncio.gather(
                *[
                    client.post(
                        "/v1/translations",
                        json={"text": f"需要翻译的第{index}个句子"},
                        headers=bearer(token),
                    )
                    for index in range(8)
                ]
            )

    responses = run(hammer())

    assert [item.status_code for item in responses] == [200] * 8
    assert len(calls) == 8
    assert state["peak"] == cap, state["peak"]


def test_per_minute_model_budget_overflow_returns_the_stable_code(
    monkeypatch, tmp_path, sample_db
):
    budget = 3
    app, store = build_client_app(
        tmp_path,
        sample_db,
        llm_calls_per_minute=budget,
        rate_limit_per_minute=100,
    )
    _, token = issue_device(store, "owner desktop")
    calls: list[tuple[str, object]] = []

    async def answer(locked_text, locks):
        return "done"

    enable_mock_llm(monkeypatch, calls, answer)

    codes = []
    for index in range(budget + 2):
        response = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": f"需要翻译的第{index}个句子"},
                headers=bearer(token),
            )
        )
        codes.append(response.status_code)

    assert codes[:budget] == [200] * budget
    assert codes[budget:] == [503, 503]
    assert len(calls) == budget
    overflow = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "另一个需要翻译的句子"},
            headers=bearer(token),
        )
    )
    assert overflow.json()["error"]["code"] == "llm_budget_exhausted"


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------


def test_error_codes_and_statuses_are_pinned():
    assert STATUS_BY_CODE == {
        "unauthorized": 401,
        "forbidden": 403,
        "rate_limited": 429,
        "payload_too_large": 413,
        "invalid_request": 400,
        "input_too_long": 422,
        "llm_unavailable": 503,
        "llm_budget_exhausted": 503,
        "internal": 500,
    }


def test_committed_openapi_snapshot_matches_the_application():
    result = subprocess.run(
        [sys.executable, "scripts/check_api_contract.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_openapi_snapshot_documents_the_versioned_surface():
    from scripts.check_api_contract import check_tokens

    raw = (ROOT / "docs" / "api" / "openapi.json").read_text(encoding="utf-8")
    document = json.loads(raw)

    assert sorted(document["paths"]) == [
        "/healthz",
        "/readyz",
        "/v1/meta",
        "/v1/reviews",
        "/v1/terms",
        "/v1/translations",
    ]
    # The one committed JSON artifact must also respect the product token bans;
    # scripts/check_non_goals.py does not read .json files, so the same
    # patterns are re-applied here through the contract gate.
    assert check_tokens(raw) == []


def test_openapi_documents_conservative_client_limits():
    document = json.loads(
        (ROOT / "docs" / "api" / "openapi.json").read_text(encoding="utf-8")
    )

    text_schema = document["components"]["schemas"]["TranslationRequestBody"][
        "properties"
    ]["text"]
    query_schema = next(
        parameter["schema"]
        for parameter in document["paths"]["/v1/terms"]["get"]["parameters"]
        if parameter["name"] == "q"
    )

    assert text_schema["maxLength"] == LLM_INPUT_CHAR_LIMIT
    assert query_schema["maxLength"] == TERM_QUERY_MAX_LENGTH
    review_properties = document["components"]["schemas"]["ReviewRequestBody"][
        "properties"
    ]
    assert review_properties["source"]["maxLength"] == MAX_SIDE_SCALARS
    assert review_properties["target"]["maxLength"] == MAX_SIDE_SCALARS
    span_properties = document["components"]["schemas"]["ReviewSpanBody"]["properties"]
    assert "Unicode scalars" in span_properties["start"]["description"]
    assert "Half-open" in span_properties["start"]["description"]
    assert "Unicode scalars" in span_properties["end"]["description"]
    assert "Half-open" in span_properties["end"]["description"]


def test_openapi_limit_postprocessor_handles_pydantic1_shapes():
    from wuwaterm_api.app import _apply_openapi_client_limits

    document = {
        "components": {
            "schemas": {
                "TranslationRequestBody": {
                    "properties": {
                        "text": {
                            "type": "string",
                            "json_schema_extra": {
                                "maxLength": 1,
                                "x-preserved": "body",
                            },
                        }
                    }
                },
                "ReviewRequestBody": {
                    "properties": {
                        "source": {"type": "string"},
                        "target": {"type": "string"},
                    }
                },
            }
        },
        "paths": {
            "/v1/terms": {
                "get": {
                    "parameters": [
                        {
                            "name": "q",
                            "schema": {
                                "type": "string",
                                "json_schema_extra": {
                                    "maxLength": 2,
                                    "x-preserved": "query",
                                },
                            },
                        }
                    ]
                }
            }
        },
    }

    result = _apply_openapi_client_limits(document)

    text_schema = result["components"]["schemas"]["TranslationRequestBody"][
        "properties"
    ]["text"]
    query_schema = result["paths"]["/v1/terms"]["get"]["parameters"][0]["schema"]
    assert text_schema["maxLength"] == LLM_INPUT_CHAR_LIMIT
    assert text_schema["json_schema_extra"] == {"x-preserved": "body"}
    assert query_schema["maxLength"] == TERM_QUERY_MAX_LENGTH
    assert query_schema["json_schema_extra"] == {"x-preserved": "query"}
    assert result["components"]["schemas"]["ReviewRequestBody"]["properties"][
        "source"
    ]["maxLength"] == MAX_SIDE_SCALARS
    assert result["components"]["schemas"]["ReviewRequestBody"]["properties"][
        "target"
    ]["maxLength"] == MAX_SIDE_SCALARS


def test_openapi_limit_postprocessor_is_idempotent_and_overrides_conflicts():
    from wuwaterm_api.app import _apply_openapi_client_limits

    document = {
        "components": {
            "schemas": {
                "TranslationRequestBody": {
                    "properties": {"text": {"type": "string", "maxLength": 7}}
                },
                "ReviewRequestBody": {
                    "properties": {
                        "source": {"type": "string", "maxLength": 9},
                        "target": {"type": "string", "maxLength": 9},
                    }
                },
            }
        },
        "paths": {
            "/v1/terms": {
                "get": {
                    "parameters": [
                        {"name": "q", "schema": {"type": "string", "maxLength": 8}}
                    ]
                }
            }
        },
    }

    first = _apply_openapi_client_limits(document)
    second = _apply_openapi_client_limits(first)

    assert first is document
    assert second is document
    assert document["components"]["schemas"]["TranslationRequestBody"][
        "properties"
    ]["text"]["maxLength"] == LLM_INPUT_CHAR_LIMIT
    assert document["paths"]["/v1/terms"]["get"]["parameters"][0]["schema"][
        "maxLength"
    ] == TERM_QUERY_MAX_LENGTH
    assert document["components"]["schemas"]["ReviewRequestBody"]["properties"][
        "source"
    ]["maxLength"] == MAX_SIDE_SCALARS
    assert document["components"]["schemas"]["ReviewRequestBody"]["properties"][
        "target"
    ]["maxLength"] == MAX_SIDE_SCALARS


@pytest.mark.parametrize(
    "document",
    [
        {},
        {
            "components": {
                "schemas": {
                    "TranslationRequestBody": {
                        "properties": {"text": {"type": "string"}}
                    }
                }
            },
            "paths": {"/v1/terms": {"get": {"parameters": []}}},
        },
    ],
)
def test_openapi_limit_postprocessor_fails_on_missing_paths(document):
    from wuwaterm_api.app import _apply_openapi_client_limits

    with pytest.raises(KeyError):
        _apply_openapi_client_limits(document)


def test_openapi_schema_cache_uses_postprocessed_document(tmp_path, sample_db):
    app, _store = build_client_app(tmp_path, sample_db)

    first = app.openapi()
    second = app.openapi()

    assert first is second
    assert first is app.openapi_schema
    assert first["components"]["schemas"]["TranslationRequestBody"]["properties"][
        "text"
    ]["maxLength"] == LLM_INPUT_CHAR_LIMIT
    assert next(
        parameter["schema"]["maxLength"]
        for parameter in first["paths"]["/v1/terms"]["get"]["parameters"]
        if parameter["name"] == "q"
    ) == TERM_QUERY_MAX_LENGTH
    assert first["components"]["schemas"]["ReviewRequestBody"]["properties"][
        "source"
    ]["maxLength"] == MAX_SIDE_SCALARS
    assert first["components"]["schemas"]["ReviewRequestBody"]["properties"][
        "target"
    ]["maxLength"] == MAX_SIDE_SCALARS


API_NUMERIC_SETTING_CASES = (
    (
        "WUWATERM_API_PORT",
        "port",
        8788,
        ("1", 1),
        ("65535", 65535),
        ("SENSITIVE_RAW_DO_NOT_ECHO", "0", "65536"),
    ),
    (
        "WUWATERM_API_LLM_TIMEOUT_SECONDS",
        "llm_timeout_seconds",
        45.0,
        ("0.1", 0.1),
        ("300", 300.0),
        ("SENSITIVE_RAW_DO_NOT_ECHO", "0", "301", "nan", "inf"),
    ),
    (
        "WUWATERM_API_LLM_MAX_CONCURRENCY",
        "llm_max_concurrency",
        2,
        ("1", 1),
        ("64", 64),
        ("SENSITIVE_RAW_DO_NOT_ECHO", "0", "65"),
    ),
    (
        "WUWATERM_API_LLM_CALLS_PER_MINUTE",
        "llm_calls_per_minute",
        30,
        ("1", 1),
        ("10000", 10000),
        ("SENSITIVE_RAW_DO_NOT_ECHO", "0", "10001"),
    ),
    (
        "WUWATERM_API_RATE_LIMIT_PER_MINUTE",
        "rate_limit_per_minute",
        30,
        ("1", 1),
        ("10000", 10000),
        ("SENSITIVE_RAW_DO_NOT_ECHO", "0", "10001"),
    ),
    (
        "WUWATERM_API_MAX_BODY_BYTES",
        "max_body_bytes",
        32 * 1024,
        ("64", 64),
        (str(1024 * 1024), 1024 * 1024),
        ("SENSITIVE_RAW_DO_NOT_ECHO", "63", str(1024 * 1024 + 1)),
    ),
    (
        "WUWATERM_API_REQUEST_TIMEOUT_SECONDS",
        "request_timeout_seconds",
        90.0,
        ("1", 1.0),
        ("600", 600.0),
        ("SENSITIVE_RAW_DO_NOT_ECHO", "0", "601", "nan", "inf"),
    ),
    (
        "WUWATERM_API_AUTH_MAX_CONCURRENCY",
        "auth_max_concurrency",
        2,
        ("1", 1),
        ("64", 64),
        ("SENSITIVE_RAW_DO_NOT_ECHO", "0", "65"),
    ),
)


@pytest.mark.parametrize(
    ("env_name", "attribute", "default", "minimum", "maximum", "invalid_values"),
    API_NUMERIC_SETTING_CASES,
)
def test_api_numeric_settings_are_lenient_until_serve(
    monkeypatch, env_name, attribute, default, minimum, maximum, invalid_values
):
    monkeypatch.delenv(env_name, raising=False)
    assert getattr(ApiSettings.from_env(), attribute) == default

    for raw in invalid_values:
        monkeypatch.setenv(env_name, raw)
        settings = ApiSettings.from_env()
        assert getattr(settings, attribute) == default
        assert getattr(settings._serve_numeric_raw, attribute) == raw

    for raw, expected in (minimum, maximum):
        monkeypatch.setenv(env_name, raw)
        assert getattr(ApiSettings.from_env(), attribute) == expected


def test_api_numeric_raw_values_are_immutable_hidden_and_nonsemantic(monkeypatch):
    from dataclasses import FrozenInstanceError

    monkeypatch.delenv("WUWATERM_API_PORT", raising=False)
    baseline = ApiSettings.from_env()
    monkeypatch.setenv("WUWATERM_API_PORT", "SENSITIVE_RAW_DO_NOT_ECHO")
    settings = ApiSettings.from_env()

    assert settings == baseline
    assert "SENSITIVE_RAW_DO_NOT_ECHO" not in repr(settings)
    with pytest.raises(FrozenInstanceError):
        settings._serve_numeric_raw.port = "changed"


@pytest.mark.parametrize(
    ("env_name", "attribute", "default", "minimum", "maximum", "invalid_values"),
    API_NUMERIC_SETTING_CASES,
)
def test_api_numeric_errors_do_not_block_real_device_revocation(
    monkeypatch,
    tmp_path,
    capsys,
    env_name,
    attribute,
    default,
    minimum,
    maximum,
    invalid_values,
):
    import wuwaterm_api.cli as cli

    store_path = tmp_path / env_name / "devices.db"
    store = DeviceStore(store_path, guard_legacy_default=False)
    device, _ = issue_device(store, "owner desktop")
    monkeypatch.setenv("WUWATERM_API_DEVICE_DB_PATH", str(store_path))
    monkeypatch.setenv(env_name, invalid_values[0])

    assert cli.main(["device", "revoke", "--device-id", device.device_id]) == 0
    capsys.readouterr()
    persisted = {
        item.device_id: item
        for item in DeviceStore(store_path, guard_legacy_default=False).list_devices()
    }
    assert persisted[device.device_id].revoked


@pytest.mark.parametrize(
    ("env_name", "attribute", "default", "minimum", "maximum", "invalid_values"),
    API_NUMERIC_SETTING_CASES,
)
def test_api_numeric_errors_fail_serve_before_any_side_effect(
    monkeypatch,
    capsys,
    env_name,
    attribute,
    default,
    minimum,
    maximum,
    invalid_values,
):
    import wuwaterm_api.cli as cli

    for raw in invalid_values:
        calls = []
        monkeypatch.setenv(env_name, raw)
        monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: calls.append("log"))
        monkeypatch.setattr(cli, "DeviceStore", lambda *a, **k: calls.append("store"))
        monkeypatch.setattr(
            "wuwaterm_api.app.create_app", lambda *a, **k: calls.append("app")
        )
        monkeypatch.setattr("uvicorn.run", lambda *a, **k: calls.append("uvicorn"))

        assert cli.main(["serve"]) == 2
        captured = capsys.readouterr()
        assert calls == []
        if raw == "SENSITIVE_RAW_DO_NOT_ECHO":
            assert raw not in captured.err


def test_settings_defaults_bind_to_loopback(monkeypatch):
    for name in (
        "WUWATERM_API_BIND",
        "WUWATERM_API_PORT",
        "WUWATERM_API_LLM_MAX_CONCURRENCY",
        "WUWATERM_API_LLM_CALLS_PER_MINUTE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = ApiSettings.from_env()

    assert settings.bind == "127.0.0.1"
    assert settings.port == 8788
    # Deliberately smaller than the bot's budget; the documented worst case is
    # the SUM of the per-process budgets, never a shared global one.
    assert settings.llm_max_concurrency == 2
    assert settings.llm_calls_per_minute == 30


# --------------------------------------------------------------------------
# Review follow-ups: streaming cap, routing shape, credential-store writes,
# refusing a pretend model answer, and contract completeness
# --------------------------------------------------------------------------


def test_body_cap_holds_without_a_content_length_header(tmp_path, sample_db):
    """A chunked caller must not be able to make the process buffer freely."""
    app, store = build_client_app(tmp_path, sample_db, max_body_bytes=256)
    _, token = issue_device(store, "owner desktop")

    async def chunked():
        yield b'{"text": "'
        for _ in range(50):
            yield b"x" * 64
        yield b'"}'

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            content=chunked(),
            headers={**bearer(token), "Content-Type": "application/json"},
        )
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_negative_content_length_is_an_invalid_request(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            content=b'{"text": "hi"}',
            headers={
                **bearer(token),
                "Content-Type": "application/json",
                "Content-Length": "-1",
            },
        )
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/v2/meta", 404),
        ("GET", "/nope", 404),
        ("DELETE", "/healthz", 405),
    ],
)
def test_routing_failures_use_the_stable_envelope(
    tmp_path, sample_db, method, path, status
):
    app, _ = build_client_app(tmp_path, sample_db)

    response = run(call(app, method, path))

    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error", "request_id"}
    assert body["error"]["code"] == "invalid_request"
    assert "detail" not in body


def test_fresh_credential_store_answers_401_not_an_error(tmp_path, sample_db):
    """A first install has an EMPTY devices.db; that is a rejection, not a crash.

    The store is created ONCE, at startup (``cli._serve`` calls ``initialize``),
    which is what this test now models. It used to be created by the request
    path itself; see
    ``test_a_missing_credential_store_is_never_created_by_a_request`` for what a
    genuinely missing store answers, and why it must not be 401.
    """
    app, store = build_client_app(tmp_path, sample_db)
    assert store.path.exists()
    assert store.list_devices() == []

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "声骸"},
            headers=bearer("wtd1.deadbeef.secret"),
        )
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_rate_limited_requests_do_not_write_to_the_credential_store(
    tmp_path, sample_db
):
    app, store = build_client_app(tmp_path, sample_db, rate_limit_per_minute=1)
    device, token = issue_device(store, "owner desktop")

    run(call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=bearer(token)))
    admitted = store.list_devices()[0].last_used_at
    assert admitted is not None

    for _ in range(5):
        blocked = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers=bearer(token),
            )
        )
        assert blocked.status_code == 429

    assert store.list_devices()[0].last_used_at == admitted


def test_unauthenticated_requests_never_touch_the_credential_store(
    tmp_path, sample_db
):
    app, store = build_client_app(tmp_path, sample_db)
    device, _token = issue_device(store, "owner desktop")

    run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "声骸"},
            headers=bearer("wtd1.%s.wrong" % device.device_id),
        )
    )

    assert store.list_devices()[0].last_used_at is None


def test_model_path_is_refused_when_no_model_is_configured(
    monkeypatch, tmp_path, sample_db
):
    """Without a model the pipeline returns source text; HTTP must not lie."""
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    disable_llm(monkeypatch)

    refused = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "需要翻译的一个句子"},
            headers=bearer(token),
        )
    )
    # A dictionary hit still answers, because it needs no model at all.
    dictionary = run(
        call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=bearer(token))
    )

    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "llm_unavailable"
    assert dictionary.status_code == 200
    assert dictionary.json()["kind"] == "exact"


def test_openapi_declares_the_credential_scheme_and_every_failure_shape():
    document = json.loads(
        (ROOT / "docs" / "api" / "openapi.json").read_text(encoding="utf-8")
    )

    schemes = document["components"]["securitySchemes"]
    assert any(
        item.get("type") == "http" and item.get("scheme") == "bearer"
        for item in schemes.values()
    ), schemes
    translations = document["paths"]["/v1/translations"]["post"]
    assert translations["security"]
    assert "504" in translations["responses"]
    code = document["components"]["schemas"]["ErrorDetailBody"]["properties"]["code"]
    assert set(code["enum"]) == set(STATUS_BY_CODE)
    assert document["paths"]["/v1/terms"]["get"]["parameters"][0]["required"] is True


def test_a_slow_body_is_bounded_by_the_request_time_budget(tmp_path, sample_db):
    """The time budget wraps the body read, not the other way round."""
    app, store = build_client_app(
        tmp_path, sample_db, request_timeout_seconds=0.2, max_body_bytes=4096
    )
    _, token = issue_device(store, "owner desktop")

    async def drip():
        yield b'{"text": "'
        for _ in range(20):
            await asyncio.sleep(0.1)
            yield b"x"
        yield b'"}'

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            content=drip(),
            headers={**bearer(token), "Content-Type": "application/json"},
        )
    )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "internal"


def test_stored_verifier_is_salted_and_slow_to_search(tmp_path):
    """Operator-chosen secrets need a KDF, not a bare digest."""
    import hashlib

    from wuwaterm_api import auth as auth_module

    store = DeviceStore(tmp_path / "devices.db")
    secret = "the-same-secret-for-both-devices-0123456789"
    first = store.issue("device one", None, secret=secret)
    second = store.issue("device two", None, secret=secret)

    rows = {}
    import sqlite3

    with sqlite3.connect(tmp_path / "devices.db") as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT device_id, salt, token_hash FROM devices"):
            rows[row["device_id"]] = (bytes(row["salt"]), bytes(row["token_hash"]))

    # Same secret, different salt, therefore different stored verifiers.
    assert rows[first.device_id][0] != rows[second.device_id][0]
    assert rows[first.device_id][1] != rows[second.device_id][1]
    # And the stored value is not a bare digest of the secret.
    assert hashlib.sha256(secret.encode()).digest() not in {
        value for _salt, value in rows.values()
    }
    assert auth_module.SCRYPT_N >= 1 << 14


def test_a_secret_containing_the_token_separator_round_trips(tmp_path, sample_db):
    """The credential charset is unconstrained; dots must not break auth."""
    app, store = build_client_app(tmp_path, sample_db)
    dotted = "v1.abc.def-0123456789abcdefghijklmnop=="
    assert "." in dotted and len(dotted) >= MIN_SECRET_LENGTH
    _, token = issue_device(store, "owner desktop", secret=dotted)

    parsed = parse_token(token)
    response = run(
        call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=bearer(token))
    )

    assert parsed == (token.split(".", 2)[1], dotted)
    assert response.status_code == 200


def test_a_secret_with_surrounding_whitespace_is_refused(tmp_path):
    """Refuse rather than trim: a trimmed secret would never authenticate."""
    store = DeviceStore(tmp_path / "devices.db")
    body = "x" * MIN_SECRET_LENGTH

    for candidate in (f" {body}", f"{body} ", f"\t{body}\n"):
        with pytest.raises(DeviceStoreError):
            store.issue("owner desktop", None, secret=candidate)
    assert store.list_devices() == []


def test_a_store_written_by_an_older_shape_is_reported_clearly(tmp_path):
    import sqlite3

    path = tmp_path / "devices.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE devices (device_id TEXT PRIMARY KEY, device_name TEXT,"
            " scopes TEXT, created_at TEXT, revoked_at TEXT, last_used_at TEXT)"
        )

    with pytest.raises(DeviceStoreError) as excinfo:
        DeviceStore(path).initialize()

    assert "older device store" in str(excinfo.value)


def test_a_store_left_at_the_old_default_path_is_not_silently_abandoned(tmp_path):
    """The store moved out of the bot's writable mount between builds.

    An installation that ran on the old default keeps every verifier in
    state/api/devices.db. Creating an empty store at the new path would look
    like a clean start while every registered device stopped authenticating,
    so the move has to be refused loudly instead.
    """
    legacy = tmp_path / "state" / "api" / "devices.db"
    legacy.parent.mkdir(parents=True)
    DeviceStore(legacy).initialize()
    assert legacy.exists()

    current = tmp_path / "state-api" / "devices.db"
    with pytest.raises(DeviceStoreError) as excinfo:
        DeviceStore(current).initialize()

    message = str(excinfo.value)
    assert str(legacy) in message
    assert str(current) in message
    assert not current.exists()


def test_a_store_at_both_paths_is_refused_rather_than_guessed(tmp_path):
    """An earlier start may already have created an empty store here."""
    legacy = tmp_path / "state" / "api" / "devices.db"
    legacy.parent.mkdir(parents=True)
    DeviceStore(legacy).initialize()

    current = tmp_path / "state-api" / "devices.db"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"SQLite format 3\x00")

    with pytest.raises(DeviceStoreError) as excinfo:
        DeviceStore(current).initialize()

    assert str(legacy) in str(excinfo.value)


def test_an_explicit_store_path_never_looks_for_the_old_default(tmp_path):
    """Only the default layout ever had the old path."""
    legacy = tmp_path / "state" / "api" / "devices.db"
    legacy.parent.mkdir(parents=True)
    DeviceStore(legacy).initialize()

    chosen = tmp_path / "somewhere-else" / "devices.db"
    DeviceStore(chosen).initialize()

    assert chosen.exists()


def test_a_named_store_is_used_even_when_it_sits_in_a_state_api_directory(tmp_path):
    """The exemption is about being NAMED, not about the directory's name.

    An operator who sets WUWATERM_API_DEVICE_DB_PATH means that store, and the
    obvious value to set is a path under state-api/. Deciding by the parent
    directory's name would make the escape hatch unusable in exactly the case
    the message suggests it for.
    """
    legacy = tmp_path / "state" / "api" / "devices.db"
    legacy.parent.mkdir(parents=True)
    DeviceStore(legacy).initialize()

    named = tmp_path / "state-api" / "devices.db"
    DeviceStore(named, guard_legacy_default=False).initialize()

    assert named.exists()


def test_settings_report_whether_the_store_path_was_chosen(tmp_path, monkeypatch):
    monkeypatch.delenv("WUWATERM_API_DEVICE_DB_PATH", raising=False)
    monkeypatch.setenv("WUWATERM_API_STATE_DIR", str(tmp_path / "state-api"))
    assert ApiSettings.from_env().device_db_is_default is True

    monkeypatch.setenv(
        "WUWATERM_API_DEVICE_DB_PATH", str(tmp_path / "state-api" / "devices.db")
    )
    chosen = ApiSettings.from_env()
    assert chosen.device_db_is_default is False
    assert chosen.device_db_path == tmp_path / "state-api" / "devices.db"


def test_the_moved_store_starts_normally_once_it_is_in_place(tmp_path):
    """The guard must not fire when there is nothing left behind."""
    current = tmp_path / "state-api" / "devices.db"
    DeviceStore(current).initialize()

    assert current.exists()


def test_credential_verification_is_bounded_before_any_device_limit(
    tmp_path, sample_db
):
    """The deliberately expensive check must not become the load itself."""
    app, store = build_client_app(tmp_path, sample_db, auth_max_concurrency=1)
    issue_device(store, "owner desktop")
    state = {"in_flight": 0, "peak": 0}
    real = DeviceStore.authenticate

    def watched(self, token):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        try:
            return real(self, token)
        finally:
            state["in_flight"] -= 1

    DeviceStore.authenticate = watched
    try:

        async def hammer():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://api.test", timeout=30.0
            ) as client:
                return await asyncio.gather(
                    *[
                        client.post(
                            "/v1/translations",
                            json={"text": "声骸"},
                            headers=bearer("wtd1.deadbeef.%s" % ("x" * 40)),
                        )
                        for _ in range(6)
                    ]
                )

        responses = run(hammer())
    finally:
        DeviceStore.authenticate = real

    # Non-queuing admission: an admitted attempt is rejected as a bad
    # credential (401); an attempt that found the verifier full is shed (429).
    # Either way the expensive check itself never runs more than the bound.
    # `401 in` guards against the vacuous all-429 case (verification never ran).
    statuses = {item.status_code for item in responses}
    assert 401 in statuses and statuses <= {401, 429}, statuses
    assert state["peak"] == 1, state["peak"]


def test_an_unusable_store_is_one_more_uniform_rejection(tmp_path, sample_db):
    """Loud at startup, uniform on the request path."""
    import sqlite3

    settings = build_settings(tmp_path, sample_db)
    settings.device_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.device_db_path) as conn:
        conn.execute("CREATE TABLE devices (device_id TEXT PRIMARY KEY)")
    app = create_app(settings, device_store=DeviceStore(settings.device_db_path))

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "声骸"},
            headers=bearer("wtd1.deadbeef.%s" % ("x" * 40)),
        )
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    # The operator still gets the real reason where it has an audience.
    with pytest.raises(DeviceStoreError):
        DeviceStore(settings.device_db_path).initialize()


def test_a_secret_that_cannot_be_sent_in_a_header_is_refused(tmp_path):
    """Registering an unusable credential is the worst kind of failure."""
    store = DeviceStore(tmp_path / "devices.db")
    body = "x" * MIN_SECRET_LENGTH

    for candidate in (
        f"{body} with space",
        f"{body}\nnewline",
        f"{body}\ttab",
        f"{body}\x7fdel",
        f"{body}密码",
    ):
        with pytest.raises(DeviceStoreError):
            store.issue("owner desktop", None, secret=candidate)
    assert store.list_devices() == []


@pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX permission bits; the deployment target is Linux and CI checks it",
)
def test_every_file_carrying_verifier_material_is_restricted(tmp_path):
    """The write-ahead log holds the same rows as the database."""
    import stat

    store = DeviceStore(tmp_path / "state" / "devices.db")
    issue_device(store, "owner desktop")
    store.authenticate("wtd1.nope.%s" % ("x" * 40))

    paths = [store.path.parent, store.path]
    paths += [
        store.path.with_name(store.path.name + suffix) for suffix in ("-wal", "-shm")
    ]
    for path in paths:
        if not path.exists():
            continue
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert not mode & 0o077, (path, oct(mode))


def test_a_corrupt_store_is_a_service_unavailable_not_a_rejection(tmp_path, sample_db):
    """An UNREADABLE store is the store being unusable, not a bad credential.

    It must not be answered as 401 (which would tell a valid device to re-pair);
    it is 503, and the response is device-independent so nothing is probeable.
    """
    settings = build_settings(tmp_path, sample_db)
    settings.device_db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.device_db_path.write_bytes(b"this is not a database at all")
    app = create_app(settings, device_store=DeviceStore(settings.device_db_path))

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "声骸"},
            headers=bearer("wtd1.deadbeef.%s" % ("x" * 40)),
        )
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "internal"


def test_a_cancelled_request_does_not_leak_a_verification_slot(tmp_path, sample_db):
    """A request cancelled mid-verification leaves the service usable.

    The admission slot is released on the loop, so it comes back immediately;
    the worker that was already started keeps running on the bounded credential
    pool and finishes on its own. Both have to recover, or one cancelled
    request would wedge every later one.
    """
    app, store = build_client_app(
        tmp_path, sample_db, auth_max_concurrency=1, request_timeout_seconds=1.0
    )
    _, token = issue_device(store, "owner desktop")
    real = DeviceStore.authenticate
    worker_finished = threading.Event()

    def slow(self, presented):
        try:
            time.sleep(3.0)
            return real(self, presented)
        finally:
            worker_finished.set()

    DeviceStore.authenticate = slow
    try:
        timed_out = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers=bearer(token),
            )
        )
    finally:
        DeviceStore.authenticate = real

    assert timed_out.status_code == 504
    # The slot is released on the loop, so it is already free here. Without
    # that release it would stay stranded and every later request would 429.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if app.state.auth_slots.acquire(blocking=False):
            app.state.auth_slots.release()
            break
        time.sleep(0.05)
    else:  # pragma: no cover - only on a real leak
        raise AssertionError("verification slot was never released")

    # The started worker is not joined by the loop it was submitted from (it
    # lives on this app's own credential pool, not the default executor), so
    # wait for it before measuring recovery rather than racing its 3s sleep.
    assert worker_finished.wait(15.0), "the verification worker never finished"

    recovered = run(
        call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=bearer(token))
    )
    assert recovered.status_code == 200


def test_admission_slot_is_released_if_verification_is_cancelled_before_it_runs(
    tmp_path, sample_db, monkeypatch
):
    """The admission slot must be released on the loop, not only in the worker.

    If the verification job is cancelled while still QUEUED on a saturated
    pool, the worker never runs — so releasing only inside the worker would
    leak the slot permanently and wedge every later request into 429. Here the
    submission seam is cancelled before the worker callable runs, exactly that
    case, and the slot must still be reacquirable afterwards.
    """
    from fastapi.security import HTTPAuthorizationCredentials

    import wuwaterm_api.app as appmod

    app, store = build_client_app(tmp_path, sample_db, auth_max_concurrency=1)
    _, token = issue_device(store, "owner desktop")

    class _Url:
        path = "/v1/translations"

    class _State:
        pass

    class _Req:
        def __init__(self, app):
            self.app = app
            self.url = _Url()
            self.state = _State()

    async def cancel_before_worker(app, func, *args):
        # The awaiting task is cancelled before the queued worker starts.
        raise asyncio.CancelledError()

    monkeypatch.setattr(appmod, "_in_credential_pool", cancel_before_worker)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await appmod.authenticated_device(_Req(app), creds)

    run(scenario())

    # The slot survived the cancelled-before-it-ran verification.
    assert app.state.auth_slots.acquire(blocking=False)
    app.state.auth_slots.release()


# --------------------------------------------------------------------------
# Hardening fixes (gpt5.6 safety critic): loopback bind, auth admission,
# revocation TOCTOU
# --------------------------------------------------------------------------


def test_validate_loopback_bind_accepts_only_numeric_loopback():
    """A numeric loopback literal is required; everything else is refused.

    The returned value is the NORMALIZED literal that actually binds — brackets
    and surrounding whitespace are stripped — so an accepted value can never be
    one uvicorn then rejects at bind time.
    """
    from wuwaterm_api.settings import validate_loopback_bind

    assert validate_loopback_bind("127.0.0.1") == "127.0.0.1"
    assert validate_loopback_bind("127.255.255.254") == "127.255.255.254"
    assert validate_loopback_bind("::1") == "::1"
    assert validate_loopback_bind("[::1]") == "::1"
    assert validate_loopback_bind(" 127.0.0.1 ") == "127.0.0.1"
    for bad in (
        "0.0.0.0",
        "::",
        "192.168.0.10",
        "10.0.0.1",
        "8.8.8.8",
        "localhost",
        "example.com",
        "",
        "not-an-address",
        # Zone-scoped IPv6: ipaddress PARSES these and reports is_loopback, but
        # the scope is unvalidated and getaddrinfo fails on a nonexistent one,
        # which would escape as a raw socket error instead of exit 2.
        "::1%does-not-exist",
        "::1%eth0",
        "[::1%eth0]",
    ):
        with pytest.raises(ApiConfigError):
            validate_loopback_bind(bad)


def test_serve_rejects_a_zone_scoped_ipv6_bind_with_exit_2(monkeypatch, tmp_path):
    """A scoped-IPv6 bind must fail as a config error, not a socket error.

    `::1%does-not-exist` satisfies ipaddress.is_loopback, so without the zone-id
    refusal it reached uvicorn and getaddrinfo raised, escaping main() as an
    unhandled socket error instead of the intended ApiConfigError -> exit 2.
    """
    import wuwaterm_api.cli as cli

    called = {}
    monkeypatch.setenv("WUWATERM_API_BIND", "::1%does-not-exist")
    monkeypatch.setenv(
        "WUWATERM_API_DEVICE_DB_PATH", str(tmp_path / "state-api" / "devices.db")
    )
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: called.setdefault("kw", kw))

    assert cli.main(["serve"]) == 2
    assert "kw" not in called  # never reached uvicorn.run / getaddrinfo

    # Same for the override path.
    monkeypatch.delenv("WUWATERM_API_BIND", raising=False)
    assert cli.main(["serve", "--host", "::1%does-not-exist"]) == 2
    assert "kw" not in called


def test_serve_rejects_a_non_loopback_env_bind(monkeypatch, tmp_path):
    """A non-loopback WUWATERM_API_BIND refuses to SERVE (exit 2, nothing bound)."""
    import wuwaterm_api.cli as cli

    called = {}
    monkeypatch.setenv("WUWATERM_API_BIND", "0.0.0.0")
    monkeypatch.setenv(
        "WUWATERM_API_DEVICE_DB_PATH", str(tmp_path / "state-api" / "devices.db")
    )
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: called.setdefault("kw", kw))

    assert cli.main(["serve"]) == 2
    assert "kw" not in called  # never reached uvicorn.run


def test_operator_commands_are_not_gated_on_the_serve_bind(monkeypatch, tmp_path, capsys):
    """Credential revocation must never depend on serve-time network config.

    The loopback guard belongs on the serve path only: a mistyped or deliberately
    public WUWATERM_API_BIND must not block `device issue|list|revoke`, which is
    the one operation that has to work when something is wrong (AC5/AC14).
    """
    import wuwaterm_api.cli as cli

    db = tmp_path / "state-api" / "devices.db"
    monkeypatch.setenv("WUWATERM_API_DEVICE_DB_PATH", str(db))
    # A value the serve path refuses outright.
    monkeypatch.setenv("WUWATERM_API_BIND", "0.0.0.0")
    store = DeviceStore(db, guard_legacy_default=False)
    device, _ = issue_device(store, "owner desktop")

    assert cli.main(["device", "list"]) == 0
    assert cli.main(["device", "revoke", "--device-id", device.device_id]) == 0

    assert capsys.readouterr().out.count(device.device_id) >= 2
    assert DeviceStore(db, guard_legacy_default=False).authenticate(
        f"{TOKEN_SCHEME}.{device.device_id}.{TEST_SECRET_BASE}-owner-desktop"
    ) is None  # revoked, and the revocation went through with a bad bind set


def test_serve_refuses_a_non_loopback_host_override(monkeypatch):
    """The --host override goes through the same guard, before anything binds."""
    import wuwaterm_api.cli as cli

    called = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: called.setdefault("kw", kw))

    code = cli.main(["serve", "--host", "0.0.0.0"])

    assert code == 2
    assert "kw" not in called  # never reached uvicorn.run


def test_serve_binds_the_validated_loopback_host(monkeypatch):
    """The happy path binds the validated loopback bind."""
    import types

    import wuwaterm_api.cli as cli

    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    monkeypatch.setattr(
        cli, "DeviceStore", lambda *a, **k: types.SimpleNamespace(initialize=lambda: None)
    )
    monkeypatch.setattr("wuwaterm_api.app.create_app", lambda *a, **k: object())
    for name in ("WUWATERM_API_BIND", "WUWATERM_API_PORT"):
        monkeypatch.delenv(name, raising=False)

    assert cli.main(["serve"]) == 0
    assert captured["host"] == "127.0.0.1"


def test_auth_admission_sheds_load_when_the_verifier_is_full(tmp_path, sample_db):
    """When the bounded verification executor is full, an extra credentialed
    request is shed immediately (429), not queued behind an expensive scrypt.

    With the whole executor occupied, the old code blocked the worker thread on
    the semaphore and the request only ended at the time budget (504); the fix
    admits without queuing and returns the back-off code straight away.
    """
    app, store = build_client_app(
        tmp_path, sample_db, auth_max_concurrency=1, request_timeout_seconds=2.0
    )
    _, token = issue_device(store, "owner desktop")

    # Occupy the single verification slot so the executor is full.
    assert app.state.auth_slots.acquire(blocking=False)
    try:
        shed = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers=bearer(token),
            )
        )
    finally:
        app.state.auth_slots.release()

    assert shed.status_code == 429
    assert shed.json()["error"]["code"] == "rate_limited"

    # With the slot free again the same credential is admitted normally.
    admitted = run(
        call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=bearer(token))
    )
    assert admitted.status_code == 200


def test_record_use_reports_whether_a_live_row_was_stamped(tmp_path):
    """record_use returns affected rows and is_active reflects revocation."""
    store = DeviceStore(tmp_path / "devices.db")
    device, _ = issue_device(store, "owner desktop")

    assert store.record_use(device.device_id) == 1
    assert store.is_active(device.device_id) is True

    store.revoke(device.device_id)

    assert store.record_use(device.device_id) == 0
    assert store.is_active(device.device_id) is False
    assert store.is_active("does-not-exist") is False


def test_a_device_revoked_during_verification_is_refused(tmp_path, sample_db):
    """TOCTOU: a revocation that commits between the verify snapshot and
    admission must not be served. record_use stamps zero rows, so admission
    fails with 401 rather than serving the withdrawn credential."""
    app, store = build_client_app(tmp_path, sample_db)
    device, token = issue_device(store, "owner desktop")
    real = DeviceStore.authenticate

    def revoke_then_return(self, presented):
        result = real(self, presented)
        if result is not None:
            # The operator revokes in the same instant the snapshot is taken.
            self.revoke(result.device_id)
        return result

    DeviceStore.authenticate = revoke_then_return
    try:
        response = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers=bearer(token),
            )
        )
    finally:
        DeviceStore.authenticate = real

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_a_device_revoked_after_admission_is_refused_before_serving(
    tmp_path, sample_db
):
    """TOCTOU: a revocation that commits after admission but before the response
    is caught by the active re-check at the serving seam, not served."""
    app, store = build_client_app(tmp_path, sample_db)
    device, token = issue_device(store, "owner desktop")
    real_record = DeviceStore.record_use

    def record_then_revoke(self, device_id, *, now=None):
        count = real_record(self, device_id, now=now)
        # Admitted on a live row; the operator revokes before the response is
        # built.
        self.revoke(device_id)
        return count

    DeviceStore.record_use = record_then_revoke
    try:
        response = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers=bearer(token),
            )
        )
    finally:
        DeviceStore.record_use = real_record

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_is_active_propagates_a_transient_store_error(tmp_path):
    """A store failure on the re-check is surfaced, not swallowed into False.

    Swallowing it (as authenticate deliberately does, for anti-enumeration)
    would misread a locked database as a revoked device and reject a valid one.
    """
    import sqlite3

    store = DeviceStore(tmp_path / "devices.db")
    device, _ = issue_device(store, "owner desktop")

    def boom(self):
        raise sqlite3.OperationalError("database is locked")

    # The request path reads through a read-only connection, so that is the
    # seam a transient failure arrives on.
    real = DeviceStore._connect_readonly
    DeviceStore._connect_readonly = boom
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.is_active(device.device_id)
    finally:
        DeviceStore._connect_readonly = real


def test_a_transient_recheck_store_error_is_503_not_401(tmp_path, sample_db):
    """A transient store failure on the post-auth re-check must not tell a
    valid device to re-pair: it is the service-unavailable envelope, not
    unauthorized."""
    import sqlite3

    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    real = DeviceStore.is_active

    def boom(self, device_id):
        raise sqlite3.OperationalError("database is locked")

    DeviceStore.is_active = boom
    try:
        response = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers=bearer(token),
            )
        )
    finally:
        DeviceStore.is_active = real

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "internal"
    # The one thing it must never be: a credential rejection.
    assert response.json()["error"]["code"] != "unauthorized"


def test_a_transient_record_use_store_error_is_503_not_500(tmp_path, sample_db):
    """A transient failure on the admission write is infrastructure (503), not a
    generic 500 or a credential rejection."""
    import sqlite3

    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    real = DeviceStore.record_use

    def boom(self, device_id, *, now=None):
        raise sqlite3.OperationalError("database is locked")

    DeviceStore.record_use = boom
    try:
        response = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers=bearer(token),
            )
        )
    finally:
        DeviceStore.record_use = real

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "internal"


def test_a_post_model_store_hiccup_still_serves_the_paid_translation(
    monkeypatch, tmp_path, sample_db
):
    """A transient store error after the model must not discard a completed
    translation and invite a second paid retry: it logs and serves. Both
    pre-model re-checks (before queuing and after acquiring the slot) still
    fail closed.

    The body is a genuine model-answered translation, so the work being
    protected here really is a paid model round trip — exactly one of them.
    """
    import sqlite3

    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    model_calls: list[tuple[str, object]] = []

    async def answer(locked_text, locks):
        return "a model answer"

    enable_mock_llm(monkeypatch, model_calls, answer)

    real = DeviceStore.is_active
    checks = []

    def flaky(self, device_id):
        checks.append(bool(model_calls))
        if model_calls:  # the model has answered; independent of read count
            raise sqlite3.OperationalError("database is locked")
        return real(self, device_id)  # pre-model seams: device is active

    DeviceStore.is_active = flaky
    try:
        response = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "这是一个需要模型翻译的长句子"},
                headers=bearer(token),
            )
        )
    finally:
        DeviceStore.is_active = real

    assert response.status_code == 200
    assert response.json()["kind"] == "llm"
    assert len(model_calls) == 1  # the paid call happened once and was not wasted
    assert checks == [False, False, True]  # two pre-model reads, one post-model failure


def test_a_locked_store_on_the_auth_read_is_503_a_wrong_secret_is_401(
    tmp_path, sample_db
):
    """The verification READ failing (store unusable) is 503, not 401; a genuine
    wrong secret is still a credential rejection (401)."""
    import sqlite3

    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    real = DeviceStore._verify

    def boom(self, device_id, secret):
        raise sqlite3.OperationalError("database is locked")

    DeviceStore._verify = boom
    try:
        locked = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers=bearer(token),
            )
        )
    finally:
        DeviceStore._verify = real

    assert locked.status_code == 503
    assert locked.json()["error"]["code"] == "internal"

    # Store readable again: a genuine wrong secret is still a rejection.
    device2, _ = issue_device(store, "another")
    wrong = "wtd1.%s.%s" % (device2.device_id, "z" * 40)
    rejected = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "声骸"},
            headers=bearer(wrong),
        )
    )

    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "unauthorized"


# --------------------------------------------------------------------------
# Second hardening pass (safety re-check N1/N2/N9 + delta audit): the request
# path never writes to the credential store, the verification bound holds
# under cancellation and cannot be starved by unauthenticated traffic, and the
# --port override is validated like the environment variable it overrides.
# --------------------------------------------------------------------------


def test_a_missing_credential_store_is_never_created_by_a_request(tmp_path, sample_db):
    """An unauthenticated request must not be able to WRITE the credential store.

    `authenticate()` said READ ONLY and then called `initialize()`: mkdir, a
    write-mode connect, a journal pragma and DDL, on every request. Two
    consequences, both real: an unauthenticated caller wrote to the credential
    store (and contended with a concurrent `revoke()`), and a store an operator
    had DELETED as an emergency revocation was silently recreated by the very
    next unauthenticated request.

    A missing store is now the store being unusable — 503, the existing
    store-unavailable path — and not a credential rejection (401), which would
    tell a valid device to re-pair over an infrastructure fault.
    """
    import sqlite3

    store_path = tmp_path / "deep" / "nested" / "devices.db"
    settings = build_settings(tmp_path, sample_db, device_db_path=store_path)
    store = DeviceStore(store_path, guard_legacy_default=False)
    app = create_app(settings, device_store=store)
    assert not (tmp_path / "deep").exists()

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "声骸"},
            headers=bearer("wtd1.deadbeef.%s" % ("x" * 40)),
        )
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "internal"
    # Nothing was created: not the directory tree, not the database.
    assert not (tmp_path / "deep").exists()

    # The store object itself is read-only on this path, not just the endpoint.
    with pytest.raises(sqlite3.Error):
        store.authenticate("wtd1.deadbeef.%s" % ("x" * 40))
    assert not (tmp_path / "deep").exists()


def test_deleting_the_store_is_a_revocation_the_request_path_cannot_undo(
    tmp_path, sample_db
):
    """Deleting devices.db is the bluntest revocation an operator has.

    It has to stay deleted. Previously the next request re-created an empty
    store, so the emergency measure lasted exactly until the next
    unauthenticated caller. Now every later request answers 503 and the file
    stays gone.
    """
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    served = run(
        call(app, "POST", "/v1/translations", json={"text": "声骸"}, headers=bearer(token))
    )
    assert served.status_code == 200

    for suffix in ("", "-wal", "-shm"):
        store.path.with_name(store.path.name + suffix).unlink(missing_ok=True)
    assert not store.path.exists()

    for _ in range(3):
        refused = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers=bearer(token),
            )
        )
        assert refused.status_code == 503
        assert refused.json()["error"]["code"] == "internal"
        assert not store.path.exists(), "the request path recreated the store"


def test_unauthenticated_probes_cannot_shed_a_valid_credential(tmp_path, sample_db):
    """/readyz is unauthenticated and used to share the credential pool.

    Every blocking call went to asyncio's process-wide DEFAULT executor, and
    the admission slot was held from before submission until the awaiting
    coroutine resumed — so QUEUING occupied a slot. Two unauthenticated
    /readyz probes were therefore enough to make the owner's own valid token
    answer 429: the slots were held by verifications that had not started and
    could not start.

    Here the shared pool is deliberately narrow (2 workers) and both workers
    are held by unauthenticated probes for the whole run. Valid-token requests
    are issued one at a time, each given a chance to complete its
    verification, and NONE of them may be shed.
    """
    import wuwaterm_api.app as appmod

    app, store = build_client_app(
        tmp_path,
        sample_db,
        auth_max_concurrency=2,
        request_timeout_seconds=60.0,
    )
    _, token = issue_device(store, "owner desktop")

    hold = threading.Event()
    running = []
    verified = []

    def blocking_probe(service):
        running.append(1)
        hold.wait(30.0)
        return True

    real = DeviceStore.authenticate

    def counted(self, presented):
        try:
            return real(self, presented)
        finally:
            verified.append(1)

    original_probe = appmod.probe_database
    appmod.probe_database = blocking_probe
    DeviceStore.authenticate = counted
    try:

        async def scenario():
            loop = asyncio.get_running_loop()
            loop.set_default_executor(
                concurrent.futures.ThreadPoolExecutor(max_workers=2)
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://api.test", timeout=60.0
            ) as client:
                probes = [asyncio.create_task(client.get("/readyz")) for _ in range(2)]
                # Both shared workers must really be occupied before the
                # measurement starts, or the test proves nothing.
                deadline = time.monotonic() + 10.0
                while len(running) < 2 and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                assert len(running) == 2, "the shared pool was never saturated"

                pending = []
                for _ in range(4):
                    before = len(verified)
                    pending.append(
                        asyncio.create_task(
                            client.post(
                                "/v1/translations",
                                json={"text": "声骸"},
                                headers=bearer(token),
                            )
                        )
                    )
                    # Give this verification a chance to run. On the shared
                    # pool it never can, so the next request finds the slot
                    # still held and is shed.
                    step = time.monotonic() + 3.0
                    while len(verified) == before and time.monotonic() < step:
                        await asyncio.sleep(0.02)

                hold.set()
                responses = await asyncio.gather(*pending)
                await asyncio.gather(*probes)
                return [item.status_code for item in responses]

        statuses = run(scenario())
    finally:
        hold.set()
        appmod.probe_database = original_probe
        DeviceStore.authenticate = real

    assert 429 not in statuses, statuses
    assert statuses == [200, 200, 200, 200], statuses


def test_the_verification_bound_holds_under_a_flood_of_cancellations(
    tmp_path, sample_db
):
    """The configured maximum must bound concurrent scrypt, not just admission.

    The admission slot has to be released on the loop (a verification cancelled
    while still queued would otherwise strand it forever), but the worker it
    started keeps running — so with a shared, wide executor a flood of
    cancellations let the number of derivations actually running at once drift
    far above the configured maximum. A dedicated pool of exactly
    `auth_max_concurrency` workers is not releasable by anything the caller
    does, so the real bound holds.
    """
    import contextlib

    app, store = build_client_app(
        tmp_path, sample_db, auth_max_concurrency=1, request_timeout_seconds=60.0
    )
    _, token = issue_device(store, "owner desktop")
    state = {"in_flight": 0, "peak": 0}
    guard = threading.Lock()
    real = DeviceStore.authenticate

    def slow(self, presented):
        with guard:
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
        try:
            time.sleep(1.0)
            return real(self, presented)
        finally:
            with guard:
                state["in_flight"] -= 1

    DeviceStore.authenticate = slow
    try:

        async def flood():
            # One loop for the whole flood: a fresh asyncio.run per request
            # would JOIN the default executor between them and serialize the
            # very overlap this measures. The CALLER abandons each request
            # mid-verification, which is what a client disconnect looks like.
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://api.test", timeout=60.0
            ) as client:
                for _ in range(8):
                    task = asyncio.create_task(
                        client.post(
                            "/v1/translations",
                            json={"text": "声骸"},
                            headers=bearer(token),
                        )
                    )
                    await asyncio.sleep(0.15)
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task

        run(flood())
        # Do not leave an abandoned verification running into the next test:
        # this test's whole point is that a started worker outlives the request
        # that scheduled it.
        app.state.auth_pool.shutdown(wait=True)
    finally:
        DeviceStore.authenticate = real

    # Non-vacuous: verification really ran, and never more than the bound.
    # Each abandoned request releases its admission slot immediately (it must,
    # or a cancel while queued would strand it), so without a pool of exactly
    # this width the derivations pile up: 8 cancels 0.15s apart against a 1.0s
    # verification overlapped 7 deep on the shared executor.
    assert state["peak"] == 1, state["peak"]


def test_serve_validates_the_port_override_like_the_environment(monkeypatch, tmp_path):
    """--port used to bypass the range WUWATERM_API_PORT is held to.

    999999 and -1 reached uvicorn and escaped as a raw socket error rather than
    a config error, and 0 was silently discarded by `args.port or settings.port`
    — the operator asked for one port and got another with no diagnostic. Same
    class the bind guard closed for --host.
    """
    import types

    import wuwaterm_api.cli as cli

    monkeypatch.setenv(
        "WUWATERM_API_DEVICE_DB_PATH", str(tmp_path / "state-api" / "devices.db")
    )
    monkeypatch.delenv("WUWATERM_API_PORT", raising=False)
    monkeypatch.delenv("WUWATERM_API_BIND", raising=False)

    called = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: called.setdefault("kw", kw))

    for bad in ("999999", "-1", "0", "65536"):
        called.clear()
        assert cli.main(["serve", "--port", bad]) == 2, bad
        assert "kw" not in called, bad  # never reached uvicorn.run

    # A valid override is honoured, and honoured explicitly.
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    monkeypatch.setattr(
        cli, "DeviceStore", lambda *a, **k: types.SimpleNamespace(initialize=lambda: None)
    )
    monkeypatch.setattr("wuwaterm_api.app.create_app", lambda *a, **k: object())

    assert cli.main(["serve", "--port", "9001"]) == 0
    assert captured["port"] == 9001


def test_a_refused_env_bind_is_recoverable_with_a_host_override(
    monkeypatch, tmp_path, caplog
):
    """The escape hatch has to keep working, and has to be visible.

    A machine whose environment carries a bind this service refuses is
    recovered with `--host 127.0.0.1`: the override is validated on its own and
    the configured value is never consulted, so a bad WUWATERM_API_BIND cannot
    keep the service down. Nothing pinned that combination before, so a
    refactor could have removed the recovery path with a green suite.

    The ignored setting is logged at WARNING — silently discarding a configured
    bind is how an operator ends up believing the environment took effect — but
    the raw value is NOT echoed, the same rule the rest of settings follows.
    """
    import logging
    import types

    import wuwaterm_api.cli as cli

    monkeypatch.setenv("WUWATERM_API_BIND", "0.0.0.0")
    monkeypatch.delenv("WUWATERM_API_PORT", raising=False)
    monkeypatch.setenv(
        "WUWATERM_API_DEVICE_DB_PATH", str(tmp_path / "state-api" / "devices.db")
    )
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    monkeypatch.setattr(
        cli, "DeviceStore", lambda *a, **k: types.SimpleNamespace(initialize=lambda: None)
    )
    monkeypatch.setattr("wuwaterm_api.app.create_app", lambda *a, **k: object())

    with caplog.at_level(logging.WARNING, logger="wuwaterm_api"):
        code = cli.main(["serve", "--host", "127.0.0.1"])

    assert code == 0
    assert captured["host"] == "127.0.0.1"
    messages = [record.getMessage() for record in caplog.records]
    assert any("overrides the configured API bind" in item for item in messages), messages
    assert not any("0.0.0.0" in item for item in messages), messages


def test_the_credential_pool_is_shut_down_with_the_app(tmp_path, sample_db):
    """The dedicated pool is the app's, so the app has to end it.

    A pool that outlives its application keeps worker threads (and whatever
    they hold) alive until the interpreter's own atexit hook runs. Shutdown
    drops anything still queued and joins what already started.
    """
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    async def scenario():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://api.test"
            ) as client:
                response = await client.post(
                    "/v1/translations",
                    json={"text": "声骸"},
                    headers=bearer(token),
                )
            assert response.status_code == 200
            # Usable while the app is running.
            assert app.state.auth_pool.submit(lambda: 7).result(timeout=10) == 7

    run(scenario())

    # And refuses new work once the app has shut down.
    with pytest.raises(RuntimeError):
        app.state.auth_pool.submit(lambda: None)


def test_a_second_lifespan_cycle_gets_a_live_credential_pool(tmp_path, sample_db):
    """`shutdown()` is permanent, so the pool cannot be created once and
    shut down once per application OBJECT.

    An app started twice — two sequential TestClient contexts, an embedding
    that cycles the ASGI lifespan, a supervisor that restarts the app in
    process — reached a dead executor on the second cycle and answered every
    credentialed request with `RuntimeError: cannot schedule new futures after
    shutdown` (a 500). Each cycle gets its own pool.
    """
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    async def cycle():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://api.test"
            ) as client:
                response = await client.post(
                    "/v1/translations",
                    json={"text": "声骸"},
                    headers=bearer(token),
                )
            return response.status_code

    assert run(cycle()) == 200
    assert run(cycle()) == 200


def test_an_old_shape_store_rejects_every_device_id_for_the_same_work(tmp_path):
    """The legacy-store rejection must not become a device-id oracle.

    Tolerating an old-shape row on the request path (so removing `initialize()`
    from it does not turn a legacy store into a distinguishable error class)
    introduced one: a token naming an EXISTING device returned immediately while
    a token naming an absent one still paid for the compensating derivation.
    That timing difference is exactly what the dummy derivation exists to hide.
    Counted rather than timed, so the assertion is about the work itself.
    """
    import sqlite3

    import wuwaterm_api.auth as authmod

    path = tmp_path / "devices.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE devices (device_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO devices VALUES ('cafebabe')")
    store = DeviceStore(path, guard_legacy_default=False)

    calls = []
    real = authmod._derive
    # Count only work done on THIS thread: a verification worker left running
    # by an earlier test (the cancel-flood one deliberately abandons requests
    # mid-derivation) would otherwise land inside the measurement window.
    here = threading.get_ident()

    def counted(secret, salt):
        if threading.get_ident() == here:
            calls.append(1)
        return real(secret, salt)

    authmod._derive = counted
    try:
        known = store.authenticate("wtd1.cafebabe.%s" % ("x" * 40))
        after_known = len(calls)
        unknown = store.authenticate("wtd1.0badc0de.%s" % ("x" * 40))
        after_unknown = len(calls)
    finally:
        authmod._derive = real

    # Both are refusals, and both cost the same derivation.
    assert known is None and unknown is None
    assert after_known == 1, after_known
    assert after_unknown - after_known == 1, (after_known, after_unknown)


def test_an_old_shape_store_with_wrong_typed_columns_is_a_rejection_not_a_500(
    tmp_path, sample_db
):
    """A verifier column that exists but holds TEXT or NULL is still legacy.

    `bytes(row["salt"])` raises TypeError there, which is in neither
    `sqlite3.Error` nor `OSError`, so it would have escaped as an unhandled 500
    instead of the uniform rejection the request path promises.
    """
    import sqlite3

    settings = build_settings(tmp_path, sample_db)
    settings.device_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.device_db_path) as conn:
        conn.execute(
            "CREATE TABLE devices (device_id TEXT PRIMARY KEY, device_name TEXT,"
            " salt TEXT, token_hash TEXT, scopes TEXT, created_at TEXT,"
            " revoked_at TEXT, last_used_at TEXT)"
        )
        conn.execute(
            "INSERT INTO devices VALUES ('cafebabe', 'old', 'not-bytes', NULL,"
            " 'translate,meta', '2020-01-01T00:00:00+00:00', NULL, NULL)"
        )
    app = create_app(
        settings,
        device_store=DeviceStore(settings.device_db_path, guard_legacy_default=False),
    )

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "声骸"},
            headers=bearer("wtd1.cafebabe.%s" % ("x" * 40)),
        )
    )

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "unauthorized"


def test_the_credential_pool_is_shut_down_even_when_the_translator_raises(
    tmp_path, sample_db
):
    """Teardown must not be skippable.

    `asynccontextmanager` throws into the generator at the `yield` when the
    surrounding context exits with an exception, and a translator that raises on
    close would take the pool shutdown with it — leaving worker threads and
    their SQLite handles to the interpreter's atexit hook.
    """

    class _AngryTranslator:
        async def aclose(self):
            raise RuntimeError("translator close failed")

    settings = build_settings(tmp_path, sample_db)
    store = DeviceStore(settings.device_db_path)
    store.initialize()
    app = create_app(settings, device_store=store, translator=_AngryTranslator())

    async def scenario():
        with pytest.raises(RuntimeError, match="translator close failed"):
            async with app.router.lifespan_context(app):
                pass

    run(scenario())

    with pytest.raises(RuntimeError):
        app.state.auth_pool.submit(lambda: None)


def test_a_request_after_teardown_is_503_not_an_unhandled_500(tmp_path, sample_db):
    """A shut-down pool is one more way for the store to be unusable.

    `run_in_executor` on a shut-down executor raises RuntimeError, which is in
    neither `sqlite3.Error` nor `OSError` — so a request landing during or after
    teardown became an unhandled 500: an infrastructure fault dressed as a bug,
    which is the one thing this whole change is removing.
    """
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    app.state.auth_pool.shutdown(wait=True)

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "声骸"},
            headers=bearer(token),
        )
    )

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "internal"


# --------------------------------------------------------------------------
# Pair review
# --------------------------------------------------------------------------

REVIEW_TOP_KEYS = {
    "coverage",
    "dictionary",
    "findings",
    "request_id",
    "rule_version",
    "source_revision",
    "target_revision",
    "truncated",
}


def _assert_review_exact_keys(body: dict) -> None:
    assert set(body) == REVIEW_TOP_KEYS
    assert set(body["dictionary"]) == {"schema_version", "source_commit", "term_count"}
    assert set(body["coverage"]) == {"evaluated", "not_evaluated", "rules"}
    assert isinstance(body["truncated"], bool)
    assert isinstance(body["findings"], list)
    for finding in body["findings"]:
        assert set(finding) == {
            "candidates",
            "candidates_truncated",
            "id",
            "rule_id",
            "source_span",
            "target_span",
            "verdict",
        }
        assert finding["verdict"] in {
            "verified_constraint",
            "confirmed_conflict",
            "needs_review",
            "not_evaluated",
        }
        for span_key in ("source_span", "target_span"):
            span = finding[span_key]
            if span is not None:
                assert set(span) == {"end", "start", "text"}
        assert isinstance(finding["candidates_truncated"], bool)
        for candidate in finding["candidates"]:
            assert set(candidate) == {"category", "en", "sources", "zh"}
            assert "source_file" not in candidate
            assert "source_id" not in candidate
            for source in candidate["sources"]:
                assert set(source) == {"source_file", "source_id"}


def test_review_success_exact_keys(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={"source": "今汐拿到了声骸。", "target": "Jinhsi got an Echo.", "direction": "en"},
            headers=bearer(token),
        )
    )

    assert response.status_code == 200, response.text
    body = response.json()
    _assert_review_exact_keys(body)
    assert body["rule_version"] == "review-v1"
    assert body["truncated"] is False
    assert all(item["verdict"] != "confirmed_conflict" for item in body["findings"])
    assert body["coverage"]["not_evaluated"] > 0


def test_review_empty_source_is_4xx_not_a_report(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={"source": "", "target": "Jinhsi", "direction": "en"},
            headers=bearer(token),
        )
    )

    assert response.status_code == 400
    assert set(response.json()) == {"error", "request_id"}
    assert response.json()["error"]["code"] == "invalid_request"
    assert "findings" not in response.json()


def test_review_empty_target_is_4xx_not_a_report(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={"source": "今汐", "target": "", "direction": "en"},
            headers=bearer(token),
        )
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_review_without_resolutions_never_emits_confirmed_conflict(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={"source": "今汐拿到了声骸。", "target": "Jinhsi got a phantom.", "direction": "en"},
            headers=bearer(token),
        )
    )

    assert response.status_code == 200, response.text
    body = response.json()
    _assert_review_exact_keys(body)
    assert all(item["verdict"] != "confirmed_conflict" for item in body["findings"])


def test_review_overlong_side_is_input_too_long(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db, max_body_bytes=32 * 1024)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={"source": "今" * 2001, "target": "Jinhsi", "direction": "en"},
            headers=bearer(token),
        )
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "input_too_long"


def test_review_does_not_call_translate_request(tmp_path, sample_db, monkeypatch):
    import wuwaterm.application as application

    def boom(*_args, **_kwargs):
        raise AssertionError("POST /v1/reviews must not call translate_request")

    monkeypatch.setattr(application, "translate_request", boom)
    monkeypatch.setattr(application, "translate_request_async", boom)
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={"source": "今汐", "target": "Jinhsi", "direction": "en"},
            headers=bearer(token),
        )
    )

    assert response.status_code == 200, response.text
    _assert_review_exact_keys(response.json())


def test_review_capped_json_fits_max_upstream_bytes(tmp_path, sample_db):
    from wuwaterm.db import connect, insert_records
    from wuwaterm.models import TermRecord

    with connect(sample_db) as conn:
        extra = []
        for candidate in range(8):
            for source_index in range(8):
                extra.append(
                    TermRecord(
                        category="resonator",
                        source_file=("RoleInfoExtra.json/" + "N" * 180) + f"{candidate}_{source_index}",
                        source_id=f"RoleInfo_extra_{candidate}_{source_index}_Name",
                        text_key=f"RoleInfo_extra_{candidate}_{source_index}_Name",
                        zh="今汐",
                        en=f"Jinhsi{candidate}",
                    )
                )
        insert_records(conn, extra)
        conn.commit()

    app, store = build_client_app(tmp_path, sample_db, max_body_bytes=32 * 1024)
    _, token = issue_device(store, "owner desktop")
    source = "今汐，" * 32
    target = "Jinhsi0, " * 32
    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={"source": source, "target": target, "direction": "en"},
            headers=bearer(token),
        )
    )

    assert response.status_code == 200, response.text
    raw = response.content
    assert len(raw) <= 65536
    body = response.json()
    _assert_review_exact_keys(body)
    assert len(body["findings"]) <= 32
    if len(body["findings"]) == 32 or body["truncated"]:
        assert body["truncated"] is True or all(
            len(item["candidates"]) <= 8 for item in body["findings"]
        )


def test_review_v2_matcher_opt_in_adds_matcher_to_wire(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={
                "review_version": "review-v2",
                "source": "今汐拿到了声骸。",
                "target": "Jinhsi got an Echo.",
                "direction": "en",
            },
            headers={**bearer(token), "X-WuwaTerm-Matcher-Basis": "1"},
        )
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == REVIEW_TOP_KEYS | {"matcher_revision"}
    assert body["rule_version"] == "review-v2"
    assert len(body["matcher_revision"]) == 64
    assert all(
        character in "0123456789abcdef" for character in body["matcher_revision"]
    )
    assert set(body["dictionary"]) == {
        "revision",
        "schema_version",
        "source_commit",
        "term_count",
    }
    assert len(body["dictionary"]["revision"]) == 64
    assert all(
        character in "0123456789abcdef" for character in body["dictionary"]["revision"]
    )
    for finding in body["findings"]:
        for candidate in finding["candidates"]:
            assert set(candidate) == {
                "candidate_id",
                "category",
                "en",
                "sources",
                "zh",
            }
            assert len(candidate["candidate_id"]) == 64


def test_review_v2_current_candidate_resolution_round_trip(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    headers = {**bearer(token), "X-WuwaTerm-Matcher-Basis": "1"}
    request = {
        "review_version": "review-v2",
        "source": "今汐拿到了声骸。",
        "target": "Jinhsi got something.",
        "direction": "en",
    }
    baseline = run(call(app, "POST", "/v1/reviews", json=request, headers=headers))
    assert baseline.status_code == 200, baseline.text
    body = baseline.json()
    echo = next(
        item for item in body["findings"] if item["source_span"]["text"] == "声骸"
    )
    official = next(
        item
        for item in echo["candidates"]
        if item["zh"] == "声骸" and item["en"] == "Echo"
    )
    request["resolutions"] = [
        {
            "mention_id": echo["id"],
            "choice": "official_pair",
            "candidate_id": official["candidate_id"],
        }
    ]
    request["resolution_context"] = {
        "source_revision": body["source_revision"],
        "rule_version": body["rule_version"],
        "dictionary_revision": body["dictionary"]["revision"],
    }
    stale = run(call(app, "POST", "/v1/reviews", json=request, headers=headers))
    assert stale.status_code == 400, stale.text
    assert stale.json()["error"]["code"] == "invalid_request"
    assert "stale" in stale.json()["error"]["message"]
    request["resolution_context"]["matcher_revision"] = body["matcher_revision"]

    resolved = run(call(app, "POST", "/v1/reviews", json=request, headers=headers))
    assert resolved.status_code == 200, resolved.text
    resolved_echo = next(
        item
        for item in resolved.json()["findings"]
        if item["source_span"]["text"] == "声骸"
    )
    assert resolved_echo["verdict"] == "confirmed_conflict"


@pytest.mark.parametrize("opt_in", [False, True])
def test_review_v2_rollout_resolution_compatibility(tmp_path, sample_db, opt_in):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "synthetic desktop")
    headers = bearer(token)
    if opt_in:
        headers["X-WuwaTerm-Matcher-Basis"] = "1"
    request = {
        "review_version": "review-v2",
        "source": "今汐拿到了声骸。",
        "target": "Jinhsi got something.",
        "direction": "en",
    }
    baseline = run(call(app, "POST", "/v1/reviews", json=request, headers=headers))
    assert baseline.status_code == 200, baseline.text
    report = baseline.json()
    assert set(report) == REVIEW_TOP_KEYS | ({"matcher_revision"} if opt_in else set())
    finding = next(x for x in report["findings"] if x["source_span"]["text"] == "声骸")
    request["resolutions"] = [{"mention_id": finding["id"], "choice": "not_a_term"}]
    request["resolution_context"] = {
        "source_revision": report["source_revision"],
        "rule_version": report["rule_version"],
        "dictionary_revision": report["dictionary"]["revision"],
    }
    # The intermediate deploy and rollback state is the new API with old Site.
    legacy = run(call(app, "POST", "/v1/reviews", json=request, headers=headers))
    if opt_in:
        assert legacy.status_code == 400, legacy.text
        assert legacy.json()["error"]["code"] == "invalid_request"
        assert "stale" in legacy.json()["error"]["message"]
    else:
        assert legacy.status_code == 200, legacy.text
        assert "matcher_revision" not in legacy.json()
        assert (
            next(x for x in legacy.json()["findings"] if x["id"] == finding["id"])[
                "verdict"
            ]
            == "not_evaluated"
        )

    # Explicit matcher values are authoritative even for a legacy request.
    request["resolution_context"]["matcher_revision"] = "0" * 64
    stale = run(call(app, "POST", "/v1/reviews", json=request, headers=headers))
    assert stale.status_code == 400, stale.text
    assert stale.json()["error"]["code"] == "invalid_request"
    assert "stale" in stale.json()["error"]["message"]
    current = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={
                k: v
                for k, v in request.items()
                if k not in ("resolutions", "resolution_context")
            },
            headers={**bearer(token), "X-WuwaTerm-Matcher-Basis": "1"},
        )
    ).json()
    request["resolution_context"]["matcher_revision"] = current["matcher_revision"]
    accepted = run(call(app, "POST", "/v1/reviews", json=request, headers=headers))
    assert accepted.status_code == 200, accepted.text


@pytest.mark.parametrize("value", ["", "0", "2", "true", " 1", "1, 1"])
def test_review_v2_rejects_unknown_matcher_capability(tmp_path, sample_db, value):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "synthetic desktop")
    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={
                "review_version": "review-v2",
                "source": "今汐。",
                "target": "Jinhsi.",
                "direction": "en",
            },
            headers={**bearer(token), "X-WuwaTerm-Matcher-Basis": value},
        )
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("value", ["1", "unknown"])
def test_review_v1_ignores_matcher_capability(tmp_path, sample_db, value):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "synthetic desktop")
    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={
                "review_version": "review-v1",
                "source": "今汐。",
                "target": "Jinhsi.",
                "direction": "en",
            },
            headers={**bearer(token), "X-WuwaTerm-Matcher-Basis": value},
        )
    )
    assert response.status_code == 200, response.text
    assert set(response.json()) == REVIEW_TOP_KEYS


@pytest.mark.parametrize(
    "extra",
    (
        {"review_version": "review-v3"},
        {"review_version": "review-v1", "alignments": []},
        {"review_version": "review-v1", "alignments": None},
        {"review_version": "review-v1", "resolution_context": None},
        {"review_version": "review-v2", "alignments": None},
        {"review_version": "review-v2", "resolution_context": None},
        {"review_version": "review-v2", "resolutions": None},
        {
            "review_version": "review-v2",
            "resolutions": [
                {
                    "mention_id": "0:2:今汐",
                    "choice": "official_pair",
                    "zh": "今汐",
                    "en": "Jinhsi",
                }
            ],
        },
    ),
)
def test_review_rejects_cross_version_request_shapes(tmp_path, sample_db, extra):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    body = {"source": "今汐", "target": "Jinhsi", "direction": "en"}
    body.update(extra)

    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json=body,
            headers=bearer(token),
        )
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"


def test_review_v2_proxy_rejects_malformed_alignment_before_core(
    tmp_path, sample_db, monkeypatch
):
    import wuwaterm_api.app as app_module

    def boom(*_args, **_kwargs):
        raise AssertionError("syntactically malformed alignment reached review core")

    monkeypatch.setattr(app_module, "review_pair", boom)
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={
                "review_version": "review-v2",
                "source": "今汐",
                "target": "Jinhsi",
                "direction": "en",
                "alignments": [
                    {
                        "source": {"start": True, "end": 2, "text": "今汐"},
                        "target": {"start": 0, "end": 6, "text": "Jinhsi"},
                    }
                ],
            },
            headers=bearer(token),
        )
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"


def test_review_v2_semantic_term_cut_and_stale_context_are_invalid_request(
    tmp_path, sample_db
):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    cut = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={
                "review_version": "review-v2",
                "source": "今汐。",
                "target": "Jinhsi.",
                "direction": "en",
                "alignments": [
                    {
                        "source": {"start": 1, "end": 3, "text": "汐。"},
                        "target": {"start": 0, "end": 7, "text": "Jinhsi."},
                    }
                ],
            },
            headers=bearer(token),
        )
    )
    assert cut.status_code == 400, cut.text
    assert cut.json()["error"]["code"] == "invalid_request"

    baseline = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={
                "review_version": "review-v2",
                "source": "今汐",
                "target": "Jinhsi",
                "direction": "en",
            },
            headers=bearer(token),
        )
    ).json()
    stale = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json={
                "review_version": "review-v2",
                "source": "今汐",
                "target": "Jinhsi",
                "direction": "en",
                "resolutions": [
                    {
                        "mention_id": baseline["findings"][0]["id"],
                        "choice": "not_a_term",
                    }
                ],
                "resolution_context": {
                    "source_revision": "0" * 64,
                    "rule_version": "review-v2",
                    "dictionary_revision": baseline["dictionary"]["revision"],
                },
            },
            headers=bearer(token),
        )
    )
    assert stale.status_code == 400, stale.text
    assert stale.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("change, expected", [("revoke", 401), ("store_error", 503), ("active", 200)])
def test_queued_translation_rechecks_device_before_spending(
    tmp_path, sample_db, monkeypatch, change, expected
):
    import sqlite3

    app, store = build_client_app(tmp_path, sample_db, llm_max_concurrency=1)
    device, token = issue_device(store, "queued synthetic device")
    calls = []

    async def response_factory(locked_text, locks):
        return "Translated sentence."

    enable_mock_llm(monkeypatch, calls, response_factory)

    async def scenario():
        slots = app.state.translator._current_llm_slots()
        acquire = slots.acquire
        await acquire()  # Occupied by a synthetic holder, no upstream/budget.
        queued = asyncio.Event()

        async def observed_acquire():
            assert slots.locked()
            queued.set()
            return await acquire()

        monkeypatch.setattr(slots, "acquire", observed_acquire)
        task = asyncio.create_task(call(
            app, "POST", "/v1/translations",
            json={"text": "这是一句没有词典命中的完整句子。"},
            headers=bearer(token),
        ))
        try:
            await asyncio.wait_for(queued.wait(), timeout=5)
            assert not task.done()
            assert calls == []
            assert app.state.llm_budget.in_window() == 0
            if change == "revoke":
                store.revoke(device.device_id)
            elif change == "store_error":
                def unavailable(device_id):
                    raise sqlite3.OperationalError("synthetic store unavailable")
                monkeypatch.setattr(store, "is_active", unavailable)
            slots.release()
            return await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    response = run(scenario())
    assert response.status_code == expected
    spent = 1 if change == "active" else 0
    assert len(calls) == spent
    assert app.state.llm_budget.in_window() == spent
    if change != "active":
        assert response.json()["error"]["code"] == (
            "unauthorized" if change == "revoke" else "internal"
        )


@pytest.mark.parametrize("change, expected", [("revoke", 401), ("store_error", 200)])
def test_translation_retains_final_device_recheck_policy(
    tmp_path, sample_db, monkeypatch, change, expected
):
    import sqlite3

    app, store = build_client_app(tmp_path, sample_db)
    device, token = issue_device(store, "inflight synthetic device")
    calls = []

    async def response_factory(locked_text, locks):
        if change == "revoke":
            store.revoke(device.device_id)
        else:
            def unavailable(device_id):
                raise sqlite3.OperationalError("synthetic store unavailable")
            monkeypatch.setattr(store, "is_active", unavailable)
        return "Translated sentence."

    enable_mock_llm(monkeypatch, calls, response_factory)
    response = run(call(
        app, "POST", "/v1/translations",
        json={"text": "这是一句没有词典命中的完整句子。"}, headers=bearer(token),
    ))
    assert response.status_code == expected
    assert len(calls) == 1
    assert app.state.llm_budget.in_window() == 1
