export const UI_LANGUAGE_COOKIE = 'wuwaterm-lang';
export const UI_LANGUAGES = ['zh', 'en'] as const;
export type UiLanguage = (typeof UI_LANGUAGES)[number];

export function isUiLanguage(value: unknown): value is UiLanguage {
  return value === 'zh' || value === 'en';
}

/** First visit and any unreadable value fall back to Chinese, the original language. */
export function uiLanguageOrZh(value: unknown): UiLanguage {
  return isUiLanguage(value) ? value : 'zh';
}

export function numberLocale(lang: UiLanguage): string {
  return lang === 'zh' ? 'zh-CN' : 'en-US';
}

export function ogLocale(lang: UiLanguage): string {
  return lang === 'zh' ? 'zh_CN' : 'en_US';
}

export function htmlLang(lang: UiLanguage): string {
  return lang === 'zh' ? 'zh-CN' : 'en';
}
