import { createPool } from './helpers/pool-fixture.mjs';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  parseReviewRequest,
  parseTermsRequest,
  parseTranslationRequest,
  proxyMetaRequest,
  proxyReviewRequest,
  proxyTermsRequest,
  proxyTranslationRequest,
} from '../lib/wuwaterm-proxy.js';

const TOKEN = 'SYNTHETIC_PRODUCT_TOKEN_61E8';
const HOST = 'api.wuwaterm-test.net';
const BASE_URL = `https://${HOST}/wuwaterm-api/`;
// Fresh DB per projection fixture; shared contention is covered in shared-pool/D1 suites.
const ENVIRONMENT = {
  get DB() { return createPool(); },
  WUWATERM_SHARED_POOL_ENABLED: 'true',
  WUWATERM_TRANSLATION_ENABLED: 'true',
  WUWATERM_API_BASE_URL: BASE_URL,
  WUWATERM_API_ALLOWED_HOST: HOST,
  WUWATERM_SITE_DEVICE_TOKEN: TOKEN,
};

function upstreamJson(status, body, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

function assertSafeResponse(response) {
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(response.headers.get('set-cookie'), null);
  assert.equal(response.headers.get('content-type'), 'application/json; charset=utf-8');
}

test('metadata projects the backend revision fields without exposing private configuration', async () => {
  const response = await proxyMetaRequest({
    environment: ENVIRONMENT,
    fetchImpl: async () => upstreamJson(200, {
      service_version: '0.4.1',
      api_version: 'v1',
      schema_version: '3.6.0',
      source_profile: 'official',
      source_commit: 'abc123',
      term_count: 12_345,
      llm_configured: true,
      request_id: 'req-meta',
    }),
  });

  assert.equal(response.status, 200);
  assertSafeResponse(response);
  assert.deepEqual(await response.json(), {
    schema_version: '3.6.0',
    source_commit: 'abc123',
    game_version: null,
    resource_version: null,
    changelist: null,
    term_count: 12_345,
    request_id: 'req-meta',
  });
});

test('metadata accepts the versioned meta shape and projects only the public fields', async () => {
  const response = await proxyMetaRequest({
    environment: ENVIRONMENT,
    fetchImpl: async () => upstreamJson(200, {
      service_version: '0.4.1',
      api_version: 'v1',
      schema_version: '2',
      source_profile: 'official',
      source_commit: 'abc123',
      source_game_version: '3.7.0',
      source_resource_version: '3.7.8',
      source_changelist: '8975829',
      term_count: 11_329,
      llm_configured: true,
      request_id: 'req-meta-new',
    }),
  });

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    schema_version: '2',
    source_commit: 'abc123',
    game_version: '3.7.0',
    resource_version: '3.7.8',
    changelist: '8975829',
    term_count: 11_329,
    request_id: 'req-meta-new',
  });
});

test('term lookup uses a fixed endpoint and preserves every backend match in backend order', async () => {
  const matches = [
    { zh: '穗穗', en: 'Suisui', category: 'resonator', score: 100, reason: 'exact' },
    { zh: '穗穗（通讯中）', en: 'Suisui', category: 'speaker', score: 100, reason: 'exact' },
    { zh: '岁岁平安', en: 'Suisui Blessing', category: 'item', score: 71.25, reason: 'fuzzy' },
  ];
  let captured;
  const response = await proxyTermsRequest({
    environment: ENVIRONMENT,
    query: 'Suisui',
    fetchImpl: async (url, init) => {
      captured = { url: url.toString(), init };
      return upstreamJson(200, { query: 'Suisui', matches, request_id: 'req-terms' });
    },
  });

  assert.equal(captured.url, `${BASE_URL}v1/terms?q=Suisui`);
  assert.equal(captured.init.method, 'GET');
  assert.equal(captured.init.body, undefined);
  assert.equal(captured.init.redirect, 'manual');
  assert.equal(captured.init.credentials, 'omit');
  assert.equal(captured.init.headers.authorization, `Bearer ${TOKEN}`);
  assert.equal(response.status, 200);
  assertSafeResponse(response);
  assert.deepEqual((await response.json()).matches, matches);
});

test('term query is encoded as data and cannot rewrite the pinned upstream target', async () => {
  let captured;
  const response = await proxyTermsRequest({
    environment: ENVIRONMENT,
    query: '../translations?token=attacker&x=今汐',
    fetchImpl: async (url) => {
      captured = url;
      return upstreamJson(200, {
        query: '../translations?token=attacker&x=今汐',
        matches: [],
        request_id: 'req-encoded',
      });
    },
  });
  assert.equal(captured.pathname, '/wuwaterm-api/v1/terms');
  assert.equal(captured.searchParams.get('q'), '../translations?token=attacker&x=今汐');
  assert.equal(response.status, 200);
});

test('legal five-result fuzzy lookup preserves exact backend cardinality and order', async () => {
  const matches = [
    { zh: '今汐', en: 'Jinhsi', category: 'resonator', score: 92.5, reason: 'fuzzy' },
    { zh: '今汐（共鸣者）', en: 'Jinhsi', category: 'speaker', score: 89, reason: 'fuzzy' },
    { zh: '今汐突破材料', en: 'Jinhsi Ascension Material', category: 'item', score: 77, reason: 'fuzzy' },
    { zh: '今汐·待机', en: 'Jinhsi Idle', category: 'ui', score: 71, reason: 'fuzzy' },
    { zh: '今汐的信物', en: "Jinhsi's Token", category: 'quest', score: 68.25, reason: 'fuzzy' },
  ];
  const response = await proxyTermsRequest({
    environment: ENVIRONMENT,
    query: 'jinxi',
    fetchImpl: async () => upstreamJson(200, {
      query: 'jinxi',
      matches,
      request_id: 'req-five-fuzzy',
    }),
  });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.matches.length, 5);
  assert.deepEqual(body.matches, matches);
});

test('translation forwards only text and an optional explicit target direction', async () => {
  let captured;
  const response = await proxyTranslationRequest({
    environment: ENVIRONMENT,
    input: { text: '今汐踏着月光而来。', to: 'en' },
    fetchImpl: async (url, init) => {
      captured = { url: url.toString(), init };
      return upstreamJson(200, {
        kind: 'llm',
        text: 'Jinhsi arrives in the moonlight.',
        direction: 'en',
        dictionary_miss: false,
        request_id: 'req-translation',
      });
    },
  });
  assert.equal(captured.url, `${BASE_URL}v1/translations`);
  assert.equal(captured.init.method, 'POST');
  assert.equal(captured.init.headers['content-type'], 'application/json');
  assert.deepEqual(JSON.parse(captured.init.body), {
    text: '今汐踏着月光而来。',
    to: 'en',
  });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    kind: 'llm',
    text: 'Jinhsi arrives in the moonlight.',
    direction: 'en',
    dictionary_miss: false,
    request_id: 'req-translation',
  });
});

test('automatic translation omits to instead of detecting direction in the Site', async () => {
  let body;
  await proxyTranslationRequest({
    environment: ENVIRONMENT,
    input: { text: 'The shore is quiet.' },
    fetchImpl: async (_url, init) => {
      body = JSON.parse(init.body);
      return upstreamJson(200, {
        kind: 'exact',
        text: '海岸很安静。',
        direction: 'zh',
        dictionary_miss: false,
        request_id: 'req-auto',
      });
    },
  });
  assert.deepEqual(body, { text: 'The shore is quiet.' });
});

test('enumerated upstream failures retain only status, reason, and request id', async () => {
  const response = await proxyTranslationRequest({
    environment: ENVIRONMENT,
    input: { text: '需要模型处理的句子。' },
    fetchImpl: async () => upstreamJson(503, {
      error: { code: 'llm_unavailable', message: `secret ${TOKEN} ${BASE_URL}` },
      request_id: 'req-llm-down',
    }),
  });
  assert.equal(response.status, 503);
  assertSafeResponse(response);
  assert.deepEqual(await response.json(), {
    status: 'unavailable',
    reason: 'llm_unavailable',
    request_id: 'req-llm-down',
  });
});

test('an error request id containing a secret or case-equivalent internal origin is rejected, never reflected', async () => {
  for (const requestId of [TOKEN, `req-${HOST}`, `req-${BASE_URL}`, `req-${BASE_URL.toUpperCase()}`]) {
    const response = await proxyTranslationRequest({
      environment: ENVIRONMENT,
      input: { text: 'test' },
      fetchImpl: async () => upstreamJson(503, {
        error: { code: 'llm_unavailable', message: 'safe' },
        request_id: requestId,
      }),
    });
    assert.equal(response.status, 502);
    const text = await response.text();
    assert.equal(text.includes(requestId), false);
    assert.deepEqual(JSON.parse(text), {
      status: 'unavailable',
      reason: 'upstream_schema_mismatch',
    });
  }
});

test('a credential colliding with a fixed error field is still never reflected', async () => {
  const collidingToken = 'llm_unavailable';
  const response = await proxyTranslationRequest({
    environment: { ...ENVIRONMENT, WUWATERM_SITE_DEVICE_TOKEN: collidingToken },
    input: { text: 'test' },
    fetchImpl: async () => upstreamJson(503, {
      error: { code: 'llm_unavailable', message: 'safe' },
      request_id: 'req-collision',
    }),
  });
  assert.equal(response.status, 502);
  const text = await response.text();
  assert.equal(text.includes(collidingToken), false);
  assert.deepEqual(JSON.parse(text), {
    status: 'unavailable',
    reason: 'upstream_schema_mismatch',
  });
});

test('malformed success payloads and redirects fail closed without reflecting upstream data', async () => {
  const malformed = await proxyTermsRequest({
    environment: ENVIRONMENT,
    query: '今汐',
    fetchImpl: async () => upstreamJson(200, {
      query: '今汐',
      matches: [{ zh: TOKEN, en: 'Jinhsi' }],
      request_id: 'req-bad',
    }),
  });
  assert.equal(malformed.status, 502);
  assert.deepEqual(await malformed.json(), {
    status: 'unavailable',
    reason: 'upstream_schema_mismatch',
  });

  const redirected = await proxyTranslationRequest({
    environment: ENVIRONMENT,
    input: { text: 'test' },
    fetchImpl: async () => new Response(null, {
      status: 302,
      headers: { location: `https://attacker.invalid/${TOKEN}` },
    }),
  });
  assert.equal(redirected.status, 502);
  assert.deepEqual(await redirected.json(), {
    status: 'unavailable',
    reason: 'upstream_redirect',
  });
});

test('all translation success kinds pass the strict shared schema', async () => {
  for (const kind of ['noop', 'exact', 'fuzzy', 'llm']) {
    const response = await proxyTranslationRequest({
      environment: ENVIRONMENT,
      input: { text: 'test' },
      fetchImpl: async () => upstreamJson(200, {
        kind,
        text: 'result',
        direction: 'zh',
        dictionary_miss: kind === 'llm',
        request_id: `req-${kind}`,
      }),
    });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).kind, kind);
  }
});

test('VPS error status and code must be a documented compatible pair', async () => {
  const valid = [
    [400, 'invalid_request', 400, 'invalid_request'],
    [401, 'unauthorized', 401, 'upstream_unauthorized'],
    [403, 'forbidden', 403, 'upstream_forbidden'],
    [413, 'payload_too_large', 413, 'payload_too_large'],
    [422, 'input_too_long', 422, 'input_too_long'],
    [429, 'rate_limited', 429, 'upstream_rate_limited'],
    [500, 'internal', 503, 'upstream_unavailable'],
    [503, 'internal', 503, 'upstream_unavailable'],
    [503, 'llm_unavailable', 503, 'llm_unavailable'],
    [503, 'llm_budget_exhausted', 503, 'llm_budget_exhausted'],
    [504, 'internal', 504, 'upstream_timeout'],
  ];
  for (const [upstreamStatus, code, clientStatus, reason] of valid) {
    const response = await proxyTranslationRequest({
      environment: ENVIRONMENT,
      input: { text: 'test' },
      fetchImpl: async () => upstreamJson(upstreamStatus, {
        error: { code, message: 'safe message' },
        request_id: `req-${code}`,
      }),
    });
    assert.equal(response.status, clientStatus);
    assert.deepEqual(await response.json(), {
      status: 'unavailable',
      reason,
      request_id: `req-${code}`,
    });
  }

  const mismatch = await proxyTranslationRequest({
    environment: ENVIRONMENT,
    input: { text: 'test' },
    fetchImpl: async () => upstreamJson(503, {
      error: { code: 'unauthorized', message: 'wrong status' },
      request_id: 'req-mismatch',
    }),
  });
  assert.equal(mismatch.status, 502);
  assert.deepEqual(await mismatch.json(), {
    status: 'unavailable',
    reason: 'upstream_schema_mismatch',
  });
});

test('terms reject more matches than the backend contract permits', async () => {
  const response = await proxyTermsRequest({
    environment: ENVIRONMENT,
    query: 'test',
    fetchImpl: async () => upstreamJson(200, {
      query: 'test',
      matches: Array.from({ length: 6 }, (_, index) => ({
        zh: `词${index}`,
        en: `Term ${index}`,
        category: 'item',
        score: 50,
        reason: 'fuzzy',
      })),
      request_id: 'req-too-many',
    }),
  });
  assert.equal(response.status, 502);
  assert.deepEqual(await response.json(), {
    status: 'unavailable',
    reason: 'upstream_schema_mismatch',
  });
});

test('translation timeout covers response-header stalls and returns no upstream body', async () => {
  let cancelled = false;
  const response = await proxyTranslationRequest({
    environment: ENVIRONMENT,
    input: { text: 'test' },
    timeoutMs: 5,
    fetchImpl: async () => new Response(new ReadableStream({
      pull() { return new Promise(() => {}); },
      cancel() { cancelled = true; },
    }), { status: 200, headers: { 'content-type': 'application/json' } }),
  });
  assert.equal(cancelled, true);
  assert.equal(response.status, 504);
  assert.deepEqual(await response.json(), {
    status: 'unavailable',
    reason: 'upstream_timeout',
  });
});

test('translation error-body timeout stays armed after response headers', async () => {
  let cancelled = false;
  const result = await Promise.race([
    proxyTranslationRequest({
      environment: ENVIRONMENT,
      input: { text: 'test' },
      timeoutMs: 5,
      fetchImpl: async () => new Response(new ReadableStream({
        pull() { return new Promise(() => {}); },
        cancel() { cancelled = true; },
      }), { status: 503, headers: { 'content-type': 'application/json' } }),
    }),
    new Promise((resolve) => setTimeout(() => resolve('did-not-settle'), 100)),
  ]);
  assert.notEqual(result, 'did-not-settle');
  assert.equal(cancelled, true);
  assert.equal(result.status, 504);
  assert.deepEqual(await result.json(), {
    status: 'unavailable',
    reason: 'upstream_timeout',
  });
});

test('chunked inbound translation bodies are capped while streaming', async () => {
  let cancelled = false;
  const request = new Request('https://site.invalid/api/translations', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    duplex: 'half',
    body: new ReadableStream({
      start(controller) {
        controller.enqueue(new Uint8Array(40_000));
        controller.enqueue(new Uint8Array(30_000));
      },
      cancel() { cancelled = true; },
    }),
  });
  const parsed = await parseTranslationRequest(request);
  assert.equal(parsed.ok, false);
  assert.equal(parsed.response.status, 413);
  assert.equal(cancelled, true);
  assert.deepEqual(await parsed.response.json(), {
    status: 'unavailable',
    reason: 'site_request_too_large',
  });
});

test('translation request rejects wrong content type and invalid JSON before fetching', async () => {
  for (const request of [
    new Request('https://site.invalid/api/translations', {
      method: 'POST',
      headers: { 'content-type': 'text/plain' },
      body: '{}',
    }),
    new Request('https://site.invalid/api/translations', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{broken',
    }),
  ]) {
    const parsed = await parseTranslationRequest(request);
    assert.equal(parsed.ok, false);
    assert.equal(parsed.response.status, 400);
    assertSafeResponse(parsed.response);
  }
});

test('local request parsers accept only the public Site contract', async () => {
  assert.deepEqual(parseTermsRequest(new Request('https://site.invalid/api/terms?q=%E4%BB%8A%E6%B1%90')), {
    ok: true,
    query: '今汐',
  });
  const emptyTerms = parseTermsRequest(new Request('https://site.invalid/api/terms?q='));
  assert.equal(emptyTerms.ok, false);
  assert.equal(emptyTerms.response.status, 400);

  const automatic = await parseTranslationRequest(new Request('https://site.invalid/api/translations', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ text: '今汐', to: null }),
  }));
  assert.deepEqual(automatic, { ok: true, input: { text: '今汐' } });

  for (const body of [
    {},
    { text: '' },
    { text: '今汐', to: 'fr' },
    { text: '今汐', extra: true },
  ]) {
    const parsed = await parseTranslationRequest(new Request('https://site.invalid/api/translations', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }));
    assert.equal(parsed.ok, false);
    assert.equal(parsed.response.status, 400);
    assert.deepEqual(await parsed.response.json(), {
      status: 'unavailable',
      reason: 'site_invalid_request',
    });
  }
});

function sampleReviewBody(source, target, extraFindings = []) {
  const findings = [
    {
      id: '0:2:今汐',
      verdict: 'verified_constraint',
      rule_id: 'review.term_pair',
      source_span: { start: 0, end: 2, text: '今汐' },
      target_span: { start: 0, end: 6, text: 'Jinhsi' },
      candidates: [{
        zh: '今汐',
        en: 'Jinhsi',
        category: 'resonator',
        sources: [{ source_file: 'RoleInfo.json', source_id: 'RoleInfo_1304_Name' }],
      }],
      candidates_truncated: false,
    },
    ...extraFindings,
  ];
  return {
    request_id: 'req-review',
    source_revision: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    target_revision: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    rule_version: 'review-v1',
    dictionary: { schema_version: '2', source_commit: 'abc123', term_count: 12 },
    coverage: { evaluated: 1, not_evaluated: 1, rules: ['review.term_pair', 'review.sentence_meaning'] },
    findings,
    truncated: false,
  };
}

function denseCappedReviewBody(source) {
  const findings = [];
  const pad = 'N'.repeat(240);
  for (let index = 0; index < 32; index += 1) {
    findings.push({
      id: `${index}:${index + 1}:${Array.from(source)[index]}`,
      verdict: 'needs_review',
      rule_id: 'review.term_pair',
      source_span: { start: index, end: index + 1, text: Array.from(source)[index] },
      target_span: null,
      candidates: Array.from({ length: 8 }, (_, candidate) => ({
        zh: '今',
        en: `Jinhsi${candidate}`,
        category: 'resonator',
        sources: Array.from({ length: 8 }, (_, sourceIndex) => ({
          source_file: `${pad}${index}_${candidate}_${sourceIndex}.json`,
          source_id: `id_${index}_${candidate}_${sourceIndex}`,
        })),
      })),
      candidates_truncated: true,
    });
  }
  const body = {
    request_id: 'req-dense',
    source_revision: 'c'.repeat(64),
    target_revision: 'd'.repeat(64),
    rule_version: 'review-v1',
    dictionary: { schema_version: '2', source_commit: 'abc123', term_count: 99 },
    coverage: { evaluated: 32, not_evaluated: 1, rules: ['review.term_pair', 'review.sentence_meaning'] },
    findings,
    truncated: true,
  };
  while (Buffer.byteLength(JSON.stringify(body), 'utf8') > 65536) {
    const last = body.findings[body.findings.length - 1];
    if (last.candidates.length > 1) last.candidates.pop();
    else if (body.findings.length > 1) body.findings.pop();
    else break;
    body.truncated = true;
  }
  return body;
}

test('review proxy uses an independent endpoint and keeps exact keys', async () => {
  let captured;
  const input = { source: '今汐', target: 'Jinhsi', direction: 'en' };
  const upstream = sampleReviewBody(input.source, input.target);
  const response = await proxyReviewRequest({
    environment: ENVIRONMENT,
    input,
    fetchImpl: async (url, init) => {
      captured = { url: url.toString(), init };
      return upstreamJson(200, upstream);
    },
  });
  assert.equal(captured.url, `${BASE_URL}v1/reviews`);
  assert.equal(captured.init.method, 'POST');
  assert.equal(JSON.parse(captured.init.body).source, '今汐');
  assert.equal(response.status, 200);
  assertSafeResponse(response);
  const body = await response.json();
  assert.deepEqual(Object.keys(body).sort(), [
    'coverage', 'dictionary', 'findings', 'request_id', 'rule_version',
    'source_revision', 'target_revision', 'truncated',
  ]);
  assert.equal('reviews' in body, false);
});

test('dense 2000+2000 review responses stay 200 rather than upstream_response_too_large', async () => {
  const source = '今'.repeat(2000);
  const target = 'A'.repeat(2000);
  const upstream = denseCappedReviewBody(source);
  const encoded = Buffer.byteLength(JSON.stringify(upstream), 'utf8');
  assert.ok(encoded > 40_000, `stub too small: ${encoded}`);
  assert.ok(encoded <= 65_536, `stub over cap: ${encoded}`);
  const response = await proxyReviewRequest({
    environment: ENVIRONMENT,
    input: { source, target, direction: 'en' },
    fetchImpl: async () => upstreamJson(200, upstream),
  });
  assert.equal(response.status, 200, await response.clone().text());
  const body = await response.json();
  assert.notEqual(body.reason, 'upstream_response_too_large');
  assert.equal(body.truncated, true);
});

test('oversized or invalid review requests never reach upstream', async () => {
  let calls = 0;
  const fetchImpl = async () => { calls += 1; return upstreamJson(200, sampleReviewBody('今汐', 'Jinhsi')); };
  const parsedEmpty = await parseReviewRequest(new Request('https://site.invalid/api/reviews', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ source: '', target: 'Jinhsi', direction: 'en' }),
  }));
  assert.equal(parsedEmpty.ok, false);
  const tooLong = await proxyReviewRequest({
    environment: ENVIRONMENT,
    input: { source: '今'.repeat(2001), target: 'Jinhsi', direction: 'en' },
    fetchImpl,
  });
  assert.equal(tooLong.status, 400);
  assert.equal(calls, 0);
});

test('routes all delegate to the shared proxy and expose no environment names', () => {
  const routeFiles = [
    '../app/api/meta/route.ts',
    '../app/api/terms/route.ts',
    '../app/api/translations/route.ts',
    '../app/api/reviews/route.ts',
  ];
  for (const relativePath of routeFiles) {
    const source = readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
    assert.match(source, /wuwaterm-proxy\.js/u);
    assert.equal(source.includes('WUWATERM_API_BASE_URL'), false);
    assert.equal(source.includes('WUWATERM_SITE_DEVICE_TOKEN'), false);
    assert.equal(source.includes('set-cookie'), false);
  }
});

test('provenance UI reads meta on demand and does not embed a repository game version', () => {
  const pageSource = readFileSync(fileURLToPath(new URL('../app/page.tsx', import.meta.url)), 'utf8');
  const source = readFileSync(fileURLToPath(new URL('../app/components/dictionary-provenance.tsx', import.meta.url)), 'utf8');
  assert.equal(pageSource.includes('/api/meta'), false);
  assert.equal(pageSource.includes('useEffect'), false);
  assert.equal(source.includes('useEffect'), false);
  assert.equal(source.includes('/api/meta'), true);
  assert.equal(source.includes('3.7'), false);
  assert.equal(source.includes('9218d612'), false);
});

test('Product v1 UI uses only same-origin APIs and does not sort, filter, or deduplicate matches', () => {
  const componentPath = fileURLToPath(new URL('../app/components/translation-workbench.tsx', import.meta.url));
  const source = readFileSync(componentPath, 'utf8');
  for (const endpoint of ['/api/pool', '/api/terms', '/api/translations']) {
    assert.equal(source.includes(endpoint), true, `missing ${endpoint}`);
  }
  assert.equal(/https?:\/\//u.test(source), false);
  assert.equal(source.includes('new Set('), false);
  assert.equal(source.includes('.sort('), false);
  assert.equal(source.includes('.filter('), false);
  for (const field of ['zh', 'en', 'category', 'reason', 'score', 'request_id', 'kind']) {
    assert.equal(source.includes(field), true, `UI does not render ${field}`);
  }
  assert.equal(source.includes('AbortController'), true);
  assert.match(source, /仅停止本页等待/u);
  assert.match(source, /controller\.signal\.aborted \|\| translationController\.current !== controller/u);
  assert.match(source, /dictionary_miss/u);
  assert.equal(source.includes('dictionary hit'), false);
  assert.match(source, /转到整句翻译/u);
  assert.match(source, /刷新额度/u);
  const transfer = source.slice(
    source.indexOf('function moveQueryToTranslation()'),
    source.indexOf('return (', source.indexOf('function moveQueryToTranslation()')),
  );
  assert.ok(transfer.indexOf('translationController.current?.abort()') >= 0);
  assert.ok(transfer.indexOf('translationController.current = null') >= 0);
  assert.ok(transfer.indexOf('translationController.current?.abort()') < transfer.indexOf('setSource(query)'));
  assert.ok(transfer.indexOf('translationController.current = null') < transfer.indexOf('setSource(query)'));
  const proxySource = readFileSync(fileURLToPath(new URL('../lib/wuwaterm-proxy.js', import.meta.url)), 'utf8');
  assert.match(proxySource, /timeoutMs = 100_000/u);
  const pageSource = readFileSync(fileURLToPath(new URL('../app/page.tsx', import.meta.url)), 'utf8');
  assert.match(pageSource, /id="top"/u);
});
