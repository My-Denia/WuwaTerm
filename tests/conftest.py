from __future__ import annotations

import inspect
import json
import os
from functools import wraps
from pathlib import Path
from typing import Any

import pytest

from wuwaterm.builder import iter_records
from wuwaterm.constants import get_source_profile
from wuwaterm.data_source import SourceProvenance
from wuwaterm.db import create_database


def _fd9_is_open() -> bool:
    try:
        os.fstat(9)
    except OSError:
        return False
    return True


def _park_fd9() -> int | None:
    """Close descriptor 9 for a marked call, returning what restores it.

    The mutating artifact entry points refuse to run while file descriptor 9 is
    open: that number is the deployment lock in ``scripts/build-delivery.sh``,
    and a tool that inherits it can wait forever behind a deploy it does not
    own. Tests that exercise those entry points therefore have to run in the
    state the build script establishes with ``exec 9>&-``, which is what the
    ``closed_fd9`` marker asks for.

    The parking belongs to the call phase, not to a fixture. pytest suspends,
    resumes and reads back its fd capture at the setup, call and teardown
    boundaries, so a fixture would hold the number closed across a capture
    resume. On a machine that inherits no descriptor 9 - the ordinary local
    case, and the one ``scripts/validate.py`` produces with ``close_fds`` -
    pytest's own capture temporary file is allocated on that very number, and
    closing it makes the next capture restore raise EBADF for every test in the
    session. Around the call the number is free exactly where the entry points
    run, and it is handed back before pytest touches the file again.
    """
    if not _fd9_is_open():
        return None
    saved = os.dup(9)
    os.close(9)
    return saved


@pytest.hookimpl(wrapper=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function):
    if pyfuncitem.get_closest_marker("closed_fd9") is None:
        return (yield)
    saved = _park_fd9()
    try:
        return (yield)
    finally:
        if saved is not None:
            if _fd9_is_open():
                # The call opened a descriptor of its own on the number. pytest
                # needs the number itself back to read its capture, so the
                # call's descriptor goes first.
                os.close(9)
            os.dup2(saved, 9)
            os.close(saved)


@pytest.fixture()
def sample_data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "wutheringdata"
    (root / "ConfigDB").mkdir(parents=True)
    (root / "TextMap" / "zh-Hans").mkdir(parents=True)
    (root / "TextMap" / "en").mkdir(parents=True)

    configs = {
        "RoleInfo.json": [
            {"Id": 1304, "Name": "RoleInfo_1304_Name"},
            {"Id": 1505, "Name": "RoleInfo_1505_Name"},
            {"Id": 1102, "Name": "RoleInfo_1102_Name"},
            {"Id": 1106, "Name": "RoleInfo_1106_Name"},
            {"Id": 1207, "Name": "RoleInfo_1207_Name"},
            {"Id": 1607, "Name": "RoleInfo_1607_Name"},
            {"Id": 1608, "Name": "RoleInfo_1608_Name"},
        ],
        "WeaponConf.json": [{"ItemId": 2101, "WeaponName": "WeaponConf_2101_WeaponName"}],
        "PhantomItem.json": [{"ItemId": 6001, "MonsterName": "MonsterInfo_6001_Name"}],
        "ItemInfo.json": [
            {"Id": 1, "Name": "ItemInfo_1_Name"},
            {"Id": 2, "Name": "ItemInfo_2_Name"},
        ],
        "Skill.json": [
            {"Id": 1001, "SkillName": "Skill_1001_SkillName"},
            {"Id": 1002, "SkillName": "Skill_1002_SkillName"},
        ],
        "PhantomFetterGroup.json": [{"Id": 1, "FetterGroupName": "PhantomFetter_1_Name"}],
        "Area.json": [{"AreaId": 2, "Title": "Area_2_Title"}],
    }
    for name, rows in configs.items():
        (root / "ConfigDB" / name).write_text(
            json.dumps(rows, ensure_ascii=False),
            encoding="utf-8",
        )

    zh = {
        "LoadingTipsText_1005_Title": "共鸣者",
        "Term850082_Title": "声骸",
        "OccupationConfig_漂泊者_Name": "漂泊者",
        "RoleInfo_1304_Name": "今汐",
        "RoleInfo_1505_Name": "守岸人",
        "RoleInfo_1102_Name": "安可",
        "RoleInfo_1106_Name": "卡卡罗",
        "RoleInfo_1207_Name": "椿",
        "RoleInfo_1607_Name": "卡提希娅",
        "RoleInfo_1608_Name": "洛瑟菈",
        "WeaponConf_2101_WeaponName": "纹秋",
        "MonsterInfo_6001_Name": "先锋幼岩",
        "ItemInfo_1_Name": "联觉经验",
        "ItemInfo_2_Name": "巧手烹调",
        "Skill_1001_SkillName": "风羽为刃",
        "Skill_1002_SkillName": "巧手烹调",
        "PhantomFetter_1_Name": "凝夜白霜",
        "Area_2_Title": "云陵谷",
    }
    en = {
        "LoadingTipsText_1005_Title": "Resonator",
        "Term850082_Title": "Echo",
        "OccupationConfig_漂泊者_Name": "Rover",
        "RoleInfo_1304_Name": "Jinhsi",
        "RoleInfo_1505_Name": "Shorekeeper",
        "RoleInfo_1102_Name": "Encore",
        "RoleInfo_1106_Name": "Calcharo",
        "RoleInfo_1207_Name": "Camellya",
        "RoleInfo_1607_Name": "Cartethyia",
        "RoleInfo_1608_Name": "Lucilla",
        "WeaponConf_2101_WeaponName": "Autumntrace",
        "MonsterInfo_6001_Name": "Vanguard Junrock",
        "ItemInfo_1_Name": "Union EXP",
        "ItemInfo_2_Name": "Life Skill",
        "Skill_1001_SkillName": "Feather as Blade",
        "Skill_1002_SkillName": "Skillful Cooking",
        "PhantomFetter_1_Name": "Freezing Frost",
        "Area_2_Title": "Gorges of Spirits",
    }
    for lang, mapping in (("zh-Hans", zh), ("en", en)):
        (root / "TextMap" / lang / "MultiText.json").write_text(
            json.dumps(mapping, ensure_ascii=False),
            encoding="utf-8",
        )
    return root


@pytest.fixture()
def sample_db(tmp_path: Path, sample_data_dir: Path) -> Path:
    db_path = tmp_path / "terms.db"
    profile = get_source_profile("dimbreath_legacy")
    create_database(
        db_path,
        iter_records(sample_data_dir, profile.name),
        source_profile=profile,
        source_provenance=SourceProvenance(
            profile=profile.name,
            repo_url=profile.repo_url,
            commit=profile.pinned_commit,
            game_version="fixture-unavailable",
            resource_version="fixture-unavailable",
            changelist="fixture-unavailable",
        ),
    )
    return db_path


def _filter_supported_kwargs(callable_obj: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Keep only keyword arguments accepted by narrow legacy test stubs."""
    params = inspect.signature(callable_obj).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {name: value for name, value in kwargs.items() if name in params}


def _wrap_llm_caller(fn: Any, *, async_caller: bool) -> Any:
    if async_caller:

        @wraps(fn)
        async def _async_shim(*args: Any, **kwargs: Any) -> Any:
            result = fn(*args, **_filter_supported_kwargs(fn, kwargs))
            if inspect.isawaitable(result):
                return await result
            return result

        return _async_shim

    @wraps(fn)
    def _sync_shim(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **_filter_supported_kwargs(fn, kwargs))

    return _sync_shim


@pytest.fixture(autouse=True)
def _adapt_legacy_llm_monkeypatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow tests patching _call_llm / _call_llm_async with narrower signatures."""
    import wuwaterm.sentence as sentence_mod

    orig_setattr = monkeypatch.setattr

    def wrapped_setattr(
        target: Any,
        name: Any,
        value: Any = ...,
        raising: bool = True,
    ) -> None:
        is_string_form = isinstance(target, str) and value is ...
        if is_string_form:
            target_path = target
            attr_value = name
            is_target = target_path in (
                "wuwaterm.sentence._call_llm",
                "wuwaterm.sentence._call_llm_async",
            )
            attr_name = target_path.rsplit(".", 1)[-1]
        else:
            attr_value = value
            is_target = target is sentence_mod and name in ("_call_llm", "_call_llm_async")
            attr_name = name if isinstance(name, str) else ""

        if is_target and callable(attr_value):
            attr_value = _wrap_llm_caller(
                attr_value, async_caller=(attr_name == "_call_llm_async")
            )

        if is_string_form:
            orig_setattr(target, attr_value, raising=raising)
        else:
            orig_setattr(target, name, attr_value, raising=raising)

    orig_setattr(monkeypatch, "setattr", wrapped_setattr)
