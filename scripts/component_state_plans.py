#!/usr/bin/env python3
"""Compare the old and target Telegram state plans."""
from __future__ import annotations

from typing import Any


class PrepareError(RuntimeError):
    """A runtime update cannot be safely prepared from the given inputs."""


RELOCATION_REFUSAL = ("state relocation is not supported in protocol-2 cutover; "
                      "preserve the existing state mount and paths")


_STATE_KINDS = ("chat-settings", "channel-replies")


_PLAN_KEYS = {"effective_path", "legacy_path", "migration", "mapped_target", "mapped_legacy"}


_PLAN_COMPARED = ("effective_path", "legacy_path", "mapped_target")


_PLAN_CLASS = {"none": "none", "validate-existing": "validate-existing", "copy-if-absent": "native-pending"}


def verify_state_plan_equivalence(old_plan: dict[str, Any], target_plan: dict[str, Any]) -> dict[str, str]:
    """Require the old and translated state plans to name exactly the same state.

    Both plans are native product plans already mapped to host paths. A path
    outside every bind maps to ``None``; two such paths are not equivalent.
    Returns the per-kind classification: none, validate-existing or
    native-pending (the product itself copies at start; the transaction never does).
    """
    result = {}
    for plan in (old_plan, target_plan):
        if type(plan) is not dict or type(plan.get("kinds")) is not dict or set(plan["kinds"]) != set(_STATE_KINDS):
            raise PrepareError("Telegram state plan is invalid")
    for kind in _STATE_KINDS:
        old, new = old_plan["kinds"][kind], target_plan["kinds"][kind]
        for item in (old, new):
            if type(item) is not dict or set(item) != _PLAN_KEYS or item["migration"] not in _PLAN_CLASS:
                raise PrepareError("Telegram state plan is invalid")
            if item["mapped_target"] is None:
                raise PrepareError(f"{kind} state is outside every writable bind")
        if old["migration"] != new["migration"]:
            raise PrepareError(f"{kind} old and target state plans disagree on the migration action")
        if any(old[key] != new[key] for key in _PLAN_COMPARED):
            raise PrepareError(RELOCATION_REFUSAL)
        if new["migration"] == "copy-if-absent":
            # Both planners name the retained legacy endpoint, but only the
            # copy case decides what the transaction may do with it, so only
            # that case has to agree exactly.
            for item in (old, new):
                if item["mapped_legacy"] is None:
                    raise PrepareError(f"{kind} legacy state is outside every bind")
            if old["mapped_legacy"] != new["mapped_legacy"]:
                raise PrepareError(RELOCATION_REFUSAL)
        result[kind] = _PLAN_CLASS[new["migration"]]
    return result
