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

Usage, as the workflow runs it:

    python scripts/check_dependency_advisories.py pip-audit.json --audit-exit 1

Exit status: 0 no blocking advisory, 1 a critical or high advisory (or one
whose severity could not be read), 2 the audit cannot be trusted - which
includes a missing, empty or malformed report, a dependency pip-audit could
not collect, and a pip-audit exit status that says the run itself failed.
"""

from __future__ import annotations

import argparse
import json
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
