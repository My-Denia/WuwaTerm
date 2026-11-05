"""Real-Qt smoke test: constructs each widget under the offscreen platform
(set in conftest.py) and checks it builds without raising."""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from wuwaterm_client.api import ApiClient  # noqa: E402
from wuwaterm_client.config import ClientConfig  # noqa: E402
from wuwaterm_client.errors import message_for  # noqa: E402
from wuwaterm_client.ui.first_run_dialog import FirstRunDialog  # noqa: E402
from wuwaterm_client.ui.main_window import MainWindow  # noqa: E402
from wuwaterm_client.ui.settings_dialog import SettingsDialog  # noqa: E402
from wuwaterm_client.ui.status_view import StatusView  # noqa: E402
from wuwaterm_client.ui.terms_view import TermsView  # noqa: E402
from wuwaterm_client.ui.translate_view import TranslateView  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _dummy_client() -> ApiClient:
    async def handler(request):  # pragma: no cover - never invoked here
        raise AssertionError("no network calls expected in a construction smoke test")

    return ApiClient("https://test", _test_transport=httpx.MockTransport(handler))


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


def test_translate_view_constructs(qapp) -> None:
    view = TranslateView(_dummy_client())
    assert view.translate_button.isEnabled()
    assert not view.cancel_button.isEnabled()


def test_terms_view_constructs(qapp) -> None:
    view = TermsView(_dummy_client())
    assert view.table.columnCount() == 5


def test_status_view_constructs(qapp) -> None:
    view = StatusView(_dummy_client())
    assert view.refresh_button.isEnabled()


def test_settings_dialog_constructs(qapp) -> None:
    dialog = SettingsDialog(ClientConfig())
    assert dialog.windowTitle()


def test_first_run_dialog_constructs(qapp) -> None:
    dialog = FirstRunDialog()
    assert dialog.windowTitle()


def test_main_window_constructs(qapp, tmp_path, monkeypatch) -> None:
    from wuwaterm_client.ui import main_window as main_window_module

    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(main_window_module, "has_token", lambda: False)

    window = MainWindow(config=ClientConfig())

    assert window.windowTitle()
    assert window.stack.currentWidget() is window.manuscript_view
    assert window._nav_by_page[main_window_module.PAGE_MANUSCRIPT].isChecked()
    _assert_the_three_areas_are_reachable(window)
    # An invalid page must leave the current page unchanged.
    current = window.stack.currentWidget()
    window.show_page(99)
    assert window.stack.currentWidget() is current


def test_settings_push_the_new_timeouts_into_the_live_client(qapp, tmp_path, monkeypatch) -> None:
    """A saved timeout that only takes effect next launch is a silent lie.

    The settings dialog writes the value and the window pushes it; without the
    push the running client keeps the old timeout while the file says
    otherwise.
    """
    from wuwaterm_client import config as config_module
    from wuwaterm_client.config import ClientConfig
    from wuwaterm_client.ui import main_window as main_window_module

    monkeypatch.setattr(config_module, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(main_window_module, "has_token", lambda: True)

    window = MainWindow(ClientConfig(base_url="https://test", request_timeout_seconds=5.0))
    changed = ClientConfig(
        base_url="https://elsewhere",
        request_timeout_seconds=42.0,
        translate_timeout_seconds=99.0,
    )

    class _AcceptedDialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            from PySide6.QtWidgets import QDialog

            return QDialog.DialogCode.Accepted

        def result_config(self):
            return changed

    monkeypatch.setattr(main_window_module, "SettingsDialog", _AcceptedDialog)
    window._on_settings_clicked()

    assert window.api_client._timeout == 42.0
    assert window.api_client._translate_timeout == 99.0
    assert str(window.api_client._client.base_url).startswith("https://elsewhere")


def test_the_first_run_dialog_will_not_continue_without_a_token(qapp) -> None:
    """Accepting an empty field closed the dialog, and the caller then shut
    the whole application down as though the user had chosen Quit.

    The refusal is now stated before the click rather than after it: Continue
    is disabled while the field holds nothing usable, because a button that
    accepts the press and then silently moves focus reads as broken. The
    guarantee is unchanged and still belongs to the caller - `ensure_credential`
    treats anything but an accepted dialog as "no credential yet" - so what
    matters here is that an empty field cannot produce an accepted dialog by
    any route.
    """
    from PySide6.QtWidgets import QDialog

    dialog = FirstRunDialog()
    assert dialog.continue_button.isEnabled() is False

    dialog.token_edit.setText("   ")
    assert dialog.continue_button.isEnabled() is False
    dialog.continue_button.click()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.isVisible() is False or dialog.result() == 0

    # The slot stays reachable without the button - a default-button
    # activation, or a caller holding the dialog - so it refuses on its own
    # rather than relying on the disabled state.
    dialog._on_continue_clicked()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.token() == ""

    dialog.token_edit.setText("wtd1.device.secret")
    assert dialog.continue_button.isEnabled() is True
    dialog.continue_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_storing_a_first_run_credential_refreshes_the_status_view(
    qapp, tmp_path, monkeypatch
) -> None:
    """The status view read the credential state before the first-run dialog
    ran, so without a refresh it reports "missing" for the whole session."""
    from wuwaterm_client.config import ClientConfig
    from wuwaterm_client.ui import main_window as main_window_module

    stored: dict[str, str] = {}
    monkeypatch.setattr(main_window_module, "has_token", lambda: bool(stored))
    monkeypatch.setattr(
        main_window_module, "store_token", lambda token: stored.update(token=token)
    )

    window = MainWindow(ClientConfig(base_url="https://test"))
    refreshed: list[bool] = []
    monkeypatch.setattr(
        window.status_view,
        "refresh_credential_state",
        lambda: refreshed.append(True),
    )

    class _AcceptedDialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            from PySide6.QtWidgets import QDialog

            return QDialog.DialogCode.Accepted

        def token(self):
            return "wtd1.device.secret"

    monkeypatch.setattr(main_window_module, "FirstRunDialog", _AcceptedDialog)

    assert window.ensure_credential() is True
    assert stored["token"] == "wtd1.device.secret"
    assert refreshed == [True]


def test_an_unprotected_address_is_refused_on_screen_and_changes_nothing(
    qapp, monkeypatch
) -> None:
    """The refusal has to reach the owner, not just the transport.

    If the window swallowed the error the settings dialog would close on an
    address the client will never use, and the only sign would be that
    requests kept going somewhere else.

    The refusal used to be a modal warning box. It is now the global banner,
    which is a strictly stronger form of the same claim: a box disappears on
    the first click and takes the reason with it, while the banner stays
    readable while the owner goes back to Settings to fix the address.
    """
    from PySide6.QtWidgets import QDialog

    from wuwaterm_client import strings
    from wuwaterm_client.config import ClientConfig
    from wuwaterm_client.ui import main_window as main_window_module

    monkeypatch.setattr(main_window_module, "has_token", lambda: True)

    window = MainWindow(ClientConfig(base_url="http://127.0.0.1:8787"))
    refused = ClientConfig(base_url="http://198.51.100.7:8787", request_timeout_seconds=42.0)

    saved: list[str] = []
    monkeypatch.setattr(
        ClientConfig, "save", lambda self, base_dir=None: saved.append(self.base_url)
    )

    class _AcceptedDialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def result_config(self):
            return refused

    monkeypatch.setattr(main_window_module, "SettingsDialog", _AcceptedDialog)
    window._on_settings_clicked()

    assert window.global_banner.is_showing(), (
        "the owner must be told the address was refused"
    )
    assert window.global_banner.message_text == strings.ERROR_MSG_INSECURE_ENDPOINT
    # Nothing half-applied: not the address, not the timeout, not the file.
    live = window.api_client._client.base_url
    assert (live.scheme, live.host, live.port) == ("http", "127.0.0.1", 8787)
    assert window.config.base_url == "http://127.0.0.1:8787"
    assert window.api_client._timeout != 42.0
    assert saved == []
    # ...and the chip still names the address that is actually in use, rather
    # than the one that was just rejected.
    assert window.endpoint_chip.is_configured is True
    assert window.endpoint_chip.address_text == "http://127.0.0.1:8787"


def test_settings_refuse_an_address_that_cannot_be_used(qapp) -> None:
    """A saved address that fails every request is worse than no change.

    The refusal is stated on the field that caused it and the dialog stays
    open, because the correction has to be made in that field: a modal over a
    dialog that had already closed left the owner with a value they could no
    longer see, let alone edit.
    """
    from PySide6.QtWidgets import QDialog

    from wuwaterm_client import strings

    dialog = SettingsDialog(ClientConfig(base_url="http://127.0.0.1:8787"))
    dialog.base_url_edit.setText("http://127.0.0.1:notaport")
    dialog._on_accepted()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.base_url_error.is_showing() is True
    assert dialog.base_url_error.text() == strings.SETTINGS_INVALID_BASE_URL_MESSAGE
    assert dialog.base_url_edit.property("invalid") is True

    dialog.base_url_edit.setText("http://127.0.0.1:9999")
    # Editing withdraws the complaint: it was about text nobody is looking at
    # any more.
    assert dialog.base_url_error.is_showing() is False
    assert dialog.base_url_edit.property("invalid") is False
    dialog._on_accepted()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_the_caller_can_refuse_an_address_before_the_dialog_closes(qapp) -> None:
    """The window's own refusal lands on the field, not after the fact.

    The transport is the authority on which addresses may be used, and it is
    reached through the callback the window hands in. This is the wiring that
    lets its answer arrive while the field is still on screen.
    """
    from PySide6.QtWidgets import QDialog

    from wuwaterm_client import strings

    seen: list[str] = []

    def refuse(base_url: str) -> str | None:
        seen.append(base_url)
        return strings.ERROR_MSG_INSECURE_ENDPOINT

    dialog = SettingsDialog(ClientConfig(), None, refuse)
    dialog.base_url_edit.setText("  https://api.example.invalid/wuwaterm-api  ")
    dialog._on_accepted()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.base_url_error.is_showing() is True
    assert dialog.base_url_error.text() == strings.ERROR_MSG_INSECURE_ENDPOINT
    assert dialog.base_url_edit.property("invalid") is True
    # The address handed to the check is the one the client would use, not the
    # raw field text: a leading space reads as a relative URL to the transport.
    assert seen == ["https://api.example.invalid/wuwaterm-api"]


def test_cancelling_before_the_task_starts_restores_the_buttons(qapp) -> None:
    """A task cancelled before its first step never runs its own body."""
    import asyncio

    view = TranslateView(_dummy_client())
    view.input_edit.setPlainText("Jinhsi")

    async def scenario() -> None:
        view._on_translate_clicked()
        # No await in between: the task has been scheduled and has not run.
        view._on_cancel_clicked()
        await asyncio.sleep(0.01)

    asyncio.run(scenario())

    assert view.translate_button.isEnabled()
    assert not view.cancel_button.isEnabled()
    assert view.status_label.text() == message_for("cancelled")


def test_a_status_refresh_does_not_start_while_one_is_running(qapp) -> None:
    """The button is disabled inside the coroutine, which has not run yet.

    Unlike term lookup, this area has NOT moved to replacement: a refresh is
    asked for deliberately, one press at a time, so a second one while the
    first is in flight is a double-click rather than a newer question.
    """
    import asyncio

    view = StatusView(_dummy_client())

    class _Pending:
        def done(self) -> bool:
            return False

    view._task = _Pending()
    started = []
    original = asyncio.ensure_future
    try:
        asyncio.ensure_future = lambda *args, **kwargs: started.append(args) or _Pending()
        view._on_refresh_clicked()
    finally:
        asyncio.ensure_future = original

    assert started == []


def test_settings_that_cannot_be_written_still_apply_and_say_so(
    qapp, monkeypatch
) -> None:
    """A save that fails must not be silent, and must not lose the session."""
    from PySide6.QtWidgets import QDialog

    from wuwaterm_client import strings
    from wuwaterm_client.config import ClientConfig
    from wuwaterm_client.ui import main_window as main_window_module

    monkeypatch.setattr(main_window_module, "has_token", lambda: True)

    window = MainWindow(ClientConfig(base_url="http://127.0.0.1:8787"))
    changed = ClientConfig(base_url="http://127.0.0.1:9999", request_timeout_seconds=42.0)

    def refuse_to_save(self, base_dir=None):
        raise OSError("read-only")

    monkeypatch.setattr(ClientConfig, "save", refuse_to_save)

    class _AcceptedDialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def result_config(self):
            return changed

    monkeypatch.setattr(main_window_module, "SettingsDialog", _AcceptedDialog)
    window._on_settings_clicked()

    assert window.global_banner.is_showing(), (
        "the owner must be told the settings were not kept"
    )
    assert window.global_banner.message_text == strings.SETTINGS_NOT_SAVED_MESSAGE
    # ...and they really did take effect for this session, which is the other
    # half of the sentence the banner has to be telling the truth about.
    assert window.api_client._timeout == 42.0
    assert window.config.base_url == "http://127.0.0.1:9999"


# -- Codex P2 回归门(PR #63 评审发现) --------------------------------------


def test_starting_a_translation_takes_down_the_previous_answer(qapp) -> None:
    """上一条译文不能留在新请求底下。

    清理只清了徽章、附注、提示条与请求 ID,唯独没清译文本身 —— 于是旧答案原样
    留在新原文下方,却已经失去了「它从哪来」和「哪次请求produced它」的全部标记,
    正好会被读成当前输入的回答。
    """
    from wuwaterm_client.api import TranslationResult

    view = TranslateView(_dummy_client())
    view._show_result(
        TranslationResult(
            kind="exact", text="Resonance Circuit", direction="en",
            dictionary_miss=False, request_id="req-1",
        )
    )
    assert view.result_edit.toPlainText() == "Resonance Circuit"

    view._clear_outcome_surfaces()

    assert view.result_edit.toPlainText() == "", "旧译文仍留在屏幕上"
    assert view.kind_badge.isVisibleTo(view) is False


def test_every_area_can_offer_the_token_action(qapp, tmp_path, monkeypatch) -> None:
    """分派表把 unauthorized / forbidden 归类为「输入新令牌」。

    在此之前没有任何视图能画出这个动作:凭据对话框归主窗口所有,而三个视图都
    没有拿到入口,于是分派表宣称了一个谁也交付不了的按钮。
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    window = MainWindow(config=ClientConfig())

    assert callable(window.enter_token)
    for view in (window.translate_view, window.terms_view, window.status_view):
        assert view._on_enter_token is not None, (
            f"{type(view).__name__} 拿不到令牌入口,分派表的动作无人交付"
        )
        assert view._on_enter_token == window.enter_token

    # ...而且它必须真的能跑。第一版这条测试只断了「回调接上了」,于是接上的
    # 是一个一按就抛 NameError 的槽(TokenDialog 没有被 import),测试照绿。
    # 断机制不断结果的教训,原样发生在写这条测试的人身上。
    # 对话框在这里被替换成一个立即取消的替身:要验的是这个方法能走完,不是
    # 一个模态窗能不能在测试里弹出来。
    import wuwaterm_client.ui.main_window as main_window_module

    class _CancelledDialog:
        def __init__(self, parent=None) -> None:
            pass

        def exec(self) -> int:
            return int(QDialog.DialogCode.Rejected)

        def token(self) -> str:  # pragma: no cover - 取消路径不会读它
            return ""

    monkeypatch.setattr(main_window_module, "TokenDialog", _CancelledDialog)
    window.enter_token()  # 不得抛出


# -- 第三轮评审的三条就地修(其余降范围退出本 PR)---------------------------


def test_translate_submit_is_disabled_without_an_endpoint(qapp, tmp_path, monkeypatch) -> None:
    """未配置时三个区的提交动作都必须是灰的,翻译区曾经漏了。

    传输层本来就会拒绝 not_configured,所以这不是安全边界;它是「这个界面说得清
    自己会做什么」与「得按一下才知道」的区别 —— 而未配置态整屏的意思就是不会发出
    任何请求。
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    window = MainWindow(config=ClientConfig())

    assert window.translate_view.translate_button.isEnabled() is False
    assert window.terms_view.search_button.isEnabled() is False
    assert window.status_view.refresh_button.isEnabled() is False

    # 配置好之后必须解禁,否则这条门会把一个可用的界面锁死。
    window.api_client.update_base_url("https://example.invalid/api")
    window.translate_view._apply_endpoint_state()
    assert window.translate_view.translate_button.isEnabled() is True


def test_ctrl_k_puts_the_caret_in_each_area_input(qapp, tmp_path, monkeypatch) -> None:
    """Ctrl+K 必须真的把焦点交给该区的输入部件。

    这是 Issue #66 那条规则的第一次实战应用:不断「快捷键接上了」,断**按下之后
    焦点落在哪**。此前 focus_current_input 找一个名为 focus_input 的方法,而三个
    视图一个都没定义 —— 快捷键接得好好的,按下去焦点落在页面容器上,光标不出现。

    断的是 page.focusWidget(),不是 widget.hasFocus():offscreen 平台从不激活窗口,
    hasFocus 因此恒为假,那样断出来的只是平台限制,不是这个功能的对错。
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    # 已配置:未配置时刷新按钮是禁用的,而禁用的控件本来就不接焦点 —— 那是正确
    # 行为,不该被这条门算作失败。
    window = MainWindow(config=ClientConfig(base_url="https://example.invalid/api"))
    window.show()

    expected = {
        0: window.terms_view.query_edit,
        1: window.translate_view.input_edit,
        2: window.status_view.refresh_button,
    }
    for index, widget in expected.items():
        page = window.stack.widget(index)
        assert callable(getattr(page, "focus_input", None)), (
            f"第 {index} 页没有定义 focus_input,快捷键会落到页面容器上"
        )
        window.show_page(index)
        window.focus_current_input()
        assert page.focusWidget() is widget, (
            f"第 {index} 页按下 Ctrl+K 后焦点在 "
            f"{type(page.focusWidget()).__name__},不是 {type(widget).__name__}"
        )
    window.hide()


def test_the_window_minimum_fits_a_small_workspace(qapp, tmp_path, monkeypatch) -> None:
    """最小尺寸不得大于屏幕能给的工作区。

    它是设备无关像素:1366x768 面板在 150% 缩放下只有约 911x512,在 200% 下约
    683x384。一个大于工作区的下限没法被满足 —— 窗口开得过高,而这里没有滚动,
    底部按钮会被挤出屏幕且够不着。
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    window = MainWindow(config=ClientConfig())

    screen = window.screen() or QApplication.primaryScreen()
    if screen is None:  # pragma: no cover - offscreen 平台无屏可量
        import pytest as _pytest

        _pytest.skip("no screen to measure")
    workspace = screen.availableGeometry()
    minimum = window.minimumSize()

    assert minimum.width() <= workspace.width(), (
        f"最小宽 {minimum.width()} 超过工作区 {workspace.width()}"
    )
    assert minimum.height() <= workspace.height(), (
        f"最小高 {minimum.height()} 超过工作区 {workspace.height()}"
    )
    # 默认尺寸同样不得开出屏外。
    assert window.width() <= workspace.width()
    assert window.height() <= workspace.height()
