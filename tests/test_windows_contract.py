"""Core producer compatibility with the pinned, independently maintained Windows client."""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_api import bearer, build_client_app, call, disable_llm, issue_device, run
from wuwaterm_api import API_VERSION

ROOT = Path(__file__).resolve().parents[1]
CONSUMER_FILES = ("src/wuwaterm_client/api.py", "src/wuwaterm_client/manuscript.py")


def supported_versions(source: str) -> tuple[str, ...]:
    """Read a literal assignment without importing the Qt/network client."""
    values = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "SUPPORTED_API_VERSIONS"
               for target in targets):
            values.append(ast.literal_eval(value))
    if (len(values) != 1 or not isinstance(values[0], tuple) or not values[0]
            or not all(isinstance(version, str) and version for version in values[0])):
        raise ValueError("expected one nonempty literal API version tuple")
    return values[0]


def require_windows(root: str | None, pin: str) -> Path:
    if not root or not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise ValueError("Windows checkout and a full pinned SHA are required")
    path = Path(root).resolve(strict=True)
    # Inherited Git environment can point git -C at another repository.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(path), *args], env=env,
                                       stderr=subprocess.PIPE)

    if Path(os.fsdecode(git("rev-parse", "--show-toplevel")).strip()).resolve() != path:
        raise ValueError("Windows root must be the checkout root")
    if git("rev-parse", "--verify", "HEAD^{commit}").decode().strip() != pin:
        raise ValueError("Windows checkout does not match the pin")
    for name in CONSUMER_FILES:
        if (path / name).read_bytes() != git("show", f"{pin}:{name}"):
            raise ValueError(f"Windows source differs from pinned commit: {name}")
    return path



def assert_api_compatible(versions: tuple[str, ...], version: str, document: dict) -> None:
    assert version in versions
    assert f"/{version}/reviews" in document["paths"]


def test_supported_versions_reads_annotated_and_plain_assignments():
    assert supported_versions('SUPPORTED_API_VERSIONS: tuple[str, ...] = ("v1",)') == ("v1",)
    assert supported_versions('SUPPORTED_API_VERSIONS = ("v1", "v2")') == ("v1", "v2")
    assert_api_compatible(("v1",), "v1", {"paths": {"/v1/reviews": {}}})
    with pytest.raises(AssertionError):
        assert_api_compatible(("v2",), "v1", {"paths": {"/v1/reviews": {}}})
    with pytest.raises(AssertionError):
        assert_api_compatible(("v1",), "v1", {"paths": {}})
    for source in ('SUPPORTED_API_VERSIONS = ()', 'SUPPORTED_API_VERSIONS = ["v1"]',
                   'OTHER = ("v1",)', 'SUPPORTED_API_VERSIONS = ("v1",)\nSUPPORTED_API_VERSIONS = ("v2",)'):
        with pytest.raises(ValueError):
            supported_versions(source)


def test_windows_checkout_rejects_missing_wrong_and_modified_source(tmp_path):
    with pytest.raises(ValueError):
        require_windows(None, "a" * 40)
    with pytest.raises(ValueError):
        require_windows(str(tmp_path), "bad-pin")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], env=env,
                                       stderr=subprocess.PIPE).decode().strip()
    git("init", "-q")
    for name in CONSUMER_FILES:
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("synthetic source\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    sha = git("rev-parse", "HEAD")
    assert require_windows(str(tmp_path), sha) == tmp_path.resolve()
    with pytest.raises(ValueError, match="pin"):
        require_windows(str(tmp_path), "a" * 40)
    with pytest.raises(ValueError, match="checkout root"):
        require_windows(str(tmp_path / "src"), sha)
    (tmp_path / CONSUMER_FILES[0]).write_text("modified\n")
    with pytest.raises(ValueError, match="differs"):
        require_windows(str(tmp_path), sha)
    (tmp_path / CONSUMER_FILES[0]).unlink()
    with pytest.raises(FileNotFoundError):
        require_windows(str(tmp_path), sha)


@pytest.fixture(scope="module")
def windows_root():
    pin = (ROOT / "contracts/windows-ref.txt").read_text().strip()
    return require_windows(os.environ.get("WUWATERM_WINDOWS_ROOT"), pin)


@pytest.mark.cross_component
def test_windows_speaks_current_api_and_openapi(windows_root, tmp_path, sample_db):
    versions = supported_versions((windows_root / CONSUMER_FILES[0]).read_text())
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "contract reader")
    response = run(call(app, "GET", "/v1/meta", headers=bearer(token)))
    assert response.status_code == 200
    assert response.json()["api_version"] == API_VERSION
    snapshot = json.loads((ROOT / "docs/api/openapi.json").read_text())
    assert app.openapi() == snapshot
    assert_api_compatible(versions, API_VERSION, snapshot)


@pytest.mark.cross_component
def test_real_review_report_is_accepted_by_windows(windows_root, tmp_path, sample_db, monkeypatch):
    spec = importlib.util.spec_from_file_location("windows_manuscript_contract", windows_root / CONSUMER_FILES[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    disable_llm(monkeypatch)
    app, store = build_client_app(tmp_path, sample_db)
    _, token = issue_device(store, "contract reviewer")
    source, target = "今汐拿到了声骸。", "Jinhsi got an Echo."
    response = run(call(app, "POST", "/v1/reviews", headers={**bearer(token),
                        "X-WuwaTerm-Matcher-Basis": "1"},
                        json={"review_version": "review-v2", "source": source,
                              "target": target, "direction": "en"}))
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["rule_version"] == "review-v2"
    assert report["findings"] and any(item["candidates"] for item in report["findings"])
    assert module.valid_report(report, source, target)
    broken = copy.deepcopy(report)
    next(item for item in broken["findings"] if item["candidates"])["candidates"][0]["candidate_id"] = "invalid"
    assert not module.valid_report(broken, source, target)
