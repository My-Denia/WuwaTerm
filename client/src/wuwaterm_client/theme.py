"""设计令牌,以及由令牌渲染出的两套样式表。

Qt 样式表没有变量机制:一个颜色在样式表里只能写成字面值,而这套界面要求
亮暗两套取值一一对位。因此令牌在这里以 Python 字典定义,`.qss` 文件写成带
占位符的模板,加载时替换后整串下发。切换主题就是换一套令牌重新渲染。

占位符写成 `@token-name@` 而不是 `{token}`:QSS 的每一条规则都被花括号包着,
`str.format` 会把选择器的花括号当成自己的字段并报错,于是模板里每一个花括号
都得转义——那是一条只要有人漏转义就会静默产生半截样式的路。`@` 在 QSS 语法
里没有含义,用它做定界符就不需要转义任何 QSS 原文。

资源缺失时这里返回空串而不是抛异常。样式是装饰,不是功能:打包产物的
`--self-check` 会完整构造主窗口并要求 0 退出,一个读不到 `.qss` 的构建应该
表现为"没有样式",而不是"启动失败"。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 三态偏好的取值。与 config.APPEARANCE_* 是同样的字面值,但这里不 import
# config:主题渲染要能在没有 httpx、没有 Qt 的环境里被测到(资源链条的测试就是
# 这样跑的),而 config 会拉进 httpx。两处的字面值由 test_theme_resources.py
# 比对,不靠约定。
SCHEME_LIGHT = "light"
SCHEME_DARK = "dark"
SCHEME_SYSTEM = "system"

RESOURCE_DIR_NAME = "resources"
STYLESHEET_FILE_NAMES = {
    SCHEME_LIGHT: "theme_light.qss",
    SCHEME_DARK: "theme_dark.qss",
}

# 字体与字号在两套配色里取值相同,但仍然是令牌:模板里不出现字面值,是
# "亮暗两份占位符集合必须完全一致"这条测试能成立的前提。
_TYPOGRAPHY_TOKENS = {
    "font-family": (
        '"Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif'
    ),
    "font-family-mono": '"Cascadia Mono", "Consolas", "Courier New", monospace',
    "font-size-display": "18pt",
    "font-size-title": "14pt",
    "font-size-section": "12pt",
    "font-size-context": "13.5pt",
    "font-size-body": "10.5pt",
    "font-size-secondary": "9.5pt",
    "font-size-caption": "9pt",
}

LIGHT_TOKENS = {
    **_TYPOGRAPHY_TOKENS,
    "bg-canvas": "#F0F5F4",
    "bg-surface": "#FFFFFF",
    "bg-sunken": "#E8F0EF",
    "border-subtle": "#D2DEDD",
    "border-strong": "#A8BEBC",
    "text-primary": "#192C30",
    "text-secondary": "#4E6569",
    "text-muted": "#657B7E",
    "accent": "#087E75",
    "accent-hover": "#096B64",
    "accent-fg": "#FFFFFF",
    "accent-soft-bg": "#DAEFEB",
    "accent-soft-fg": "#08675F",
    "success": "#17794F",
    "success-soft": "#E6F4EC",
    "warn": "#925C00",
    "warn-soft": "#FDF1DC",
    "danger": "#B4232B",
    "danger-soft": "#FCE9E9",
    "info": "#1F5FA8",
    "info-soft": "#E7F0FB",
    "focus-ring": "#087E75",
}

DARK_TOKENS = {
    **_TYPOGRAPHY_TOKENS,
    "bg-canvas": "#10191D",
    "bg-surface": "#182328",
    "bg-sunken": "#111C21",
    "border-subtle": "#304047",
    "border-strong": "#485B60",
    "text-primary": "#E7EFF0",
    "text-secondary": "#B2C0C2",
    "text-muted": "#91A3A5",
    "accent": "#34C4B6",
    "accent-hover": "#58D5C8",
    "accent-fg": "#0B2523",
    "accent-soft-bg": "#183B3A",
    "accent-soft-fg": "#79DED2",
    "success": "#4ECB8C",
    "success-soft": "#142A20",
    "warn": "#E0A64A",
    "warn-soft": "#33270F",
    "danger": "#F2727A",
    "danger-soft": "#3A1B1E",
    "info": "#6FA8E8",
    "info-soft": "#14243A",
    "focus-ring": "#34C4B6",
}

TOKENS_BY_SCHEME = {
    SCHEME_LIGHT: LIGHT_TOKENS,
    SCHEME_DARK: DARK_TOKENS,
}

_PLACEHOLDER = re.compile(r"@([a-z0-9-]+)@")

# 当前生效的偏好。系统主题变化时要重新渲染,而"渲染成哪一套"取决于用户选的
# 是跟随系统还是固定亮/暗——回调拿不到那个值,所以记在这里。样式表本来就是
# 应用级的单一状态,这个模块级变量描述的正是它。
_preference = SCHEME_SYSTEM

# 已连上系统主题信号的回调。连接持有的是一个闭包,而闭包只被这次连接引用;
# 留一份强引用,免得它在下一次垃圾回收时消失、信号变成空转。
_CONNECTED_HANDLERS: list[object] = []


def _resource_roots() -> list[Path]:
    """样式表可能所在的目录,按优先级排列。

    PyInstaller 把 datas 解包到 `sys._MEIPASS`,而源码运行时资源就在包目录
    下面。两条路都要能走通,否则要么开发机没样式,要么打包产物没样式,而这
    两种情况各自都能在另一种下通过检查。
    """
    roots: list[Path] = []
    frozen = getattr(sys, "_MEIPASS", None)
    if isinstance(frozen, str) and frozen:
        roots.append(Path(frozen) / RESOURCE_DIR_NAME)
    roots.append(Path(__file__).parent / RESOURCE_DIR_NAME)
    return roots


def resource_path(file_name: str) -> Path | None:
    """第一个真实存在的候选路径,都不存在时返回 None。"""
    for root in _resource_roots():
        candidate = root / file_name
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            # 不可读的父目录、被拒绝的 ACL:都不是"文件在这里",继续找下一个。
            continue
    return None


def load_stylesheet(scheme: object) -> str:
    """渲染好的样式表整串,资源缺失时是空串。永不抛异常。

    未知的 scheme 按亮色处理:这是显示层的降级,不是安全判断,给不出样式远
    比拒绝启动糟糕得多。
    """
    # `in` 前面的 isinstance 不是多余的:字典的成员判断要对键做哈希,一个
    # 列表传进来会在这里抛 TypeError,而这个函数的契约是永不抛异常。
    key = scheme if isinstance(scheme, str) and scheme in TOKENS_BY_SCHEME else SCHEME_LIGHT
    tokens = TOKENS_BY_SCHEME[key]
    path = resource_path(STYLESHEET_FILE_NAMES[key])
    if path is None:
        return ""
    try:
        template = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    # 认不出的占位符原样留下,不是被吞掉:test_theme_resources.py 断言渲染结果
    # 里没有残留占位符,所以一个写错的令牌名会在 CI 里变红,而不是变成一条
    # 悄悄失效的规则。
    return _PLACEHOLDER.sub(lambda match: tokens.get(match.group(1), match.group(0)), template)


def _system_scheme() -> str:
    """系统当前是亮还是暗;问不出来就当亮色。

    `QStyleHints.colorScheme()` 是 Qt 6.5 起才有的,而本项目的下限是
    PySide6 6.7——够用,但仍然用 getattr 探测:这条路径在没有 QApplication
    实例、甚至没装 Qt 的进程里也会被调用(资源测试),探测比 try/import 更能
    说清"这里允许没有"。
    """
    try:
        from PySide6.QtGui import QGuiApplication
    except Exception:  # pragma: no cover - 没有 Qt 的环境
        return SCHEME_LIGHT
    application = QGuiApplication.instance()
    if application is None:
        return SCHEME_LIGHT
    hints_getter = getattr(application, "styleHints", None)
    if hints_getter is None:
        return SCHEME_LIGHT
    try:
        hints = hints_getter()
        color_scheme_getter = getattr(hints, "colorScheme", None)
        if color_scheme_getter is None:
            return SCHEME_LIGHT
        value = color_scheme_getter()
    except Exception:
        return SCHEME_LIGHT
    # 枚举成员的名字("Light" / "Dark" / "Unknown")而不是枚举本身:比较名字
    # 不需要 import Qt 的枚举类型,Unknown 也就自然落到亮色。
    name = getattr(value, "name", None) or str(value)
    return SCHEME_DARK if "Dark" in name else SCHEME_LIGHT


def resolve_scheme(config_value: object) -> str:
    """把三态偏好折算成实际要渲染的那一套,只会返回 "light" 或 "dark"。

    非法值按跟随系统处理,和 config 里对同一个键的回落方向一致。
    """
    if config_value == SCHEME_DARK:
        return SCHEME_DARK
    if config_value == SCHEME_LIGHT:
        return SCHEME_LIGHT
    return _system_scheme()


def current_scheme() -> str:
    """Return the rendered scheme without changing the saved preference."""
    return resolve_scheme(_preference)


def _normalized_preference(config_value: object) -> str:
    if config_value in (SCHEME_LIGHT, SCHEME_DARK, SCHEME_SYSTEM):
        return str(config_value)
    return SCHEME_SYSTEM


def apply_theme(app: object, config_value: object) -> None:
    """按偏好渲染并下发样式表,同时记住这个偏好。永不抛异常。

    记住偏好是为了 `follow_system_scheme`:系统主题变了要重渲染,而重渲染成
    哪一套取决于用户选的是跟随系统还是固定亮/暗。设置里改了主题的调用点只要
    再调一次本函数即可,不需要另外通知谁。
    """
    global _preference
    _preference = _normalized_preference(config_value)
    stylesheet = load_stylesheet(resolve_scheme(_preference))
    setter = getattr(app, "setStyleSheet", None)
    if setter is None:
        return
    try:
        setter(stylesheet)
    except Exception:
        # 样式是装饰。它不该成为启动排练失败的理由。
        return


def follow_system_scheme(app: object) -> bool:
    """连上系统主题变化信号做热切换;连上了返回 True。

    用能力探测而不是版本判断:`colorSchemeChanged` 自 Qt 6.5 起存在,低版本
    没有这个信号时应用照常运行,只是不跟随。这里刻意不用 Qt 6.8 才有的
    `setColorScheme`——本项目允许 PySide6 6.7。
    """
    hints_getter = getattr(app, "styleHints", None)
    if hints_getter is None:
        return False
    try:
        hints = hints_getter()
    except Exception:
        return False
    signal = getattr(hints, "colorSchemeChanged", None)
    if signal is None:
        return False

    def _on_system_scheme_changed(*_arguments: object) -> None:
        apply_theme(app, _preference)

    try:
        signal.connect(_on_system_scheme_changed)
    except Exception:
        return False
    _CONNECTED_HANDLERS.append(_on_system_scheme_changed)
    return True
