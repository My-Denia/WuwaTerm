"""Shared pytest setup.

Sets QT_QPA_PLATFORM=offscreen before any PySide6 import so Qt-constructing
tests run headlessly, without a real display. The API, configuration,
credential and error tests do not need Qt or its event loop.
"""

from __future__ import annotations

import os
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_user_state(tmp_path, monkeypatch, request):
    # This read-only Windows-only test explicitly inspects the real path.
    if request.node.name != "test_on_this_machine_the_configuration_is_not_in_a_temporary_directory":
        monkeypatch.setenv("APPDATA", str(tmp_path))
    # Portable manuscript contract tests also run in the server environment,
    # where desktop dependencies are intentionally absent. They still run;
    # only credential-store isolation is unnecessary there.
    try:
        import keyring
        import keyring.errors
        from keyring.backend import KeyringBackend
    except ModuleNotFoundError as exc:
        if exc.name != "keyring":
            raise
        yield
        return

    class _IsolatedKeyring(KeyringBackend):
        priority = 1

        def __init__(self):
            self.values = {}

        def get_password(self, service, username):
            return self.values.get((service, username))

        def set_password(self, service, username, password):
            self.values[(service, username)] = password

        def delete_password(self, service, username):
            try:
                del self.values[(service, username)]
            except KeyError:
                raise keyring.errors.PasswordDeleteError("not found") from None

    previous = keyring.get_keyring()
    keyring.set_keyring(_IsolatedKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)
