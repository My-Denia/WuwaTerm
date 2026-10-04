'use client';

import Link from 'next/link';
import { useEffect } from 'react';
import { msg } from '../../lib/messages';
import { useUiLanguage } from './ui-language-context';
import { LanguageToggle } from './language-toggle';

export function PrivacyBody() {
  const { lang } = useUiLanguage();
  const m = msg(lang);
  // Keep the tab title in step with an in-place language switch.
  useEffect(() => { document.title = m.privacy.title; }, [lang, m.privacy.title]);
  return <main className="document"><div className="document-top"><Link className="back-link" href="/">{m.nav.back}</Link><LanguageToggle /></div><p className="eyebrow">{m.privacy.eyebrow}</p><h1>{m.privacy.h1}</h1><p className="document-lead">{m.privacy.lead}</p>
    <section><h2>{m.privacy.s1h}</h2><p>{m.privacy.s1p}</p></section>
    <section><h2>{m.privacy.s2h}</h2><p>{m.privacy.s2p1}</p><p>{m.privacy.s2p2}</p><p>{m.privacy.s2p3}</p></section>
    <section><h2>{m.privacy.s3h}</h2><p>{m.privacy.s3p1}</p><p>{m.privacy.s3p2}</p></section>
    <section><h2>{m.privacy.s4h}</h2><p>{m.privacy.s4p1}</p><p>{m.privacy.s4p2}</p></section>
    <section><h2>{m.privacy.s5h}</h2><p>{m.privacy.s5p1}</p><p>{m.privacy.s5p2Before}<Link href="/limits">{m.privacy.limitsLinkText}</Link>{m.privacy.s5p2After}</p></section>
    <footer><Link href="/limits">{m.privacy.limitsLinkText}</Link> · {m.privacy.footerUpdated}</footer>
  </main>;
}
