"""Unit tests for legacy container handling during component adoption."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import component_adoption as adoption  # noqa: E402
import component_observe as observe  # noqa: E402

API_ID = "a" * 64
LEGACY_BOT_ID = "b" * 64
TG_ID = "c" * 64


class _FakeDockerResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.status = status
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self, _limit: int = -1) -> bytes:
        return self._raw


class _FakeUnixConnection:
    entries: list[dict[str, Any]] = []

    def __init__(self, _host: str, timeout: int = 15) -> None:
        self.timeout = timeout

    def request(self, method: str, path: str) -> None:
        assert method == "GET"
        assert path == "/containers/json?all=1"

    def getresponse(self) -> _FakeDockerResponse:
        return _FakeDockerResponse(self.entries)

    def close(self) -> None:
        pass


def _entry(container_id: str, *, project: str, service: str, state: str) -> dict[str, Any]:
    return {
        "Id": container_id,
        "State": state,
        "Labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.service": service,
        },
    }


def _patch_docker(monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, Any]]) -> list[str]:
    inspected: list[str] = []
    _FakeUnixConnection.entries = entries
    monkeypatch.setattr(observe, "_UnixHTTPConnection", _FakeUnixConnection)

    def fake_inspect(identifier: str) -> dict[str, Any]:
        inspected.append(identifier)
        return {"Id": identifier}

    monkeypatch.setattr(observe, "_inspect", fake_inspect)
    return inspected


@pytest.mark.parametrize("legacy_state", ["exited", "created", "dead"])
def test_project_containers_skips_stopped_legacy_bot_and_adopts_api(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    legacy_state: str,
) -> None:
    inspected = _patch_docker(
        monkeypatch,
        [
            _entry(LEGACY_BOT_ID, project="deploy", service="wuwaterm", state=legacy_state),
            _entry(API_ID, project="deploy", service="wuwaterm-api", state="running"),
        ],
    )

    installed = adoption._project_containers(["deploy"])

    assert set(installed) == {"api"}
    assert installed["api"] == ({"Id": API_ID}, "deploy", "wuwaterm-api")
    assert inspected == [API_ID]
    err = capsys.readouterr().err
    assert "docker rm wuwaterm-bot" in err


def test_project_containers_skips_stopped_legacy_bot_alongside_standalone_telegram(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    inspected = _patch_docker(
        monkeypatch,
        [
            _entry(LEGACY_BOT_ID, project="deploy", service="wuwaterm", state="exited"),
            _entry(API_ID, project="deploy", service="wuwaterm-api", state="running"),
            _entry(TG_ID, project="deploy", service="wuwaterm-telegram", state="running"),
        ],
    )

    installed = adoption._project_containers(["deploy"])

    assert set(installed) == {"api", "telegram"}
    assert installed["api"] == ({"Id": API_ID}, "deploy", "wuwaterm-api")
    assert installed["telegram"] == ({"Id": TG_ID}, "deploy", "wuwaterm-telegram")
    assert inspected == [API_ID, TG_ID]
    assert "docker rm wuwaterm-bot" in capsys.readouterr().err


@pytest.mark.parametrize("active_state", ["running", "restarting", "paused"])
def test_project_containers_fails_closed_when_legacy_bot_is_active(
    monkeypatch: pytest.MonkeyPatch, active_state: str
) -> None:
    inspected = _patch_docker(
        monkeypatch,
        [
            _entry(LEGACY_BOT_ID, project="deploy", service="wuwaterm", state=active_state),
            _entry(API_ID, project="deploy", service="wuwaterm-api", state="running"),
        ],
    )

    with pytest.raises(adoption.JournalError, match="WuwaTerm-Telegram"):
        adoption._project_containers(["deploy"])
    assert inspected == []


def test_project_containers_fails_when_only_stopped_legacy_bot_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_docker(
        monkeypatch,
        [
            _entry(LEGACY_BOT_ID, project="deploy", service="wuwaterm", state="exited"),
        ],
    )

    with pytest.raises(adoption.JournalError, match="no installed product containers were discovered"):
        adoption._project_containers(["deploy"])
    assert "docker rm wuwaterm-bot" in capsys.readouterr().err
