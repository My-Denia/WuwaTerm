# Release Checklist

Use this checklist to publish a GitHub release. From v0.4.0 onward the assets
are **built by a workflow, not by hand**: `.github/workflows/release.yml` builds
everything from one reviewed commit, and the human steps are deciding which
commit, reading the draft back, and publishing it.

Do not attach generated SQLite databases, TextMap files, or bulk game data to a
release. That boundary has not moved.

## Release Metadata

- Prospective release version: derive `v<project.version>` from
  `pyproject.toml` at the reviewed release commit; never copy the latest
  published tag. The current project version is `0.6.0`; `v0.5.0` is the
  previous published release. Set
  `NEXT_VERSION` to the derived tag explicitly before any release command
  below.
- Desktop client version: `0.2.0` (`client/pyproject.toml`), versioned
  independently of the server and carried in the release as
  `WuwaTerm-0.2.0-windows-x64.zip`
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
`SHA256SUMS`: no client binary and no container image. From v0.4.0 onward:

**Five assets, and no others:**

| asset | built by | audited by |
| --- | --- | --- |
| `wuwaterm-<version>-py3-none-any.whl` | `python -m build` on `ubuntu-latest` | `twine check --strict`, `scripts/check_package_artifacts.py`, clean-venv install + import + CLI smoke |
| `wuwaterm-<version>.tar.gz` | the same build | the same three |
| `WuwaTerm-<client version>-windows-x64.zip` | `client/build.ps1` on `windows-latest` | the client test suite, then the built executable's own `--self-check` start-up rehearsal, before the folder is packaged |
| `SHA256SUMS` | the workflow, over the three files above | verified by re-download while the release is still a draft |
| `release-manifest.json` | the workflow | read back against the reviewed commit while the release is still a draft |

`release-manifest.json` records the tag, the server version, the client
version, the source commit, the build time, whether the run was a dry run, the
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

**The Windows client executable is UNSIGNED.** There is no code-signing
certificate, no installer, and no plan for either this round. Windows
SmartScreen will warn on first run and the user has to choose More info and
then Run anyway. The release notes must say this in the Assets section — a user
who meets the warning without having been told has been handed a reason to
distrust the download. Two builds of the client are not expected to be
byte-identical either, and nothing checks that they are; there is no lock file
for the client and the runner image floats.

## Validation

Run the repository's gates from a clean checkout of the candidate commit:

```bash
python scripts/validate.py
```

That is hygiene, non-goals, architecture boundaries, the API contract, ruff and
the test suite, in that order, and it is the same entry point CI's server
matrix runs. It is not the whole pull request: the lock-drift check, the
packaging audit, the Windows client build and the Docker boundary job are
separate CI jobs. Confirm on the pull request, not only locally.

For a locally built release candidate database, also run:

```bash
python scripts/verify_db.py data/terms.candidate.db --profile arikatsu --require-free-text-exclusions
python scripts/verify_seed_terms.py data/terms.candidate.db
python scripts/verify_exact_hits.py data/terms.candidate.db --sample-size 500
python scripts/verify_idempotent_build.py --data-dir data/wutheringdata --out-dir goal-runs/wuwaterm-v2-translator --profile arikatsu
```

The packaging gates (`python -m build`, `twine check --strict`,
`scripts/check_package_artifacts.py`) still exist and still have to pass, but
the release workflow runs them itself on the exact assets it uploads, which is
the point: CI's copies and the release's copies are no longer separate builds.

Release only after GitHub CI and the configured review workflows are green for
the exact commit you are about to release.

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
2. **Dry run first.** Every pull request touching the workflow, `deploy/**`,
   `client/**` or `pyproject.toml` already runs the workflow in dry-run mode,
   and a dry run can also be dispatched deliberately. A dry run builds every
   asset, builds both images, and publishes nothing — no registry login, no
   image push, no release. Read its `release-assets` artifact before doing the
   real run.
3. **The real run** creates a **draft**. It is the only mode that logs in to
   the registry, pushes images, or touches releases, and the two jobs that can
   do so are unreachable from any other trigger. The push runs only after the
   wheel, the sdist, the client build and both image builds have already
   succeeded, so a release tag is never pushed for a commit whose package or
   client had failed:

   ```bash
   NEXT_VERSION=vX.Y.Z
   reviewed_main_commit=<reviewed-main-commit-sha>
   gh workflow run release.yml --ref main \
     -f target_commit="$reviewed_main_commit" -f dry_run=false
   gh run watch "$(gh run list --workflow=release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
   ```

   The workflow refuses to proceed when `refs/tags/$NEXT_VERSION` already
   exists, when the declared version is malformed, or when the target is not an
   ancestor of `origin/main`.
4. **Read the draft back** (next section). Everything that can be checked is
   checked while the release is still a draft, because a draft can be deleted
   and a published release cannot.
5. **Publish** by hand. The workflow never publishes.
6. **Read the published release back** and only then report it as released.

A draft has no tag. GitHub creates `refs/tags/<tag>` at the moment of
publication, not at draft creation, which is why deleting a draft leaves
nothing behind and why the tag-absence check is meaningful right up to the
publish command. The only registry tags a discarded draft can leave are
`sha-<7>` (unconfusable with `vX.Y.Z` / `X.Y`). If `promote-images` fails
after publication, re-run that job on the same workflow run; do not create
another draft.

## Draft Readback

Run all of it. A single failure stops the publication.

```bash
NEXT_VERSION=vX.Y.Z
reviewed_main_commit=<reviewed-main-commit-sha>
set -euo pipefail

# 1. Exactly the five authorized assets, and no more.
test "$(gh release view "$NEXT_VERSION" --json assets --jq '.assets | length')" -eq 5
gh release view "$NEXT_VERSION" --json assets --jq '.assets[].name' | sort

# 2. The bytes on the release page are the bytes the checksums describe.
verify_dir="$(mktemp -d)"
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

**Then probe GHCR**, without Docker. A first-published user package on this
registry defaults to private, so an anonymous denial is an expected outcome
and an owner visibility step, not a defect in the release. At draft time the
probe must still distinguish “package private” from “semantic tag already
exists”:

```bash
NEXT_VERSION=vX.Y.Z
reviewed_main_commit=<reviewed-main-commit-sha>
short="$(printf '%s' "$reviewed_main_commit" | cut -c1-7)"
major_minor="${NEXT_VERSION#v}"; major_minor="${major_minor%.*}"

probe() {
  repository="$1"
  ref="$2"
  token="$(curl -sS "https://ghcr.io/token?scope=repository:$repository:pull" | jq -r .token)"
  curl -sS -o /tmp/ghcr-manifest.json -w '%{http_code}' \
    -H "Authorization: Bearer $token" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
    "https://ghcr.io/v2/$repository/manifests/$ref"
}

for repository in my-denia/wuwaterm my-denia/wuwaterm-builder; do
  sha_code="$(probe "$repository" "sha-$short")"
  semver_code="$(probe "$repository" "$NEXT_VERSION")"
  mm_code="$(probe "$repository" "$major_minor")"
  printf '%s sha-%s=%s %s=%s %s=%s\n' \
    "$repository" "$short" "$sha_code" "$NEXT_VERSION" "$semver_code" "$major_minor" "$mm_code"
  # Draft: sha tag must exist (200, or 401/403 if the package is private).
  # Semantic tags must not exist yet: 404. A 200 here is a stop — the draft
  # left a release-looking tag. 401/403 on the semantic ref is ambiguous
  # (private package); confirm with an authenticated `gh api` package-version
  # listing that `vX.Y.Z` / `X.Y` were not created.
  test "$semver_code" != "200"
  test "$mm_code" != "200"
done
```

After publication, repeat the same probe: `vX.Y.Z` and `X.Y` must resolve to
the digests in the downloaded `release-manifest.json`. Authenticated
`docker buildx imagetools inspect` (or `gh api` package versions) is the
digest compare when anonymous pull is denied. `v0.4.0` / `0.4` must still
resolve to the historical digests in that older release's manifest.

## Publish

**Repeat the tag check here, in the same block as the publish command.** The
one in the draft readback above finished before the registry probes, and those
take minutes. GitHub ignores `target_commitish` once the tag exists, so a tag
created by anyone in that gap would silently retarget the release at publish
time — and the check is only worth anything if nothing can happen between it
and the command it guards:

```bash
NEXT_VERSION=vX.Y.Z
set -euo pipefail

tag_lookup_status=0
git ls-remote --exit-code --tags origin "refs/tags/$NEXT_VERSION" >/dev/null || tag_lookup_status=$?
test "$tag_lookup_status" -eq 2  # absent, and a lookup ERROR also stops here

gh release edit "$NEXT_VERSION" --draft=false
```

That command is the publication gate. Nothing before it is public, and nothing
after it can be taken back quietly.

## Post-Publish Readback

```bash
NEXT_VERSION=vX.Y.Z
reviewed_main_commit=<reviewed-main-commit-sha>
gh release view "$NEXT_VERSION" --json tagName,targetCommitish,isDraft,url,assets
tag_ref="$(git ls-remote --exit-code --tags origin "refs/tags/$NEXT_VERSION" "refs/tags/$NEXT_VERSION^{}")" || {
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

Then re-download the published assets and check them against `SHA256SUMS`
again, exactly as in step 2 of the draft readback, before reporting the release
as published. Verifying the draft is not verifying the release.

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

- `python scripts/validate.py` (hygiene, non-goals, architecture, API contract,
  ruff, test suite) on the CI matrix
- `python scripts/verify_db.py data/terms.candidate.db --profile arikatsu --require-free-text-exclusions`
- Packaging: build, `twine check --strict`,
  `scripts/check_package_artifacts.py`, and a clean-environment
  install/import/CLI smoke of the exact wheel and sdist attached here
- Desktop client: its own suite, the one-folder build, and the built
  executable's `--self-check` start-up rehearsal

### Privacy And LLM

Exact database hits do not call the LLM. Free-text sentence translation can call
an OpenAI-compatible endpoint only when the operator configures one. Do not
include tokens, API keys, chat IDs, owner IDs, `.env` files, runtime settings,
deployment logs, or host names, addresses and paths that identify a deployment
in release materials.

### Assets

This release attaches five files, all built from the exact release commit: one
wheel, one sdist, the Windows desktop client as
`WuwaTerm-<client version>-windows-x64.zip`, a `SHA256SUMS` covering those
three, and `release-manifest.json` recording the source commit, the versions,
the image digests and the game-data pin. Verify downloads with
`sha256sum -c SHA256SUMS`.

The Windows desktop client in the zip is **UNSIGNED**: no code signing is
performed on it at any point. SmartScreen will warn about an unrecognised
publisher; continue through More info and then Run anyway only after checking
the download against `SHA256SUMS`.

Container images built from the same commit, with their digests:
`ghcr.io/my-denia/wuwaterm` (runtime, which serves the Telegram bot and the
HTTP API) and `ghcr.io/my-denia/wuwaterm-builder` (which fetches the pinned
upstream data and builds and verifies the terminology database). The runtime
image needs a locally built `terms.db` and the builder image is what builds it,
which is why both are published. They save the local image build and do not
replace the source checkout at this tag. Verify that the pull works for you
before relying on it; if it is denied, build from source.

### Distribution Boundary

This release distributes source code, a Python package, an unsigned Windows
client build, and container images. It does not distribute generated SQLite
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
- The Windows client is unsigned and there is no installer.
```
