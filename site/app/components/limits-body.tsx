'use client';

import Link from 'next/link';
import { useEffect } from 'react';
import { POOL_LIMITS as limits } from '../../lib/shared-pool.js';
import { fill, msg } from '../../lib/messages';
import { numberLocale } from '../../lib/ui-language';
import { useUiLanguage } from './ui-language-context';
import { LanguageToggle } from './language-toggle';

export function LimitsBody() {
  const { lang } = useUiLanguage();
  const m = msg(lang);
  const locale = numberLocale(lang);
  // Keep the tab title in step with an in-place language switch.
  useEffect(() => { document.title = m.limits.title; }, [lang, m.limits.title]);
  return <main className="document"><div className="document-top"><Link className="back-link" href="/">{m.nav.back}</Link><LanguageToggle /></div><p className="eyebrow">{m.limits.eyebrow}</p><h1>{m.limits.h1}</h1><p className="document-lead">{m.limits.lead}</p>
    <section><h2>{m.limits.s1h}</h2><table><thead><tr><th>{m.limits.tableFeature}</th><th>{m.limits.tableCeiling}</th></tr></thead><tbody>
      <tr><td>{m.limits.rowTerms}</td><td>{fill(m.limits.rowTermsCeiling, { n: limits.termsPerDay })}</td></tr>
      <tr><td>{m.limits.rowTranslation}</td><td>{fill(m.limits.rowTranslationCeiling, { n: limits.translationsPerDay, chars: limits.charactersPerDay.toLocaleString(locale) })}</td></tr>
      <tr><td>{m.limits.rowReview}</td><td>{fill(m.limits.rowReviewCeiling, { n: limits.reviewsPerDay })}</td></tr>
      <tr><td>{m.limits.rowAll}</td><td>{fill(m.limits.rowAllCeiling, { n: limits.upstreamPerMinute })}</td></tr>
      <tr><td>{m.limits.rowWindow}</td><td>{fill(m.limits.rowWindowCeiling, { n: limits.translationsPerMinute })}</td></tr>
    </tbody></table><p>{m.limits.s1p}</p></section>
    <section><h2>{m.limits.s2h}</h2><p>{m.limits.s2p}</p></section>
    <section><h2>{m.limits.s3h}</h2><p>{m.limits.s3p1}</p><p>{m.limits.s3p2}</p></section>
    <section><h2>{m.limits.s4h}</h2><p>{m.limits.s4p1}</p><p>{m.limits.s4p2}</p></section>
    <section><h2>{m.limits.s5h}</h2><p>{m.limits.s5p1}</p><p>{m.limits.s5p2}</p></section>
    <footer><a href="/privacy">{m.limits.footerPrivacy}</a> · {m.limits.footerUpdated}</footer>
  </main>;
}
