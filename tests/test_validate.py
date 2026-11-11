"""Tests for the repository validation entry point (scripts/validate.py).

Pins the gate sequence, CLI flags, failure propagation, and coverage arguments
cheaply with subprocess execution stubbed out.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_validate_module() -> Any:
    script_path = ROOT / "scripts" / "validate.py"
    spec = importlib.util.spec_from_file_location("scripts.validate", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validate = _load_validate_module()


def test_server_steps_declared_sequence_and_structure() -> None:
    """The script defines the gate sequence and each step's metadata."""
    expected_order = [
        "hygiene",
        "architecture",
        "api-contract",
        "ruff",
        "typing",
        "pytest",
    ]
    step_names = [step.name for step in validate.SERVER_STEPS]
    assert step_names == expected_order

    for step in validate.SERVER_STEPS:
        assert isinstance(step, validate.Step)
        assert step.name.strip() != ""
        assert step.meaning.strip() != ""
        assert len(step.argv) > 0
        assert step.cwd == validate.ROOT

    # The direct Python script paths must exist on disk relative to repo root.
    for step in validate.SERVER_STEPS:
        if step.argv[0].endswith(".py"):
            assert (validate.ROOT / step.argv[0]).is_file(), f"Missing script {step.argv[0]}"


def test_list_prints_every_step_without_executing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """--list prints each step and its failure meaning; runner must never be called."""
    runner_mock = MagicMock()
    monkeypatch.setattr(validate.subprocess, "run", runner_mock)

    ret = validate.main(["--list"])
    assert ret == 0
    runner_mock.assert_not_called()

    captured = capsys.readouterr()
    for step in validate.SERVER_STEPS:
        assert step.name in captured.out
        assert step.meaning in captured.out
        assert f"python {' '.join(step.argv)}" in captured.out


def test_list_with_quick_excludes_pytest(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """--list combined with --quick prints only fast gates and executes nothing."""
    runner_mock = MagicMock()
    monkeypatch.setattr(validate.subprocess, "run", runner_mock)

    ret = validate.main(["--list", "--quick"])
    assert ret == 0
    runner_mock.assert_not_called()

    captured = capsys.readouterr()
    assert "pytest" not in captured.out
    assert "typing" in captured.out
    assert "ruff" in captured.out


def test_list_with_only_prints_target_step_only(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """--list combined with --only prints only that target step and executes nothing."""
    runner_mock = MagicMock()
    monkeypatch.setattr(validate.subprocess, "run", runner_mock)

    ret = validate.main(["--list", "--only", "typing"])
    assert ret == 0
    runner_mock.assert_not_called()

    captured = capsys.readouterr()
    assert "typing" in captured.out
    assert "pytest" not in captured.out
    assert "ruff" not in captured.out


def test_quick_excludes_pytest_suite_and_preserves_fast_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """--quick excludes the slow test suite while keeping all fast gates including typing."""
    executed_commands: list[list[str]] = []

    def fake_run(command: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
        executed_commands.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0)

    monkeypatch.setattr(validate.subprocess, "run", fake_run)

    ret = validate.main(["--quick"])
    assert ret == 0

    expected_fast_names = ["hygiene", "architecture", "api-contract", "ruff", "typing"]
    assert len(executed_commands) == len(expected_fast_names)

    for command, expected_step in zip(executed_commands, expected_fast_names):
        assert command[0] == sys.executable
        matching = [s for s in validate.SERVER_STEPS if s.name == expected_step][0]
        assert command[1:] == matching.argv

    # Ensure pytest step was never executed
    assert not any("-m" in cmd and "pytest" in cmd for cmd in executed_commands)


def test_quick_with_cov_fails_with_clear_message_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--quick combined with --cov is rejected by argument parsing; no steps run."""
    runner_mock = MagicMock()
    monkeypatch.setattr(validate.subprocess, "run", runner_mock)

    with pytest.raises(SystemExit) as exc_info:
        validate.main(["--quick", "--cov"])

    assert exc_info.value.code != 0
    runner_mock.assert_not_called()

    captured = capsys.readouterr()
    assert "--cov cannot be combined with --quick" in captured.err


def test_only_non_pytest_with_cov_fails_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--cov combined with --only on a non-pytest step is rejected; no steps run."""
    runner_mock = MagicMock()
    monkeypatch.setattr(validate.subprocess, "run", runner_mock)

    with pytest.raises(SystemExit) as exc_info:
        validate.main(["--only", "ruff", "--cov"])

    assert exc_info.value.code != 0
    runner_mock.assert_not_called()

    captured = capsys.readouterr()
    assert "--cov cannot be combined with --only 'ruff'" in captured.err


def test_only_runs_just_the_named_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """--only <step> runs solely that step."""
    executed_commands: list[list[str]] = []

    def fake_run(command: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
        executed_commands.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0)

    monkeypatch.setattr(validate.subprocess, "run", fake_run)

    ret = validate.main(["--only", "ruff"])
    assert ret == 0
    assert len(executed_commands) == 1
    assert executed_commands[0] == [sys.executable, "-m", "ruff", "check", "."]


def test_only_rejects_unknown_step_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """--only with an unrecognized step name raises SystemExit and names known steps."""
    runner_mock = MagicMock()
    monkeypatch.setattr(validate.subprocess, "run", runner_mock)

    with pytest.raises(SystemExit) as exc_info:
        validate.main(["--only", "not-a-step"])

    assert "validate: unknown step 'not-a-step'" in str(exc_info.value)
    assert "known steps:" in str(exc_info.value)
    assert "hygiene" in str(exc_info.value)
    runner_mock.assert_not_called()


def test_failing_step_stops_run_names_step_and_propagates_exit_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing step stops execution immediately and surfaces the step name and exit code."""
    call_log: list[str] = []

    def fake_run(command: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
        # Second step is 'architecture'
        if "scripts/check_architecture_boundaries.py" in command:
            call_log.append("architecture")
            return subprocess.CompletedProcess(args=command, returncode=42)
        call_log.append("hygiene")
        return subprocess.CompletedProcess(args=command, returncode=0)

    monkeypatch.setattr(validate.subprocess, "run", fake_run)

    ret = validate.main([])
    assert ret == 42
    assert call_log == ["hygiene", "architecture"]

    captured = capsys.readouterr()
    assert "validate: step 'architecture' FAILED with exit code 42" in captured.err
    arch_step = [s for s in validate.SERVER_STEPS if s.name == "architecture"][0]
    assert arch_step.meaning in captured.err


def test_failing_step_oserror_reports_and_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When a step cannot start (OSError), it prints the failure and returns 1."""
    def fake_run(command: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("interpreter not found")

    monkeypatch.setattr(validate.subprocess, "run", fake_run)

    ret = validate.main([])
    assert ret == 1

    captured = capsys.readouterr()
    assert "validate: step 'hygiene' could not start: interpreter not found" in captured.err


def test_cov_appends_coverage_arguments_only_to_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """--cov adds coverage args and alters failure meaning only for pytest."""
    executed_commands: list[list[str]] = []

    def fake_run(command: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
        executed_commands.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0)

    monkeypatch.setattr(validate.subprocess, "run", fake_run)

    ret = validate.main(["--cov"])
    assert ret == 0

    assert len(executed_commands) == len(validate.SERVER_STEPS)

    for step, cmd in zip(validate.SERVER_STEPS, executed_commands):
        assert cmd[0] == sys.executable
        if step.name == "pytest":
            assert cmd[1:] == ["-m", "pytest", "--cov=src", "--cov-report=term-missing"]
        else:
            assert cmd[1:] == step.argv
            assert "--cov=src" not in cmd
            assert "--cov-report=term-missing" not in cmd


def test_cov_with_only_pytest_appends_coverage_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """--only pytest --cov appends coverage flags."""
    executed_commands: list[list[str]] = []

    def fake_run(command: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
        executed_commands.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0)

    monkeypatch.setattr(validate.subprocess, "run", fake_run)

    ret = validate.main(["--only", "pytest", "--cov"])
    assert ret == 0
    assert len(executed_commands) == 1
    assert executed_commands[0] == [
        sys.executable,
        "-m",
        "pytest",
        "--cov=src",
        "--cov-report=term-missing",
    ]


def test_selected_steps_cov_updates_pytest_meaning_only() -> None:
    """selected_steps updates pytest failure meaning when cov=True."""
    args = argparse.Namespace(only=None, quick=False, cov=True)
    steps = validate.selected_steps(args)
    pytest_steps = [s for s in steps if s.name == "pytest"]
    assert len(pytest_steps) == 1
    assert pytest_steps[0].meaning == "the repository test suite is red or coverage falls below the floor"

    non_pytest_steps = [s for s in steps if s.name != "pytest"]
    for s in non_pytest_steps:
        original = [orig for orig in validate.SERVER_STEPS if orig.name == s.name][0]
        assert s.meaning == original.meaning


def test_all_steps_pass_reports_summary_and_passes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """When every step succeeds, validate prints progress and returns 0."""
    executed: list[list[str]] = []

    def fake_run(command: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
        executed.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0)

    monkeypatch.setattr(validate.subprocess, "run", fake_run)

    ret = validate.main([])
    assert ret == 0
    assert len(executed) == len(validate.SERVER_STEPS)

    captured = capsys.readouterr()
    assert f"validate: {len(validate.SERVER_STEPS)} steps passed" in captured.out
    for i, step in enumerate(validate.SERVER_STEPS, start=1):
        assert f"[{i}/{len(validate.SERVER_STEPS)}] {step.name}:" in captured.out
