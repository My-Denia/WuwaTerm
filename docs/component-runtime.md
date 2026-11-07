# Component runtime transactions

This interface supports inspection, adoption, Protocol-2 completed-artifact
updates, and saved-source recovery. Protocol-2 recovery and updates
(`ARTIFACT_RECOVERY_EXECUTION_READY = True`,
`ARTIFACT_FULL_RECOVERY_EXECUTION_READY = True`) are enabled for completed
delivery bundles. Legacy Protocol-1 recovery (`RECOVERY_EXECUTION_READY = False`,
`FULL_RECOVERY_EXECUTION_READY = False`), in-deployment `build`, and legacy
builder cancellation (`cancel-build`) remain closed (`deploy/product-update.sh`
unconditionally rejects `build` as not a deployment command and directs
operators to `scripts/build-delivery.sh` and `scripts/component_artifact.py`).
Interrupted Protocol-2 artifact acquisitions are cancelled or recovered via
`python scripts/component_artifact.py cancel|recover --root "$PWD" --id <attempt-id>`.
Do not create journals, receipts, bundles or deployment pointers by hand.

The wrapper acquires the deployment lock and preserves a valid inherited FD9.
Use the physical, absolute checkout path for `--root`. Direct Python mutators
still require the same admitted source, root and lock. `inspect` takes no lock
and creates no files:

```sh
deploy/product-update.sh --root /path/to/checkout inspect
```

Recorded metadata does not prove that a service is running or healthy.
Inspection reports `live_binding: unverified`. To compare one container with a
recorded component, add `--component api --container-id <full-container-id>`.
This observation does not establish env-file provenance or service health.
If a runtime journal is present, inspection reports the saved launcher's argv
as recorded, unverified metadata. That launcher acquires or reuses FD9 itself.

## Completed-artifact deployment workflow

The executable deployment workflow operates on sealed delivery bundles built
outside the deployment lock:

### 1. Adopt existing services

Adoption observes the installed Compose services and records their effective
configuration as Schema 4 metadata without starting, stopping or replacing them,
and initializes `.deployments/` with mode `0700` under the deployment lock:

```sh
deploy/product-update.sh --root "$PWD" adopt --project <compose-project> \
  --compose-executable /path/to/docker-compose
```

The optional `--expected-sha <full-commit>` rejects an unexpected operations
commit. Adoption may run an isolated, network-disabled legacy image probe to
identify the installed Core version. The result reports polling health as
unconfirmed.

### 2. Build a sealed delivery bundle

Delivery bundles are built outside the deployment lock without holding or
inheriting FD9:

```sh
# Build a sealed Core/API delivery bundle
scripts/build-delivery.sh --product api --source /path/to/wuwaterm \
  --out /path/to/delivery-dir [--commit <rev>] [--tag <name>]

# Or build a Telegram delivery bundle in WuwaTerm-Telegram
python scripts/build_delivery.py build ...
```

### 3. Acquire and verify the bundle into the adopted deployment root

Acquisition verifies the bundle, loads the image into Docker, and registers an
immutable acquisition receipt under
`.deployments/artifacts/acquisitions/<acquisition-id>.json`. This tool takes its
own artifact lock, requires `.deployments/` to already exist (initialized by
`deploy/product-update.sh` / `adopt`), and refuses an inherited FD9:

```sh
# Acquire and verify an API bundle into the adopted deployment root
python scripts/component_artifact.py acquire --bundle /path/to/bundle.json --root "$PWD"

# Acquire a Telegram bundle with required dependency wheels
python scripts/component_artifact.py acquire --bundle /path/to/bundle.json --root "$PWD" \
  --wheel-dir /path/to/wheels

# List acquired attempts or manage interrupted acquisitions
python scripts/component_artifact.py list --root "$PWD"
python scripts/component_artifact.py cancel --root "$PWD" --id <attempt-id>
python scripts/component_artifact.py recover --root "$PWD" --id <attempt-id>
```

### 4. Run runtime updates or full updates

Pass the path to a registered acquisition receipt under
`.deployments/artifacts/acquisitions/<acquisition-id>.json` to `--api-artifact`
and `--telegram-artifact` for each selected component (a bare acquisition ID is
rejected by `artifact_input`):

```sh
# Update the API component image
deploy/product-update.sh --root "$PWD" runtime-update --components api \
  --api-artifact .deployments/artifacts/acquisitions/<acquisition-id>.json \
  --compose-executable /path/to/docker-compose

# Update the Telegram component image
deploy/product-update.sh --root "$PWD" runtime-update --components telegram \
  --telegram-artifact .deployments/artifacts/acquisitions/<acquisition-id>.json \
  --compose-executable /path/to/docker-compose

# Update both components (update is an alias for runtime-update)
deploy/product-update.sh --root "$PWD" update --components both \
  --api-artifact .deployments/artifacts/acquisitions/<api-acquisition-id>.json \
  --telegram-artifact .deployments/artifacts/acquisitions/<telegram-acquisition-id>.json \
  --compose-executable /path/to/docker-compose

# Full database update without changing component images
deploy/product-update.sh --root "$PWD" full-update --candidate-db /path/to/candidate.db \
  --components none --compose-executable /path/to/docker-compose

# Full database update with component image updates
deploy/product-update.sh --root "$PWD" full-update --candidate-db /path/to/candidate.db \
  --components api \
  --api-artifact .deployments/artifacts/acquisitions/<api-acquisition-id>.json \
  --compose-executable /path/to/docker-compose
```

### 5. Recovery

If a transaction or probe is interrupted, run the matching recovery command:

```sh
deploy/product-update.sh --root "$PWD" recover --transaction-id <transaction-id> \
  --compose-executable /path/to/docker-compose
deploy/product-update.sh --root "$PWD" recover --kind full --transaction-id <transaction-id> \
  --compose-executable /path/to/docker-compose
deploy/product-update.sh --root "$PWD" recover-probe --probe-id <probe-id> \
  --compose-executable /path/to/docker-compose
```

## Preserved-state cutover and metadata schemas

Runtime updates preserve the database and current component running states. API
updates only substitute the image; they do not migrate API configuration. Schema
5 translates the retained recipe in place and preserves existing state bindings
and leaf symlinks without cross-path state relocation, while Schema 4 / Protocol
1 records remain readable for inspection.

Schema 4 metadata adoption records installed Compose service configurations and
admits a Telegram state path whose final entry is an existing bounded, singly
linked symlink, provided its parent and writable directory bind pass entry
validation. Adoption does not resolve or change the link.

Protocol-2 Schema 5 updates translate the retained recipe in place. They admit
native leaf forms directly through the image without staging copy-back or
cross-path state relocations: a settings entry may be a link to the file the
product rewrites, and a reply entry may be the link the product replaces in
place. A full update validates the candidate database with the existing
complete database checks before it stops readers or promotes the file.
`--components none` keeps the installed images; `api`, `telegram`, or `both`
require the corresponding acquisition receipts. API recipe changes beyond the
image are not supported by either update.

To build a candidate from existing local input without refreshing upstream data:

```sh
python -m wuwaterm.cli build-db --data-dir /path/to/existing-input \
  --db /path/to/unique-candidate.db --atomic
```

Pass that candidate to `full-update`; preparation copies and verifies it before
the running database is changed.

When an independent `wuwaterm-telegram` artifact replaces an adopted legacy
combined service (`/entrypoint.sh` + `bot`), `translate_preserved_recipe`
automatically translates the retained recipe in place (`entrypoint: ["wuwaterm-telegram"]`,
`command: ["--db", <effective-db-path>]`, `WUWATERM_POLL_HEALTH=1`, and a
`tmpfs` mount at `/run/wuwaterm-telegram` with
`rw,noexec,nosuid,nodev,size=1048576,mode=0700,uid=<uid>,gid=<gid>`) while
keeping the existing working directory, numeric `uid:gid`, mounts, env files,
and environment overrides unchanged.

No `--telegram-compose-file` flag is needed for cutover or subsequent updates;
if a caller passes `--telegram-compose-file`, `refuse_target_recipe_change`
rejects any recipe that differs from the translated retained recipe. The
acquired artifact receipt supplies the actual target image and verified
metadata. Runtime updates require the same effective database and state
bindings as the adopted service. The renderer refuses unsupported settings and
environment values that cannot be reproduced from retained file references.

Preparation preserves the full clean source, a separate bootstrap and the
verified Compose executable under the deployment root before a journal is
written. Recovery verifies the recorded source, bootstrap and retained Compose
identity and executes that saved source against the deployment root; a newer
checkout cannot substitute its own Compose executable. A mismatched requested
Compose file is refused. An interrupted probe or transaction remains recorded
until its matching recovery completes. Legacy journals retain their original
format and are not rewritten to impersonate a newer source.

New immutable metadata publication requires a Linux filesystem that supports
unnamed temporary files and a working `/proc/self/fd`; an unsupported filesystem
is refused before the record is published. Interrupted preparation can leave
source-identity or transaction-named staging artifacts for a separate owner
inventory; unknown files are never removed automatically.
