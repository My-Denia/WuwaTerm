"""Shared pytest setup.

Sets QT_QPA_PLATFORM=offscreen before any PySide6 import so Qt-constructing
tests run headlessly, without a real display. The API, configuration,
credential and error tests do not need Qt or its event loop.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
