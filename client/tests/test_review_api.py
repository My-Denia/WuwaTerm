"""Review requests use the real client and its protected HTTP transport."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import httpx
import pytest

from wuwaterm_client.api import ApiClient
from wuwaterm_client.errors import ERROR_CANCELLED, ERROR_NOT_CONFIGURED, ERROR_UNKNOWN, ClientError


SOURCE = "😀今汐 and 今汐"
TARGET = "Jinhsi and Jinhsi"


def report() -> dict:
    start = SOURCE.rindex("今汐")
    target_start = TARGET.rindex("Jinhsi")
    return {
        "request_id": "example",
        "source_revision": hashlib.sha256(SOURCE.encode()).hexdigest(),
        "target_revision": hashlib.sha256(TARGET.encode()).hexdigest(),
        "rule_version": "review-v2",
        "matcher_revision": "a" * 64,
        "dictionary": {
            "schema_version": "test", "source_commit": "test", "term_count": 1,
            "revision": "b" * 64,
        },
        "coverage": {"evaluated": 1, "not_evaluated": 0, "rules": ["dictionary"]},
        "findings": [{
            "id": f"{start}:{start + 2}:今汐", "rule_id": "dictionary",
            "verdict": "verified_constraint",
            "source_span": {"start": start, "end": start + 2, "text": "今汐"},
            "target_span": {"start": target_start, "end": target_start + 6, "text": "Jinhsi"},
            "candidates": [{"candidate_id": "c" * 64, "zh": "今汐", "en": "Jinhsi",
                            "category": "character", "sources": []}],
            "candidates_truncated": False,
        }],
        "truncated": False,
    }


def make_client(handler, base_url: str | None = "https://test") -> ApiClient:
    return ApiClient(base_url, token_provider=lambda: "example-token",
                     _test_transport=httpx.MockTransport(handler))


def test_review_sends_v2_basis_and_accepts_current_spans() -> None:
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=report())

    async def scenario() -> dict:
        client = make_client(handler)
        try:
            return await client.review_pair(SOURCE, TARGET, "en", alignments=[])
        finally:
            await client.aclose()

    actual = asyncio.run(scenario())
    assert actual == report()
    assert str(seen[0].url) == "https://test/v1/reviews"
    assert seen[0].headers["authorization"] == "Bearer example-token"
    assert seen[0].headers["x-wuwaterm-matcher-basis"] == "1"
    assert json.loads(seen[0].content) == {
        "source": SOURCE, "target": TARGET, "direction": "en",
        "review_version": "review-v2", "alignments": [],
    }


@pytest.mark.parametrize("change", ["source_revision", "target_revision", "span", "matcher", "schema"])
def test_review_rejects_unusable_or_wrong_draft_report(change: str) -> None:
    payload = copy.deepcopy(report())
    if change in {"source_revision", "target_revision"}:
        payload[change] = "0" * 64
    elif change == "span":
        payload["findings"][0]["target_span"]["start"] = 0
    elif change == "matcher":
        del payload["matcher_revision"]
    else:
        payload["findings"][0]["candidates"][0]["candidate_id"] = None

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def scenario() -> None:
        client = make_client(handler)
        try:
            with pytest.raises(ClientError) as exc:
                await client.review_pair(SOURCE, TARGET, "en")
            assert exc.value.code == ERROR_UNKNOWN
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_unconfigured_review_never_calls_transport() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected network request")

    async def scenario() -> None:
        client = make_client(handler, base_url=None)
        try:
            with pytest.raises(ClientError) as exc:
                await client.review_pair(SOURCE, TARGET, "en")
            assert exc.value.code == ERROR_NOT_CONFIGURED
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_cancelling_review_reports_cancelled() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        client = make_client(handler)
        try:
            task = asyncio.create_task(client.review_pair(SOURCE, TARGET, "en"))
            await entered.wait()
            task.cancel()
            with pytest.raises(ClientError) as exc:
                await task
            assert exc.value.code == ERROR_CANCELLED
        finally:
            await client.aclose()

    asyncio.run(scenario())
