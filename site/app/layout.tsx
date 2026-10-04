import type { Metadata } from 'next';
import { publicOrigin } from '@/lib/public-metadata';
import { msg } from '@/lib/messages';
import { htmlLang, ogLocale, uiLanguageOrZh, type UiLanguage } from '@/lib/ui-language';
import { readUiLanguage } from './ui-language-server';
import { UiLanguageProvider } from './components/ui-language-context';
import './globals.css';

export async function generateMetadata(): Promise<Metadata> {
  const [origin, lang] = await Promise.all([publicOrigin(), readUiLanguage()]);
  const title = msg(lang).meta.title;
  const description = msg(lang).meta.description;
  return {
    title, description,
    ...(origin ? { metadataBase: new URL(origin), alternates: { canonical: '/' } } : {}),
    openGraph: { title, description, locale: ogLocale(lang), type: 'website', ...(origin ? { url: origin, images: [{ url: origin + '/og.png', alt: title }] } : {}) },
    twitter: { card: 'summary_large_image', title, description, ...(origin ? { images: [origin + '/og.png'] } : {}) },
    robots: { index: !!origin, follow: !!origin, nocache: true },
  };
}
export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const lang: UiLanguage = uiLanguageOrZh(await readUiLanguage());
  return <html lang={htmlLang(lang)}><body><UiLanguageProvider initial={lang}>{children}</UiLanguageProvider></body></html>;
}
