"""Local manuscript workbench backed by the shared review API.

A QTextDocument normalises several characters. LosslessEditor retains the
original text and maps each real edit through the document's UTF-16 offsets;
opening and saving without editing therefore does not rewrite a manuscript.
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QTextEdit, QVBoxLayout, QWidget,
)

from .. import strings
from ..errors import ClientError
from ..manuscript import (
    WorkfileError, choice_conflict_groups, empty_workfile, load_workfile,
    save_workfile, serialize_workfile,
)
from .icons import icon


def _display_map(raw: str) -> tuple[str, list[int]]:
    """Qt plain text and the corresponding raw offset at each boundary."""
    out: list[str] = []
    bounds = [0]
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\r":
            i += 2 if i + 1 < len(raw) and raw[i + 1] == "\n" else 1
            out.append("\n")
        else:
            i += 1
            out.append(" " if c == "\xa0" else "\n" if c in "\u2028\u2029" else c)
        bounds.append(i)
    return "".join(out), bounds


def _utf16_position(text: str, index: int) -> int:
    return len(text[:index].encode("utf-16-le")) // 2


def _scalar_boundary(text: str, position: int) -> int | None:
    if position < 0:
        return None
    units = 0
    for index, char in enumerate(text):
        if units == position:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
        if units > position:
            return None
    return len(text) if units == position else None


class LosslessEditor(QPlainTextEdit):
    """Use document edits to preserve characters Qt cannot display distinctly."""

    rawChanged = Signal()
    editRejected = Signal()
    undoRequested = Signal()
    redoRequested = Signal()
    compositionChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._raw = ""
        self._shown = ""
        self._bounds = [0]
        self._setting = False
        self._pending: str | None = None
        self._pending_range: tuple[int, int] | None = None
        self._ime_active = False
        self._ime_origin: tuple[int, int] | None = None
        self._ignore_ime_changes = False
        # Qt's undo buffer contains normalised text, so using it could turn an
        # original CRLF or non-breaking space into a different character.
        self.setUndoRedoEnabled(False)
        self.document().contentsChange.connect(self._content_changed)

    def raw_text(self) -> str:
        return self._raw

    def can_display(self, raw: str) -> bool:
        shown, _ = _display_map(raw)
        probe = QPlainTextEdit()
        probe.setPlainText(raw)
        return probe.toPlainText() == shown

    def set_raw_text(self, raw: str) -> None:
        shown, bounds = _display_map(raw)
        self._setting = True
        try:
            super().setPlainText(raw)
            if self.toPlainText() != shown:
                raise ValueError("text cannot be represented by Qt")
            self._raw, self._shown, self._bounds = raw, shown, bounds
        finally:
            self._setting = False

    def _remember_cursor(self) -> None:
        cursor = self.textCursor()
        self._pending_range = (cursor.selectionStart(), cursor.selectionEnd())

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        self._pending = source.text() if source.hasText() else None
        self._remember_cursor()
        try:
            super().insertFromMimeData(source)
        finally:
            self._pending = None
            self._pending_range = None

    def createMimeDataFromSelection(self) -> QMimeData:  # noqa: N802
        cursor = self.textCursor()
        start = _scalar_boundary(self._shown, cursor.selectionStart())
        end = _scalar_boundary(self._shown, cursor.selectionEnd())
        if start is None or end is None:
            return super().createMimeDataFromSelection()
        data = QMimeData()
        data.setText(self._raw[self._bounds[start]:self._bounds[end]])
        return data

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._ime_active and (event.matches(QKeySequence.StandardKey.Undo) or event.matches(QKeySequence.StandardKey.Redo)):
            super().keyPressEvent(event)
            return
        if (event.key() == Qt.Key.Key_Z and
                event.modifiers() == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)):
            self.redoRequested.emit()
            return
        if event.matches(QKeySequence.StandardKey.Undo):
            self.undoRequested.emit()
            return
        if event.matches(QKeySequence.StandardKey.Redo):
            self.redoRequested.emit()
            return
        self._pending = event.text() or None
        self._remember_cursor()
        try:
            super().keyPressEvent(event)
        finally:
            self._pending = None
            self._pending_range = None

    def inputMethodEvent(self, event) -> None:  # noqa: N802
        committed = bool(event.commitString()) or bool(event.replacementLength())
        preedit = event.preeditString()
        if not committed:
            if preedit and not self._ime_active:
                self._remember_cursor()
                self._ime_origin = self._pending_range
                self._ime_active = True
                self.compositionChanged.emit()
            self._ignore_ime_changes = True
            try:
                super().inputMethodEvent(event)
            finally:
                self._ignore_ime_changes = False
                self._pending_range = None
            if not preedit and self._ime_active:
                # Cancelling a preedit after a selection can leave Qt's
                # provisional deletion in the document. Restore the raw
                # draft only after the composition has ended.
                position = self.textCursor().position()
                if self.toPlainText() != self._shown:
                    self.set_raw_text(self._raw)
                    cursor = self.textCursor()
                    cursor.setPosition(min(position, self.document().characterCount() - 1))
                    self.setTextCursor(cursor)
                self._ime_active, self._ime_origin = False, None
                self.compositionChanged.emit()
            return
        self._pending = event.commitString()
        self._remember_cursor()
        if self._ime_active and self._ime_origin is not None:
            start, end = self._ime_origin
            if event.replacementStart() or event.replacementLength():
                relative = self.textCursor().position() + event.replacementStart()
                relative_end = relative + event.replacementLength()
                removed = end - start
                start = relative if relative <= start else relative + removed
                end = relative_end + removed if relative_end >= self._ime_origin[0] else relative_end
            self._pending_range = (start, end)
        elif event.replacementStart() or event.replacementLength():
            start = self.textCursor().position() + event.replacementStart()
            self._pending_range = (start, start + event.replacementLength())
        try:
            super().inputMethodEvent(event)
        finally:
            self._pending = None
            self._pending_range = None
        if preedit:
            self._remember_cursor()
            self._ime_origin = self._pending_range
            self._pending_range = None
            self._ime_active = True
        else:
            self._ime_active, self._ime_origin = False, None
            self.compositionChanged.emit()

    def _content_changed(self, position: int, removed: int, added: int) -> None:
        if self._setting or self._ignore_ime_changes:
            return
        old, new = self._shown, self.toPlainText()
        # Qt reports IME preedit as a whole-document contents change even
        # though committed text is unchanged. Resetting the document here
        # destroys the active composition and moves its caret.
        if old == new and self._pending is None:
            return
        if self._pending is not None and self._pending_range is not None:
            left = _scalar_boundary(old, self._pending_range[0])
            right = _scalar_boundary(old, self._pending_range[1])
            inserted_shown, _ = _display_map(self._pending)
            if left is not None and right is not None and old[:left] + inserted_shown + old[right:] == new:
                raw = self._raw[:self._bounds[left]] + self._pending + self._raw[self._bounds[right]:]
                shown, bounds = _display_map(raw)
                self._raw, self._shown, self._bounds = raw, shown, bounds
                self.rawChanged.emit()
                return
        start = _scalar_boundary(old, position)
        end = _scalar_boundary(old, position + removed)
        new_start = _scalar_boundary(new, position)
        new_end = _scalar_boundary(new, position + added)
        valid = (
            start is not None and end is not None and new_start is not None
            and new_end is not None and old[:start] == new[:new_start]
            and old[end:] == new[new_end:]
        )
        if valid:
            removed_part, inserted = old[start:end], new[new_start:new_end]
            prefix = 0
            while prefix < min(len(removed_part), len(inserted)) and removed_part[prefix] == inserted[prefix]:
                prefix += 1
            suffix = 0
            while (suffix < len(removed_part) - prefix and suffix < len(inserted) - prefix
                   and removed_part[-1 - suffix] == inserted[-1 - suffix]):
                suffix += 1
            left, right = start + prefix, end - suffix
            inserted = inserted[prefix:len(inserted) - suffix if suffix else None]
            raw = self._raw[:self._bounds[left]] + inserted + self._raw[self._bounds[right]:]
            shown, bounds = _display_map(raw)
            if shown == new:
                self._raw, self._shown, self._bounds = raw, shown, bounds
                self.rawChanged.emit()
                return
        # Never guess which of two visually identical characters was edited.
        raw = self._raw
        self.set_raw_text(raw)
        self.editRejected.emit()

    def select_raw_span(self, span: dict | None, *, move_cursor: bool = True) -> bool:
        self.setExtraSelections([])
        if not isinstance(span, dict):
            return False
        start, end, value = span.get("start"), span.get("end"), span.get("text")
        if (isinstance(start, bool) or not isinstance(start, int) or
                isinstance(end, bool) or not isinstance(end, int) or
                not isinstance(value, str) or start < 0 or end <= start or
                end > len(self._raw) or self._raw[start:end] != value):
            return False
        try:
            left, right = self._bounds.index(start), self._bounds.index(end)
        except ValueError:
            return False
        cursor = self.textCursor()
        cursor.setPosition(_utf16_position(self._shown, left))
        cursor.setPosition(_utf16_position(self._shown, right), QTextCursor.MoveMode.KeepAnchor)
        if move_cursor:
            self.setTextCursor(cursor)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format.setBackground(self.palette().color(QPalette.ColorRole.Highlight))
        selection.format.setForeground(self.palette().color(QPalette.ColorRole.HighlightedText))
        self.setExtraSelections([selection])
        return True


class _ElidedDocumentLabel(QLabel):
    """Keep long filenames inside the command bar and available to readers."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self.setAccessibleName(text)
        self._update_elision()

    def _update_elision(self) -> None:
        self.setText(self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideMiddle, max(0, self.contentsRect().width())
        ))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_elision()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, "_full_text"):
            self._update_elision()


class ManuscriptView(QWidget):
    documentChanged = Signal()
    lookup_requested = Signal(str)

    def __init__(self, api_client, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._api_client = api_client
        self._payload = empty_workfile()
        self._saved_payload = copy.deepcopy(self._payload)
        self._path: Path | None = None
        self._report: dict | None = None
        self._task: asyncio.Task | None = None
        self._generation = 0
        self._updating = False
        # Document history excludes report snapshots, saved baseline and path:
        # an edit undo must never erase a report added later or undo a save.
        self._undo_history: list[dict] = []
        self._redo_history: list[dict] = []
        self._review_state = "none"
        self._review_detail = ""
        self._request_id: str | None = None
        self._conflict_mode = False
        self._conflict_indexes: list[int] = []
        self._document_state: tuple[Path | None, bool, int] | None = None
        self._document_revision = 0
        self._compact = False
        self._inspector_requested = True

        self.source_edit = LosslessEditor(self)
        self.target_edit = LosslessEditor(self)
        self.source_edit.setPlaceholderText(strings.MANUSCRIPT_SOURCE_PLACEHOLDER)
        self.target_edit.setPlaceholderText(strings.MANUSCRIPT_TARGET_PLACEHOLDER)
        for editor in (self.source_edit, self.target_edit):
            editor.setMinimumHeight(36)
            editor.setMinimumWidth(80)
            editor.rawChanged.connect(self._on_edit)
            editor.undoRequested.connect(self._undo_replace)
            editor.redoRequested.connect(self._redo_edit)
            editor.compositionChanged.connect(self._on_composition_changed)
            editor.editRejected.connect(lambda: self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED))
        self.direction_combo = QComboBox(self)
        self.direction_combo.addItem(strings.DIRECTION_TO_EN, "en")
        self.direction_combo.addItem(strings.DIRECTION_TO_ZH, "zh")
        self.direction_combo.currentIndexChanged.connect(self._on_edit)
        self.review_button = QPushButton(strings.MANUSCRIPT_REVIEW, self)
        self.review_button.setObjectName("primaryButton")
        self.review_button.clicked.connect(self._start_review)
        self.cancel_button = QPushButton(strings.CANCEL_BUTTON, self)
        self.cancel_button.clicked.connect(self._cancel_review)
        self.auto_align_button = QPushButton(strings.MANUSCRIPT_AUTO_ALIGN, self)
        self.auto_align_button.clicked.connect(self._use_auto_alignment)
        self.findings = QListWidget(self)
        self.findings.setMinimumHeight(76)
        self.findings.setMaximumHeight(150)
        self.findings.setWordWrap(True)
        self.findings.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.findings.setAccessibleName(strings.MANUSCRIPT_INSPECTOR_TITLE)
        self.findings.currentRowChanged.connect(self._select_finding)
        self.replacement_edit = QLineEdit(self)
        self.replacement_edit.setPlaceholderText(strings.MANUSCRIPT_REPLACEMENT_HINT)
        self.replacement_edit.textChanged.connect(self._update_controls)
        self.replace_button = QPushButton(strings.MANUSCRIPT_REPLACE, self)
        self.replace_button.clicked.connect(self._replace_one)
        self.undo_button = QPushButton(strings.MANUSCRIPT_UNDO, self)
        self.undo_button.clicked.connect(self._undo_replace)
        self.redo_button = QPushButton(strings.MANUSCRIPT_REDO, self)
        self.redo_button.clicked.connect(self._redo_edit)
        self.new_button = QPushButton(strings.MANUSCRIPT_NEW, self)
        self.new_button.clicked.connect(self.new_document)
        self.open_button = QPushButton(strings.MANUSCRIPT_OPEN, self)
        self.open_button.clicked.connect(self.open_document)
        self.save_button = QPushButton(strings.MANUSCRIPT_SAVE, self)
        # Keep IME preedit in the editor on mouse clicks while retaining Tab navigation.
        self.save_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.save_button.clicked.connect(self.save_document)
        self.save_as_button = QPushButton(strings.MANUSCRIPT_SAVE_AS, self)
        self.save_as_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.save_as_button.clicked.connect(lambda: self.save_document(save_as=True))
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        self.status_label.setText(strings.MANUSCRIPT_REPORT_NOTE)
        self.save_label = QLabel(self)
        self.report_label = QLabel(self)
        self.report_label.setWordWrap(True)
        self.report_label.setText(strings.MANUSCRIPT_NO_REPORT)
        self.request_id_label = QLabel(strings.REQUEST_ID_LABEL.format(request_id=strings.REQUEST_ID_PLACEHOLDER), self)
        self.history_label = QLabel(self)
        self.history_label.setWordWrap(True)
        self.conflict_label = QLabel(strings.MANUSCRIPT_CHOICES_CONFLICT, self)
        self.conflict_label.setWordWrap(True)
        self.conflict_list = QListWidget(self)
        self.conflict_list.setMaximumHeight(110)
        self.conflict_list.currentRowChanged.connect(self._update_controls)
        self.remove_choice_button = QPushButton(strings.MANUSCRIPT_REMOVE_CHOICE, self)
        self.remove_choice_button.clicked.connect(self._remove_conflicting_choice)
        for widget in (self.conflict_label, self.conflict_list, self.remove_choice_button):
            widget.setVisible(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # These compact, named buttons are actual controls (rather than an
        # icon-only decoration), and keep the save buttons in the tab order.
        self.toolbar = QFrame(self)
        self.toolbar.setObjectName("workbenchToolbar")
        toolbar = QHBoxLayout(self.toolbar)
        toolbar.setContentsMargins(10, 5, 10, 5)
        toolbar.setSpacing(2)
        self.document_icon = QLabel(self.toolbar)
        self.document_icon.setFixedWidth(24)
        self._refresh_document_icon()
        toolbar.addWidget(self.document_icon)
        self.document_label = _ElidedDocumentLabel(self.toolbar)
        self.document_label.setObjectName("contextTitle")
        self.document_label.setMinimumWidth(44)
        self.document_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(self.document_label, 1)
        self.save_label.setObjectName("secondaryText")
        toolbar.addWidget(self.save_label)
        for button, name, description in (
            (self.new_button, "file-plus", strings.MANUSCRIPT_NEW),
            (self.open_button, "folder-open", strings.MANUSCRIPT_OPEN),
            (self.save_button, "save", strings.MANUSCRIPT_SAVE),
            (self.save_as_button, "save", strings.MANUSCRIPT_SAVE_AS),
            (self.undo_button, "undo-2", strings.MANUSCRIPT_UNDO),
            (self.redo_button, "redo-2", strings.MANUSCRIPT_REDO),
        ):
            button.setObjectName("commandButton")
            button.setText("")
            button.setIcon(icon(name))
            button.setIconSize(QSize(18, 18))
            button.setFixedWidth(34)
            button.setToolTip(description)
            button.setAccessibleName(description)
            toolbar.addWidget(button)
        self.direction_combo.setAccessibleName(strings.DIRECTION_LABEL)
        toolbar.addWidget(self.direction_combo)
        toolbar.addWidget(self.review_button)
        toolbar.addWidget(self.cancel_button)
        self.inspector_button = QPushButton(self.toolbar)
        self.inspector_button.setObjectName("commandButton")
        self.inspector_button.setFixedWidth(34)
        self.inspector_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.inspector_button.clicked.connect(self._toggle_inspector)
        toolbar.addWidget(self.inspector_button)
        outer.addWidget(self.toolbar)

        self.editor_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.editor_splitter.setChildrenCollapsible(False)
        self.editor_splitter.setHandleWidth(8)
        for text, editor in ((strings.INPUT_LABEL, self.source_edit), (strings.RESULT_LABEL, self.target_edit)):
            surface = QFrame(self.editor_splitter)
            surface.setObjectName("editorSurface")
            column = QVBoxLayout(surface)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(0)
            header = QFrame(surface)
            header.setObjectName("editorHeader")
            header_layout = QHBoxLayout(header)
            header_layout.setContentsMargins(14, 4, 14, 4)
            label = QLabel(text, header)
            label.setBuddy(editor)
            if editor is self.source_edit:
                self.source_header_label = label
            else:
                self.target_header_label = label
            header_layout.addWidget(label)
            header_layout.addStretch(1)
            column.addWidget(header)
            column.addWidget(editor, 1)
            self.editor_splitter.addWidget(surface)
        self.editor_splitter.setStretchFactor(0, 1)
        self.editor_splitter.setStretchFactor(1, 1)

        self.inspector = QFrame(self)
        self.inspector.setObjectName("reviewInspector")
        self.inspector.setMinimumWidth(260)
        self.inspector.setMaximumWidth(330)
        inspector_layout = QVBoxLayout(self.inspector)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.setSpacing(0)
        inspector_header = QFrame(self.inspector)
        inspector_header.setObjectName("editorHeader")
        inspector_heading = QHBoxLayout(inspector_header)
        inspector_heading.setContentsMargins(14, 4, 8, 4)
        heading = QLabel(strings.MANUSCRIPT_INSPECTOR_TITLE, inspector_header)
        heading.setObjectName("contextTitle")
        self.finding_count = QLabel(inspector_header)
        inspector_heading.addWidget(heading, 1)
        inspector_heading.addWidget(self.finding_count)
        inspector_layout.addWidget(inspector_header)
        inspector_scroll = QScrollArea(self.inspector)
        inspector_scroll.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        inspector_scroll.setWidgetResizable(True)
        inspector_body = QWidget(inspector_scroll)
        inspector_body.setObjectName("inspectorBody")
        details = QVBoxLayout(inspector_body)
        details.setContentsMargins(18, 18, 18, 18)
        details.setSpacing(12)
        summary = QHBoxLayout()
        pair = QVBoxLayout()
        self.finding_title = QLabel(strings.MANUSCRIPT_FINDING_PROMPT, inspector_body)
        self.finding_title.setObjectName("findingTitle")
        self.finding_title.setWordWrap(True)
        pair.addWidget(self.finding_title)
        self.finding_pair = QLabel(inspector_body)
        self.finding_pair.setObjectName("findingPair")
        self.finding_pair.setTextFormat(Qt.TextFormat.PlainText)
        self.finding_pair.setWordWrap(True)
        pair.addWidget(self.finding_pair)
        summary.addLayout(pair, 1)
        self.finding_verdict = QLabel(inspector_body)
        self.finding_verdict.setObjectName("findingVerdict")
        self.finding_verdict.setWordWrap(True)
        summary.addWidget(self.finding_verdict, 0, Qt.AlignmentFlag.AlignTop)
        details.addLayout(summary)
        self.finding_rule = QLabel(inspector_body)
        self.finding_rule.setObjectName("secondaryText")
        self.finding_rule.setWordWrap(True)
        self.finding_positions = QLabel(inspector_body)
        self.finding_positions.setObjectName("secondaryText")
        self.finding_positions.setWordWrap(True)
        self.candidates_label = QLabel(inspector_body)
        self.candidates_label.setWordWrap(True)
        details.addWidget(self.candidates_label)
        self.lookup_button = QPushButton(strings.NAV_ITEM_TERMS, inspector_body)
        self.lookup_button.setIcon(icon("external-link"))
        self.lookup_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.lookup_button.clicked.connect(self._request_lookup)
        details.addWidget(self.lookup_button)
        self.manual_edit_label = QLabel(strings.MANUSCRIPT_MANUAL_EDIT, inspector_body)
        self.manual_edit_label.setWordWrap(True)
        details.addWidget(self.manual_edit_label)
        self.replace_toggle = QPushButton(strings.MANUSCRIPT_REPLACE, inspector_body)
        self.replace_toggle.setCheckable(True)
        self.replace_toggle.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        details.addWidget(self.replace_toggle)
        self.replacement_area = QWidget(inspector_body)
        self.replacement_area.setObjectName("inspectorBody")
        replacement = QVBoxLayout(self.replacement_area)
        replacement.setContentsMargins(0, 0, 0, 0)
        replacement_label = QLabel(strings.MANUSCRIPT_REPLACEMENT, self.replacement_area)
        replacement_label.setBuddy(self.replacement_edit)
        replacement.addWidget(replacement_label)
        replacement.addWidget(self.replacement_edit)
        replacement.addWidget(self.replace_button)
        self.replace_toggle.toggled.connect(self.replacement_area.setVisible)
        details.addWidget(self.replacement_area)
        details.addWidget(self.findings)
        details.addWidget(self.conflict_label)
        details.addWidget(self.conflict_list)
        details.addWidget(self.remove_choice_button)
        self.details_button = QPushButton(strings.MANUSCRIPT_DETAILS, inspector_body)
        self.details_button.setObjectName("commandButton")
        self.details_button.setCheckable(True)
        details.addWidget(self.details_button)
        self.more_details = QWidget(inspector_body)
        self.more_details.setObjectName("inspectorBody")
        more = QVBoxLayout(self.more_details)
        more.setContentsMargins(0, 0, 0, 0)
        more.addWidget(self.finding_rule)
        more.addWidget(self.finding_positions)
        more.addWidget(self.history_label)
        more.addWidget(self.auto_align_button)
        self.request_id_label.setObjectName("secondaryText")
        self.request_id_label.setWordWrap(True)
        more.addWidget(self.request_id_label)
        self.more_details.hide()
        self.details_button.toggled.connect(self.more_details.setVisible)
        details.addWidget(self.more_details)
        details.addStretch(1)
        inspector_scroll.setWidget(inspector_body)
        inspector_layout.addWidget(inspector_scroll, 1)

        self.work_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.work_splitter.setChildrenCollapsible(False)
        self.work_splitter.setHandleWidth(8)
        self.work_splitter.addWidget(self.editor_splitter)
        self.work_splitter.addWidget(self.inspector)
        self.work_splitter.setStretchFactor(0, 1)
        self.work_splitter.setStretchFactor(1, 0)
        self.work_splitter.setSizes([1000, 300])
        workspace = QWidget(self)
        workspace_layout = QHBoxLayout(workspace)
        workspace_layout.setContentsMargins(12, 10, 12, 10)
        workspace_layout.addWidget(self.work_splitter)
        outer.addWidget(workspace, 1)

        footer = QFrame(self)
        footer.setObjectName("workbenchToolbar")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 4, 12, 4)
        self.report_label.setObjectName("secondaryText")
        self.status_label.setObjectName("secondaryText")
        self.limit_label = QLabel(strings.MANUSCRIPT_REVIEW_LIMIT, footer)
        self.limit_label.setObjectName("secondaryText")
        self.limit_label.setWordWrap(True)
        footer_layout.addWidget(self.report_label, 1)
        footer_layout.addWidget(self.limit_label, 1)
        footer_layout.addWidget(self.status_label, 1)
        outer.addWidget(footer)
        self._render_finding(-1)
        self._update_inspector_button()
        self._update_controls()

    def focus_input(self) -> None:
        self.source_edit.setFocus()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange) and hasattr(self, "document_icon"):
            self._refresh_document_icon()

    def _refresh_document_icon(self) -> None:
        self.document_icon.setPixmap(icon("file-text").pixmap(18, 18))

    @property
    def document_title(self) -> str:
        return self._path.name if self._path is not None else strings.MANUSCRIPT_UNTITLED

    @property
    def document_dirty(self) -> bool:
        return self._payload != self._saved_payload

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        compact = event.size().width() < 900
        # Short workspaces need the available height for both editing areas.
        # On a narrow but tall workspace, stacking still offers wider lines.
        stacked = compact and event.size().height() >= 560
        orientation = Qt.Orientation.Vertical if stacked else Qt.Orientation.Horizontal
        if self.editor_splitter.orientation() != orientation:
            self.editor_splitter.setOrientation(orientation)
        if compact != self._compact:
            self._compact = compact
            # On a small workspace make room for the manuscript. The action
            # remains available so the inspector can be opened deliberately.
            self.inspector.setVisible(False if compact else self._inspector_requested)
            self.save_label.setVisible(not compact)
            self._update_inspector_button()

    def _toggle_inspector(self) -> None:
        self._inspector_requested = self.inspector.isHidden()
        self.inspector.setVisible(self._inspector_requested)
        if self._inspector_requested:
            self.work_splitter.setSizes([max(1, self.width() - 300), 300])
        self._update_inspector_button()

    def _update_inspector_button(self) -> None:
        showing = not self.inspector.isHidden()
        description = strings.MANUSCRIPT_INSPECTOR_HIDE if showing else strings.MANUSCRIPT_INSPECTOR_SHOW
        self.inspector_button.setIcon(icon("panel-right-close" if showing else "panel-right-open"))
        self.inspector_button.setIconSize(QSize(18, 18))
        self.inspector_button.setToolTip(description)
        self.inspector_button.setAccessibleName(description)

    def _target_span_reliable(self, span: object) -> bool:
        if not isinstance(span, dict):
            return False
        start, end, value = span.get("start"), span.get("end"), span.get("text")
        raw = self.target_edit.raw_text()
        return (type(start) is int and type(end) is int and isinstance(value, str)
                and 0 <= start < end <= len(raw) and raw[start:end] == value
                and start in self.target_edit._bounds and end in self.target_edit._bounds)

    def _render_finding(self, index: int) -> None:
        report = self._report
        finding = report["findings"][index] if report is not None and 0 <= index < len(report["findings"]) else None
        total = len(report["findings"]) if report is not None else 0
        self.finding_count.setText(strings.MANUSCRIPT_FINDING_COUNT.format(
            current=index + 1 if finding is not None else 0, total=total))
        self.findings.setVisible(total > 0)
        if finding is None:
            self.finding_title.setText(
                strings.MANUSCRIPT_FINDING_PROMPT if total else
                strings.MANUSCRIPT_NO_FINDINGS if report is not None else self.report_label.text()
            )
            for label in (self.finding_verdict, self.finding_pair, self.finding_rule, self.finding_positions, self.candidates_label):
                label.clear()
            self.finding_verdict.hide()
            self.lookup_button.hide()
            self.manual_edit_label.hide()
            self.replace_toggle.hide()
            self.replace_toggle.setChecked(False)
            self.replacement_area.hide()
            return
        source = finding.get("source_span") or {}
        target = finding.get("target_span")
        self.finding_verdict.show()
        self.finding_title.setTextFormat(Qt.TextFormat.PlainText)
        self.finding_title.setText(source.get("text", ""))
        verdicts = {"verified_constraint": strings.MANUSCRIPT_VERDICT_VERIFIED,
                    "confirmed_conflict": strings.MANUSCRIPT_VERDICT_CONFLICT,
                    "needs_review": strings.MANUSCRIPT_VERDICT_NEEDS_REVIEW,
                    "not_evaluated": strings.MANUSCRIPT_VERDICT_NOT_EVALUATED}
        self.finding_verdict.setText(verdicts.get(finding["verdict"], finding["verdict"]))
        self.finding_verdict.setProperty("verdict", finding["verdict"])
        self.finding_verdict.style().unpolish(self.finding_verdict)
        self.finding_verdict.style().polish(self.finding_verdict)
        self.finding_pair.setText(target["text"] if isinstance(target, dict) and isinstance(target.get("text"), str) else "")
        rule = strings.MANUSCRIPT_RULE_TERM_PAIR if finding["rule_id"] == "review.term_pair" else finding["rule_id"]
        self.finding_rule.setText(strings.MANUSCRIPT_RULE.format(rule=rule))
        positions = []
        for side, span in ((strings.INPUT_LABEL, source), (strings.RESULT_LABEL, target)):
            if isinstance(span, dict) and type(span.get("start")) is int and type(span.get("end")) is int:
                positions.append(strings.MANUSCRIPT_POSITION.format(side=side, start=span["start"] + 1, end=span["end"]))
        self.finding_positions.setText("\n".join(positions))
        categories = {"core_term": strings.CATEGORY_LABEL_CORE_TERM,
                      "resonator": strings.CATEGORY_LABEL_RESONATOR,
                      "weapon": strings.CATEGORY_LABEL_WEAPON,
                      "echo": strings.CATEGORY_LABEL_ECHO,
                      "skill": strings.CATEGORY_LABEL_SKILL,
                      "sonata_effect": strings.CATEGORY_LABEL_SONATA_EFFECT,
                      "location": strings.CATEGORY_LABEL_LOCATION,
                      "item": strings.CATEGORY_LABEL_ITEM,
                      "speaker": strings.CATEGORY_LABEL_SPEAKER}
        pairs = [f'{item["zh"]} ↔ {item["en"]} · {categories.get(item["category"], item["category"])}'
                 for item in finding["candidates"]]
        self.candidates_label.setTextFormat(Qt.TextFormat.PlainText)
        self.candidates_label.setText("\n".join([strings.MANUSCRIPT_CANDIDATES, *pairs]))
        self.lookup_button.setVisible(bool(source.get("text")))
        reliable = self._target_span_reliable(target)
        self.manual_edit_label.setVisible(not reliable)
        self.replace_toggle.setChecked(False)
        self.replace_toggle.setVisible(reliable and finding["verdict"] != "verified_constraint")
        self.replacement_area.hide()

    def _request_lookup(self) -> None:
        if not self._input_ready():
            return
        index = self.findings.currentRow()
        if self._report is not None and 0 <= index < len(self._report["findings"]):
            source = self._report["findings"][index].get("source_span") or {}
            term = source.get("text")
            if isinstance(term, str) and term:
                self.lookup_requested.emit(term)

    def _update_controls(self) -> None:
        busy = self._review_state == "reviewing"
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        valid = all(0 < len(value) <= 2000 for value in (self._payload["source"], self._payload["target"]))
        self.review_button.setEnabled(bool(self._api_client.is_configured and valid and not busy and not composing))
        self.review_button.setVisible(not busy)
        self.cancel_button.setEnabled(busy)
        self.cancel_button.setVisible(busy)
        self.auto_align_button.setEnabled(self._payload["alignments"] is not None)
        self.findings.setEnabled(not composing)
        self.lookup_button.setEnabled(not composing)
        self.replace_toggle.setEnabled(not composing)
        index = self.findings.currentRow()
        has_target = (self._report is not None and 0 <= index < len(self._report["findings"])
                      and self._target_span_reliable(self._report["findings"][index].get("target_span")))
        self.replace_button.setEnabled(bool(has_target and self.replacement_edit.text() and not composing))
        self.undo_button.setEnabled(bool(self._undo_history) and not composing)
        self.redo_button.setEnabled(bool(self._redo_history) and not composing)
        self.remove_choice_button.setEnabled(0 <= self.conflict_list.currentRow() < len(self._conflict_indexes))
        self.save_label.setText(strings.MANUSCRIPT_SAVED if self._payload == self._saved_payload else strings.MANUSCRIPT_UNSAVED)
        source_language, target_language = (("EN", "ZH") if self._payload["direction"] == "zh" else ("ZH", "EN"))
        self.source_header_label.setText(f"{strings.INPUT_LABEL} · {source_language}")
        self.target_header_label.setText(f"{strings.RESULT_LABEL} · {target_language}")
        self.limit_label.setVisible(any(len(self._payload[key]) > 2000 for key in ("source", "target")))
        self.document_label.set_full_text(self.document_title + (" *" if self.document_dirty else ""))
        state = (self._path, self.document_dirty, self._document_revision)
        if state != self._document_state:
            self._document_state = state
            self.documentChanged.emit()
        choices, history = len(self._payload["choices"]), len(self._payload["history"])
        self.history_label.setVisible(bool(choices or history))
        self.history_label.setText(strings.MANUSCRIPT_SAVED_DETAILS.format(choices=choices, history=history))
        self._render_review_state()

    def _render_review_state(self) -> None:
        labels = {
            "none": strings.MANUSCRIPT_NO_REPORT,
            "history": strings.MANUSCRIPT_HISTORY,
            "reviewing": strings.MANUSCRIPT_REVIEWING,
            "stale": strings.MANUSCRIPT_REPORT_STALE,
            "stopped": strings.MANUSCRIPT_REVIEW_STOPPED,
            "cancelled": strings.STATUS_CANCELLED_NOTE,
            "error": self._review_detail,
        }
        if self._review_state == "current" and self._report is not None:
            report = self._report
            label = strings.MANUSCRIPT_CURRENT_REPORT.format(
                count=len(report["findings"]), not_evaluated=report["coverage"]["not_evaluated"],
                truncated=strings.STATUS_YES if report["truncated"] or any(f["candidates_truncated"] for f in report["findings"]) else strings.STATUS_NO)
        else:
            label = labels.get(self._review_state, strings.MANUSCRIPT_NO_REPORT)
        self.report_label.setText(label)
        self.report_label.setToolTip(strings.MANUSCRIPT_REPORT_NOTE)
        self.request_id_label.setText(strings.REQUEST_ID_LABEL.format(request_id=self._request_id or strings.REQUEST_ID_PLACEHOLDER))

    def _set_review_state(self, state: str, *, detail: str = "", request_id: str | None = None) -> None:
        self._review_state, self._review_detail, self._request_id = state, detail, request_id
        self._render_review_state()
        if self._report is None:
            self._render_finding(-1)

    def _draft_snapshot(self) -> dict:
        return copy.deepcopy({key: self._payload[key] for key in ("source", "target", "direction", "alignments", "choices")})

    def _apply_edit(self, value: dict, *, editors_already_updated: bool = False) -> None:
        before = self._draft_snapshot()
        if value == before:
            return
        self._undo_history = (self._undo_history + [before])[-20:]
        self._redo_history.clear()
        self._payload.update(copy.deepcopy(value))
        self._document_revision += 1
        if self.status_label.text() == strings.MANUSCRIPT_SAVE_DONE:
            self.status_label.setText(strings.MANUSCRIPT_UNSAVED)
        if not editors_already_updated:
            self._set_payload(self._payload)
        self._invalidate_report()
        if self._conflict_mode:
            self._refresh_conflicts()
        self._update_controls()

    def _apply_endpoint_state(self) -> None:
        self._update_controls()

    def _on_composition_changed(self) -> None:
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        if composing and self._review_state == "reviewing":
            self._invalidate_report()
        index = self.findings.currentRow()
        if self._report is not None and 0 <= index < len(self._report["findings"]):
            finding = self._report["findings"][index]
            for editor, key in ((self.source_edit, "source_span"), (self.target_edit, "target_span")):
                if editor._ime_active:
                    # A selected span can collapse while Qt displays preedit.
                    editor.setExtraSelections([])
                elif not composing:
                    editor.select_raw_span(finding.get(key), move_cursor=False)
        self._update_controls()

    def _input_ready(self) -> bool:
        if self.source_edit._ime_active or self.target_edit._ime_active:
            self.status_label.setText(strings.MANUSCRIPT_FINISH_INPUT)
            return False
        return True

    def _cancel_pending(self) -> None:
        self._generation += 1
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()

    def _invalidate_report(self) -> None:
        previous = self._review_state
        self._cancel_pending()
        self._report = None
        self.findings.clear()
        if previous in ("current", "stale"):
            state = "stale"
        elif previous == "reviewing":
            state = "stopped"
        else:
            state = "history" if self._payload["history"] else "none"
        self._set_review_state(state)
        self.source_edit.setExtraSelections([])
        self.target_edit.setExtraSelections([])
        self._update_controls()

    def reset_for_endpoint_change(self) -> None:
        self._invalidate_report()
        self.status_label.setText(strings.MANUSCRIPT_ENDPOINT_CHANGED)
        self._update_controls()

    def _on_edit(self, *_args) -> None:
        if self._updating:
            return
        value = self._draft_snapshot()
        value["source"] = self.source_edit.raw_text()
        value["target"] = self.target_edit.raw_text()
        value["direction"] = self.direction_combo.currentData()
        if value == self._draft_snapshot():
            return
        if value["alignments"] is not None:
            value["alignments"] = []
            self.status_label.setText(strings.MANUSCRIPT_ALIGNMENTS_CLEARED)
        self._apply_edit(value, editors_already_updated=True)

    def _start_review(self) -> None:
        if not self.review_button.isEnabled():
            return
        self._cancel_pending()
        generation = self._generation
        source, target = self._payload["source"], self._payload["target"]
        direction = self._payload["direction"]
        alignments = copy.deepcopy(self._payload["alignments"])
        self._report = None
        self.findings.clear()
        self._set_review_state("reviewing")
        self._task = asyncio.ensure_future(self._run_review(generation, source, target, direction, alignments))
        self._task.add_done_callback(lambda task: self._task_finished(task, generation))
        self._update_controls()

    def _use_auto_alignment(self) -> None:
        if self._payload["alignments"] is None or not self._input_ready():
            return
        value = self._draft_snapshot()
        value["alignments"] = None
        self._apply_edit(value)
        self.status_label.setText(strings.MANUSCRIPT_AUTO_ALIGN_SET)
        self._update_controls()

    async def _run_review(self, generation, source, target, direction, alignments) -> None:
        try:
            report = await self._api_client.review_pair(source, target, direction, alignments=alignments)
        except ClientError as exc:
            if generation == self._generation:
                self._set_review_state("error", detail=exc.message, request_id=exc.request_id)
        except asyncio.CancelledError:
            if generation == self._generation:
                self._set_review_state("cancelled")
        else:
            if generation != self._generation:
                return
            snapshot = {"source": source, "target": target, "direction": direction,
                        "alignments": alignments, "report": report}
            candidate = copy.deepcopy(self._payload)
            candidate["history"] = (candidate["history"] + [snapshot])[-2:]
            try:
                serialize_workfile(candidate)
            except WorkfileError as exc:
                self._set_review_state("error", detail=strings.MANUSCRIPT_REPORT_UNSAVEABLE)
                if exc.code == "workfile_duplicate_choices":
                    self._conflict_mode = True
                    self._refresh_conflicts()
                return
            self._payload = candidate
            self._document_revision += 1
            self._report = report
            self._set_review_state("current", request_id=report["request_id"])
            for finding in report["findings"]:
                source_span = finding.get("source_span") or {}
                target_span = finding.get("target_span") or {}
                verdicts = {"verified_constraint": strings.MANUSCRIPT_VERDICT_VERIFIED,
                            "confirmed_conflict": strings.MANUSCRIPT_VERDICT_CONFLICT,
                            "needs_review": strings.MANUSCRIPT_VERDICT_NEEDS_REVIEW,
                            "not_evaluated": strings.MANUSCRIPT_VERDICT_NOT_EVALUATED}
                target_text = target_span.get("text")
                verdict = verdicts[finding["verdict"]]
                second_line = f"{target_text} · {verdict}" if target_text else verdict
                self.findings.addItem(f'{source_span.get("text", "")}\n{second_line}')
            if self.findings.count():
                self.findings.setCurrentRow(0)
            else:
                self._render_finding(-1)
            self.status_label.setText(strings.MANUSCRIPT_REPORT_NOTE)
            self._update_controls()

    def _task_finished(self, task: asyncio.Task, generation: int) -> None:
        if generation != self._generation:
            return
        self._task = None
        if task.cancelled():
            self._set_review_state("cancelled")
        self._update_controls()

    def _cancel_review(self) -> None:
        if self._task is None:
            return
        self._cancel_pending()
        self._set_review_state("cancelled")
        self._update_controls()

    def _select_finding(self, index: int) -> None:
        self._render_finding(index)
        if self._report is None or not 0 <= index < len(self._report["findings"]):
            self._update_controls()
            return
        finding = self._report["findings"][index]
        source_ok = self.source_edit.select_raw_span(finding.get("source_span"))
        target_ok = self.target_edit.select_raw_span(finding.get("target_span"))
        self.status_label.setText(strings.MANUSCRIPT_LOCATED if source_ok and target_ok else strings.MANUSCRIPT_PARTIAL_LOCATION)
        candidates = finding["candidates"]
        if len(candidates) == 1:
            self.replacement_edit.setText(candidates[0][self._payload["direction"]])
        else:
            self.replacement_edit.clear()
        self._update_controls()

    def _replace_one(self) -> None:
        if not self._input_ready():
            return
        index = self.findings.currentRow()
        if self._report is None or not 0 <= index < len(self._report["findings"]):
            return
        span = self._report["findings"][index].get("target_span")
        if not span or not self.target_edit.select_raw_span(span):
            self.status_label.setText(strings.MANUSCRIPT_PARTIAL_LOCATION)
            return
        replacement = self.replacement_edit.text()
        if not replacement:
            return
        raw = self._payload["target"]
        changed = raw[:span["start"]] + replacement + raw[span["end"]:]
        if not self.target_edit.can_display(changed):
            self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED)
            return
        value = self._draft_snapshot()
        value["target"] = changed
        if value["alignments"] is not None:
            value["alignments"] = []
        self._apply_edit(value)
        self.status_label.setText(strings.MANUSCRIPT_REPLACED)
        self._update_controls()

    def _undo_replace(self) -> None:
        if not self._undo_history or not self._input_ready():
            return
        value = self._undo_history.pop()
        self._redo_history = (self._redo_history + [self._draft_snapshot()])[-20:]
        self._payload.update(copy.deepcopy(value))
        self._set_payload(self._payload)
        self._invalidate_report()
        if self._conflict_mode:
            self._refresh_conflicts()
        self.status_label.setText(strings.MANUSCRIPT_UNDONE)
        self._update_controls()

    def _redo_edit(self) -> None:
        if not self._redo_history or not self._input_ready():
            return
        value = self._redo_history.pop()
        self._undo_history = (self._undo_history + [self._draft_snapshot()])[-20:]
        self._payload.update(copy.deepcopy(value))
        self._set_payload(self._payload)
        self._invalidate_report()
        if self._conflict_mode:
            self._refresh_conflicts()
        self.status_label.setText(strings.MANUSCRIPT_REDONE)
        self._update_controls()

    def _set_payload(self, value: dict) -> None:
        self._updating = True
        try:
            self.source_edit.set_raw_text(value["source"])
            self.target_edit.set_raw_text(value["target"])
            self.direction_combo.setCurrentIndex(self.direction_combo.findData(value["direction"]))
            self._payload = copy.deepcopy(value)
            self._document_revision += 1
        finally:
            self._updating = False

    def _refresh_conflicts(self) -> None:
        groups = choice_conflict_groups(self._payload) if self._conflict_mode else []
        self._conflict_indexes = [index for group in groups for index in group]
        self.conflict_list.clear()
        for group_number, group in enumerate(groups, 1):
            for index in group:
                choice = self._payload["choices"][index]
                span = choice["source_span"]
                start, end = int(span["start"]), int(span["end"])
                decision = strings.MANUSCRIPT_CHOICE_NOT_TERM
                if choice["choice"] == "official_pair":
                    decision = strings.MANUSCRIPT_CHOICE_OFFICIAL.format(**choice["candidate"])
                context = choice["source"][max(0, start - 16):end + 16]
                context = context.replace("\r\n", " ↵ ").replace("\n", " ↵ ").replace("\r", " ↵ ")
                self.conflict_list.addItem(strings.MANUSCRIPT_CHOICE_ITEM.format(
                    group=group_number, number=index + 1, decision=decision, context=context,
                    start=start + 1, end=end))
        for widget in (self.conflict_label, self.conflict_list, self.remove_choice_button):
            widget.setVisible(bool(groups))
        if groups:
            self._inspector_requested = True
            self.inspector.show()
            self._update_inspector_button()
        self._update_controls()

    def _remove_conflicting_choice(self) -> None:
        if not self._input_ready():
            return
        row = self.conflict_list.currentRow()
        if not 0 <= row < len(self._conflict_indexes):
            return
        index = self._conflict_indexes[row]
        value = self._draft_snapshot()
        value["choices"].pop(index)
        self._apply_edit(value)
        self.status_label.setText(strings.MANUSCRIPT_CHOICE_REMOVED)

    def confirm_discard(self) -> bool:
        if not self._input_ready():
            return False
        if self._payload == self._saved_payload:
            return True
        box = QMessageBox(self)
        box.setWindowTitle(strings.MANUSCRIPT_UNSAVED_TITLE)
        box.setText(strings.MANUSCRIPT_UNSAVED_QUESTION)
        box.setStandardButtons(QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Save).setText(strings.MANUSCRIPT_SAVE)
        box.button(QMessageBox.StandardButton.Discard).setText(strings.MANUSCRIPT_DISCARD)
        box.button(QMessageBox.StandardButton.Cancel).setText(strings.CANCEL_BUTTON)
        result = box.exec()
        if result == QMessageBox.StandardButton.Save:
            return self.save_document()
        return result == QMessageBox.StandardButton.Discard

    def save_document(self, _checked: bool = False, *, save_as: bool = False) -> bool:
        if not self._input_ready():
            return False
        path = None if save_as else self._path
        if path is None:
            name, _ = QFileDialog.getSaveFileName(self, strings.MANUSCRIPT_SAVE_AS, "", strings.MANUSCRIPT_FILE_FILTER)
            if not name:
                return False
            path = Path(name)
        try:
            save_workfile(path, self._payload)
        except WorkfileError as exc:
            if exc.code == "workfile_duplicate_choices":
                self._conflict_mode = True
                self._refresh_conflicts()
                self.status_label.setText(strings.MANUSCRIPT_CHOICES_CONFLICT)
            else:
                self.status_label.setText(strings.MANUSCRIPT_SAVE_FAILED.format(reason=str(exc)))
            return False
        except OSError as exc:
            self.status_label.setText(strings.MANUSCRIPT_SAVE_FAILED.format(reason=str(exc)))
            return False
        self._path = path
        self._saved_payload = copy.deepcopy(self._payload)
        self.status_label.setText(strings.MANUSCRIPT_SAVE_DONE)
        self._update_controls()
        return True

    def open_document(self, _checked: bool = False) -> None:
        if not self._input_ready():
            return
        name, _ = QFileDialog.getOpenFileName(self, strings.MANUSCRIPT_OPEN, "", strings.MANUSCRIPT_FILE_FILTER)
        if not name:
            return
        def read_candidate() -> dict:
            value = load_workfile(name)
            if not self.source_edit.can_display(value["source"]) or not self.target_edit.can_display(value["target"]):
                raise ValueError(strings.MANUSCRIPT_UNSUPPORTED_TEXT)
            return value
        try:
            read_candidate()
        except (OSError, WorkfileError, ValueError) as exc:
            self.status_label.setText(strings.MANUSCRIPT_OPEN_FAILED.format(reason=str(exc)))
            return
        if not self.confirm_discard():
            return
        # Saving the dirty draft may have overwritten the selected file. Read
        # again so the restored draft and the saved baseline reflect disk.
        try:
            value = read_candidate()
        except (OSError, WorkfileError, ValueError) as exc:
            self.status_label.setText(strings.MANUSCRIPT_OPEN_FAILED.format(reason=str(exc)))
            return
        self._invalidate_report()
        self._undo_history.clear()
        self._redo_history.clear()
        self._conflict_mode = False
        self._refresh_conflicts()
        self._set_payload(value)
        self._path = Path(name)
        self._saved_payload = copy.deepcopy(value)
        self._set_review_state("history" if value["history"] else "none")
        self.status_label.setText(strings.MANUSCRIPT_OPENED)
        self._update_controls()

    def new_document(self, _checked: bool = False) -> None:
        if not self.confirm_discard():
            return
        self._invalidate_report()
        self._undo_history.clear()
        self._redo_history.clear()
        self._conflict_mode = False
        self._refresh_conflicts()
        value = empty_workfile()
        self._set_payload(value)
        self._saved_payload = copy.deepcopy(value)
        self._path = None
        self._set_review_state("none")
        self._update_controls()
