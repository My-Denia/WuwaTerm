"""The open database's version strings survive the API and the site proxy.

The site helper does not rebuild the validator. It hands the real ``/v1/meta``
bytes to ``proxyMetaRequest``.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from wuwaterm.application import service_metadata
from wuwaterm.db import connect
from wuwaterm.lookup import TermService
from tests.test_api import (
    bearer,
    build_client_app,
    call,
    disable_llm,
    issue_device,
    run,
)

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "site" / "tests" / "helpers" / "project-live-meta.mjs"
VERSION_KEYS = (
    "source_game_version",
    "source_resource_version",
    "source_changelist",
)
RECORDED = {
    "source_game_version": "9.9-test",
    "source_resource_version": "9.9-resource-test",
    "source_changelist": "123456-test",
}
PIN_TEXT = ("3.7.0", "3.7.8", "8975829")
BFF_SENTINELS = (
    "SYNTHETIC_SITE_PROVENANCE_TOKEN_74A1C9",
    "provenance-bff.invalid",
)


def _copy_db(tmp_path: Path, sample_db: Path) -> Path:
    copied = tmp_path / "provenance.db"
    copied.write_bytes(sample_db.read_bytes())
    return copied


def _store(db: Path, values: dict[str, str | None]) -> None:
    with connect(db) as conn:
        for key, value in values.items():
            if value is None:
                conn.execute("DELETE FROM metadata WHERE key = ?", (key,))
                continue
            updated = conn.execute(
                "UPDATE metadata SET value = ? WHERE key = ?",
                (value, key),
            ).rowcount
            assert updated == 1, key


def _read_meta(tmp_path: Path, db: Path, monkeypatch):
    recorded = service_metadata(TermService(db))
    app, store = build_client_app(tmp_path, db)
    _device, token = issue_device(store, "provenance reader")
    disable_llm(monkeypatch)
    response = run(call(app, "GET", "/v1/meta", headers=bearer(token)))
    assert response.status_code == 200, response.text
    return recorded, response


def _project(body: dict) -> dict:
    encoded = json.dumps(body)
    for sentinel in BFF_SENTINELS:
        assert sentinel not in encoded
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", delete=False
    ) as handle:
        handle.write(encoded)
        path = Path(handle.name)
    try:
        completed = subprocess.run(
            ["node", str(HELPER), str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        path.unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _assert_not_repository_pin(text: str) -> None:
    for fragment in PIN_TEXT:
        assert fragment not in text


def _assert_projection(projected: dict, versions: dict[str, str | None]) -> None:
    assert projected["status"] == 200
    body = projected["body"]
    assert body["game_version"] == versions["source_game_version"]
    assert body["resource_version"] == versions["source_resource_version"]
    assert body["changelist"] == versions["source_changelist"]
    assert "llm_configured" not in body
    assert set(body) == {
        "changelist",
        "game_version",
        "request_id",
        "resource_version",
        "schema_version",
        "source_commit",
        "term_count",
    }


def test_recorded_database_versions_reach_the_site_projection(
    tmp_path, sample_db, monkeypatch
):
    db = _copy_db(tmp_path, sample_db)
    _store(db, RECORDED)
    recorded, response = _read_meta(tmp_path, db, monkeypatch)
    body = response.json()

    assert recorded.source_game_version == "9.9-test"
    assert recorded.source_resource_version == "9.9-resource-test"
    assert recorded.source_changelist == "123456-test"
    assert body["source_game_version"] == "9.9-test"
    assert body["source_resource_version"] == "9.9-resource-test"
    assert body["source_changelist"] == "123456-test"
    _assert_not_repository_pin(response.text)
    _assert_projection(_project(body), RECORDED)


def test_missing_version_keys_are_json_null_through_the_site_projection(
    tmp_path, sample_db, monkeypatch
):
    db = _copy_db(tmp_path, sample_db)
    _store(db, dict.fromkeys(VERSION_KEYS))
    recorded, response = _read_meta(tmp_path, db, monkeypatch)
    body = response.json()

    assert recorded.source_game_version is None
    assert recorded.source_resource_version is None
    assert recorded.source_changelist is None
    for key in VERSION_KEYS:
        assert key in body
        assert body[key] is None
    _assert_not_repository_pin(response.text)
    _assert_projection(_project(body), dict.fromkeys(VERSION_KEYS))


def test_unavailable_version_strings_stay_unavailable(
    tmp_path, sample_db, monkeypatch
):
    stored = dict.fromkeys(VERSION_KEYS, "unavailable")
    db = _copy_db(tmp_path, sample_db)
    _store(db, stored)
    recorded, response = _read_meta(tmp_path, db, monkeypatch)
    body = response.json()

    assert recorded.source_game_version == "unavailable"
    assert recorded.source_resource_version == "unavailable"
    assert recorded.source_changelist == "unavailable"
    assert body["source_game_version"] == "unavailable"
    assert body["source_resource_version"] == "unavailable"
    assert body["source_changelist"] == "unavailable"
    _assert_projection(_project(body), stored)


def test_surrounding_spaces_are_not_trimmed(tmp_path, sample_db, monkeypatch):
    stored = {
        "source_game_version": " 9.9-test ",
        "source_resource_version": "9.9-resource-test",
        "source_changelist": "123456-test",
    }
    db = _copy_db(tmp_path, sample_db)
    _store(db, stored)
    recorded, response = _read_meta(tmp_path, db, monkeypatch)
    body = response.json()

    assert recorded.source_game_version == " 9.9-test "
    assert body["source_game_version"] == " 9.9-test "
    _assert_projection(_project(body), stored)


def test_site_rejects_an_unapproved_key_on_the_real_meta_body(
    tmp_path, sample_db, monkeypatch
):
    db = _copy_db(tmp_path, sample_db)
    _store(db, RECORDED)
    _recorded, response = _read_meta(tmp_path, db, monkeypatch)
    body = response.json()
    accepted = _project(body)
    _assert_projection(accepted, RECORDED)

    contaminated = dict(body)
    contaminated["source_repo_url"] = "not-a-version-field"
    rejected = _project(contaminated)
    assert rejected["status"] == 502
    assert rejected["body"] == {
        "status": "unavailable",
        "reason": "upstream_schema_mismatch",
    }


def test_site_rejects_a_partial_provenance_shape_on_the_real_meta_body(
    tmp_path, sample_db, monkeypatch
):
    db = _copy_db(tmp_path, sample_db)
    _store(db, RECORDED)
    _recorded, response = _read_meta(tmp_path, db, monkeypatch)
    body = response.json()
    partial = dict(body)
    del partial["source_changelist"]
    rejected = _project(partial)
    assert rejected["status"] == 502
    assert rejected["body"] == {
        "status": "unavailable",
        "reason": "upstream_schema_mismatch",
    }
