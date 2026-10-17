'use client';

import { useSyncExternalStore } from 'react';

export const QUERY_CHANGE_EVENT = 'wuwaterm-query-change';

function subscribe(callback: () => void) {
  if (typeof window === 'undefined') return () => {};
  window.addEventListener('popstate', callback);
  window.addEventListener(QUERY_CHANGE_EVENT, callback);
  return () => {
    window.removeEventListener('popstate', callback);
    window.removeEventListener(QUERY_CHANGE_EVENT, callback);
  };
}

function getQuerySnapshot(): string {
  if (typeof window === 'undefined') return '';
  return new URLSearchParams(window.location.search).get('q') || '';
}

function getServerSnapshot(): string {
  return '';
}

/** Hook returning current 'q' search param reactively on client, empty on server. */
export function useTermQuery(): string {
  return useSyncExternalStore(subscribe, getQuerySnapshot, getServerSnapshot);
}

/** Synchronize the browser URL immediately and notify all listening links of query change. */
export function setBrowserQuery(target: string | null) {
  if (typeof window === 'undefined' || !window.history?.replaceState) return;
  const u = new URL(window.location.href);
  const trimmed = (target || '').trim();
  if (trimmed) {
    u.searchParams.set('q', trimmed);
  } else {
    u.searchParams.delete('q');
  }
  window.history.replaceState(null, '', u.toString());
  window.dispatchEvent(new CustomEvent(QUERY_CHANGE_EVENT, { detail: trimmed }));
}

/** Append current or given 'q' parameter to an internal pathname. */
export function withQuery(href: string, q?: string): string {
  const query = (q !== undefined ? q : (typeof window !== 'undefined' ? new URLSearchParams(window.location.search).get('q') || '' : '')).trim();
  const [base] = href.split('?');
  if (!query) return base;
  return `${base}?q=${encodeURIComponent(query)}`;
}
