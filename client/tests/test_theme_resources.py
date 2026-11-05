"""The theme is a chain, and every link fails quietly.

A stylesheet that is not packaged, a token defined in one scheme but not the
other, a placeholder nobody substituted - none of them stop the application.
It starts, it works, and it looks like an unstyled prototype; the artifact's
own `--self-check` constructs the window and never looks at it. That is
precisely the shape of defect a test suite has to carry, because no runtime
check will.

No Qt is constructed here. `load_stylesheet` reads a file and substitutes
text, and the degradation path has to work in a process that has no
application object at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from wuwaterm_client import config, theme

CLIENT_ROOT = Path(__file__).resolve().parents[1]
SPEC = CLIENT_ROOT / "WuwaTerm.spec"
RESOURCES = CLIENT_ROOT / "src" / "wuwaterm_client" / "resources"

PLACEHOLDER = re.compile(r"@([a-z0-9-]+)@")
# The comment form the two files carry, stripped before they are compared:
# the heading names the scheme, and that is the ONLY difference allowed.
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# Every objectName the design pins. A selector that quietly disappears takes
# one control's whole appearance with it, and the control still lays out
# correctly - so nothing else in this suite would notice.
OBJECT_NAMES = (
    "navBar",
    "navItem",
    "endpointChip",
    "endpointDot",
    "pageTitle",
    "sectionTitle",
    "card",
    "cardTitle",
    "banner",
    "bannerIcon",
    "bannerText",
    "bannerActions",
    "kindBadge",
    "kindDot",
    "emptyCard",
    "emptyTitle",
    "emptySubtitle",
    "stepList",
    "stepItem",
    "fieldError",
    "statusStrip",
    "searchField",
    "progressLine",
    "monoLabel",
    "primaryButton",
    "secondaryButton",
    "linkButton",
    "scoreBar",
    "workbenchToolbar",
    "editorSurface",
    "editorHeader",
    "reviewInspector",
    "inspectorBody",
    "commandButton",
    "activityItem",
    "contextTitle",
    "secondaryText",
    "findingTitle",
    "findingPair",
    "findingVerdict",
)

# The dynamic properties, and the values the views set on them.
PROPERTY_VALUES = (
    ("kind", ("exact", "fuzzy", "llm", "noop")),
    ("severity", ("danger", "warn", "info")),
    ("state", ("ok", "missing")),
    ("invalid", ("true", "false")),
    ("done", ("true", "false")),
    ("verdict", ("verified_constraint", "confirmed_conflict", "needs_review", "not_evaluated")),
)

# Widget classes that appear in this application and would otherwise render in
# whatever the platform style thinks they should look like.
WIDGET_SELECTORS = (
    "QMainWindow",
    "QWidget",
    "QDialog",
    "QLabel",
    "QLineEdit",
    "QPlainTextEdit",
    "QPushButton",
    "QComboBox",
    "QComboBox::drop-down",
    "QComboBox::down-arrow",
    "QComboBox QAbstractItemView",
    "QTableWidget",
    "QHeaderView",
    "QScrollBar",
    "QScrollBar::handle",
    "QDoubleSpinBox",
    "QProgressBar",
    "QRadioButton",
    "QMenuBar",
    "QMenu",
    "QMessageBox",
)


def _stylesheet_paths() -> dict[str, Path]:
    return {
        scheme: RESOURCES / name
        for scheme, name in theme.STYLESHEET_FILE_NAMES.items()
    }


def _resource_file_names() -> set[str]:
    return {path.name for path in RESOURCES.iterdir() if path.is_file()}


def _spec_resource_files() -> set[str]:
    """The names the spec really packages, read out of its committed text."""
    spec = SPEC.read_text(encoding="utf-8")
    match = re.search(r"RESOURCE_FILES\s*=\s*\((?P<body>[^)]*)\)", spec)
    assert match, "WuwaTerm.spec no longer declares RESOURCE_FILES"
    return set(re.findall(r"\"([^\"]+)\"", match.group("body")))


def test_the_spec_packages_every_resource_file() -> None:
    """A resource added without a line in the spec is an unstyled build.

    Compared as an EQUALITY in both directions: a missing name ships a build
    that cannot find its stylesheet, and a leftover name points PyInstaller at
    a file that is not there, which fails the build for a reason that reads
    like a tool problem.
    """
    on_disk = _resource_file_names()
    assert on_disk, "the resources directory is empty, which would pass vacuously"
    assert _spec_resource_files() == on_disk

    spec = SPEC.read_text(encoding="utf-8")
    # The target directory the spec unpacks into has to be the one the loader
    # looks in; they are two literals in two files, so they are compared.
    assert f'RESOURCES_TARGET = "{theme.RESOURCE_DIR_NAME}"' in spec
    # ...and the list has to actually reach Analysis. Matched as "appears in
    # the datas argument" rather than as the whole argument: the spec also
    # ships Qt's own Chinese translation there (see
    # test_qt_translations.py::test_the_spec_packages_the_translation), and a
    # gate that forbids a SECOND kind of packaged data would be pinning the
    # argument's punctuation rather than the guarantee - which is that these
    # files reach the build.
    datas_argument = re.search(r"datas=([^\n]*)", spec)
    assert datas_argument is not None, "Analysis has no datas argument at all"
    assert "RESOURCE_DATAS" in datas_argument.group(1)


def test_both_stylesheets_declare_the_same_placeholders() -> None:
    """Light and dark are two renderings of one template.

    A token defined in only one of them is the defect the design named first:
    the missing side falls back to whatever Qt would have drawn, and only one
    of the two themes is visibly wrong - so a screenshot of the other proves
    nothing.
    """
    placeholders = {
        scheme: set(PLACEHOLDER.findall(path.read_text(encoding="utf-8")))
        for scheme, path in _stylesheet_paths().items()
    }

    light = placeholders[theme.SCHEME_LIGHT]
    dark = placeholders[theme.SCHEME_DARK]
    assert light, "no placeholders found, which would pass vacuously"
    assert light == dark


def test_the_two_stylesheets_are_the_same_template() -> None:
    """Stronger than the placeholder comparison, and for the same reason.

    Equal placeholder sets still permit one file to gain a rule the other
    never got. The two files are the same template by construction, so the
    only difference allowed is in the comments - where the heading names the
    scheme. Genuine divergence between the schemes would be a deliberate edit
    to this test, not a silent drift.
    """
    bodies = {
        scheme: COMMENT.sub("", path.read_text(encoding="utf-8"))
        for scheme, path in _stylesheet_paths().items()
    }

    assert bodies[theme.SCHEME_LIGHT] == bodies[theme.SCHEME_DARK]


def test_both_schemes_define_the_same_tokens() -> None:
    """The Python half of the same property."""
    assert set(theme.LIGHT_TOKENS) == set(theme.DARK_TOKENS)
    assert set(theme.TOKENS_BY_SCHEME) == {theme.SCHEME_LIGHT, theme.SCHEME_DARK}
    # Every placeholder in the template has a value in both schemes; every
    # token defined here is really used. The second half is what keeps a
    # renamed selector from leaving a token behind that looks maintained.
    used = set(PLACEHOLDER.findall(
        _stylesheet_paths()[theme.SCHEME_LIGHT].read_text(encoding="utf-8")
    ))
    assert used == set(theme.LIGHT_TOKENS)


@pytest.mark.parametrize("scheme", [theme.SCHEME_LIGHT, theme.SCHEME_DARK])
def test_a_rendered_stylesheet_has_no_placeholder_left(scheme: str) -> None:
    """A placeholder that survives substitution is a rule Qt discards.

    `load_stylesheet` leaves an unknown token verbatim rather than blanking
    it, so a mistyped name surfaces here instead of becoming a rule that
    silently does nothing.
    """
    rendered = theme.load_stylesheet(scheme)

    assert rendered
    assert PLACEHOLDER.search(rendered) is None
    # The substitution really happened, rather than the file having had no
    # placeholders to begin with.
    assert theme.TOKENS_BY_SCHEME[scheme]["accent"] in rendered
    assert theme.TOKENS_BY_SCHEME[scheme]["bg-canvas"] in rendered


@pytest.mark.parametrize("name", OBJECT_NAMES)
def test_every_named_control_has_a_rule(name: str) -> None:
    text = _stylesheet_paths()[theme.SCHEME_LIGHT].read_text(encoding="utf-8")

    assert re.search(rf"#{name}\b", text), name


@pytest.mark.parametrize(("prop", "values"), PROPERTY_VALUES)
def test_every_dynamic_property_value_has_a_rule(
    prop: str, values: tuple[str, ...]
) -> None:
    """The views switch appearance by setting these properties. A value with
    no rule behind it is a control that simply does not change."""
    text = _stylesheet_paths()[theme.SCHEME_LIGHT].read_text(encoding="utf-8")

    for value in values:
        assert f'[{prop}="{value}"]' in text, f"{prop}={value}"


@pytest.mark.parametrize("selector", WIDGET_SELECTORS)
def test_every_widget_class_in_use_is_styled(selector: str) -> None:
    """Otherwise it renders in the platform style, inside a themed window."""
    text = _stylesheet_paths()[theme.SCHEME_LIGHT].read_text(encoding="utf-8")

    assert selector in text, selector


def test_a_missing_resource_degrades_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The packaged artifact's start-up rehearsal must not depend on this.

    `build.ps1` starts the built program with `--self-check` and requires exit
    code 0. If a stylesheet that failed to be packaged raised on the way up,
    the build would fail with a traceback about a missing file rather than
    with the assertion that exists for it - and, worse, an application that
    cannot find its decoration would refuse to run at all.
    """
    monkeypatch.setattr(theme, "_resource_roots", lambda: [tmp_path / "absent"])

    assert theme.resource_path("theme_light.qss") is None
    assert theme.load_stylesheet(theme.SCHEME_LIGHT) == ""
    assert theme.load_stylesheet(theme.SCHEME_DARK) == ""

    applied: list[str] = []

    class Recorder:
        def setStyleSheet(self, sheet: str) -> None:  # noqa: N802 - Qt's name
            applied.append(sheet)

    theme.apply_theme(Recorder(), config.APPEARANCE_LIGHT)

    assert applied == [""]


def test_load_stylesheet_never_raises_on_a_value_it_did_not_expect() -> None:
    """`load_stylesheet` is reached with whatever was on disk in config.json."""
    for value in (None, 42, ["light"], "", "midnight"):
        rendered = theme.load_stylesheet(value)
        assert PLACEHOLDER.search(rendered) is None
        # Unknown means light: a display fallback, not a refusal.
        assert theme.LIGHT_TOKENS["bg-canvas"] in rendered


def test_apply_theme_survives_an_object_that_cannot_take_a_stylesheet() -> None:
    """Belt and braces for the same start-up path: nothing about applying a
    theme may become the reason a launch fails."""
    class Hostile:
        def setStyleSheet(self, sheet: str) -> None:  # noqa: N802 - Qt's name
            raise RuntimeError("the wrapped C++ object has been deleted")

    theme.apply_theme(Hostile(), config.APPEARANCE_DARK)
    theme.apply_theme(object(), config.APPEARANCE_DARK)


def test_following_the_system_is_a_capability_probe_not_a_version_check() -> None:
    """`colorSchemeChanged` arrived in Qt 6.5 and this project's floor is 6.7,
    so it is there today. It is still probed for rather than assumed: an
    application that cannot be told about a system change must go on running
    without it, and the probe is what makes that a fact instead of a hope."""
    connected: list[object] = []

    class Signal:
        def connect(self, handler: object) -> None:
            connected.append(handler)

    class Hints:
        colorSchemeChanged = Signal()  # noqa: N815 - Qt's name

    class Application:
        def styleHints(self) -> Hints:  # noqa: N802 - Qt's name
            return Hints()

        def setStyleSheet(self, sheet: str) -> None:  # noqa: N802 - Qt's name
            pass

    assert theme.follow_system_scheme(Application()) is True
    assert connected

    class Older:
        """A style hints object from before the signal existed."""

        def styleHints(self) -> object:  # noqa: N802 - Qt's name
            return object()

    assert theme.follow_system_scheme(Older()) is False
    assert theme.follow_system_scheme(object()) is False


def test_resolve_scheme_answers_only_light_or_dark() -> None:
    assert theme.resolve_scheme(config.APPEARANCE_LIGHT) == theme.SCHEME_LIGHT
    assert theme.resolve_scheme(config.APPEARANCE_DARK) == theme.SCHEME_DARK
    # "follow the system" is decided at run time; without an application
    # object to ask, the answer is the light scheme rather than an exception.
    for value in (config.APPEARANCE_SYSTEM, None, "midnight"):
        assert theme.resolve_scheme(value) in (theme.SCHEME_LIGHT, theme.SCHEME_DARK)


def test_the_theme_and_the_configuration_agree_on_the_three_values() -> None:
    """theme.py deliberately imports nothing from config - it has to work in a
    process with no HTTP client and no Qt - so the two sets of literals are
    compared here rather than shared."""
    assert config.APPEARANCE_VALUES == (
        theme.SCHEME_SYSTEM,
        theme.SCHEME_LIGHT,
        theme.SCHEME_DARK,
    )
    assert config.DEFAULT_APPEARANCE == theme.SCHEME_SYSTEM
