const ALL_PASSED = '全部通过';

function hexSha256(bytes) {
  return [...new Uint8Array(bytes)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

export async function revisionOf(text) {
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return hexSha256(digest);
}

export function revisionsMatch(report, sourceRevision, targetRevision) {
  return Boolean(
    report
    && report.source_revision === sourceRevision
    && report.target_revision === targetRevision,
  );
}

export function isTruncated(report) {
  if (!report) return false;
  if (report.truncated === true) return true;
  return Array.isArray(report.findings)
    && report.findings.some((finding) => finding.candidates_truncated === true);
}

// Structured summary for localized rendering (see lib/messages.ts); the zh
// strings below remain the canonical data form used by exports.
/** @returns {{ verified: number, conflicts: number, needs: number, notEvaluated: number, truncated: boolean, zeroCoverage: boolean, headline: 'fail' | 'needs_check' | 'partial_verified' } | null} */
export function summarizeReviewParts(report) {
  if (!report || !Array.isArray(report.findings) || !report.coverage) return null;
  const truncated = isTruncated(report);
  const notEvaluated = Number(report.coverage.not_evaluated) || 0;
  const verified = report.findings.filter((item) => item.verdict === 'verified_constraint').length;
  const conflicts = report.findings.filter((item) => item.verdict === 'confirmed_conflict').length;
  const needs = report.findings.filter((item) => item.verdict === 'needs_review').length;
  return {
    verified,
    conflicts,
    needs,
    notEvaluated,
    truncated,
    zeroCoverage: report.findings.length === 0 || report.coverage.evaluated === 0,
    headline: conflicts > 0 ? 'fail' : (needs > 0 || notEvaluated > 0 || truncated || report.findings.length === 0 ? 'needs_check' : 'partial_verified'),
  };
}

export function summarizeReview(report) {
  const parts = summarizeReviewParts(report);
  if (!parts) {
    return {
      headline: '未通过',
      detail: '没有可用的审校报告，不能视为已核。',
      allPassed: false,
    };
  }
  const segments = [`约束已核 ${parts.verified}`, `冲突 ${parts.conflicts}`, `需核对 ${parts.needs}`];
  if (parts.notEvaluated > 0) segments.push(`未评估 ${parts.notEvaluated}`);
  if (parts.truncated) segments.push('结果已截断，不能当作完整核对');
  if (parts.zeroCoverage) segments.push('零覆盖');
  segments.push('未评估完整句意');
  const headline = parts.headline === 'fail' ? '未通过' : parts.headline === 'needs_check' ? '需核对' : '部分已核';
  return {
    headline,
    detail: `本次检查：词典术语与基础结构；${segments.join('；')}。`,
    allPassed: false,
  };
}

export function isCurrentVerified({
  report,
  sourceRevision,
  targetRevision,
  reportGeneration,
  currentGeneration,
}) {
  if (reportGeneration !== currentGeneration) return false;
  if (!revisionsMatch(report, sourceRevision, targetRevision)) return false;
  if (isTruncated(report)) return false;
  return true;
}

export function exportReview({
  report,
  source,
  target,
  sourceRevision,
  targetRevision,
  reportGeneration,
  currentGeneration,
}) {
  const summary = summarizeReview(report);
  const current = isCurrentVerified({
    report,
    sourceRevision,
    targetRevision,
    reportGeneration,
    currentGeneration,
  });
  const stampValid = current
    && report
    && report.coverage.not_evaluated === 0
    && !isTruncated(report)
    && Array.isArray(report.findings)
    && report.findings.length > 0
    && report.findings.every((item) => item.verdict === 'verified_constraint');
  return {
    format: 'wuwaterm-review-v1',
    source,
    target,
    summary: summary.detail,
    headline: summary.headline,
    report,
    verified_stamp: stampValid ? { valid: true } : { valid: false },
  };
}

export function scalarToUtf16(text, scalarOffset) {
  if (typeof text !== 'string' || !Number.isInteger(scalarOffset) || scalarOffset < 0) return null;
  const scalars = Array.from(text);
  if (scalarOffset > scalars.length) return null;
  return scalars.slice(0, scalarOffset).join('').length;
}

export function highlightSegments(text, span) {
  if (!span || typeof text !== 'string') return null;
  const start = scalarToUtf16(text, span.start);
  const end = scalarToUtf16(text, span.end);
  if (start === null || end === null || start > end) return null;
  const slice = text.slice(start, end);
  if (slice !== span.text) return null;
  return { before: text.slice(0, start), hit: slice, after: text.slice(end) };
}

export function containsForbiddenPassPhrase(text) {
  return typeof text === 'string' && text.includes(ALL_PASSED);
}
