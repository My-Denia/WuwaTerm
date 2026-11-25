"""HTTP surface of POST /v1/structure-checks (structure-check-v1).

The last test pins `/v1/reviews`: its published schemas and its response bytes
are fixed to what Core 0.13.0 served before this endpoint existed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

import pytest

from tests.test_api import bearer, build_client_app, call, issue_device, run
from wuwaterm_api.auth import SCOPE_META

BODY_LIMIT = 32 * 1024
SEGMENT_KEYS = {"index", "segment_id", "findings", "omitted_findings", "layout"}


def structure_app(tmp_path, sample_db, **overrides):
    overrides.setdefault("max_body_bytes", BODY_LIMIT)
    app, store = build_client_app(tmp_path, sample_db, **overrides)
    _, token = issue_device(store, "owner desktop")
    return app, store, token


def check(app, token, segments, **extra):
    body = {"check_version": "structure-check-v1", "segments": segments, **extra}
    return run(
        call(
            app,
            "POST",
            "/v1/structure-checks",
            content=json.dumps(body).encode(),
            headers={**bearer(token), "Content-Type": "application/json"},
        )
    )


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def test_structure_check_reports_each_segment_in_request_order(tmp_path, sample_db):
    app, _, token = structure_app(tmp_path, sample_db)
    segments = [
        {"segment_id": "intro", "source": "造成300点伤害{0}", "target": "Deals 30 damage {0}"},
        {"source": "第一段\n\n第二段", "target": "   "},
        {"segment_id": None, "source": "获得1.5万星声", "target": "Obtain 15,000 Astrite"},
    ]
    response = check(app, token, segments)
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body) == ["request_id", "check_version", "checks", "segments"]
    assert body["check_version"] == "structure-check-v1"
    assert body["checks"] == ["empty_target", "numbers", "placeholders", "paragraphs"]
    first, second, third = body["segments"]
    assert all(set(item) == SEGMENT_KEYS for item in body["segments"])
    assert (first["index"], first["segment_id"]) == (0, "intro")
    assert first["findings"] == [
        {
            "rule_id": "number_missing",
            "source_span": {"start": 2, "end": 5, "text": "300"},
            "target_span": None,
        },
        {
            "rule_id": "number_unexpected",
            "source_span": None,
            "target_span": {"start": 6, "end": 8, "text": "30"},
        },
    ]
    assert first["layout"] == {
        "source_paragraphs": 1,
        "target_paragraphs": 1,
        "source_lines": 1,
        "target_lines": 1,
    }
    assert (second["index"], second["segment_id"]) == (1, None)
    assert second["findings"] == [
        {"rule_id": "empty_target", "source_span": None, "target_span": None}
    ]
    assert second["layout"]["source_paragraphs"] == 2
    assert (third["index"], third["findings"], third["omitted_findings"]) == (2, [], 0)

    again = check(app, token, segments).json()
    assert again["request_id"] != body["request_id"]
    assert again["segments"] == body["segments"]


def test_structure_check_caps_findings_per_segment(tmp_path, sample_db):
    app, _, token = structure_app(tmp_path, sample_db)
    source = " ".join(str(number) for number in range(100, 120))
    response = check(app, token, [{"source": source, "target": "none"}])
    assert response.status_code == 200, response.text
    (segment,) = response.json()["segments"]
    assert len(segment["findings"]) == 16
    assert segment["omitted_findings"] == 4
    assert [item["source_span"]["text"] for item in segment["findings"]] == [
        str(number) for number in range(100, 116)
    ]


def test_structure_check_answers_numerals_too_large_to_print(tmp_path, sample_db):
    # 亿 repeated multiplies by 10**8 each time; 538 of them used to build an
    # integer past Python's digit limit for str() and answer 500.
    app, _, token = structure_app(tmp_path, sample_db)
    response = check(app, token, [{"source": "亿" * 600, "target": "test"}])
    assert response.status_code == 200, response.text
    assert response.json()["segments"][0]["findings"] == []


def test_structure_check_needs_the_translate_scope_and_spends_the_allowance(
    tmp_path, sample_db
):
    app, store, token = structure_app(tmp_path, sample_db, rate_limit_per_minute=2)
    _, meta_only = issue_device(store, "meta reader", scopes=[SCOPE_META])
    segment = [{"source": "1", "target": "1"}]

    assert_error(check(app, "wtd1.missing.credential", segment), 401, "unauthorized")
    assert_error(check(app, meta_only, segment), 403, "forbidden")
    assert check(app, token, segment).status_code == 200
    assert check(app, token, segment).status_code == 200
    assert_error(check(app, token, segment), 429, "rate_limited")


@pytest.mark.parametrize(
    "body",
    [
        {"segments": [{"source": "a", "target": "b"}]},
        {"check_version": "structure-check-v2", "segments": [{"source": "a", "target": "b"}]},
        {"check_version": "structure-check-v1", "segments": []},
        {"check_version": "structure-check-v1", "segments": [{"source": "a", "target": "b"}] * 65},
        {"check_version": "structure-check-v1", "segments": [{"source": "a", "target": "b"}], "x": 1},
        {"check_version": "structure-check-v1", "segments": [{"source": "a", "target": "b", "x": 1}]},
        {"check_version": "structure-check-v1", "segments": [{"source": "a"}]},
        {"check_version": "structure-check-v1", "segments": [{"source": 1, "target": "b"}]},
        {"check_version": "structure-check-v1", "segments": [{"segment_id": "", "source": "a", "target": "b"}]},
        {"check_version": "structure-check-v1", "segments": [{"segment_id": "a b", "source": "a", "target": "b"}]},
        {"check_version": "structure-check-v1", "segments": [{"segment_id": 7, "source": "a", "target": "b"}]},
        {
            "check_version": "structure-check-v1",
            "segments": [
                {"segment_id": "s", "source": "a", "target": "b"},
                {"segment_id": "s", "source": "c", "target": "d"},
            ],
        },
        {"check_version": "structure-check-v1", "segments": [{"source": "a", "target": "\ud800"}]},
    ],
)
def test_structure_check_rejects_malformed_bodies(tmp_path, sample_db, body):
    app, _, token = structure_app(tmp_path, sample_db)
    response = run(
        call(
            app,
            "POST",
            "/v1/structure-checks",
            content=json.dumps(body).encode(),
            headers={**bearer(token), "Content-Type": "application/json"},
        )
    )
    assert_error(response, 400, "invalid_request")


def test_structure_check_bounds_text_length_and_body_size(tmp_path, sample_db):
    app, _, token = structure_app(tmp_path, sample_db)
    assert check(app, token, [{"source": "a" * 2000, "target": "b" * 2000}]).status_code == 200
    assert_error(
        check(app, token, [{"source": "a", "target": "b" * 2001}]), 422, "input_too_long"
    )
    ten = [{"source": "a" * 1000, "target": "b" * 1000}] * 10
    assert check(app, token, ten).status_code == 200
    assert_error(
        check(app, token, ten + [{"source": "a", "target": ""}]), 422, "input_too_long"
    )

    small, _, small_token = structure_app(tmp_path / "small", sample_db, max_body_bytes=2048)
    assert_error(
        check(small, small_token, [{"source": "a" * 2000, "target": "b"}]),
        413,
        "payload_too_large",
    )


def test_structure_check_logs_counts_and_never_text(tmp_path, sample_db, caplog):
    caplog.set_level(logging.INFO)
    app, _, token = structure_app(tmp_path, sample_db)
    response = check(
        app,
        token,
        [{"segment_id": "s1", "source": "原文哨兵 7 {Sentinel}", "target": "TargetSentinel 8"}],
    )
    assert response.status_code == 200, response.text
    lines = [record.getMessage() for record in caplog.records]
    (line,) = [item for item in lines if item.startswith("structure check ")]
    assert "segments=1" in line and "findings=3" in line and "omitted=0" in line
    for text in ("原文哨兵", "Sentinel", "TargetSentinel", "s1"):
        assert all(text not in item for item in lines)


# --------------------------------------------------------------------------
# /v1/reviews is untouched
# --------------------------------------------------------------------------

# Recorded on Core 0.13.0 (e5b7f1a) before structure checks were added. A
# legitimate change to /v1/reviews updates these together with its own tests.
REVIEW_CONTRACT_SHA256 = "75ad601b8dc7b5127e7cf2da17304cd43e3fd7fbf0670f56efd99b3970485f7d"
REVIEW_RESPONSE_SHA256 = {
    "review-v1": "e05fbb6238692bc470837dc1d138d386b06a293863f1f8cc186be00c1f480b91",
    "review-v2": "9360a29eef240a97050c7d4f43b1187fca4df20faa1ecda6ee918d1201017e78",
    "review-v2-basis-features": "bac1f254604a006dafaa341d9b5b7b90b712dc16e31cf10ca3295192f66adfc4",
    "invalid": "ab443ded6062379594904b9c323234d2aecb7a61d422b658c7e6607b6d338b8a",
}
_REVIEW_SCHEMAS = (
    "ErrorDetailBody",
    "ErrorResponseBody",
    "NotATermResolutionBody",
    "OfficialPairResolutionBody",
    "ReviewAlignmentBody",
    "ReviewAlignmentSpanInputBody",
    "ReviewCandidateBody",
    "ReviewCoverageBody",
    "ReviewDictionaryBody",
    "ReviewFindingBody",
    "ReviewRequestBody",
    "ReviewResolutionContextBody",
    "ReviewResponseBody",
    "ReviewSourceBody",
    "ReviewSpanBody",
    "V2OfficialPairResolutionBody",
)
_REQUEST_ID = re.compile(rb'"request_id":"[0-9a-f]{32}"')


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _review_contract(document: dict) -> bytes:
    schemas = document["components"]["schemas"]
    subset = {
        "path": document["paths"]["/v1/reviews"],
        "schemas": {name: schemas[name] for name in _REVIEW_SCHEMAS},
    }
    return json.dumps(subset, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _review_bytes(app, token: str, body: dict, headers: dict | None = None) -> bytes:
    response = run(
        call(
            app,
            "POST",
            "/v1/reviews",
            json=body,
            headers={**bearer(token), **(headers or {})},
        )
    )
    return response.headers.get("X-WuwaTerm-Review-Features", "").encode() + b"|" + (
        str(response.status_code).encode()
        + b"|"
        + _REQUEST_ID.sub(b'"request_id":"-"', response.content)
    )


def test_reviews_schema_and_response_bytes_are_unchanged(tmp_path, sample_db):
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "owner desktop")
    pair = {"source": "今汐拿到了声骸。", "target": "Jinhsi got an Echo.", "direction": "en"}
    observed = {
        "review-v1": _review_bytes(app, token, pair),
        "review-v2": _review_bytes(app, token, {**pair, "review_version": "review-v2"}),
        "review-v2-basis-features": _review_bytes(
            app,
            token,
            {**pair, "review_version": "review-v2", "target": "Jinhsi got something."},
            {
                "X-WuwaTerm-Matcher-Basis": "1",
                "X-WuwaTerm-Review-Features": "dense-128,reason-v1",
            },
        ),
        "invalid": _review_bytes(app, token, {**pair, "segments": []}),
    }
    assert _sha256(_review_contract(app.openapi())) == REVIEW_CONTRACT_SHA256
    assert {key: _sha256(value) for key, value in observed.items()} == REVIEW_RESPONSE_SHA256
