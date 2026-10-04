import test from 'node:test';
import assert from 'node:assert/strict';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { fixtureEnvironment } from './helpers/pool-fixture.mjs';

test('0000-only D1 schema keeps the original 10 columns and is not exercised by review UPSERT', async () => {
  const bindings = fixtureEnvironment(null);
  delete bindings.DB;
  const workerOptions = { modulesRoot: fileURLToPath(new URL('../', import.meta.url)), modules: ['tests/helpers/runtime-worker.mjs', 'tests/helpers/synthetic-upstream.mjs', 'lib/wuwaterm-proxy.js', 'lib/shared-pool.js', 'lib/manuscript.js', 'lib/review-report.js'].map(path => ({type: 'ESModule', path: fileURLToPath(new URL('../' + path, import.meta.url))})), compatibilityDate: '2026-08-27', compatibilityFlags: ['nodejs_compat'], d1Databases: { DB: 'shared-beta' }, bindings };
  const mf = new Miniflare(convertV4MiniflareOptions({ workers: [{ ...workerOptions, name: 'first' }], cf: false }));
  try {
    const db = await mf.getD1Database('DB', 'first');
    const sql = readFileSync(new URL('../drizzle/0000_shared_pool.sql', import.meta.url), 'utf8');
    await db.prepare(sql).run();
    const table = await db.prepare('PRAGMA table_info(shared_pool)').all();
    assert.deepEqual(table.results.map(r => r.name), ['id','second_key','minute_key','day_key','upstream_used','translation_minute_used','terms_used','translation_used','character_used','meta_used']);
    assert.equal((await db.prepare('SELECT count(*) n FROM shared_pool').first()).n, 0);
  } finally { await mf.dispose(); }
});

test('0000 then 0001 D1 admits mixed traffic, keeps review off the translation counter, and exhausts reviews independently', async () => {
  const bindings = fixtureEnvironment(null);
  delete bindings.DB;
  const workerOptions = { modulesRoot: fileURLToPath(new URL('../', import.meta.url)), modules: ['tests/helpers/runtime-worker.mjs', 'tests/helpers/synthetic-upstream.mjs', 'lib/wuwaterm-proxy.js', 'lib/shared-pool.js', 'lib/manuscript.js', 'lib/review-report.js'].map(path => ({type: 'ESModule', path: fileURLToPath(new URL('../' + path, import.meta.url))})), compatibilityDate: '2026-08-27', compatibilityFlags: ['nodejs_compat'], d1Databases: { DB: 'shared-beta' }, bindings };
  const mf = new Miniflare(convertV4MiniflareOptions({ workers: [{ ...workerOptions, name: 'first' }, { ...workerOptions, name: 'second' }], cf: false }));
  try {
    const db = await mf.getD1Database('DB', 'first');
    await db.prepare(readFileSync(new URL('../drizzle/0000_shared_pool.sql', import.meta.url), 'utf8')).run();
    await db.prepare(readFileSync(new URL('../drizzle/0001_review_used.sql', import.meta.url), 'utf8')).run();
    const table = await db.prepare('PRAGMA table_info(shared_pool)').all();
    assert.deepEqual(table.results.map(r => r.name), ['id','second_key','minute_key','day_key','upstream_used','translation_minute_used','terms_used','translation_used','character_used','meta_used','review_used']);
  const other = await mf.getWorker('second');
  // upstream_used is a minute-window counter that legitimately resets when
  // the burst crosses a minute boundary (and the day counters when it
  // crosses UTC midnight), so capture the clock and only assert equality
  // within a window the burst provably did not leave.
  const clockBefore = await db.prepare('SELECT unixepoch() AS clock').first();
  const results = await Promise.all(Array.from({ length: 40 }, async (_, i) => {
    const response = await (i % 2 ? other.fetch.bind(other) : mf.dispatchFetch)('http://site.test/api/' + (i % 2 ? 'terms?q=今汐' : 'meta'));
    return { status: response.status, body: await response.text() };
  }));
  const clockAfter = await db.prepare('SELECT unixepoch() AS clock').first();
  const minuteFlipped = Math.floor(clockBefore.clock / 60) !== Math.floor(clockAfter.clock / 60);
  const dayFlipped = Math.floor(clockBefore.clock / 86400) !== Math.floor(clockAfter.clock / 86400);
  const admitted = results.filter(r => r.status === 200).length;
  assert.ok(admitted >= 1 && admitted <= 6);
  assert.ok(results.every(r => [200, 429].includes(r.status)));
  // Cross-worker D1 reads can settle just after the responses under CI
  // scheduling; poll briefly for the counters to match the admissions
  // instead of racing one immediate read. A genuinely lost update still
  // fails once the bounded wait expires.
  let row;
  for (let attempt = 0; ; attempt++) {
    row = await db.prepare('SELECT * FROM shared_pool').first();
    const daySettled = dayFlipped || row.terms_used + row.meta_used === admitted;
    const minuteSettled = minuteFlipped || row.upstream_used === admitted;
    if (daySettled && minuteSettled) break;
    assert.ok(attempt < 80, `counters did not settle: admitted=${admitted} terms+meta=${row.terms_used + row.meta_used} upstream=${row.upstream_used}`);
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  if (!dayFlipped) assert.equal(row.terms_used + row.meta_used, admitted);
  else assert.ok(row.terms_used + row.meta_used >= 1 && row.terms_used + row.meta_used <= admitted);
  if (!minuteFlipped) assert.equal(row.upstream_used, admitted);
  else assert.ok(row.upstream_used >= 1 && row.upstream_used <= admitted);
  assert.equal(row.translation_used, 0);
    assert.equal(row.review_used, 0);
    assert.equal((await db.prepare('SELECT count(*) n FROM shared_pool').first()).n, 1);
    for (const r of results) {
      const body = r.body;
      for (const secret of ['SYNTHETIC_PRODUCT_TOKEN_61E8','api.wuwaterm-test.net','Authorization','Bearer ']) assert.equal(body.includes(secret), false);
      if (r.status === 200) assert.equal(JSON.parse(body).request_id, 'synthetic-correlation-1');
    }
    await db.prepare('UPDATE shared_pool SET second_key=0,minute_key=0,day_key=unixepoch()/86400,upstream_used=0,translation_minute_used=0,translation_used=30,character_used=12000').run();
    const denied = await mf.dispatchFetch('http://site.test/api/translations', { method: 'POST', body: JSON.stringify({ text: '今汐' }) });
    assert.equal(denied.status, 429);
    assert.equal((await denied.json()).reason, 'translation_pool_exhausted');
    const review = await mf.dispatchFetch('http://site.test/api/reviews', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ source: '今汐', target: 'Jinhsi', direction: 'en' }) });
    assert.equal(review.status, 200, await review.clone().text());
    assert.equal((await db.prepare('SELECT translation_used, review_used FROM shared_pool').first()).translation_used, 30);
    assert.equal((await db.prepare('SELECT review_used FROM shared_pool').first()).review_used, 1);
    const status = await mf.dispatchFetch('http://site.test/api/pool');
    const poolBody = await status.json();
    assert.equal(poolBody.translations.remaining, 0);
    assert.equal('reviews' in poolBody, false);
    await db.prepare('UPDATE shared_pool SET second_key=0,minute_key=0,day_key=unixepoch()/86400,upstream_used=0,review_used=60').run();
    const reviewDenied = await mf.dispatchFetch('http://site.test/api/reviews', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ source: '今汐', target: 'Jinhsi', direction: 'en' }) });
    assert.equal(reviewDenied.status, 429);
    assert.equal((await reviewDenied.json()).reason, 'reviews_pool_exhausted');
  } finally { await mf.dispose(); }
});
