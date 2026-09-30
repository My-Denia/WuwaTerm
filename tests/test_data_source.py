from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from wuwaterm.constants import SOURCE_PROFILES
from wuwaterm.builder import build_database
from wuwaterm.data_source import (
    DataSourceError,
    inspect_data_source,
    parse_source_version,
    refresh_data,
)
from wuwaterm.db import connect
from wuwaterm.models import TermRecord


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def local_source(monkeypatch, tmp_path):
    source = tmp_path / "upstream"
    source.mkdir()
    _git("init", "-b", "main", cwd=source)
    (source / "README.md").write_text(
        "# Data\n\n"
        "> Game Version: 3.7.0\n"
        "> Resource Version: 3.7.8\n"
        "> Changelist: 8975829\n",
        encoding="utf-8",
    )
    (source / "BinData").mkdir()
    (source / "BinData" / "placeholder.json").write_text("[]\n", encoding="utf-8")
    (source / "Textmaps").mkdir()
    (source / "Textmaps" / "placeholder.json").write_text("[]\n", encoding="utf-8")
    _git("add", ".", cwd=source)
    _git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "fixture",
        cwd=source,
    )
    commit = _git("rev-parse", "HEAD", cwd=source)
    profile = replace(
        SOURCE_PROFILES["arikatsu"],
        repo_url=str(source),
        pinned_commit=commit,
    )
    monkeypatch.setitem(SOURCE_PROFILES, "arikatsu", profile)
    return source, profile


def test_parse_source_version_requires_all_exact_fields(tmp_path):
    version = tmp_path / "README.md"
    version.write_text(
        "> Game Version: 3.6.0\n"
        "> Resource Version: 3.6.4\n"
        "> Changelist: 8464573\n",
        encoding="utf-8",
    )

    assert parse_source_version(version) == {
        "game_version": "3.6.0",
        "resource_version": "3.6.4",
        "changelist": "8464573",
    }

    version.write_text("> Game Version: 3.6.0\n", encoding="utf-8")
    with pytest.raises(DataSourceError, match="missing version fields"):
        parse_source_version(version)


def test_parse_source_version_accepts_upstream_trailing_html_breaks(tmp_path):
    version = tmp_path / "README.md"
    version.write_text(
        "> Game Version: 3.6.0</br>\n"
        "> Resource Version: 3.6.4<br />\n"
        "> Changelist: 8464573\n",
        encoding="utf-8",
    )

    assert parse_source_version(version) == {
        "game_version": "3.6.0",
        "resource_version": "3.6.4",
        "changelist": "8464573",
    }


def test_refresh_data_measures_checkout_and_includes_version_file(local_source, tmp_path):
    source, profile = local_source
    checkout = refresh_data(tmp_path / "checkout", profile_name="arikatsu")

    provenance = inspect_data_source(checkout, "arikatsu")

    assert (checkout / "README.md").is_file()
    assert provenance.repo_url == str(source)
    assert provenance.commit == profile.pinned_commit
    assert provenance.game_version == profile.expected_game_version
    assert provenance.resource_version == profile.expected_resource_version
    assert provenance.changelist == profile.expected_changelist


def test_refresh_data_honors_explicit_repo_url_override(local_source, tmp_path):
    source, _profile = local_source
    mirror = tmp_path / "mirror.git"
    _git("clone", "--bare", str(source), str(mirror), cwd=tmp_path)

    checkout = refresh_data(
        tmp_path / "checkout-from-mirror",
        repo_url=str(mirror),
        profile_name="arikatsu",
    )

    provenance = inspect_data_source(
        checkout,
        "arikatsu",
        expected_repo_url=str(mirror),
    )
    assert provenance.repo_url == str(mirror)
    with pytest.raises(DataSourceError, match="expected origin"):
        inspect_data_source(checkout, "arikatsu")

    _git("remote", "set-url", "origin", str(tmp_path / "other"), cwd=checkout)
    with pytest.raises(DataSourceError, match="expected origin"):
        inspect_data_source(
            checkout,
            "arikatsu",
            expected_repo_url=str(mirror),
        )


def test_refresh_data_measures_unversioned_legacy_checkout(
    local_source, monkeypatch, tmp_path
):
    source, source_profile = local_source
    profile = replace(
        SOURCE_PROFILES["dimbreath_legacy"],
        repo_url=str(source),
        pinned_commit=source_profile.pinned_commit,
        sparse_paths=("BinData", "Textmaps"),
    )
    monkeypatch.setitem(SOURCE_PROFILES, "dimbreath_legacy", profile)

    checkout = refresh_data(
        tmp_path / "legacy-checkout", profile_name="dimbreath_legacy"
    )
    provenance = inspect_data_source(checkout, "dimbreath_legacy")

    assert provenance.repo_url == str(source)
    assert provenance.commit == source_profile.pinned_commit
    assert provenance.game_version == "unavailable"
    assert provenance.resource_version == "unavailable"
    assert provenance.changelist == "unavailable"


def test_builder_stamps_observed_checkout_provenance(
    local_source, monkeypatch, tmp_path
):
    source, profile = local_source
    checkout = refresh_data(tmp_path / "checkout", profile_name="arikatsu")
    monkeypatch.setattr(
        "wuwaterm.builder.iter_records",
        lambda *_args, **_kwargs: [
            TermRecord(
                category="resonator",
                source_file="fixture.json",
                source_id="1212",
                text_key="RoleInfo_1212_Name",
                zh="景燃",
                en="Jingran",
            )
        ],
    )
    db_path = tmp_path / "candidate.db"

    build_database(checkout, db_path, profile_name="arikatsu")

    with connect(db_path) as conn:
        metadata = dict(conn.execute("SELECT key, value FROM metadata"))
    assert metadata["source_repo_url"] == str(source)
    assert metadata["source_commit"] == profile.pinned_commit
    assert metadata["wutheringdata_commit"] == profile.pinned_commit
    assert metadata["source_game_version"] == profile.expected_game_version
    assert metadata["source_resource_version"] == profile.expected_resource_version
    assert metadata["source_changelist"] == profile.expected_changelist


def test_inspect_data_source_rejects_wrong_remote(local_source, tmp_path):
    _source, _profile = local_source
    checkout = refresh_data(tmp_path / "checkout", profile_name="arikatsu")
    _git("remote", "set-url", "origin", str(tmp_path / "other"), cwd=checkout)

    with pytest.raises(DataSourceError, match="expected origin"):
        inspect_data_source(checkout, "arikatsu")


def test_inspect_data_source_rejects_dirty_or_wrong_version(local_source, tmp_path):
    _source, _profile = local_source
    checkout = refresh_data(tmp_path / "checkout", profile_name="arikatsu")
    readme = checkout / "README.md"
    readme.write_text(readme.read_text().replace("3.7.8", "3.7.7"), encoding="utf-8")

    with pytest.raises(DataSourceError, match="modifications or untracked files"):
        inspect_data_source(checkout, "arikatsu")

    _git("checkout", "--", "README.md", cwd=checkout)
    rogue = checkout / "BinData" / "rogue.json"
    rogue.write_text("[]\n", encoding="utf-8")
    with pytest.raises(DataSourceError, match="modifications or untracked files"):
        inspect_data_source(checkout, "arikatsu")
    rogue.unlink()

    profile = replace(SOURCE_PROFILES["arikatsu"], expected_resource_version="9.9.9")
    SOURCE_PROFILES["arikatsu"] = profile
    with pytest.raises(DataSourceError, match="expected resource_version"):
        inspect_data_source(checkout, "arikatsu")
