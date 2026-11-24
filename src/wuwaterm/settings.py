"""Per-chat runtime settings persisted as a transactionally updated JSON file."""

from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
import tempfile
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable, Iterator


LOGGER = logging.getLogger(__name__)
_PROCESS_LOCKS: dict[str, RLock] = {}
_PROCESS_LOCKS_GUARD = Lock()
_StateMutator = Callable[[dict[int, bool], set[int]], bool]


class ChatSettingsError(OSError):
    """The settings file cannot be safely read or updated."""


class ChatSettingsDurabilityError(ChatSettingsError):
    """The new file is visible, but directory durability could not be confirmed."""


def _process_lock_for(path: Path) -> RLock:
    key = str(path)
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, RLock())


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


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Hold a stable sibling lock file across a settings transaction."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt_mod: Any = msvcrt
            if os.fstat(fd).st_size < 1:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt_mod.locking(fd, msvcrt_mod.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt_mod = msvcrt
                    msvcrt_mod.locking(fd, msvcrt_mod.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        else:
            os.close(fd)


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path.parent, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
def _confirm_parent_durability(path: Path) -> None:
    try:
        _fsync_parent_directory(path)
    except OSError:
        raise ChatSettingsDurabilityError(
            "chat settings are visible but directory durability is uncertain"
        ) from None



class ChatSettings:
    """Public-mode flags and the authorized group allowlist."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve(strict=False)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self._lock = _process_lock_for(self.path)
        self._public: dict[int, bool] = {}
        self._allowed: set[int] = set()
        self._load()

    @staticmethod
    def _schema_error() -> ChatSettingsError:
        return ChatSettingsError("chat settings file has an invalid schema")

    def _read_state(self, *, strict: bool) -> tuple[dict[int, bool], set[int]]:
        if not self.path.exists():
            return {}, set()
        try:
            with self.path.open(encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, UnicodeError, json.JSONDecodeError):
            if strict:
                raise ChatSettingsError("chat settings file is unreadable") from None
            LOGGER.warning("chat settings unreadable, starting empty")
            return {}, set()

        if not isinstance(data, dict):
            if strict:
                raise self._schema_error()
            LOGGER.warning("chat settings schema invalid, starting empty")
            return {}, set()

        public_raw = data.get("public", {})
        allowed_raw = data.get("allowed", [])
        if not isinstance(public_raw, dict) or not isinstance(allowed_raw, list):
            if strict:
                raise self._schema_error()
            LOGGER.warning("chat settings schema invalid, starting empty")
            return {}, set()

        public: dict[int, bool] = {}
        allowed: set[int] = set()
        for key, value in public_raw.items():
            if not isinstance(value, bool):
                if strict:
                    raise self._schema_error()
                continue
            chat_id = _chat_id_from_json(key)
            if chat_id is None:
                if strict:
                    raise self._schema_error() from None
                continue
            public[chat_id] = value
        for item in allowed_raw:
            chat_id = _chat_id_from_json(item)
            if chat_id is None:
                if strict:
                    raise self._schema_error() from None
                continue
            allowed.add(chat_id)
        return public, allowed

    def _load(self) -> None:
        public, allowed = self._read_state(strict=False)
        self._publish(public, allowed)

    def _publish(self, public: dict[int, bool], allowed: set[int]) -> None:
        self._public = dict(public)
        self._allowed = set(allowed)

    def _save_state(self, public: dict[int, bool], allowed: set[int]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "public": {str(key): value for key, value in public.items()},
            "allowed": sorted(allowed),
        }
        fd, tmp = tempfile.mkstemp(prefix=".chat_settings.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        _confirm_parent_durability(self.path)

    def _mutate(
        self,
        mutator: _StateMutator,
        *,
        deny_on_precommit_failure: bool,
    ) -> bool:
        with self._lock:
            fallback_public = dict(self._public)
            fallback_allowed = set(self._allowed)
            mutator(fallback_public, fallback_allowed)
            fresh_public: dict[int, bool] | None = None
            fresh_allowed: set[int] | None = None
            candidate_public: dict[int, bool] | None = None
            candidate_allowed: set[int] | None = None
            try:
                with _file_lock(self.lock_path):
                    fresh_public, fresh_allowed = self._read_state(strict=True)
                    candidate_public = dict(fresh_public)
                    candidate_allowed = set(fresh_allowed)
                    changed = mutator(candidate_public, candidate_allowed)
                    if not changed:
                        self._publish(fresh_public, fresh_allowed)
                        if self.path.exists():
                            _confirm_parent_durability(self.path)
                        return False
                    try:
                        self._save_state(candidate_public, candidate_allowed)
                    except ChatSettingsDurabilityError:
                        self._publish(candidate_public, candidate_allowed)
                        raise
                    self._publish(candidate_public, candidate_allowed)
                    return True
            except ChatSettingsDurabilityError:
                raise
            except Exception:
                if deny_on_precommit_failure:
                    if (
                        candidate_public is not None
                        and candidate_allowed is not None
                    ):
                        self._publish(candidate_public, candidate_allowed)
                    else:
                        self._publish(fallback_public, fallback_allowed)
                elif fresh_public is not None and fresh_allowed is not None:
                    self._publish(fresh_public, fresh_allowed)
                raise

    def is_public(self, chat_id: int) -> bool:
        with self._lock:
            return self._public.get(chat_id, False)

    def set_public(self, chat_id: int, value: bool) -> bool:
        def mutate(public: dict[int, bool], _allowed: set[int]) -> bool:
            if public.get(chat_id, False) == value:
                return False
            public[chat_id] = value
            return True

        return self._mutate(mutate, deny_on_precommit_failure=not value)

    def is_allowed(self, chat_id: int) -> bool:
        with self._lock:
            return chat_id in self._allowed

    def allow(self, chat_id: int) -> bool:
        def mutate(_public: dict[int, bool], allowed: set[int]) -> bool:
            if chat_id in allowed:
                return False
            allowed.add(chat_id)
            return True

        return self._mutate(mutate, deny_on_precommit_failure=False)

    def disallow(self, chat_id: int) -> bool:
        def mutate(_public: dict[int, bool], allowed: set[int]) -> bool:
            changed = chat_id in allowed
            allowed.discard(chat_id)
            return changed

        return self._mutate(mutate, deny_on_precommit_failure=True)

    def allowed_chats(self) -> list[int]:
        with self._lock:
            return sorted(self._allowed)

    def allowed_count(self) -> int:
        with self._lock:
            return len(self._allowed)

    def public_count(self) -> int:
        with self._lock:
            return sum(1 for value in self._public.values() if value)
