# Component runtime transactions

This interface is under development. Inspection is available. Adoption and
runtime and full updates refuse until their saved-source recovery has passed
its execution gates. Both image builders and their cancellation commands refuse
until their interrupted-build cleanup is implemented and verified. Do not
create journals, receipts or deployment pointers by hand.

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

When the execution gates are enabled, the normal operations are:

```sh
deploy/product-update.sh --root "$PWD" build api
deploy/product-update.sh --root "$PWD" build telegram \
  --telegram-repo /path/to/telegram --telegram-commit <full-commit> \
  --core-wheel /path/to/core.whl --core-artifact /path/to/core-artifact \
  --dependencies /path/to/dependency-wheelhouse \
  --build-tool-wheelhouse /path/to/build-tool-wheelhouse

deploy/product-update.sh --root "$PWD" adopt --project <compose-project> \
  --compose-executable /path/to/docker-compose \
  --api-receipt <source-receipt-sha256> --telegram-receipt <wheel-receipt-sha256>

deploy/product-update.sh --root "$PWD" runtime-update --components api \
  --api-receipt <source-receipt-sha256> --compose-executable /path/to/docker-compose
deploy/product-update.sh --root "$PWD" runtime-update --components telegram \
  --telegram-receipt <wheel-receipt-sha256> --compose-executable /path/to/docker-compose
deploy/product-update.sh --root "$PWD" runtime-update --components both \
  --api-receipt <source-receipt-sha256> --telegram-receipt <wheel-receipt-sha256> \
  --compose-executable /path/to/docker-compose

deploy/product-update.sh --root "$PWD" full-update --candidate-db /path/to/candidate.db \
  --components none --compose-executable /path/to/docker-compose

deploy/product-update.sh --root "$PWD" recover --transaction-id <transaction-id> \
  --compose-executable /path/to/docker-compose
deploy/product-update.sh --root "$PWD" recover --kind full --transaction-id <transaction-id> \
  --compose-executable /path/to/docker-compose
deploy/product-update.sh --root "$PWD" recover-probe --probe-id <probe-id> \
  --compose-executable /path/to/docker-compose
deploy/product-update.sh --root "$PWD" cancel-build --kind source --build-id <build-id>
deploy/product-update.sh --root "$PWD" cancel-build --kind wheel --build-id <build-id>
```

The optional `--expected-sha <full-commit>` rejects an unexpected operations
commit. The CLI derives the operations tree itself. Telegram builds also
accept `--core-source-repo` and `--core-checksums` when needed by the selected
Core artifact. Receipts belong to the deployment root; image labels and
caller-supplied JSON are not receipts. Supply exactly the receipts for the
selected installed components.

Adoption observes the installed Compose services and records their effective
configuration without starting or replacing them. It may run an isolated,
network-disabled legacy image probe to identify the installed Core version.
The result reports polling health as unconfirmed. Runtime updates preserve the
database and current component running states. API updates only substitute the
image; they do not migrate API configuration. Full database promotion and
state-file relocation require a separate full update. A full update validates
the candidate database with the existing complete database checks before it
stops readers or promotes the file. `--components none` keeps the installed
images; `api`, `telegram`, or `both` also require the corresponding receipts.
API recipe changes beyond the image are not supported by either update.
Schema 4 metadata adoption can record a Telegram state path whose final entry
is an existing bounded, singly linked symlink, provided its parent and writable
directory bind pass entry validation. Adoption does not resolve or change the
link. A full update must independently validate the target and writer before
changing state; ordinary runtime updates still refuse these links.
To build a candidate from existing local input without refreshing upstream data:

```sh
python -m wuwaterm.cli build-db --data-dir /path/to/existing-input \
  --db /path/to/unique-candidate.db --atomic
```

Pass that candidate to `full-update`; preparation copies and verifies it before
the running database is changed.

An independent Telegram image replacing a legacy combined service needs an
explicit target Compose recipe. For example, after replacing the placeholder
host paths with the existing database, state directory and env file, use a file
such as:

```yaml
services:
  telegram:
    image: placeholder
    container_name: wuwaterm-telegram
    network_mode: host
    restart: unless-stopped
    entrypoint: [wuwaterm-telegram]
    command: [--db, /app/data/terms.db]
    env_file: [/existing/absolute/telegram.env]
    environment:
      WUWATERM_DB_PATH: /app/data/terms.db
      WUWATERM_STATE_DIR: /app/state
      WUWATERM_POLL_HEALTH: "1"
    volumes:
      - /existing/absolute/data:/app/data:ro
      - /existing/absolute/state:/app/state:rw
    tmpfs:
      - /run/wuwaterm-telegram:rw,noexec,nosuid,nodev,mode=0700
```

Pass this file with `--telegram-compose-file /absolute/path/telegram.yml`,
`--telegram-project-directory /absolute/path`, `--telegram-project <project>`
and `--telegram-service telegram`. Add `--telegram-interpolation-env-file`
for each required Compose interpolation file. The receipt supplies the actual
target image. Runtime updates require the same effective database and state
bindings as the adopted service. A full update can perform an admitted state
relocation when the target image's native migration plan identifies the old
writer as its source. The renderer refuses unsupported settings and environment
values that cannot be reproduced from retained file references.

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
