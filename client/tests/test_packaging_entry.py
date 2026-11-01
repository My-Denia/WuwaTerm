"""Exercise the client startup self-check in a fresh process."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

CLIENT_ROOT = Path(__file__).resolve().parents[1]

_SELF_CHECK_PROGRAM = """
from wuwaterm_client.app import SELF_CHECK_FLAG, run

raise SystemExit(run(["WuwaTerm", SELF_CHECK_FLAG]))
"""


def test_self_check_starts_up_and_exits_without_a_window() -> None:
    """Exercise startup in a fresh headless process, with its own Qt event loop."""
    pytest.importorskip("PySide6")
    pytest.importorskip("qasync")

    environment = dict(os.environ)
    # A child does not inherit pytest's `pythonpath` setting, and an editable
    # install is not something a test should assume about the machine.
    environment["PYTHONPATH"] = str(CLIENT_ROOT / "src")
    # conftest.py pins this for the parent; the child needs it in its own
    # environment, and this is the same headless platform build.ps1 uses
    # against the packaged artifact.
    environment["QT_QPA_PLATFORM"] = "offscreen"

    completed = subprocess.run(
        [sys.executable, "-c", _SELF_CHECK_PROGRAM],
        capture_output=True,
        text=True,
        env=environment,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
