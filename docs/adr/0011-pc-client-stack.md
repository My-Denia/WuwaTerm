# ADR 0011: PC client stack and transport policy

- Status: Accepted
- Date: 2026-08-11

## Context

The service now exposes a versioned HTTP API behind device-principal
authentication ([ADR 0009](0009-http-api-adapter.md),
[ADR 0010](0010-device-principal-authentication.md)), and the owner uses a
Windows desktop machine. An earlier client design reached the service through
the operator's shell-access channel to the host; that design is revoked, and
the repository now carries gates that keep it from returning (see
[ADR 0012](0012-client-transport-selection.md) for how the transport was
selected). This record documents the desktop client as implemented under
`client/`: its stack, its plain-text contract, and the transport rules the
code itself enforces.

## Decision

### Stack

- **Python 3.12** (`client/pyproject.toml` `requires-python = ">=3.12"`; the
  documented build venv pins CPython 3.12.13 in `client/README.md` and
  `client/build.ps1`). Same language as the server, so one contributor
  maintains both sides.
- **PySide6 >= 6.7, < 7** for the UI, with **qasync** bridging the Qt event
  loop and asyncio (`client/src/wuwaterm_client/app.py`). The bridge is what
  makes the UI's wait on an in-flight HTTP request cancellable from the UI
  thread without a worker-thread layer (the wait, not the request: see
  Timeouts, cancellation, stable errors below). A plain Tk UI would have cost
  the native look and the async bridge; a web UI would have reintroduced a
  hosted surface the project deliberately does not have.
- **httpx >= 0.27, < 1** as the async HTTP client
  (`client/src/wuwaterm_client/api.py`). A synchronous client would block the
  UI or force threads; httpx's `AsyncClient` also carries the explicit
  `verify=True` / `trust_env=False` configuration the transport policy pins.
- **keyring >= 25, < 26** with the Windows Credential Manager backend
  (`client/src/wuwaterm_client/credentials.py`; the PyInstaller spec lists
  `keyring.backends.Windows` as a hidden import). The alternative - the token
  in `config.json` - would put the only secret on disk in plain form.
- **PyInstaller >= 6.10, < 7, one-folder build**, driven by
  `client/build.ps1` from `client/WuwaTerm.spec`, producing
  `client/dist/WuwaTerm/WuwaTerm.exe`. The spec binds the interpreter's own
  OpenSSL DLLs so the artifact does not ship a mismatched pair from the build
  machine's PATH, and the build script runs the artifact's `--self-check`
  (imports and constructs everything a normal start does, off-screen) before
  declaring success. A one-file build was not chosen: it unpacks at every
  start and hides the payload from inspection. The build is version-bounded
  (a committed spec, dependency ranges in `client/pyproject.toml`), scripted,
  CI-executed and self-checked. It is **not** reproducible and nothing in it
  attempts that: there is no client lock file, the interpreter patch release
  and the `windows-latest` image both float, and there is no timestamp
  normalisation, hash-seed pinning or two-build comparison. Two builds may
  therefore differ in both inputs and bytes. Not because the property would be
  worthless: a byte-identical rebuild is what lets a recipient rebuild in an
  independent environment and check that the binary they hold corresponds to
  this source, and nothing here offers that. It answers a different question
  from the one this distribution has, though. The artifact goes by hand from
  the owner to the owner, so what is unanswered is authenticity of origin,
  which signing addresses and a byte-identical rebuild does not. The check
  that pays for itself at this scale is the artifact's own start-up
  self-check. Recorded as a candidate control, not as a rejected one.
- **No code signing** (`WuwaTerm.spec` sets `codesign_identity=None`;
  `client/build.ps1` and `client/README.md` state it). Accepted cost:
  first-run SmartScreen friction on a machine that has never seen the binary.
  The client is owner-distributed by hand, so a certificate buys little.
- **Never published to a package index.** `client/pyproject.toml` carries the
  `Private :: Do Not Upload` classifier, which the public index rejects, and
  the package lives outside the server wheel (its own `pyproject.toml` under
  `client/`, not under `src/`).

Windows is the only packaged target; `config.py` falls back to
`~/.config/WuwaTerm` off Windows for development runs, but no non-Windows
artifact is built.

### Plain-text contract

The client is one more caller of the same versioned API: `POST
/v1/translations`, `GET /v1/terms`, `GET /v1/meta`, every response field a
direct pass-through of `docs/api/openapi.json`
(`client/src/wuwaterm_client/api.py` module docstring and response models).
It renders plain text only. No Telegram markup, entity, or chat concept
appears in any user-facing string: every displayed literal lives in
`client/src/wuwaterm_client/strings.py`. The source scanners that once
enforced this and the absence of Telegram-runtime markers were retired in
2026-11; review and the real Qt tests own these boundaries now.

### Transport policy

The device token travels in an `Authorization: Bearer` header on every
request, so the transport rules exist to keep that header off any wire that
is not protected. All of them are enforced in code, not only in the settings
dialog, because `ApiClient` is reachable without the dialog (a hand-edited
config file, a future caller).

- **HTTPS with certificate verification for any non-loopback host.**
  `endpoint_is_confidential` (`config.py`) accepts `https://` to any host;
  `api.py` constructs its `httpx.AsyncClient` with an explicit `verify=True`
  and no code path that changes it. `client/tests/test_transport_security.py`
  pins this three ways:
  `test_certificate_verification_is_on_for_the_client_it_actually_builds`
  inspects the SSL context handed to the connection pool
  (`CERT_REQUIRED`, `check_hostname`),
  `test_the_client_asks_for_verification_explicitly` records the constructor
  arguments, and `test_the_client_exposes_no_way_to_turn_verification_off`
  asserts no `verify`/`insecure`-shaped parameter exists. The private
  `_test_transport` seam is a private test hook, not a security boundary;
  production clients always verify (`client/tests/test_transport_security.py`).
- **Plain HTTP only to this machine's loopback.** `_is_loopback`
  (`config.py`) accepts the literal names `localhost` and `::1` and any
  address `ipaddress` classifies as loopback - so the name `localhost` IS
  accepted for plain HTTP, not only numeric forms. The name is matched
  textually, trusting the platform convention that it resolves to this
  machine. A loopback-looking non-loopback host
  (`127.0.0.1.example.com`) is refused
  (`test_the_transport_refuses_an_address_it_cannot_protect`;
  `client/tests/test_config.py::test_plain_http_is_only_accepted_for_this_machine`).
  There is no shipped default address (amended 2026-08-12, see below); the
  service's default API port (8788) appears only as the settings field's
  placeholder text.
- **No address is better than a wrong one** (amended 2026-08-12). The client
  originally defaulted `base_url` to plain HTTP on this machine's loopback
  and fell back to it whenever the stored setting was missing or unusable.
  A real incident retired that: `%APPDATA%\WuwaTerm\config.json` was found
  missing after a Windows restart, the client silently adopted the loopback
  address, and the resulting "could not reach the server" described a machine
  the owner had never configured it to call. `ClientConfig.base_url` is now
  `str | None`, `None` meaning UNCONFIGURED; the main window displays the
  configured address or says it has none, and every request path refuses with
  the client-only code `not_configured`, whose message names Settings
  (`client/tests/test_unconfigured_endpoint.py`,
  `client/tests/test_ui_endpoint_state.py`). `ClientConfig.save` writes
  through a temporary file and `os.replace`, so an interrupted save cannot
  leave a truncated file for the next launch to reject
  (`client/tests/test_config_persistence.py`).
- **One predicate at every layer.** `usable_base_url` (`config.py`) is
  applied by the settings dialog, by `ClientConfig.load` (an unusable saved
  address leaves the client unconfigured rather than reaching the transport),
  and by the `ApiClient` constructor and `update_base_url`. It additionally
  refuses user information, a query, or a fragment embedded in the address
  (`test_the_transport_is_no_more_permissive_than_the_settings_field`,
  `test_a_stored_configuration_can_never_carry_a_refused_address`). A
  refused update leaves the previous address in effect
  (`test_a_refused_address_leaves_the_running_client_where_it_was`).
- **Request-target guard, before the credential header.**
  `ApiClient._guard_request_target` (`api.py`) resolves each request path
  against the configured base and refuses, with the stable code
  `insecure_endpoint`, any target that carries user information, a query, or
  a fragment, or whose scheme, host, or port differ from the configured
  origin - and it runs before `_headers()` attaches the Bearer header. This
  closes the httpx behavior where an absolute URL overrides the base
  entirely, and the historical hole where embedded user information became a
  Basic credential that overwrote the device token
  (`client/tests/test_api.py::test_an_absolute_url_is_refused_before_the_token_is_attached`,
  `test_a_url_carrying_embedded_credentials_is_refused_before_the_token`,
  `test_the_request_guard_and_the_constructor_apply_one_policy`).
- **Redirects are not followed.** The client never enables redirect
  following on its `httpx.AsyncClient`, so httpx's default applies: a
  redirect answer is returned as-is, fails response parsing, and surfaces as
  the client's own error state instead of being chased to a new origin.
- **No environment proxy trust.** `trust_env=False` keeps a machine-level
  proxy variable from silently rerouting requests - credential included -
  away from the configured address
  (`client/tests/test_api.py::test_the_client_does_not_trust_environment_proxies`).

### No shell channel in the product path

The client reaches the service by HTTPS requests to the configured secure
endpoint, and by nothing else. It starts no processes, opens no shell, and
holds no key material. SSH remains the operator's administration channel for
the host; the note in `client/README.md` says it is not part of the client's
path. The wording scanners that once policed this were retired in 2026-11;
`client/tests/test_transport_security.py` covers the real TLS behavior.

### Credential lifecycle

- **First run.** `app.run` calls `MainWindow.ensure_credential`; with no
  stored token, the first-run dialog asks for the operator-issued device
  token and `store_token` writes it to the OS credential store (service
  `WuwaTerm`, entry `device-token`). Declining quits without a window.
- **Storage.** The token lives only in the Windows Credential Manager via
  `keyring`; `credentials.py` is the only module that touches it, and
  `config.py` never sees it. The config file holds no secret
  (`client/tests/test_config.py::test_config_file_never_contains_the_credential`).
- **Reuse.** Every request reads the token through the token provider at
  send time (`ApiClient._headers`), so a stored credential survives restart
  with no re-entry and a changed one takes effect without one.
- **Change and removal.** Settings offers Enter/Change token and Forget
  token (with confirmation); `delete_token` removes the entry and treats
  "nothing to delete" as success, while a vault that cannot be used is
  reported, not swallowed (`client/tests/test_credentials.py`). A store
  failure or a token that cannot be an HTTP header is rendered as the stable
  `unauthorized` state rather than a crash
  (`client/tests/test_api.py::test_a_credential_store_failure_becomes_a_client_error`,
  `test_a_credential_that_cannot_be_a_header_is_reported_as_unusable`).

### Timeouts, cancellation, stable errors

- Two configurable timeouts (`config.py`): `request_timeout_seconds`
  (default 10 s) for lookups and status, `translate_timeout_seconds`
  (default 60 s) for `POST /v1/translations`. Values from the dialog or a
  hand-edited file are clamped to 1-600 s, with non-finite JSON numbers
  rejected (`_sane_timeout`;
  `client/tests/test_config.py::test_a_hand_edited_timeout_is_clamped_rather_than_trusted`,
  `test_a_non_finite_timeout_falls_back_to_the_default`). httpx applies the
  value per phase (connect, read, write, pool acquisition), so it bounds
  waiting without progress, not total wall clock; the translate value is
  passed per request (`ApiClient.translate`).
- Cancellation: the Cancel button cancels the asyncio task;
  `ApiClient._request` consumes the `CancelledError` and reports the stable
  code `cancelled`, deliberately completing the awaiting task instead of
  propagating cancellation, so the view always renders an outcome
  (`client/tests/test_api.py::test_cancel_reports_cancellation_not_a_generic_error`;
  `client/tests/test_translate_view_status.py::test_a_cancelled_request_reports_that_it_was_cancelled`;
  the view also covers a cancel that lands between awaits or before the task
  starts). **Its scope is this process.** The `CancelledError` is caught around
  the whole `httpx` call — pool acquisition, connect, handshake and body write
  included — so a cancel anywhere before the service has the WHOLE request body
  leaves it with nothing to act on: no translation, no model spend, and at most
  a `499` from a disconnect during the read. That window, not "before the task
  starts", is the boundary, and for a short body it is short. Once the service
  has the whole request, nothing that reaches it cancels anything: the work
  continues, a model call in flight is still paid for (a dictionary hit
  returns before the model stage and costs nothing), and the server records
  an ordinary completion rather than a client-gone `499` — the log side of
  that is in `docs/deployment.md`, the user-facing side in
  `client/README.md`. Making cancel actually cancel is a server-side change
  (a disconnect the service watches for, and orchestration that unwinds on
  it), not a client wording change, and it is not implemented.
- Stable error rendering (`errors.py`, `strings.py`): connect-level failures
  become `offline`, a deadline becomes `timeout` (including the service's
  own 504, remapped so a server-side deadline reads as a timeout), a
  rejected credential becomes `unauthorized` with a message pointing at
  Settings, and a body that is not this service's becomes `unknown` instead
  of a stack trace
  (`client/tests/test_api.py::test_connect_refused_renders_offline_message`,
  `test_timeout_renders_timeout_message`,
  `test_a_server_side_deadline_is_reported_as_a_timeout`,
  `test_each_stable_error_code_renders_mapped_message`). Nothing is retried
  automatically: a translate request that already reached the service may
  have spent model budget.

## Consequences

- Positive: the whole client suite runs offline - mocked transport, fake
  keyring, no server - and the transport policy is enforced by constructors
  and guards, not by documentation.
- Positive: the only secret never touches disk in plain form, and the
  repository gates make both regressions (a shell-channel client design, a
  verification toggle) a failing pull request instead of a review catch.
- Negative: no code signing means SmartScreen friction on first run, and
  hand distribution is the only channel.
- Negative: a PySide6 one-folder artifact is large; accepted for an
  owner-only tool.
- Constraint: plain HTTP accepts the textual name `localhost` as loopback
  without resolving it; the guarantee leans on the platform keeping that
  name local.
- Constraint: Windows is the only packaged target, and the client is never
  published to any package index.

## Evidence

- `client/pyproject.toml`, `client/build.ps1`, `client/WuwaTerm.spec`,
  `client/main.py`, `client/README.md`
- `client/src/wuwaterm_client/config.py` (`endpoint_is_confidential`,
  `usable_base_url`, `_is_loopback`, `_sane_timeout`)
- `client/src/wuwaterm_client/api.py` (`ApiClient`,
  `_guard_request_target`, `_require_verifying_transport`,
  `_require_confidential_endpoint`)
- `client/src/wuwaterm_client/credentials.py`, `errors.py`, `strings.py`,
  `app.py`, `ui/`
- `client/tests/test_transport_security.py`, `test_api.py`,
  `test_config.py`, `test_credentials.py`,
  `test_translate_view_status.py`, `test_packaging_entry.py`
- [ADR 0009](0009-http-api-adapter.md),
  [ADR 0010](0010-device-principal-authentication.md),
  [ADR 0012](0012-client-transport-selection.md)
