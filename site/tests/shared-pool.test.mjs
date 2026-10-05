import assert from 'node:assert/strict';
import test from 'node:test';
import { admitRequest, poolStatus, POOL_LIMITS } from '../lib/shared-pool.js';
import { createPool, fixtureEnvironment } from './helpers/pool-fixture.mjs';
import { proxyReviewRequest, proxyTermsRequest, proxyTranslationRequest, parseTermsRequest, parseTranslationRequest } from '../lib/wuwaterm-proxy.js';

test('one caller can exhaust translations; lookup retains its own allowance', async () => {
  const db = createPool(); const env = fixtureEnvironment(db);
  for (let i = 0; i < POOL_LIMITS.translationsPerDay; i++) {
    db.advance(61);
    assert.equal((await admitRequest(env, 'translations', 3)).ok, true);
  }
  db.advance(61);
  const refused = await admitRequest(env, 'translations', 3);
  assert.equal(refused.ok, false);
  assert.equal((await refused.response.json()).reason, 'translation_pool_exhausted');
  assert.equal((await admitRequest(env, 'terms')).ok, true);
  const status = await (await poolStatus(env)).json();
  assert.equal(status.terms.remaining, POOL_LIMITS.termsPerDay - 1);
  assert.equal(status.translations.remaining, 0);
});

test('shared counters atomically limit concurrent mixed requests with no partial debit', async () => {
  const db = createPool(); const env = fixtureEnvironment(db);
  const results = await Promise.all(Array.from({ length: 100 }, (_, i) => admitRequest(env, i % 2 ? 'terms' : 'meta')));
  assert.equal(results.filter(x => x.ok).length, 1);
  for (let i = 1; i < POOL_LIMITS.upstreamPerMinute; i++) { db.advance(1); assert.equal((await admitRequest(env, 'terms')).ok, true); }
  db.advance(1);
  assert.equal((await admitRequest(env, 'terms')).ok, false);
  assert.equal(db.row().upstream_used, POOL_LIMITS.upstreamPerMinute);
  assert.equal(db.row().terms_used + db.row().meta_used, POOL_LIMITS.upstreamPerMinute);
});

test('independent UTC rollover and character boundary do not leave counter history', async () => {
  const db = createPool(); const env = fixtureEnvironment(db);
  for (let i = 0; i < POOL_LIMITS.charactersPerDay / 2000; i++) { db.advance(61); assert.equal((await admitRequest(env, 'translations', 2000)).ok, true); }
  db.advance(61);
  const denied = await admitRequest(env, 'translations', 1);
  assert.equal((await denied.response.json()).reason, 'translation_pool_exhausted');
  assert.equal((await admitRequest(env, 'terms')).ok, true);
  db.advance(86400);
  assert.equal((await admitRequest(env, 'translations', 2000)).ok, true);
  assert.equal(db.row().translation_used, 1);
  assert.equal(db.row().terms_used, 0);
  assert.equal(db.count(), 1);
});

test('missing DB, disabled translation, invalid policy, corrupt and ambiguous DB fail closed', async () => {
  let calls = 0;
  for (const DB of [undefined, { prepare() { throw Error('SYNTHETIC_PRIVATE_DETAIL'); } }, { prepare() { return { bind() { return this; }, first: async () => ({ upstream_used: -1 }) }; } }]) {
    const env = { ...fixtureEnvironment(createPool()), DB };
    const r = await proxyTermsRequest({ environment: env, query: '今汐', fetchImpl: async () => { calls++; } });
    assert.equal(r.status, 503); assert.equal((await r.text()).includes('SYNTHETIC_PRIVATE_DETAIL'), false);
  }
  const db = createPool(); const env = fixtureEnvironment(db);
  env.WUWATERM_TRANSLATION_ENABLED = 'false';
  assert.equal((await admitRequest(env, 'translations', 3)).response.status, 503);
  assert.equal(db.count(), 0);
  assert.equal((await admitRequest(env, 'terms')).ok, true);
  env.WUWATERM_SHARED_POOL_ENABLED = 'yes';
  assert.equal((await admitRequest(env, 'terms')).ok, false);
  assert.equal(calls, 0);
});

test('upstream failure and commit-then-error consume a reservation without retry/refund', async () => {
  const db = createPool(); const env = fixtureEnvironment(db); let calls = 0;
  const r = await proxyTranslationRequest({ environment: env, input: { text: '今汐' }, fetchImpl: async () => { calls++; throw Error('network'); } });
  assert.equal(r.status, 502); assert.equal(calls, 1); assert.equal(db.row().translation_used, 1);
  db.advance(61);
  const original = db.prepare;
  db.prepare = (...args) => { const stmt = original(...args); const first = stmt.first; stmt.first = async () => { await first(); throw Error('uncertain'); }; return stmt; };
  const r2 = await proxyTranslationRequest({ environment: env, input: { text: '今汐' }, fetchImpl: async () => { calls++; } });
  assert.equal(r2.status, 503); assert.equal(calls, 1); assert.equal(db.row().translation_used, 2);
});

test('slow D1 never dispatches even if the reservation later commits', async () => {
  const db = createPool(); const env = fixtureEnvironment(db); const original = db.prepare; let calls = 0;
  db.prepare = (...args) => { const stmt = original(...args); const first = stmt.first; stmt.first = async () => { await new Promise(r => setTimeout(r, 1100)); return first(); }; return stmt; };
  const r = await proxyTermsRequest({ environment: env, query: '今汐', fetchImpl: async () => { calls++; } });
  assert.equal(r.status, 503);
  await new Promise(r => setTimeout(r, 200));
  assert.equal(calls, 0); assert.equal(db.row().terms_used, 1);
});

test('Unicode scalar and body ceilings reject malformed or oversized input before admission', async () => {
  const request = text => new Request('http://site.test/api/translations', { method: 'POST', headers: { 'content-type':'application/json' }, body: JSON.stringify({text}) });
  assert.equal((await parseTranslationRequest(request('😀'.repeat(2000)))).ok, true);
  for (const text of ['😀'.repeat(2001), '\ud800', '\udc00']) assert.equal((await parseTranslationRequest(request(text))).ok, false);
  assert.equal(parseTermsRequest(new Request('http://site.test/api/terms?q='+encodeURIComponent('😀'.repeat(200)))).ok, true);
  assert.equal(parseTermsRequest(new Request('http://site.test/api/terms?q='+encodeURIComponent('😀'.repeat(201)))).ok, false);
  const tooLarge = await parseTranslationRequest(new Request('http://site.test/api/translations',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text:'x'.repeat(32769)})}));
  assert.equal(tooLarge.response.status, 413);
});

test('one caller can exhaust lookup independently without closing translation', async () => {
  const db = createPool(); const env = fixtureEnvironment(db);
  for (let i=0;i<POOL_LIMITS.termsPerDay;i++) { db.advance(61); assert.equal((await admitRequest(env,'terms')).ok,true); }
  db.advance(61);
  const denied=await admitRequest(env,'terms');
  assert.equal((await denied.response.json()).reason,'terms_pool_exhausted');
  assert.equal((await admitRequest(env,'translations',2)).ok,true);
});

test('independent callers share the same store and fixed window rollover has bounded headroom', async () => {
  const db=createPool(); const first=fixtureEnvironment(db); const second=fixtureEnvironment(db);
  for (let i=0;i<6;i++) { assert.equal((await admitRequest(i%2?first:second,'terms')).ok,true); db.advance(1); }
  assert.equal((await admitRequest(first,'meta')).ok,false);
  db.advance(54);
  assert.equal((await admitRequest(second,'meta')).ok,true);
  assert.equal(db.row().upstream_used,1);
  assert.ok(3*POOL_LIMITS.upstreamPerMinute < 30);
});

test('second-only contention retries in one second but translation minute exhaustion does not', async () => {
  const db = createPool(); const env = fixtureEnvironment(db);
  assert.equal((await admitRequest(env, 'translations', 2)).ok, true);
  const terms = await admitRequest(env, 'terms');
  assert.equal(terms.response.headers.get('retry-after'), '1');
  assert.equal((await terms.response.json()).reason, 'shared_pool_busy');
  const translation = await admitRequest(env, 'translations', 2);
  assert.equal(translation.response.headers.get('retry-after'), '60');
  db.advance(1);
  assert.equal((await admitRequest(env, 'terms')).ok, true);
});

test('review admission does not increment translation_used and survives translation exhaustion', async () => {
  const db = createPool(); const env = fixtureEnvironment(db);
  assert.equal((await admitRequest(env, 'review', 0)).ok, true);
  assert.equal(db.row().review_used, 1);
  assert.equal(db.row().translation_used, 0);
  assert.equal(db.row().character_used, 0);
  assert.equal(db.row().terms_used, 0);
  assert.equal(db.row().meta_used, 0);
  for (let i = 0; i < POOL_LIMITS.translationsPerDay; i++) {
    db.advance(61);
    assert.equal((await admitRequest(env, 'translations', 3)).ok, true);
  }
  db.advance(61);
  const refused = await admitRequest(env, 'translations', 3);
  assert.equal((await refused.response.json()).reason, 'translation_pool_exhausted');
  assert.equal((await admitRequest(env, 'review', 0)).ok, true);
  const status = await (await poolStatus(env)).json();
  assert.equal(status.reviews.remaining, POOL_LIMITS.reviewsPerDay - 2);
});

test('pool snapshot projects review allowance across unused, exhausted, and next UTC day', async () => {
  const db = createPool(); const env = fixtureEnvironment(db);
  const unused = await (await poolStatus(env)).json();
  assert.equal(unused.reviews.used, 0);
  assert.equal(unused.reviews.remaining, unused.reviews.limit);
  assert.equal(unused.reviews.limit, POOL_LIMITS.reviewsPerDay);
  assert.equal(unused.shared, true);
  assert.equal(unused.fairness_guaranteed, false);
  for (let i = 0; i < POOL_LIMITS.reviewsPerDay; i++) {
    db.advance(61);
    assert.equal((await admitRequest(env, 'review', 0)).ok, true);
  }
  const exhausted = await (await poolStatus(env)).json();
  assert.equal(exhausted.reviews.remaining, 0);
  assert.equal(exhausted.reviews.used, POOL_LIMITS.reviewsPerDay);
  const denied = await admitRequest(env, 'review', 0);
  assert.equal((await denied.response.json()).reason, 'reviews_pool_exhausted');
  assert.equal(exhausted.translations.remaining, POOL_LIMITS.translationsPerDay);
  db.advance(86400);
  const rolled = await (await poolStatus(env)).json();
  assert.equal(rolled.reviews.used, 0);
  assert.equal(rolled.reviews.remaining, POOL_LIMITS.reviewsPerDay);
  assert.equal((await admitRequest(env, 'review', 0)).ok, true);
});

test('review daily exhaustion is independent of translation remaining', async () => {
  const db = createPool(); const env = fixtureEnvironment(db);
  for (let i = 0; i < POOL_LIMITS.reviewsPerDay; i++) {
    db.advance(61);
    assert.equal((await admitRequest(env, 'review', 0)).ok, true);
  }
  db.advance(61);
  const denied = await admitRequest(env, 'review', 0);
  assert.equal((await denied.response.json()).reason, 'reviews_pool_exhausted');
  assert.equal((await admitRequest(env, 'translations', 3)).ok, true);
});

test('proxyReviewRequest charges review_used and not translation_used', async () => {
  const db = createPool(); const env = fixtureEnvironment(db);
  const response = await proxyReviewRequest({
    environment: env,
    input: { source: '今汐', target: 'Jinhsi', direction: 'en' },
    fetchImpl: async () => new Response(JSON.stringify({
      request_id: 'req-review-pool',
      source_revision: 'a'.repeat(64),
      target_revision: 'b'.repeat(64),
      rule_version: 'review-v1',
      dictionary: { schema_version: '2', source_commit: 'abc123', term_count: 1 },
      coverage: { evaluated: 1, not_evaluated: 1, rules: ['review.term_pair'] },
      findings: [{
        id: '0:2:今汐',
        verdict: 'verified_constraint',
        rule_id: 'review.term_pair',
        source_span: { start: 0, end: 2, text: '今汐' },
        target_span: { start: 0, end: 6, text: 'Jinhsi' },
        candidates: [{ zh: '今汐', en: 'Jinhsi', category: 'resonator', sources: [{ source_file: 'RoleInfo.json', source_id: '1' }] }],
        candidates_truncated: false,
      }],
      truncated: false,
    }), { status: 200, headers: { 'content-type': 'application/json' } }),
  });
  assert.equal(response.status, 200, await response.clone().text());
  assert.equal(db.row().review_used, 1);
  assert.equal(db.row().translation_used, 0);
});

test('meta daily exhaustion advertises UTC day reset, independently of the minute gate', async () => {
  const db = createPool(); const env = fixtureEnvironment(db);
  for (let i = 0; i < POOL_LIMITS.metaPerDay; i++) { db.advance(61); assert.equal((await admitRequest(env, 'meta')).ok, true); }
  const exhausted = await admitRequest(env, 'meta');
  assert.equal(exhausted.response.status, 429);
  assert.equal(exhausted.response.headers.get('retry-after'), String(86400 - 61 * POOL_LIMITS.metaPerDay));
  assert.equal((await exhausted.response.json()).reason, 'meta_pool_exhausted');
  db.advance(86400);
  assert.equal((await admitRequest(env, 'meta')).ok, true);
});
