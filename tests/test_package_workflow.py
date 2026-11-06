"""Exercise both callers' package layouts through the release consumer."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


def _enabled(condition, inputs):
    expression = condition.strip().removeprefix("${{").removesuffix("}}").strip()
    expression = re.sub(
        r"inputs\.([a-z_]+)", lambda match: str(inputs[match[1]]), expression
    )
    expression = expression.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    expression = re.sub(r"\btrue\b", "True", expression, flags=re.IGNORECASE)
    expression = re.sub(r"\bfalse\b", "False", expression, flags=re.IGNORECASE).strip()
    tree = ast.parse(expression, mode="eval")
    allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Constant)
    assert all(isinstance(node, allowed) for node in ast.walk(tree))
    return bool(eval(compile(tree, "<workflow condition>", "eval"), {"__builtins__": {}}))


def _environment(overrides=None):
    clean_env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        clean_env.pop(name, None)
    clean_env.update(overrides or {})
    return clean_env


def _run(script, cwd, env=None):
    return subprocess.run(["bash", "-e", "-c", script], cwd=cwd, env=_environment(env),
                          capture_output=True, text=True)


def _step(steps, name):
    return next(step for step in steps if step.get("name") == name)


@pytest.mark.parametrize("caller_name,candidate", [("ci.yml", True), ("release.yml", False)])
def test_package_callers_feed_their_real_consumers(tmp_path, caller_name, candidate):
    package = _workflow("python-package.yml")
    inputs = package["on"]["workflow_call"]["inputs"]
    selected = {name: value.get("default") == "true" for name, value in inputs.items()}
    selected.setdefault("candidate_metadata", False)
    caller = _workflow(caller_name)["jobs"]["package" if candidate else "python-package"]
    assert caller["uses"] == "./.github/workflows/python-package.yml"
    selected.update({key: value == "true" for key, value in caller["with"].items()
                     if key in {"upload_package", "candidate_metadata"}})
    assert selected["upload_package"] and selected["candidate_metadata"] == candidate
    if candidate:
        assert inputs["candidate_metadata"] == {"default": "false", "type": "boolean"}

    source = tmp_path / "source"
    source.mkdir()
    (source / ".gitignore").write_text("dist/\n")
    version = "0.7.0.dev1" if candidate else "0.7.0"
    (source / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    setup = _run(
        "git init -q && git add . && "
        "git -c user.name=Fixture -c user.email=fixture@example.invalid "
        "-c commit.gpgsign=false -c core.hooksPath=/dev/null commit -qm fixture",
        source,
    )
    assert setup.returncode == 0, setup.stderr
    source_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True, env=_environment()
    ).strip()
    source_tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=source, text=True, env=_environment()
    ).strip()
    steps = package["jobs"]["build"]["steps"]
    source_step = _step(steps, "Confirm clean source checkout")
    assert _run(source_step["run"], source, {"SOURCE_COMMIT": source_sha}).returncode == 0
    assert _run(source_step["run"], source, {"SOURCE_COMMIT": "0" * 40}).returncode != 0

    dist = source / "dist"
    dist.mkdir()
    wheel = dist / f"wuwaterm-{version}-py3-none-any.whl"
    sdist = dist / f"wuwaterm-{version}.tar.gz"
    wheel.write_bytes(b"synthetic wheel")
    sdist.write_bytes(b"synthetic sdist")
    metadata_step = _step(steps, "Record package checksums and source")
    if _enabled(metadata_step["if"], selected):
        assert _run(metadata_step["run"], source, {"SOURCE_COMMIT": "0" * 40}).returncode != 0
        result = _run(metadata_step["run"], source,
                      {"SOURCE_COMMIT": source_sha, "GITHUB_SHA": "f" * 40})
        assert result.returncode == 0, result.stderr

    staging = tmp_path / "staging"
    staging.mkdir()
    uploads = [step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")
               and _enabled(step["if"], selected)]
    assert len(uploads) == 1 and uploads[0]["with"]["name"] == "python-package"
    for pattern in uploads[0]["with"]["path"].splitlines():
        matches = list(source.glob(pattern))
        assert matches, pattern
        for path in matches:
            shutil.copyfile(path, staging / path.name)
    names = {wheel.name, sdist.name}
    if candidate:
        assert {p.name for p in staging.iterdir()} == names | {"SHA256SUMS", "source-manifest.json"}
        manifest = json.loads((staging / "source-manifest.json").read_text())
        assert manifest["source_commit"] == source_sha
        assert manifest["source_tree"] == source_tree
        assert manifest["version"] == version
        assert manifest["sha256"] == {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (wheel, sdist)
        }
        assert _run("sha256sum -c SHA256SUMS", staging).returncode == 0
        (staging / wheel.name).write_bytes(b"altered wheel")
        assert _run("sha256sum -c SHA256SUMS", staging).returncode != 0
        return

    assert {p.name for p in staging.iterdir()} == names
    assemble = _workflow("release.yml")["jobs"]["assemble"]["steps"]
    checksum = _step(assemble, "Checksums over the two downloadable build outputs")
    manifest_step = _step(assemble, "Write release-manifest.json")
    guard = _step(assemble, "The staged set is exactly the four release assets and verifies")
    assert _run(checksum["run"], staging).returncode == 0
    release_env = {key: "fixture" for key in manifest_step["env"]}
    release_env.update(TAG="v0.7.0", VERSION=version, SOURCE_COMMIT=source_sha, DRY_RUN="true")
    result = _run(manifest_step["run"], tmp_path, release_env)
    assert result.returncode == 0, result.stderr
    assert {p.name for p in staging.iterdir()} == names | {"SHA256SUMS", "release-manifest.json"}
    assert _run(guard["run"], staging).returncode == 0
    contaminated = tmp_path / "contaminated"
    shutil.copytree(staging, contaminated)
    (contaminated / "source-manifest.json").write_text("{}")
    assert _run(guard["run"], contaminated).returncode != 0
    (staging / wheel.name).write_bytes(b"altered wheel")
    assert _run(guard["run"], staging).returncode != 0


def test_every_existing_package_caller_is_covered():
    callers = set()
    for workflow in WORKFLOWS.glob("*.yml"):
        for job in _workflow(workflow.name).get("jobs", {}).values():
            if job.get("uses") == "./.github/workflows/python-package.yml":
                callers.add(workflow.name)
    assert callers == {"ci.yml", "release.yml"}
