"""semantic-review-v1 serializer and the contract fixtures it generates."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from starlette.responses import JSONResponse

from wuwaterm.semantic_review_contract import (
    CATEGORIES,
    MAX_EXCERPT_SCALARS,
    MAX_ISSUES,
    MAX_REPLACEMENT_SCALARS,
    MAX_SUGGESTION_SPAN_SCALARS,
    REQUEST_BYTE_LIMIT,
    SEMANTIC_RESPONSE_BYTE_BUDGET,
    Alternative,
    AnchoredIssue,
    SemanticContractError,
    Span,
    Suggestion,
    build_not_reviewed,
    build_response,
    excerpt,
    issue_fingerprint,
    normalize_quote,
    serialize,
    text_revision,
)
from wuwaterm_api.app import SemanticReviewRequestBody, SemanticReviewResponseBody

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contracts" / "semantic-review-v1"
BASIS = {"dictionary_revision": "a" * 64, "matcher_revision": "b" * 64}


def _load_generator():
    path = ROOT / "scripts" / "build_semantic_review_fixtures.py"
    spec = importlib.util.spec_from_file_location("build_semantic_review_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GENERATOR = _load_generator()
MANIFEST = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
RESPONSE_CASES = [case for case in MANIFEST["cases"] if case["response"]]


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _issue(**overrides) -> AnchoredIssue:
    fields = dict(
        category="meaning_shift",
        confidence="medium",
        source_span=Span(0, 2),
        target_span=Span(0, 3),
        rationale="r",
    )
    fields.update(overrides)
    return AnchoredIssue(**fields)


# --------------------------------------------------------------------------
# Fixtures are the serializer's output, byte for byte
# --------------------------------------------------------------------------


def test_committed_fixtures_equal_the_generator_output():
    files = GENERATOR.render_files()
    assert sorted(files) == sorted(path.name for path in FIXTURES.glob("*.json"))
    for name, data in files.items():
        assert _fixture(name) == data, name


@pytest.mark.parametrize("case", RESPONSE_CASES, ids=lambda case: case["name"])
def test_response_fixture_is_a_valid_response_to_its_request(case):
    request_data = _fixture(case["request"])
    response_data = _fixture(case["response"])
    request = json.loads(request_data)
    response = json.loads(response_data)

    assert len(request_data) == case["request_bytes"] <= REQUEST_BYTE_LIMIT
    assert len(response_data) == case["response_bytes"] <= SEMANTIC_RESPONSE_BYTE_BUDGET
    # Exactly what Starlette's JSONResponse would put on the wire.
    assert JSONResponse(content=response).body == response_data == serialize(response)
    SemanticReviewRequestBody.model_validate(request)
    SemanticReviewResponseBody.model_validate(response)

    source, target = request["source"], request["target"]
    assert response["source_revision"] == text_revision(source)
    assert response["target_revision"] == text_revision(target)
    assert len(response["issues"]) <= MAX_ISSUES
    assert response["truncated"] is (response["omitted_issues"] > 0)
    for issue in response["issues"]:
        for side_name, side in (("source", source), ("target", target)):
            span = issue[f"{side_name}_span"]
            if span is None:
                assert issue[f"{side_name}_excerpt"] is None
                continue
            # The span text is complete, never shortened.
            assert span["text"] == side[span["start"] : span["end"]]
            assert issue[f"{side_name}_excerpt"] == excerpt(span["text"])
        assert issue["source_span"] or issue["target_span"]
        suggestion = issue["suggestion"]
        if suggestion is None:
            assert issue["alternatives"] == []
            continue
        assert issue["suggestion_withheld"] is None
        span = suggestion["target_span"]
        assert span["text"] == target[span["start"] : span["end"]]
        assert span["end"] - span["start"] <= MAX_SUGGESTION_SPAN_SCALARS
        assert len(suggestion["replacement"]) <= MAX_REPLACEMENT_SCALARS


def test_max_response_sits_within_one_kib_below_the_budget():
    case = GENERATOR.case_max()
    data = _fixture("max-response.json")
    body = json.loads(data)

    assert case.response.data == data
    assert SEMANTIC_RESPONSE_BYTE_BUDGET - 1024 <= len(data) <= SEMANTIC_RESPONSE_BYTE_BUDGET
    assert body["truncated_reason"] == "byte_limit"
    assert body["omitted_issues"] >= 10
    # The next issue, with the counters it would carry, does not fit.
    assert case.response.next_size > SEMANTIC_RESPONSE_BYTE_BUDGET


def test_byte_limit_fixture_drops_whole_issues_without_shortening_spans():
    case = GENERATOR.case_byte_limit()
    body = json.loads(_fixture("byte-limit-response.json"))

    assert body["truncated_reason"] == "byte_limit"
    assert body["omitted_issues"] == case.response.total - case.response.kept >= 1
    assert case.response.next_size > SEMANTIC_RESPONSE_BYTE_BUDGET
    for issue in body["issues"]:
        assert issue["source_span"]["text"] == case.request["source"]
        assert issue["target_span"]["text"] == case.request["target"]


def test_suggestion_withheld_fixtures():
    long_span = json.loads(_fixture("suggestion-span-too-long-response.json"))["issues"][0]
    assert long_span["suggestion_withheld"] == "span_too_long"
    assert long_span["suggestion"] is None and long_span["alternatives"] == []
    assert len(long_span["target_span"]["text"]) == 300
    assert len(long_span["target_excerpt"]) == MAX_EXCERPT_SCALARS - 9

    long_replacement = json.loads(
        _fixture("suggestion-replacement-too-long-response.json")
    )["issues"][0]
    assert long_replacement["suggestion_withheld"] == "replacement_too_long"
    assert long_replacement["suggestion"] is None

    boundary = json.loads(_fixture("suggestion-boundary-response.json"))["issues"][0]
    assert boundary["suggestion_withheld"] is None
    assert len(boundary["suggestion"]["target_span"]["text"]) == 200
    assert len(boundary["suggestion"]["replacement"]) == 240
    assert len(boundary["alternatives"][0]["replacement"]) == 240


def test_not_reviewed_and_dropped_fixtures():
    not_reviewed = json.loads(_fixture("not-reviewed-response.json"))
    assert not_reviewed["status"] == "not_reviewed"
    assert not_reviewed["status_reason"] == "invalid_model_output"
    assert not_reviewed["checked"] == [] and not_reviewed["issues"] == []

    dropped = json.loads(_fixture("dropped-unanchored-response.json"))
    assert dropped["status"] == "complete" and dropped["dropped_unanchored"] == 3
    assert dropped["checked"] == list(CATEGORIES)


def test_near_limit_request_is_just_under_the_request_limit():
    data = _fixture("near-limit-request.json")
    assert REQUEST_BYTE_LIMIT - 64 <= len(data) <= REQUEST_BYTE_LIMIT
    request = SemanticReviewRequestBody.model_validate(json.loads(data))
    assert len(request.source) == len(request.target) == 2000
    assert len(request.precedents) == 12


# --------------------------------------------------------------------------
# Serializer rules
# --------------------------------------------------------------------------


def test_worst_case_single_issue_fits_the_budget():
    # Every text field at its limit and made of 6-byte JSON escapes.
    source = target = "\u0001" * 2000
    issue = AnchoredIssue(
        category="omission",
        confidence="high",
        source_span=Span(0, 2000),
        target_span=Span(0, 2000),
        rationale="\u0001" * 240,
        suggestion=Suggestion(Span(0, 200), "\u0001" * 240, "none"),
        alternatives=(Alternative("\u0001" * 240, "none"),),
    )
    fitted = build_response(
        request_id="r", source=source, target=target, term_basis=BASIS, issues=[issue]
    )
    assert fitted.kept == 1
    assert len(fitted.data) <= SEMANTIC_RESPONSE_BYTE_BUDGET


def test_fit_measures_the_final_response_with_its_counters():
    issues = [
        _issue(source_span=Span(index, index + 1), target_span=None, rationale="x" * 200)
        for index in range(12)
    ]
    full = build_response(
        request_id="r", source="a" * 20, target="b" * 20, term_basis=BASIS, issues=issues
    )
    assert full.kept == 12
    # A budget one byte short of the full response must cost exactly one issue,
    # and the reported size is the real size of what is returned.
    tight = build_response(
        request_id="r",
        source="a" * 20,
        target="b" * 20,
        term_basis=BASIS,
        issues=issues,
        byte_budget=len(full.data) - 1,
    )
    assert tight.kept == 11
    assert tight.body["omitted_issues"] == 1
    assert tight.body["truncated_reason"] == "byte_limit"
    assert len(serialize(tight.body)) == len(tight.data) <= len(full.data) - 1
    assert tight.next_size == len(full.data)


def test_issue_limit_versus_byte_limit_reason():
    issues = [_issue(source_span=Span(i, i + 1), target_span=None) for i in range(18)]
    by_count = build_response(
        request_id="r", source="a" * 20, target="b", term_basis=BASIS, issues=issues
    )
    assert by_count.kept == 16 and by_count.body["truncated_reason"] == "issue_limit"
    assert by_count.body["omitted_issues"] == 2
    by_bytes = build_response(
        request_id="r",
        source="a" * 20,
        target="b",
        term_basis=BASIS,
        issues=issues,
        byte_budget=len(by_count.data) - 1,
    )
    assert by_bytes.kept == 15 and by_bytes.body["truncated_reason"] == "byte_limit"
    assert by_bytes.body["omitted_issues"] == 3


def test_order_is_confidence_then_source_then_target_position():
    issues = [
        _issue(confidence="low", source_span=Span(0, 1)),
        _issue(confidence="high", source_span=Span(5, 6)),
        _issue(confidence="high", source_span=None, target_span=Span(0, 1)),
        _issue(confidence="high", source_span=Span(1, 2)),
        _issue(confidence="medium", source_span=Span(0, 1), target_span=Span(1, 3)),
    ]
    body = build_response(
        request_id="r", source="a" * 10, target="b" * 10, term_basis=BASIS, issues=issues
    ).body
    order = [
        (issue["confidence"], issue["source_span"] and issue["source_span"]["start"])
        for issue in body["issues"]
    ]
    assert order == [("high", 1), ("high", 5), ("high", None), ("medium", 0), ("low", 0)]


def test_duplicate_ids_keep_the_most_confident_issue():
    body = build_response(
        request_id="r",
        source="ab",
        target="abc",
        term_basis=BASIS,
        issues=[
            _issue(confidence="low", rationale="low"),
            _issue(confidence="high", rationale="first high"),
            _issue(confidence="high", rationale="second high"),
        ],
    ).body
    assert [issue["rationale"] for issue in body["issues"]] == ["first high"]
    assert body["omitted_issues"] == 0


def test_ids_bind_to_position_and_fingerprints_do_not():
    first = build_response(
        request_id="r", source="ab ab", target="xy xy", term_basis=BASIS,
        issues=[_issue(source_span=Span(0, 2), target_span=Span(0, 2))],
    ).body["issues"][0]
    second = build_response(
        request_id="r", source="ab ab", target="xy xy", term_basis=BASIS,
        issues=[_issue(source_span=Span(3, 5), target_span=Span(3, 5))],
    ).body["issues"][0]
    assert first["id"] != second["id"]
    assert first["fingerprint"] == second["fingerprint"]
    assert len(first["id"]) == len(first["fingerprint"]) == 32


def test_fingerprint_normalization():
    assert normalize_quote("  Ｅｎｃｏｒｅ\u3000 Song\n") == "encore song"
    assert issue_fingerprint("term_sense", "Encore", None) == issue_fingerprint(
        "term_sense", " ENCORE ", None
    )
    # A missing side and an empty quote are different inputs.
    assert issue_fingerprint("omission", "a", None) != issue_fingerprint(
        "omission", "a", ""
    )


def test_excerpt_and_rationale_limits():
    assert excerpt("x" * 200) == "x" * 200
    long = "a" * 95 + "m" * 11 + "z" * 95
    assert excerpt(long) == "a" * 95 + "…" + "z" * 95
    body = build_response(
        request_id="r", source="ab", target="abc", term_basis=BASIS,
        issues=[_issue(rationale="r" * 300)],
    ).body
    assert body["issues"][0]["rationale"] == "r" * 239 + "…"


def test_alternatives_follow_their_suggestion():
    span = Span(0, 3)
    body = build_response(
        request_id="r", source="ab", target="abc", term_basis=BASIS,
        issues=[
            _issue(
                suggestion=Suggestion(span, "x", "none"),
                alternatives=(
                    Alternative("y" * 241, "none"),
                    Alternative("z", "none"),
                    Alternative("w", "none"),
                ),
            )
        ],
    ).body
    # Over-long alternatives are skipped; at most one is kept.
    assert body["issues"][0]["alternatives"] == [{"replacement": "z", "term_effect": "none"}]


@pytest.mark.parametrize(
    "overrides",
    [
        {"category": "verified_constraint"},
        {"confidence": "certain"},
        {"source_span": None, "target_span": None},
        {"source_span": Span(1, 1)},
        {"source_span": Span(0, 3)},
        {"target_span": Span(-1, 1)},
        {"rationale": "\ud800"},
        {"suggestion": Suggestion(Span(0, 4), "x", "none")},
        {"suggestion": Suggestion(Span(0, 1), "x", "conflict")},
        {"alternatives": (Alternative("x", "none"),)},
    ],
)
def test_invalid_issues_are_refused(overrides):
    with pytest.raises(SemanticContractError):
        build_response(
            request_id="r", source="ab", target="abc", term_basis=BASIS,
            issues=[_issue(**overrides)],
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": "a" * 2001},
        {"target": "\udfff"},
        {"term_basis": {"dictionary_revision": "a" * 64}},
        {"term_basis": {"dictionary_revision": "A" * 64, "matcher_revision": "b" * 64}},
        {"dropped_unanchored": -1},
    ],
)
def test_invalid_envelopes_are_refused(kwargs):
    arguments = dict(request_id="r", source="ab", target="abc", term_basis=BASIS, issues=[])
    arguments.update(kwargs)
    with pytest.raises(SemanticContractError):
        build_response(**arguments)


def test_not_reviewed_shape():
    body = build_not_reviewed(request_id="r", source="ab", target="abc", term_basis=BASIS).body
    assert body["status"] == "not_reviewed"
    assert body["checked"] == [] and body["issues"] == []
    assert body["truncated"] is False and body["truncated_reason"] is None
    SemanticReviewResponseBody.model_validate(body)
