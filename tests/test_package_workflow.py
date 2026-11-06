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


@pytest.mark.parametrize(
    "telegram_optional,public_smoke", [(False, False), (True, False), (True, True)]
)
def test_package_smokes_follow_resolved_historical_capabilities(
    tmp_path, telegram_optional, public_smoke
):
    """Run the actual workflow steps; a selected smoke must execute and exist."""
    root = tmp_path / "source"
    root.mkdir()
    (root / "dist").mkdir()
    (root / "dist" / "package.whl").touch()
    (root / "dist" / "package.tar.gz").touch()
    (root / "scripts").mkdir()
    if public_smoke:
        (root / "scripts" / "check_package_public_interface.py").touch()
    runner = tmp_path / "runner"
    runner.mkdir()
    stub = tmp_path / "python"
    stub.write_text('''#!/bin/bash
set -eu
if [ "${1-}" = -m ] && [ "${2-}" = venv ]; then
  mkdir -p "$3/bin"
  for cmd in python pip wuwaterm wuwaterm-api; do cp "$MOCK_PYTHON" "$3/bin/$cmd"; done
  exit 0
fi
printf '%s\\n' "$*" >> "$MOCK_LOG"
case "${1-}" in
  -I) test -f "$2" ;;
  -c)
    case "$2" in
      *"find_spec('telegram') is None"*) test "$MOCK_INSTALLED" = false ;;
      "import telegram") test "$MOCK_INSTALLED" = true ;;
    esac ;;
esac
''')
    stub.chmod(0o755)
    log = tmp_path / "invocations"
    env = {
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "MOCK_PYTHON": str(stub), "MOCK_LOG": str(log), "RUNNER_TEMP": str(runner),
        "MOCK_INSTALLED": str(not telegram_optional).lower(),
        "TELEGRAM_OPTIONAL": str(telegram_optional).lower(),
        "PUBLIC_INTERFACE_SMOKE": str(public_smoke).lower(),
    }
    steps = _workflow("python-package.yml")["jobs"]["build"]["steps"]
    for name in (
        "Install wheel into a clean venv and smoke it",
        "Install API and build extras into clean venvs",
        "Install sdist into a clean venv and smoke it",
    ):
        step = _step(steps, name)
        assert step["env"] == {
            "TELEGRAM_OPTIONAL": "${{ steps.epoch.outputs.telegram_optional }}",
            "PUBLIC_INTERFACE_SMOKE": "${{ steps.epoch.outputs.public_interface_smoke }}",
        }
        result = _run(step["run"], root, env)
        assert result.returncode == 0, (name, result.stderr)
    calls = log.read_text().splitlines()
    assert sum("find_spec('telegram') is None" in line for line in calls) == (4 if telegram_optional else 0)
    assert calls.count("-c import telegram") == (0 if telegram_optional else 4)
    assert sum("check_package_public_interface.py" in line for line in calls) == (4 if public_smoke else 0)
    if public_smoke:
        (root / "scripts" / "check_package_public_interface.py").unlink()
        step = _step(steps, "Install wheel into a clean venv and smoke it")
        assert _run(step["run"], root, env).returncode != 0


def test_package_resolves_at_control_before_checking_out_source():
    steps = _workflow("python-package.yml")["jobs"]["build"]["steps"]
    control = next(i for i, step in enumerate(steps) if step.get("with", {}).get("ref") == "${{ github.workflow_sha }}")
    epoch = next(i for i, step in enumerate(steps) if step.get("id") == "epoch")
    source = next(i for i, step in enumerate(steps) if step.get("with", {}).get("ref") == "${{ inputs.source_commit }}")
    assert control < epoch < source
    assert steps[control]["with"]["fetch-depth"] == "0"
    assert 'test "$(git rev-parse HEAD)" = "$CONTROL_COMMIT"' in steps[epoch]["run"]
    assert 'python scripts/ci_source_epoch.py resolve "$SOURCE_COMMIT"' in steps[epoch]["run"]
    assert set(_workflow("python-package.yml")["on"]["workflow_call"]["inputs"]) == {
        "source_commit", "upload_package", "candidate_metadata"
    }


@pytest.mark.parametrize(
    "epoch,valid",
    [
        ({"telegram_optional": False, "public_interface_smoke": False}, True),
        ({"telegram_optional": True, "public_interface_smoke": True}, True),
        ({"telegram_optional": False}, False),
        ({"telegram_optional": "false", "public_interface_smoke": False}, False),
    ],
)
def test_package_rejects_missing_or_invalid_resolver_outputs(tmp_path, epoch, valid):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "ci_source_epoch.py").write_text('import os\nprint(os.environ["MOCK_EPOCH"])\n')
    setup = _run(
        "git init -q && git add . && "
        "git -c user.name=Fixture -c user.email=fixture@example.invalid "
        "-c commit.gpgsign=false -c core.hooksPath=/dev/null commit -qm fixture",
        tmp_path,
    )
    assert setup.returncode == 0, setup.stderr
    control = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True,
                                      env=_environment()).strip()
    output = tmp_path / "output"
    step = _step(_workflow("python-package.yml")["jobs"]["build"]["steps"],
                 "Resolve source epoch from workflow control")
    result = _run(step["run"], tmp_path, {"CONTROL_COMMIT": control, "SOURCE_COMMIT": "a" * 40,
                                         "MOCK_EPOCH": json.dumps(epoch), "GITHUB_OUTPUT": str(output)})
    assert (result.returncode == 0) == valid, result.stderr
    if valid:
        assert output.read_text().splitlines() == [
            f'telegram_optional={str(epoch["telegram_optional"]).lower()}',
            f'public_interface_smoke={str(epoch["public_interface_smoke"]).lower()}',
        ]
    else:
        assert not output.exists()
