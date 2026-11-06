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


def _fixture(*, dispatch=True):
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
        "backend_scope": "full",
    }
    names = ci_source.BACKEND_JOBS
    jobs = [{"name": name, "status": "completed", "conclusion": "success"} for name in names]
    return {"run": run, "receipt": receipt, "jobs": jobs}


@pytest.mark.parametrize("case", [
    "valid_dispatch", "valid_push", "valid_lookup", "wrong_source", "wrong_run", "wrong_workflow",
    "wrong_workflow_path", "wrong_ref", "wrong_repository", "wrong_control", "failed_run",
    "failed_job", "missing_job", "missing_windows_contract", "skipped_windows_contract",
    "missing_receipt", "wrong_attempt", "incomplete_scope", "other_job_failed",
    "backend_cancelled", "cancelled_run", "incomplete_run", "incomplete_job", "control_not_on_main",
])
def test_release_source_verification_uses_api_jobs_and_bound_receipt(tmp_path, case):
    fixture = _fixture(dispatch=case not in {"valid_push", "valid_lookup"})
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
    elif case == "missing_windows_contract":
        jobs[:] = [job for job in jobs if job["name"] != "API to Windows contract"]
    elif case == "skipped_windows_contract":
        next(job for job in jobs if job["name"] == "API to Windows contract")["conclusion"] = "skipped"
    elif case == "wrong_attempt":
        receipt["run_attempt"] = 2
    elif case == "incomplete_scope":
        receipt["backend_scope"] = "partial"
    elif case == "other_job_failed":
        jobs.append({"name": "unexpected failed job", "status": "completed", "conclusion": "failure"})
    elif case == "backend_cancelled":
        jobs[1]["conclusion"] = "cancelled"
    elif case == "cancelled_run":
        run["conclusion"] = "cancelled"
    elif case == "incomplete_run":
        run.update(status="in_progress", conclusion=None)
    elif case == "incomplete_job":
        jobs[1].update(status="in_progress", conclusion=None)
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
        assert all(value in args for value in ['branch=main', 'event=push', 'head_sha=' + 'a' * 40])
        visible = 'status=completed' not in args or data['run']['status'] == 'completed'
        print(json.dumps({'workflow_runs': [{'id': 42}] if visible else []}))
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
    if case != "valid_lookup":
        command += ["--run-id", "42"]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
             "GH_REPO": REPOSITORY, "FIXTURE": str(tmp_path / "fixture.json")})
    valid = case in {"valid_dispatch", "valid_push", "valid_lookup"}
    assert (result.returncode == 0) == valid, result.stderr
    if not valid:
        assert "No trusted CI receipt" in result.stderr


def test_receipt_requires_both_consumer_contracts():
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    receipt = workflow["jobs"]["receipt"]
    assert {"source", "test", "lock", "web-contract", "windows-contract", "package", "deploy-boundary"} <= set(receipt["needs"])
    assert "needs.windows-contract.result == 'success'" in receipt["if"]


def test_consumer_contract_jobs_checkout_and_pass_their_pins():
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]
    assert "client" not in jobs
    web_steps = jobs["web-contract"]["steps"]
    checkout = next(step for step in web_steps if step.get("with", {}).get("repository") == "My-Denia/WuwaTerm-Windows")
    assert checkout["with"]["ref"] == "${{ steps.web-windows.outputs.revision }}"
    read_pin = next(step for step in web_steps if step.get("id") == "web-windows")
    assert "web/contracts/windows-ref.txt" in read_pin["run"]
    npm = next(step for step in web_steps if step.get("run") == "npm run test:contract")
    assert npm["env"]["WUWATERM_WINDOWS_ROOT"] == "${{ github.workspace }}/windows"
    assert any(step.get("with", {}).get("python-version") == "3.12" for step in web_steps)

    windows_steps = jobs["windows-contract"]["steps"]
    read_pin = next(step for step in windows_steps if step.get("id") == "windows")
    assert "contracts/windows-ref.txt" in read_pin["run"]
    checkout = next(step for step in windows_steps if step.get("with", {}).get("repository") == "My-Denia/WuwaTerm-Windows")
    assert checkout["with"]["ref"] == "${{ steps.windows.outputs.revision }}"
    contract = next(step for step in windows_steps if "tests/test_windows_contract.py" in step.get("run", ""))
    assert contract["env"]["WUWATERM_WINDOWS_ROOT"] == "${{ github.workspace }}/windows"


def _source_repo(path):
    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=path, check=True, capture_output=True, text=True,
        ).stdout.strip()

    path.mkdir()
    git("init", "-q", "-b", "main")
    (path / "fixture.txt").write_text("fixture\n")
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
