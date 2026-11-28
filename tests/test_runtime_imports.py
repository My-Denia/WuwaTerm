from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_import_path_does_not_require_pypinyin():
    script = """
import builtins
import importlib

real_import = builtins.__import__

def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "pypinyin" or name.startswith("pypinyin."):
        raise ModuleNotFoundError("No module named 'pypinyin'")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = blocked_import

for module in ("wuwaterm.lookup", "wuwaterm.sentence", "wuwaterm.application", "wuwaterm.cli", "wuwaterm_api.app"):
    importlib.import_module(module)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
