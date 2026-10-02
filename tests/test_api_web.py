"""The owner-private web presentation layer.

Everything here runs in-process against the ASGI app, in the same style as
tests/test_api.py. The properties under test are the ones that make this
surface safe to expose at all:

- with the switch off it does not exist, so nothing about the API changes;
- a request that did not come through the edge is refused before it reaches
  application logic;
- no credential material ever reaches the browser;
- it spends the SAME rate limiter and the SAME call budget as the API, rather
  than a second set that would raise the deployment's aggregate ceiling;
- it enforces the same scopes the API enforces over the same pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path

import httpx
import pytest

from wuwaterm_api.app import create_app
from wuwaterm_api.auth import SCOPE_META, SCOPE_TRANSLATE, TOKEN_SCHEME, DeviceStore
from wuwaterm_api.settings import WEB_MOUNT_PATH, ApiSettings
from wuwaterm_api.web import render
from wuwaterm_api.web.app import EDGE_HEADER_NAME, SESSION_COOKIE_NAME
from wuwaterm_api.web.session import SessionStore

ROOT = Path(__file__).resolve().parents[1]

EDGE_SECRET = "edge-marker-material-for-tests-0123456789"
DEVICE_SECRET = "unguessable-material-for-tests-0123456789abcdef-web"


def run(coro):
    return asyncio.run(coro)


async def call(app, method: str, url: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    # Cookies belong on the client, not the request: per-request cookies are
    # deprecated in httpx and warn on every call that passes them.
    cookies = kwargs.pop("cookies", None)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://api.test", cookies=cookies
    ) as client:
        return await client.request(method, url, **kwargs)


def edge() -> dict[str, str]:
    return {EDGE_HEADER_NAME: EDGE_SECRET}


def form(**fields) -> dict[str, str]:
    """A urlencoded body. The web layer accepts no other content type."""
    return fields


def session_cookie(app) -> dict[str, str]:
    """Load a page first, the way a browser reaching this surface does.

    A POST cannot mint a session - that restriction is what closes cross-site
    request forgery - so every POST test has to arrive holding a cookie it got
    from a GET, exactly as the owner's browser does after loading the form.
    """
    page = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    assert page.status_code == 200, page.status_code
    return {SESSION_COOKIE_NAME: page.cookies[SESSION_COOKIE_NAME]}


def build_web_app(tmp_path: Path, db_path: Path, *, scopes=None, **overrides):
    """An app with the web layer switched ON and a device behind it."""
    store = DeviceStore(tmp_path / "api-state" / "devices.db")
    store.initialize()
    device = store.issue("web", scopes, secret=DEVICE_SECRET)
    token = f"{TOKEN_SCHEME}.{device.device_id}.{DEVICE_SECRET}"
    defaults = dict(
        db_path=db_path,
        device_db_path=tmp_path / "api-state" / "devices.db",
        rate_limit_per_minute=100,
        llm_calls_per_minute=100,
        max_body_bytes=8192,
        request_timeout_seconds=30.0,
        web_enabled=True,
        web_device_token=token,
        web_edge_secret=EDGE_SECRET,
    )
    defaults.update(overrides)
    settings = ApiSettings(**defaults)
    app = create_app(settings, device_store=store)
    return app, store, device, token


def mounted_web_app(app):
    """The sub-application, or None when the switch left it unmounted."""
    for route in app.routes:
        if route.__class__.__name__ == "Mount" and route.path == WEB_MOUNT_PATH:
            return route.app
    return None


def _env_names_read_by_settings() -> set[str]:
    """Every environment variable from_env actually reads, DERIVED not listed.

    Parsed out of the settings module's own source, so a variable added there
    tomorrow is covered by the assertion below without anyone remembering to
    add it here. A hand-written list would need the same edit as the code and
    would be forgotten in the same breath - which is precisely how the first
    version of this fix covered the boolean switch and missed the two integer
    settings introduced in the same commit.
    """
    import ast
    import inspect

    from wuwaterm_api import settings as settings_module

    tree = ast.parse(inspect.getsource(settings_module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        label = getattr(target, "id", None) or getattr(target, "attr", None)
        if label not in {"getenv", "_env_int", "_env_float", "_env_path"}:
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            value = node.args[0].value
            if isinstance(value, str) and value.startswith("WUWATERM"):
                names.add(value)
    return names


def test_no_setting_read_by_from_env_can_block_device_revocation(
    tmp_path, sample_db, monkeypatch, capsys
):
    """FULL-SET ASSERTION over the environment dimension, derived not listed.

    `from_env()` runs for EVERY subcommand, `device revoke` included, so a
    reader that raises there gates credential revocation on the spelling of an
    unrelated setting. Fixing the boolean switch alone left the two integer
    settings added in the same commit still raising - the fix was complete for
    the reported case and incomplete for the property.

    So this enumerates the variables the settings module actually reads, drives
    the REAL CLI with a bad value for each, and requires revocation to succeed.
    No serve-only variable is exempt: adding a new strict reader fails here
    rather than quietly making credential revocation configuration-dependent.
    """
    from wuwaterm_api.cli import main as cli_main

    discovered = _env_names_read_by_settings()
    assert discovered, "the derivation found nothing - it has broken"
    must_be_safe = sorted(discovered)

    # The property is that reading the environment does not RAISE - that is the
    # mechanism by which an unrelated setting blocks revocation. Asserted on
    # from_env directly rather than through the CLI, because two of these
    # variables point AT the credential store, so feeding them garbage makes
    # revocation fail for an honest reason and would report the wrong thing.
    raised = []
    for name in must_be_safe:
        monkeypatch.setenv(name, "!!not-a-valid-value!!")
        try:
            ApiSettings.from_env()
        except Exception as exc:  # noqa: BLE001 - any raise is the defect
            raised.append((name, f"{type(exc).__name__}: {exc}"))
        finally:
            monkeypatch.delenv(name, raising=False)
    assert not raised, (
        "a bad value in these settings raises from from_env(), which runs on "
        "every subcommand and so blocks `device revoke`: " + repr(raised)
    )

    # ...and one end-to-end run through the real CLI, on a variable that does
    # not point at the store, to show the property is the one that matters.
    store_path = tmp_path / "api-state" / "devices.db"
    store = DeviceStore(store_path)
    store.initialize()
    device = store.issue("compromised", secret=DEVICE_SECRET)
    monkeypatch.setenv("WUWATERM_API_DEVICE_DB_PATH", str(store_path))
    monkeypatch.setenv("WUWATERM_DB_PATH", str(sample_db))
    monkeypatch.setenv("WUWATERM_API_PORT", "not-an-integer")
    monkeypatch.setenv("WUWATERM_API_LLM_TIMEOUT_SECONDS", "not-a-number")
    assert cli_main(["device", "revoke", "--device-id", device.device_id]) == 0
    capsys.readouterr()


def test_a_mistyped_web_switch_does_not_block_device_revocation(
    tmp_path, sample_db, monkeypatch, capsys
):
    """Credential revocation must never depend on a presentation-layer typo.

    `from_env()` runs for EVERY subcommand, `device revoke` included. A reader
    that raised on an unrecognised value therefore made a misspelled
    serve-only web flag - `treu` - prevent revoking a compromised device until
    the environment was repaired. The precedent against this was already stated
    in prose in the same file, twenty lines from where the reader was added,
    and prose did not prevent it; this test does.
    """
    from wuwaterm_api.cli import main as cli_main
    from wuwaterm_api.settings import ApiConfigError, validate_web_enabled

    store_path = tmp_path / "api-state" / "devices.db"
    store = DeviceStore(store_path)
    store.initialize()
    device = store.issue("compromised", secret=DEVICE_SECRET)

    monkeypatch.setenv("WUWATERM_API_WEB_ENABLED", "treu")
    monkeypatch.setenv("WUWATERM_API_DEVICE_DB_PATH", str(store_path))
    monkeypatch.setenv("WUWATERM_DB_PATH", str(sample_db))

    # from_env itself must not raise...
    settings = ApiSettings.from_env()
    assert settings.web_enabled is False, "an unreadable value must not turn it on"
    assert settings.web_enabled_raw == "treu"

    # ...and the operator command that matters must succeed.
    assert cli_main(["device", "revoke", "--device-id", device.device_id]) == 0
    capsys.readouterr()
    assert store.authenticate(
        f"{TOKEN_SCHEME}.{device.device_id}.{DEVICE_SECRET}"
    ) is None, "the device really was revoked"

    # The strictness is not lost, it moved to the path where refusing is right.
    try:
        validate_web_enabled("treu")
    except ApiConfigError:
        pass
    else:  # pragma: no cover - the assertion below reports it
        raise AssertionError("the serve path must still refuse a typo")


# --------------------------------------------------------------- the switch


def test_the_web_layer_is_absent_by_default(tmp_path, sample_db):
    """Default OFF. Not "mounted but refusing" - absent.

    This is what makes "the API is unchanged" checkable rather than argued: if
    nothing is mounted, there is no code path to reason about.
    """
    settings = ApiSettings(
        db_path=sample_db, device_db_path=tmp_path / "api-state" / "devices.db"
    )
    assert settings.web_enabled is False
    store = DeviceStore(settings.device_db_path)
    store.initialize()
    app = create_app(settings, device_store=store)
    assert mounted_web_app(app) is None
    response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/"))
    assert response.status_code == 404


def test_the_switch_does_not_change_the_published_api_document(tmp_path, sample_db):
    """The OpenAPI document is byte-identical with the layer on and off.

    A mounted sub-application is a Mount, not an APIRoute, so it contributes no
    schema. Pinning that here means a future change that turned the web layer
    into API routes would be caught by this test rather than by a client that
    suddenly sees new paths.
    """
    off = ApiSettings(
        db_path=sample_db, device_db_path=tmp_path / "off" / "devices.db"
    )
    DeviceStore(off.device_db_path).initialize()
    app_off = create_app(off, device_store=DeviceStore(off.device_db_path))
    app_on, _store, _device, _token = build_web_app(tmp_path, sample_db)

    doc_off = run(call(app_off, "GET", "/openapi.json")).json()
    doc_on = run(call(app_on, "GET", "/openapi.json")).json()
    assert doc_off == doc_on
    assert not [path for path in doc_on["paths"] if "wuwaterm-web" in path]


# ------------------------------------------------- criterion 5: one instance


def test_the_web_layer_spends_the_api_s_own_limiter_and_budget(tmp_path, sample_db):
    """Identity, not equality.

    Two SlidingWindowRateLimiter objects with the same limit compare equal in
    every way a test might casually check and are still two independent
    buckets. The property that matters is that there is ONE object, so this
    asserts `is`.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    web = mounted_web_app(app)
    assert web is not None
    assert web.state is app.state
    assert web.state.rate_limiter is app.state.rate_limiter
    assert web.state.llm_budget is app.state.llm_budget
    assert web.state.term_service is app.state.term_service
    assert web.state.translator is app.state.translator
    assert web.state.device_store is app.state.device_store


def test_an_established_session_re_runs_no_credential_derivation(
    tmp_path, sample_db, monkeypatch
):
    """What the shared-process cost actually is, pinned so it cannot drift.

    Sharing the rate limiter with the desktop client is an accepted cost, and
    its weight depends entirely on this: a browser request that re-verified the
    credential would spend a deliberate ~16 MiB scrypt AND one of the bounded
    admission slots the desktop client sheds 429 from, on every page view. It
    does not. Verification happens once, when the session is created; after
    that a request costs one bucket token and a cheap liveness read.

    Counted at hashlib.scrypt - the real derivation - rather than by reading
    the call graph, because the call graph is what a regression would change.
    """
    # Built BEFORE the counter is installed: issuing the device runs a
    # derivation of its own, and counting it would make the first assertion
    # below read 2 for a reason that has nothing to do with serving requests.
    app, _store, _device, token = build_web_app(tmp_path, sample_db)

    calls = []
    real_scrypt = hashlib.scrypt

    def counting_scrypt(*args, **kwargs):
        calls.append(1)
        return real_scrypt(*args, **kwargs)

    monkeypatch.setattr(hashlib, "scrypt", counting_scrypt)
    slots = []
    real_acquire = app.state.auth_slots.acquire
    monkeypatch.setattr(
        app.state.auth_slots,
        "acquire",
        lambda *a, **k: (slots.append(1), real_acquire(*a, **k))[1],
    )

    first = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    assert first.status_code == 200
    assert len(calls) == 1, "session creation verifies the credential exactly once"
    assert len(slots) == 1
    jar = {SESSION_COOKIE_NAME: first.cookies[SESSION_COOKIE_NAME]}

    for _ in range(10):
        later = run(
            call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge(), cookies=jar)
        )
        assert later.status_code == 200
    assert len(calls) == 1, "an established session must not re-derive"
    assert len(slots) == 1, "an established session must not take an admission slot"

    # For contrast, and to show the counter is live: the JSON API's shape
    # presents a credential on every request and pays for it every time.
    for _ in range(3):
        run(call(app, "GET", "/v1/meta", headers={"Authorization": f"Bearer {token}"}))
    assert len(calls) == 4


def test_the_model_call_budget_is_one_account_for_every_principal(
    tmp_path, sample_db, monkeypatch
):
    """What actually protects the money, measured across TWO principals.

    The rate limiter buckets by device id, so two devices get two allowances -
    which is expected for an owner-private deployment where the devices are all
    one person's, and is not what "no new amplification surface" was ever about.
    Spending is different: a second spending ceiling would be a second bill.

    So this uses the two devices the deployment guide actually creates - one for
    the desktop client, one for the web surface - rather than reusing a single
    principal, which is what made the earlier bucket test vacuous.
    """
    calls = []

    async def respond(locked_text, locks):
        return locked_text

    async def fake_call(locked_text, locks, **kwargs):
        calls.append(locked_text)
        return await respond(locked_text, locks)

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)

    store = DeviceStore(tmp_path / "api-state" / "devices.db")
    store.initialize()
    desktop_secret = "desktop-client-material-0123456789abcdef-x"
    web_secret = "web-surface-material-0123456789abcdef-x"
    desktop = store.issue("desktop", secret=desktop_secret)
    web = store.issue("web", secret=web_secret)
    assert desktop.device_id != web.device_id
    settings = ApiSettings(
        db_path=sample_db,
        device_db_path=tmp_path / "api-state" / "devices.db",
        rate_limit_per_minute=1000,
        llm_calls_per_minute=2,
        web_enabled=True,
        web_device_token=f"{TOKEN_SCHEME}.{web.device_id}.{web_secret}",
        web_edge_secret=EDGE_SECRET,
    )
    app = create_app(settings, device_store=store)
    bearer = {"Authorization": f"Bearer {TOKEN_SCHEME}.{desktop.device_id}.{desktop_secret}"}
    sentence = "今汐在云陵谷与守岸人交谈了很久然后离开了。"

    # The desktop principal spends the whole allowance.
    for _ in range(2):
        spent = run(call(app, "POST", "/v1/translations", headers=bearer,
                         json={"text": sentence}))
        assert spent.status_code == 200, spent.text
    exhausted = run(call(app, "POST", "/v1/translations", headers=bearer,
                         json={"text": sentence}))
    assert exhausted.status_code == 503
    assert exhausted.json()["error"]["code"] == "llm_budget_exhausted"

    # The web principal is a DIFFERENT device, and must find the same account
    # already empty. A second account here would be a second bill.
    page = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    jar = {SESSION_COOKIE_NAME: page.cookies[SESSION_COOKIE_NAME]}
    web_attempt = run(
        call(app, "POST", f"{WEB_MOUNT_PATH}/translate", headers=edge(),
             cookies=jar, data=form(text=sentence))
    )
    assert web_attempt.status_code == 503
    assert "翻译额度已用尽" in web_attempt.text, web_attempt.text[:400]
    # Two principals, one account: the model was called exactly the budget.
    assert len(calls) == 2, calls


def test_admission_is_bucketed_per_device_across_both_surfaces(
    tmp_path, sample_db
):
    """Admission is per DEVICE, and this test says so rather than hiding it.

    It replaces one that claimed browser requests spend the desktop client's
    bucket. That test drove both surfaces with a SINGLE device, which makes
    "the same device shares a bucket" true by definition — a limiter keyed by
    device id cannot do anything else — so it proved nothing about the two
    devices the deployment guide actually creates, and the claim it appeared to
    support was false for that deployment.

    Both halves are asserted here: one device really does share, and two
    devices really do not. The second half is the deployed shape, and it is
    expected behaviour rather than a defect — every principal in this
    deployment is one of the owner's own devices, so a second allowance for his
    browser is him using two of his things at once. What must NOT double is
    spending, and that is pinned separately by the budget test above.
    """
    # Same device on both surfaces: one bucket, necessarily.
    app, _store, _device, token = build_web_app(
        tmp_path, sample_db, rate_limit_per_minute=3
    )
    for _ in range(3):
        spent = run(call(app, "GET", "/v1/meta",
                         headers={"Authorization": f"Bearer {token}"}))
        assert spent.status_code == 200
    assert run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge())).status_code == 429

    # Two devices, as the deployment guide creates them: two allowances.
    store = DeviceStore(tmp_path / "two" / "devices.db")
    store.initialize()
    desktop_secret = "desktop-material-0123456789abcdef-two"
    web_secret = "web-material-0123456789abcdef-two"
    desktop = store.issue("desktop", secret=desktop_secret)
    web = store.issue("web", secret=web_secret)
    settings = ApiSettings(
        db_path=sample_db,
        device_db_path=tmp_path / "two" / "devices.db",
        rate_limit_per_minute=2,
        web_enabled=True,
        web_device_token=f"{TOKEN_SCHEME}.{web.device_id}.{web_secret}",
        web_edge_secret=EDGE_SECRET,
    )
    two = create_app(settings, device_store=store)
    desktop_auth = {"Authorization": f"Bearer {TOKEN_SCHEME}.{desktop.device_id}.{desktop_secret}"}
    for _ in range(2):
        assert run(call(two, "GET", "/v1/meta", headers=desktop_auth)).status_code == 200
    assert run(call(two, "GET", "/v1/meta", headers=desktop_auth)).status_code == 429
    # The web principal has its own allowance, and that is the documented shape.
    assert run(call(two, "GET", f"{WEB_MOUNT_PATH}/", headers=edge())).status_code == 200


# --------------------------------------- criterion 2: refused before logic


def test_a_request_without_the_edge_marker_is_refused(tmp_path, sample_db):
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/"))
    assert response.status_code == 404
    # 404, not 401 or 403: a caller that bypassed the edge learns nothing about
    # what is mounted here, including whether anything is.
    assert response.text == ""


def test_a_request_with_the_wrong_edge_marker_is_refused(tmp_path, sample_db):
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    response = run(
        call(app, "GET", f"{WEB_MOUNT_PATH}/", headers={EDGE_HEADER_NAME: "wrong"})
    )
    assert response.status_code == 404


def test_the_refusal_happens_before_the_credential_store_is_touched(
    tmp_path, sample_db
):
    """The ordering claim, made falsifiable.

    Asserting a status code proves the request was refused; it does not prove
    WHERE. This replaces the credential store with one that raises if anything
    reaches it, so a future edit that resolved the session before checking the
    edge would fail here rather than pass quietly.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)

    class Tripwire:
        def authenticate(self, *_args, **_kwargs):
            raise AssertionError("credential store reached before the edge check")

        def is_active(self, *_args, **_kwargs):
            raise AssertionError("credential store reached before the edge check")

    app.state.device_store = Tripwire()
    assert run(call(app, "GET", f"{WEB_MOUNT_PATH}/")).status_code == 404


def test_every_path_under_the_mount_refuses_identically(tmp_path, sample_db):
    """One answer for everything off-edge, or the refusal is an oracle.

    Each of these used to answer differently and each difference told a caller
    that never presented the marker something about what is mounted here:
    the router's 303 on an unguarded route, its 307 on a trailing slash, its
    405 on a known path with the wrong method, and its `Not Found` BODY on an
    unknown one. A refusal that varies is a map.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    probes = (
        ("GET", f"{WEB_MOUNT_PATH}/"),
        ("GET", f"{WEB_MOUNT_PATH}/lookup"),
        ("GET", f"{WEB_MOUNT_PATH}/lookup/"),
        ("GET", f"{WEB_MOUNT_PATH}/translate"),
        ("GET", f"{WEB_MOUNT_PATH}/translate/"),
        ("POST", f"{WEB_MOUNT_PATH}/"),
        ("POST", f"{WEB_MOUNT_PATH}/translate"),
        ("GET", f"{WEB_MOUNT_PATH}/does-not-exist"),
        ("DELETE", f"{WEB_MOUNT_PATH}/lookup"),
    )
    seen = set()
    for method, url in probes:
        response = run(call(app, method, url))
        seen.add((response.status_code, response.text))
    assert seen == {(404, "")}, seen


def test_a_trailing_slash_is_not_a_route_even_on_edge(tmp_path, sample_db):
    """Pins the SECOND of the two controls, independently of the first.

    Off-edge uniformity is enforced by the gate that refuses above the router,
    so a test that only probes without the marker passes whether or not the
    router still redirects - it is measuring the gate. This probes WITH a valid
    marker, where the router is actually reached, so it fails if
    redirect_slashes comes back on. Two controls, two assertions; otherwise the
    inner one is carried by the outer one and nothing says so.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    for url in (f"{WEB_MOUNT_PATH}/translate/", f"{WEB_MOUNT_PATH}/lookup/"):
        response = run(call(app, "GET", url, headers=edge()))
        assert response.status_code == 404, (url, response.status_code)


def test_the_bare_mount_path_does_not_redirect_off_edge(tmp_path, sample_db):
    """The oracle that survived one level up.

    A Mount matches only the slash-prefixed remainder, so the exact path
    `/wuwaterm-web` never enters the sub-application - and the PARENT router's
    slash redirect answered it with a 307 before the edge gate could look at
    anything. Turning off redirect_slashes on the child could not fix that; the
    redirect belonged to the parent. Measured before the fix: 307 with a
    Location header and no hardening headers, while every other path under the
    mount answered a bare 404.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    off_edge = run(call(app, "GET", WEB_MOUNT_PATH))
    assert off_edge.status_code == 404, off_edge.status_code
    assert off_edge.text == ""
    assert "location" not in off_edge.headers
    assert off_edge.headers["cache-control"] == "no-store"
    # With the marker it is a normal redirect onto the slashed form.
    on_edge = run(call(app, "GET", WEB_MOUNT_PATH, headers=edge()))
    assert on_edge.status_code == 307
    assert on_edge.headers["location"] == f"{WEB_MOUNT_PATH}/"
    # ...and the API's own routes are untouched by the inserted route.
    assert run(call(app, "GET", "/healthz")).status_code == 200


def test_a_device_revoked_before_the_first_request_mints_no_session(
    tmp_path, sample_db
):
    """The easy half: already revoked when the request arrives.

    Closed by credential verification itself, not by the admission re-check -
    which is exactly why this test alone does NOT prove the race below is
    handled, and why both exist.
    """
    app, store, device, _token = build_web_app(tmp_path, sample_db)
    store.revoke(device.device_id)
    response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    assert response.status_code == 401
    assert SESSION_COOKIE_NAME not in response.cookies
    assert len(app.state.web_sessions) == 0


def test_a_revocation_committing_inside_the_mint_window_is_caught(
    tmp_path, sample_db
):
    """The actual race, driven rather than described.

    The window is between the credential verifying and the session being
    created. Verification has already returned a live principal by then, so
    nothing on that path can notice; only a re-check at admission can. The
    revocation is committed from inside the verification call itself, which is
    the narrowest reproduction of "it committed while the request was in
    flight".
    """
    app, store, device, _token = build_web_app(tmp_path, sample_db)
    real_authenticate = store.authenticate

    def authenticate_then_revoke(token):
        verified = real_authenticate(token)
        # Commits AFTER verification succeeded and BEFORE the session is made.
        store.revoke(device.device_id)
        return verified

    store.authenticate = authenticate_then_revoke
    response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    assert response.status_code == 401
    assert SESSION_COOKIE_NAME not in response.cookies
    assert len(app.state.web_sessions) == 0


def test_the_authenticated_principal_reaches_the_completion_log(
    tmp_path, sample_db, caplog
):
    """Browser traffic must be attributable to the device that spent the budget.

    The parent's completion log reads the principal only from request.state,
    so without the assignment every web request logged no principal at all -
    on the one surface whose accepted cost is that it spends the desktop
    client's allowance.

    Asserted on the log line the parent actually emits for a real request, not
    on a constructed object: the point is that the principal survives the whole
    request path into the record an operator would read.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    with caplog.at_level(logging.INFO, logger="wuwaterm_api"):
        response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    assert response.status_code == 200
    completions = [
        r.getMessage() for r in caplog.records if "request complete" in r.getMessage()
    ]
    assert completions, [r.getMessage() for r in caplog.records]
    # "device=-" is the no-principal marker. Any web request logging it is a
    # request nobody can attribute to the device that spent the shared budget.
    assert not [line for line in completions if "device=-" in line], completions


def test_the_mount_point_and_its_children_answer_identically_off_edge(
    tmp_path, sample_db
):
    """The standing assertion, across BOTH dimensions: path AND method.

    The first version of the mount-point fix covered the path dimension only -
    it was registered for GET and HEAD, so an off-edge POST was answered 405 by
    the parent router, unhardened, while GET was correctly refused. The same
    oracle, reached by changing the verb. A test that varied only the path
    would have stayed green through that, which is exactly why this one varies
    both and compares the full response shape rather than the status alone.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    # DERIVED, not listed: the standard library's own enumeration of HTTP
    # methods. A hand-written tuple is what let the first version of the
    # mount-point fix cover GET and HEAD and leave POST, PUT and DELETE
    # answering 405 from the parent - the list and the fix would have needed
    # the same edit, and got neither.
    import http

    methods = tuple(sorted(m.value for m in http.HTTPMethod))
    assert "GET" in methods and "POST" in methods and len(methods) >= 9, methods
    paths = (
        WEB_MOUNT_PATH,
        f"{WEB_MOUNT_PATH}/",
        f"{WEB_MOUNT_PATH}/lookup",
        f"{WEB_MOUNT_PATH}/translate",
        f"{WEB_MOUNT_PATH}/translate/",
        f"{WEB_MOUNT_PATH}/does-not-exist",
    )
    shapes = {}
    for method in methods:
        for path in paths:
            r = run(call(app, method, path))
            shapes[(method, path)] = (
                r.status_code,
                r.text,
                r.headers.get("content-security-policy"),
                r.headers.get("cache-control"),
                r.headers.get("location"),
            )
    distinct = set(shapes.values())
    assert len(distinct) == 1, {k: v for k, v in shapes.items()}
    status, body, csp, cache, location = distinct.pop()
    assert status == 404
    assert body == ""
    assert location is None
    assert cache == "no-store"
    assert csp and "default-src 'none'" in csp


def test_every_response_this_surface_emits_is_hardened_and_is_a_page(
    tmp_path, sample_db
):
    """FULL-SET ASSERTION over the response-producer dimension.

    Three producers can answer a request under this mount, and each was fixed
    only after being reported separately: the views, the child ROUTER, and the
    PARENT middleware. The last is the one that keeps being forgotten because
    it never enters the sub-application at all - an oversized form is answered
    by the body limit, a slow translation by the timeout, both above the child.

    So the probes below are chosen to reach one producer each, and the
    assertion is the same for all of them: hardening headers present, and the
    body is a page rather than the JSON envelope meant for the desktop client.
    """
    app, _store, _device, _token = build_web_app(
        tmp_path, sample_db, max_body_bytes=200
    )
    jar = session_cookie(app)
    oversized = "text=" + "a" * 400

    probes = (
        ("a view", "GET", f"{WEB_MOUNT_PATH}/", {}, None),
        ("the child router, unknown path", "GET", f"{WEB_MOUNT_PATH}/nope", {}, None),
        ("the child router, wrong method", "DELETE", f"{WEB_MOUNT_PATH}/lookup", {}, None),
        ("the child router, slash variant", "GET", f"{WEB_MOUNT_PATH}/translate/", {}, None),
        (
            "the PARENT body-limit middleware",
            "POST",
            f"{WEB_MOUNT_PATH}/translate",
            {"Content-Type": "application/x-www-form-urlencoded"},
            oversized,
        ),
    )
    problems = []
    for label, method, url, extra, content in probes:
        kwargs = {"headers": {**edge(), **extra}, "cookies": jar}
        if content is not None:
            kwargs["content"] = content.encode("utf-8")
        response = run(call(app, method, url, **kwargs))
        policy = response.headers.get("content-security-policy") or ""
        if response.headers.get("cache-control") != "no-store":
            problems.append((label, response.status_code, "no cache-control"))
        if "default-src 'none'" not in policy:
            problems.append((label, response.status_code, "no CSP"))
        if response.headers.get("x-content-type-options") != "nosniff":
            problems.append((label, response.status_code, "no nosniff"))
        if response.text.lstrip().startswith("{"):
            problems.append((label, response.status_code, "JSON envelope, not a page"))
    assert not problems, problems


def test_router_generated_responses_are_hardened_too(tmp_path, sample_db):
    """`_harden` claimed to cover every response and did not.

    A 404 for an unknown child path, a 405 for a known path with the wrong
    method, and the slash-variant 404 are produced by the child ROUTER. They
    never touch a view, so they never reached the code that attaches the
    headers - the claim was true of everything this module writes and false of
    everything the framework writes.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    probes = (
        ("GET", f"{WEB_MOUNT_PATH}/does-not-exist"),
        ("DELETE", f"{WEB_MOUNT_PATH}/lookup"),
        ("GET", f"{WEB_MOUNT_PATH}/translate/"),
    )
    for method, path in probes:
        r = run(call(app, method, path, headers=edge()))
        assert r.status_code in (404, 405), (method, path, r.status_code)
        assert r.headers.get("cache-control") == "no-store", (method, path)
        policy = r.headers.get("content-security-policy") or ""
        assert "default-src 'none'" in policy, (method, path)
        assert r.headers.get("x-content-type-options") == "nosniff", (method, path)


def test_a_cross_site_sub_resource_request_cannot_mint(tmp_path, sample_db):
    """A hostile page full of image tags must not drive the verifier.

    SameSite=Strict withholds the cookie, and that does not help: the browser
    still sends the basic-auth credentials it cached for the site and the edge
    still injects its marker, so each such GET would mint - one ~16 MiB
    derivation and one session per tag, saturating the shared verifier the
    desktop client also admits through.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    hostile = {
        **edge(),
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
    }
    response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=hostile))
    assert response.status_code == 404
    assert response.text == ""
    assert "set-cookie" not in response.headers
    assert len(app.state.web_sessions) == 0

    # The owner's own navigation - typed address or bookmark - still works.
    own = {**edge(), "Sec-Fetch-Site": "none", "Sec-Fetch-Dest": "document"}
    ok = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=own))
    assert ok.status_code == 200
    assert ok.cookies[SESSION_COOKIE_NAME]


def test_usage_is_recorded_after_admission_and_for_every_request(
    tmp_path, sample_db
):
    """Accounting order, matching the JSON path rather than inverting it.

    It ran only when a session was created, so an established session never
    stamped anything and `device list` could lag a whole TTL behind real
    browser use. And it ran BEFORE the limiter, so a request that was refused
    still wrote to the credential store - the opposite of the JSON admission
    path, which records only what it admits.
    """
    app, _store, device, _token = build_web_app(
        tmp_path, sample_db, rate_limit_per_minute=2
    )
    writes = []
    real_record = app.state.device_store.record_use

    def counting_record(device_id, **kw):
        writes.append(device_id)
        return real_record(device_id, **kw)

    app.state.device_store.record_use = counting_record

    first = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    assert first.status_code == 200
    jar = {SESSION_COOKIE_NAME: first.cookies[SESSION_COOKIE_NAME]}
    assert len(writes) == 1, "the minting request records its use"

    second = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge(), cookies=jar))
    assert second.status_code == 200
    assert len(writes) == 2, "an established session records its use too"

    # The third request exceeds the limit of two: refused, and NOT recorded.
    third = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge(), cookies=jar))
    assert third.status_code == 429
    assert len(writes) == 2, "a refused request must not write to the store"


def test_a_revocation_before_the_model_call_is_caught(tmp_path, sample_db):
    """The seam before the paid work, not only the one after it.

    A revocation committing between admission and the model stage still spent
    a budget slot and a round trip, and was noticed only by the post-model
    check - after the cost was incurred. The JSON route closes this seam
    immediately before its own pipeline call.

    The post-model check alone also answers 401, so a test that only asserted
    the status code could not tell the two seams apart - and did not, until it
    was made to assert the thing that actually differs: whether the PIPELINE
    WAS ENTERED AT ALL. With the seam closed the request is refused before any
    paid work; without it the model call happens and is thrown away.
    """
    import wuwaterm_api.web.app as web_module

    app, store, device, _token = build_web_app(tmp_path, sample_db)
    jar = session_cookie(app)

    entered = []
    real_pipeline = web_module.translate_request_async

    async def spy_pipeline(*args, **kwargs):
        entered.append(1)
        return await real_pipeline(*args, **kwargs)

    monkeypatch_target = web_module
    monkeypatch_target.translate_request_async = spy_pipeline

    real_is_active = store.is_active

    def revoke_then_answer(device_id):
        store.revoke(device_id)
        return real_is_active(device_id)

    store.is_active = revoke_then_answer
    try:
        response = run(
            call(
                app,
                "POST",
                f"{WEB_MOUNT_PATH}/translate",
                headers=edge(),
                cookies=jar,
                data=form(text="今汐在云陵谷使用了风羽为刃。"),
            )
        )
    finally:
        monkeypatch_target.translate_request_async = real_pipeline
    assert response.status_code == 401
    assert entered == [], "the pipeline must not be entered for a dead principal"


def test_an_admission_failure_keeps_the_submitted_text(tmp_path, sample_db):
    """Admission runs before the body is read, so its failures had nothing to
    re-render and returned an empty form - the exact loss the error page was
    built to prevent, through the one path that skipped it."""
    app, _store, _device, _token = build_web_app(
        tmp_path, sample_db, rate_limit_per_minute=1
    )
    jar = session_cookie(app)  # spends the single allowed request
    sentence = "今汐在云陵谷使用了风羽为刃。"
    response = run(
        call(
            app,
            "POST",
            f"{WEB_MOUNT_PATH}/translate",
            headers=edge(),
            cookies=jar,
            data=form(text=sentence),
        )
    )
    assert response.status_code == 429
    assert "<textarea" in response.text
    assert sentence in response.text


def test_the_lookup_redirect_carries_the_minted_cookie(tmp_path, sample_db):
    """A redirect that mints must hand over what it minted.

    Visiting /lookup directly authenticates and creates a session, but the
    redirect omitted the cookie - so following it authenticated again, made a
    second session and spent a second rate-limit token. With a one-request
    bucket the redirect landed on 429 instead of the page.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    redirect = run(call(app, "GET", f"{WEB_MOUNT_PATH}/lookup", headers=edge()))
    assert redirect.status_code == 303
    assert redirect.headers["location"] == f"{WEB_MOUNT_PATH}/"
    assert redirect.cookies.get(SESSION_COOKIE_NAME), "the minted cookie must be sent"
    assert len(app.state.web_sessions) == 1

    # Following it reuses that session instead of minting a second one. The
    # session COUNT is the discriminator, not the status code: every request
    # spends a rate-limit token whether or not it mints, so a status-based
    # assertion would report the wrong thing.
    landing = run(
        call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge(),
             cookies={SESSION_COOKIE_NAME: redirect.cookies[SESSION_COOKIE_NAME]})
    )
    assert landing.status_code == 200, landing.status_code
    assert len(app.state.web_sessions) == 1, "the redirect must not cause a second mint"


def test_every_pipeline_result_kind_has_a_rendering():
    """FULL-SET ASSERTION over the kind dimension, derived not copied.

    The renderer maps outcome kinds to Chinese labels. It mapped three of the
    pipeline's four renderable kinds, so a submission that normalised to
    nothing rendered the raw internal token `noop` and an English message on an
    interface promised to be Chinese.

    The set comes from introspecting the application module for its KIND_*
    constants, so a kind added on the server side that the renderer does not
    follow fails HERE rather than reaching the owner as a raw token. A
    hand-written list would need the same edit as the renderer and would be
    forgotten in the same breath.

    The desktop client's category mapping had this exact defect and was fixed
    with this exact assertion; the web layer repeated it because that solution
    was not findable from here. See the ledger entry on indexing solved shapes.
    """
    import wuwaterm.application as pipeline
    from wuwaterm_api.web import render

    defined = {
        value
        for name, value in vars(pipeline).items()
        if name.startswith("KIND_") and isinstance(value, str)
    }
    assert defined, "introspection found no KIND_* constants - the derivation broke"
    renderable = defined - render._KINDS_NOT_RENDERED_AS_RESULTS
    mapped = set(render._SOURCE_LABELS)
    assert mapped == renderable, {
        "kinds the pipeline can return but the page cannot label": renderable - mapped,
        "labels for kinds that no longer exist": mapped - renderable,
    }


def test_every_pipeline_direction_has_a_rendering():
    """The same full-set assertion for the other enumerated dimension."""
    import wuwaterm.application as pipeline
    from wuwaterm_api.web import render

    defined = {
        value
        for name, value in vars(pipeline).items()
        if name.startswith("DIRECTION_") and isinstance(value, str)
    }
    assert defined
    assert set(render._DIRECTION_LABELS) == defined, {
        "directions with no label": defined - set(render._DIRECTION_LABELS),
        "labels for unknown directions": set(render._DIRECTION_LABELS) - defined,
    }


def test_a_noop_outcome_renders_in_chinese(tmp_path, sample_db):
    """The instance the full-set assertion above would have prevented."""
    from wuwaterm_api.web import render

    outcome = type("O", (), {"kind": "noop", "direction": "en",
                             "text": "Nothing to translate after removing metadata."})()
    body = render.translate_view(
        mount=WEB_MOUNT_PATH, text="[WW 2.1]", result=outcome, translated=True
    )
    heading = re.search(r'<div class="card"><h2>(.*?)</h2>', body)
    assert heading is not None, body
    assert "noop" not in heading.group(1), heading.group(1)
    assert "无可翻译内容" in heading.group(1)
    assert "Nothing to translate" not in body
    assert "输入中没有可翻译的内容" in body


def test_multi_line_translations_keep_their_line_breaks(tmp_path, sample_db):
    """The pipeline joins chunks with newlines; the page must not collapse them."""
    from wuwaterm_api.web import render

    body = render.translate_view(
        mount=WEB_MOUNT_PATH,
        text="x",
        result=type("O", (), {"kind": "llm", "direction": "en",
                              "text": "first line\nsecond line"})(),
        translated=True,
    )
    assert "first line\nsecond line" in body
    assert "white-space: pre-wrap" in render.page(
        mount=WEB_MOUNT_PATH, active="translate", body=body
    )


def test_a_non_ascii_edge_marker_is_refused_not_a_server_error(tmp_path, sample_db):
    """A caller-controlled byte must not become a 500.

    Starlette decodes header values latin-1, so a byte >= 0x80 produces a
    non-ASCII str, and secrets.compare_digest raises TypeError on those. That
    exception was neither ApiError nor the refusal type, so it escaped the
    sub-application and surfaced through the PARENT's handler: an
    unauthenticated caller got a 500 where every other bad marker gives 404,
    and drove a traceback into the API process's log once per request.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    response = run(
        call(
            app,
            "GET",
            f"{WEB_MOUNT_PATH}/",
            headers={EDGE_HEADER_NAME.encode(): b"\xff\xfe"},
        )
    )
    assert response.status_code == 404
    assert response.text == ""


def test_the_refusal_carries_the_hardening_headers(tmp_path, sample_db):
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/"))
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]


def test_a_post_without_a_session_cannot_mint_one(tmp_path, sample_db):
    """The control that actually closes cross-site request forgery.

    While any request could mint a session, an absent cookie was not a refusal
    but a trigger to create one - so a cross-site form POST still ran: the
    browser withholds the SameSite=Strict cookie, but the edge injects its
    marker on every proxied request regardless of who caused it and the browser
    attaches its cached basic-auth credentials. The attacker could not read the
    response, but could spend the model-call budget at will.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    response = run(
        call(
            app,
            "POST",
            f"{WEB_MOUNT_PATH}/translate",
            headers=edge(),
            data=form(text="今汐"),
        )
    )
    assert response.status_code == 401
    # Nothing was minted, so nothing was spent.
    assert "set-cookie" not in response.headers
    # ...and the submitted text came back with the form, rather than being
    # thrown away by a redirect the owner would have to retype after.
    assert "<textarea" in response.text
    assert "今汐" in response.text
    # ...and a GET first still works, which is the ordinary flow.
    page = run(call(app, "GET", f"{WEB_MOUNT_PATH}/translate", headers=edge()))
    assert page.status_code == 200
    assert page.cookies[SESSION_COOKIE_NAME]


def test_a_revoked_session_is_dropped_from_the_store(tmp_path, sample_db):
    """Revocation already ended access; this is the bookkeeping half.

    A dead entry left in place holds one of the bounded slots for its whole
    TTL, and the eviction loop drops the OLDEST entry rather than the deadest -
    so under pressure a revoked session could outlive a live one.
    """
    app, store, device, _token = build_web_app(tmp_path, sample_db)
    first = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    session_id = first.cookies[SESSION_COOKIE_NAME]
    sessions = app.state.web_sessions
    assert sessions.resolve(session_id) is not None
    store.revoke(device.device_id)
    second = run(
        call(
            app,
            "GET",
            f"{WEB_MOUNT_PATH}/",
            headers=edge(),
            cookies={SESSION_COOKIE_NAME: session_id},
        )
    )
    assert second.status_code == 401
    assert sessions.resolve(session_id) is None


def test_an_unconfigured_edge_secret_refuses_everything(tmp_path, sample_db):
    """Fail closed: an operator who enabled the layer but set no edge secret
    gets a surface that refuses, never one that is open to anyone."""
    app, _store, _device, _token = build_web_app(
        tmp_path, sample_db, web_edge_secret=""
    )
    assert run(call(app, "GET", f"{WEB_MOUNT_PATH}/")).status_code == 404
    assert (
        run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge())).status_code == 404
    )


# ------------------------------------ criterion 1: nothing lands in the browser


def test_no_credential_material_reaches_the_browser(tmp_path, sample_db):
    """The token, its secret half, and the device id must appear nowhere in
    what is sent to the client - not in the body, not in a header, not in a
    cookie."""
    app, _store, device, token = build_web_app(tmp_path, sample_db)
    response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    assert response.status_code == 200
    wire = response.text + "\n" + "\n".join(
        f"{name}: {value}" for name, value in response.headers.items()
    )
    assert token not in wire
    assert DEVICE_SECRET not in wire
    assert device.device_id not in wire


def test_the_session_cookie_is_opaque_and_locked_down(tmp_path, sample_db):
    app, _store, _device, token = build_web_app(tmp_path, sample_db)
    response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    raw = response.headers["set-cookie"]
    assert raw.startswith(f"{SESSION_COOKIE_NAME}=")
    lowered = raw.lower()
    # HttpOnly is the one that makes "no JS-readable credential" structural.
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=strict" in lowered
    assert f"path={WEB_MOUNT_PATH}" in lowered
    value = response.cookies[SESSION_COOKIE_NAME]
    # Opaque: it is not the token, contains no part of it, and nothing about
    # the device can be read out of it.
    assert value != token
    assert DEVICE_SECRET not in value
    assert value not in token


def test_a_forged_session_cookie_is_not_accepted(tmp_path, sample_db):
    """A guessed identifier must mint nothing. It resolves to no session, so
    the request falls through to establishing a new one from the server-held
    token - and the response must therefore carry a NEW cookie, not honour the
    forged one."""
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    response = run(
        call(
            app,
            "GET",
            f"{WEB_MOUNT_PATH}/",
            headers=edge(),
            cookies={SESSION_COOKIE_NAME: "not-a-real-session-identifier"},
        )
    )
    assert response.status_code == 200
    assert response.cookies[SESSION_COOKIE_NAME] != "not-a-real-session-identifier"


# ---------------------------------------------------------- scope enforcement


def test_translation_requires_the_translate_scope(tmp_path, sample_db):
    """A device granted only meta must not be able to translate through the
    browser, exactly as it cannot through the API."""
    app, _store, _device, _token = build_web_app(
        tmp_path, sample_db, scopes=[SCOPE_META]
    )
    response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/translate", headers=edge()))
    assert response.status_code == 403


def test_lookup_requires_the_meta_scope(tmp_path, sample_db):
    app, _store, _device, _token = build_web_app(
        tmp_path, sample_db, scopes=[SCOPE_TRANSLATE]
    )
    response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    assert response.status_code == 403


def test_revoking_the_device_ends_the_browser_session(tmp_path, sample_db):
    """A session must not be a way to outlive revocation."""
    app, store, device, _token = build_web_app(tmp_path, sample_db)
    first = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    assert first.status_code == 200
    cookie = {SESSION_COOKIE_NAME: first.cookies[SESSION_COOKIE_NAME]}
    store.revoke(device.device_id)
    second = run(
        call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge(), cookies=cookie)
    )
    assert second.status_code == 401


# ------------------------------------------------------------------ rendering


def test_a_dictionary_query_renders_its_matches(tmp_path, sample_db):
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    jar = session_cookie(app)
    response = run(
        call(
            app,
            "POST",
            f"{WEB_MOUNT_PATH}/lookup",
            headers=edge(),
            cookies=jar,
            data=form(q="共鸣者"),
        )
    )
    assert response.status_code == 200
    assert "词典结果" in response.text


def test_user_input_is_escaped_into_the_page(tmp_path, sample_db):
    """This surface renders HTML from Python, so escaping is this code's job.

    The query is echoed back into the search box, which is the interpolation an
    injected string would ride in on.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    jar = session_cookie(app)
    response = run(
        call(
            app,
            "POST",
            f"{WEB_MOUNT_PATH}/lookup",
            headers=edge(),
            cookies=jar,
            data=form(q='"><script>alert(1)</script>'),
        )
    )
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_only_urlencoded_form_bodies_are_accepted(tmp_path, sample_db):
    """No multipart parser is reachable from the network on this surface."""
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    jar = session_cookie(app)
    response = run(
        call(
            app,
            "POST",
            f"{WEB_MOUNT_PATH}/lookup",
            headers=edge(),
            cookies=jar,
            files={"q": ("q.txt", "共鸣者".encode("utf-8"))},
        )
    )
    assert response.status_code == 400


def test_a_dictionary_answer_is_not_labelled_as_a_model_translation(
    tmp_path, sample_db
):
    """Provenance is the one thing this label carries, so it must be right.

    The pipeline emits kinds "exact"/"fuzzy"/"llm" and directions "zh"/"en".
    A renderer that tested for a kind named "dictionary" matched none of them
    and fell through to "model" for every answer - so an official dictionary
    hit, on a product built around dictionary-first, was reported to the owner
    as something the model made up.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    jar = session_cookie(app)
    response = run(
        call(
            app,
            "POST",
            f"{WEB_MOUNT_PATH}/translate",
            headers=edge(),
            cookies=jar,
            data=form(text="共鸣者"),
        )
    )
    assert response.status_code == 200
    assert "Resonator" in response.text
    # Scoped to the result card's own heading: the page footer legitimately
    # mentions the model ("...才会调用模型。"), so a whole-page search for that
    # word would pass or fail for the wrong reason.
    heading = re.search(r'<div class="card"><h2>(.*?)</h2>', response.text)
    assert heading is not None, response.text
    label = heading.group(1)
    assert "词典" in label, label
    assert "模型" not in label, label
    # ...and the direction reads as Chinese, not as the raw pipeline token.
    assert "译为英文" in label, label
    assert label.strip() != "en", label


def test_an_error_keeps_the_form_and_what_was_typed(tmp_path, sample_db):
    """An error page the owner cannot retry from is a dead end on a phone.

    With no model configured the translation path refuses rather than echoing
    the source text back as though it had been translated - so this exercises
    a real refusal, and requires that the refusal still renders a usable form
    carrying the submitted text.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    jar = session_cookie(app)
    sentence = "今汐在云陵谷使用了风羽为刃。"
    response = run(
        call(
            app,
            "POST",
            f"{WEB_MOUNT_PATH}/translate",
            headers=edge(),
            cookies=jar,
            data=form(text=sentence),
        )
    )
    assert response.status_code == 503
    assert "翻译服务暂时不可用" in response.text
    # The form came back...
    assert "<textarea" in response.text
    # ...carrying what was typed, so the owner can edit rather than retype.
    assert sentence in response.text


def test_the_page_carries_its_hardening_headers(tmp_path, sample_db):
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy


def test_the_web_layer_adds_no_cross_origin_permission(tmp_path, sample_db):
    """Criterion: same-origin architecture, no CORS relaxation.

    The browser reaches this surface on the same host name as the API through a
    second path prefix, so there is no cross-origin request to permit. Any
    access-control header appearing here would mean that stopped being true.
    """
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    response = run(
        call(
            app,
            "GET",
            f"{WEB_MOUNT_PATH}/",
            headers={**edge(), "Origin": "https://elsewhere.example"},
        )
    )
    assert not [
        name for name in response.headers if name.lower().startswith("access-control-")
    ]


def test_the_interface_is_in_chinese(tmp_path, sample_db):
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    assert 'lang="zh-CN"' in response.text
    for label in ("查词", "翻译", "私用工具"):
        assert label in response.text


def test_the_page_declares_a_mobile_viewport(tmp_path, sample_db):
    """Mobile-first is a rendering claim, and this is the part of it that is
    checkable without a browser: without this meta element a phone lays the
    page out at a desktop width and scales it down."""
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    response = run(call(app, "GET", f"{WEB_MOUNT_PATH}/", headers=edge()))
    assert 'name="viewport"' in response.text
    assert "width=device-width" in response.text


# -------------------------------------------------------------- session store


def test_sessions_expire_on_a_monotonic_clock():
    store = SessionStore(ttl_seconds=100, max_sessions=8)

    class Principal:
        device_id = "abc"

    session = store.create(Principal(), now=1000.0)
    assert store.resolve(session.session_id, now=1099.0) is not None
    assert store.resolve(session.session_id, now=1100.0) is None
    # ...and the expired entry is dropped rather than merely hidden.
    assert len(store) == 0


def test_the_session_map_is_bounded():
    store = SessionStore(ttl_seconds=1000, max_sessions=3)

    class Principal:
        device_id = "abc"

    for _ in range(10):
        store.create(Principal(), now=1.0)
    assert len(store) == 3


def test_an_absent_identifier_resolves_to_nothing():
    store = SessionStore(ttl_seconds=1000, max_sessions=3)
    assert store.resolve(None) is None
    assert store.resolve("") is None
    assert store.resolve("never-issued") is None


def test_stylesheet_uses_literal_decorative_glyphs():
    # CSS code-point escapes do not survive a plain Python string: "\25C6"
    # parses as the octal control character U+0015 followed by "C6". The
    # decorative glyphs must be literal Unicode in the stylesheet.
    style = render._STYLE
    assert "◆" in style
    assert "—" in style
    assert all(ord(ch) >= 0x20 or ch in "\n\r\t" for ch in style)


@pytest.mark.parametrize("change, expected", [("revoke", 401), ("store_error", 503), ("active", 200)])
def test_queued_web_translation_rechecks_device_before_spending(
    tmp_path, sample_db, monkeypatch, change, expected
):
    import sqlite3

    app, store, device, _token = build_web_app(tmp_path, sample_db, llm_max_concurrency=1)
    jar = session_cookie(app)
    calls = []

    async def fake_call(locked_text, locks, **kwargs):
        calls.append(locked_text)
        return "Translated synthetic sentence."

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://model.example.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "synthetic-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)

    async def scenario():
        slots = app.state.translator._current_llm_slots()
        acquire = slots.acquire
        await acquire()  # Synthetic holder uses no budget or upstream call.
        queued = asyncio.Event()

        async def observed_acquire():
            assert slots.locked()
            queued.set()
            return await acquire()

        monkeypatch.setattr(slots, "acquire", observed_acquire)
        task = asyncio.create_task(call(
            app, "POST", f"{WEB_MOUNT_PATH}/translate", headers=edge(), cookies=jar,
            data=form(text="这是一句没有词典命中的完整句子。"),
        ))
        try:
            await asyncio.wait_for(queued.wait(), timeout=5)
            assert not task.done()
            assert calls == []
            assert app.state.llm_budget.in_window() == 0
            if change == "revoke":
                store.revoke(device.device_id)
            elif change == "store_error":
                def unavailable(device_id):
                    raise sqlite3.OperationalError("synthetic store unavailable")
                monkeypatch.setattr(store, "is_active", unavailable)
            slots.release()
            return await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    response = run(scenario())
    spent = 1 if change == "active" else 0
    assert (len(calls), app.state.llm_budget.in_window(), response.status_code) == (
        spent, spent, expected
    ), response.text
    if change == "active":
        assert "Translated synthetic sentence." in response.text
    else:
        assert "Translated synthetic sentence." not in response.text


@pytest.mark.parametrize("change, expected", [("revoke", 401), ("store_error", 200)])
def test_web_translation_retains_final_device_recheck_policy(
    tmp_path, sample_db, monkeypatch, change, expected
):
    import sqlite3

    app, store, device, _token = build_web_app(tmp_path, sample_db)
    jar = session_cookie(app)
    calls = []

    async def fake_call(locked_text, locks, **kwargs):
        calls.append(locked_text)
        if change == "revoke":
            store.revoke(device.device_id)
        else:
            def unavailable(device_id):
                raise sqlite3.OperationalError("synthetic final store unavailable")
            monkeypatch.setattr(store, "is_active", unavailable)
        return "Translated synthetic sentence."

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://model.example.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "synthetic-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    response = run(call(
        app, "POST", f"{WEB_MOUNT_PATH}/translate", headers=edge(), cookies=jar,
        data=form(text="这是一句没有词典命中的完整句子。"),
    ))
    assert response.status_code == expected, response.text
    assert len(calls) == 1
    assert app.state.llm_budget.in_window() == 1
    assert ("Translated synthetic sentence." in response.text) == (change == "store_error")


def test_web_dictionary_translation_spends_no_model_budget(tmp_path, sample_db, monkeypatch):
    app, _store, _device, _token = build_web_app(tmp_path, sample_db)
    jar = session_cookie(app)
    calls = []

    async def fake_call(locked_text, locks, **kwargs):
        calls.append(locked_text)
        return "Unexpected model call."

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://model.example.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "synthetic-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    response = run(call(
        app, "POST", f"{WEB_MOUNT_PATH}/translate", headers=edge(), cookies=jar,
        data=form(text="共鸣者"),
    ))
    assert response.status_code == 200, response.text
    assert "Resonator" in response.text
    assert calls == []
    assert app.state.llm_budget.in_window() == 0
