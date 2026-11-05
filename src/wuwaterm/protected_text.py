"""Protocol-neutral visible text interleaved with opaque source structure.

Adapters decide what is structural and provide its visible-context contribution.
Core never interprets the original structure or decodes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import secrets


_RESERVED_PLACEHOLDER = re.compile(r"__WUWA_HTML_[0-9a-f]{16}_[0-9]{4,}__")


class ProtectedTextIntegrityError(ValueError):
    """A model response lost, duplicated, reordered or invented structure."""


@dataclass(frozen=True)
class ProtectedStructure:
    """An opaque source string and its contribution to displayed context."""

    original: str
    visible_context: str = ""

    def __post_init__(self) -> None:
        if type(self.original) is not str or type(self.visible_context) is not str:
            raise TypeError("structure and visible context must be strings")


@dataclass(frozen=True)
class ProtectedText:
    """Alternating visible segments and opaque structures, beginning with text.

    A structure occurs between each pair of visible segments. Empty segments
    allow adjacent structures and structures at either end. The generated
    placeholders are private to this instance and never expose original bytes.
    """

    visible_segments: tuple[str, ...]
    structures: tuple[ProtectedStructure, ...] = ()
    _prefix: str = field(init=False, repr=False, compare=False)
    _placeholders: tuple[str, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.visible_segments) is not tuple or not all(
            type(segment) is str for segment in self.visible_segments
        ):
            raise TypeError("visible segments must be a tuple of strings")
        if type(self.structures) is not tuple or not all(
            type(structure) is ProtectedStructure for structure in self.structures
        ):
            raise TypeError("structures must be a tuple of ProtectedStructure")
        if len(self.visible_segments) != len(self.structures) + 1:
            raise ValueError("one more visible segment than structures is required")
        source = "".join(self.visible_segments) + "".join(
            item.original + item.visible_context for item in self.structures
        )
        # Retain the existing token spelling during the Telegram compatibility
        # period; its contents and interpretation are protocol-neutral.
        while True:
            prefix = f"__WUWA_HTML_{secrets.token_hex(8)}_"
            if prefix not in source:
                break
        object.__setattr__(self, "_prefix", prefix)
        object.__setattr__(self, "_placeholders", tuple(
            f"{prefix}{index:04d}__" for index in range(len(self.structures))
        ))

    def for_model(self, segments: tuple[str, ...] | None = None) -> str:
        """Interleave visible text with generated opaque placeholders."""
        parts = self.visible_segments if segments is None else segments
        if len(parts) != len(self.structures) + 1:
            raise ProtectedTextIntegrityError("visible segment count changed")
        output: list[str] = []
        for segment, placeholder in zip(parts, self._placeholders, strict=False):
            output.extend((segment, placeholder))
        output.append(parts[-1])
        return "".join(output)

    def restore(self, translated: str) -> ProtectedText:
        """Validate all placeholders and return translated visible segments."""
        cursor = 0
        segments: list[str] = []
        for placeholder in self._placeholders:
            if translated.count(placeholder) != 1:
                raise ProtectedTextIntegrityError("structural placeholder count changed")
            position = translated.find(placeholder, cursor)
            if position < 0:
                raise ProtectedTextIntegrityError("structural placeholder order changed")
            segments.append(translated[cursor:position])
            cursor = position + len(placeholder)
        segments.append(translated[cursor:])
        unprotected = "".join(segments)
        if self._prefix in unprotected or _RESERVED_PLACEHOLDER.search(unprotected):
            raise ProtectedTextIntegrityError("unknown structural placeholder")
        return ProtectedText(tuple(segments), self.structures)

    def render(self) -> str:
        """Restore the exact original structures into visible text."""
        output: list[str] = []
        for segment, structure in zip(
            self.visible_segments, self.structures, strict=False
        ):
            output.extend((segment, structure.original))
        output.append(self.visible_segments[-1])
        return "".join(output)
