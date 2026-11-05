"""Real widgets exercise draft safety, review freshness and file operations."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from PySide6.QtCore import QCoreApplication, QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import QInputMethodEvent, QTextCursor, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QScrollArea

from wuwaterm_client.api import ApiClient
from wuwaterm_client.manuscript import empty_workfile, load_workfile, save_workfile
from wuwaterm_client.ui.manuscript_view import LosslessEditor, ManuscriptView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def report(source: str, target: str) -> dict:
    term = "term"
    start = source.index(term)
    target_term = "词"
    target_start = target.rindex(target_term)
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()
    return {
        "request_id": "example", "source_revision": digest(source), "target_revision": digest(target),
        "rule_version": "review-v2", "matcher_revision": "a" * 64,
        "dictionary": {"schema_version": "1", "source_commit": "b" * 40, "term_count": 1, "revision": "a" * 64},
        "coverage": {"evaluated": 1, "not_evaluated": 0, "rules": ["term"]},
        "findings": [{"id": f"{start}:{start + len(term)}:{term}", "rule_id": "term", "verdict": "confirmed_conflict",
                      "source_span": {"start": start, "end": start + len(term), "text": term},
                      "target_span": {"start": target_start, "end": target_start + 1, "text": target_term},
                      "candidates": [{"zh": "正确词", "en": "term", "category": "example", "sources": [], "candidate_id": "b" * 64}],
                      "candidates_truncated": False}], "truncated": False,
    }


def client_with(handler) -> ApiClient:
    return ApiClient("https://example.invalid", token_provider=lambda: "test-token", _test_transport=httpx.MockTransport(handler))


def install(view: ManuscriptView, source: str, target: str) -> None:
    value = empty_workfile()
    value.update(source=source, target=target, direction="zh")
    view._set_payload(value)
    view._update_controls()


def test_real_qt_editor_preserves_normalised_characters_and_paste(qapp) -> None:
    editor = LosslessEditor()
    original = "A\r\nB\xa0C\u2028D\u2029😀"
    editor.set_raw_text(original)
    assert editor.toPlainText() == "A\nB C\nD\n😀"
    cursor = editor.textCursor()
    cursor.setPosition(1)
    cursor.insertText("!")
    assert editor.raw_text() == "A!\r\nB\xa0C\u2028D\u2029😀"
    cursor = editor.textCursor()
    cursor.setPosition(0)
    editor.setTextCursor(cursor)
    mime = QMimeData()
    mime.setText("new\r\nline\xa0")
    editor.insertFromMimeData(mime)
    assert editor.raw_text() == "new\r\nline\xa0A!\r\nB\xa0C\u2028D\u2029😀"
    editor.set_raw_text("A\r\n😀B")
    cursor = editor.textCursor()
    cursor.select(QTextCursor.SelectionType.Document)
    editor.setTextCursor(cursor)
    assert editor.createMimeDataFromSelection().text() == "A\r\n😀B"
    assert editor.select_raw_span({"start": 4, "end": 5, "text": "B"})
    assert editor.textCursor().selectedText() == "B"
    assert editor.textCursor().selectionStart() == len("A\n😀".encode("utf-16-le")) // 2


def test_save_reopen_long_document_preserves_original_bytes_semantics(qapp, tmp_path: Path, monkeypatch) -> None:
    view = ManuscriptView(ApiClient(None))
    source = "😀\r\n词\xa0" * 500
    target = "稿" * 2100
    install(view, source, target)
    path = tmp_path / "draft.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    assert view.save_document()
    assert load_workfile(path)["source"] == source
    assert load_workfile(path)["target"] == target
    assert not view.review_button.isEnabled()
    reopened = ManuscriptView(ApiClient(None))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    reopened.open_document()
    assert reopened.source_edit.raw_text() == source
    assert reopened.target_edit.raw_text() == target
    assert reopened._payload == reopened._saved_payload


def test_edit_after_save_replaces_only_the_obsolete_success_notice(qapp, tmp_path) -> None:
    from wuwaterm_client import strings

    view = ManuscriptView(ApiClient(None))
    install(view, "source", "target")
    view._path = tmp_path / "draft.json"
    assert view.save_document()
    assert view.status_label.text() == strings.MANUSCRIPT_SAVE_DONE
    view.source_edit.textCursor().insertText("!")
    assert view.document_dirty
    assert view.status_label.text() == strings.MANUSCRIPT_UNSAVED
    assert load_workfile(view._path)["source"] == "source"


def test_edit_after_real_save_failure_keeps_the_failure_and_length_notice(qapp, tmp_path) -> None:
    from wuwaterm_client import strings

    view = ManuscriptView(ApiClient(None))
    install(view, "x" * 2000, "target")
    view._path = tmp_path / "missing-directory" / "draft.json"
    assert not view.save_document()
    failure = view.status_label.text()
    assert failure.startswith(strings.MANUSCRIPT_SAVE_FAILED.split("{reason}")[0])
    view.source_edit.textCursor().insertText("!")
    assert view.document_dirty and view.status_label.text() == failure
    assert not view.limit_label.isHidden()


def test_edit_after_choice_conflict_preserves_the_unresolved_notice(qapp, tmp_path, monkeypatch) -> None:
    from wuwaterm_client import strings

    source, target = "term", "词"
    prior = report(source, target)
    first = {"source": source, "direction": "zh", "source_span": prior["findings"][0]["source_span"],
             "scope": "positional", "choice": "not_a_term", "candidate": None,
             "basis": {"rule_version": "review-v2", "dictionary": prior["dictionary"],
                       "matcher_revision": prior["matcher_revision"]}}
    second = copy.deepcopy(first)
    second["source"] = "another block\n" + source
    second["source_span"]["start"] += len("another block\n")
    second["source_span"]["end"] += len("another block\n")
    value = empty_workfile()
    value.update(source=source + "\n" + source, target=target, direction="zh", choices=[first, second])
    path = tmp_path / "choices.json"
    save_workfile(path, value)
    original = path.read_bytes()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(path), ""))
    view = ManuscriptView(ApiClient(None))
    view.open_document()
    cursor = view.source_edit.textCursor()
    cursor.setPosition(len(source))
    cursor.setPosition(len(value["source"]), QTextCursor.MoveMode.KeepAnchor)
    cursor.removeSelectedText()
    assert not view.save_document()
    assert view.status_label.text() == strings.MANUSCRIPT_CHOICES_CONFLICT
    assert view.conflict_list.count() == 2
    view.target_edit.textCursor().insertText("!")
    assert view.status_label.text() == strings.MANUSCRIPT_CHOICES_CONFLICT
    assert view.conflict_list.count() == 2 and path.read_bytes() == original


def test_failed_save_cannot_replace_unsaved_current_document(qapp, tmp_path: Path, monkeypatch) -> None:
    from wuwaterm_client.ui import manuscript_view as module
    current = ManuscriptView(ApiClient(None))
    install(current, "unsaved source", "unsaved target")
    incoming = empty_workfile()
    incoming.update(source="incoming", target="translation")
    path = tmp_path / "incoming.json"
    save_workfile(path, incoming)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(tmp_path / "save.json"), ""))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Save)
    def fail(*args):
        raise OSError("simulated save failure")
    monkeypatch.setattr(module, "save_workfile", fail)
    current.open_document()
    assert current.source_edit.raw_text() == "unsaved source"
    assert current.target_edit.raw_text() == "unsaved target"
    assert current._payload != current._saved_payload
    assert "保存失败" in current.status_label.text()


def test_opening_same_file_after_save_reads_the_new_disk_contents(qapp, tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "draft.json"
    original = empty_workfile()
    original.update(source="source", target="target")
    save_workfile(path, original)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Save)
    view = ManuscriptView(ApiClient(None))
    view.open_document()
    cursor = view.source_edit.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertText("!")
    assert view._payload != view._saved_payload
    view.open_document()
    assert view.source_edit.raw_text() == "source!"
    assert view._payload == view._saved_payload == load_workfile(path)


def test_editing_explicit_alignment_is_undoable_and_auto_can_be_restored(qapp) -> None:
    view = ManuscriptView(ApiClient(None))
    value = empty_workfile()
    value.update(source="term", target="词", direction="zh", alignments=[
        {"source": {"start": 0, "end": 4, "text": "term"},
         "target": {"start": 0, "end": 1, "text": "词"}}
    ])
    view._set_payload(value)
    cursor = view.source_edit.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertText("!")
    assert view._payload["alignments"] == []
    assert "已清除" in view.status_label.text()
    view._undo_replace()
    assert view._payload == value
    view.source_edit.textCursor().insertText("!")
    assert view._payload["alignments"] == []
    view.auto_align_button.click()
    assert view._payload["alignments"] is None
    assert "自动对应" in view.status_label.text()
    assert view._payload != view._saved_payload
    view._undo_replace()
    assert view._payload["alignments"] == []


def test_review_locates_second_occurrence_and_replaces_once_then_undoes(qapp) -> None:
    async def scenario():
        source, target = "😀 term", "😀词 / 词"
        async def handler(request):
            assert request.url.path == "/v1/reviews"
            return httpx.Response(200, json=report(source, target))
        client = client_with(handler)
        view = ManuscriptView(client)
        install(view, source, target)
        view._start_review()
        await view._task
        await asyncio.sleep(0)
        assert view._report is not None
        view.findings.setCurrentRow(0)
        view.replacement_edit.clear()
        assert not view.replace_button.isEnabled()
        view._replace_one()
        assert view.target_edit.raw_text() == target
        view.replacement_edit.setText("正确词")
        assert view.replace_button.isEnabled()
        cursor = view.target_edit.textCursor()
        assert cursor.selectedText() == "词"
        assert cursor.selectionStart() == len("😀词 / ".encode("utf-16-le")) // 2
        view._replace_one()
        assert view.target_edit.raw_text() == "😀词 / 正确词"
        assert view._report is None
        assert "过期" in view.report_label.text()
        view._undo_replace()
        assert view.target_edit.raw_text() == target
        assert view._report is None
        await client.aclose()
    asyncio.run(scenario())


def test_review_error_preserves_draft_and_displays_request_id(qapp) -> None:
    async def scenario():
        async def handler(request):
            return httpx.Response(429, json={"error": {"code": "rate_limited", "message": "unavailable"}, "request_id": "req-example"})
        client = client_with(handler)
        view = ManuscriptView(client)
        install(view, "term", "词")
        view._start_review()
        await view._task
        await asyncio.sleep(0)
        assert "req-example" in view.request_id_label.text()
        assert view.source_edit.raw_text() == "term"
        assert view.target_edit.raw_text() == "词"
        assert view._report is None
        await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("old_status", [200, 429])
def test_old_response_cannot_be_displayed_after_edit_or_endpoint_change(qapp, old_status) -> None:
    async def scenario():
        gate = asyncio.Event()
        async def handler(request):
            try:
                await gate.wait()
            except asyncio.CancelledError:
                # A transport may finish after cancellation; model that race.
                await gate.wait()
            payload = report("term", "词") if old_status == 200 else {
                "error": {"code": "rate_limited", "message": "old error"}, "request_id": "old-request"}
            return httpx.Response(old_status, json=payload)
        client = client_with(handler)
        view = ManuscriptView(client)
        install(view, "term", "词")
        view._start_review()
        task = view._task
        await asyncio.sleep(0)
        cursor = view.source_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("!")
        edited = copy.deepcopy(view._payload)
        view.reset_for_endpoint_change()
        assert view._payload == edited
        gate.set()
        await task
        await asyncio.sleep(0)
        assert view._report is None
        assert view.source_edit.raw_text() == "term!"
        assert view.target_edit.raw_text() == "词"
        assert "old-request" not in view.request_id_label.text()
        await client.aclose()
    asyncio.run(scenario())


def test_full_import_edit_undo_save_review_and_endpoint_lifecycle(qapp, tmp_path: Path, monkeypatch) -> None:
    async def scenario():
        source, target = "😀 term", "😀词 / 词"
        prior = report(source, target)
        alignment = {"source": {"start": 0, "end": len(source), "text": source},
                     "target": {"start": 0, "end": len(target), "text": target}}
        finding = prior["findings"][0]
        choice = {"source": source, "direction": "zh", "source_span": finding["source_span"],
                  "scope": "positional", "choice": "not_a_term", "candidate": None,
                  "basis": {"rule_version": "review-v2", "dictionary": prior["dictionary"],
                            "matcher_revision": prior["matcher_revision"]}}
        value = empty_workfile()
        value.update(source=source, target=target, direction="zh", alignments=[alignment], choices=[choice],
                     history=[{"source": source, "target": target, "direction": "zh",
                               "alignments": [alignment], "report": prior}])
        path = tmp_path / "draft.json"
        save_workfile(path, value)
        async def handler(request):
            sent = json.loads(request.content)
            assert "resolutions" not in sent
            assert "alignments" not in sent
            return httpx.Response(200, json=report(sent["source"], sent["target"]))
        client = client_with(handler)
        view = ManuscriptView(client)
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
        view.open_document()
        assert view._payload == view._saved_payload == value
        assert view._report is None
        assert "待核对" in view.history_label.text() and "历史报告" in view.history_label.text()
        cursor = view.source_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("!")
        assert view._payload["alignments"] == []
        assert view._payload["choices"] == value["choices"] and view._payload["history"] == value["history"]
        assert view._payload != view._saved_payload
        view._undo_replace()
        assert view._payload == view._saved_payload == value and view._report is None
        cursor = view.source_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("!")
        view.auto_align_button.click()
        assert view._payload["alignments"] is None
        assert view.save_document()
        assert load_workfile(path) == view._payload == view._saved_payload
        view._start_review()
        await view._task
        await asyncio.sleep(0)
        assert view._report is not None
        assert view._payload["choices"] == value["choices"]
        assert view._payload["history"][0] == value["history"][0] and len(view._payload["history"]) == 2
        assert view._payload != view._saved_payload
        current = copy.deepcopy(view._payload)
        saved = copy.deepcopy(view._saved_payload)
        view.reset_for_endpoint_change()
        assert view._report is None and view._payload == current and view._saved_payload == saved
        await client.aclose()
    asyncio.run(scenario())


def test_cancel_before_request_starts_preserves_document(qapp) -> None:
    async def scenario():
        async def handler(request):
            raise AssertionError("cancelled before first step")
        client = client_with(handler)
        view = ManuscriptView(client)
        install(view, "term", "词")
        before = copy.deepcopy(view._payload)
        view._start_review()
        task = view._task
        view._cancel_review()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert view._payload == before and view._report is None
        assert "取消" in view.report_label.text()
        await client.aclose()
    asyncio.run(scenario())


def _document_with_report(source: str, target: str) -> dict:
    previous = report(source, target)
    alignment = {"source": {"start": 0, "end": len(source), "text": source},
                 "target": {"start": 0, "end": len(target), "text": target}}
    result = empty_workfile()
    result.update(source=source, target=target, direction="zh", alignments=[alignment],
                  history=[{"source": source, "target": target, "direction": "zh",
                            "alignments": [alignment], "report": previous}])
    return result


def test_ime_selection_commit_and_document_undo_redo_keep_raw_and_metadata(qapp, tmp_path) -> None:
    from PySide6.QtWidgets import QPlainTextEdit
    source, target = "A\r\nb😀c\xa0d\u2028e\u2029term", "词"
    value = _document_with_report(source, target)
    view = ManuscriptView(ApiClient(None))
    view._set_payload(value)
    view._saved_payload = copy.deepcopy(value)
    view._path = tmp_path / "draft.json"
    editor = view.source_edit
    reference = QPlainTextEdit()
    reference.setPlainText(source)
    for widget in (editor, reference):
        cursor = widget.textCursor()
        cursor.setPosition(2)
        cursor.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
        widget.setTextCursor(cursor)
    before_undo = copy.deepcopy(view._undo_history)
    for widget in (editor, reference):
        QCoreApplication.sendEvent(widget, QInputMethodEvent("ni", []))
    assert editor.raw_text() == source
    assert view._payload == view._saved_payload == value
    assert view._undo_history == before_undo
    assert editor.toPlainText() == reference.toPlainText()
    assert editor.textCursor().position() == reference.textCursor().position()
    assert view.save_document() is False
    assert not view._path.exists()
    for widget in (editor, reference):
        event = QInputMethodEvent()
        event.setCommitString("你")
        QCoreApplication.sendEvent(widget, event)
    expected = "A\r\n你c\xa0d\u2028e\u2029term"
    assert editor.raw_text() == expected
    assert editor.toPlainText() == reference.toPlainText()
    assert view._payload["alignments"] == []
    assert view._payload["history"] == value["history"]
    QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert view._payload == value
    assert view._payload == view._saved_payload and view._path == tmp_path / "draft.json"
    QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert view._payload["source"] == expected
    assert view._payload["history"] == value["history"]
    assert view._payload != view._saved_payload
    view.undo_button.click()
    assert view._payload == value
    view.redo_button.click()
    assert view._payload["source"] == expected


def test_preedit_cancellation_and_relative_replacement_follow_qt(qapp) -> None:
    source = "ab😀cd term"
    view = ManuscriptView(ApiClient(None))
    value = _document_with_report(source, "词")
    view._set_payload(value)
    editor = view.source_edit
    cursor = editor.textCursor()
    cursor.setPosition(1)
    cursor.setPosition(4, QTextCursor.MoveMode.KeepAnchor)
    editor.setTextCursor(cursor)
    QCoreApplication.sendEvent(editor, QInputMethodEvent("ni", []))
    QCoreApplication.sendEvent(editor, QInputMethodEvent())
    assert editor.raw_text() == source and view._payload == value
    assert view._undo_history == []
    cursor = editor.textCursor()
    cursor.setPosition(1)
    editor.setTextCursor(cursor)
    event = QInputMethodEvent()
    event.setCommitString("字", -1, 1)
    QCoreApplication.sendEvent(editor, event)
    assert editor.raw_text() == "字b😀cd term"
    QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert view._payload == value


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("save_as", [False, True])
@pytest.mark.parametrize("resolution", ["cancel", "commit"])
def test_mouse_save_during_preedit_keeps_focus_and_does_not_save(qapp, tmp_path, monkeypatch, side, save_as, resolution) -> None:
    async def scenario():
        from wuwaterm_client.config import ClientConfig
        from wuwaterm_client.ui import main_window, status_view

        source, target = "term", "词"
        async def handler(request):
            return httpx.Response(200, json=report(source, target))
        client = client_with(handler)
        monkeypatch.setattr(main_window, "has_token", lambda: False)
        monkeypatch.setattr(status_view, "read_token", lambda: None)
        monkeypatch.setattr(status_view, "active_backend_name", lambda: "test")
        window = main_window.MainWindow(ClientConfig(base_url="https://example.invalid"))
        window_api = window.api_client
        view = window.manuscript_view
        view._api_client = client
        try:
            window.show_page(main_window.PAGE_MANUSCRIPT)
            window.resize(1100, 800)
            window.show()
            window.activateWindow()
            install(view, source, target)
            view._start_review()
            await view._task
            await asyncio.sleep(0)
            assert view._review_state == "current" and view._report is not None
            view.findings.setCurrentRow(0)
            path, alternate = tmp_path / "draft.json", tmp_path / "copy.json"
            view._path = path
            assert view.save_document()
            original_bytes = path.read_bytes()
            before = copy.deepcopy(view._payload)
            saved_before = copy.deepcopy(view._saved_payload)
            current_report = copy.deepcopy(view._report)
            request_id, generation = view.request_id_label.text(), view._generation
            dialogs = []
            def choose_file(*_args):
                dialogs.append(True)
                return str(alternate), ""
            monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_file)

            editor = getattr(view, f"{side}_edit")
            button = view.save_as_button if save_as else view.save_button
            cursor = editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            editor.setTextCursor(cursor)
            editor.setFocus()
            qapp.processEvents()
            assert editor.hasFocus()
            QCoreApplication.sendEvent(editor, QInputMethodEvent("zhong", []))
            assert editor._ime_active and not view.review_button.isEnabled()
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            assert editor.hasFocus()
            assert editor._ime_active and view._payload == before
            assert view._saved_payload == saved_before and not view.review_button.isEnabled()
            assert view._report == current_report and view._review_state == "current"
            assert view._generation == generation and view.request_id_label.text() == request_id
            assert view.findings.currentRow() == 0 and view.findings.count() == 1
            assert path.read_bytes() == original_bytes and not alternate.exists() and dialogs == []
            assert "完成或取消" in view.status_label.text()

            event = QInputMethodEvent()
            if resolution == "commit":
                event.setCommitString("中")
            QCoreApplication.sendEvent(editor, event)
            if resolution == "cancel":
                assert view._payload == before and view._report == current_report
                assert view._generation == generation and view.request_id_label.text() == request_id
                assert view.review_button.isEnabled()
                QTest.keyClicks(editor, "x")
            added = "x" if resolution == "cancel" else "中"
            assert view._payload[side] == (source if side == "source" else target) + added
            assert view._review_state == "stale" and view._report is None
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            saved_path = alternate if save_as else path
            assert load_workfile(saved_path) == view._payload == view._saved_payload
            assert dialogs == ([True] if save_as else [])
            if save_as:
                assert path.read_bytes() == original_bytes
            else:
                assert path.read_bytes() != original_bytes
        finally:
            window.hide()
            await client.aclose()
            await window_api.aclose()
            window.deleteLater()
    asyncio.run(scenario())


def test_save_buttons_remain_reachable_by_keyboard_tab(qapp, tmp_path, monkeypatch) -> None:
    view = ManuscriptView(ApiClient(None))
    try:
        install(view, "source", "target")
        path, alternate = tmp_path / "draft.json", tmp_path / "copy.json"
        view._path = path
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(alternate), ""))
        view.resize(1100, 800)
        view.show()
        view.activateWindow()
        view.open_button.setFocus()
        qapp.processEvents()
        assert view.open_button.hasFocus()
        QTest.keyClick(view.open_button, Qt.Key.Key_Tab)
        assert view.save_button.hasFocus()
        QTest.keyClick(view.save_button, Qt.Key.Key_Space)
        assert load_workfile(path) == view._payload == view._saved_payload
        QTest.keyClick(view.save_button, Qt.Key.Key_Tab)
        assert view.save_as_button.hasFocus()
        QTest.keyClick(view.save_as_button, Qt.Key.Key_Space)
        assert load_workfile(alternate) == view._payload == view._saved_payload
    finally:
        view.close()


def test_long_manuscript_scrolls_each_editor_without_scrolling_the_workbench(qapp) -> None:
    view = ManuscriptView(ApiClient(None))
    try:
        view.resize(640, 380)
        source = "source line\r\n" * 200
        target = "target line\n" * 200
        install(view, source, target)
        view.show()
        qapp.processEvents()
        inspector_scroll = view.findChild(QScrollArea)
        assert not inspector_scroll.isAncestorOf(view.source_edit)
        assert not inspector_scroll.isAncestorOf(view.target_edit)
        for editor in (view.source_edit, view.target_edit):
            bar = editor.verticalScrollBar()
            assert bar.maximum() > 0
            bar.setValue(0)
            pos = QPointF(editor.viewport().rect().center())
            wheel = QWheelEvent(pos, pos, QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
            QCoreApplication.sendEvent(editor.viewport(), wheel)
            assert bar.value() > 0
        assert view.source_edit.raw_text() == source
        assert view.target_edit.raw_text() == target
        assert not view.review_button.isEnabled()
        assert view.limit_label.isVisible()
    finally:
        view.close()


def test_compact_workbench_keeps_actions_and_both_editors_reachable(qapp, tmp_path, monkeypatch) -> None:
    from wuwaterm_client import theme

    previous_style = qapp.styleSheet()
    qapp.setStyleSheet(theme.load_stylesheet("dark"))
    client = client_with(lambda _request: httpx.Response(500))
    view = ManuscriptView(client)
    try:
        path = tmp_path / ("a-very-long-manuscript-name-" * 5 + "draft.json")
        value = empty_workfile()
        value.update(source="source", target="translation", direction="zh")
        save_workfile(path, value)
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(path), ""))
        view.open_document()
        # Smaller than the manuscript area in a 683 x 384 logical window.
        view.resize(590, 340)
        view.show()
        qapp.processEvents()
        assert view.size().width() == 590
        assert view.document_title == path.name
        assert view.document_label.text() != path.name
        assert view.document_label.toolTip() == path.name
        assert view.document_label.accessibleName() == path.name
        assert view.editor_splitter.orientation() == Qt.Orientation.Horizontal
        assert not view.inspector.isVisible()
        for button in (view.new_button, view.open_button, view.save_button,
                       view.save_as_button, view.review_button, view.inspector_button,
                       view.direction_combo):
            assert button.isVisible()
            assert button.accessibleName() or (hasattr(button, "text") and button.text())
            assert view.rect().contains(button.mapTo(view, button.rect().bottomRight()))
        assert view.review_button.isEnabled()
        assert view.source_header_label.isVisible() and view.target_header_label.isVisible()
        assert view.source_edit.isVisible() and view.target_edit.isVisible()
        assert view.source_edit.height() > 0 and view.target_edit.height() > 0
        view.inspector_button.click()
        qapp.processEvents()
        assert view.inspector.isVisible()
        assert view.source_edit.isVisible() and view.target_edit.isVisible()
        assert view.source_edit.width() > 0 and view.target_edit.width() > 0
        view.inspector_button.click()
        assert not view.inspector.isVisible()
        # Height can change without a width transition; recompute both ways.
        view.resize(590, 700)
        qapp.processEvents()
        assert view.editor_splitter.orientation() == Qt.Orientation.Vertical
        view.resize(590, 340)
        qapp.processEvents()
        assert view.editor_splitter.orientation() == Qt.Orientation.Horizontal
    finally:
        view.close()
        asyncio.run(client.aclose())
        qapp.setStyleSheet(previous_style)


def test_short_full_window_keeps_both_editor_viewports_usable(qapp, monkeypatch) -> None:
    from wuwaterm_client import theme
    from wuwaterm_client.config import ClientConfig
    from wuwaterm_client.ui import main_window, status_view
    from wuwaterm_client.ui.components import clamp_to_workspace

    previous_style = qapp.styleSheet()
    qapp.setStyleSheet(theme.load_stylesheet("dark"))
    monkeypatch.setattr(main_window, "has_token", lambda: False)
    monkeypatch.setattr(status_view, "read_token", lambda: None)
    monkeypatch.setattr(status_view, "active_backend_name", lambda: "test")
    window = main_window.MainWindow(ClientConfig(base_url=None))
    try:
        minimum, _ = clamp_to_workspace(
            (683, 384), main_window.WINDOW_PREFERRED_MINIMUM,
            main_window.WINDOW_DEFAULT_SIZE, main_window.WINDOW_ABSOLUTE_MINIMUM)
        window.setMinimumSize(*minimum)
        window.resize(683, 384)
        window.show()
        qapp.processEvents()
        view = window.manuscript_view
        assert window.size().toTuple() == (683, 384)
        assert view.editor_splitter.orientation() == Qt.Orientation.Horizontal
        assert not view.inspector.isVisible()
        for editor in (view.source_edit, view.target_edit):
            assert editor.isVisible()
            assert editor.viewport().height() >= 90
        for button in (view.new_button, view.open_button, view.save_button,
                       view.save_as_button, view.review_button, view.inspector_button,
                       view.direction_combo):
            assert button.isVisibleTo(window)
            assert window.rect().contains(button.mapTo(window, button.rect().bottomRight()))
    finally:
        window.close()
        asyncio.run(window.api_client.aclose())
        qapp.setStyleSheet(previous_style)


def test_document_signal_and_title_follow_edits_save_open_and_new(qapp, tmp_path, monkeypatch) -> None:
    from wuwaterm_client import strings

    view = ManuscriptView(ApiClient(None))
    changes = []
    view.documentChanged.connect(lambda: changes.append((view.document_title, view.document_dirty)))
    assert view.document_title == strings.MANUSCRIPT_UNTITLED and not view.document_dirty
    view.source_edit.textCursor().insertText("a")
    assert changes[-1] == (strings.MANUSCRIPT_UNTITLED, True)
    count = len(changes)
    view.source_edit.textCursor().insertText("b")
    assert len(changes) > count
    path = tmp_path / "draft.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(path), ""))
    assert view.save_document()
    assert changes[-1] == ("draft.json", False)
    view.source_edit.textCursor().insertText("c")
    monkeypatch.setattr(QMessageBox, "exec", lambda _box: QMessageBox.StandardButton.Discard)
    view.new_document()
    assert changes[-1] == (strings.MANUSCRIPT_UNTITLED, False)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(path), ""))
    view.open_document()
    assert changes[-1] == ("draft.json", False)
    assert view.source_edit.raw_text() == "ab"


def test_missing_target_span_is_manual_and_lookup_uses_real_source(qapp) -> None:
    async def scenario():
        source, target = "term", "译文"
        payload = report(source, "词")
        payload["target_revision"] = hashlib.sha256(target.encode()).hexdigest()
        payload["findings"][0]["target_span"] = None
        payload["findings"][0]["rule_id"] = "review.term_pair"
        async def handler(request):
            return httpx.Response(200, json=payload)
        client = client_with(handler)
        view = ManuscriptView(client)
        try:
            view.resize(1100, 800)
            view.show()
            install(view, source, target)
            view._start_review()
            await view._task
            await asyncio.sleep(0)
            qapp.processEvents()
            assert view.findings.currentRow() == 0
            assert view.manual_edit_label.isVisible()
            assert not view.replace_toggle.isVisible()
            assert not view.replacement_area.isVisible()
            assert not view.replace_button.isEnabled()
            assert "term" in view.finding_title.text()
            assert "正确词" in view.candidates_label.text()
            assert "术语配对" in view.finding_rule.text()
            requested = []
            view.lookup_requested.connect(requested.append)
            view.lookup_button.click()
            assert requested == ["term"]
            assert view.target_edit.raw_text() == target
        finally:
            view.close()
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("verdict", ["verified_constraint", "confirmed_conflict"])
def test_replacement_action_requires_a_located_nonverified_finding(qapp, verdict) -> None:
    async def scenario():
        payload = report("term", "词")
        payload["findings"][0]["verdict"] = verdict
        async def handler(_request):
            return httpx.Response(200, json=payload)
        client = client_with(handler)
        view = ManuscriptView(client)
        try:
            view.resize(1100, 800)
            view.show()
            install(view, "term", "词")
            view._start_review()
            await view._task
            await asyncio.sleep(0)
            qapp.processEvents()
            assert not view.manual_edit_label.isVisible()
            assert view.replace_toggle.isVisible() == (verdict == "confirmed_conflict")
            assert not view.replacement_area.isVisible()
            if verdict == "confirmed_conflict":
                view.replace_toggle.click()
                assert view.replacement_area.isVisible()
                assert view.replace_button.isEnabled()
        finally:
            view.close()
            await client.aclose()
    asyncio.run(scenario())


def test_successful_empty_review_does_not_say_unreviewed(qapp) -> None:
    from wuwaterm_client import strings

    async def scenario():
        payload = report("term", "词")
        payload["findings"] = []
        async def handler(_request):
            return httpx.Response(200, json=payload)
        client = client_with(handler)
        view = ManuscriptView(client)
        try:
            view.resize(1100, 800)
            view.show()
            install(view, "term", "词")
            view._start_review()
            await view._task
            await asyncio.sleep(0)
            qapp.processEvents()
            assert view._review_state == "current"
            assert view.finding_title.text() == strings.MANUSCRIPT_NO_FINDINGS
            assert not view.findings.isVisible()
            assert "0" in view.report_label.text()
        finally:
            view.close()
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("selected", [False, True])
def test_current_report_survives_preedit_cancel_with_correct_locations(qapp, side, selected) -> None:
    async def scenario():
        source, target = "😀 term / term", "😀词 / 词"
        response = report(source, target)
        second = copy.deepcopy(response["findings"][0])
        start = source.rindex("term")
        second["id"] = f"{start}:{start + 4}:term"
        second["source_span"] = {"start": start, "end": start + 4, "text": "term"}
        second["target_span"] = {"start": 1, "end": 2, "text": "词"}
        response["findings"].append(second)
        response["coverage"]["evaluated"] = 2

        async def handler(request):
            return httpx.Response(200, json=response)

        client = client_with(handler)
        view = ManuscriptView(client)
        view.show()
        install(view, source[:-1], target)
        cursor = view.source_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("m")
        cursor.insertText("x")
        view.undo_button.click()
        assert view._payload["source"] == source
        view._start_review()
        await view._task
        await asyncio.sleep(0)
        view.findings.setCurrentRow(0)
        editor = getattr(view, f"{side}_edit")
        other = view.target_edit if side == "source" else view.source_edit
        cursor = editor.textCursor()
        if not selected:
            cursor.movePosition(QTextCursor.MoveOperation.End)
            editor.setTextCursor(cursor)

        # Compare the resulting caret with an editor that has no report: merely
        # restoring highlights must not move it after cancellation.
        reference = LosslessEditor()
        reference.set_raw_text(editor.raw_text())
        reference_cursor = reference.textCursor()
        reference_cursor.setPosition(cursor.anchor())
        reference_cursor.setPosition(cursor.position(), QTextCursor.MoveMode.KeepAnchor)
        reference.setTextCursor(reference_cursor)

        def highlight(widget):
            return [(item.cursor.selectionStart(), item.cursor.selectionEnd(), item.cursor.selectedText())
                    for item in widget.extraSelections()]

        original_highlights = (highlight(view.source_edit), highlight(view.target_edit))
        assert original_highlights[0][0][2] == "term"
        assert original_highlights[1][0][2] == "词"
        before = copy.deepcopy(view._payload)
        request_id = view.request_id_label.text()
        generation = view._generation
        report_label = view.report_label.text()
        items = [view.findings.item(i).text() for i in range(view.findings.count())]
        assert view.replace_button.isEnabled() and view.undo_button.isEnabled() and view.redo_button.isEnabled()
        for widget in (editor, reference):
            QCoreApplication.sendEvent(widget, QInputMethodEvent("ni", []))
        assert view._review_state == "current" and view._report == response
        assert view._payload == before and view._generation == generation
        assert view.request_id_label.text() == request_id and view.report_label.text() == report_label
        assert [view.findings.item(i).text() for i in range(view.findings.count())] == items
        assert not view.findings.isEnabled() and not view.replace_button.isEnabled()
        assert not view.review_button.isEnabled() and not view.undo_button.isEnabled() and not view.redo_button.isEnabled()
        assert highlight(editor) == []
        assert highlight(other) == original_highlights[1 if side == "source" else 0]
        QTest.mouseClick(view.findings.viewport(), Qt.MouseButton.LeftButton,
                         pos=view.findings.visualItemRect(view.findings.item(1)).center())
        assert view.findings.currentRow() == 0 and editor._ime_active

        for widget in (editor, reference):
            QCoreApplication.sendEvent(widget, QInputMethodEvent())
        assert editor.raw_text() == reference.raw_text() and view._payload == before
        assert (editor.textCursor().position(), editor.textCursor().anchor()) == (
            reference.textCursor().position(), reference.textCursor().anchor())
        assert view._review_state == "current" and view._report == response
        assert view._generation == generation and view.request_id_label.text() == request_id
        assert view.report_label.text() == report_label and view.findings.currentRow() == 0
        assert (highlight(view.source_edit), highlight(view.target_edit)) == original_highlights
        assert view.findings.isEnabled() and view.review_button.isEnabled() and view.replace_button.isEnabled()
        assert view.undo_button.isEnabled() and view.redo_button.isEnabled()

        view.findings.setFocus()
        QTest.keyClick(view.findings, Qt.Key.Key_Down)
        assert view.findings.currentRow() == 1
        assert highlight(view.source_edit)[0][2] == "term"
        assert highlight(view.target_edit)[0][2] == "词"
        assert highlight(view.source_edit)[0][0] == len(source[:start].encode("utf-16-le")) // 2
        assert highlight(view.target_edit)[0][0] == 2
        await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("side", ["source", "target"])
def test_committed_preedit_stales_current_report(qapp, side) -> None:
    async def scenario():
        source, target = "term", "词"
        async def handler(request):
            return httpx.Response(200, json=report(source, target))
        client = client_with(handler)
        view = ManuscriptView(client)
        install(view, source, target)
        view._start_review()
        await view._task
        await asyncio.sleep(0)
        view.findings.setCurrentRow(0)
        editor = getattr(view, f"{side}_edit")
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cursor)
        QCoreApplication.sendEvent(editor, QInputMethodEvent("ni", []))
        assert view._review_state == "current" and view._report is not None
        event = QInputMethodEvent()
        event.setCommitString("字")
        QCoreApplication.sendEvent(editor, event)
        assert view._payload[side] == (source if side == "source" else target) + "字"
        assert view._review_state == "stale" and view._report is None and view.findings.count() == 0
        assert not view.source_edit.extraSelections() and not view.target_edit.extraSelections()
        assert view.findings.isEnabled() and view.review_button.isEnabled()
        await client.aclose()
    asyncio.run(scenario())


def test_keyboard_paste_delete_and_both_undo_routes_share_document_history(qapp) -> None:
    view = ManuscriptView(ApiClient(None))
    original = _document_with_report("term\r\n😀\xa0x", "词")
    view._set_payload(original)
    view._saved_payload = copy.deepcopy(original)
    editor = view.source_edit
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    editor.setTextCursor(cursor)
    QApplication.clipboard().setText("p\r\nq\xa0")
    QTest.keyClick(editor, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    pasted = "term\r\n😀\xa0xp\r\nq\xa0"
    assert view._payload["source"] == pasted
    assert view._payload["history"] == original["history"]
    QTest.keyClick(editor, Qt.Key.Key_Backspace)
    assert view._payload["source"] == pasted[:-1]
    QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert view._payload["source"] == pasted
    view.undo_button.click()
    assert view._payload == original and view._payload == view._saved_payload
    view.redo_button.click()
    assert view._payload["source"] == pasted
    QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert view._payload["source"] == pasted[:-1]
    assert view._payload["history"] == original["history"]


def test_replacement_then_edit_undo_preserves_later_report_history(qapp) -> None:
    async def scenario():
        source, target = "😀 term", "😀词 / 词"
        initial = _document_with_report(source, target)
        async def handler(request):
            return httpx.Response(200, json=report(source, target))
        client = client_with(handler)
        view = ManuscriptView(client)
        view._set_payload(initial)
        view._saved_payload = copy.deepcopy(initial)
        view._update_controls()
        view._start_review()
        await view._task
        await asyncio.sleep(0)
        history = copy.deepcopy(view._payload["history"])
        assert len(history) == 2
        view.findings.setCurrentRow(0)
        view._replace_one()
        assert view._payload["target"] == "😀词 / 正确词"
        cursor = view.source_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("!")
        assert view._payload["source"] == source + "!"
        view.undo_button.click()
        assert view._payload["source"] == source and view._payload["target"] == "😀词 / 正确词"
        assert view._payload["history"] == history and view._report is None
        QTest.keyClick(view.target_edit, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert view._payload["target"] == target
        assert view._payload["alignments"] == initial["alignments"]
        assert view._payload["history"] == history and view._payload != view._saved_payload
        assert view._report is None and "过期" in view.report_label.text()
        await client.aclose()
    asyncio.run(scenario())


@pytest.mark.skipif(not __import__("os").environ.get("WUWATERM_WEB_ROOT") or not __import__("shutil").which("node"),
                    reason="requires Web checkout and Node")
def test_conflicting_imported_choices_require_explicit_undoable_removal(qapp, tmp_path, monkeypatch) -> None:
    import os
    import subprocess
    base, target = "😀 term", "词"
    prior = report(base, target)
    finding = prior["findings"][0]
    first = {"source": base, "direction": "zh", "source_span": finding["source_span"],
             "scope": "positional", "choice": "not_a_term", "candidate": None,
             "basis": {"rule_version": "review-v2", "dictionary": prior["dictionary"],
                       "matcher_revision": prior["matcher_revision"]}}
    second = copy.deepcopy(first)
    second["source"] = "another block\n" + base
    second["source_span"]["start"] += len("another block\n")
    second["source_span"]["end"] += len("another block\n")
    value = empty_workfile()
    value.update(source=base + "\n" + base, target=target, direction="zh", choices=[first, second],
                 history=[{"source": base, "target": target, "direction": "zh",
                           "alignments": None, "report": prior}])
    path = tmp_path / "draft.json"
    save_workfile(path, value)
    before_bytes = path.read_bytes()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    view = ManuscriptView(ApiClient(None))
    view.open_document()
    assert view._payload == value and view._report is None
    cursor = view.source_edit.textCursor()
    cursor.setPosition(len(base.encode("utf-16-le")) // 2)
    cursor.setPosition(len((base + "\n" + base).encode("utf-16-le")) // 2, QTextCursor.MoveMode.KeepAnchor)
    cursor.removeSelectedText()
    assert view._payload["source"] == base and view._payload["choices"] == [first, second]
    assert view.save_document() is False
    assert "无法保存" in view.status_label.text() and path.read_bytes() == before_bytes
    assert view.conflict_list.count() == 2
    assert "不是术语" in view.conflict_list.item(0).text()
    assert "原文" in view.conflict_list.item(1).text()
    view.conflict_list.setCurrentRow(1)
    view.remove_choice_button.click()
    assert view._payload["choices"] == [first] and view._payload["history"] == value["history"]
    view.undo_button.click()
    assert view._payload["choices"] == [first, second] and view.conflict_list.count() == 2
    view.redo_button.click()
    assert view._payload["choices"] == [first] and view._payload["history"] == value["history"]
    assert view.save_document()
    assert load_workfile(path) == view._payload == view._saved_payload
    program = "import fs from 'node:fs'; import {parseWorkfile} from './lib/manuscript.js'; console.log(JSON.stringify(parseWorkfile(fs.readFileSync(process.argv[1], 'utf8'))));"
    parsed = subprocess.run(["node", "--input-type=module", "-e", program, str(path)],
                            cwd=os.environ["WUWATERM_WEB_ROOT"], capture_output=True, text=True, check=True, timeout=20)
    assert json.loads(parsed.stdout) == view._payload


@pytest.mark.parametrize("answer", [QMessageBox.StandardButton.Save, QMessageBox.StandardButton.Discard, QMessageBox.StandardButton.Cancel])
def test_open_same_file_save_discard_cancel_preserves_disk_and_baseline(qapp, tmp_path, monkeypatch, answer) -> None:
    original = _document_with_report("term", "词")
    path = tmp_path / "draft.json"
    save_workfile(path, original)
    original_bytes = path.read_bytes()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: answer)
    view = ManuscriptView(ApiClient(None))
    view.open_document()
    cursor = view.source_edit.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertText("!")
    edited = copy.deepcopy(view._payload)
    assert view._payload != view._saved_payload
    view.open_document()
    assert view._path == path
    if answer == QMessageBox.StandardButton.Save:
        assert load_workfile(path) == edited == view._payload == view._saved_payload
        assert path.read_bytes() != original_bytes
    elif answer == QMessageBox.StandardButton.Discard:
        assert path.read_bytes() == original_bytes
        assert view._payload == view._saved_payload == original
    else:
        assert path.read_bytes() == original_bytes
        assert view._payload == edited and view._saved_payload == original


def test_actual_filesystem_failure_preserves_existing_path_bytes_and_dirty(qapp, tmp_path, monkeypatch) -> None:
    original = _document_with_report("term", "词")
    path = tmp_path / "draft.json"
    save_workfile(path, original)
    original_bytes = path.read_bytes()
    view = ManuscriptView(ApiClient(None))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    view.open_document()
    cursor = view.source_edit.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertText("!")
    edited = copy.deepcopy(view._payload)
    missing_parent = tmp_path / "missing" / "draft.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(missing_parent), ""))
    assert view.save_document(save_as=True) is False
    assert "保存失败" in view.status_label.text()
    assert not missing_parent.exists()
    assert path.read_bytes() == original_bytes and view._path == path
    assert view._payload == edited and view._saved_payload == original


@pytest.mark.parametrize("operation", ["edit", "endpoint", "new", "second", "source_preedit", "target_preedit"])
@pytest.mark.parametrize("old_status", [200, 429])
def test_delayed_review_cannot_leave_busy_state_or_overwrite_new_state(qapp, operation, old_status) -> None:
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                if old_status == 429:
                    return httpx.Response(429, json={"error": {"code": "rate_limited", "message": "old"},
                                                     "request_id": "old-request"})
                old = report("term", "词")
                old["request_id"] = "old-request"
                return httpx.Response(200, json=old)
            latest = report("term", "词")
            latest["request_id"] = "new-request"
            return httpx.Response(200, json=latest)
        client = client_with(handler)
        view = ManuscriptView(client)
        install(view, "term", "词")
        view._saved_payload = copy.deepcopy(view._payload)
        view._start_review()
        old_task = view._task
        await started.wait()
        assert view._review_state == "reviewing" and "正在审校" in view.report_label.text()
        if operation == "edit":
            cursor = view.source_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText("!")
        elif operation == "endpoint":
            view.reset_for_endpoint_change()
        elif operation == "new":
            view.new_document()
        elif operation.endswith("_preedit"):
            editor = view.source_edit if operation == "source_preedit" else view.target_edit
            QCoreApplication.sendEvent(editor, QInputMethodEvent("ni", []))
            assert view._review_state == "stopped" and not view.cancel_button.isEnabled()
            release.set()
            await asyncio.gather(old_task, return_exceptions=True)
            await asyncio.sleep(0)
            assert view._review_state == "stopped" and view._report is None
            assert "old-request" not in view.request_id_label.text()
            QCoreApplication.sendEvent(editor, QInputMethodEvent())
        else:
            view._cancel_review()
            assert view._review_state == "cancelled" and "取消" in view.report_label.text()
            view._start_review()
            await view._task
            await asyncio.sleep(0)
            assert view._review_state == "current" and view._report["request_id"] == "new-request"
        assert "正在审校" not in view.report_label.text()
        assert not view.cancel_button.isEnabled()
        expected_payload = copy.deepcopy(view._payload)
        expected_saved = copy.deepcopy(view._saved_payload)
        expected_state = view._review_state
        expected_label = view.report_label.text()
        expected_request_id = view.request_id_label.text()
        expected_report = copy.deepcopy(view._report)
        expected_review_enabled = view.review_button.isEnabled()
        release.set()
        await asyncio.gather(old_task, return_exceptions=True)
        await asyncio.sleep(0)
        assert view._payload == expected_payload and view._saved_payload == expected_saved
        assert view._review_state == expected_state and view.report_label.text() == expected_label
        assert view.request_id_label.text() == expected_request_id and view._report == expected_report
        assert view.review_button.isEnabled() == expected_review_enabled
        assert not view.cancel_button.isEnabled()
        await client.aclose()
    asyncio.run(scenario())
