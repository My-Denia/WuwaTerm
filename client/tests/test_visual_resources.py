"""Exercise the packaged icon chain and the workbench's two palettes."""

from __future__ import annotations

import hashlib
import runpy
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QIcon, QPalette  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QFrame, QLabel, QListWidget, QPlainTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from wuwaterm_client import theme  # noqa: E402
from wuwaterm_client.ui.icons import icon  # noqa: E402

CLIENT_ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = CLIENT_ROOT / "src" / "wuwaterm_client" / "resources" / "icons"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def preserve_theme(qapp, monkeypatch):
    previous_stylesheet = qapp.styleSheet()
    monkeypatch.setattr(theme, "_preference", theme._preference)
    yield
    qapp.setStyleSheet(previous_stylesheet)


def _visible_color(image):
    """Choose an opaque stroke pixel rather than a transparent canvas pixel."""
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() >= 240:
                return pixel
    pytest.fail("the icon did not paint a visible stroke")


def _assert_color(actual, expected):
    # Antialiasing/unpremultiplication can round a channel by one.
    assert max(abs(a - b) for a, b in zip(actual.getRgb()[:3], expected.getRgb()[:3])) <= 2


def _contrast(first, second):
    def luminance(color):
        channels = (color.redF(), color.greenF(), color.blueF())
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_spec_stages_every_icon_and_license_and_icons_render(qapp, tmp_path, monkeypatch):
    """Exercise the actual spec datas, then load and paint from its frozen path."""
    analysis_args = {}

    def analysis(*args, **kwargs):
        analysis_args.update(kwargs)
        return SimpleNamespace(pure=[], zipped_data=[], scripts=[], binaries=[], zipfiles=[], datas=[])

    namespace = runpy.run_path(
        str(CLIENT_ROOT / "WuwaTerm.spec"),
        init_globals={
            "SPECPATH": str(CLIENT_ROOT),
            "Analysis": analysis,
            "PYZ": lambda *args, **kwargs: None,
            "EXE": lambda *args, **kwargs: None,
            "COLLECT": lambda *args, **kwargs: None,
        },
    )
    icon_datas = namespace["ICON_DATAS"]
    assert set(map(tuple, icon_datas)) <= set(map(tuple, analysis_args["datas"]))
    assert {Path(source).name for source, _ in icon_datas} == {
        path.name for path in ICON_DIR.iterdir() if path.is_file()
    }
    assert all(Path(target) == Path("resources/icons") for _, target in icon_datas)

    for source, target in namespace["RESOURCE_DATAS"] + icon_datas:
        destination = tmp_path / target
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination / Path(source).name)
    monkeypatch.setattr(theme, "_resource_roots", lambda: [tmp_path / "resources"])
    assert theme.load_stylesheet(theme.SCHEME_DARK)

    checksums = {}
    for line in (tmp_path / "resources/icons/SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        checksums[name] = digest
    icon_names = {Path(source).name for source, _ in icon_datas if source.endswith(".svg")}
    assert set(checksums) == icon_names | {"LICENSE"}
    for name, digest in checksums.items():
        assert hashlib.sha256((tmp_path / "resources/icons" / name).read_bytes()).hexdigest() == digest
    for name in icon_names:
        bundled = icon(Path(name).stem)
        assert not bundled.isNull(), name
        _visible_color(bundled.pixmap(QSize(24, 24)).toImage())
    for alias in ("history", "waves"):
        assert not icon(alias).isNull()
    assert icon("missing").isNull()
    assert icon("../LICENSE").isNull()


def test_existing_icons_follow_theme_and_control_state(qapp, preserve_theme):
    from PySide6.QtGui import QColor

    normal = icon("file-text")
    foreground = icon("refresh-cw", color="accent-fg")
    button = QToolButton()
    button.setObjectName("commandButton")
    button.setIcon(normal)
    button.setIconSize(QSize(24, 24))
    button.resize(48, 48)
    button.show()
    for scheme in (theme.SCHEME_LIGHT, theme.SCHEME_DARK):
        theme.apply_theme(qapp, scheme)
        qapp.processEvents()
        tokens = theme.TOKENS_BY_SCHEME[scheme]
        for resource, mode, state, expected in (
            (normal, QIcon.Mode.Normal, QIcon.State.Off, "text-primary"),
            (normal, QIcon.Mode.Normal, QIcon.State.On, "accent"),
            (normal, QIcon.Mode.Disabled, QIcon.State.Off, "text-muted"),
            (foreground, QIcon.Mode.Normal, QIcon.State.Off, "accent-fg"),
        ):
            image = resource.pixmap(QSize(24, 24), mode, state).toImage()
            _assert_color(_visible_color(image), QColor(tokens[expected]))
        # The same, already displayed control must repaint after a theme change.
        image = button.grab().toImage()
        expected = QColor(tokens["text-primary"])
        assert any(
            max(abs(a - b) for a, b in zip(image.pixelColor(x, y).getRgb()[:3], expected.getRgb()[:3])) <= 2
            for y in range(image.height()) for x in range(image.width())
        )
    button.close()


@pytest.mark.parametrize("scheme", [theme.SCHEME_LIGHT, theme.SCHEME_DARK])
def test_workbench_editor_and_selection_have_readable_colors(qapp, preserve_theme, scheme):
    theme.apply_theme(qapp, scheme)
    frame = QFrame()
    frame.setObjectName("editorSurface")
    layout = QVBoxLayout(frame)
    editor = QPlainTextEdit()
    label = QLabel("Secondary text")
    label.setObjectName("secondaryText")
    layout.addWidget(editor)
    layout.addWidget(label)
    frame.show()
    qapp.processEvents()
    palette = editor.palette()
    assert _contrast(palette.color(QPalette.ColorRole.Text), palette.color(QPalette.ColorRole.Base)) >= 4.5
    assert _contrast(palette.color(QPalette.ColorRole.HighlightedText), palette.color(QPalette.ColorRole.Highlight)) >= 4.5
    assert _contrast(label.palette().color(QPalette.ColorRole.WindowText), palette.color(QPalette.ColorRole.Base)) >= 4.5
    assert editor.font().pointSizeF() == 13.5
    frame.close()


@pytest.mark.parametrize("scheme", [theme.SCHEME_LIGHT, theme.SCHEME_DARK])
def test_inspector_body_and_selected_finding_use_theme(qapp, preserve_theme, scheme):
    from PySide6.QtGui import QColor

    theme.apply_theme(qapp, scheme)
    inspector = QFrame()
    inspector.setObjectName("reviewInspector")
    outer = QVBoxLayout(inspector)
    body = QWidget(inspector)
    body.setObjectName("inspectorBody")
    outer.addWidget(body)
    content = QVBoxLayout(body)
    verdict = QLabel("Review result", body)
    verdict.setObjectName("findingVerdict")
    content.addWidget(verdict)
    findings = QListWidget(body)
    findings.addItem("A finding")
    findings.setCurrentRow(0)
    content.addWidget(findings)
    inspector.show()
    qapp.processEvents()
    tokens = theme.TOKENS_BY_SCHEME[scheme]
    _assert_color(body.palette().color(QPalette.ColorRole.Window), QColor(tokens["bg-surface"]))
    image = findings.viewport().grab().toImage()
    selected_background = QColor(tokens["accent-soft-bg"])
    assert any(
        image.pixelColor(x, y) == selected_background
        for y in range(image.height()) for x in range(image.width())
    )
    assert _contrast(QColor(tokens["accent-soft-fg"]), selected_background) >= 4.5
    for value, foreground, background in (
        ("verified_constraint", "accent-soft-fg", "accent-soft-bg"),
        ("confirmed_conflict", "danger", "danger-soft"),
        ("needs_review", "warn", "warn-soft"),
        ("not_evaluated", "text-secondary", "bg-sunken"),
    ):
        verdict.setProperty("verdict", value)
        verdict.style().unpolish(verdict)
        verdict.style().polish(verdict)
        palette = verdict.palette()
        _assert_color(palette.color(QPalette.ColorRole.WindowText), QColor(tokens[foreground]))
        _assert_color(palette.color(QPalette.ColorRole.Window), QColor(tokens[background]))
        assert _contrast(palette.color(QPalette.ColorRole.WindowText), palette.color(QPalette.ColorRole.Window)) >= 4.5
    inspector.close()
