import assert from 'node:assert/strict';
import test from 'node:test';
import { MAX_WORKFILE_BYTES, basisOf, compareReports, makeChoice, mentionId, parseWorkfile, reconcileChoice, recoverSpan, serializeWorkfile, validAlignments, validReport } from '../lib/manuscript.js';
import { fixture, hash } from './fixtures/manuscript.mjs';
const choiceFor = (s, i = 0, c = s.report.findings[i].candidates[0]) => makeChoice({ ...s, finding: s.report.findings[i], candidate: c });
const clean = s => { const { source, target, direction, alignments, report, resolutions } = s; return { source, target, direction, alignments, report, resolutions }; };
const fileFor = s => ({ ...clean(s), choices: s.report.findings.map((_, i) => choiceFor(s, i)), history: [clean(s)] });
const save = s => { const x = fileFor(s); delete x.report; return serializeWorkfile(x); };

test('local workfile roundtrip preserves Unicode, direction, per-occurrence decisions and history without trusted status', () => {
  const s = fixture('😀今汐。\n今汐。', '😀Jinhsi.\nJinhsi.');
  const restored = parseWorkfile(save(s));
  assert.equal(restored.source, s.source); assert.equal(restored.target, s.target); assert.equal(restored.direction, 'en');
  assert.equal(restored.choices.length, 2); assert.notEqual(restored.choices[0].source_span.start, restored.choices[1].source_span.start);
  assert.equal('trusted' in restored.history[0], false); assert.equal('status' in restored.choices[0], false);
});
test('imported hash/pass is history only: first check pending, fresh basis restores intent but not prior certification', () => {
  const s = fixture(); const restored = parseWorkfile(save(s)); const c = restored.choices[0];
  const fake = { ...s, trusted: false, report: { ...s.report, source_revision: '0'.repeat(64) } };
  assert.equal(reconcileChoice(c, s, fake, true).status, 'pending');
  assert.equal(reconcileChoice(c, s, s, true).status, 'applicable');
  assert.equal(compareReports(fake, s).resolved.length, 0);
  assert.equal(compareReports(fake, s).incomparable.length, 2);
});
test('unique unchanged block recovers; edited, deleted or duplicate blocks never migrate', () => {
  const s = fixture(); const span = s.report.findings[0].source_span;
  assert.deepEqual(recoverSpan(s.source, span, '前言\n' + s.source), { start: 3, end: 5, text: '今汐' });
  assert.equal(recoverSpan(s.source, span, '今汐来了。\n声骸。'), null);
  assert.equal(recoverSpan(s.source, span, '声骸。'), null);
  assert.equal(recoverSpan(s.source, span, '今汐。\n今汐。'), null);
  const repeated = fixture('今汐。\n今汐。', 'Jinhsi.');
  assert.equal(recoverSpan(repeated.source, repeated.report.findings[0].source_span, '标题\n'+repeated.source), null);
});
test('source edit requires new source-compatible evidence; unchanged block then retains choice', () => {
  const s = fixture(); const c = choiceFor(s); const next = fixture('前言\n'+s.source, s.target);
  assert.equal(reconcileChoice(c, next, s).status, 'pending');
  assert.equal(reconcileChoice(c, next, next).status, 'applicable');
  assert.equal(reconcileChoice(c, next, next).resolution.mention_id, '3:5:今汐');
});
test('target-only edits retain source intent but direction, content basis, candidate identity and mapping changes require confirmation', () => {
  const s = fixture(); const c = choiceFor(s);
  assert.equal(reconcileChoice(c, { ...s, target: 'Jinhsi arrived.\nEcho.' }, s).status, 'applicable');
  assert.equal(reconcileChoice(c, { ...s, direction: 'zh' }, s).status, 'pending');
  const changed = structuredClone(s); changed.report.dictionary.revision = hash('new same-count dictionary');
  assert.equal(reconcileChoice(c, s, changed).status, 'pending');
  const changedRule = structuredClone(s); changedRule.report.rule_version = 'review-v3';
  assert.equal(reconcileChoice(c, s, changedRule).status, 'pending');
  const missingCandidate = structuredClone(s); missingCandidate.report.findings[0].candidates[0].candidate_id = hash('different category');
  assert.equal(reconcileChoice(c, s, missingCandidate).status, 'pending');
  assert.equal(reconcileChoice(c, { ...s, alignments: [] }, s).status, 'pending');
});
test('imported not-a-term always requires new user confirmation; same-session user decision is never official', () => {
  const s = fixture(); const c = choiceFor(s, 0, null);
  assert.equal(reconcileChoice(c, s, s, true).status, 'pending');
  assert.equal(reconcileChoice(c, s, s, false).resolution.choice, 'not_a_term');
  assert.equal(c.candidate, null);
});
test('stale/same-count basis, missing findings and truncation block recovered choice', () => {
  const s = fixture(); const c = choiceFor(s);
  for (const report of [{ ...s.report, findings: [] }, { ...s.report, truncated: true }]) {
    assert.equal(reconcileChoice(c, s, { ...s, report }).status, 'pending');
  }
});
test('only positive comparable checks resolve; disappearance, omission, basis change and untrusted history cannot', () => {
  const old = fixture(undefined, 'Wrong.\nEcho.'); const now = fixture();
  assert.equal(compareReports(old, now).resolved.length, 1);
  const gone = { ...now, report: { ...now.report, findings: now.report.findings.slice(1) } };
  assert.equal(compareReports(old, gone).resolved.length, 0); assert.equal(compareReports(old, gone).incomparable.length, 1);
  const omitted = structuredClone(now); omitted.report.findings[0].verdict = 'not_evaluated';
  assert.equal(compareReports(old, omitted).resolved.length, 0);
  const changed = structuredClone(now); changed.report.dictionary.revision = hash('changed');
  assert.equal(compareReports(old, changed).resolved.length, 0); assert.equal(compareReports(old, changed).incomparable.length, 2);
  assert.equal(compareReports({ ...old, trusted: false }, now).resolved.length, 0);
  assert.equal(compareReports(old, old).pending.length, 1);
  assert.equal(compareReports({ ...old, report: { ...old.report, findings: [] } }, now).new.length, 2);
});
test('malformed workfiles reject before replacing current draft: version, fields, spans, duplicate choices, surrogates, size and depth', () => {
  const s = fixture(); const original = save(s); const base = JSON.parse(original);
  for (const mutate of [x => x.format = 'future-v9', x => x.direction = 'auto', x => x.verified_stamp = { valid: true },
    x => x.choices[0].source_span.end = 9000, x => x.choices.push(x.choices[0]), x => x.source = '\ud800',
    x => x.history[0].report.findings[0].target_span.text = 'wrong', x => x.choices[0].candidate.candidate_id = '0']) {
    const x = structuredClone(base); mutate(x); assert.throws(() => parseWorkfile(JSON.stringify(x)));
  }
  assert.throws(() => parseWorkfile('{"__proto__":{"polluted":true}}'));
  assert.equal({}.polluted, undefined);
  assert.throws(() => parseWorkfile(' '.repeat(MAX_WORKFILE_BYTES + 1)));
  assert.throws(() => parseWorkfile('['.repeat(100)+'0'+']'.repeat(100)));
  assert.equal(save(s), original);
});
test('alignment strict spans reject overlap/reversal/out-of-bounds/boolean/mismatched text; omission and explicit empty valid', () => {
  const source = '今汐。声骸。'; const target = 'Jinhsi. Echo.';
  const first = { source: { start: 0, end: 3, text: '今汐。' }, target: { start: 0, end: 7, text: 'Jinhsi.' } };
  const second = { source: { start: 3, end: 6, text: '声骸。' }, target: null };
  assert.equal(validAlignments([first, second], source, target), true);
  assert.equal(validAlignments([], source, target), true);
  for (const items of [[first, first], [second, first], [{ ...first, source: { ...first.source, end: 99 } }],
    [{ ...first, source: { ...first.source, start: false } }], [{ ...first, target: { ...first.target, text: 'Wrong' } }],
    Array(65).fill(first)]) assert.equal(validAlignments(items, source, target), false);
});
test('report validator accepts exact v2 and rejects corrupted provenance/span shape', () => {
  const s = fixture(); assert.equal(validReport(s.report, s.source, s.target), true);
  assert.equal(validReport({ ...s.report, injected: true }, s.source, s.target), false);
  assert.equal(validReport({ ...s.report, dictionary: { ...s.report.dictionary, revision: 'fake' } }, s.source, s.target), false);
  assert.equal(basisOf(s.report).dictionary.revision.length, 64);
});
test('a kept finding whose saved basis omits matcher revision must be confirmed again', () => {
  const historical = fixture();
  const fresh = structuredClone(historical);
  fresh.report.matcher_revision = 'a'.repeat(64);
  const oldChoice = choiceFor(historical);
  assert.equal(Object.hasOwn(oldChoice.basis, 'matcher_revision'), false);
  assert.equal(validReport(historical.report, historical.source, historical.target), true);
  assert.equal(validReport(fresh.report, fresh.source, fresh.target), true);
  const restored = parseWorkfile(save(historical));
  assert.equal(restored.choices.length, historical.report.findings.length);
  assert.equal(Object.hasOwn(restored.choices[0].basis, 'matcher_revision'), false);
  const pending = reconcileChoice(oldChoice, fresh, fresh);
  assert.equal(pending.status, 'pending');
  assert.equal(pending.code, 'basis_changed');
  assert.ok(fresh.report.findings.some(finding => finding.id === mentionId(oldChoice.source_span)));
  const current = reconcileChoice(choiceFor(fresh), fresh, fresh);
  assert.equal(current.status, 'applicable');
  assert.equal(compareReports(historical, fresh).resolved.length, 0);
});

test('missing dictionary provenance never auto-restores intent or resolves a finding', () => {
  const before = fixture(undefined, 'Wrong.\nEcho.'); const after = fixture();
  for (const field of ['schema_version','source_commit']) for (const value of [null, '']) {
    const old = structuredClone(before); const now = structuredClone(after);
    old.report.dictionary[field] = value; now.report.dictionary[field] = value;
    const c = choiceFor(old);
    assert.equal(reconcileChoice(c, now, now).status, 'pending');
    assert.equal(compareReports(old, now).resolved.length, 0);
    assert.equal(compareReports(old, now).incomparable.length, 2);
  }
});
test('comparison recovers a unique unchanged block and reports current location, but edited/duplicate blocks stay incomparable', () => {
  const before = fixture(undefined, 'Wrong.\nEcho.');
  const after = fixture('前言\n'+before.source, 'Preface.\nJinhsi.\nEcho.');
  assert.equal(compareReports(before, after).resolved.length, 1);
  assert.match(compareReports(before, after).resolved[0].label, /原文 4/u);
  for (const source of ['今汐来了。\n声骸。','今汐。\n今汐。\n声骸。']) {
    const next = fixture(source, 'Jinhsi.\nJinhsi.\nEcho.');
    assert.equal(compareReports(before, next).resolved.length, 0);
    assert.ok(compareReports(before, next).incomparable.length >= 1);
  }
});
test('latent duplicate choices reject on import and convergent historical origins remain pending before submission', async () => {
  const { reconcileChoices } = await import('../lib/manuscript.js');
  const s = fixture(); const c = choiceFor(s);
  const file = JSON.parse(save(s)); file.source = '别的。'; file.choices = [c, structuredClone(c)]; file.history = [];
  assert.throws(() => parseWorkfile(JSON.stringify(file)), /重复/u);
  const other = fixture('标题\n'+s.source, s.target); const c2 = choiceFor(other);
  file.choices = [c, c2];
  const imported = parseWorkfile(JSON.stringify(file));
  const results = reconcileChoices(imported.choices, s, s, imported.choices);
  assert.ok(results.every(r => r.status === 'pending' && r.resolution === null));
  assert.match(results[0].reason, /汇聚/u);
});

test('partial candidate/source display permits exact visible identity recovery but never global truncation or unseen identity', () => {
  const s = fixture(); s.report.findings[0].candidates_truncated = true;
  const c = choiceFor(s);
  assert.equal(reconcileChoice(c, s, s, true).status, 'applicable');
  assert.match(reconcileChoice(c, s, s, true).reason, /展示来源不完整/u);
  const global = { ...s, report: { ...s.report, truncated: true } };
  assert.equal(reconcileChoice(c, s, global, true).status, 'pending');
  const missing = structuredClone(s); missing.report.findings[0].candidates = [];
  assert.equal(reconcileChoice(c, s, missing, true).status, 'pending');
});
test('only identical submitted official decisions can resolve under partial candidate display', () => {
  const before = fixture(undefined, 'Wrong.\nEcho.'); const after = fixture();
  const resolution = { mention_id: before.report.findings[0].id, choice: 'official_pair', candidate_id: before.report.findings[0].candidates[0].candidate_id };
  before.resolutions = [resolution]; after.resolutions = [resolution];
  before.report.findings[0].candidates_truncated = true; after.report.findings[0].candidates_truncated = true;
  assert.equal(compareReports(before, after).resolved.length, 1);
  for (const resolutions of [[], [{ ...resolution, candidate_id: hash('another-choice') }], null]) {
    const changed = { ...after, resolutions };
    assert.equal(compareReports(before, changed).resolved.length, 0);
    assert.ok(compareReports(before, changed).incomparable.length > 0);
  }
  before.resolutions = []; after.resolutions = [];
  assert.equal(compareReports(before, after).resolved.length, 0);
});
test('changing a submitted choice is incomparable even without any truncation', () => {
  const before = fixture(undefined, 'Wrong.\nEcho.'); const after = fixture();
  after.resolutions = [{ mention_id: after.report.findings[0].id, choice: 'official_pair', candidate_id: after.report.findings[0].candidates[0].candidate_id }];
  assert.equal(compareReports(before, after).resolved.length, 0);
  assert.match(compareReports(before, after).incomparable[0].reason, /选择不同/u);
  const file = JSON.parse(save(after));
  assert.deepEqual(file.history[0].resolutions, after.resolutions);
  file.history[0].resolutions[0].injected = true;
  assert.throws(() => parseWorkfile(JSON.stringify(file)));
});

// F10: dictionary exclusion lists downgrade automatic findings to not_evaluated
// with a null target span. The site runtime already treats either-side
// not_evaluated as incomparable and ignores verdicts when reusing saved
// choices; the tests below pin that contract against the new report shapes.
test('F10 downgrade needs_review to not_evaluated on same basis is incomparable, never resolved or pending', () => {
  const before = fixture(undefined, 'Wrong.\nEcho.'); // 今汐 needs_review (null target), 声骸 verified
  const after = structuredClone(before);
  after.report.findings[0] = { ...after.report.findings[0], verdict: 'not_evaluated', target_span: null };
  assert.equal(validReport(after.report, after.source, after.target), true);
  assert.equal(after.report.findings[0].id, before.report.findings[0].id);
  assert.equal(after.report.findings[0].rule_id, before.report.findings[0].rule_id);
  assert.deepEqual(after.report.findings[0].candidates, before.report.findings[0].candidates); // F10 keeps candidates
  const result = compareReports(before, after);
  assert.equal(result.resolved.length, 0);
  assert.equal(result.pending.length, 0);
  assert.equal(result.new.length, 0);
  assert.equal(result.incomparable.length, 1);
  assert.equal(result.incomparable[0].mention.text, '今汐');
  assert.equal(result.incomparable[0].mention.historical, false);
  assert.equal(result.incomparable[0].code, 'scope_position_incomparable');
  assert.match(result.incomparable[0].reason, /不可比/u);
});
test('F10 downgrade verified_constraint to not_evaluated is incomparable, never resolved or pending', () => {
  const before = fixture(); // both findings verified_constraint
  const after = structuredClone(before);
  after.report.findings[0] = { ...after.report.findings[0], verdict: 'not_evaluated', target_span: null };
  assert.equal(validReport(after.report, after.source, after.target), true);
  const result = compareReports(before, after);
  assert.equal(result.resolved.length, 0);
  assert.equal(result.pending.length, 0);
  assert.equal(result.new.length, 0);
  assert.equal(result.incomparable.length, 1);
  assert.equal(result.incomparable[0].mention.text, '今汐');
  assert.equal(result.incomparable[0].code, 'scope_position_incomparable');
});
test('unchanged saved official_pair stays applicable against a fresh not_evaluated finding with same candidate and basis', () => {
  const s = fixture();
  const gated = structuredClone(s);
  gated.report.findings[0] = { ...gated.report.findings[0], verdict: 'not_evaluated', target_span: null };
  const c = choiceFor(s);
  const candidate = s.report.findings[0].candidates[0];
  assert.equal(c.choice, 'official_pair');
  assert.equal(c.candidate.candidate_id, candidate.candidate_id);
  assert.deepEqual(c.basis, basisOf(gated.report));
  assert.ok(gated.report.findings[0].candidates.some(x => x.candidate_id === candidate.candidate_id));
  const result = reconcileChoice(c, s, gated);
  assert.equal(result.status, 'applicable');
  assert.equal(result.code, 'position_basis_match');
  assert.deepEqual(result.resolution, { mention_id: gated.report.findings[0].id, choice: 'official_pair', candidate_id: candidate.candidate_id });
});
test('not_a_term against a fresh not_evaluated finding: same-session stays applicable, imported still needs re-confirmation', () => {
  const s = fixture();
  const gated = structuredClone(s);
  gated.report.findings[0] = { ...gated.report.findings[0], verdict: 'not_evaluated', target_span: null };
  const c = choiceFor(s, 0, null);
  assert.equal(c.choice, 'not_a_term'); assert.equal(c.candidate, null);
  const sameSession = reconcileChoice(c, s, gated, false);
  assert.equal(sameSession.status, 'applicable');
  assert.equal(sameSession.code, 'user_not_a_term');
  assert.deepEqual(sameSession.resolution, { mention_id: gated.report.findings[0].id, choice: 'not_a_term' });
  const imported = reconcileChoice(c, s, gated, true);
  assert.equal(imported.status, 'pending');
  assert.equal(imported.code, 'imported_not_a_term');
  assert.equal(imported.resolution, null);
});
test('workfile round-trip keeps original verdicts: historical needs_review and fresh not_evaluated are not rewritten', async () => {
  const { freshApiReport, FRESH_MATCHER_REVISION } = await import('./fixtures/manuscript.mjs');
  const historical = fixture(undefined, 'Wrong.\nEcho.'); // 今汐 needs_review (null target), 声骸 verified
  const fresh = structuredClone(historical);
  fresh.report = freshApiReport({
    ...historical.report,
    findings: historical.report.findings.map(f => f.verdict === 'needs_review'
      ? { ...f, verdict: 'not_evaluated', target_span: null } : f),
  });
  const payload = { source: historical.source, target: historical.target, direction: historical.direction,
    alignments: historical.alignments, choices: [], history: [clean(historical), clean(fresh)] };
  const restored = parseWorkfile(serializeWorkfile(payload));
  assert.equal(restored.history.length, 2);
  const [oldSnap, freshSnap] = restored.history;
  assert.equal(oldSnap.report.findings[0].verdict, 'needs_review');
  assert.equal(oldSnap.report.findings[0].target_span, null);
  assert.equal(oldSnap.report.findings[1].verdict, 'verified_constraint');
  assert.deepEqual(oldSnap.report.findings[1].target_span, historical.report.findings[1].target_span);
  assert.equal(Object.hasOwn(oldSnap.report, 'matcher_revision'), false);
  assert.equal(freshSnap.report.findings[0].verdict, 'not_evaluated');
  assert.equal(freshSnap.report.findings[0].target_span, null);
  assert.deepEqual(freshSnap.report.findings[0].candidates, historical.report.findings[0].candidates);
  assert.equal(freshSnap.report.findings[1].verdict, 'verified_constraint');
  assert.equal(freshSnap.report.matcher_revision, FRESH_MATCHER_REVISION);
});
test('gated not_evaluated reports keep existing protections: missing finding and changed basis still require confirmation', () => {
  const s = fixture();
  const gated = structuredClone(s);
  gated.report.findings[0] = { ...gated.report.findings[0], verdict: 'not_evaluated', target_span: null };
  const c = choiceFor(s);
  const rebased = structuredClone(gated);
  rebased.report.dictionary = { ...rebased.report.dictionary, revision: hash('rotated dictionary') };
  const basisPending = reconcileChoice(c, s, rebased);
  assert.equal(basisPending.status, 'pending');
  assert.equal(basisPending.code, 'basis_changed');
  assert.equal(basisPending.resolution, null);
  const missing = structuredClone(gated);
  missing.report.findings = missing.report.findings.slice(1);
  const missingPending = reconcileChoice(c, s, missing);
  assert.equal(missingPending.status, 'pending');
  assert.equal(missingPending.code, 'finding_missing_or_truncated');
  assert.equal(missingPending.resolution, null);
  assert.equal(reconcileChoice(c, s, gated).status, 'applicable'); // verdict alone never blocks reuse
});
