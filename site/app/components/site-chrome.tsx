'use client';

import { useEffect } from 'react';
import { msg } from '../../lib/messages';
import { useUiLanguage } from './ui-language-context';
import { LanguageToggle } from './language-toggle';

export function SiteChrome({ children }: { children: React.ReactNode }) {
  const { lang } = useUiLanguage();
  const m = msg(lang);
  // The document title is server-rendered per language; keep it in step with
  // an in-place switch so the tab never shows the previous language.
  useEffect(() => { document.title = m.meta.title; }, [lang, m.meta.title]);
  return (
    <>
      <nav className="topbar" aria-label={m.nav.ariaLabel}>
        <a className="brand" href="#top" aria-label={m.nav.homeAria}>
          <span className="brand-mark" aria-hidden="true">W</span>
          <span><strong>WuwaTerm</strong><small>{m.nav.brandSmall}</small></span>
        </a>
        <div className="nav-links"><a href="/limits">{m.nav.limits}</a><a href="/privacy">{m.nav.privacy}</a><span className="beta-badge">{m.nav.betaBadge}</span><LanguageToggle /></div>
      </nav>
      {children}
      <footer><p>{m.nav.footerTagline}</p><div><a href="/limits">{m.nav.footerLimits}</a><a href="/privacy">{m.nav.footerPrivacy}</a></div></footer>
    </>
  );
}
