import { readFileSync } from 'node:fs';
import { proxyMetaRequest } from '../../lib/wuwaterm-proxy.js';

// Admission only. proxyMetaRequest still runs validMetaBody and the browser projector.
const HOST = 'provenance-bff.invalid';
const TOKEN = 'SYNTHETIC_SITE_PROVENANCE_TOKEN_74A1C9';
const clock = 1_700_000_060;
const admission = {
  clock,
  second_key: clock,
  minute_key: Math.floor(clock / 60),
  day_key: Math.floor(clock / 86400),
  upstream_used: 1,
  translation_minute_used: 0,
  terms_used: 0,
  translation_used: 0,
  character_used: 0,
  meta_used: 1,
  review_used: 0,
};
const statement = {
  bind() {
    return statement;
  },
  async first() {
    return admission;
  },
};
const environment = {
  DB: {
    prepare() {
      return statement;
    },
  },
  WUWATERM_SHARED_POOL_ENABLED: 'true',
  WUWATERM_TRANSLATION_ENABLED: 'true',
  WUWATERM_API_ALLOWED_HOST: HOST,
  WUWATERM_API_BASE_URL: `https://${HOST}/wuwaterm-api/`,
  WUWATERM_SITE_DEVICE_TOKEN: TOKEN,
};

const upstream = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const response = await proxyMetaRequest({
  environment,
  fetchImpl: async () => new Response(JSON.stringify(upstream), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  }),
});
const text = await response.text();
process.stdout.write(JSON.stringify({ status: response.status, body: JSON.parse(text) }));
