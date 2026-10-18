# ADR 0008: Candidate verification and transactional deployment

- Status: Accepted
- Date: 2026-08-05

## Context

Replacing a live glossary or runtime image in place risks serving a bad DB,
leaking partial upgrades, or losing the previous known-good pair of
`(source commit, image, terms.db)`. Manual `mv` of candidates over production
has caused operational risk historically and is explicitly disallowed in docs.

## Decision

Deploy through `deploy/vps-update.sh` (owner-gated) with a candidate pipeline:

1. Require clean Git checkout and `HEAD ==` freshly fetched `origin/main`.
2. Build/verify a unique candidate DB under `data/candidates/` (not live path).
3. Build an immutable `wuwaterm-runtime:<source-commit>` image; verify revision
   label.
4. Snapshot old DB and pointer; tag old image for rollback.
5. Stop runtime → promote DB → start exact image → smoke
   (`scripts/deploy_smoke.py`, diagnostic send disabled in updater path).
6. Write immutable `.deployments/<source-commit>.json` and atomic
   `.deploy_commit`.
7. On any post-promote failure, restore previous DB, image, and pointer.

Re-running the same source commit is accepted only when image/DB binding is
byte-identical; different rebuilds need a new source commit rather than
rewriting history manifests.

### Runtime-only extension

For a code-only update with an already compatible terminology database,
`deploy/vps-update.sh --runtime-only` preserves that exact database file. It
uses the shared deployment lock, read-only schema/provenance/integrity and
identity checks, and an immutable runtime image for **both** serving containers.
The existing single-image manifest continues to describe the complete runtime;
an API-only mixed revision is not introduced.

This path skips the builder, data refresh, candidate creation, DB promotion,
state migration and new DB backup. A durable transaction record binds the old
images/pointer and unchanged database to the target. New-deployment admission
requires current `origin/main`; recovery instead uses the trusted local record
and retained old images, so an interrupted update remains recoverable offline.
Unresolved recovery blocks normal deployment. Recovery never rewrites a changed
database or disguises an incomplete transition as success.

The default updater retains the full candidate pipeline for an intentional
data update. Runtime-only deployment adds an explicit operation with a stricter
database-preservation contract; it does not weaken full deployment verification.

### Full-data transaction record

A full update has the same interruption exposure across a wider window — the
database itself is promoted before the new services start — so it keeps its
own durable record at `.deployments/.full-transaction.json`, written before
the first serving-binding mutation and removed only through observed-binding
verification (`scripts/full_update.py`, driven by `deploy/vps-update.sh`; the
recovery entry is `--recover-full`). The record binds the old/new database
hashes, the hash-named backup, the old pointer and manifest, both surfaces'
observed image ids/refs/running states, the target image identity and a
fingerprint over the tool code. Recovery classifies the host from observed
pointer/database/manifest/containers — never the phase marker alone —
confirms a complete new binding or restores the recorded previous one, and on
any uncertainty keeps the record and changes nothing further. Any outstanding
record refuses every deployment mode until recovery resolves it. The shell's
EXIT rollback may clear the record only through the same observed old-binding
verification. See `docs/deployment.md` for the operator contract.

## Consequences

- Positive: provenance is auditable; rollback path is scripted.
- Positive: strong verifiers run before traffic sees the candidate.
- Negative: deploy is intentionally serial and owner-attended; not a CD
  free-for-all.
- Constraint: architecture docs describe this from repo scripts — production
  mutation remains an owner-authorized ops action, not an agent default.

## Evidence

- `deploy/vps-update.sh` candidate paths, `rollback_on_failure`, manifests
- `scripts/deployment_manifest.py`, `scripts/deploy_smoke.py`
- `scripts/full_update.py` (full-data transaction record and recovery)
- `docs/deployment.md`, `docs/validation.md`, `docs/release-checklist.md`
- `tests/test_deploy_scripts.py` (script contracts where covered)
- `tests/test_full_deploy_recovery.py` (interruption window, recovery
  reentry and the fail-closed binding matrix in isolated harnesses)
