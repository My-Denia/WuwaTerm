from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from wuwaterm.constants import get_source_profile
from wuwaterm.data_source import SourceProvenance
from wuwaterm.db import create_database
from wuwaterm.models import TermRecord

from scripts import verify_db as verify_db_module


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def verified_db(tmp_path):
    profile = get_source_profile("arikatsu")
    provenance = SourceProvenance(
        profile=profile.name,
        repo_url=profile.repo_url,
        commit=profile.pinned_commit,
        game_version=profile.expected_game_version or "",
        resource_version=profile.expected_resource_version or "",
        changelist=profile.expected_changelist or "",
    )
    records = [
        TermRecord(
            category=category,
            source_file=f"{category}.json",
            source_id=str(index),
            text_key=f"{category}_{index}",
            zh="景燃" if category == "resonator" else f"测试{index}",
            en="Jingran" if category == "resonator" else f"Test {index}",
        )
        for index, category in enumerate(
            (
                "core_term",
                "resonator",
                "weapon",
                "echo",
                "item",
                "skill",
                "sonata_effect",
                "location",
            ),
            start=1,
        )
    ]
    path = tmp_path / "terms.candidate.db"
    create_database(
        path,
        records,
        source_profile=profile,
        source_provenance=provenance,
    )
    return path


def _verify(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_db.py"), str(path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def _copy_db(source: Path, tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copy2(source, target)
    return target


def test_strong_verifier_accepts_complete_candidate_read_only(verified_db):
    before = hashlib.sha256(verified_db.read_bytes()).hexdigest()

    result = _verify(verified_db)

    assert result.returncode == 0, result.stderr
    profile = get_source_profile("arikatsu")
    assert f"source_commit\t{profile.pinned_commit}" in result.stdout
    assert f"source_changelist\t{profile.expected_changelist}" in result.stdout
    assert hashlib.sha256(verified_db.read_bytes()).hexdigest() == before


def test_imported_verifier_closes_read_only_connection(monkeypatch, verified_db):
    closed = []
    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

    def tracking_connect(*args, **kwargs):
        return real_connect(*args, factory=TrackingConnection, **kwargs)

    monkeypatch.setattr(verify_db_module.sqlite3, "connect", tracking_connect)

    verify_db_module.verify_database(verified_db)

    assert closed == [True]


def test_database_creation_requires_measured_provenance(tmp_path):
    target = tmp_path / "terms.db"
    target.write_bytes(b"old database")

    with pytest.raises(ValueError, match="measured source_provenance"):
        create_database(target, [])

    assert target.read_bytes() == b"old database"


@pytest.mark.parametrize(
    ("name", "mutation"),
    [
        (
            "metadata",
            "UPDATE metadata SET value = 'wrong' WHERE key = 'source_commit'",
        ),
        ("schema", "CREATE TABLE unexpected(value TEXT)"),
        ("index", "DROP INDEX idx_terms_zh_norm"),
        ("category", "DELETE FROM terms WHERE category = 'item'"),
        ("exact", "UPDATE terms SET en = 'Wrong' WHERE zh = '景燃'"),
        # A second zh row carrying the same en breaks the reverse direction of
        # the representative pair while the forward direction still looks fine.
        # This is exactly how the retired 3.5 pair 穗穗 -> Suisui failed on the
        # 3.6 data, so the verifier has to reject it.
        (
            "exact_reverse",
            "INSERT INTO terms (category, source_file, source_id, text_key, zh, en, "
            "zh_norm, en_norm, pinyin, pinyin_abbrev, priority) VALUES "
            "('speaker', 'speaker.json', '9001', 'Speaker_9001_Name', "
            "'通讯中的景燃', 'Jingran', '通讯中的景燃', 'jingran', '', '', 80)",
        ),
    ],
)
def test_strong_verifier_rejects_invalid_candidate(
    verified_db, tmp_path, name, mutation
):
    candidate = _copy_db(verified_db, tmp_path, f"{name}.db")
    with sqlite3.connect(candidate) as conn:
        conn.execute(mutation)

    result = _verify(candidate)

    assert result.returncode == 1
    assert "database verification failed" in result.stderr


def test_strong_verifier_rejects_corrupt_database(verified_db, tmp_path):
    candidate = _copy_db(verified_db, tmp_path, "corrupt.db")
    data = candidate.read_bytes()
    candidate.write_bytes(data[: len(data) // 2])

    result = _verify(candidate)

    assert result.returncode == 1
    assert "database verification failed" in result.stderr
