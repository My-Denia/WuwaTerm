#!/usr/bin/env python3
"""Resolve a CI source against the control revision's immutable epoch policy.

The caller admits the source for its event first. This resolver uses the Git
commit graph and blobs, never the working tree, to select mandatory checks.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

SHA = re.compile(r"[0-9a-f]{40}\Z")
COMMON_JOBS = (
    "Resolve source",
    *(f"pytest (py{version})" for version in ("3.11", "3.12", "3.13", "3.14")),
    "uv locked runtime behavior (py3.11)",
    "API to Web contract",
    "wheel/sdist build, audit, install / build and smoke",
    "docker runtime/builder boundary",
    "validated source",
)
COMMON_FILES = (
    "pyproject.toml", "uv.lock", "contracts/web-ref.txt", "scripts/validate.py",
    "scripts/check_package_artifacts.py", "tests/test_meta_site_contract.py",
    "deploy/Dockerfile", "deploy/docker-compose.yml",
)
LEGACY_FILES = (
    "client/pyproject.toml", "client/build.ps1", "client/WuwaTerm.spec",
    "client/src/wuwaterm_client/strings.py", "client/tests/test_terms_category_labels.py",
)
MANUSCRIPT_FILES = (
    "client/src/wuwaterm_client/manuscript.py", "client/tests/test_manuscript.py",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _sha(value: Any, name: str) -> str:
    _require(isinstance(value, str) and bool(SHA.fullmatch(value)), f"missing or invalid {name}")
    return value


def load_config(raw: bytes) -> dict[str, Any]:
    """Validate config bytes obtained from the running CONTROL Git object."""
    try:
        config = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid source epoch config") from exc
    _require(isinstance(config, dict) and type(config.get("schema")) is int
             and config["schema"] == 1, "invalid source epoch config schema")
    for key in ("source_floor", "manuscript_milestone", "optional_telegram_milestone",
                "public_interface_milestone", "last_legacy_source"):
        _sha(config.get(key), key)
    consumers = config.get("consumers")
    jobs = config.get("jobs")
    _require(isinstance(consumers, dict) and isinstance(jobs, dict), "missing consumers or jobs")
    for key in ("legacy_web_revision", "split_web_revision", "split_windows_revision",
                "windows_recipe_sha"):
        _sha(consumers.get(key), key)
    _require(consumers.get("web_repository") == "My-Denia/WuwaTerm-Web", "invalid Web repository")
    _require(consumers.get("windows_repository") == "My-Denia/WuwaTerm-Windows", "invalid Windows repository")
    _require(consumers.get("windows_recipe_workflow") == ".github/workflows/legacy-core-validation.yml",
             "invalid Windows recipe workflow")
    _require(jobs.get("common") == list(COMMON_JOBS), "missing or incorrect common jobs")
    _require(jobs.get("split_windows") == "API to Windows contract", "invalid split Windows job")
    legacy_job = jobs.get("legacy_windows")
    _require(isinstance(legacy_job, str) and bool(legacy_job.strip())
             and legacy_job not in (*COMMON_JOBS, jobs["split_windows"]),
             "missing or invalid legacy Windows job")
    return config


def validate_workflow(raw_workflow: str, config: dict[str, Any]) -> None:
    """Bind the selected reusable job to the recipe and source in CONTROL.

    This is deliberately a narrow, strict stanza check, not a YAML parser. The
    early CI and release steps cannot assume PyYAML is installed. A different
    job format must be reviewed and reflected here before a receipt is issued.
    """
    lines = raw_workflow.splitlines()
    roots = [index for index, line in enumerate(lines)
             if re.match(r"^(?:jobs|['\"]jobs['\"])\s*:", line)]
    _require(len(roots) == 1 and lines[roots[0]] == "jobs:",
             "missing or ambiguous CI jobs section")
    _require(not any(line.startswith(("'", '"', "?", "*", "{")) for line in lines),
             "noncanonical top-level workflow keys are not supported")
    root = roots[0]
    end = next((index for index in range(root + 1, len(lines))
                if lines[index] and not lines[index][0].isspace()
                and not lines[index].startswith("#")), len(lines))
    section = lines[root + 1:end]
    for line in section:
        if line.startswith("  ") and not line.startswith("   ") and line.strip() and not line.lstrip().startswith("#"):
            _require(bool(re.fullmatch(r"  [A-Za-z_][A-Za-z0-9_-]*:\s*", line)),
                     "noncanonical CI job keys are not supported")
    job_pattern = re.compile(r"^  ['\"]?historical-windows['\"]?\s*:")
    matches = [index for index, line in enumerate(section) if job_pattern.match(line)]
    _require(len(matches) == 1 and section[matches[0]] == "  historical-windows:",
             "missing or ambiguous historical Windows job")
    start = matches[0]
    finish = next((index for index in range(start + 1, len(section))
                   if re.match(r"^  \S[^:]*:", section[index])), len(section))
    job = [line for line in section[start:finish]
           if line.strip() and not line.lstrip().startswith("#")]
    consumers = config["consumers"]
    recipe = (f"{consumers['windows_repository']}/{consumers['windows_recipe_workflow']}"
              f"@{_sha(consumers.get('windows_recipe_sha'), 'windows_recipe_sha')}")
    _require(len(job) >= 3 and bool(re.fullmatch(r"    needs: (?:source|\[\s*source\s*\])", job[2])),
             "historical Windows job does not match control policy")
    normalized_job = ["    needs: source" if index == 2 else line
                      for index, line in enumerate(job)]
    _require(normalized_job == [
        "  historical-windows:",
        "    name: Historical bundled Windows validation",
        "    needs: source",
        "    if: needs.source.outputs.source_epoch == 'legacy'",
        f"    uses: {recipe}",
        "    with:",
        "      source_commit: ${{ needs.source.outputs.source_commit }}",
    ], "historical Windows job does not match control policy")


def _git(*arguments: str) -> str:
    try:
        result = subprocess.run(["git", *arguments], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Git object unavailable or invalid: {' '.join(arguments)}") from exc
    return result.stdout


def _ancestor(ancestor: str, descendant: str) -> bool:
    result = subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode not in (0, 1):
        raise ValueError("unable to establish source ancestry")
    return result.returncode == 0


def _blob(source: str, path: str) -> str:
    return _git("show", f"{source}:{path}")


def _has_telegram(requirements: Any) -> bool:
    _require(isinstance(requirements, list) and all(isinstance(item, str) for item in requirements),
             "invalid project dependency metadata")
    return any(re.match(r"^python-telegram-bot(?:\s|\[|[<>=!~;]|$)", item, re.I)
               for item in requirements)


def resolve(source: str, config: dict[str, Any]) -> dict[str, Any]:
    """Resolve SOURCE; the caller must first admit it for its CI event.

    No equal-tree comparison can establish epoch membership. Both historical
    and post-split sources must connect to the actual last bundled main commit.
    """
    _sha(source, "source")
    _require(_git("cat-file", "-t", source).strip() == "commit", "source is not a commit")
    floor = config["source_floor"]
    last = config["last_legacy_source"]
    milestones = [floor, config["manuscript_milestone"], config["optional_telegram_milestone"],
                  config["public_interface_milestone"], last]
    for left, right in zip(milestones, milestones[1:]):
        _require(_ancestor(left, right), "invalid control milestone ancestry")
    _require(_ancestor(floor, source), "source predates or is disconnected from the supported floor")
    if _ancestor(source, last):
        epoch = "legacy"
    elif _ancestor(last, source):
        epoch = "split"
    else:
        raise ValueError("source is disconnected from the last bundled main commit")

    optional = _ancestor(config["optional_telegram_milestone"], source)
    public_smoke = _ancestor(config["public_interface_milestone"], source)
    manuscript = epoch == "legacy" and _ancestor(config["manuscript_milestone"], source)
    for path in COMMON_FILES + (LEGACY_FILES if epoch == "legacy" else ("contracts/windows-ref.txt", "tests/test_windows_contract.py")):
        _blob(source, path)
    if manuscript:
        for path in MANUSCRIPT_FILES:
            _blob(source, path)
        _require("def test_actual_web_and_client_read_each_others_files(" in
                 _blob(source, "client/tests/test_manuscript.py"), "missing actual manuscript interoperability test")
    if public_smoke:
        _blob(source, "scripts/check_package_public_interface.py")

    try:
        project = tomllib.loads(_blob(source, "pyproject.toml"))["project"]
        extras = project["optional-dependencies"]
        _require(isinstance(extras, dict), "invalid optional dependency metadata")
        for extra in ("dev", "api", "build"):
            _require(isinstance(extras.get(extra), list) and bool(extras[extra]), f"missing {extra} extra")
        base_telegram = _has_telegram(project["dependencies"])
        if optional:
            _require(not base_telegram and _has_telegram(extras.get("telegram")),
                     "source does not have the expected optional Telegram metadata")
        else:
            _require(base_telegram and "telegram" not in extras,
                     "source does not have the expected base Telegram metadata")
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("invalid source dependency metadata") from exc

    consumers = config["consumers"]
    web_revision = consumers["legacy_web_revision" if epoch == "legacy" else "split_web_revision"]
    _require(_blob(source, "contracts/web-ref.txt").strip() == web_revision, "wrong source Web pin")
    windows_revision = None
    if epoch == "split":
        windows_revision = consumers["split_windows_revision"]
        _require(_blob(source, "contracts/windows-ref.txt").strip() == windows_revision,
                 "wrong source Windows pin")
    jobs = config["jobs"]
    return {
        "source_epoch": epoch,
        "telegram_optional": optional,
        "public_interface_smoke": public_smoke,
        "manuscript_interop": manuscript,
        "web_repository": consumers["web_repository"],
        "web_revision": web_revision,
        "windows_repository": consumers["windows_repository"],
        "windows_revision": windows_revision,
        "windows_recipe_workflow": consumers["windows_recipe_workflow"],
        "windows_recipe_sha": consumers["windows_recipe_sha"],
        "required_jobs": jobs["common"] + [jobs["legacy_windows" if epoch == "legacy" else "split_windows"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("resolve")
    command.add_argument("source")
    command.add_argument("--config", type=Path, default=Path("contracts/ci-source-epochs.json"))
    command.add_argument("--workflow", type=Path)
    args = parser.parse_args()
    try:
        config = load_config(args.config.read_bytes())
        if args.workflow is not None:
            validate_workflow(args.workflow.read_text(encoding="utf-8"), config)
        print(json.dumps(resolve(args.source, config), sort_keys=True))
    except (OSError, ValueError) as exc:
        print(f"CI source epoch resolution failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
