'use client';

import Link from 'next/link';
import { useEffect } from 'react';
import { msg } from '../../lib/messages';
import { useTermQuery, withQuery } from '../query-state';
import { useUiLanguage } from './ui-language-context';
import { LanguageToggle } from './language-toggle';

export function SiteChrome({ children }: { children: React.ReactNode }) {
  const { lang } = useUiLanguage();
  const m = msg(lang);
  const termQuery = useTermQuery();
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
        <div className="section-pills" role="navigation" aria-label={m.terms.quickJumpTitle}>
          <a href="#terms-title" className="section-pill">{m.terms.jumpTerms}</a>
          <a href="#translation-title" className="section-pill">{m.terms.jumpTranslation}</a>
          <a href="#review-title" className="section-pill">{m.terms.jumpReview}</a>
          <a href="#provenance-section" className="section-pill">{m.terms.jumpProvenance}</a>
        </div>
        <div className="nav-links"><Link href={withQuery('/limits', termQuery)}>{m.nav.limits}</Link><Link href={withQuery('/privacy', termQuery)}>{m.nav.privacy}</Link><span className="beta-badge">{m.nav.betaBadge}</span><LanguageToggle /></div>
      </nav>
      {children}
      <footer><p>{m.nav.footerTagline}</p><div><Link href={withQuery('/limits', termQuery)}>{m.nav.footerLimits}</Link><Link href={withQuery('/privacy', termQuery)}>{m.nav.footerPrivacy}</Link></div></footer>
    </>
  );
}
