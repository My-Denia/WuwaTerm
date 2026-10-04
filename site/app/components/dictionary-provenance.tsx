'use client';

import { useRef, useState } from 'react';
import { msg } from '../../lib/messages';
import { numberLocale } from '../../lib/ui-language';
import { useUiLanguage } from './ui-language-context';

type Provenance = {
  schema_version: string | null;
  source_commit: string | null;
  game_version: string | null;
  resource_version: string | null;
  changelist: string | null;
  term_count: number;
  request_id: string;
};
type Snapshot = { kind: 'success'; data: Provenance } | { kind: 'error' };

const KEYS = [
  'changelist', 'game_version', 'request_id', 'resource_version',
  'schema_version', 'source_commit', 'term_count',
];

function nullable(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && value.length > 0 && value.length <= 256);
}

function isProvenance(value: unknown): value is Provenance {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  if (Object.keys(record).sort().join('\u0000') !== [...KEYS].sort().join('\u0000')) return false;
  return nullable(record.schema_version)
    && nullable(record.source_commit)
    && nullable(record.game_version)
    && nullable(record.resource_version)
    && nullable(record.changelist)
    && Number.isInteger(record.term_count)
    && (record.term_count as number) >= 0
    && typeof record.request_id === 'string'
    && record.request_id.length > 0;
}

export function DictionaryProvenance() {
  const { lang } = useUiLanguage();
  const m = msg(lang);
  const snapshot = useRef<Snapshot | null>(null);
  const inflight = useRef(false);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<Snapshot | { kind: 'loading' } | null>(null);

  function reported(value: string | null): string {
    if (value === null) return m.provenance.notProvided;
    if (value === 'unavailable') return m.provenance.notRecorded;
    return value;
  }

  function plain(value: string | null): string {
    return value === null ? m.provenance.plainNotProvided : value;
  }

  async function load(force: boolean) {
    setOpen(true);
    if (inflight.current) return;
    if (!force && snapshot.current) {
      setView(snapshot.current);
      return;
    }
    inflight.current = true;
    setBusy(true);
    setView({ kind: 'loading' });
    try {
      const response = await fetch('/api/meta', { cache: 'no-store' });
      const body: unknown = await response.json().catch(() => null);
      const next: Snapshot = response.ok && isProvenance(body)
        ? { kind: 'success', data: body }
        : { kind: 'error' };
      snapshot.current = next;
      setView(next);
    } catch {
      const next: Snapshot = { kind: 'error' };
      snapshot.current = next;
      setView(next);
    } finally {
      inflight.current = false;
      setBusy(false);
    }
  }

  return (
    <section className="provenance" aria-label={m.provenance.aria}>
      <p>{m.provenance.intro}</p>
      <div className="provenance-actions">
        <button className="secondary-button" type="button" onClick={() => void load(false)}>{m.provenance.open}</button>
      </div>
      {open && (
        <>
          <p>{m.provenance.quotaNote}</p>
          {view?.kind === 'loading' && <p role="status">{m.provenance.loading}</p>}
          {view?.kind === 'error' && <p role="status">{m.provenance.error}</p>}
          {view?.kind === 'success' && (
            <dl>
              <div className="provenance-row"><dt>{m.provenance.game}</dt><dd>{reported(view.data.game_version)}</dd></div>
              <div className="provenance-row"><dt>{m.provenance.resource}</dt><dd>{reported(view.data.resource_version)}</dd></div>
              <div className="provenance-row"><dt>{m.provenance.changelist}</dt><dd>{reported(view.data.changelist)}</dd></div>
              <div className="provenance-row"><dt>{m.provenance.sourceCommit}</dt><dd>{plain(view.data.source_commit)}</dd></div>
              <div className="provenance-row"><dt>{m.provenance.terms}</dt><dd>{view.data.term_count.toLocaleString(numberLocale(lang))}</dd></div>
              <div className="provenance-row"><dt>{m.provenance.schema}</dt><dd>{plain(view.data.schema_version)}</dd></div>
              <div className="provenance-row"><dt>{m.provenance.requestId}</dt><dd>{view.data.request_id}</dd></div>
            </dl>
          )}
          <div className="provenance-actions">
            <button className="secondary-button" type="button" disabled={busy} onClick={() => void load(true)}>{m.provenance.reread}</button>
            <button className="secondary-button" type="button" onClick={() => setOpen(false)}>{m.provenance.collapse}</button>
          </div>
        </>
      )}
    </section>
  );
}
