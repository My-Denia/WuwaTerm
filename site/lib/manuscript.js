// Device-local workfiles contain user intent, never reusable certification.
import { highlightSegments } from './review-report.js';

export const WORKFILE_FORMAT = 'wuwaterm-manuscript-v1';
export const MAX_WORKFILE_BYTES = 1_048_576;
export const MAX_CHOICES = 32;
export const MAX_HISTORY = 2;
const V2 = 'review-v2';
const HEX = /^[a-f0-9]{64}$/u;
const VERDICTS = ['verified_constraint', 'confirmed_conflict', 'needs_review', 'not_evaluated'];

function object(x) { return !!x && typeof x === 'object' && !Array.isArray(x); }
function keys(x, required) {
  return object(x) && Object.keys(x).length === required.length && required.every(k => Object.hasOwn(x, k));
}
function text(x, max = 2000, empty = true) {
  return typeof x === 'string' && (empty || x.length > 0) && Array.from(x).length <= max
    && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(x);
}
function direction(x) { return x === 'en' || x === 'zh'; }
export function validSpan(span, content) {
  return keys(span, ['start', 'end', 'text']) && Number.isInteger(span.start) && Number.isInteger(span.end)
    && span.start >= 0 && span.end > span.start && text(span.text, 2000, false)
    && highlightSegments(content, span) !== null;
}
export function validAlignments(items, source, target) {
  if (items === null) return true;
  if (!Array.isArray(items) || items.length > 64) return false;
  let sourceEnd = 0; let targetEnd = 0;
  for (const item of items) {
    if (!keys(item, ['source', 'target']) || !validSpan(item.source, source)
      || (item.target !== null && !validSpan(item.target, target)) || item.source.start < sourceEnd) return false;
    sourceEnd = item.source.end;
    if (item.target !== null) {
      if (item.target.start < targetEnd) return false;
      targetEnd = item.target.end;
    }
  }
  return true;
}
function validDictionary(x, version) {
  const fields = ['schema_version', 'source_commit', 'term_count'];
  if (version === V2) fields.push('revision');
  return keys(x, fields) && (x.schema_version === null || text(x.schema_version, 4096))
    && (x.source_commit === null || text(x.source_commit, 4096)) && Number.isSafeInteger(x.term_count) && x.term_count >= 0
    && (version !== V2 || HEX.test(x.revision));
}
function validCandidate(x, version) {
  const fields = ['zh', 'en', 'category', 'sources'];
  if (version === V2) fields.push('candidate_id');
  return keys(x, fields) && text(x.zh, 2000, false) && text(x.en, 2000, false) && text(x.category, 256, false)
    && Array.isArray(x.sources) && x.sources.length <= 8
    && x.sources.every(s => keys(s, ['source_file', 'source_id']) && text(s.source_file, 4096, false) && text(s.source_id, 1024, false))
    && (version !== V2 || HEX.test(x.candidate_id));
}
export function validReport(x, source, target) {
  return keys(x, ['request_id', 'source_revision', 'target_revision', 'rule_version', 'dictionary', 'coverage', 'findings', 'truncated'])
    && text(x.request_id, 256, false) && HEX.test(x.source_revision) && HEX.test(x.target_revision)
    && ['review-v1', V2].includes(x.rule_version) && validDictionary(x.dictionary, x.rule_version)
    && keys(x.coverage, ['evaluated', 'not_evaluated', 'rules'])
    && Number.isSafeInteger(x.coverage.evaluated) && x.coverage.evaluated >= 0
    && Number.isSafeInteger(x.coverage.not_evaluated) && x.coverage.not_evaluated >= 0
    && Array.isArray(x.coverage.rules) && x.coverage.rules.length <= 16 && x.coverage.rules.every(r => text(r, 256, false))
    && typeof x.truncated === 'boolean' && Array.isArray(x.findings) && x.findings.length <= 32
    && new Set(x.findings.map(f => f?.id)).size === x.findings.length
    && x.findings.every(f => keys(f, ['id', 'rule_id', 'verdict', 'source_span', 'target_span', 'candidates', 'candidates_truncated'])
      && text(f.id, 8192, false) && text(f.rule_id, 256, false) && VERDICTS.includes(f.verdict)
      && validSpan(f.source_span, source) && f.id === mentionId(f.source_span)
      && (f.target_span === null || validSpan(f.target_span, target))
      && typeof f.candidates_truncated === 'boolean' && Array.isArray(f.candidates) && f.candidates.length <= 8
      && f.candidates.every(c => validCandidate(c, x.rule_version)));
}
export function basisOf(report) { return { rule_version: report.rule_version, dictionary: { ...report.dictionary } }; }
export function sameBasis(a, b) {
  return !!a && !!b && a.rule_version === V2 && b.rule_version === V2
    && text(a.dictionary?.schema_version, 4096, false) && text(b.dictionary?.schema_version, 4096, false)
    && text(a.dictionary?.source_commit, 4096, false) && text(b.dictionary?.source_commit, 4096, false)
    && HEX.test(a.dictionary?.revision) && a.dictionary.revision === b.dictionary?.revision
    && a.dictionary.source_commit === b.dictionary.source_commit
    && a.dictionary.schema_version === b.dictionary.schema_version && a.dictionary.term_count === b.dictionary.term_count;
}
export function mentionId(span) { return `${span.start}:${span.end}:${span.text}`; }
export function scopeOf(alignments, span) {
  if (alignments === null) return 'positional';
  const region = alignments.find(a => a.source.start <= span.start && a.source.end >= span.end);
  return region ? JSON.stringify(region) : 'unmapped';
}
function sourceBlocks(source) {
  let start = 0;
  return source.split('\n').map(value => {
    const block = { start, end: start + Array.from(value).length, text: value };
    start = block.end + 1;
    return block;
  });
}
// Relocation is permitted only inside an identical, unique, complete source block.
export function recoverSpan(oldSource, span, source) {
  if (!validSpan(span, oldSource)) return null;
  if (oldSource === source) return { ...span };
  const oldBlocks = sourceBlocks(oldSource); const newBlocks = sourceBlocks(source);
  const block = oldBlocks.find(b => b.start <= span.start && b.end >= span.end);
  if (!block || oldBlocks.filter(b => b.text === block.text).length !== 1) return null;
  const matches = newBlocks.filter(b => b.text === block.text);
  if (matches.length !== 1) return null;
  const start = matches[0].start + span.start - block.start;
  const recovered = { start, end: start + span.end - span.start, text: span.text };
  return validSpan(recovered, source) ? recovered : null;
}
export function makeChoice({ source, direction, alignments, report, finding, candidate }) {
  return {
    source, direction, source_span: { ...finding.source_span }, scope: scopeOf(alignments, finding.source_span),
    choice: candidate ? 'official_pair' : 'not_a_term',
    candidate: candidate ? { candidate_id: candidate.candidate_id, zh: candidate.zh, en: candidate.en, category: candidate.category } : null,
    basis: basisOf(report),
  };
}
function validChoice(x) {
  return keys(x, ['source', 'direction', 'source_span', 'scope', 'choice', 'candidate', 'basis'])
    && text(x.source) && direction(x.direction) && validSpan(x.source_span, x.source) && text(x.scope, 20000)
    && keys(x.basis, ['rule_version', 'dictionary']) && x.basis.rule_version === V2 && validDictionary(x.basis.dictionary, V2)
    && (x.choice === 'not_a_term' ? x.candidate === null : x.choice === 'official_pair'
      && keys(x.candidate, ['candidate_id', 'zh', 'en', 'category']) && HEX.test(x.candidate.candidate_id)
      && text(x.candidate.zh, 2000, false) && text(x.candidate.en, 2000, false) && text(x.candidate.category, 256, false));
}
function validSnapshot(x) {
  const fields = ['source', 'target', 'direction', 'alignments', 'report'];
  if (object(x) && Object.hasOwn(x, 'resolutions')) fields.push('resolutions');
  return keys(x, fields) && text(x.source) && text(x.target)
    && direction(x.direction) && validAlignments(x.alignments, x.source, x.target) && validReport(x.report, x.source, x.target)
    && (!Object.hasOwn(x, 'resolutions') || validSubmittedResolutions(x.resolutions));
}
function validSubmittedResolutions(items) {
  return Array.isArray(items) && items.length <= MAX_CHOICES
    && new Set(items.map(item => item?.mention_id)).size === items.length
    && items.every(item => object(item) && text(item.mention_id, 8192, false)
      && (item.choice === 'not_a_term' ? keys(item, ['mention_id', 'choice'])
        : item.choice === 'official_pair' && keys(item, ['mention_id', 'choice', 'candidate_id']) && HEX.test(item.candidate_id)));
}
// Localized UIs render reason codes from lib/messages.ts; the Chinese strings
// stay the canonical data form (result JSON keeps them verbatim).
function fail(message, code) {
  return Object.assign(new Error(message), { code });
}
function safeTree(x, depth = 0) {
  if (depth > 16) throw fail('稿件文件嵌套过深。', 'workfile_too_deep');
  if (Array.isArray(x)) {
    if (x.length > 128) throw fail('稿件文件条目过多。', 'workfile_too_many_entries');
    x.forEach(v => safeTree(v, depth + 1));
  } else if (object(x)) {
    if (Object.keys(x).length > 32) throw fail('稿件文件字段过多。', 'workfile_too_many_fields');
    for (const [key, value] of Object.entries(x)) {
      if (['__proto__', 'constructor', 'prototype'].includes(key)) throw fail('稿件文件含不支持的字段。', 'workfile_forbidden_field');
      safeTree(value, depth + 1);
    }
  }
}
export function parseWorkfile(content) {
  if (typeof content !== 'string' || new TextEncoder().encode(content).length > MAX_WORKFILE_BYTES) throw fail('稿件文件不能超过 1 MiB。', 'workfile_too_large');
  return parseWorkfileContent(content);
}
function parseWorkfileContent(content) {
  let x;
  try { x = JSON.parse(content); } catch { throw fail('无法读取 JSON 稿件文件。', 'workfile_not_json'); }
  safeTree(x);
  if (!keys(x, ['format', 'source', 'target', 'direction', 'alignments', 'choices', 'history']) || x.format !== WORKFILE_FORMAT) {
    throw fail('不支持此稿件格式或版本。请导入“保存稿件”生成的文件。', 'workfile_unsupported_format');
  }
  if (!text(x.source) || !text(x.target) || !direction(x.direction) || !validAlignments(x.alignments, x.source, x.target)
    || !Array.isArray(x.choices) || x.choices.length > MAX_CHOICES || !x.choices.every(validChoice)
    || !Array.isArray(x.history) || x.history.length > MAX_HISTORY || !x.history.every(validSnapshot)) {
    throw fail('稿件内容、选择或位置依据无效；当前稿件未被替换。', 'workfile_invalid_content');
  }
  const recoveredIds = x.choices.map(c => recoverSpan(c.source, c.source_span, x.source)).filter(Boolean).map(mentionId);
  const originalIds = x.choices.map(c => JSON.stringify([c.source, c.direction, mentionId(c.source_span)]));
  if (new Set(recoveredIds).size !== recoveredIds.length || new Set(originalIds).size !== originalIds.length) throw fail('稿件包含同一位置的重复选择，未导入。', 'workfile_duplicate_choices');
  // No runtime trust or active flags come from the file.
  return x;
}
function prettyManuscript({ source, target, direction, alignments, choices, history }) {
  return JSON.stringify({ format: WORKFILE_FORMAT, source, target, direction, alignments, choices, history: history.slice(-MAX_HISTORY) }, null, 2);
}
export function serializeWorkfile(payload) {
  const content = prettyManuscript(payload);
  parseWorkfile(content);
  return content;
}

/** Pretty form of a valid manuscript. Save still rejects a string over the size cap;
 *  this returns that string so an imported compact file can stay the restore baseline. */
export function canonicalManuscript(payload) {
  try {
    const content = prettyManuscript(payload);
    parseWorkfileContent(content);
    return content;
  } catch { return null; }
}

export function recoverableWorkPresent({ source, target, choices, alignments, reports }) {
  return source.length > 0 || target.length > 0 || choices.length > 0 || alignments !== null || reports.length > 0;
}

export function manuscriptSaveView({ holdsWork, canonical, importedCanonical, downloadedCanonical }) {
  if (!holdsWork) return 'empty';
  if (canonical == null) return 'not-serializable';
  if (importedCanonical != null && canonical === importedCanonical) return 'matches-import';
  if (downloadedCanonical != null && canonical === downloadedCanonical) return 'matches-download';
  if (importedCanonical != null || downloadedCanonical != null) return 'diverged';
  return 'page-only';
}

/** A matching download does not turn this off: the page never saw the file reach disk. */
export function leaveGuardActive({ view, checkInFlight = false }) {
  if (checkInFlight) return true;
  return view === 'page-only' || view === 'matches-download' || view === 'diverged' || view === 'not-serializable';
}

/** A fresh report can recover intent; only a later request can check that decision. */
export function reconcileChoice(choice, draft, fresh, imported = false) {
  const span = recoverSpan(choice.source, choice.source_span, draft.source);
  const pending = (reason, code) => ({ status: 'pending', reason, code, span, resolution: null });
  if (choice.direction !== draft.direction) return pending('翻译方向已变化，请重新选择', 'direction_changed');
  if (!span) return pending('原文段落已改变、删除或重复，不能迁移位置', 'source_block_changed');
  if (choice.scope !== scopeOf(draft.alignments, span)) return pending('分段对应已变化，请重新确认', 'scope_changed');
  if (!fresh || fresh.source !== draft.source || fresh.direction !== draft.direction || !fresh.trusted) return pending('等待当前原文的新依据', 'awaiting_fresh_basis');
  if (!sameBasis(choice.basis, basisOf(fresh.report))) return pending('词典或规则依据已变化，请重新确认', 'basis_changed');
  const finding = fresh.report.findings.find(f => f.id === mentionId(span));
  if (!finding || fresh.report.truncated) return pending('本次发现缺失或报告截断，请重新确认', 'finding_missing_or_truncated');
  if (choice.choice === 'not_a_term') {
    if (imported) return pending('文件中的“不是术语”是历史决定，请重新确认', 'imported_not_a_term');
    return { status: 'applicable', reason: '用户决定；此处不评估术语', code: 'user_not_a_term', span, resolution: { mention_id: finding.id, choice: 'not_a_term' } };
  }
  const candidate = finding.candidates.find(c => c.candidate_id === choice.candidate.candidate_id
    && c.zh === choice.candidate.zh && c.en === choice.candidate.en && c.category === choice.candidate.category);
  if (!candidate) return pending('原词对或其来源已不在当前候选中', 'candidate_gone');
  return { status: 'applicable', reason: finding.candidates_truncated ? '展示来源不完整；具体词对身份与当前依据一致，提交时由服务端复核' : '位置与当前官方候选依据一致',
    code: finding.candidates_truncated ? 'candidate_partial_visible' : 'position_basis_match', span,
    resolution: { mention_id: finding.id, choice: 'official_pair', candidate_id: candidate.candidate_id } };
}

/** @returns {Array<ReturnType<typeof reconcileChoice>>} */
export function reconcileChoices(choices, draft, fresh, importedChoices = []) {
  const results = choices.map(c => reconcileChoice(c, draft, fresh, importedChoices.includes(c)));
  const counts = new Map();
  for (const result of results) if (result.span) counts.set(mentionId(result.span), (counts.get(mentionId(result.span)) ?? 0) + 1);
  return results.map(result => result.span && counts.get(mentionId(result.span)) > 1
    ? { ...result, status: 'pending', reason: '多份历史选择汇聚到同一位置，请移除冲突记录后重新选择', code: 'duplicate_position', resolution: null } : result);
}

function positionalSourceRegion(snapshot, span) {
  const ranges = [];
  const chars = Array.from(snapshot.source);
  let start = 0;
  for (let i = 0; i < chars.length; i++) {
    const decimal = chars[i] === '.' && /[0-9]/u.test(chars[i - 1] ?? '') && /[0-9]/u.test(chars[i + 1] ?? '');
    if (!decimal && /[。．.！？!?\n]/u.test(chars[i])) {
      const region = chars.slice(start, i + 1).join('');
      if (/[^。．.！？!?\n\s]/u.test(region)) ranges.push({ start, end: i + 1, text: region });
      start = i + 1;
    }
  }
  if (start < chars.length) ranges.push({ start, end: chars.length, text: chars.slice(start).join('') });
  return ranges.find(r => r.start <= span.start && r.end >= span.end)?.text ?? null;
}

function comparableScope(previous, beforeSpan, current, span) {
  if (previous.alignments === null && current.alignments === null) {
    const before = positionalSourceRegion(previous, beforeSpan);
    return before !== null && before === positionalSourceRegion(current, span);
  }
  return scopeOf(previous.alignments, beforeSpan) === scopeOf(current.alignments, span);
}

function submittedDecision(snapshot, finding) {
  if (!Array.isArray(snapshot.resolutions)) return { descriptor: null, candidate: null };
  const item = snapshot.resolutions.find(r => r.mention_id === finding.id);
  if (!item) return { descriptor: 'automatic', candidate: null };
  return {
    descriptor: JSON.stringify([item.choice, item.candidate_id ?? null]),
    candidate: item.choice === 'official_pair' ? finding.candidates.find(c => c.candidate_id === item.candidate_id) ?? null : null,
  };
}

export function compareReports(previous, current) {
  const result = { new: [], resolved: [], pending: [], incomparable: [] };
  if (!previous) return result;
  const oldFindings = previous.report.findings;
  const newFindings = current.report.findings;
  const compatible = previous.trusted && current.trusted && previous.direction === current.direction
    && sameBasis(basisOf(previous.report), basisOf(current.report)) && !previous.report.truncated && !current.report.truncated;
  const seen = new Set();
  for (const before of oldFindings) {
    const span = recoverSpan(previous.source, before.source_span, current.source);
    const after = span && newFindings.find(f => f.id === mentionId(span) && f.rule_id === before.rule_id);
    const scopesMatch = span && comparableScope(previous, before.source_span, current, span);
    const oldDecision = submittedDecision(previous, before);
    const newDecision = after ? submittedDecision(current, after) : { descriptor: null, candidate: null };
    const decisionsMatch = oldDecision.descriptor !== null && oldDecision.descriptor === newDecision.descriptor;
    const sameExplicitCandidate = decisionsMatch && oldDecision.candidate && newDecision.candidate
      && oldDecision.candidate.candidate_id === newDecision.candidate.candidate_id
      && oldDecision.candidate.zh === newDecision.candidate.zh && oldDecision.candidate.en === newDecision.candidate.en
      && oldDecision.candidate.category === newDecision.candidate.category;
    if (after) seen.add(after.id);
    const mention = { text: before.source_span.text, start: (span ?? before.source_span).start, historical: !span };
    const label = `${before.source_span.text}（${span ? '原文' : '历史原文'} ${(span ?? before.source_span).start + 1}）`;
    if (!compatible || !after || !scopesMatch || !decisionsMatch
      || ((before.candidates_truncated || after.candidates_truncated) && !sameExplicitCandidate)
      || before.verdict === 'not_evaluated' || after.verdict === 'not_evaluated') {
      result.incomparable.push({ label, mention, reason: !previous.trusted ? '导入的历史报告未经本次验证'
        : !decisionsMatch ? '前后选择不同或选择记录未知，不能视为同一约束已解决' : '位置、依据、覆盖或对应范围不可比；消失不代表解决',
        code: !previous.trusted ? 'imported_history_unverified' : !decisionsMatch ? 'decision_changed_or_unknown' : 'scope_position_incomparable' });
    } else if (['needs_review', 'confirmed_conflict'].includes(before.verdict) && after.verdict === 'verified_constraint') {
      result.resolved.push({ label, mention, reason: '同一可比位置的新检查确认术语约束已满足，未评估句意', code: 'resolved_same_constraint' });
    } else if (['needs_review', 'confirmed_conflict'].includes(after.verdict)) {
      result.pending.push({ label, mention, reason: '本次仍需人工核对', code: 'still_needs_review' });
    }
  }
  for (const finding of newFindings) {
    if (!seen.has(finding.id)) result.new.push({ label: `${finding.source_span.text}（原文 ${finding.source_span.start + 1}）`,
      mention: { text: finding.source_span.text, start: finding.source_span.start, historical: false },
      reason: '本次发现；没有可比的旧位置', code: 'new_finding' });
  }
  return result;
}
