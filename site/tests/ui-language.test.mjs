import assert from 'node:assert/strict';
import test from 'node:test';

import { fill, msg, summaryHeadline, summarySentence } from '../lib/messages.ts';
import { htmlLang, numberLocale, ogLocale, uiLanguageOrZh } from '../lib/ui-language.ts';
import { summarizeReview, summarizeReviewParts } from '../lib/review-report.js';
import { compareReports, parseWorkfile } from '../lib/manuscript.js';

const CJK = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/u;
// English copy may legitimately show Chinese only where a Chinese term itself
// is the content: the zh button label, the decorative hero eyebrow, and the
// zh-term lookup example. Unit suffixes are legitimately empty in English.
const EN_CJK_ALLOWLIST = new Set(['language.zh', 'hero.eyebrow', 'terms.placeholder', 'privacy.s2p3']);
const EN_EMPTY_ALLOWLIST = new Set(['pool.termsUnit', 'pool.charactersUnit']);

function shape(value) {
  if (Array.isArray(value)) return value.map(shape);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, shape(value[key])]));
  }
  return typeof value;
}

function leaves(value, prefix = '', out = []) {
  if (Array.isArray(value)) value.forEach((item, i) => leaves(item, `${prefix}[${i}]`, out));
  else if (value && typeof value === 'object') {
    for (const [key, item] of Object.entries(value)) leaves(item, prefix ? `${prefix}.${key}` : key, out);
  } else out.push([prefix, value]);
  return out;
}

test('zh and en catalogs share the exact same key structure', () => {
  assert.deepEqual(shape(msg('en')), shape(msg('zh')));
});

test('every en leaf is nonempty and free of untranslated CJK outside the term allowlist', () => {
  for (const [path, value] of leaves(msg('en'))) {
    assert.equal(typeof value, 'string', `en ${path} must be a string`);
    if (!EN_EMPTY_ALLOWLIST.has(path)) {
      assert.equal(value.length > 0, true, `en ${path} must not be empty`);
    }
    if (!EN_CJK_ALLOWLIST.has(path) && !path.startsWith('productNotes[') && !path.startsWith('review.guide[')) {
      assert.equal(CJK.test(value), false, `en ${path} leaks CJK copy: ${value}`);
    }
  }
});

test('unknown languages fall back to zh and helpers map consistently', () => {
  assert.equal(msg('fr'), msg('zh'));
  assert.equal(uiLanguageOrZh('en'), 'en');
  assert.equal(uiLanguageOrZh('fr'), 'zh');
  assert.equal(uiLanguageOrZh(undefined), 'zh');
  assert.equal(ogLocale('zh'), 'zh_CN');
  assert.equal(ogLocale('en'), 'en_US');
  assert.equal(htmlLang('zh'), 'zh-CN');
  assert.equal(htmlLang('en'), 'en');
  assert.equal(numberLocale('zh'), 'zh-CN');
  assert.equal(numberLocale('en'), 'en-US');
});

test('fill() interpolates params and leaves unknown tokens visible', () => {
  assert.equal(fill('a {n} b', { n: 3 }), 'a 3 b');
  assert.equal(fill('a {x} {y}', { x: 's' }), 'a s {y}');
});

function reportFixture(overrides = {}) {
  return {
    request_id: 'r1',
    source_revision: 'a'.repeat(64),
    target_revision: 'b'.repeat(64),
    rule_version: 'review-v2',
    dictionary: { schema_version: '2', source_commit: 'c0ffee', term_count: 5, revision: 'd'.repeat(64) },
    coverage: { evaluated: 2, not_evaluated: 1, rules: ['review.term_pair'] },
    findings: [
      { id: '0:2:今汐', rule_id: 'review.term_pair', verdict: 'verified_constraint', source_span: { start: 0, end: 2, text: '今汐' }, target_span: null, candidates: [], candidates_truncated: false },
      { id: '3:5:声骸', rule_id: 'review.term_pair', verdict: 'needs_review', source_span: { start: 3, end: 5, text: '声骸' }, target_span: null, candidates: [], candidates_truncated: false },
    ],
    truncated: false,
    ...overrides,
  };
}

test('the localized zh summary sentence is identical to summarizeReview output', () => {
  const reports = [
    reportFixture(),
    reportFixture({ findings: [], coverage: { evaluated: 0, not_evaluated: 0, rules: [] } }),
    reportFixture({ truncated: true }),
    reportFixture({ findings: reportFixture().findings.map((f) => ({ ...f, verdict: 'confirmed_conflict' })) }),
  ];
  for (const report of reports) {
    const canonical = summarizeReview(report);
    const parts = summarizeReviewParts(report);
    assert.equal(summarySentence(parts, 'zh'), canonical.detail);
    assert.equal(summaryHeadline(parts, 'zh'), canonical.headline);
  }
  assert.equal(summarizeReviewParts(null), null);
  assert.equal(summarySentence(summarizeReviewParts(reportFixture()), 'en').includes('verified constraints 1'), true);
});

test('reconcile/compare/error codes are all covered by both catalogs', () => {
  const zhReasons = msg('zh').choiceReasons;
  const enReasons = msg('en').choiceReasons;
  const zhComparison = msg('zh').comparisonReasons;
  const enComparison = msg('en').comparisonReasons;
  const zhErrors = msg('zh').errors;
  const enErrors = msg('en').errors;

  const snapshot = (trusted) => ({
    source: '今汐与声骸', target: 'Jinhsi and Echo', direction: 'en', alignments: null,
    report: reportFixture(), trusted, signature: trusted ? 's' : '', resolutions: null,
  });
  const before = snapshot(true);
  const after = { ...snapshot(true), report: reportFixture({ dictionary: { schema_version: '9', source_commit: 'zz', term_count: 6, revision: 'e'.repeat(64) } }) };
  const codes = new Set();
  for (const group of Object.values(compareReports(before, after))) for (const item of group) codes.add(item.code);
  for (const code of codes) {
    assert.equal(typeof zhComparison[code], 'string', `comparison code ${code} missing zh`);
    assert.equal(typeof enComparison[code], 'string', `comparison code ${code} missing en`);
  }
  // compareReports(null, ...) returns the empty result shape.
  for (const code of ['imported_history_unverified', 'decision_changed_or_unknown', 'scope_position_incomparable', 'resolved_same_constraint', 'still_needs_review', 'new_finding']) {
    assert.equal(typeof zhComparison[code], 'string');
    assert.equal(typeof enComparison[code], 'string');
  }

  const errorCodes = new Set();
  for (const content of ['', '{', '[]', JSON.stringify({ format: 'other' }), 'x'.repeat(1_048_577)]) {
    try { parseWorkfile(content); } catch (cause) { errorCodes.add(cause.code); }
  }
  for (const code of errorCodes) {
    assert.equal(typeof zhErrors[code], 'string', `error code ${code} missing zh`);
    assert.equal(typeof enErrors[code], 'string', `error code ${code} missing en`);
  }
  for (const code of ['direction_changed', 'source_block_changed', 'scope_changed', 'awaiting_fresh_basis', 'basis_changed', 'finding_missing_or_truncated', 'imported_not_a_term', 'user_not_a_term', 'candidate_gone', 'candidate_partial_visible', 'position_basis_match', 'duplicate_position']) {
    assert.equal(typeof zhReasons[code], 'string', `choice code ${code} missing zh`);
    assert.equal(typeof enReasons[code], 'string', `choice code ${code} missing en`);
  }
});
