from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_architecture_boundaries.py"


def test_architecture_boundary_script_passes_on_shipped_tree():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "architecture boundary guard ok" in result.stdout


def test_shipped_modules_are_fully_classified():
    from scripts import check_architecture_boundaries as cab

    modules = set(cab._pkg_modules())
    assert modules <= cab.ALL_CLASSIFIED
    assert modules  # cardinality floor: package is non-empty


def test_absolute_package_import_normalizes_to_local_stem():
    from scripts import check_architecture_boundaries as cab

    assert cab._normalize_imported_name("wuwaterm.bot") == "bot"
    assert cab._normalize_imported_name("wuwaterm.build_pinyin") == "build_pinyin"
    assert cab._normalize_imported_name("telegram.ext") == "telegram.ext"


def test_absolute_import_wuwaterm_bot_is_detected_as_local_bot(tmp_path: Path):
    """Fail-closed: plain ``import wuwaterm.bot`` must not evade stem rules."""
    from scripts import check_architecture_boundaries as cab

    snippet = tmp_path / "snippet.py"
    snippet.write_text("import wuwaterm.bot\n", encoding="utf-8")
    events = cab._iter_import_events(snippet)
    names = {name for name, _type_only, _lineno in events}
    assert "bot" in names
    assert "wuwaterm.bot" not in names


def test_type_checking_else_import_is_runtime(tmp_path: Path):
    from scripts import check_architecture_boundaries as cab

    source = textwrap.dedent(
        """
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from .models import TermEntry
        else:
            from .bot import BotConfig
        """
    )
    probe = tmp_path / "type_checking_else_probe.py"
    probe.write_text(source, encoding="utf-8")
    events = cab._iter_import_events(probe)
    by_name = {name: type_only for name, type_only, _ in events}
    assert by_name.get("models") is True
    assert by_name.get("bot") is False


def test_channel_bot_import_is_type_checking_only():
    from scripts import check_architecture_boundaries as cab

    modules = cab._pkg_modules()
    events = cab._iter_import_events(modules["channel"])
    bot_events = [(type_only, lineno) for name, type_only, lineno in events if name == "bot"]
    assert bot_events, "expected channel to TYPE_CHECKING-import BotConfig from bot"
    assert all(type_only for type_only, _ in bot_events)


def test_build_pinyin_only_from_allowed_importers():
    from scripts import check_architecture_boundaries as cab

    modules = cab._pkg_modules()
    for name, path in modules.items():
        events = cab._iter_import_events(path)
        if any(imported == "build_pinyin" for imported, *_ in events):
            assert name in cab.BUILD_PINYIN_ALLOWED_IMPORTERS, name


def test_presentation_does_not_import_builder_modules():
    from scripts import check_architecture_boundaries as cab

    modules = cab._pkg_modules()
    for pres in ("bot", "channel"):
        events = cab._iter_import_events(modules[pres])
        imported = {name for name, *_ in events if name in modules}
        assert not imported & cab.BUILDER, (pres, imported & cab.BUILDER)


def test_domain_lookup_has_no_presentation_runtime_imports():
    from scripts import check_architecture_boundaries as cab

    modules = cab._pkg_modules()
    events = cab._iter_import_events(modules["lookup"])
    runtime_local = {
        name for name, type_only, _ in events if not type_only and name in modules
    }
    assert "bot" not in runtime_local
    assert "channel" not in runtime_local


def test_sentence_only_allows_the_named_direct_lazy_legacy_imports(tmp_path: Path, monkeypatch):
    from scripts import check_architecture_boundaries as cab

    real_modules, collisions = cab._discover_modules()
    sample = tmp_path / "sentence.py"
    sample.write_text(
        "class SentenceTranslator:\n"
        "    def translate_html(self):\n"
        "        from .telegram_html import translate_html\n"
        "    def translate_protected(self):\n"
        "        from .telegram_html import translate_html\n",
        encoding="utf-8",
    )
    assert cab._sentence_legacy_bridge_lines(sample) == {3}
    modules = dict(real_modules)
    modules["sentence"] = sample
    monkeypatch.setattr(cab, "_discover_modules", lambda: (modules, collisions))
    assert any("telegram_html" in failure for failure in cab.check())

    sample.write_text(
        "from .telegram_html import translate_html\n"
        "class SentenceTranslator:\n"
        "    def translate_html(self):\n"
        "        from .telegram_html import translate_html\n",
        encoding="utf-8",
    )
    assert any("telegram_html" in failure for failure in cab.check())


def test_sentence_legacy_bridge_cannot_allow_other_imports(tmp_path: Path):
    from scripts import check_architecture_boundaries as cab

    sample = tmp_path / "sentence.py"
    sample.write_text(
        "class SentenceTranslator:\n"
        "    def translate_html(self):\n"
        "        if True:\n"
        "            from .telegram_html import translate_html\n"
        "        from .telegram_html import protect_telegram_html\n",
        encoding="utf-8",
    )
    assert cab._sentence_legacy_bridge_lines(sample) == set()


def test_check_reports_unclassified_modules(monkeypatch):
    from scripts import check_architecture_boundaries as cab

    real_modules, real_collisions = cab._discover_modules()
    # Inject a fake unclassified stem without writing into src/.
    fake = dict(real_modules)
    fake["rogue_helper"] = ROOT / "src" / "wuwaterm" / "lookup.py"
    monkeypatch.setattr(cab, "_discover_modules", lambda: (fake, list(real_collisions)))
    failures = cab.check()
    assert any("unclassified modules" in f and "rogue_helper" in f for f in failures)


def test_nested_package_module_is_discovered(tmp_path: Path, monkeypatch):
    """Subpackages must not escape scanning via flat-only discovery."""
    from scripts import check_architecture_boundaries as cab

    # Point PACKAGE at a temp tree with a nested module.
    pkg = tmp_path / "wuwaterm"
    (pkg / "domain").mkdir(parents=True)
    (pkg / "lookup.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "domain" / "helper.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from ..bot import BotConfig\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cab, "PACKAGE", pkg)
    modules = cab._pkg_modules()
    assert "lookup" in modules
    assert "domain.helper" in modules
    # Nested module is unclassified relative to real layer sets → check fails.
    failures = cab.check()
    assert any("unclassified modules" in f and "domain.helper" in f for f in failures)


def test_type_only_presentation_import_from_domain_is_rejected(monkeypatch, tmp_path: Path):
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm"
    pkg.mkdir()
    (pkg / "lookup.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .bot import BotConfig\n",
        encoding="utf-8",
    )
    (pkg / "bot.py").write_text("class BotConfig: pass\n", encoding="utf-8")
    (pkg / "channel.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "telegram_html.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "telegram_text.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(cab, "PACKAGE", pkg)
    # Minimal classification so only the presentation edge is under test.
    monkeypatch.setattr(
        cab,
        "ALL_CLASSIFIED",
        frozenset({"lookup", "bot", "channel", "telegram_html", "telegram_text"}),
    )
    monkeypatch.setattr(cab, "DOMAIN_CORE", frozenset({"lookup"}))
    monkeypatch.setattr(cab, "NO_TELEGRAM_PRESENTATION", frozenset({"lookup"}))
    monkeypatch.setattr(
        cab,
        "PRESENTATION",
        frozenset({"bot", "channel", "telegram_html", "telegram_text"}),
    )
    failures = cab.check()
    assert any("must not import presentation" in f and "bot" in f for f in failures)


def test_presentation_must_not_import_cli(monkeypatch, tmp_path: Path):
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm"
    pkg.mkdir()
    for name in (
        "bot",
        "channel",
        "telegram_html",
        "telegram_text",
        "cli",
        "lookup",
        "normalize",
        "models",
    ):
        (pkg / f"{name}.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "bot.py").write_text("from .cli import main\n", encoding="utf-8")
    monkeypatch.setattr(cab, "PACKAGE", pkg)
    # Use real layer sets; ensure required stems exist as empty modules.
    for name in cab.ALL_CLASSIFIED:
        path = pkg / f"{name.replace('.', '/')}.py"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x = 1\n", encoding="utf-8")
    (pkg / "bot.py").write_text("from .cli import main\n", encoding="utf-8")
    failures = cab.check()
    assert any("must not import bootstrap cli" in f for f in failures)


def test_nested_package_init_is_discovered_and_scanned(tmp_path: Path, monkeypatch):
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm"
    (pkg / "domain").mkdir(parents=True)
    (pkg / "lookup.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "domain" / "__init__.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from ..bot import BotConfig\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cab, "PACKAGE", pkg)
    modules = cab._pkg_modules()
    assert "domain" in modules
    assert modules["domain"].name == "__init__.py"
    failures = cab.check()
    assert any("unclassified modules" in f and "domain" in f for f in failures)


def test_nested_import_keys_are_preserved(tmp_path: Path):
    from scripts import check_architecture_boundaries as cab

    assert cab._normalize_imported_name("wuwaterm.domain.helper") == "domain.helper"
    probe = tmp_path / "importer.py"
    # Absolute nested form
    probe.write_text("import wuwaterm.ui.helper\n", encoding="utf-8")
    names = {n for n, *_ in cab._iter_import_events(probe)}
    assert "ui.helper" in names
    assert "ui" not in names or "ui.helper" in names


def test_relative_nested_import_resolves_against_importer(tmp_path: Path, monkeypatch):
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm"
    (pkg / "domain").mkdir(parents=True)
    (pkg / "ui").mkdir(parents=True)
    (pkg / "domain" / "service.py").write_text(
        "from ..ui.helper import X\n",
        encoding="utf-8",
    )
    (pkg / "ui" / "helper.py").write_text("X = 1\n", encoding="utf-8")
    monkeypatch.setattr(cab, "PACKAGE", pkg)
    events = cab._iter_import_events(pkg / "domain" / "service.py")
    names = {n for n, *_ in events}
    assert "ui.helper" in names


def test_package_from_submodule_import_records_dotted_key(tmp_path: Path, monkeypatch):
    """from ..ui import helper must surface ui.helper, not only ui."""
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm"
    (pkg / "domain").mkdir(parents=True)
    (pkg / "ui").mkdir(parents=True)
    (pkg / "domain" / "service.py").write_text(
        "from ..ui import helper\n",
        encoding="utf-8",
    )
    (pkg / "ui" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(cab, "PACKAGE", pkg)
    events = cab._iter_import_events(pkg / "domain" / "service.py")
    names = {n for n, *_ in events}
    assert "ui" in names
    assert "ui.helper" in names


def test_package_from_submodule_forbidden_edge_is_caught(tmp_path: Path, monkeypatch):
    """Classified nested presentation via package-from must fail domain import."""
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm"
    (pkg / "ui").mkdir(parents=True)
    (pkg / "lookup.py").write_text("from .ui import helper\n", encoding="utf-8")
    (pkg / "ui" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    for name in (
        "bot",
        "channel",
        "telegram_html",
        "telegram_text",
        "normalize",
        "models",
        "cli",
        "sentence",
        "translation_policy",
        "runtime_keys",
        "constants",
        "settings",
        "channel_reply_index",
        "channel_reply_schema",
        "channel_runtime",
        "logging_utils",
        "db",
        "builder",
        "data_source",
        "build_pinyin",
    ):
        path = pkg / f"{name}.py"
        if not path.exists():
            path.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(cab, "PACKAGE", pkg)
    # Treat nested ui.helper as presentation so the edge is forbidden for lookup.
    monkeypatch.setattr(
        cab,
        "PRESENTATION",
        frozenset({"bot", "channel", "telegram_html", "telegram_text", "ui.helper"}),
    )
    monkeypatch.setattr(
        cab,
        "ALL_CLASSIFIED",
        cab.ALL_CLASSIFIED | frozenset({"ui.helper"}),
    )
    failures = cab.check()
    assert any(
        "must not import presentation" in f and "ui.helper" in f for f in failures
    ), failures


def test_nested_from_emits_parent_package_prefix(tmp_path: Path, monkeypatch):
    """from .ui.helper import X must also record parent package ui."""
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm"
    (pkg / "domain").mkdir(parents=True)
    (pkg / "ui").mkdir(parents=True)
    (pkg / "domain" / "service.py").write_text(
        "from ..ui.helper import X\n",
        encoding="utf-8",
    )
    (pkg / "ui" / "helper.py").write_text("X = 1\n", encoding="utf-8")
    monkeypatch.setattr(cab, "PACKAGE", pkg)
    names = {n for n, *_ in cab._iter_import_events(pkg / "domain" / "service.py")}
    assert "ui.helper" in names
    assert "ui" in names


def test_parent_package_presentation_edge_is_caught(tmp_path: Path, monkeypatch):
    """Importing ui.helper must fail if package ui is classified presentation."""
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm"
    (pkg / "ui").mkdir(parents=True)
    (pkg / "lookup.py").write_text("from .ui.helper import X\n", encoding="utf-8")
    (pkg / "ui" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "ui" / "helper.py").write_text("X = 1\n", encoding="utf-8")
    for name in cab.ALL_CLASSIFIED:
        path = pkg / f"{name.replace('.', '/')}.py"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x = 1\n", encoding="utf-8")
    # lookup.py rewritten after seed
    (pkg / "lookup.py").write_text("from .ui.helper import X\n", encoding="utf-8")
    monkeypatch.setattr(cab, "PACKAGE", pkg)
    monkeypatch.setattr(
        cab,
        "PRESENTATION",
        frozenset({"bot", "channel", "telegram_html", "telegram_text", "ui"}),
    )
    monkeypatch.setattr(
        cab,
        "ALL_CLASSIFIED",
        cab.ALL_CLASSIFIED | frozenset({"ui", "ui.helper"}),
    )
    failures = cab.check()
    assert any(
        "must not import presentation" in f and "ui" in f for f in failures
    ), failures


def test_duplicate_module_keys_fail_closed(tmp_path: Path, monkeypatch):
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm"
    (pkg / "foo").mkdir(parents=True)
    (pkg / "foo.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "foo" / "__init__.py").write_text("y = 2\n", encoding="utf-8")
    monkeypatch.setattr(cab, "PACKAGE", pkg)
    modules, collisions = cab._discover_modules()
    assert "foo" in modules
    assert any("duplicate module key 'foo'" in c for c in collisions)
    failures = cab.check()
    assert any("duplicate module key" in f for f in failures)


def test_shipped_api_package_respects_its_narrow_allowlist():
    from scripts import check_architecture_boundaries as cab

    assert cab.API_PACKAGE.is_dir()
    assert cab.check_api_package() == []


def test_api_package_may_not_reach_past_the_application_layer(
    tmp_path: Path, monkeypatch
):
    """The API must not be able to grow its own copy of the pipeline."""
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm_api"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "routes.py").write_text(
        "from wuwaterm.application import translate_request_async\n"
        "from wuwaterm.lookup import TermService\n"
        "from wuwaterm.sentence import SentenceTranslator\n"
        "import wuwaterm.bot\n"
        "import telegram\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cab, "API_PACKAGE", pkg)
    monkeypatch.setattr(cab, "ROOT", tmp_path)

    failures = cab.check_api_package()

    joined = "\n".join(failures)
    assert "wuwaterm.lookup" in joined
    assert "wuwaterm.sentence" in joined
    assert "wuwaterm.bot" in joined
    assert "Telegram SDK" in joined
    assert "wuwaterm.application" not in joined


def test_api_package_may_not_import_the_package_root(tmp_path: Path, monkeypatch):
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm_api"
    pkg.mkdir(parents=True)
    (pkg / "broad.py").write_text("import wuwaterm\n", encoding="utf-8")
    monkeypatch.setattr(cab, "API_PACKAGE", pkg)
    monkeypatch.setattr(cab, "ROOT", tmp_path)

    failures = cab.check_api_package()

    assert any("not the wuwaterm package root" in f for f in failures), failures


def test_api_package_type_only_imports_are_not_exempt(tmp_path: Path, monkeypatch):
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm_api"
    pkg.mkdir(parents=True)
    (pkg / "typed.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from wuwaterm.bot import BotConfig\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cab, "API_PACKAGE", pkg)
    monkeypatch.setattr(cab, "ROOT", tmp_path)

    failures = cab.check_api_package()

    assert any("wuwaterm.bot" in f for f in failures), failures
