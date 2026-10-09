# Data Refresh

## Data Source

Primary source:

- `https://github.com/Arikatsu/WutheringWaves_Data`
- pinned commit: `9218d612ad815e398e064e577e42aaf878899968`
- pinned version: `GameVer 3.7.0 | ResVer 3.7.8 | Changelist 8975829`

Both source profiles, with their pinned commits, are defined in
`src/wuwaterm/constants.py`.

Fallback mirror to try manually if the primary source is unavailable:

- `https://github.com/Dimbreath/WutheringData` is frozen at 3.1.0
  (`e9234ffe094b2d944d16b222d31102e8ab32d954`, 2026-03-13) and is kept only
  as a legacy fallback. It has no machine-readable version file, so metadata
  records those version fields as `unavailable`; its remote, exact HEAD, and
  clean Git checkout are still measured and required. Arbitrary copied
  `ConfigDB` directories are rejected rather than stamped from constants.

The active Arikatsu source profile uses sparse checkout for only:

- `BinData`
- `README.md` (required checkout-version provenance)
- `Textmaps`

Bulk TextMap data and generated `terms.db` are local artifacts and are ignored
by Git. This project does **not** redistribute Wuthering Waves game data; only a
small derived term dictionary is built locally from the public source above. All
Wuthering Waves game data and in-game terminology are © Kuro Games. The
project's MIT license covers its source code only, not the upstream game data or
in-game terminology.

## Local Development

WSL/Linux is the primary development environment. Keep the checkout on the WSL
filesystem (for example under `~/projects/...`) so file watching,
permissions, line endings, and virtualenv scripts behave like Linux.

```bash
test -x .venv/bin/python || uv venv .venv
uv sync --locked --extra dev
```

Use `uv venv --clear .venv` only when intentionally rebuilding the local
virtualenv from scratch.

If the WSL image has `python3-venv` and pip installed, the standard-library
path also works:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e ".[dev]"
```

Docker data refresh uses the build image, not the production runtime image:

```bash
docker compose -f deploy/docker-compose.yml run --rm wuwaterm-builder refresh-data
docker compose -f deploy/docker-compose.yml run --rm -e WUWATERM_DB_PATH=/app/data/terms.candidate.db wuwaterm-builder build-db --atomic
docker compose -f deploy/docker-compose.yml run --rm -e WUWATERM_DB_PATH=/app/data/terms.candidate.db wuwaterm-builder verify-db
```

## Build Dictionary

```bash
.venv/bin/python -m wuwaterm.cli refresh-data --dest data/wutheringdata --profile arikatsu
.venv/bin/python -m wuwaterm.cli build-db --data-dir data/wutheringdata --db data/terms.candidate.db --profile arikatsu --atomic
.venv/bin/python scripts/verify_db.py data/terms.candidate.db --profile arikatsu --require-free-text-exclusions --require-free-text-zh-exclusions
```

The refresh and build fail closed unless the checkout's `origin`, full HEAD,
clean tracked state, and all three version fields in the root README match the
active source profile. The builder writes those observed values into DB
metadata. The verifier opens the candidate read-only and checks integrity,
exact tables/columns/indexes, schema and source metadata, every required
category, and the representative exact pair `景燃 -> Jingran` in both
directions. It must pass before any production promotion.

`build-db` also reads every official English string that has a Chinese
counterpart and records, in the `free_text_lock_exclusions` metadata key, the
English dictionary surfaces that free-text sentence translation must not lock:
a surface locked at least 10 times whose parallel Chinese string carries its
official Chinese name in fewer than one in five of those lines (for example a
speaker label that is also the ordinary word `It`). It likewise reads every
official Chinese string that has an English counterpart and records, in the
`free_text_lock_exclusions_zh` metadata key, the Chinese surfaces measured the
same way — a line counts as aligned when the parallel English, casefolded and
with surrounding quote characters stripped, carries the surface's official
English (for example the speaker label that is also the ordinary word `大人`).
Both values are sorted compact JSON, so the idempotent-build check still
holds. The verifier rejects a malformed key, a key built with other
parameters, or a surface that is not a term, for each key separately;
`--require-free-text-exclusions` and `--require-free-text-zh-exclusions`
(both always passed by the builder image's `verify-db`) each reject a
candidate without the respective key, so the image's default
`verify-db` against a live database built before a key existed fails until
the next full build replaces it. A database built before
these keys existed, or written by `create_database` alone, keeps every surface
lockable. A promoted database is what changes runtime behaviour: the code
alone does not. Generated candidates
remain ignored and are not distributed.

## Lookup

```bash
.venv/bin/python -m wuwaterm.cli lookup --db data/terms.db 声骸
.venv/bin/python -m wuwaterm.cli sentence --db data/terms.db "今汐装备了声骸"
```

## Refresh Checks

`景燃 -> Jingran` remains the required representative exact check. It was
re-measured on the built 3.7 candidate and is still single-valued in both
directions. The 3.5 pair `穗穗 -> Suisui` stays retired: `穗穗（通讯中） -> Suisui`
still makes the reverse direction multi-valued. A pair that is new at 3.7 and
single-valued both ways on that same candidate is `棠宁 -> Tangning`; it is the
measured new-term sample, not a second required check. A representative pair
must stay single-valued in both directions in the built database; pick or keep
it by measuring the candidate, not by reading upstream release notes.
Offline candidate verification proves the source and DB content; it does not
prove that a VPS is running that DB. After an owner-authorized deployment, the
immutable deployment manifest records the DB hash and provenance. A live
`/tr` check is separate real-Telegram evidence and is never implied by offline
or deployment validation. Use [Validation](validation.md) for the full offline
validation command set.
