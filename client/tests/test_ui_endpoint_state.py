"""The window has to show which server it is talking to, or that it has none.

The failure this closes was invisible by construction: the client silently
adopted a machine-local development address whenever its settings file could
not be read, and nothing on screen said which address was in use - so a
missing `config.json` looked exactly like an unreachable service.

The claim is now made by three widgets instead of one label, and each of them
is checked here: the chip in the navigation column (a word and a shortened
address, plus the full sentence for assistive technology), the global banner
that states nothing will be sent, and the setup checklist that says what is
missing. They are refreshed together on purpose - a window that says "not
configured" in one corner and shows an address in another is worse than one
that says nothing - so they are asserted together too.

Real Qt under the offscreen platform (conftest.py), like the other UI tests
here; the state logic itself is a plain function so it can be checked without
driving a widget.

The name is `test_ui_*` to match the other Qt files, and that is now the only
reason for it. It began as a workaround: `tests/test_packaging_entry.py` built
its own QApplication in-process, libshiboken refuses a second one while a
session-scoped instance is alive, and this file - first called
`test_main_window_endpoint_state.py` - sorted earlier and broke it on sight.
That constraint is history: the packaging probe runs in a child interpreter
now, and `test_the_suite_does_not_depend_on_this_file_running_first` keeps it
that way. A new Qt test file here can be called anything.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402

from wuwaterm_client import strings  # noqa: E402
from wuwaterm_client.config import ClientConfig  # noqa: E402
from wuwaterm_client.errors import ERROR_OFFLINE, ClientError  # noqa: E402
from wuwaterm_client.ui import main_window as main_window_module  # noqa: E402
from wuwaterm_client.ui.main_window import MainWindow, endpoint_status_text  # noqa: E402

CONFIGURED = "https://api.example.invalid/wuwaterm-api"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _visible_texts(window: MainWindow) -> list[str]:
    """Read what the rendered window says without naming any one control."""
    texts: list[str] = []
    for widget in window.findChildren(QWidget):
        if not widget.isVisibleTo(window):
            continue
        for attribute in ("text", "toPlainText", "currentText", "placeholderText"):
            reader = getattr(widget, attribute, None)
            if not callable(reader):
                continue
            try:
                value = reader()
            except TypeError:
                continue
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())
    return texts


def _assert_the_three_areas_are_reachable(window: MainWindow) -> None:
    from wuwaterm_client.ui import main_window as main_window_module

    assert window.stack.count() == 4
    assert [window.stack.widget(index) for index in range(4)] == [
        window.terms_view, window.translate_view, window.status_view,
        window.manuscript_view,
    ]
    for page, view in (
        (main_window_module.PAGE_MANUSCRIPT, window.manuscript_view),
        (main_window_module.PAGE_TRANSLATE, window.translate_view),
        (main_window_module.PAGE_TERMS, window.terms_view),
    ):
        window._nav_by_page[page].click()
        assert window.stack.currentWidget() is view
        assert window._nav_by_page[page].isChecked()
        assert sum(button.isChecked() for button in window.nav_buttons) == 1
    window.status_action.trigger()
    assert window.stack.currentWidget() is window.status_view
    assert not any(button.isChecked() for button in window.nav_buttons)


def test_the_state_logic_distinguishes_the_two_cases() -> None:
    unconfigured = endpoint_status_text(None)
    configured = endpoint_status_text(CONFIGURED)

    assert unconfigured == strings.ENDPOINT_NOT_CONFIGURED
    assert unconfigured != configured
    # The configured line names the address, so a client pointed somewhere
    # unexpected can be recognised as such by reading the window.
    assert CONFIGURED in configured
    # ...and the unconfigured line names the place to fix it, because nothing
    # the owner did put them in that state. The words are the ones the window
    # now uses; the claim is the same one the English assertions made.
    assert "设置" in unconfigured
    assert "尚未配置" in unconfigured


def test_the_window_shows_the_configured_address(qapp, monkeypatch) -> None:
    monkeypatch.setattr(main_window_module, "has_token", lambda: True)

    window = MainWindow(ClientConfig(base_url=CONFIGURED))

    assert window.endpoint_chip.is_configured is True
    # The chip holds the whole address even though it draws a shortened one,
    # and the full sentence is what it reports to assistive technology - so
    # the state is never only available to someone who can read an elided
    # string in a small font.
    assert window.endpoint_chip.address_text == CONFIGURED
    assert window.endpoint_chip.accessibleName() == endpoint_status_text(CONFIGURED)
    assert CONFIGURED in window.endpoint_chip.accessibleName()
    # A configured client says nothing standing about being unable to send.
    assert window.global_banner.is_showing() is False


def test_the_window_says_so_when_nothing_is_configured(qapp, monkeypatch) -> None:
    """A config file that vanished must produce a window that says the
    address is gone - not one that quietly points at a development port."""
    monkeypatch.setattr(main_window_module, "has_token", lambda: True)

    window = MainWindow(ClientConfig())

    assert window.endpoint_chip.is_configured is False
    assert window.endpoint_chip.address_text == strings.ENDPOINT_CHIP_NO_ADDRESS
    assert window.endpoint_chip.accessibleName() == strings.ENDPOINT_NOT_CONFIGURED
    assert "127.0.0.1" not in window.endpoint_chip.accessibleName()
    assert "127.0.0.1" not in window.endpoint_chip.address_text
    assert window.api_client.is_configured is False
    # Said once as a state and once as a task, because the two answer
    # different questions: what is wrong, and what to do about it.
    assert window.global_banner.is_showing() is True
    assert window.global_banner.message_text == strings.GLOBAL_BANNER_NOT_CONFIGURED
    assert window.endpoint_button.text() == strings.WORKBENCH_ENDPOINT_UNCONFIGURED
    assert window.stack.currentWidget() is window.manuscript_view
    assert not window.setup_card.isVisibleTo(window)
    assert not window.global_banner.isVisibleTo(window)
    window.show_page(main_window_module.PAGE_TERMS)
    assert not window.global_banner.isVisibleTo(window)
    assert window.endpoint_button.isVisibleTo(window)
    assert not window.terms_view.search_button.isEnabled()
    # The window is still usable: Settings is how the owner recovers, so all
    # three areas have to stay reachable rather than be blocked behind a
    # broken client.
    _assert_the_three_areas_are_reachable(window)


def test_unconfigured_window_has_no_request_id_text_in_any_area(
    qapp, monkeypatch
) -> None:
    """No-request screens must not display a request handle or placeholder."""
    monkeypatch.setattr(main_window_module, "has_token", lambda: True)
    window = MainWindow(ClientConfig())
    window.show()

    violations: dict[str, list[str]] = {}
    for name, page in (
        ("terms", main_window_module.PAGE_TERMS),
        ("translate", main_window_module.PAGE_TRANSLATE),
        ("status", main_window_module.PAGE_STATUS),
    ):
        window.show_page(page)
        qapp.processEvents()
        bad = [
            text
            for text in _visible_texts(window)
            if strings.REQUEST_ID_ROW_LABEL in text
            or text == strings.REQUEST_ID_PLACEHOLDER
        ]
        if bad:
            violations[name] = bad

    window.hide()
    assert not violations, f"未配置页面显示了从未发生过的请求 ID:{violations}"


def test_request_without_id_keeps_request_id_text_visible(
    qapp, monkeypatch
) -> None:
    """Explicitly missing IDs still belong to a request and keep their row."""
    monkeypatch.setattr(main_window_module, "has_token", lambda: True)
    window = MainWindow(ClientConfig(base_url=CONFIGURED))
    window.show_page(main_window_module.PAGE_TRANSLATE)
    window.show()
    window.translate_view._show_error(ClientError(ERROR_OFFLINE))
    qapp.processEvents()

    assert window.translate_view.diagnostics_button.isVisibleTo(window)
    window.translate_view.diagnostics_button.click()
    qapp.processEvents()
    visible = _visible_texts(window)
    window.hide()
    assert strings.REQUEST_ID_ROW_LABEL in visible
    assert strings.REQUEST_ID_PLACEHOLDER in visible


def test_setting_an_address_updates_what_the_window_shows(
    qapp, tmp_path, monkeypatch
) -> None:
    """The chip is not read once at start-up: a client configured during the
    session must stop reporting that it is not."""
    from wuwaterm_client import config as config_module

    monkeypatch.setattr(config_module, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(main_window_module, "has_token", lambda: True)

    window = MainWindow(ClientConfig())
    assert window.endpoint_chip.is_configured is False
    assert window.endpoint_chip.accessibleName() == strings.ENDPOINT_NOT_CONFIGURED

    class _AcceptedDialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def result_config(self):
            return ClientConfig(base_url=CONFIGURED)

    monkeypatch.setattr(main_window_module, "SettingsDialog", _AcceptedDialog)
    window._on_settings_clicked()

    assert window.endpoint_chip.is_configured is True
    assert window.endpoint_chip.address_text == CONFIGURED
    assert window.endpoint_chip.accessibleName() == endpoint_status_text(CONFIGURED)
    assert window.api_client.is_configured is True
    # The standing "nothing will be sent" notice and the checklist are gone,
    # because the condition they describe is over.
    assert window.global_banner.message_text != strings.GLOBAL_BANNER_NOT_CONFIGURED
    assert window.setup_card.isVisibleTo(window) is False
    # ...and it was written where the next launch will look for it.
    assert ClientConfig.load(tmp_path).base_url == CONFIGURED


def test_a_refused_address_does_not_change_what_the_window_shows(
    qapp, monkeypatch
) -> None:
    """A refusal must not half-apply to the chip either, or the window would
    describe a client that does not exist.

    The refusal itself used to be a modal warning box; it is the global banner
    now, which keeps it readable while the owner goes back and corrects the
    address instead of taking the reason away on the first click.
    """
    monkeypatch.setattr(main_window_module, "has_token", lambda: True)

    window = MainWindow(ClientConfig(base_url=CONFIGURED))

    class _AcceptedDialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def result_config(self):
            return ClientConfig(base_url="http://198.51.100.7:8787")

    monkeypatch.setattr(main_window_module, "SettingsDialog", _AcceptedDialog)
    window._on_settings_clicked()

    assert window.global_banner.is_showing() is True
    assert window.global_banner.message_text == strings.ERROR_MSG_INSECURE_ENDPOINT
    assert window.endpoint_chip.is_configured is True
    assert window.endpoint_chip.address_text == CONFIGURED
    assert window.endpoint_chip.accessibleName() == endpoint_status_text(CONFIGURED)


class _PendingTask:
    """Stands in for an in-flight request task. Records its cancellation."""

    def __init__(self) -> None:
        self.cancelled = False

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancelled = True


def _accepting_dialog(config: ClientConfig):
    class _AcceptedDialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def result_config(self):
            return config

    return _AcceptedDialog


def test_changing_the_address_cancels_in_flight_work_and_clears_stale_answers(
    qapp, tmp_path, monkeypatch
) -> None:
    """An answer from the OLD server must not render under the NEW address.

    Changing the address does not stop a request already on its way, and a
    reply carries nothing that marks it stale - so without this the window
    shows one service's translation, terms and version under another
    service's name. That is the same defect class as the silent fallback
    this change removes: the screen asserting something it cannot know.
    """
    from wuwaterm_client import config as config_module

    monkeypatch.setattr(config_module, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(main_window_module, "has_token", lambda: True)

    window = MainWindow(ClientConfig(base_url=CONFIGURED))

    # State that belongs to the address currently configured.
    tasks = [_PendingTask() for _ in range(3)]
    window.translate_view._task, window.terms_view._task, window.status_view._task = tasks
    window.translate_view.result_edit.setPlainText("Jinhsi")
    window.translate_view.status_label.setText("Exact dictionary match")
    window.terms_view.table.setRowCount(4)
    window.status_view.service_version_value.setText("0.2.1")
    window.status_view.term_count_value.setText("1234")
    window.status_view.status_label.setText("Loading...")

    monkeypatch.setattr(
        main_window_module,
        "SettingsDialog",
        _accepting_dialog(ClientConfig(base_url="https://other.example.invalid")),
    )
    window._on_settings_clicked()

    assert [task.cancelled for task in tasks] == [True, True, True]
    assert window.translate_view.result_edit.toPlainText() == ""
    assert window.translate_view.status_label.text() == ""
    assert window.terms_view.table.rowCount() == 0
    assert window.status_view.status_label.text() == ""
    for stale in ("0.2.1", "1234"):
        assert stale not in (
            window.status_view.service_version_value.text()
            + window.status_view.term_count_value.text()
        )
    # ...and the chip names the new address, so nothing on screen still refers
    # to the old one.
    assert window.endpoint_chip.address_text == "https://other.example.invalid"
    assert window.endpoint_chip.accessibleName() == endpoint_status_text(
        "https://other.example.invalid"
    )
    # Three areas emptying at once reads as data loss unless something says it
    # was deliberate.
    assert window.global_banner.message_text == strings.ENDPOINT_CHANGED_BANNER


def test_settings_that_leave_the_address_alone_do_not_discard_work(
    qapp, tmp_path, monkeypatch
) -> None:
    """The reset is triggered by the ADDRESS changing, not by opening
    Settings. Editing a timeout must not cancel a translation in progress or
    wipe a result the owner is reading."""
    from wuwaterm_client import config as config_module

    monkeypatch.setattr(config_module, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(main_window_module, "has_token", lambda: True)

    window = MainWindow(ClientConfig(base_url=CONFIGURED, request_timeout_seconds=5.0))
    task = _PendingTask()
    window.translate_view._task = task
    window.translate_view.result_edit.setPlainText("Jinhsi")

    monkeypatch.setattr(
        main_window_module,
        "SettingsDialog",
        _accepting_dialog(
            ClientConfig(base_url=CONFIGURED, request_timeout_seconds=42.0)
        ),
    )
    window._on_settings_clicked()

    assert task.cancelled is False
    assert window.translate_view.result_edit.toPlainText() == "Jinhsi"
    assert window.api_client._timeout == 42.0


def test_a_refused_address_does_not_discard_the_work_in_progress(
    qapp, monkeypatch
) -> None:
    """A refusal changes nothing, so it must not cancel anything either."""
    monkeypatch.setattr(main_window_module, "has_token", lambda: True)

    window = MainWindow(ClientConfig(base_url=CONFIGURED))
    task = _PendingTask()
    window.translate_view._task = task
    window.translate_view.result_edit.setPlainText("Jinhsi")

    monkeypatch.setattr(
        main_window_module,
        "SettingsDialog",
        _accepting_dialog(ClientConfig(base_url="http://198.51.100.7:8787")),
    )
    window._on_settings_clicked()

    assert task.cancelled is False
    assert window.translate_view.result_edit.toPlainText() == "Jinhsi"
    # The refusal is on screen all the same, so "nothing changed" is not
    # indistinguishable from "nothing happened".
    assert window.global_banner.message_text == strings.ERROR_MSG_INSECURE_ENDPOINT


def test_the_settings_dialog_opens_empty_when_nothing_is_configured(qapp) -> None:
    """The field shows what is set. Pre-filling it with a development address
    is how a value nobody chose gets saved by pressing OK."""
    from wuwaterm_client.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(ClientConfig())

    assert dialog.base_url_edit.text() == ""
    # The example still exists - as a placeholder, which is not a value.
    assert dialog.base_url_edit.placeholderText()
    assert dialog.result_config().base_url is None
