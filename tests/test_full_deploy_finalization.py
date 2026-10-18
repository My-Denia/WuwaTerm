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


def test_exit_clears_coherent_api_owned_distinct_image_binding(full_harness):
    from scripts.deployment_manifest import build_manifest, write_manifest
    from test_full_deploy_recovery import (
        API_ALT_IMAGE, OLD_COMMIT, OLD_DEPLOYED, OLD_IMAGE, _actions, _surface,
    )

    root, env, _old_hash, _new_hash, old_bytes, _nb, _backup = full_harness
    api_ref = "wuwaterm-runtime:api-old"
    _surface(root, "api", image=API_ALT_IMAGE, ref=api_ref, running=True)
    manifest = root / ".deployments" / f"{OLD_COMMIT}.json"
    manifest.chmod(0o600)
    manifest.unlink()
    write_manifest(manifest, build_manifest(
        source_commit=OLD_COMMIT, image_ref=api_ref, image_id=API_ALT_IMAGE,
        image_digest=API_ALT_IMAGE, image_revision=OLD_COMMIT,
        db_path=root / "data" / "terms.db", db_display_path="data/terms.db",
        backup_path="legacy", deployment_utc=OLD_DEPLOYED,
    ))
    env["WUWATERM_FAIL_STEP"] = "start"

    result = _run_mode(root, env, [])

    assert result.returncode == 97, result.stderr
    assert not (root / JOURNAL_RELPATH).exists(), result.stderr
    assert "rollback completed with errors" not in result.stderr
    assert (root / "data" / "terms.db").read_bytes() == old_bytes
    assert (root / "bot-image").read_text().strip() == OLD_IMAGE
    assert (root / "api-image").read_text().strip() == API_ALT_IMAGE
    assert any("full_update.py rollback-clear" in line for line in _actions(root))


def test_exit_authority_refuses_manifest_tampered_after_restoration(full_harness):
    from test_full_deploy_recovery import OLD_COMMIT, _actions

    root, env, _old_hash, _new_hash, old_bytes, _nb, _backup = full_harness
    tamper = root / "tamper-old-manifest.py"
    tamper.write_text(
        'import json\nfrom pathlib import Path\n'
        f'p = Path(".deployments/{OLD_COMMIT}.json")\np.chmod(0o600)\n'
        'value = json.loads(p.read_text())\n'
        f'value["image"]["id"] = "{UNKNOWN_IMAGE}"\n'
        'p.write_text(json.dumps(value))\np.chmod(0o444)\n',
        encoding="utf-8",
    )
    wrapper = root.parent / "bin" / "python3"
    script = wrapper.read_text()
    needle = 'case "$*" in\n'
    assert script.count(needle) == 1
    wrapper.write_text(script.replace(
        needle, needle + '  *"full_update.py rollback-clear"*)\n'
        '    "$real_python" "$FAKE_DEPLOY_ROOT/tamper-old-manifest.py"\n'
        '    exec "$real_python" "$@" ;;\n',
    ))
    env["WUWATERM_FAIL_STEP"] = "start"

    result = _run_mode(root, env, [])

    assert result.returncode == 97
    assert (root / JOURNAL_RELPATH).exists()
    assert "failed observed verification" in result.stderr
    assert (root / "data" / "terms.db").read_bytes() == old_bytes
    assert any("full_update.py rollback-clear" in line for line in _actions(root))
