'use client';

import { useEffect, useState } from 'react';
import { msg } from '../../lib/messages';
import { useUiLanguage } from './ui-language-context';
import { LanguageToggle } from './language-toggle';

export function SiteChrome({ children }: { children: React.ReactNode }) {
  const { lang } = useUiLanguage();
  const m = msg(lang);
  const [limitsHref, setLimitsHref] = useState('/limits');
  const [privacyHref, setPrivacyHref] = useState('/privacy');
  // The document title is server-rendered per language; keep it in step with
  // an in-place switch so the tab never shows the previous language.
  useEffect(() => { document.title = m.meta.title; }, [lang, m.meta.title]);
  function syncHrefs() {
    if (typeof window === 'undefined') return;
    const q = new URLSearchParams(window.location.search).get('q');
    const param = q && q.trim() ? '?q=' + encodeURIComponent(q.trim()) : '';
    setLimitsHref('/limits' + param);
    setPrivacyHref('/privacy' + param);
  }
  useEffect(() => {
    const timer = setTimeout(syncHrefs, 0);
    return () => clearTimeout(timer);
  }, []);
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
        <div className="nav-links"><a href={limitsHref} onPointerEnter={syncHrefs} onFocus={syncHrefs}>{m.nav.limits}</a><a href={privacyHref} onPointerEnter={syncHrefs} onFocus={syncHrefs}>{m.nav.privacy}</a><span className="beta-badge">{m.nav.betaBadge}</span><LanguageToggle /></div>
      </nav>
      {children}
      <footer><p>{m.nav.footerTagline}</p><div><a href={limitsHref} onPointerEnter={syncHrefs} onFocus={syncHrefs}>{m.nav.footerLimits}</a><a href={privacyHref} onPointerEnter={syncHrefs} onFocus={syncHrefs}>{m.nav.footerPrivacy}</a></div></footer>
    </>
  );
}
