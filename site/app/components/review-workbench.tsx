'use client';

import { FormEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import {
  FILTERS, filterFindings, findingVerdictCounts, reconcileActiveId, compactScalarContext,
} from '../../lib/review-navigation.js';
import { highlightSegments, revisionOf, scalarToUtf16, summarizeReviewParts } from '../../lib/review-report.js';
import {
  MAX_CHOICES, MAX_WORKFILE_BYTES, basisOf, canonicalManuscript, compareReports, leaveGuardActive,
  makeChoice, manuscriptSaveView, mentionId, parseWorkfile, reconcileChoices, recoverableWorkPresent,
  recoverSpan, sameBasis, serializeWorkfile, validAlignments, validReport,
} from '../../lib/manuscript.js';
import { categoryLabel, labelJoiner } from '../../lib/dictionary-labels';
import { fill, msg, summarySentence } from '../../lib/messages';
import { isPool, POOL_REFRESH_EVENT, requestPoolRefresh, type Pool } from '../../lib/pool-snapshot';
import { numberLocale } from '../../lib/ui-language';
import { renderMarkdownReport, type MarkdownCurrency } from '../../lib/markdown-report';
import { useUiLanguage } from './ui-language-context';

type Span = { start: number; end: number; text: string };
type Alignment = { source: Span; target: Span | null };
type Candidate = { candidate_id: string; zh: string; en: string; category: string; sources: { source_file: string; source_id: string }[] };
type Finding = { id: string; rule_id: string; verdict: string; source_span: Span; target_span: Span | null; candidates: Candidate[]; candidates_truncated: boolean };
type Report = { request_id: string; source_revision: string; target_revision: string; rule_version: string; matcher_revision?: string; dictionary: { schema_version: string | null; source_commit: string | null; term_count: number; revision: string }; coverage: { evaluated: number; not_evaluated: number; rules: string[] }; findings: Finding[]; truncated: boolean };
type Draft = { source: string; target: string; direction: 'en' | 'zh'; alignments: Alignment[] | null };
type Choice = { source: string; direction: string; source_span: Span; scope: string; choice: string; candidate: Pick<Candidate, 'candidate_id' | 'zh' | 'en' | 'category'> | null; basis: ReturnType<typeof basisOf> };
type Resolution = { mention_id: string; choice: string; candidate_id?: string };
type Snapshot = Draft & { report: Report; trusted: boolean; signature: string; resolutions: Resolution[] | null };
type Undo = { draft: Draft; choices: Choice[]; imported: Choice[]; reports?: Snapshot[] };
// Status messages store language-independent codes (catalog notice keys, or
// structured error identities); the visible text resolves from the catalog on
// each render, so switching interface language re-localizes a message that is
// still on screen, including one stored by an async check or import.
type NoticeState = string | null;
type ErrorState = { reason?: string; code?: string; text?: string } | null;
const EMPTY: Draft = { source: '', target: '', direction: 'en', alignments: null };
type PoolView = { kind: 'loading' } | { kind: 'success'; data: Pool } | { kind: 'error' };
async function poolPayload(response: Response): Promise<unknown> {
  try { return await response.json(); } catch { return null; }
}
function signature(draft: Draft, resolutions: object[]) { return JSON.stringify({ ...draft, resolutions }); }
function Excerpt({ text, span, missing }: { text: string; span: Span | null; missing: string }) {
  const { lang } = useUiLanguage();
  const located = span ? highlightSegments(text, span) : null;
  const compact = located && span ? compactScalarContext(text, span) : null;
  if (!located || !compact) return <p className="review-excerpt">{missing}</p>;
  return <>
    <p className="review-excerpt">{compact.clippedBefore ? '…' : null}{compact.before}<mark>{compact.hit}</mark>{compact.after}{compact.clippedAfter ? '…' : null}</p>
    {(compact.clippedBefore || compact.clippedAfter) && <details className="review-context"><summary>{msg(lang).review.viewFullContext}</summary>
      <p className="review-excerpt">{located.before}<mark>{located.hit}</mark>{located.after}</p>
    </details>}
  </>;
}
function downloadFile(name: string, content: string, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement('a'); link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function cleanSnapshot(snapshot: Snapshot) {
  const { source, target, direction, alignments, report } = snapshot;
  return { source, target, direction, alignments, report, ...(snapshot.resolutions === null ? {} : { resolutions: snapshot.resolutions }) };
}
function manuscriptBody(nextDraft: Draft, nextChoices: Choice[], nextReports: Snapshot[]) {
  return { ...nextDraft, choices: nextChoices, history: nextReports.map(cleanSnapshot) };
}
function unloadUrl(anchor: HTMLAnchorElement): string | null {
  if (anchor.hasAttribute('download') || anchor.target === '_blank') return null;
  const raw = anchor.getAttribute('href');
  if (!raw || raw.startsWith('#')) return null;
  let url: URL;
  try { url = new URL(anchor.href, window.location.href); } catch { return null; }
  if (url.origin === window.location.origin && url.pathname === window.location.pathname && url.search === window.location.search) return null;
  return url.href;
}

export function ReviewWorkbench() {
  const { lang } = useUiLanguage();
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const { source, target, direction, alignments } = draft;
  const [choices, setChoices] = useState<Choice[]>([]);
  const [imported, setImported] = useState<Choice[]>([]);
  const [reports, setReports] = useState<Snapshot[]>([]);
  const [history, setHistory] = useState<Undo[]>([]);
  const [phase, setPhase] = useState<'idle' | 'loading' | 'success' | 'error' | 'cancelled'>('idle');
  const [notice, setNotice] = useState<NoticeState>(null);
  const [error, setError] = useState<ErrorState>(null);
  const [pendingDraft, setPendingDraft] = useState<Draft | null>(null);
  const [importedCanonical, setImportedCanonical] = useState<string | null>(null);
  const [downloadedCanonical, setDownloadedCanonical] = useState<string | null>(null);
  const [pendingLeaveUrl, setPendingLeaveUrl] = useState<string | null>(null);
  const [verdictFilter, setVerdictFilter] = useState<(typeof FILTERS)[number]['id']>('all');
  const [canonicalFindingId, setCanonicalFindingId] = useState<string | null>(null);
  const [pool, setPool] = useState<PoolView>({ kind: 'loading' });
  const [poolAttempt, setPoolAttempt] = useState(0);
  const sourceBox = useRef<HTMLTextAreaElement>(null);
  const targetBox = useRef<HTMLTextAreaElement>(null);
  const fileBox = useRef<HTMLInputElement>(null);
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const handoffDialog = useRef<HTMLDialogElement>(null);
  const keepDraftButton = useRef<HTMLButtonElement>(null);
  const leaveDialog = useRef<HTMLDialogElement>(null);
  const stayLeaveButton = useRef<HTMLButtonElement>(null);
  const leaveBypass = useRef(false);
  const work = useRef({ draft, choices, imported, reports, history });
  // The window listener must see edits made after mount, including reports
  // arriving while a replacement decision is open.
  useLayoutEffect(() => { work.current = { draft, choices, imported, reports, history }; }, [draft, choices, imported, reports, history]);
  const latest = reports.at(-1) ?? null;
  const previous = reports.at(-2) ?? null;
  const visibleFindings = latest ? filterFindings(latest.report.findings, verdictFilter) : [];
  const findingCounts = findingVerdictCounts(latest?.report.findings ?? []);
  const activeFindingId = reconcileActiveId(canonicalFindingId, visibleFindings);
  // A displayed fallback is the real selection; empty projections keep memory.
  if (activeFindingId !== null && activeFindingId !== canonicalFindingId) {
    setCanonicalFindingId(activeFindingId);
  }
  const activeVisibleIndex = visibleFindings.findIndex(finding => finding.id === activeFindingId);
  const reconciled = reconcileChoices(choices, draft, latest, imported);
  const ready = reconciled.flatMap(item => item.resolution ? [item.resolution] : []);
  const current = !!latest?.trusted && latest.signature === signature(draft, ready) && phase === 'success';
  const sourceLength = Array.from(source).length; const targetLength = Array.from(target).length;
  const validInput = !!source.trim() && !!target.trim() && sourceLength <= 2000 && targetLength <= 2000;
  const reviewsClosed = pool.kind === 'success' && pool.data.reviews.remaining === 0;
  const sourceCompatible = !!latest?.trusted && latest.source === source && latest.direction === direction;
  const comparison = latest && previous ? compareReports(previous, latest) : null;
  const basisChanged = !!latest?.trusted && choices.some(choice => !sameBasis(choice.basis, basisOf(latest.report)));
  const canonical = canonicalManuscript(manuscriptBody(draft, choices, reports));
  const holdsWork = recoverableWorkPresent({ source, target, choices, alignments, reports });
  const saveView = manuscriptSaveView({ holdsWork, canonical, importedCanonical, downloadedCanonical });
  const guard = leaveGuardActive({ view: saveView, checkInFlight: phase === 'loading' });
  const m = msg(lang);
  const saveStateText = {
    empty: m.review.saveStateEmpty,
    'page-only': m.review.saveStatePageOnly,
    'matches-import': m.review.saveStateMatchesImport,
    'matches-download': m.review.saveStateMatchesDownload,
    diverged: m.review.saveStateDiverged,
    'not-serializable': m.review.saveStateNotSerializable,
  }[saveView];
  const locale = numberLocale(lang);
  const reasonText = (item: { reason: string; code?: string }) =>
    ((m.choiceReasons as Record<string, string>)[item.code ?? ''] ?? item.reason);
  const noticeText = notice ? ((m.notices as Record<string, string>)[notice] ?? '') : '';
  const errorTextValue = !error ? ''
    // Nullish (not falsy) check: an empty reason string from a reason-less
    // error body must still land on the localized fallback, not a blank alert.
    : error.reason !== undefined && error.reason !== null ? ((m.rMsg as Record<string, string>)[error.reason] ?? m.rMsg.fallback)
    : error.code && (m.errors as Record<string, string>)[error.code] ? (m.errors as Record<string, string>)[error.code]!
    : error.text ?? '';
  // Library throws (parse/serialize) always carry stable codes; an uncoded
  // failure is a browser-level error (unreadable file and similar), whose
  // raw message is neither localizable nor fixed copy — use the catalog key.
  const errorFrom = (cause: unknown, fallbackKey: string): ErrorState => {
    const code = cause instanceof Error && 'code' in cause ? String((cause as Error & { code?: unknown }).code) : '';
    return code && (msg(lang).errors as Record<string, string>)[code] ? { code } : { code: fallbackKey };
  };

  const discardInFlight = useCallback(() => {
    controller.current?.abort(); controller.current = null; generation.current += 1; setPhase('idle'); setError(null);
  }, []);
  const receiveDraft = useCallback((next: Draft) => {
    handoffDialog.current?.close(); setPendingDraft(null);
    const before = work.current;
    if (before.draft.source === next.source && before.draft.target === next.target && before.draft.direction === next.direction) return;
    const saved: Undo = { draft: before.draft, choices: before.choices, imported: before.imported, reports: before.reports };
    const nextHistory = [...before.history.slice(-19), saved];
    // Update synchronously as well so successive events in one turn cannot
    // mistake an already received manuscript for an empty workbench.
    work.current = { draft: next, choices: [], imported: [], reports: [], history: nextHistory };
    setHistory(nextHistory); discardInFlight(); setChoices([]); setImported([]); setReports([]); setDraft(next);
    setNotice('receivedDraft');
    sourceBox.current?.focus();
  }, [discardInFlight]);
  function remember() { setHistory(stack => [...stack.slice(-19), { draft, choices, imported }]); }
  function edit(next: Draft, message = '') {
    remember(); discardInFlight(); setDraft(next); setNotice(message);
  }
  function editText(side: 'source' | 'target', value: string) {
    edit({ ...draft, [side]: value, alignments: alignments === null ? null : [] },
      alignments !== null ? 'editedAlignments' : 'editedText');
  }
  useEffect(() => {
    function onDraft(event: Event) {
      const detail = (event as CustomEvent).detail;
      if (!detail || typeof detail.source !== 'string' || typeof detail.target !== 'string' || !['en', 'zh'].includes(detail.direction)) return;
      const before = work.current;
      if (before.draft.source === detail.source && before.draft.target === detail.target && before.draft.direction === detail.direction) return;
      const next: Draft = { source: detail.source, target: detail.target, direction: detail.direction, alignments: null };
      const empty = before.draft.source === '' && before.draft.target === '' && before.draft.alignments === null
        && !before.choices.length && !before.imported.length && !before.reports.length && !before.history.length;
      if (empty) receiveDraft(next);
      else setPendingDraft(next);
    }
    window.addEventListener('wuwaterm-send-review', onDraft);
    return () => window.removeEventListener('wuwaterm-send-review', onDraft);
  }, [receiveDraft]);
  useEffect(() => () => { controller.current?.abort(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch('/api/pool', { cache: 'no-store', signal: controller.signal });
        const value = await poolPayload(response);
        if (!controller.signal.aborted) setPool(response.ok && isPool(value) ? { kind: 'success', data: value } : { kind: 'error' });
      } catch { if (!controller.signal.aborted) setPool({ kind: 'error' }); }
    })();
    return () => controller.abort();
  }, [poolAttempt]);
  useEffect(() => {
    const onRefresh = () => setPoolAttempt(count => count + 1);
    window.addEventListener(POOL_REFRESH_EVENT, onRefresh);
    return () => window.removeEventListener(POOL_REFRESH_EVENT, onRefresh);
  }, []);
  useEffect(() => {
    if (!activeFindingId) return;
    document.querySelector(`[data-mention-id="${CSS.escape(activeFindingId)}"]`)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [activeFindingId]);
  useEffect(() => {
    if (pendingDraft) {
      if (!handoffDialog.current?.open) handoffDialog.current?.showModal();
      keepDraftButton.current?.focus();
    } else handoffDialog.current?.close();
  }, [pendingDraft]);
  useEffect(() => {
    if (pendingLeaveUrl) {
      if (!leaveDialog.current?.open) leaveDialog.current?.showModal();
      stayLeaveButton.current?.focus();
    } else leaveDialog.current?.close();
  }, [pendingLeaveUrl]);
  useEffect(() => {
    if (!guard) return;
    function onClick(event: MouseEvent) {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if (!(event.target instanceof Element)) return;
      const anchor = event.target.closest('a');
      if (!(anchor instanceof HTMLAnchorElement)) return;
      const url = unloadUrl(anchor);
      if (!url) return;
      event.preventDefault();
      event.stopPropagation();
      setPendingLeaveUrl(url);
    }
    document.addEventListener('click', onClick, true);
    return () => document.removeEventListener('click', onClick, true);
  }, [guard]);
  useEffect(() => {
    if (!guard) return;
    function onBeforeUnload(event: BeforeUnloadEvent) {
      if (leaveBypass.current) return;
      event.preventDefault();
      event.returnValue = '1';
    }
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [guard]);

  async function check(withChoices = true) {
    if (!validInput || reviewsClosed) return;
    discardInFlight(); const mine = generation.current; const abort = new AbortController(); controller.current = abort;
    const resolutions = withChoices ? ready : [];
    const submitted = draft;
    const submittedSignature = signature(submitted, resolutions);
    setPhase('loading'); setNotice(null);
    const body = {
      source, target, direction, review_version: 'review-v2',
      ...(alignments === null ? {} : { alignments }),
      ...(resolutions.length && latest ? { resolutions, resolution_context: {
        source_revision: latest.report.source_revision, rule_version: latest.report.rule_version, dictionary_revision: latest.report.dictionary.revision, matcher_revision: latest.report.matcher_revision,
      } } : {}),
    };
    if (new TextEncoder().encode(JSON.stringify(body)).length > 32768) {
      controller.current = null; setPhase('error'); setError({ code: 'bodyTooLarge' }); return;
    }
    try {
      const response = await fetch('/api/reviews', { method: 'POST', headers: { 'content-type': 'application/json' }, cache: 'no-store', body: JSON.stringify(body), signal: abort.signal });
      const value = await response.json();
      if (abort.signal.aborted || generation.current !== mine) return;
      if (!response.ok) {
        const reason = value && typeof value === 'object' && 'reason' in value && typeof value.reason === 'string' ? value.reason : '';
        throw Object.assign(new Error((m.rMsg as Record<string, string>)[reason] ?? m.rMsg.fallback), { gahReason: reason });
      }
      if (!validReport(value, source, target)) throw Object.assign(new Error(m.errors.responseInvalid), { gahCode: 'responseInvalid' });
      const report = value as Report;
      if (report.rule_version !== 'review-v2' || !/^[0-9a-f]{64}$/u.test(report.matcher_revision ?? '')
        || report.source_revision !== await revisionOf(source)
        || report.target_revision !== await revisionOf(target)) throw Object.assign(new Error(m.errors.responseBasisMismatch), { gahCode: 'responseBasisMismatch' });
      if (abort.signal.aborted || generation.current !== mine) return;
      const snapshot: Snapshot = { ...submitted, report, trusted: true, signature: submittedSignature, resolutions };
      setReports(stack => [...stack.slice(-1), snapshot]); setPhase('success');
      setNotice('checked');
      requestPoolRefresh();
    } catch (cause) {
      if (!abort.signal.aborted && generation.current === mine) {
        setPhase('error');
        // Unmarked failures here are network-layer rejections or malformed
        // JSON bodies; both use the fixed catalog copy instead of browser
        // error text (public UI errors stay fixed copy and re-localizable).
        setError(cause instanceof Error && 'gahReason' in cause ? { reason: String((cause as Error & { gahReason?: unknown }).gahReason) }
          : cause instanceof Error && 'gahCode' in cause ? { code: String((cause as Error & { gahCode?: unknown }).gahCode) }
          : { code: 'requestFailed' });
        requestPoolRefresh();
      }
    } finally { if (controller.current === abort) controller.current = null; }
  }
  async function submit(event: FormEvent) { event.preventDefault(); await check(); }
  function choose(finding: Finding, candidate: Candidate | null) {
    if (!sourceCompatible || !latest) return;
    const span = recoverSpan(latest.source, finding.source_span, source);
    if (!span) return;
    const selected = makeChoice({ source, direction, alignments, report: latest.report, finding: { ...finding, source_span: span }, candidate }) as Choice;
    const existing = choices.findIndex(c => {
      const recovered = recoverSpan(c.source, c.source_span, source);
      return recovered && mentionId(recovered) === mentionId(span);
    });
    if (existing < 0 && choices.length >= MAX_CHOICES) { setNotice('choiceLimit'); return; }
    remember(); discardInFlight();
    setChoices(old => existing < 0 ? [...old, selected] : old.map((c, i) => i === existing ? selected : c));
    setImported(old => old.filter(c => c !== choices[existing]));
    setNotice('choiceStaged');
  }
  function jumpTo(box: HTMLTextAreaElement | null, text: string, span: Span) {
    if (!box) return;
    const located = highlightSegments(text, span);
    const start = scalarToUtf16(text, span.start);
    const end = scalarToUtf16(text, span.end);
    if (!located || start === null || end === null) return;
    box.focus({ preventScroll: true });
    // Textareas expose LF line endings even when an imported file retains CRLF.
    // Keep the report coordinates and saved text raw; map only the DOM offsets.
    const domOffset = (offset: number) => text.slice(0, offset).replace(/\r\n?/g, '\n').length;
    box.setSelectionRange(domOffset(start), domOffset(end));
    box.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  function replace(finding: Finding, candidate: Candidate) {
    if (!latest || latest.target !== target || !sourceCompatible || !finding.target_span) return;
    const parts = highlightSegments(target, finding.target_span);
    const forms = finding.candidates.flatMap(c => [c.zh, c.en]);
    if (!parts || !forms.includes(finding.target_span.text)) return;
    editText('target', parts.before + (direction === 'en' ? candidate.en : candidate.zh) + parts.after);
  }
  function selectedSpan(box: HTMLTextAreaElement | null, text: string): Span | null {
    if (!box || box.selectionStart === box.selectionEnd) return null;
    const rawOffset = (offset: number) => {
      let raw = 0;
      for (let dom = 0; dom < offset; dom += 1) {
        if (raw >= text.length) return null;
        raw += text[raw] === '\r' && text[raw + 1] === '\n' ? 2 : 1;
      }
      return raw;
    };
    const start = rawOffset(box.selectionStart);
    const end = rawOffset(box.selectionEnd);
    if (start === null || end === null) return null;
    return { start: Array.from(text.slice(0, start)).length, end: Array.from(text.slice(0, end)).length, text: text.slice(start, end) };
  }
  function addAlignment(omit = false, whole = false) {
    const a = whole ? { start: 0, end: sourceLength, text: source } : selectedSpan(sourceBox.current, source);
    const b = omit ? null : whole ? { start: 0, end: targetLength, text: target } : selectedSpan(targetBox.current, target);
    if (!a || (!omit && !b)) { setNotice('alignmentHint'); return; }
    const next = [...(whole ? [] : alignments ?? []), { source: a, target: b }].sort((x, y) => x.source.start - y.source.start);
    if (!validAlignments(next, source, target)) { setNotice('alignmentInvalid'); return; }
    edit({ ...draft, alignments: next }, 'alignmentRecorded');
  }
  function undo() {
    const last = history.at(-1); if (!last) return;
    discardInFlight(); setDraft(last.draft); setChoices(last.choices); setImported(last.imported); setHistory(stack => stack.slice(0, -1));
    if (last.reports) setReports(last.reports);
    setNotice(last.reports ? 'undoWithReports' : 'undoLocal');
  }
  async function importFile(file?: File) {
    if (!file) return;
    if (file.size > MAX_WORKFILE_BYTES) { setError({ code: 'workfile_too_large' }); return; }
    const mine = generation.current;
    try {
      const value = parseWorkfile(await file.text());
      if (generation.current !== mine) { setNotice('importChangedDuringRead'); return; }
      remember(); discardInFlight();
      const nextDraft: Draft = { source: value.source, target: value.target, direction: value.direction, alignments: value.alignments };
      const nextChoices = value.choices as Choice[];
      const nextReports: Snapshot[] = value.history.map((s: Draft & { report: Report; resolutions?: Resolution[] }) => ({ ...s, resolutions: s.resolutions ?? null, trusted: false, signature: '' }));
      const importedBody = canonicalManuscript(manuscriptBody(nextDraft, nextChoices, nextReports));
      setDraft(nextDraft); setChoices(nextChoices); setImported(nextChoices); setReports(nextReports);
      if (importedBody) setImportedCanonical(importedBody);
      setNotice('imported');
    } catch (cause) {
      setError(errorFrom(cause, 'importInvalid'));
    }
    finally { if (fileBox.current) fileBox.current.value = ''; }
  }
  function save() {
    try {
      const content = serializeWorkfile(manuscriptBody(draft, choices, reports));
      downloadFile('wuwaterm-manuscript.json', content);
      setDownloadedCanonical(content);
      setError(null);
      setNotice('saved');
    } catch (cause) {
      setError(errorFrom(cause, 'saveFailed'));
    }
  }
  function exportTxt() {
    downloadFile('wuwaterm-translation.txt', target, 'text/plain;charset=utf-8');
    setNotice('exportTxt');
  }
  function exportResult() {
    // The exported JSON keeps the lib's canonical Chinese reason/label strings
    // so the file format stays byte-stable regardless of interface language.
    const strip = (items: { label: string; reason: string }[]) => items.map(({ label, reason }) => ({ label, reason }));
    downloadFile('wuwaterm-result.json', JSON.stringify({ format: 'wuwaterm-result-v2', ...draft,
      status: current ? 'current_terminology_check' : 'requires_recheck', sentence_meaning_evaluated: false,
      report: latest ? cleanSnapshot(latest) : null, report_is_current: current,
      choices: choices.map((choice, i) => ({ ...choice, status: reconciled[i].status, reason: reconciled[i].reason })),
      changes: current && comparison ? { new: strip(comparison.new), resolved: strip(comparison.resolved), pending: strip(comparison.pending), incomparable: strip(comparison.incomparable) } : null,
      verified_stamp: { valid: false },
    }, null, 2));
    setNotice(current ? 'exportCurrent' : 'exportStale');
  }
  function exportMarkdown() {
    // Read-only local snapshot: no requests, no state change; the document
    // renders the full report regardless of the UI's finding filter.
    const currency: MarkdownCurrency = !latest ? 'none' : current ? 'current' : latest.trusted ? 'stale' : 'imported';
    const strip = (snapshot: Snapshot) => ({
      source: snapshot.source, target: snapshot.target, direction: snapshot.direction,
      report: snapshot.report, resolutions: snapshot.resolutions,
    });
    downloadFile('wuwaterm-report.md', renderMarkdownReport({
      draft: { source, target, direction },
      latest: latest ? strip(latest) : null,
      previous: previous ? strip(previous) : null,
      currency,
      choices,
      reconciled,
      comparison,
    }, lang), 'text/markdown;charset=utf-8');
    setNotice('exportMarkdown');
  }
  const summaryParts = latest ? summarizeReviewParts(latest.report) : null;
  const comparisonGroups = ([['new', m.review.comparisonNew], ['resolved', m.review.comparisonResolved], ['pending', m.review.comparisonPending], ['incomparable', m.review.comparisonIncomparable]] as const)
    .map(([key, label]) => ({ key, label, items: comparison ? comparison[key] : [] }));
  return <section className="workspace-card review-card" aria-labelledby="review-title">
    <dialog className="handoff-dialog" ref={leaveDialog} aria-labelledby="leave-title" aria-describedby="leave-description" onCancel={() => setPendingLeaveUrl(null)}>
      <h3 id="leave-title">{m.review.leaveTitle}</h3>
      <div id="leave-description">
        {phase === 'loading' && <p>{m.review.leaveInFlight}</p>}
        <p>{saveView === 'matches-download' ? m.review.leaveDownload : m.review.leaveUnsaved}</p>
      </div>
      {error && <p role="alert">{errorTextValue}</p>}
      <div className="actions">
        <button ref={stayLeaveButton} type="button" onClick={() => setPendingLeaveUrl(null)}>{m.review.leaveStay}</button>
        <button type="button" className="secondary-button" onClick={save}>{m.review.leaveSaveStay}</button>
        <button type="button" className="secondary-button" onClick={() => { const url = pendingLeaveUrl; if (!url) { setPendingLeaveUrl(null); return; } leaveBypass.current = true; window.location.assign(url); }}>{m.review.leaveAnyway}</button>
      </div>
    </dialog>
    <dialog className="handoff-dialog" ref={handoffDialog} aria-labelledby="handoff-title" aria-describedby="handoff-description" onCancel={() => setPendingDraft(null)}>
      <h3 id="handoff-title">{m.review.dialogTitle}</h3>
      <p id="handoff-description">{m.review.dialogBody}</p>
      <div className="actions">
        <button ref={keepDraftButton} type="button" className="secondary-button" onClick={() => { handoffDialog.current?.close(); setPendingDraft(null); }}>{m.review.dialogKeep}</button>
        <button type="button" className="secondary-button" onClick={() => { if (pendingDraft) receiveDraft(pendingDraft); }}>{m.review.dialogReplace}</button>
      </div>
    </dialog>
    <div className="card-heading"><div><p className="section-kicker">{m.review.kicker}</p><h2 id="review-title">{m.review.title}</h2></div><span className="tag">{m.review.tag}</span></div>
    <p className="card-intro">{m.review.intro}</p>
    {!latest && !source.trim() && !target.trim() && choices.length === 0 && (
      <ol className="guide-steps" aria-label={m.review.guideAria} role="list">
        {m.review.guide.map((step, i) => <li key={i}><strong>{step.title}</strong><span>{step.body}</span></li>)}
      </ol>
    )}
    <p className="manuscript-save-state" role="status">{saveStateText}{guard ? ` ${m.review.saveStateLeaveNote}` : ''}</p>
    <div className="actions draft-actions">
      <button type="button" className="secondary-button" onClick={save}>{m.review.save}</button>
      <button type="button" className="secondary-button" onClick={() => fileBox.current?.click()}>{m.review.import}</button>
      <input ref={fileBox} className="visually-hidden" type="file" accept=".json,application/json" aria-label={m.review.importFileAria} onChange={e => void importFile(e.target.files?.[0])} />
      <button type="button" className="secondary-button" onClick={exportTxt}>{m.review.exportTxt}</button>
      <button type="button" className="secondary-button" onClick={exportResult}>{m.review.exportResult}</button>
      <button type="button" className="secondary-button" onClick={exportMarkdown}>{m.review.exportMarkdown}</button>
    </div>
    <form onSubmit={submit}>
      <div className="label-row"><label htmlFor="review-source">{m.review.sourceLabel}</label><select aria-label={m.review.directionAria} value={direction} onChange={e => edit({ ...draft, direction: e.target.value as 'en' | 'zh', alignments: alignments === null ? null : [] }, 'directionChanged')}><option value="en">{m.review.directionEn}</option><option value="zh">{m.review.directionZh}</option></select></div>
      <textarea ref={sourceBox} id="review-source" value={source} onChange={e => editText('source', e.target.value)} rows={5} placeholder={m.review.sourcePlaceholder} />
      <label htmlFor="review-target">{m.review.targetLabel}</label>
      <textarea ref={targetBox} id="review-target" value={target} onChange={e => editText('target', e.target.value)} rows={5} placeholder={m.review.targetPlaceholder} />
      <div className="field-hint"><span>{m.review.hintSafety}</span><span>{fill(m.review.charCounts, { s: sourceLength.toLocaleString(locale), t: targetLength.toLocaleString(locale) })}</span></div>
      <details className="alignment-panel"><summary>{alignments === null ? m.review.alignmentSummaryNone : fill(m.review.alignmentSummaryCount, { n: alignments.length })}</summary>
        <p>{m.review.alignmentIntro}</p>
        <div className="actions">
          <button type="button" className="secondary-button" onClick={() => addAlignment()}>{m.review.alignmentConfirm}</button>
          <button type="button" className="secondary-button" onClick={() => addAlignment(true)}>{m.review.alignmentOmit}</button>
          <button type="button" className="secondary-button" disabled={!validInput} onClick={() => addAlignment(false, true)}>{m.review.alignmentWhole}</button>
          <button type="button" className="secondary-button" onClick={() => edit({ ...draft, alignments: [] }, 'alignmentsCleared')}>{m.review.alignmentClear}</button>
          <button type="button" className="secondary-button" onClick={() => edit({ ...draft, alignments: null }, 'alignmentsReset')}>{m.review.alignmentReset}</button>
        </div>
        {alignments?.map((a, i) => <div className="alignment-row" key={i}><p>{fill(m.review.alignmentRowSource, { start: a.source.start + 1, end: a.source.end, text: a.source.text })}</p><p>{a.target ? fill(m.review.alignmentRowTarget, { start: a.target.start + 1, end: a.target.end, text: a.target.text }) : m.review.alignmentRowOmit}</p><button type="button" className="text-button" onClick={() => edit({ ...draft, alignments: alignments.filter((_, j) => i !== j) }, 'alignmentRemoved')}>{m.review.alignmentRemove}</button></div>)}
      </details>
      <div className="review-quota">
        {pool.kind === 'success' ? <>
          <p>{fill(m.review.quotaSnapshot, { n: pool.data.reviews.remaining, limit: pool.data.reviews.limit })}</p>
          {reviewsClosed && <p>{m.rMsg.reviews_pool_exhausted}</p>}
        </> : <p>{pool.kind === 'loading' ? m.pool.loading : m.pool.unavailable}</p>}
      </div>
      <div className="actions">
        <button type="submit" disabled={!validInput || phase === 'loading' || reviewsClosed}>{phase === 'loading' ? m.review.checkLoading : m.review.checkIdle}</button>
        <button className="secondary-button" type="button" disabled={!validInput || phase === 'loading' || reviewsClosed} onClick={() => void check(false)}>{m.review.freshBasis}</button>
        {phase === 'loading' && <button className="secondary-button" type="button" onClick={() => { discardInFlight(); setPhase('cancelled'); setNotice('cancelledWait'); requestPoolRefresh(); }}>{m.review.cancelWait}</button>}
        <button className="secondary-button" type="button" onClick={undo} disabled={!history.length}>{m.review.undo}</button>
      </div>
    </form>
    {notice && <p className="notice-state" role="status">{noticeText}</p>}
    {error && <p className="error-panel" role="alert">{errorTextValue}</p>}
    {phase === 'loading' && <p role="status">{m.review.loadingStatus}</p>}
    {basisChanged && <p className="review-stale">{m.review.staleBasis}</p>}
    {choices.length > 0 && <div className="choice-records"><h3>{fill(m.review.choiceHeader, { ready: ready.length, pending: choices.length - ready.length })}</h3>
      {choices.map((choice, i) => <div className="choice-record" key={i}><p>{choice.candidate ? fill(m.review.choicePair, { span: choice.source_span.text, zh: choice.candidate.zh, en: choice.candidate.en }) : fill(m.review.choiceNotTerm, { span: choice.source_span.text })} — {reconciled[i].status === 'applicable' ? m.review.choiceApplicable : m.review.choicePending}</p><p>{reasonText(reconciled[i])}</p><button type="button" className="text-button" onClick={() => { remember(); discardInFlight(); setChoices(old => old.filter((_, j) => i !== j)); }}>{m.review.choiceRemove}</button></div>)}
    </div>}
    {latest ? <>
      <div className="review-summary" aria-live="polite">
        {summaryParts ? <p>{summarySentence(summaryParts, lang)}</p> : <p>{m.summary.detailMissing}</p>}
        {current ? <p className="review-current">{m.review.summaryCurrent}</p> : <p className="review-stale">{latest.trusted ? m.review.summaryStale : m.review.summaryImported}</p>}
        <p>{fill(m.review.ruleLine, { rule: latest.report.rule_version, commit: latest.report.dictionary.source_commit?.slice(0, 12) ?? m.review.dictUnknown })}</p>
      </div>
      {comparison && <details className="report-changes" open><summary>{m.review.comparisonTitle}{current ? '' : m.review.comparisonSuffix}</summary>
        {comparisonGroups.map(group => <div key={group.key}><h4>{group.label} · {group.items.length}</h4>{group.items.map((item: { label: string; reason: string; code?: string; mention: { text: string; start: number; historical: boolean } }, i: number) => <p key={i}>{fill(item.mention.historical ? m.review.comparisonMentionHistorical : m.review.comparisonMention, { text: item.mention.text, n: item.mention.start + 1 })}：{((m.comparisonReasons as Record<string, string>)[item.code ?? ''] ?? item.reason)}</p>)}</div>)}
        <p>{m.review.comparisonNote}</p>
      </details>}
      <div className="finding-nav" role="group" aria-label={m.review.filterAria}>
        {FILTERS.map(item => <button key={item.id} type="button" className="secondary-button" aria-pressed={verdictFilter === item.id} onClick={() => setVerdictFilter(item.id)}>{(m.review.filters as Record<string, string>)[item.id] ?? item.label} {findingCounts[item.id as keyof typeof findingCounts]}</button>)}
      </div>
      <div className="finding-nav" role="group" aria-label={m.review.navAria}>
        <button type="button" className="secondary-button" disabled={activeVisibleIndex <= 0} onClick={() => { const previousFinding = visibleFindings[activeVisibleIndex - 1]; if (previousFinding) setCanonicalFindingId(previousFinding.id); }}>{m.review.navPrev}</button>
        <button type="button" className="secondary-button" disabled={activeVisibleIndex < 0 || activeVisibleIndex >= visibleFindings.length - 1} onClick={() => { const nextFinding = visibleFindings[activeVisibleIndex + 1]; if (nextFinding) setCanonicalFindingId(nextFinding.id); }}>{m.review.navNext}</button>
        <p className="finding-position">{fill(m.review.position, { i: activeVisibleIndex < 0 ? 0 : activeVisibleIndex + 1, n: visibleFindings.length })}</p>
      </div>
      <div className="review-findings">{latest.report.findings.length === 0 && <p>{m.review.noFindings}</p>}
        {visibleFindings.length === 0 && latest.report.findings.length > 0 && <p>{m.review.noFindingsFiltered}</p>}
        {visibleFindings.map((finding: Finding) => {
          const active = finding.id === activeFindingId;
          const sourceLocated = latest.source === source && highlightSegments(source, finding.source_span) !== null;
          const targetLocated = finding.target_span !== null && latest.target === target && highlightSegments(target, finding.target_span) !== null;
          return <article className="term-result" key={finding.id} data-mention-id={finding.id} data-active={active ? 'true' : 'false'} aria-current={active ? 'true' : undefined}><p className="result-summary">{active ? m.review.currentMark : ''}{(m.review.verdicts as Record<string, string>)[finding.verdict]} · {fill(m.review.spanRange, { start: finding.source_span.start + 1, end: finding.source_span.end })}{finding.candidates_truncated ? m.review.truncatedMark : ''}</p>
            <Excerpt text={source} span={sourceLocated ? finding.source_span : null} missing={fill(m.review.excerptMissingSource, { text: finding.source_span.text })} />
            <Excerpt text={target} span={targetLocated ? finding.target_span : null} missing={m.review.excerptMissingTarget} />
            <div className="finding-jumps">
              <button className="text-button" type="button" disabled={!sourceLocated} onClick={() => jumpTo(sourceBox.current, source, finding.source_span)}>{m.review.jumpSource}</button>
              <button className="text-button" type="button" disabled={!targetLocated} onClick={() => { if (finding.target_span) jumpTo(targetBox.current, target, finding.target_span); }}>{m.review.jumpTarget}</button>
            </div>
            {finding.candidates.map((candidate: Candidate) => <div className="term-pair" key={candidate.candidate_id}><strong>{candidate.zh}</strong><span>{candidate.en}</span><small>{m.terms.category}{labelJoiner(lang)}{categoryLabel(lang, candidate.category)}</small>
              <button className="text-button" type="button" disabled={!sourceCompatible || phase === 'loading' || latest.report.truncated} onClick={() => choose(finding, candidate)}>{m.review.adopt}</button>
              {targetLocated && <button className="text-button" type="button" disabled={!sourceCompatible || phase === 'loading'} onClick={() => replace(finding, candidate)}>{m.review.replace}</button>}
              <details className="candidate-sources"><summary>{m.review.viewSources}</summary>
                <ul>{candidate.sources.map(item => <li key={`${item.source_file}\u0000${item.source_id}`}><span>{item.source_file}</span><span>{item.source_id}</span></li>)}</ul>
              </details>
            </div>)}
            <button className="text-button" type="button" disabled={!sourceCompatible || phase === 'loading'} onClick={() => choose(finding, null)}>{m.review.notTerm}</button>
          </article>;
        })}
      </div>
    </> : <p className="translation-placeholder">{m.review.placeholder}</p>}
  </section>;
}
