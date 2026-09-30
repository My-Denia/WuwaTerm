import { createPool } from './helpers/pool-fixture.mjs';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { proxyMetaRequest } from '../lib/wuwaterm-proxy.js';

const TOKEN = 'SYNTHETIC_DEVICE_SENTINEL_74A1C9';
const ALLOWED_HOST = 'meta.wuwaterm-test.net';
const BASE_URL = `https://${ALLOWED_HOST}/wuwaterm-api/`;
// Fresh DB per projection fixture; shared contention is covered in shared-pool/D1 suites.
const ENVIRONMENT = {
  get DB() { return createPool(); },
  WUWATERM_SHARED_POOL_ENABLED: 'true',
  WUWATERM_TRANSLATION_ENABLED: 'true',
  WUWATERM_API_BASE_URL: BASE_URL,
  WUWATERM_API_ALLOWED_HOST: ALLOWED_HOST,
  WUWATERM_SITE_DEVICE_TOKEN: TOKEN,
};

function metaBody(overrides = {}) {
  return {
    service_version: '0.4.0',
    api_version: 'v1',
    schema_version: '3.6.0',
    source_profile: 'official',
    source_commit: 'abc123',
    term_count: 12_345,
    llm_configured: true,
    request_id: 'req-test-001',
    ...overrides,
  };
}

function upstreamJson(status, body, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

function stalledResponse(status, headers = {}, { cancelStalls = false } = {}) {
  let cancelled = false;
  const stream = new ReadableStream({
    pull() {
      return new Promise(() => {});
    },
    cancel() {
      cancelled = true;
      if (cancelStalls) return new Promise(() => {});
    },
  });
  return {
    response: new Response(stream, { status, headers }),
    wasCancelled: () => cancelled,
  };
}

function unsettledReaderResponse(read) {
  let cancelled = false;
  const reader = {
    read,
    cancel() {
      cancelled = true;
      return new Promise(() => {});
    },
    releaseLock() {},
  };
  return {
    response: {
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      body: { getReader: () => reader },
    },
    wasCancelled: () => cancelled,
  };
}

async function settleWithin(promise, timeoutMs = 150) {
  return Promise.race([
    promise.then((value) => ({ settled: true, value })),
    new Promise((resolve) => setTimeout(() => resolve({ settled: false }), timeoutMs)),
  ]);
}

async function bodyOf(response) {
  return JSON.parse(await response.text());
}

function assertSafeHeaders(response) {
  assert.deepEqual(
    [...response.headers.keys()].sort(),
    ['cache-control', 'content-type', 'x-robots-tag'],
  );
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(response.headers.get('content-type'), 'application/json; charset=utf-8');
  assert.equal(response.headers.get('x-robots-tag'), 'noindex, nofollow, noarchive');
  assert.equal(response.headers.get('set-cookie'), null);
}

function assertNoCanary(value) {
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  assert.equal(text.includes(TOKEN), false);
  assert.equal(text.includes(BASE_URL), false);
  assert.equal(text.includes(ALLOWED_HOST), false);
  assert.equal(text.includes('Authorization'), false);
}

async function assertUnavailable(response, status, reason) {
  assert.equal(response.status, status);
  assertSafeHeaders(response);
  const body = await bodyOf(response);
  assert.deepEqual(body, { status: 'unavailable', reason });
  assertNoCanary(body);
}

test('200 uses the fixed metadata path and strict server-owned request', async () => {
  let captured;
  const response = await proxyMetaRequest({
    environment: ENVIRONMENT,
    fetchImpl: async (url, init) => {
      captured = { url: url.toString(), init };
      return upstreamJson(200, metaBody(), { 'set-cookie': `upstream=${TOKEN}` });
    },
  });

  assert.equal(captured.url, `${BASE_URL}v1/meta`);
  assert.equal(captured.init.method, 'GET');
  assert.equal(captured.init.headers.authorization, `Bearer ${TOKEN}`);
  assert.equal(captured.init.headers.accept, 'application/json');
  assert.equal(Object.keys(captured.init.headers).length, 2);
  assert.equal(captured.init.cache, 'no-store');
  assert.equal(captured.init.credentials, 'omit');
  assert.equal(captured.init.redirect, 'manual');
  assert.equal(response.status, 200);
  assertSafeHeaders(response);
  assert.deepEqual(await bodyOf(response), {
    schema_version: '3.6.0',
    source_commit: 'abc123',
    game_version: null,
    resource_version: null,
    changelist: null,
    term_count: 12_345,
    request_id: 'req-test-001',
  });
});

test('the documented API mount accepts an omitted trailing slash only', async () => {
  let captured;
  const response = await proxyMetaRequest({
    environment: {
      ...ENVIRONMENT,
      WUWATERM_API_BASE_URL: BASE_URL.slice(0, -1),
    },
    fetchImpl: async (url) => {
      captured = url.toString();
      return upstreamJson(200, metaBody());
    },
  });
  assert.equal(response.status, 200);
  assert.equal(captured, `${BASE_URL}v1/meta`);
});

test('missing or unsafe runtime settings fail closed without fetching', async () => {
  const credentialedBase = new URL(BASE_URL);
  credentialedBase.username = 'synthetic-user';
  credentialedBase.password = 'synthetic-password';
  const cases = [
    undefined,
    {},
    { ...ENVIRONMENT, WUWATERM_API_ALLOWED_HOST: undefined },
    { ...ENVIRONMENT, WUWATERM_API_BASE_URL: `http://${ALLOWED_HOST}/wuwaterm-api/` },
    { ...ENVIRONMENT, WUWATERM_API_ALLOWED_HOST: 'other.wuwaterm-test.net' },
    { ...ENVIRONMENT, WUWATERM_API_ALLOWED_HOST: ALLOWED_HOST.toUpperCase() },
    { ...ENVIRONMENT, WUWATERM_API_BASE_URL: credentialedBase.toString() },
    { ...ENVIRONMENT, WUWATERM_API_BASE_URL: `https://${ALLOWED_HOST}/` },
    { ...ENVIRONMENT, WUWATERM_API_BASE_URL: `https://${ALLOWED_HOST}/root/` },
    { ...ENVIRONMENT, WUWATERM_API_BASE_URL: `https://${ALLOWED_HOST}/wuwaterm-api//` },
    { ...ENVIRONMENT, WUWATERM_API_BASE_URL: `${BASE_URL}?leak=1` },
    { ...ENVIRONMENT, WUWATERM_API_BASE_URL: `https://${ALLOWED_HOST}:8443/wuwaterm-api/` },
    { ...ENVIRONMENT, WUWATERM_API_BASE_URL: `https://${ALLOWED_HOST}:443/wuwaterm-api/` },
    { ...ENVIRONMENT, WUWATERM_API_BASE_URL: `https://${ALLOWED_HOST}\\wuwaterm-api\\` },
    {
      ...ENVIRONMENT,
      WUWATERM_API_BASE_URL: 'https://127.0.0.1/wuwaterm-api/',
      WUWATERM_API_ALLOWED_HOST: '127.0.0.1',
    },
    {
      ...ENVIRONMENT,
      WUWATERM_API_BASE_URL: 'https://[::1]/wuwaterm-api/',
      WUWATERM_API_ALLOWED_HOST: '[::1]',
    },
    {
      ...ENVIRONMENT,
      WUWATERM_API_BASE_URL: 'https://service.internal/wuwaterm-api/',
      WUWATERM_API_ALLOWED_HOST: 'service.internal',
    },
  ];
  for (const environment of cases) {
    let called = false;
    const response = await proxyMetaRequest({
      environment,
      fetchImpl: async () => {
        called = true;
        throw new Error('must not be reached');
      },
    });
    assert.equal(called, false);
    await assertUnavailable(response, 503, 'site_not_configured');
  }
});

test('client-shaped URL, headers, body, and method inputs cannot rewrite upstream', async () => {
  let captured;
  const response = await proxyMetaRequest({
    environment: ENVIRONMENT,
    request: {
      url: 'https://attacker.invalid/private',
      method: 'POST',
      headers: { authorization: 'Bearer attacker', cookie: 'session=attacker' },
      body: 'attacker',
    },
    fetchImpl: async (url, init) => {
      captured = { url: url.toString(), init };
      return upstreamJson(200, metaBody());
    },
  });
  assert.equal(response.status, 200);
  assert.equal(captured.url, `${BASE_URL}v1/meta`);
  assert.equal(captured.init.method, 'GET');
  assert.deepEqual(captured.init.headers, {
    accept: 'application/json',
    authorization: `Bearer ${TOKEN}`,
  });
});

for (const [status, expectedStatus, expectedReason] of [
  [401, 401, 'upstream_unauthorized'],
  [403, 403, 'upstream_forbidden'],
  [404, 502, 'upstream_not_found'],
  [429, 429, 'upstream_rate_limited'],
  [500, 503, 'upstream_unavailable'],
  [503, 503, 'upstream_unavailable'],
  [504, 504, 'upstream_timeout'],
  [418, 502, 'upstream_network_error'],
]) {
  test(`upstream ${status} maps to fixed ${expectedReason}`, async () => {
    const response = await proxyMetaRequest({
      environment: ENVIRONMENT,
      fetchImpl: async () => upstreamJson(status, {
        error: { code: TOKEN, message: BASE_URL },
        request_id: TOKEN,
      }, { location: `https://${ALLOWED_HOST}/${TOKEN}`, 'set-cookie': `device=${TOKEN}` }),
    });
    await assertUnavailable(response, expectedStatus, expectedReason);
  });
}

for (const status of [300, 301, 302, 307, 308]) {
  test(`redirect ${status} is observed and rejected without following Location`, async () => {
    let calls = 0;
    const response = await proxyMetaRequest({
      environment: ENVIRONMENT,
      fetchImpl: async (_url, init) => {
        calls += 1;
        assert.equal(init.redirect, 'manual');
        return new Response(null, {
          status,
          headers: { location: `https://redirect-target.invalid/${TOKEN}` },
        });
      },
    });
    assert.equal(calls, 1);
    await assertUnavailable(response, 502, 'upstream_redirect');
  });
}

test('every pre-body rejection cancels a stalled upstream stream', async () => {
  const cases = [
    { status: 302, reason: 'upstream_redirect', expectedStatus: 502 },
    { status: 401, reason: 'upstream_unauthorized', expectedStatus: 401 },
    { status: 403, reason: 'upstream_forbidden', expectedStatus: 403 },
    { status: 404, reason: 'upstream_not_found', expectedStatus: 502 },
    { status: 429, reason: 'upstream_rate_limited', expectedStatus: 429 },
    { status: 500, reason: 'upstream_unavailable', expectedStatus: 503 },
    { status: 504, reason: 'upstream_timeout', expectedStatus: 504 },
    { status: 418, reason: 'upstream_network_error', expectedStatus: 502 },
    {
      status: 200,
      headers: { 'content-type': 'text/plain' },
      reason: 'upstream_invalid_content_type',
      expectedStatus: 502,
    },
    {
      status: 200,
      headers: { 'content-type': 'application/json', 'content-length': '65537' },
      reason: 'upstream_response_too_large',
      expectedStatus: 502,
    },
  ];

  for (const [index, item] of cases.entries()) {
    const stalled = stalledResponse(item.status, {
      'content-type': 'application/json',
      ...item.headers,
    }, { cancelStalls: index === 0 });
    const response = await proxyMetaRequest({
      environment: ENVIRONMENT,
      fetchImpl: async () => stalled.response,
    });
    assert.equal(stalled.wasCancelled(), true, `status ${item.status} body was not cancelled`);
    await assertUnavailable(response, item.expectedStatus, item.reason);
  }
});

test('timeout aborts one request and returns a fixed 504', async () => {
  let observedSignal;
  let calls = 0;
  const response = await proxyMetaRequest({
    environment: ENVIRONMENT,
    timeoutMs: 5,
    fetchImpl: async (_url, init) => {
      calls += 1;
      observedSignal = init.signal;
      return new Promise((_resolve, reject) => {
        init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
      });
    },
  });
  assert.equal(calls, 1);
  assert.equal(observedSignal.aborted, true);
  await assertUnavailable(response, 504, 'upstream_timeout');
});

test('timeout also cancels a body that stalls after response headers', async () => {
  let cancelled = false;
  const stream = new ReadableStream({
    pull() {
      return new Promise(() => {});
    },
    cancel() {
      cancelled = true;
    },
  });
  const response = await proxyMetaRequest({
    environment: ENVIRONMENT,
    timeoutMs: 5,
    fetchImpl: async () => new Response(stream, {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
  });
  assert.equal(cancelled, true);
  await assertUnavailable(response, 504, 'upstream_timeout');
});

test('timeout does not await a reader cancellation promise that never settles', async () => {
  const stalled = unsettledReaderResponse(() => new Promise(() => {}));
  const outcome = await settleWithin(proxyMetaRequest({
    environment: ENVIRONMENT,
    timeoutMs: 5,
    fetchImpl: async () => stalled.response,
  }));
  assert.equal(stalled.wasCancelled(), true);
  assert.equal(outcome.settled, true);
  await assertUnavailable(outcome.value, 504, 'upstream_timeout');
});

for (const label of ['dns failure', 'tls failure', 'connection reset']) {
  test(`${label} is a redacted fixed 502 and is not retried or logged`, async () => {
    const original = { log: console.log, warn: console.warn, error: console.error };
    const logs = [];
    let calls = 0;
    console.log = console.warn = console.error = (...args) => logs.push(args);
    try {
      const response = await proxyMetaRequest({
        environment: ENVIRONMENT,
        fetchImpl: async () => {
          calls += 1;
          throw new Error(`${label} ${TOKEN} ${BASE_URL}`);
        },
      });
      assert.equal(calls, 1);
      assert.equal(logs.length, 0);
      await assertUnavailable(response, 502, 'upstream_network_error');
    } finally {
      Object.assign(console, original);
    }
  });
}

test('only exact application/json media type is accepted', async () => {
  const accepted = await proxyMetaRequest({
    environment: ENVIRONMENT,
    fetchImpl: async () => upstreamJson(200, metaBody(), {
      'content-type': 'Application/JSON; charset=utf-8',
    }),
  });
  assert.equal(accepted.status, 200);

  for (const contentType of ['application/jsonp', 'text/json', 'text/plain', '']) {
    const response = await proxyMetaRequest({
      environment: ENVIRONMENT,
      fetchImpl: async () => new Response(JSON.stringify(metaBody()), {
        status: 200,
        headers: contentType ? { 'content-type': contentType } : {},
      }),
    });
    await assertUnavailable(response, 502, 'upstream_invalid_content_type');
  }
});

test('invalid JSON and invalid UTF-8 fail closed with no raw body', async () => {
  const cases = [
    new Response(`{broken ${TOKEN}`, {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
    new Response(new Uint8Array([0xc3, 0x28]), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
  ];
  for (const upstream of cases) {
    const response = await proxyMetaRequest({ environment: ENVIRONMENT, fetchImpl: async () => upstream });
    await assertUnavailable(response, 502, 'upstream_invalid_json');
  }
});

test('schema mismatch is stable and allowlists no upstream fields', async () => {
  for (const overrides of [
    { request_id: undefined },
    { term_count: -1 },
    { llm_configured: 'yes' },
    { schema_version: 1 },
    { source_profile: { secret: TOKEN } },
    { source_repo_url: 'https://example.invalid' },
    { source_game_version: '3.7.0' },
    { source_game_version: 1, source_resource_version: '3.7.8', source_changelist: '8975829' },
    { source_game_version: '', source_resource_version: '3.7.8', source_changelist: '8975829' },
  ]) {
    const response = await proxyMetaRequest({
      environment: ENVIRONMENT,
      fetchImpl: async () => upstreamJson(200, metaBody(overrides)),
    });
    await assertUnavailable(response, 502, 'upstream_schema_mismatch');
  }
});

test('the closed versioned meta shape projects reported values, nulls, and the unavailable sentinel', async () => {
  const versions = {
    source_game_version: '3.7.0',
    source_resource_version: '3.7.8',
    source_changelist: '8975829',
  };
  const reported = await proxyMetaRequest({
    environment: ENVIRONMENT,
    fetchImpl: async () => upstreamJson(200, metaBody(versions)),
  });
  assert.deepEqual(await bodyOf(reported), {
    schema_version: '3.6.0',
    source_commit: 'abc123',
    game_version: '3.7.0',
    resource_version: '3.7.8',
    changelist: '8975829',
    term_count: 12_345,
    request_id: 'req-test-001',
  });

  const missing = await proxyMetaRequest({
    environment: ENVIRONMENT,
    fetchImpl: async () => upstreamJson(200, metaBody({
      source_game_version: null,
      source_resource_version: null,
      source_changelist: null,
    })),
  });
  assert.deepEqual(await bodyOf(missing), {
    schema_version: '3.6.0',
    source_commit: 'abc123',
    game_version: null,
    resource_version: null,
    changelist: null,
    term_count: 12_345,
    request_id: 'req-test-001',
  });

  const unrecorded = await proxyMetaRequest({
    environment: ENVIRONMENT,
    fetchImpl: async () => upstreamJson(200, metaBody({
      source_game_version: 'unavailable',
      source_resource_version: 'unavailable',
      source_changelist: 'unavailable',
    })),
  });
  assert.equal((await bodyOf(unrecorded)).game_version, 'unavailable');
});

test('a token inside a reported version field is not returned to the browser', async () => {
  const response = await proxyMetaRequest({
    environment: ENVIRONMENT,
    fetchImpl: async () => upstreamJson(200, metaBody({
      source_game_version: TOKEN,
      source_resource_version: '3.7.8',
      source_changelist: '8975829',
    })),
  });
  assert.equal(response.status, 502);
  const text = await response.text();
  assert.equal(text.includes(TOKEN), false);
  assert.deepEqual(JSON.parse(text), { status: 'unavailable', reason: 'upstream_schema_mismatch' });
});

test('canary values in projected success fields are rejected rather than reflected', async () => {
  for (const overrides of [
    { service_version: TOKEN },
    { api_version: BASE_URL },
    { service_version: new URL(BASE_URL).origin },
    { api_version: ALLOWED_HOST },
    { request_id: `req-${TOKEN}` },
    { source_commit: BASE_URL },
    { source_commit: ALLOWED_HOST },
  ]) {
    const response = await proxyMetaRequest({
      environment: ENVIRONMENT,
      fetchImpl: async () => upstreamJson(200, metaBody(overrides)),
    });
    await assertUnavailable(response, 502, 'upstream_schema_mismatch');
  }
});

test('printable quote and backslash token values are checked before JSON escaping', async () => {
  const token = 'SYNTHETIC_DEVICE_"TOKEN\\VALUE';
  const environment = { ...ENVIRONMENT, get DB() { return createPool(); }, WUWATERM_SITE_DEVICE_TOKEN: token };
  for (const overrides of [
    { service_version: token },
    { api_version: `v1-${token}` },
    { request_id: `req-${token}` },
  ]) {
    const response = await proxyMetaRequest({
      environment,
      fetchImpl: async () => upstreamJson(200, metaBody(overrides)),
    });
    assert.equal(response.status, 502);
    const text = await response.text();
    assert.equal(text.includes(token), false);
    assert.deepEqual(JSON.parse(text), {
      status: 'unavailable',
      reason: 'upstream_schema_mismatch',
    });
  }

  const escapedLookalike = JSON.stringify(token).slice(1, -1);
  assert.equal(escapedLookalike.includes(token), false);
  const allowed = await proxyMetaRequest({
    environment,
    fetchImpl: async () => upstreamJson(200, metaBody({
      service_version: escapedLookalike,
    })),
  });
  assert.equal(allowed.status, 200);
  assert.equal((await bodyOf(allowed)).service_version, undefined);
});

test('sensitive values colliding with numeric fields or fixed JSON keys are rejected', async () => {
  for (const token of [String(metaBody().term_count), 'api_version']) {
    const response = await proxyMetaRequest({
      environment: { ...ENVIRONMENT, WUWATERM_SITE_DEVICE_TOKEN: token },
      fetchImpl: async () => upstreamJson(200, metaBody()),
    });
    assert.equal(response.status, 502);
    const text = await response.text();
    assert.deepEqual(JSON.parse(text), {
      status: 'unavailable',
      reason: 'upstream_schema_mismatch',
    });
  }
});

test('Content-Length rejects oversized or invalid values before body consumption', async () => {
  for (const contentLength of ['65537', '-1', 'not-a-number']) {
    const response = await proxyMetaRequest({
      environment: ENVIRONMENT,
      fetchImpl: async () => new Response(JSON.stringify(metaBody()), {
        status: 200,
        headers: { 'content-type': 'application/json', 'content-length': contentLength },
      }),
    });
    await assertUnavailable(response, 502, 'upstream_response_too_large');
  }
});

test('streaming byte cap cancels an oversized response', async () => {
  let cancelled = false;
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array(40_000));
      controller.enqueue(new Uint8Array(30_000));
    },
    cancel() {
      cancelled = true;
    },
  });
  const response = await proxyMetaRequest({
    environment: ENVIRONMENT,
    fetchImpl: async () => new Response(stream, {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
  });
  assert.equal(cancelled, true);
  await assertUnavailable(response, 502, 'upstream_response_too_large');
});

test('streaming byte cap does not await a reader cancellation promise that never settles', async () => {
  const chunks = [new Uint8Array(40_000), new Uint8Array(30_000)];
  const stalled = unsettledReaderResponse(async () => (
    chunks.length ? { done: false, value: chunks.shift() } : { done: true }
  ));
  const outcome = await settleWithin(proxyMetaRequest({
    environment: ENVIRONMENT,
    fetchImpl: async () => stalled.response,
  }));
  assert.equal(stalled.wasCancelled(), true);
  assert.equal(outcome.settled, true);
  await assertUnavailable(outcome.value, 502, 'upstream_response_too_large');
});

test('concurrent browser requests remain bounded one-to-one with upstream requests', async () => {
  let calls = 0;
  const responses = await Promise.all(Array.from({ length: 20 }, async (_, index) => proxyMetaRequest({
    environment: ENVIRONMENT,
    fetchImpl: async () => {
      calls += 1;
      return upstreamJson(200, metaBody({ request_id: `req-concurrent-${index}` }));
    },
  })));
  assert.equal(calls, 20);
  assert.equal(responses.every((response) => response.status === 200), true);
});

test('route exposes only a parameterless GET and cannot read inbound request controls', () => {
  const routePath = fileURLToPath(new URL('../app/api/meta/route.ts', import.meta.url));
  const source = readFileSync(routePath, 'utf8');
  assert.match(source, /export async function GET\(\): Promise<Response>/u);
  assert.doesNotMatch(source, /\bRequest\b/u);
  assert.equal(source.includes('POST'), false);
  assert.equal(source.includes('cookies('), false);
  assert.equal(source.includes('headers('), false);
  assert.equal(source.includes('searchParams'), false);
  assert.equal(source.includes('WUWATERM_API_BASE_URL'), false);
  assert.equal(source.includes('WUWATERM_SITE_DEVICE_TOKEN'), false);
});
