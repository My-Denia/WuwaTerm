# minimatch CommonJS compatibility

This adapter keeps `require('minimatch')(path, pattern)` available to the older
ESLint plugins in this project. It delegates every matching operation to the
unmodified npm `minimatch` 11.0.3 package, installed under `minimatch-modern`.
Named exports remain available to consumers of the modern API.

The patched implementation addresses
[GHSA-g3hv-75wg-m249](https://github.com/advisories/GHSA-g3hv-75wg-m249).
A plain override to v11 passes the current lint command but breaks callable
plugin paths (for example React's `jsx-pascal-case` ignore patterns). The
consumer-resolution regression test covers this compatibility requirement.
Remove the adapter when all consumers use the modern API.

Other audit overrides: `@vercel/og` 1.0.3 brings Satori 0.33.5 for
[GHSA-wx4j-mvgx-mqwp](https://github.com/advisories/GHSA-wx4j-mvgx-mqwp).
Its Satori dependency pins fflate 0.7.3, so fflate remains on the compatible
0.7.5 patch to avoid reintroducing
[GHSA-px8p-9vwx-vf98](https://github.com/advisories/GHSA-px8p-9vwx-vf98).

`site/.npmrc` packs local file dependencies during installation. This avoids
npm creating broken nested links relative to consumer package directories.
The lockfile is verified with the project-pinned npm 11.6.2 and `npm ci`.
