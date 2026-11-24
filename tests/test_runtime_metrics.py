from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient
import httpx

import scripts.summarize_runtime_metrics as summarizer
from wuwaterm_api.app import create_app
from wuwaterm_api.auth import TOKEN_SCHEME, DeviceStore
from wuwaterm_api.settings import ApiSettings


TEST_SECRET = "metrics-test-secret-0123456789abcdef"
SMALL_PARAGRAPH_SOURCE = "The title line.\n\nFirst body line.\nSecond body line."
MERGED_PARAGRAPH_ANSWER = "标题行 第一行 合并了\n第二行"
REPAIRED_PARAGRAPH_ANSWER = "标题行\n\n第一行\n第二行"
REGROUP_ZH_SOURCE = "标题行\n\n第二行"
REGROUP_EN_UNGROUPED = "Title line\nSecond line"
REGROUP_EN_EXPECTED = "Title line\n\nSecond line"

EXPECTED_COMPLETION_BASE = {"request_id", "method", "route", "status", "duration_ms", "device"}
EXPECTED_COMPLETION_STAGE = EXPECTED_COMPLETION_BASE | {
    "slot_wait_ms",
    "llm_post_ms",
    "llm_calls",
    "direction",
}


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append((record.name, self.format(record)))


def _make_app(tmp_path: Path, sample_db: Path, *, request_timeout: float = 30.0):
    settings = ApiSettings(
        db_path=sample_db,
        device_db_path=tmp_path / "api-state" / "devices.db",
        rate_limit_per_minute=100,
        llm_calls_per_minute=100,
        llm_max_concurrency=2,
        request_timeout_seconds=request_timeout,
    )
    store = DeviceStore(settings.device_db_path)
    store.initialize()
    device = store.issue("metrics-desktop", secret=TEST_SECRET)
    token = f"{TOKEN_SCHEME}.{device.device_id}.{TEST_SECRET}"
    return create_app(settings, device_store=store), token


def _parse_kv(tail: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for token in tail.strip().split():
        key, sep, value = token.partition("=")
        assert sep == "=", token
        parsed[key] = value
    return parsed


def test_api_runtime_metrics_and_strategy_summary_end_to_end(
    monkeypatch, tmp_path: Path, sample_db: Path
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://metrics.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "sk-metrics-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "metrics-model")
    monkeypatch.setenv("WUWATERM_TRANSLATION_THINKING_EN_ZH", "auto")
    monkeypatch.delenv("WUWATERM_TRANSLATION_THINKING_ZH_EN", raising=False)

    app, token = _make_app(tmp_path, sample_db)
    headers = {"Authorization": f"Bearer {token}"}
    script: list[str | Exception] = [
        "你好世界。",
        httpx.ReadTimeout("slow"),
        "短句已回退。",
        MERGED_PARAGRAPH_ANSWER,
        REPAIRED_PARAGRAPH_ANSWER,
        REGROUP_EN_UNGROUPED,
    ]
    call_idx = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_idx
        item = script[call_idx]
        call_idx += 1
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json={"choices": [{"message": {"content": item}}]})

    app.state.translator._llm_transport = httpx.MockTransport(handler)

    recorder = _ListHandler()
    root = logging.getLogger()
    prev_level = root.level
    root.addHandler(recorder)
    root.setLevel(logging.INFO)
    try:
        with TestClient(app) as client:
            # 1. Exact dictionary hit (no LLM)
            r1 = client.post("/v1/translations", json={"text": "声骸"}, headers=headers)
            assert r1.status_code == 200
            assert r1.json()["kind"] == "exact"

            # 2. Ordinary en->zh auto (1 POST ok)
            r2 = client.post(
                "/v1/translations", json={"text": "Hello world."}, headers=headers
            )
            assert r2.status_code == 200
            assert r2.json()["text"] == "你好世界。"

            # 3. Timeout fallback rescued (ReadTimeout -> disabled succeeds)
            r3 = client.post(
                "/v1/translations", json={"text": "Short phrase."}, headers=headers
            )
            assert r3.status_code == 200
            assert r3.json()["text"] == "短句已回退。"

            # 4. Context paragraph repair rescued (merged -> repaired)
            r4 = client.post(
                "/v1/translations",
                json={"text": SMALL_PARAGRAPH_SOURCE, "to": "zh", "context": {}},
                headers=headers,
            )
            assert r4.status_code == 200
            assert r4.json()["text"] == REPAIRED_PARAGRAPH_ANSWER

            # 5. zh->en disabled context regroup applied (1 POST, 0 repair calls)
            r5 = client.post(
                "/v1/translations",
                json={"text": REGROUP_ZH_SOURCE, "to": "en", "context": {}},
                headers=headers,
            )
            assert r5.status_code == 200
            assert r5.json()["text"] == REGROUP_EN_EXPECTED

        # 6. Dedicated tight-timeout app to exercise 504 request timeout accounting.
        timeout_app, timeout_token = _make_app(
            tmp_path / "timeout", sample_db, request_timeout=0.1
        )
        timeout_headers = {"Authorization": f"Bearer {timeout_token}"}

        async def slow_handler(_request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.35)
            return httpx.Response(200, json={"choices": [{"message": {"content": "late"}}]})

        timeout_app.state.translator._llm_transport = httpx.MockTransport(slow_handler)
        with TestClient(timeout_app) as tclient:
            r6 = tclient.post(
                "/v1/translations",
                json={"text": "这句请求会在传输阶段超时。"},
                headers=timeout_headers,
            )
            assert r6.status_code == 504
    finally:
        root.removeHandler(recorder)
        root.setLevel(prev_level)

    assert call_idx == len(script)

    snap = app.state.metrics.snapshot()
    assert snap["requests"]["total"] == 5
    assert snap["requests"]["succeeded"] == 5
    assert snap["requests"]["timed_out"] == 0
    assert snap["requests"]["success_rate"] == 1.0

    # Dict hit does not count as LLM-backed request -> 4 LLM-backed requests, 6 POSTs.
    assert snap["llm"]["requests"] == 4
    assert snap["llm"]["succeeded"] == 4
    assert snap["llm"]["calls_total"] == 6
    assert snap["llm"]["avg_calls_per_request"] == 1.5
    assert snap["llm"]["outcomes"] == {"ok": 5, "timeout": 1}
    assert snap["llm"]["by_direction"]["en->zh"] == {
        "requests": 3,
        "succeeded": 3,
        "timed_out": 0,
        "calls": 5,
        "avg_calls_per_request": 1.6667,
    }
    assert snap["llm"]["by_direction"]["zh->en"] == {
        "requests": 1,
        "succeeded": 1,
        "timed_out": 0,
        "calls": 1,
        "avg_calls_per_request": 1.0,
    }
    assert snap["latency_ms"]["request_elapsed"]["count"] == 5
    assert snap["latency_ms"]["llm_post"]["count"] == 4
    assert snap["strategy"]["configured_thinking"] == {"auto": 3, "disabled": 1}
    assert snap["strategy"]["initial_thinking"] == {"disabled": 1, "low": 3}
    assert snap["strategy"]["timeout_fallback"] == {
        "attempts": 1,
        "succeeded": 1,
        "rescue_rate": 1.0,
    }
    assert snap["strategy"]["paragraph_regroup_applied"] == 1
    assert snap["strategy"]["paragraph_repair"] == {
        "attempts": 1,
        "succeeded": 1,
        "rescue_rate": 1.0,
    }
    assert snap["strategy"]["html_fallback_attempts"] == 0
    assert snap["strategy"]["budget_exhausted_count"] == 0

    tsnap = timeout_app.state.metrics.snapshot()
    assert tsnap["requests"]["total"] == 1
    assert tsnap["requests"]["timed_out"] == 1
    assert tsnap["requests"]["timeout_rate"] == 1.0
    assert tsnap["llm"]["timed_out"] == 1
    assert tsnap["llm"]["timeout_rate"] == 1.0

    # Completion log schemas stay strictly unchanged; strategy logs sit on wuwaterm.request_trace.
    completion_records = [
        msg for name, msg in recorder.messages if name == "wuwaterm_api" and msg.startswith("request complete ")
    ]
    strategy_records = [
        (name, msg) for name, msg in recorder.messages if msg.startswith("llm strategy ")
    ]
    assert len(completion_records) == 6
    for msg in completion_records:
        parsed = _parse_kv(msg[len("request complete ") :])
        assert set(parsed) in (EXPECTED_COMPLETION_BASE, EXPECTED_COMPLETION_STAGE)
    assert len(strategy_records) == 5
    for name, msg in strategy_records:
        assert name == "wuwaterm.request_trace"
        assert SMALL_PARAGRAPH_SOURCE not in msg
        assert REGROUP_ZH_SOURCE not in msg
        assert TEST_SECRET not in msg


def test_summarize_runtime_metrics_cli_json_and_markdown(tmp_path: Path):
    log_text = "\n".join(
        [
            "2026-11-24T12:00:00Z INFO wuwaterm_api request complete request_id=req1 method=POST route=/v1/translations status=200 duration_ms=120.0 device=dev1 slot_wait_ms=2.0 llm_post_ms=110.0 llm_calls=2 direction=en->zh",
            "2026-11-24T12:00:01Z INFO wuwaterm.request_trace llm strategy request_id=req1 direction=en->zh configured_thinking=auto initial_thinking=low timeout_fallback=1/1 paragraph_regroup=0 paragraph_repair=0/0 html_fallback=0 budget_exhausted=0",
            "2026-11-24T12:00:01Z INFO wuwaterm.llm_transport llm call purpose=translation outcome=timeout status=None elapsed_ms=45000",
            "2026-11-24T12:00:01Z INFO wuwaterm.llm_transport llm call purpose=translation outcome=ok status=200 elapsed_ms=3200",
            "2026-11-24T12:00:05Z WARNING wuwaterm_api request timed out route=/v1/translations request_id=req2 sent_status=- elapsed_ms=90005.0 stage=llm_post slot_wait_ms=0.0 llm_post_ms=90001.0 llm_calls=1 direction=zh->en",
            "2026-11-24T12:00:05Z INFO wuwaterm_api request complete request_id=req2 method=POST route=/v1/translations status=504 duration_ms=90005.0 device=dev1 slot_wait_ms=0.0 llm_post_ms=90001.0 llm_calls=1 direction=zh->en",
            "2026-11-24T12:00:05Z WARNING wuwaterm_api llm translation diagnostic request_id=req2 reason=timeout detail=unspecified expected_count=- actual_count=-",
            "2026-11-24T12:00:06Z WARNING wuwaterm.llm_transport llm translation failed reason=invalid_response detail=missing_placeholder expected=1 actual=0",
            "2026-11-24T12:00:07Z INFO wuwaterm_telegram.channel channel_translation stage=llm reason=success chat=c1 incoming_message=m1 edit=False direction=en->zh text_len=40 mode=HTML active=0 pending=0 high_water=1",
            "unrelated noise containing SUPER_PRIVATE_USER_TEXT that must never appear in output",
        ]
    )
    log_file = tmp_path / "runtime.log"
    log_file.write_text(log_text, encoding="utf-8")

    summary = summarizer.summarize_lines(log_text.splitlines())
    assert summary["requests"]["total"] == 2
    assert summary["requests"]["succeeded"] == 1
    assert summary["requests"]["timed_out"] == 1
    assert summary["requests"]["success_rate"] == 0.5
    assert summary["requests"]["timeout_rate"] == 0.5
    assert summary["requests"]["timed_out_by_stage"] == {"llm_post": 1}
    assert summary["llm"]["avg_calls_per_request"] == 1.5
    assert summary["llm"]["outcomes"] == {"ok": 1, "timeout": 1}
    assert summary["strategy"]["timeout_fallback"] == {
        "attempts": 1,
        "succeeded": 1,
        "rescue_rate": 1.0,
    }
    assert summary["diagnostics"]["reasons"] == {"invalid_response": 1, "timeout": 1}
    assert summary["channel_events"] == {"llm:success": 1}

    cli_json = subprocess.run(
        [sys.executable, "scripts/summarize_runtime_metrics.py", "--format", "json", str(log_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    parsed = json.loads(cli_json.stdout)
    assert parsed == summary
    assert "SUPER_PRIVATE_USER_TEXT" not in cli_json.stdout

    cli_md = subprocess.run(
        [sys.executable, "scripts/summarize_runtime_metrics.py", "--format", "markdown"],
        input=log_text,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "# WuwaTerm Runtime Metrics Summary" in cli_md.stdout
    assert "SUPER_PRIVATE_USER_TEXT" not in cli_md.stdout
