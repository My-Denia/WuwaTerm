#!/usr/bin/env python3
"""Inspect component deployments and adopt an existing topology as metadata.

Adoption observes but does not start, stop or replace installed services.
Component update and recovery execution remain separate unfinished work.
A valid journal or image identity is not a service-health check.
"""

from __future__ import annotations

from pathlib import Path
import sys

# Historical recovery executes this module from the verified source capsule,
# with isolated mode enabled. Do not import modules from the deployment root.
CODE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(CODE_ROOT / "scripts"))
sys.path.insert(0, str(CODE_ROOT / "src"))
import deployment_admission as admission  # noqa: E402, F401
import product_manifest as manifest  # noqa: E402, F401

from component_adoption import (  # noqa: E402
    _LEGACY_VERSION_CODE as _LEGACY_VERSION_CODE,
    inspect_deployment as inspect_deployment,
    inspect_container as inspect_container,
    observe_unrecorded as observe_unrecorded,
    _project_containers as _project_containers,
    _compose_input as _compose_input,
    _compose_config as _compose_config,
    _env_mapping as _env_mapping,
    _recipe_from_compose as _recipe_from_compose,
    _snapshot_pointer as _snapshot_pointer,
    _replace_pointer as _replace_pointer,
    _write_adopt_journal as _write_adopt_journal,
    _PROBE_STAGE_DIR as _PROBE_STAGE_DIR,
    _PROBE_STAGE_NAME as _PROBE_STAGE_NAME,
    _PROBE_STAT_KEYS as _PROBE_STAT_KEYS,
    _PROBE_STABLE_KEYS as _PROBE_STABLE_KEYS,
    _PROBE_PARENT_KEYS as _PROBE_PARENT_KEYS,
    _probe_file_stat as _probe_file_stat,
    _probe_parent_stat as _probe_parent_stat,
    _probe_read_at as _probe_read_at,
    _probe_staging as _probe_staging,
    pending_adopt_probe_staging as pending_adopt_probe_staging,
    reconcile_adopt_probe_staging as reconcile_adopt_probe_staging,
    _read_adopt_probe as _read_adopt_probe,
    _write_adopt_probe as _write_adopt_probe,
    _adopt_probe_daemon as _adopt_probe_daemon,
    _owned_adopt_probe as _owned_adopt_probe,
    _remove_owned_adopt_probe as _remove_owned_adopt_probe,
    _adopt_probe_argv as _adopt_probe_argv,
    _run_adopt_probe as _run_adopt_probe,
    _legacy_core_version as _legacy_core_version,
    recover_adopt_probe as recover_adopt_probe,
    _legacy_component as _legacy_component,
    _adopt_component as _adopt_component,
    adopt as adopt,
)

from component_journal_runtime import (  # noqa: E402
    JOURNAL_NAME as JOURNAL_NAME,
    JOURNAL_SCHEMA_VERSION as JOURNAL_SCHEMA_VERSION,
    RUNTIME4_SCHEMA_VERSION as RUNTIME4_SCHEMA_VERSION,
    TOOL_PATHS as TOOL_PATHS,
    BOOTSTRAP_FILES as BOOTSTRAP_FILES,
    PHASES as PHASES,
    _HEX40 as _HEX40,
    _HEX64 as _HEX64,
    _DEPLOYMENT_ID as _DEPLOYMENT_ID,
    _NAME as _NAME,
    _DOCKER_TIME as _DOCKER_TIME,
    _LIMIT as _LIMIT,
    JournalError as JournalError,
    _fail as _fail,
    _object as _object,
    _match as _match,
    _text as _text,
    _absolute as _absolute,
    _boolean as _boolean,
    _integer as _integer,
    _image_id as _image_id,
    source_tool_identity as source_tool_identity,
    bootstrap_identity as bootstrap_identity,
    tool_bytes_identity as tool_bytes_identity,
    _stat as _stat,
    _file_preimage as _file_preimage,
    _container as _container,
    _pointer as _pointer,
    _migrations as _migrations,
    _manifest as _manifest,
    predecessor_reference as predecessor_reference,
    validate_predecessor as validate_predecessor,
    validate_journal as validate_journal,
    _decode as _decode,
    _read_regular as _read_regular,
    load_journal as load_journal,
    _read_pointer as _read_pointer,
)


# Schema 5 is the protocol-2 (completed-artifact, no copy) runtime update.
# Schemas 2/3/4 keep their exact historical meaning; adoption still writes 4.
PRODUCER_EXECUTION_READY = False
# This source's saved-source recovery protocol. Protocol 2 means completed-
# artifact inputs and no producer; protocol-1 capsules keep being interpreted
# by their own saved source and its own (closed) readiness flags.
CAPSULE_RECOVERY_PROTOCOL = 2
CAPSULE_FULL_RECOVERY_PROTOCOL = 2


def _capsule_protocol(*, full: bool) -> object:
    return globals().get("CAPSULE_FULL_RECOVERY_PROTOCOL" if full else "CAPSULE_RECOVERY_PROTOCOL")


def capsule_execution_ready(*, full: bool) -> bool:
    """This source's own readiness for the protocol it declares."""
    import component_recovery as recovery  # noqa: PLC0415
    return recovery.protocol_ready(_capsule_protocol(full=full), full=full)


def main(argv: list[str] | None = None) -> int:
    """Run the component deployment coordinator from this source version."""
    import component_deployment  # noqa: PLC0415

    return component_deployment.main(argv, runtime=sys.modules[__name__])


if __name__ == "__main__":
    raise SystemExit(main())
