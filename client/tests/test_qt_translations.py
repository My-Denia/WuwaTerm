"""Qt 自带部件上的字也必须是中文。

输入框右键菜单里的撤销、剪切、复制、粘贴和全选,以及标准按钮盒的默认
按钮名,都由 Qt 自己提供,不会经过应用的 setText 调用。

所以这里断的是**结果**:菜单上出现的是不是中文。不断「翻译器装上了没有」,
因为装上了但文件是错的域、Qt 换了文件名、打包漏了目标目录,每一种都是装上了
而菜单仍是英文。断结果的测试对这些一视同仁。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit  # noqa: E402

from wuwaterm_client import i18n  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app

CLIENT_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = CLIENT_ROOT / "WuwaTerm.spec"

# 一个 CJK 统一表意文字。菜单项里只要有一个,这一项就不是英文。
CJK = re.compile(r"[一-鿿]")

# Qt 未翻译时这些部件菜单上的原文。逐条列出而不是只看有没有中文,是因为
# 「菜单是空的」也会让「没有英文」成立。
ENGLISH_MENU_ENTRIES = ("Undo", "Redo", "Cut", "Copy", "Paste", "Select All")


def _menu_texts(widget) -> list[str]:
    """右键菜单上的文案,去掉助记符与快捷键那一段。"""
    menu = widget.createStandardContextMenu()
    texts = []
    for action in menu.actions():
        text = action.text()
        if not text:
            continue  # 分隔线
        texts.append(text.replace("&", "").split("\t")[0].strip())
    return texts


def test_the_translation_file_is_found_where_the_application_looks() -> None:
    """先证明找得到,后面的失败才好归因。"""
    found = i18n.find_translation_file()
    assert found is not None, (
        "找不到 qtbase 的中文翻译文件;搜索路径:"
        + ", ".join(str(p) for p in i18n.translation_search_paths())
    )
    assert found.name == i18n.TRANSLATION_FILE_NAME
    assert found.is_file()


@pytest.mark.parametrize("factory", (QLineEdit, QPlainTextEdit))
def test_the_input_context_menu_is_chinese(qapp, factory) -> None:
    """右键菜单是这个洞最早露出来的地方,也是它最容易复发的地方。

    两种输入部件都测:查词框是 QLineEdit(选它是因为中文输入法的候选阶段不改
    变 text),原文框是 QPlainTextEdit,两者的菜单来自不同的类。
    """
    assert i18n.install_qt_translations(qapp) is True

    texts = _menu_texts(factory())

    assert texts, "菜单是空的,这样『没有英文』会毫无意义地成立"
    for entry in ENGLISH_MENU_ENTRIES:
        assert entry not in texts, f"右键菜单上仍然是英文:{entry!r}(全部:{texts})"
    assert any(CJK.search(text) for text in texts), f"菜单里没有一个中文:{texts}"


def test_the_spec_packages_the_translation() -> None:
    """源码跑得对,打包产物里没有,是这一类缺陷最典型的下场。

    与 test_theme_resources.py 里那条样式表打包断言同一个理由:--self-check
    不检查菜单上的字,所以漏配只会表现为产物里一半中文一半英文,而开发机上
    一切正常。
    """
    spec = SPEC_PATH.read_text(encoding="utf-8")

    assert "qtbase_zh_CN.qm" in spec
    # 目标目录是运行时靠 Qt 自己报告的翻译路径找到它的地方;写错了就等于没打。
    assert "PySide6/translations" in spec
    assert "QT_TRANSLATION_DATAS" in spec
    import ast

    tree = ast.parse(spec)
    analysis_calls = [node for node in ast.walk(tree)
                      if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Name)
                      and node.func.id == "Analysis"]
    assert analysis_calls
    datas = next(keyword.value for keyword in analysis_calls[0].keywords
                 if keyword.arg == "datas")
    assert "QT_TRANSLATION_DATAS" in {
        node.id for node in ast.walk(datas) if isinstance(node, ast.Name)
    }


def test_the_spec_can_actually_find_the_translation_on_this_install() -> None:
    """断结果:spec 找的那些目录里,**真的有**一个存在的 .qm。

    上一条断的是 spec 的文本长什么样——那是机制。一个把源路径写错的 spec 完全
    可以通过它:字符串都在,而 `is_file()` 返回 False,`QT_TRANSLATION_DATAS`
    静默变成空列表。源码运行时仍然是中文(i18n 另有三处搜索路径都能兜住),
    只有打出来的包是英文菜单——本 PR 的四个「字未经 strings.py 就上屏」出口,
    每一个都是这种形状。

    这里因此复算 spec 的候选目录,断言其中至少一个在这台机器上真实存在。
    PySide6 的轮子在不同版本/平台上把 .qm 放在 PySide6/translations 或
    PySide6/Qt/translations,所以两个都要在 spec 里列出。
    """
    import re

    import PySide6

    spec = SPEC_PATH.read_text(encoding="utf-8")
    match = re.search(r"_QT_TRANSLATION_DIRS\s*=\s*\(([^)]*)\)", spec)
    assert match, "spec 不再声明候选目录,这条断言失去了对象"
    candidates = re.findall(r"[\"']([^\"']+)[\"']", match.group(1))
    assert "translations" in candidates and "Qt/translations" in candidates, (
        f"spec 只找了 {candidates};另一种轮子构型会静默打不进翻译"
    )

    root = Path(PySide6.__file__).parent
    found = [rel for rel in candidates if (root / rel / "qtbase_zh_CN.qm").is_file()]
    assert found, (
        f"spec 会打包的候选目录在这台机器上一个都不存在:{candidates};"
        f"实际的 PySide6 根目录是 {root}"
    )


def test_a_missing_translation_is_reported_rather_than_raised(monkeypatch, qapp) -> None:
    """菜单是英文的窗口仍然可用;打不开的窗口不可用。

    翻译文件缺失把一个措辞问题升级成可用性问题,是比英文菜单更坏的结果,所以
    安装路径返回 False 而不是抛异常。让它不声不响的那部分风险,由上面那条断
    菜单是中文的测试兜住。
    """
    monkeypatch.setattr(i18n, "find_translation_file", lambda: None)

    assert i18n.install_qt_translations(qapp) is False


def test_an_unreadable_candidate_directory_does_not_stop_the_client(monkeypatch) -> None:
    """一个候选目录读不动,不能变成客户端起不来。

    app.run() 在构造窗口之前调用 install_qt_translations,且不捕获异常。所以
    候选目录上的 is_file() 一旦抛 OSError(拒绝性 ACL、瞬时故障、断开的网络
    驱动器),代价就不是「菜单是英文」而是「程序打不开」——用一个可选的本地化
    资源换掉了整个可用性。theme.py 的资源查找一直是包着 try/except OSError 的,
    这里曾经不是。

    断的是结果:第一个候选目录爆掉之后,**后面的候选仍然被走到**,并且函数
    正常返回而不是抛出。
    """
    real = Path.is_file
    exploded: list[str] = []

    def is_file(self):
        if self.parent.name == "boom":
            exploded.append(str(self))
            raise OSError(5, "denied by ACL")
        return real(self)

    good = Path(i18n.__file__).parent / "resources"
    monkeypatch.setattr(
        i18n, "translation_search_paths", lambda: [Path("boom"), good]
    )
    monkeypatch.setattr(Path, "is_file", is_file)

    found = i18n.find_translation_file()

    assert exploded, "前置条件没建立:那个会爆的候选根本没被探到"
    assert found is None or found.parent == good, (
        "爆掉的候选之后,搜索没有继续走到下一个"
    )
