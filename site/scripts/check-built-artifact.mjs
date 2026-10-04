import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export function requireBuiltArtifact(root = fileURLToPath(new URL('../', import.meta.url))) {
  const required = ['dist/server/wrangler.json', 'dist/server/index.js', 'dist/client/vinext-client-entry-manifest.json'];
  if (required.some(file => !existsSync(resolve(root, file)))) {
    throw new Error('Production build is required. Run npm run build, then npm run verify:no-client-secret before npm run test:built.');
  }
  return resolve(root, 'dist/server/wrangler.json');
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try { requireBuiltArtifact(process.argv[2]); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
