"""Tests for scripts/check_release_manifest.py."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

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


def test_real_repository_release_manifest_converges() -> None:
    assert gate.check_manifest(ROOT) == []
    assert gate.main(["--root", str(ROOT)]) == 0


@pytest.mark.parametrize(
    ("rel_path", "mutator", "expected_fragment"),
    [
        (
            "pyproject.toml",
            lambda text: text.replace('version = "0.16.0"', 'version = "0.16.1"'),
            "core version drift",
        ),
        (
            "docs/api/openapi.json",
            lambda text: text.replace('"version": "0.16.0"', '"version": "0.15.9"'),
            "openapi version drift",
        ),
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
        (
            "docs/products.md",
            lambda text: text.replace("| Core / API | `8565b48`（源码 `0.16.0`） |", "| Core / API | `122870b`（源码 `0.15.0`） |"),
            "docs/products.md row 'Core / API'",
        ),
        (
            "docs/products.md",
            lambda text: text.replace("Web v0.7.0 发布页", "Web v0.6.0 发布页"),
            "docs/products.md top section does not link Web v0.7.0",
        ),
    ],
)
def test_release_manifest_gate_rejects_drift(
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
