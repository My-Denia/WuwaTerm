"""Environment-driven settings for the HTTP adapter.

Every knob is read once at startup. Serve-only values are parsed leniently
here because every operator subcommand constructs these settings; the serve
path validates their retained raw forms strictly before producing any side
effect. Invalid values are reported without echoing the raw value (it may
contain a secret).
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path


class ApiConfigError(ValueError):
    """Raised when the environment cannot produce a usable configuration."""


def validate_loopback_bind(value: str) -> str:
    """Return ``value`` if it is a numeric loopback address, else raise.

    This API is a single-owner surface that must never listen where a remote
    host can reach it. The bind is therefore required to be a numeric loopback
    literal (``127.0.0.1``, any ``127.0.0.0/8`` address, or ``::1``). Refused:

    * ``0.0.0.0`` and ``::`` — every interface, the exact exposure hazard;
    * any routable address — a public or LAN interface;
    * any hostname, including ``localhost`` — it is not numeric and could
      resolve, now or later by DNS the operator does not control, to a
      non-loopback interface. Only an address that is loopback by inspection,
      never by resolution, is accepted.

    The offending value is not echoed: settings never reflect a raw environment
    value back, and the fix is the same regardless of what was set. The returned
    value is the NORMALIZED literal (``str(address)``) so the accepted value is
    the one that actually binds: brackets are URL syntax, not host syntax, and
    ``uvicorn.run(host="[::1]")`` raises at bind time, so ``[::1]`` is returned
    as ``::1`` and surrounding whitespace is dropped.
    """
    candidate = (value or "").strip()
    host = (
        candidate[1:-1]
        if candidate.startswith("[") and candidate.endswith("]")
        else candidate
    )
    if "%" in host:
        # A zone id is accepted by ipaddress (``::1%does-not-exist`` parses AND
        # reports is_loopback), but the scope is not validated here and
        # getaddrinfo fails on a nonexistent one — which would escape as a raw
        # socket error instead of this module's ApiConfigError -> exit 2. The
        # loopback interface needs no scope, so refuse the whole class rather
        # than try to resolve it.
        raise ApiConfigError(
            "the API bind must be a plain numeric loopback address without a "
            "zone id, such as 127.0.0.1 or ::1"
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ApiConfigError(
            "the API bind must be a numeric loopback address such as "
            "127.0.0.1 or ::1"
        ) from None
    if not address.is_loopback:
        raise ApiConfigError(
            "the API bind must be a loopback address such as 127.0.0.1 or ::1; "
            "binding any other interface would expose this single-owner surface"
        )
    return str(address)


LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def validate_log_level(value: str) -> str:
    """Return the normalized level name, else raise ApiConfigError.

    Only the five standard names are accepted. ``uvicorn``'s own ``--log-level``
    additionally understands ``trace``, which is not a level the standard
    library can be configured with, so accepting it here would mean a value
    that starts the server and then fails when the first record is emitted.

    Like every other setting in this module the offending value is not echoed;
    the fix is the same whatever was set, and an environment value can carry
    material that does not belong in a startup error.
    """
    candidate = (value or "").strip().upper()
    if candidate not in LOG_LEVELS:
        raise ApiConfigError(
            "the API log level must be one of " + ", ".join(LOG_LEVELS)
        )
    return candidate


MIN_PORT = 1
MAX_PORT = 65535


def validate_port(value: int) -> int:
    """Return ``value`` if it is a usable TCP port, else raise ApiConfigError.

    ``WUWATERM_API_PORT`` is range-checked on the serve path; the ``--port``
    override used to skip that check entirely and go straight to uvicorn, so
    ``--port 999999`` and ``--port -1`` reached the socket layer and escaped as
    a raw error instead of a config error, and ``--port 0`` was silently
    discarded by an ``or`` that treats it as "unset". The override now goes
    through the same range, so every route into the port setting agrees and
    0 is REFUSED explicitly rather than ignored: an operator who asks for an
    ephemeral port is asking for an address the client cannot be configured
    for, and answering "no" is better than answering 8788.
    """
    if not MIN_PORT <= value <= MAX_PORT:
        raise ApiConfigError(
            f"the API port must be between {MIN_PORT} and {MAX_PORT}"
        )
    return value


DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8788
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_DB_PATH = "data/terms.db"
# A sibling of the bot's state directory, never a child of it: the bot
# mounts the whole of state/ read-write.
DEFAULT_STATE_DIR = "state-api"
DEVICE_DB_FILENAME = "devices.db"

# Deliberately smaller than the bot's: the API is a single-owner surface and a
# lower ceiling keeps the documented worst-case LLM concurrency small.
DEFAULT_LLM_MAX_CONCURRENCY = 2
DEFAULT_LLM_CALLS_PER_MINUTE = 30
DEFAULT_LLM_TIMEOUT_SECONDS = 45.0
DEFAULT_RATE_LIMIT_PER_MINUTE = 30
# Authentication probes have their own per-verified-device budget. Browser
# page and asset loads must not consume the business request allowance.
DEFAULT_AUTH_STATUS_RATE_LIMIT_PER_MINUTE = 120
DEFAULT_MAX_BODY_BYTES = 64 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 90.0
DEFAULT_LONG_TEXT_INPUT_LIMIT = 4096
MIN_LONG_TEXT_INPUT_LIMIT = 2000
MAX_LONG_TEXT_INPUT_LIMIT = 16384
# Verifying a presented credential costs a deliberate ~16 MiB scrypt
# derivation, and it happens BEFORE any per-device limit can apply. Bound how
# many of those can run at once so an unauthenticated caller cannot turn the
# credential check itself into the load.
DEFAULT_AUTH_MAX_CONCURRENCY = 2


_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})

RETIRED_WEB_ERROR_MESSAGE = (
    "WUWATERM_API_WEB_ENABLED is no longer supported: the legacy in-process "
    "Python Web UI has been retired from WuwaTerm Core; deploy the standalone "
    "My-Denia/WuwaTerm-Web product instead (or roll back to v0.16.0)"
)


def parse_bool(raw: str, default: bool) -> bool:
    """Parse a boolean setting, treating anything unrecognised as the default."""
    value = (raw or "").strip().lower()
    if value in _TRUE_WORDS:
        return True
    if value in _FALSE_WORDS:
        return False
    return default


def parse_int(raw: str, default: int, *, minimum: int, maximum: int) -> int:
    """Lenient integer parsing for serve-only settings read on every command.

    Unreadable or out-of-range values become the default here because
    ``from_env()`` runs for ``device revoke`` too; strict serve-time validation
    lives in ``validate_serve_numeric_settings``.
    """
    try:
        value = int((raw or "").strip())
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


def parse_float(
    raw: str, default: float, *, minimum: float, maximum: float
) -> float:
    """Lenient float parsing with strict validation deferred to ``serve``."""
    try:
        value = float((raw or "").strip())
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


def validate_retired_web_setting(raw: str) -> None:
    """Fail closed on serve if a legacy env file asks to enable in-process Web.

    Existing `.env` templates explicitly carried ``WUWATERM_API_WEB_ENABLED=0``
    (or blank/unset), so falsey and empty values are silently ignored to keep
    in-place upgrades smooth. Any truthy or unrecognised non-empty value fails
    closed without echoing the raw value so an operator never gets silent
    degradation into an API without the web UI they expected.
    """
    value = (raw or "").strip()
    if not value or value.lower() in _FALSE_WORDS:
        return
    raise ApiConfigError(RETIRED_WEB_ERROR_MESSAGE)


def _validate_int(name: str, raw: str, *, minimum: int, maximum: int) -> None:
    candidate = (raw or "").strip()
    if not candidate:
        return
    try:
        value = int(candidate)
    except ValueError:
        raise ApiConfigError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ApiConfigError(f"{name} must be between {minimum} and {maximum}")


def _validate_float(
    name: str, raw: str, *, minimum: float, maximum: float
) -> None:
    candidate = (raw or "").strip()
    if not candidate:
        return
    try:
        value = float(candidate)
    except ValueError:
        raise ApiConfigError(f"{name} must be a number") from None
    if not minimum <= value <= maximum:
        raise ApiConfigError(f"{name} must be between {minimum} and {maximum}")


def _env_path(name: str, default: str) -> Path:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return Path(default)
    return Path(raw.strip())


@dataclass(frozen=True)
class _ServeNumericRaw:
    """Raw numeric environment forms retained for strict serve validation."""

    port: str = ""
    llm_timeout_seconds: str = ""
    llm_max_concurrency: str = ""
    llm_calls_per_minute: str = ""
    rate_limit_per_minute: str = ""
    max_body_bytes: str = ""
    request_timeout_seconds: str = ""
    auth_max_concurrency: str = ""
    long_text_input_limit: str = ""


@dataclass(frozen=True)
class ApiSettings:
    db_path: Path
    device_db_path: Path
    knowledge_db_path: Path | None = None
    # False when an operator named the store explicitly. Only the default
    # layout can have inherited the pre-move path, so only the default layout
    # is checked for one.
    device_db_is_default: bool = True
    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT
    llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    llm_max_concurrency: int = DEFAULT_LLM_MAX_CONCURRENCY
    llm_calls_per_minute: int = DEFAULT_LLM_CALLS_PER_MINUTE
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    auth_max_concurrency: int = DEFAULT_AUTH_MAX_CONCURRENCY
    long_text_input_limit: int = DEFAULT_LONG_TEXT_INPUT_LIMIT
    # Every operator command constructs ApiSettings, so these serve-only raw
    # values cannot be validated until `_serve`. Keep them immutable and out of
    # repr/equality: raw environment input must not leak or change value
    # semantics merely because validation was deferred.
    _serve_numeric_raw: _ServeNumericRaw = field(
        default_factory=_ServeNumericRaw, repr=False, compare=False
    )
    log_level: str = DEFAULT_LOG_LEVEL
    # Retained raw so serve/create_app can fail closed if an old .env still
    # tries to turn on the retired in-process Python Web UI, without blocking
    # operator commands such as `device revoke`.
    retired_web_enabled_raw: str = field(default="", repr=False, compare=False)

    @classmethod
    def from_env(cls) -> "ApiSettings":
        state_dir = _env_path("WUWATERM_API_STATE_DIR", DEFAULT_STATE_DIR)
        device_db = os.getenv("WUWATERM_API_DEVICE_DB_PATH")
        knowledge_db = os.getenv("WUWATERM_API_KNOWLEDGE_DB_PATH")
        numeric_raw = _ServeNumericRaw(
            port=(os.getenv("WUWATERM_API_PORT") or "").strip(),
            llm_timeout_seconds=(
                os.getenv("WUWATERM_API_LLM_TIMEOUT_SECONDS") or ""
            ).strip(),
            llm_max_concurrency=(
                os.getenv("WUWATERM_API_LLM_MAX_CONCURRENCY") or ""
            ).strip(),
            llm_calls_per_minute=(
                os.getenv("WUWATERM_API_LLM_CALLS_PER_MINUTE") or ""
            ).strip(),
            rate_limit_per_minute=(
                os.getenv("WUWATERM_API_RATE_LIMIT_PER_MINUTE") or ""
            ).strip(),
            max_body_bytes=(
                os.getenv("WUWATERM_API_MAX_BODY_BYTES") or ""
            ).strip(),
            request_timeout_seconds=(
                os.getenv("WUWATERM_API_REQUEST_TIMEOUT_SECONDS") or ""
            ).strip(),
            auth_max_concurrency=(
                os.getenv("WUWATERM_API_AUTH_MAX_CONCURRENCY") or ""
            ).strip(),
            long_text_input_limit=(
                os.getenv("WUWATERM_API_LONG_TEXT_INPUT_LIMIT") or ""
            ).strip(),
        )
        return cls(
            db_path=_env_path("WUWATERM_DB_PATH", DEFAULT_DB_PATH),
            device_db_path=(
                Path(device_db.strip())
                if device_db and device_db.strip()
                else state_dir / DEVICE_DB_FILENAME
            ),
            knowledge_db_path=(
                Path(knowledge_db.strip())
                if knowledge_db and knowledge_db.strip()
                else None
            ),
            device_db_is_default=not (device_db and device_db.strip()),
            # Deliberately NOT validated here. from_env() is called by EVERY
            # subcommand, including `device revoke` — gating credential
            # revocation on serve-time network configuration would mean a
            # mistyped bind blocks the one operation that must always work. The
            # loopback guard is applied on the serve path, where a socket is
            # actually bound; see validate_loopback_bind and cli._serve.
            bind=(os.getenv("WUWATERM_API_BIND") or DEFAULT_BIND).strip()
            or DEFAULT_BIND,
            port=parse_int(
                numeric_raw.port, DEFAULT_PORT, minimum=MIN_PORT, maximum=MAX_PORT
            ),
            llm_timeout_seconds=parse_float(
                numeric_raw.llm_timeout_seconds,
                DEFAULT_LLM_TIMEOUT_SECONDS,
                minimum=0.1,
                maximum=300.0,
            ),
            llm_max_concurrency=parse_int(
                numeric_raw.llm_max_concurrency,
                DEFAULT_LLM_MAX_CONCURRENCY,
                minimum=1,
                maximum=64,
            ),
            llm_calls_per_minute=parse_int(
                numeric_raw.llm_calls_per_minute,
                DEFAULT_LLM_CALLS_PER_MINUTE,
                minimum=1,
                maximum=10000,
            ),
            rate_limit_per_minute=parse_int(
                numeric_raw.rate_limit_per_minute,
                DEFAULT_RATE_LIMIT_PER_MINUTE,
                minimum=1,
                maximum=10000,
            ),
            max_body_bytes=parse_int(
                numeric_raw.max_body_bytes,
                DEFAULT_MAX_BODY_BYTES,
                minimum=64,
                maximum=1024 * 1024,
            ),
            request_timeout_seconds=parse_float(
                numeric_raw.request_timeout_seconds,
                DEFAULT_REQUEST_TIMEOUT_SECONDS,
                minimum=1.0,
                maximum=600.0,
            ),
            auth_max_concurrency=parse_int(
                numeric_raw.auth_max_concurrency,
                DEFAULT_AUTH_MAX_CONCURRENCY,
                minimum=1,
                maximum=64,
            ),
            long_text_input_limit=parse_int(
                numeric_raw.long_text_input_limit,
                DEFAULT_LONG_TEXT_INPUT_LIMIT,
                minimum=MIN_LONG_TEXT_INPUT_LIMIT,
                maximum=MAX_LONG_TEXT_INPUT_LIMIT,
            ),
            _serve_numeric_raw=numeric_raw,
            # Carried raw for the same reason as `bind`, and validated in the
            # same place: from_env() runs for EVERY subcommand, so a typo in a
            # serve-time knob must not be able to block `device revoke`. See
            # validate_log_level and cli._serve.
            log_level=(
                os.getenv("WUWATERM_API_LOG_LEVEL") or DEFAULT_LOG_LEVEL
            ).strip()
            or DEFAULT_LOG_LEVEL,
            retired_web_enabled_raw=(
                os.getenv("WUWATERM_API_WEB_ENABLED") or ""
            ).strip(),
        )


def validate_serve_numeric_settings(settings: ApiSettings) -> None:
    """Strictly validate raw numeric settings on the only path that serves."""
    raw = settings._serve_numeric_raw
    for name, value, minimum, maximum in (
        ("WUWATERM_API_PORT", raw.port, MIN_PORT, MAX_PORT),
        ("WUWATERM_API_LLM_MAX_CONCURRENCY", raw.llm_max_concurrency, 1, 64),
        ("WUWATERM_API_LLM_CALLS_PER_MINUTE", raw.llm_calls_per_minute, 1, 10000),
        ("WUWATERM_API_RATE_LIMIT_PER_MINUTE", raw.rate_limit_per_minute, 1, 10000),
        ("WUWATERM_API_MAX_BODY_BYTES", raw.max_body_bytes, 64, 1024 * 1024),
        ("WUWATERM_API_AUTH_MAX_CONCURRENCY", raw.auth_max_concurrency, 1, 64),
        (
            "WUWATERM_API_LONG_TEXT_INPUT_LIMIT",
            raw.long_text_input_limit,
            MIN_LONG_TEXT_INPUT_LIMIT,
            MAX_LONG_TEXT_INPUT_LIMIT,
        ),
    ):
        _validate_int(name, value, minimum=minimum, maximum=maximum)
    for name, value, min_val, max_val in (
        ("WUWATERM_API_LLM_TIMEOUT_SECONDS", raw.llm_timeout_seconds, 0.1, 300.0),
        (
            "WUWATERM_API_REQUEST_TIMEOUT_SECONDS",
            raw.request_timeout_seconds,
            1.0,
            600.0,
        ),
    ):
        _validate_float(name, value, minimum=min_val, maximum=max_val)
