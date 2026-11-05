"""Entry point wiring: PySide6 + qasync so httpx runs on the Qt event loop
and an in-flight request can be cancelled from the UI thread."""

from __future__ import annotations

import asyncio
import sys

import qasync
from PySide6.QtWidgets import QApplication

from . import i18n, strings, theme
from .config import ClientConfig
from .ui.main_window import MainWindow


SELF_CHECK_FLAG = "--self-check"

# Qt 6.7 起 Windows 11 上的默认样式是 windows11,而样式表只覆盖它被写到的那
# 些部件——没被覆盖的部件会保留平台原生外观,于是同一个窗口里出现两种设计语
# 言。统一到一个基底样式,这个变量就消失了。
BASE_STYLE_NAME = "Fusion"


def run(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv)
    self_check = SELF_CHECK_FLAG in arguments
    if self_check:
        arguments = [item for item in arguments if item != SELF_CHECK_FLAG]

    app = QApplication(arguments)
    app.setApplicationName(strings.APP_TITLE)
    app.setStyle(BASE_STYLE_NAME)

    # 在构造任何部件之前:Qt 部件在创建时就取好自己的默认文案,右键菜单是现
    # 用现取所以晚装也来得及,但标准按钮盒不是。这里装,后面就没有先英文再中
    # 文的窗口。装的是 Qt 自带部件的那批字(右键菜单、标准按钮),这个程序自
    # 己写的字全部来自 strings.py,与它无关。
    i18n.install_qt_translations(app)

    # 只读一次配置,而且只为了取外观偏好:主窗口有它自己的配置生命周期,这里
    # 不介入。样式在构造主窗口之前下发,窗口第一次显示就是最终外观,不会先闪
    # 一下无样式。
    theme.apply_theme(app, ClientConfig.load().appearance)
    # 跟随系统是默认值,所以热切换在默认路径上就要工作;固定亮/暗时这个回调
    # 会用记下来的偏好重渲染,等于什么都没变。
    theme.follow_system_scheme(app)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow()

    if self_check:
        # Start-up rehearsal for a packaged build: everything a normal start
        # imports and constructs has now been imported and constructed, and
        # nothing has been shown, no credential has been requested and no
        # request has been sent. A frozen build that cannot import its own
        # package, or is missing a Qt plugin, fails before this point, which
        # is what makes the artifact testable without a human at the screen.
        window.close()
        loop.close()
        return 0

    window.show()

    app_close_event = asyncio.Event()
    app.aboutToQuit.connect(app_close_event.set)

    with loop:
        loop.run_until_complete(app_close_event.wait())
    return 0
