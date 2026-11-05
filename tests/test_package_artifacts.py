from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

from scripts.check_package_artifacts import (
    audit_sdist,
    audit_wheel,
    declared_version,
    forbidden_member_reason,
    main,
)

VERSION = "9.9.9"
DIST_INFO = f"wuwaterm-{VERSION}.dist-info"
SDIST_ROOT = f"wuwaterm-{VERSION}"

WHEEL_METADATA = f"Metadata-Version: 2.1\nName: wuwaterm\nVersion: {VERSION}\n"
ENTRY_POINTS = (
    "[console_scripts]\n"
    "wuwaterm = wuwaterm.cli:main\n"
    "wuwaterm-api = wuwaterm_api.cli:main\n"
)


def build_wheel(
    path: Path,
    *,
    extra: dict[str, str] | None = None,
    version: str = VERSION,
    omit: set[str] | None = None,
) -> Path:
    dist_info = f"wuwaterm-{version}.dist-info"
    members = {
        "wuwaterm/__init__.py": "",
        "wuwaterm/cli.py": "def main():\n    return 0\n",
        "wuwaterm/bot.py": "",
        "wuwaterm/application.py": "",
        "wuwaterm/protected_text.py": "",
        "wuwaterm_api/__init__.py": "",
        "wuwaterm_api/app.py": "",
        "wuwaterm_api/auth.py": "",
        "wuwaterm_api/cli.py": "",
        "wuwaterm_api/errors.py": "",
        "wuwaterm_api/settings.py": "",
        "wuwaterm_api/web/__init__.py": "",
        "wuwaterm_api/web/app.py": "",
        "wuwaterm_api/web/render.py": "",
        "wuwaterm_api/web/session.py": "",
        f"{dist_info}/METADATA": WHEEL_METADATA,
        f"{dist_info}/entry_points.txt": ENTRY_POINTS,
        f"{dist_info}/RECORD": "",
    }
    members.update(extra or {})
    for name in omit or set():
        members.pop(name, None)
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return path


def build_sdist(
    path: Path,
    *,
    extra: dict[str, str] | None = None,
    version: str = VERSION,
    omit: set[str] | None = None,
) -> Path:
    root = f"wuwaterm-{version}"
    members = {
        f"{root}/pyproject.toml": "[project]\nname = 'wuwaterm'\n",
        f"{root}/PKG-INFO": WHEEL_METADATA,
        f"{root}/src/wuwaterm/__init__.py": "",
        f"{root}/src/wuwaterm/cli.py": "",
        f"{root}/src/wuwaterm/bot.py": "",
        f"{root}/src/wuwaterm/application.py": "",
        f"{root}/src/wuwaterm/protected_text.py": "",
        f"{root}/src/wuwaterm_api/__init__.py": "",
        f"{root}/src/wuwaterm_api/app.py": "",
        f"{root}/src/wuwaterm_api/auth.py": "",
        f"{root}/src/wuwaterm_api/cli.py": "",
        f"{root}/src/wuwaterm_api/errors.py": "",
        f"{root}/src/wuwaterm_api/settings.py": "",
        f"{root}/src/wuwaterm_api/web/__init__.py": "",
        f"{root}/src/wuwaterm_api/web/app.py": "",
        f"{root}/src/wuwaterm_api/web/render.py": "",
        f"{root}/src/wuwaterm_api/web/session.py": "",
    }
    members.update(extra or {})
    for name in omit or set():
        members.pop(name, None)
    with tarfile.open(path, "w:gz") as archive:
        for name, text in members.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def test_declared_version_matches_pyproject():
    root = Path(__file__).resolve().parents[1]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{declared_version()}"' in text


def test_forbidden_member_reasons():
    assert forbidden_member_reason("wuwaterm/terms.db")
    assert forbidden_member_reason("wuwaterm/dump.sqlite3")
    assert forbidden_member_reason("data/terms.db")
    assert forbidden_member_reason("state/chat_settings.json")
    assert forbidden_member_reason("TextMap/en/MultiText.json")
    assert forbidden_member_reason("Textmaps/en/MultiText.json")
    assert forbidden_member_reason("ConfigDB/RoleInfo.json")
    assert forbidden_member_reason("BinData/role.json")
    assert forbidden_member_reason("goal-runs/run/plan.md")
    assert forbidden_member_reason(".deployments/abc.json")
    assert forbidden_member_reason("deploy/vps-update.sh")
    assert forbidden_member_reason("tests/test_bot.py")
    assert forbidden_member_reason(".env")
    assert forbidden_member_reason(".env.example")
    assert forbidden_member_reason(".deploy_commit")
    assert forbidden_member_reason("chat_settings.json")
    assert forbidden_member_reason("channel_replies.json")
    assert forbidden_member_reason(".chat_settings.tmp")
    assert forbidden_member_reason("wuwaterm/server.pem")
    assert forbidden_member_reason("run.log")
    assert forbidden_member_reason("data/wutheringdata/README.md")
    assert forbidden_member_reason("vendor/WutheringData/file.json")
    assert forbidden_member_reason("vps.credential.xml")
    # The desktop client is never published; it must not ride along in a
    # server distribution if the packaging configuration ever widens.
    assert forbidden_member_reason("wuwaterm_client/app.py")
    assert forbidden_member_reason("src/wuwaterm_client/__init__.py")


def test_forbidden_member_is_case_insensitive():
    assert forbidden_member_reason("wuwaterm/terms.DB")
    assert forbidden_member_reason("wuwaterm/key.PEM")
    assert forbidden_member_reason("TEXTMAP/en/MultiText.json")
    assert forbidden_member_reason("textmap/en/MultiText.json")
    assert forbidden_member_reason("Data/terms.db")
    assert forbidden_member_reason(".ENV")


def test_forbidden_member_allows_normal_source():
    assert forbidden_member_reason("wuwaterm/__init__.py") is None
    assert forbidden_member_reason("wuwaterm/data_source.py") is None
    assert forbidden_member_reason("src/wuwaterm/db.py") is None
    assert forbidden_member_reason("README.md") is None
    assert forbidden_member_reason(f"{DIST_INFO}/METADATA") is None


def test_clean_wheel_and_sdist_pass(tmp_path: Path):
    wheel = build_wheel(tmp_path / f"wuwaterm-{VERSION}-py3-none-any.whl")
    sdist = build_sdist(tmp_path / f"wuwaterm-{VERSION}.tar.gz")
    assert audit_wheel(wheel, VERSION) == []
    assert audit_sdist(sdist, VERSION) == []
    assert (
        main([str(wheel), str(sdist), "--expect-version", VERSION]) == 0
    )


def test_wheel_with_forbidden_member_fails(tmp_path: Path):
    wheel = build_wheel(
        tmp_path / "bad.whl", extra={"wuwaterm/terms.db": "sqlite"}
    )
    failures = audit_wheel(wheel, VERSION)
    assert any("forbidden file type" in f for f in failures)


def test_wheel_missing_required_member_fails(tmp_path: Path):
    wheel = build_wheel(tmp_path / "bad.whl", omit={"wuwaterm/bot.py"})
    failures = audit_wheel(wheel, VERSION)
    assert any("missing required member: wuwaterm/bot.py" in f for f in failures)


def test_wheel_missing_the_web_subpackage_fails(tmp_path: Path):
    """The subpackage is the member a packaging change drops most quietly.

    `packages.find` discovers `wuwaterm_api.web` only because it carries an
    __init__.py. A switch to an explicit package list, or an __init__.py lost in
    a refactor, would leave the parent package shipping intact while this
    directory vanished - and the symptom would not be a build error but a 404
    for the whole browser surface on a deployed host. Named individually rather
    than trusting the parent's presence to imply it.
    """
    for member in (
        "wuwaterm_api/web/__init__.py",
        "wuwaterm_api/web/app.py",
        "wuwaterm_api/web/render.py",
        "wuwaterm_api/web/session.py",
    ):
        wheel = build_wheel(tmp_path / "bad.whl", omit={member})
        failures = audit_wheel(wheel, VERSION)
        assert any(f"missing required member: {member}" in f for f in failures), member


def test_wheel_version_mismatch_fails(tmp_path: Path):
    wheel = build_wheel(tmp_path / "bad.whl")
    failures = audit_wheel(wheel, "1.0.0")
    assert any("METADATA" in f or "dist-info" in f for f in failures)


def test_wheel_missing_entry_point_fails(tmp_path: Path):
    wheel = build_wheel(
        tmp_path / "bad.whl",
        extra={f"{DIST_INFO}/entry_points.txt": "[console_scripts]\n"},
    )
    failures = audit_wheel(wheel, VERSION)
    assert any("entry_points.txt lacks" in f for f in failures)


def test_sdist_with_forbidden_member_fails(tmp_path: Path):
    sdist = build_sdist(
        tmp_path / "bad.tar.gz",
        extra={f"{SDIST_ROOT}/data/terms.db": "sqlite"},
    )
    failures = audit_sdist(sdist, VERSION)
    assert failures


def test_sdist_env_file_fails(tmp_path: Path):
    sdist = build_sdist(
        tmp_path / "bad.tar.gz", extra={f"{SDIST_ROOT}/.env": "TOKEN=x"}
    )
    failures = audit_sdist(sdist, VERSION)
    assert any("forbidden file name" in f for f in failures)


def test_sdist_root_dir_version_drift_fails(tmp_path: Path):
    sdist = build_sdist(tmp_path / "bad.tar.gz", version="0.0.1")
    failures = audit_sdist(sdist, VERSION)
    assert any("outside" in f for f in failures)


def test_sdist_link_member_fails(tmp_path: Path):
    path = tmp_path / "bad.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        data = b""
        info = tarfile.TarInfo(f"{SDIST_ROOT}/PKG-INFO")
        info.size = 0
        archive.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(f"{SDIST_ROOT}/src/wuwaterm/evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    failures = audit_sdist(path, VERSION)
    assert any("link member not allowed" in f for f in failures)


def test_main_requires_wheel_and_sdist(tmp_path: Path):
    wheel = build_wheel(tmp_path / f"wuwaterm-{VERSION}-py3-none-any.whl")
    assert main([str(wheel), "--expect-version", VERSION]) == 1


def test_main_reports_missing_artifact(tmp_path: Path):
    assert main([str(tmp_path / "absent.whl"), "--expect-version", VERSION]) == 1


def test_main_rejects_noncanonical_version(tmp_path: Path):
    wheel = build_wheel(tmp_path / f"wuwaterm-{VERSION}-py3-none-any.whl")
    sdist = build_sdist(tmp_path / f"wuwaterm-{VERSION}.tar.gz")
    assert main([str(wheel), str(sdist), "--expect-version", "1.0-1"]) == 1
