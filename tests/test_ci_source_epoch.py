"""Prove epoch selection uses real ancestry and source-owned contracts."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_source_epoch", ROOT / "scripts/ci_source_epoch.py")
epoch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(epoch)

FLOOR = "13f601871566d89b372ecf31b45345f5387b601c"
OPTIONAL = "0c7be297472c6051b7190974f803b3c6e03d7b63"
PUBLIC = "1c4400aad98578c9abed0429ab30f39c13fed3d8"
LAST = "64e293cf53ba9106a4a67639fd5c6b0857e6c384"
FEATURE = "f8a9008d7adf60006ca210c750aa97f5de386a26"
PR_HEAD = "8f0aa8ce565226466c05b8c5138bb95144f9383d"


def git(*args, env=None, input=None):
    return subprocess.run(["git", *args], cwd=ROOT, env=env, input=input,
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def config():
    data = json.loads((ROOT / "contracts/ci-source-epochs.json").read_bytes())
    # Only a test fixture: the product-owned merged recipe must replace the
    # absent production value before any workflow or receipt can be trusted.
    data["consumers"].setdefault("windows_recipe_sha", hashlib.sha1(b"UNFINALIZED test recipe").hexdigest())
    data["jobs"].setdefault("legacy_windows", "Historical bundled Windows validation / build and test")
    return epoch.load_config(json.dumps(data).encode())


@pytest.fixture
def temporary_git_objects(tmp_path, monkeypatch):
    """Create synthetic commits in isolated test objects, never a checkout/ref."""
    obj = tmp_path / "objects"
    obj.mkdir()
    common = Path(git("rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = (ROOT / common).resolve()
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(obj))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(common / "objects"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "index"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")

    def commit(tree, *parents):
        args = ["commit-tree", tree]
        for parent in parents:
            args.extend(("-p", parent))
        return git(*args, input="fixture\n")

    def change_tree(source, path, content=None):
        git("read-tree", source)
        if content is None:
            git("update-index", "--force-remove", "--", path)
        else:
            blob = git("hash-object", "-w", "--stdin", input=content)
            git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        return git("write-tree")

    return commit, change_tree


def test_real_milestones_and_last_bundled_are_inclusive(config):
    floor = epoch.resolve(FLOOR, config)
    assert (floor["source_epoch"], floor["telegram_optional"],
            floor["public_interface_smoke"], floor["manuscript_interop"]) == ("legacy", False, False, False)
    transition = epoch.resolve(OPTIONAL, config)
    assert (transition["source_epoch"], transition["telegram_optional"],
            transition["public_interface_smoke"], transition["manuscript_interop"]) == ("legacy", True, False, True)
    assert epoch.resolve(PUBLIC, config)["public_interface_smoke"] is True
    last = epoch.resolve(LAST, config)
    assert last["source_epoch"] == "legacy"
    assert last["web_revision"] == "6cbba41bb934d44d1dde1b94f1b50d6d845af599"
    assert last["windows_revision"] is None
    assert last["required_jobs"][-1] == config["jobs"]["legacy_windows"]


def test_equal_tree_does_not_replace_actual_main_ancestry(config, temporary_git_objects):
    commit, _ = temporary_git_objects
    legacy_tree = git("rev-parse", f"{LAST}^{{tree}}")
    sibling = commit(legacy_tree, FLOOR)
    assert git("rev-parse", f"{sibling}^{{tree}}") == legacy_tree
    with pytest.raises(ValueError, match="disconnected"):
        epoch.resolve(sibling, config)


@pytest.mark.parametrize("source", [FEATURE, PR_HEAD])
def test_available_real_feature_objects_are_disconnected(config, source):
    present = subprocess.run(["git", "cat-file", "-e", f"{source}^{{commit}}"],
                             cwd=ROOT, capture_output=True).returncode == 0
    if not present:
        pytest.skip("historical feature object is not required in a main clone")
    if source == FEATURE:
        assert git("rev-parse", f"{LAST}^{{tree}}") == git("rev-parse", f"{source}^{{tree}}")
    with pytest.raises(ValueError, match="disconnected"):
        epoch.resolve(source, config)


def test_strict_descendant_and_synchronized_pr_merge_select_split(config, temporary_git_objects):
    commit, _ = temporary_git_objects
    # HEAD supplies only a split content tree, never an ancestry substitute.
    # The parent graph below is constructed in the isolated test object store.
    split_tree = git("rev-parse", "HEAD^{tree}")
    sibling = commit(git("rev-parse", f"{LAST}^{{tree}}"), FLOOR)
    feature_head = commit(split_tree, sibling)
    with pytest.raises(ValueError, match="disconnected"):
        epoch.resolve(feature_head, config)
    successor = commit(split_tree, LAST)
    synthetic_merge = commit(split_tree, LAST, feature_head)
    for source in (successor, synthetic_merge):
        result = epoch.resolve(source, config)
        assert result["source_epoch"] == "split"
        assert result["web_revision"] == "df0a4d0ca578f35c5cdbecf6ec9672bdbf74f5a0"
        assert result["windows_revision"] == "e6835f38dfcc0ac217bc0cc8c608ad6570668428"
        assert result["required_jobs"][-1] == "API to Windows contract"
    # Newer split CONTROL does not change the target's legacy source epoch.
    assert epoch.resolve(LAST, config)["source_epoch"] == "legacy"


@pytest.mark.parametrize(("source", "expected_web", "expected_windows"), [
    (
        "df895009c9ed4ca723befaa2bdc1e388226214ef",
        "eed5d30e26c6446dbff49aa63905ec3ba2db9677",
        "97c659e22b97f05b9a817c0740ca19d4add63441",
    ),
    (
        "8565b48b628addb6a01569cee00503f1547077c8",
        "eed5d30e26c6446dbff49aa63905ec3ba2db9677",
        "97c659e22b97f05b9a817c0740ca19d4add63441",
    ),
    (
        "b7abb953b8e997328344064e85c9db28f55f40aa",
        "eed5d30e26c6446dbff49aa63905ec3ba2db9677",
        "97c659e22b97f05b9a817c0740ca19d4add63441",
    ),
    (
        "3be7c5027ab94ac9a25a60e19692b21c4fd6de55",
        "eed5d30e26c6446dbff49aa63905ec3ba2db9677",
        "97c659e22b97f05b9a817c0740ca19d4add63441",
    ),
    (
        "122870b1eb20195f4e790c2899e0fa6d73bbb020",
        "13faa392cd6f4826d2115ae8ad04117a95577ffe",
        "1ada119a0ff9284359fff2740f887140f6324545",
    ),
    (
        "d36af51286199ec4900c4d7c806c5f7cac6f461d",
        "13faa392cd6f4826d2115ae8ad04117a95577ffe",
        "1ada119a0ff9284359fff2740f887140f6324545",
    ),
    (
        "6509896c9c3c0606e45cc46bb2afc6bd663d11f2",
        "6cd0a7c5c65243c3d2cd741dc567690fa941f460",
        "d87a66c37b1d4809774774c45bc5731f6ebb66d2",
    ),
    (
        "1949f8ddf9c81dfd02b2bbea59bc8799d4943728",
        "6cd0a7c5c65243c3d2cd741dc567690fa941f460",
        "d87a66c37b1d4809774774c45bc5731f6ebb66d2",
    ),
    (
        "2737530187f1e1c3a6b8758de357c8d32d21dfc7",
        "ba8d01b26e7dd7d0ca3c1e28a61a847d5dc5a131",
        "d0bd6c4b2b4c6e55315dc1b804d379c02aebb854",
    ),
])
def test_historical_split_release_commits_resolve_with_their_pinned_consumers(
    config, source, expected_web, expected_windows
):
    result = epoch.resolve(source, config)
    assert result["source_epoch"] == "split"
    assert result["web_revision"] == expected_web
    assert result["windows_revision"] == expected_windows
    assert result["required_jobs"][-1] == "API to Windows contract"


def test_actual_pr_event_source_includes_last_bundled_main(config):
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        pytest.skip("actual GitHub synthetic merge is supplied on pull_request only")
    source = os.environ["GITHUB_SHA"]
    assert epoch._ancestor(LAST, source)
    assert epoch.resolve(source, config)["source_epoch"] == "split"


@pytest.mark.parametrize("path", [
    "contracts/web-ref.txt", "contracts/windows-ref.txt", "tests/test_windows_contract.py",
    "scripts/check_package_public_interface.py", "pyproject.toml",
])
def test_split_missing_required_layout_rejected(config, temporary_git_objects, path):
    commit, change_tree = temporary_git_objects
    source = commit(change_tree("HEAD", path), LAST)
    with pytest.raises(ValueError):
        epoch.resolve(source, config)


def test_wrong_consumer_pin_rejected(config, temporary_git_objects):
    commit, change_tree = temporary_git_objects
    source = commit(change_tree("HEAD", "contracts/web-ref.txt", "0" * 40 + "\n"), LAST)
    with pytest.raises(ValueError, match="wrong source Web pin"):
        epoch.resolve(source, config)
    source = commit(change_tree("HEAD", "contracts/windows-ref.txt", "0" * 40 + "\n"), LAST)
    with pytest.raises(ValueError, match="wrong source Windows pin"):
        epoch.resolve(source, config)


def test_unconnected_or_pre_floor_sources_rejected(config, temporary_git_objects):
    commit, _ = temporary_git_objects
    root = commit(git("rev-parse", "HEAD^{tree}"))
    with pytest.raises(ValueError, match="floor"):
        epoch.resolve(root, config)
    before = git("rev-parse", f"{FLOOR}^")
    with pytest.raises(ValueError, match="floor"):
        epoch.resolve(before, config)


def test_invalid_or_incomplete_control_config_rejected(config):
    for change in ("recipe", "job", "common", "floor", "schema"):
        broken = copy.deepcopy(config)
        if change == "recipe":
            broken["consumers"].pop("windows_recipe_sha")
        elif change == "job":
            broken["jobs"].pop("legacy_windows")
        elif change == "common":
            broken["jobs"]["common"].remove("API to Web contract")
        elif change == "floor":
            broken["source_floor"] = "bad"
        else:
            broken["schema"] = 2
        with pytest.raises(ValueError):
            epoch.load_config(json.dumps(broken).encode())
    with pytest.raises(ValueError):
        epoch.load_config(b"not json")


def test_control_workflow_historical_job_matches_config(config):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    epoch.validate_workflow(workflow, config)


@pytest.mark.parametrize("change", [
    "wrong_sha", "wrong_repository", "wrong_workflow", "wrong_source", "wrong_condition",
    "wrong_needs", "extra_input", "duplicate_uses", "duplicate_job", "duplicate_jobs",
    "missing_job", "missing_jobs",
])
def test_control_workflow_rejects_wrong_recipe_or_job(config, change):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    sha = config["consumers"]["windows_recipe_sha"]
    uses = (f"    uses: My-Denia/WuwaTerm-Windows/.github/workflows/"
            f"legacy-core-validation.yml@{sha}")
    if change == "wrong_sha":
        altered = uses.replace(sha, "0" * 40)
    elif change == "wrong_repository":
        altered = uses.replace("My-Denia/WuwaTerm-Windows", "other/repo")
    elif change == "wrong_workflow":
        altered = uses.replace("legacy-core-validation.yml", "other.yml")
    elif change == "duplicate_uses":
        altered = uses + "\n    uses: other/repo/.github/workflows/other.yml@" + "0" * 40
    else:
        altered = uses
    workflow = workflow.replace(uses, altered, 1)
    if change == "wrong_source":
        workflow = workflow.replace(
            uses + "\n    with:\n      source_commit: ${{ needs.source.outputs.source_commit }}",
            uses + "\n    with:\n      source_commit: ${{ github.sha }}", 1,
        )
    elif change == "wrong_condition":
        workflow = workflow.replace("    if: needs.source.outputs.source_epoch == 'legacy'",
                                    "    if: always()", 1)
    elif change == "wrong_needs":
        workflow = workflow.replace("    name: Historical bundled Windows validation\n    needs: source",
                                    "    name: Historical bundled Windows validation\n    needs: test", 1)
    elif change == "extra_input":
        workflow = workflow.replace(uses, uses + "\n    secrets: inherit", 1)
    elif change == "duplicate_job":
        workflow = workflow.replace("  deploy-boundary:", "  'historical-windows':\n    uses: attacker\n\n  deploy-boundary:", 1)
    elif change == "duplicate_jobs":
        workflow += "\n'jobs': {}\n"
    elif change == "missing_job":
        workflow = workflow.replace("  historical-windows:", "  historical-renamed:", 1)
    elif change == "missing_jobs":
        workflow = workflow.replace("jobs:\n", "tasks:\n", 1)
    with pytest.raises(ValueError):
        epoch.validate_workflow(workflow, config)


def test_resolve_cli_checks_supplied_control_workflow(config, tmp_path):
    config_path = tmp_path / "config.json"
    workflow_path = tmp_path / "ci.yml"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    workflow_path.write_text(workflow, encoding="utf-8")
    command = [sys.executable, str(ROOT / "scripts/ci_source_epoch.py"), "resolve", LAST,
               "--config", str(config_path), "--workflow", str(workflow_path)]
    good = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert good.returncode == 0, good.stderr
    assert json.loads(good.stdout)["windows_recipe_sha"] == config["consumers"]["windows_recipe_sha"]
    workflow_path.write_text(workflow.replace(config["consumers"]["windows_recipe_sha"], "0" * 40),
                             encoding="utf-8")
    bad = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert bad.returncode != 0
    assert "historical Windows job does not match control policy" in bad.stderr


@pytest.mark.parametrize("path", [
    "client/build.ps1", "client/src/wuwaterm_client/strings.py",
    "client/src/wuwaterm_client/manuscript.py", "client/tests/test_manuscript.py",
])
def test_legacy_missing_required_layout_rejected(config, temporary_git_objects, path):
    commit, change_tree = temporary_git_objects
    source = commit(change_tree(LAST, path), LAST)
    control = copy.deepcopy(config)
    control["last_legacy_source"] = source
    with pytest.raises(ValueError):
        epoch.resolve(source, control)


def test_missing_actual_interop_or_optional_sdk_is_not_downgraded(config, temporary_git_objects):
    commit, change_tree = temporary_git_objects
    manuscript = git("show", f"{LAST}:client/tests/test_manuscript.py")
    tree = change_tree(LAST, "client/tests/test_manuscript.py", manuscript.replace(
        "def test_actual_web_and_client_read_each_others_files(", "def not_the_interop_test("))
    source = commit(tree, LAST)
    control = copy.deepcopy(config)
    control["last_legacy_source"] = source
    with pytest.raises(ValueError, match="missing actual manuscript"):
        epoch.resolve(source, control)

    project = git("show", f"{LAST}:pyproject.toml")
    tree = change_tree(LAST, "pyproject.toml", project.replace(
        "python-telegram-bot>=22.7,<23", "not-telegram>=1"))
    source = commit(tree, LAST)
    control["last_legacy_source"] = source
    with pytest.raises(ValueError, match="optional Telegram"):
        epoch.resolve(source, control)


def test_validate_workflow_accepts_bracketed_needs(config):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    bracketed = workflow.replace("    needs: source\n    if: needs.source.outputs.source_epoch == 'legacy'",
                                "    needs: [source]\n    if: needs.source.outputs.source_epoch == 'legacy'", 1)
    spaced = workflow.replace("    needs: source\n    if: needs.source.outputs.source_epoch == 'legacy'",
                             "    needs: [ source ]\n    if: needs.source.outputs.source_epoch == 'legacy'", 1)
    epoch.validate_workflow(workflow, config)
    epoch.validate_workflow(bracketed, config)
    epoch.validate_workflow(spaced, config)

    for invalid_needs in (
        "    needs: test",
        "    needs: [test]",
        "   needs: source",
        "      needs: source",
        "    needs:source",
    ):
        invalid = workflow.replace(
            "    needs: source\n    if: needs.source.outputs.source_epoch == 'legacy'",
            f"{invalid_needs}\n    if: needs.source.outputs.source_epoch == 'legacy'",
            1,
        )
        with pytest.raises(ValueError):
            epoch.validate_workflow(invalid, config)
