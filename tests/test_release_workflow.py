"""What the release workflow promises, and what the checklist says it does.

`docs/release-checklist.md` describes a release the maintainer never assembles
by hand any more: `.github/workflows/release.yml` builds every asset, writes the
manifest and composes the release notes. Two documents describing one mechanism
is exactly the shape that drifts - the checklist is read at release time, the
workflow runs at release time, and nothing else compares them.

So the properties that would be expensive to discover at release time are
pinned here:

* the note template in the checklist has the SAME section headings, in the same
  order, as the notes the workflow actually writes. The prose under them is
  deliberately NOT pinned: making every wording change a two-file edit buys
  nothing, and the reason to read the generated notes before publishing does
  not go away.
* publishing stays a human step - no tag trigger, no `--draft=false` anywhere
  in the workflow.
* the write permissions stay where the design put them.
* the client zip is named the same thing by the build script, the workflow, the
  client's own guide and the support matrix. That name is a cross-file contract
  with four holders and no other reader.

Everything here is a TEXT check on purpose. The workflow is YAML, but PyYAML is
not a dependency of this project and adding one so a test can read a file it
could read as text would be the wrong trade. The greps below are anchored
tightly enough to say what they mean.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CHECKLIST = ROOT / "docs" / "release-checklist.md"

# The list entries of the notes generator: `              "### Assets",`.
# Anchored to the trailing comma so the step-summary headings the workflow also
# writes (`echo "### Images"`) cannot be mistaken for release-note sections.
NOTES_SECTION = re.compile(r'^\s*"(### .+)",$', re.MULTILINE)

# The version part differs legitimately per file - a literal, an English
# placeholder, a Chinese one, a shell variable - so it is matched loosely and
# normalised away below. The prefix, the platform and the extension are the
# contract.
ZIP_NAME = re.compile(r"WuwaTerm-[^\n`'\"]*?-windows-x64\.zip")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _code_lines(text: str) -> list[str]:
    """Lines that DO something, with comment-only lines dropped.

    A permission named in a comment explaining where permissions live must not
    be counted as a permission being granted; the first version of this file
    counted both and failed on its own explanatory comment.
    """
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def _trigger_keys(text: str) -> list[str]:
    """The top-level keys of the workflow's `on:` block."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.rstrip() == "on:")
    keys = []
    for line in lines[start + 1 :]:
        if line.strip() == "" or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):  # back to column 0: the block ended
            break
        match = re.match(r"^  ([A-Za-z_]+):", line)
        if match:
            keys.append(match.group(1))
    return keys


def _template_block() -> str:
    """The fenced release-note template out of the checklist."""
    text = CHECKLIST.read_text(encoding="utf-8")
    blocks = re.findall(r"^```markdown\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)
    assert len(blocks) == 1, (
        f"expected exactly one fenced markdown template in {CHECKLIST.name}, "
        f"found {len(blocks)}"
    )
    return blocks[0]


def test_release_workflow_exists_and_has_no_tag_trigger():
    # Dispatch builds the draft; pull_request is always a dry run; release
    # published only retags recorded GHCR digests. A push or tag trigger
    # would still make publication a side effect of moving a ref.
    assert _trigger_keys(_workflow_text()) == [
        "workflow_dispatch",
        "pull_request",
        "release",
    ]
    assert re.search(
        r"^  release:\n    types:\n      - published\s*$",
        _workflow_text(),
        re.MULTILINE,
    )


def test_release_workflow_never_publishes():
    text = _workflow_text()
    executable = [
        line
        for line in text.splitlines()
        if "--draft=false" in line and not line.lstrip().startswith("#")
    ]
    assert executable == [], (
        "the workflow must never publish a release; publication is "
        f"`gh release edit --draft=false`, run by a person: {executable}"
    )


def test_release_workflow_keeps_write_permissions_where_the_design_put_them():
    text = _workflow_text()

    assert re.search(r"^permissions:\n  contents: read\n", text, re.MULTILINE), (
        "the workflow default must be read-only"
    )
    # Two package writes: draft-time sha push, and post-publish digest retag.
    # One draft job may write releases. More than that is a permission spread.
    code = _code_lines(text)
    assert sum("packages: write" in line for line in code) == 2
    assert sum("contents: write" in line for line in code) == 1


def test_no_write_scope_is_reachable_from_a_pull_request():
    """The workflow is inside its own pull_request path filter.

    So a branch pull request can rewrite this file and have it run. Any job
    that holds a write scope on that run holds it for whatever the pull request
    turned the job into - the dry-run switch only guards the steps that are
    checked in. Both widened jobs therefore carry a condition that a
    pull_request run cannot satisfy, and this test reads the condition rather
    than trusting the comment above it.
    """
    text = _workflow_text()
    lines = text.splitlines()
    widened: dict[str, str] = {}
    current = None
    for index, line in enumerate(lines):
        if re.match(r"^  [a-z0-9-]+:$", line):
            current = line.strip().rstrip(":")
        if line.lstrip().startswith("#"):
            continue
        if "packages: write" in line or "contents: write" in line:
            # `permissions:` is nested under the job, so the scope belongs to
            # whichever job header was seen last.
            assert current is not None, f"a write scope outside any job at line {index + 1}"
            widened[current] = line.strip()

    assert set(widened) == {"push-images", "draft-release", "promote-images"}, widened

    for job in widened:
        start = lines.index(f"  {job}:")
        block = []
        for line in lines[start + 1 :]:
            if re.match(r"^  [a-z0-9-]+:$", line):
                break
            block.append(line)
        condition = [line for line in block if line.strip().startswith("if:")]
        assert condition, f"{job} holds a write scope with no condition at all"
        text_of = " ".join(condition)
        if job == "promote-images":
            assert "release" in text_of, f"{job}: {text_of}"
            assert "published" in text_of, f"{job}: {text_of}"
            assert "pull_request" not in text_of, f"{job}: {text_of}"
            continue
        assert "workflow_dispatch" in text_of, f"{job}: {text_of}"
        assert "dry_run" in text_of and "'false'" in text_of, f"{job}: {text_of}"


def test_note_template_headings_match_the_notes_the_workflow_writes():
    generated = NOTES_SECTION.findall(_workflow_text())
    documented = [
        line.rstrip()
        for line in _template_block().splitlines()
        if line.startswith("### ")
    ]

    assert generated, "found no release-note sections in the workflow"
    assert generated == documented, (
        "docs/release-checklist.md's note template and the notes "
        ".github/workflows/release.yml writes describe different releases:\n"
        f"  workflow:  {generated}\n"
        f"  checklist: {documented}"
    )


def _job_block(name: str) -> str:
    lines = _workflow_text().splitlines()
    start = lines.index(f"  {name}:")
    block = []
    for line in lines[start + 1 :]:
        if re.match(r"^  [a-z0-9-]+:$", line):
            break
        block.append(line)
    return "\n".join(block)


def test_build_jobs_skip_the_release_event():
    for job in ("preflight", "python-package", "client", "images", "assemble"):
        block = _job_block(job)
        condition = [line for line in block.splitlines() if line.strip().startswith("if:")]
        assert condition, f"{job} has no if: and would rebuild on release published"
        text_of = " ".join(condition)
        assert "github.event_name != 'release'" in text_of, f"{job}: {text_of}"


def test_draft_image_tags_are_sha_class_only():
    assignments = re.findall(r"tag_values=\(([^)]+)\)", _workflow_text())
    assert assignments, "no tag_values assignments found"
    for assigned in assignments:
        assert "v$VERSION" not in assigned, assigned
        assert "major_minor" not in assigned, assigned
        assert "sha-$short" in assigned, assigned


def test_promote_images_retags_published_manifest_digests():
    block = _job_block("promote-images")
    assert "imagetools" in block
    assert "release-manifest.json" in block
    assert "deploy/Dockerfile" not in block
    assert "docker buildx build" not in block
    executable = [
        line
        for line in block.splitlines()
        if "--draft=false" in line and not line.lstrip().startswith("#")
    ]
    assert executable == []


def test_the_note_template_states_the_unsigned_client():
    template = _template_block().lower()
    assert "unsigned" in template
    assert "smartscreen" in template


def test_the_client_zip_has_one_name_across_every_file_that_names_it():
    holders = {
        ".github/workflows/release.yml": WORKFLOW,
        "client/build.ps1": ROOT / "client" / "build.ps1",
        "client/README.md": ROOT / "client" / "README.md",
        "docs/support-matrix.md": ROOT / "docs" / "support-matrix.md",
        "docs/release-checklist.md": CHECKLIST,
        "README.md": ROOT / "README.md",
        "README.zh-CN.md": ROOT / "README.zh-CN.md",
    }
    shapes: dict[str, set[str]] = {}
    for label, path in holders.items():
        assert path.is_file(), f"{label} is missing"
        found = ZIP_NAME.findall(path.read_text(encoding="utf-8"))
        assert found, f"{label} no longer names the client zip"
        # Normalise the version, which each file legitimately writes its own
        # way (a literal, a placeholder, a shell variable). What must agree is
        # the prefix, the platform suffix and the extension.
        shapes[label] = {re.sub(r"WuwaTerm-.*?-windows", "WuwaTerm-<v>-windows", n) for n in found}

    distinct = set().union(*shapes.values())
    assert distinct == {"WuwaTerm-<v>-windows-x64.zip"}, (
        f"the client zip is named more than one way: {shapes}"
    )


# Execute the actual promotion step with a stateful local registry stub.
# No Docker daemon, credentials, socket, or upstream registry is used.
def _promotion_script():
    block = _job_block("promote-images")
    start = block.index("      - name: Retag the recorded digests")
    run = block.index("        run: |\n", start) + len("        run: |\n")
    return textwrap.dedent(block[run:])


REGISTRY_STUB = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

path = Path(os.environ['STUB_REGISTRY'])
state = json.loads(path.read_text())
args = sys.argv[1:]
assert args[:2] == ['buildx', 'imagetools'], args
command = args[2]
if command == 'inspect':
    ref = args[3]
    state['reads'].append(ref)
    mutation = state.get('mutation')
    if mutation and mutation['ref'] == ref and state['reads'].count(ref) == 2:
        state['tags'][ref] = mutation['digest']
    path.write_text(json.dumps(state))
    if ref in state.get('errors', {}):
        print(state['errors'][ref], file=sys.stderr)
        sys.exit(1)
    digest = ref.split('@', 1)[1] if '@' in ref else state['tags'].get(ref)
    source = ref if '@' in ref else ref.rsplit(':', 1)[0] + '@' + str(digest)
    if not digest or source not in state['images']:
        print('ERROR: ' + ref + ': not found', file=sys.stderr)
        sys.exit(1)
    if '--format' in args:
        assert args[args.index('--format') + 1] == '{{json .Image}}'
        print(json.dumps(state['images'][source]))
    else:
        print('Name: ' + ref + '\nDigest: ' + digest)
elif command == 'create':
    assert args[3] == '--tag' and len(args) == 6, args
    dest, source = args[4:]
    assert source in state['images'], source
    state['creates'].append(dest)
    state['tags'][dest] = source.split('@', 1)[1]
    path.write_text(json.dumps(state))
else:
    raise AssertionError(args)
"""


def _stub_registry(tmp_path, *, tag="v0.5.1", old_version=None, multi=False):
    names = {"runtime": "ghcr.io/fixture/runtime", "builder": "ghcr.io/fixture/builder"}
    digests = {"runtime": "sha256:" + "a" * 64, "builder": "sha256:" + "b" * 64}
    previous = {"runtime": "sha256:" + "c" * 64, "builder": "sha256:" + "d" * 64}
    state = {"tags": {}, "images": {}, "creates": [], "reads": [], "errors": {}}
    manifest = {"images": {}}
    version = tag.removeprefix("v")
    for kind, name in names.items():
        def image_config(value):
            config = {"config": {"Labels": {"org.opencontainers.image.version": value}}}
            if multi:
                return {"linux/amd64": config, "linux/arm64": config}
            return config
        state["images"][name + "@" + digests[kind]] = image_config(version)
        manifest["images"][kind] = {"name": name, "digest": digests[kind]}
        if old_version:
            state["images"][name + "@" + previous[kind]] = image_config(old_version)
            state["tags"][name + ":v" + old_version] = previous[kind]
            state["tags"][name + ":" + version.rsplit(".", 1)[0]] = previous[kind]
    (tmp_path / "staging").mkdir()
    (tmp_path / "staging/release-manifest.json").write_text(json.dumps(manifest))
    docker = tmp_path / "docker"
    docker.write_text(REGISTRY_STUB)
    docker.chmod(0o755)
    return state, names, digests


def _run_promotion(tmp_path, state, tag="v0.5.1"):
    state_path = tmp_path / "registry.json"
    state_path.write_text(json.dumps(state))
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "TAG": tag, "STUB_REGISTRY": str(state_path),
           "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md")}
    result = subprocess.run(["bash", "-c", _promotion_script()], cwd=tmp_path,
                            env=env, text=True, capture_output=True, timeout=10)
    final = json.loads(state_path.read_text())
    assert final["reads"], "promotion did not execute registry inspections"
    return result, final


@pytest.mark.parametrize("old_version,multi", [(None, False), ("0.5.0", False), ("0.5.0", True)])
def test_release_promotion_first_release_and_patch_advance(tmp_path, old_version, multi):
    state, names, digests = _stub_registry(tmp_path, old_version=old_version, multi=multi)
    result, final = _run_promotion(tmp_path, state)
    assert result.returncode == 0, result.stderr
    assert len(final["creates"]) == 4
    for kind, name in names.items():
        assert final["tags"][name + ":v0.5.1"] == digests[kind]
        assert final["tags"][name + ":0.5"] == digests[kind]
        if old_version:
            assert final["tags"][name + ":v0.5.0"] == state["tags"][name + ":v0.5.0"]


def test_release_promotion_same_digest_rerun_does_not_write(tmp_path):
    state, names, digests = _stub_registry(tmp_path)
    for kind, name in names.items():
        state["tags"][name + ":v0.5.1"] = digests[kind]
        state["tags"][name + ":0.5"] = digests[kind]
    result, final = _run_promotion(tmp_path, state)
    assert result.returncode == 0, result.stderr
    assert final["creates"] == []
    assert final["tags"] == state["tags"]


@pytest.mark.parametrize("case", ["rollback", "runtime_conflict", "builder_conflict",
                                   "alias_conflict", "unknown_version", "wrong_minor",
                                   "inconsistent_platforms", "missing_platform_version", "registry_error"])
def test_release_promotion_refusals_do_not_partially_promote(tmp_path, case):
    old_version = "0.5.2" if case == "rollback" else "0.5.0"
    state, names, digests = _stub_registry(tmp_path, old_version=old_version)
    builder = names["builder"]
    old_builder = state["tags"][builder + ":0.5"]
    if case in {"runtime_conflict", "builder_conflict"}:
        kind = "runtime" if case == "runtime_conflict" else "builder"
        state["tags"][names[kind] + ":v0.5.1"] = state["tags"][names[kind] + ":0.5"]
    elif case == "alias_conflict":
        state["tags"][builder + ":v0.5.0"] = digests["builder"]
    elif case == "unknown_version":
        state["images"][builder + "@" + old_builder] = {"config": {"Labels": {}}}
    elif case == "wrong_minor":
        state["images"][builder + "@" + old_builder]["config"]["Labels"]["org.opencontainers.image.version"] = "0.4.0"
    elif case == "inconsistent_platforms":
        state["images"][builder + "@" + old_builder] = {
            "linux/amd64": {"config": {"Labels": {"org.opencontainers.image.version": "0.5.0"}}},
            "linux/arm64": {"config": {"Labels": {"org.opencontainers.image.version": "0.5.2"}}},
        }
    elif case == "missing_platform_version":
        state["images"][builder + "@" + old_builder] = {
            "linux/amd64": {"config": {"Labels": {"org.opencontainers.image.version": "0.5.0"}}},
            "linux/arm64": {"config": {"Labels": {}}},
        }
    elif case == "registry_error":
        state["errors"][builder + ":v0.5.1"] = "ERROR: registry connection refused"
    result, final = _run_promotion(tmp_path, state)
    assert result.returncode != 0, result.stdout
    assert final["creates"] == []
    assert final["tags"] == state["tags"]


def test_release_events_share_a_non_cancelling_promotion_group():
    text = _workflow_text()
    concurrency = text.split("concurrency:\n", 1)[1].split("\njobs:", 1)[0]
    assert "github.event_name == 'release'" in concurrency
    assert "&& format('promotion-run-{0}', github.run_id)" in concurrency
    assert "&& 'dispatch'" in concurrency
    assert "format('pr-{0}', github.ref)" in concurrency
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in concurrency
    promotion = _job_block("promote-images")
    assert "      group: ${{ github.workflow }}-promotion" in promotion
    assert "      queue: max" in promotion
    assert "      cancel-in-progress: false" in promotion


@pytest.mark.parametrize("destination", ["immutable", "minor"])
def test_release_final_read_refuses_external_tag_changes(tmp_path, destination):
    state, names, _ = _stub_registry(tmp_path, old_version="0.5.0")
    runtime = names["runtime"]
    if destination == "immutable":
        ref = runtime + ":v0.5.1"
        changed_digest = state["tags"][runtime + ":0.5"]
    else:
        ref = runtime + ":0.5"
        changed_digest = "sha256:" + "e" * 64
        state["images"][runtime + "@" + changed_digest] = {
            "config": {"Labels": {"org.opencontainers.image.version": "0.5.2"}},
        }
        state["tags"][runtime + ":v0.5.2"] = changed_digest
    state["mutation"] = {"ref": ref, "digest": changed_digest}
    result, final = _run_promotion(tmp_path, state)
    assert result.returncode != 0, result.stdout
    assert final["reads"].count(ref) == 2
    assert final["tags"][ref] == changed_digest
    assert ref not in final["creates"]
    if destination == "immutable":
        assert final["creates"] == []
    else:
        # A valid immutable tag can already be written before the external
        # minor update is observed; cross-tag transactions are unavailable.
        assert final["creates"] == [runtime + ":v0.5.1"]
    assert all(not created.startswith(names["builder"]) for created in final["creates"])


def _pending_release_contract(tags, policy):
    # Documented Actions contract, not a hosted scheduler emulator. The first
    # run is already active when every remaining release starts waiting.
    assert 1 <= len(tags) <= 101
    assert policy in {"single", "max"}
    if policy == "max" or len(tags) == 1:
        return tags
    return [tags[0], tags[-1]]


def _release_runs_retained_by_workflow(tags):
    outer = _workflow_text().split("concurrency:\n", 1)[1].split("\njobs:", 1)[0]
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in outer
    shared_outer = bool(re.search(r"'release'\s*&& 'promotion'", outer))
    if shared_outer:
        queue = re.search(r"^  queue: (single|max)$", outer, re.MULTILINE)
        retained = _pending_release_contract(tags, queue.group(1) if queue else "single")
    else:
        assert re.search(
            r"'release'\s*&& format\('promotion-run-\{0\}', github.run_id\)", outer
        ), "published releases need unique outer groups before their job queue"
        retained = tags
    promotion = _job_block("promote-images")
    shared_job = "    concurrency:\n" in promotion
    if shared_job:
        assert "      group: ${{ github.workflow }}-promotion" in promotion
        assert "      cancel-in-progress: false" in promotion
        queue = re.search(r"^      queue: (single|max)$", promotion, re.MULTILINE)
        retained = _pending_release_contract(retained, queue.group(1) if queue else "single")
    assert shared_outer or shared_job, "promotion writers must remain serialized"
    return retained


def test_three_published_releases_keep_every_immutable_image_tag(tmp_path):
    # Uses the workflow's declared queue at BOTH layers and the actual retag
    # bash against the existing PATH-pinned stub. No GitHub release or Docker
    # daemon/registry is contacted. Source: GitHub Actions concurrency docs.
    tags = ["v0.5.0", "v0.5.1", "v0.5.2"]
    retained = _release_runs_retained_by_workflow(tags)
    state, names, first_digests = _stub_registry(tmp_path, tag=tags[0])
    expected = {tags[0]: first_digests}
    for tag, characters in zip(tags[1:], [("c", "d"), ("e", "f")], strict=True):
        expected[tag] = {}
        for kind, character in zip(names, characters, strict=True):
            digest = "sha256:" + character * 64
            expected[tag][kind] = digest
            state["images"][names[kind] + "@" + digest] = {
                "config": {"Labels": {"org.opencontainers.image.version": tag[1:]}}
            }
    for tag in retained:
        manifest = {"images": {
            kind: {"name": name, "digest": expected[tag][kind]}
            for kind, name in names.items()
        }}
        (tmp_path / "staging/release-manifest.json").write_text(json.dumps(manifest))
        result, state = _run_promotion(tmp_path, state, tag)
        assert result.returncode == 0, result.stdout + result.stderr
    for tag in tags:
        for kind, name in names.items():
            assert state["tags"].get(name + ":" + tag) == expected[tag][kind], (
                f"queued release {tag} was lost; retained={retained}"
            )
    for kind, name in names.items():
        assert state["tags"][name + ":0.5"] == expected[tags[-1]][kind]
