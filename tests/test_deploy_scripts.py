from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from wuwaterm.constants import SOURCE_PROFILES, get_source_profile
from wuwaterm.data_source import SourceProvenance
from wuwaterm.db import create_database
from wuwaterm.models import TermRecord

import scripts.deployment_manifest as deployment_manifest_module
from scripts.deployment_manifest import (
    build_manifest,
    durable_remove,
    durable_replace,
    verify_manifest,
    write_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def test_vps_update_uses_atomic_database_build():
    text = (ROOT / "deploy" / "vps-update.sh").read_text(encoding="utf-8")

    assert "wuwaterm-builder build-db --atomic" in text


def test_vps_update_uses_builder_service_for_data_commands():
    text = (ROOT / "deploy" / "vps-update.sh").read_text(encoding="utf-8")

    assert "wuwaterm refresh-data" not in text
    assert "wuwaterm build-db" not in text
    assert "wuwaterm verify-db" not in text
    assert "wuwaterm-builder refresh-data" in text
    assert "wuwaterm-builder build-db --atomic" in text
    assert "wuwaterm-builder verify-db" in text
    assert "scripts/verify_exact_hits.py" in text
    assert "scripts/verify_seed_terms.py" in text
    assert "scripts/diff_terms_db.py" in text
    assert "${TELEGRAM_BOT_TOKEN+x}" in text
    assert "${TELEGRAM_TEST_CHAT_ID+x}" in text
    assert "${WUWATERM_OPENAI_API_KEY+x}" in text
    assert "${WUWATERM_OPENAI_BASE_URL+x}" in text
    assert "${WUWATERM_REDACTION_SECRET+x}" in text
    assert "chat_settings.json" in text
    assert "channel_replies.json" in text
    assert 'data/$state_file' in text
    assert 'state/$state_file' in text
    assert 'python3 scripts/validate_state_file.py "$tmp" "$state_file"' in text
    assert 'ln "$tmp" "state/$state_file"' in text
    assert (
        'python3 scripts/validate_state_file.py "state/$state_file" "$state_file"'
        in text
    )


def test_vps_update_freezes_legacy_state_only_after_database_is_ready():
    text = (ROOT / "deploy" / "vps-update.sh").read_text(encoding="utf-8")

    builder = text.index("compose build wuwaterm-builder")
    refresh = text.index("wuwaterm-builder refresh-data")
    build = text.index("wuwaterm-builder build-db --atomic")
    verify = text.index("wuwaterm-builder verify-db")
    transaction = text.index("# Candidate DB and immutable image")
    stop = text.index("stop wuwaterm", transaction)
    migrate = text.index("for state_file in chat_settings.json channel_replies.json")
    start = text.index("compose up -d --no-build", transaction)

    assert builder < refresh < build < verify < stop < migrate < start
    assert "rollback_on_failure" in text
    assert "rollback_image_ref" in text


def test_vps_update_durably_promotes_and_restores_database_and_pointer():
    text = (ROOT / "deploy" / "vps-update.sh").read_text(encoding="utf-8")

    backup = text.index(
        'python3 scripts/deployment_manifest.py durable-replace \\\n'
        '    --source "$db_backup_tmp" --destination "$db_backup"'
    )
    backup_hash = text.index('sha256sum "$db_backup"', backup)
    promotion = text.index(
        'db_promoted=1\npython3 scripts/deployment_manifest.py durable-replace'
    )
    pointer = text.index(
        'pointer_published=1\npython3 scripts/deployment_manifest.py publish-pointer'
    )
    assert backup < backup_hash < promotion < pointer
    assert (
        'python3 scripts/deployment_manifest.py durable-remove \\\n'
        '        --path "$db_path"'
        in text
    )
    assert '--path "$pointer_path"' in text
    assert '--source "$restore_tmp" --destination "$db_path"' in text


def test_durable_replace_and_remove_fsync_changed_directories(monkeypatch, tmp_path):
    source_dir = tmp_path / "candidates"
    destination_dir = tmp_path / "data"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "candidate.db"
    destination = destination_dir / "terms.db"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    synced_files: list[Path] = []
    synced: list[Path] = []
    monkeypatch.setattr(
        deployment_manifest_module, "_fsync_file", synced_files.append
    )
    monkeypatch.setattr(
        deployment_manifest_module, "_fsync_directory", synced.append
    )

    durable_replace(source, destination)

    assert destination.read_bytes() == b"new"
    assert not source.exists()
    assert synced_files == [source]
    assert synced == [destination_dir.resolve(), source_dir.resolve()]

    synced.clear()
    durable_remove(destination)
    assert not destination.exists()
    assert synced == [destination_dir.resolve()]


def test_deployment_docs_do_not_recommend_live_non_atomic_state_copy():
    """The warning has to reach whoever is upgrading an existing deployment.

    This guard is about DATA LOSS - copying state files out from under a
    running bot - not about wording. The upgrade procedure, and so the warning,
    lives in docs/deployment.md, which the positive assertion pins.

    The READMEs used to repeat the upgrade notes in both languages, and each
    asserted its own copy of the warning. They are now a landing page
    (README.md in English, README.zh-CN.md in Chinese) that carries no upgrade
    procedure at all and sends operators to the guides, so there is no second
    copy of the warning to pin. What stays is the NEGATIVE scan over BOTH
    files: a banned recipe is banned wherever a reader can find it, and a
    not-in check that names one of two documents is a scope that shrank
    without anyone deciding to shrink it - the same defect class PR #61 closed
    elsewhere.
    """
    text = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "Do not manually copy these files while the old bot is running" in text
    assert "cp -p data/chat_settings.json state/chat_settings.json" not in text
    assert "cp -p data/channel_replies.json state/channel_replies.json" not in text

    # ...and the recipe that warning exists to keep out, in every README a
    # reader might open.
    for name, document in (("README.md", readme), ("README.zh-CN.md", readme_zh)):
        assert "copy any existing `data/chat_settings.json`" not in document, name
        assert "cp -p data/chat_settings.json state/chat_settings.json" not in document, name
        assert "cp -p data/channel_replies.json state/channel_replies.json" not in document, name


def test_docker_context_excludes_data_and_runtime_state():
    entries = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "data" in entries
    assert "state" in entries
    assert "goal-runs" in entries
    assert ".deployments" in entries
    assert ".deploy_commit" in entries


def test_deployment_markers_are_ignored_runtime_state():
    entries = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert ".deployments/" in entries
    assert ".deploy_commit" in entries


def test_state_file_validator_accepts_current_state_formats(tmp_path):
    settings_path = tmp_path / "chat_settings.json"
    reply_index_path = tmp_path / "channel_replies.json"
    settings_path.write_text(
        '{"public":{"-2001":true},"allowed":[-2001]}',
        encoding="utf-8",
    )
    reply_index_path.write_text(
        '{"version":1,"entries":[{"chat_id":-2001,"message_id":4001,'
        '"expires_at":9999999999,"reply_message_ids":[5001]}]}',
        encoding="utf-8",
    )

    settings_result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "validate_state_file.py"),
            str(settings_path),
            "chat_settings.json",
        ],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    reply_result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "validate_state_file.py"),
            str(reply_index_path),
            "channel_replies.json",
        ],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert settings_result.returncode == 0, settings_result.stderr
    assert reply_result.returncode == 0, reply_result.stderr


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("chat_settings.json", '{"public":{"01":true},"allowed":[]}'),
        ("channel_replies.json", "{}"),
    ],
)
def test_state_file_validator_rejects_json_valid_schema_drift(
    tmp_path, filename, payload
):
    path = tmp_path / filename
    path.write_text(payload, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "validate_state_file.py"),
            str(path),
            filename,
        ],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1


def test_builder_entrypoint_passes_extra_build_arguments():
    text = (ROOT / "deploy" / "builder-entrypoint.sh").read_text(encoding="utf-8")

    assert 'exec python -m wuwaterm.cli build-db' in text
    assert '"$@"' in text


def test_runtime_entrypoint_forwards_bot_arguments():
    env = os.environ.copy()
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = f"{ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"

    result = subprocess.run(
        ["sh", str(ROOT / "deploy" / "entrypoint.sh"), "bot", "--help"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


@pytest.mark.parametrize(
    "args",
    [
        ["refresh-data", "--help"],
        ["build-db", "--atomic", "--help"],
        ["verify-db", "--help"],
    ],
)
def test_builder_entrypoint_forwards_extra_arguments_to_subcommands(args):
    env = os.environ.copy()
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = f"{ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"

    result = subprocess.run(
        ["sh", str(ROOT / "deploy" / "builder-entrypoint.sh"), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_builder_entrypoint_preserves_fallback_command():
    result = subprocess.run(
        ["sh", str(ROOT / "deploy" / "builder-entrypoint.sh"), "printf", "ok"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


@pytest.mark.parametrize(
    "command", ["refresh-data", "build-db", "verify-db", "shell", "python"]
)
def test_runtime_entrypoint_rejects_data_builder_commands(command: str):
    result = subprocess.run(
        ["sh", str(ROOT / "deploy" / "entrypoint.sh"), command],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 64
    assert "use the builder image for data commands" in result.stderr


@pytest.mark.parametrize(
    ("command", "expected"),
    [("api", "wuwaterm_api.cli serve"), ("device", "wuwaterm_api.cli device")],
)
def test_runtime_entrypoint_serves_the_api_surfaces(command: str, expected: str):
    """The runtime image runs both inbound surfaces, and only those."""
    text = (ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")

    assert f"exec python -m {expected}" in text
    assert f"  {command})" in text


def test_env_examples_are_byte_identical():
    assert (ROOT / ".env.example").read_bytes() == (
        ROOT / "deploy" / "env.example"
    ).read_bytes()


def test_env_example_covers_runtime_state_and_llm_controls():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in (
        "WUWATERM_LLM_TIMEOUT_SECONDS",
        "WUWATERM_LLM_MAX_CONCURRENCY",
        "WUWATERM_CHANNEL_MIN_LATIN",
        "WUWATERM_CHANNEL_MAX_PENDING",
        "WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE",
        "WUWATERM_CHANNEL_REPLY_INDEX_PATH",
        "WUWATERM_SETTINGS_PATH",
        "WUWATERM_STATE_DIR",
        "OWNER_USER_ID",
        "WUWATERM_TR_REJECT_SILENT",
    ):
        assert name in text
    assert "WUWATERM_STATE_DIR=state" in text
    assert "#WUWATERM_SETTINGS_PATH=state/chat_settings.json" in text
    assert "#WUWATERM_CHANNEL_REPLY_INDEX_PATH=state/channel_replies.json" in text
    assert "WUWATERM_SETTINGS_PATH=data/chat_settings.json" not in text
    assert "WUWATERM_CHANNEL_REPLY_INDEX_PATH=data/channel_replies.json" not in text


def test_pypinyin_is_build_only_dependency():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    dependencies = pyproject["project"]["dependencies"]
    optional = pyproject["project"]["optional-dependencies"]

    assert not any(item.startswith("pypinyin") for item in dependencies)
    assert any(item.startswith("pypinyin") for item in optional["build"])
    assert any(item.startswith("pypinyin") for item in optional["dev"])


def test_dockerfile_declares_separate_locked_runtime_and_builder_targets():
    text = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python-base AS runtime" in text
    assert "FROM python-base AS builder" in text
    assert "uv sync --locked --no-dev --no-editable --extra api --no-cache" in text
    assert "uv sync --locked --no-dev --no-editable --extra build --no-cache" in text
    runtime_section = text.split("FROM python-base AS runtime", 1)[1].split(
        "FROM python-base AS builder", 1
    )[0]
    builder_section = text.split("FROM python-base AS builder", 1)[1]
    assert "git" not in runtime_section
    assert "--extra build" not in runtime_section
    # The serving surface needs its own extra; the builder path must stay out.
    assert "--extra api" in runtime_section
    assert "--extra api" not in builder_section
    assert "apt-get install -y --no-install-recommends git ca-certificates" in builder_section
    assert "deploy/entrypoint.sh" in runtime_section
    assert "deploy/builder-entrypoint.sh" in builder_section
    assert "org.opencontainers.image.revision=$SOURCE_COMMIT" in runtime_section


def test_compose_uses_readonly_data_for_runtime_and_writable_data_for_builder():
    text = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "target: runtime" in text
    assert "target: builder" in text
    assert "wuwaterm-runtime:local" in text
    assert "wuwaterm-builder:local" in text
    assert "../data:/app/data:ro" in text
    assert "../state:/app/state" in text
    assert "WUWATERM_STATE_DIR: /app/state" in text
    assert "WUWATERM_SOURCE_PROFILE: ${WUWATERM_SOURCE_PROFILE:-arikatsu}" in text
    assert "WUWATERM_SETTINGS_PATH: /app/state/chat_settings.json" in text
    assert (
        "WUWATERM_CHANNEL_REPLY_INDEX_PATH: /app/state/channel_replies.json"
        in text
    )
    assert "../data:/app/data\n" in text
    builder_section = text.split("  wuwaterm-builder:", 1)[1]
    assert "env_file:" not in builder_section
    assert "${WUWATERM_RUNTIME_IMAGE:-wuwaterm-runtime:local}" in text
    # The builder reference is an expression whose default is the local tag,
    # and the bare default is a SUBSTRING of that expression: asserting the
    # substring alone would keep passing if the expression were dropped again.
    assert "${WUWATERM_BUILDER_IMAGE:-wuwaterm-builder:local}" in builder_section
    # Still profiled, so a bare `build` or `up` cannot start the data-build
    # toolchain by accident.
    assert 'profiles: ["builder"]' in builder_section
    assert "SOURCE_COMMIT: ${SOURCE_COMMIT:-unknown}" in text


def test_compose_builder_image_defaults_to_the_local_tag():
    """The builder reference is overridable; its DEFAULT must not move.

    deploy/vps-update.sh never sets WUWATERM_BUILDER_IMAGE and rebuilds the
    local tag from the verified checkout before every data command, so a
    default that resolved to anything else would silently change the code that
    refreshes, builds and verifies the production database.
    """
    text = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    builder_section = text.split("  wuwaterm-builder:", 1)[1]

    image_lines = [
        line.strip()
        for line in builder_section.splitlines()
        if line.strip().startswith("image:")
    ]
    assert image_lines == ["image: ${WUWATERM_BUILDER_IMAGE:-wuwaterm-builder:local}"]

    expression = image_lines[0].split("image:", 1)[1].strip()
    assert expression.startswith("${") and expression.endswith("}")
    variable, separator, default = expression[2:-1].partition(":-")
    assert variable == "WUWATERM_BUILDER_IMAGE"
    assert separator == ":-"
    assert default == "wuwaterm-builder:local"

    # ...and nothing in the repository sets the variable, so the default is
    # what the production updater and a fresh self-hosted install both get.
    for relative in ("deploy/env.example", ".env.example", "deploy/vps-update.sh"):
        assert "WUWATERM_BUILDER_IMAGE" not in (ROOT / relative).read_text(
            encoding="utf-8"
        )


def test_ghcr_overlay_declares_image_references_only():
    """The overlay may only redirect images.

    It is passed after the base file, so anything else it declared would
    silently override a reviewed decision in that file - the loopback bind, the
    read-only data mount, the blanked secrets - from a file that reads like a
    list of image names.
    """
    path = ROOT / "deploy" / "docker-compose.ghcr.yml"
    body_lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert body_lines[0] == "services:"

    services: list[str] = []
    images: list[str] = []
    for line in body_lines[1:]:
        if line.startswith("    image: "):
            images.append(line.removeprefix("    image: "))
        elif line.startswith("  ") and not line.startswith("   ") and line.endswith(":"):
            services.append(line.strip().rstrip(":"))
        else:
            raise AssertionError(f"unexpected key in the overlay: {line!r}")

    assert services == ["wuwaterm", "wuwaterm-api", "wuwaterm-builder"]
    assert len(images) == 3
    # Comments are excluded on purpose: the header explains that the overlay
    # does not remove the base file's build sections, and that sentence must
    # not be what this assertion reads.
    body = "\n".join(body_lines)
    for forbidden in (
        "build:",
        "command:",
        "volumes:",
        "environment:",
        "env_file:",
        "profiles:",
        "network_mode:",
    ):
        assert forbidden not in body

    assert any(image.startswith("ghcr.io/my-denia/wuwaterm:") for image in images)
    assert any(
        image.startswith("ghcr.io/my-denia/wuwaterm-builder:") for image in images
    )
    # One knob, and it is required: an unset tag stops Compose instead of
    # resolving to something arbitrary.
    assert all("${WUWATERM_IMAGE_TAG:?" in image for image in images)


NEW_COMMIT = "2" * 40
OLD_COMMIT = "1" * 40
OLD_IMAGE = "sha256:oldimage"
NEW_IMAGE = "sha256:newimage"


def _deployment_db(path: Path, suffix: str) -> None:
    profile = get_source_profile("arikatsu")
    provenance = SourceProvenance(
        profile=profile.name,
        repo_url=profile.repo_url,
        commit=profile.pinned_commit,
        game_version=profile.expected_game_version or "",
        resource_version=profile.expected_resource_version or "",
        changelist=profile.expected_changelist or "",
    )
    records = [
        TermRecord(
            category=category,
            source_file=f"{category}.json",
            source_id=f"{index}-{suffix}",
            text_key=f"{category}_{index}_{suffix}",
            zh="景燃" if category == "resonator" else f"测试{index}{suffix}",
            en="Jingran" if category == "resonator" else f"Test {index} {suffix}",
        )
        for index, category in enumerate(
            (
                "core_term",
                "resonator",
                "weapon",
                "echo",
                "item",
                "skill",
                "sonata_effect",
                "location",
            ),
            start=1,
        )
    ]
    create_database(
        path,
        records,
        source_profile=profile,
        source_provenance=provenance,
    )


def test_historical_manifest_readback_survives_a_later_data_pin(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "terms.db"
    _deployment_db(db_path, "historical")
    manifest_path = tmp_path / "historical.json"
    payload = build_manifest(
        source_commit=OLD_COMMIT,
        image_ref="wuwaterm-runtime:historical",
        image_id=OLD_IMAGE,
        image_digest=OLD_IMAGE,
        image_revision=OLD_COMMIT,
        db_path=db_path,
        db_display_path="data/terms.db",
        backup_path="historical-backup",
        deployment_utc="2026-07-15T00:00:00Z",
    )
    write_manifest(manifest_path, payload)

    current = SOURCE_PROFILES["arikatsu"]
    monkeypatch.setitem(
        SOURCE_PROFILES,
        "arikatsu",
        replace(
            current,
            pinned_commit="f" * 40,
            expected_game_version="9.9.0",
            expected_resource_version="9.9.1",
            expected_changelist="9999999",
        ),
    )

    verified = verify_manifest(
        manifest_path,
        source_commit=OLD_COMMIT,
        image_id=OLD_IMAGE,
        image_digest=OLD_IMAGE,
        db_path=db_path,
    )

    assert verified["database"]["provenance"]["source_commit"] == (
        current.pinned_commit
    )


def test_read_db_provenance_closes_database_connection(monkeypatch, tmp_path):
    db_path = tmp_path / "terms.db"
    _deployment_db(db_path, "close-check")
    real_connect = sqlite3.connect
    closed: list[bool] = []

    class TrackingConnection(sqlite3.Connection):
        def close(self) -> None:
            closed.append(True)
            super().close()

    def tracking_connect(*args, **kwargs):
        return real_connect(*args, factory=TrackingConnection, **kwargs)

    monkeypatch.setattr(
        deployment_manifest_module.sqlite3, "connect", tracking_connect
    )

    deployment_manifest_module.read_db_provenance(db_path)

    assert closed == [True]


@pytest.fixture()
def deploy_harness(tmp_path):
    return _make_deploy_harness(tmp_path)


def _make_deploy_harness(tmp_path):
    root = tmp_path / "deploy-root"
    (root / "deploy").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "data").mkdir()
    (root / "state").mkdir()
    (root / ".deployments").mkdir()
    shutil.copytree(ROOT / "src", root / "src")
    for relative in (
        "deploy/vps-update.sh",
        "deploy/docker-compose.yml",
        "scripts/deployment_manifest.py",
        "scripts/validate_state_file.py",
        "scripts/verify_db.py",
    ):
        shutil.copy2(ROOT / relative, root / relative)
    runtime_helper = ROOT / "scripts" / "runtime_update.py"
    if runtime_helper.is_file():
        shutil.copy2(runtime_helper, root / "scripts" / runtime_helper.name)
    env_file = root / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=not-loaded-by-builder\n", encoding="utf-8")
    env_file.chmod(0o600)

    old_db = root / "data" / "terms.db"
    seed_db = root / "data" / "candidate-seed.db"
    _deployment_db(old_db, "old")
    _deployment_db(seed_db, "new")
    (root / ".deploy_commit").write_text(f"{OLD_COMMIT}\n", encoding="ascii")
    (root / ".deploy_commit").chmod(0o444)
    old_manifest = build_manifest(
        source_commit=OLD_COMMIT,
        image_ref="wuwaterm-runtime:old",
        image_id=OLD_IMAGE,
        image_digest=OLD_IMAGE,
        image_revision=OLD_COMMIT,
        db_path=old_db,
        db_display_path="data/terms.db",
        backup_path="legacy",
        deployment_utc="2026-07-15T00:00:00Z",
    )
    write_manifest(root / ".deployments" / f"{OLD_COMMIT}.json", old_manifest)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git_script = fake_bin / "git"
    git_script.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  'status --porcelain --untracked-files=all') exit 0 ;;\n"
        "  'fetch --quiet origin main:refs/remotes/origin/main') exit 0 ;;\n"
        f"  'rev-parse HEAD') echo '{NEW_COMMIT}'; exit 0 ;;\n"
        f"  'rev-parse refs/remotes/origin/main') echo '{NEW_COMMIT}'; exit 0 ;;\n"
        "esac\n"
        "exit 2\n",
        encoding="utf-8",
    )
    git_script.chmod(0o755)

    docker_script = fake_bin / "docker"
    docker_script.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        ": \"${FAKE_DEPLOY_ROOT:?}\"\n"
        "echo \"${WUWATERM_RUNTIME_IMAGE:-unset}|$*\" >> \"$FAKE_DEPLOY_ROOT/docker.log\"\n"
        # One ordered stream across both fakes, so a test can assert that a
        # container stop happens BEFORE the database is touched.
        "echo \"docker $*\" >> \"$FAKE_DEPLOY_ROOT/actions.log\"\n"
        # Fail only the SECOND stop: the deployment's own stop must succeed so
        # the run reaches the rollback whose stop is under test.
        "if [ \"${FAKE_ALL_STOPS_FAIL:-0}\" = 1 ]; then\n"
        "  case \"$*\" in\n"
        "    *' stop '*) exit 1 ;;\n"
        "  esac\n"
        "fi\n"
        "if [ \"${FAKE_ROLLBACK_STOP_FAILURE:-0}\" = 1 ]; then\n"
        "  case \"$*\" in\n"
        "    *' stop '*)\n"
        "      stops=$(cat \"$FAKE_DEPLOY_ROOT/stop-count\" 2>/dev/null || echo 0)\n"
        "      stops=$((stops + 1))\n"
        "      echo \"$stops\" > \"$FAKE_DEPLOY_ROOT/stop-count\"\n"
        "      if [ \"$stops\" -ge 2 ]; then exit 1; fi\n"
        "      ;;\n"
        "  esac\n"
        "fi\n"
        "if [ \"${1:-}\" = inspect ]; then\n"
        # A host may run one surface and not the other: an absent container
        # answers nothing at all and exits nonzero.
        "  if [ \"${FAKE_NO_BOT_CONTAINER:-0}\" = 1 ]; then\n"
        "    case \"$*\" in *wuwaterm-bot*) exit 1 ;; esac\n"
        "  fi\n"
        # A stopped container still answers `docker inspect`, so the fake has
        # to answer the running-state query separately from the image query.
        "  case \"$*\" in\n"
        "    *State.Running*wuwaterm-api*)\n"
        "      cat \"$FAKE_DEPLOY_ROOT/api-running\" ;;\n"
        "    *State.Running*)\n"
        "      cat \"$FAKE_DEPLOY_ROOT/bot-running\" ;;\n"
        "    *)\n"
        "      cat \"$FAKE_DEPLOY_ROOT/running-image\" ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"${1:-}\" = image ] && [ \"${2:-}\" = tag ]; then exit 0; fi\n"
        "if [ \"${1:-}\" = image ] && [ \"${2:-}\" = inspect ]; then\n"
        "  case \"$*\" in\n"
        f"    *Config.Labels*) echo '{NEW_COMMIT}' ;;\n"
        f"    *RepoDigests*) echo '{NEW_IMAGE}' ;;\n"
        f"    *) echo '{NEW_IMAGE}' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        "case \"$*\" in\n"
        "  *'build-db --atomic'*)\n"
        "    candidate=''\n"
        "    for arg in \"$@\"; do\n"
        "      case \"$arg\" in WUWATERM_DB_PATH=*) candidate=${arg#WUWATERM_DB_PATH=} ;; esac\n"
        "    done\n"
        "    candidate=$FAKE_DEPLOY_ROOT/data/${candidate#/app/data/}\n"
        "    cp \"$FAKE_DEPLOY_ROOT/data/candidate-seed.db\" \"$candidate\"\n"
        "    if [ \"${FAKE_CORRUPT_CANDIDATE:-0}\" = 1 ]; then\n"
        "      python3 -c \"import sqlite3,sys; "
        "c=sqlite3.connect(sys.argv[1]); "
        "c.execute('DELETE FROM terms WHERE category = \\\'item\\\''); "
        "c.commit()\" \"$candidate\"\n"
        "    fi\n"
        "    ;;\n"
        "  *'verify-db'*)\n"
        "    candidate=''\n"
        "    for arg in \"$@\"; do\n"
        "      case \"$arg\" in WUWATERM_DB_PATH=*) candidate=${arg#WUWATERM_DB_PATH=} ;; esac\n"
        "    done\n"
        "    candidate=$FAKE_DEPLOY_ROOT/data/${candidate#/app/data/}\n"
        "    python3 \"$FAKE_DEPLOY_ROOT/scripts/verify_db.py\" \"$candidate\"\n"
        "    ;;\n"
        "  *' up -d '*wuwaterm*)\n"
        "    case \"${WUWATERM_RUNTIME_IMAGE:-}\" in\n"
        f"      *rollback-*) echo '{OLD_IMAGE}' > \"$FAKE_DEPLOY_ROOT/running-image\" ;;\n"
        f"      *) echo '{NEW_IMAGE}' > \"$FAKE_DEPLOY_ROOT/running-image\" ;;\n"
        "    esac\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker_script.chmod(0o755)

    python_script = fake_bin / "python3"
    python_script.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        ": \"${FAKE_DEPLOY_ROOT:?}\"\n"
        "echo \"python3 $*\" >> \"$FAKE_DEPLOY_ROOT/actions.log\"\n"
        f"real_python={shlex.quote(sys.executable)}\n"
        "case \"$*\" in\n"
        "  *'deployment_manifest.py durable-replace --source data/terms.db.backup.'*)\n"
        "    \"$real_python\" \"$@\"\n"
        "    if [ \"${FAKE_DB_BACKUP_DURABILITY_FAILURE:-0}\" = 1 ]; then\n"
        "      echo 'injected database backup durability failure' >&2\n"
        "      exit 98\n"
        "    fi\n"
        "    exit 0\n"
        "    ;;\n"
        "  *'deployment_manifest.py durable-replace --source data/candidates/'*)\n"
        "    if [ \"${FAKE_DB_PROMOTION_PRE_REPLACE_FAILURE:-0}\" = 1 ]; then\n"
        "      exit 94\n"
        "    fi\n"
        "    \"$real_python\" \"$@\"\n"
        "    if [ \"${FAKE_DB_PROMOTION_DURABILITY_FAILURE:-0}\" = 1 ]; then\n"
        "      exit 95\n"
        "    fi\n"
        "    exit 0\n"
        "    ;;\n"
        f"  *'deployment_manifest.py publish-pointer --path .deploy_commit --source-commit {OLD_COMMIT}'*)\n"
        "    \"$real_python\" \"$@\"\n"
        "    if [ \"${FAKE_POINTER_ROLLBACK_DURABILITY_FAILURE:-0}\" = 1 ]; then\n"
        "      echo 'injected pointer rollback durability failure' >&2\n"
        "      exit 92\n"
        "    fi\n"
        "    exit 0\n"
        "    ;;\n"
        "  *'deployment_manifest.py durable-replace --source data/terms.db.rollback.'*)\n"
        "    if [ \"${FAKE_DB_ROLLBACK_CONTENT_FAILURE:-0}\" = 1 ]; then\n"
        "      echo 'injected rollback content failure' >&2\n"
        "      exit 91\n"
        "    fi\n"
        "    \"$real_python\" \"$@\"\n"
        "    if [ \"${FAKE_DB_ROLLBACK_DURABILITY_FAILURE:-0}\" = 1 ]; then\n"
        "      echo 'injected rollback durability failure' >&2\n"
        "      exit 93\n"
        "    fi\n"
        "    exit 0\n"
        "    ;;\n"
        f"  *'deployment_manifest.py publish-pointer --path .deploy_commit --source-commit {NEW_COMMIT}'*)\n"
        "    \"$real_python\" \"$@\"\n"
        "    if [ \"${FAKE_POINTER_DURABILITY_FAILURE:-0}\" = 1 ]; then\n"
        "      exit 96\n"
        "    fi\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        "exec \"$real_python\" \"$@\"\n",
        encoding="utf-8",
    )
    python_script.chmod(0o755)
    (root / "running-image").write_text(f"{OLD_IMAGE}\n", encoding="ascii")
    # Default host state: both surfaces were up when the deployment started.
    # A test that wants a present-but-stopped surface rewrites these.
    (root / "api-running").write_text("true\n", encoding="ascii")
    (root / "bot-running").write_text("true\n", encoding="ascii")

    old_hash = hashlib.sha256(old_db.read_bytes()).hexdigest()
    new_hash = hashlib.sha256(seed_db.read_bytes()).hexdigest()
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["WUWATERM_DEPLOY_ROOT"] = str(root)
    env["FAKE_DEPLOY_ROOT"] = str(root)
    return root, env, old_hash, new_hash


@pytest.mark.parametrize(
    "failure_step",
    (
        "builder_image",
        "refresh",
        "build",
        "verify",
        "image",
        "backup",
        "state",
        "start",
        "smoke",
        "manifest",
        "pointer",
    ),
)
def test_vps_update_failure_injection_preserves_previous_binding(
    deploy_harness, failure_step
):
    root, env, old_hash, _new_hash = deploy_harness
    env["WUWATERM_FAIL_STEP"] = failure_step

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 97, result.stderr
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == old_hash
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (root / "running-image").read_text(encoding="ascii").strip() == OLD_IMAGE
    assert list((root / "data" / "deployment-backups").glob("*.deploy_commit"))


def test_vps_update_refuses_promotion_when_backup_durability_is_uncertain(
    deploy_harness,
):
    root, env, old_hash, _new_hash = deploy_harness
    env["FAKE_DB_BACKUP_DURABILITY_FAILURE"] = "1"

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 98
    assert "injected database backup durability failure" in result.stderr
    assert (
        hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest()
        == old_hash
    )
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (root / "running-image").read_text(encoding="ascii").strip() == OLD_IMAGE
    docker_lines = (root / "docker.log").read_text(encoding="utf-8").splitlines()
    assert not any("stop wuwaterm" in line for line in docker_lines)
    assert not any(
        "up -d --no-build --force-recreate wuwaterm" in line
        for line in docker_lines
    )


@pytest.mark.parametrize(
    ("failure_env", "returncode"),
    (
        ("FAKE_DB_PROMOTION_PRE_REPLACE_FAILURE", 94),
        ("FAKE_DB_PROMOTION_DURABILITY_FAILURE", 95),
        ("FAKE_POINTER_DURABILITY_FAILURE", 96),
    ),
)
def test_vps_update_rolls_back_after_replace_before_durability_confirmation(
    deploy_harness, failure_env, returncode
):
    root, env, old_hash, _new_hash = deploy_harness
    env[failure_env] = "1"

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == returncode, result.stderr
    assert (
        hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest()
        == old_hash
    )
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (root / "running-image").read_text(encoding="ascii").strip() == OLD_IMAGE


def test_vps_update_surfaces_rollback_durability_failure(deploy_harness):
    root, env, old_hash, _new_hash = deploy_harness
    env["WUWATERM_FAIL_STEP"] = "pointer"
    env["FAKE_DB_ROLLBACK_DURABILITY_FAILURE"] = "1"

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 97
    assert "injected rollback durability failure" in result.stderr
    assert "restored database durability could not be confirmed" in result.stderr
    assert "rollback completed with errors; manual recovery is required" in result.stderr
    assert (
        hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest()
        == old_hash
    )
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (root / "running-image").read_text(encoding="ascii").strip() == OLD_IMAGE
    docker_log = (root / "docker.log").read_text(encoding="utf-8")
    assert any(
        "rollback-" in line
        and "up -d --no-build --force-recreate wuwaterm" in line
        for line in docker_log.splitlines()
    )


def test_vps_update_reports_pointer_rollback_durability_failure(deploy_harness):
    root, env, old_hash, _new_hash = deploy_harness
    env["FAKE_POINTER_DURABILITY_FAILURE"] = "1"
    env["FAKE_POINTER_ROLLBACK_DURABILITY_FAILURE"] = "1"

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 96
    assert "injected pointer rollback durability failure" in result.stderr
    assert "restored pointer durability could not be confirmed" in result.stderr
    assert (
        hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest()
        == old_hash
    )
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (root / "running-image").read_text(encoding="ascii").strip() == OLD_IMAGE


def test_vps_update_stops_replacement_when_old_database_cannot_be_restored(
    deploy_harness,
):
    root, env, old_hash, new_hash = deploy_harness
    env["WUWATERM_FAIL_STEP"] = "pointer"
    env["FAKE_DB_ROLLBACK_CONTENT_FAILURE"] = "1"

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 97
    assert "injected rollback content failure" in result.stderr
    assert "old database content was not restored" in result.stderr
    assert "refusing to restart old runtime without its database" in result.stderr
    assert (
        hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest()
        == new_hash
    )
    assert new_hash != old_hash
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    docker_lines = (root / "docker.log").read_text(encoding="utf-8").splitlines()
    stop_indices = [
        index
        for index, line in enumerate(docker_lines)
        if "stop wuwaterm" in line
    ]
    new_runtime_up_index = next(
        index
        for index, line in enumerate(docker_lines)
        if "rollback-" not in line
        and "up -d --no-build --force-recreate wuwaterm" in line
    )
    assert len(stop_indices) == 2
    assert stop_indices[0] < new_runtime_up_index < stop_indices[1]
    assert not any(
        "rollback-" in line
        and "up -d --no-build --force-recreate wuwaterm" in line
        for line in docker_lines
    )


def test_vps_update_removes_new_pointer_when_no_previous_pointer_existed(
    deploy_harness,
):
    root, env, old_hash, _new_hash = deploy_harness
    (root / ".deploy_commit").unlink()
    (root / ".deployments" / f"{OLD_COMMIT}.json").unlink()
    env["FAKE_POINTER_DURABILITY_FAILURE"] = "1"

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 96, result.stderr
    assert (
        hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest()
        == old_hash
    )
    assert not (root / ".deploy_commit").exists()
    assert (root / "running-image").read_text(encoding="ascii").strip() == OLD_IMAGE


def test_vps_update_success_publishes_verified_immutable_binding(deploy_harness):
    root, env, _old_hash, new_hash = deploy_harness

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == new_hash
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{NEW_COMMIT}\n"
    assert (root / "running-image").read_text(encoding="ascii").strip() == NEW_IMAGE
    manifest_path = root / ".deployments" / f"{NEW_COMMIT}.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["source_commit"] == NEW_COMMIT
    assert payload["image"]["id"] == NEW_IMAGE
    assert payload["database"]["sha256"] == new_hash
    assert payload["database"]["provenance"]["source_commit"] == (
        get_source_profile("arikatsu").pinned_commit
    )
    assert manifest_path.stat().st_mode & 0o222 == 0


def test_vps_update_does_not_promote_candidate_rejected_by_real_verifier(
    deploy_harness,
):
    root, env, old_hash, _new_hash = deploy_harness
    env["FAKE_CORRUPT_CANDIDATE"] = "1"

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "missing or empty categories: item" in result.stderr
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == old_hash
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (root / "running-image").read_text(encoding="ascii").strip() == OLD_IMAGE


def test_compose_api_service_is_loopback_only_with_separate_state():
    """The api container adds no public surface and no writable game data."""
    text = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "  wuwaterm-api:" in text
    api_section = text.split("  wuwaterm-api:", 1)[1].split("  wuwaterm-builder:", 1)[0]
    assert "container_name: wuwaterm-api" in api_section
    assert 'command: ["api"]' in api_section
    assert "target: runtime" in api_section
    assert "network_mode: host" in api_section
    assert "restart: unless-stopped" in api_section
    # Same terminology database, read-only; its own writable state directory.
    assert "../data:/app/data:ro" in api_section
    # A SIBLING of the bot's state tree, never a child of it: the bot mounts
    # the whole of ../state read-write.
    assert "../state-api:/app/state-api" in api_section
    assert "../state/" not in api_section
    assert "WUWATERM_API_STATE_DIR: /app/state-api" in api_section
    # The bind is fixed in this file, not interpolated: with host networking an
    # environment knob would make a public exposure a one-line edit.
    assert "WUWATERM_API_BIND: 127.0.0.1" in api_section
    assert "${WUWATERM_API_BIND" not in text
    assert "ports:" not in text
    # The bot's credentials are not this process' business.
    for blanked in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_TEST_CHAT_ID",
        "OWNER_USER_ID",
        # Keys the bot's log redaction, and only the bot's.
        "WUWATERM_REDACTION_SECRET",
        # Would otherwise win over the state dir and point the credential
        # store at a path the serving container never sees.
        "WUWATERM_API_DEVICE_DB_PATH",
    ):
        assert f'{blanked}: ""' in api_section, blanked
    # It must not be able to write the chat state the bot owns.
    assert "/app/state\n" not in api_section


def test_env_example_covers_the_api_surface():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")

    for name in (
        "WUWATERM_API_PORT=8788",
        "WUWATERM_API_STATE_DIR=state-api",
        "WUWATERM_API_LLM_MAX_CONCURRENCY=2",
        "WUWATERM_API_LLM_CALLS_PER_MINUTE=30",
        "WUWATERM_API_RATE_LIMIT_PER_MINUTE=30",
        "WUWATERM_API_MAX_BODY_BYTES=32768",
        "WUWATERM_API_REQUEST_TIMEOUT_SECONDS=90",
    ):
        assert name in text, name
    # The per-process nature of the budgets must be stated where they are set.
    assert "per process" in text


def test_vps_update_manages_both_surfaces_together():
    """Both containers share the terminology database, so both move together."""
    text = (ROOT / "deploy" / "vps-update.sh").read_text(encoding="utf-8")

    # Down before the shared read-only database is replaced.
    assert "compose stop wuwaterm wuwaterm-api" in text
    # Up together, from the same validated image.
    assert "--force-recreate wuwaterm wuwaterm-api" in text
    # Smoked in-container over loopback, so nothing has to be exposed, and
    # against readiness rather than liveness: /healthz answers even when the
    # terminology database is missing or mounted at the wrong path.
    assert "compose exec -T wuwaterm-api" in text
    assert "/readyz" in text
    # compose up returns before the server binds its socket, so a single shot
    # would fail the deployment on a connection refusal that only meant
    # "not yet". The smoke waits, and says why it gave up.
    assert "api readiness never reported ok" in text
    assert "deadline = time.monotonic()" in text
    # Read back separately, and both must match the validated image id.
    assert "running_api_image_id=" in text
    assert "running api container image does not match validated image" in text
    # Rollback restores the api surface only when the host was RUNNING it.
    # Existence is not the test: a container left stopped by an earlier failed
    # upgrade, or stopped deliberately, still answers `docker inspect`, and
    # starting it here would put a surface up that was down before.
    assert "old_api_running=" in text
    assert "{{.State.Running}}" in text
    assert 'if [ "$old_api_running" = "true" ] && [ -n "$old_api_image_id" ]; then' in text
    assert "compose up -d --no-build --force-recreate wuwaterm-api" in text
    # Rollback takes both surfaces down before it touches the database, so no
    # replacement container can serve new code against a database that is being
    # rolled back underneath it. Only the restart is conditional.
    assert "replacement containers could not be stopped before rollback" in text
    rollback_body = text.split("rollback_on_failure() {", 1)[1]
    stop_at = rollback_body.index("compose stop wuwaterm wuwaterm-api")
    restore_at = rollback_body.index("durable-replace")
    assert stop_at < restore_at


def test_vps_update_does_not_start_an_api_that_was_not_running(deploy_harness):
    """A present-but-stopped API must not be started by a rollback.

    The first upgrade of a host can fail after the replacement API starts,
    which leaves the container present and stopped. On the NEXT deployment
    `docker inspect` reports an image for it, so existence alone would make a
    rollback start a surface the host was not serving.
    """
    root, env, _old_hash, _new_hash = deploy_harness
    (root / "api-running").write_text("false\n", encoding="ascii")
    env["WUWATERM_FAIL_STEP"] = "smoke"

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 97, result.stdout + result.stderr
    docker_lines = (root / "docker.log").read_text(encoding="utf-8").splitlines()
    rollback_ups = [
        line
        for line in docker_lines
        if "up -d --no-build --force-recreate" in line and "rollback-" in line
    ]
    assert rollback_ups, docker_lines
    # The bot comes back; the api does not.
    assert not any("wuwaterm-api" in line for line in rollback_ups), rollback_ups


def test_vps_update_stops_both_surfaces_before_restoring_the_database(
    deploy_harness,
):
    """A replacement container must not serve while the database rolls back.

    The deployment starts both surfaces from the new image before the smoke
    runs. If that smoke fails, the database goes back to its previous content
    underneath containers that are still running new code, which is exactly the
    mixed binding the transactional updater exists to prevent.
    """
    root, env, _old_hash, _new_hash = deploy_harness
    env["WUWATERM_FAIL_STEP"] = "smoke"

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 97, result.stdout + result.stderr
    actions = (root / "actions.log").read_text(encoding="utf-8").splitlines()
    restore_at = next(
        index
        for index, line in enumerate(actions)
        if "durable-replace --source data/terms.db.rollback." in line
    )
    stops_before_restore = [
        index
        for index, line in enumerate(actions[:restore_at])
        if "stop wuwaterm wuwaterm-api" in line
    ]
    # Two: the planned stop before promotion, and the rollback's own stop.
    assert len(stops_before_restore) == 2, actions[: restore_at + 1]
    # Nothing is started between that stop and the restored database.
    between = actions[stops_before_restore[-1] : restore_at]
    assert not [line for line in between if "up -d" in line], between


def test_vps_update_refuses_to_deploy_over_a_store_at_the_old_path(deploy_harness):
    """Only the host can see both paths.

    The library refuses to create an empty store beside an old one, but the
    API container mounts only `state-api/`, so inside the container the old
    path does not exist and that guard can never fire. The updater runs on the
    host, where both directories are visible.
    """
    root, env, old_hash, _new_hash = deploy_harness
    legacy = root / "state" / "api"
    legacy.mkdir(parents=True)
    (legacy / "devices.db").write_bytes(b"SQLite format 3\x00")
    # Even with a store already at the new path: an earlier attempt may have
    # created an empty one there, and then nobody can tell which file holds
    # the live verifiers.
    current = root / "state-api"
    current.mkdir(parents=True, exist_ok=True)
    (current / "devices.db").write_bytes(b"SQLite format 3\x00")

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "state/api/devices.db" in result.stderr
    assert "state-api/devices.db" in result.stderr
    # Refused before anything was touched.
    assert (
        hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest()
        == old_hash
    )
    assert not (root / "docker.log").exists()


def test_vps_update_restores_an_api_only_host(deploy_harness):
    """A host may run the API and no bot; the bot's absence is not a veto."""
    root, env, _old_hash, _new_hash = deploy_harness
    # No bot container: `docker inspect wuwaterm-bot` answers nothing.
    (root / "no-bot").write_text("1\n", encoding="ascii")
    env["FAKE_NO_BOT_CONTAINER"] = "1"
    env["WUWATERM_FAIL_STEP"] = "smoke"

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 97, result.stdout + result.stderr
    rollback_ups = [
        line
        for line in (root / "docker.log").read_text(encoding="utf-8").splitlines()
        if "up -d --no-build --force-recreate" in line and "rollback-" in line
    ]
    assert rollback_ups, "the api was running and must come back"
    assert all(line.endswith("wuwaterm-api") for line in rollback_ups), rollback_ups
    # Restored from the API's own rollback tag, not the bot's.
    assert all("rollback-api-" in line for line in rollback_ups), rollback_ups


def test_vps_update_keeps_a_rollback_image_per_surface():
    """Two containers can be on two different images after a hand recovery.

    Restoring both from one tag would move a surface onto an image it was
    never running.
    """
    text = (ROOT / "deploy" / "vps-update.sh").read_text(encoding="utf-8")

    assert 'rollback_image_ref="wuwaterm-runtime:rollback-$deployment_id"' in text
    assert (
        'rollback_api_image_ref="wuwaterm-runtime:rollback-api-$deployment_id"' in text
    )
    assert 'docker image tag "$old_image_id" "$rollback_image_ref"' in text
    assert 'docker image tag "$old_api_image_id" "$rollback_api_image_ref"' in text
    bot_restart = text.index('compose up -d --no-build --force-recreate wuwaterm;')
    api_restart = text.index('compose up -d --no-build --force-recreate wuwaterm-api;')
    assert 'WUWATERM_RUNTIME_IMAGE="$rollback_image_ref"' in text[:bot_restart]
    assert (
        'WUWATERM_RUNTIME_IMAGE="$rollback_api_image_ref"'
        in text[bot_restart:api_restart]
    )
    # The restored binding is verified against a surface that was actually
    # restored, never against one that stayed down on a different image.
    assert 'verify_image_id=""' in text
    assert '[ "$old_bot_running" = "true" ] && [ -n "$old_image_id" ]' in text
    assert '[ "$old_api_running" = "true" ] && [ -n "$old_api_image_id" ]' in text
    assert '--image-id "$verify_image_id"' in text


def test_vps_update_does_not_start_a_bot_that_was_not_running(deploy_harness):
    """The rule is the same for both surfaces: restore what was running.

    A combined stop that fails on one container still records the transition,
    so the rollback must not read "we stopped it" as "it was up".
    """
    root, env, _old_hash, _new_hash = deploy_harness
    (root / "bot-running").write_text("false\n", encoding="ascii")
    env["WUWATERM_FAIL_STEP"] = "smoke"

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 97, result.stdout + result.stderr
    rollback_ups = [
        line
        for line in (root / "docker.log").read_text(encoding="utf-8").splitlines()
        if "up -d --no-build --force-recreate" in line and "rollback-" in line
    ]
    assert rollback_ups, "the api was running and should have come back"
    assert all(line.endswith("wuwaterm-api") for line in rollback_ups), rollback_ups


def test_vps_update_reports_a_failed_stop_before_the_database_moves(
    deploy_harness,
):
    """A combined stop can fail after stopping one of the two surfaces.

    `compose stop wuwaterm wuwaterm-api` is one command over two containers.
    If the transition were recorded only after it returned, a partial failure
    would leave the previously running bot down while the rollback concluded
    that nothing had been touched, and said nothing about it.
    """
    root, env, old_hash, _new_hash = deploy_harness
    env["FAKE_ALL_STOPS_FAIL"] = "1"

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode != 0
    assert "could not be stopped" in result.stderr
    assert "manual recovery is required" in result.stderr
    # The database never moved, because the stop is what precedes promotion.
    assert (
        hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest()
        == old_hash
    )


def test_vps_update_leaves_the_binding_alone_when_it_cannot_stop_the_surfaces(
    deploy_harness,
):
    """A stop that fails must abort the restoration, not proceed without it.

    Rolling the database back while a replacement container may still be
    serving would CREATE the mixed binding the rollback exists to prevent. The
    promoted database and pointer are at least consistent with the code that is
    running, so they stay, and the operator is told to intervene.
    """
    root, env, _old_hash, new_hash = deploy_harness
    env["WUWATERM_FAIL_STEP"] = "smoke"
    env["FAKE_ROLLBACK_STOP_FAILURE"] = "1"

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 97, result.stdout + result.stderr
    assert "could not be stopped" in result.stderr
    assert "manual recovery is required" in result.stderr
    # The promoted database is still the promoted one: nothing was reverted
    # underneath a container that may still be reading it.
    assert (
        hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest()
        == new_hash
    )
    # The pointer is published after the smoke, so it never moved; what
    # matters is that the rollback did not try to move it back either.
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    actions = (root / "actions.log").read_text(encoding="utf-8").splitlines()
    assert not [
        line for line in actions if "durable-replace --source data/terms.db.rollback." in line
    ], actions
    # And nothing was restarted either.
    rollback_ups = [
        line
        for line in (root / "docker.log").read_text(encoding="utf-8").splitlines()
        if "up -d" in line and "rollback-" in line
    ]
    assert not rollback_ups, rollback_ups


def test_vps_update_stops_and_restarts_both_surfaces(deploy_harness):
    root, env, _old_hash, _new_hash = deploy_harness

    result = subprocess.run(
        ["sh", "deploy/vps-update.sh"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    docker_log = (root / "docker.log").read_text(encoding="utf-8")
    stop_lines = [
        line for line in docker_log.splitlines() if "stop wuwaterm" in line
    ]
    assert stop_lines
    assert all("wuwaterm-api" in line for line in stop_lines), stop_lines
    up_lines = [line for line in docker_log.splitlines() if "up -d" in line]
    assert up_lines
    assert all("wuwaterm-api" in line for line in up_lines), up_lines
    assert "exec -T wuwaterm-api" in docker_log
    assert "deployment api container image id:" in result.stdout


def test_api_state_directory_is_not_inside_the_bot_state_tree():
    """The bot mounts all of state/ read-write; credentials must be outside."""
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")

    bot_section = compose.split("  wuwaterm:", 1)[1].split("  wuwaterm-api:", 1)[0]
    assert "../state:/app/state" in bot_section
    api_section = compose.split("  wuwaterm-api:", 1)[1].split("  wuwaterm-builder:", 1)[0]
    assert "../state-api:/app/state-api" in api_section
    assert ":/app/state" + chr(10) not in api_section

    assert "WUWATERM_API_STATE_DIR=state-api" in env_example
    # Runtime state, generated on the host, must never be committable.
    assert "state-api/" in ignored


def test_docker_context_excludes_the_api_state_and_sqlite_sidecars():
    """Sidecars carry credential rows and do not match the *.db pattern."""
    lines = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    }

    for pattern in ("state", "state-api", "*.db", "*.db-wal", "*.db-shm"):
        assert pattern in lines, pattern


def test_documented_readback_uses_the_same_endpoint_the_updater_gates_on():
    text = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    assert "/readyz" in text
    # Liveness answers even with no terminology database mounted, so it must
    # not be what an operator is told to read back.
    assert "/healthz', timeout" not in text


def test_the_guide_describes_the_credential_boundary_it_actually_has():
    """The boundary is specific, not total, and saying otherwise is worse
    than saying nothing: an operator would believe the model credential is
    confined to the bot when both surfaces receive it."""
    text = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "Runtime secrets are injected only into `wuwaterm`" not in text
    assert "WUWATERM_OPENAI_API_KEY" in text
    # Everything the guide claims is blanked must actually be blanked.
    api_section = compose.split("  wuwaterm-api:", 1)[1].split("  wuwaterm-builder:", 1)[0]
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_TEST_CHAT_ID",
        "OWNER_USER_ID",
        "WUWATERM_REDACTION_SECRET",
    ):
        assert f"{name}: \"\"" in api_section, name
        assert name in text, name
    # ...and the model settings it says are shared must NOT be blanked.
    assert "WUWATERM_OPENAI_API_KEY:" not in api_section


def test_documented_api_commands_use_the_configured_port():
    """The port is an option, so no operator command may assume the default.

    A readback pinned to 8788 reports a connection failure after a perfectly
    successful deployment on any host that set WUWATERM_API_PORT to something
    else.
    """
    text = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    updater = (ROOT / "deploy" / "vps-update.sh").read_text(encoding="utf-8")

    # No documented URL may carry a literal port; the updater does not.
    assert "http://127.0.0.1:8788" not in text
    assert "http://127.0.0.1:8788" not in updater
    # The readback reads the port the serving container was actually given,
    # exactly as the updater's readiness smoke does.
    assert "os.environ.get('WUWATERM_API_PORT', '8788')" in text
    assert "os.environ.get('WUWATERM_API_PORT', '8788')" in updater
    # The serving port is discovered from the running container, not assumed.
    assert "printenv WUWATERM_API_PORT" in text


def _fenced_lines(block: str) -> list[str]:
    """Only the lines inside fenced code blocks.

    A gate that reads prose as well would assert against whatever a future
    sentence happens to quote, and pass today only because the one prose
    mention of the command is punctuated without a trailing space.
    """
    lines: list[str] = []
    inside = False
    for line in block.splitlines():
        if line.startswith("```"):
            inside = not inside
            continue
        if inside:
            lines.append(line)
    return lines


def _resolution_lines(block: str) -> list[str]:
    """The image-pin resolution, as a list of lines.

    Both blocks must resolve the pin the same way; the credentials section
    tells the operator so in as many words. Comparing the lines makes that a
    check rather than a coincidence of two tests asserting the same literals.
    """
    wanted = (
        'pinned=""',
        "image=",
        "running_id=",
        "tag_id=",
        '[ -n "$tag_id" ]',
        # The refusal itself: the credentials section promises "the same
        # resolution, the same refusals", and a message that drifts in one
        # block and not the other would keep both gates green otherwise.
        "echo ",
    )
    return [
        line.strip()
        for line in block.splitlines()
        if line.strip().startswith(wanted)
    ]


def test_documented_one_shot_commands_run_on_the_deployed_image():
    """A one-shot `compose run` resolves the image from the Compose default,
    not from the deployment.

    The serving containers are created from an immutable
    `wuwaterm-runtime:<commit>`; the Compose default is the mutable
    `wuwaterm-runtime:local`. On a host that still carries an old `:local`
    tag, an unpinned `run` therefore executes a DIFFERENT image than the one
    deployed, and an image older than a subcommand refuses that subcommand -
    which reads as a broken runbook rather than as the wrong image. Observed
    exactly that way on this project's own host while issuing the owner's
    credential.
    """
    text = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    # Split on the heading MARKER, so a sub-heading added inside the section
    # cannot silently shorten what this gate reads.
    credentials = text.split("### Device Credentials", 1)[1].split("\n### ", 1)[0]
    # Line wrapping is editorial; the phrases below are the property.
    flat = " ".join(credentials.split())

    # The reference comes from the running container, and it comes BEFORE the
    # commands it has to cover.
    pin = (
        "image=\"$(docker inspect --format '{{.Config.Image}}' "
        'wuwaterm-api 2>/dev/null)"'
    )
    assert pin in credentials
    for subcommand in ("device issue", "device list", "device revoke"):
        assert credentials.index(pin) < credentials.index(subcommand), subcommand

    # `.Config.Image` is the reference the container was CREATED with, and a
    # tag is mutable: the updater retags `wuwaterm-runtime:<commit>` before its
    # rollback trap exists, so an aborted same-commit rerun can leave that tag
    # on a fresh unvalidated image while this container still runs the old one.
    # The identity of record is the image id, which is what the traceability
    # readback compares - so the reference is only used once it has been shown
    # to still name the running id.
    assert (
        "running_id=\"$(docker inspect --format '{{.Image}}' wuwaterm-api 2>/dev/null)\""
        in credentials
    )
    # `${image:?}` sits inside a command substitution, so on a host with no
    # container it kills only that subshell: `tag_id` and `running_id` are then
    # both empty and a bare equality would pass VACUOUSLY, on exactly the host
    # the check exists for. The emptiness test is part of the check.
    assert (
        '[ -n "$tag_id" ] && [ "$tag_id" = "$running_id" ] && pinned="$image"'
        in credentials
    )
    # ...and the block clears `pinned` first, so a shell that pinned
    # successfully earlier cannot carry that value into a rerun where the
    # resolution failed and have the refusals below pass on a stale reference.
    assert credentials.index('pinned=""') < credentials.index(pin)

    # Every command carries the pin and REFUSES rather than warns. A bare
    # export would report its own success and leave an EMPTY value behind on a
    # failed lookup, and Compose's `:-` default treats empty exactly like
    # unset - straight back to the mutable tag this section exists to avoid,
    # by following this section.
    assert "export WUWATERM_RUNTIME_IMAGE" not in credentials
    # EVERY compose run in the section carries it - counting the ones that do
    # would leave a fifth, unpinned command added later perfectly green.
    runs = [
        line
        for line in _fenced_lines(credentials)
        if "docker compose" in line and " run " in line
    ]
    assert runs
    for line in runs:
        assert line.startswith('WUWATERM_RUNTIME_IMAGE="${pinned:?'), line
    assert "The pin travels on the command, and refuses rather than warns." in flat

    # The property is stated next to the command, so a reader can tell whether
    # a variant of it is still correct instead of matching the text.
    assert (
        "the one-shot container must be the deployed image, not whatever the "
        "default tag resolves to" in flat
    )


def test_the_documented_recreate_pins_the_same_way_the_credential_commands_do():
    """The credentials section names the recreate block as its model, so the
    two must not diverge - and the recreate block is the one with the worse
    landing.

    `--no-build` prevents a BUILD, not a wrong image: an unguarded reference
    that resolves to nothing sends `--force-recreate` at the mutable
    `wuwaterm-runtime:local` tag and puts the SERVING api container on a stale
    image, where the credentials failure was only a refused subcommand.
    """
    text = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    recreate = text.split("**Recreate it on the image it is already on.**", 1)[1]
    recreate = recreate.split("\n### ", 1)[0]

    assert (
        "image=\"$(docker inspect --format '{{.Config.Image}}' wuwaterm-api 2>/dev/null)\""
        in recreate
    )
    assert (
        "running_id=\"$(docker inspect --format '{{.Image}}' wuwaterm-api 2>/dev/null)\""
        in recreate
    )
    assert 'WUWATERM_RUNTIME_IMAGE="${pinned:?nothing pinned}"' in recreate
    assert "export WUWATERM_RUNTIME_IMAGE" not in recreate
    # The recreate must still forbid building, which the credential commands
    # do not need: this one would replace a running container.
    assert "--no-build" in recreate
    # ...and the port readback is chained to it, so a refused pin cannot leave
    # the operator reading the port of a container that was never recreated.
    assert "--force-recreate wuwaterm-api &&" in recreate

    # The two resolutions are compared directly rather than by both tests
    # happening to assert the same literals.
    credentials = text.split("### Device Credentials", 1)[1].split("\n### ", 1)[0]
    assert _resolution_lines(recreate) == _resolution_lines(credentials)
    assert len(_resolution_lines(recreate)) == 6


def test_the_log_guide_says_a_client_cancel_is_not_a_client_gone_record():
    """`499` is the record for a caller that went away, and a reader can
    reasonably expect a Cancel button to produce one. It does not.

    Cancelling in the desktop client stops that client from waiting; the
    request was already sent, nothing tells the service, and the request runs
    to completion and is recorded with its ordinary status - model cost
    included. An operator told otherwise would look for `499` records that
    never arrive and would under-count what the service actually did.
    """
    flat = " ".join(
        (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8").split()
    )

    assert "A desktop client's Cancel button does not normally produce one." in flat
    # The two consequences, not just the mechanism.
    assert "whose cost is spent" in flat
    assert "has no id the user can quote" in flat
    # ...and the case that really does stop the work, so the correction does
    # not overshoot into a second false claim.
    assert "sends nothing it can act on, so that case leaves no record here" in flat
    # ...and the cost claim is scoped to requests that reach the model.
    assert "A dictionary hit returns before the model stage" in flat


def test_the_guide_does_not_teach_host_administration_as_the_client_path():
    """The deployment guide is a runbook, and a runbook that documents a
    host-administration channel as the way a desktop client reaches the
    service teaches exactly the design this project does not have.

    The client reaches a configured secure endpoint with device
    authentication on every call. Host shell access is the operator's own
    channel: it appears here for deployment and credential commands, and
    never as the application's path.
    """
    text = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    # No local port-forwarding recipe, in any of the forms that would put one
    # back into the guide.
    for recipe in ("ssh -N -L", "ssh -L", "-N -L", "LocalForward"):
        assert recipe not in text, recipe
    # The replacement wording is present, not merely the removal.
    assert "configured secure endpoint" in text
    assert "Every `/v1` operation is authenticated at the application layer" in text
    # The transport has since been selected, so the guide names it instead of
    # saying it does not exist: a path route on a site the host already
    # serves, which the API is reached THROUGH and never bound to. The two
    # properties that make that safe are pinned, not the prose around them.
    assert "path route on an HTTPS site the host already serves" in text
    assert "Rollback is deleting that block and reloading again" in text
    # ...and the readback that would prove it must not be one taken on the
    # host, which proves only that the process is up.
    assert "Readback belongs on the client machine" in text
    # ...and the claim stays true about the routes that really are open.
    for unauthenticated in ("GET /healthz", "GET /readyz", "GET /openapi.json"):
        assert unauthenticated in text, unauthenticated
    # The Compose comment describes the same surface to whoever reads the file
    # instead of the guide, so it may not promise more than the guide does.
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "every /v1" in compose
    assert "GET /healthz, GET /readyz, GET /openapi.json" in compose
