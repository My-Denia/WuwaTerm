"""The shared widgets every view in this client is assembled from.

Two rules shape this module.

Components receive their display text from the caller or read it from
``strings``. They own shape and state rather than wording.

The second is that a Qt widget does not restyle itself when a dynamic
property changes. ``setProperty`` alone leaves the old style applied; the
style has to be told to recompute with ``unpolish``/``polish``. Doing that at
every call site is how half of them end up missing it and the badge stays the
wrong color, so every property switch in this codebase goes through
``set_dynamic_property`` below and lives inside a component method. Call sites
never touch a dynamic property directly.
"""

from __future__ import annotations

import math

from collections.abc import Callable, Sequence

from PySide6.QtCore import QEvent, QMargins, QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import strings

# How long the endpoint chip stays highlighted after the address changes.
# Long enough to be noticed while looking elsewhere on screen, short enough
# that it is over before it reads as a new permanent state.
FLASH_MILLISECONDS = 240

# The severities a banner understands. A caller passing anything else gets the
# neutral treatment rather than an exception: a mis-labelled banner is bad, a
# window that fails to draw an error is worse.
BANNER_SEVERITIES = ("danger", "warn", "info", "muted")

# The four match kinds the badge can draw. Anything else - a kind this client
# release has never heard of - degrades to the neutral shape while the caller
# still shows the server's own word for it.
BADGE_KINDS = ("exact", "fuzzy", "llm", "noop")
FALLBACK_BADGE_KIND = "noop"

SCORE_MINIMUM = 0.0
SCORE_MAXIMUM = 100.0

# "This banner is not about a request." Distinct from None, which means "a
# request happened and produced no id" - those two must not render the same,
# because only one of them is something an operator could be asked about.
NO_REQUEST = "\x00no-request"


def repolish(widget: QWidget) -> None:
    """Make the style recompute this widget after a dynamic property changed."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_dynamic_property(widget: QWidget, name: str, value: object) -> None:
    """Set a style-selectable property AND make it take effect.

    The pairing is the whole point: `setProperty` on its own changes what a
    selector would match without asking anything to match again.
    """
    widget.setProperty(name, value)
    repolish(widget)


def mark_field_invalid(widget: QWidget, invalid: bool) -> None:
    """Turn an input control's outline to the danger color, or back.

    Kept next to the field-error label because the two always move together:
    an outline with no explanation says something is wrong without saying
    what, and an explanation with no outline does not say where.
    """
    set_dynamic_property(widget, "invalid", bool(invalid))


# keyring's backend CLASS NAMES, mapped to what the row should read. Same
# shape as the view-level tables elsewhere in this package: strings.py holds
# the words, the consumer holds which value earns which word. "Keyring" is the
# stand-in keyring installs when it can find no usable backend at all - its
# get and set both raise.
_CREDENTIAL_BACKEND_LABELS = {
    "WinVaultKeyring": strings.CREDENTIAL_BACKEND_WINDOWS,
    "Keyring": strings.CREDENTIAL_BACKEND_UNAVAILABLE,
    "fail.Keyring": strings.CREDENTIAL_BACKEND_UNAVAILABLE,
}


def apply_credential_backend(label: QLabel, raw_backend: str) -> None:
    """Say where the credential lives, in the interface's language.

    `raw_backend` is a keyring CLASS NAME, so it is neither text this program
    wrote nor text Qt supplies - the third way English reaches the screen past
    every gate. The reader of this row wants to know where their token is, not
    which class implements it; the class name stays one hover away for the one
    case that needs it, which is working out why keyring chose that backend.

    An unrecognised backend degrades to the raw name, for the same reason an
    unknown term category does: a readable English word is a defect anyone can
    see and report, and a blank is not.
    """
    label.setText(_CREDENTIAL_BACKEND_LABELS.get(raw_backend, raw_backend))
    label.setToolTip(
        strings.CREDENTIAL_BACKEND_TOOLTIP.format(backend=raw_backend)
    )


def _clear_layout(layout: QHBoxLayout | QVBoxLayout) -> None:
    """Remove and destroy every widget currently in a layout."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


class Banner(QFrame):
    """One message, one severity, up to a handful of actions.

    At most one message is shown at a time: a second call replaces the first
    rather than stacking, because two red boxes for one action read as two
    failures. The request-id row underneath is laid out for every REQUEST
    outcome, even one that carries no id - hiding it there would change the
    banner's height between an error the owner can quote to an operator and
    one they cannot, and a box that resizes as it changes wording is a box
    people stop reading. It is absent entirely when the banner is not about
    a request at all; see `show_message`.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("banner")
        self._message_text = ""
        self._request_id: str | None = None
        self._showing = False

        # Deliberately textless: the severity mark is drawn by the stylesheet
        # from the `severity` property, so there is no glyph to translate and
        # no bitmap to ship.
        self._icon = QLabel(self)
        self._icon.setObjectName("bannerIcon")

        self._text_label = QLabel(self)
        self._text_label.setObjectName("bannerText")
        self._text_label.setWordWrap(True)
        self._text_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        self._actions_host = QWidget(self)
        self._actions_host.setObjectName("bannerActions")
        self._actions_layout = QHBoxLayout(self._actions_host)
        self._actions_layout.setContentsMargins(0, 0, 0, 0)
        self._actions_layout.setSpacing(8)

        self._close_button = QPushButton(strings.ACTION_DISMISS, self)
        self._close_button.setObjectName("linkButton")
        self._close_button.clicked.connect(self.clear)

        self._request_id_label = QLabel(self)
        self._request_id_label.setObjectName("monoLabel")
        self._request_id_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self._copy_button = QPushButton(strings.REQUEST_ID_COPY_BUTTON, self)
        self._copy_button.setObjectName("linkButton")
        self._copy_button.clicked.connect(self._on_copy_clicked)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(8)
        top_row.addWidget(self._icon)
        top_row.addWidget(self._text_label, 1)
        top_row.addWidget(self._actions_host)
        top_row.addWidget(self._close_button)

        # A widget rather than a bare layout, because the row is shown for a
        # request outcome and hidden for everything else, and only a widget
        # can be toggled as one.
        self._request_id_row = QWidget(self)
        id_row = QHBoxLayout(self._request_id_row)
        id_row.setContentsMargins(0, 0, 0, 0)
        id_row.setSpacing(8)
        id_row.addWidget(self._request_id_label)
        id_row.addWidget(self._copy_button)
        id_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.addLayout(top_row)
        layout.addWidget(self._request_id_row)

        self._apply_request_id(NO_REQUEST)
        set_dynamic_property(self, "severity", BANNER_SEVERITIES[-1])
        self.setVisible(False)

    # -- public API --------------------------------------------------------

    @property
    def message_text(self) -> str:
        """What the banner currently says, or the empty string when cleared."""
        return self._message_text

    @property
    def request_id(self) -> str | None:
        return self._request_id

    def show_message(
        self,
        text: str,
        severity: str,
        actions: Sequence[tuple[str, Callable[[], None]]] = (),
        closable: bool = True,
        request_id: str | None = NO_REQUEST,
    ) -> None:
        """Replace whatever the banner was showing with this message.

        ``request_id`` has three states, not two, and the third is why the
        default is a sentinel rather than ``None``. A request that failed
        without an id still gets the row - a placeholder, so the block does
        not change height between an outcome that carries one and one that
        does not. But a banner about something that was never a request at
        all - no address configured, the credential store unavailable - has
        no id to be missing, and printing "request ID: -" there invites the
        owner to go asking an operator about a call this client never made.
        Omitting the argument means exactly that: no request, no row.
        """
        value = severity if severity in BANNER_SEVERITIES else BANNER_SEVERITIES[-1]
        self._message_text = text
        self._text_label.setText(text)
        for widget in (self, self._icon, self._text_label):
            set_dynamic_property(widget, "severity", value)

        _clear_layout(self._actions_layout)
        for label, callback in actions:
            button = QPushButton(label, self._actions_host)
            button.setObjectName("secondaryButton")
            button.clicked.connect(lambda _checked=False, cb=callback: cb())
            self._actions_layout.addWidget(button)
        self._actions_host.setVisible(bool(actions))

        self._close_button.setVisible(bool(closable))
        self._apply_request_id(request_id)

        self._showing = True
        self.setVisible(True)

    def clear(self) -> None:
        """Take the banner off screen and forget what it said."""
        self._message_text = ""
        self._text_label.setText("")
        _clear_layout(self._actions_layout)
        self._actions_host.setVisible(False)
        self._apply_request_id(NO_REQUEST)
        self._showing = False
        self.setVisible(False)

    def is_showing(self) -> bool:
        """Whether a message is currently displayed.

        Tracked rather than read from `isVisible`, which is False for every
        widget whose window has not been shown - including in the test
        suite, where no window ever is.
        """
        return self._showing

    # -- internals ---------------------------------------------------------

    def _apply_request_id(self, request_id: str | None) -> None:
        applicable = request_id is not NO_REQUEST
        self._request_id = request_id if applicable else None
        self._request_id_row.setVisible(applicable)
        if not applicable:
            return
        shown = self._request_id if self._request_id else strings.REQUEST_ID_PLACEHOLDER
        self._request_id_label.setText(
            strings.REQUEST_ID_LABEL.format(request_id=shown)
        )
        self._request_id_label.setToolTip(shown)
        self._copy_button.setEnabled(self._request_id is not None)

    def _on_copy_clicked(self) -> None:
        if not self._request_id:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._request_id)


class KindBadge(QFrame):
    """Where a translation came from, encoded three ways at once.

    Shape, hue and word each carry the whole answer, so the badge survives a
    grayscale screenshot and a reader who cannot separate the hues. An
    unrecognised kind keeps the server's own word and takes the neutral
    shape, rather than being silently mapped onto a kind it is not.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("kindBadge")

        self._dot = QLabel(self)
        self._dot.setObjectName("kindDot")

        self._label = QLabel(self)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(4)
        layout.addWidget(self._dot)
        layout.addWidget(self._label)

        self._current_kind = FALLBACK_BADGE_KIND
        self.set_kind(FALLBACK_BADGE_KIND, "")

    @property
    def current_kind(self) -> str:
        """The kind actually applied - the fallback when the input was unknown."""
        return self._current_kind

    @property
    def label_text(self) -> str:
        return self._label.text()

    def set_kind(self, kind: str, label_text: str) -> None:
        value = kind if kind in BADGE_KINDS else FALLBACK_BADGE_KIND
        self._current_kind = value
        for widget in (self, self._dot):
            set_dynamic_property(widget, "kind", value)
        self._label.setText(label_text)


class FieldError(QLabel):
    """The one line under an input that says what is wrong with it.

    Takes no height while there is nothing to say: a permanently reserved
    error row trains the eye to skip the place errors appear.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("fieldError")
        self.setWordWrap(True)
        self.setVisible(False)

    def show_error(self, text: str) -> None:
        self.setText(text)
        self.setVisible(True)

    def clear(self) -> None:
        self.setText("")
        self.setVisible(False)

    def is_showing(self) -> bool:
        return bool(self.text())


class EmptyStateCard(QFrame):
    """What an area shows before it has anything to show.

    Two shapes, one widget: a title with an optional subtitle and action, and
    a numbered checklist for the state where the client cannot do anything
    until the owner has configured it. They are one widget because they
    occupy the same place and only one of them is ever true.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("emptyCard")

        self._title = QLabel(self)
        self._title.setObjectName("emptyTitle")
        self._title.setWordWrap(True)
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._subtitle = QLabel(self)
        self._subtitle.setObjectName("emptySubtitle")
        self._subtitle.setWordWrap(True)
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle.setVisible(False)

        self._steps_host = QFrame(self)
        self._steps_host.setObjectName("stepList")
        self._steps_layout = QVBoxLayout(self._steps_host)
        self._steps_layout.setContentsMargins(0, 0, 0, 0)
        self._steps_layout.setSpacing(8)
        self._steps_host.setVisible(False)

        self._action_host = QWidget(self)
        self._action_layout = QHBoxLayout(self._action_host)
        self._action_layout.setContentsMargins(0, 0, 0, 0)
        self._action_layout.setSpacing(8)
        self._action_layout.addStretch(1)
        self._action_host.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addWidget(self._title)
        layout.addWidget(self._subtitle)
        layout.addWidget(self._steps_host)
        layout.addWidget(self._action_host)

    @property
    def title_text(self) -> str:
        return self._title.text()

    @property
    def subtitle_text(self) -> str:
        return self._subtitle.text()

    def set_content(
        self,
        title: str,
        subtitle: str = "",
        action: tuple[str, Callable[[], None]] | None = None,
    ) -> None:
        self._title.setText(title)
        self._title.setVisible(bool(title))
        self._subtitle.setText(subtitle)
        self._subtitle.setVisible(bool(subtitle))
        self._clear_steps()
        self._steps_host.setVisible(False)
        self._set_action(action)

    def set_steps(
        self,
        steps: Sequence[tuple[str, bool]],
        action: tuple[str, Callable[[], None]] | None = None,
    ) -> None:
        """Draw the checklist form: each step, and whether it is already done.

        The done flags are decided by the caller from configuration and the
        credential store. Nothing here asks the network anything - the state
        this card describes is precisely the one in which no request may be
        sent.
        """
        self._clear_steps()
        for text, done in steps:
            row = QFrame(self._steps_host)
            row.setObjectName("stepItem")

            row_label = QLabel(text, row)
            row_label.setWordWrap(True)

            done_label = QLabel(row)
            done_label.setText(strings.STEP_DONE_MARK if done else "")

            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            row_layout.addWidget(row_label, 1)
            row_layout.addWidget(done_label)

            set_dynamic_property(row, "done", bool(done))
            self._steps_layout.addWidget(row)

        self._steps_host.setVisible(bool(steps))
        self._subtitle.setVisible(bool(self._subtitle.text()))
        self._set_action(action)

    def _clear_steps(self) -> None:
        _clear_layout(self._steps_layout)

    def _set_action(self, action: tuple[str, Callable[[], None]] | None) -> None:
        # The stretch that keeps the button centred is rebuilt with the row,
        # so the whole layout is emptied rather than only its widgets.
        while self._action_layout.count():
            item = self._action_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        if action is None:
            self._action_host.setVisible(False)
            return
        label, callback = action
        button = QPushButton(label, self._action_host)
        button.setObjectName("primaryButton")
        button.clicked.connect(lambda _checked=False, cb=callback: cb())
        self._action_layout.addStretch(1)
        self._action_layout.addWidget(button)
        self._action_layout.addStretch(1)
        self._action_host.setVisible(True)


class StatusStrip(QLabel):
    """The single quiet line at the bottom, overwritten by the next action.

    It carries confirmations - cancelled, copied, saved - and never an
    alarm: anything the owner has to act on belongs in a banner or on the
    field that caused it.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statusStrip")

    def set_text(self, text: str) -> None:
        self.setText(text)

    def clear(self) -> None:
        self.setText("")


class EndpointChip(QFrame):
    """Which server this client talks to, or that it has none.

    The configured wording says "configured" and stops there. This client has
    not spoken to the address before its first successful request, so any
    word implying a live connection would be a claim it has never checked.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("endpointChip")

        self._dot = QLabel(self)
        self._dot.setObjectName("endpointDot")

        self._state_label = QLabel(self)

        self._address_label = QLabel(self)
        self._address_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        state_row = QHBoxLayout()
        state_row.setContentsMargins(0, 0, 0, 0)
        state_row.setSpacing(4)
        state_row.addWidget(self._dot)
        state_row.addWidget(self._state_label)
        state_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.addLayout(state_row)
        layout.addWidget(self._address_label)

        self._address_text = ""
        self._configured = False
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self._end_flash)
        set_dynamic_property(self, "state", "missing")
        set_dynamic_property(self._dot, "state", "missing")

    @property
    def is_configured(self) -> bool:
        return self._configured

    @property
    def address_text(self) -> str:
        """The full address, before it is shortened to fit the chip."""
        return self._address_text

    def set_state(
        self,
        configured: bool,
        label_text: str,
        address_text: str,
        tooltip: str,
    ) -> None:
        self._configured = bool(configured)
        state = "ok" if configured else "missing"
        for widget in (self, self._dot):
            set_dynamic_property(widget, "state", state)
        self._state_label.setText(label_text)
        self._address_text = address_text
        self._apply_address_elide()
        self.setToolTip(tooltip)

    def flash(self) -> None:
        """Highlight the chip once, so a change of address is noticed."""
        set_dynamic_property(self, "flash", True)
        self._restore_timer.start(FLASH_MILLISECONDS)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._apply_address_elide()

    def _apply_address_elide(self) -> None:
        """Shorten the address in the middle so both ends stay readable.

        The head names the scheme and host, the tail names the path - the two
        halves an owner uses to recognise which service this is. Dropping
        either would make two different addresses look the same.
        """
        metrics = self._address_label.fontMetrics()
        available = max(self._address_label.width(), 0)
        if available <= 0:
            self._address_label.setText(self._address_text)
            return
        self._address_label.setText(
            metrics.elidedText(
                self._address_text, Qt.TextElideMode.ElideMiddle, available
            )
        )

    def _end_flash(self) -> None:
        set_dynamic_property(self, "flash", False)


class ProgressLine(QProgressBar):
    """A hairline that says work is in progress and nothing more.

    Indeterminate on purpose: this client cannot know how far along a request
    is, and a bar that invents a percentage is worse than one that admits it
    does not know. It never blocks input - no modal, no overlay.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("progressLine")
        self.setRange(0, 0)
        self.setTextVisible(False)
        self._running = False
        self.setVisible(False)

    def start(self) -> None:
        self._running = True
        self.setVisible(True)

    def stop(self) -> None:
        self._running = False
        self.setVisible(False)

    def is_running(self) -> bool:
        """Tracked rather than read back from visibility, which is False for
        every widget in a window that was never shown."""
        return self._running


class ScoreBar(QWidget):
    """How strong a dictionary match is, as a length rather than a number.

    The number stays in its own column; this is the second encoding of the
    same fact, for reading a table at a glance. The range is the service's
    own: `GET /v1/terms` scores an exact hit at 100 and drops fuzzy matches
    below 45, so the bar is filled against 0-100 and values outside that are
    clamped rather than rescaled.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("scoreBar")
        # A plain QWidget draws no stylesheet background without this; the
        # track would be invisible and the fill would float on nothing.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._fill = QFrame(self)
        self._fill.setObjectName("scoreBarFill")
        self._fill.setMinimumWidth(0)
        self._fill.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        self._remainder = QWidget(self)
        self._remainder.setMinimumWidth(0)
        self._remainder.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._fill)
        layout.addWidget(self._remainder)
        self._layout = layout

        self._score = SCORE_MINIMUM
        self.set_score(SCORE_MINIMUM)

    def set_score(self, value: float) -> None:
        # Non-finite values are refused one layer up, in the response parser,
        # where an unusable body becomes a reported error. This is the second
        # floor under that: min/max PROPAGATE NaN rather than clamping it, so
        # a NaN arriving by any other route would reach `round()` and raise
        # in the middle of drawing. A widget that cannot draw a number should
        # draw an empty bar, not take the window down with it.
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = SCORE_MINIMUM
        if not math.isfinite(number):
            number = SCORE_MINIMUM
        score = min(max(number, SCORE_MINIMUM), SCORE_MAXIMUM)
        self._score = score
        filled = int(round(score))
        self._layout.setStretch(0, filled)
        self._layout.setStretch(1, 100 - filled)
        self._fill.setVisible(filled > 0)

    def score(self) -> float:
        """The clamped score currently drawn, on the service's 0-100 range."""
        return self._score

    def percent(self) -> int:
        """The same value as a whole percentage of the bar's width."""
        return int(round(self._score))


def fit_to_workspace(
    widget: QWidget,
    preferred_minimum: tuple[int, int],
    default_size: tuple[int, int],
    absolute_minimum: tuple[int, int],
) -> None:
    """Size a top-level widget against the desktop it will actually appear on.

    Qt reports both the workspace and window geometry in logical pixels. The
    window's client area must leave room for the visible frame as well as the
    taskbar, even when that leaves less room than the desired minimum.

    Native frame margins may change on first show. Recheck once after that
    show, without resetting a size or position that already fits.
    """
    widget.ensurePolished()
    widget.winId()  # Create the native window before asking for its frame.
    available = _available_workspace(widget)
    if available is None:
        # Without a screen there is no measured limit to apply.
        widget.setMinimumSize(*preferred_minimum)
        widget.resize(*default_size)
    else:
        minimum, size = clamp_to_workspace(
            (available.width(), available.height()),
            preferred_minimum,
            default_size,
            absolute_minimum,
            _frame_margins(widget),
        )
        widget.setMinimumSize(*minimum)
        widget.resize(*size)

    if not hasattr(widget, "_workspace_fit_correction"):
        widget._workspace_fit_correction = _FirstShowWorkspaceFit(widget)


def _available_workspace(widget: QWidget) -> QRect | None:
    screen = widget.screen() or QGuiApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else None


def _frame_margins(widget: QWidget) -> QMargins:
    window = widget.windowHandle()
    return window.frameMargins() if window is not None else QMargins()


class _FirstShowWorkspaceFit(QObject):
    """Correct the frame once native decorations have been established."""

    def __init__(self, widget: QWidget) -> None:
        super().__init__(widget)
        self._widget = widget
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._correct)
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self._widget and event.type() == QEvent.Type.Show:
            self._widget.removeEventFilter(self)
            self._timer.start(0)
        return False

    def _correct(self) -> None:
        widget = self._widget
        available = _available_workspace(widget)
        window = widget.windowHandle()
        if available is None or window is None:
            return

        old_minimum = (widget.minimumWidth(), widget.minimumHeight())
        old_size = (widget.width(), widget.height())
        minimum, size = clamp_to_workspace(
            (available.width(), available.height()),
            old_minimum,
            old_size,
            (0, 0),
            _frame_margins(widget),
        )
        if minimum != old_minimum:
            widget.setMinimumSize(*minimum)
        if size != old_size:
            widget.resize(*size)

        frame = window.frameGeometry()
        x = max(available.left(), min(frame.left(), available.right() - frame.width() + 1))
        y = max(available.top(), min(frame.top(), available.bottom() - frame.height() + 1))
        if (x, y) != (frame.left(), frame.top()):
            window.setFramePosition(QPoint(x, y))


def clamp_to_workspace(
    workspace: tuple[int, int],
    preferred_minimum: tuple[int, int],
    default_size: tuple[int, int],
    absolute_minimum: tuple[int, int],
    frame_margins: QMargins | None = None,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Clamp client dimensions to the space left after window decorations."""
    if frame_margins is None:
        frame_margins = QMargins()
    budget = (
        max(1, workspace[0] - frame_margins.left() - frame_margins.right()),
        max(1, workspace[1] - frame_margins.top() - frame_margins.bottom()),
    )
    minimum = tuple(
        min(budget[i], max(preferred_minimum[i], absolute_minimum[i]))
        for i in range(2)
    )
    size = tuple(
        min(budget[i], max(default_size[i], minimum[i])) for i in range(2)
    )
    return minimum, size
