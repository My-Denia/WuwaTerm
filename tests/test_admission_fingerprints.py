"""Versioned recovery fingerprints preserve historical journal identities."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

from scripts import full_update, package_recovery_source as capsule, runtime_update


ROOT = Path(__file__).resolve().parents[1]
HISTORY = {
    "34bd8b4a465624b4521bb093dc03ad999a869ac9": (
        "6b172316ab49cae68383a3942fd7d9bc29037277b0d001d739ce74dba6657a69", None),
    "d6b7e0ff3a59f5e4e8fe1957fa688e917a27325a": (
        "1e024cd0630c003d38fe40fa10d9ed11b1aab370d14ea4d0d75d930645e400b7",
        "e3bb030a03eddd369e687ad2c8ee69a539c10ed9afa66087d31042bb8d78c8fd"),
    "a9491997369d68c9934f17bbfc4a343bd1527307": (
        "5bcdf0019aeca57166164f05c7b0654d110cddc4afc2ee251f280d214dc9754b",
        "70cc11d9ccb7ba771572ceb7b88fdd6ac68bd9f94a2e13da13e429a81ae636a9"),
    "f8a9008d7adf60006ca210c750aa97f5de386a26": (
        "5bcdf0019aeca57166164f05c7b0654d110cddc4afc2ee251f280d214dc9754b",
        "70cc11d9ccb7ba771572ceb7b88fdd6ac68bd9f94a2e13da13e429a81ae636a9"),
}
NAMES = (
    "deploy/vps-update.sh", "deploy/docker-compose.yml", "scripts/deployment_manifest.py",
    "scripts/runtime_update.py", "scripts/full_update.py", "scripts/deployment_admission.py",
)
MARKER = b"# WUWATERM-LEGACY-ADMISSION-FINGERPRINT: v2"


def inspect(tmp_path: Path, contents: dict[str, bytes]) -> dict[str, str | None]:
    archive = tmp_path / "source.tar"
    files = {}
    with tarfile.open(archive, "w") as stream:
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.size = len(data)
            stream.addfile(info, io.BytesIO(data))
            files[name] = {"mode": "100644", "size": len(data),
                           "sha256": hashlib.sha256(data).hexdigest()}
    return capsule.inspect_archive(archive, files, require_tools=True)


def historical_files(commit: str) -> dict[str, bytes]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT_", "PYTHON"))}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_NO_REPLACE_OBJECTS="1")
    if subprocess.run(["git", "cat-file", "-e", commit], cwd=ROOT, env=env,
                      capture_output=True, check=False).returncode:
        pytest.skip("historical source is not present in this checkout")
    contents = {}
    for name in NAMES:
        result = subprocess.run(["git", "show", f"{commit}:{name}"], cwd=ROOT,
                                env=env, capture_output=True, check=False)
        if result.returncode == 0:
            contents[name] = result.stdout
    return contents


@pytest.mark.parametrize("commit", HISTORY)
def test_original_archives_keep_original_fingerprints(tmp_path: Path, commit: str) -> None:
    hashes = inspect(tmp_path, historical_files(commit))
    assert (hashes["runtime"], hashes["full"]) == HISTORY[commit]


def current_files() -> dict[str, bytes]:
    return {name: (ROOT / name).read_bytes() for name in NAMES}


def test_current_capsule_matches_both_actual_updaters_and_binds_admission(tmp_path: Path) -> None:
    contents = current_files()
    hashes = inspect(tmp_path, contents)
    assert hashes == {"runtime": runtime_update._tool_hash(), "full": full_update._tool_hash()}
    contents["scripts/deployment_admission.py"] += b"\n# changed admission\n"
    changed = inspect(tmp_path, contents)
    assert changed["runtime"] != hashes["runtime"]
    assert changed["full"] != hashes["full"]


@pytest.mark.parametrize("name", ["deploy/vps-update.sh", "scripts/runtime_update.py",
                                  "scripts/full_update.py"])
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown"])
def test_bad_or_unpaired_source_version_is_rejected(tmp_path: Path, name: str, mutation: str) -> None:
    contents = current_files()
    if mutation == "missing":
        contents[name] = contents[name].replace(MARKER + b"\n", b"")
    elif mutation == "duplicate":
        contents[name] = contents[name].replace(MARKER, MARKER + b"\n" + MARKER)
    else:
        contents[name] = contents[name].replace(MARKER, MARKER[:-2] + b"v3")
    with pytest.raises(capsule.CapsuleError):
        inspect(tmp_path, contents)


def test_marked_source_without_admission_bytes_is_rejected(tmp_path: Path) -> None:
    contents = current_files()
    del contents["scripts/deployment_admission.py"]
    with pytest.raises(capsule.CapsuleError):
        inspect(tmp_path, contents)
