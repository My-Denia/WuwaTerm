import type { UiLanguage } from '../lib/ui-language';

// The single sanctioned browser-storage write on this site: the interface
// language preference, and nothing else. verify-no-client-secret.mjs pins
// the exact assignment below; any other storage use fails the build.
export function persistUiLanguage(value: UiLanguage): void {
  if (value !== 'zh' && value !== 'en') return;
  const secure = window.location.protocol === 'https:' ? '; Secure' : '';
  document.cookie = `wuwaterm-lang=${value}; Max-Age=31536000; Path=/; SameSite=Lax${secure}`;
}
