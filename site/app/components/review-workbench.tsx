'use client';

import { FormEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { highlightSegments, revisionOf, summarizeReview } from '../../lib/review-report.js';
import {
  MAX_CHOICES, MAX_WORKFILE_BYTES, basisOf, compareReports, makeChoice, mentionId,
  parseWorkfile, reconcileChoices, recoverSpan, sameBasis, serializeWorkfile, validAlignments, validReport,
} from '../../lib/manuscript.js';

type Span = { start: number; end: number; text: string };
type Alignment = { source: Span; target: Span | null };
type Candidate = { candidate_id: string; zh: string; en: string; category: string; sources: { source_file: string; source_id: string }[] };
type Finding = { id: string; rule_id: string; verdict: string; source_span: Span; target_span: Span | null; candidates: Candidate[]; candidates_truncated: boolean };
type Report = { request_id: string; source_revision: string; target_revision: string; rule_version: string; dictionary: { schema_version: string | null; source_commit: string | null; term_count: number; revision: string }; coverage: { evaluated: number; not_evaluated: number; rules: string[] }; findings: Finding[]; truncated: boolean };
type Draft = { source: string; target: string; direction: 'en' | 'zh'; alignments: Alignment[] | null };
type Choice = { source: string; direction: string; source_span: Span; scope: string; choice: string; candidate: Pick<Candidate, 'candidate_id' | 'zh' | 'en' | 'category'> | null; basis: ReturnType<typeof basisOf> };
type Resolution = { mention_id: string; choice: string; candidate_id?: string };
type Snapshot = Draft & { report: Report; trusted: boolean; signature: string; resolutions: Resolution[] | null };
type Undo = { draft: Draft; choices: Choice[]; imported: Choice[]; reports?: Snapshot[] };
const EMPTY: Draft = { source: '', target: '', direction: 'en', alignments: null };
const VERDICTS: Record<string, string> = { verified_constraint: '术语约束已核', confirmed_conflict: '与所选词对冲突', needs_review: '需要核对', not_evaluated: '未评估' };
const MESSAGES: Record<string, string> = {
  reviews_pool_exhausted: '今日审校共享额度已用完，请在次日 UTC 00:00 后再来。',
  shared_pool_busy: '共享公测池正在忙碌，请稍后重试。',
  shared_pool_unavailable: '暂时无法确认共享额度，已暂停接收请求。',
  invalid_request: '输入或选择依据已变化。稿件已保留；可先“仅获取新依据”，再重新确认选择。',
  site_invalid_request: '文本、位置或选择格式无效，请检查后重试。',
  upstream_timeout: '等待超时，稿件已保留。已获准的请求可能仍在处理，额度不会返还。',
  upstream_rate_limited: '服务繁忙，请稍后重试。',
};
function signature(draft: Draft, resolutions: object[]) { return JSON.stringify({ ...draft, resolutions }); }
function downloadFile(name: string, content: string, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement('a'); link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function cleanSnapshot(snapshot: Snapshot) {
  const { source, target, direction, alignments, report } = snapshot;
  return { source, target, direction, alignments, report, ...(snapshot.resolutions === null ? {} : { resolutions: snapshot.resolutions }) };
}

export function ReviewWorkbench() {
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const { source, target, direction, alignments } = draft;
  const [choices, setChoices] = useState<Choice[]>([]);
  const [imported, setImported] = useState<Choice[]>([]);
  const [reports, setReports] = useState<Snapshot[]>([]);
  const [history, setHistory] = useState<Undo[]>([]);
  const [phase, setPhase] = useState<'idle' | 'loading' | 'success' | 'error' | 'cancelled'>('idle');
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [pendingDraft, setPendingDraft] = useState<Draft | null>(null);
  const sourceBox = useRef<HTMLTextAreaElement>(null);
  const targetBox = useRef<HTMLTextAreaElement>(null);
  const fileBox = useRef<HTMLInputElement>(null);
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const handoffDialog = useRef<HTMLDialogElement>(null);
  const keepDraftButton = useRef<HTMLButtonElement>(null);
  const work = useRef({ draft, choices, imported, reports, history });
  // The window listener must see edits made after mount, including reports
  // arriving while a replacement decision is open.
  useLayoutEffect(() => { work.current = { draft, choices, imported, reports, history }; }, [draft, choices, imported, reports, history]);
  const latest = reports.at(-1) ?? null;
  const previous = reports.at(-2) ?? null;
  const reconciled = reconcileChoices(choices, draft, latest, imported);
  const ready = reconciled.flatMap(item => item.resolution ? [item.resolution] : []);
  const current = !!latest?.trusted && latest.signature === signature(draft, ready) && phase === 'success';
  const sourceLength = Array.from(source).length; const targetLength = Array.from(target).length;
  const validInput = !!source.trim() && !!target.trim() && sourceLength <= 2000 && targetLength <= 2000;
  const sourceCompatible = !!latest?.trusted && latest.source === source && latest.direction === direction;
  const comparison = latest && previous ? compareReports(previous, latest) : null;
  const basisChanged = !!latest?.trusted && choices.some(choice => !sameBasis(choice.basis, basisOf(latest.report)));

  const discardInFlight = useCallback(() => {
    controller.current?.abort(); controller.current = null; generation.current += 1; setPhase('idle'); setError('');
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
    setNotice('已接收译文，可撤销此次交接。需要续作时，请主动保存稿件文件。');
    sourceBox.current?.focus();
  }, [discardInFlight]);
  function remember() { setHistory(stack => [...stack.slice(-19), { draft, choices, imported }]); }
  function edit(next: Draft, message = '') {
    remember(); discardInFlight(); setDraft(next); setNotice(message);
  }
  function editText(side: 'source' | 'target', value: string) {
    edit({ ...draft, [side]: value, alignments: alignments === null ? null : [] },
      alignments !== null ? '文本已编辑：原分段对应已取消确认，请重新选择范围。术语选择保留为待核对记录。' : '文本已编辑；旧报告已失效，请重新核对。');
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
    if (pendingDraft) {
      if (!handoffDialog.current?.open) handoffDialog.current?.showModal();
      keepDraftButton.current?.focus();
    } else handoffDialog.current?.close();
  }, [pendingDraft]);

  async function check(withChoices = true) {
    if (!validInput) return;
    discardInFlight(); const mine = generation.current; const abort = new AbortController(); controller.current = abort;
    const resolutions = withChoices ? ready : [];
    const submitted = draft;
    const submittedSignature = signature(submitted, resolutions);
    setPhase('loading'); setNotice('');
    const body = {
      source, target, direction, review_version: 'review-v2',
      ...(alignments === null ? {} : { alignments }),
      ...(resolutions.length && latest ? { resolutions, resolution_context: {
        source_revision: latest.report.source_revision, rule_version: latest.report.rule_version, dictionary_revision: latest.report.dictionary.revision,
      } } : {}),
    };
    if (new TextEncoder().encode(JSON.stringify(body)).length > 32768) {
      controller.current = null; setPhase('error'); setError('文字与对应范围合计超过请求上限（32 KiB）。请减少对应范围或缩短稿件；尚未发送请求。'); return;
    }
    try {
      const response = await fetch('/api/reviews', { method: 'POST', headers: { 'content-type': 'application/json' }, cache: 'no-store', body: JSON.stringify(body), signal: abort.signal });
      const value = await response.json();
      if (abort.signal.aborted || generation.current !== mine) return;
      if (!response.ok) {
        const reason = value && typeof value === 'object' && 'reason' in value && typeof value.reason === 'string' ? value.reason : '';
        throw new Error(MESSAGES[reason] ?? '服务暂时不可用。稿件已保留，请稍后重试。');
      }
      if (!validReport(value, source, target)) throw new Error('响应格式无效，未采用此报告。');
      const report = value as Report;
      if (report.rule_version !== 'review-v2' || report.source_revision !== await revisionOf(source)
        || report.target_revision !== await revisionOf(target)) throw new Error('响应依据与当前文本不一致，未采用此报告。');
      if (abort.signal.aborted || generation.current !== mine) return;
      const snapshot: Snapshot = { ...submitted, report, trusted: true, signature: submittedSignature, resolutions };
      setReports(stack => [...stack.slice(-1), snapshot]); setPhase('success');
      setNotice('已取得本次术语检查。适用但尚未提交的恢复选择需要再按一次“核对术语”；待确认记录不会自动提交。');
    } catch (cause) {
      if (!abort.signal.aborted && generation.current === mine) { setPhase('error'); setError(cause instanceof Error ? cause.message : '请求失败，稿件已保留。'); }
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
    if (existing < 0 && choices.length >= MAX_CHOICES) { setNotice('最多保留 32 处选择，请先移除不再需要的记录。'); return; }
    remember(); discardInFlight();
    setChoices(old => existing < 0 ? [...old, selected] : old.map((c, i) => i === existing ? selected : c));
    setImported(old => old.filter(c => c !== choices[existing]));
    setNotice('已暂存此处选择；可继续选择其他位置，最后统一重新核对。');
  }
  function replace(finding: Finding, candidate: Candidate) {
    if (!latest || latest.target !== target || !sourceCompatible || !finding.target_span) return;
    const parts = highlightSegments(target, finding.target_span);
    const forms = finding.candidates.flatMap(c => [c.zh, c.en]);
    if (!parts || !forms.includes(finding.target_span.text)) return;
    editText('target', parts.before + (direction === 'en' ? candidate.en : candidate.zh) + parts.after);
  }
  function selectedSpan(box: HTMLTextAreaElement | null): Span | null {
    if (!box || box.selectionStart === box.selectionEnd) return null;
    return { start: Array.from(box.value.slice(0, box.selectionStart)).length, end: Array.from(box.value.slice(0, box.selectionEnd)).length, text: box.value.slice(box.selectionStart, box.selectionEnd) };
  }
  function addAlignment(omit = false, whole = false) {
    const a = whole ? { start: 0, end: sourceLength, text: source } : selectedSpan(sourceBox.current);
    const b = omit ? null : whole ? { start: 0, end: targetLength, text: target } : selectedSpan(targetBox.current);
    if (!a || (!omit && !b)) { setNotice('先分别选中原文和译文的对应文字，再确认范围。'); return; }
    const next = [...(whole ? [] : alignments ?? []), { source: a, target: b }].sort((x, y) => x.source.start - y.source.start);
    if (!validAlignments(next, source, target)) { setNotice('范围重叠、顺序不一致或文字已变化，请调整选择。'); return; }
    edit({ ...draft, alignments: next }, '已记录用户确认的对应范围。范围只约束术语检查，不证明句意相同；范围外不评估。');
  }
  function undo() {
    const last = history.at(-1); if (!last) return;
    discardInFlight(); setDraft(last.draft); setChoices(last.choices); setImported(last.imported); setHistory(stack => stack.slice(0, -1));
    if (last.reports) setReports(last.reports);
    setNotice(last.reports ? '已恢复交接前的稿件、选择与历史报告；仍需重新核对。' : '已撤销本地修改；仍需重新核对。');
  }
  async function importFile(file?: File) {
    if (!file) return;
    if (file.size > MAX_WORKFILE_BYTES) { setError('稿件文件不能超过 1 MiB；当前稿件未被替换。'); return; }
    const mine = generation.current;
    try {
      const value = parseWorkfile(await file.text());
      if (generation.current !== mine) { setNotice('读取文件期间稿件发生变化，未覆盖当前内容，请重新导入。'); return; }
      remember(); discardInFlight();
      setDraft({ source: value.source, target: value.target, direction: value.direction, alignments: value.alignments });
      setChoices(value.choices); setImported(value.choices);
      setReports(value.history.map((s: Draft & { report: Report; resolutions?: Resolution[] }) => ({ ...s, resolutions: s.resolutions ?? null, trusted: false, signature: '' })));
      setNotice('已在本地恢复稿件，未上传。历史报告不是当前认证；请主动核对新依据。');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '文件无效；当前稿件未被替换。'); }
    finally { if (fileBox.current) fileBox.current.value = ''; }
  }
  function save() {
    try {
      downloadFile('wuwaterm-manuscript.json', serializeWorkfile({ ...draft, choices, history: reports.map(cleanSnapshot) }));
      setNotice('已保存本地稿件文件。下次可导入续作；文件包含原文和译文，请自行妥善保管。');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '无法保存，请检查稿件内容。'); }
  }
  function exportResult() {
    downloadFile('wuwaterm-result.json', JSON.stringify({ format: 'wuwaterm-result-v2', ...draft,
      status: current ? 'current_terminology_check' : 'requires_recheck', sentence_meaning_evaluated: false,
      report: latest ? cleanSnapshot(latest) : null, report_is_current: current,
      choices: choices.map((choice, i) => ({ ...choice, status: reconciled[i].status, reason: reconciled[i].reason })),
      changes: current ? comparison : null, verified_stamp: { valid: false },
    }, null, 2));
    setNotice(current ? '已导出当前文本与术语检查结果；未认证整句语义。' : '已导出当前文本，并明确标记需要重新核对。');
  }
  return <section className="workspace-card review-card" aria-labelledby="review-title">
    <dialog className="handoff-dialog" ref={handoffDialog} aria-labelledby="handoff-title" aria-describedby="handoff-description" onCancel={() => setPendingDraft(null)}>
      <h3 id="handoff-title">替换审校稿件？</h3>
      <p id="handoff-description">将替换当前稿件、术语选择、对应范围和报告，并停止等待当前检查。可以撤销此次替换；需要续作时请先保存稿件文件。</p>
      <div className="actions">
        <button ref={keepDraftButton} type="button" className="secondary-button" onClick={() => { handoffDialog.current?.close(); setPendingDraft(null); }}>保留当前稿件</button>
        <button type="button" className="secondary-button" onClick={() => { if (pendingDraft) receiveDraft(pendingDraft); }}>替换稿件</button>
      </div>
    </dialog>
    <div className="card-heading"><div><p className="section-kicker">03 / REVIEW</p><h2 id="review-title">双语稿件工作台</h2></div><span className="tag">本地续作</span></div>
    <p className="card-intro">保存稿件文件，下次导入继续修改。只有主动核对时才提交两框文本；不保存云端稿件，不调用整句模型。</p>
    {!latest && !source.trim() && !target.trim() && choices.length === 0 && (
      <ol className="guide-steps" aria-label="上手步骤" role="list">
        <li><strong>粘贴原文和已有译文</strong><span>两侧都填，并选择译文语言。</span></li>
        <li><strong>点「核对术语」</strong><span>只检查官方术语约束；空结果不算已核。</span></li>
        <li><strong>按发现选择官方词对</strong><span>选择会暂存，需再核对一次生效；合句拆句先在「分段对应」里确认范围。</span></li>
      </ol>
    )}
    <div className="actions draft-actions">
      <button type="button" className="secondary-button" onClick={save}>保存稿件</button>
      <button type="button" className="secondary-button" onClick={() => fileBox.current?.click()}>导入稿件</button>
      <input ref={fileBox} className="visually-hidden" type="file" accept=".json,application/json" aria-label="选择稿件文件" onChange={e => void importFile(e.target.files?.[0])} />
      <button type="button" className="secondary-button" onClick={() => downloadFile('wuwaterm-translation.txt', target, 'text/plain;charset=utf-8')}>导出译文</button>
      <button type="button" className="secondary-button" onClick={exportResult}>导出当前结果</button>
    </div>
    <form onSubmit={submit}>
      <div className="label-row"><label htmlFor="review-source">原文</label><select aria-label="译文语言" value={direction} onChange={e => edit({ ...draft, direction: e.target.value as 'en' | 'zh', alignments: alignments === null ? null : [] }, '方向已改变：旧报告失效，请重新确认选择与对应关系。')}><option value="en">译文为英文</option><option value="zh">译文为中文</option></select></div>
      <textarea ref={sourceBox} id="review-source" value={source} onChange={e => editText('source', e.target.value)} rows={5} placeholder="粘贴原文；可选中文字来确认分段对应…" />
      <label htmlFor="review-target">已有译文</label>
      <textarea ref={targetBox} id="review-target" value={target} onChange={e => editText('target', e.target.value)} rows={5} placeholder="粘贴已有译文…" />
      <div className="field-hint"><span>请勿输入敏感或个人信息</span><span>原文 {sourceLength.toLocaleString()} / 2,000 · 译文 {targetLength.toLocaleString()} / 2,000</span></div>
      <details className="alignment-panel"><summary>分段对应 · {alignments === null ? '按句序检查' : `${alignments.length} 个用户确认范围`}</summary>
        <p>合句或拆句时，分别选中两框的对应文字并确认，可包含多个句子。默认按句序检查只在句数一致时适用；术语位置相同不证明句意相同。</p>
        <div className="actions">
          <button type="button" className="secondary-button" onClick={() => addAlignment()}>确认选中范围对应</button>
          <button type="button" className="secondary-button" onClick={() => addAlignment(true)}>选中原文不评估</button>
          <button type="button" className="secondary-button" disabled={!validInput} onClick={() => addAlignment(false, true)}>确认全文为一个对应段</button>
          <button type="button" className="secondary-button" onClick={() => edit({ ...draft, alignments: [] }, '已清空对应范围；没有确认的区域将不评估。')}>清空对应</button>
          <button type="button" className="secondary-button" onClick={() => edit({ ...draft, alignments: null }, '已恢复按句序检查；旧报告失效。')}>恢复按句序</button>
        </div>
        {alignments?.map((a, i) => <div className="alignment-row" key={i}><p>原文 {a.source.start + 1}–{a.source.end}：{a.source.text}</p><p>{a.target ? `译文 ${a.target.start + 1}–${a.target.end}：${a.target.text}` : '此段不评估'}</p><button type="button" className="text-button" onClick={() => edit({ ...draft, alignments: alignments.filter((_, j) => i !== j) }, '已移除此对应，相关检查已失效。')}>移除对应</button></div>)}
      </details>
      <div className="actions">
        <button type="submit" disabled={!validInput || phase === 'loading'}>{phase === 'loading' ? '核对中…' : '核对术语'}</button>
        <button className="secondary-button" type="button" disabled={!validInput || phase === 'loading'} onClick={() => void check(false)}>仅获取新依据</button>
        {phase === 'loading' && <button className="secondary-button" type="button" onClick={() => { discardInFlight(); setPhase('cancelled'); setNotice('已停止等待；已获准的请求额度不会返还，迟到结果不会采用。'); }}>取消等待</button>}
        <button className="secondary-button" type="button" onClick={undo} disabled={!history.length}>撤销修订</button>
      </div>
    </form>
    {notice && <p className="notice-state" role="status">{notice}</p>}
    {error && <p className="error-panel" role="alert">{error}</p>}
    {phase === 'loading' && <p role="status">正在核对；编辑或导入会停止本页等待，已获准的额度不返还。</p>}
    {basisChanged && <p className="review-stale">词典或规则版本变化：历史结论不能沿用，请根据新依据重新确认选择。</p>}
    {choices.length > 0 && <div className="choice-records"><h3>保留的局部选择 · {ready.length} 处适用 / {choices.length - ready.length} 处待确认</h3>
      {choices.map((choice, i) => <div className="choice-record" key={i}><p>{choice.source_span.text} · {choice.candidate ? `${choice.candidate.zh} / ${choice.candidate.en}` : '这里不是术语'} — {reconciled[i].status === 'applicable' ? '可沿用，需纳入本次核对' : '待确认'}</p><p>{reconciled[i].reason}</p><button type="button" className="text-button" onClick={() => { remember(); discardInFlight(); setChoices(old => old.filter((_, j) => i !== j)); }}>移除选择</button></div>)}
    </div>}
    {latest ? <>
      <div className="review-summary" aria-live="polite"><p>{summarizeReview(latest.report).detail}</p>
        {current ? <p className="review-current">当前报告与文本、方向和已提交选择一致。仅检查术语约束，未评估整句语义。</p> : <p className="review-stale">{latest.trusted ? '报告已失效或还有未核对的选择；请重新核对。' : '来自文件的历史报告，仅供参考，不是当前认证。'}</p>}
        <p>规则 {latest.report.rule_version} · 词典 {latest.report.dictionary.source_commit?.slice(0, 12) ?? '来源版本未知'}</p>
      </div>
      {comparison && <details className="report-changes" open><summary>与上次报告比较{current ? '' : '（当前编辑尚未核对）'}</summary>
        {([['new', '新发现'], ['resolved', '已解决的术语约束'], ['pending', '仍待确认'], ['incomparable', '无法比较']] as const).map(([key, label]) => <div key={key}><h4>{label} · {comparison[key].length}</h4>{comparison[key].map((item: { label: string; reason: string }, i: number) => <p key={i}>{item.label}：{item.reason}</p>)}</div>)}
        <p>发现消失不代表问题解决；通过术语约束也不代表整句语义正确。</p>
      </details>}
      <div className="review-findings">{latest.report.findings.length === 0 && <p>没有术语发现，整句含义仍未评估。</p>}
        {latest.report.findings.map(finding => {
          const sourceHit = latest.source === source ? highlightSegments(source, finding.source_span) : null;
          const targetHit = latest.target === target && finding.target_span ? highlightSegments(target, finding.target_span) : null;
          return <article className="term-result" key={finding.id} data-mention-id={finding.id}><p className="result-summary">{VERDICTS[finding.verdict]} · 原文 {finding.source_span.start + 1}–{finding.source_span.end}{finding.candidates_truncated ? ' · 候选已截断' : ''}</p>
            <p className="review-excerpt">{sourceHit ? <>{sourceHit.before}<mark>{sourceHit.hit}</mark>{sourceHit.after}</> : `${finding.source_span.text}（历史位置，请重新核对）`}</p>
            <p className="review-excerpt">{targetHit ? <>{targetHit.before}<mark>{targetHit.hit}</mark>{targetHit.after}</> : '译文未可靠定位或已编辑，不使用旧位置。'}</p>
            {finding.candidates.map(candidate => <div className="term-pair" key={candidate.candidate_id}><strong>{candidate.zh}</strong><span>{candidate.en}</span><small>{candidate.category}</small>
              <button className="text-button" type="button" disabled={!sourceCompatible || phase === 'loading' || latest.report.truncated} onClick={() => choose(finding, candidate)}>采用此官方词对</button>
              {targetHit && <button className="text-button" type="button" disabled={!sourceCompatible || phase === 'loading'} onClick={() => replace(finding, candidate)}>替换已定位词语</button>}
            </div>)}
            <button className="text-button" type="button" disabled={!sourceCompatible || phase === 'loading'} onClick={() => choose(finding, null)}>这里不是术语</button>
          </article>;
        })}
      </div>
    </> : <p className="translation-placeholder">粘贴或导入稿件后主动核对。空结果不会被视为已核。</p>}
  </section>;
}
