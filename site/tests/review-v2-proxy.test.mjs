import assert from 'node:assert/strict';
import test from 'node:test';
import { parseReviewRequest, proxyReviewRequest } from '../lib/wuwaterm-proxy.js';
import { createPool, fixtureEnvironment } from './helpers/pool-fixture.mjs';
import { fixture } from './fixtures/manuscript.mjs';
const sample = fixture();
const MATCHER = 'c'.repeat(64);
const freshReport = { ...sample.report, matcher_revision: MATCHER };
const input = { source: sample.source, target: sample.target, direction: 'en', review_version: 'review-v2' };
const response = body => new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });

test('v2 proxy admits exactly once, preserves candidate/dictionary identity and leaves translation counters unchanged', async () => {
  const DB = createPool(); let calls = 0; let actual;
  try {
    const result = await proxyReviewRequest({ environment: fixtureEnvironment(DB), input, fetchImpl: async (url, options) => {
      calls++; assert.equal(options.headers['X-WuwaTerm-Matcher-Basis'], '1'); actual = JSON.parse(options.body); assert.equal(url.pathname, '/wuwaterm-api/v1/reviews'); return response(freshReport);
    } });
    assert.equal(result.status, 200); assert.equal(calls, 1); assert.deepEqual(actual, input);
    const body = await result.json(); assert.deepEqual(body, freshReport);
    assert.equal(DB.row().review_used, 1); assert.equal(DB.row().translation_used, 0); assert.equal(DB.row().character_used, 0);
  } finally { DB.close(); }
});
test('v2 invalid maps/context/choices reject before quota admission or upstream call', async () => {
  const mapping = { source: { start: 0, end: 3, text: '今汐。' }, target: { start: 0, end: 7, text: 'Jinhsi.' } };
  const malformed = [
    { ...input, alignments: [mapping, mapping] },
    { ...input, alignments: [{ ...mapping, source: { start: 0, end: 999, text: '今汐。' } }] },
    { ...input, alignments: [{ ...mapping, target: { start: 0, end: 7, text: 'Incorrect' } }] },
    { ...input, alignments: null }, { ...input, alignments: Array(65).fill(mapping) },
    { ...input, resolutions: [{ mention_id: '0:2:今汐', choice: 'official_pair', candidate_id: sample.report.findings[0].candidates[0].candidate_id }] },
    { ...input, resolution_context: { source_revision: 'fake', rule_version: 'review-v2', dictionary_revision: 'fake' } },
    { ...input, resolutions: [{ mention_id: '0:2:今汐', choice: 'not_a_term' }], resolution_context: { source_revision: 'a'.repeat(64), rule_version: 'review-v2', dictionary_revision: 'b'.repeat(64) } },
    { ...input, review_version: 'review-v1', alignments: [] }, { ...input, review_version: 'review-v3' },
  ];
  for (const request of malformed) {
    const DB = createPool(); let calls = 0;
    try {
      const result = await proxyReviewRequest({ environment: fixtureEnvironment(DB), input: request, fetchImpl: async () => { calls++; return response(sample.report); } });
      assert.equal(result.status, 400); assert.equal(calls, 0); assert.equal(DB.count(), 0);
    } finally { DB.close(); }
  }
});
test('v2 strict success validation rejects stale protocol, altered candidate keys and secret collisions', async () => {
  for (const mutate of [x => x.rule_version = 'review-v1', x => x.dictionary.revision = 'fake', x => delete x.matcher_revision, x => x.findings[0].candidates[0].extra = 'not allowed',
    x => x.findings[0].candidates[0].sources[0].source_id = 'SYNTHETIC_PRODUCT_TOKEN_61E8']) {
    const DB = createPool(); const body = structuredClone(freshReport); mutate(body);
    try {
      const result = await proxyReviewRequest({ environment: fixtureEnvironment(DB), input, fetchImpl: async () => response(body) });
      assert.equal(result.status, 502); assert.equal((await result.json()).reason, 'upstream_schema_mismatch');
      assert.equal(DB.row().review_used, 1);
    } finally { DB.close(); }
  }
});
test('public parser accepts opt-in and context while keeping raw workfiles off the network contract', async () => {
  const context = { source_revision: sample.report.source_revision, rule_version: 'review-v2', dictionary_revision: sample.report.dictionary.revision, matcher_revision: MATCHER };
  const value = { ...input, resolutions: [{ mention_id: '0:2:今汐', choice: 'not_a_term' }], resolution_context: context, alignments: [] };
  const parsed = await parseReviewRequest(new Request('https://example.test/api/reviews', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(value) }));
  assert.equal(parsed.ok, true); assert.deepEqual(parsed.input, value);
  const bad = await parseReviewRequest(new Request('https://example.test/api/reviews', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ...value, history: [] }) }));
  assert.equal(bad.ok, false);
});


test('v2 proxy rejects structurally valid reports for different source or target revisions', async () => {
  for (const field of ['source_revision', 'target_revision']) {
    const DB = createPool(); const body = structuredClone(freshReport); body[field] = '0'.repeat(64);
    try {
      const result = await proxyReviewRequest({environment:fixtureEnvironment(DB),input,fetchImpl:async()=>response(body)});
      assert.equal(result.status,502); assert.equal((await result.json()).reason,'upstream_schema_mismatch');
    } finally { DB.close(); }
  }
});
