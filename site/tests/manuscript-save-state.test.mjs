import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeChoice, canonicalManuscript, leaveGuardActive, manuscriptSaveView, recoverableWorkPresent } from '../lib/manuscript.js';
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

test('unserializable work is guarded and is not described as a download match', () => {
  const view = manuscriptSaveView({ holdsWork: true, canonical: null, downloadedCanonical: 'stale' });
  assert.equal(view, 'not-serializable');
  assert.equal(leaveGuardActive({ view }), true);
  assert.equal(recoverableWorkPresent({ source: '\uD800', target: '', choices: [], alignments: null, reports: [] }), true);
  assert.equal(canonicalManuscript({ ...blank, source: '\uD800' }), null);
});
