"""Qt 自带部件的中文化。

这个模块处理 Qt 自己提供的文案:输入框右键菜单中的撤销、剪切、复制、
粘贴和全选,以及标准按钮盒和消息框的默认按钮名。这些文字默认是英文,
不会经过应用自己的 setText 调用。

修法是装上 Qt 自己的中文翻译。它随 PySide6 一起分发(qtbase_zh_CN.qm),
所以不引入任何需要联网的资源,客户端仍然本地自足。

**显式设定的文案优先。** 装了翻译之后,QDialogButtonBox 的 Ok/Cancel 也会变成
Qt 译的"确定/取消";但设置对话框仍然自己 setText 一次,因为那两个按钮的措辞是
这个程序要负责的,不该随 Qt 的译法变动。翻译负责的是我们枚举不完的长尾。

装不上不会让程序起不来——一个能用但菜单是英文的窗口,好过一个打不开的窗口。
但它也不能悄悄地就这么算了:client/tests/test_qt_translations.py 直接断言
右键菜单里出现的是中文,断的是**结果**而不是机制,所以无论是文件没打进包、
路径找错了还是 Qt 换了文件名,都会红。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

# Qt 把 QLineEdit / QPlainTextEdit 的右键菜单、标准按钮这些文案放在 qtbase 这
# 个翻译域里。其余几个域(qtdeclarative 等)这个程序没有用到的部件,不装。
TRANSLATION_DOMAIN = "qtbase"
CHINESE_LOCALE = "zh_CN"
TRANSLATION_FILE_NAME = f"{TRANSLATION_DOMAIN}_{CHINESE_LOCALE}.qm"

# 装上的翻译器要有人持有:QApplication.installTranslator 不取所有权,局部变量
# 一出作用域就被回收,菜单会不声不响地变回英文。
_installed: list[QTranslator] = []


def translation_search_paths() -> list[Path]:
    """按顺序找 .qm 的三个位置。

    第一个是 Qt 自己报告的翻译目录——打包之后它指向产物内部,这也是打包路径
    唯一需要对上的地方。第二个是 PySide6 包自己的 translations 目录,源码运行
    时走这条。第三个是这个包的 resources,留给"把 .qm 复制进来自带一份"的将来,
    现在是空的,不作为依赖。
    """
    paths: list[Path] = []
    reported = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if reported:
        paths.append(Path(reported))
    try:
        import PySide6

        paths.append(Path(PySide6.__file__).parent / "translations")
    except Exception:
        # PySide6 不可导入的话这个模块根本不会被执行到;写在这里只是为了让这
        # 个函数在任何情况下都返回一个列表而不是抛出。
        pass
    paths.append(Path(__file__).parent / "resources")
    return paths


def find_translation_file() -> Path | None:
    """第一个真实存在的 qtbase_zh_CN.qm,找不到返回 None。"""
    for directory in translation_search_paths():
        candidate = directory / TRANSLATION_FILE_NAME
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            # A denying ACL or a transient filesystem error on ONE candidate
            # must not end the search, and must not end the program. This runs
            # from app.run() before the window is built, so an exception here
            # is a client that will not start - over an optional localization
            # resource whose absence costs an English context menu.
            # theme.py:141-143 already guards its own lookup this way.
            continue
    return None


def install_qt_translations(app: object) -> bool:
    """给 Qt 自带部件装上中文,返回是否装成功。

    失败只是返回 False:菜单是英文的窗口仍然可用,而一个因为翻译文件缺失就打不
    开的客户端,是把一个措辞问题升级成了可用性问题。测试负责让这件事不会悄悄
    发生。
    """
    path = find_translation_file()
    if path is None:
        return False
    translator = QTranslator()
    # load(路径) 要的是不带后缀的文件名加目录;直接给全路径也可以,但分开写
    # 的形式在 Qt 各版本上行为一致。
    if not translator.load(path.stem, str(path.parent)):
        return False
    if not app.installTranslator(translator):
        return False
    _installed.append(translator)
    return True


def system_is_chinese() -> bool:
    """这台机器的系统语言是不是中文。

    这个程序的界面只有中文,所以这个判断**不用来决定装不装翻译**——它在任何
    系统语言下都装。留着这个函数是为了让下面这句话有地方说清楚:界面语言不跟
    随系统,是产品决定,不是漏掉的功能。
    """
    return QLocale.system().language() == QLocale.Language.Chinese
