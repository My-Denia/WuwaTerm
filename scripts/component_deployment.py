#!/usr/bin/env python3
"""Coordinate component deployment commands and saved-source recovery."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType


def main(argv: list[str] | None = None, *, runtime: ModuleType) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-journal")
    validate.add_argument("--journal", required=True, type=Path)
    validate_full = commands.add_parser("validate-full-journal", help=argparse.SUPPRESS)
    validate_full.add_argument("--journal", required=True, type=Path)
    validate_probe = commands.add_parser("validate-probe", help=argparse.SUPPRESS)
    validate_probe.add_argument("--probe-id", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--component", choices=("api", "telegram"))
    inspect.add_argument("--container-id")
    observe = commands.add_parser("observe")
    observe.add_argument("--container-id", required=True)
    observe.add_argument("--image-id", required=True)
    observe.add_argument("--project", required=True)
    observe.add_argument("--service", required=True)
    observe.add_argument("--command-json", required=True)
    observe.add_argument("--entrypoint-json", required=True)
    adopt_command = commands.add_parser("adopt")
    adopt_command.add_argument("--project", action="append", required=True)
    adopt_command.add_argument("--compose-executable", type=Path, required=True)
    adopt_command.add_argument("--api-receipt")
    adopt_command.add_argument("--telegram-receipt")
    adopt_command.add_argument("--expected-sha")
    refresh = commands.add_parser(
        "refresh-runtime-options",
        help="record the API memory reservation and OOM score that an earlier adoption lacks")
    refresh.add_argument("--container-id", required=True, help="full ID of the running recorded or recreated API")
    refresh.add_argument("--compose-executable", type=Path, required=True)
    refresh.add_argument("--expected-sha")
    probe_recovery = commands.add_parser("recover-probe")
    probe_recovery.add_argument("--probe-id", required=True)
    probe_recovery.add_argument("--compose-executable", type=Path, required=True)
    update = commands.add_parser("runtime-update", aliases=["update"])
    update.add_argument("--components", choices=("api", "telegram", "both"), required=True)
    update.add_argument("--api-receipt", help=argparse.SUPPRESS)
    update.add_argument("--telegram-receipt", help=argparse.SUPPRESS)
    update.add_argument("--api-artifact", type=Path, help="registered completed-artifact acquisition receipt")
    update.add_argument("--telegram-artifact", type=Path, help="registered completed-artifact acquisition receipt")
    update.add_argument("--compose-executable", type=Path, required=True)
    update.add_argument("--expected-sha")
    full_update = commands.add_parser("full-update")
    full_update.add_argument("--candidate-db", type=Path, required=True)
    full_update.add_argument("--components", choices=("none", "api", "telegram", "both"), default="none")
    full_update.add_argument("--api-receipt", help=argparse.SUPPRESS)
    full_update.add_argument("--telegram-receipt", help=argparse.SUPPRESS)
    full_update.add_argument("--api-artifact", type=Path, help="registered completed-artifact acquisition receipt")
    full_update.add_argument("--telegram-artifact", type=Path, help="registered completed-artifact acquisition receipt")
    full_update.add_argument("--compose-executable", type=Path, required=True)
    full_update.add_argument("--expected-sha")
    full_update.add_argument(
        "--allow-knowledge-degrade",
        action="store_true",
        default=False,
        help="proceed even if companion knowledge.db is missing or mismatched with the candidate terms.db",
    )
    for command in (update, full_update):
        for name in ("api", "telegram"):
            command.add_argument(f"--{name}-compose-file", action="append", default=[])
            command.add_argument(f"--{name}-project-directory")
            command.add_argument(f"--{name}-project")
            command.add_argument(f"--{name}-service")
            command.add_argument(f"--{name}-interpolation-env-file", action="append", default=[])
    build_command = commands.add_parser("build")
    build_types = build_command.add_subparsers(dest="build_component", required=True)
    api_build = build_types.add_parser("api")
    api_build.add_argument("--expected-sha")
    telegram_build = build_types.add_parser("telegram")
    telegram_build.add_argument("--expected-sha")
    telegram_build.add_argument("--telegram-repo", type=Path, required=True)
    telegram_build.add_argument("--telegram-commit", required=True)
    telegram_build.add_argument("--core-wheel", type=Path, required=True)
    telegram_build.add_argument("--core-artifact", type=Path, required=True)
    telegram_build.add_argument("--dependencies", type=Path, required=True)
    telegram_build.add_argument("--build-tool-wheelhouse", type=Path, required=True)
    telegram_build.add_argument("--core-source-repo", type=Path)
    telegram_build.add_argument("--core-checksums", type=Path)
    cancel = commands.add_parser("cancel-build")
    cancel.add_argument("--kind", choices=("source", "wheel"), required=True)
    cancel.add_argument("--build-id", required=True)
    recover = commands.add_parser("recover")
    recover.add_argument("--kind", choices=("runtime", "full"), default="runtime")
    recover.add_argument("--transaction-id", required=True)
    recover.add_argument("--compose-executable", type=Path, required=True)
    internal = commands.add_parser("capsule-recover", help=argparse.SUPPRESS)
    internal.add_argument("--transaction-id", required=True)
    internal.add_argument("--capsule-source-tool", required=True)
    internal.add_argument("--compose-executable", type=Path, required=True)
    internal_full = commands.add_parser("capsule-recover-full", help=argparse.SUPPRESS)
    internal_full.add_argument("--transaction-id", required=True)
    internal_full.add_argument("--capsule-source-tool", required=True)
    internal_full.add_argument("--compose-executable", type=Path, required=True)
    internal_probe = commands.add_parser("capsule-recover-probe", help=argparse.SUPPRESS)
    internal_probe.add_argument("--probe-id", required=True)
    internal_probe.add_argument("--capsule-source-tool", required=True)
    internal_probe.add_argument("--compose-executable", type=Path, required=True)
    validate_build = commands.add_parser("validate-build-attempt", help=argparse.SUPPRESS)
    validate_build.add_argument("--kind", choices=("source", "wheel"), required=True)
    validate_build.add_argument("--build-id", required=True)
    cancel_internal = commands.add_parser("capsule-cancel-build", help=argparse.SUPPRESS)
    cancel_internal.add_argument("--kind", choices=("source", "wheel"), required=True)
    cancel_internal.add_argument("--build-id", required=True)
    cancel_internal.add_argument("--capsule-source-tool", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-journal":
            if not args.journal.is_absolute():
                raise runtime.JournalError("journal path must be absolute")
            runtime.load_journal(args.journal)
            return 0
        if args.command == "validate-full-journal":
            import component_full as full  # noqa: PLC0415
            if not args.journal.is_absolute():
                raise runtime.JournalError("full journal path must be absolute")
            try:
                full.load_journal(args.journal)
            except full.FullError as exc:
                raise runtime.JournalError("full journal is invalid") from exc
            return 0
        if args.command == "observe":
            try:
                command = json.loads(args.command_json)
                entrypoint = json.loads(args.entrypoint_json)
            except (ValueError, TypeError) as exc:
                raise runtime.JournalError("command and entrypoint must be JSON arrays or null") from exc
            print(json.dumps(runtime.observe_unrecorded(args.container_id, args.image_id, args.project, args.service,
                                               command, entrypoint), sort_keys=True))
            return 0
        if args.command == "inspect":
            if args.root is None:
                raise runtime.JournalError("deployment root is required")
            if (args.component is None) != (args.container_id is None):
                raise runtime.JournalError("component and container ID must be supplied together")
            result = (runtime.inspect_deployment(args.root) if args.component is None else
                      runtime.inspect_container(args.root, args.component, args.container_id))
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.root is None:
            raise runtime.JournalError("deployment root is required")
        root = Path(os.path.abspath(os.fspath(args.root)))
        if args.command == "capsule-cancel-build":
            import component_recovery as recovery  # noqa: PLC0415
            # Producer attempts are protocol-1 only.
            if runtime._capsule_protocol(full=False) != 1 or not runtime.capsule_execution_ready(full=False):
                runtime._fail("saved-source builder cancellation is not yet enabled")
            try:
                root = runtime.admission._physical_root(root)
                raw, attempt = recovery._build_attempt(root, args.kind, args.build_id)
                source = attempt["recovery_source"]
                if source["source_tool_sha256"] != args.capsule_source_tool:
                    runtime._fail("builder cancellation source identity differs")
                runtime.admission._verify_recovery_extraction(root, source, runtime.CODE_ROOT)
                import component_build_isolation as isolation  # noqa: PLC0415
                import component_build as build  # noqa: PLC0415
                if Path(isolation.__file__).resolve() != runtime.CODE_ROOT / "scripts/component_build_isolation.py":
                    runtime._fail("builder cancellation imported a foreign isolation helper")
                if recovery._build_attempt(root, args.kind, args.build_id)[0] != raw:
                    runtime._fail("builder attempt changed before cancellation")
                recovery._verify_bootstrap(root, source)
                try:
                    result = isolation.request_cancel(root, args.kind, args.build_id)
                except (isolation.IsolationError, build.BuildError) as exc:
                    raise runtime.JournalError("builder cancellation refused or remains unresolved") from exc
                if (type(result) is not dict or set(result) != {"build_id", "status"}
                        or result["build_id"] != args.build_id
                        or result["status"] not in ("requested", "failed", "retained_import", "registered")):
                    runtime._fail("builder cancellation returned an invalid outcome")
            except (runtime.admission.AdmissionError, recovery.RecoveryError) as exc:
                raise runtime.JournalError("builder cancellation refused or remains unresolved") from exc
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "validate-build-attempt":
            import component_build as build  # noqa: PLC0415
            import component_wheel_build as wheel  # noqa: PLC0415
            identifier = runtime._match(args.build_id, runtime._DEPLOYMENT_ID, "build ID")
            try:
                (build._read_attempt if args.kind == "source" else wheel.read_attempt)(root, identifier + ".json")
            except build.BuildError as exc:
                raise runtime.JournalError("build attempt cannot be verified") from exc
            return 0
        if args.command == "validate-probe":
            identifier = runtime._match(args.probe_id, runtime._DEPLOYMENT_ID, "probe ID")
            pending = runtime.pending_adopt_probe_staging(root)
            if pending is not None and pending != identifier:
                runtime._fail("another probe staging claim requires its own recovery")
            record = runtime._read_adopt_probe(root, identifier + ".json", _pre_reconcile=pending is not None)
            if record["kind"] != "legacy-core-version":
                runtime._fail("standalone recovery is limited to a version probe")
            return 0
        if args.command == "capsule-recover-probe":
            # The declared protocol must be supported AND its own flag open.
            if not runtime.capsule_execution_ready(full=False):
                runtime._fail("capsule recovery execution is not yet available")
            runtime.recover_adopt_probe(root, args.probe_id, compose_path=args.compose_executable,
                                capsule_source_tool=args.capsule_source_tool)
            record = runtime._read_adopt_probe(root, args.probe_id + ".json")
            status = "completed" if record["status"] == "completed" else "failed-retryable"
            print(json.dumps({"probe_id": args.probe_id, "status": status}, sort_keys=True))
            return 0
        if args.command == "capsule-recover":
            import component_transaction as transaction  # noqa: PLC0415
            if not runtime.capsule_execution_ready(full=False):
                runtime._fail("capsule recovery execution is not yet available")
            try:
                result = transaction.recover(root, args.transaction_id, args.capsule_source_tool,
                                             compose_path=args.compose_executable)
            except transaction.TransactionError as exc:
                raise runtime.JournalError("recorded transaction recovery refused or remains unresolved") from exc
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "capsule-recover-full":
            if not runtime.capsule_execution_ready(full=True):
                runtime._fail("full capsule recovery execution is not yet available")
            import component_full as full  # noqa: PLC0415
            try:
                result = full.recover(root, args.transaction_id, args.capsule_source_tool,
                                      compose_path=args.compose_executable)
            except full.FullError as exc:
                raise runtime.JournalError("full transaction recovery refused or remains unresolved") from exc
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "recover":
            import component_recovery as recovery  # noqa: PLC0415
            runtime.admission.require_lock(root)
            try:
                _, record = (recovery._full_journal(root, args.transaction_id) if args.kind == "full" else
                             recovery._journal(root, args.transaction_id))
                recovery._verify_bootstrap(root, record["source"])
            except recovery.RecoveryError as exc:
                raise runtime.JournalError("recorded transaction bootstrap cannot be verified") from exc
            source = record["source"]
            script = root / source["bootstrap"]["path"] / "component_recovery.py"
            kind = "full" if args.kind == "full" else "adopt" if record["action"] == "adopt" else "runtime"
            process = subprocess.run(
                [sys.executable, "-I", "-B", str(script), "--root", str(root), "--kind", kind,
                 "--id", args.transaction_id, "--compose-executable", str(args.compose_executable)],
                env=recovery._isolated_env(),
                pass_fds=(9,), check=False,
            )
            return process.returncode
        if args.command == "adopt":
            import component_recovery as recovery  # noqa: PLC0415
            runtime.admission.admit_component(root, operation="adopt", code_root=runtime.CODE_ROOT)
            if not runtime.capsule_execution_ready(full=False):
                runtime._fail("adoption is unavailable until saved-source recovery is enabled")
            deployment_id = runtime.adopt(args.root, projects=args.project, compose_executable=args.compose_executable,
                                  api_receipt=args.api_receipt, telegram_receipt=args.telegram_receipt,
                                  expected_sha=args.expected_sha)
            print(json.dumps({"deployment_id": deployment_id, "polling_health": "unconfirmed"}, sort_keys=True))
            return 0
        if args.command == "refresh-runtime-options":
            runtime.admission.admit_component(root, operation=args.command, code_root=runtime.CODE_ROOT)
            if not runtime.capsule_execution_ready(full=False):
                runtime._fail("record refresh is unavailable until saved-source recovery is enabled")
            result = runtime.refresh_runtime_options(
                args.root, container_id=args.container_id, compose_executable=args.compose_executable,
                expected_sha=args.expected_sha)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "recover-probe":
            import component_recovery as recovery  # noqa: PLC0415
            runtime.admission.require_lock(root)
            try:
                _, record = recovery._probe(root, args.probe_id)
                recovery._verify_bootstrap(root, record["source"])
            except recovery.RecoveryError as exc:
                raise runtime.JournalError("recorded version probe bootstrap cannot be verified") from exc
            script = root / record["source"]["bootstrap"]["path"] / "component_recovery.py"
            process = subprocess.run(
                [sys.executable, "-I", "-B", str(script), "--root", str(root), "--kind", "versionprobe",
                 "--id", args.probe_id, "--compose-executable", str(args.compose_executable)],
                env=recovery._isolated_env(),
                pass_fds=(9,), check=False,
            )
            return process.returncode
        if args.command == "cancel-build":
            import component_recovery as recovery  # noqa: PLC0415
            try:
                root = recovery._root(root)
                _, record = recovery._build_attempt(root, args.kind, args.build_id)
                recovery._verify_bootstrap(root, record["recovery_source"])
            except recovery.RecoveryError as exc:
                raise runtime.JournalError("recorded builder bootstrap cannot be verified") from exc
            script = root / record["recovery_source"]["bootstrap"]["path"] / "component_recovery.py"
            process = subprocess.run(
                [sys.executable, "-I", "-B", str(script), "--root", str(root),
                 "--kind", "sourcebuild" if args.kind == "source" else "wheelbuild", "--id", args.build_id],
                env=recovery._isolated_env(), check=False,
            )
            return process.returncode
        if args.command == "build":
            runtime.admission.admit_component(root, operation="build", code_root=runtime.CODE_ROOT)
            if not runtime.PRODUCER_EXECUTION_READY:
                runtime._fail("component builds are unavailable until the builder lifecycle is supported")
            import component_build as build  # noqa: PLC0415
            import component_wheel_build as wheel  # noqa: PLC0415
            import component_recovery as recovery  # noqa: PLC0415
            import package_recovery_source as capsule  # noqa: PLC0415
            attempt_kind = "source" if args.build_component == "api" else "wheel"
            attempt_directory = root / ".deployments" / (
                runtime.admission.BUILD_ATTEMPTS if attempt_kind == "source" else runtime.admission.WHEEL_BUILD_ATTEMPTS)
            before_attempts = ({item.name for item in attempt_directory.iterdir()}
                               if attempt_directory.exists() else set())
            try:
                commit = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{commit}", lock_root=root).decode("ascii").strip()
                tree = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{tree}", lock_root=root).decode("ascii").strip()
                if args.expected_sha is not None and runtime._match(args.expected_sha, runtime._HEX40, "expected source commit") != commit:
                    runtime._fail("source commit differs from --expected-sha")
                if args.build_component == "api":
                    digest = build.build(root, commit, tree)
                else:
                    revision = runtime._match(args.telegram_commit, runtime._HEX40, "Telegram source commit")
                    repo = args.telegram_repo
                    if not repo.is_absolute() or repo.resolve(strict=True) != repo or not repo.is_dir():
                        runtime._fail("Telegram source repository is not a physical absolute directory")
                    telegram_tree = recovery.safe_git(repo, "rev-parse", "--verify", revision + "^{tree}",
                                                      lock_root=root).decode("ascii").strip()
                    digest = wheel.build_wheel_image(root, commit, tree, repo, revision, telegram_tree,
                                                     args.core_wheel, args.core_artifact, args.dependencies,
                                                     args.core_source_repo, args.core_checksums,
                                                     args.build_tool_wheelhouse)
                print(runtime._match(digest, runtime._HEX64, "registered build receipt"))
                return 0
            except (build.BuildError, capsule.CapsuleError, recovery.RecoveryError, OSError, UnicodeError) as exc:
                try:
                    names = ({item.name for item in attempt_directory.iterdir()} - before_attempts
                             if attempt_directory.exists() else set())
                    if len(names) == 1:
                        name = next(iter(names))
                        reader = build._read_attempt if attempt_kind == "source" else wheel.read_attempt
                        record = reader(root, name)
                        if record["status"] == "active":
                            raise runtime.JournalError(f"build {record['build_id']} requires cancel-build "
                                               f"--kind {attempt_kind} --build-id {record['build_id']}") from exc
                except runtime.JournalError:
                    raise
                except (build.BuildError, OSError, ValueError, TypeError, KeyError):
                    pass
                raise runtime.JournalError("component build failed or requires recorded attempt recovery") from exc
        if args.command in ("runtime-update", "update", "full-update"):
            import component_full as full  # noqa: PLC0415
            import component_prepare as prepare  # noqa: PLC0415
            import component_recovery as recovery  # noqa: PLC0415
            import component_transaction as transaction  # noqa: PLC0415
            import package_recovery_source as capsule  # noqa: PLC0415
            is_full = args.command == "full-update"
            runtime.admission.admit_component(root, operation="full-update" if is_full else "update", code_root=runtime.CODE_ROOT)
            if is_full and not runtime.capsule_execution_ready(full=True):
                runtime._fail("full update is unavailable until full saved-source recovery is enabled")
            if not is_full and not runtime.capsule_execution_ready(full=False):
                runtime._fail("runtime update is unavailable until saved-source recovery is enabled")
            if args.expected_sha is not None:
                expected = runtime._match(args.expected_sha, runtime._HEX40, "expected source commit")
                try:
                    actual = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{commit}", lock_root=root).decode("ascii").strip()
                except (capsule.CapsuleError, recovery.RecoveryError, UnicodeError) as exc:
                    raise runtime.JournalError("operations source cannot be verified") from exc
                if expected != actual:
                    runtime._fail("source commit differs from --expected-sha")
            recipes = {}
            for name in ("api", "telegram"):
                files = getattr(args, name + "_compose_file")
                directory = getattr(args, name + "_project_directory")
                project = getattr(args, name + "_project")
                service = getattr(args, name + "_service")
                interpolation = getattr(args, name + "_interpolation_env_file")
                if files or directory or project or service or interpolation:
                    if not files or not directory or not project or not service:
                        runtime._fail(f"{name} Compose recipe requires files, project directory, project and service")
                    recipes[name] = {"compose_files": files, "project_directory": directory,
                                     "project": project, "service": service,
                                     "interpolation_env_files": interpolation}
            receipts = {name: value for name, value in (("api", args.api_receipt),
                        ("telegram", args.telegram_receipt)) if value is not None}
            artifacts = {name: str(value) for name, value in (("api", args.api_artifact),
                         ("telegram", args.telegram_artifact)) if value is not None}
            if receipts and artifacts:
                runtime._fail("producer receipts and completed artifacts cannot be combined")
            try:
                request = ({"selection": args.components, "receipts": receipts, "recipes": recipes} if receipts else
                           {"selection": args.components, "artifacts": artifacts, "recipes": recipes})
                journal = (full.prepare(root, args.candidate_db, request, compose_path=args.compose_executable,
                                        expected_sha=args.expected_sha,
                                        allow_knowledge_degrade=args.allow_knowledge_degrade) if is_full else
                           prepare.prepare_update(root, request, args.compose_executable))
            except (full.FullError, prepare.PrepareError, recovery.RecoveryError, transaction.TransactionError) as exc:
                if is_full and isinstance(exc, full.FullError) and "--allow-knowledge-degrade" in str(exc):
                    raise runtime.JournalError(f"full update preparation failed: {exc}") from exc
                raise runtime.JournalError("full update preparation failed" if is_full else "runtime update preparation failed") from exc
            if args.expected_sha is not None and (
                runtime._match(args.expected_sha, runtime._HEX40, "expected source commit") != journal["source"]["commit"]
            ):
                runtime._fail("source commit differs from --expected-sha")
            bound_compose = recovery._verify_bound_compose(root, journal["source"])
            try:
                result = (full.update(root, journal, compose_path=bound_compose) if is_full else
                          transaction.update(root, journal, compose_path=bound_compose))
            except (full.FullError, transaction.TransactionError) as exc:
                try:
                    saved = (full.load_journal(root / ".deployments" / full.JOURNAL_NAME) if is_full else
                             runtime.load_journal(root / ".deployments" / runtime.JOURNAL_NAME))
                except (full.FullError, runtime.JournalError):
                    saved = None
                if saved is not None and saved["transaction_id"] == journal["transaction_id"]:
                    raise runtime.JournalError(f"transaction {journal['transaction_id']} interrupted; use recover "
                                       f"--kind {'full' if is_full else 'runtime'} "
                                       f"--transaction-id {journal['transaction_id']}") from exc
                raise runtime.JournalError("full update preflight failed" if is_full else "runtime update preflight failed") from exc
            print(json.dumps(result, sort_keys=True))
            return 0
        runtime._fail("unsupported component command")
    except (runtime.JournalError, runtime.admission.AdmissionError, OSError, ValueError) as exc:
        print(f"component runtime: {exc}", file=sys.stderr)
        return 1
