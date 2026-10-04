'use client';

import { createContext, useCallback, useContext, useState } from 'react';
import { htmlLang, type UiLanguage } from '../../lib/ui-language';
import { persistUiLanguage } from '../ui-language-cookie';

type UiLanguageContextValue = {
  lang: UiLanguage;
  setLang: (next: UiLanguage) => void;
};

const UiLanguageContext = createContext<UiLanguageContextValue>({ lang: 'zh', setLang: () => {} });

export function UiLanguageProvider({ initial, children }: { initial: UiLanguage; children: React.ReactNode }) {
  const [lang, setState] = useState<UiLanguage>(initial);
  const setLang = useCallback((next: UiLanguage) => {
    persistUiLanguage(next);
    document.documentElement.lang = htmlLang(next);
    setState(next);
  }, []);
  return <UiLanguageContext.Provider value={{ lang, setLang }}>{children}</UiLanguageContext.Provider>;
}

export function useUiLanguage() {
  return useContext(UiLanguageContext);
}
