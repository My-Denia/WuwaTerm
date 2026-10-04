import { DictionaryProvenance } from './components/dictionary-provenance';
import { ReviewWorkbench } from './components/review-workbench';
import { TranslationWorkbench } from './components/translation-workbench';
import { SiteChrome } from './components/site-chrome';

export default function Home() {
  return (
    <main id="top" className="site-main">
      <SiteChrome>
        <TranslationWorkbench />
        <ReviewWorkbench />
        <DictionaryProvenance />
      </SiteChrome>
    </main>
  );
}
