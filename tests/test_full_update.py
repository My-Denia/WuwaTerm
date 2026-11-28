"""End-to-end tests for the unified ``terms.db`` + companion ``knowledge.db`` lifecycle.

Covers:
1. ``wuwaterm.cli build-kb`` + ``scripts/verify_knowledge_db.py`` real build & check;
2. ``scripts/full_update.py resolve-kb`` path resolution and fail-closed confinement;
3. Unified ``deploy/vps-update.sh`` promotion of ``terms.db`` + ``knowledge.db``
   bound to the same ``terms_db_revision``, including manifest recording;
4. Pre-stop fail-closed rejection when candidate ``knowledge.db`` mismatches
   candidate ``terms.db`` revision;
5. Post-promotion failure rolling back BOTH ``terms.db`` and ``knowledge.db``
   (including cleaning up a newly introduced ``knowledge.db`` when none existed);
6. ``--recover-full`` interruption recovery for paired ``terms.db`` + ``knowledge.db``
   transactions and backward compatibility with legacy single-DB journals/flows;
7. Backup retention pruning companion ``knowledge.<id>.<sha>.db`` snapshots.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import full_update
from scripts.deployment_manifest import (
    ManifestError,
    build_manifest,
    publish_commit_pointer,
    verify_manifest,
    write_manifest,
)
from scripts.verify_knowledge_db import main as verify_kb_main
from scripts.verify_knowledge_db import verify_knowledge_database
from test_deploy_scripts import (
    NEW_COMMIT,
    NEW_IMAGE,
    OLD_COMMIT,
    OLD_IMAGE,
    _make_deploy_harness,
    _run_vps_update,
)
from test_full_deploy_recovery import (
    API_ROLLBACK_REF,
    BOT_ROLLBACK_REF,
    JOURNAL_RELPATH,
    OLD_REF,
    TXN,
    _journal,
    _plant,
    _run_recover,
    _surface,
    _write_full_fake_docker,
    _write_full_fake_python,
)
from test_knowledge_base import _build_synthetic_wutheringdata, _provenance
from wuwaterm.cli import main as cli_main
from wuwaterm.knowledge_builder import build_knowledge_database
from wuwaterm.knowledge_db import compute_terms_db_revision

ROOT = Path(__file__).resolve().parents[1]


def _build_matching_kb(
    data_dir: Path, kb_path: Path, terms_db_path: Path
) -> tuple[str, str]:
    build_knowledge_database(
        data_dir,
        kb_path,
        terms_db_path=terms_db_path,
        source_provenance=_provenance(),
    )
    kb_sha = hashlib.sha256(kb_path.read_bytes()).hexdigest()
    rev = compute_terms_db_revision(terms_db_path)
    return kb_sha, rev


def _augment_harness_with_kb(
    tmp_path: Path,
    *,
    enable_in_env: bool = True,
    seed_old_kb: bool = True,
    env_kb_value: str = "/app/data/knowledge.db",
    mismatch_candidate_kb: bool = False,
) -> dict[str, object]:
    root, env, old_db_hash, new_db_hash = _make_deploy_harness(tmp_path)
    shutil.copy2(
        ROOT / "scripts" / "verify_knowledge_db.py",
        root / "scripts" / "verify_knowledge_db.py",
    )

    synth_data = _build_synthetic_wutheringdata(tmp_path / "synth-wutheringdata")
    old_terms = root / "data" / "terms.db"
    new_terms = root / "data" / "candidate-seed.db"

    old_kb_hash: str | None = None
    old_rev = compute_terms_db_revision(old_terms)
    new_rev = compute_terms_db_revision(new_terms)
    assert old_rev != new_rev

    # Always prebuild the old and new companion KB seeds from the same synthetic data
    old_kb_seed = root / "data" / "old-kb-seed.db"
    old_kb_seed_hash, _ = _build_matching_kb(synth_data, old_kb_seed, old_terms)

    new_kb_seed = root / "data" / "candidate-kb-seed.db"
    target_terms_for_candidate_kb = old_terms if mismatch_candidate_kb else new_terms
    new_kb_hash, _ = _build_matching_kb(
        synth_data, new_kb_seed, target_terms_for_candidate_kb
    )

    kb_rel = (
        "data/" + env_kb_value[len("/app/data/") :]
        if env_kb_value.startswith("/app/data/")
        else env_kb_value
    )
    live_kb = root / kb_rel
    if seed_old_kb:
        live_kb.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_kb_seed, live_kb)
        old_kb_hash = old_kb_seed_hash

    if enable_in_env:
        env_file = root / ".env"
        env_text = env_file.read_text(encoding="utf-8")
        env_file.write_text(
            env_text + f"WUWATERM_API_KNOWLEDGE_DB_PATH={env_kb_value}\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)

    # Extend the fake docker binary with build-kb and verify-kb handlers.
    docker_script = tmp_path / "bin" / "docker"
    original = docker_script.read_text(encoding="utf-8")
    kb_handlers = (
        "  *'build-kb --atomic'*)\n"
        "    kb_cand=''\n"
        "    for arg in \"$@\"; do\n"
        "      case \"$arg\" in WUWATERM_KNOWLEDGE_DB_PATH=*) kb_cand=${arg#WUWATERM_KNOWLEDGE_DB_PATH=} ;; esac\n"
        "    done\n"
        "    kb_cand=$FAKE_DEPLOY_ROOT/data/${kb_cand#/app/data/}\n"
        "    cp \"$FAKE_DEPLOY_ROOT/data/candidate-kb-seed.db\" \"$kb_cand\"\n"
        "    ;;\n"
        "  *'verify-kb'*)\n"
        "    terms_cand=''\n"
        "    kb_cand=''\n"
        "    for arg in \"$@\"; do\n"
        "      case \"$arg\" in\n"
        "        WUWATERM_DB_PATH=*) terms_cand=${arg#WUWATERM_DB_PATH=} ;;\n"
        "        WUWATERM_KNOWLEDGE_DB_PATH=*) kb_cand=${arg#WUWATERM_KNOWLEDGE_DB_PATH=} ;;\n"
        "      esac\n"
        "    done\n"
        "    terms_cand=$FAKE_DEPLOY_ROOT/data/${terms_cand#/app/data/}\n"
        "    kb_cand=$FAKE_DEPLOY_ROOT/data/${kb_cand#/app/data/}\n"
        "    python3 \"$FAKE_DEPLOY_ROOT/scripts/verify_knowledge_db.py\" \"$kb_cand\" --terms-db \"$terms_cand\"\n"
        "    ;;\n"
    )
    anchor = "  *'verify-db'*)\n"
    assert anchor in original
    docker_script.write_text(
        original.replace(anchor, kb_handlers + anchor, 1),
        encoding="utf-8",
    )
    docker_script.chmod(0o755)

    return {
        "root": root,
        "env": env,
        "old_db_hash": old_db_hash,
        "new_db_hash": new_db_hash,
        "old_kb_hash": old_kb_hash,
        "new_kb_hash": new_kb_hash,
        "old_rev": old_rev,
        "new_rev": new_rev,
        "kb_rel": kb_rel,
        "live_kb": live_kb,
        "synth_data": synth_data,
    }


def test_cli_build_kb_and_verify_knowledge_db_end_to_end(
    tmp_path: Path, monkeypatch, capsys
):
    from wuwaterm.constants import get_source_profile
    from wuwaterm.db import create_database
    from wuwaterm.models import TermRecord

    data_dir = _build_synthetic_wutheringdata(tmp_path / "wutheringdata")
    terms_db = tmp_path / "terms.db"
    kb_db = tmp_path / "knowledge.db"
    profile = get_source_profile("arikatsu")
    create_database(
        terms_db,
        [
            TermRecord(
                category="resonator",
                source_file="roleinfo.json",
                source_id="1603",
                text_key="RoleInfo_1603_Name",
                zh="椿",
                en="Camellya",
            )
        ],
        source_profile=profile,
        source_provenance=_provenance(),
    )

    monkeypatch.setattr(
        "wuwaterm.knowledge_builder.inspect_data_source",
        lambda _root, _profile: _provenance(),
    )

    rc = cli_main(
        [
            "build-kb",
            "--data-dir",
            str(data_dir),
            "--db",
            str(kb_db),
            "--terms-db",
            str(terms_db),
            "--profile",
            "arikatsu",
            "--atomic",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "built " in out and "entities" in out and str(kb_db) in out

    verify_rc = verify_kb_main([str(kb_db), "--terms-db", str(terms_db)])
    assert verify_rc == 0
    verify_out = capsys.readouterr().out
    expected_rev = compute_terms_db_revision(terms_db)
    assert f"terms_db_revision\t{expected_rev}" in verify_out


def test_resolve_kb_path_env_existing_file_and_confinement(tmp_path: Path):
    root = tmp_path / "deploy"
    (root / "data").mkdir(parents=True)
    env_file = root / ".env"
    env_file.write_text("#WUWATERM_API_KNOWLEDGE_DB_PATH=/app/data/knowledge.db\n")

    assert full_update.resolve_knowledge_db_relpath(root) is None

    # Existing data/knowledge.db activates companion lifecycle even without .env knob.
    (root / "data" / "knowledge.db").write_bytes(b"kb")
    assert full_update.resolve_knowledge_db_relpath(root) == "data/knowledge.db"
    (root / "data" / "knowledge.db").unlink()

    # Configured /app/data/... path activates companion lifecycle before file exists.
    env_file.write_text("WUWATERM_API_KNOWLEDGE_DB_PATH=/app/data/kb/custom.db\n")
    assert full_update.resolve_knowledge_db_relpath(root) == "data/kb/custom.db"

    # Quoted value and export prefix are handled without shell execution.
    env_file.write_text('export WUWATERM_API_KNOWLEDGE_DB_PATH="/app/data/knowledge.db"\n')
    assert full_update.resolve_knowledge_db_relpath(root) == "data/knowledge.db"

    # Paths outside /app/data/, pointing to terms.db, or escaping via .. are refused.
    for bad in (
        "/tmp/knowledge.db",
        "/app/data/terms.db",
        "/app/data/../state/knowledge.db",
        "/app/data/candidates/knowledge.db",
        "/app/data/deployment-backups/knowledge.db",
    ):
        env_file.write_text(f"WUWATERM_API_KNOWLEDGE_DB_PATH={bad}\n")
        with pytest.raises(full_update.FullUpdateError, match="invalid_kb_path"):
            full_update.resolve_knowledge_db_relpath(root)

    # Symlinked data/knowledge.db is refused fail-closed.
    env_file.write_text("")
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"outside")
    (root / "data" / "knowledge.db").symlink_to(outside)
    with pytest.raises(full_update.FullUpdateError, match="invalid_kb_path"):
        full_update.resolve_knowledge_db_relpath(root)


def test_vps_update_unified_promote_updates_terms_and_kb_and_records_manifest(
    tmp_path: Path,
):
    ctx = _augment_harness_with_kb(tmp_path, enable_in_env=True, seed_old_kb=True)
    root: Path = ctx["root"]  # type: ignore[assignment]
    env: dict[str, str] = ctx["env"]  # type: ignore[assignment]
    live_kb: Path = ctx["live_kb"]  # type: ignore[assignment]

    result = _run_vps_update(root, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == ctx["new_db_hash"]
    assert hashlib.sha256(live_kb.read_bytes()).hexdigest() == ctx["new_kb_hash"]
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{NEW_COMMIT}\n"
    assert not (root / JOURNAL_RELPATH).exists()
    assert f"deployment knowledge database sha256: {ctx['new_kb_hash']}" in result.stdout

    counts, meta = verify_knowledge_database(live_kb, terms_db_path=root / "data" / "terms.db")
    assert counts and meta["terms_db_revision"] == ctx["new_rev"]

    manifest_path = root / ".deployments" / f"{NEW_COMMIT}.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert "knowledge_database" in payload
    kb_entry = payload["knowledge_database"]
    assert kb_entry["path"] == "data/knowledge.db"
    assert kb_entry["sha256"] == ctx["new_kb_hash"]
    assert kb_entry["terms_db_revision"] == ctx["new_rev"]
    assert kb_entry["backup_path"].startswith("data/deployment-backups/knowledge.")
    assert (root / kb_entry["backup_path"]).is_file()
    assert (
        hashlib.sha256((root / kb_entry["backup_path"]).read_bytes()).hexdigest()
        == ctx["old_kb_hash"]
    )

    # Manifest verification passes with the matching pair and rejects a tampered KB.
    verify_manifest(
        manifest_path,
        source_commit=NEW_COMMIT,
        image_id=NEW_IMAGE,
        image_digest=NEW_IMAGE,
        db_path=root / "data" / "terms.db",
        immutable=True,
        kb_path=live_kb,
    )
    with pytest.raises(ManifestError, match="knowledge_database"):
        verify_manifest(
            manifest_path,
            source_commit=NEW_COMMIT,
            image_id=NEW_IMAGE,
            image_digest=NEW_IMAGE,
            db_path=root / "data" / "terms.db",
            immutable=True,
            kb_path=root / kb_entry["backup_path"],
        )


def test_vps_update_auto_manages_existing_knowledge_db_without_env_knob(tmp_path: Path):
    ctx = _augment_harness_with_kb(tmp_path, enable_in_env=False, seed_old_kb=True)
    root: Path = ctx["root"]  # type: ignore[assignment]
    env: dict[str, str] = ctx["env"]  # type: ignore[assignment]
    live_kb: Path = ctx["live_kb"]  # type: ignore[assignment]

    result = _run_vps_update(root, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert hashlib.sha256(live_kb.read_bytes()).hexdigest() == ctx["new_kb_hash"]
    verify_knowledge_database(live_kb, terms_db_path=root / "data" / "terms.db")


def test_vps_update_rejects_mismatched_candidate_kb_before_stopping_container(
    tmp_path: Path,
):
    ctx = _augment_harness_with_kb(
        tmp_path,
        enable_in_env=True,
        seed_old_kb=True,
        mismatch_candidate_kb=True,
    )
    root: Path = ctx["root"]  # type: ignore[assignment]
    env: dict[str, str] = ctx["env"]  # type: ignore[assignment]
    live_kb: Path = ctx["live_kb"]  # type: ignore[assignment]

    result = _run_vps_update(root, env)

    assert result.returncode != 0
    assert "terms_db_revision mismatch" in result.stderr
    # Live DBs, pointer, and running container were NEVER touched.
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == ctx["old_db_hash"]
    assert hashlib.sha256(live_kb.read_bytes()).hexdigest() == ctx["old_kb_hash"]
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (root / "running-image").read_text(encoding="ascii").strip() == OLD_IMAGE
    assert not (root / JOURNAL_RELPATH).exists()
    docker_lines = (root / "docker.log").read_text(encoding="utf-8").splitlines()
    assert not any("stop wuwaterm" in line for line in docker_lines)
    assert not any("up -d" in line for line in docker_lines)


@pytest.mark.parametrize("failure_step", ("smoke", "manifest", "pointer"))
def test_vps_update_post_promote_failure_rolls_back_both_terms_and_kb(
    tmp_path: Path, failure_step: str
):
    ctx = _augment_harness_with_kb(tmp_path, enable_in_env=True, seed_old_kb=True)
    root: Path = ctx["root"]  # type: ignore[assignment]
    env: dict[str, str] = dict(ctx["env"])  # type: ignore[arg-type]
    live_kb: Path = ctx["live_kb"]  # type: ignore[assignment]
    env["WUWATERM_FAIL_STEP"] = failure_step

    result = _run_vps_update(root, env)

    assert result.returncode == 97, result.stdout + result.stderr
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == ctx["old_db_hash"]
    assert hashlib.sha256(live_kb.read_bytes()).hexdigest() == ctx["old_kb_hash"]
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (root / "running-image").read_text(encoding="ascii").strip() == OLD_IMAGE
    assert not (root / JOURNAL_RELPATH).exists()
    verify_knowledge_database(live_kb, terms_db_path=root / "data" / "terms.db")


def test_vps_update_post_promote_failure_cleans_up_new_kb_when_none_existed(
    tmp_path: Path,
):
    ctx = _augment_harness_with_kb(tmp_path, enable_in_env=True, seed_old_kb=False)
    root: Path = ctx["root"]  # type: ignore[assignment]
    env: dict[str, str] = dict(ctx["env"])  # type: ignore[arg-type]
    live_kb: Path = ctx["live_kb"]  # type: ignore[assignment]
    assert not live_kb.exists()
    env["WUWATERM_FAIL_STEP"] = "smoke"

    result = _run_vps_update(root, env)

    assert result.returncode == 97, result.stdout + result.stderr
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == ctx["old_db_hash"]
    assert not live_kb.exists()
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (root / "running-image").read_text(encoding="ascii").strip() == OLD_IMAGE
    assert not (root / JOURNAL_RELPATH).exists()


def test_recover_full_rolls_back_and_confirms_paired_terms_and_kb(tmp_path: Path):
    ctx = _augment_harness_with_kb(tmp_path, enable_in_env=True, seed_old_kb=True)
    root: Path = ctx["root"]  # type: ignore[assignment]
    env: dict[str, str] = ctx["env"]  # type: ignore[assignment]
    live_kb: Path = ctx["live_kb"]  # type: ignore[assignment]
    _write_full_fake_docker(root)
    _write_full_fake_python(root)
    _surface(root, "bot", image=OLD_IMAGE, ref=OLD_REF, running=False)
    _surface(root, "api", image=NEW_IMAGE, ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    (root / f"tag-{BOT_ROLLBACK_REF}").write_text(f"{OLD_IMAGE}\n", encoding="ascii")
    (root / f"tag-{API_ROLLBACK_REF}").write_text(f"{OLD_IMAGE}\n", encoding="ascii")

    old_db_hash = str(ctx["old_db_hash"])
    new_db_hash = str(ctx["new_db_hash"])
    old_kb_hash = str(ctx["old_kb_hash"])
    new_kb_hash = str(ctx["new_kb_hash"])
    new_rev = str(ctx["new_rev"])

    db_backup_rel = f"data/deployment-backups/terms.{TXN}.{old_db_hash}.db"
    kb_backup_rel = f"data/deployment-backups/knowledge.{TXN}.{old_kb_hash}.db"
    (root / db_backup_rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "data" / "terms.db", root / db_backup_rel)
    shutil.copy2(live_kb, root / kb_backup_rel)

    # Simulate crash after both DBs were promoted and API started, before pointer moved.
    shutil.copy2(root / "data" / "candidate-seed.db", root / "data" / "terms.db")
    shutil.copy2(root / "data" / "candidate-kb-seed.db", live_kb)

    payload = _journal(
        root,
        phase="started",
        old_db_sha=old_db_hash,
        db_backup=db_backup_rel,
        new_db_sha=new_db_hash,
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    )
    payload["knowledge_db"] = {
        "path": "data/knowledge.db",
        "old_sha256": old_kb_hash,
        "new_sha256": new_kb_hash,
        "backup_path": kb_backup_rel,
        "candidate_path": f"data/candidates/knowledge.{TXN}.db",
        "terms_db_revision": new_rev,
    }
    _plant(root, payload)

    rec = _run_recover(root, env)
    assert rec.returncode == 0, rec.stdout + rec.stderr
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == old_db_hash
    assert hashlib.sha256(live_kb.read_bytes()).hexdigest() == old_kb_hash
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert not (root / JOURNAL_RELPATH).exists()
    verify_knowledge_database(live_kb, terms_db_path=root / "data" / "terms.db")

    # Now test committed-side confirmation with paired terms.db + knowledge.db.
    shutil.copy2(root / "data" / "candidate-seed.db", root / "data" / "terms.db")
    shutil.copy2(root / "data" / "candidate-kb-seed.db", live_kb)
    _surface(root, "api", image=NEW_IMAGE, ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    publish_commit_pointer(root / ".deploy_commit", NEW_COMMIT)
    write_manifest(
        root / ".deployments" / f"{NEW_COMMIT}.json",
        build_manifest(
            source_commit=NEW_COMMIT,
            image_ref=f"wuwaterm-runtime:{NEW_COMMIT}",
            image_id=NEW_IMAGE,
            image_digest=NEW_IMAGE,
            image_revision=NEW_COMMIT,
            db_path=root / "data" / "terms.db",
            db_display_path="data/terms.db",
            backup_path=db_backup_rel,
            deployment_utc="2026-10-17T00:00:00Z",
            kb_path=live_kb,
            kb_display_path="data/knowledge.db",
            kb_backup_path=kb_backup_rel,
        ),
    )
    payload["phase"] = "committed"
    _plant(root, payload)

    confirm = _run_recover(root, env)
    assert confirm.returncode == 0, confirm.stdout + confirm.stderr
    assert "confirmed on the new binding" in confirm.stderr
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == new_db_hash
    assert hashlib.sha256(live_kb.read_bytes()).hexdigest() == new_kb_hash
    assert not (root / JOURNAL_RELPATH).exists()


def test_single_db_flow_remains_fully_compatible_when_kb_disabled_and_absent(
    tmp_path: Path,
):
    root, env, _old_hash, new_hash = _make_deploy_harness(tmp_path)
    assert not (root / "data" / "knowledge.db").exists()

    result = _run_vps_update(root, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == new_hash
    assert not (root / "data" / "knowledge.db").exists()
    manifest = json.loads((root / ".deployments" / f"{NEW_COMMIT}.json").read_text(encoding="utf-8"))
    assert "knowledge_database" not in manifest
    docker_log = (root / "docker.log").read_text(encoding="utf-8")
    assert "build-kb" not in docker_log
    assert "verify-kb" not in docker_log


def test_backup_retention_prunes_companion_knowledge_db_backups(tmp_path: Path):
    directory = tmp_path / "backups"
    directory.mkdir()
    sha = "b" * 64
    ids = [f"2026010{day}T120000Z-{day}00" for day in range(1, 8)]
    for snap_id in ids:
        for name in (
            f"terms.{snap_id}.{sha}.db",
            f"knowledge.{snap_id}.{sha}.db",
            f"db-diff.{snap_id}.json",
            f"{snap_id}.deploy_commit",
        ):
            (directory / name).write_text(name, encoding="ascii")

    res = subprocess.run(
        [
            "sh",
            "-c",
            '. "$1"; backup_retention_prune "$2" 5 "$3"',
            "sh",
            str(ROOT / "deploy" / "backup-retention.sh"),
            str(directory),
            ids[-1],
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    remaining = set(os.listdir(directory))
    # Oldest 2 snapshots (including their knowledge.*.db backups) removed; newest 5 kept.
    for dropped in ids[:2]:
        assert f"terms.{dropped}.{sha}.db" not in remaining
        assert f"knowledge.{dropped}.{sha}.db" not in remaining
    for kept in ids[2:]:
        assert f"terms.{kept}.{sha}.db" in remaining
        assert f"knowledge.{kept}.{sha}.db" in remaining
