"""Run every offline gate this repository has, in one command.

The checks existed before this script did; what did not exist was one way to
run them. CI listed them as five separate workflow steps, the README listed
four of them, and `docs/validation.md` listed eleven POSIX-flavoured command
lines. Three lists drift: a gate added to CI is invisible to a contributor
reading the README, and a contributor who runs the README's list can be green
locally and red on the pull request. So the list lives HERE, once, and both CI
and the documentation point at this file instead of repeating it.

Standard library only, deliberately: this is the script someone runs BEFORE
they have worked out which extras to install, and it must be able to tell them
that the linter or the test runner is missing rather than fail on its own
imports. Server steps are spawned with ``sys.executable``, so the interpreter
that runs this script is the interpreter they run under - on Windows as well as
on Linux, where the documented `.venv/bin/python` prefix does not exist.

The working directory does not matter; the repository root is derived from this
file's location and every step is run there.

    python scripts/validate.py            # everything, in order
    python scripts/validate.py --list     # what it would run, runs nothing
    python scripts/validate.py --quick    # everything except the test suite
    python scripts/validate.py --only ruff

Exit status is 0 only when every selected step passed. The first failing step
stops the run and its name is printed, so the output says which gate failed
rather than leaving the reader to match a traceback to a command.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple


ROOT = Path(__file__).resolve().parents[1]


class Step(NamedTuple):
    name: str
    # What a failure means, printed when the step fails and by --list.
    meaning: str
    # Arguments after the interpreter.
    argv: list[str]
    cwd: Path


SERVER_STEPS: list[Step] = [
    Step(
        "hygiene",
        "generated data, a database blob or runtime state would be committed",
        ["scripts/check_repo_hygiene.py"],
        ROOT,
    ),
    Step(
        "architecture",
        "an import crosses a layer boundary the architecture forbids",
        ["scripts/check_architecture_boundaries.py"],
        ROOT,
    ),
    Step(
        "api-contract",
        "the committed docs/api/openapi.json no longer equals what the app generates",
        ["scripts/check_api_contract.py"],
        ROOT,
    ),
    Step(
        "ruff",
        "the linter reports an error under the rule set pinned in pyproject.toml",
        ["-m", "ruff", "check", "."],
        ROOT,
    ),
    Step(
        "typing",
        "the type checker reports an error outside the baseline in pyproject.toml",
        ["-m", "mypy", "src/wuwaterm"],
        ROOT,
    ),
    Step(
        "pytest",
        "the repository test suite is red",
        ["-m", "pytest"],
        ROOT,
    ),
]


def selected_steps(args: argparse.Namespace) -> list[Step]:
    steps = list(SERVER_STEPS)
    if args.only:
        by_name = {step.name: step for step in steps}
        if args.only not in by_name:
            raise SystemExit(
                f"validate: unknown step {args.only!r}; known steps: "
                + ", ".join(by_name)
            )
        return [by_name[args.only]]
    if args.quick:
        steps = [step for step in steps if step.name not in {"pytest"}]
    return steps


def describe(steps: list[Step]) -> None:
    width = max(len(step.name) for step in steps)
    for step in steps:
        where = (
            ""
            if step.cwd == ROOT
            else f"  (in {step.cwd.relative_to(ROOT).as_posix()}/)"
        )
        python = "python"
        print(f"{step.name.ljust(width)}  {python} {' '.join(step.argv)}{where}")
        print(f"{' ' * width}  fails when: {step.meaning}")


def run(steps: list[Step]) -> int:
    for position, step in enumerate(steps, start=1):
        name = step.name
        command = [sys.executable, *step.argv]
        print(f"[{position}/{len(steps)}] {name}: {' '.join(command)}", flush=True)
        started = time.monotonic()
        try:
            completed = subprocess.run(command, cwd=step.cwd, check=False)
        except OSError as exc:  # interpreter or working directory unusable
            print(f"validate: step {name!r} could not start: {exc}", file=sys.stderr)
            return 1
        elapsed = time.monotonic() - started
        if completed.returncode != 0:
            print(
                f"validate: step {name!r} FAILED with exit code "
                f"{completed.returncode} after {elapsed:.1f}s - {step.meaning}",
                file=sys.stderr,
            )
            return completed.returncode
        print(f"    ok ({elapsed:.1f}s)", flush=True)
    print(f"validate: {len(steps)} step{'' if len(steps) == 1 else 's'} passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate",
        description="Run the repository's offline gates in one command.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="print the steps and their commands, run nothing",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="skip the test suites; the fast gates only",
    )
    parser.add_argument(
        "--only",
        metavar="NAME",
        help="run a single step by name",
    )
    args = parser.parse_args(argv)

    steps = selected_steps(args)
    if args.list_only:
        describe(steps)
        return 0
    return run(steps)


if __name__ == "__main__":
    raise SystemExit(main())
