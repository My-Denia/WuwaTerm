"""Tests for scripts/check_release_manifest.py and release manifest integrity."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tomllib

import pytest

from scripts import check_release_manifest as gate

ROOT = Path(__file__).resolve().parents[1]


def _copy_manifest_inputs(tmp_path: Path) -> Path:
    for rel in (
        "pyproject.toml",
        "contracts/products.json",
        "contracts/web-ref.txt",
        "contracts/windows-ref.txt",
        "contracts/ci-source-epochs.json",
        "docs/api/openapi.json",
        "docs/products.md",
    ):
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    return tmp_path


def _recorded_core_version(root: Path) -> str:
    manifest = json.loads(
        (root / "contracts" / "products.json").read_text(encoding="utf-8")
    )
    core = next(item for item in manifest["products"] if item["id"] == "core")
    return core["release"]["version"]


def _bump_patch(version: str, delta: int) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + delta}"


def _older_version(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    if patch > 0:
        return f"{major}.{minor}.{patch - 1}"
    if minor > 0:
        return f"{major}.{minor - 1}.0"
    if major > 0:
        return f"{major - 1}.0.0"
    raise ValueError(f"cannot construct older semver below {version!r}")


def test_real_repository_release_manifest_converges() -> None:
    assert gate.check_manifest(ROOT) == []
    assert gate.main(["--root", str(ROOT)]) == 0


def test_core_version_bump_ahead_of_snapshot_is_allowed_when_openapi_matches(
    tmp_path: Path,
) -> None:
    """Release prep bumps pyproject.toml + openapi.json before products.json is tagged."""
    root = _copy_manifest_inputs(tmp_path)
    recorded = _recorded_core_version(root)
    next_version = _bump_patch(recorded, 1)

    pyproject_path = root / "pyproject.toml"
    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    current_pkg_ver = tomllib.loads(pyproject_text)["project"]["version"]
    pyproject_path.write_text(
        pyproject_text.replace(
            f'version = "{current_pkg_ver}"', f'version = "{next_version}"', 1
        ),
        encoding="utf-8",
    )

    openapi_path = root / "docs" / "api" / "openapi.json"
    openapi = json.loads(openapi_path.read_text(encoding="utf-8"))
    openapi["info"]["version"] = next_version
    openapi_path.write_text(
        json.dumps(openapi, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    assert gate.check_manifest(root) == []
    assert gate.main(["--root", str(root)]) == 0


def test_core_version_regression_below_snapshot_is_rejected(tmp_path: Path) -> None:
    root = _copy_manifest_inputs(tmp_path)
    recorded = _recorded_core_version(root)
    older_version = _older_version(recorded)

    pyproject_path = root / "pyproject.toml"
    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    current_pkg_ver = tomllib.loads(pyproject_text)["project"]["version"]
    pyproject_path.write_text(
        pyproject_text.replace(
            f'version = "{current_pkg_ver}"', f'version = "{older_version}"', 1
        ),
        encoding="utf-8",
    )

    openapi_path = root / "docs" / "api" / "openapi.json"
    openapi = json.loads(openapi_path.read_text(encoding="utf-8"))
    openapi["info"]["version"] = older_version
    openapi_path.write_text(
        json.dumps(openapi, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    errors = gate.check_manifest(root)
    assert any("core version regression" in err for err in errors), errors
    assert gate.main(["--root", str(root)]) == 1


def test_pyproject_and_openapi_mismatch_is_rejected(tmp_path: Path) -> None:
    root = _copy_manifest_inputs(tmp_path)
    recorded = _recorded_core_version(root)
    next_version = _bump_patch(recorded, 1)

    pyproject_path = root / "pyproject.toml"
    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    current_pkg_ver = tomllib.loads(pyproject_text)["project"]["version"]
    pyproject_path.write_text(
        pyproject_text.replace(
            f'version = "{current_pkg_ver}"', f'version = "{next_version}"', 1
        ),
        encoding="utf-8",
    )

    errors = gate.check_manifest(root)
    assert any("core/openapi version mismatch" in err for err in errors), errors
    assert gate.main(["--root", str(root)]) == 1


def test_release_manifest_gate_rejects_mismatched_markdown_link_target(
    tmp_path: Path,
) -> None:
    """Link label saying v0.7.0 while URL still points to v0.6.0 must fail."""
    root = _copy_manifest_inputs(tmp_path)
    manifest = json.loads(
        (root / "contracts" / "products.json").read_text(encoding="utf-8")
    )
    web = next(item for item in manifest["products"] if item["id"] == "web")
    web_tag = web["release"]["tag"]
    prev_tag = web["previous_release"]["tag"]
    current_url = web["release"]["url"]
    stale_url = web["previous_release"]["url"]

    products_md = root / "docs" / "products.md"
    text = products_md.read_text(encoding="utf-8")
    assert f"[{web_tag}]({current_url})" in text
    assert f"[Web {web_tag} 发布页]({current_url})" in text

    # Keep the visible label as current tag, point only the URL at the previous tag.
    tampered = text.replace(
        f"[{web_tag}]({current_url})", f"[{web_tag}]({stale_url})"
    ).replace(
        f"[Web {web_tag} 发布页]({current_url})",
        f"[Web {web_tag} 发布页]({stale_url})",
    )
    assert prev_tag in stale_url
    products_md.write_text(tampered, encoding="utf-8")

    errors = gate.check_manifest(root)
    assert any("top section missing link" in err for err in errors), errors
    assert any("row 'Web' release" in err for err in errors), errors
    assert gate.main(["--root", str(root)]) == 1


@pytest.mark.parametrize(
    ("rel_path", "mutator", "expected_fragment"),
    [
        (
            "contracts/web-ref.txt",
            lambda _text: "0" * 40 + "\n",
            "web pin drift",
        ),
        (
            "contracts/windows-ref.txt",
            lambda _text: "1" * 40 + "\n",
            "windows pin drift",
        ),
    ],
)
def test_release_manifest_gate_rejects_pin_drift(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    rel_path: str,
    mutator,
    expected_fragment: str,
) -> None:
    root = _copy_manifest_inputs(tmp_path)
    target = root / rel_path
    target.write_text(mutator(target.read_text(encoding="utf-8")), encoding="utf-8")

    errors = gate.check_manifest(root)
    assert any(expected_fragment in err for err in errors), errors
    assert gate.main(["--root", str(root)]) == 1
    captured = capsys.readouterr()
    assert expected_fragment in captured.err


def test_release_manifest_gate_rejects_bad_ci_pins_and_epochs(tmp_path: Path) -> None:
    root = _copy_manifest_inputs(tmp_path)
    epochs_path = root / "contracts" / "ci-source-epochs.json"
    epochs = json.loads(epochs_path.read_text(encoding="utf-8"))
    epochs["consumers"]["split_web_revision"] = "a" * 40
    epochs_path.write_text(json.dumps(epochs, indent=2) + "\n", encoding="utf-8")

    errors = gate.check_manifest(root)
    assert any("split_web_revision" in err for err in errors), errors


def test_products_json_preserves_full_previous_and_archived_release_digests() -> None:
    manifest = json.loads(
        (ROOT / "contracts" / "products.json").read_text(encoding="utf-8")
    )
    by_id = {p["id"]: p for p in manifest["products"]}

    # Every moved previous_release keeps its full asset digests and detailed note,
    # and the displaced older releases are archived intact with all 29 asset digests.
    expected_previous = {
        "core": ("v0.15.0", 4),
        "telegram": ("v0.6.1", 7),
        "windows": ("v0.9.0", 3),
        "web": ("v0.7.0", 6),
    }
    expected_archived = {
        "core": [("v0.14.1", 4)],
        "telegram": [("v0.6.0", 7)],
        "windows": [("v0.8.0", 3), ("v0.7.1", 3)],
        "web": [("v0.6.0", 6), ("v0.5.1", 6)],
    }

    for pid, (tag, asset_count) in expected_previous.items():
        prev = by_id[pid]["previous_release"]
        assert prev["tag"] == tag
        assert len(prev["asset_sha256"]) == asset_count
        assert all(
            isinstance(v, str) and len(v) == 64 for v in prev["asset_sha256"].values()
        )
        assert len(prev["note"]) > 200

    total_archived_digests = 0
    for pid, entries in expected_archived.items():
        archived = by_id[pid]["archived_releases"]
        assert isinstance(archived, list) and len(archived) >= len(entries)
        for tag, asset_count in entries:
            entry = next(item for item in archived if item["tag"] == tag)
            assert len(entry["asset_sha256"]) == asset_count
            assert all(
                isinstance(v, str) and len(v) == 64
                for v in entry["asset_sha256"].values()
            )
            total_archived_digests += len(entry["asset_sha256"])

    assert total_archived_digests == 29
