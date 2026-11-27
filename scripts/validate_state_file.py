from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

CHANNEL_REPLY_INDEX_VERSION = 1


class ChannelReplyPayloadError(ValueError):
    pass


def _strict_int(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ChannelReplyPayloadError(f"{name} must be an integer")
    return value


def _validate_channel_reply_row(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise ChannelReplyPayloadError("entry must be an object")
    _strict_int(raw.get("chat_id"), "chat_id")
    _strict_int(raw.get("message_id"), "message_id")
    expires_raw = raw.get("expires_at")
    if isinstance(expires_raw, bool) or not isinstance(expires_raw, (int, float)):
        raise ChannelReplyPayloadError("expires_at must be numeric")
    try:
        expires_at = float(expires_raw)
    except (OverflowError, ValueError) as exc:
        raise ChannelReplyPayloadError("expires_at is out of range") from exc
    if not math.isfinite(expires_at):
        raise ChannelReplyPayloadError("expires_at must be finite")
    reply_raw = raw.get("reply_message_ids")
    if not isinstance(reply_raw, list) or not reply_raw:
        raise ChannelReplyPayloadError("reply_message_ids must be a non-empty list")
    for value in reply_raw:
        _strict_int(value, "reply_message_id")


def _chat_id_from_json(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        try:
            parsed = int(normalized)
        except ValueError:
            return None
        return parsed if str(parsed) == normalized else None
    return None


def validate_chat_settings(payload: object) -> None:
    if not isinstance(payload, dict):
        raise ValueError("chat settings must be an object")
    public = payload.get("public", {})
    allowed = payload.get("allowed", [])
    if not isinstance(public, dict) or not isinstance(allowed, list):
        raise ValueError("chat settings schema is invalid")
    for key, value in public.items():
        if not isinstance(value, bool) or _chat_id_from_json(key) is None:
            raise ValueError("chat settings public state is invalid")
    for item in allowed:
        if _chat_id_from_json(item) is None:
            raise ValueError("chat settings allowlist is invalid")


def validate_channel_replies(payload: object) -> None:
    if not isinstance(payload, dict):
        raise ChannelReplyPayloadError("payload must be an object")
    if (
        type(payload.get("version")) is not int
        or payload["version"] != CHANNEL_REPLY_INDEX_VERSION
    ):
        raise ChannelReplyPayloadError("unsupported payload version")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ChannelReplyPayloadError("entries must be a list")
    for raw in entries:
        _validate_channel_reply_row(raw)


def validate(path: str | Path, filename: str) -> None:
    with Path(path).open(encoding="utf-8") as file:
        payload = json.load(file)
    if filename == "chat_settings.json":
        validate_chat_settings(payload)
    elif filename == "channel_replies.json":
        validate_channel_replies(payload)
    else:
        raise ValueError(f"unknown state file type: {filename}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(
            "usage: validate_state_file.py "
            "PATH chat_settings.json|channel_replies.json",
            file=sys.stderr,
        )
        return 2
    path, filename = argv
    try:
        validate(path, filename)
    except Exception as exc:
        print(f"invalid {filename}: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
