// Literal displayed-text matching only. Raw manuscript bytes remain authoritative.
import { validSpan } from './manuscript.js';
import { scalarToUtf16 } from './review-report.js';

function scalarText(value) {
  return typeof value === 'string' && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value);
}
export function fragmentOccurrences(target, fragment) {
  if (!scalarText(target) || !scalarText(fragment) || !fragment.trim()) return [];
  const needle = fragment.replace(/\r\n?/g, '\n');
  let displayed = '';
  const boundaries = new Map([[0, 0]]);
  const raw = Array.from(target);
  for (let i = 0; i < raw.length; i += 1) {
    if (raw[i] === '\r') {
      displayed += '\n';
      if (raw[i + 1] === '\n') i += 1;
    } else displayed += raw[i];
    boundaries.set(displayed.length, i + 1);
  }
  const matches = [];
  for (let offset = displayed.indexOf(needle); offset >= 0; offset = displayed.indexOf(needle, offset + 1)) {
    const start = boundaries.get(offset), end = boundaries.get(offset + needle.length);
    if (start === undefined || end === undefined) continue;
    const span = { start, end, text: raw.slice(start, end).join('') };
    if (validSpan(span, target)) matches.push(span);
  }
  return matches;
}
export function spliceOccurrence(target, fragment, selected, replacement) {
  if (!scalarText(replacement) || !replacement.trim()) return { error: 'revisionInvalid' };
  if (!validSpan(selected, target) || !fragmentOccurrences(target, fragment).some(span => span.start === selected.start && span.end === selected.end && span.text === selected.text)) return { error: 'revisionSelect' };
  if (selected.text === replacement) return { error: 'revisionNoop' };
  const start = scalarToUtf16(target, selected.start), end = scalarToUtf16(target, selected.end);
  const next = target.slice(0, start) + replacement + target.slice(end);
  if (Array.from(next).length > 2000) return { error: 'revisionTooLong' };
  return { target: next, span: { start: selected.start, end: selected.start + Array.from(replacement).length, text: replacement } };
}
