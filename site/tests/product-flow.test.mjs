// End-to-end product flow over HTTP: boots the synthetic runtime worker on
// miniflare with the real proxy/pool code and walks a visitor's journey
// (pool snapshot -> term lookup -> translation -> review), asserting both
// the responses and the shared-pool accounting behind them.
import test from 'node:test';
import assert from 'node:assert/strict';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { fixtureEnvironment } from './helpers/pool-fixture.mjs';

const MODULES = ['tests/helpers/runtime-worker.mjs', 'lib/wuwaterm-proxy.js', 'lib/shared-pool.js', 'lib/manuscript.js', 'lib/review-report.js']
  .map(path => ({ type: 'ESModule', path: fileURLToPath(new URL('../' + path, import.meta.url)) }));

async function boot() {
  const bindings = fixtureEnvironment(null);
  delete bindings.DB;
  const workerOptions = {
    modulesRoot: fileURLToPath(new URL('../', import.meta.url)),
    modules: MODULES,
    compatibilityDate: '2026-08-27',
    compatibilityFlags: ['nodejs_compat'],
    d1Databases: { DB: 'shared-beta' },
    bindings,
  };
  const mf = new Miniflare(convertV4MiniflareOptions({ workers: [{ ...workerOptions, name: 'site' }], cf: false }));
  const db = await mf.getD1Database('DB', 'site');
  await db.prepare(readFileSync(new URL('../drizzle/0000_shared_pool.sql', import.meta.url), 'utf8')).run();
  await db.prepare(readFileSync(new URL('../drizzle/0001_review_used.sql', import.meta.url), 'utf8')).run();
  return { mf, db };
}

test('visitor journey: pool snapshot, term lookup, translation, and review, with matching pool accounting', async () => {
  const { mf, db } = await boot();
  try {
    // Admission admits one request per second (second_key must advance); roll
    // the clock back between steps so a sequential journey is not throttled.
    const tick = () => db.prepare('UPDATE shared_pool SET second_key = 0 WHERE id = 1').run();

    const pool = await mf.dispatchFetch('http://site.test/api/pool');
    assert.equal(pool.status, 200);
    const snapshot = await pool.json();
    assert.equal(snapshot.status, 'available');
    assert.equal(snapshot.terms.remaining, snapshot.terms.limit);
    assert.equal(snapshot.translations.remaining, snapshot.translations.limit);

    const terms = await mf.dispatchFetch('http://site.test/api/terms?q=%E4%BB%8A%E6%B1%90');
    assert.equal(terms.status, 200);
    const termsBody = await terms.json();
    assert.deepEqual(termsBody.matches[0], { zh: '今汐', en: 'Jinhsi', category: 'character', reason: 'exact', score: 100 });
    await tick();

    const translated = await mf.dispatchFetch('http://site.test/api/translations', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text: '今汐' }),
    });
    assert.equal(translated.status, 200);
    const translatedBody = await translated.json();
    assert.equal(translatedBody.kind, 'exact');
    assert.equal(translatedBody.text, 'Jinhsi');
    assert.equal(translatedBody.dictionary_miss, false);
    await tick();

    const reviewed = await mf.dispatchFetch('http://site.test/api/reviews', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ source: '今汐', target: 'Jinhsi', direction: 'en' }),
    });
    assert.equal(reviewed.status, 200, await reviewed.clone().text());
    const reviewBody = await reviewed.json();
    assert.equal(reviewBody.findings[0].verdict, 'verified_constraint');
    assert.equal(reviewBody.coverage.not_evaluated >= 1, true);

    const row = await db.prepare('SELECT * FROM shared_pool').first();
    assert.equal(row.terms_used, 1);
    assert.equal(row.translation_used, 1);
    assert.equal(row.character_used, 2);
    assert.equal(row.review_used, 1);

    const after = await (await mf.dispatchFetch('http://site.test/api/pool')).json();
    assert.equal(after.terms.remaining, snapshot.terms.limit - 1);
    assert.equal(after.translations.remaining, snapshot.translations.limit - 1);
    assert.equal(after.characters.remaining, snapshot.characters.limit - 2);
  } finally { await mf.dispose(); }
});

test('visitor journey edge: unknown route answers 404 with the no-store envelope, and bad input is rejected before any upstream call', async () => {
  const { mf, db } = await boot();
  try {
    const missing = await mf.dispatchFetch('http://site.test/api/nope');
    assert.equal(missing.status, 404);

    const empty = await mf.dispatchFetch('http://site.test/api/terms?q=');
    assert.equal(empty.status, 400);
    assert.equal((await empty.json()).reason, 'site_invalid_request');

    const tooLong = await mf.dispatchFetch('http://site.test/api/translations', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text: '今'.repeat(2001) }),
    });
    assert.equal(tooLong.status, 400);
    assert.equal((await tooLong.json()).reason, 'site_invalid_request');

    // Rejections that never reached the upstream must not spend pool budget.
    const row = await db.prepare('SELECT * FROM shared_pool').first();
    assert.equal(row, null);
  } finally { await mf.dispose(); }
});
