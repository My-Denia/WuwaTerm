// Synthetic upstream only. This module is not a production entrypoint.
import { proxyMetaRequest, proxyReviewRequest, proxyTermsRequest, proxyTranslationRequest } from '../../lib/wuwaterm-proxy.js';
import { poolStatus } from '../../lib/shared-pool.js';
import { syntheticUpstreamFetch } from './synthetic-upstream.mjs';

const worker = {
  async fetch(request, environment) {
    const url = new URL(request.url);
    if (url.pathname === '/api/pool') return poolStatus(environment);
    const fetchImpl = syntheticUpstreamFetch;
    if (url.pathname === '/api/meta') return proxyMetaRequest({ environment, fetchImpl });
    if (url.pathname === '/api/terms') return proxyTermsRequest({ environment, query: url.searchParams.get('q'), fetchImpl });
    if (url.pathname === '/api/translations') {
      const input = await request.json();
      if (input.text === '等待测试') await new Promise(resolve => setTimeout(resolve, 5000));
      return proxyTranslationRequest({ environment, input, fetchImpl });
    }
    if (url.pathname === '/api/reviews') {
      const input = await request.json();
      return proxyReviewRequest({ environment, input, fetchImpl });
    }
    return new Response(JSON.stringify({ status: 'unavailable', reason: 'not_found' }), {
      status: 404,
      headers: {
        'content-type': 'application/json; charset=utf-8',
        'cache-control': 'no-store',
        'x-robots-tag': 'noindex, nofollow, noarchive',
      },
    });
  },
};
export default worker;
