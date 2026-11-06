#!/usr/bin/env python3
"""Verify a release source against an attempt's trusted CI receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")
CONFIG_PATH = "contracts/ci-source-epochs.json"
RESOLVER_PATH = "scripts/ci_source_epoch.py"
WORKFLOW_PATH = ".github/workflows/ci.yml"
COMMON_JOBS = frozenset((
    "Resolve source",
    *(f"pytest (py{version})" for version in ("3.11", "3.12", "3.13", "3.14")),
    "uv locked runtime behavior (py3.11)",
    "API to Web contract",
    "wheel/sdist build, audit, install / build and smoke",
    "docker runtime/builder boundary",
    "validated source",
))
BOUND_FIELDS = (
    "source_epoch", "windows_recipe_sha", "web_revision", "windows_revision", "required_jobs",
    "web_repository", "windows_repository", "windows_recipe_workflow",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def gh_json(*args: str) -> object:
    return json.loads(subprocess.check_output(["gh", "api", *args], text=True))


def verify_run(run: dict, workflow_id: int, repository: str, source: str, run_id: int) -> str:
    """Validate GitHub's run identity before fetching its policy or receipt."""
    require(bool(SHA.fullmatch(source)), "source is not a full commit SHA")
    require(type(run.get("id")) is int and run["id"] == run_id, "wrong run")
    require(type(workflow_id) is int and type(run.get("workflow_id")) is int
            and run["workflow_id"] == workflow_id, "wrong workflow")
    path = run.get("path")
    require(isinstance(path, str) and path.split("@", 1)[0] == ".github/workflows/ci.yml",
            "wrong workflow path")
    require(run.get("status") == "completed" and run.get("conclusion") == "success",
            "CI run is incomplete or unsuccessful")
    require(run.get("event") in {"push", "workflow_dispatch"}, "untrusted CI event")
    require(run.get("head_branch") == "main", "CI control ref is not main")
    for field in ("repository", "head_repository"):
        value = run.get(field)
        name = value.get("full_name") if isinstance(value, dict) else None
        require(isinstance(name, str) and name.lower() == repository.lower(), "wrong repository")
    control = run.get("head_sha", "")
    require(isinstance(control, str) and bool(SHA.fullmatch(control)), "invalid control SHA")
    attempt = run.get("run_attempt")
    require(type(attempt) is int and attempt > 0, "invalid run attempt")
    if run["event"] == "push":
        require(control == source, "push did not validate its own source")
    return control


def trusted_policy(control: str, source: str) -> tuple[str, dict]:
    """Read the workflow, resolver and config from the verified main CONTROL.

    The invoking release checkout may have a different policy. All three Git
    objects must exist in the actual CI control commit; a working-tree file is
    not a substitute. The resolver operates on this repository's Git object graph.
    """
    for commit in (control, source):
        require(isinstance(commit, str) and bool(SHA.fullmatch(commit)), "invalid commit SHA")
        subprocess.run(["git", "merge-base", "--is-ancestor", commit, "origin/main"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    raw = subprocess.check_output(["git", "show", f"{control}:{CONFIG_PATH}"])
    resolver = subprocess.check_output(["git", "show", f"{control}:{RESOLVER_PATH}"])
    workflow = subprocess.check_output(["git", "show", f"{control}:{WORKFLOW_PATH}"])
    with tempfile.TemporaryDirectory(prefix="ci-source-policy-") as directory:
        config_path = Path(directory) / "ci-source-epochs.json"
        script_path = Path(directory) / "ci_source_epoch.py"
        workflow_path = Path(directory) / "ci.yml"
        config_path.write_bytes(raw)
        script_path.write_bytes(resolver)
        workflow_path.write_bytes(workflow)
        output = subprocess.check_output(
            [sys.executable, "-I", str(script_path), "resolve", source, "--config", str(config_path),
             "--workflow", str(workflow_path)],
            text=True,
        )
    policy = json.loads(output)
    require(isinstance(policy, dict), "invalid resolved source policy")
    for field in BOUND_FIELDS:
        require(field in policy, f"resolved policy missing {field}")
    require(policy["source_epoch"] in {"legacy", "split"}, "invalid source epoch")
    for field in ("web_revision", "windows_recipe_sha"):
        require(isinstance(policy[field], str) and bool(SHA.fullmatch(policy[field])),
                f"invalid {field}")
    for field in ("web_repository", "windows_repository", "windows_recipe_workflow"):
        require(isinstance(policy[field], str) and bool(policy[field]), f"invalid {field}")
    windows = policy["windows_revision"]
    require(windows is None if policy["source_epoch"] == "legacy"
            else isinstance(windows, str) and bool(SHA.fullmatch(windows)),
            "invalid Windows consumer")
    names = policy["required_jobs"]
    require(isinstance(names, list) and all(isinstance(name, str) and name for name in names),
            "invalid required jobs")
    require(len(names) == len(set(names)), "duplicate required jobs")
    require(COMMON_JOBS <= set(names) and len(set(names) - COMMON_JOBS) == 1,
            "incomplete selected job policy")
    if policy["source_epoch"] == "split":
        require("API to Windows contract" in names, "missing split Windows contract")
    else:
        require("API to Windows contract" not in names, "wrong historical Windows job")
    return hashlib.sha256(raw).hexdigest(), policy


def verify_record(
    run: dict, jobs: list[dict], receipt: dict, workflow_id: int,
    repository: str, source: str, run_id: int, config_sha256: str, policy: dict,
) -> str:
    """Check attempt identity, recomputed receipt bindings and job coverage."""
    control = verify_run(run, workflow_id, repository, source, run_id)
    require(isinstance(receipt, dict), "invalid receipt")
    require(type(receipt.get("schema")) is int and receipt["schema"] == 2, "wrong receipt schema")
    require(type(receipt.get("run_id")) is int and receipt["run_id"] == run_id, "wrong run in receipt")
    require(receipt.get("source_commit") == source, "wrong source SHA")
    require(receipt.get("control_commit") == control, "wrong control SHA")
    require(receipt.get("event") == run.get("event"), "wrong event in receipt")
    require(type(receipt.get("run_attempt")) is int and receipt["run_attempt"] == run["run_attempt"],
            "wrong run attempt")
    require(receipt.get("backend_scope") == "full", "incomplete backend scope")
    require(receipt.get("control_config_sha256") == config_sha256, "wrong control config digest")
    for field in BOUND_FIELDS:
        require(field in receipt and receipt[field] == policy[field], f"wrong or missing {field}")
    require(isinstance(jobs, list) and all(isinstance(job, dict) for job in jobs), "invalid jobs")
    by_name = {job.get("name"): job for job in jobs}
    require(len(by_name) == len(jobs), "ambiguous job names")
    for name in policy["required_jobs"]:
        job = by_name.get(name, {})
        require(job.get("status") == "completed" and job.get("conclusion") == "success",
                f"missing or unsuccessful job: {name}")
    require(all(job.get("status") == "completed" for job in jobs), "incomplete job")
    require(all(job.get("conclusion") in {"success", "skipped"} for job in jobs),
            "successful run contains an unsuccessful job")
    return control


def verify_source(source: str, run_id: str | None) -> None:
    require(isinstance(source, str) and bool(SHA.fullmatch(source)), "source is not a full commit SHA")
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
            control = verify_run(run, workflow["id"], repository, source, candidate)
            config_sha256, policy = trusted_policy(control, source)
            attempt = run["run_attempt"]
            pages = gh_json(
                f"repos/{repository}/actions/runs/{candidate}/attempts/{attempt}/jobs?per_page=100",
                "--paginate", "--slurp",
            )
            jobs = [job for page in pages for job in page["jobs"]]
            with tempfile.TemporaryDirectory(prefix="validated-source-") as directory:
                subprocess.run(
                    ["gh", "run", "download", str(candidate), "--repo", repository,
                     "--name", f"validated-source-{attempt}", "--dir", directory],
                    check=True, stdout=subprocess.DEVNULL,
                )
                receipt = json.loads((Path(directory) / "validated-source.json").read_text())
            verify_record(run, jobs, receipt, workflow["id"], repository, source, candidate,
                          config_sha256, policy)
            print(f"CI run {candidate}, attempt {attempt}, validated source {source} using main control {control}")
            return
        except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
            failures.append(f"{candidate}: {exc}")
    detail = "; ".join(failures) if failures else "no matching main push run"
    raise ValueError(
        f"No trusted CI receipt for {source} ({detail}). Dispatch current CI on main with "
        f"target_commit={source}; wait until the entire run completes successfully, then "
        "separately verify that attempt's receipt with its run ID."
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("source")
    verify.add_argument("--run-id")
    args = parser.parse_args()
    try:
        verify_source(args.source, args.run_id)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"CI source verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
