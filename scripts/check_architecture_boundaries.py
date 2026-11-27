#!/usr/bin/env python3
"""Fail closed on forbidden import directions among shipped modules.

Intended layers (must stay aligned with docs/architecture.md):

  domain core:     attribution_candidates, lookup, native_kernel, normalize,
                   models, review, cjk_span, document_anchor, protected_text,
                   english_plural, segmentation, semantic_review_contract,
                   single_char_context, skill_references, term_classification,
                   term_matching, term_preparation, termbase, text_spans
  domain+LLM:      sentence (three lazy Telegram compatibility bridges only),
                   translation_plan (immutable plan, policy, provider profile,
                   request/operation identity),
                   llm_transport (HTTP transport for chat completions),
                   semantic_review (semantic-review-v1 engine)
  application:     application  (protocol-neutral command/API/web pipeline;
                   must not import any presentation module, the Telegram SDK,
                   or builder modules)
  shared policy:   translation_policy, runtime_keys, constants,
                   exclusion_metadata (strict builder-metadata parsers)
  presentation:    bot, channel, telegram_html, telegram_text
  local state:     settings, channel_reply_index, channel_reply_schema,
                   channel_runtime, logging_utils, request_trace
  storage:         db  (lazy build_pinyin only inside write helpers)
  builder:         builder, data_source, build_pinyin
  bootstrap:       cli  (may wire runtime and builder entrypoints)

Rules enforced here (all against real src/wuwaterm package modules, including
nested subpackages discovered via rglob):

1. Domain core, the application layer and pure helpers must not import
   presentation or the Telegram SDK even under TYPE_CHECKING (only ``channel``
   may TYPE_CHECKING-import bot).
2. sentence must not import bot or channel (runtime or type-only).
3. Presentation must not import builder-path modules (builder, data_source,
   build_pinyin) or bootstrap ``cli`` (cli pulls the builder graph).
4. channel must not runtime-import bot (TYPE_CHECKING-only is allowed).
5. build_pinyin may only be imported from db (lazy write path) or builder;
   never from bot, channel, lookup, sentence, settings, etc.
6. Builder modules must not import bot or channel.
7. Every discovered shipped module must belong to a layer set (no silent
   unclassified files, including future nested packages).
8. The HTTP adapter package ``src/wuwaterm_api`` may import ONLY
   ``wuwaterm.application``, ``wuwaterm.models``,
   ``wuwaterm.translation_policy``, ``wuwaterm.logging_utils`` and
   ``wuwaterm.request_trace`` — never the bot, channel, lookup, sentence, db,
   telegram_* or builder modules, never the ``wuwaterm`` package root, and
   never the Telegram SDK. This is the machine-checkable form of "the API
   cannot bypass the shared pipeline". ``request_trace`` is on that list as an
   instrument, not as a way in: it holds no dictionary, no pipeline and no
   transport, imports nothing from this package at all, and is what the API
   uses to time the stages the CORE runs (the model slot and the POST).
9. Modules under ``src/wuwaterm`` must not import private ``_name`` symbols
   from sibling modules (relative ``from .mod import _x`` or absolute
   ``from wuwaterm.mod import _x``). Intentionally shared helpers are promoted
   to public names; local ``public as _alias`` rebindings remain allowed.
10. The flat ``scripts/`` deployment tools are checked for import cycles over
   real AST imports, including imports inside function bodies. Verification
   primitives sit below the admission layer that calls them: a module in
   ``SCRIPTS_ONE_WAY`` may not import the module it is called from, and no new
   strongly connected component may appear — a two-module mutual import is one
   cycle shape, not the only one.
11. Pure matching modules (``attribution_candidates``, ``term_matching``,
   ``term_preparation``, ``cjk_span``, ``document_anchor``, ``protected_text``,
   ``segmentation``, ``english_plural``, ``single_char_context``,
   ``skill_references``, ``term_classification``, ``termbase``, ``text_spans``)
   must not import
   storage ``db``, storage-coupled modules (``lookup``, ``review``), or
   ``sqlite3`` (including under ``TYPE_CHECKING``).
12. Dynamic tool imports in ``scripts/deployment_admission.py`` via
   ``_tool_module`` / ``_tool_modules`` must be statically discoverable and
   listed in ``DECLARED_DYNAMIC_TOOL_DEPS``. Every declared tool must exist
   in ``scripts/`` and ``component_journal_runtime.TOOL_PATHS`` (where
   applicable), must not point to a forbidden higher layer (such as
   ``component_handoff_finish``), and must not be stale.

Companion gates (not duplicated here):
- tests/test_runtime_imports.py — runtime import without pypinyin
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "wuwaterm"
SCRIPTS = ROOT / "scripts"

# The HTTP adapter is a separate shipped package. It is allowed to reach the
# shared application layer and the protocol-neutral helpers, and nothing else
# inside wuwaterm. This allowlist is the machine-checkable form of "the API
# cannot bypass the shared translation pipeline": if it could import lookup,
# sentence, db or bot directly it could grow a second, divergent pipeline.
API_PACKAGE = ROOT / "src" / "wuwaterm_api"
API_ALLOWED_WUWATERM_MODULES = frozenset(
    {
        "application",
        "models",
        "translation_policy",
        "logging_utils",
        "request_trace",
    }
)

DOMAIN_CORE = frozenset(
    {
        "attribution_candidates",
        "lookup",
        "native_kernel",
        "normalize",
        "models",
        "review",
        "cjk_span",
        "protected_text",
        "english_plural",
        "segmentation",
        "semantic_review_contract",
        "single_char_context",
        "skill_references",
        "structural_checks",
        "term_classification",
        "term_matching",
        "term_preparation",
        "termbase",
        "text_spans",
        "document_anchor",
    }
)
DOMAIN_LLM = frozenset({"sentence", "translation_plan", "llm_transport", "semantic_review"})
# Pure matching / text-structure modules inside DOMAIN_CORE that must remain
# independent of storage (`db`), storage-coupled (`lookup`, `review`), and `sqlite3`.
PURE_MATCHING = frozenset(
    {
        "attribution_candidates",
        "native_kernel",
        "term_matching",
        "term_preparation",
        "cjk_span",
        "protected_text",
        "segmentation",
        "english_plural",
        "single_char_context",
        "skill_references",
        "structural_checks",
        "term_classification",
        "termbase",
        "text_spans",
        "document_anchor",
    }
)
# Storage and storage-coupled domain modules forbidden to PURE_MATCHING.
PURE_MATCHING_FORBIDDEN_STORAGE = frozenset({"db", "lookup", "review"})
# Protocol-neutral orchestration used by command, API and in-process web
# translation. Linked-channel auto-translation owns specialized channel
# orchestration while sharing lower-level term and sentence primitives. This
# module sits above domain/LLM and below presentation: callers import it; it
# imports none of them.
APPLICATION = frozenset({"application"})
SHARED = frozenset(
    {"translation_policy", "runtime_keys", "constants", "exclusion_metadata"}
)
PRESENTATION = frozenset({"bot", "channel", "telegram_html", "telegram_text"})
LOCAL_STATE = frozenset(
    {
        "settings",
        "channel_reply_index",
        "channel_reply_schema",
        "channel_runtime",
        "logging_utils",
        "request_trace",
    }
)
STORAGE = frozenset({"db"})
BUILDER = frozenset({"builder", "data_source", "build_pinyin"})
BOOTSTRAP = frozenset({"cli"})

# Modules that must never depend on Telegram presentation or the Bot SDK.
NO_TELEGRAM_PRESENTATION = (
    DOMAIN_CORE | DOMAIN_LLM | APPLICATION | SHARED | LOCAL_STATE | STORAGE | BUILDER
)

# Modules that must never pull builder-only graph into the bot edge.
NO_BUILDER_IMPORTS = (
    PRESENTATION | DOMAIN_CORE | DOMAIN_LLM | APPLICATION | SHARED | LOCAL_STATE
)

# Who may import build_pinyin at all (including lazy imports).
BUILD_PINYIN_ALLOWED_IMPORTERS = frozenset({"db", "builder", "build_pinyin"})

# Every shipped module must be classified into exactly one layer set so new
# files cannot silently escape every rule.
ALL_CLASSIFIED = (
    DOMAIN_CORE
    | DOMAIN_LLM
    | APPLICATION
    | SHARED
    | PRESENTATION
    | LOCAL_STATE
    | STORAGE
    | BUILDER
    | BOOTSTRAP
)

TELEGRAM_SDK_PREFIXES = (
    "telegram",
    "telegram.ext",
)

# One-way edges inside the flat deployment tools. ``component_handoff`` holds
# the predecessor primitives; ``deployment_admission`` calls them while it
# admits a recovery, so the handoff module must never import admission back.
# An AST walk over function bodies still sees that reverse edge, which is why
# the rule cannot be satisfied by keeping the import lazy.
SCRIPTS_ONE_WAY = {
    "component_handoff": frozenset({"deployment_admission"}),
}

# Import cycles that already exist among the flat deployment tools. They
# predate this rule and belong to other subsystems, so they are pinned instead
# of enforced here: any cycle this set does not name is refused, and an entry
# that no longer exists has to be deleted, so the record cannot go stale.
KNOWN_SCRIPTS_CYCLES = frozenset({
    frozenset({"component_build", "component_build_isolation", "component_wheel_build"}),
    frozenset({"component_deployment", "component_full", "component_runtime"}),
})

# Higher-layer or self/boundary-checker modules that deployment_admission must
# never dynamically load via _tool_module / _tool_modules (preserving the
# one-way DAG even across dynamic loads).
ADMISSION_FORBIDDEN_DYNAMIC_TOOLS = frozenset(
    {
        "component_handoff_finish",
        "component_full",
        "component_transaction",
        "component_deployment",
        "deployment_admission",
        "check_architecture_boundaries",
    }
)

# Legacy tool scripts loaded by deployment_admission that predate the
# component TOOL_PATHS manifest in component_journal_runtime.
ADMISSION_NON_TOOL_PATHS_TOOLS = frozenset({"runtime_update"})


def _module_key_for_path(path: Path) -> str | None:
    """Return classification key for a path under PACKAGE, or None to skip."""
    rel = path.relative_to(PACKAGE)
    if path.name == "__init__.py":
        # Package root __init__ is not a classified "module edge" today.
        # Nested package initializers (domain/__init__.py) MUST be scanned —
        # otherwise forbidden imports hide in __init__ and escape every rule.
        if len(rel.parts) == 1:
            return None
        return ".".join(rel.parts[:-1])
    return ".".join(rel.with_suffix("").parts)


def _pkg_modules() -> dict[str, Path]:
    """Map local module keys to paths; see ``_discover_modules`` for collisions."""
    modules, _collisions = _discover_modules()
    return modules


def _discover_modules() -> tuple[dict[str, Path], list[str]]:
    """Discover shipped modules and report duplicate classification keys.

    Top-level files use their stem (``bot``). Nested files use dotted relative
    keys (``domain.helper`` for ``src/wuwaterm/domain/helper.py``;
    ``domain`` for ``src/wuwaterm/domain/__init__.py``).

    If both ``foo.py`` and ``foo/__init__.py`` exist they share key ``foo``;
    Python loads the package initializer, so silent overwrite would skip the
    actually-executed file — treat that as a failure instead.
    """
    modules: dict[str, Path] = {}
    collisions: list[str] = []
    if not PACKAGE.is_dir():
        return modules, collisions
    for path in sorted(PACKAGE.rglob("*.py")):
        key = _module_key_for_path(path)
        if key is None:
            continue
        if key in modules:
            prev = modules[key]
            try:
                prev_rel = prev.relative_to(PACKAGE).as_posix()
                path_rel = path.relative_to(PACKAGE).as_posix()
            except ValueError:
                prev_rel, path_rel = prev.as_posix(), path.as_posix()
            collisions.append(
                f"duplicate module key {key!r}: {prev_rel} and {path_rel} "
                f"(refuse silent overwrite; package initializer is what Python loads)"
            )
        modules[key] = path
    return modules, collisions


def _importer_package_parts(path: Path) -> tuple[str, ...]:
    """Package parts that relative imports resolve against for ``path``."""
    try:
        rel = path.relative_to(PACKAGE)
    except ValueError:
        return ()
    if path.name == "__init__.py":
        return rel.parts[:-1]
    return rel.with_suffix("").parts[:-1]


def _resolve_relative_import(
    importer: Path, level: int, module: str | None, name: str | None = None
) -> str:
    """Resolve a relative import to a package-local dotted key when possible."""
    pkg_parts = list(_importer_package_parts(importer))
    # PEP 328: level is the number of leading dots. level=1 → current package.
    up = max(level - 1, 0)
    if up > len(pkg_parts):
        base: list[str] = []
    else:
        base = pkg_parts[: len(pkg_parts) - up]
    if module:
        return ".".join(base + module.split(".")) if base else module
    if name:
        return ".".join(base + [name.split(".", 1)[0]]) if base else name.split(".", 1)[0]
    return ".".join(base)


def _expand_local_keys(key: str) -> list[str]:
    """Emit a local key and every parent package prefix.

    ``from .ui.helper import X`` must record both ``ui.helper`` and ``ui``:
    Python executes ``ui/__init__.py`` before loading the submodule, so a
    forbidden edge in the package initializer is still an import-time edge.
    """
    if not key:
        return []
    parts = key.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _append_local_events(
    events: list[tuple[str, bool, int]],
    key: str,
    type_only: bool,
    lineno: int,
) -> None:
    for item in _expand_local_keys(key):
        events.append((item, type_only, lineno))


def _iter_import_events(path: Path) -> list[tuple[str, bool, int]]:
    """Return (imported_name, type_checking_only, lineno) for local + external.

    Local names keep full dotted keys when nested (e.g. ``domain.helper``),
    matching ``_pkg_modules`` keys so layer intersections stay fail-closed.
    Nested imports also emit parent package prefixes (``ui.helper`` → ``ui``).
    External names remain dotted as imported (e.g. ``telegram.ext``).
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    type_checking_nodes: set[ast.AST] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            # Only the true branch is type-checking-only. Imports in `else:`
            # execute at runtime and must still face boundary rules.
            for child in ast.walk(ast.Module(body=list(node.body), type_ignores=[])):
                type_checking_nodes.add(child)

    events: list[tuple[str, bool, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            type_only = node in type_checking_nodes
            if node.level:
                if module:
                    # from ..ui import helper  → ``ui`` and ``ui.helper``.
                    # from .ui.helper import X → ``ui``, ``ui.helper`` (+ X form).
                    base_key = _resolve_relative_import(path, node.level, module)
                    _append_local_events(events, base_key, type_only, node.lineno)
                    for entry in node.names:
                        if entry.name == "*":
                            continue
                        sub = entry.name.split(".", 1)[0]
                        sub_key = f"{base_key}.{sub}" if base_key else sub
                        _append_local_events(events, sub_key, type_only, node.lineno)
                else:
                    for entry in node.names:
                        key = _resolve_relative_import(
                            path, node.level, None, name=entry.name
                        )
                        _append_local_events(events, key, type_only, node.lineno)
            elif module.startswith("wuwaterm."):
                # Keep full nested path: wuwaterm.domain.helper → domain.helper
                rest = module[len("wuwaterm.") :]
                _append_local_events(events, rest, type_only, node.lineno)
                for entry in node.names:
                    if entry.name == "*":
                        continue
                    sub = entry.name.split(".", 1)[0]
                    _append_local_events(
                        events, f"{rest}.{sub}", type_only, node.lineno
                    )
            elif module == "wuwaterm":
                for entry in node.names:
                    _append_local_events(
                        events,
                        entry.name.split(".", 1)[0],
                        type_only,
                        node.lineno,
                    )
            else:
                events.append((module, type_only, node.lineno))
        elif isinstance(node, ast.Import):
            for entry in node.names:
                raw = entry.name
                name = _normalize_imported_name(raw)
                # Only expand package-local absolute imports; leave third-party
                # dotted names (telegram.ext, httpx) as a single external event.
                if raw == "wuwaterm" or raw.startswith("wuwaterm."):
                    _append_local_events(
                        events, name, node in type_checking_nodes, node.lineno
                    )
                else:
                    events.append((name, node in type_checking_nodes, node.lineno))
    return events


def _normalize_imported_name(name: str) -> str:
    """Map absolute package imports to local keys when possible.

    ``import wuwaterm.bot`` → ``bot``;
    ``import wuwaterm.domain.helper`` → ``domain.helper`` (full nested key).
    """
    if name == "wuwaterm":
        return name
    if name.startswith("wuwaterm."):
        return name[len("wuwaterm.") :]
    return name


def _is_type_checking_test(test: ast.expr) -> bool:
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    # if TYPE_CHECKING and <other>: still type-only for the true branch.
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return any(_is_type_checking_test(value) for value in test.values)
    return False


def _is_telegram_sdk(name: str) -> bool:
    return name == "telegram" or name.startswith("telegram.")


def _sentence_legacy_bridge_lines(path: Path) -> set[int]:
    """Allow only specific lazy imports inside the old forwarding methods."""
    allowed = {
        "_restore_html": "restore_html",
        "translate_html": "translate_html",
        "translate_html_async": "translate_html_async",
    }
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: set[int] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "SentenceTranslator":
            continue
        for method in node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            expected = allowed.get(method.name)
            if expected is None:
                continue
            for statement in method.body:
                if (
                    isinstance(statement, ast.ImportFrom)
                    and statement.level == 1
                    and statement.module == "telegram_html"
                    and len(statement.names) == 1
                    and statement.names[0].name == expected
                    and statement.names[0].asname is None
                ):
                    lines.add(statement.lineno)
    return lines


def _api_wuwaterm_imports(path: Path) -> list[tuple[str, int]]:
    """Return (imported wuwaterm target, lineno) for one adapter module.

    ``wuwaterm`` on its own is reported as the empty string: importing the
    package root is too broad to classify, so it is refused. Relative imports
    stay inside ``wuwaterm_api`` and are not reported.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            if module == "wuwaterm":
                for entry in node.names:
                    found.append((entry.name.split(".", 1)[0], node.lineno))
            elif module.startswith("wuwaterm."):
                found.append((module[len("wuwaterm.") :].split(".", 1)[0], node.lineno))
        elif isinstance(node, ast.Import):
            for entry in node.names:
                name = entry.name
                if name == "wuwaterm":
                    found.append(("", node.lineno))
                elif name.startswith("wuwaterm."):
                    found.append(
                        (name[len("wuwaterm.") :].split(".", 1)[0], node.lineno)
                    )
    return found


def _api_external_imports(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            found.append((node.module, node.lineno))
        elif isinstance(node, ast.Import):
            for entry in node.names:
                found.append((entry.name, node.lineno))
    return found


def check_api_package() -> list[str]:
    """Fail closed on anything the HTTP adapter must not reach.

    TYPE_CHECKING is not an exemption here either: a type-only import of the
    bot would still document a dependency the adapter is not allowed to have.
    """
    failures: list[str] = []
    if not API_PACKAGE.is_dir():
        return failures
    for path in sorted(API_PACKAGE.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for target, lineno in _api_wuwaterm_imports(path):
            if not target:
                failures.append(
                    f"{rel}:{lineno}: must import specific wuwaterm modules "
                    f"({', '.join(sorted(API_ALLOWED_WUWATERM_MODULES))}), "
                    f"not the wuwaterm package root"
                )
            elif target not in API_ALLOWED_WUWATERM_MODULES:
                failures.append(
                    f"{rel}:{lineno}: wuwaterm_api may only import "
                    f"{sorted(API_ALLOWED_WUWATERM_MODULES)} from wuwaterm; "
                    f"found wuwaterm.{target}"
                )
        for name, lineno in _api_external_imports(path):
            if _is_telegram_sdk(name):
                failures.append(
                    f"{rel}:{lineno}: wuwaterm_api must not import the Telegram "
                    f"SDK ({name})"
                )
    return failures


def _scripts_modules() -> dict[str, Path]:
    """Map each flat ``scripts/`` module stem to its file."""
    return {path.stem: path for path in sorted(SCRIPTS.glob("*.py"))}


def _scripts_local_imports(path: Path, modules: dict[str, Path]) -> dict[str, int]:
    """Return {local module stem: first importing line} for one tool module.

    ``ast.walk`` descends into function and method bodies, so a lazily imported
    reverse edge is reported exactly like a module-level one. Only sibling
    ``scripts/`` modules count; standard-library and third-party imports are
    not part of this graph, and package-relative imports cannot occur in a
    flat tool directory.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for entry in node.names:
                parts = entry.name.split(".")
                stem = parts[1] if parts[0] == "scripts" and len(parts) > 1 else parts[0]
                if stem in modules and stem != path.stem:
                    found.setdefault(stem, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            if module == "scripts" or module.startswith("scripts."):
                if module != "scripts":
                    stem = module.split(".")[1]
                    if stem in modules and stem != path.stem:
                        found.setdefault(stem, node.lineno)
                else:
                    for entry in node.names:
                        name = entry.name.split(".")[0]
                        if name in modules and name != path.stem:
                            found.setdefault(name, node.lineno)
                continue
            stem = module.split(".")[0]
            if stem in modules and stem != path.stem:
                found.setdefault(stem, node.lineno)
    return found


def _scripts_cycle_components(edges: dict[str, dict[str, int]]) -> list[frozenset[str]]:
    """Return every strongly connected component of the tool graph that cycles.

    Tarjan's algorithm over the ``scripts/`` graph. A single module is acyclic
    by inspection (``_scripts_local_imports`` already drops a self-import), so
    meaning starts at two members: every component reported here is a real
    cycle of whatever length it has.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[frozenset[str]] = []
    counter = 0

    def connect(node: str) -> None:
        nonlocal counter
        index[node] = low[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(edges.get(node, {})):
            if target not in index:
                connect(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], index[target])
        if low[node] == index[node]:
            members: set[str] = set()
            while True:
                item = stack.pop()
                on_stack.discard(item)
                members.add(item)
                if item == node:
                    break
            if len(members) > 1:
                components.append(frozenset(members))

    for node in sorted(edges):
        if node not in index:
            connect(node)
    return components


def check_scripts_import_cycles() -> list[str]:
    """Fail closed on forbidden or new import cycles among deployment tools.

    ``deployment_admission`` is the read-only layer every deployment tool
    calls. It resolves a tool by name and proves the loaded source, so it can
    call a tool back without depending on import order; what must not happen is
    a *static* import cycle of any length. Comparing mutual pairs only would
    still miss the three-module cycles the builder and transaction graphs
    already have, so the walk uses strongly connected components.
    """
    modules = _scripts_modules()
    edges = {name: _scripts_local_imports(path, modules) for name, path in modules.items()}
    failures: list[str] = []
    for importer in sorted(SCRIPTS_ONE_WAY):
        forbidden = SCRIPTS_ONE_WAY[importer] & set(edges.get(importer, {}))
        for target in sorted(forbidden):
            failures.append(
                f"scripts/{importer}.py:{edges[importer][target]}: {importer} must not "
                f"import {target} (one-way edge: {target} imports {importer})"
            )
    cycles = _scripts_cycle_components(edges)
    for component in sorted(cycles, key=sorted):
        if component in KNOWN_SCRIPTS_CYCLES:
            continue
        inner = sorted(
            f"{name} -> {target} at scripts/{name}.py:{line}"
            for name in component
            for target, line in edges[name].items()
            if target in component
        )
        failures.append(
            "scripts: new import cycle among " + ", ".join(sorted(component))
            + " (" + "; ".join(inner) + ")"
        )
    for component in sorted(KNOWN_SCRIPTS_CYCLES, key=sorted):
        if component not in cycles:
            failures.append(
                "scripts: recorded import cycle among " + ", ".join(sorted(component))
                + " no longer exists; delete it from KNOWN_SCRIPTS_CYCLES"
            )
    return failures


def _extract_string_collection(node: ast.AST) -> tuple[list[str], bool]:
    """Extract string literals from a set/tuple/list or frozenset(...) AST."""
    inner: ast.AST = node
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "set", "tuple", "list"}
        and len(node.args) == 1
        and not node.keywords
    ):
        inner = node.args[0]
    if not isinstance(inner, (ast.Set, ast.Tuple, ast.List)):
        return [], False
    items: list[str] = []
    for element in inner.elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            items.append(element.value)
        else:
            return [], False
    return items, True


def _module_level_assignment_value(
    tree: ast.Module, name: str
) -> tuple[ast.AST | None, int]:
    """Return (rhs_node, assignment_count) for top-level ``name = ...``."""
    found: ast.AST | None = None
    count = 0
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    found = statement.value
                    count += 1
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
            and statement.value is not None
        ):
            found = statement.value
            count += 1
    return found, count


def _extract_tool_paths(scripts_dir: Path) -> tuple[set[str] | None, list[str]]:
    """Read ``TOOL_PATHS`` from ``component_journal_runtime.py`` via AST."""
    failures: list[str] = []
    journal_path = scripts_dir / "component_journal_runtime.py"
    if not journal_path.is_file():
        failures.append(
            "scripts/component_journal_runtime.py is missing (cannot verify TOOL_PATHS)"
        )
        return None, failures
    tree = ast.parse(
        journal_path.read_text(encoding="utf-8"), filename=str(journal_path)
    )
    rhs, count = _module_level_assignment_value(tree, "TOOL_PATHS")
    if count != 1 or rhs is None:
        failures.append(
            "scripts/component_journal_runtime.py: expected a single top-level TOOL_PATHS assignment"
        )
        return None, failures
    items, ok = _extract_string_collection(rhs)
    if not ok:
        failures.append(
            "scripts/component_journal_runtime.py: TOOL_PATHS must be a literal collection of strings"
        )
        return None, failures
    return set(items), failures


def _extract_attempt_producer_modules(tree: ast.Module) -> tuple[set[str], list[str]]:
    """Extract statically declared module names from ``_ATTEMPT_PRODUCERS``."""
    failures: list[str] = []
    rhs, count = _module_level_assignment_value(tree, "_ATTEMPT_PRODUCERS")
    if count == 0 or rhs is None:
        return set(), failures
    if count > 1 or not isinstance(rhs, ast.Dict):
        failures.append(
            "scripts/deployment_admission.py: _ATTEMPT_PRODUCERS must be a single dict literal"
        )
        return set(), failures
    modules: set[str] = set()
    for value in rhs.values:
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "_AttemptProducer"
        ):
            failures.append(
                f"scripts/deployment_admission.py:{value.lineno}: _ATTEMPT_PRODUCERS values must be _AttemptProducer(...) calls"
            )
            continue
        module_expr: ast.AST | None = value.args[0] if value.args else None
        if module_expr is None:
            for kw in value.keywords:
                if kw.arg == "module":
                    module_expr = kw.value
                    break
        if isinstance(module_expr, ast.Constant) and isinstance(module_expr.value, str):
            modules.add(module_expr.value)
        else:
            failures.append(
                f"scripts/deployment_admission.py:{value.lineno}: _AttemptProducer module must be a string literal"
            )
    return modules, failures


def _is_attempt_producers_subscript(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "_ATTEMPT_PRODUCERS"
    )


def _is_attempt_producers_module_attr(node: ast.AST) -> bool:
    """Match direct ``_ATTEMPT_PRODUCERS[<subscript>].module`` syntax only."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "module"
        and _is_attempt_producers_subscript(node.value)
    )


def _collect_scope_aliases(
    scope_node: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    enclosing_importlib: set[str],
    enclosing_import_module: set[str],
    enclosing_tool_module: set[str],
    enclosing_tool_modules: set[str],
) -> tuple[set[str], set[str], set[str], set[str], list[tuple[str, int]]]:
    """Collect aliases of ``importlib``, ``import_module``, and ``_tool_module*``."""
    importlib_aliases = set(enclosing_importlib)
    import_module_aliases = set(enclosing_import_module)
    tool_module_aliases = set(enclosing_tool_module)
    tool_modules_aliases = set(enclosing_tool_modules)
    wildcard_errors: list[tuple[str, int]] = []

    direct_nodes: list[ast.AST] = []

    def gather_nodes(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            direct_nodes.append(stmt)
            if isinstance(stmt, ast.Import):
                for entry in stmt.names:
                    if entry.name == "importlib":
                        importlib_aliases.add(entry.asname or "importlib")
                    elif entry.name.startswith("importlib.") and not entry.asname:
                        importlib_aliases.add("importlib")
            elif isinstance(stmt, ast.ImportFrom) and stmt.level == 0 and stmt.module == "importlib":
                for entry in stmt.names:
                    if entry.name == "*":
                        wildcard_errors.append(("from importlib import *", stmt.lineno))
                    elif entry.name == "import_module":
                        import_module_aliases.add(entry.asname or "import_module")
            for child_attr in ("body", "orelse", "finalbody"):
                child = getattr(stmt, child_attr, None)
                if isinstance(child, list):
                    gather_nodes(child)
            if isinstance(stmt, ast.Try):
                for handler in stmt.handlers:
                    gather_nodes(handler.body)
            if isinstance(stmt, ast.Match):
                for case in stmt.cases:
                    gather_nodes(case.body)

    gather_nodes(scope_node.body)

    def bind_from_expr(target_name: str, expr: ast.AST) -> bool:
        changed = False
        if isinstance(expr, ast.Name):
            if expr.id in importlib_aliases and target_name not in importlib_aliases:
                importlib_aliases.add(target_name)
                changed = True
            if expr.id in import_module_aliases and target_name not in import_module_aliases:
                import_module_aliases.add(target_name)
                changed = True
            if expr.id in tool_module_aliases and target_name not in tool_module_aliases:
                tool_module_aliases.add(target_name)
                changed = True
            if expr.id in tool_modules_aliases and target_name not in tool_modules_aliases:
                tool_modules_aliases.add(target_name)
                changed = True
        elif (
            isinstance(expr, ast.Attribute)
            and expr.attr == "import_module"
            and isinstance(expr.value, ast.Name)
            and expr.value.id in importlib_aliases
            and target_name not in import_module_aliases
        ):
            import_module_aliases.add(target_name)
            changed = True
        elif (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "getattr"
            and len(expr.args) >= 2
            and isinstance(expr.args[0], ast.Name)
            and expr.args[0].id in importlib_aliases
            and isinstance(expr.args[1], ast.Constant)
            and expr.args[1].value == "import_module"
            and target_name not in import_module_aliases
        ):
            import_module_aliases.add(target_name)
            changed = True
        return changed

    changed = True
    while changed:
        changed = False
        for stmt in direct_nodes:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                changed |= bind_from_expr(stmt.targets[0].id, stmt.value)
            elif (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.value is not None
            ):
                changed |= bind_from_expr(stmt.target.id, stmt.value)
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.NamedExpr):
                    changed |= bind_from_expr(sub.target.id, sub.value)

    return (
        importlib_aliases,
        import_module_aliases,
        tool_module_aliases,
        tool_modules_aliases,
        wildcard_errors,
    )


def _is_raw_import_module_call(
    node: ast.Call, importlib_aliases: set[str], import_module_aliases: set[str]
) -> bool:
    if isinstance(node.func, ast.Name) and node.func.id in import_module_aliases:
        return True
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "import_module"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in importlib_aliases
    ):
        return True
    if (
        isinstance(node.func, ast.Call)
        and isinstance(node.func.func, ast.Name)
        and node.func.func.id == "getattr"
        and len(node.func.args) >= 2
        and isinstance(node.func.args[0], ast.Name)
        and node.func.args[0].id in importlib_aliases
        and isinstance(node.func.args[1], ast.Constant)
        and node.func.args[1].value == "import_module"
    ):
        return True
    return False


def _discover_admission_dynamic_calls(
    tree: ast.Module, producer_modules: set[str]
) -> tuple[list[tuple[str, int]], list[str]]:
    """Discover all ``_tool_module`` / ``_tool_modules`` tool names via AST.

    Only two syntactic argument shapes are admitted: a string literal or a
    direct ``_ATTEMPT_PRODUCERS[<subscript>].module`` expression. No
    intermediate variables or dataflow tracking are permitted.
    """
    discovered: list[tuple[str, int]] = []
    failures: list[str] = []

    def analyze_scope(
        scope_node: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        func_name: str | None,
        enclosing_importlib: set[str],
        enclosing_import_module: set[str],
        enclosing_tool_module: set[str],
        enclosing_tool_modules: set[str],
    ) -> None:
        (
            importlib_aliases,
            import_module_aliases,
            tool_module_aliases,
            tool_modules_aliases,
            wildcard_errors,
        ) = _collect_scope_aliases(
            scope_node,
            enclosing_importlib,
            enclosing_import_module,
            enclosing_tool_module,
            enclosing_tool_modules,
        )
        for text, lineno in wildcard_errors:
            failures.append(
                f"scripts/deployment_admission.py:{lineno}: wildcard {text!r} is forbidden"
            )

        def visit_node(node: ast.AST) -> None:
            if node is not scope_node:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    analyze_scope(
                        node,
                        node.name,
                        importlib_aliases,
                        import_module_aliases,
                        tool_module_aliases,
                        tool_modules_aliases,
                    )
                    return
                if isinstance(node, ast.ClassDef):
                    analyze_scope(
                        node,
                        func_name,
                        importlib_aliases,
                        import_module_aliases,
                        tool_module_aliases,
                        tool_modules_aliases,
                    )
                    return
            if isinstance(node, ast.Call):
                if _is_raw_import_module_call(
                    node, importlib_aliases, import_module_aliases
                ):
                    allowed_wrapper = (
                        func_name == "_tool_modules"
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "importlib"
                        and node.func.attr == "import_module"
                        and len(node.args) == 1
                        and not node.keywords
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == "name"
                    )
                    if not allowed_wrapper:
                        failures.append(
                            f"scripts/deployment_admission.py:{node.lineno}: raw import_module call bypasses _tool_module/_tool_modules"
                        )
                elif isinstance(node.func, ast.Name) and (
                    node.func.id in tool_module_aliases
                    or node.func.id in tool_modules_aliases
                ):
                    is_single = node.func.id in tool_module_aliases
                    is_wrapper = (
                        func_name == "_tool_module"
                        and node.func.id == "_tool_modules"
                        and len(node.args) == 1
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == "name"
                    )
                    if not is_wrapper:
                        name_exprs: list[ast.AST] = list(node.args)
                        if is_single:
                            for kw in node.keywords:
                                if kw.arg == "name":
                                    name_exprs.append(kw.value)
                        if not name_exprs:
                            failures.append(
                                f"scripts/deployment_admission.py:{node.lineno}: {node.func.id}() missing tool name argument"
                            )
                        for arg_expr in name_exprs:
                            if isinstance(arg_expr, ast.Constant) and isinstance(
                                arg_expr.value, str
                            ):
                                discovered.append((arg_expr.value, node.lineno))
                            elif _is_attempt_producers_module_attr(arg_expr):
                                if not producer_modules:
                                    failures.append(
                                        f"scripts/deployment_admission.py:{node.lineno}: _ATTEMPT_PRODUCERS has no declared producer modules"
                                    )
                                else:
                                    for pmod in sorted(producer_modules):
                                        discovered.append((pmod, node.lineno))
                            else:
                                failures.append(
                                    f"scripts/deployment_admission.py:{node.lineno}: dynamic tool argument "
                                    f"{ast.unparse(arg_expr)!r} in {node.func.id}() is not statically discoverable"
                                )
            for child in ast.iter_child_nodes(node):
                visit_node(child)

        visit_node(scope_node)

    analyze_scope(
        tree,
        None,
        set(),
        set(),
        {"_tool_module"},
        {"_tool_modules"},
    )
    return discovered, failures


def check_deployment_admission_manifest() -> list[str]:
    """Verify ``DECLARED_DYNAMIC_TOOL_DEPS`` in ``scripts/deployment_admission.py``."""
    admission_path = SCRIPTS / "deployment_admission.py"
    if not admission_path.is_file():
        return ["scripts/deployment_admission.py is missing"]

    tree = ast.parse(
        admission_path.read_text(encoding="utf-8"), filename=str(admission_path)
    )
    failures: list[str] = []

    rhs, count = _module_level_assignment_value(tree, "DECLARED_DYNAMIC_TOOL_DEPS")
    declared: set[str] | None = None
    if count != 1 or rhs is None:
        failures.append(
            "scripts/deployment_admission.py: must define a single top-level DECLARED_DYNAMIC_TOOL_DEPS manifest"
        )
    else:
        items, ok = _extract_string_collection(rhs)
        if not ok:
            failures.append(
                "scripts/deployment_admission.py: DECLARED_DYNAMIC_TOOL_DEPS must be a literal collection of strings"
            )
        else:
            if len(items) != len(set(items)):
                failures.append(
                    "scripts/deployment_admission.py: DECLARED_DYNAMIC_TOOL_DEPS contains duplicate entries"
                )
            declared = set(items)

    producer_modules, producer_failures = _extract_attempt_producer_modules(tree)
    failures.extend(producer_failures)

    discovered_calls, call_failures = _discover_admission_dynamic_calls(
        tree, producer_modules
    )
    failures.extend(call_failures)

    if declared is None:
        return failures

    discovered_lines: dict[str, list[int]] = {}
    for name, lineno in discovered_calls:
        discovered_lines.setdefault(name, []).append(lineno)
    discovered = set(discovered_lines)

    for name in sorted(discovered - declared):
        lines = ", ".join(str(ln) for ln in sorted(set(discovered_lines[name])))
        failures.append(
            f"scripts/deployment_admission.py:{lines}: dynamic tool {name!r} "
            f"is not listed in DECLARED_DYNAMIC_TOOL_DEPS"
        )

    for name in sorted(declared - discovered):
        failures.append(
            f"scripts/deployment_admission.py: stale entry {name!r} in "
            f"DECLARED_DYNAMIC_TOOL_DEPS is never loaded via _tool_module/_tool_modules"
        )

    modules = _scripts_modules()
    edges = {
        mod: _scripts_local_imports(mod_path, modules)
        for mod, mod_path in modules.items()
    }
    for name in sorted(declared | discovered):
        if name in ADMISSION_FORBIDDEN_DYNAMIC_TOOLS or (
            name in edges and "component_handoff_finish" in edges[name]
        ):
            failures.append(
                f"scripts/deployment_admission.py: forbidden higher-layer dynamic tool dependency {name!r}"
            )

    tool_paths, tool_path_failures = _extract_tool_paths(SCRIPTS)
    failures.extend(tool_path_failures)

    for name in sorted(declared):
        if name not in modules:
            failures.append(
                f"scripts/deployment_admission.py: declared dynamic tool {name!r} "
                f"does not exist in scripts/"
            )
        elif (
            tool_paths is not None
            and name not in ADMISSION_NON_TOOL_PATHS_TOOLS
            and f"scripts/{name}.py" not in tool_paths
        ):
            failures.append(
                f"scripts/deployment_admission.py: declared dynamic tool {name!r} "
                f"is missing from component_journal_runtime.TOOL_PATHS"
            )

    return failures


def _is_private_symbol_name(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _iter_private_cross_module_imports(path: Path) -> list[tuple[str, str, int]]:
    """Return (target_module, symbol, lineno) for private `_name` imports."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if node.level:
            target = _resolve_relative_import(path, node.level, node.module) or "wuwaterm"
        elif module == "wuwaterm":
            target = "wuwaterm"
        elif module.startswith("wuwaterm."):
            target = module[len("wuwaterm.") :]
        else:
            continue
        for entry in node.names:
            symbol = entry.name.split(".", 1)[0]
            if symbol != "*" and _is_private_symbol_name(symbol):
                found.append((target, symbol, node.lineno))
    return found


def check() -> list[str]:
    modules, collisions = _discover_modules()
    failures: list[str] = list(collisions)
    failures.extend(check_api_package())
    failures.extend(check_scripts_import_cycles())
    failures.extend(check_deployment_admission_manifest())

    unclassified = sorted(set(modules) - ALL_CLASSIFIED)
    if unclassified:
        failures.append(
            "unclassified modules (add each stem to exactly one layer set): "
            + ", ".join(unclassified)
        )

    for name, path in modules.items():
        try:
            rel = path.relative_to(ROOT).as_posix()
        except ValueError:
            # Tests may point PACKAGE at a temp tree outside the repo root.
            rel = path.as_posix()
        events = _iter_import_events(path)
        local_runtime = {
            imported
            for imported, type_only, _ in events
            if imported in modules and not type_only
        }
        local_all = {
            imported for imported, _type_only, _ in events if imported in modules
        }
        external_all = {
            imported for imported, _type_only, _ in events if imported not in modules
        }

        if name in NO_TELEGRAM_PRESENTATION:
            # Fail closed on type-only edges too: only channel has a documented
            # TYPE_CHECKING exception for BotConfig (handled below separately).
            legacy_lines = _sentence_legacy_bridge_lines(path) if name == "sentence" else set()
            bad_pres = sorted({
                imported for imported, _type_only, lineno in events
                if imported in PRESENTATION and not (
                    name == "sentence" and imported == "telegram_html" and lineno in legacy_lines
                )
            })
            if bad_pres:
                failures.append(
                    f"{rel}: domain/infra/builder module must not import "
                    f"presentation {bad_pres} (TYPE_CHECKING not exempt)"
                )
            sdk = sorted(n for n in external_all if _is_telegram_sdk(n))
            if sdk:
                failures.append(
                    f"{rel}: must not import Telegram SDK {sdk} "
                    f"(TYPE_CHECKING not exempt)"
                )

        if name in DOMAIN_CORE:
            bad_llm = sorted(local_all & DOMAIN_LLM)
            if bad_llm:
                failures.append(
                    f"{rel}: domain core must not import domain+LLM {bad_llm}"
                )

        if name in PURE_MATCHING:
            forbidden_storage = STORAGE | PURE_MATCHING_FORBIDDEN_STORAGE
            bad_storage = sorted(local_all & forbidden_storage)
            if bad_storage:
                failures.append(
                    f"{rel}: pure matching module must not import storage or "
                    f"storage-coupled modules {bad_storage} (TYPE_CHECKING not exempt)"
                )
            bad_sqlite = sorted(
                ext for ext in external_all if ext == "sqlite3" or ext.startswith("sqlite3.")
            )
            if bad_sqlite:
                failures.append(
                    f"{rel}: pure matching module must not import sqlite3 "
                    f"{bad_sqlite} (TYPE_CHECKING not exempt)"
                )

        if name in DOMAIN_LLM:
            bad = sorted(local_all & {"bot", "channel"})
            if bad:
                failures.append(
                    f"{rel}: sentence must not import presentation cores {bad}"
                )

        if name in NO_BUILDER_IMPORTS:
            bad_builder = sorted(local_all & BUILDER)
            # db is storage, not in NO_BUILDER_IMPORTS; presentation/domain must
            # not import any builder module even under TYPE_CHECKING.
            if bad_builder:
                failures.append(
                    f"{rel}: must not import builder-path modules {bad_builder}"
                )

        if name in PRESENTATION:
            # cli is bootstrap and imports builder at module load; presentation
            # must not reverse that edge even under TYPE_CHECKING.
            if "cli" in local_all:
                failures.append(
                    f"{rel}: presentation must not import bootstrap cli "
                    f"(cli pulls builder path)"
                )

        if name == "channel":
            # Runtime import of bot is forbidden; TYPE_CHECKING is the documented
            # exception for BotConfig annotations.
            if "bot" in local_runtime:
                failures.append(
                    f"{rel}: must not runtime-import bot "
                    f"(TYPE_CHECKING-only is allowed)"
                )

        if name in BUILDER:
            bad = sorted(local_all & {"bot", "channel"})
            if bad:
                failures.append(
                    f"{rel}: builder module must not import presentation {bad}"
                )

        if "build_pinyin" in local_all and name not in BUILD_PINYIN_ALLOWED_IMPORTERS:
            failures.append(
                f"{rel}: only db/builder may import build_pinyin "
                f"(found import of build_pinyin)"
            )

        for target, symbol, lineno in _iter_private_cross_module_imports(path):
            failures.append(
                f"{rel}:{lineno}: must not import private symbol {symbol!r} "
                f"from {target}"
            )

    # Explicit: presentation files must never mention build_pinyin import.
    for pres in sorted(PRESENTATION):
        if pres not in modules:
            continue
        path = modules[pres]
        for imported, _type_only, lineno in _iter_import_events(path):
            if imported == "build_pinyin":
                failures.append(
                    f"{path.relative_to(ROOT).as_posix()}:{lineno}: "
                    f"presentation must not import build_pinyin"
                )

    return failures


def main() -> int:
    if not PACKAGE.is_dir():
        print(f"missing package directory: {PACKAGE}", file=sys.stderr)
        return 2
    failures = check()
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("architecture boundary guard ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
