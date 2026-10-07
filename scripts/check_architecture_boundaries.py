#!/usr/bin/env python3
"""Fail closed on forbidden import directions among shipped modules.

Intended layers (must stay aligned with docs/architecture.md):

  domain core:     lookup, normalize, models, review, cjk_span
  domain+LLM:      sentence  (may use telegram_html; must not import bot/channel)
  application:     application  (protocol-neutral command/API/web pipeline;
                   must not import any presentation module, the Telegram SDK,
                   or builder modules)
  shared policy:   translation_policy, runtime_keys, constants
  presentation:    bot, channel, telegram_html, telegram_text
  local state:     settings, channel_reply_index, channel_reply_schema,
                   channel_runtime, logging_utils
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
   ``wuwaterm.translation_policy`` and ``wuwaterm.logging_utils`` — never the
   bot, channel, lookup, sentence, db, telegram_* or builder modules, never
   the ``wuwaterm`` package root, and never the Telegram SDK. This is the
   machine-checkable form of "the API cannot bypass the shared pipeline".

Companion gates (not duplicated here):
- scripts/check_non_goals.py — delivery-mode and channel-listener product pins
- tests/test_runtime_imports.py — runtime import without pypinyin
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "wuwaterm"

# The HTTP adapter is a separate shipped package. It is allowed to reach the
# shared application layer and the protocol-neutral helpers, and nothing else
# inside wuwaterm. This allowlist is the machine-checkable form of "the API
# cannot bypass the shared translation pipeline": if it could import lookup,
# sentence, db or bot directly it could grow a second, divergent pipeline.
API_PACKAGE = ROOT / "src" / "wuwaterm_api"
API_ALLOWED_WUWATERM_MODULES = frozenset(
    {"application", "models", "translation_policy", "logging_utils"}
)

CURRENT_CONTRACT_FILES = (
    ROOT / "src" / "wuwaterm" / "application.py",
    ROOT / "src" / "wuwaterm_api" / "app.py",
    ROOT / "scripts" / "check_architecture_boundaries.py",
    ROOT / "README.md",
    ROOT / "README.zh-CN.md",
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "web-presentation-layer.md",
    ROOT / "docs" / "adr" / "0001-telegram-as-presentation-layer.md",
    ROOT / "docs" / "adr" / "0009-http-api-adapter.md",
    ROOT / "docs" / "api" / "openapi.json",
)

# Build fragments from pieces so the guard can scan its own source without the
# denylisted prose appearing verbatim in this declaration.
STALE_CONTRACT_FRAGMENTS = tuple(
    " ".join(parts).casefold()
    for parts in (
        ("every", "inbound", "adapter"),
        ("are", "served", "by", "that", "one", "pipeline"),
        ("here", "and", "in", "the", "chat", "adapter"),
        ("shared", "by", "every", "inbound", "adapter"),
        ("both", "inbound", "adapters", "call", "it"),
        ("pipeline", "exactly", "once"),
        ("one", "shared", "translation", "pipeline"),
        ("shared", "by", "every", "adapter"),
        ("唯一一次持有", "dictionary-first", "翻译流水线"),
    )
)

DOMAIN_CORE = frozenset({"lookup", "normalize", "models", "review", "cjk_span"})
DOMAIN_LLM = frozenset({"sentence"})
# Protocol-neutral orchestration used by command, API and in-process web
# translation. Linked-channel auto-translation owns specialized channel
# orchestration while sharing lower-level term and sentence primitives. This
# module sits above domain/LLM and below presentation: callers import it; it
# imports none of them.
APPLICATION = frozenset({"application"})
SHARED = frozenset({"translation_policy", "runtime_keys", "constants"})
PRESENTATION = frozenset({"bot", "channel", "telegram_html", "telegram_text"})
LOCAL_STATE = frozenset(
    {
        "settings",
        "channel_reply_index",
        "channel_reply_schema",
        "channel_runtime",
        "logging_utils",
    }
)
STORAGE = frozenset({"db"})
BUILDER = frozenset({"builder", "data_source", "build_pinyin"})
BOOTSTRAP = frozenset({"cli"})

# Modules that must never depend on Telegram presentation or the Bot SDK.
NO_TELEGRAM_PRESENTATION = (
    DOMAIN_CORE | APPLICATION | SHARED | LOCAL_STATE | STORAGE | BUILDER
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


def check_current_contract_wording() -> list[str]:
    """Reject stale universal-pipeline prose even when Markdown wraps it."""
    failures: list[str] = []
    for path in CURRENT_CONTRACT_FILES:
        if not path.is_file():
            failures.append(f"missing current contract file: {path.relative_to(ROOT)}")
            continue
        normalized = " ".join(path.read_text(encoding="utf-8").split()).casefold()
        for fragment in STALE_CONTRACT_FRAGMENTS:
            if fragment in normalized:
                failures.append(
                    f"{path.relative_to(ROOT).as_posix()}: stale universal-pipeline "
                    f"claim contains {fragment!r}"
                )
    return failures


def check() -> list[str]:
    modules, collisions = _discover_modules()
    failures: list[str] = list(collisions)
    failures.extend(check_api_package())
    failures.extend(check_current_contract_wording())

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
            bad_pres = sorted(local_all & PRESENTATION)
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
