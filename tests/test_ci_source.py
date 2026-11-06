"""Exercise bound CI receipts, their selected jobs, and release admission."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
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
DIGEST = "d" * 64


def _fixture(*, dispatch=True, legacy=False):
    event = "workflow_dispatch" if dispatch else "push"
    control = CONTROL if dispatch else SOURCE
    run = {
        "id": 42, "workflow_id": 17, "path": ".github/workflows/ci.yml",
        "status": "completed", "conclusion": "success", "event": event,
        "head_branch": "main", "head_sha": control, "run_attempt": 1,
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
    }
    policy = {
        "source_epoch": "legacy" if legacy else "split",
        "windows_recipe_sha": "e" * 40,
        "web_revision": "f" * 40,
        "windows_revision": None if legacy else "c" * 40,
        "web_repository": "My-Denia/WuwaTerm-Web",
        "windows_repository": "My-Denia/WuwaTerm-Windows",
        "windows_recipe_workflow": ".github/workflows/legacy-core-validation.yml",
        "required_jobs": sorted(ci_source.COMMON_JOBS) + [
            "Historical bundled Windows validation / build and test" if legacy
            else "API to Windows contract"
        ],
    }
    receipt = {
        "schema": 2, "source_commit": SOURCE, "control_commit": control,
        "run_id": 42, "run_attempt": 1, "event": event,
        "backend_scope": "full", "control_config_sha256": DIGEST, **policy,
    }
    jobs = [{"name": name, "status": "completed", "conclusion": "success"}
            for name in policy["required_jobs"]]
    return run, receipt, jobs, policy


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("dispatch", [False, True])
def test_receipt_accepts_complete_bound_attempt(legacy, dispatch):
    run, receipt, jobs, policy = _fixture(dispatch=dispatch, legacy=legacy)
    assert ci_source.verify_record(run, jobs, receipt, 17, REPOSITORY, SOURCE, 42, DIGEST, policy) == run["head_sha"]


@pytest.mark.parametrize("case", [
    "schema1", "missing_schema", "wrong_source", "wrong_run", "wrong_workflow", "wrong_workflow_path",
    "wrong_ref", "wrong_repository", "wrong_control", "failed_run", "cancelled_run", "incomplete_run",
    "wrong_event", "wrong_attempt", "zero_attempt", "incomplete_scope", "missing_config_digest",
    "wrong_config_digest", "missing_epoch", "wrong_epoch", "wrong_recipe", "missing_recipe",
    "wrong_web", "wrong_windows", "missing_nullable_windows", "wrong_web_repository",
    "wrong_windows_repository", "wrong_recipe_workflow", "missing_selected_job",
    "skipped_selected_job", "failed_selected_job", "missing_common_job", "skipped_common_job",
    "incomplete_job", "other_job_failed", "duplicate_job", "wrong_selected_job_set",
    "legacy_missing_selected", "legacy_skipped_selected", "legacy_failed_selected",
])
def test_receipt_rejects_identity_binding_and_job_failures(case):
    legacy = case == "missing_nullable_windows" or case.startswith("legacy_")
    run, receipt, jobs, policy = _fixture(legacy=legacy)
    if case == "schema1":
        receipt["schema"] = 1
    elif case == "missing_schema":
        del receipt["schema"]
    elif case == "wrong_source":
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
    elif case == "cancelled_run":
        run["conclusion"] = "cancelled"
    elif case == "incomplete_run":
        run.update(status="in_progress", conclusion=None)
    elif case == "wrong_event":
        receipt["event"] = "push"
    elif case == "wrong_attempt":
        receipt["run_attempt"] = 2
    elif case == "zero_attempt":
        run["run_attempt"] = 0
    elif case == "incomplete_scope":
        receipt["backend_scope"] = "partial"
    elif case == "missing_config_digest":
        del receipt["control_config_sha256"]
    elif case == "wrong_config_digest":
        receipt["control_config_sha256"] = "c" * 64
    elif case == "missing_epoch":
        del receipt["source_epoch"]
    elif case == "wrong_epoch":
        receipt["source_epoch"] = "legacy"
    elif case == "wrong_recipe":
        receipt["windows_recipe_sha"] = "c" * 40
    elif case == "missing_recipe":
        del receipt["windows_recipe_sha"]
    elif case == "wrong_web":
        receipt["web_revision"] = "c" * 40
    elif case == "wrong_windows":
        receipt["windows_revision"] = "a" * 40
    elif case == "missing_nullable_windows":
        del receipt["windows_revision"]
    elif case == "wrong_web_repository":
        receipt["web_repository"] = "other/Web"
    elif case == "wrong_windows_repository":
        receipt["windows_repository"] = "other/Windows"
    elif case == "wrong_recipe_workflow":
        receipt["windows_recipe_workflow"] = ".github/workflows/other.yml"
    elif case == "missing_selected_job":
        jobs.pop()
    elif case == "skipped_selected_job":
        jobs[-1]["conclusion"] = "skipped"
    elif case == "failed_selected_job":
        jobs[-1]["conclusion"] = "failure"
    elif case == "missing_common_job":
        jobs.pop(0)
    elif case == "skipped_common_job":
        jobs[0]["conclusion"] = "skipped"
    elif case == "incomplete_job":
        jobs[0].update(status="in_progress", conclusion=None)
    elif case == "other_job_failed":
        jobs.append({"name": "other", "status": "completed", "conclusion": "failure"})
    elif case == "duplicate_job":
        jobs.append(jobs[0].copy())
    elif case == "wrong_selected_job_set":
        receipt["required_jobs"] = receipt["required_jobs"][:-1]
    elif case == "legacy_missing_selected":
        jobs.pop()
    elif case == "legacy_skipped_selected":
        jobs[-1]["conclusion"] = "skipped"
    elif case == "legacy_failed_selected":
        jobs[-1]["conclusion"] = "failure"
    with pytest.raises(ValueError):
        ci_source.verify_record(run, jobs, receipt, 17, REPOSITORY, SOURCE, 42, DIGEST, policy)


# Only the Git object transport is simulated: the returned resolver is the real
# script and it resolves actual historical commits against the local Git graph.
# These values are an explicitly unfinalized local fixture, not a recipe pin.
LAST_BUNDLED = "64e293cf53ba9106a4a67639fd5c6b0857e6c384"
FLOOR = "13f601871566d89b372ecf31b45345f5387b601c"
OPTIONAL = "0c7be297472c6051b7190974f803b3c6e03d7b63"
UNREACHABLE_SAME_TREE = "f8a9008d7adf60006ca210c750aa97f5de386a26"


def _object_fixture(monkeypatch, mutate=None, missing=None, raw_override=None, workflow_override=None):
    monkeypatch.chdir(ROOT)
    config = json.loads((ROOT / "contracts/ci-source-epochs.json").read_bytes())
    config["consumers"]["windows_recipe_sha"] = "e" * 40
    config["jobs"]["legacy_windows"] = "UNFINALIZED / build and test"
    if mutate:
        mutate(config)
    raw = raw_override if raw_override is not None else (json.dumps(config, sort_keys=True) + "\n").encode()
    workflow = ("jobs:\n"
                "  historical-windows:\n"
                "    name: Historical bundled Windows validation\n"
                "    needs: source\n"
                "    if: needs.source.outputs.source_epoch == 'legacy'\n"
                "    uses: My-Denia/WuwaTerm-Windows/.github/workflows/legacy-core-validation.yml@"
                + "e" * 40 + "\n"
                "    with:\n"
                "      source_commit: ${{ needs.source.outputs.source_commit }}\n").encode()
    if workflow_override is not None:
        workflow = workflow_override
    original = subprocess.check_output
    calls = []

    def from_control(args, *a, **kw):
        if args == ["git", "show", f"{LAST_BUNDLED}:{ci_source.CONFIG_PATH}"]:
            calls.append("config")
            if missing == "config":
                raise subprocess.CalledProcessError(128, args)
            return raw
        if args == ["git", "show", f"{LAST_BUNDLED}:{ci_source.RESOLVER_PATH}"]:
            calls.append("resolver")
            if missing == "resolver":
                raise subprocess.CalledProcessError(128, args)
            return (ROOT / ci_source.RESOLVER_PATH).read_bytes()
        if args == ["git", "show", f"{LAST_BUNDLED}:{ci_source.WORKFLOW_PATH}"]:
            calls.append("workflow")
            if missing == "workflow":
                raise subprocess.CalledProcessError(128, args)
            return workflow
        return original(args, *a, **kw)

    monkeypatch.setattr(subprocess, "check_output", from_control)
    return raw, calls


@pytest.mark.parametrize("source", [FLOOR, OPTIONAL, LAST_BUNDLED])
def test_control_object_policy_resolves_actual_historical_source(monkeypatch, source):
    raw, calls = _object_fixture(monkeypatch)
    digest, policy = ci_source.trusted_policy(LAST_BUNDLED, source)
    assert calls == ["config", "resolver", "workflow"]
    assert digest == hashlib.sha256(raw).hexdigest()
    assert policy["source_epoch"] == "legacy"
    assert policy["telegram_optional"] == (source != FLOOR)
    assert policy["windows_recipe_sha"] == "e" * 40
    assert policy["required_jobs"][-1] == "UNFINALIZED / build and test"


@pytest.mark.parametrize("missing", ["config", "resolver", "workflow"])
def test_unavailable_control_objects_fail_closed(monkeypatch, missing):
    _object_fixture(monkeypatch, missing=missing)
    with pytest.raises(subprocess.CalledProcessError):
        ci_source.trusted_policy(LAST_BUNDLED, LAST_BUNDLED)


def test_missing_recipe_in_control_config_fails_closed(monkeypatch):
    _object_fixture(monkeypatch, mutate=lambda config: config["consumers"].pop("windows_recipe_sha"))
    with pytest.raises(subprocess.CalledProcessError):
        ci_source.trusted_policy(LAST_BUNDLED, LAST_BUNDLED)


@pytest.mark.parametrize("bad_config", ["malformed", "missing_job", "wrong_web_pin"])
def test_invalid_or_mismatched_control_config_fails_closed(monkeypatch, bad_config):
    if bad_config == "malformed":
        _object_fixture(monkeypatch, raw_override=b"not json")
    elif bad_config == "missing_job":
        _object_fixture(monkeypatch, mutate=lambda c: c["jobs"].pop("legacy_windows"))
    else:
        _object_fixture(monkeypatch, mutate=lambda c: c["consumers"].update(legacy_web_revision="f" * 40))
    with pytest.raises(subprocess.CalledProcessError):
        ci_source.trusted_policy(LAST_BUNDLED, LAST_BUNDLED)


def _assert_not_main_ancestor(control, source, rejected):
    with pytest.raises(subprocess.CalledProcessError) as error:
        ci_source.trusted_policy(control, source)
    assert error.value.cmd == ["git", "merge-base", "--is-ancestor", rejected, "origin/main"]


def test_unconnected_same_tree_is_rejected_in_both_positions(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    common = Path(subprocess.check_output(["git", "rev-parse", "--git-common-dir"], text=True).strip())
    if not common.is_absolute():
        common = (ROOT / common).resolve()
    objects = tmp_path / "objects"
    objects.mkdir()
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(objects))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(common / "objects"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    tree = subprocess.check_output(["git", "rev-parse", f"{LAST_BUNDLED}^{{tree}}"], text=True).strip()
    sibling = subprocess.check_output(["git", "commit-tree", tree, "-p", FLOOR],
                                      input="fixture\n", text=True).strip()
    assert subprocess.check_output(["git", "rev-parse", f"{sibling}^{{tree}}"], text=True).strip() == tree
    _assert_not_main_ancestor(LAST_BUNDLED, sibling, sibling)
    _assert_not_main_ancestor(sibling, LAST_BUNDLED, sibling)


def test_real_f8_equal_tree_is_rejected_when_object_is_available(monkeypatch):
    monkeypatch.chdir(ROOT)
    result = subprocess.run(["git", "cat-file", "-e", f"{UNREACHABLE_SAME_TREE}^{{commit}}"],
                            capture_output=True)
    if result.returncode:
        pytest.skip("historical feature object is not required in a main clone")
    tree = subprocess.check_output(["git", "rev-parse", f"{LAST_BUNDLED}^{{tree}}"], text=True).strip()
    assert subprocess.check_output(["git", "rev-parse", f"{UNREACHABLE_SAME_TREE}^{{tree}}"], text=True).strip() == tree
    _assert_not_main_ancestor(LAST_BUNDLED, UNREACHABLE_SAME_TREE, UNREACHABLE_SAME_TREE)
    _assert_not_main_ancestor(UNREACHABLE_SAME_TREE, LAST_BUNDLED, UNREACHABLE_SAME_TREE)


@pytest.mark.parametrize("workflow", [b"jobs: {}\n", b"jobs:\n  historical-windows:\n    uses: wrong@" + b"e" * 40])
def test_missing_or_wrong_recipe_in_control_workflow_fails_closed(monkeypatch, workflow):
    _object_fixture(monkeypatch, workflow_override=workflow)
    with pytest.raises(subprocess.CalledProcessError):
        ci_source.trusted_policy(LAST_BUNDLED, LAST_BUNDLED)


@pytest.mark.parametrize("case", ["valid", "bad_digest", "job_skipped", "missing_receipt", "run_incomplete"])
def test_entire_verifier_queries_the_exact_attempt_and_receipt(monkeypatch, case):
    raw, _ = _object_fixture(monkeypatch)
    digest, policy = ci_source.trusted_policy(LAST_BUNDLED, LAST_BUNDLED)
    assert digest == hashlib.sha256(raw).hexdigest()
    run, receipt, _, _ = _fixture(legacy=True)
    run.update(head_sha=LAST_BUNDLED, run_attempt=2)
    receipt.update(policy, source_commit=LAST_BUNDLED, control_commit=LAST_BUNDLED,
                   control_config_sha256=digest, run_attempt=2)
    jobs = [{"name": name, "status": "completed", "conclusion": "success"}
            for name in policy["required_jobs"]]
    if case == "bad_digest":
        receipt["control_config_sha256"] = "0" * 64
    elif case == "job_skipped":
        jobs[-1]["conclusion"] = "skipped"
    elif case == "run_incomplete":
        run.update(status="in_progress", conclusion=None)
    requests = []

    def gh_json(*args):
        requests.append(args[0])
        if args[0].endswith("/actions/workflows/ci.yml"):
            return {"id": 17}
        if "/attempts/2/jobs?" in args[0]:
            return [{"jobs": jobs}]
        if args[0].endswith("/actions/runs/42"):
            return run
        raise AssertionError(args)

    run_command = subprocess.run

    def command(args, *a, **kw):
        if args[:3] == ["gh", "run", "download"]:
            assert args[args.index("--name") + 1] == "validated-source-2"
            dest = Path(args[args.index("--dir") + 1])
            if case != "missing_receipt":
                (dest / "validated-source.json").write_text(json.dumps(receipt))
            return subprocess.CompletedProcess(args, 0)
        return run_command(args, *a, **kw)

    monkeypatch.setenv("GH_REPO", REPOSITORY)
    monkeypatch.setattr(ci_source, "gh_json", gh_json)
    monkeypatch.setattr(subprocess, "run", command)
    if case == "valid":
        ci_source.verify_source(LAST_BUNDLED, "42")
    else:
        with pytest.raises(ValueError, match="Dispatch current CI on main"):
            ci_source.verify_source(LAST_BUNDLED, "42")
    if case == "run_incomplete":
        assert not any("/attempts/" in request for request in requests)
    else:
        assert any("/actions/runs/42/attempts/2/jobs?" in request for request in requests)


def test_receipt_requires_the_selected_windows_path_and_control_policy():
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    receipt = workflow["jobs"]["receipt"]
    assert {"source", "test", "lock", "web-contract", "windows-contract", "historical-windows",
            "package", "deploy-boundary"} <= set(receipt["needs"])
    gate = receipt["if"]
    assert "needs.source.outputs.source_epoch == 'split'" in gate
    assert "needs.windows-contract.result == 'success'" in gate
    assert "needs.historical-windows.result == 'skipped'" in gate
    assert "needs.source.outputs.source_epoch == 'legacy'" in gate
    assert "needs.historical-windows.result == 'success'" in gate
    assert "needs.windows-contract.result == 'skipped'" in gate
    writer = next(step for step in receipt["steps"] if step.get("name") == "Write source receipt")
    assert "config = load_config(raw)" in writer["run"]
    assert "validate_workflow(Path('.github/workflows/ci.yml').read_text(), config)" in writer["run"]
    assert "resolve(os.environ['SOURCE_COMMIT'], config)" in writer["run"]
    assert "'schema': 2" in writer["run"]
    assert "hashlib.sha256(raw).hexdigest()" in writer["run"]
    assert "test \"$(git rev-parse HEAD)\" = \"$CONTROL_COMMIT\"" in writer["run"]


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
    control = main if ref == "refs/heads/main" else candidate
    subprocess.run(["git", "checkout", "-q", "--detach", control], cwd=repo, check=True)
    result = subprocess.run(
        ["bash", "-c", _guard_script(workflow, step_name)], cwd=repo, capture_output=True, text=True,
        env={**os.environ, "TARGET": main if target == "main" else candidate, "DRY_RUN": "true",
             "CONTROL_REF": ref, "CONTROL_COMMIT": control, "GITHUB_OUTPUT": str(output)},
    )
    assert (result.returncode == 0) == allowed, result.stderr


def test_full_validator_checkouts_include_required_epoch_history():
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    for job in ("test", "lock"):
        steps = workflow["jobs"][job]["steps"]
        checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
        assert checkout.get("with", {}).get("fetch-depth") == "0", job
