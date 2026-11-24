"""Revocable device principals for the HTTP adapter.

Identity here is completely separate from Telegram: a device is its own
principal with its own store (``state-api/devices.db``), so revoking API access
cannot touch the bot's allowlist and vice versa.

Token format::

    wtd1.<device_id>.<secret>

**This service never produces or emits a secret.** The operator supplies the
secret on standard input when issuing a device, and only a derived verifier is
stored. That keeps every credential-bearing byte out of the server process'
output, out of terminal scrollback and out of anything that could capture,
format or persist it — a property worth more than the small convenience of
having the server mint the value for you.

The secret may contain dots, which the token parser accounts for, but it has
to survive the transport it will be presented over: an HTTP header value. It is
therefore required to be printable ASCII with no spaces or control characters,
so a registered credential can never be one that cannot be sent.

Because the operator chooses the secret, this store cannot assume the entropy
a machine-generated value would have had. It therefore refuses anything shorter
than ``MIN_SECRET_LENGTH`` **and** derives the stored value with ``scrypt`` and
a per-device salt rather than a bare digest, so a stolen store cannot be
searched cheaply even if an operator picked something guessable. Comparison is
constant time.

Revocation sets ``revoked_at`` and keeps the row, so an operator can still see
that a device existed and when it was withdrawn.

The model extends to multi-principal later without changing this schema: the
device id IS the principal id today. The documented trigger for adding a
separate principals table is a second human user or per-user quotas.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


TOKEN_SCHEME = "wtd1"
TOKEN_PARTS = 3

# The store used to default to state/api/, a child of the directory the bot
# mounts read-write in full. It now lives in the sibling state-api/. An
# installation that ran on the old default still holds every verifier in the
# old file, and creating an empty store at the new path would look like a
# clean start while silently refusing every registered device.
LEGACY_STATE_DIR_NAME = "state"
LEGACY_STATE_SUBDIR_NAME = "api"
CURRENT_STATE_DIR_NAME = "state-api"
# A supplied secret must have at least this much material. 32 URL-safe
# characters is roughly 190 bits, far past what a hash-only store needs.
MIN_SECRET_LENGTH = 32

SCOPE_TRANSLATE = "translate"
SCOPE_META = "meta"
KNOWN_SCOPES = frozenset({SCOPE_TRANSLATE, SCOPE_META})
DEFAULT_SCOPES = (SCOPE_TRANSLATE, SCOPE_META)

# scrypt parameters. 2**14 blocks with r=8 is ~16 MiB and a few tens of
# milliseconds per verification: comfortably inside a per-device request budget
# measured in tens per minute, and expensive enough that a stolen store is not
# worth grinding.
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id    TEXT PRIMARY KEY,
    device_name  TEXT NOT NULL,
    salt         BLOB NOT NULL,
    token_hash   BLOB NOT NULL,
    scopes       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    revoked_at   TEXT,
    last_used_at TEXT
);
"""


class DeviceStoreError(RuntimeError):
    """Raised for operator-facing device store problems."""


@dataclass(frozen=True)
class Device:
    device_id: str
    device_name: str
    scopes: tuple[str, ...]
    created_at: str
    revoked_at: str | None
    last_used_at: str | None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_presentable(secret: str) -> bool:
    """Whether ``secret`` can survive being sent in an HTTP header value."""
    if not secret:
        return False
    return all("!" <= char <= "~" for char in secret)


def _derive(secret: str, salt: bytes) -> bytes:
    """Derive the stored verifier for an operator-chosen secret."""
    return hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def normalize_scopes(scopes: str | Iterable[object] | None) -> tuple[str, ...]:
    if scopes is None:
        return DEFAULT_SCOPES
    if isinstance(scopes, str):
        items = [part.strip() for part in scopes.split(",")]
    else:
        items = [str(part).strip() for part in scopes]
    cleaned = tuple(sorted({item for item in items if item}))
    if not cleaned:
        raise DeviceStoreError("at least one scope is required")
    unknown = sorted(set(cleaned) - KNOWN_SCOPES)
    if unknown:
        known = ", ".join(sorted(KNOWN_SCOPES))
        raise DeviceStoreError(f"unknown scopes {unknown}; known scopes: {known}")
    return cleaned


def parse_token(token: str) -> tuple[str, str] | None:
    """Split a presented token into (device_id, secret), or None if malformed.

    The split is bounded at two separators so the secret's own character set
    stays unconstrained: an operator-chosen secret may contain dots (base64,
    PEM-ish and UUID-ish material all do) and must still round-trip.
    """
    if not token:
        return None
    parts = token.strip().split(".", TOKEN_PARTS - 1)
    if len(parts) != TOKEN_PARTS:
        return None
    scheme, device_id, secret = parts
    if scheme != TOKEN_SCHEME or not device_id or not secret:
        return None
    return device_id, secret


def legacy_store_path(path: Path) -> Path | None:
    """Where a store written before the sibling-directory move would live.

    Only meaningful for the default layout: an operator who set an explicit
    path never had the old default in the first place.
    """
    if path.parent.name != CURRENT_STATE_DIR_NAME:
        return None
    return (
        path.parent.parent
        / LEGACY_STATE_DIR_NAME
        / LEGACY_STATE_SUBDIR_NAME
        / path.name
    )


class DeviceStore:
    """SQLite-backed device registry. One small file, no server."""

    def __init__(self, path: str | Path, *, guard_legacy_default: bool = True):
        self.path = Path(path)
        # Only the DEFAULT layout ever had the old path. An operator who names
        # a store explicitly means that store, even if it happens to sit in a
        # directory called state-api, so callers that know the path was chosen
        # turn this off.
        self.guard_legacy_default = guard_legacy_default

    def initialize(self) -> None:
        # Whether or not a store already exists here: an earlier start may
        # have created an empty one at this path, and two stores is precisely
        # the state in which nobody can tell which file holds the live
        # verifiers.
        legacy = legacy_store_path(self.path) if self.guard_legacy_default else None
        if legacy is not None and legacy.exists():
            raise DeviceStoreError(
                f"a device store still exists at {legacy}, the path this "
                f"service used before its state directory moved out of the "
                f"bot's writable mount. It now reads {self.path}. Move that "
                f"file (with any -wal and -shm sidecars) here if it holds the "
                f"live verifiers, or delete it if this one does, or point "
                f"WUWATERM_API_DEVICE_DB_PATH at the store you mean. Starting "
                f"with both in place could refuse every device ever registered"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)
            # CREATE TABLE IF NOT EXISTS cannot add a column to a store written
            # by an older shape. Say so plainly instead of failing later with a
            # confusing SQL error.
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(devices)")
            }
            missing = {"salt", "token_hash"} - columns
            if missing:
                raise DeviceStoreError(
                    f"{self.path} was written by an older device store and is "
                    f"missing {sorted(missing)}; remove the file and register "
                    f"the devices again"
                )
        if not existed:
            # A creation-time action, not a per-request one: re-applying it on
            # the read path would be redundant work on the hot path and would
            # silently override an operator's own choice.
            self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        """Keep verifier material off the world-readable path.

        SQLite's write-ahead log and shared-memory sidecars carry the same rows
        as the main file, so all three plus the directory are restricted, not
        just the database the caller named.

        This runs once, at creation. Sidecars created LATER — including by a
        request-path read, which recreates them if the last writer removed them
        — are not chmod'ed here: SQLite gives a journal/WAL file the mode of the
        database it belongs to, which the POSIX gate below
        (``test_every_file_carrying_verifier_material_is_restricted``, which
        authenticates before it looks) asserts on the deployment platform. The
        ``0o700`` on the directory is the backstop either way.
        """
        targets = [
            (self.path.parent, 0o700),
            (self.path, 0o600),
            (self.path.with_name(self.path.name + "-wal"), 0o600),
            (self.path.with_name(self.path.name + "-shm"), 0o600),
        ]
        for target, mode in targets:
            try:
                if target.exists():
                    target.chmod(mode)
            except OSError:  # pragma: no cover - filesystem without POSIX modes
                pass

    # Every connection below is opened inside ``closing(...)``. ``with conn:``
    # is a TRANSACTION manager, not a closer, so the connection used to stay
    # open until the garbage collector got to it — holding a WAL read lock and
    # the sidecar files, which is exactly the contention with ``revoke()`` (and,
    # on Windows, the reason a store an operator deleted could not actually be
    # deleted). Writers use ``closing(...) as conn, conn`` so the transaction
    # still commits before the handle goes.

    def _connect(self) -> sqlite3.Connection:
        """Open the store for an OPERATOR command: creates it if absent."""
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _uri(self, mode: str) -> str:
        # Built from the resolved path so a store whose name or directory
        # contains a URI metacharacter ('?', '#') cannot turn part of the path
        # into connection parameters.
        return f"{Path(self.path).resolve().as_uri()}?mode={mode}"

    def _connect_readonly(self) -> sqlite3.Connection:
        """Open the store for a REQUEST-PATH read. Never creates the DATABASE.

        The request path used to call :meth:`initialize` — mkdir, a write-mode
        connect, a journal-mode pragma and DDL — on EVERY request, so an
        unauthenticated caller wrote to the credential store, contended with
        ``revoke()``, and silently RECREATED a store an operator had deleted as
        an emergency revocation. A ``mode=ro`` connection cannot do any of that:
        a missing directory or file, a corrupt file or an unreadable one all
        raise ``sqlite3.Error``, which the request path already answers as 503
        (store unavailable) rather than 401 (credential rejected).

        Stated exactly, because the difference matters: ``mode=ro`` protects the
        DATABASE FILE. Reading a WAL database still uses the ``-shm`` WAL index
        and can create the ``-shm``/``-wal`` sidecars if the last writer removed
        them, so the directory must remain writable — which it must be anyway,
        since :meth:`record_use` writes to it on every admitted request. This is
        inherent to WAL, and WAL is what lets this read proceed while a
        concurrent ``revoke()`` writes; the sidecars carry no schema and their
        loss or recreation cannot resurrect a deleted store, because the main
        database file is what ``mode=ro`` refuses to create.

        No pragma is issued: ``journal_mode`` is a persistent property of the
        file set at creation, and there are no foreign keys in this schema.
        """
        conn = sqlite3.connect(self._uri("ro"), uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _connect_existing(self) -> sqlite3.Connection:
        """Open the store for a REQUEST-PATH write of an EXISTING store.

        ``mode=rw`` writes but does not create. The one request-path write
        (:meth:`record_use`) therefore cannot resurrect a deleted store either:
        it raises ``sqlite3.Error`` into the same 503 path.
        """
        conn = sqlite3.connect(self._uri("rw"), uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    # -- operator commands -------------------------------------------------

    def issue(
        self,
        device_name: str,
        scopes: str | Iterable[object] | None = None,
        *,
        secret: str,
    ) -> Device:
        """Register a device for an operator-supplied secret.

        Returns the device only. The secret is never echoed back, so no caller
        of this method can accidentally route it to output.
        """
        name = (device_name or "").strip()
        if not name:
            raise DeviceStoreError("device_name must not be empty")
        # Taken verbatim, but it must be presentable in an HTTP header:
        # anything else would register cleanly and then be impossible to send,
        # which is the worst kind of failure for a credential.
        material = secret or ""
        if not _is_presentable(material):
            raise DeviceStoreError(
                "the supplied secret must be printable ASCII without spaces or "
                "control characters, so it can be presented in a request header"
            )
        if len(material) < MIN_SECRET_LENGTH:
            raise DeviceStoreError(
                f"the supplied secret must be at least {MIN_SECRET_LENGTH}"
                " characters of unguessable material"
            )
        resolved = normalize_scopes(scopes)
        device_id = secrets.token_hex(8)
        created_at = _now()
        salt = secrets.token_bytes(SALT_BYTES)
        self.initialize()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO devices(device_id, device_name, salt, token_hash,"
                " scopes, created_at, revoked_at, last_used_at)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    device_id,
                    name,
                    salt,
                    _derive(material, salt),
                    ",".join(resolved),
                    created_at,
                ),
            )
        return Device(
            device_id=device_id,
            device_name=name,
            scopes=resolved,
            created_at=created_at,
            revoked_at=None,
            last_used_at=None,
        )

    def list_devices(self) -> list[Device]:
        self.initialize()
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT * FROM devices ORDER BY created_at, device_id"
            ).fetchall()
        return [_row_to_device(row) for row in rows]

    def revoke(self, device_id: str) -> Device:
        self.initialize()
        revoked_at = _now()
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if row is None:
                raise DeviceStoreError(f"unknown device: {device_id}")
            if row["revoked_at"] is None:
                conn.execute(
                    "UPDATE devices SET revoked_at = ? WHERE device_id = ?",
                    (revoked_at, device_id),
                )
                row = conn.execute(
                    "SELECT * FROM devices WHERE device_id = ?", (device_id,)
                ).fetchone()
        return _row_to_device(row)

    # -- request path ------------------------------------------------------

    def authenticate(self, token: str) -> Device | None:
        """Return the live device for ``token``, or None. READ ONLY — really.

        It says READ ONLY and it now IS read only. This method used to call
        :meth:`initialize` first, which creates the directory, connects in
        write mode, sets a pragma and runs DDL. Two things followed from that,
        both bad: an UNAUTHENTICATED caller could write to the credential store
        and contend with a concurrent ``revoke()``, and deleting the store —
        the bluntest emergency revocation an operator has — was silently undone
        by the next unauthenticated request, which recreated an empty store.
        The store is created by ``initialize()`` — from ``cli._serve`` at
        startup and from ``issue()`` when an operator registers the first
        device. Nothing on the REQUEST path creates it.

        None means the CREDENTIAL was not proven: unknown device, wrong secret,
        malformed token, a revoked device and a store written by an older shape
        are all indistinguishable, so the endpoint cannot be used to enumerate
        device ids. These are the only cases that map to 401.

        A store that cannot be READ — missing, deleted mid-run, corrupt,
        ``database is locked``, a disk I/O error — is a different thing: the
        store being unusable, not the credential being wrong. The
        ``sqlite3.Error`` PROPAGATES, so the request path answers 503 rather
        than telling a valid device to re-pair, and a deleted store keeps
        answering 503 instead of being recreated. This leaks nothing probeable:
        an unusable store is device-independent, so the outcome does not vary
        with the token.

        Usage is deliberately NOT recorded here: a caller that is about to be
        refused by a rate limit must not be able to drive an unbounded stream
        of writes. Call :meth:`record_use` once the request is admitted.
        """
        parsed = parse_token(token)
        if parsed is None:
            return None
        device_id, secret = parsed
        return self._verify(device_id, secret)

    def _verify(self, device_id: str, secret: str) -> Device | None:
        with closing(self._connect_readonly()) as conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if row is None:
                # Still spend a derivation so a missing device and a wrong
                # secret take a similar amount of work.
                hmac.compare_digest(
                    _derive(secret, b"\x00" * SALT_BYTES), b"\x00" * SCRYPT_DKLEN
                )
                return None
            try:
                salt = bytes(row["salt"])
                stored = bytes(row["token_hash"])
            except (IndexError, KeyError, TypeError):
                # A store written by an older shape has no verifier columns, or
                # holds something that is not one (TEXT, NULL). initialize()
                # reports that to the OPERATOR at startup; on the request path
                # it stays one more uniform rejection — and it has to be uniform
                # in TIME too. Returning early here would cost nothing while an
                # unknown device id still paid for a full derivation, which is
                # exactly the device-id oracle the dummy derivation above
                # exists to close, so spend the same work before refusing.
                hmac.compare_digest(
                    _derive(secret, b"\x00" * SALT_BYTES), b"\x00" * SCRYPT_DKLEN
                )
                return None
            if not hmac.compare_digest(_derive(secret, salt), stored):
                return None
            if row["revoked_at"] is not None:
                return None
        return _row_to_device(row)

    def record_use(self, device_id: str, *, now: str | None = None) -> int:
        """Stamp last_used_at for an ADMITTED request; return affected rows.

        The UPDATE only touches a row that is still active, so the returned
        count is 1 for a live device and 0 for one revoked (or removed) between
        verification and this write. The caller treats a count other than 1 as
        a revocation that committed in-flight and rejects the request, closing
        the window where a snapshot taken at verify time would otherwise be
        served after the device was withdrawn.

        The connection writes but does not CREATE: this is the one write on the
        request path, and it must not be able to resurrect a store an operator
        deleted either. A missing store raises into the 503 path.
        """
        with closing(self._connect_existing()) as conn, conn:
            cursor = conn.execute(
                "UPDATE devices SET last_used_at = ? WHERE device_id = ?"
                " AND revoked_at IS NULL",
                (now or _now(), device_id),
            )
            return cursor.rowcount

    def is_active(self, device_id: str) -> bool:
        """Whether ``device_id`` is registered and not revoked, right now.

        READ ONLY, and cheap: a single indexed lookup, run at the request-time
        TOCTOU seams AFTER the device has already authenticated. Unlike
        :meth:`authenticate`, a store error here is NOT swallowed into a
        rejection: this is not an anti-enumeration surface (the caller is a
        known, verified device), so a transient failure — ``database is
        locked``, a disk I/O error — must surface as an infrastructure problem
        and not be misread as the credential being invalid. ``False`` means the
        device is genuinely absent or revoked; a ``sqlite3.Error`` propagates.
        """
        with closing(self._connect_readonly()) as conn:
            row = conn.execute(
                "SELECT revoked_at FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        return row is not None and row["revoked_at"] is None


def _row_to_device(row: sqlite3.Row) -> Device:
    return Device(
        device_id=row["device_id"],
        device_name=row["device_name"],
        scopes=tuple(part for part in str(row["scopes"]).split(",") if part),
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
        last_used_at=row["last_used_at"],
    )
