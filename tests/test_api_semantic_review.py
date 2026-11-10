"""HTTP surface of GET /v1/capabilities and POST /v1/semantic-reviews (contract only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_api import bearer, build_client_app, call, issue_device, run
from wuwaterm_api.auth import SCOPE_META, SCOPE_TRANSLATE

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contracts" / "semantic-review-v1"
MANIFEST = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
JSON_HEADERS = {"Content-Type": "application/json"}
PRODUCTION_BODY_LIMIT = 32 * 1024


def configure_llm(monkeypatch) -> list:
    """A configured model that must never be called by this build."""
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    calls: list = []

    async def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("semantic review must not call the model in this build")

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", forbidden)
    return calls


def semantic_app(tmp_path, sample_db, **overrides):
    overrides.setdefault("max_body_bytes", PRODUCTION_BODY_LIMIT)
    app, store = build_client_app(tmp_path, sample_db, **overrides)
    _, token = issue_device(store, "reviewer", [SCOPE_META, SCOPE_TRANSLATE])
    return app, store, token


def post_raw(app, token: str, data: bytes):
    return run(
        call(
            app,
            "POST",
            "/v1/semantic-reviews",
            content=data,
            headers={**bearer(token), **JSON_HEADERS},
        )
    )


def valid_request(**overrides) -> dict:
    body = {
        "source": "甲乙不会出发。",
        "target": "Jia and Yi will leave.",
        "direction": "en",
        "review_version": "semantic-review-v1",
        "explain_language": "zh",
        "precedents": [],
    }
    body.update(overrides)
    return body


def precedent(**overrides) -> dict:
    item = {
        "surface": "甲乙",
        "choice": "official_pair",
        "rendering": "Jia and Yi",
        "snippet": "甲乙不会出发。",
        "anchor": {"segment_index": 0, "source_start": 0, "source_end": 2},
        "scope": "occurrence",
    }
    item.update(overrides)
    return item


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------


def test_capabilities_lists_nothing_before_the_engine_exists(tmp_path, sample_db, monkeypatch):
    configure_llm(monkeypatch)
    app, _, token = semantic_app(tmp_path, sample_db)

    response = run(call(app, "GET", "/v1/capabilities", headers=bearer(token)))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"api_version", "capabilities", "request_id"}
    assert body["api_version"] == "v1"
    assert body["capabilities"] == []


def test_capabilities_needs_meta_scope_and_spares_the_business_allowance(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db, rate_limit_per_minute=1)
    _, meta = issue_device(store, "reader", [SCOPE_META])
    _, translate_only = issue_device(store, "translator", [SCOPE_TRANSLATE])

    assert run(call(app, "GET", "/v1/capabilities")).status_code == 401
    assert_error(
        run(call(app, "GET", "/v1/capabilities", headers=bearer(translate_only))),
        403,
        "forbidden",
    )
    for _ in range(3):
        probe = run(call(app, "GET", "/v1/capabilities", headers=bearer(meta)))
        assert probe.status_code == 200
    # Three probes later the one business request per minute is still unspent.
    assert run(call(app, "GET", "/v1/meta", headers=bearer(meta))).status_code == 200


# --------------------------------------------------------------------------
# Semantic review: validated, then refused without a model call
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case", MANIFEST["cases"], ids=lambda case: case["name"]
)
def test_every_fixture_request_is_accepted_and_answered_unavailable(
    tmp_path, sample_db, monkeypatch, case
):
    calls = configure_llm(monkeypatch)
    app, _, token = semantic_app(tmp_path, sample_db)
    data = (FIXTURES / case["request"]).read_bytes()

    response = post_raw(app, token, data)

    assert_error(response, 503, "llm_unavailable")
    assert calls == []


def test_request_one_byte_over_the_limit_is_too_large(tmp_path, sample_db, monkeypatch):
    configure_llm(monkeypatch)
    app, _, token = semantic_app(tmp_path, sample_db)
    data = (FIXTURES / "near-limit-request.json").read_bytes()
    oversized = data + b" " * (PRODUCTION_BODY_LIMIT + 1 - len(data))
    assert len(oversized) == PRODUCTION_BODY_LIMIT + 1

    assert_error(post_raw(app, token, oversized), 413, "payload_too_large")


@pytest.mark.parametrize("side", ["source", "target"])
def test_a_side_over_2000_scalars_is_input_too_long_like_reviews(
    tmp_path, sample_db, side
):
    app, _, token = semantic_app(tmp_path, sample_db)
    body = valid_request(**{side: "甲" * 2001})

    response = post_raw(app, token, json.dumps(body, ensure_ascii=False).encode())

    assert_error(response, 422, "input_too_long")


@pytest.mark.parametrize(
    "body",
    [
        valid_request(extra="field"),
        valid_request(review_version="review-v2"),
        valid_request(explain_language="fr"),
        valid_request(direction="ja"),
        valid_request(precedents=[precedent()] * 13),
        valid_request(precedents=[precedent(choice="not_a_term")]),
        valid_request(precedents=[precedent(rendering=None)]),
        valid_request(precedents=[precedent(rendering="")]),
        valid_request(precedents=[precedent(scope="document")]),
        valid_request(precedents=[precedent(surface="甲" * 33)]),
        valid_request(precedents=[precedent(snippet="甲" * 81)]),
        valid_request(
            precedents=[precedent(anchor={"segment_index": 0, "source_start": 2, "source_end": 2})]
        ),
        valid_request(context={"before": {"source": "甲" * 401, "target": ""}}),
        valid_request(context={"before": {"source": "", "target": "", "after": ""}}),
    ],
    ids=[
        "unknown-key",
        "wrong-version",
        "explain-language",
        "direction",
        "too-many-precedents",
        "not-a-term-with-rendering",
        "official-pair-without-rendering",
        "empty-rendering",
        "document-scope",
        "long-surface",
        "long-snippet",
        "empty-anchor",
        "long-context",
        "unknown-context-key",
    ],
)
def test_invalid_requests_are_refused_before_anything_else(tmp_path, sample_db, body):
    app, _, token = semantic_app(tmp_path, sample_db)

    response = post_raw(app, token, json.dumps(body, ensure_ascii=False).encode())

    assert_error(response, 400, "invalid_request")


def test_isolated_surrogates_are_refused(tmp_path, sample_db):
    app, _, token = semantic_app(tmp_path, sample_db)
    data = json.dumps(valid_request(target="\ud800")).encode()

    assert_error(post_raw(app, token, data), 400, "invalid_request")


def test_not_a_term_precedent_without_rendering_is_valid(tmp_path, sample_db, monkeypatch):
    configure_llm(monkeypatch)
    app, _, token = semantic_app(tmp_path, sample_db)
    body = valid_request(precedents=[precedent(choice="not_a_term", rendering=None)])

    response = post_raw(app, token, json.dumps(body, ensure_ascii=False).encode())

    assert_error(response, 503, "llm_unavailable")


def test_semantic_review_needs_the_translate_scope(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db, max_body_bytes=PRODUCTION_BODY_LIMIT)
    _, meta_only = issue_device(store, "reader", [SCOPE_META])

    response = post_raw(app, meta_only, json.dumps(valid_request()).encode())

    assert_error(response, 403, "forbidden")
