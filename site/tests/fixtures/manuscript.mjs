import { createHash } from 'node:crypto';
export const hash = value => createHash('sha256').update(value).digest('hex');
/** Stand-in identity for a fresh review-v2 API report. Historical fixtures omit it. */
export const FRESH_MATCHER_REVISION = 'ab'.repeat(32);
export const freshApiReport = report => ({ ...report, matcher_revision: FRESH_MATCHER_REVISION });
export const dictionary = { schema_version: '2', source_commit: 'synthetic-fixture', term_count: 2, revision: hash('dictionary-v1') };
export const candidate = { zh: '今汐', en: 'Jinhsi', category: 'character', sources: [{ source_file: 'Fixture.json', source_id: 'fixture_jinhsi' }], candidate_id: hash('candidate-jinhsi') };
export const echo = { zh: '声骸', en: 'Echo', category: 'item', sources: [{ source_file: 'Fixture.json', source_id: 'fixture_echo' }], candidate_id: hash('candidate-echo') };
export function fixture(source = '今汐。\n声骸。', target = 'Jinhsi.\nEcho.', direction = 'en') {
  const findings = [];
  for (const c of [candidate, echo]) {
    const needle = direction === 'en' ? c.zh : c.en;
    const expected = direction === 'en' ? c.en : c.zh;
    let offset = source.indexOf(needle);
    while (offset >= 0) {
      const start = Array.from(source.slice(0, offset)).length;
      const end = start + Array.from(needle).length;
      const targetOffset = target.indexOf(expected);
      const targetStart = Array.from(target.slice(0, targetOffset)).length;
      findings.push({ id: `${start}:${end}:${needle}`, source_span: { start, end, text: needle },
        target_span: targetOffset < 0 ? null : { start: targetStart, end: targetStart + Array.from(expected).length, text: expected },
        verdict: targetOffset < 0 ? 'needs_review' : 'verified_constraint', rule_id: 'review.term_pair', candidates: [c], candidates_truncated: false });
      offset = source.indexOf(needle, offset + needle.length);
    }
  }
  findings.sort((a,b) => a.source_span.start - b.source_span.start);
  return { source, target, direction, alignments: null, trusted: true, resolutions: [],
    report: { request_id: 'fixture-request', source_revision: hash(source), target_revision: hash(target), rule_version: 'review-v2', dictionary,
      coverage: { evaluated: findings.length, not_evaluated: 1, rules: ['review.term_pair', 'review.sentence_meaning'] }, findings, truncated: false } };
}
