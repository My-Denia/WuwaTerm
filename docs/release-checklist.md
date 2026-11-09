# Release Checklist

Use this checklist to publish a GitHub release. From v0.4.0 onward the assets
are **built by a workflow, not by hand**: `.github/workflows/release.yml` builds
the backend artifacts from one reviewed commit, and the human steps are deciding which
commit, reading the draft back, and publishing it.

Do not attach generated SQLite databases, TextMap files, or bulk game data to a
release. That boundary has not moved.

## Release Metadata

- Prospective release version: derive `v<project.version>` from
  `pyproject.toml` at the reviewed release commit; never copy the latest
  published tag. The current Core version is `0.7.1`, the installer and
  review-correctness patch on top of `0.7.0`; `v0.7.0` remains the published
  baseline and `v0.6.1` the one before it. Set `NEXT_VERSION` to the derived
  tag explicitly before any release command below.
- Windows product: [v0.3.0](https://github.com/My-Denia/WuwaTerm-Windows/releases/tag/v0.3.0), released independently.
- Supported source profile: `arikatsu`
- Supported game data version:
  `GameVer 3.7.0 | ResVer 3.7.8 | Changelist 8975829`
- Pinned source repository:
  `https://github.com/Arikatsu/WutheringWaves_Data`
- Pinned source commit:
  `9218d612ad815e398e064e577e42aaf878899968`
- Fallback source profile: `dimbreath_legacy`
- Fallback pinned commit:
  `e9234ffe094b2d944d16b222d31102e8ab32d954`

These values are not typed into the release notes by hand. The workflow reads
them out of `src/wuwaterm/constants.py` at build time and writes them into the
notes and into `release-manifest.json`, so a stale pin in this file cannot
become a wrong claim in a published release. Keep them in step anyway — this is
the page a reader checks first.

## What A Release Carries

Releases up to and including v0.3.0 carried only the wheel, the sdist and
`SHA256SUMS`: no client binary and no container image. Versions v0.4.0 through v0.6.0 also bundled the client. Future backend releases
use the independent workflows described here.

**Four backend assets, and no others:**

| asset | built by | audited by |
| --- | --- | --- |
| `wuwaterm-<version>-py3-none-any.whl` | `python -m build` on `ubuntu-latest` | `twine check --strict`, `scripts/check_package_artifacts.py`, clean-venv install + import + CLI smoke |
| `wuwaterm-<version>.tar.gz` | the same build | the same three |
| `SHA256SUMS` | the workflow, over the two files above | verified by re-download while the release is still a draft |
| `release-manifest.json` | the workflow | read back against the reviewed commit while the release is still a draft |

`release-manifest.json` records the tag, the server version, the source commit, the build time, whether the run was a dry run, the
image names, tags and digests, and the game-data pin. It is what makes a
downloaded asset traceable to a commit without trusting the release page.

**Two container images, on the registry rather than on the release page:**
`ghcr.io/my-denia/wuwaterm` (runtime) and `ghcr.io/my-denia/wuwaterm-builder`
(builder). The draft-time push writes only `sha-<7>`. After a maintainer
publishes the GitHub draft, `promote-images` retags the exact digests recorded
in that release's `release-manifest.json` as `vX.Y.Z` and `X.Y`. The full
version tag is immutable; the `X.Y` registry tag advances to a newer patch only after
its current OCI version label and immutable tag agree. Missing or conflicting
version evidence, registry read failures and rollback attempts stop promotion.
Both images are checked before any tag changes. Published-release workflow runs
have unique groups; their promotion jobs share one group with `queue: max` and
`cancel-in-progress: false`. This retains up to 100 pending jobs; further jobs
are canceled when that queue is full. GitHub queues by when jobs start waiting,
not release publication or version order. An older patch arriving after a newer
promotion can therefore fail the rollback preflight without writing any tags.
See the [Actions concurrency contract](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
A rerun skips tags already at the recorded digest.
Final reads also refuse an externally changed immutable tag or a newer minor
version. Inspection and retagging have no compare-and-swap guarantee, and the
registry has no transaction across tags: a race or write failure can still leave a
partial promotion, which a rerun completes after the failure is resolved. A discarded
draft therefore cannot leave release-looking registry tags. Both images are
published because the runtime image is useless without a terminology database,
and that database is built by the builder image and is never distributed. The
images save the local image build and nothing more: the generic path still
needs a source checkout at the release tag for the Compose files, the
entrypoints, the data build and the verification scripts. Say that in the
notes; do not let "images are published" be read as "no checkout needed".

## Independent Windows client release

[WuwaTerm-Windows](https://github.com/My-Denia/WuwaTerm-Windows) owns its own build and release workflow.
The [v0.3.0 release](https://github.com/My-Denia/WuwaTerm-Windows/releases/tag/v0.3.0) is unsigned; see its repository for
current release instructions and SmartScreen guidance. Existing Core bundled
release assets remain available unchanged.

## Validation

Run the repository's gates from a clean checkout of the candidate commit:

```bash
python scripts/validate.py
```

That is hygiene, architecture boundaries, the API contract, ruff and
the test suite, in that order, and it is the same entry point CI's server
matrix runs. It is not the whole pull request: the lock-drift check, the
packaging audit, Web and Windows contract checks, and the Docker boundary job are
separate CI jobs. Confirm on the pull request, not only locally.

For 0.7.1 specifically, also confirm:

- On a clean host with a verified standalone Compose binary, `deploy/setup.sh`
  reports `updates: not adopted` when `adopt` refuses, and retries on the next
  run instead of claiming the metadata is already there. `--status` reads the
  same pointer. This is what a first install now says, so check the wording
  before quoting it in the release notes.
- `--with-web` against the published WuwaTerm-Web `v0.1.0` assets installs the
  `archive.name` archive, runs `bundle/start.mjs` and passes the edge-gated
  readiness probe. The installer tests cover this with fixtures; a real host
  still has to be checked once per release.
- `review-v2` reports a one-character Chinese resonator name in a context the
  translation step referenced (`欢迎椿`, `快找椿`) and still ignores an ordinary
  word covering it (`香椿`, `臭椿`, `椿树`, `椿芽`). A `心` mention still needs a
  name-like context, so `心很重要` is neither referenced nor reported. The
  canonical matcher label is `wuwaterm-cjk-matcher-v5`, so resolution contexts
  carrying the older hash are stale by design, not by accident.
- `docs/self-hosting.md`'s backup and restore steps still match what
  `./deploy/setup.sh --with-web` writes under `state-web/`.
- `docs/api/openapi.json` matches the generated document after the version and
  description changes (`python scripts/check_api_contract.py`).
- The Web workbench is still a separate product with its own release. This
  repository carries the installer and verification path for `--with-web`,
  which installs the published `manifest-{root|prefix}.json` and
  `SHA256SUMS-{root|prefix}` package; the Core release notes link that package
  instead of shipping it.

For a locally built release candidate database, also run:

```bash
python scripts/verify_db.py data/terms.candidate.db --profile arikatsu --require-free-text-exclusions --require-free-text-zh-exclusions
python scripts/verify_seed_terms.py data/terms.candidate.db
python scripts/verify_exact_hits.py data/terms.candidate.db --sample-size 500
python scripts/verify_idempotent_build.py --data-dir data/wutheringdata --out-dir goal-runs/wuwaterm-v2-translator --profile arikatsu
```

The packaging gates (`python -m build`, `twine check --strict`,
`scripts/check_package_artifacts.py`) still exist and still have to pass, but
CI and the release workflow call the same package workflow; the release run
builds and checks the exact assets it uploads.

Release only after all required Core CI jobs and configured review workflows
pass for the exact commit. Windows releases follow their own repository gates.

## Privacy And LLM Notes

- Exact database hits are dictionary-first and do not call the LLM.
- Free-text sentence translation can call an OpenAI-compatible endpoint only
  when the maintainer configures one.
- Telegram bot tokens, OpenAI-compatible API keys, device credentials, owner
  IDs, chat IDs, `.env` files, runtime settings, deployment logs, and host
  names, addresses or paths that identify a deployment must not appear in
  release notes, release assets, commits, or screenshots.
- Operational logs must keep chat, user, and message identifiers redacted.

## Distribution Boundary

- Do not publish generated `terms.db` files as release assets.
- Do not publish generated TextMap data or bulk Wuthering Waves game data.
- Do not copy upstream game data into this repository.
- The published container images carry the application, not data: the runtime
  image has no terminology database in it and the builder image has no game
  data in it. Whoever runs them builds the database themselves.
- State that the MIT license covers this project's source code only, not
  Wuthering Waves game data or in-game terminology.

## The Flow

1. **Pick the commit.** Fetch and fast-forward `main`, and record the exact
   reviewed `origin/main` commit that passed CI and review. That SHA is the
   release target and is what goes into `target_commit`.
2. **Dry run first.** Dispatch the release workflow with `dry_run=true`. A dry
   run builds every asset and both images, without a registry login, image push,
   or release. Read its `release-assets` artifact before the real run. A run
   dispatched on `main` accepts only a target already on `main`, dry runs
   included. To dry-run a candidate commit, dispatch from its branch with
   `--ref`; its exact control and target must still satisfy the source epoch
   ancestry and layout rules. Dry runs require a stable `X.Y.Z` version, skip
   the receipt gate, and do not prove receipt verification.
3. **Record the registry baseline** using the block below before dispatch.
4. **The real run** creates a **draft**. It is the only mode that logs in to
   the registry, pushes images, or touches releases, and the two jobs that can
   do so are unreachable from any other trigger. The push runs only after the
   wheel, the sdist and both image builds have already
   succeeded, so a release tag is never pushed for a commit whose package or
   images had failed:

   ```bash
   NEXT_VERSION=vX.Y.Z
   reviewed_main_commit=<reviewed-main-commit-sha>
   gh workflow run release.yml --ref main \
     -f target_commit="$reviewed_main_commit" -f dry_run=false
   gh run list --repo My-Denia/WuwaTerm --workflow=release.yml \
     --event workflow_dispatch --limit 5 --json databaseId,headSha,createdAt,status
   # Select the run from this dispatch by time and source; do not assume latest.
   release_run_id=<matching-run-id>
   gh run view "$release_run_id" --repo My-Denia/WuwaTerm --json status,conclusion,jobs
   ```

   Before any write, the workflow verifies a schema 2 CI receipt for the exact
   source, trusted main control revision and all common and epoch-selected
   jobs. The complete run and selected jobs must succeed. Old receipts and
   failed, skipped or incomplete selected jobs do not qualify. If a source has
   no qualifying receipt, redispatch the current CI on main for that exact
   source, wait for the entire run to complete successfully, then separately
   verify that attempt before passing its run ID to the release:

   ```bash
   gh workflow run ci.yml --ref main -f target_commit="$reviewed_main_commit"
   # Select the matching dispatch; do not assume latest. Wait for completed/success.
   gh run list --repo My-Denia/WuwaTerm --workflow=ci.yml --event workflow_dispatch
   validation_run_id=<matching-completed-successful-ci-run-id>
   gh run view "$validation_run_id" --repo My-Denia/WuwaTerm --json status,conclusion,jobs
   GH_REPO=My-Denia/WuwaTerm python scripts/verify_ci_source.py verify \
     "$reviewed_main_commit" --run-id "$validation_run_id"
   gh workflow run release.yml --ref main -f target_commit="$reviewed_main_commit" \
     -f dry_run=false -f validation_run_id="$validation_run_id"
   ```

   Use a current main checkout for the separate verification. Every attempt
   needs its own receipt; re-running failed jobs alone cannot supply one.

   The workflow also refuses a present tag, malformed version, or source that
   is not an ancestor of `origin/main`.
5. **Read the draft back** ([Draft Readback](#draft-readback)). Everything that can be checked is
   checked while the release is still a draft, because a draft can be deleted
   and a published release cannot.
6. **Publish** by hand. The workflow never publishes.
7. **Read the published release back** and only then report it as released.

A draft has no tag. GitHub creates `refs/tags/<tag>` at the moment of
publication, not at draft creation, which is why deleting a draft leaves
nothing behind and why the tag-absence check is meaningful right up to the
publish command. The only registry tags a discarded draft can leave are
`sha-<7>` (unconfusable with `vX.Y.Z` / `X.Y`). If `promote-images` fails
after publication, re-run that job on the same workflow run; do not create
another draft.

## Registry Baseline Before Dispatch

Run these blocks in one Bash session after choosing the reviewed source. Keep
`verify_dir` and the `probe` function for the draft and published readbacks.
A patch release may already have a minor tag: capture its digest before dispatch
and require that digest to remain unchanged until publication. The new full-version
tag must be absent. This example stops on denied/failed lookups; use the existing
authenticated inspection route to establish equivalent evidence, never infer absence.

```bash
NEXT_VERSION=vX.Y.Z
reviewed_main_commit=<reviewed-main-commit-sha>
set -euo pipefail
verify_dir="$(mktemp -d)"
major_minor="${NEXT_VERSION#v}"; major_minor="${major_minor%.*}"

probe() {
  local repository="$1" ref="$2" token code digest headers
  headers="$verify_dir/headers-${repository##*/}-$ref"
  token="$(curl --fail -sS "https://ghcr.io/token?scope=repository:$repository:pull" | jq -er .token)" || return 1
  code="$(curl -sS -D "$headers" -o "$verify_dir/manifest-${repository##*/}-$ref.json" -w '%{http_code}' \
    -H "Authorization: Bearer $token" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
    "https://ghcr.io/v2/$repository/manifests/$ref")" || return 1
  digest="$(awk 'tolower($1) == "docker-content-digest:" {gsub("\r", "", $2); print $2}' "$headers")"
  if [ "$code" = 404 ]; then digest=-; fi
  printf '%s %s\n' "$code" "${digest:--}"
}

: > "$verify_dir/minor-before.tsv"
for repository in my-denia/wuwaterm my-denia/wuwaterm-builder; do
  observed="$(probe "$repository" "$major_minor")"
  [[ "$observed" =~ ^(200\ sha256:[a-f0-9]{64}|404\ -)$ ]]
  printf '%s\t%s\n' "$repository" "$observed" >> "$verify_dir/minor-before.tsv"
done
```

## Draft Readback

Run all of it. A single failure stops the publication.

```bash
NEXT_VERSION=vX.Y.Z
reviewed_main_commit=<reviewed-main-commit-sha>
set -euo pipefail

# 1. Exactly the four authorized assets, and no more.
test "$(gh release view "$NEXT_VERSION" --json assets --jq '.assets | length')" -eq 4
gh release view "$NEXT_VERSION" --json assets --jq '.assets[].name' | sort

# 2. The bytes on the release page are the bytes the checksums describe.
: "${verify_dir:?Run the registry baseline before dispatch}"
gh release download "$NEXT_VERSION" --dir "$verify_dir"
(cd "$verify_dir" && sha256sum -c SHA256SUMS)

# 3. The manifest describes the commit that was reviewed, and a real run.
test "$(jq -r .source_commit "$verify_dir/release-manifest.json")" = "$reviewed_main_commit"
test "$(jq -r .tag "$verify_dir/release-manifest.json")" = "$NEXT_VERSION"
test "$(jq -r .dry_run "$verify_dir/release-manifest.json")" = "false"
test "$(jq -r .images.runtime.digest "$verify_dir/release-manifest.json")" != "null"
test "$(jq -r .images.builder.digest "$verify_dir/release-manifest.json")" != "null"

# 4. The tag must STILL be absent. GitHub ignores target_commitish once a tag
#    exists, so a tag created by anyone between the dispatch and this moment
#    would silently retarget the release.
tag_lookup_status=0
git ls-remote --exit-code --tags origin "refs/tags/$NEXT_VERSION" >/dev/null || tag_lookup_status=$?
test "$tag_lookup_status" -eq 2
```

A `git ls-remote --exit-code` status of 2 means no matching ref was found,
which is the expected pass state before publication. A status of 0 means the
tag exists — stop. Any other status is a lookup ERROR, not an absence, and is
also a stop: the check must fail closed when it cannot answer.

**Then compare the registry to the pre-dispatch baseline**, without writing tags.
A new full-version tag must not exist. An existing minor tag must still have its
old digest; a missing minor tag must still be absent. The draft sha images must
match the downloaded manifest.

```bash
# Draft registry guard: preserve the pre-dispatch minor baseline.
set -euo pipefail
: "${NEXT_VERSION:?Set the candidate tag}"
: "${reviewed_main_commit:?Set the reviewed full SHA}"
: "${verify_dir:?Use the baseline/readback directory}"
short="${reviewed_main_commit:0:7}"
major_minor="${NEXT_VERSION#v}"; major_minor="${major_minor%.*}"
for repository in my-denia/wuwaterm my-denia/wuwaterm-builder; do
  expected_digest="$(jq -er --arg name "ghcr.io/$repository" '.images[] | select(.name == $name) | .digest' "$verify_dir/release-manifest.json")"
  [[ "$expected_digest" =~ ^sha256:[a-f0-9]{64}$ ]]
  test "$(probe "$repository" "sha-$short")" = "200 $expected_digest"
  test "$(probe "$repository" "$NEXT_VERSION")" = '404 -'
  before="$(awk -F '\t' -v repo="$repository" '$1 == repo {print $2}' "$verify_dir/minor-before.tsv")"
  [[ "$before" =~ ^(200\ sha256:[a-f0-9]{64}|404\ -)$ ]]
  test "$(probe "$repository" "$major_minor")" = "$before"
done
```

Record and independently review the draft itself, not only its manifest. Keep the
raw response and its SHA256 with the artifact audit. The release API URL identifies
the same release object for the final read, even if its tag or target is later edited.

```bash
release_api="$(gh release view "$NEXT_VERSION" --repo My-Denia/WuwaTerm --json apiUrl --jq .apiUrl)"
gh api "$release_api" > "$verify_dir/audited-release.json"
sha256sum "$verify_dir/audited-release.json"
```

After publication, the new full and minor tags must match the release manifest's
digests. Existing immutable release tags, including `v0.6.0`, must retain their
recorded digests. Historical `v0.4.0` / `0.4` must also remain unchanged. Use
an authenticated `docker buildx imagetools inspect` or package-version readback
when anonymous access is denied; do not change package visibility implicitly.

## Publish

The Python checks use isolated mode (`-I`) so `PYTHONOPTIMIZE` or local module
settings cannot disable or replace their validation. A stable release must have
`prerelease=false` before and after publication.

**Repeat the tag check here, in the same block as the publish command.** The
one in the draft readback above finished before the registry probes, and those
take minutes. GitHub ignores `target_commitish` once the tag exists, so a tag
created by anyone in that gap would silently retarget the release at publish
time — and the check is only worth anything if nothing can happen between it
and the command it guards:

```bash
# Publication guard: bind the audited draft, source, body and assets.
set -euo pipefail
: "${NEXT_VERSION:?Set the candidate tag}"
: "${reviewed_main_commit:?Set the reviewed full SHA}"
: "${verify_dir:?Use the independently reviewed readback directory}"
: "${audited_snapshot_sha256:?Set the snapshot hash accepted by the artifact audit}"
release_api="$(python3 -I - "$verify_dir/audited-release.json" "$audited_snapshot_sha256" <<'PY'
import hashlib, json, re, sys
from pathlib import Path
raw = Path(sys.argv[1]).read_bytes()
assert hashlib.sha256(raw).hexdigest() == sys.argv[2], 'audited snapshot changed'
url = json.loads(raw)['url']
assert re.fullmatch(r'https://api.github.com/repos/My-Denia/WuwaTerm/releases/[0-9]+', url), 'wrong release repository/object'
print(url)
PY
)"
gh api "$release_api" > "$verify_dir/current-release.json"
python3 -I - "$verify_dir/audited-release.json" "$verify_dir/current-release.json" "$NEXT_VERSION" "$reviewed_main_commit" <<'PY'
import json, re, sys
from pathlib import Path
old, current = [json.loads(Path(path).read_text()) for path in sys.argv[1:3]]
tag, source = sys.argv[3:5]
assert re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag)
assert re.fullmatch(r'[0-9a-f]{40}', source)
def fingerprint(doc):
    assert doc['tag_name'] == tag and doc['draft'] is True, 'not the expected draft'
    assert doc['prerelease'] is False, 'not a stable release draft'
    assert doc['target_commitish'] == source, 'draft target changed'
    assert isinstance(doc['id'], int) and doc['id'] > 0
    assert isinstance(doc['body'], str) and doc['body'].strip()
    assets = doc['assets']
    assert len(assets) == 4, 'wrong asset count'
    version = tag[1:]
    assert {a['name'] for a in assets} == {
        f'wuwaterm-{version}-py3-none-any.whl', f'wuwaterm-{version}.tar.gz',
        'SHA256SUMS', 'release-manifest.json'}, 'wrong asset names'
    for asset in assets:
        assert asset['state'] == 'uploaded' and asset['size'] > 0
        assert re.fullmatch(r'sha256:[0-9a-f]{64}', asset['digest']), 'missing asset digest'
    return (doc['id'], doc['name'], doc['body'], sorted(
        (a['id'], a['name'], a['size'], a['digest']) for a in assets))
assert fingerprint(old) == fingerprint(current), 'audited release body/assets changed'
PY

tag_lookup_status=0
git ls-remote --exit-code --tags https://github.com/My-Denia/WuwaTerm.git "refs/tags/$NEXT_VERSION" >/dev/null || tag_lookup_status=$?
test "$tag_lookup_status" -eq 2

gh release edit "$NEXT_VERSION" --repo My-Denia/WuwaTerm --draft=false --prerelease=false --latest --target "$reviewed_main_commit"
```

That explicit command publishes the GitHub draft. The sha-tagged images may
already be visible before it. These reads and publication are not a transaction
against an independent concurrent publisher; stop on observed drift and verify
the published result. A published tag/release cannot be taken back quietly.

## Post-Publish Readback

```bash
# Published release state: stable, public and bound to the reviewed source.
set -euo pipefail
: "${NEXT_VERSION:?Set the published tag}"
: "${reviewed_main_commit:?Set the reviewed full SHA}"
: "${verify_dir:?Use the reviewed readback directory}"
gh release view "$NEXT_VERSION" --repo My-Denia/WuwaTerm --json tagName,targetCommitish,isDraft,isPrerelease,url,assets > "$verify_dir/published-release.json"
python3 -I - "$verify_dir/published-release.json" "$NEXT_VERSION" "$reviewed_main_commit" <<'PY'
import json, sys
from pathlib import Path
release = json.loads(Path(sys.argv[1]).read_text())
assert release['isDraft'] is False and release['isPrerelease'] is False, 'release is not public and stable'
assert release['tagName'] == sys.argv[2] and release['targetCommitish'] == sys.argv[3], 'published release source differs'
PY
tag_ref="$(git ls-remote --exit-code --tags https://github.com/My-Denia/WuwaTerm.git "refs/tags/$NEXT_VERSION" "refs/tags/$NEXT_VERSION^{}")" || {
  echo "tag lookup failed or tag missing; stop"
  exit 1
}
tag_sha="$(
  printf '%s\n' "$tag_ref" | awk -v tag="refs/tags/$NEXT_VERSION" '
    $2 == tag "^{}" { print $1; found = 1; exit }
    $2 == tag { fallback = $1 }
    END {
      if (!found && fallback != "") print fallback
      else if (!found) exit 1
    }
  '
)" || {
  echo "tag ref parse failed; stop"
  exit 1
}
test "$tag_sha" = "$reviewed_main_commit"
```

The awk block exists because an annotated tag has to be compared against its
peeled commit ref `refs/tags/$NEXT_VERSION^{}` while a lightweight tag is
compared against the tag ref itself; this repository's tags have been
lightweight, and the block does not depend on that staying true.

Re-download into a fresh directory so the draft assets and audited snapshot
remain intact. Verify the published checksums and compare all four downloaded
files to the digests accepted in the draft audit. Verifying the draft is not
verifying the release.

```bash
# Published artifact readback: fresh bytes, preserved draft evidence.
set -euo pipefail
: "${verify_dir:?Use the reviewed draft directory}"
: "${NEXT_VERSION:?Set the published tag}"
: "${reviewed_main_commit:?Set the reviewed full SHA}"
published_dir="$(mktemp -d "$verify_dir/published.XXXXXX")"
gh release download "$NEXT_VERSION" --repo My-Denia/WuwaTerm --dir "$published_dir"
(cd "$published_dir" && sha256sum -c SHA256SUMS)
python3 -I - "$verify_dir/audited-release.json" "$published_dir" "$NEXT_VERSION" "$reviewed_main_commit" <<'PY'
import hashlib, json, sys
from pathlib import Path
snapshot = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
assets = snapshot['assets']
assert len(assets) == 4
assert {p.name for p in root.iterdir()} == {a['name'] for a in assets}
for asset in assets:
    actual = 'sha256:' + hashlib.sha256((root / asset['name']).read_bytes()).hexdigest()
    assert actual == asset['digest'], 'published bytes differ from audited draft'
manifest = json.loads((root / 'release-manifest.json').read_text())
assert manifest['tag'] == sys.argv[3] and manifest['source_commit'] == sys.argv[4]
assert manifest['dry_run'] is False
PY
```

## When Something Is Wrong

- **Before publication:** delete the draft. It has no tag, so nothing else has
  to be undone, and the workflow can be dispatched again for the same version.
- **After publication: do not delete the release and do not delete the tag.**
  A published release and its tag are things other people may already have
  consumed, and deleting either is a maintainer decision made deliberately and
  in the open, never a cleanup step. A defect found after publication ships as
  the next patch release. If a published release genuinely has to be withdrawn,
  that is a decision to take explicitly, not a command to run from a checklist.

## Branch Cleanup

Delete a squash-merged source branch only if it is an ancestor of `main` or
`git cherry main <branch>` prints no `+` lines:

```bash
git merge-base --is-ancestor <branch> main
git cherry main <branch>
```

If the ancestry check fails and `git cherry` prints `+` lines, leave the branch
in place. Report the branch name and tip SHA, and record the restore commands
so the branch can be recreated if it is ever dropped:

```bash
git branch <branch> <tip-sha>
git push origin <tip-sha>:refs/heads/<branch>
```

When merging a release documentation pull request, pass the expected head SHA
to the merge operation. If the wrong documentation change is merged, do not
rewrite `main`; open a revert pull request for the squash merge commit.

## Release Note Template

The workflow generates the notes: the `## <next-version>` section of
`CHANGELOG.md` first, then the six blocks below, with the pin values read from
`src/wuwaterm/constants.py`, the asset names read from the files it is about to
upload, and the image digests read from the build. This is the shape it
produces, and the shape to check before publishing.

The six `###` headings here and the six the workflow writes are pinned to each
other by `tests/test_release_workflow.py`, so this template cannot quietly stop
describing what is published. The prose under each heading is not pinned —
that would make every wording change a two-file edit for no gain — so read the
generated notes before publishing rather than assuming this page.

```markdown
## <next-version>

### Supported Game Data

- Source profile: arikatsu
- Source repository: https://github.com/Arikatsu/WutheringWaves_Data
- Pinned source commit: 9218d612ad815e398e064e577e42aaf878899968
- GameVer: 3.7.0
- ResVer: 3.7.8
- Changelist: 8975829

### Validation

- `python scripts/validate.py` (hygiene, architecture, API contract,
  ruff, test suite) on the CI matrix
- `python scripts/verify_db.py data/terms.candidate.db --profile arikatsu --require-free-text-exclusions --require-free-text-zh-exclusions`
- Packaging: build, `twine check --strict`,
  `scripts/check_package_artifacts.py`, and a clean-environment
  install/import/CLI smoke of the exact wheel and sdist attached here

### Privacy And LLM

Exact database hits do not call the LLM. Free-text sentence translation can call
an OpenAI-compatible endpoint only when the operator configures one. Do not
include tokens, API keys, chat IDs, owner IDs, `.env` files, runtime settings,
deployment logs, or host names, addresses and paths that identify a deployment
in release materials.

### Assets

This backend release attaches four files from the exact release commit:
a wheel, an sdist, `SHA256SUMS` covering those two files, and
`release-manifest.json` recording the source commit, version, image digests
and game-data pin. Verify downloads with `sha256sum -c SHA256SUMS`.
Windows packages are released separately.

Container images built from the same commit, with their digests:
`ghcr.io/my-denia/wuwaterm` (runtime, which serves the Telegram bot and the
HTTP API) and `ghcr.io/my-denia/wuwaterm-builder` (which fetches the pinned
upstream data and builds and verifies the terminology database). The runtime
image needs a locally built `terms.db` and the builder image is what builds it,
which is why both are published. They save the local image build and do not
replace the source checkout at this tag. Verify that the pull works for you
before relying on it; if it is denied, build from source.

### Distribution Boundary

This release distributes source code, a Python package and container images. It does not distribute generated SQLite
databases, generated TextMap files, or Wuthering Waves game data.

### Known Limitations

- WuwaTerm is an unofficial, independent fan project. It is not affiliated
  with, authorized by, or endorsed by Kuro Games.
- Release artifacts remain self-hosting inputs. The separate anonymous public
  beta is at https://wuwaterm.denia-official.chatgpt.site; it uses one shared,
  first-come pool and has no SLA or per-visitor fairness guarantee.
- Live Telegram operation requires maintainer-provided credentials and chat
  configuration.
- Free-text sentence translation requires an external OpenAI-compatible
  endpoint.
- The HTTP API serves operator-registered devices; there is no public
  registration.
```
