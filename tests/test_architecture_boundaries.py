from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


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


def test_shipped_scripts_report_no_import_cycles():
    """The deployment tools must not have grown a new import cycle."""
    from scripts import check_architecture_boundaries as cab

    assert cab.check_scripts_import_cycles() == []


def test_shipped_scripts_cycles_are_exactly_the_recorded_components():
    """Two cycles exist and both are recorded; admission is in neither."""
    from scripts import check_architecture_boundaries as cab

    modules = cab._scripts_modules()
    edges = {name: cab._scripts_local_imports(path, modules)
             for name, path in modules.items()}
    components = cab._scripts_cycle_components(edges)
    assert set(components) == set(cab.KNOWN_SCRIPTS_CYCLES), components
    assert all(len(component) > 1 for component in components), components
    for name in ("component_handoff", "component_handoff_finish"):
        assert not any(name in component for component in components), name


def test_handoff_primitives_do_not_import_the_admission_layer():
    """Verified predecessors are read below admission, never back into it."""
    from scripts import check_architecture_boundaries as cab

    modules = cab._scripts_modules()
    handoff = cab._scripts_local_imports(modules["component_handoff"], modules)
    assert "deployment_admission" not in handoff, handoff
    # The entry point that needs both sits above them, not inside either.
    finish = cab._scripts_local_imports(modules["component_handoff_finish"], modules)
    assert {"deployment_admission", "component_handoff"} <= set(finish), finish


def test_scripts_local_imports_sees_function_body_imports(tmp_path: Path, monkeypatch):
    """A lazy reverse import is still an edge; only an AST walk finds it."""
    from scripts import check_architecture_boundaries as cab

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "outer.py").write_text("import inner\n", encoding="utf-8")
    (scripts / "inner.py").write_text(
        "def use_outer():\n"
        "    import outer\n"
        "    return outer\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    monkeypatch.setattr(cab, "KNOWN_SCRIPTS_CYCLES", frozenset())

    modules = cab._scripts_modules()
    assert cab._scripts_local_imports(scripts / "inner.py", modules) == {"outer": 2}
    failures = cab.check_scripts_import_cycles()
    assert any("new import cycle among inner, outer" in failure for failure in failures), failures


def test_forbidden_one_way_edge_is_refused_even_when_lazy(tmp_path: Path, monkeypatch):
    from scripts import check_architecture_boundaries as cab

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "deployment_admission.py").write_text(
        "import component_handoff\n", encoding="utf-8"
    )
    (scripts / "component_handoff.py").write_text(
        "def finish():\n"
        "    import deployment_admission\n"
        "    return deployment_admission\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    monkeypatch.setattr(cab, "KNOWN_SCRIPTS_CYCLES", frozenset())

    failures = cab.check_scripts_import_cycles()
    joined = "\n".join(failures)
    assert "component_handoff must not import deployment_admission" in joined, failures


def test_popular_shipped_scripts_use_the_scripts_module_form(tmp_path: Path, monkeypatch):
    """``from scripts import x`` counts as an edge in the flat tool graph."""
    from scripts import check_architecture_boundaries as cab

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "alpha.py").write_text("from scripts import beta\n", encoding="utf-8")
    (scripts / "beta.py").write_text("import alpha\n", encoding="utf-8")
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    monkeypatch.setattr(cab, "KNOWN_SCRIPTS_CYCLES", frozenset())

    failures = cab.check_scripts_import_cycles()
    assert any("new import cycle among alpha, beta" in failure for failure in failures), failures


def test_stale_cycle_record_is_reported(tmp_path: Path, monkeypatch):
    """A fixed cycle must delete its record instead of leaving a dead entry."""
    from scripts import check_architecture_boundaries as cab

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "one.py").write_text("x = 1\n", encoding="utf-8")
    (scripts / "two.py").write_text("import one\n", encoding="utf-8")
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    monkeypatch.setattr(
        cab,
        "KNOWN_SCRIPTS_CYCLES",
        frozenset({frozenset({"one", "two"})}),
    )

    failures = cab.check_scripts_import_cycles()
    assert any("no longer exists" in failure for failure in failures), failures


def test_private_cross_module_import_is_rejected(tmp_path: Path, monkeypatch):
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm"
    pkg.mkdir()
    for name in cab.ALL_CLASSIFIED:
        path = pkg / f"{name.replace('.', '/')}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")

    (pkg / "term_matching.py").write_text(
        "def public_helper():\n    return 1\n"
        "def _private_helper():\n    return 2\n",
        encoding="utf-8",
    )
    (pkg / "term_preparation.py").write_text(
        "from .term_matching import _private_helper\n",
        encoding="utf-8",
    )
    (pkg / "sentence.py").write_text(
        "from wuwaterm.term_matching import _private_helper\n"
        "from .term_matching import public_helper as _local_alias\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(cab, "PACKAGE", pkg)
    failures = cab.check()

    assert any(
        "term_preparation.py:1: must not import private symbol '_private_helper' from term_matching"
        in f
        for f in failures
    ), failures
    assert any(
        "sentence.py:1: must not import private symbol '_private_helper' from term_matching"
        in f
        for f in failures
    ), failures
    assert not any("_local_alias" in f or "public_helper" in f for f in failures), failures


def test_three_node_import_cycle_is_refused(tmp_path: Path, monkeypatch):
    """A pair check misses `a -> b -> c -> a`; the component walk does not."""
    from scripts import check_architecture_boundaries as cab

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "a.py").write_text("import b\n", encoding="utf-8")
    (scripts / "b.py").write_text("import c\n", encoding="utf-8")
    (scripts / "c.py").write_text("import a\n", encoding="utf-8")
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    monkeypatch.setattr(cab, "KNOWN_SCRIPTS_CYCLES", frozenset())

    components = cab._scripts_cycle_components(
        {name: cab._scripts_local_imports(path, cab._scripts_modules())
         for name, path in cab._scripts_modules().items()}
    )
    assert components == [frozenset({"a", "b", "c"})], components

    failures = cab.check_scripts_import_cycles()
    joined = "\n".join(failures)
    assert "new import cycle among a, b, c" in joined, failures
    assert "a -> b at scripts/a.py:1" in joined, failures
    assert "c -> a at scripts/c.py:1" in joined, failures


def test_recorded_three_node_cycle_is_accepted(tmp_path: Path, monkeypatch):
    """A recorded component passes, so the pin can describe a real cycle."""
    from scripts import check_architecture_boundaries as cab

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "a.py").write_text("import b\n", encoding="utf-8")
    (scripts / "b.py").write_text("import c\n", encoding="utf-8")
    (scripts / "c.py").write_text("import a\n", encoding="utf-8")
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    monkeypatch.setattr(
        cab, "KNOWN_SCRIPTS_CYCLES", frozenset({frozenset({"a", "b", "c"})})
    )

    assert cab.check_scripts_import_cycles() == []


def test_pure_term_modules_do_not_reach_lookup_or_db_transitively():
    """Pure matching/text modules and pure plan/contract modules stay decoupled.

    All twelve ``PURE_MATCHING`` modules (including ``structural_checks``,
    ``attribution_candidates``, ``segmentation`` and ``text_spans``) plus
    ``translation_plan`` and ``semantic_review_contract`` must not reach
    ``review``, ``lookup``, ``db``, or ``sqlite3`` even through transitive
    imports.
    """
    from scripts import check_architecture_boundaries as cab

    modules = cab._pkg_modules()
    graph = {
        name: {
            imported
            for imported, _type_only, _lineno in cab._iter_import_events(path)
            if imported in modules
        }
        for name, path in modules.items()
    }
    externals = {
        name: {
            imported
            for imported, _type_only, _lineno in cab._iter_import_events(path)
            if imported not in modules
        }
        for name, path in modules.items()
    }
    assert cab.PURE_MATCHING == {
        "attribution_candidates",
        "cjk_span",
        "document_anchor",
        "domain_knowledge",
        "english_plural",
        "fuzzy_index",
        "native_kernel",
        "protected_text",
        "segmentation",
        "single_char_context",
        "skill_references",
        "structural_checks",
        "term_classification",
        "term_matching",
        "term_preparation",
        "termbase",
        "text_spans",
    }
    check_roots = cab.PURE_MATCHING | {"translation_plan", "semantic_review_contract"}
    for root in sorted(check_roots):
        seen: set[str] = {root}
        stack = [root]
        while stack:
            current = stack.pop()
            for neighbor in graph.get(current, ()):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        assert not (seen & cab.PURE_MATCHING_FORBIDDEN_STORAGE), (root, sorted(seen))
        for reached in seen:
            assert not any(
                ext == "sqlite3" or ext.startswith("sqlite3.")
                for ext in externals.get(reached, ())
            ), (root, reached, sorted(externals.get(reached, ())))


def test_pure_matching_modules_do_not_import_db_or_lookup():
    from scripts import check_architecture_boundaries as cab

    modules = cab._pkg_modules()
    assert cab.PURE_MATCHING <= set(modules)
    for name in sorted(cab.PURE_MATCHING):
        events = cab._iter_import_events(modules[name])
        local_imported = {imported for imported, _type_only, _lineno in events}
        assert not (local_imported & cab.PURE_MATCHING_FORBIDDEN_STORAGE), name
        assert not any(
            ext == "sqlite3" or ext.startswith("sqlite3.")
            for ext in local_imported
        ), name


def test_pure_matching_module_importing_db_or_lookup_is_rejected(
    tmp_path: Path, monkeypatch
):
    from scripts import check_architecture_boundaries as cab

    pkg = tmp_path / "wuwaterm"
    pkg.mkdir()
    for name in cab.ALL_CLASSIFIED:
        path = pkg / f"{name.replace('.', '/')}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")

    (pkg / "term_matching.py").write_text(
        "from .db import connect\n", encoding="utf-8"
    )
    (pkg / "term_preparation.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .lookup import TermService\n",
        encoding="utf-8",
    )
    (pkg / "segmentation.py").write_text(
        "import sqlite3\n", encoding="utf-8"
    )
    (pkg / "text_spans.py").write_text(
        "from .review import review_pair\n", encoding="utf-8"
    )

    monkeypatch.setattr(cab, "PACKAGE", pkg)
    failures = cab.check()
    joined = "\n".join(failures)
    assert "term_matching.py: pure matching module must not import storage" in joined, failures
    assert "'db'" in joined, failures
    assert "term_preparation.py: pure matching module must not import storage" in joined, failures
    assert "'lookup'" in joined, failures
    assert "segmentation.py: pure matching module must not import sqlite3" in joined, failures
    assert "text_spans.py: pure matching module must not import storage" in joined, failures
    assert "'review'" in joined, failures


def test_shipped_deployment_admission_manifest_is_valid():
    from scripts import check_architecture_boundaries as cab

    assert cab.check_deployment_admission_manifest() == []


def _write_minimal_admission_scripts(
    tmp_path: Path,
    *,
    declared: tuple[str, ...] = ("component_runtime", "component_build"),
    admission_body: str | None = None,
    tool_paths: tuple[str, ...] = (
        "scripts/deployment_admission.py",
        "scripts/component_runtime.py",
        "scripts/component_build.py",
        "scripts/component_handoff_finish.py",
    ),
    extra_files: dict[str, str] | None = None,
) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    declared_repr = ", ".join(repr(item) for item in declared)
    tool_paths_repr = ", ".join(repr(item) for item in tool_paths)
    (scripts / "component_journal_runtime.py").write_text(
        f"TOOL_PATHS = ({tool_paths_repr},)\n", encoding="utf-8"
    )
    (scripts / "component_runtime.py").write_text("x = 1\n", encoding="utf-8")
    (scripts / "component_build.py").write_text("x = 1\n", encoding="utf-8")
    (scripts / "component_handoff_finish.py").write_text(
        "import deployment_admission\n", encoding="utf-8"
    )
    if admission_body is None:
        admission_body = textwrap.dedent(
            """\
            _ATTEMPT_PRODUCERS = {
                "builds": _AttemptProducer("component_build", "_read_attempt", None, None),
            }
            def _tool_modules(*names, message):
                for name in names:
                    importlib.import_module(name)
            def _tool_module(name, *, message):
                return _tool_modules(name, message=message)[0]
            def use_tools(key):
                return (
                    _tool_module(_ATTEMPT_PRODUCERS[key].module, message="m"),
                    _tool_module("component_runtime", message="m"),
                )
            """
        )
    (scripts / "deployment_admission.py").write_text(
        f"import importlib\nDECLARED_DYNAMIC_TOOL_DEPS = frozenset({{{declared_repr}}})\n"
        + admission_body,
        encoding="utf-8",
    )
    if extra_files:
        for filename, content in extra_files.items():
            (scripts / filename).write_text(content, encoding="utf-8")
    return scripts


def test_admission_manifest_accepts_minimal_valid_graph(tmp_path: Path, monkeypatch):
    from scripts import check_architecture_boundaries as cab

    scripts = _write_minimal_admission_scripts(tmp_path)
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    assert cab.check_deployment_admission_manifest() == []


def test_admission_manifest_rejects_undeclared_tool_call(tmp_path: Path, monkeypatch):
    from scripts import check_architecture_boundaries as cab

    scripts = _write_minimal_admission_scripts(
        tmp_path,
        declared=("component_runtime",),
        admission_body=textwrap.dedent(
            """\
            def _tool_modules(*names, message):
                for name in names:
                    importlib.import_module(name)
            def _tool_module(name, *, message):
                return _tool_modules(name, message=message)[0]
            def use_tools():
                _tool_module("component_runtime", message="m")
                _tool_module("foo", message="m")
            """
        ),
        extra_files={"foo.py": "x = 1\n"},
    )
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    failures = cab.check_deployment_admission_manifest()
    assert any(
        "dynamic tool 'foo' is not listed in DECLARED_DYNAMIC_TOOL_DEPS" in f
        for f in failures
    ), failures


def test_admission_manifest_rejects_non_discoverable_tool_call(
    tmp_path: Path, monkeypatch
):
    from scripts import check_architecture_boundaries as cab

    scripts = _write_minimal_admission_scripts(
        tmp_path,
        declared=("component_runtime",),
        admission_body=textwrap.dedent(
            """\
            def _tool_modules(*names, message):
                for name in names:
                    importlib.import_module(name)
            def _tool_module(name, *, message):
                return _tool_modules(name, message=message)[0]
            def use_tools(dynamic_name):
                _tool_module("component_runtime", message="m")
                _tool_module(dynamic_name, message="m")
            """
        ),
    )
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    failures = cab.check_deployment_admission_manifest()
    assert any("not statically discoverable" in f for f in failures), failures


def test_admission_manifest_rejects_forbidden_higher_layer_dynamic_dep(
    tmp_path: Path, monkeypatch
):
    from scripts import check_architecture_boundaries as cab

    scripts = _write_minimal_admission_scripts(
        tmp_path,
        declared=("component_runtime", "component_handoff_finish"),
        admission_body=textwrap.dedent(
            """\
            def _tool_modules(*names, message):
                for name in names:
                    importlib.import_module(name)
            def _tool_module(name, *, message):
                return _tool_modules(name, message=message)[0]
            def use_tools():
                return _tool_modules("component_runtime", "component_handoff_finish", message="m")
            """
        ),
    )
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    failures = cab.check_deployment_admission_manifest()
    assert any(
        "forbidden higher-layer dynamic tool dependency 'component_handoff_finish'" in f
        for f in failures
    ), failures


def test_admission_manifest_rejects_stale_entry(tmp_path: Path, monkeypatch):
    from scripts import check_architecture_boundaries as cab

    scripts = _write_minimal_admission_scripts(
        tmp_path,
        declared=("component_runtime", "component_build", "stale_tool"),
        tool_paths=(
            "scripts/deployment_admission.py",
            "scripts/component_runtime.py",
            "scripts/component_build.py",
            "scripts/stale_tool.py",
        ),
        extra_files={"stale_tool.py": "x = 1\n"},
    )
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    failures = cab.check_deployment_admission_manifest()
    assert any(
        "stale entry 'stale_tool' in DECLARED_DYNAMIC_TOOL_DEPS" in f
        for f in failures
    ), failures


def test_admission_manifest_rejects_missing_tool_paths_entry(
    tmp_path: Path, monkeypatch
):
    from scripts import check_architecture_boundaries as cab

    scripts = _write_minimal_admission_scripts(
        tmp_path,
        declared=("component_runtime", "component_build"),
        tool_paths=("scripts/deployment_admission.py", "scripts/component_runtime.py"),
    )
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    failures = cab.check_deployment_admission_manifest()
    assert any(
        "declared dynamic tool 'component_build' is missing from component_journal_runtime.TOOL_PATHS"
        in f
        for f in failures
    ), failures


@pytest.mark.parametrize(
    "bad_use",
    [
        # Plain intermediate variable holding _ATTEMPT_PRODUCERS[key]
        (
            "def use_tools(key):\n"
            "    producer_spec = _ATTEMPT_PRODUCERS[key]\n"
            '    return _tool_module(producer_spec.module, message="m")\n'
        ),
        # Mixed if/else branches (proven then unknown)
        (
            "def use_tools(cond, key, other_spec):\n"
            "    if cond:\n"
            "        producer_spec = _ATTEMPT_PRODUCERS[key]\n"
            "    else:\n"
            "        producer_spec = other_spec\n"
            '    return _tool_module(producer_spec.module, message="m")\n'
        ),
        # Mixed if/else branches (unknown then proven)
        (
            "def use_tools(cond, key, other_spec):\n"
            "    if cond:\n"
            "        producer_spec = other_spec\n"
            "    else:\n"
            "        producer_spec = _ATTEMPT_PRODUCERS[key]\n"
            '    return _tool_module(producer_spec.module, message="m")\n'
        ),
        # Conditional walrus assignment (:=)
        (
            "def use_tools(cond, key):\n"
            "    if cond and (producer_spec := _ATTEMPT_PRODUCERS[key]):\n"
            "        pass\n"
            '    return _tool_module(producer_spec.module, message="m")\n'
        ),
    ],
)
def test_admission_manifest_rejects_indirect_producer_var_uses(
    tmp_path: Path, monkeypatch, bad_use: str
):
    from scripts import check_architecture_boundaries as cab

    admission_body = (
        "_ATTEMPT_PRODUCERS = {\n"
        '    "builds": _AttemptProducer("component_build", "_read_attempt", None, None),\n'
        "}\n"
        "def _tool_modules(*names, message):\n"
        "    for name in names:\n"
        "        importlib.import_module(name)\n"
        "def _tool_module(name, *, message):\n"
        "    return _tool_modules(name, message=message)[0]\n"
        + bad_use
    )
    scripts = _write_minimal_admission_scripts(
        tmp_path,
        declared=("component_build",),
        admission_body=admission_body,
    )
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    failures = cab.check_deployment_admission_manifest()
    assert any("not statically discoverable" in f for f in failures), failures


@pytest.mark.parametrize(
    "alias_snippet",
    [
        'import importlib as il\ndef bypass():\n    return il.import_module("component_runtime")\n',
        'from importlib import import_module as im\ndef bypass():\n    return im("component_runtime")\n',
        'def bypass():\n    loader = importlib.import_module\n    return loader("component_runtime")\n',
    ],
)
def test_admission_manifest_rejects_import_module_aliases(
    tmp_path: Path, monkeypatch, alias_snippet: str
):
    from scripts import check_architecture_boundaries as cab

    admission_body = (
        alias_snippet
        + "def _tool_modules(*names, message):\n"
        "    for name in names:\n"
        "        importlib.import_module(name)\n"
        "def _tool_module(name, *, message):\n"
        "    return _tool_modules(name, message=message)[0]\n"
        "def use_tools():\n"
        '    return _tool_module("component_runtime", message="m")\n'
    )
    scripts = _write_minimal_admission_scripts(
        tmp_path,
        declared=("component_runtime",),
        admission_body=admission_body,
    )
    monkeypatch.setattr(cab, "SCRIPTS", scripts)
    failures = cab.check_deployment_admission_manifest()
    assert any("raw import_module call bypasses" in f for f in failures), failures


def test_entity_graph_and_domain_knowledge_layer_boundaries():
    from scripts import check_architecture_boundaries as cab

    assert "domain_knowledge" in cab.PURE_MATCHING
    assert "entity_graph" in cab.DOMAIN_CORE
    assert "entity_graph" not in cab.PURE_MATCHING
    assert "entity_graph" in cab.PURE_MATCHING_FORBIDDEN_STORAGE

    modules = cab._pkg_modules()
    eg_imports = {
        imported
        for imported, _type_only, _lineno in cab._iter_import_events(
            modules["entity_graph"]
        )
        if imported in modules
    }
    assert "knowledge_db" not in eg_imports
    assert "sentence" not in eg_imports
    assert "application" not in eg_imports
    assert "translation_plan" not in eg_imports
