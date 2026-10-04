'use client';

import { useUiLanguage } from './ui-language-context';
import { msg } from '../../lib/messages';

export function LanguageToggle() {
  const { lang, setLang } = useUiLanguage();
  const m = msg(lang);
  return (
    <div className="lang-toggle" role="group" aria-label={m.language.toggleAria}>
      <button type="button" aria-pressed={lang === 'zh'} onClick={() => setLang('zh')}>{m.language.zh}</button>
      <button type="button" aria-pressed={lang === 'en'} onClick={() => setLang('en')}>{m.language.en}</button>
    </div>
  );
}
