# Temporary, repository-owned braces security patch

This is **not an upstream release**. `@wuwaterm/braces-patched` is a private,
source-vendored fork of `braces@3.0.3`, installed under the `braces` import name
with npm's direct dependency override. Both locked `fast-glob` versions still
use their original `micromatch` implementation and resolve this patched copy.
No third-party fork, postinstall patch, downloaded patch or audit exception is
used. The full and production-only audit commands in CI are unchanged.

## Provenance and maintenance responsibility

- Official source: <https://github.com/micromatch/braces/tree/3.0.3>
- Upstream commit: `74b2db2938fad48a2ea54a9c8bf27a37a62c350d`
- Registry source: <https://registry.npmjs.org/braces/-/braces-3.0.3.tgz>
- Original tarball integrity:
  `sha512-yQbXgO/OSZVD2IsiLlro+7Hf6Q18EJrKSEsdoMzKePKXct3gvD8oLcOQdIzGupr5Fj+EDe8gO/lxc1BzfMpxvA==`
- `index.js`, `lib/*.js` and `LICENSE` were copied from that registry package.
  Original author/contributor attribution and MIT license are retained.
- The original manifest's development tools were not copied into this package.
  Its one runtime dependency, `fill-range`, is pinned to the already locked
  `7.1.1`. Its own private name/version identify WuwaTerm's maintenance, not an
  upstream fixed version.

The repository maintainer owns this patch until it is removed. npm audit knows
the registry dependency graph but does **not** establish that this private fork
is safe: changing package identity alone is not remediation. The source changes
and behavioral security tests below are the evidence for this specific fix.
Continue checking upstream `braces` advisories while this fork is present.

## Security boundary and exact source changes

[GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)
affects unbounded recursive AST traversal. The original 10,000-character limit
does not prevent a deeply nested pattern from exhausting the JavaScript stack.

The patch adds `lib/depth.js` and changes only these upstream source files:

- `lib/parse.js`: check combined brace/parenthesis nesting before either push
- `lib/compile.js`: bound recursive node traversal, including direct AST input
- `lib/expand.js`: bound recursive node/array traversal and both parent-chain loops
- `lib/stringify.js`: bound recursive node traversal, also when imported directly
- `lib/utils.js`: bound recursive array flattening, including arrays supplied
  through non-parser-produced AST values

All three walkers first validate direct AST children iteratively. As in
parser-produced ASTs, nodes must be objects, children must be arrays and scalar
`value` fields must be strings when present. Malformed values throw `TypeError`
before array-to-string coercion can recurse. Existing parser-produced ASTs are
unchanged; undocumented non-string scalar values are deliberately rejected.

The root has depth zero. Up to 100 nested containers remain valid. Deeper
patterns/ASTs throw an explicit `SyntaxError` containing `maximum depth`, before
native stack exhaustion. `maxDepth` can reduce the ceiling; finite nonnegative
numbers are floored and capped at 100. Invalid values use the hard ceiling.
Quotes, escaped delimiters and bracket literals do not count as nesting.
Direct cyclic child paths, expansion parent chains and nested/cyclic arrays in
caller-provided AST values are also bounded.

This is a nesting fix, not a claim to bound every possible brace expansion,
caller-provided JavaScript object/getter or deliberately disabled range limit.
The original range-limit and ordinary matching semantics remain unchanged.

## Validation and removal

`npm test` includes `tests/braces-security.test.mjs`: resource/time-isolated
deep string/AST/cycle regressions, internal entry points, boundary and option
bypass cases, both actual dependency paths, and normal behavior compared with
captured official 3.0.3 outputs in `tests/fixtures/braces-3.0.3-semantics.json`.
The same suite can target untouched official source with
`WUWATERM_BRACES_UNDER_TEST=/absolute/path/to/braces/index.js`; security tests
must fail there while normal controls pass. Run the complete Site CI checks,
not only audit, after changing this package.

When an official fixed `braces` release becomes available, inspect its patch
and advisory status, update the normal dependency resolution, remove the
direct file dependency/override, this vendor directory and its ESLint CommonJS
style exception, then repeat the security/compatibility tests and complete Site
CI. Review the regression tests' private-package identity assertion at that
time; retain the substantive security regression coverage. Do not switch back
solely because an advisory entry changes.
