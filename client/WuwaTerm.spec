# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the WuwaTerm desktop client.

One-folder build: client/build.ps1 drives
``pyinstaller client/WuwaTerm.spec`` and produces
``client/dist/WuwaTerm/WuwaTerm.exe``. No code signing.

The names below (Analysis, PYZ, EXE, COLLECT, SPECPATH) are injected into
this file's execution namespace by PyInstaller itself when it runs the spec;
they are not imports.
"""

from __future__ import annotations

import sys
from pathlib import Path

block_cipher = None

# PyInstaller resolves an extension module's dependent DLLs through the build
# machine's PATH. `_ssl.pyd` is taken from this interpreter, but its
# `libssl-3-x64.dll` / `libcrypto-3-x64.dll` are whatever PATH found first, so
# a machine with any other OpenSSL installed silently ships a mismatched pair
# and the built program dies at start-up with
# "ImportError: DLL load failed while importing _ssl" the first time anything
# constructs an HTTP client. Bind this interpreter's own copies explicitly.
#
# Where those files live differs between interpreters - a uv-managed CPython
# keeps them in DLLs/, other builds keep them beside python.exe, and the file
# name carries a different suffix per platform - so several locations are
# searched with a loose pattern. An interpreter with no OpenSSL beside it at
# all is not an error: it may be linked statically. The artifact's own
# --self-check is what actually proves the built program can open a
# connection, and it runs on every build.
def _openssl_binaries():
    roots = (
        Path(sys.base_prefix) / "DLLs",
        Path(sys.base_prefix),
        Path(sys.prefix) / "DLLs",
        Path(sys.prefix),
    )
    found = {}
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in ("libssl-*.dll", "libcrypto-*.dll"):
            for path in sorted(root.glob(pattern)):
                found.setdefault(path.name, path)
    return [(str(path), ".") for path in found.values()]


OPENSSL_BINARIES = _openssl_binaries()
if OPENSSL_BINARIES:
    print(f"WuwaTerm.spec: binding {len(OPENSSL_BINARIES)} OpenSSL runtime file(s)")
else:
    print(
        "WuwaTerm.spec: no OpenSSL runtime found beside this interpreter;"
        " relying on the dependency scan and the artifact self-check"
    )

CLIENT_ROOT = Path(SPECPATH)
SRC_ROOT = CLIENT_ROOT / "src"
# client/main.py, NOT wuwaterm_client/__main__.py: PyInstaller runs the entry
# script as a top-level module with no package context, so an entry that uses
# relative imports fails at start-up with "attempted relative import with no
# known parent package". See client/main.py.
ENTRY_POINT = CLIENT_ROOT / "main.py"

# The stylesheets are non-code files the application reads at run time,
# and a build that ships without them starts perfectly well and looks
# like an unstyled prototype - which the artifact's own --self-check cannot
# see, because it constructs the window and never looks at it.
#
# They are listed BY NAME rather than collected with a pattern. A pattern
# cannot be wrong, but it also cannot be reviewed: what is packaged would stop
# being visible in this committed file. The list is what
# client/tests/test_theme_resources.py compares against the directory, so a
# resource added without a line here is a red test rather than a silently
# unstyled build.
RESOURCES_DIR = SRC_ROOT / "wuwaterm_client" / "resources"
RESOURCES_TARGET = "resources"
RESOURCE_FILES = (
    "theme_light.qss",
    "theme_dark.qss",
)
RESOURCE_DATAS = [
    (str(RESOURCES_DIR / name), RESOURCES_TARGET) for name in RESOURCE_FILES
]

# Preserve the icon resource path used by theme.resource_path in frozen builds.
# The list includes the upstream license and provenance beside the SVGs.
ICON_FILES = (
    "LICENSE",
    "README.md",
    "SHA256SUMS",
    "arrow-right.svg",
    "book-open.svg",
    "check.svg",
    "chevron-down.svg",
    "chevron-left.svg",
    "chevron-right.svg",
    "circle-check.svg",
    "clipboard.svg",
    "copy.svg",
    "external-link.svg",
    "file-plus.svg",
    "file-text.svg",
    "folder-open.svg",
    "languages.svg",
    "link-2.svg",
    "menu.svg",
    "panel-right-close.svg",
    "panel-right-open.svg",
    "panel-right.svg",
    "panels-top-left.svg",
    "plus.svg",
    "redo-2.svg",
    "refresh-cw.svg",
    "rotate-ccw-clock.svg",
    "save-all.svg",
    "save.svg",
    "search.svg",
    "settings.svg",
    "undo-2.svg",
    "waves-horizontal.svg",
    "x.svg",
)
ICON_DATAS = [
    (str(RESOURCES_DIR / "icons" / name), str(Path(RESOURCES_TARGET) / "icons"))
    for name in ICON_FILES
]

# Qt 自带部件的中文:输入框右键菜单(撤销/剪切/复制/粘贴/全选)和标准按钮的
# 默认名。这些字从来不经过这个程序的任何一次 setText,所以静态门看不见它们,
# 缺了就是一个界面上一半中文一半英文。翻译文件随 PySide6 一起分发,从它自己
# 的 translations 目录取,不复制进仓库——复制一份就会和装着的 PySide6 版本各
# 走各的。目标目录必须是 PySide6/translations,因为运行时是靠 Qt 自己报告的
# 翻译路径找它的。
# 两个候选目录:这台机器上装的 PySide6 6.11 把 .qm 放在 PySide6/translations,
# 而别的构型(以及某些平台的轮子)放在 PySide6/Qt/translations。只写一个的话,
# 在另一种构型上这里会静默返回空列表——源码运行时照样是中文(运行时另有三处
# 搜索路径),打出来的包却是英文菜单,而所有测试都绿。所以两个都找。
_QT_TRANSLATION_DIRS = ("translations", "Qt/translations")


def _qt_translation_source():
    try:
        import PySide6
    except ImportError:
        return None
    root = Path(PySide6.__file__).parent
    for relative in _QT_TRANSLATION_DIRS:
        candidate = root / relative / "qtbase_zh_CN.qm"
        if candidate.is_file():
            return candidate
    return None


def _qt_translation_datas():
    source = _qt_translation_source()
    if source is None:
        return []
    return [(str(source), "PySide6/translations")]


QT_TRANSLATION_DATAS = _qt_translation_datas()

a = Analysis(
    [str(ENTRY_POINT)],
    pathex=[str(SRC_ROOT)],
    binaries=OPENSSL_BINARIES,
    datas=RESOURCE_DATAS + ICON_DATAS + QT_TRANSLATION_DATAS,
    hiddenimports=[
        "keyring.backends.Windows",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WuwaTerm",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WuwaTerm",
)
