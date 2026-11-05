"""A top-level window's visible frame must fit its logical work area."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMargins, QPoint, QRect  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QWidget  # noqa: E402

from wuwaterm_client import strings, theme  # noqa: E402
from wuwaterm_client.config import APPEARANCE_LIGHT, ClientConfig  # noqa: E402
from wuwaterm_client.ui import components  # noqa: E402
from wuwaterm_client.ui.main_window import (  # noqa: E402
    MainWindow,
    WINDOW_ABSOLUTE_MINIMUM,
    WINDOW_DEFAULT_SIZE,
    WINDOW_PREFERRED_MINIMUM,
)
from wuwaterm_client.ui.settings_dialog import (  # noqa: E402
    SETTINGS_ABSOLUTE_MINIMUM,
    SETTINGS_DEFAULT_SIZE,
    SETTINGS_PREFERRED_MINIMUM,
    SettingsDialog,
)

TIGHT_WORKSPACE = (683, 384)
ROOMY_WORKSPACE = (2560, 1440)
FRAME = QMargins(8, 30, 8, 8)
WINDOW_SIZES = [
    (WINDOW_PREFERRED_MINIMUM, WINDOW_DEFAULT_SIZE, WINDOW_ABSOLUTE_MINIMUM),
    (SETTINGS_PREFERRED_MINIMUM, SETTINGS_DEFAULT_SIZE, SETTINGS_ABSOLUTE_MINIMUM),
]


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.parametrize("preferred,default,floor", WINDOW_SIZES)
def test_frame_and_client_fit_a_tight_workspace(preferred, default, floor):
    minimum, size = components.clamp_to_workspace(
        TIGHT_WORKSPACE, preferred, default, floor, FRAME
    )
    assert minimum[0] + FRAME.left() + FRAME.right() <= TIGHT_WORKSPACE[0]
    assert minimum[1] + FRAME.top() + FRAME.bottom() <= TIGHT_WORKSPACE[1]
    assert size == (667, 346)
    assert minimum[1] == 346  # Smaller than the main window's 380px floor.


@pytest.mark.parametrize("preferred,default,floor", WINDOW_SIZES)
def test_roomy_workspace_preserves_preferred_sizes(preferred, default, floor):
    assert components.clamp_to_workspace(
        ROOMY_WORKSPACE, preferred, default, floor, FRAME
    ) == (preferred, default)


def test_zero_margins_and_no_screen_keep_the_old_fallback(qapp, monkeypatch):
    assert components.clamp_to_workspace(
        TIGHT_WORKSPACE, (600, 300), (760, 560), (320, 240), QMargins()
    ) == ((600, 300), TIGHT_WORKSPACE)
    monkeypatch.setattr(components, "_available_workspace", lambda _widget: None)
    widget = QWidget()
    components.fit_to_workspace(widget, (600, 400), (760, 560), (320, 240))
    assert widget.minimumSize().toTuple() == (600, 400)
    assert widget.size().toTuple() == (760, 560)
    widget.show()
    qapp.processEvents()
    assert widget.size().toTuple() == (760, 560)
    widget.close()


@pytest.mark.parametrize("screen_changes_on_show", [False, True])
def test_first_show_rechecks_delayed_margins_and_minimum(
    qapp, monkeypatch, screen_changes_on_show
):
    work = QRect(0, 0, *TIGHT_WORKSPACE)
    roomy = QRect(0, 0, *ROOMY_WORKSPACE)
    monkeypatch.setattr(
        components, "_available_workspace",
        lambda widget: roomy if screen_changes_on_show and not widget.isVisible() else work,
    )
    monkeypatch.setattr(
        components, "_frame_margins",
        lambda widget: FRAME if widget.isVisible() else QMargins(),
    )
    widget = QWidget()
    components.fit_to_workspace(
        widget, WINDOW_PREFERRED_MINIMUM, WINDOW_DEFAULT_SIZE, WINDOW_ABSOLUTE_MINIMUM
    )
    assert widget.size().toTuple() == (
        WINDOW_DEFAULT_SIZE if screen_changes_on_show else TIGHT_WORKSPACE
    )
    assert widget.minimumSize().toTuple() == (
        WINDOW_PREFERRED_MINIMUM if screen_changes_on_show else TIGHT_WORKSPACE
    )
    widget.show()
    qapp.processEvents()
    assert widget.size().toTuple() == (667, 346)
    assert widget.minimumSize().toTuple() == (667, 346)
    assert work.contains(widget.windowHandle().frameGeometry())
    widget.close()


def test_later_resize_and_position_survive_hide_show(qapp, monkeypatch):
    work = QRect(0, 0, *TIGHT_WORKSPACE)
    monkeypatch.setattr(components, "_available_workspace", lambda _widget: work)
    monkeypatch.setattr(components, "_frame_margins", lambda _widget: FRAME)
    widget = QWidget()
    components.fit_to_workspace(widget, (300, 200), (760, 560), (200, 150))
    widget.show()
    qapp.processEvents()
    widget.resize(500, 250)
    widget.windowHandle().setFramePosition(QPoint(50, 40))
    widget.hide()
    widget.show()
    before = widget.windowHandle().frameGeometry()
    qapp.processEvents()
    assert widget.size().toTuple() == (500, 250)
    assert widget.windowHandle().frameGeometry() == before
    widget.close()


@pytest.mark.parametrize("origin", [(-800, -300), (120, 75)])
@pytest.mark.parametrize("edge", ["left", "right", "top", "bottom"])
def test_first_show_moves_only_the_frame_edge_outside_the_workspace(
    qapp, monkeypatch, origin, edge
):
    work = QRect(*origin, *TIGHT_WORKSPACE)
    monkeypatch.setattr(components, "_available_workspace", lambda _widget: work)
    widget = QWidget()
    components.fit_to_workspace(widget, (200, 120), (320, 180), (100, 80))
    positions = {
        "left": (work.left() - 50, work.top() + 30),
        "right": (work.right() + 50, work.top() + 30),
        "top": (work.left() + 30, work.top() - 50),
        "bottom": (work.left() + 30, work.bottom() + 50),
    }
    window = widget.windowHandle()
    window.setFramePosition(QPoint(*positions[edge]))
    widget.show()
    before = window.frameGeometry()
    qapp.processEvents()
    after = window.frameGeometry()
    assert work.contains(after)
    assert getattr(after, edge)() == getattr(work, edge)()
    if edge in ("left", "right"):
        assert after.top() == before.top()
    else:
        assert after.left() == before.left()
    widget.close()


def test_roomy_position_and_default_are_not_changed(qapp, monkeypatch):
    work = QRect(100, 75, *ROOMY_WORKSPACE)
    monkeypatch.setattr(components, "_available_workspace", lambda _widget: work)
    monkeypatch.setattr(components, "_frame_margins", lambda _widget: FRAME)
    widget = QWidget()
    components.fit_to_workspace(
        widget, WINDOW_PREFERRED_MINIMUM, WINDOW_DEFAULT_SIZE, WINDOW_ABSOLUTE_MINIMUM
    )
    widget.windowHandle().setFramePosition(QPoint(250, 150))
    widget.show()
    before = widget.windowHandle().frameGeometry()
    qapp.processEvents()
    assert widget.size().toTuple() == WINDOW_DEFAULT_SIZE
    assert widget.minimumSize().toTuple() == WINDOW_PREFERRED_MINIMUM
    assert widget.windowHandle().frameGeometry() == before
    widget.close()


def _assert_reachable(window, control):
    assert control.isVisibleTo(window)
    assert control.width() > 0 and control.height() > 0
    bounds = QRect(control.mapTo(window, QPoint(0, 0)), control.size())
    assert window.rect().contains(bounds), (bounds, window.rect())


def test_real_windows_keep_actions_and_editors_reachable_at_client_budget(
    qapp, monkeypatch
):
    work = QRect(0, 0, *TIGHT_WORKSPACE)
    monkeypatch.setattr(components, "_available_workspace", lambda _widget: work)
    monkeypatch.setattr(components, "_frame_margins", lambda _widget: FRAME)
    previous = qapp.styleSheet()
    qapp.setStyleSheet(theme.load_stylesheet(APPEARANCE_LIGHT))
    main = MainWindow(ClientConfig())
    dialog = SettingsDialog(ClientConfig())
    try:
        for window in (main, dialog):
            window.show()
            qapp.processEvents()
            assert window.size().toTuple() == (667, 346)
            assert window.minimumHeight() == 346
            assert work.contains(window.windowHandle().frameGeometry())

        _assert_reachable(main, main.menuBar())
        file_actions = main.menuBar().actions()[0].menu().actions()[:4]
        assert [action.text() for action in file_actions] == [
            strings.MANUSCRIPT_NEW, strings.MANUSCRIPT_OPEN,
            strings.MANUSCRIPT_SAVE, strings.MANUSCRIPT_SAVE_AS,
        ]
        assert all(action.isEnabled() for action in file_actions)
        for control in (
            main.manuscript_view.new_button, main.manuscript_view.open_button,
            main.manuscript_view.save_button, main.manuscript_view.save_as_button,
            main.manuscript_view.source_edit.viewport(),
            main.manuscript_view.target_edit.viewport(),
        ):
            _assert_reachable(main, control)

        box = dialog.findChild(QDialogButtonBox)
        assert box is not None
        for button in (QDialogButtonBox.StandardButton.Ok, QDialogButtonBox.StandardButton.Cancel):
            _assert_reachable(dialog, box.button(button))
    finally:
        dialog.close()
        main.close()
        qapp.setStyleSheet(previous)
