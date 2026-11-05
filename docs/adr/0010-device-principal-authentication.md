# ADR 0010: Device-principal authentication

- Status: Accepted
- Date: 2026-08-11

## Context

The HTTP adapter ([ADR 0009](0009-http-api-adapter.md)) exposes the translation
pipeline to the owner's own PC client, bound to loopback and published only
through the operator's existing HTTPS ingress. That surface needs a credential
scheme that:

- is revocable without touching the Telegram bot's own access controls;
- never lets credential material pass through server output, logs, terminal
  scrollback, or a captured command transcript;
- makes a stolen credential store expensive to search offline;
- cannot be used by an unauthenticated caller to enumerate device ids;
- does not let the verification cost itself become the denial-of-service
  lever.

There is deliberately no self-service registration endpoint: devices are
registered by the operator, who has shell access on the host
(`src/wuwaterm_api/cli.py`).

## Decision

### Token format and issuance

A presented credential is a bearer token of the form
`wtd1.<device_id>.<secret>`. The parser splits on at most two dots, so the
secret itself may contain dots (base64, PEM-ish and UUID-ish material all do)
and still round-trips (`parse_token` in `src/wuwaterm_api/auth.py`).

The server never generates and never prints a secret. `wuwaterm-api device
issue` reads the secret from standard input (`getpass` when attached to a
terminal, a line read otherwise; `_read_secret` in `src/wuwaterm_api/cli.py`)
and `DeviceStore.issue` returns only the device record, so no caller can
accidentally route the secret to output. The command prints the device id --
which is not a secret -- and reminds the operator that the token is
`wtd1.<device_id>.<the secret you supplied>`. Every credential-bearing byte
therefore stays out of the server process' output.

Two requirements are enforced at issuance, because the operator chooses the
secret and the store cannot assume machine-generated entropy:

- Presentability: the secret must be printable ASCII with no spaces or
  control characters (`_is_presentable`), so a registered credential can
  always be sent in an HTTP header value. Anything else would register
  cleanly and then be impossible to present, the worst failure mode for a
  credential.
- Minimum length: at least `MIN_SECRET_LENGTH` (32) characters. 32 URL-safe
  characters is roughly 190 bits, far past what a hash-only store needs.

### Verifier storage: scrypt, per-device salt, constant-time compare

Only a derived verifier is stored, never the secret. Derivation is `scrypt`
with a fresh 16-byte per-device salt (`secrets.token_bytes` at issue time) and
parameters `n=2**14, r=8, p=1, dklen=32`: about 16 MiB and a few tens of
milliseconds per verification. That is comfortably inside a per-device request
budget measured in tens per minute, and expensive enough that a stolen store
is not worth grinding even if an operator picked something guessable.
Comparison uses `hmac.compare_digest`.

The verification path is deliberately not a device-id oracle. When the
presented device id is unknown, `_verify` still spends a full derivation
against a dummy salt before refusing, so a missing device and a wrong secret
take a similar amount of work. The same compensating derivation runs when a
row from a store written by an older shape lacks usable `salt`/`token_hash`
columns: returning early there would cost nothing while an unknown id still
paid full price, which is exactly the timing signal the dummy derivation
exists to close. Wrong secret, unknown device, malformed token, revoked
device and old-shape rows are all one uniform rejection.

### Scopes and the principal model

Devices carry scopes, drawn from a closed set: `translate` (POST
/v1/translations) and `meta` (GET /v1/terms, GET /v1/meta, GET
/v1/auth/status). Both are granted
by default; unknown scopes are refused at issuance (`normalize_scopes`). A
device that authenticates but lacks the required scope is answered 403 with
the enumerated code `forbidden` (`require_scope` in `src/wuwaterm_api/app.py`,
`STATUS_BY_CODE` in `src/wuwaterm_api/errors.py`) -- distinct from 401, which
means the credential itself was not proven.

The device id IS the principal id today; there is no separate principals
table. The documented trigger for adding one is a second human user or
per-user quotas (module docstring of `src/wuwaterm_api/auth.py`). The schema
does not need to change for that extension.

### Revocation and the time-of-check/time-of-use window

`device revoke` sets `revoked_at` and keeps the row, so the operator can
still see that a device existed and when it was withdrawn. The request path
re-checks liveness at three points after the initial verification snapshot:

1. Admission: `record_use` stamps `last_used_at` with an UPDATE guarded by
   `revoked_at IS NULL` and returns the affected row count. A count other
   than 1 means the device was revoked (or removed) between verification and
   admission; the request is rejected 401 rather than served on a snapshot.
   The rate limiter runs before `record_use`, so a request that will be
   refused cannot drive an unbounded stream of writes into the store.
2. Before the model call: a cheap `is_active` read, so a device revoked
   since admission spends no LLM budget slot and no model round trip.
3. Before returning: a second `is_active` read, so a revocation that
   committed during the model call is not served. At this seam the work is
   already paid for, so a TRANSIENT store error serves the completed
   translation (logged); a definitive revocation still answers 401.

Stated honestly: these reads NARROW the window, they do not close it. A
revocation that commits after the final read but before the response is
written is still served, and a transient store failure on the post-model
re-check serves rather than discards (`_require_active_device` in
`src/wuwaterm_api/app.py`).

The authentication status route also performs the final active-device read
with transient store errors failing closed as 503. It does not call
`record_use`; its separate probe limiter runs after full verification and the
scope check, without consuming the business limiter. It therefore narrows the
same revocation window without changing `last_used_at`.

### The request path cannot create or resurrect the store

Request-path reads open the store with a `mode=ro` SQLite URI
(`_connect_readonly`); the one request-path write, `record_use`, opens it
with `mode=rw` (`_connect_existing`). Neither mode creates a database. The
store is created only by `initialize()` -- from `cli._serve` at startup and
from `issue()` when the operator registers a device. Deleting the store is
therefore a break-glass revocation the request path cannot undo: every
subsequent request answers 503 instead of silently recreating an empty store
(which is what an earlier revision did, by running `initialize()` on every
request).

The exact scope of that property: `mode=ro` protects the MAIN DATABASE FILE.
Reading a WAL database still uses the `-shm` index and can recreate the
`-shm`/`-wal` sidecars if the last writer removed them, so the directory must
stay writable -- which it must be anyway, since `record_use` writes on every
admitted request. The sidecars carry no schema and cannot resurrect a deleted
store. WAL is also what lets a request-path read proceed while a concurrent
`revoke()` writes.

Two adjacent protections, for completeness: `initialize()` refuses to start
when a store still exists at the pre-move default path (`state/api/`, inside
the bot's writable mount) as well as the current one (`state-api/`), because
two stores is precisely the state in which nobody can tell which file holds
the live verifiers; and at creation the store file, its sidecars and its
directory are restricted to owner-only POSIX modes.

### A dedicated credential pool bounds verification concurrency

Verification costs a deliberate ~16 MiB scrypt derivation, and it happens
before any per-device limit can apply, so the server bounds how many can run
at once (`auth_max_concurrency`, default 2, `src/wuwaterm_api/settings.py`).
The bound is two halves of one mechanism (`create_app`):

- `auth_slots`, a non-queuing admission semaphore: when full, the request is
  shed immediately with 429 and no derivation is ever scheduled. Queuing
  would let the credential check itself become the load.
- `auth_pool`, a dedicated `ThreadPoolExecutor` whose `max_workers` is the
  REAL bound. The semaphore alone was not enough: a cancelled awaiting task
  releases its slot immediately (it must, or a verification cancelled while
  still queued would strand the slot forever) while the worker it started
  keeps running, so under a flood of cancellations concurrent derivations
  drifted above the configured maximum. `max_workers` is not releasable by
  anything a caller does.

A shared default executor was not enough either: credential work used to run
on asyncio's process-wide default executor alongside the unauthenticated
`/readyz` probe and the dictionary stage, so two unauthenticated probes could
occupy it and make the owner's own valid token answer 429. Nothing an
unauthenticated caller can schedule shares the credential pool's workers
(`_in_credential_pool` in `src/wuwaterm_api/app.py`).

### 503 for an unusable store, 401 for an unproven credential

`authenticate` returns None only when the CREDENTIAL was not proven; that is
the only case mapped to 401. A store that cannot be read -- missing, deleted
mid-run, corrupt, `database is locked`, a disk I/O error, or the pool already
shut down at teardown (`CredentialPoolClosed`) -- propagates and is answered
503. The distinction exists because a valid device must never be told to
re-pair (discard its credential) because of a database hiccup. It leaks
nothing probeable: an unusable store is device-independent, so the outcome
does not vary with the presented token. Auth-reject and store-error log lines
carry the path and the server-generated request id, never the token; the
inbound `X-Request-Id` header is ignored entirely so a caller cannot route
its own credential into the logs or the error envelope
(`RequestIdMiddleware`).

### Separation from Telegram identity

The API's device principals and the bot's Telegram-side controls are separate
mechanisms in separate stores. The bot gates its private command on
`OWNER_USER_ID` and its group behaviour on the per-chat allowlist persisted
under `state/` (ADR 0005; `src/wuwaterm/bot.py`). Devices live in
`state-api/devices.db`, a SIBLING of the bot's state directory, never a child
of it, because the bot mounts the whole of `state/` read-write. Neither
control can grant or revoke the other: revoking a device cannot touch the
chat allowlist, and no Telegram identity confers API access.

## Consequences

- Positive: no secret ever exists in server output, logs, or the store;
  compromise of the store yields only salted scrypt verifiers.
- Positive: revocation is immediate at admission, narrowed to a small window
  in flight, and the store file itself is a break-glass kill switch the
  request path cannot undo.
- Positive: verification cost is bounded independently of everything else in
  the process. What that establishes is exactly what `_in_credential_pool`
  proves and no more: nothing an unauthenticated caller can schedule shares
  the credential pool's workers, so probes and the dictionary stage cannot
  displace verification.
- Negative, and sharper once the surface is publicly routed: the bound is
  bounded, but ACCESS to it is not reserved. A caller with no credential at
  all can occupy both slots with well-formed tokens carrying invented device
  ids, because an unknown id deliberately pays a full derivation before being
  refused (that is what closes the enumeration oracle). While the slots are
  occupied the owner's own valid token is shed with `429` at admission. This
  is a residual availability risk, not a protection: the mitigations available
  are ingress-side (rate limiting in front of the route) or an admission
  scheme that reserves capacity for callers already known, and neither exists
  today.
- Negative: operators must generate and safeguard secrets themselves; a
  weak-but-32-character secret is accepted, mitigated (not eliminated) by
  scrypt cost.
- Negative: the in-flight revocation window is narrowed, not closed; a
  revocation committing after the final re-check is still served once.
- Constraint: scrypt at ~16 MiB per verification caps sustainable auth
  throughput by design; this is a single-owner surface (client transport
  choices in [ADR 0012](0012-client-transport-selection.md)), not a multi-tenant
  API.
- Constraint: the store's directory must remain writable to the server
  process (WAL sidecars, `record_use`), so read-only protection applies to
  the database file, not the directory.

## Evidence

- `src/wuwaterm_api/auth.py` -- token scheme, `parse_token`, `_is_presentable`,
  `MIN_SECRET_LENGTH`, scrypt parameters, `_verify` compensating derivation,
  `record_use` rowcount, `is_active`, `revoke`, legacy-path guard,
  `_restrict_permissions`, `_connect_readonly`/`_connect_existing`
- `src/wuwaterm_api/app.py` -- `authenticated_device`, `require_scope`,
  `_in_credential_pool`, `_require_active_device`, `create_app` pool wiring,
  `RequestIdMiddleware`
- `src/wuwaterm_api/cli.py` -- `_read_secret`, `_device_issue`, `_device_list`,
  `_device_revoke`
- `src/wuwaterm_api/settings.py` -- `auth_max_concurrency`,
  `device_db_path`/`device_db_is_default`, `state-api` layout
- `src/wuwaterm_api/errors.py` -- `STATUS_BY_CODE`, `MESSAGE_BY_CODE`
- `tests/test_api.py` --
  `test_only_a_hash_of_the_secret_is_stored`,
  `test_stored_verifier_is_salted_and_slow_to_search`,
  `test_bad_credentials_are_indistinguishable`,
  `test_an_old_shape_store_rejects_every_device_id_for_the_same_work`,
  `test_a_secret_containing_the_token_separator_round_trips`,
  `test_a_secret_that_cannot_be_sent_in_a_header_is_refused`,
  `test_a_weak_supplied_secret_is_refused`,
  `test_scope_is_enforced`,
  `test_revoked_device_is_rejected`,
  `test_record_use_reports_whether_a_live_row_was_stamped`,
  `test_a_device_revoked_during_verification_is_refused`,
  `test_a_device_revoked_after_admission_is_refused_before_serving`,
  `test_a_missing_credential_store_is_never_created_by_a_request`,
  `test_deleting_the_store_is_a_revocation_the_request_path_cannot_undo`,
  `test_a_locked_store_on_the_auth_read_is_503_a_wrong_secret_is_401`,
  `test_a_transient_recheck_store_error_is_503_not_401`,
  `test_a_post_model_store_hiccup_still_serves_the_paid_translation`,
  `test_auth_admission_sheds_load_when_the_verifier_is_full`,
  `test_the_verification_bound_holds_under_a_flood_of_cancellations`,
  `test_unauthenticated_probes_cannot_shed_a_valid_credential`,
  `test_rate_limited_requests_do_not_write_to_the_credential_store`,
  `test_every_file_carrying_verifier_material_is_restricted`,
  `test_a_store_at_both_paths_is_refused_rather_than_guessed`
