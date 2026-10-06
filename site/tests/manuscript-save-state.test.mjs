import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import { MAX_WORKFILE_BYTES, canonicalManuscript, leaveGuardActive, makeChoice, manuscriptSaveView, parseWorkfile, recoverableWorkPresent, serializeWorkfile } from '../lib/manuscript.js';
import { candidate, fixture } from './fixtures/manuscript.mjs';

const blank = { source: '', target: '', direction: 'en', alignments: null, choices: [], history: [] };

function fullPayload() {
  const draft = fixture('今汐。', 'Jinhsi.', 'en');
  const choice = makeChoice({ source: draft.source, direction: draft.direction, alignments: null, report: draft.report, finding: draft.report.findings[0], candidate });
  const history = [{ source: draft.source, target: draft.target, direction: draft.direction, alignments: null, report: draft.report, resolutions: [] }];
  return { source: draft.source, target: draft.target, direction: draft.direction, alignments: null, choices: [choice], history };
}

test('blank workbench is empty and does not guard', () => {
  const canonical = canonicalManuscript(blank);
  assert.equal(typeof canonical, 'string');
  assert.equal(recoverableWorkPresent({ ...blank, reports: [] }), false);
  const view = manuscriptSaveView({ holdsWork: false, canonical, importedCanonical: null, downloadedCanonical: null });
  assert.equal(view, 'empty');
  assert.equal(leaveGuardActive({ view }), false);
});

test('work with no reference is page-only and guarded', () => {
  const payload = fullPayload();
  const canonical = canonicalManuscript(payload);
  assert.ok(canonical);
  const view = manuscriptSaveView({ holdsWork: true, canonical });
  assert.equal(view, 'page-only');
  assert.equal(leaveGuardActive({ view }), true);
});

test('import match wins over an identical download and is not guarded until a check is in flight', () => {
  const canonical = canonicalManuscript(fullPayload());
  const view = manuscriptSaveView({ holdsWork: true, canonical, importedCanonical: canonical, downloadedCanonical: canonical });
  assert.equal(view, 'matches-import');
  assert.equal(leaveGuardActive({ view }), false);
  assert.equal(leaveGuardActive({ view, checkInFlight: true }), true);
});

test('download match stays guarded because the write was not observed', () => {
  const canonical = canonicalManuscript(fullPayload());
  const view = manuscriptSaveView({ holdsWork: true, canonical, downloadedCanonical: canonical });
  assert.equal(view, 'matches-download');
  assert.equal(leaveGuardActive({ view }), true);
});

test('full manuscript leaves import match when the choice or the report changes, and returns when restored', () => {
  const payload = fullPayload();
  const imported = canonicalManuscript(payload);
  const matched = () => manuscriptSaveView({ holdsWork: true, canonical: canonicalManuscript(payload), importedCanonical: imported });
  assert.equal(matched(), 'matches-import');

  const originalChoice = payload.choices[0].scope;
  payload.choices[0] = { ...payload.choices[0], scope: originalChoice + '-edited' };
  assert.equal(matched(), 'diverged');
  payload.choices[0] = { ...payload.choices[0], scope: originalChoice };
  assert.equal(matched(), 'matches-import');

  const originalRequest = payload.history[0].report.request_id;
  payload.history[0] = { ...payload.history[0], report: { ...payload.history[0].report, request_id: originalRequest + '-2' } };
  assert.equal(matched(), 'diverged');
  payload.history[0] = { ...payload.history[0], report: { ...payload.history[0].report, request_id: originalRequest } };
  assert.equal(matched(), 'matches-import');
});

test('a reference that no longer matches is diverged', () => {
  const imported = canonicalManuscript(fullPayload());
  const edited = canonicalManuscript({ ...fullPayload(), target: 'Jinhsi!' });
  assert.equal(manuscriptSaveView({ holdsWork: true, canonical: edited, importedCanonical: imported }), 'diverged');
});

test('a compact import under the size cap stays the baseline when indentation would exceed it', () => {
  const hash = value => createHash('sha256').update(value).digest('hex');
  const source = '今汐。'.repeat(32);
  const target = 'Jinhsi.'.repeat(32);
  const dictionary = { schema_version: '2', source_commit: 'synthetic-fixture', term_count: 1, revision: hash('dictionary-v1') };
  const payloadFor = pad => {
    const sources = Array.from({ length: 8 }, (_, index) => ({ source_file: 'f'.repeat(pad), source_id: `s${index}` }));
    const cand = { zh: '今汐', en: 'Jinhsi', category: 'character', sources, candidate_id: hash('candidate-jinhsi') };
    const findings = Array.from({ length: 32 }, (_, index) => {
      const start = index * 3;
      const targetStart = index * 7;
      const text = '今汐';
      return {
        id: `${start}:${start + 2}:${text}`, rule_id: 'review.term_pair', verdict: 'verified_constraint',
        source_span: { start, end: start + 2, text },
        target_span: { start: targetStart, end: targetStart + 6, text: 'Jinhsi' },
        candidates: Array.from({ length: 8 }, () => cand), candidates_truncated: false,
      };
    });
    const report = {
      request_id: 'size-window', source_revision: hash(source), target_revision: hash(target),
      rule_version: 'review-v2', dictionary,
      coverage: { evaluated: findings.length, not_evaluated: 0, rules: ['review.term_pair'] },
      findings, truncated: false,
    };
    const snapshot = { source, target, direction: 'en', alignments: null, report, resolutions: [] };
    return { source, target, direction: 'en', alignments: null, choices: [], history: [snapshot, snapshot] };
  };
  const bytes = value => new TextEncoder().encode(value).length;
  let low = 1;
  let high = 4096;
  let pad = 0;
  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    const compact = bytes(JSON.stringify({ format: 'wuwaterm-manuscript-v1', ...payloadFor(mid) }));
    if (compact <= MAX_WORKFILE_BYTES) { pad = mid; low = mid + 1; }
    else high = mid - 1;
  }
  const payload = payloadFor(pad);
  const compact = JSON.stringify({ format: 'wuwaterm-manuscript-v1', ...payload });
  assert.ok(bytes(compact) <= MAX_WORKFILE_BYTES);
  const parsed = parseWorkfile(compact);
  assert.throws(() => serializeWorkfile(parsed), error => error.code === 'workfile_too_large');
  const canonical = canonicalManuscript(parsed);
  assert.equal(typeof canonical, 'string');
  assert.ok(bytes(canonical) > MAX_WORKFILE_BYTES);
  const view = manuscriptSaveView({ holdsWork: true, canonical, importedCanonical: canonical });
  assert.equal(view, 'matches-import');
  assert.equal(leaveGuardActive({ view }), false);
  const edited = canonicalManuscript({ ...parsed, target: `${parsed.target}啊` });
  assert.notEqual(edited, canonical);
  assert.equal(manuscriptSaveView({ holdsWork: true, canonical: edited, importedCanonical: canonical }), 'diverged');
  assert.equal(leaveGuardActive({ view: 'diverged' }), true);
});

test('unserializable work is guarded and is not described as a download match', () => {
  const view = manuscriptSaveView({ holdsWork: true, canonical: null, downloadedCanonical: 'stale' });
  assert.equal(view, 'not-serializable');
  assert.equal(leaveGuardActive({ view }), true);
  assert.equal(recoverableWorkPresent({ source: '\uD800', target: '', choices: [], alignments: null, reports: [] }), true);
  assert.equal(canonicalManuscript({ ...blank, source: '\uD800' }), null);
});
