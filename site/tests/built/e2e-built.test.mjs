import test from 'node:test';
import assert from 'node:assert/strict';
import { bootBuilt } from '../helpers/built-harness.mjs';
import { fixture, hash } from '../fixtures/manuscript.mjs';
import { validReport } from '../../lib/manuscript.js';
import { fixtureEnvironment } from '../helpers/pool-fixture.mjs';

const draft = { source: '今汐', target: 'Jinhsi', direction: 'en', review_version: 'review-v2' };
const post = body => ({ method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
const counters = db => db.prepare('SELECT * FROM shared_pool').first();

async function json(response, status, label = '') {
  assert.equal(response.status, status, label + ': ' + await response.clone().text());
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(response.headers.get('content-type'), 'application/json; charset=utf-8');
  const text = await response.text();
  for (const secret of [fixtureEnvironment(null).WUWATERM_SITE_DEVICE_TOKEN, 'api.wuwaterm-test.net', 'Bearer ']) assert.equal(text.includes(secret), false);
  return JSON.parse(text);
}

async function boot(t, options) {
  const runtime = await bootBuilt(options);
  t.after(runtime.close);
  return runtime;
}

test('built site renders landing pages and serves the emitted client assets', async t => {
  const { server, db, upstream } = await boot(t);
  const home = await server.fetch('/');
  assert.equal(home.status, 200);
  const html = await home.text();
  for (const label of ['术语查询', '整句翻译', '双语稿件工作台']) assert.ok(html.includes(label));
  const scripts = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map(match => match[1]);
  assert.ok(scripts.length > 0, 'the real client entry must be present');
  for (const script of scripts) {
    const asset = await server.fetch(script);
    assert.equal(asset.status, 200);
    assert.match(asset.headers.get('content-type'), /javascript/);
    assert.ok((await asset.text()).length > 0);
  }
  for (const page of ['/limits', '/privacy']) assert.equal((await server.fetch(page)).status, 200);
  assert.equal(await counters(db), null);
  assert.equal(upstream.calls.length, 0);
});

test('built real routes use bound environment and D1 for the review-v2 visitor journey', async t => {
  const { server, db, upstream, tick } = await boot(t);
  async function admitted(path, options) {
    const before = await counters(db);
    const result = await json(await server.fetch(path, options), 200);
    const after = await counters(db);
    const expected = before?.minute_key === after.minute_key ? before.upstream_used + 1 : 1;
    assert.equal(after.upstream_used, expected, 'each admission debits its actual minute window exactly once');
    return result;
  }
  const pool = await json(await server.fetch('/api/pool'), 200);
  assert.equal(pool.status, 'available');
  await admitted('/api/meta');
  // Make the next admission enter a new minute without waiting on wall time.
  await db.prepare('UPDATE shared_pool SET minute_key = minute_key - 1 WHERE id = 1').run();
  await tick();
  const terms = await admitted('/api/terms?q=%E4%BB%8A%E6%B1%90');
  assert.equal(terms.matches[0].en, 'Jinhsi');
  await tick();
  const translation = await admitted('/api/translations', post({ text: draft.source }));
  assert.equal(translation.text, draft.target);
  await tick();
  const input = { ...draft, alignments: [{ source: { start: 0, end: 2, text: draft.source }, target: { start: 0, end: 6, text: draft.target } }] };
  const report = await admitted('/api/reviews', post(input));
  assert.equal(report.rule_version, 'review-v2');
  assert.equal(report.source_revision, hash(draft.source));
  assert.equal(report.target_revision, hash(draft.target));
  assert.ok(validReport(report, draft.source, draft.target));
  assert.equal(report.findings[0].verdict, 'verified_constraint');
  assert.deepEqual(upstream.calls.at(-1).body, input);
  await tick();
  const selected = { ...input, resolutions: [{ mention_id: report.findings[0].id, choice: 'official_pair', candidate_id: report.findings[0].candidates[0].candidate_id }],
    resolution_context: { source_revision: report.source_revision, rule_version: 'review-v2', dictionary_revision: report.dictionary.revision, matcher_revision: report.matcher_revision } };
  await admitted('/api/reviews', post(selected));
  assert.deepEqual(upstream.calls.at(-1).body, selected);
  assert.equal(upstream.calls.at(-1).method, 'POST');
  assert.equal(upstream.calls.at(-1).headers['content-type'], 'application/json');
  const row = await counters(db);
  assert.equal(row.meta_used, 1);
  assert.equal(row.terms_used, 1);
  assert.equal(row.translation_used, 1);
  assert.equal(row.character_used, 2);
  assert.equal(row.review_used, 2);
  assert.equal(upstream.calls.length, 5);
  const after = await json(await server.fetch('/api/pool'), 200);
  assert.equal(after.terms.remaining, pool.terms.remaining - 1);
  assert.equal(after.translations.remaining, pool.translations.remaining - 1);
  assert.equal(after.characters.remaining, pool.characters.remaining - 2);
  assert.equal(after.reviews.remaining, pool.reviews.remaining - 2);
});

test('built production parser rejects malformed bodies and v2 fields before consuming quota', async t => {
  const { server, db, upstream } = await boot(t);
  assert.equal((await json(await server.fetch('/api/terms?q='), 400)).reason, 'site_invalid_request');
  assert.equal((await json(await server.fetch('/api/translations', post({ text: '' })), 400)).reason, 'site_invalid_request');
  // The text/plain case must stay last: the production handler rejects it on
  // content type without reading the body, and wrangler's locked dispatch
  // transport fails the next request ("Network connection lost") whenever a
  // request body is left unread, so no further fetch may follow it here.
  const cases = [
    [post({ ...draft, direction: 'auto' }), 'site_invalid_request'],
    [post({ ...draft, review_version: 'review-v3' }), 'site_invalid_request'],
    [post({ ...draft, alignments: null }), 'site_invalid_request'],
    [post({ ...draft, alignments: [{ source: { start: 0, end: 99, text: draft.source }, target: null }] }), 'site_invalid_request'],
    [post({ ...draft, resolutions: [{ mention_id: '0:2:今汐', choice: 'not_a_term' }] }), 'site_invalid_request'],
    [post({ ...draft, target: 'x'.repeat(2001) }), 'site_invalid_request'],
    [post({ ...draft, headers: { authorization: 'client-control' } }), 'site_invalid_request'],
    [{ method: 'POST', headers: { 'content-type': 'application/json' }, body: '{' }, 'site_invalid_request'],
    [{ method: 'POST', headers: { 'content-type': 'application/json' }, body: new Uint8Array([0xc3, 0x28]) }, 'site_invalid_request'],
    [{ method: 'POST', headers: { 'content-type': 'application/json' }, body: ' '.repeat(32769) }, 'site_request_too_large'],
    [{ method: 'POST', headers: { 'content-type': 'text/plain' }, body: JSON.stringify(draft) }, 'site_invalid_request'],
  ];
  for (const [index, [input, reason]] of cases.entries()) {
    const body = await json(await server.fetch('/api/reviews', input), reason === 'site_request_too_large' ? 413 : 400, `parser case ${index}`);
    assert.equal(body.reason, reason);
  }
  assert.equal(upstream.calls.length, 0);
  assert.equal(await counters(db), null);
});

test('built unknown routes expose the observed framework HTML 404, separately from API JSON errors', async t => {
  const { server, db, upstream } = await boot(t);
  for (const path of ['/api/nope', '/missing-page']) {
    const response = await server.fetch(path);
    assert.equal(response.status, 404);
    assert.equal(response.headers.get('content-type'), 'text/html; charset=utf-8');
    // The locked vinext bundle has no global JSON not_found/no-store handler.
    assert.equal(response.headers.get('cache-control'), null);
    assert.match(await response.text(), /404/);
  }
  assert.equal(upstream.calls.length, 0);
  assert.equal(await counters(db), null);
});

test('built upstream failures have redacted no-store errors and retain admitted review counts', async t => {
  const { server, db, upstream, tick } = await boot(t);
  for (const [status, code, expected, reason] of [[401, 'unauthorized', 401, 'upstream_unauthorized'], [429, 'rate_limited', 429, 'upstream_rate_limited'], [503, 'internal', 503, 'upstream_unavailable']]) {
    upstream.reply = () => Response.json({ error: { code, message: fixtureEnvironment(null).WUWATERM_SITE_DEVICE_TOKEN }, request_id: 'synthetic-upstream-error' }, { status });
    assert.equal((await json(await server.fetch('/api/reviews', post(draft)), expected)).reason, reason);
    await tick();
  }
  upstream.reply = () => Response.json({ ...fixture(draft.source, draft.target).report, matcher_revision: 'ab'.repeat(32), source_revision: '0'.repeat(64) });
  assert.equal((await json(await server.fetch('/api/reviews', post(draft)), 502)).reason, 'upstream_schema_mismatch');
  const row = await counters(db);
  assert.equal(row.review_used, 4);
  assert.equal(row.translation_used, 0);
  assert.equal(upstream.calls.length, 4);
});

test('built review admission stays independent of translation exhaustion and denies exhausted review without charging', async t => {
  const { server, db, upstream } = await boot(t);
  await json(await server.fetch('/api/reviews', post(draft)), 200);
  await db.prepare('UPDATE shared_pool SET second_key=0,minute_key=0,day_key=unixepoch()/86400,upstream_used=0,translation_minute_used=0,translation_used=30,character_used=12000').run();
  assert.equal((await json(await server.fetch('/api/translations', post({ text: '今汐' })), 429)).reason, 'translation_pool_exhausted');
  await json(await server.fetch('/api/reviews', post(draft)), 200);
  assert.equal((await counters(db)).review_used, 2);
  assert.equal((await counters(db)).translation_used, 30);
  await db.prepare('UPDATE shared_pool SET second_key=0,minute_key=0,upstream_used=0,review_used=60').run();
  assert.equal((await json(await server.fetch('/api/reviews', post(draft)), 429)).reason, 'reviews_pool_exhausted');
  assert.equal((await counters(db)).review_used, 60);
  assert.equal(upstream.calls.length, 2);
});

for (const [label, options, reason] of [
  ['missing token', { environment: { WUWATERM_SITE_DEVICE_TOKEN: '' } }, 'site_not_configured'],
  ['wrong allowed host', { environment: { WUWATERM_API_ALLOWED_HOST: 'wrong.fixture.test' } }, 'site_not_configured'],
  ['absent D1 binding', { withoutDB: true }, 'shared_pool_unavailable'],
  ['unmigrated D1 binding', { migrations: false }, 'shared_pool_unavailable'],
]) test(`built runtimeEnvironment fails closed with ${label}`, async t => {
  const { server, upstream } = await boot(t, options);
  assert.equal((await json(await server.fetch('/api/reviews', post(draft)), 503)).reason, reason);
  assert.equal(upstream.calls.length, 0);
});
