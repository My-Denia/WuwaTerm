import { fill, msg, summaryHeadline, summarySentence } from './messages.ts';
import type { UiLanguage } from './ui-language';
import { compactScalarContext } from './review-navigation.js';
import { summarizeReviewParts } from './review-report.js';

// Every user-derived string (source, translation, terms, categories, source
// records, excerpts, basis strings) is rendered inside a code span or a code
// fence, so it can never become report structure, active HTML or a link.
// Fences grow longer than the longest backtick run inside the content, so a
// user cannot close a block. Structural lines are emitted only by this file.

export type MarkdownSpan = { start: number; end: number; text: string };
export type MarkdownCandidate = { candidate_id: string; zh: string; en: string; category: string; sources: { source_file: string; source_id: string }[] };
export type MarkdownFinding = { id: string; rule_id: string; verdict: string; source_span: MarkdownSpan; target_span: MarkdownSpan | null; candidates: MarkdownCandidate[]; candidates_truncated: boolean };
export type MarkdownReport = {
  request_id: string; source_revision: string; target_revision: string; rule_version: string;
  dictionary: { schema_version: string | null; source_commit: string | null; term_count: number; revision?: string };
  coverage: { evaluated: number; not_evaluated: number; rules: string[] };
  findings: MarkdownFinding[]; truncated: boolean;
};
export type MarkdownSnapshot = { source: string; target: string; direction: 'en' | 'zh'; report: MarkdownReport; resolutions: { mention_id: string; choice: string; candidate_id?: string }[] | null };
export type MarkdownChoice = { source: string; direction: string; source_span: MarkdownSpan; choice: string; candidate: { candidate_id: string; zh: string; en: string; category: string } | null; basis: { rule_version: string; dictionary: { revision: string; source_commit: string | null } } };
export type MarkdownReconciled = { status: string; reason: string; code?: string };
export type MarkdownComparisonItem = { label: string; reason: string; code?: string; mention: { text: string; start: number; historical: boolean } };
export type MarkdownComparison = { new: MarkdownComparisonItem[]; resolved: MarkdownComparisonItem[]; pending: MarkdownComparisonItem[]; incomparable: MarkdownComparisonItem[] };
export type MarkdownCurrency = 'current' | 'stale' | 'imported' | 'none';

export type MarkdownReportInput = {
  draft: { source: string; target: string; direction: 'en' | 'zh' };
  latest: MarkdownSnapshot | null;
  previous: MarkdownSnapshot | null;
  currency: MarkdownCurrency;
  choices: MarkdownChoice[];
  reconciled: MarkdownReconciled[];
  comparison: MarkdownComparison | null;
};

function backtickRun(text: string): number {
  let max = 0;
  for (const match of text.matchAll(/`+/gu)) max = Math.max(max, match[0].length);
  return max;
}

/** Inline literal: single-line user text inside a code span that no content can break out of. */
function inline(text: string): string {
  // Markdown normalizes LF, CRLF and a standalone CR to line breaks before
  // inline parsing, so every line-ending form must be flattened first.
  const flat = text.replace(/\r\n|\r|\n/gu, '⏎');
  const run = backtickRun(flat);
  if (run === 0) return '`' + flat + '`';
  return '`'.repeat(run + 1) + ' ' + flat + ' ' + '`'.repeat(run + 1);
}

/** Block literal: user text (possibly multi-line) inside an unclosable fence. */
function block(text: string): string[] {
  const fence = '`'.repeat(Math.max(3, backtickRun(text) + 1));
  return [fence, text, fence];
}

function bullets(lines: string[]): string[] {
  return lines.filter(Boolean).map(line => '- ' + line);
}

/** Localized "label (n)" suffix: fullwidth for zh, ASCII for en. */
function counted(label: string, count: number, lang: UiLanguage): string {
  return lang === 'zh' ? `${label}（${count}）` : `${label} (${count})`;
}

/** Localized label-value separators. */
function colon(lang: UiLanguage): string {
  return lang === 'zh' ? '：' : ': ';
}
function semicolon(lang: UiLanguage): string {
  return lang === 'zh' ? '；' : '; ';
}

export function renderMarkdownReport(input: MarkdownReportInput, lang: UiLanguage): string {
  const m = msg(lang);
  const md = m.md;
  const out: string[] = [];
  const heading = (depth: number, text: string) => out.push('#'.repeat(depth) + ' ' + text, '');
  const paragraph = (text: string) => out.push(text, '');

  out.push('# ' + md.title, '');
  paragraph(md.snapshotLine);

  const report = input.latest?.report ?? null;
  const parts = summarizeReviewParts(report);

  heading(2, md.currencyHeading);
  paragraph(input.currency === 'current' ? md.currencyCurrent : input.currency === 'stale' ? md.currencyStale
    : input.currency === 'imported' ? md.currencyImported : md.noReportLine);

  // The direction line comes from the report itself whenever one exists;
  // a draft whose direction was edited after the check must not be stitched
  // onto the older report, so a mismatch is stated explicitly.
  if (input.latest) {
    paragraph(md.reportDirectionLabel + colon(lang) + (input.latest.direction === 'en' ? md.directionToEn : md.directionToZh));
    if (input.draft.direction !== input.latest.direction) {
      paragraph(fill(md.directionChangedNote, { direction: input.draft.direction === 'en' ? md.directionToEn : md.directionToZh }));
    }
  } else {
    paragraph(md.directionLabel + colon(lang) + (input.draft.direction === 'en' ? md.directionToEn : md.directionToZh));
  }

  heading(2, md.sourceHeading);
  out.push(...block(input.draft.source), '');
  heading(2, md.targetHeading);
  out.push(...block(input.draft.target), '');

  if (report && input.latest && parts) {
    heading(2, md.summaryHeading);
    paragraph(summaryHeadline(parts, lang) + colon(lang) + summarySentence(parts, lang));

    heading(2, md.basisHeading);
    out.push(...bullets([
      md.ruleLabel + colon(lang) + inline(report.rule_version),
      // Imported review-v1 reports have no dictionary revision; keep the
      // export working and mark it as not provided.
      md.revisionLabel + colon(lang) + inline(report.dictionary.revision ?? md.notProvided),
      md.commitLabel + colon(lang) + inline(report.dictionary.source_commit ?? md.notProvided),
      md.schemaLabel + colon(lang) + inline(report.dictionary.schema_version ?? md.notProvided),
      md.termCountLabel + colon(lang) + report.dictionary.term_count,
      md.requestIdLabel + colon(lang) + inline(report.request_id),
      md.sourceRevisionLabel + colon(lang) + inline(report.source_revision),
      md.targetRevisionLabel + colon(lang) + inline(report.target_revision),
    ]), '');

    heading(2, md.coverageHeading);
    out.push(...bullets([
      md.evaluatedLabel + colon(lang) + report.coverage.evaluated,
      md.notEvaluatedLabel + colon(lang) + report.coverage.not_evaluated,
      md.rulesLabel + colon(lang) + (report.coverage.rules.length ? report.coverage.rules.map(rule => inline(rule)).join(' · ') : md.notProvided),
    ]), '');
    if (parts.zeroCoverage) paragraph(md.zeroCoverageLine);
    if (report.truncated) paragraph(md.truncatedLine);

    heading(2, counted(md.findingsHeading, report.findings.length, lang));
    if (report.findings.length === 0) paragraph(md.zeroFindingsLine);
    report.findings.forEach((finding, index) => {
      const verdict = (msg(lang).review.verdicts as Record<string, string>)[finding.verdict] ?? finding.verdict;
      out.push('### ' + (index + 1) + '. ' + verdict + ' · ' + inline(finding.source_span.text), '');
      out.push(...bullets([
        fill(md.spanLabel, { start: finding.source_span.start + 1, end: finding.source_span.end })
          + ' · ' + md.ruleLabelShort + colon(lang) + inline(finding.rule_id),
        finding.target_span
          ? fill(md.targetSpanLabel, { start: finding.target_span.start + 1, end: finding.target_span.end }) + colon(lang) + inline(finding.target_span.text)
          : md.noTargetSpan,
      ]), '');
      const context = compactScalarContext(input.latest!.source, finding.source_span);
      if (context) {
        out.push(md.excerptLabel + colon(lang), ...block((context.clippedBefore ? '…' : '') + context.before + context.hit + context.after + (context.clippedAfter ? '…' : '')), '');
      } else {
        paragraph(md.excerptLabel + colon(lang) + md.excerptMissing);
      }
      paragraph(counted(md.candidatesHeading, finding.candidates.length, lang));
      if (!finding.candidates.length) paragraph(md.noCandidates);
      for (const candidate of finding.candidates) {
        out.push('- ' + inline(candidate.zh) + ' / ' + inline(candidate.en) + ' · ' + inline(candidate.category));
        out.push('  - ' + md.sourceRecordsLabel + colon(lang) + (candidate.sources.length
          ? candidate.sources.map(item => inline(item.source_file) + ' · ' + inline(item.source_id)).join(semicolon(lang))
          : md.notProvided));
      }
      if (finding.candidates_truncated) paragraph(md.candidatesTruncatedLine);
      out.push('');
    });

    heading(2, md.decisionsHeading);
    const resolutions = input.latest.resolutions ?? [];
    if (!resolutions.length) paragraph(md.noDecisions);
    else out.push(...bullets(resolutions.map(resolution => {
      const findingIndex = report.findings.findIndex(finding => finding.id === resolution.mention_id);
      const where = findingIndex >= 0 ? ' #' + (findingIndex + 1) : '';
      return (resolution.choice === 'not_a_term' ? md.decisionNotTerm : md.decisionOfficial) + where
        + (resolution.choice === 'official_pair' && resolution.candidate_id ? ' · ' + inline(resolution.candidate_id) : '');
    })), '');
  }

  heading(2, counted(md.choicesHeading, input.choices.length, lang));
  if (!input.choices.length) paragraph(md.noChoices);
  else out.push(...bullets(input.choices.map((choice, index) => {
    const status = input.reconciled[index]?.status === 'applicable' ? md.choiceApplicable : md.choicePending;
    const reason = input.reconciled[index]
      ? ((msg(lang).choiceReasons as Record<string, string>)[input.reconciled[index].code ?? ''] ?? input.reconciled[index].reason)
      : '';
    const pair = choice.candidate
      ? inline(choice.candidate.zh) + ' / ' + inline(choice.candidate.en)
      : msg(lang).review.notTerm;
    return inline(choice.source_span.text) + ' · ' + pair + ' · ' + status + (reason ? ' · ' + reason : '');
  })), '');
  if (input.choices.length) {
    out.push(...bullets(input.choices.map(choice => fill(md.basisLine, {
      rule: inline(choice.basis.rule_version),
      rev: inline(choice.basis.dictionary.revision),
      commit: inline(choice.basis.dictionary.source_commit ?? md.notProvided),
    }))), '');
  }

  heading(2, md.comparisonHeading);
  if (!input.latest) paragraph(md.comparisonNone);
  else if (!input.previous) paragraph(md.comparisonSingle);
  else if (input.comparison) {
    const groups: [keyof MarkdownComparison, string][] = [
      ['new', msg(lang).review.comparisonNew],
      ['resolved', msg(lang).review.comparisonResolved],
      ['pending', msg(lang).review.comparisonPending],
      ['incomparable', msg(lang).review.comparisonIncomparable],
    ];
    for (const [key, label] of groups) {
      const items = input.comparison[key];
      if (!items.length) continue;
      heading(3, counted(label, items.length, lang));
      out.push(...bullets(items.map(item =>
        inline(item.mention.text) + (lang === 'zh' ? '（' + (item.mention.start + 1) + '）：' : ' (' + (item.mention.start + 1) + '): ')
        + ((msg(lang).comparisonReasons as Record<string, string>)[item.code ?? ''] ?? item.reason))), '');
    }
    paragraph(md.comparisonNote);
  }

  paragraph(md.notCertifiedLine);
  return out.join('\n').replace(/\n{3,}/gu, '\n\n').trimEnd() + '\n';
}

/**
 * Structural probe for tests: counts markdown structure that appears OUTSIDE
 * code regions. The serializer emits every heading itself and never emits a
 * link or a raw HTML tag, so any surplus here means user text escaped.
 */
export function markdownStructure(markdown: string): { headings: string[]; links: number; htmlTags: number } {
  const headings: string[] = [];
  let links = 0;
  let htmlTags = 0;
  let fenceLength = 0;
  for (const line of markdown.split('\n')) {
    const fence = /^(`{3,})\s*$/u.exec(line);
    if (fence) {
      if (fenceLength === 0) fenceLength = fence[1].length;
      else if (fence[1].length >= fenceLength) fenceLength = 0;
      continue;
    }
    if (fenceLength > 0) continue;
    const outsideCode = line.replace(/`{2,} .*? `{2,}/gu, '').replace(/`[^`]*`/gu, '');
    const heading = /^#{1,6} (.*)$/u.exec(outsideCode);
    if (heading) headings.push(heading[1]);
    links += (outsideCode.match(/\]\(/gu) ?? []).length;
    htmlTags += (outsideCode.match(/<[a-zA-Z!/]/gu) ?? []).length;
  }
  return { headings, links, htmlTags };
}
