"""User-visible workbench flows with isolated configuration and credentials."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from wuwaterm_client import strings
from wuwaterm_client.api import ApiClient, TermMatch, TermsResult, TranslationResult
from wuwaterm_client.config import APPEARANCE_DARK, ClientConfig
from wuwaterm_client.credentials import CredentialStoreUnavailable
from wuwaterm_client.ui import settings_dialog, status_view
from wuwaterm_client.ui.main_window import MainWindow
from wuwaterm_client.ui.settings_dialog import SettingsDialog
from wuwaterm_client.ui.terms_view import TermsView
from wuwaterm_client.ui.translate_view import TranslateView
from wuwaterm_client.errors import ClientError


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def test_local_editor_starts_when_credential_store_is_unavailable(tmp_path):
    # A separate process exercises the actual Qt/qasync startup path.
    program = '''
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import httpx
from wuwaterm_client import app, strings
from wuwaterm_client.api import ApiClient
from wuwaterm_client.config import ClientConfig
from wuwaterm_client.credentials import CredentialStoreUnavailable
from wuwaterm_client.ui import status_view
from wuwaterm_client.ui.main_window import MainWindow

def unavailable():
    raise CredentialStoreUnavailable("unavailable")
status_view.read_token = unavailable
ClientConfig.load = classmethod(lambda cls, *args: cls(base_url="https://example.invalid"))
seen = []
def transport(request):
    seen.append(request.url.path)
    return httpx.Response(200, json={})
ApiClient.from_config = classmethod(lambda cls, config: cls(
    config.base_url, token_provider=lambda: "mock", _test_transport=httpx.MockTransport(transport)))
MainWindow.ensure_credential = lambda self: (_ for _ in ()).throw(AssertionError("startup asked for credentials"))

def check():
    window = next(w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow))
    assert window.isVisible()
    assert window.stack.currentWidget() is window.manuscript_view
    assert window.status_view.credential_status_value.text() == strings.SETTINGS_TOKEN_STATUS_UNKNOWN
    assert seen == []
    window.manuscript_view.source_edit.insertPlainText("local text")
    assert window.manuscript_view.source_edit.toPlainText() == "local text"
    window.manuscript_view._saved_payload = window.manuscript_view._payload.copy()
    window.close()
original_show = MainWindow.show
def show_with_check(self):
    original_show(self)
    QTimer.singleShot(25, check)
MainWindow.show = show_with_check
raise SystemExit(app.run(["WuwaTerm"]))
'''
    environment = dict(os.environ, APPDATA=str(tmp_path), QT_QPA_PLATFORM="offscreen")
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    completed = subprocess.run([sys.executable, "-c", program], env=environment,
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr


def test_unconfigured_appearance_save_does_not_change_endpoint(qapp, monkeypatch):
    dialog = SettingsDialog(ClientConfig(), validator=lambda _value: "unexpected")
    dialog.theme_dark_radio.setChecked(True)
    dialog._on_accepted()
    assert dialog.result() == QDialog.DialogCode.Accepted
    new_config = dialog.result_config()
    assert new_config.base_url is None and new_config.appearance == APPEARANCE_DARK

    window = MainWindow(ClientConfig())
    calls = []
    monkeypatch.setattr(window.api_client, "update_base_url", lambda value: calls.append(value))
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    window._apply_settings(new_config)
    assert calls == []
    assert window.config.appearance == APPEARANCE_DARK
    assert not window.api_client.is_configured


def test_unreadable_credential_state_is_unknown(qapp, monkeypatch):
    def unavailable():
        raise CredentialStoreUnavailable("unavailable")
    monkeypatch.setattr(status_view, "read_token", unavailable)
    monkeypatch.setattr(settings_dialog, "read_token", unavailable)
    monkeypatch.setattr(status_view, "active_backend_name", unavailable)
    monkeypatch.setattr(settings_dialog, "active_backend_name", unavailable)
    window = MainWindow(ClientConfig())
    dialog = SettingsDialog(ClientConfig())
    assert window.status_view.credential_status_value.text() == strings.SETTINGS_TOKEN_STATUS_UNKNOWN
    assert dialog.credential_status_label.text() == strings.SETTINGS_TOKEN_STATUS_UNKNOWN
    assert window.status_view.keyring_backend_value.text() == strings.STATUS_UNKNOWN_VALUE
    assert dialog.backend_label.text() == strings.STATUS_UNKNOWN_VALUE


def test_term_selection_filter_copy_and_translation_bridge(qapp):
    view = TermsView(ApiClient("https://test.invalid", token_provider=lambda: "example"))
    matches = (
        TermMatch("漂泊者", "Rover", "character", 1.0, "exact"),
        TermMatch("今州", "Jinzhou", "location", .75, "fuzzy"),
    )
    view._render_result(TermsResult("Rover", matches, "request"))
    assert (view.detail_zh.text(), view.detail_en.text(), view.detail_score.text()) == ("漂泊者", "Rover", "1.00")
    index = view.category_filter.findData("location")
    view.category_filter.setCurrentIndex(index)
    assert view.table.isRowHidden(0) and not view.table.isRowHidden(1)
    assert view.detail_en.text() == "Jinzhou" and view.detail_score.text() == "0.75"
    view.copy_zh_button.click()
    assert qapp.clipboard().text() == "今州"

    window = MainWindow(ClientConfig())
    window.manuscript_view.lookup_requested.emit("Rover")
    assert window.stack.currentWidget() is window.terms_view
    assert window.terms_view.query_edit.text() == "Rover"
    assert window.terms_view._task is None
    window.terms_view.translate_requested.emit("Rover")
    assert window.stack.currentWidget() is window.translate_view
    assert window.translate_view.input_edit.toPlainText() == "Rover"
    assert window.translate_view._task is None


def test_repeated_lookup_refreshes_selected_details_and_copies(qapp):
    view = TermsView(ApiClient("https://test.invalid", token_provider=lambda: "example"))
    for zh, en, category in (("漂泊者", "Rover", "resonator"), ("今州", "Jinzhou", "location")):
        view._render_result(TermsResult(en, (TermMatch(zh, en, category, 100.0, "exact"),), en))
        assert view.table.item(0, 1).text() == en
        assert view.detail_en.text() == en and view.detail_zh.text() == zh
        view.copy_en_button.click()
        assert qapp.clipboard().text() == en
        view.copy_zh_button.click()
        assert qapp.clipboard().text() == zh
    view._render_result(TermsResult("missing", (), "empty"))
    assert view.detail_en.text() == view.detail_zh.text() == ""
    assert not view.copy_en_button.isEnabled() and not view.copy_zh_button.isEnabled()


def test_global_save_shortcut_respects_active_input_method(qapp, tmp_path):
    window = MainWindow(ClientConfig())
    view = window.manuscript_view
    path = tmp_path / "draft.json"
    view._path = path
    window.show()
    view.source_edit.setFocus()
    qapp.processEvents()
    QCoreApplication.sendEvent(view.source_edit, QInputMethodEvent("ni", []))
    QTest.keyClick(view.source_edit, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()
    assert not path.exists()
    assert view.source_edit._ime_active
    assert strings.MANUSCRIPT_FINISH_INPUT in view.status_label.text()
    QCoreApplication.sendEvent(view.source_edit, QInputMethodEvent())
    QTest.keyClick(view.source_edit, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()
    assert path.exists()
    window.close()


def test_settings_categories_and_validation_remain_reachable(qapp, monkeypatch):
    previews = []
    monkeypatch.setattr(settings_dialog.theme, "apply_theme", lambda _app, value: previews.append(value))
    dialog = SettingsDialog(ClientConfig(base_url="https://example.invalid", appearance="light"))
    dialog.resize(760, 560)
    dialog.show()
    qapp.processEvents()
    assert dialog.category_sidebar.isVisibleTo(dialog)
    dialog.category_buttons[1].click()
    assert dialog.base_url_edit.isVisibleTo(dialog)
    dialog.base_url_edit.setText("http://example.invalid")
    dialog.category_buttons[2].click()
    assert dialog.enter_token_button.isVisibleTo(dialog)
    dialog._on_accepted()
    assert dialog.category_stack.currentIndex() == 1
    assert dialog.base_url_error.is_showing()
    assert dialog.base_url_edit.isVisibleTo(dialog)
    dialog.category_buttons[0].click()
    dialog.theme_dark_radio.setChecked(True)
    dialog.reject()
    assert previews[-2:] == ["dark", "light"]


def test_small_settings_switch_categories_and_keep_decisions_reachable(qapp):
    dialog = SettingsDialog(ClientConfig())
    dialog.setMinimumSize(0, 0)
    dialog.resize(500, 350)
    dialog.show()
    qapp.processEvents()
    assert dialog.size().width() <= 500 and dialog.size().height() <= 350
    assert dialog.category_combo.isVisibleTo(dialog)
    dialog.category_combo.setCurrentIndex(2)
    assert dialog.enter_token_button.isVisibleTo(dialog)
    assert dialog.scroll_area.verticalScrollBar().maximum() >= 0
    assert dialog.findChild(QDialogButtonBox).geometry().bottom() < dialog.height()
    dialog.reject()


def test_file_shortcut_from_lookup_opens_the_manuscript_before_saving(qapp, tmp_path):
    window = MainWindow(ClientConfig())
    view = window.manuscript_view
    view._path = tmp_path / "draft.json"
    view.source_edit.insertPlainText("draft text")
    window.show()
    window._nav_by_page[0].click()
    window.terms_view.query_edit.setFocus()
    qapp.processEvents()
    QTest.keyClick(window.terms_view.query_edit, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()
    assert window.stack.currentWidget() is view
    assert view._path.exists()
    assert not view.document_dirty
    window.close()


def test_settings_diagnostics_opens_service_status(qapp):
    window = MainWindow(ClientConfig())
    dialog = window._new_settings_dialog()
    dialog.category_buttons[1].click()
    dialog.diagnostics_button.click()
    assert window.stack.currentWidget() is window.status_view


def test_translation_copy_uses_result_not_request_id(qapp):
    view = TranslateView(ApiClient("https://test.invalid", token_provider=lambda: "example"))
    assert not view.copy_result_button.isEnabled()
    view._show_result(TranslationResult("llm", "译文内容", "zh", True, "request-handle"))
    assert view.copy_result_button.isEnabled()
    view.copy_result_button.click()
    assert qapp.clipboard().text() == "译文内容"
    view._show_error(ClientError("offline", request_id="failure-handle"))
    assert not view.copy_result_button.isEnabled()
    assert view.result_edit.toPlainText() == ""
