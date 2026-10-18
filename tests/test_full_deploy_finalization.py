"""Final recovery-boundary regressions, isolated fake Docker only.

These tests cover the observed last gaps between explicit recovery and the
shell EXIT trap; they do not claim or inject production damage.
"""
from __future__ import annotations

import json

import pytest

from test_full_deploy_recovery import (
    JOURNAL_RELPATH,
    NEW_IMAGE,
    UNKNOWN_IMAGE,
    _docker_log,
    _run_mode,
    _run_recover,
    _started_reality,
)

pytest_plugins = ["test_full_deploy_recovery"]


@pytest.mark.parametrize("surface", ["bot", "api"])
def test_shell_rollback_refuses_drifted_tag_before_start(full_harness, surface):
    root, env, _old_hash, _new_hash, old_bytes, _new_bytes, _backup = full_harness
    # Drift the already-recorded tag after the replacement services start,
    # before the ordinary start-failure enters the real EXIT rollback trap.
    wrapper = root.parent / "bin" / "python3"
    script = wrapper.read_text(encoding="utf-8")
    needle = "  *'full_update.py phase started'*)\n"
    assert script.count(needle) == 1
    pattern = "rollback-[0-9]*" if surface == "bot" else "rollback-api-*"
    script = script.replace(
        needle,
        needle + f'    for f in "$FAKE_DEPLOY_ROOT"/tag-wuwaterm-runtime:{pattern}; do\n'
        '      [ -e "$f" ] || continue\n'
        f'      printf "%s\\n" "{UNKNOWN_IMAGE}" > "$f"\n'
        '    done\n',
    )
    wrapper.write_text(script, encoding="utf-8")
    env["WUWATERM_FAIL_STEP"] = "start"

    result = _run_mode(root, env, [])

    assert result.returncode == 97, result.stderr
    record = json.loads((root / JOURNAL_RELPATH).read_text(encoding="utf-8"))
    key = "rollback_image_ref" if surface == "bot" else "rollback_api_image_ref"
    drifted = record[key]
    assert (root / f"{surface}-image").read_text().strip() == NEW_IMAGE
    assert (root / f"{surface}-running").read_text().strip() == "false"
    assert not any(line.startswith(drifted + "|") and " up -d " in line
                   for line in _docker_log(root)), "drifted tag must not execute"
    assert (root / "data" / "terms.db").read_bytes() == old_bytes


@pytest.mark.parametrize("fault", ["backup-bytes", "unknown-live-db"])
def test_explicit_recovery_refuses_bad_bytes_before_stopping(full_harness, fault):
    root, env, _old_hash, _new_hash, new_bytes = _started_reality(full_harness)
    journal = json.loads((root / JOURNAL_RELPATH).read_text())
    expected = new_bytes
    if fault == "backup-bytes":
        (root / journal["db_backup"]).write_bytes(b"corrupted backup")
    else:
        expected = b"unknown live bytes"
        (root / "data" / "terms.db").write_bytes(expected)

    result = _run_recover(root, env)

    assert result.returncode == 1, result.stdout
    assert (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == expected
    for surface in ("bot", "api"):
        assert (root / f"{surface}-running").read_text().strip() == "true"
        assert (root / f"{surface}-image").read_text().strip() == NEW_IMAGE
    assert not any(" stop " in line or " up -d " in line
                   for line in _docker_log(root))


def test_equal_hash_restore_failure_does_not_restart_services(full_harness):
    root, env, _old_hash, _new_hash, old_bytes, _new_bytes, _backup = full_harness
    (root / "data" / "candidate-seed.db").write_bytes(old_bytes)
    env["WUWATERM_FAIL_STEP"] = "start"
    env["FULL_CORRUPT_BACKUP"] = "1"

    result = _run_mode(root, env, [])

    assert result.returncode == 97, result.stderr
    assert "verified database restoration failed" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == old_bytes
    for surface in ("bot", "api"):
        assert (root / f"{surface}-running").read_text().strip() == "false"
    assert not any(" up -d " in line and "rollback" in line
                   for line in _docker_log(root))
