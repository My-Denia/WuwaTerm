#!/usr/bin/env python3
"""Verify release manifest convergence across contracts, package metadata, and docs.

Checks that ``contracts/products.json`` matches ``pyproject.toml``,
``docs/api/openapi.json``, ``contracts/web-ref.txt``,
``contracts/windows-ref.txt``, ``contracts/ci-source-epochs.json``, and the
current-status sections (including Markdown link target URLs) of
``docs/products.md``.

Allows ``pyproject.toml`` and ``docs/api/openapi.json`` to advance ahead of
``contracts/products.json`` during release preparation (as long as both match
each other and are >= the recorded Core release version).

Usage:
    python scripts/check_release_manifest.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PRODUCT_IDS = ("core", "telegram", "windows", "web")
TABLE_ROW_NAMES = {
    "core": "Core / API",
    "telegram": "Telegram",
    "windows": "Windows",
    "web": "Web",
}
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_semver(version: str) -> tuple[int, int, int] | None:
    if not VERSION_RE.fullmatch(version):
        return None
    major, minor, patch = version.split(".")
    return (int(major), int(minor), int(patch))


def _parse_products_table(text: str) -> dict[str, tuple[str, str, str]]:
    """Return ``{product_label: (snapshot_col, release_col, adoption_col)}``."""
    section = text.split("## 版本与兼容", 1)[-1]
    rows: dict[str, tuple[str, str, str]] = {}
    for line in section.splitlines():
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0] in {"产品", "---"} or set(cells[0]) == {"-"}:
            continue
        rows[cells[0]] = (cells[1], cells[2], cells[3])
    return rows


def check_manifest(root: Path) -> list[str]:
    """Return a list of drift or schema errors; empty list means consistent."""
    errors: list[str] = []

    manifest_path = root / "contracts" / "products.json"
    if not manifest_path.is_file():
        return [f"missing manifest: {manifest_path}"]
    manifest = _load_json(manifest_path)
    if manifest.get("schema") != 1:
        errors.append(
            f"contracts/products.json: expected schema 1, got {manifest.get('schema')!r}"
        )

    raw_products = manifest.get("products")
    if not isinstance(raw_products, list):
        return [*errors, "contracts/products.json: products must be a list"]

    by_id: dict[str, dict[str, Any]] = {}
    for item in raw_products:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            errors.append("contracts/products.json: product entry missing string id")
            continue
        pid = item["id"]
        if pid in by_id:
            errors.append(f"contracts/products.json: duplicate product id {pid!r}")
        by_id[pid] = item

    for pid in EXPECTED_PRODUCT_IDS:
        if pid not in by_id:
            errors.append(f"contracts/products.json: missing product {pid!r}")

    if errors:
        return errors

    for pid in EXPECTED_PRODUCT_IDS:
        product = by_id[pid]
        release = product.get("release")
        main = product.get("main")
        if not isinstance(release, dict) or not isinstance(main, dict):
            errors.append(f"{pid}: missing release or main object")
            continue
        version = release.get("version")
        tag = release.get("tag")
        source_commit = release.get("source_commit")
        main_commit = main.get("commit")
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            errors.append(f"{pid}: invalid release.version {version!r}")
        if tag != f"v{version}":
            errors.append(
                f"{pid}: release.tag {tag!r} does not match release.version {version!r}"
            )
        if not isinstance(source_commit, str) or not SHA40.fullmatch(source_commit):
            errors.append(f"{pid}: invalid release.source_commit {source_commit!r}")
        if not isinstance(main_commit, str) or not SHA40.fullmatch(main_commit):
            errors.append(f"{pid}: invalid main.commit {main_commit!r}")
        url = release.get("url", "")
        if not isinstance(url, str) or not url.endswith(f"/releases/tag/{tag}"):
            errors.append(
                f"{pid}: release.url {url!r} does not end with /releases/tag/{tag}"
            )
        if "source_version" in product and product["source_version"] != version:
            errors.append(
                f"{pid}: source_version {product['source_version']!r} != release.version {version!r}"
            )

    if errors:
        return errors

    core = by_id["core"]
    core_version = core["release"]["version"]
    core_semver = _parse_semver(core_version)
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    pyproject_version = pyproject["project"]["version"]
    pyproject_semver = (
        _parse_semver(pyproject_version)
        if isinstance(pyproject_version, str)
        else None
    )

    openapi = _load_json(root / "docs" / "api" / "openapi.json")
    openapi_version = openapi.get("info", {}).get("version")
    openapi_semver = (
        _parse_semver(openapi_version) if isinstance(openapi_version, str) else None
    )

    if pyproject_semver is None:
        errors.append(f"invalid pyproject.toml version {pyproject_version!r}")
    if openapi_semver is None:
        errors.append(f"invalid docs/api/openapi.json info.version {openapi_version!r}")
    if pyproject_version != openapi_version:
        errors.append(
            f"core/openapi version mismatch: pyproject.toml={pyproject_version!r} != docs/api/openapi.json={openapi_version!r}"
        )
    if core_semver is not None and pyproject_semver is not None:
        if pyproject_semver < core_semver:
            errors.append(
                f"core version regression: pyproject.toml={pyproject_version!r} < contracts/products.json={core_version!r}"
            )
        elif pyproject_semver == core_semver:
            if core.get("source_version") != core_version:
                errors.append(
                    f"core source_version {core.get('source_version')!r} != {core_version!r}"
                )
            if core["release"].get("tag") != f"v{core_version}":
                errors.append(
                    f"core release.tag {core['release'].get('tag')!r} != v{core_version!r}"
                )

    web_commit = by_id["web"]["release"]["source_commit"]
    windows_commit = by_id["windows"]["release"]["source_commit"]
    web_ref = (root / "contracts" / "web-ref.txt").read_text(encoding="utf-8").strip()
    windows_ref = (
        (root / "contracts" / "windows-ref.txt").read_text(encoding="utf-8").strip()
    )
    if web_ref != web_commit:
        errors.append(
            f"web pin drift: contracts/web-ref.txt={web_ref!r} != products.web.release.source_commit={web_commit!r}"
        )
    if windows_ref != windows_commit:
        errors.append(
            f"windows pin drift: contracts/windows-ref.txt={windows_ref!r} != products.windows.release.source_commit={windows_commit!r}"
        )

    ci_pins = manifest.get("ci_pins", {})
    if ci_pins.get("core_tests_web") != web_ref:
        errors.append(
            f"ci_pins.core_tests_web={ci_pins.get('core_tests_web')!r} != contracts/web-ref.txt={web_ref!r}"
        )
    if ci_pins.get("core_tests_windows") != windows_ref:
        errors.append(
            f"ci_pins.core_tests_windows={ci_pins.get('core_tests_windows')!r} != contracts/windows-ref.txt={windows_ref!r}"
        )

    epochs = _load_json(root / "contracts" / "ci-source-epochs.json")
    consumers = epochs.get("consumers", {})
    if consumers.get("split_web_revision") != web_ref:
        errors.append(
            f"ci-source-epochs split_web_revision={consumers.get('split_web_revision')!r} != contracts/web-ref.txt={web_ref!r}"
        )
    if consumers.get("split_windows_revision") != windows_ref:
        errors.append(
            f"ci-source-epochs split_windows_revision={consumers.get('split_windows_revision')!r} != contracts/windows-ref.txt={windows_ref!r}"
        )

    products_doc = (root / "docs" / "products.md").read_text(encoding="utf-8")
    top_section = products_doc.split("## 版本与兼容", 1)[0]
    expected_tg_ver = by_id["telegram"]["release"]["version"]
    expected_win_tag = by_id["windows"]["release"]["tag"]
    expected_win_url = by_id["windows"]["release"]["url"]
    expected_web_tag = by_id["web"]["release"]["tag"]
    expected_web_url = by_id["web"]["release"]["url"]

    if f"当前 `{expected_tg_ver}` 已正式发布" not in top_section:
        errors.append(
            f"docs/products.md top section does not recommend Telegram {expected_tg_ver}"
        )
    if f"[Windows {expected_win_tag} 发布页]({expected_win_url})" not in top_section:
        errors.append(
            f"docs/products.md top section missing link [Windows {expected_win_tag} 发布页]({expected_win_url})"
        )
    if f"[Web {expected_web_tag} 发布页]({expected_web_url})" not in top_section:
        errors.append(
            f"docs/products.md top section missing link [Web {expected_web_tag} 发布页]({expected_web_url})"
        )

    table_rows = _parse_products_table(products_doc)
    for pid, row_name in TABLE_ROW_NAMES.items():
        if row_name not in table_rows:
            errors.append(f"docs/products.md table missing row {row_name!r}")
            continue
        snapshot_col, release_col, _ = table_rows[row_name]
        prod = by_id[pid]
        ver = prod["release"]["version"]
        tag = prod["release"]["tag"]
        expected_url = prod["release"]["url"]
        short_sha = prod["main"]["commit"][:7]
        if ver not in snapshot_col or short_sha not in snapshot_col:
            errors.append(
                f"docs/products.md row {row_name!r} snapshot {snapshot_col!r} missing {ver!r} or {short_sha!r}"
            )
        links = MD_LINK_RE.findall(release_col)
        if (tag, expected_url) not in links:
            errors.append(
                f"docs/products.md row {row_name!r} release {release_col!r} missing exact link [{tag}]({expected_url})"
            )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="repository root to check"
    )
    args = parser.parse_args(argv)

    errors = check_manifest(args.root.resolve())
    if errors:
        for err in errors:
            print(f"release manifest drift: {err}", file=sys.stderr)
        return 1
    print("release manifest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
