import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { extname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const envNames = [
  'WUWATERM_API_BASE_URL',
  'WUWATERM_API_ALLOWED_HOST',
  'WUWATERM_SITE_DEVICE_TOKEN',
];
const canaries = [
  'SYNTHETIC_DEVICE_SENTINEL_74A1C9',
  'SYNTHETIC_PRODUCT_TOKEN_61E8',
  'not-a-real-service.invalid',
  'api.wuwaterm-test.net',
  '/wuwaterm-api/',
  '/private',
];
const sourceExtensions = new Set(['.css', '.ts', '.tsx']);
const clientSources = [...walk(join(root, 'app'))]
  .filter((path) => sourceExtensions.has(extname(path)))
  .filter((path) => !relative(root, path).split(sep).includes('api'))
  .map((path) => relative(root, path));
const forbiddenClientPatterns = [
  /localStorage/u,
  /sessionStorage/u,
  /indexedDB/u,
  /document\.cookie/u,
  /\bcaches\s*\./u,
  /\bgtag\s*\(/u,
  /\banalytics\b/iu,
  /\bXMLHttpRequest\b/u,
  /\bWebSocket\b/u,
  /\bEventSource\b/u,
  /\bnavigator\.sendBeacon\b/u,
];

// The interface-language preference is the single sanctioned browser-storage
// write. The exemption is content-pinned: the allowlisted file must contain
// exactly one document.cookie occurrence and it must be this exact assignment
// of the fixed wuwaterm-lang cookie, so "still strict" is a property this
// script itself enforces rather than a review judgment.
const languageCookieFile = 'app/ui-language-cookie.ts';
const languageCookieDocumentCookieCount = 1;
const pinnedLanguageCookieWrite = /document\.cookie\s*=\s*`wuwaterm-lang=\$\{value\}; Max-Age=31536000; Path=\/; SameSite=Lax\$\{secure\}`;/u;

for (const source of clientSources) {
  const text = readFileSync(join(root, source), 'utf8');
  for (const name of envNames) assert.equal(text.includes(name), false, `${source} references ${name}`);
  for (const pattern of forbiddenClientPatterns) {
    if (source === languageCookieFile && String(pattern) === String(/document\.cookie/u)) continue;
    assert.equal(pattern.test(text), false, `${source} matches ${pattern}`);
  }
  assert.equal(/https?:\/\//u.test(text), false, `${source} contains an absolute network target`);
  if (source === languageCookieFile) {
    assert.equal((text.match(/document\.cookie/gu) ?? []).length, languageCookieDocumentCookieCount,
      `${source} must reference document.cookie exactly ${languageCookieDocumentCookieCount} time(s)`);
    assert.equal(pinnedLanguageCookieWrite.test(text), true,
      `${source} must only write the pinned wuwaterm-lang preference cookie`);
  }
}

const clientComponent = readFileSync(join(root, 'app/components/translation-workbench.tsx'), 'utf8');
assert.equal((clientComponent.match(/fetch\s*\(/gu) ?? []).length, 3, 'client must contain exactly three fetch calls');
assert.equal(clientComponent.includes("fetch('/api/pool'"), true, 'pool status fetch must be same-origin and avoid upstream calls');
assert.equal(clientComponent.includes("fetch('/api/meta'"), false, 'page load must not spend upstream quota');
assert.equal(clientComponent.includes("fetch('/api/terms?q='"), true, 'terms fetch must be same-origin');
assert.equal(clientComponent.includes("fetch('/api/translations'"), true, 'translation fetch must be same-origin');
for (const forbidden of ['Authorization', 'Bearer ', '/wuwaterm-api/', 'WUWATERM_']) {
  assert.equal(clientComponent.includes(forbidden), false, `client contains ${forbidden}`);
}

const hostingText = readFileSync(join(root, '.openai/hosting.json'), 'utf8');
for (const name of envNames) assert.equal(hostingText.includes(name), false, `hosting.json contains ${name}`);
assert.deepEqual(Object.keys(JSON.parse(hostingText)).sort(), ['d1', 'project_id', 'r2']);

const scanExtensions = new Set(['.html', '.js', '.css', '.map', '.json']);
const requiredBuildArtifacts = [
  'dist/server/index.js',
  'dist/client/vinext-client-entry-manifest.json',
];
for (const artifact of requiredBuildArtifacts) {
  assert.equal(existsSync(join(root, artifact)), true, `missing ${artifact}; run the production build first`);
}

const emittedRoots = ['dist/client', 'dist/assets'].filter((path) => existsSync(join(root, path)));
let scannedFiles = 0;
for (const emittedRoot of emittedRoots) {
  const absolute = join(root, emittedRoot);
  for (const file of walk(absolute)) {
    if (!scanExtensions.has(extname(file))) continue;
    scannedFiles += 1;
    const text = readFileSync(file, 'utf8');
    for (const needle of [...envNames, ...canaries, 'Authorization', 'Bearer ']) {
      assert.equal(text.includes(needle), false, `${relative(root, file)} exposes ${needle}`);
    }
  }
}
assert.ok(scannedFiles > 0, 'production build emitted no scannable client artifacts');

const rootHtml = join(root, 'dist/index.html');
if (existsSync(rootHtml)) {
  const text = readFileSync(rootHtml, 'utf8');
  for (const needle of [...envNames, ...canaries]) assert.equal(text.includes(needle), false);
}

for (const emittedRoot of emittedRoots) {
  const absolute = join(root, emittedRoot);
  for (const file of walk(absolute)) {
    assert.notEqual(extname(file), '.map', `${relative(root, file)} is an unexpected client source map`);
  }
}

console.log('verify:no-client-secret PASS');

function* walk(directory) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) yield* walk(path);
    else if (entry.isFile() && !path.split(sep).includes('server')) yield path;
  }
}
