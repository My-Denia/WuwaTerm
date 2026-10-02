import { highlightSegments } from './review-report.js';

export const FILTERS = [
  { id: 'all', label: '全部' },
  { id: 'confirmed_conflict', label: '冲突' },
  { id: 'needs_review', label: '需核对' },
  { id: 'not_evaluated', label: '未评估' },
  { id: 'verified_constraint', label: '已核' },
];

const VERDICTS = ['confirmed_conflict', 'needs_review', 'not_evaluated', 'verified_constraint'];
export const CONTEXT_RADIUS = 48;

export function findingVerdictCounts(findings) {
  const counts = {
    all: 0,
    confirmed_conflict: 0,
    needs_review: 0,
    not_evaluated: 0,
    verified_constraint: 0,
  };
  if (!Array.isArray(findings)) return counts;
  counts.all = findings.length;
  for (const finding of findings) {
    if (VERDICTS.includes(finding?.verdict)) counts[finding.verdict] += 1;
  }
  return counts;
}

export function filterFindings(findings, filter) {
  if (!Array.isArray(findings)) return [];
  if (filter === 'all') return findings.slice();
  return findings.filter((finding) => finding.verdict === filter);
}

export function reconcileActiveId(requestedId, visibleFindings) {
  if (!Array.isArray(visibleFindings) || visibleFindings.length === 0) return null;
  if (typeof requestedId === 'string' && visibleFindings.some((finding) => finding.id === requestedId)) {
    return requestedId;
  }
  return visibleFindings[0].id;
}

export function compactScalarContext(text, span, radius = CONTEXT_RADIUS) {
  const located = highlightSegments(text, span);
  if (!located || !Number.isInteger(radius) || radius < 0) return null;
  const scalars = Array.from(text);
  const beforeStart = Math.max(0, span.start - radius);
  const afterEnd = Math.min(scalars.length, span.end + radius);
  return {
    before: scalars.slice(beforeStart, span.start).join(''),
    hit: located.hit,
    after: scalars.slice(span.end, afterEnd).join(''),
    clippedBefore: beforeStart > 0,
    clippedAfter: afterEnd < scalars.length,
  };
}
