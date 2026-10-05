import type { UiLanguage } from './ui-language';

// Display labels for dictionary keys the service already sends.
// Chinese category and reason words match the desktop client
// (client/src/wuwaterm_client/strings.py). Lookup is exact: spaces,
// case, and unknown keys stay untouched so callers can keep escaping them.

const CATEGORY_LABELS: Record<string, Record<UiLanguage, string>> = {
  core_term: { zh: '核心术语', en: 'Core term' },
  resonator: { zh: '共鸣者', en: 'Resonator' },
  weapon: { zh: '武器', en: 'Weapon' },
  echo: { zh: '声骸', en: 'Echo' },
  skill: { zh: '技能', en: 'Skill' },
  sonata_effect: { zh: '合鸣效果', en: 'Sonata effect' },
  location: { zh: '地区', en: 'Location' },
  item: { zh: '物品', en: 'Item' },
  speaker: { zh: '角色', en: 'Speaker' },
};

const REASON_LABELS: Record<string, Record<UiLanguage, string>> = {
  exact: { zh: '精确', en: 'Exact' },
  pinyin: { zh: '拼音全拼', en: 'Full pinyin' },
  'pinyin-abbrev': { zh: '拼音缩写', en: 'Pinyin abbreviation' },
  'pinyin-prefix': { zh: '拼音前缀', en: 'Pinyin prefix' },
  'pinyin-substring': { zh: '拼音包含', en: 'Pinyin substring' },
  fuzzy: { zh: '模糊', en: 'Fuzzy' },
  'low-score': { zh: '低相关', en: 'Low relevance' },
};

export const CATEGORY_LABEL_KEYS = Object.keys(CATEGORY_LABELS);
export const REASON_LABEL_KEYS = Object.keys(REASON_LABELS);

function labeled(table: Record<string, Record<UiLanguage, string>>, lang: UiLanguage, value: string): string {
  return Object.prototype.hasOwnProperty.call(table, value) ? table[value][lang] : value;
}

export function categoryLabel(lang: UiLanguage, value: string): string {
  return labeled(CATEGORY_LABELS, lang, value);
}

export function reasonLabel(lang: UiLanguage, value: string): string {
  return labeled(REASON_LABELS, lang, value);
}

/** Same separators as the Markdown report's label/value colon. */
export function labelJoiner(lang: UiLanguage): string {
  return lang === 'zh' ? '：' : ': ';
}
