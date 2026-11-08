from __future__ import annotations

import asyncio
import dataclasses
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading

import pytest

from wuwaterm.application import (
    LLMTranslationError,
    MarkupTranslation,
    ProtectedStructure,
    ProtectedText,
    TranslationJob,
    build_term_service,
    build_translator,
    translate_request_async,
)


@pytest.fixture
def model_server(monkeypatch):
    """A real loopback HTTP endpoint; only the upstream model is substituted."""
    class State:
        requests = []
        mode = "identity"
        hold_first = False
        first_started = threading.Event()
        release_first = threading.Event()

    state = State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(payload)
            if state.hold_first and len(state.requests) == 1:
                state.first_started.set()
                state.release_first.wait(10)
            content = payload["messages"][1]["content"]
            # This fixture stands in for the model: apply the dictionary
            # references the request supplied, then corrupt the structure or
            # the placeholders as a real model may.
            system = payload["messages"][0]["content"]
            if "Dictionary term references:\n" in system:
                block = system.split("Dictionary term references:\n", 1)[1]
                for line in block.splitlines():
                    source, sep, rest = line.partition(" = ")
                    if not sep or not rest.endswith(")"):
                        continue
                    official, _, category = rest.rpartition(" (")
                    if not official or not category[:-1].replace("_", "").islower():
                        continue
                    content = content.replace(source, official, 1)
            tokens = re.findall(r"__WUWA_HTML_[0-9a-f]{16}_[0-9]{4,}__", content)
            terms = re.findall(r"__WUWA_TERM_[0-9a-f]{16}_[0-9]{4,}__", content)
            if state.mode == "missing":
                content = content.replace(tokens[0], "", 1)
            elif state.mode == "duplicate":
                content += tokens[0]
            elif state.mode == "reordered":
                content = content.replace(tokens[0], "_SWAP_", 1)
                content = content.replace(tokens[1], tokens[0], 1).replace("_SWAP_", tokens[1], 1)
            elif state.mode == "unknown":
                content += "__WUWA_HTML_deadbeefdeadbeef_9999__"
            elif state.mode == "tag":
                content += "<b>new</b>"
            elif state.mode == "entity":
                content += "&amp;"
            elif state.mode == "empty":
                content = ""
            elif state.mode == "term":
                content = content.replace(terms[0], "", 1)
            response = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            try:
                self.wfile.write(response)
            except BrokenPipeError:
                pass  # A cancelled in-flight request closed the connection.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "fixture")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "fixture")
    try:
        yield state
    finally:
        state.release_first.set()
        server.shutdown()
        server.server_close()
        thread.join()


def test_neutral_public_path_uses_visible_context_and_preserves_structure(sample_db, model_server):
    translator = build_translator(sample_db)
    source = ProtectedText(
        ("", "椿", " joins ", "声", "骸"),
        (
            ProtectedStructure("[ENTITY]", "香"),
            ProtectedStructure("[/ENTITY]"),
            ProtectedStructure("[OPEN]"),
            ProtectedStructure("[CLOSE]"),
        ),
    )

    async def run():
        try:
            return await translator.translate_protected_async(source)
        finally:
            await translator.aclose()

    result = asyncio.run(run())
    # Context prevents 椿 from being treated as a standalone name. A term
    # split across structures is never locked as a multi-character match.
    assert result.render() == "[ENTITY]椿[/ENTITY] joins [OPEN]声[CLOSE]骸"
    assert result.structures == source.structures
    payload = model_server.requests[0]
    assert "[ENTITY]" not in payload["messages"][1]["content"]
    assert "[OPEN]" not in payload["messages"][1]["content"]
    assert "Opaque placeholders represent source structure" in payload["messages"][0]["content"]


def test_synchronous_public_and_legacy_paths_use_the_real_model_request(sample_db, model_server):
    translator = build_translator(sample_db)
    source = ProtectedText(("Use ", "Echo", " now."), (ProtectedStructure("["), ProtectedStructure("]")))
    assert translator.translate_protected(source, to_chinese=True).render() == "Use [声骸] now."
    assert translator.translate_html("<b>椿</b> joins.") == "<b>Camellya</b> joins."
    assert len(model_server.requests) == 2


@pytest.mark.parametrize(
    ("source", "to_chinese", "expected"),
    [
        ('<a href="https://example.invalid/camellya">camellya</a> &amp; <b>Resonators</b>', True,
         '<a href="https://example.invalid/camellya">椿</a> &amp; <b>共鸣者</b>'),
        ('&#39321;<b>椿</b> 和 <i>声</i>骸。', False, '&#39321;<b>椿</b> 和 <i>声</i>骸。'),
        ('<b>椿</b>加入队伍。 <a href="https://example.invalid/椿">声骸</a>', False,
         '<b>Camellya</b>加入队伍。 <a href="https://example.invalid/椿">Echo</a>'),
        ('<b>Echo</b>s and <i>Resonators</i> arrive.', True,
         '<b>Echo</b>s and <i>共鸣者</i> arrive.'),
        ('>  <b>Echo</b>\n', True, '>  <b>声骸</b>'),
    ],
)
def test_real_http_html_compatibility(sample_db, model_server, source, to_chinese, expected):
    translator = build_translator(sample_db)

    async def run():
        try:
            return await translator.translate_html_async(source, to_chinese=to_chinese)
        finally:
            await translator.aclose()

    assert asyncio.run(run()) == expected
    assert len(model_server.requests) == 1
    assert "href=" not in model_server.requests[0]["messages"][1]["content"]
    if source.startswith(">"):
        # Model input bypasses preparation; the existing response reader
        # intentionally strips leading/trailing whitespace from model output.
        assert model_server.requests[0]["messages"][1]["content"].endswith("\n")


@pytest.mark.parametrize("mode", ["missing", "duplicate", "reordered", "unknown", "tag", "entity"])
def test_legacy_html_rejects_corruption_and_added_valid_markup(sample_db, model_server, mode):
    model_server.mode = mode
    translator = build_translator(sample_db)

    async def run():
        try:
            return await translator.translate_html_async("<b>椿</b> joins.")
        finally:
            await translator.aclose()

    with pytest.raises(LLMTranslationError) as failure:
        asyncio.run(run())
    assert failure.value.reason == "html_integrity"
    assert failure.value.diagnostic.reason == "html_integrity"
    assert len(model_server.requests) == 1


@pytest.mark.parametrize("mode", ["missing", "duplicate", "reordered", "unknown"])
def test_neutral_integrity_has_neutral_reason(sample_db, model_server, mode):
    model_server.mode = mode
    translator = build_translator(sample_db)
    source = ProtectedText(("", "hello", ""), (ProtectedStructure("["), ProtectedStructure("]")))

    async def run():
        try:
            await translator.translate_protected_async(source)
        finally:
            await translator.aclose()

    with pytest.raises(LLMTranslationError) as failure:
        asyncio.run(run())
    assert failure.value.reason == "protected_text_integrity"
    assert failure.value.diagnostic.reason == "protected_text_integrity"
    assert len(model_server.requests) == 1


@pytest.mark.parametrize("mode", ["empty", "term"])
def test_empty_and_corrupt_term_responses_remain_content_failures(sample_db, model_server, mode):
    model_server.mode = mode
    translator = build_translator(sample_db)

    async def run():
        try:
            await translator.translate_html_async("<b>Echo</b> joins.", to_chinese=True)
        finally:
            await translator.aclose()

    with pytest.raises(LLMTranslationError) as failure:
        asyncio.run(run())
    assert failure.value.reason == "invalid_response"
    assert failure.value.diagnostic.reason == "invalid_response"
    assert len(model_server.requests) == 1


def test_protected_input_is_immutable_and_rejects_invalid_shapes():
    value = ProtectedText(("left", "right"), (ProtectedStructure("|", " "),))
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.visible_segments = ("changed",)
    with pytest.raises((ValueError, TypeError)):
        ProtectedText(("only",), (ProtectedStructure("|"),))
    with pytest.raises(TypeError):
        ProtectedText(["mutable"])  # type: ignore[arg-type]


@pytest.mark.parametrize("reason", ["stale_before_llm", "authorization_changed_before_llm"])
def test_guarded_markup_rechecks_after_shared_slot_before_http(sample_db, model_server, reason):
    model_server.hold_first = True
    translator = build_translator(sample_db, max_concurrency=1)
    service = build_term_service(sample_db)

    async def run():
        hook_entered = asyncio.Event()
        revoked = False
        checks = []

        def guard():
            checks.append(revoked)
            if revoked:
                raise LLMTranslationError("blocked", reason=reason)

        async def markup_hook(markup, *, to_chinese, before_llm_call):
            hook_entered.set()
            text = await translator.translate_html_async(
                markup, to_chinese=to_chinese, before_llm_call=before_llm_call
            )
            return MarkupTranslation(text=text)

        first = asyncio.create_task(translator.translate_protected_async(ProtectedText(("first request",))))
        assert await asyncio.to_thread(model_server.first_started.wait, 5)
        second = asyncio.create_task(translate_request_async(
            service, translator, TranslationJob("Use Echo now in this field.", markup="<b>Echo</b> in this field."),
            markup_translator=markup_hook, before_llm_call=guard,
        ))
        await asyncio.wait_for(hook_entered.wait(), 5)
        await asyncio.sleep(0)
        assert not checks
        revoked = True
        model_server.release_first.set()
        await first
        with pytest.raises(LLMTranslationError) as failure:
            await second
        assert failure.value.reason == reason
        assert checks == [True]
        await translator.aclose()

    asyncio.run(run())
    assert len(model_server.requests) == 1


def test_cancelling_waiter_restores_shared_slot_capacity(sample_db, model_server):
    model_server.hold_first = True
    translator = build_translator(sample_db, max_concurrency=1)

    async def run():
        first = asyncio.create_task(translator.translate_protected_async(ProtectedText(("first",))))
        assert await asyncio.to_thread(model_server.first_started.wait, 5)
        second = asyncio.create_task(translator.translate_protected_async(ProtectedText(("cancelled",))))
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        model_server.release_first.set()
        await first
        third = await asyncio.wait_for(translator.translate_protected_async(ProtectedText(("third",))), 5)
        assert third.render() == "third"
        await translator.aclose()

    asyncio.run(run())
    assert len(model_server.requests) == 2
    assert [request["messages"][1]["content"] for request in model_server.requests] == ["first", "third"]


def test_cancelling_inflight_request_releases_shared_slot(sample_db, model_server):
    model_server.hold_first = True
    translator = build_translator(sample_db, max_concurrency=1)

    async def run():
        first = asyncio.create_task(translator.translate_protected_async(ProtectedText(("first",))))
        assert await asyncio.to_thread(model_server.first_started.wait, 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        result = await asyncio.wait_for(
            translator.translate_protected_async(ProtectedText(("next",))), 5
        )
        assert result.render() == "next"
        model_server.release_first.set()
        await translator.aclose()

    asyncio.run(run())
    assert [request["messages"][1]["content"] for request in model_server.requests] == ["first", "next"]


def test_guarded_legacy_hook_fails_closed_and_internal_typeerror_is_not_retried(sample_db):
    service = build_term_service(sample_db)
    translator = build_translator(sample_db)
    calls = []

    async def legacy(markup, *, to_chinese):
        calls.append(markup)
        return MarkupTranslation(text="legacy")

    async def broken(markup, *, to_chinese, before_llm_call):
        calls.append(markup)
        raise TypeError("inside hook")

    async def run():
        job = TranslationJob("Use Echo now in this field.", markup="<b>Echo</b>")
        assert (await translate_request_async(service, translator, job, markup_translator=legacy)).text == "legacy"
        with pytest.raises(TypeError):
            await translate_request_async(service, translator, job, markup_translator=legacy, before_llm_call=lambda: None)
        with pytest.raises(TypeError, match="inside hook"):
            await translate_request_async(service, translator, job, markup_translator=broken, before_llm_call=lambda: None)
        await translator.aclose()

    asyncio.run(run())
    assert calls == ["<b>Echo</b>", "<b>Echo</b>"]


@pytest.mark.parametrize("revoke_after_markup", [False, True])
def test_html_content_fallback_rechecks_guard_before_plain_request(
    sample_db, model_server, revoke_after_markup
):
    model_server.mode = "tag"
    service = build_term_service(sample_db)
    translator = build_translator(sample_db)
    checks = []

    def guard():
        checks.append(len(model_server.requests))
        if revoke_after_markup and model_server.requests:
            raise LLMTranslationError("blocked", reason="authorization_changed_before_llm")

    async def markup(markup, *, to_chinese, before_llm_call):
        try:
            text = await translator.translate_html_async(
                markup, to_chinese=to_chinese, before_llm_call=before_llm_call
            )
        except LLMTranslationError as exc:
            assert exc.reason == "html_integrity"
            model_server.mode = "identity"
            return MarkupTranslation(fallback_to_plain=True)
        return MarkupTranslation(text=text)

    async def run():
        try:
            return await translate_request_async(
                service, translator,
                TranslationJob("Use Echo now in this field.", markup="<b>Echo</b> in this field."),
                markup_translator=markup, before_llm_call=guard,
            )
        finally:
            await translator.aclose()

    outcome = asyncio.run(run())
    assert checks == [0, 1]
    if revoke_after_markup:
        assert outcome.failed
        assert outcome.llm_failure.reason == "authorization_changed_before_llm"
        assert len(model_server.requests) == 1
    else:
        assert not outcome.failed and not outcome.markup_used
        assert len(model_server.requests) == 2
