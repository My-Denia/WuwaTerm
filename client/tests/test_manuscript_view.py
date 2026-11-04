"""Real widgets exercise draft safety, review freshness and file operations."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from PySide6.QtCore import QCoreApplication, QMimeData, Qt
from PySide6.QtGui import QInputMethodEvent, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

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


@pytest.mark.parametrize("operation", ["edit", "endpoint", "new", "second"])
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
