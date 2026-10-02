import assert from 'node:assert/strict';
import test from 'node:test';

import {
  compactScalarContext,
  filterFindings,
  findingVerdictCounts,
  reconcileActiveId,
} from '../lib/review-navigation.js';

const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u;

function finding(id, verdict, start = 0) {
  return { id, verdict, source_span: { start, end: start + 1, text: 'x' } };
}

test('verdict counts come from findings and ignore coverage', () => {
  const findings = [
    finding('a', 'verified_constraint'),
    finding('b', 'confirmed_conflict'),
    finding('c', 'needs_review'),
    finding('d', 'not_evaluated'),
    finding('e', 'verified_constraint'),
  ];
  const counts = findingVerdictCounts(findings);
  assert.deepEqual(counts, {
    all: 5,
    verified_constraint: 2,
    confirmed_conflict: 1,
    needs_review: 1,
    not_evaluated: 1,
  });
  assert.notEqual(counts.not_evaluated, 7);
  assert.equal(filterFindings(findings, 'verified_constraint').map((item) => item.id).join(','), 'a,e');
  assert.equal(filterFindings(findings, 'all').length, 5);
  assert.deepEqual(filterFindings(findings, 'confirmed_conflict').map((item) => item.id), ['b']);
});

test('an empty filter clears the active id and does not invent a finding', () => {
  const findings = [finding('a', 'needs_review')];
  assert.deepEqual(filterFindings(findings, 'confirmed_conflict'), []);
  assert.equal(reconcileActiveId('a', []), null);
  assert.equal(reconcileActiveId(null, []), null);
});

test('active id stays when it is still visible and otherwise becomes the first visible id', () => {
  const visible = [finding('a', 'needs_review'), finding('b', 'needs_review')];
  assert.equal(reconcileActiveId('b', visible), 'b');
  assert.equal(reconcileActiveId('missing', visible), 'a');
  assert.equal(reconcileActiveId(null, visible), 'a');
  assert.equal(reconcileActiveId(0, visible), 'a');
});

test('compact context keeps the hit, clips by scalar, and does not split an emoji', () => {
  const hit = '今汐';
  const before = `${'前'.repeat(49)}😀`;
  const after = `${'后'.repeat(10)}尾`;
  const text = `${before}${hit}${after}`;
  const start = Array.from(before).length;
  const span = { start, end: start + Array.from(hit).length, text: hit };
  const context = compactScalarContext(text, span, 48);
  assert.equal(context.hit, hit);
  assert.equal(Array.from(context.before).length, 48);
  assert.equal(context.before.endsWith('😀'), true);
  assert.equal(context.clippedBefore, true);
  assert.equal(context.clippedAfter, false);
  assert.equal(LONE_SURROGATE.test(`${context.before}${context.hit}${context.after}`), false);

  const edge = compactScalarContext(hit, { start: 0, end: 2, text: hit }, 48);
  assert.equal(edge.before, '');
  assert.equal(edge.after, '');
  assert.equal(edge.clippedBefore, false);
  assert.equal(edge.clippedAfter, false);

  const longAfter = `${hit}${'后'.repeat(60)}`;
  const tail = compactScalarContext(longAfter, { start: 0, end: 2, text: hit }, 48);
  assert.equal(Array.from(tail.after).length, 48);
  assert.equal(tail.clippedAfter, true);
  assert.equal(tail.clippedBefore, false);
  assert.equal(compactScalarContext('今汐。', { start: 0, end: 2, text: '声骸' }, 48), null);
});
