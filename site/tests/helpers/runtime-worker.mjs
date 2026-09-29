// Synthetic upstream only. This module is not a production entrypoint.
import { proxyMetaRequest, proxyReviewRequest, proxyTermsRequest, proxyTranslationRequest } from '../../lib/wuwaterm-proxy.js';
import { poolStatus } from '../../lib/shared-pool.js';

const worker = {
  async fetch(request, environment) {
    const url = new URL(request.url);
    if (url.pathname === '/api/pool') return poolStatus(environment);
    const fetchImpl = async upstream => {
      const target = new URL(upstream); const query = target.searchParams.get('q');
      const request_id = 'synthetic-correlation-1';
      if (target.pathname.endsWith('/terms')) return Response.json({ query, matches: [{ zh: '今汐', en: 'Jinhsi', category: 'character', reason: 'exact', score: 100 }], request_id });
      if (target.pathname.endsWith('/translations')) return Response.json({ text: 'Jinhsi', kind: 'exact', direction: 'en', dictionary_miss: false, request_id });
      if (target.pathname.endsWith('/reviews')) return Response.json({
        request_id,
        source_revision: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        target_revision: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        rule_version: 'review-v1',
        dictionary: { schema_version: '2', source_commit: 'abc123', term_count: 1 },
        coverage: { evaluated: 1, not_evaluated: 1, rules: ['review.term_pair', 'review.sentence_meaning'] },
        findings: [{
          id: '0:2:今汐',
          verdict: 'verified_constraint',
          rule_id: 'review.term_pair',
          source_span: { start: 0, end: 2, text: '今汐' },
          target_span: { start: 0, end: 6, text: 'Jinhsi' },
          candidates: [{ zh: '今汐', en: 'Jinhsi', category: 'resonator', sources: [{ source_file: 'RoleInfo.json', source_id: 'RoleInfo_1304_Name' }] }],
          candidates_truncated: false,
        }],
        truncated: false,
      });
      return Response.json({ api_version: 'v1', service_version: '0.4.1', term_count: 12345, schema_version: '3.6', source_profile: 'synthetic', source_commit: 'abc123', llm_configured: true, request_id });
    };
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
