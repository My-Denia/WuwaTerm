'use client';

import { FormEvent, useEffect, useRef, useState } from 'react';
import { fill, msg } from '../../lib/messages';
import { numberLocale } from '../../lib/ui-language';
import { useUiLanguage } from './ui-language-context';

type Failure = { status: 'unavailable'; reason: string; request_id?: string; retry_after_seconds?: number };
type Allowance = { used: number; limit: number; remaining: number };
type Pool = { status: 'available'; translation_enabled: boolean; terms: Allowance; translations: Allowance; characters: Allowance; reset_at: string };
type TermMatch = { zh: string; en: string; category: string; reason: string; score: number };
type TermsResult = { query: string; matches: TermMatch[]; request_id: string };
type TranslationResult = { kind: 'noop' | 'exact' | 'fuzzy' | 'llm'; text: string; direction: 'en' | 'zh'; dictionary_miss: boolean; request_id: string };
type State<T> = { kind: 'idle' | 'loading' | 'cancelled' } | { kind: 'success'; data: T } | { kind: 'error'; error: Failure };
const FALLBACK: Failure = { status: 'unavailable', reason: 'site_response_invalid' };
function failure(value: unknown): value is Failure { return !!value && typeof value === 'object' && (value as Failure).status === 'unavailable' && typeof (value as Failure).reason === 'string'; }
async function payload(r: Response): Promise<unknown> { try { return await r.json(); } catch { return FALLBACK; } }
function isTerms(v: unknown): v is TermsResult { const x = v as TermsResult; return !!x && typeof x.request_id === 'string' && Array.isArray(x.matches) && x.matches.every(m => typeof m.zh === 'string' && typeof m.en === 'string' && typeof m.category === 'string' && typeof m.reason === 'string' && typeof m.score === 'number'); }
function isTranslation(v: unknown): v is TranslationResult { const x = v as TranslationResult; return !!x && typeof x.text === 'string' && typeof x.request_id === 'string' && ['noop', 'exact', 'fuzzy', 'llm'].includes(x.kind) && ['en', 'zh'].includes(x.direction) && typeof x.dictionary_miss === 'boolean'; }
function isPool(v: unknown): v is Pool { const x = v as Pool; return !!x && x.status === 'available' && typeof x.translation_enabled === 'boolean' && [x.terms,x.translations,x.characters].every(a => a && Number.isInteger(a.remaining) && Number.isInteger(a.limit) && a.remaining >= 0 && a.limit >= a.remaining) && typeof x.reset_at === 'string'; }

export function TranslationWorkbench() {
  const { lang } = useUiLanguage();
  const [pool, setPool] = useState<State<Pool>>({ kind: 'loading' });
  const [poolAttempt, setPoolAttempt] = useState(0);
  const [query, setQuery] = useState('');
  const [terms, setTerms] = useState<State<TermsResult>>({ kind: 'idle' });
  const [source, setSource] = useState('');
  const [target, setTarget] = useState<'auto' | 'en' | 'zh'>('auto');
  const [translation, setTranslation] = useState<State<TranslationResult>>({ kind: 'idle' });
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const termsController = useRef<AbortController | null>(null);
  const translationController = useRef<AbortController | null>(null);
  const translationInput = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const r = await fetch('/api/pool', { cache: 'no-store', signal: controller.signal });
        const v = await payload(r);
        if (!controller.signal.aborted) setPool(r.ok && isPool(v) ? { kind: 'success', data: v } : { kind: 'error', error: failure(v) ? v : FALLBACK });
      } catch { if (!controller.signal.aborted) setPool({ kind: 'error', error: FALLBACK }); }
    })();
    return () => controller.abort();
  }, [poolAttempt]);
  useEffect(() => () => { termsController.current?.abort(); translationController.current?.abort(); }, []);

  async function lookup(event: FormEvent) {
    event.preventDefault(); if (!query.trim()) return;
    termsController.current?.abort();
    const controller = new AbortController(); termsController.current = controller;
    setTerms({ kind: 'loading' });
    try {
      const r = await fetch('/api/terms?q=' + encodeURIComponent(query.trim()), { cache: 'no-store', signal: controller.signal });
      const v = await payload(r);
      if (controller.signal.aborted || termsController.current !== controller) return;
      setTerms(r.ok && isTerms(v) ? { kind: 'success', data: v } : { kind: 'error', error: failure(v) ? v : FALLBACK });
      setPoolAttempt(n => n + 1);
    } catch { if (!controller.signal.aborted) setTerms({ kind: 'error', error: FALLBACK }); }
    finally { if (termsController.current === controller) termsController.current = null; }
  }
  async function translate(event: FormEvent) {
    event.preventDefault(); if (!source.trim()) return;
    translationController.current?.abort();
    const controller = new AbortController(); translationController.current = controller;
    setCopied(false); setCopyFailed(false); setTranslation({ kind: 'loading' });
    try {
      const r = await fetch('/api/translations', { method: 'POST', headers: { 'content-type': 'application/json' }, cache: 'no-store', body: JSON.stringify(target === 'auto' ? { text: source } : { text: source, to: target }), signal: controller.signal });
      const v = await payload(r);
      if (controller.signal.aborted || translationController.current !== controller) return;
      setTranslation(r.ok && isTranslation(v) ? { kind: 'success', data: v } : { kind: 'error', error: failure(v) ? v : FALLBACK });
      setPoolAttempt(n => n + 1);
    } catch { if (!controller.signal.aborted && translationController.current === controller) setTranslation({ kind: 'error', error: FALLBACK }); }
    finally { if (translationController.current === controller) translationController.current = null; }
  }
  function moveQueryToTranslation() {
    translationController.current?.abort(); translationController.current = null;
    setSource(query); setTranslation({ kind: 'idle' }); setCopied(false);
    translationInput.current?.focus();
  }
  function cancelTranslation() {
    translationController.current?.abort(); translationController.current = null;
    setTranslation({ kind: 'cancelled' }); setPoolAttempt(n => n + 1);
  }
  async function copyTranslation() {
    if (translation.kind !== 'success') return;
    try { await navigator.clipboard.writeText(translation.data.text); setCopied(true); setCopyFailed(false); }
    catch { setCopyFailed(true); }
  }
  const m = msg(lang);
  const locale = numberLocale(lang);
  const translationClosed = pool.kind === 'success' && (!pool.data.translation_enabled || pool.data.translations.remaining === 0 || pool.data.characters.remaining === 0);
  const sourceLength = Array.from(source).length;
  return <div className="workbench">
    <header className="workbench-header">
      <p className="eyebrow">{m.hero.eyebrow}</p>
      <h1>{m.hero.title}<span>{m.hero.titleAccent}</span></h1>
      <p>{m.hero.subtitle}</p>
    </header>
    <aside className="pool-notice" aria-label={m.pool.noticeAria}>
      <span className="notice-symbol" aria-hidden="true">↗</span>
      <div><strong>{m.pool.noticeTitle}</strong><p>{m.pool.noticeBody}</p></div>
      <a href="/limits">{m.pool.noticeLink} <span aria-hidden="true">↗</span></a>
    </aside>
    <section className="pool-strip" aria-live="polite" aria-label={m.pool.stripAria}>
      {pool.kind === 'success' ? <>
        <div><span>{m.pool.termsLabel}</span><strong>{pool.data.terms.remaining}<small>{' / ' + pool.data.terms.limit + m.pool.termsUnit}</small></strong></div>
        <div><span>{m.pool.translationsLabel}</span><strong>{pool.data.translation_enabled ? pool.data.translations.remaining : m.pool.translationsClosed}<small>{pool.data.translation_enabled ? ' / ' + pool.data.translations.limit : ''}</small></strong></div>
        <div><span>{m.pool.charactersLabel}</span><strong>{pool.data.characters.remaining.toLocaleString(locale)}<small>{m.pool.charactersUnit}</small></strong></div>
        <p>{m.pool.resetNote}</p>
      </> : <p>{pool.kind === 'loading' ? m.pool.loading : m.pool.unavailable}</p>}
      <button className="text-button" type="button" onClick={() => setPoolAttempt(n => n + 1)}>{m.pool.refresh}</button>
    </section>
    <div className="workspace-grid">
      <section className="workspace-card terms-card" aria-labelledby="terms-title">
        <div className="card-heading"><div><p className="section-kicker">{m.terms.kicker}</p><h2 id="terms-title">{m.terms.title}</h2></div><span className="tag">{m.terms.tag}</span></div>
        <p className="card-intro">{m.terms.intro}</p>
        <form onSubmit={lookup}>
          <label htmlFor="term-query">{m.terms.label}</label>
          <div className="input-row"><input id="term-query" value={query} onChange={e => { setQuery(e.target.value); setTerms({ kind: 'idle' }); }} placeholder={m.terms.placeholder} autoComplete="off" disabled={terms.kind === 'loading'} /><button type="submit" disabled={!query.trim() || Array.from(query.trim()).length > 200 || terms.kind === 'loading'}>{terms.kind === 'loading' ? m.terms.buttonLoading : m.terms.buttonIdle}</button></div>
          <p className="field-hint">{m.terms.hint}</p>
        </form>
        <TermsView state={terms} onTranslate={moveQueryToTranslation} />
      </section>
      <section className="workspace-card translation-card" aria-labelledby="translation-title">
        <div className="card-heading"><div><p className="section-kicker">{m.translation.kicker}</p><h2 id="translation-title">{m.translation.title}</h2></div><span className="tag">{m.translation.tag}</span></div>
        <p className="card-intro">{m.translation.intro}</p>
        {translationClosed && <p className="notice-state">{pool.kind === 'success' && !pool.data.translation_enabled ? m.tMsg.translation_disabled : m.tMsg.translation_pool_exhausted}</p>}
        <form onSubmit={translate}>
          <div className="label-row"><label htmlFor="translation-source">{m.translation.label}</label><select aria-label={m.translation.directionAria} value={target} disabled={translation.kind === 'loading'} onChange={e => setTarget(e.target.value as 'auto' | 'en' | 'zh')}><option value="auto">{m.translation.directionAuto}</option><option value="en">{m.translation.directionToEn}</option><option value="zh">{m.translation.directionToZh}</option></select></div>
          <textarea ref={translationInput} id="translation-source" value={source} disabled={translation.kind === 'loading'} onChange={e => { setSource(e.target.value); setTranslation({ kind: 'idle' }); setCopied(false); }} placeholder={m.translation.placeholder} rows={5} />
          <div className="field-hint"><span>{m.translation.hintSafety}</span><span aria-live="polite">{fill(m.translation.charCount, { n: sourceLength.toLocaleString(locale) })}</span></div>
          <div className="actions"><button type="submit" disabled={!source.trim() || sourceLength > 2000 || translation.kind === 'loading' || translationClosed}>{translation.kind === 'loading' ? m.translation.submitLoading : m.translation.submitIdle}</button>{translation.kind === 'loading' && <button className="secondary-button" type="button" onClick={cancelTranslation}>{m.translation.cancelWait}</button>}</div>
        </form>
        <TranslationView state={translation} copied={copied} copyFailed={copyFailed} onCopy={copyTranslation} source={source} />
      </section>
    </div>
    <div className="product-notes">{m.productNotes.map((note, i) => <p key={i}><strong>{note.title}</strong><span>{note.body}</span></p>)}</div>
  </div>;
}
function TermsView({ state, onTranslate }: { state: State<TermsResult>; onTranslate: () => void }) {
  const { lang } = useUiLanguage();
  const m = msg(lang);
  if (state.kind === 'idle') return <div className="empty-state"><span aria-hidden="true">文 ⇄ A</span><p>{m.terms.emptyTitle}</p><small>{m.terms.emptyBody}</small></div>;
  if (state.kind === 'loading') return <p className="empty-state" role="status">{m.terms.loading}</p>;
  if (state.kind === 'error') return <FailureView value={state.error} />;
  if (state.kind !== 'success') return null;
  if (!state.data.matches.length) return <div className="empty-state"><p>{m.terms.noneFound}</p><button className="text-button" type="button" onClick={onTranslate}>{m.terms.noneAction}</button></div>;
  return <div className="terms-results" aria-live="polite"><p className="result-summary">{fill(m.terms.matchesFound, { n: state.data.matches.length })}</p>{state.data.matches.map((match,i) => <article className="term-result" key={i}><div className="term-pair"><strong>{match.zh}</strong><span>{match.en}</span></div><dl><div><dt>{m.terms.category}</dt><dd>{match.category}</dd></div><div><dt>{m.terms.matchReason}</dt><dd>{match.reason}</dd></div><div><dt>{m.terms.matchScore}</dt><dd>{match.score}</dd></div></dl></article>)}<RequestId value={state.data.request_id} /></div>;
}
function TranslationView({ state, copied, copyFailed, onCopy, source }: { state: State<TranslationResult>; copied: boolean; copyFailed: boolean; onCopy: () => void; source: string }) {
  const { lang } = useUiLanguage();
  const m = msg(lang);
  if (state.kind === 'idle') return <p className="translation-placeholder">{m.translation.idlePlaceholder}</p>;
  if (state.kind === 'loading') return <p className="notice-state" role="status">{m.translation.loadingStatus}</p>;
  if (state.kind === 'cancelled') return <p className="notice-state" role="status">{m.translation.cancelledStatus}</p>;
  if (state.kind === 'error') return <FailureView value={state.error} />;
  if (state.kind !== 'success') return null;
  const result = state.data;
  function sendToReview() {
    window.dispatchEvent(new CustomEvent('wuwaterm-send-review', { detail: { source, target: result.text, direction: result.direction } }));
  }
  return <div className="translation-result" aria-live="polite"><div className="result-meta"><span>{({ exact: m.translation.kindExact, fuzzy: m.translation.kindFuzzy, noop: m.translation.kindNoop, llm: m.translation.kindLlm })[result.kind]}</span><span>{result.direction === 'en' ? m.translation.directionEn : m.translation.directionZh}</span></div>{result.dictionary_miss && <p className="field-hint">{m.translation.dictionaryMiss}</p>}<p className="translated-text">{result.text}</p><button type="button" className="secondary-button" onClick={onCopy}>{copied ? m.translation.copied : m.translation.copy}</button><button type="button" className="secondary-button" onClick={sendToReview}>{m.translation.sendToReview}</button>{copyFailed && <p role="status">{m.translation.copyFailed}</p>}<RequestId value={result.request_id} /></div>;
}
function FailureView({ value }: { value: Failure }) {
  const { lang } = useUiLanguage();
  const m = msg(lang);
  return <div className="error-panel" role="status"><p>{(m.tMsg as Record<string, string>)[value.reason] ?? m.translation.failureFallback}</p>{value.retry_after_seconds !== undefined && <p>{fill(value.retry_after_seconds >= 3600 ? m.translation.retryHours : m.translation.retrySeconds, { n: value.retry_after_seconds >= 3600 ? Math.ceil(value.retry_after_seconds / 3600) : Math.ceil(value.retry_after_seconds) })}</p>}{value.request_id && <RequestId value={value.request_id} />}</div>;
}
function RequestId({ value }: { value: string }) {
  const { lang } = useUiLanguage();
  return <details className="request-id"><summary>{msg(lang).terms.requestId}</summary><code>{value}</code></details>;
}
