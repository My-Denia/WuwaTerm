export type Allowance = { used: number; limit: number; remaining: number };
export type Pool = {
  status: 'available';
  translation_enabled: boolean;
  terms: Allowance;
  translations: Allowance;
  characters: Allowance;
  reviews: Allowance;
  reset_at: string;
};

export function isPool(v: unknown): v is Pool {
  const x = v as Pool;
  return !!x && x.status === 'available' && typeof x.translation_enabled === 'boolean'
    && [x.terms, x.translations, x.characters, x.reviews].every(a => a && Number.isInteger(a.remaining) && Number.isInteger(a.limit) && a.remaining >= 0 && a.limit >= a.remaining)
    && typeof x.reset_at === 'string';
}

export const POOL_REFRESH_EVENT = 'wuwaterm-pool-refresh';

export function requestPoolRefresh(): void {
  window.dispatchEvent(new Event(POOL_REFRESH_EVENT));
}
