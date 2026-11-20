"""API protocol instructions preserve default and HTML translation behavior."""
import asyncio
import json

import httpx
import pytest

from wuwaterm.application import build_translator
from wuwaterm.sentence import SentenceTranslator, LLMTranslationError
from wuwaterm_api.app import create_app
from wuwaterm_api.settings import ApiSettings


@pytest.mark.parametrize("to_chinese,html_mode", [(True, False), (False, False), (True, True), (False, True)])
def test_api_protocol_opt_in_preserves_other_payloads(monkeypatch, sample_db, tmp_path, to_chinese, html_mode):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-only")
    monkeypatch.setattr("secrets.token_hex", lambda _: "fixedtest")
    payloads = []

    def response(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": payload["messages"][1]["content"]}}]})

    client_type = httpx.AsyncClient

    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(response)
        return client_type(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    app = create_app(ApiSettings(db_path=sample_db, device_db_path=tmp_path / "devices.db"))
    default = build_translator(sample_db)
    source = "Use Echo now." if to_chinese else "现在使用声骸。"
    if html_mode:
        source = f"<b>{source}</b>"

    async def run():
        for translator in (default, app.state.translator):
            try:
                method = translator.translate_html_async if html_mode else translator.translate_async
                await method(source, to_chinese=to_chinese)
            finally:
                await translator._close_llm_client()

    asyncio.run(run())
    assert len(payloads) == 2
    before, after = payloads
    if to_chinese and not html_mode:
        assert "Placeholder tokens are mandatory protocol syntax" in after["messages"][0]["content"]
        assert "Placeholder tokens are mandatory protocol syntax" not in before["messages"][0]["content"]
        assert after["messages"][1:] == before["messages"][1:]
    else:
        assert after == before


@pytest.mark.parametrize("opt_in,to_chinese,html_mode", [
    (False, True, False), (True, True, False),
    (True, False, False), (True, True, True),
])
def test_protocol_candidate_is_explicit_and_plain_en_zh_only(
    monkeypatch, sample_db, opt_in, to_chinese, html_mode,
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-only")
    payloads = []
    marker = "__WUWA_TERM_test_0000__"

    def response(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": marker}}]})

    async def run(enabled):
        kwargs = {"en_zh_protocol": True} if enabled else {}
        translator = SentenceTranslator(sample_db, llm_transport=httpx.MockTransport(response), **kwargs)
        try:
            return await translator._call_llm_async_limited(
                marker, ((marker, "测试", "Fixture"),),
                to_chinese=to_chinese, html_mode=html_mode,
            )
        finally:
            await translator._close_llm_client()

    assert asyncio.run(run(False)) == marker
    assert asyncio.run(run(opt_in)) == marker
    before, after = payloads
    if opt_in and to_chinese and not html_mode:
        expected = before["messages"][0]["content"].replace(
            "Keep all placeholders exactly unchanged. ",
            "Placeholder tokens are mandatory protocol syntax, not natural-language output. "
            "Copy every placeholder byte-for-byte exactly once even though all other output "
            "must be Simplified Chinese. Never replace a placeholder with the official term "
            "shown under Locked terms; the server validates and restores official terms after "
            "your response.\n",
        )
        assert after["messages"][0]["content"] == expected
        assert after["messages"][1:] == before["messages"][1:]
        assert after.keys() == before.keys()
        assert after["model"] == before["model"]
        assert after["temperature"] == before["temperature"] == 0
    else:
        assert after == before
    assert len(payloads) == 2


@pytest.mark.parametrize("duplicate", [False, True])
def test_protocol_candidate_still_fails_closed_without_retry(monkeypatch, sample_db, duplicate):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-only")
    calls = []

    def response(request):
        import re
        payload = json.loads(request.content)
        calls.append(1)
        marker = re.search(r"__WUWA_TERM_[A-Za-z0-9_]+__", payload["messages"][1]["content"]).group()
        content = marker * 2 if duplicate else "测试"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async def run():
        translator = SentenceTranslator(sample_db, en_zh_protocol=True, llm_transport=httpx.MockTransport(response))
        try:
            await translator.translate_async("Use Echo now.", to_chinese=True, propagate_errors=True)
        finally:
            await translator._close_llm_client()

    with pytest.raises(LLMTranslationError) as failure:
        asyncio.run(run())
    assert failure.value.diagnostic.detail == ("duplicate_placeholder" if duplicate else "missing_placeholder")
    assert len(calls) == 1


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("to_chinese,html_mode", [(True, False), (False, False), (True, True), (False, True)])
def test_public_translation_entries_preserve_default_payloads(
    monkeypatch, sample_db, sync, to_chinese, html_mode,
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-only")
    monkeypatch.setattr("secrets.token_hex", lambda _: "fixedtest")
    payloads = []

    def response(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": payload["messages"][1]["content"]}}]})

    # Only the external HTTP boundary is replaced, including the sync-created client.
    client_type = httpx.AsyncClient

    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(response)
        return client_type(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    source = "Use Echo now." if to_chinese else "现在使用声骸。"
    if html_mode:
        source = f"<b>{source}</b>"
    for enabled in (False, True):
        translator = SentenceTranslator(sample_db, en_zh_protocol=enabled)
        if sync:
            method = translator.translate_html if html_mode else translator.translate
            method(source, to_chinese=to_chinese)
        else:
            async def run():
                try:
                    method = translator.translate_html_async if html_mode else translator.translate_async
                    await method(source, to_chinese=to_chinese)
                finally:
                    await translator._close_llm_client()
            asyncio.run(run())
    assert len(payloads) == 2
    before, after = payloads
    if to_chinese and not html_mode:
        assert "Placeholder tokens are mandatory protocol syntax" in after["messages"][0]["content"]
        assert "Placeholder tokens are mandatory protocol syntax" not in before["messages"][0]["content"]
        assert after["messages"][1:] == before["messages"][1:]
    else:
        assert after == before
