"""Tests for scripts/validate_state_file.py (stdlib-only, zero wuwaterm imports)."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import validate_state_file

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_state_file.py"


def test_validate_state_file_has_zero_wuwaterm_imports() -> None:
    """scripts/validate_state_file.py must not import legacy wuwaterm bot modules."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "wuwaterm" not in imported
    assert "wuwaterm_api" not in imported
    assert "channel_reply_schema" not in SCRIPT.read_text(encoding="utf-8")


def test_validate_state_file_runs_without_src_on_sys_path(tmp_path: Path) -> None:
    """Running the script in isolation without src/ still validates both schemas."""
    reply_path = tmp_path / "channel_replies.json"
    reply_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "chat_id": -100123456,
                        "message_id": 42,
                        "expires_at": 1893456000.5,
                        "reply_message_ids": [101, 102],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    settings_path = tmp_path / "chat_settings.json"
    settings_path.write_text(
        json.dumps({"public": {"-100123456": True}, "allowed": [-100123456, "99"]}),
        encoding="utf-8",
    )

    for path, name in (
        (reply_path, "channel_replies.json"),
        (settings_path, "chat_settings.json"),
    ):
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), str(path), name],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"version": 2, "entries": []},
        {"version": True, "entries": []},
        {"version": 1, "entries": {}},
        {"version": 1, "entries": ["not-an-object"]},
        {
            "version": 1,
            "entries": [
                {
                    "chat_id": True,
                    "message_id": 1,
                    "expires_at": 100,
                    "reply_message_ids": [1],
                }
            ],
        },
        {
            "version": 1,
            "entries": [
                {
                    "chat_id": 1,
                    "message_id": "1",
                    "expires_at": 100,
                    "reply_message_ids": [1],
                }
            ],
        },
        {
            "version": 1,
            "entries": [
                {
                    "chat_id": 1,
                    "message_id": 2,
                    "expires_at": True,
                    "reply_message_ids": [1],
                }
            ],
        },
        {
            "version": 1,
            "entries": [
                {
                    "chat_id": 1,
                    "message_id": 2,
                    "expires_at": float("inf"),
                    "reply_message_ids": [1],
                }
            ],
        },
        {
            "version": 1,
            "entries": [
                {
                    "chat_id": 1,
                    "message_id": 2,
                    "expires_at": 100,
                    "reply_message_ids": [],
                }
            ],
        },
        {
            "version": 1,
            "entries": [
                {
                    "chat_id": 1,
                    "message_id": 2,
                    "expires_at": 100,
                    "reply_message_ids": [False],
                }
            ],
        },
    ],
)
def test_validate_channel_replies_rejects_malformed_payloads(payload: object) -> None:
    with pytest.raises(validate_state_file.ChannelReplyPayloadError):
        validate_state_file.validate_channel_replies(payload)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"public": [], "allowed": []},
        {"public": {}, "allowed": {}},
        {"public": {"01": True}, "allowed": []},
        {"public": {"1": "yes"}, "allowed": []},
        {"public": {}, "allowed": [True]},
        {"public": {}, "allowed": ["007"]},
    ],
)
def test_validate_chat_settings_rejects_malformed_payloads(payload: object) -> None:
    with pytest.raises(ValueError):
        validate_state_file.validate_chat_settings(payload)


def test_cli_usage_and_unknown_type(tmp_path: Path) -> None:
    assert validate_state_file.main([]) == 2
    sample = tmp_path / "other.json"
    sample.write_text("{}", encoding="utf-8")
    assert validate_state_file.main([str(sample), "other.json"]) == 1
