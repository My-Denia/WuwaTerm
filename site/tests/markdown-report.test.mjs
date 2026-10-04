import assert from 'node:assert/strict';
import test from 'node:test';

import { renderMarkdownReport, markdownStructure } from '../lib/markdown-report.ts';
import { compareReports, makeChoice } from '../lib/manuscript.js';
import { candidate, echo, fixture } from './fixtures/manuscript.mjs';
import { msg } from '../lib/messages.ts';

function snapshotFor(overrides = {}) {
  const base = fixture('今汐。\n声骸。', 'Jinhsi.\nEcho.', 'en');
  return { source: base.source, target: base.target, direction: 'en', trusted: true, signature: 's', alignments: null,
    report: base.report, resolutions: null, ...overrides };
}

function inputFor(latest, { previous = null, currency = 'current', choices = [], reconciled = [], comparison = null, draft } = {}) {
  return {
    draft: draft ?? { source: latest?.source ?? '', target: latest?.target ?? '', direction: 'en' },
    latest, previous, currency, choices, reconciled, comparison,
  };
}

test('full-fidelity export renders basis, coverage, every finding, candidates, sources and decisions', () => {
  const latest = snapshotFor({ resolutions: [{ mention_id: '0:2:今汐', choice: 'official_pair', candidate_id: candidate.candidate_id }] });
  const markdown = renderMarkdownReport(inputFor(latest), 'zh');
  // Basis comes from the report itself, never an app version.
  assert.equal(markdown.includes('`review-v2`'), true);
  assert.equal(markdown.includes('`synthetic-fixture`'), true);
  assert.equal(markdown.includes('`fixture-request`'), true);
  assert.equal(markdown.includes(latest.report.source_revision), true);
  assert.equal(markdown.includes(latest.report.target_revision), true);
  // Coverage keeps true semantics.
  assert.equal(markdown.includes('已评估：2'), true);
  assert.equal(markdown.includes('未评估：1'), true);
  assert.equal(markdown.includes('review.term_pair'), true);
  // Every finding with its candidate pairs and source records.
  for (const term of ['今汐', '声骸']) {
    assert.equal(markdown.includes('`' + term + '`'), true, `missing finding ${term}`);
  }
  assert.equal(markdown.includes('`Jinhsi`'), true);
  assert.equal(markdown.includes('`Echo`'), true);
  assert.equal(markdown.includes('`Fixture.json`'), true);
  assert.equal(markdown.includes('`fixture_jinhsi`'), true);
  assert.equal(markdown.includes('术语发现（2）'), true);
  // Submitted decisions are listed.
  assert.equal(markdown.includes('采用官方词对 #1'), true);
  // Currency wording.
  assert.equal(markdown.includes(msg('zh').md.currencyCurrent), true);
  assert.equal(markdown.includes(msg('zh').md.notCertifiedLine), true);
  // Chinese terms survive verbatim; interface labels localized for en too.
  const english = renderMarkdownReport(inputFor(latest), 'en');
  assert.equal(english.includes('`今汐`'), true);
  assert.equal(english.includes(msg('en').md.currencyCurrent), true);
  assert.equal(english.includes('Term findings (2)'), true);
});

test('zero findings and zero coverage keep their true semantics instead of a pass claim', () => {
  const base = fixture('今汐。', 'Jinhsi.', 'en');
  const empty = { ...base, report: { ...base.report, findings: [], coverage: { evaluated: 0, not_evaluated: 1, rules: ['review.term_pair'] } } };
  const latest = { source: empty.source, target: empty.target, direction: 'en', report: empty.report, resolutions: null };
  const markdown = renderMarkdownReport(inputFor(latest), 'zh');
  assert.equal(markdown.includes(msg('zh').md.zeroCoverageLine), true);
  assert.equal(markdown.includes(msg('zh').md.zeroFindingsLine), true);
  assert.equal(markdown.includes('全部通过'), false);
});

test('truncation flags are surfaced, not silently dropped', () => {
  const base = fixture('今汐。', 'Jinhsi.', 'en');
  const truncated = { ...base, report: { ...base.report, truncated: true,
    findings: base.report.findings.map(f => ({ ...f, candidates_truncated: true })) } };
  const latest = { source: truncated.source, target: truncated.target, direction: 'en', report: truncated.report, resolutions: null };
  const markdown = renderMarkdownReport(inputFor(latest), 'zh');
  assert.equal(markdown.includes(msg('zh').md.truncatedLine), true);
  assert.equal(markdown.includes(msg('zh').md.candidatesTruncatedLine), true);
});

test('currency distinguishes current, stale-edited and imported-untrusted reports', () => {
  const latest = snapshotFor();
  for (const [currency, needle] of [['current', msg('zh').md.currencyCurrent], ['stale', msg('zh').md.currencyStale], ['imported', msg('zh').md.currencyImported]]) {
    const markdown = renderMarkdownReport(inputFor(latest, { currency }), 'zh');
    assert.equal(markdown.includes(needle), true, currency);
    assert.equal(markdown.includes('需要重新核对') || currency !== 'stale', true);
  }
  const without = renderMarkdownReport(inputFor(null, { currency: 'none' }), 'zh');
  assert.equal(without.includes(msg('zh').md.noReportLine), true);
  assert.equal(without.includes('fixture-request'), false);
});

test('local choices list identity, basis and honest status with localized reasons', () => {
  const latest = snapshotFor();
  const choice = makeChoice({ source: latest.source, direction: 'en', alignments: null, report: latest.report, finding: latest.report.findings[0], candidate });
  const markdown = renderMarkdownReport(inputFor(latest, {
    currency: 'stale', choices: [choice],
    reconciled: [{ status: 'pending', reason: 'zh-canonical', code: 'awaiting_fresh_basis' }],
  }), 'en');
  assert.equal(markdown.includes('`今汐`'), true);
  assert.equal(markdown.includes(msg('en').choiceReasons.awaiting_fresh_basis), true);
  assert.equal(markdown.includes(msg('en').md.choicePending), true);
  assert.equal(markdown.includes(choice.basis.dictionary.revision), true);
});

test('comparison groups localize via codes and keep incomparable semantics', () => {
  const decision = [{ mention_id: '3:5:声骸', choice: 'official_pair', candidate_id: echo.candidate_id }];
  const previous = snapshotFor({ report: { ...fixture('今汐。\n声骸。', 'Jinhsi.\nWrong.', 'en').report }, resolutions: decision });
  const latest = snapshotFor({ resolutions: decision });
  const comparison = compareReports(previous, latest);
  const markdown = renderMarkdownReport(inputFor(latest, { previous, currency: 'current', comparison }), 'zh');
  assert.equal(markdown.includes(msg('zh').comparisonReasons.resolved_same_constraint), true);
  assert.equal(markdown.includes(msg('zh').md.comparisonNote), true);
});

test('markdown agrees with the result JSON export on currency, findings and verdict counts', () => {
  const latest = snapshotFor();
  const input = inputFor(latest, { currency: 'current' });
  const markdown = renderMarkdownReport(input, 'zh');
  const resultJson = { format: 'wuwaterm-result-v2', status: 'current_terminology_check',
    report_is_current: true, report: { report: latest.report } };
  assert.equal(input.currency === 'current', resultJson.report_is_current);
  assert.equal(markdown.includes('术语发现（2）'), true);
  assert.equal(resultJson.report.report.findings.length, 2);
  assert.equal(markdown.includes('约束已核 2'), true);
  assert.equal(resultJson.report.report.findings.filter(f => f.verdict === 'verified_constraint').length, 2);
});

const POISONS = [
  '# Forged heading',
  '## Another heading',
  '```',
  '````',
  '`code`',
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(2)>',
  '[click](https://evil.invalid/a)',
  '![image](https://evil.invalid/b.png)',
  'https://evil.invalid/autolink',
  'Setext\n===',
  'A\nB\nC',
];

test('user text cannot forge structure, links or HTML outside code regions', () => {
  const poison = POISONS.join('\n');
  const base = fixture('今汐。', 'Jinhsi.', 'en');
  const report = JSON.parse(JSON.stringify(base.report));
  report.findings.push({
    id: `3:${3 + poison.length}:${poison}`,
    rule_id: poison, verdict: 'needs_review',
    source_span: { start: 3, end: 3 + Array.from(poison).length, text: poison },
    target_span: { start: 0, end: 1, text: '[x](https://evil.invalid/t)' },
    candidates: [{ candidate_id: 'f'.repeat(64), zh: poison, en: '```', category: '<b>cat</b>', sources: [{ source_file: poison, source_id: '![i](https://evil.invalid/s)' }] }],
    candidates_truncated: true,
  });
  const latest = { source: '今汐。' + poison, target: 'Jinhsi.', direction: 'en', report, resolutions: null };
  const markdown = renderMarkdownReport(inputFor(latest, { currency: 'imported' }), 'zh');
  const structure = markdownStructure(markdown);
  assert.equal(structure.links, 0, `links leaked: ${JSON.stringify(structure)}`);
  assert.equal(structure.htmlTags, 0, `html leaked: ${JSON.stringify(structure)}`);
  // Only serializer-emitted headings exist; forged ones stayed literal.
  const forged = structure.headings.filter(h => h.includes('Forged') || h.includes('Another'));
  assert.deepEqual(forged, []);
  assert.ok(structure.headings.length >= 8, 'serializer headings present');
  // The poison is still present verbatim for readability.
  assert.equal(markdown.includes('alert(1)'), true);
  assert.equal(markdown.includes('https://evil.invalid/autolink'), true);
  assert.equal(markdown.includes('<b>cat</b>'), true);
});

test('the fence grows past the longest backtick run in the content', () => {
  const latest = { source: 'a ``` b ```` c', target: 'plain', direction: 'en',
    report: fixture('a', 'b', 'en').report, resolutions: null };
  const markdown = renderMarkdownReport(inputFor(latest), 'zh');
  const lines = markdown.split('\n');
  const fenceIndexes = lines.map((line, i) => (/^`{3,}$/.test(line) ? i : -1)).filter(i => i >= 0);
  assert.equal(fenceIndexes.length >= 4, true);
  assert.equal(lines[fenceIndexes[0]], '`````', 'source fence must exceed the ```` run inside');
  assert.equal(markdown.includes('a ``` b ```` c'), true);
});

test('duplicate-term occurrences each get their own finding entry', () => {
  const base = fixture('今汐与今汐。', 'Jinhsi and Jinhsi.', 'en');
  assert.equal(base.report.findings.length, 2);
  const latest = { source: base.source, target: base.target, direction: 'en', report: base.report, resolutions: null };
  const markdown = renderMarkdownReport(inputFor(latest), 'zh');
  assert.equal(markdown.includes('术语发现（2）'), true);
  assert.equal((markdown.match(/`今汐`/gu) ?? []).length >= 3, true);
});
