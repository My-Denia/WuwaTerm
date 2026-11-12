#!/usr/bin/env python3
"""Fail the locked-dependency audit when an advisory is critical or high.

The division of labour this script exists for: `uv lock --check` (the `lock`
job) proves the lock resolves and matches `pyproject.toml`; it does not read
`[tool.uv] constraint-dependencies` (measured 2026-11-11, #217), so the certifi
floor added in #216 is a re-lock guard and not a gate. This script asks the
other question, against the same locked resolution: does the set carry a
published advisory? It does not replace the lock job and does not answer what
that job answers.

`pip-audit` names the advisory and the version that fixes it. It does not
report severity, so "fail on high and critical, report the rest" needs a
second source. That source is OSV, which is the database behind pip-audit's
PyPI service and behind the advisory pages those IDs link to. Lookups happen
only for advisories pip-audit actually found, so a clean run makes no request.

Severity comes from GitHub's label in the OSV record (`database_specific
.severity`), asked of a GHSA identifier first because that is the copy that
carries the label. An advisory whose label cannot be read is treated as
blocking: a version nobody can classify is not evidence of a clean set, and
this whole gate exists because silence used to look like safety (#217).

A clean report is also not the same claim as a complete one. pip-audit builds a
throwaway virtualenv for `-r`, upgrades pip inside it, and reads back only what
pip said it would install, so a pin pip answers "already satisfied" is never
audited and never mentioned (see BOOTSTRAP_BLIND_SPOT). The gate is therefore
also given the export the report was produced from, and joins the two: what the
export says should have been read and what the report says was read have to be
the same set. That join is what turns "no advisories" into "no advisories in a
set that was actually read".

Usage, as the workflow runs it:

    python scripts/check_dependency_advisories.py pip-audit.json \
        --audit-exit 1 --export requirements-locked.txt

Exit status: 0 no blocking advisory, 1 a critical or high advisory (or one
whose severity could not be read), 2 the audit cannot be trusted - which
includes a missing, empty or malformed report, a dependency pip-audit could not
collect, an export that is truncated or does not match the report made from it,
and a pip-audit exit status that says the run itself failed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple

OSV_VULN_URL = "https://api.osv.dev/v1/vulns/"
BLOCKING = ("critical", "high")
# GitHub spells its middle band `moderate`; `medium` is accepted as well
# because the same advisory is written both ways across the databases OSV
# aggregates. Both are reported and neither is blocking.
SEVERITIES = ("critical", "high", "moderate", "medium", "low")
UNKNOWN = "unknown"

# pip-audit does not audit the requirements it is given in place. It creates a
# virtualenv, runs `pip install --upgrade pip wheel setuptools` in it, and then
# reads the install list pip writes for `--dry-run --report`. A requirement pip
# answers "already satisfied" never reaches that list, so the distribution is
# never audited and never mentioned: the report and the export disagree silently.
#
# Measured 2026-11-12 on this repository's export (pip-audit 2.10.1, Python
# 3.11): that virtualenv holds packaging 26.3, pip 26.3, setuptools 84.2.1 and
# wheel 0.48.0. The committed lock pins packaging 26.2, one patch below the
# venv's own copy, so all 33 pins that select this platform are audited today
# and this set is empty. Pinning packaging 26.3, or adding setuptools or wheel
# at those versions, removes each of them from the report with no other change
# - a routine bump away, which is why a member of this set going missing is
# named on every run instead of being taken for coverage.
#
# It cannot be fatal for the same reason: this project does not control
# pip-audit's bootstrap venv, so a pin equal to a version it preinstalls cannot
# be audited by it at all and there would be no fix path. A pin outside this set
# going missing has a different cause - a truncated export, or a collector that
# dropped it - and that is fatal.
BOOTSTRAP_BLIND_SPOT = frozenset({"packaging", "pip", "setuptools", "wheel"})

# The pins every export of this lock contains: both adapters (the Telegram bot
# and the HTTP API), the HTTP client they share, and the CA bundle the runtime
# actually trusts. The join below cannot see a file that was truncated together
# with its report - both would agree on the smaller set - so the export is
# checked for these names before its report is believed.
# `.github/workflows/dependency-audit.yml` greps for the same names before the
# audit runs, so a truncated export never reaches pip-audit at all; the check is
# repeated here so the gate stands on its own, and
# tests/test_dependency_advisories.py compares the two lists so they cannot
# drift apart.
REQUIRED_PINS = frozenset({"certifi", "fastapi", "httpx", "python-telegram-bot", "uvicorn"})

# `uv export` writes one pin per line, optionally followed by the lock's
# environment marker. Each line here comes from that command, not from a human.
PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*(?P<version>[^;\s]+)"
    r"\s*(?:;\s*(?P<marker>.+))?$"
)

Fetch = Callable[[str], "dict | None"]


class AuditError(Exception):
    """The report cannot be trusted, so a green result would mean nothing."""


class Advisory(NamedTuple):
    package: str
    version: str
    advisory_id: str
    aliases: tuple[str, ...]
    fix_versions: tuple[str, ...]

    @property
    def identifiers(self) -> str:
        """Every ID a reader might search for, primary first."""
        others = [alias for alias in self.aliases if alias != self.advisory_id]
        return " ".join([self.advisory_id, *(f"({alias})" for alias in others)])


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def load_report(raw: bytes) -> list[Advisory]:
    """Read pip-audit's JSON report into one entry per (package, advisory)."""
    try:
        report = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditError(f"pip-audit report is not valid JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise AuditError("pip-audit report is not a JSON object")
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        raise AuditError(
            "pip-audit report lists no audited dependencies: the exported "
            "requirement set was empty or nothing was collected, and a green "
            "result over nothing is not a result"
        )
    advisories: list[Advisory] = []
    # pip-audit emits the same advisory once per source that reports it. It is
    # one finding; counting it twice would inflate every total in this report.
    seen: set[tuple[str, str, str]] = set()
    uncollected: list[str] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise AuditError("pip-audit report has a dependency that is not an object")
        name = dependency.get("name")
        version = dependency.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise AuditError("pip-audit report has a dependency without name or version")
        if dependency.get("skip_reason"):
            uncollected.append(f"{name} {version}: {dependency['skip_reason']}")
        vulns = dependency.get("vulns") or []
        if not isinstance(vulns, list):
            raise AuditError(f"pip-audit report has malformed advisories for {name}")
        for vuln in vulns:
            if not isinstance(vuln, dict) or not isinstance(vuln.get("id"), str):
                raise AuditError(f"pip-audit report has an advisory without an id for {name}")
            key = (name, version, vuln["id"])
            if key in seen:
                continue
            seen.add(key)
            advisories.append(
                Advisory(
                    package=name,
                    version=version,
                    advisory_id=vuln["id"],
                    aliases=_strings(vuln.get("aliases")),
                    fix_versions=_strings(vuln.get("fix_versions")),
                )
            )
    if uncollected:
        raise AuditError(
            "pip-audit could not audit part of the locked set, so the rest is "
            "not a complete answer:\n  " + "\n  ".join(uncollected)
        )
    return group_advisories(advisories)


def _identifiers(advisory: Advisory) -> set[str]:
    return {advisory.advisory_id, *advisory.aliases}


def group_advisories(advisories: list[Advisory]) -> list[Advisory]:
    """Collapse database records that describe one underlying vulnerability.

    The same issue is published as a PYSEC record, a GHSA record and a CVE, and
    each of those aliases the others. Two rows for one CVE in a report that is
    read to decide something is how a report teaches people to stop reading it,
    so records sharing an identifier for the same package version become one
    entry: the first reported ID, every alias, and every fix version seen.
    """
    groups: list[list[Advisory]] = []
    for advisory in advisories:
        identifiers = _identifiers(advisory)
        joined = [
            group
            for group in groups
            if any(
                item.package == advisory.package
                and item.version == advisory.version
                and identifiers & _identifiers(item)
                for item in group
            )
        ]
        if not joined:
            groups.append([advisory])
            continue
        joined[0].append(advisory)
        for other in joined[1:]:
            joined[0].extend(other)
            groups.remove(other)
    return [_merge(group) for group in groups]


def _merge(group: list[Advisory]) -> Advisory:
    first = group[0]
    aliases: list[str] = []
    fix_versions: list[str] = []
    for advisory in group:
        for identifier in (advisory.advisory_id, *advisory.aliases):
            if identifier != first.advisory_id and identifier not in aliases:
                aliases.append(identifier)
        fix_versions.extend(
            fix for fix in advisory.fix_versions if fix not in fix_versions
        )
    return Advisory(
        package=first.package,
        version=first.version,
        advisory_id=first.advisory_id,
        aliases=tuple(aliases),
        fix_versions=tuple(fix_versions),
    )


def _database_severity(record: object) -> str:
    """GitHub's severity label from an OSV record, or an empty string."""
    if not isinstance(record, dict):
        return ""
    database_specific = record.get("database_specific")
    if not isinstance(database_specific, dict):
        return ""
    label = str(database_specific.get("severity", "")).strip().lower()
    return label if label in SEVERITIES else ""


def lookup_order(advisory: Advisory) -> tuple[str, ...]:
    """Identifiers to ask for, cheapest first.

    A GHSA record carries GitHub's severity label; a PYSEC record usually
    aliases one and carries only a CVSS vector. Asking for the GHSA first is
    therefore one request instead of two for the common case.
    """
    ghsa = tuple(alias for alias in advisory.aliases if alias.startswith("GHSA-"))
    rest = tuple(alias for alias in advisory.aliases if not alias.startswith("GHSA-"))
    identifiers = [*ghsa, advisory.advisory_id, *rest]
    ordered: list[str] = []
    for identifier in identifiers:
        if identifier not in ordered:
            ordered.append(identifier)
    return tuple(ordered)


def resolve_severity(advisory: Advisory, fetch: Fetch) -> str:
    """Read the advisory's severity, or say that it could not be read."""
    for identifier in lookup_order(advisory):
        label = _database_severity(fetch(identifier))
        if label:
            return label
    return UNKNOWN


class OsvLookup:
    """Fetch one OSV advisory record per ID, remembering answers."""

    def __init__(self, base_url: str = OSV_VULN_URL, timeout: float = 30.0) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._cache: dict[str, dict | None] = {}

    def __call__(self, vuln_id: str) -> dict | None:
        if vuln_id in self._cache:
            return self._cache[vuln_id]
        url = self._base_url + urllib.parse.quote(vuln_id, safe="")
        request = urllib.request.Request(
            url, headers={"User-Agent": "wuwaterm-dependency-audit"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                record = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise AuditError(
                    f"OSV lookup for {vuln_id} failed with HTTP {exc.code}"
                ) from exc
            record = None
        except (OSError, ValueError) as exc:
            # URLError/Timeout are OSError; a non-JSON body is ValueError.
            raise AuditError(f"OSV lookup for {vuln_id} failed: {exc}") from exc
        self._cache[vuln_id] = record
        return record


def _describe_fix(advisory: Advisory) -> str:
    if advisory.fix_versions:
        return "fix: " + ", ".join(advisory.fix_versions)
    return "no fixed version published"


def _count(count: int, noun: str, plural: str | None = None) -> str:
    return f"{count} {noun if count == 1 else plural or noun + 's'}"


def _print_findings(classified: list[tuple[Advisory, str]], packages: int) -> None:
    print(f"Locked dependency audit: {_count(packages, 'package')} audited")
    if not classified:
        print("  no advisories reported for the locked set")
        return
    package_width = max(len(advisory.package) for advisory, _ in classified)
    print(f"  {'severity':<12}  {'package':<{package_width}}  {'version':<12}  advisory")
    for advisory, severity in classified:
        print(
            f"  {severity:<12}  {advisory.package:<{package_width}}  "
            f"{advisory.version:<12}  {advisory.identifiers}  "
            f"({_describe_fix(advisory)})"
        )
    counts = [severity for _, severity in classified]
    summary = ", ".join(
        f"{counts.count(label)} {label}"
        for label in (*SEVERITIES, UNKNOWN)
        if counts.count(label)
    )
    print(f"  totals: {_count(len(classified), 'advisory', 'advisories')} ({summary})")


def _sorted(classified: list[tuple[Advisory, str]]) -> list[tuple[Advisory, str]]:
    def sort_key(item: tuple[Advisory, str]) -> tuple[int, str, str]:
        advisory, severity = item
        rank = SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)
        return (rank, advisory.package, advisory.advisory_id)

    return sorted(classified, key=sort_key)


class Coverage(NamedTuple):
    """What the report covers of the export it was produced from."""

    applicable: int
    blind_spot: list[tuple[str, str]]
    other_platform: int


def _canonical(name: str) -> str:
    """PEP 503 normalisation, so `python-telegram-bot` and its variants agree."""
    return re.sub(r"[-_.]+", "-", name).lower()


def export_pins(raw: bytes, source: str) -> tuple[list[tuple[str, str]], int, set[str]]:
    """The pins an audit on this platform should read, how many it should not, and
    every distribution the file pins at all.

    `pip-audit` evaluates environment markers in the environment it runs in, so
    a pin selected only for `win32` or `darwin` is correctly absent from its
    report and must not be counted as a gap. This repository's export has one
    such pin today: `colorama`, which click and pytest need on Windows and
    nowhere else. Anything a human could not have written by way of
    `uv export` is a malformed or truncated file, and is an error rather than a
    skipped line.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuditError(f"export {source} is not UTF-8: {exc}") from exc
    try:
        from packaging.markers import InvalidMarker, Marker
    except ImportError as exc:  # the workflow runs the gate where pip-audit lives
        raise AuditError(
            "--export needs `packaging` to evaluate environment markers, and it "
            "could not be imported"
        ) from exc
    applicable: list[tuple[str, str]] = []
    other_platform = 0
    present: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = PIN.match(stripped)
        if match is None:
            raise AuditError(f"export {source} has an unreadable line: {stripped!r}")
        present.add(_canonical(match.group("name")))
        marker = match.group("marker")
        if marker is not None:
            try:
                selected = Marker(marker).evaluate()
            except InvalidMarker as exc:
                raise AuditError(
                    f"export {source} has an unreadable marker on "
                    f"{match.group('name')}: {exc}"
                ) from exc
            if not selected:
                other_platform += 1
                continue
        applicable.append((match.group("name"), match.group("version")))
    if not applicable:
        raise AuditError(
            f"export {source} selects no requirements for this platform: the "
            "export was empty or every marker excluded it"
        )
    return applicable, other_platform, present


def _require_whole_export(present: set[str], source: str) -> None:
    """Refuse a file that lacks pins every export of this lock carries.

    The join below compares the export against the report, and a file truncated
    before the audit produces a report truncated the same way: the two agree,
    and agreeing on less is exactly what a clean run looks like. The names are
    looked for in the whole file rather than among the pins selected for this
    platform, because a marker is not something truncation can remove.
    """
    absent = sorted(name for name in REQUIRED_PINS if name not in present)
    if absent:
        raise AuditError(
            f"export {source} does not contain {', '.join(absent)}, which every "
            "export of this lock contains: the file is truncated, or it was "
            "written by something other than the export step, and an audit of "
            "part of the locked set looks exactly like an audit of all of it"
        )


def coverage(export: bytes, source: str, raw_report: bytes) -> Coverage:
    """Join the export against the report, so a partial audit cannot look clean."""
    pins, other_platform, present = export_pins(export, source)
    _require_whole_export(present, source)
    reported = {_canonical(name) for name in _report_names(raw_report)}
    missing = [pin for pin in pins if _canonical(pin[0]) not in reported]
    blind_spot = [pin for pin in missing if _canonical(pin[0]) in BOOTSTRAP_BLIND_SPOT]
    unread = [pin for pin in missing if _canonical(pin[0]) not in BOOTSTRAP_BLIND_SPOT]
    if unread:
        listed = ", ".join(f"{name} {version}" for name, version in unread)
        raise AuditError(
            "pip-audit's report does not cover "
            f"{_count(len(unread), 'pinned requirement')} selected on this "
            f"platform: {listed}. A green result over a set nobody read is not "
            "a result"
        )
    return Coverage(len(pins), blind_spot, other_platform)


def _print_coverage(found: Coverage) -> None:
    print(
        f"  coverage: {found.applicable} locked requirements apply to this "
        f"platform; {found.other_platform} select another one"
    )
    if not found.blind_spot:
        return
    listed = ", ".join(f"{name} {version}" for name, version in found.blind_spot)
    print(f"  not read by pip-audit here (its bootstrap venv already has them): {listed}")
    print(
        "::warning title=Dependency audit::pip-audit does not read these pinned "
        f"distributions, so no advisory against them can be seen here: {listed}"
    )


def main(argv: list[str] | None = None, fetch: Fetch | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_dependency_advisories",
        description="Fail on critical or high advisories in a pip-audit JSON report.",
    )
    parser.add_argument("report", type=Path, help="pip-audit --format json output")
    parser.add_argument(
        "--audit-exit",
        type=int,
        default=0,
        help="pip-audit's exit status (1 means it found advisories, anything else "
        "means the audit itself failed)",
    )
    parser.add_argument(
        "--osv-url", default=OSV_VULN_URL, help="OSV advisory endpoint prefix"
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="OSV request timeout")
    parser.add_argument(
        "--export",
        type=Path,
        help="the requirements file the report was produced from; pins it holds "
        "that the report does not cover are printed, and a gap outside "
        "pip-audit's own bootstrap distributions is fatal",
    )
    args = parser.parse_args(argv)
    lookup = fetch if fetch is not None else OsvLookup(args.osv_url, args.timeout)

    try:
        if args.audit_exit not in (0, 1):
            raise AuditError(
                f"pip-audit exited {args.audit_exit}: the audit did not complete, "
                "so the report is not an answer"
            )
        try:
            raw = args.report.read_bytes()
        except OSError as exc:
            raise AuditError(f"report {args.report} could not be read: {exc}") from exc
        advisories = load_report(raw)
        found = None
        if args.export is not None:
            try:
                exported = args.export.read_bytes()
            except OSError as exc:
                raise AuditError(
                    f"export {args.export} could not be read: {exc}"
                ) from exc
            found = coverage(exported, str(args.export), raw)
        if args.audit_exit == 1 and not advisories:
            raise AuditError(
                "pip-audit exited 1 but the report lists no advisories; the report "
                "and the exit status disagree, so neither is trusted"
            )
        classified = _sorted(
            [(advisory, resolve_severity(advisory, lookup)) for advisory in advisories]
        )
    except AuditError as exc:
        print(f"dependency audit: {exc}", file=sys.stderr)
        print("::error title=Dependency audit::the audit could not be trusted; see the log")
        return 2

    _print_findings(classified, _package_count(raw))
    if found is not None:
        _print_coverage(found)
    blocking = [
        (advisory, severity)
        for advisory, severity in classified
        if severity in BLOCKING or severity == UNKNOWN
    ]
    for advisory, severity in blocking:
        reason = (
            "severity could not be read from OSV, so it is treated as blocking"
            if severity == UNKNOWN
            else f"{severity} advisory"
        )
        print(
            f"::error title={advisory.package} {advisory.advisory_id}::"
            f"{reason}; {_describe_fix(advisory)}"
        )
    if blocking:
        noun = "advisory" if len(blocking) == 1 else "advisories"
        print(
            f"FAILED: {len(blocking)} blocking {noun} in the locked dependency "
            "set. Raise the lock floor or record why the advisory does not apply; "
            "do not edit this gate to go green.",
            file=sys.stderr,
        )
        return 1
    print("PASSED: no critical, high or unclassified advisories in the locked set")
    return 0


def _report_names(raw: bytes) -> list[str]:
    """The distributions the report covers, as it names them.

    `load_report` has already refused a report whose entries lack a name, so an
    entry without one cannot exist here; this reader stays defensive anyway
    because it also serves the coverage join, which is about the file on disk
    rather than about a report the gate has decided to believe.
    """
    try:
        report = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    dependencies = report.get("dependencies") if isinstance(report, dict) else None
    if not isinstance(dependencies, list):
        return []
    return [
        dependency["name"]
        for dependency in dependencies
        if isinstance(dependency, dict) and isinstance(dependency.get("name"), str)
    ]


def _package_count(raw: bytes) -> int:
    """How many dependencies the report covers, for the summary line."""
    try:
        report = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0
    dependencies = report.get("dependencies") if isinstance(report, dict) else None
    return len(dependencies) if isinstance(dependencies, list) else 0


if __name__ == "__main__":
    sys.exit(main())
