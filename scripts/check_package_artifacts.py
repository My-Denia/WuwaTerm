"""Audit built wheel/sdist artifacts for forbidden content and required members.

Release and CI gate: fails when a built distribution contains generated
databases, game data, runtime state, environment files, deployment internals,
or secret-looking files, when required package members are missing, or when
artifact metadata disagrees with the version declared in pyproject.toml.
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PROJECT_NAME = "wuwaterm"

# Declared versions must already be in canonical PEP 440 form so the audit can
# predict the exact dist-info/sdist directory names the build backend emits.
CANONICAL_VERSION_RE = re.compile(
    r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$"
)

# All name-based checks compare casefolded values so case variants such as
# terms.DB or TEXTMAP/ cannot slip past the gate.
FORBIDDEN_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".pem",
    ".p12",
    ".pfx",
)
FORBIDDEN_SEGMENTS = {
    "data",
    "state",
    "goal-runs",
    ".deployments",
    "deploy",
    "tests",
    "textmap",
    "textmaps",
    "configdb",
    "bindata",
}
FORBIDDEN_NAME_PREFIXES = (
    ".env",
    ".deploy_commit",
    "chat_settings.json",
    "channel_replies.json",
    ".chat_settings.",
    ".channel_replies.",
    "id_rsa",
)
# `wuwaterm_client` is the independent desktop client. It has its own
# distribution boundary; its appearance in a server distribution means the
# packaging configuration changed underneath us.
FORBIDDEN_NAME_MARKERS = ("wutheringdata", "credential", "wuwaterm_client")

# The public wheel deliberately ships BOTH adapters: the Telegram bot and the
# HTTP adapter package. Only generated data, game data and runtime state are
# out of bounds for a distribution, and neither adapter carries those. Listing
# the HTTP modules here keeps that decision explicit: if a packaging change
# ever drops wuwaterm_api from the wheel, this gate fails instead of silently
# shipping a `wuwaterm-api` entry point that cannot import its own package.
REQUIRED_WHEEL_MEMBERS = (
    "wuwaterm/__init__.py",
    "wuwaterm/cli.py",
    "wuwaterm/bot.py",
    "wuwaterm/application.py",
    "wuwaterm/protected_text.py",
    "wuwaterm_api/__init__.py",
    "wuwaterm_api/app.py",
    "wuwaterm_api/auth.py",
    "wuwaterm_api/cli.py",
    "wuwaterm_api/errors.py",
    "wuwaterm_api/settings.py",
    # The web presentation layer is a SUBPACKAGE, and subpackages are the thing
    # a packaging change drops most quietly: `packages.find` discovers it only
    # because it carries an __init__.py, and a switch to an explicit package
    # list - or an __init__.py lost in a refactor - would leave the parent
    # package shipping intact while this directory vanished. The failure that
    # produces is not an import error at build time but a 404 for the whole
    # surface on a deployed host, which is exactly the kind of thing this gate
    # says it exists to prevent.
    "wuwaterm_api/web/__init__.py",
    "wuwaterm_api/web/app.py",
    "wuwaterm_api/web/render.py",
    "wuwaterm_api/web/session.py",
)
REQUIRED_SDIST_MEMBERS = (
    "pyproject.toml",
    "PKG-INFO",
    "src/wuwaterm/__init__.py",
    "src/wuwaterm/cli.py",
    "src/wuwaterm/bot.py",
    "src/wuwaterm/application.py",
    "src/wuwaterm/protected_text.py",
    "src/wuwaterm_api/__init__.py",
    "src/wuwaterm_api/app.py",
    "src/wuwaterm_api/auth.py",
    "src/wuwaterm_api/cli.py",
    "src/wuwaterm_api/errors.py",
    "src/wuwaterm_api/settings.py",
    "src/wuwaterm_api/web/__init__.py",
    "src/wuwaterm_api/web/app.py",
    "src/wuwaterm_api/web/render.py",
    "src/wuwaterm_api/web/session.py",
)
ENTRY_POINT_LINES = (
    "wuwaterm = wuwaterm.cli:main",
    "wuwaterm-api = wuwaterm_api.cli:main",
)


def declared_version(pyproject_path: Path | None = None) -> str:
    path = pyproject_path or (ROOT / "pyproject.toml")
    with path.open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def forbidden_member_reason(member: str) -> str | None:
    path = PurePosixPath(member)
    name = path.name.casefold()
    lowered = member.casefold()
    if name.endswith(FORBIDDEN_SUFFIXES):
        return f"forbidden file type: {member}"
    for part in path.parts:
        if part.casefold() in FORBIDDEN_SEGMENTS:
            return f"forbidden path segment {part!r}: {member}"
    for prefix in FORBIDDEN_NAME_PREFIXES:
        if name.startswith(prefix):
            return f"forbidden file name: {member}"
    for marker in FORBIDDEN_NAME_MARKERS:
        if marker in lowered:
            return f"forbidden name marker {marker!r}: {member}"
    return None


def _metadata_version(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return None


def audit_wheel(path: Path, expected_version: str) -> list[str]:
    failures: list[str] = []
    dist_info = f"{PROJECT_NAME}-{expected_version}.dist-info"
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        for member in members:
            reason = forbidden_member_reason(member)
            if reason:
                failures.append(f"{path.name}: {reason}")
        for required in REQUIRED_WHEEL_MEMBERS:
            if required not in members:
                failures.append(f"{path.name}: missing required member: {required}")
        metadata_name = f"{dist_info}/METADATA"
        if metadata_name not in members:
            failures.append(
                f"{path.name}: missing {metadata_name}; wheel metadata does not"
                f" match declared version {expected_version}"
            )
        else:
            version = _metadata_version(
                archive.read(metadata_name).decode("utf-8", errors="replace")
            )
            if version != expected_version:
                failures.append(
                    f"{path.name}: METADATA version {version!r} !="
                    f" declared {expected_version!r}"
                )
        entry_points_name = f"{dist_info}/entry_points.txt"
        if entry_points_name not in members:
            failures.append(f"{path.name}: missing {entry_points_name}")
        else:
            entry_points = archive.read(entry_points_name).decode(
                "utf-8", errors="replace"
            )
            for line in ENTRY_POINT_LINES:
                if line not in entry_points:
                    failures.append(
                        f"{path.name}: entry_points.txt lacks {line!r}"
                    )
    return failures


def audit_sdist(path: Path, expected_version: str) -> list[str]:
    failures: list[str] = []
    root_dir = f"{PROJECT_NAME}-{expected_version}"
    seen: set[str] = set()
    pkg_info_text: str | None = None
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            raw = member.name
            if raw.startswith("/") or ".." in PurePosixPath(raw).parts:
                failures.append(f"{path.name}: unsafe member path: {raw}")
                continue
            if member.issym() or member.islnk():
                failures.append(f"{path.name}: link member not allowed: {raw}")
                continue
            parts = PurePosixPath(raw).parts
            if not parts:
                continue
            if parts[0] != root_dir:
                failures.append(
                    f"{path.name}: member outside {root_dir}/ (version drift?): {raw}"
                )
                continue
            rel = "/".join(parts[1:])
            if not rel:
                continue
            seen.add(rel)
            reason = forbidden_member_reason(rel)
            if reason:
                failures.append(f"{path.name}: {reason}")
            if rel == "PKG-INFO" and member.isfile():
                extracted = archive.extractfile(member)
                if extracted is not None:
                    pkg_info_text = extracted.read().decode("utf-8", errors="replace")
    for required in REQUIRED_SDIST_MEMBERS:
        if required not in seen:
            failures.append(f"{path.name}: missing required member: {required}")
    if pkg_info_text is not None:
        version = _metadata_version(pkg_info_text)
        if version != expected_version:
            failures.append(
                f"{path.name}: PKG-INFO version {version!r} !="
                f" declared {expected_version!r}"
            )
    return failures


def audit_artifact(path: Path, expected_version: str) -> list[str]:
    if path.name.endswith(".whl"):
        return audit_wheel(path, expected_version)
    if path.name.endswith((".tar.gz", ".tgz")):
        return audit_sdist(path, expected_version)
    return [f"{path.name}: unsupported artifact type"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument(
        "--expect-version",
        default=None,
        help="expected package version; defaults to pyproject.toml",
    )
    args = parser.parse_args(argv)

    expected = args.expect_version or declared_version()
    failures: list[str] = []
    if not CANONICAL_VERSION_RE.fullmatch(expected):
        failures.append(
            f"declared version {expected!r} is not canonical PEP 440; the"
            " audit cannot predict backend-normalized artifact names"
        )
    wheel_count = 0
    sdist_count = 0
    for artifact in args.artifacts:
        if not artifact.is_file():
            failures.append(f"missing artifact: {artifact}")
            continue
        if artifact.name.endswith(".whl"):
            wheel_count += 1
        elif artifact.name.endswith((".tar.gz", ".tgz")):
            sdist_count += 1
        failures.extend(audit_artifact(artifact, expected))
    if wheel_count == 0:
        failures.append("no wheel artifact was audited")
    if sdist_count == 0:
        failures.append("no sdist artifact was audited")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(
        f"package artifacts ok: {wheel_count} wheel(s), {sdist_count} sdist(s),"
        f" version {expected}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
