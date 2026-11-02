#!/usr/bin/env python3
"""Select Windows coverage and verify a release source's trusted CI receipt."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")
BACKEND_JOBS = (
    "Resolve source",
    *(f"pytest (py{version})" for version in ("3.11", "3.12", "3.13", "3.14")),
    "uv locked runtime behavior (py3.11)",
    "API to Web contract",
    "wheel/sdist build, audit, install / build and smoke",
    "docker runtime/builder boundary",
    "validated source",
)
WINDOWS_JOB = "desktop client build (windows) / build and test"


def client_required(paths: list[str]) -> bool:
    """Skip Windows only for paths known not to affect its build or contract."""
    safe_exact = {
        "README.md", "README.zh-CN.md", "CHANGELOG.md", "CONTRIBUTING.md",
        "SUPPORT.md", ".github/PULL_REQUEST_TEMPLATE.md",
        "tests/test_release_workflow.py", "tests/test_ci_source.py",
        "tests/test_deploy_scripts.py", "tests/test_sentence.py",
    }
    for path in paths:
        if path in safe_exact or path.startswith(("deploy/", ".github/ISSUE_TEMPLATE/")):
            continue
        if path.startswith("docs/") and not path.startswith("docs/api/"):
            continue
        return True
    return False


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def gh_json(*args: str) -> object:
    return json.loads(subprocess.check_output(["gh", "api", *args], text=True))


def verify_record(
    run: dict, jobs: list[dict], receipt: dict, workflow_id: int,
    repository: str, source: str, run_id: int, *, require_client: bool = False,
) -> str:
    """Check API metadata, complete job coverage, and the bound artifact."""
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    require(bool(SHA.fullmatch(source)), "source is not a full commit SHA")
    require(run.get("id") == run_id and receipt.get("run_id") == run_id, "wrong run")
    require(run.get("workflow_id") == workflow_id, "wrong workflow")
    require(run.get("path", "").split("@", 1)[0] == ".github/workflows/ci.yml", "wrong workflow path")
    completed = run.get("status") == "completed" and run.get("conclusion") in {"success", "failure"}
    waiting_for_windows = (not require_client and run.get("status") == "in_progress"
                           and run.get("conclusion") is None)
    require(completed or waiting_for_windows,
            "CI run is incomplete or cancelled")
    require(run.get("event") in {"push", "workflow_dispatch"}, "untrusted CI event")
    require(run.get("head_branch") == "main", "CI control ref is not main")
    for field in ("repository", "head_repository"):
        require((run.get(field) or {}).get("full_name", "").lower() == repository.lower(), "wrong repository")
    require(receipt.get("schema") == 1, "wrong receipt schema")
    require(receipt.get("source_commit") == source, "wrong source SHA")
    control = receipt.get("control_commit", "")
    require(bool(SHA.fullmatch(control)) and run.get("head_sha") == control, "wrong control SHA")
    require(receipt.get("event") == run.get("event"), "wrong event in receipt")
    require(receipt.get("run_attempt") == run.get("run_attempt"), "wrong run attempt")
    require(isinstance(receipt.get("windows_required"), bool), "missing Windows scope")
    require(receipt.get("backend_scope") == "full", "incomplete backend scope")
    if run["event"] == "push":
        require(control == source, "push did not validate its own source")
    else:
        require(receipt["windows_required"], "historical dispatch was not full")
    by_name = {job.get("name"): job for job in jobs}
    require(len(by_name) == len(jobs), "ambiguous job names")
    for name in BACKEND_JOBS + ((WINDOWS_JOB,) if require_client else ()):
        job = by_name.get(name, {})
        require(job.get("status") == "completed" and job.get("conclusion") == "success", f"missing or unsuccessful job: {name}")
    incomplete = [job for job in jobs if job.get("status") != "completed"]
    failures = [job for job in jobs if job.get("status") == "completed"
                and job.get("conclusion") not in {"success", "skipped"}]
    if waiting_for_windows:
        # The receipt does not depend on Windows, but no other job may remain pending.
        require(receipt["windows_required"] and len(incomplete) == 1
                and incomplete[0].get("name") == WINDOWS_JOB
                and incomplete[0].get("status") in {"queued", "in_progress"}
                and incomplete[0].get("conclusion") is None, "incomplete job")
        require(not failures, "running CI contains an unsuccessful job")
    elif run["conclusion"] == "success":
        require(not incomplete, "incomplete job")
        require(not failures, "successful run contains an unsuccessful job")
    else:
        require(not incomplete, "incomplete job")
        require(len(failures) == 1 and failures[0].get("name") == WINDOWS_JOB
                and failures[0].get("conclusion") == "failure", "run failed for more than Windows")
    return control


def verify_source(source: str, run_id: str | None, *, require_client: bool = False) -> None:
    repository = os.environ["GH_REPO"]
    workflow = gh_json(f"repos/{repository}/actions/workflows/ci.yml")
    if run_id:
        if not run_id.isdecimal() or int(run_id) <= 0:
            raise ValueError("validation_run_id must be a positive run ID")
        candidates = [int(run_id)]
    else:
        listing = gh_json(
            f"repos/{repository}/actions/workflows/ci.yml/runs", "--method", "GET",
            "-f", "branch=main", "-f", "event=push",
            "-f", f"head_sha={source}", "-F", "per_page=100",
        )
        candidates = [item["id"] for item in listing["workflow_runs"]]
    failures = []
    for candidate in candidates:
        try:
            run = gh_json(f"repos/{repository}/actions/runs/{candidate}")
            pages = gh_json(f"repos/{repository}/actions/runs/{candidate}/jobs?per_page=100", "--paginate", "--slurp")
            jobs = [job for page in pages for job in page["jobs"]]
            with tempfile.TemporaryDirectory(prefix="validated-source-") as directory:
                subprocess.run(
                    ["gh", "run", "download", str(candidate), "--repo", repository,
                     "--name", f"validated-source-{run['run_attempt']}", "--dir", directory],
                    check=True, stdout=subprocess.DEVNULL,
                )
                receipt = json.loads((Path(directory) / "validated-source.json").read_text())
            control = verify_record(run, jobs, receipt, workflow["id"], repository, source, candidate,
                                    require_client=require_client)
            subprocess.run(["git", "merge-base", "--is-ancestor", control, "origin/main"], check=True)
            print(f"CI run {candidate} validated source {source} using main control {control}")
            return
        except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
            failures.append(f"{candidate}: {exc}")
    detail = "; ".join(failures) if failures else "no matching main push run"
    raise ValueError(
        f"No trusted CI receipt for {source} ({detail}). Each run attempt needs its own "
        "receipt, so use Re-run all jobs instead of Re-run failed jobs, or dispatch CI on "
        f"main with target_commit={source} and pass the completed run ID as validation_run_id."
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    paths = sub.add_parser("client-required")
    paths.add_argument("base")
    paths.add_argument("source")
    verify = sub.add_parser("verify")
    verify.add_argument("source")
    verify.add_argument("--run-id")
    verify.add_argument("--require-client", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "client-required":
            if not SHA.fullmatch(args.base) or not SHA.fullmatch(args.source) or args.base == "0" * 40:
                print("true")
            else:
                try:
                    names = git("diff", "--name-only", "--no-renames", args.base, args.source).splitlines()
                except subprocess.CalledProcessError:
                    # A rewritten or unavailable base cannot establish that Windows is unaffected.
                    print("true")
                else:
                    print(str(client_required(names)).lower())
        else:
            verify_source(args.source, args.run_id, require_client=args.require_client)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"CI source verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
