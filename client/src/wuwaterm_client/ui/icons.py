"""Palette-aware Lucide icons for desktop controls.

The SVGs are vendored with their license and pinned source under resources/icons.
Sizes belong to the control (``setIconSize``); an icon can therefore be reused
at different sizes and continues to follow the active appearance setting.
"""

from __future__ import annotations

import math
import re

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .. import theme

_ICON_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
# These familiar names were renamed in the pinned Lucide source. Keep the
# upstream SVGs unmodified and resolve callers to their current filenames.
_ALIASES = {"history": "rotate-ccw-clock", "waves": "waves-horizontal"}


class _ThemedIconEngine(QIconEngine):
    """Resolve icon colors at paint time so theme changes reach existing icons."""

    def __init__(self, source: bytes, color: str | None) -> None:
        super().__init__()
        self._source = source
        self._color = color
        self._renderers: dict[str, QSvgRenderer] = {}

    def clone(self) -> _ThemedIconEngine:
        return _ThemedIconEngine(self._source, self._color)

    def _renderer(self, mode: QIcon.Mode, state: QIcon.State) -> QSvgRenderer:
        tokens = theme.TOKENS_BY_SCHEME[theme.current_scheme()]
        if mode == QIcon.Mode.Disabled:
            chosen = tokens["text-muted"]
        elif self._color is not None:
            chosen = tokens.get(self._color, self._color)
        elif state == QIcon.State.On:
            chosen = tokens["accent"]
        else:
            chosen = tokens["text-primary"]
        color = QColor(chosen)
        if not color.isValid():
            color = QColor(tokens["text-primary"])
        name = color.name()
        if name not in self._renderers:
            svg = self._source.replace(b"currentColor", name.encode("ascii"))
            self._renderers[name] = QSvgRenderer(svg)
        return self._renderers[name]

    def paint(
        self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State
    ) -> None:
        self._renderer(mode, state).render(painter, QRectF(rect))

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(
        self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float
    ) -> QPixmap:
        pixels = QSize(math.ceil(size.width() * scale), math.ceil(size.height() * scale))
        pixmap = QPixmap(pixels)
        pixmap.setDevicePixelRatio(scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return pixmap


def icon(name: str, color: str | None = None) -> QIcon:
    """Return a Lucide icon, or a null icon when the named resource is absent.

    ``color`` may be a CSS color or a theme token such as ``accent-fg``.
    Without an override, checked controls use the theme's accent, normal
    controls use its primary text color, and disabled controls use muted text.
    """
    if not isinstance(name, str) or _ICON_NAME.fullmatch(name) is None:
        return QIcon()
    name = _ALIASES.get(name, name)
    path = theme.resource_path(f"icons/{name}.svg")
    if path is None:
        return QIcon()
    try:
        source = path.read_bytes()
    except OSError:
        return QIcon()
    if not QSvgRenderer(source).isValid():
        return QIcon()
    return QIcon(_ThemedIconEngine(source, color))
