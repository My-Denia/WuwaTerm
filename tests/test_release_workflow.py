"""Release guard, artifact, registry, and checklist behavior tests."""

from __future__ import annotations

import ast
import copy
import itertools
import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
PROMOTION = ROOT / "scripts" / "promote_release_images.sh"
CHECKLIST = ROOT / "docs" / "release-checklist.md"

# The list entries of the notes generator: `              "### Assets",`.
# Anchored to the trailing comma so the step-summary headings the workflow also
# writes (`echo "### Images"`) cannot be mistaken for release-note sections.
NOTES_SECTION = re.compile(r'^\s*"(### .+)",$', re.MULTILINE)

# The version part differs legitimately per file - a literal, an English
# placeholder, a Chinese one, a shell variable - so it is matched loosely and
# normalised away below. The prefix, the platform and the extension are the
# contract.


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow_config(text=None):
    return yaml.load(text if text is not None else _workflow_text(), Loader=yaml.BaseLoader)


def _template_block() -> str:
    """The fenced release-note template out of the checklist."""
    text = CHECKLIST.read_text(encoding="utf-8")
    blocks = re.findall(r"^```markdown\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)
    assert len(blocks) == 1, (
        f"expected exactly one fenced markdown template in {CHECKLIST.name}, "
        f"found {len(blocks)}"
    )
    return blocks[0]


def _assert_release_triggers(workflow):
    triggers = workflow["on"]
    assert set(triggers) == {"workflow_dispatch", "release"}
    assert triggers["release"]["types"] == ["published"]


def _condition_value(condition, context):
    expression = condition.strip().removeprefix("${{").removesuffix("}}").strip()
    for source, target in {
        "github.event.release.tag_name": "tag", "github.event.action": "action",
        "github.event_name": "event", "needs.preflight.outputs.dry_run": "dry",
        "startsWith": "starts_with",
    }.items():
        expression = expression.replace(source, target)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"\btrue\b", "True", expression, flags=re.IGNORECASE)
    expression = re.sub(r"\bfalse\b(?!')", "False", expression, flags=re.IGNORECASE)
    tree = ast.parse(expression, mode="eval")

    def read(node):
        if isinstance(node, ast.Expression):
            return read(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return context[node.id]
        if isinstance(node, ast.BoolOp):
            values = [bool(read(item)) for item in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = read(node.left), read(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "starts_with":
            return str(read(node.args[0])).startswith(read(node.args[1]))
        raise AssertionError(f"Unsupported condition: {condition}")

    return bool(read(tree))


def _write_scopes(permissions):
    if isinstance(permissions, str):
        assert permissions in {"read-all", "write-all"}
        return {"*"} if permissions == "write-all" else set()
    assert isinstance(permissions, dict)
    assert all(value in {"read", "write", "none"} for value in permissions.values())
    return {scope for scope, value in permissions.items() if value == "write"}


def _assert_write_guards(workflow):
    assert "permissions" in workflow
    assert not _write_scopes(workflow["permissions"]), "workflow default grants write"
    expected = {
        "push-images": {"packages"},
        "draft-release": {"contents"},
        "promote-images": {"packages"},
    }
    jobs = workflow["jobs"]
    for name, job in jobs.items():
        scopes = _write_scopes(job["permissions"]) if "permissions" in job else set()
        assert scopes == expected.get(name, set()), f"{name}: unexpected write permissions {scopes}"
    assert expected.keys() <= jobs.keys()
    for job in expected:
        condition = jobs[job]["if"]
        for event, dry, action, tag in itertools.product(
            ("workflow_dispatch", "pull_request", "push", "release", "schedule"),
            ("true", "false"), ("published", "draft"), ("v1.0.0", "other"),
        ):
            allowed = _condition_value(condition, dict(event=event, dry=dry, action=action, tag=tag))
            if allowed:
                if job == "promote-images":
                    assert event == "release" and action == "published" and tag.startswith("v"), job
                else:
                    assert event == "workflow_dispatch" and dry == "false", job
        valid = dict(event="release", dry="true", action="published", tag="v1.0.0") if job == "promote-images" else dict(event="workflow_dispatch", dry="false", action="draft", tag="other")
        assert _condition_value(condition, valid), job


def _assert_ci_receipt_gate(workflow):
    jobs = workflow["jobs"]
    step = next(
        step for step in jobs["preflight"]["steps"]
        if step.get("name") == "Verify successful CI for the actual source"
    )
    assert step["if"] == "steps.mode.outputs.dry_run == 'false'"
    assert "continue-on-error" not in step
    commands = [
        line for line in step["run"].splitlines()
        if "scripts/verify_ci_source.py verify" in line
    ]
    assert len(commands) == 2
    assert all(line.split()[:3] == ["python", "scripts/verify_ci_source.py", "verify"]
               and line.split()[3] == '"$SOURCE_COMMIT"'
               and "||" not in line for line in commands)
    assert step["env"]["SOURCE_COMMIT"] == "${{ steps.commit.outputs.source_commit }}"
    assert "continue-on-error" not in jobs["preflight"]
    for job in ("push-images", "draft-release"):
        assert "preflight" in jobs[job]["needs"], job


def test_ci_receipt_gate_is_wired_before_every_write():
    _assert_ci_receipt_gate(_workflow_config())


@pytest.mark.parametrize("mutation", [
    "removed", "always", "soft_fail", "job_soft_fail", "or_true", "echo", "wrong_source",
    "push_need", "draft_need",
])
def test_ci_receipt_gate_rejects_bypass(mutation):
    workflow = _workflow_config()
    jobs = workflow["jobs"]
    steps = jobs["preflight"]["steps"]
    index = next(
        i for i, step in enumerate(steps)
        if step.get("name") == "Verify successful CI for the actual source"
    )
    if mutation == "removed":
        del steps[index]
    elif mutation == "always":
        steps[index]["if"] = "always()"
    elif mutation == "soft_fail":
        steps[index]["continue-on-error"] = "true"
    elif mutation == "job_soft_fail":
        jobs["preflight"]["continue-on-error"] = "true"
    elif mutation == "or_true":
        steps[index]["run"] = steps[index]["run"].replace(
            '--run-id "$VALIDATION_RUN_ID"', '--run-id "$VALIDATION_RUN_ID" || true')
    elif mutation == "echo":
        steps[index]["run"] = steps[index]["run"].replace("python scripts/", "echo python scripts/")
    elif mutation == "wrong_source":
        steps[index]["env"]["SOURCE_COMMIT"] = "${{ github.workflow_sha }}"
    else:
        job = "push-images" if mutation == "push_need" else "draft-release"
        jobs[job]["needs"].remove("preflight")
    with pytest.raises((AssertionError, StopIteration)):
        _assert_ci_receipt_gate(workflow)


def _publishing_lines(text):
    return [
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
        and ("--draft=false" in line or "gh release edit" in line)
    ]


def test_release_workflow_never_publishes():
    assert _publishing_lines(_workflow_text()) == []


def test_publish_guard_rejects_an_added_publish_step():
    assert _publishing_lines('      - run: gh release edit "$TAG" --draft=false\n')


def test_release_triggers_accept_equivalent_yaml_list():
    text = _workflow_text()
    _assert_release_triggers(_workflow_config(text))
    equivalent = text.replace("types:\n      - published", "types: [published]")
    assert equivalent != text
    _assert_release_triggers(_workflow_config(equivalent))


@pytest.mark.parametrize("job", ["push-images", "draft-release", "promote-images"])
def test_write_guards_reject_or_true(job):
    workflow = _workflow_config()
    _assert_write_guards(workflow)
    altered = copy.deepcopy(workflow)
    altered["jobs"][job]["if"] += " || true"
    with pytest.raises(AssertionError):
        _assert_write_guards(altered)


@pytest.mark.parametrize("where", ["workflow", "non_write_job", "write_job", "other_scope"])
def test_write_guards_reject_unexpected_permissions(where):
    workflow = _workflow_config()
    if where == "workflow":
        workflow["permissions"] = "write-all"
    elif where == "non_write_job":
        workflow["jobs"]["images"]["permissions"] = "write-all"
    elif where == "write_job":
        workflow["jobs"]["push-images"]["permissions"] = "write-all"
    else:
        workflow["jobs"]["push-images"]["permissions"]["issues"] = "write"
    with pytest.raises(AssertionError):
        _assert_write_guards(workflow)


def test_write_guards_accept_equivalent_permissions_yaml():
    workflow = _workflow_config()
    equivalent = copy.deepcopy(workflow)
    equivalent["permissions"] = "read-all"
    text = yaml.dump(equivalent)
    _assert_write_guards(_workflow_config(text))


@pytest.mark.parametrize("remove_draft", [False, True])
def test_backend_create_command_requires_draft(tmp_path, remove_draft):
    script = _step_run(_job_block("draft-release"), "Create the draft release")
    if remove_draft:
        script = script.replace("--draft ", "", 1)
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env python3
import sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] != ['release', 'create'] or '--draft' not in args or '--draft=false' in args:
    sys.exit(1)
Path('draft-created').touch()
""")
    gh.chmod(0o755)
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
             "TAG": "v1.0.0", "TARGET_COMMIT": "a" * 40, "RUNNER_TEMP": str(tmp_path)})
    assert (result.returncode == 0) == (not remove_draft), result.stderr
    assert (tmp_path / "draft-created").exists() == (not remove_draft)


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


def _step_run(block: str, name: str) -> str:
    """Execute a workflow step's shell in a clean runner directory."""
    start = block.index(f"      - name: {name}")
    run = block.index("        run: |\n", start) + len("        run: |\n")
    end = block.find("\n      - ", run)
    return textwrap.dedent(block[run:end if end != -1 else None])


def test_promotion_download_resolves_repository_without_a_checkout(tmp_path):
    """The release runner starts outside Git; gh needs explicit repository context."""
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env python3
import os
import sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] != ['release', 'download']:
    sys.exit(2)
if '--repo' not in args and not os.environ.get('GH_REPO'):
    print('fatal: not a git repository', file=sys.stderr)
    sys.exit(1)
dest = Path(args[args.index('--dir') + 1])
dest.mkdir(parents=True, exist_ok=True)
(dest / 'release-manifest.json').write_text('''{"dry_run":false,"tag":"v0.6.0","images":{"runtime":{"digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"builder":{"digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}}''')
""")
    gh.chmod(0o755)
    block = _job_block("promote-images")
    step = _step_run(block, "Download the published release manifest")
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "TAG": "v0.6.0", "GH_TOKEN": "fixture"}
    env.pop("GH_REPO", None)
    if "GH_REPO: ${{ github.repository }}" in block:
        env["GH_REPO"] = "My-Denia/WuwaTerm"
    result = subprocess.run(["bash", "-c", step], cwd=tmp_path, env=env,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_build_jobs_skip_the_release_event():
    for job in ("preflight", "python-package", "images", "assemble"):
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
    assert "bash scripts/promote_release_images.sh" in block
    assert "imagetools" in PROMOTION.read_text()
    assert "release-manifest.json" in block
    assert "deploy/Dockerfile" not in block
    assert "docker buildx build" not in block
    executable = [
        line
        for line in block.splitlines()
        if "--draft=false" in line and not line.lstrip().startswith("#")
    ]
    assert executable == []


# Execute the actual promotion step with a stateful local registry stub.
# No Docker daemon, credentials, socket, or upstream registry is used.
def _promotion_script():
    return PROMOTION.read_text(encoding="utf-8")


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
    assert "format('promotion-run-{0}', github.run_id)" in concurrency
    assert "&& 'dispatch'" in concurrency
    assert "cancel-in-progress: false" in concurrency
    promotion = _job_block("promote-images")
    assert "      group: ${{ github.repository }}-release-promotion" in promotion
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
    assert "cancel-in-progress: false" in outer
    shared_outer = bool(re.search(r"'release'\s*&& 'promotion'", outer))
    if shared_outer:
        queue = re.search(r"^  queue: (single|max)$", outer, re.MULTILINE)
        retained = _pending_release_contract(tags, queue.group(1) if queue else "single")
    else:
        assert re.search(
            r"format\('promotion-run-\{0\}', github.run_id\)", outer
        ), "published releases need unique outer groups before their job queue"
        retained = tags
    promotion = _job_block("promote-images")
    shared_job = "    concurrency:\n" in promotion
    if shared_job:
        assert "      group: ${{ github.repository }}-release-promotion" in promotion
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


def test_backend_release_has_no_client_build_or_asset_dependency(tmp_path):
    text = _workflow_text()
    assert "client/" not in text
    assert "client-zip" not in text
    assert "client_version" not in text
    for job in ("push-images", "assemble", "draft-release"):
        assert "client" not in _job_block(job)
    for name in ("wuwaterm-1.0.0-py3-none-any.whl", "wuwaterm-1.0.0.tar.gz"):
        (tmp_path / name).write_bytes(b"synthetic artifact")
    result = subprocess.run(
        ["bash", "-c", _step_run(_job_block("assemble"),
         "Checksums over the two downloadable build outputs")],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert len((tmp_path / "SHA256SUMS").read_text().splitlines()) == 2
    assert "startsWith(github.event.release.tag_name, 'v')" in _job_block("promote-images")


def _checklist_bash(marker):
    blocks = re.findall(r"^```bash\n(.*?)^```[ \t]*$", CHECKLIST.read_text(), re.DOTALL | re.MULTILINE)
    matches = [block for block in blocks if marker in block]
    assert len(matches) == 1, marker
    return matches[0]


@pytest.mark.parametrize("case", ["valid", "target", "body", "assets", "identity", "not_draft", "existing_tag", "lookup_error", "snapshot", "prerelease", "audited_prerelease", "optimized_body", "optimized_valid"])
def test_publication_guard_refuses_draft_drift_before_publish(tmp_path, case):
    import hashlib

    source = "a" * 40
    tag = "v0.6.1"
    doc = {"url": "https://api.github.com/repos/My-Denia/WuwaTerm/releases/123",
           "id": 123, "name": tag, "tag_name": tag, "draft": True,
           "prerelease": case == "audited_prerelease",
           "target_commitish": source, "body": "Reviewed release notes", "assets": []}
    for index, name in enumerate(["wuwaterm-0.6.1-py3-none-any.whl", "wuwaterm-0.6.1.tar.gz", "SHA256SUMS", "release-manifest.json"], 1):
        doc["assets"].append({"id": index, "name": name, "size": 10, "state": "uploaded", "digest": "sha256:" + "b" * 64})
    audited = tmp_path / "audited-release.json"
    audited.write_text(json.dumps(doc))
    digest = hashlib.sha256(audited.read_bytes()).hexdigest()
    current = json.loads(json.dumps(doc))
    if case == "target":
        current["target_commitish"] = "c" * 40
    if case in {"body", "optimized_body"}:
        current["body"] += " changed"
    if case == "prerelease":
        current["prerelease"] = True
    if case == "assets":
        current["assets"][0]["size"] += 1
    if case == "identity":
        current["id"] += 1
    if case == "not_draft":
        current["draft"] = False
    if case == "snapshot":
        audited.write_text(audited.read_text() + " ")
    (tmp_path / "remote.json").write_text(json.dumps(current))
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == 'api':
    assert args[1] == 'https://api.github.com/repos/My-Denia/WuwaTerm/releases/123'
    print(Path('remote.json').read_text())
elif args[:2] == ['release', 'edit']:
    assert args[2] == 'v0.6.1'
    assert '--draft=false' in args and '--latest' in args and '--prerelease=false' in args
    assert args[args.index('--repo') + 1] == 'My-Denia/WuwaTerm'
    assert args[args.index('--target') + 1] == os.environ['reviewed_main_commit']
    Path('published').touch()
else:
    sys.exit(3)
""")
    gh.chmod(0o755)
    git = tmp_path / "git"
    git.write_text('#!/bin/sh\nexit "$MOCK_TAG_STATUS"\n')
    git.chmod(0o755)
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "NEXT_VERSION": tag, "reviewed_main_commit": source, "verify_dir": str(tmp_path),
           "audited_snapshot_sha256": digest,
           "MOCK_TAG_STATUS": "0" if case == "existing_tag" else "128" if case == "lookup_error" else "2"}
    if case.startswith("optimized_"):
        env["PYTHONOPTIMIZE"] = "1"
    result = subprocess.run(["bash", "-c", _checklist_bash("# Publication guard:")],
                            cwd=tmp_path, env=env, text=True, capture_output=True)
    assert (result.returncode == 0) == (case in {"valid", "optimized_valid"}), result.stderr
    assert (tmp_path / "published").exists() == (case in {"valid", "optimized_valid"})


@pytest.mark.parametrize("case", ["existing_minor", "first_minor", "changed_minor", "lookup_error", "existing_semver"])
def test_draft_registry_guard_preserves_existing_minor_tag(tmp_path, case):
    digest = "sha256:" + "a" * 64
    previous = "404 -" if case == "first_minor" else "200 sha256:" + "b" * 64
    repos = ["my-denia/wuwaterm", "my-denia/wuwaterm-builder"]
    (tmp_path / "minor-before.tsv").write_text("".join(f"{repo}\t{previous}\n" for repo in repos))
    (tmp_path / "release-manifest.json").write_text(json.dumps({"images": {
        str(i): {"name": "ghcr.io/" + repo, "digest": digest} for i, repo in enumerate(repos)}}))
    stub = '''probe() {
      printf '%s %s\n' "$1" "$2" >> "$verify_dir/probes"
      case "$2" in
        sha-*) printf '%s\n' "$MOCK_SOURCE";;
        v*) if [ "$MOCK_CASE" = existing_semver ]; then printf '%s\n' "$MOCK_SOURCE"; else printf '404 -\n'; fi;;
        *) case "$MOCK_CASE" in
          changed_minor) printf '%s\n' "$MOCK_SOURCE";;
          lookup_error) printf '403 -\n';;
          *) printf '%s\n' "$MOCK_MINOR";;
        esac;;
      esac
    }
'''
    env = {**os.environ, "NEXT_VERSION": "v0.6.1", "reviewed_main_commit": "a" * 40,
           "verify_dir": str(tmp_path), "MOCK_SOURCE": "200 " + digest,
           "MOCK_CASE": case, "MOCK_MINOR": previous}
    result = subprocess.run(["bash", "-c", stub + _checklist_bash("# Draft registry guard:")],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    probes = (tmp_path / "probes").read_text().splitlines()
    assert probes, "probe never ran"
    if case in {"existing_minor", "first_minor"}:
        assert len(probes) == 6, "both image repositories must be inspected"
    assert (result.returncode == 0) == (case in {"existing_minor", "first_minor"}), result.stderr


@pytest.mark.parametrize("changed", [False, True])
def test_published_readback_downloads_fresh_bytes_and_preserves_draft(tmp_path, changed):
    import hashlib

    source = "a" * 40
    data = {
        "wuwaterm-0.6.1-py3-none-any.whl": "wheel bytes",
        "wuwaterm-0.6.1.tar.gz": "sdist bytes",
        "release-manifest.json": json.dumps({"tag": "v0.6.1", "source_commit": source, "dry_run": False}),
    }
    data["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(data[name].encode()).hexdigest()}  {name}\n"
        for name in ["wuwaterm-0.6.1-py3-none-any.whl", "wuwaterm-0.6.1.tar.gz"])
    for name, body in data.items():
        (tmp_path / name).write_text(body)
    snapshot = {"assets": [{"name": name, "digest": "sha256:" + hashlib.sha256(body.encode()).hexdigest()}
                           for name, body in data.items()]}
    (tmp_path / "audited-release.json").write_text(json.dumps(snapshot))
    remote = dict(data)
    if changed:
        remote["release-manifest.json"] += " "
    (tmp_path / "remote-assets.json").write_text(json.dumps(remote))
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env python3
import json, sys
from pathlib import Path
args = sys.argv[1:]
assert args[:3] == ['release', 'download', 'v0.6.1']
assert args[args.index('--repo') + 1] == 'My-Denia/WuwaTerm'
root = Path(args[args.index('--dir') + 1])
Path('download-dir').write_text(str(root))
for name, body in json.loads(Path('remote-assets.json').read_text()).items():
    target = root / name
    assert not target.exists(), 'download refuses existing files without clobber'
    target.write_text(body)
""")
    gh.chmod(0o755)
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "verify_dir": str(tmp_path), "NEXT_VERSION": "v0.6.1", "reviewed_main_commit": source}
    result = subprocess.run(["bash", "-c", _checklist_bash("# Published artifact readback:")],
                            cwd=tmp_path, env=env, text=True, capture_output=True)
    assert (result.returncode == 0) == (not changed), result.stderr
    downloaded = Path((tmp_path / "download-dir").read_text())
    assert downloaded != tmp_path and downloaded.parent == tmp_path
    for name, body in data.items():
        assert (tmp_path / name).read_text() == body
    if changed:
        assert "published bytes differ" in result.stderr


@pytest.mark.parametrize("case", ["valid", "draft", "prerelease", "target", "tag_mismatch"])
def test_post_publish_state_requires_stable_release_and_exact_source(tmp_path, case):
    source = "a" * 40
    release = {"isDraft": case == "draft", "isPrerelease": case == "prerelease",
               "tagName": "v0.6.1", "targetCommitish": "b" * 40 if case == "target" else source}
    (tmp_path / "remote.json").write_text(json.dumps(release))
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env python3
import sys
from pathlib import Path
args = sys.argv[1:]
assert args[:3] == ['release', 'view', 'v0.6.1']
assert args[args.index('--repo') + 1] == 'My-Denia/WuwaTerm'
assert 'isPrerelease' in args[args.index('--json') + 1]
print(Path('remote.json').read_text())
""")
    gh.chmod(0o755)
    git = tmp_path / "git"
    git.write_text('#!/bin/sh\nprintf "%s\\trefs/tags/v0.6.1\\n" "$MOCK_TAG_SHA"\n')
    git.chmod(0o755)
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "NEXT_VERSION": "v0.6.1", "reviewed_main_commit": source, "verify_dir": str(tmp_path),
           "PYTHONOPTIMIZE": "1", "MOCK_TAG_SHA": "c" * 40 if case == "tag_mismatch" else source}
    result = subprocess.run(["bash", "-c", _checklist_bash("# Published release state:")],
                            cwd=tmp_path, env=env, text=True, capture_output=True)
    assert (result.returncode == 0) == (case == "valid"), result.stderr
