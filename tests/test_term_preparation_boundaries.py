"""Term preparation keeps matching, model guidance and structure in step."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

import httpx

from wuwaterm.sentence import SentenceTranslator


def test_protected_translation_numbers_locks_and_deduplicates_references(
    monkeypatch, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "fixture")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "fixture")
    monkeypatch.setattr(secrets, "token_hex", lambda _size: "0123456789abcdef")
    calls = []

    def echo(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": body["messages"][-1]["content"]}}]}
        )

    translator = SentenceTranslator(sample_db, llm_transport=httpx.MockTransport(echo))

    async def run():
        try:
            return await translator.translate_html_async(
                "Echo Jinhsi <b>Echo Camellya Jinhsi</b> Resonators &amp; Echo",
                to_chinese=True,
            )
        finally:
            await translator.aclose()

    assert asyncio.run(run()) == "声骸 Jinhsi <b>声骸 Camellya Jinhsi</b> 共鸣者 &amp; 声骸"
    assert len(calls) == 1
    system, user = calls[0]["messages"]
    assert system["content"].split("Locked terms:\n", 1)[1].split(
        "\nDictionary term references below", 1
    )[0] == (
        "__WUWA_TERM_0123456789abcdef_0000__ = 声骸\n"
        "__WUWA_TERM_0123456789abcdef_0001__ = 声骸\n"
        "__WUWA_TERM_0123456789abcdef_0002__ = 共鸣者\n"
        "__WUWA_TERM_0123456789abcdef_0003__ = 声骸"
    )
    assert system["content"].split("Dictionary term references:\n", 1)[1] == (
        "Jinhsi = 今汐 (resonator)\nCamellya = 椿 (resonator)"
    )
    assert user["content"] == (
        "__WUWA_TERM_0123456789abcdef_0000__ Jinhsi "
        "__WUWA_HTML_0123456789abcdef_0000__"
        "__WUWA_TERM_0123456789abcdef_0001__ Camellya Jinhsi"
        "__WUWA_HTML_0123456789abcdef_0001__ "
        "__WUWA_TERM_0123456789abcdef_0002__ "
        "__WUWA_HTML_0123456789abcdef_0002__ "
        "__WUWA_TERM_0123456789abcdef_0003__"
    )


def test_builder_measures_real_term_spans_without_importing_translator(sample_db):
    # Fresh interpreter: an import already made by another test cannot mask a
    # builder -> sentence dependency. Exercise both corpus-measurement paths.
    source = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ, PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1")
    code = '''
import json
import sys
from wuwaterm.builder import measure_free_text_exclusions, measure_free_text_zh_exclusions
assert "wuwaterm.sentence" not in sys.modules
english = {"example": "Jinhsi found Echo."}
chinese = {"example": "今汐找到了声骸。"}
for measure in (measure_free_text_exclusions, measure_free_text_zh_exclusions):
    assert json.loads(measure(sys.argv[1], english, chinese))["surfaces"] == []
assert "wuwaterm.sentence" not in sys.modules
'''
    result = subprocess.run(
        [sys.executable, "-c", code, str(sample_db)],
        cwd=source.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
