import { cookies } from 'next/headers';
import { UI_LANGUAGE_COOKIE, uiLanguageOrZh } from '../lib/ui-language';

/** Server-side read of the persisted interface language; first visits resolve to zh. */
export async function readUiLanguage() {
  const store = await cookies();
  return uiLanguageOrZh(store.get(UI_LANGUAGE_COOKIE)?.value);
}
