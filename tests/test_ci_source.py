"""Exercise CI source selection and release receipt verification offline."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify_ci_source.py"
spec = importlib.util.spec_from_file_location("verify_ci_source", SCRIPT)
ci_source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_source)

SOURCE = "a" * 40
CONTROL = "b" * 40
REPOSITORY = "example/project"


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["docs/validation.md", "deploy/Dockerfile"], False),
        (["tests/test_release_workflow.py"], False),
        (["client/src/wuwaterm_client/api.py"], True),
        (["src/wuwaterm_api/app.py"], True),
        (["contracts/web-ref.txt"], True),
        (["docs/api/openapi.json"], True),
        (["pyproject.toml"], True),
        ([".github/workflows/ci.yml"], True),
        (["new-unclassified-file"], True),
    ],
)
def test_windows_path_selection(paths, expected):
    assert ci_source.client_required(paths) is expected


def _fixture(*, dispatch=True, windows=True):
    event = "workflow_dispatch" if dispatch else "push"
    control = CONTROL if dispatch else SOURCE
    run = {
        "id": 42, "workflow_id": 17, "path": ".github/workflows/ci.yml",
        "status": "completed", "conclusion": "success", "event": event,
        "head_branch": "main", "head_sha": control, "run_attempt": 1,
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
    }
    receipt = {
        "schema": 1, "source_commit": SOURCE, "control_commit": control,
        "run_id": 42, "run_attempt": 1, "event": event,
        "backend_scope": "full", "windows_required": windows,
    }
    names = ci_source.BACKEND_JOBS + ((ci_source.WINDOWS_JOB,) if windows else ())
    jobs = [{"name": name, "status": "completed", "conclusion": "success"} for name in names]
    return {"run": run, "receipt": receipt, "jobs": jobs}


@pytest.mark.parametrize("case", [
    "valid_dispatch", "valid_push_without_windows", "wrong_source", "wrong_run", "wrong_workflow",
    "wrong_workflow_path", "wrong_ref", "wrong_repository", "wrong_control", "failed_run",
    "failed_job", "missing_job", "missing_receipt", "wrong_attempt", "incomplete_scope",
    "dispatch_skips_windows", "windows_skipped_backend", "windows_skipped_client",
    "windows_failed_backend", "windows_failed_client", "windows_failed_lookup", "client_success",
    "windows_and_backend_failed", "other_job_failed", "backend_cancelled", "cancelled_run",
    "incomplete_run", "incomplete_job", "control_not_on_main", "valid_lookup",
])
def test_release_source_verification_uses_api_jobs_and_bound_receipt(tmp_path, case):
    fixture = _fixture(dispatch=case not in {"valid_push_without_windows", "valid_lookup", "windows_failed_lookup"},
                       windows=case != "valid_push_without_windows")
    run, receipt, jobs = fixture["run"], fixture["receipt"], fixture["jobs"]
    if case == "wrong_source":
        receipt["source_commit"] = "c" * 40
    elif case == "wrong_run":
        receipt["run_id"] = 43
    elif case == "wrong_workflow":
        run["workflow_id"] = 18
    elif case == "wrong_workflow_path":
        run["path"] = ".github/workflows/other.yml"
    elif case == "wrong_ref":
        run["head_branch"] = "feature"
    elif case == "wrong_repository":
        run["head_repository"]["full_name"] = "other/project"
    elif case == "wrong_control":
        receipt["control_commit"] = "c" * 40
    elif case == "failed_run":
        run["conclusion"] = "failure"
    elif case == "failed_job":
        jobs[1]["conclusion"] = "failure"
    elif case == "missing_job":
        jobs.pop(1)
    elif case == "wrong_attempt":
        receipt["run_attempt"] = 2
    elif case == "incomplete_scope":
        receipt["backend_scope"] = "partial"
    elif case == "dispatch_skips_windows":
        receipt["windows_required"] = False
    elif case in {"windows_skipped_backend", "windows_skipped_client"}:
        jobs[-1]["conclusion"] = "skipped"
    elif case in {"windows_failed_backend", "windows_failed_client", "windows_failed_lookup", "windows_and_backend_failed"}:
        run["conclusion"] = "failure"
        jobs[-1]["conclusion"] = "failure"
        if case == "windows_and_backend_failed":
            jobs[1]["conclusion"] = "failure"
    elif case == "other_job_failed":
        run["conclusion"] = "failure"
        jobs.append({"name": "unexpected failed job", "status": "completed", "conclusion": "failure"})
    elif case == "backend_cancelled":
        run["conclusion"] = "failure"
        jobs[1]["conclusion"] = "cancelled"
    elif case == "cancelled_run":
        run["conclusion"] = "cancelled"
    elif case == "incomplete_run":
        run["status"] = "in_progress"
    elif case == "incomplete_job":
        jobs[1]["status"] = "in_progress"
    fixture["missing_receipt"] = case == "missing_receipt"
    fixture["main_ancestor"] = case != "control_not_on_main"
    (tmp_path / "fixture.json").write_text(json.dumps(fixture))

    gh = tmp_path / "gh"
    gh.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
data = json.loads(Path(os.environ['FIXTURE']).read_text())
args = sys.argv[1:]
if args[:2] == ['run', 'download']:
    if not data['missing_receipt']:
        dest = Path(args[args.index('--dir') + 1])
        (dest / 'validated-source.json').write_text(json.dumps(data['receipt']))
elif args[0] == 'api':
    path = args[1]
    if '/workflows/ci.yml/runs' in path:
        assert 'status=completed' in args
        print(json.dumps({'workflow_runs': [{'id': 42}]}))
    elif '/workflows/ci.yml' in path:
        print(json.dumps({'id': 17}))
    elif '/jobs?' in path:
        print(json.dumps([{'jobs': data['jobs']}]))
    else:
        print(json.dumps(data['run']))
else:
    sys.exit(2)
''')
    gh.chmod(0o755)
    git = tmp_path / "git"
    git.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
data = json.loads(Path(os.environ['FIXTURE']).read_text())
sys.exit(0 if data['main_ancestor'] else 1)
''')
    git.chmod(0o755)
    command = [sys.executable, str(SCRIPT), "verify", SOURCE]
    if case not in {"valid_lookup", "windows_failed_lookup"}:
        command += ["--run-id", "42"]
    if case in {"windows_skipped_client", "windows_failed_client", "client_success"}:
        command += ["--require-client"]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
             "GH_REPO": REPOSITORY, "FIXTURE": str(tmp_path / "fixture.json")})
    valid = case in {"valid_dispatch", "valid_push_without_windows", "valid_lookup",
                     "windows_skipped_backend", "windows_failed_backend", "windows_failed_lookup",
                     "client_success"}
    assert (result.returncode == 0) == valid, result.stderr
    if not valid:
        assert "No trusted CI receipt" in result.stderr


def test_backend_receipt_does_not_wait_for_windows():
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    receipt = workflow["jobs"]["receipt"]
    assert "client" not in receipt["needs"]
    assert "needs.client" not in receipt["if"]
    assert {"source", "test", "lock", "web-contract", "package", "deploy-boundary"} <= set(receipt["needs"])


def test_client_release_requires_windows_evidence():
    workflow = yaml.load((ROOT / ".github/workflows/client-release.yml").read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["preflight"]["steps"]
    verification = next(step for step in steps if step.get("name") == "Verify successful CI for the actual source")
    commands = [line for line in verification["run"].splitlines() if "scripts/verify_ci_source.py verify" in line]
    assert len(commands) == 2 and all("--require-client" in line for line in commands)


def test_missing_diff_base_forces_windows(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "client-required", "0" * 40, SOURCE],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "true"

    unknown_base = subprocess.run(
        [sys.executable, str(SCRIPT), "client-required", "b" * 40, SOURCE],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert unknown_base.returncode == 0
    assert unknown_base.stdout.strip() == "true"


def _source_repo(path):
    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=path, check=True, capture_output=True, text=True,
        ).stdout.strip()

    path.mkdir()
    git("init", "-q", "-b", "main")
    (path / "client").mkdir()
    (path / "client/pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    git("add", ".")
    git("commit", "-qm", "main")
    main = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", main)
    git("remote", "add", "origin", str(path))
    git("checkout", "-qb", "candidate")
    git("commit", "-q", "--allow-empty", "-m", "candidate")
    return main, git("rev-parse", "HEAD")


def _guard_script(workflow, step_name):
    text = (ROOT / ".github/workflows" / workflow).read_text()
    steps = yaml.load(text, Loader=yaml.BaseLoader)["jobs"]["preflight"]["steps"]
    index = next(i for i, step in enumerate(steps) if step.get("name") == step_name)
    assert all("run" not in step for step in steps[:index]), "source code may run before the guard"
    return steps[index]["run"]


@pytest.mark.parametrize(("workflow", "step_name"), [
    ("release.yml", "Runs on main accept only main source"),
    ("client-release.yml", "Validate source and client version"),
])
@pytest.mark.parametrize(("ref", "target", "allowed"), [
    ("refs/heads/main", "main", True),
    ("refs/heads/main", "candidate", False),
    ("refs/heads/candidate", "candidate", True),
])
def test_main_runs_execute_only_main_source(tmp_path, workflow, step_name, ref, target, allowed):
    repo = tmp_path / "repo"
    main, candidate = _source_repo(repo)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-c", _guard_script(workflow, step_name)], cwd=repo, capture_output=True, text=True,
        env={**os.environ, "TARGET": main if target == "main" else candidate, "DRY_RUN": "true",
             "CONTROL_REF": ref, "GITHUB_OUTPUT": str(output)},
    )
    assert (result.returncode == 0) == allowed, result.stderr
