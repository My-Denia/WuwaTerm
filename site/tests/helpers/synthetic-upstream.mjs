// Legacy upstream responses for the quick synthetic runtime-worker tests.
// Production artifact tests use the review-v2 responder in built-harness.mjs.
export function syntheticUpstreamFetch(upstream) {
  const target = new URL(upstream);
  const query = target.searchParams.get('q');
  const request_id = 'synthetic-correlation-1';
  if (target.pathname.endsWith('/terms')) return Response.json({ query, matches: [{ zh: '今汐', en: 'Jinhsi', category: 'character', reason: 'exact', score: 100 }], request_id });
  if (target.pathname.endsWith('/translations')) return Response.json({ text: 'Jinhsi', kind: 'exact', direction: 'en', dictionary_miss: false, request_id });
  if (target.pathname.endsWith('/reviews')) return Response.json({
    request_id,
    source_revision: 'a'.repeat(64),
    target_revision: 'b'.repeat(64),
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
}

// Standalone synthetic Worker entry. The production artifact harness instead
// intercepts outbound requests through Node fetch in built-harness.mjs.
const upstreamWorker = {
  async fetch(request) {
    return syntheticUpstreamFetch(request.url);
  },
};
export default upstreamWorker;
