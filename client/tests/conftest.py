"""Shared pytest setup.

Sets QT_QPA_PLATFORM=offscreen before any PySide6 import so Qt-constructing
tests run headlessly, without a real display. The API, configuration,
credential and error tests do not need Qt or its event loop.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# Keep all UI and subprocess tests away from the operator's configuration and
# OS credential store. Individual credential tests may install their own fake.
import keyring
import keyring.errors
from keyring.backend import KeyringBackend
import pytest


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


@pytest.fixture(autouse=True)
def isolated_user_state(tmp_path, monkeypatch, request):
    # This read-only Windows-only test explicitly inspects the real path.
    if request.node.name != "test_on_this_machine_the_configuration_is_not_in_a_temporary_directory":
        monkeypatch.setenv("APPDATA", str(tmp_path))
    previous = keyring.get_keyring()
    keyring.set_keyring(_IsolatedKeyring())
    yield
    keyring.set_keyring(previous)
