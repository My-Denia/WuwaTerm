import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { requireBuiltArtifact } from '../../scripts/check-built-artifact.mjs';
import { fixture, freshApiReport } from '../fixtures/manuscript.mjs';
import { fixtureEnvironment } from './pool-fixture.mjs';

const root = fileURLToPath(new URL('../../', import.meta.url));

function reply(call) {
  const request_id = 'synthetic-correlation-1';
  if (call.path.startsWith('/wuwaterm-api/v1/terms?')) return Response.json({
    query: new URL(call.path, 'https://fixture.test').searchParams.get('q'),
    matches: [{ zh: '今汐', en: 'Jinhsi', category: 'character', reason: 'exact', score: 100 }], request_id,
  });
  if (call.path === '/wuwaterm-api/v1/translations') return Response.json({
    text: 'Jinhsi', kind: 'exact', direction: 'en', dictionary_miss: false, request_id,
  });
  if (call.path === '/wuwaterm-api/v1/reviews') {
    assert.equal(call.body.review_version, 'review-v2');
    return Response.json({ ...freshApiReport(fixture(call.body.source, call.body.target, call.body.direction).report), request_id });
  }
  assert.equal(call.path, '/wuwaterm-api/v1/meta');
  return Response.json({ api_version: 'v1', service_version: '0.4.1', term_count: 12345,
    schema_version: '3.6', source_profile: 'synthetic', source_commit: 'abc123', llm_configured: true, request_id });
}

// Wrangler's locked test harness forwards Worker outbound requests to Node's
// global fetch. Intercept only the synthetic origin; every other non-loopback
// destination fails closed. The real site routes and bindings remain intact.
export async function bootBuilt({ environment = {}, withoutDB = false, migrations = true } = {}) {
  const configPath = requireBuiltArtifact();
  process.env.WRANGLER_SEND_METRICS = 'false';
  process.env.WRANGLER_WRITE_LOGS = 'false';
  process.env.MINIFLARE_REGISTRY_PATH = root + '.wrangler/registry';
  // Process-local tool state; no edits to the owner's Wrangler configuration.
  process.env.XDG_CONFIG_HOME = root + '.wrangler/harness-config';
  const { createTestHarness } = await import('wrangler');
  const vars = { ...fixtureEnvironment(null), ...environment }; delete vars.DB;
  const token = vars.WUWATERM_SITE_DEVICE_TOKEN; delete vars.WUWATERM_SITE_DEVICE_TOKEN;
  const upstream = { calls: [], reply };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    if (url.origin === 'https://api.wuwaterm-test.net') {
      const request = new Request(input, init);
      const call = { path: url.pathname + url.search, method: request.method,
        headers: Object.fromEntries(request.headers), body: request.body ? await request.json() : null };
      upstream.calls.push(call);
      assert.equal(request.headers.get('authorization'), 'Bearer ' + fixtureEnvironment(null).WUWATERM_SITE_DEVICE_TOKEN);
      return upstream.reply(call);
    }
    if (url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) return originalFetch(input, init);
    throw new Error('Non-local network is disabled in built artifact tests');
  };
  let server;
  async function close() {
    try { await server?.close(); } finally { globalThis.fetch = originalFetch; }
  }
  try {
    const generated = JSON.parse(readFileSync(configPath, 'utf8'));
    // A negative binding test runs the same entrypoint with D1 deliberately
    // absent. Ordinary tests consume the generated configuration directly.
    const worker = withoutDB ? { config: {
      name: generated.name, main: root + 'dist/server/index.js', no_bundle: generated.no_bundle,
      rules: generated.rules, compatibility_date: generated.compatibility_date,
      compatibility_flags: generated.compatibility_flags, assets: { directory: root + 'dist/client' },
      vars: { ...vars, WUWATERM_SITE_DEVICE_TOKEN: token },
    } } : { configPath, vars, secrets: { WUWATERM_SITE_DEVICE_TOKEN: token } };
    server = createTestHarness({ root, workers: [worker] });
    const { url } = await server.listen();
    const { DB } = await server.getWorker().getEnv();
    if (DB && migrations) for (const file of readdirSync(root + 'drizzle').filter(n => n.endsWith('.sql')).sort()) {
      await DB.prepare(readFileSync(root + 'drizzle/' + file, 'utf8')).run();
    }
    return { server, url, db: DB, upstream, close,
      tick: () => DB.prepare('UPDATE shared_pool SET second_key = 0 WHERE id = 1').run() };
  } catch (error) { await close(); throw error; }
}
