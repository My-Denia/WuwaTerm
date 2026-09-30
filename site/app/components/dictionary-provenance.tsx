'use client';

import { useRef, useState } from 'react';

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

function reported(value: string | null): string {
  if (value === null) return '版本字段未提供';
  if (value === 'unavailable') return '当前服务未记录该版本';
  return value;
}

function plain(value: string | null): string {
  return value === null ? '未提供' : value;
}

export function DictionaryProvenance() {
  const snapshot = useRef<Snapshot | null>(null);
  const inflight = useRef(false);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<Snapshot | { kind: 'loading' } | null>(null);

  async function load(force: boolean) {
    if (inflight.current) return;
    if (!force && snapshot.current) {
      setView(snapshot.current);
      setOpen(true);
      return;
    }
    inflight.current = true;
    setBusy(true);
    setOpen(true);
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
    <section className="provenance" aria-label="词典版本">
      <p>以下是当前服务实际报告的词典数据，不是本仓库的目标版本。</p>
      <div className="provenance-actions">
        <button className="secondary-button" type="button" onClick={() => void load(false)}>查看词典版本</button>
      </div>
      {open && (
        <>
          <p>打开或重新读取各会计入一次今日词典信息共享额度。</p>
          {view?.kind === 'loading' && <p role="status">正在读取当前服务报告的词典数据…</p>}
          {view?.kind === 'error' && <p role="status">当前版本无法确认。</p>}
          {view?.kind === 'success' && (
            <dl>
              <div className="provenance-row"><dt>游戏数据</dt><dd>{reported(view.data.game_version)}</dd></div>
              <div className="provenance-row"><dt>资源版本</dt><dd>{reported(view.data.resource_version)}</dd></div>
              <div className="provenance-row"><dt>Changelist</dt><dd>{reported(view.data.changelist)}</dd></div>
              <div className="provenance-row"><dt>source commit</dt><dd>{plain(view.data.source_commit)}</dd></div>
              <div className="provenance-row"><dt>词条数</dt><dd>{view.data.term_count.toLocaleString('zh-CN')}</dd></div>
              <div className="provenance-row"><dt>数据库结构版本</dt><dd>{plain(view.data.schema_version)}</dd></div>
              <div className="provenance-row"><dt>问题反馈编号</dt><dd>{view.data.request_id}</dd></div>
            </dl>
          )}
          <div className="provenance-actions">
            <button className="secondary-button" type="button" disabled={busy} onClick={() => void load(true)}>重新读取</button>
            <button className="secondary-button" type="button" onClick={() => setOpen(false)}>收起</button>
          </div>
        </>
      )}
    </section>
  );
}
