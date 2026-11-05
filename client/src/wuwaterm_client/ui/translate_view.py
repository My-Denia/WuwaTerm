"""Source text in, translation out - and every outcome of that round trip.

Three things this view has to get right, none of which are about layout.

The first is WHERE an outcome lands. A translation can end in a failure the
owner has to act on, in one they have to fix in the box in front of them, or
in a cancellation they asked for themselves. ``ui/error_presentation`` decides
which of those each of the fifteen codes is; this file only owns the widgets
those surfaces are drawn on. A code can therefore never be handled here in a
way that contradicts how the term-lookup area handles the same code.

The second is that the request id is rendered by ONE widget in ONE place for
every request outcome. It is the only handle the owner has when asking an
operator what happened on the other side, and a row that appears for one
outcome and not for another is a row nobody learns to look at. A completed
request with no id gets a placeholder; a screen where no request happened gets
no row at all.

The third is ordering. ``api._request`` turns a cancellation into an ordinary
``ClientError``, so a request stopped because the server address changed comes
back looking like a perfectly renderable outcome. Every write to a widget
below is therefore guarded by a generation counter: a coroutine started before
the last reset renders nothing at all, rather than writing the previous
server's cancellation over a view that has already been cleared for a new one.
"""

from __future__ import annotations

import asyncio

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import strings
from ..api import ApiClient, TranslationResult
from ..errors import ERROR_CANCELLED, ClientError
from .components import (
    Banner,
    FieldError,
    KindBadge,
    NO_REQUEST,
    ProgressLine,
    StatusStrip,
    mark_field_invalid,
)
from .icons import icon
from .error_presentation import (
    ACTION_ENTER_TOKEN,
    ACTION_OPEN_SETTINGS,
    ACTION_RETRY,
    SURFACE_FIELD,
    SURFACE_STATUS,
    ErrorPresentation,
    presentation_for,
)

DIRECTION_AUTO = "auto"
DIRECTION_TO_EN = "en"
DIRECTION_TO_ZH = "zh"

_DIRECTION_ITEMS = (
    (DIRECTION_AUTO, strings.DIRECTION_AUTO),
    (DIRECTION_TO_EN, strings.DIRECTION_TO_EN),
    (DIRECTION_TO_ZH, strings.DIRECTION_TO_ZH),
)

_KIND_LABELS = {
    "exact": strings.KIND_LABEL_EXACT,
    "fuzzy": strings.KIND_LABEL_FUZZY,
    "llm": strings.KIND_LABEL_LLM,
    "noop": strings.KIND_LABEL_NOOP,
}

# The dictionary-miss mark is a SECOND badge, never a replacement for the
# kind badge: "no official term stood behind this" is true or false
# independently of which of the four kinds produced the text, and collapsing
# the two dimensions into one badge would lose whichever one lost the fight.
#
# The kind passed here is not a claim that the match was fuzzy. It selects the
# only badge shape the stylesheet draws in the warn hue - a hollow ring - and
# that is the shape the design assigns to this warning.
_MISS_BADGE_KIND = "fuzzy"


class _ElidedValueLabel(QLabel):
    """A label that shortens its text in the middle and keeps the whole of it.

    Used for the request id, where the two ends are what an owner reads back
    to an operator and the middle is filler. The untruncated value stays
    reachable through the tooltip and through `full_text`, so nothing that is
    shortened on screen is actually lost.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        # Ignored, not Preferred: the label must be allowed to be narrower
        # than its text, which is the only condition under which eliding does
        # anything at all.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def full_text(self) -> str:
        """The value before it was shortened to fit."""
        return self._full_text

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._apply_elide()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        metrics = self.fontMetrics()
        available = max(self.width(), 0)
        if available <= 0:
            self.setText(self._full_text)
            return
        self.setText(
            metrics.elidedText(
                self._full_text, Qt.TextElideMode.ElideMiddle, available
            )
        )


class TranslateView(QWidget):
    def __init__(
        self,
        api_client: ApiClient,
        parent: QWidget | None = None,
        *,
        on_open_settings: "object | None" = None,
        on_enter_token: "object | None" = None,
    ) -> None:
        super().__init__(parent)
        self._api_client = api_client
        self._task: asyncio.Task | None = None
        # Bumped every time the view starts caring about a different request.
        # A coroutine compares the value it captured against this one before
        # touching a widget; see the module docstring.
        self._generation = 0
        self._last_request: tuple[str, str | None] | None = None
        self._request_id: str | None = None
        # Supplied by whoever owns the settings and token flows. An action
        # with no handler is left off the banner rather than drawn as a
        # button that does nothing when pressed.
        self._on_open_settings = on_open_settings
        self._on_enter_token = on_enter_token

        # A hairline at the top of the area, never a modal and never an
        # overlay: the owner keeps reading and editing while a request runs.
        self.progress_line = ProgressLine(self)

        self.banner = Banner(self)

        self.input_edit = QPlainTextEdit(self)
        self.input_edit.setPlaceholderText(strings.INPUT_PLACEHOLDER)
        # Editing the text is the fix for every field-level error, so the
        # first keystroke takes the red outline back off.
        self.input_edit.textChanged.connect(self._on_input_changed)

        self.field_error = FieldError(self)

        self.direction_combo = QComboBox(self)
        for value, label in _DIRECTION_ITEMS:
            self.direction_combo.addItem(label, value)

        self.translate_button = QPushButton(strings.TRANSLATE_BUTTON, self)
        self.translate_button.setObjectName("primaryButton")
        self.translate_button.clicked.connect(self._on_translate_clicked)

        # Always present, disabled while idle. Appearing only during a request
        # would move the whole button row sideways at the exact moment the
        # owner is reaching for it.
        self.cancel_button = QPushButton(strings.CANCEL_BUTTON, self)
        self.cancel_button.setObjectName("secondaryButton")
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        self.cancel_button.setEnabled(False)

        self.result_edit = QPlainTextEdit(self)
        self.result_edit.setReadOnly(True)
        self.result_edit.setPlaceholderText(strings.RESULT_PLACEHOLDER)
        self.copy_result_button = QPushButton(self)
        self.copy_result_button.setObjectName("commandButton")
        self.copy_result_button.setIcon(icon("copy"))
        self.copy_result_button.setIconSize(QSize(16, 16))
        self.copy_result_button.setToolTip(strings.TRANSLATE_COPY_RESULT)
        self.copy_result_button.setAccessibleName(strings.TRANSLATE_COPY_RESULT)
        self.copy_result_button.setEnabled(False)
        self.copy_result_button.clicked.connect(self._on_copy_result_clicked)
        self.result_edit.textChanged.connect(
            lambda: self.copy_result_button.setEnabled(bool(self.result_edit.toPlainText()))
        )

        self.kind_badge = KindBadge(self)
        self.kind_badge.setVisible(False)
        self.miss_badge = KindBadge(self)
        self.miss_badge.setVisible(False)

        self.miss_note = QLabel(self)
        self.miss_note.setObjectName("emptySubtitle")
        self.miss_note.setWordWrap(True)
        self.miss_note.setVisible(False)

        self.request_id_label = _ElidedValueLabel(self)
        self.request_id_label.setObjectName("monoLabel")
        self.request_id_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.request_id_copy_button = QPushButton(
            strings.REQUEST_ID_COPY_BUTTON, self
        )
        self.request_id_copy_button.setObjectName("linkButton")
        self.request_id_copy_button.clicked.connect(self._on_copy_request_id_clicked)

        # What cancelling did and did not do. Kept apart from the status line
        # so the status line can stay the one-line terminal word.
        self.cancel_note = QLabel(self)
        self.cancel_note.setObjectName("emptySubtitle")
        self.cancel_note.setWordWrap(True)
        self.cancel_note.setVisible(False)

        self.status_label = StatusStrip(self)

        input_label = QLabel(strings.INPUT_LABEL, self)
        direction_label = QLabel(strings.DIRECTION_LABEL, self)
        result_label = QLabel(strings.RESULT_LABEL, self)
        # A label placed NEXT TO a control is not attached to it: assistive
        # technology reads the two as unrelated, so the editor still announces
        # itself as an unnamed text area. `setBuddy` is the association, and it
        # costs a line. The sibling-consistency gate found these three - the
        # reviewer had only reported the lookup area, which had no label at all.
        input_label.setBuddy(self.input_edit)
        direction_label.setBuddy(self.direction_combo)
        result_label.setBuddy(self.result_edit)
        request_id_row_label = QLabel(strings.REQUEST_ID_ROW_LABEL, self)

        toolbar = QFrame(self)
        toolbar.setObjectName("workbenchToolbar")
        toolbar_row = QHBoxLayout(toolbar)
        toolbar_row.setContentsMargins(12, 6, 12, 6)
        heading = QLabel(strings.PAGE_TITLE_TRANSLATE, toolbar)
        heading.setObjectName("contextTitle")
        toolbar_row.addWidget(heading)
        toolbar_row.addStretch(1)
        toolbar_row.addWidget(direction_label)
        toolbar_row.addWidget(self.direction_combo)
        toolbar_row.addWidget(self.translate_button)
        toolbar_row.addWidget(self.cancel_button)

        def editor_surface(label: QLabel, editor: QPlainTextEdit) -> QFrame:
            surface = QFrame(self)
            surface.setObjectName("editorSurface")
            column = QVBoxLayout(surface)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(0)
            header = QFrame(surface)
            header.setObjectName("editorHeader")
            header_row = QHBoxLayout(header)
            header_row.setContentsMargins(14, 5, 14, 5)
            header_row.addWidget(label)
            header_row.addStretch(1)
            if editor is self.result_edit:
                header_row.addWidget(self.kind_badge)
                header_row.addWidget(self.miss_badge)
                header_row.addWidget(self.copy_result_button)
            column.addWidget(header)
            column.addWidget(editor, 1)
            return surface

        self.editor_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.editor_splitter.setChildrenCollapsible(False)
        self.editor_splitter.addWidget(editor_surface(input_label, self.input_edit))
        self.editor_splitter.addWidget(editor_surface(result_label, self.result_edit))
        self.editor_splitter.setStretchFactor(0, 1)
        self.editor_splitter.setStretchFactor(1, 1)

        self._request_id_row = QWidget(self)
        request_id_row = QHBoxLayout(self._request_id_row)
        request_id_row.setContentsMargins(0, 0, 0, 0)
        request_id_row.setSpacing(8)
        request_id_row.addWidget(request_id_row_label)
        request_id_row.addWidget(self.request_id_label, 1)
        request_id_row.addWidget(self.request_id_copy_button)
        self.diagnostics_button = QPushButton(strings.TRANSLATE_DIAGNOSTICS, self)
        self.diagnostics_button.setObjectName("linkButton")
        self.diagnostics_button.setCheckable(True)
        self.diagnostics_button.toggled.connect(self._request_id_row.setVisible)

        cost_note = QLabel(strings.TRANSLATE_COST_NOTE, self)
        cost_note.setObjectName("secondaryText")
        cost_note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(toolbar)
        layout.addWidget(self.progress_line)
        layout.addWidget(self.banner)
        layout.addWidget(self.field_error)
        layout.addWidget(self.editor_splitter, 1)
        layout.addWidget(cost_note)
        layout.addWidget(self.miss_note)
        layout.addWidget(self.cancel_note)
        layout.addWidget(self.diagnostics_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._request_id_row)
        layout.addWidget(self.status_label)

        # The first paint has to agree with the endpoint too, not only every
        # state reached afterwards.
        self._apply_endpoint_state()

        self._render_request_id()

    # -- public API --------------------------------------------------------

    @property
    def current_request_id(self) -> str | None:
        """The id currently on screen, or None when the row shows a placeholder."""
        return self._request_id

    def focus_input(self) -> None:
        """Where Ctrl+K puts the caret in this area."""
        self.input_edit.setFocus()

    def prefill(self, text: str) -> None:
        """Put `text` in the source box and hand it the keyboard.

        Used when the term lookup found nothing and the owner chose to send
        the same words to the model instead: retyping what was just typed is
        the kind of small tax that makes people stop using the second area at
        all. Nothing is sent - the owner still decides whether to spend a
        model call.
        """
        self.input_edit.setPlainText(text)
        self.input_edit.moveCursor(QTextCursor.MoveOperation.End)
        self.input_edit.setFocus()

    def reset_for_endpoint_change(self) -> None:
        """Drop everything that belonged to the previous server address.

        A reply from the OLD endpoint can land after the address has changed,
        and nothing on it says which server produced it - so it would render
        in a window whose header now names a different one. The in-flight
        request is cancelled through the same path the Cancel button uses,
        and the displayed result goes with it: a translation is an answer
        from a particular service, and keeping it on screen under a new
        address would attribute it to a service that never gave it.

        Advancing the generation is the other half of the same guarantee.
        Cancelling does not stop the coroutine from reaching a renderable
        outcome - the API client reports a cancellation as an ordinary error -
        so without this the cleared view would immediately be written over
        with the old request's ending.
        """
        self._cancel_in_flight()
        self._generation += 1
        self._task = None
        self._last_request = None
        self.result_edit.setPlainText("")
        self.status_label.clear()
        self._clear_outcome_surfaces()
        self._set_idle()

    # -- request lifecycle -------------------------------------------------

    def _selected_direction(self) -> str | None:
        value = self.direction_combo.currentData()
        if value == DIRECTION_AUTO:
            return None
        return value

    def _on_input_changed(self) -> None:
        if self.field_error.is_showing():
            self.field_error.clear()
            mark_field_invalid(self.input_edit, False)

    def _on_translate_clicked(self) -> None:
        text = self.input_edit.toPlainText()
        if not text.strip():
            # Refusing in silence reads as a broken button. The complaint
            # belongs on the box that is empty, not in a box above it.
            self.field_error.show_error(strings.FIELD_ERROR_EMPTY_INPUT)
            mark_field_invalid(self.input_edit, True)
            self.input_edit.setFocus()
            return
        self._start_translate(text, self._selected_direction())

    def _start_translate(self, text: str, to: str | None) -> None:
        self._generation += 1
        generation = self._generation
        self._last_request = (text, to)
        self._clear_outcome_surfaces()
        self._set_busy(True)
        self._task = asyncio.ensure_future(self._run_translate(text, to))
        # A task cancelled before its first step never runs its own body, so
        # the coroutine's own finally never executes and the buttons would
        # stay in the busy state for good. The callback runs either way.
        self._task.add_done_callback(
            lambda task, expected=generation: self._on_task_finished(task, expected)
        )

    def _on_task_finished(self, task: asyncio.Task, generation: int) -> None:
        if not self._is_current(generation):
            return
        if task.cancelled():
            self._show_error(ClientError(ERROR_CANCELLED))
        self._set_idle()
        self._task = None

    def _on_cancel_clicked(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        self._cancel_in_flight()
        self._enter_cancelling()

    def _cancel_in_flight(self) -> None:
        """The one place a running translation is stopped. Shared with the
        endpoint-change reset below, so both go through the same path."""
        if self._task is not None:
            self._task.cancel()

    def _retry_last_request(self) -> None:
        """Send again what the editor shows NOW, not what failed earlier.

        The retry button rides on a banner that survives editing, so it used
        to resubmit the saved text while the box beside it held something
        else - and the answer to the old text then rendered under the new
        source, attributed to input that never produced it. That is the same
        misattribution the endpoint-change reset exists to prevent, one layer
        in: an answer must never appear under text it did not come from.

        Reading the editor also makes the button mean one thing in both
        cases: translate what is on screen.
        """
        if self._task is not None and not self._task.done():
            return
        text = self.input_edit.toPlainText()
        if not text.strip():
            # Nothing to send, and the failure this banner reports no longer
            # has a subject. Take the banner down rather than leave an action
            # that would do nothing.
            self._clear_outcome_surfaces()
            return
        self._start_translate(text, self._selected_direction())

    async def _run_translate(self, text: str, to: str | None) -> None:
        # Captured here rather than passed in: the outcome of THIS coroutine
        # may only be drawn while the view still cares about it, and every
        # exit below - success, error, cancellation - checks against it.
        generation = self._generation
        self._last_request = (text, to)
        try:
            result = await self._api_client.translate(text, to=to)
        except ClientError as exc:
            if self._is_current(generation):
                self._show_error(exc)
        except asyncio.CancelledError:
            # Cancelling between awaits does not pass through the API
            # client's own handler, so the outcome is rendered here as well.
            # Without this the view would sit at "translating" for good.
            if self._is_current(generation):
                self._show_error(ClientError(ERROR_CANCELLED))
        else:
            if self._is_current(generation):
                self._show_result(result)
        finally:
            if self._is_current(generation):
                self._set_idle()
                self._task = None

    def _is_current(self, generation: int) -> bool:
        return generation == self._generation

    # -- button and progress states ----------------------------------------

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self._enter_running()
        else:
            self._set_idle()

    def _enter_running(self) -> None:
        self.translate_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_line.start()
        self.status_label.set_text(strings.STATUS_BAR_TRANSLATING)

    def _enter_cancelling(self) -> None:
        """Neither button does anything until the task actually ends.

        Leaving Cancel enabled invites a second press that cancels nothing,
        and re-enabling Translate here would let a new request start while
        the old one is still unwinding. The terminal wording is written by
        the done callback, not from here.
        """
        self.translate_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.status_label.set_text(strings.STATUS_CANCELLING)

    def _set_idle(self) -> None:
        """Return the buttons to their resting state WITHOUT touching the
        status line: every outcome has just written its own text there, and
        clearing it here is how a rendered error or request id disappears
        before it can be read."""
        # Resting state is not always "enabled": an unconfigured client has
        # nowhere to send this. Enabling unconditionally here is what made
        # the button clickable on the setup screen even after the endpoint
        # state had disabled it.
        self._apply_endpoint_state()
        self.cancel_button.setEnabled(False)
        self.progress_line.stop()

    def _apply_endpoint_state(self) -> None:
        """Submit is disabled while there is no address to submit to.

        The lookup and status areas both did this; this one did not, so on
        the unconfigured setup screen - where the whole point is that no
        request will be made - the translate button stayed live and a press
        started a task whose only possible outcome was `not_configured`. The
        transport refuses it anyway, so this is not the safety boundary; it
        is the difference between a screen that says what it will do and one
        that has to be tried to find out.
        """
        configured = self._api_client.is_configured
        self.translate_button.setEnabled(configured)
        self.translate_button.setToolTip(
            "" if configured else strings.TOOLTIP_NEEDS_ENDPOINT
        )

    # -- rendering ---------------------------------------------------------

    def _clear_outcome_surfaces(self) -> None:
        """Take down everything the PREVIOUS outcome put on screen.

        Called before a request starts and when the address changes. The
        source text is deliberately not part of this: it is the owner's own
        input, and a failed request is exactly when they need it kept.
        """
        self.banner.clear()
        self.field_error.clear()
        mark_field_invalid(self.input_edit, False)
        self.kind_badge.setVisible(False)
        self.miss_badge.setVisible(False)
        self.miss_note.clear()
        self.miss_note.setVisible(False)
        self.cancel_note.clear()
        self.cancel_note.setVisible(False)
        self._render_request_id()
        # The previous translation goes with its badge and its id. Leaving it
        # was worse than leaving nothing: the text stayed while everything
        # that said WHERE it came from and WHICH request produced it was
        # taken down, so a stale answer sat under new source text wearing no
        # provenance at all - which is exactly how it gets read as the answer
        # to the input now on screen.
        self.result_edit.setPlainText("")

    def _render_request_id(self, request_id: str | None = NO_REQUEST) -> None:
        """Report an id for request outcomes, and draw nothing before one.

        ``NO_REQUEST`` means no call happened, while explicit ``None`` means a
        call completed without an id. The latter keeps the stable row and its
        placeholder; the former must not invite an operator query about a
        request this client never made.
        """
        applicable = request_id is not NO_REQUEST
        self._request_id = request_id if applicable else None
        self.diagnostics_button.setVisible(applicable)
        self._request_id_row.setVisible(applicable and self.diagnostics_button.isChecked())
        if not applicable:
            return
        shown = request_id if request_id else strings.REQUEST_ID_PLACEHOLDER
        self.request_id_label.set_full_text(shown)
        self.request_id_copy_button.setEnabled(request_id is not None)

    def _on_copy_result_clicked(self) -> None:
        value = self.result_edit.toPlainText()
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None and value:
            clipboard.setText(value)
            self.status_label.set_text(strings.STATUS_COPIED)

    def _on_copy_request_id_clicked(self) -> None:
        if not self._request_id:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._request_id)
            self.status_label.set_text(strings.STATUS_COPIED)

    def _show_result(self, result: TranslationResult) -> None:
        self.banner.clear()
        self.field_error.clear()
        mark_field_invalid(self.input_edit, False)
        self.cancel_note.clear()
        self.cancel_note.setVisible(False)

        self.result_edit.setPlainText(result.text)

        # An unknown kind keeps the service's own word for it; only the shape
        # degrades. Inventing a translation for a kind this release has never
        # seen would be the client speaking for the service.
        self.kind_badge.set_kind(result.kind, _KIND_LABELS.get(result.kind, result.kind))
        self.kind_badge.setVisible(True)

        self.miss_badge.set_kind(_MISS_BADGE_KIND, strings.DICTIONARY_MISS_BADGE)
        self.miss_badge.setVisible(result.dictionary_miss)
        self.miss_note.setText(strings.DICTIONARY_MISS_NOTE)
        self.miss_note.setVisible(result.dictionary_miss)

        self._render_request_id(result.request_id)
        self.status_label.set_text(strings.STATUS_BAR_DONE)

    def _show_error(self, exc: ClientError) -> None:
        presentation = presentation_for(exc.code)
        self.result_edit.setPlainText("")
        self.kind_badge.setVisible(False)
        self.miss_badge.setVisible(False)
        self.miss_note.clear()
        self.miss_note.setVisible(False)
        # The id belongs to the failed request just as much as to a
        # successful one, and it is the failed ones an operator gets asked
        # about. Same widget, same place, id or placeholder.
        self._render_request_id(exc.request_id)

        if presentation.surface == SURFACE_STATUS:
            # The owner asked for this. It is a confirmation, so it goes on
            # the quiet line and never into a coloured box.
            self.banner.clear()
            self.field_error.clear()
            mark_field_invalid(self.input_edit, False)
            self.status_label.set_text(presentation.message)
            self.cancel_note.setText(strings.STATUS_CANCELLED_NOTE)
            self.cancel_note.setVisible(True)
            return

        self.cancel_note.clear()
        self.cancel_note.setVisible(False)

        if presentation.surface == SURFACE_FIELD:
            # The fault is in the text the owner is looking at, so the
            # message goes against that text and the keyboard goes with it.
            self.banner.clear()
            self.field_error.show_error(presentation.message)
            mark_field_invalid(self.input_edit, True)
            self.input_edit.setFocus()
            self.status_label.set_text(strings.STATUS_BAR_LAST_REQUEST_FAILED)
            return

        # Everything else is drawn in this area's banner - including the two
        # window-wide codes, whose permanent home is the global banner above
        # the areas. They are shown here as well rather than dropped: this
        # view raised the request, and an area that answers a press with
        # nothing at all is worse than one that repeats a warning.
        self.field_error.clear()
        mark_field_invalid(self.input_edit, False)
        # Deliberately WITHOUT the request id: this view renders it a few
        # lines up, in the row that is the same widget in the same place for
        # a success and a failure. Passing it here as well put the id, and a
        # second copy button, on screen twice for one failed request - which
        # invites the reader to wonder whether they are two different ids.
        self.banner.show_message(
            presentation.message,
            presentation.severity,
            actions=self._banner_actions(presentation),
        )
        self.status_label.set_text(strings.STATUS_BAR_LAST_REQUEST_FAILED)

    def _banner_actions(self, presentation: ErrorPresentation) -> tuple:
        """The one offer a banner makes, or nothing at all.

        An action whose handler was never wired in is left out rather than
        drawn: a button that does nothing when pressed is a worse answer than
        no button, because it costs a press to find out.
        """
        label = presentation.action_label
        if presentation.action is None or label is None:
            return ()
        if presentation.action == ACTION_RETRY:
            return ((label, self._retry_last_request),)
        if presentation.action == ACTION_OPEN_SETTINGS:
            if self._on_open_settings is None:
                return ()
            return ((label, self._on_open_settings),)
        if presentation.action == ACTION_ENTER_TOKEN:
            if self._on_enter_token is None:
                return ()
            return ((label, self._on_enter_token),)
        return ()
