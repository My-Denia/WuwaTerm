import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  containsForbiddenPassPhrase,
  exportReview,
  isCurrentVerified,
  revisionOf,
  summarizeReview,
} from '../lib/review-report.js';

function report(overrides = {}) {
  return {
    request_id: 'r1',
    source_revision: 'a'.repeat(64),
    target_revision: 'b'.repeat(64),
    rule_version: 'review-v1',
    dictionary: { schema_version: '2', source_commit: 'c', term_count: 1 },
    coverage: { evaluated: 1, not_evaluated: 1, rules: ['review.term_pair', 'review.sentence_meaning'] },
    findings: [{
      id: '0:2:今汐',
      verdict: 'verified_constraint',
      rule_id: 'review.term_pair',
      source_span: { start: 0, end: 2, text: '今汐' },
      target_span: { start: 0, end: 6, text: 'Jinhsi' },
      candidates: [],
      candidates_truncated: false,
    }],
    truncated: false,
    ...overrides,
  };
}

test('empty and zero-coverage summaries never say 全部通过', () => {
  for (const item of [
    summarizeReview(null),
    summarizeReview(report({ findings: [], coverage: { evaluated: 0, not_evaluated: 1, rules: ['review.sentence_meaning'] } })),
  ]) {
    assert.equal(containsForbiddenPassPhrase(item.headline), false);
    assert.equal(containsForbiddenPassPhrase(item.detail), false);
    assert.match(item.headline, /未通过|需核对/u);
    assert.equal(item.allPassed, false);
  }
});

test('all verified_constraint with unevaluated coverage still lists 未评估 and not 全部通过', () => {
  const summary = summarizeReview(report({
    findings: [report().findings[0], { ...report().findings[0], id: '2:4:声骸', verdict: 'verified_constraint' }],
    coverage: { evaluated: 2, not_evaluated: 1, rules: ['review.term_pair', 'review.sentence_meaning'] },
  }));
  assert.match(summary.detail, /未评估/u);
  assert.equal(containsForbiddenPassPhrase(summary.headline + summary.detail), false);
  assert.equal(summary.allPassed, false);
});

test('truncated or candidates_truncated forbids 全部通过', () => {
  const truncated = summarizeReview(report({ truncated: true }));
  const capped = summarizeReview(report({
    findings: [{ ...report().findings[0], candidates_truncated: true }],
  }));
  for (const summary of [truncated, capped]) {
    assert.match(summary.detail, /截断/u);
    assert.equal(containsForbiddenPassPhrase(summary.headline + summary.detail), false);
    assert.equal(summary.allPassed, false);
  }
});

test('paired hash mismatch yields no valid verified stamp', async () => {
  const source = '今汐';
  const target = 'Jinhsi';
  const sourceRevision = await revisionOf(source);
  const targetRevision = await revisionOf(target);
  const staleTarget = await revisionOf('Jinhsi edited');
  const current = report({ source_revision: sourceRevision, target_revision: targetRevision });
  const exported = exportReview({
    report: current,
    source,
    target: 'Jinhsi edited',
    sourceRevision,
    targetRevision: staleTarget,
    reportGeneration: 1,
    currentGeneration: 1,
  });
  assert.equal(exported.verified_stamp.valid, false);
  assert.equal(isCurrentVerified({
    report: current,
    sourceRevision,
    targetRevision: staleTarget,
    reportGeneration: 1,
    currentGeneration: 1,
  }), false);
});

test('a late generation N-1 report is not 当前已核', async () => {
  const source = '今汐';
  const target = 'Jinhsi';
  const sourceRevision = await revisionOf(source);
  const targetRevision = await revisionOf(target);
  const late = report({ source_revision: sourceRevision, target_revision: targetRevision });
  assert.equal(isCurrentVerified({
    report: late,
    sourceRevision,
    targetRevision,
    reportGeneration: 1,
    currentGeneration: 2,
  }), false);
  const exported = exportReview({
    report: late,
    source,
    target,
    sourceRevision,
    targetRevision,
    reportGeneration: 1,
    currentGeneration: 2,
  });
  assert.equal(exported.verified_stamp.valid, false);
});

test('review UI imports the report module and does not insert manuscripts as HTML', () => {
  const component = readFileSync(fileURLToPath(new URL('../app/components/review-workbench.tsx', import.meta.url)), 'utf8');
  const page = readFileSync(fileURLToPath(new URL('../app/page.tsx', import.meta.url)), 'utf8');
  const translation = readFileSync(fileURLToPath(new URL('../app/components/translation-workbench.tsx', import.meta.url)), 'utf8');
  assert.match(component, /review-report\.js/u);
  assert.match(page, /ReviewWorkbench/u);
  assert.equal(component.includes('dangerouslySetInnerHTML'), false);
  assert.equal(component.includes('innerHTML'), false);
  assert.equal(page.includes('dangerouslySetInnerHTML'), false);
  assert.match(component, /discardInFlight/u);
  // Handoff and undo state transitions are exercised by the real browser
  // regression, rather than requiring the former destructive history reset.
  assert.match(component, /parseWorkfile/u);
  assert.match(component, /reconcileChoice/u);
  assert.match(component, /forms\.includes\(finding\.target_span\.text\)/u);
  assert.match(component, /generation\.current !== mine/u);
  const limits = readFileSync(fileURLToPath(new URL('../app/limits/page.tsx', import.meta.url)), 'utf8');
  assert.match(limits, /译文审校[\s\S]{0,80}reviewsPerDay/u);
  assert.match(component, /原文 \{sourceLength\.toLocaleString\(\)\} \/ 2,000/u);
  assert.match(component, /validReport\(value, source, target\)/u);
  assert.match(translation, /const result = state\.data/u);
});
