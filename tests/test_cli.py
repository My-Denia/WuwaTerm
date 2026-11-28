from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from wuwaterm.cli import main


def test_build_db_atomic_flag_routes_to_atomic_builder(monkeypatch, capsys):
    calls = []

    def fake_build_database(*_args, **_kwargs):
        raise AssertionError("non-atomic builder should not be used")

    def fake_build_database_atomic(data_dir, db_path, profile_name=None):
        calls.append((data_dir, db_path, profile_name))
        return 7

    monkeypatch.setattr("wuwaterm.cli.build_database", fake_build_database)
    monkeypatch.setattr(
        "wuwaterm.cli.build_database_atomic", fake_build_database_atomic
    )

    assert (
        main(
            [
                "build-db",
                "--data-dir",
                "data-dir",
                "--db",
                "terms.db",
                "--profile",
                "arikatsu",
                "--atomic",
            ]
        )
        == 0
    )

    assert calls == [("data-dir", "terms.db", "arikatsu")]
    assert capsys.readouterr().out == "built 7 extracted records into terms.db\n"


def test_build_db_without_atomic_uses_regular_builder(monkeypatch):
    calls = []

    def fake_build_database(data_dir, db_path, profile_name=None):
        calls.append((data_dir, db_path, profile_name))
        return 3

    def fake_build_database_atomic(*_args, **_kwargs):
        raise AssertionError("atomic builder should not be used")

    monkeypatch.setattr("wuwaterm.cli.build_database", fake_build_database)
    monkeypatch.setattr(
        "wuwaterm.cli.build_database_atomic", fake_build_database_atomic
    )

    assert main(["build-db", "--data-dir", "data-dir", "--db", "terms.db"]) == 0

    assert calls == [("data-dir", "terms.db", None)]


def test_bot_command_exits_64_with_retirement_guidance(capsys):
    assert main(["bot"]) == 64
    err = capsys.readouterr().err
    assert "WuwaTerm-Telegram" in err
    assert "v0.16.0" in err

    assert main(["bot", "--db", "custom.db"]) == 64
    assert "WuwaTerm-Telegram" in capsys.readouterr().err


def test_cli_without_telegram_still_looks_up_terms_and_reports_bot_retired(sample_db):
    script = """
import builtins
import sys

real_import = builtins.__import__

def without_telegram(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "telegram" or name.startswith("telegram."):
        raise ModuleNotFoundError("No module named 'telegram'", name="telegram")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = without_telegram
from wuwaterm.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    env["PYTHONPATH"] = f"{root / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"

    def run_cli(*args):
        return subprocess.run(
            [sys.executable, "-c", script, *args],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    lookup = run_cli("lookup", "今汐", "--db", str(sample_db))
    assert lookup.returncode == 0, lookup.stderr
    assert "Jinhsi" in lookup.stdout

    help_result = run_cli("--help")
    assert help_result.returncode == 0, help_result.stderr
    assert "bot" not in help_result.stdout

    bot = run_cli("bot")
    assert bot.returncode == 64
    assert "WuwaTerm-Telegram" in bot.stderr
