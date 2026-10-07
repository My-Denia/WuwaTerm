import test from 'node:test';
import assert from 'node:assert/strict';
import { fragmentOccurrences, spliceOccurrence } from '../lib/target-revision.js';

test('literal duplicates and overlaps retain every distinct raw span', () => {
  assert.deepEqual(fragmentOccurrences('😀 banana banana', 'ana').map(s => s.start), [3, 5, 10, 12]);
  const spans = fragmentOccurrences(' sound shell / sound shell ', 'sound shell');
  assert.equal(spans.length, 2);
  assert.equal(spliceOccurrence(' sound shell / sound shell ', 'sound shell', spans[1], 'Echo').target, ' sound shell / Echo ');
});
test('display newlines map back to raw CRLF and CR; bytes outside interval survive', () => {
  const target = ' 😀\r\nsound\rshell\r\n sound\nshell ';
  const spans = fragmentOccurrences(target, 'sound\nshell');
  assert.deepEqual(spans, [{ start: 4, end: 15, text: 'sound\rshell' }, { start: 18, end: 29, text: 'sound\nshell' }]);
  assert.equal(spliceOccurrence(target, 'sound\nshell', spans[0], 'Echo').target, ' 😀\r\nEcho\r\n sound\nshell ');
});
test('empty, whitespace, invalid scalar and nonliteral fragments never select', () => {
  for (const fragment of ['', ' \n ', 'ECHO', '\ud800', '\ude00']) assert.deepEqual(fragmentOccurrences('😀 Echo', fragment), []);
  assert.deepEqual(fragmentOccurrences('\ud800Echo', 'Echo'), []);
  assert.equal(fragmentOccurrences(' Echo ', ' Echo ')[0].text, ' Echo ');
});
test('single splice rejects stale/forged span, absent explicit selection, no-op and length excess', () => {
  const target = 'x'.repeat(1999) + 'a';
  const span = fragmentOccurrences(target, 'a')[0];
  assert.equal(spliceOccurrence(target, 'a', span, 'bb').error, 'revisionTooLong');
  assert.equal(spliceOccurrence(target, 'a', span, 'b').target.length, 2000);
  assert.equal(spliceOccurrence('Echo', 'Echo', fragmentOccurrences('Echo', 'Echo')[0], 'Echo').error, 'revisionNoop');
  assert.equal(spliceOccurrence('Echo', 'Echo', null, '声骸').error, 'revisionSelect');
  assert.equal(spliceOccurrence('Echo', 'Echo', { start: 0, end: 0, text: '' }, '声骸').error, 'revisionSelect');
  assert.equal(spliceOccurrence('Echo', 'Echo', { start: 1, end: 4, text: 'cho' }, '声骸').error, 'revisionSelect');
  assert.equal(spliceOccurrence('Echo', 'Echo', fragmentOccurrences('Echo', 'Echo')[0], '\ud800').error, 'revisionInvalid');
});
