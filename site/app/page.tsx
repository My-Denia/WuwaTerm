import { DictionaryProvenance } from './components/dictionary-provenance';
import { ReviewWorkbench } from './components/review-workbench';
import { TranslationWorkbench } from './components/translation-workbench';

export default function Home() {
  return (
    <main id="top" className="site-main">
      <nav className="topbar" aria-label="站点导航">
        <a className="brand" href="#top" aria-label="WuwaTerm 首页">
          <span className="brand-mark" aria-hidden="true">W</span>
          <span><strong>WuwaTerm</strong><small>鸣潮官方术语</small></span>
        </a>
        <div className="nav-links"><a href="/limits">共享额度</a><a href="/privacy">隐私说明</a><span className="beta-badge">匿名公测</span></div>
      </nav>
      <TranslationWorkbench />
      <ReviewWorkbench />
      <DictionaryProvenance />
      <footer><p>WuwaTerm · 为鸣潮玩家搭建的中英语言工具</p><div><a href="/limits">使用与限额</a><a href="/privacy">隐私说明</a></div></footer>
    </main>
  );
}
