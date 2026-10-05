import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { CATEGORY_LABEL_KEYS, REASON_LABEL_KEYS, categoryLabel, reasonLabel } from '../lib/dictionary-labels.ts';
import { renderMarkdownReport, markdownStructure } from '../lib/markdown-report.ts';
import { fixture } from './fixtures/manuscript.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = name => readFileSync(join(root, name), 'utf8');

function categoryOrderKeys() {
  const source = read('src/wuwaterm/constants.py');
  const block = source.match(/CATEGORY_ORDER = \{([\s\S]*?)\n\}/);
  assert.ok(block, 'CATEGORY_ORDER block missing');
  return [...block[1].matchAll(/"([^"]+)":/g)].map(match => match[1]);
}

function lookupReasonLiterals() {
  const source = read('src/wuwaterm/lookup.py');
  const found = new Set([...source.matchAll(/reason="([^"]+)"/g)].map(match => match[1]));
  for (const line of source.split('\n')) {
    const match = line.match(/return .+, "([^"]+)"\s*$/);
    if (match) found.add(match[1]);
  }
  return [...found];
}

function clientAssignments(prefix) {
  const source = read('client/src/wuwaterm_client/strings.py');
  return Object.fromEntries([...source.matchAll(new RegExp(`^${prefix}(\\w+) = "([^"]+)"`, 'gm'))].map(match => [match[1], match[2]]));
}

test('category and reason maps equal the service keys and the desktop Chinese copy', () => {
  assert.deepEqual([...CATEGORY_LABEL_KEYS].sort(), categoryOrderKeys().sort());
  assert.deepEqual([...REASON_LABEL_KEYS].sort(), lookupReasonLiterals().sort());

  const categories = clientAssignments('CATEGORY_LABEL_');
  assert.deepEqual(Object.keys(categories).map(name => name.toLowerCase()).sort(), [...CATEGORY_LABEL_KEYS].sort());
  for (const [name, text] of Object.entries(categories)) {
    assert.equal(categoryLabel('zh', name.toLowerCase()), text);
  }

  const reasons = {
    EXACT: 'exact',
    PINYIN_FULL: 'pinyin',
    PINYIN_INITIALS: 'pinyin-abbrev',
    PINYIN_PREFIX: 'pinyin-prefix',
    PINYIN_CONTAINS: 'pinyin-substring',
    FUZZY: 'fuzzy',
    LOW_SCORE: 'low-score',
  };
  const clientReasons = clientAssignments('REASON_LABEL_');
  assert.deepEqual(Object.keys(clientReasons).sort(), Object.keys(reasons).sort());
  for (const [name, key] of Object.entries(reasons)) {
    assert.equal(reasonLabel('zh', key), clientReasons[name]);
  }
});

test('English labels are the planned words, and only an exact key is translated', () => {
  const englishCategories = {
    core_term: 'Core term',
    resonator: 'Resonator',
    weapon: 'Weapon',
    echo: 'Echo',
    skill: 'Skill',
    sonata_effect: 'Sonata effect',
    location: 'Location',
    item: 'Item',
    speaker: 'Speaker',
  };
  for (const [key, text] of Object.entries(englishCategories)) {
    assert.equal(categoryLabel('en', key), text);
    assert.notEqual(text, key);
  }
  assert.equal(reasonLabel('en', 'pinyin-abbrev'), 'Pinyin abbreviation');
  assert.equal(reasonLabel('zh', 'pinyin-abbrev'), '拼音缩写');
  for (const value of [' item ', '<b>cat</b>', 'character', 'Echo', 'not_a_reason']) {
    assert.equal(categoryLabel('zh', value), value);
    assert.equal(categoryLabel('en', value), value);
    assert.equal(reasonLabel('zh', value), value);
    assert.equal(reasonLabel('en', value), value);
  }
});

function inputFor(latest) {
  return {
    draft: { source: latest.source, target: latest.target, direction: 'en' },
    latest, previous: null, currency: 'current', choices: [], reconciled: [], comparison: null,
  };
}

test('Markdown labels known categories and still escapes unknown ones', () => {
  const base = fixture('声骸。', 'Echo.', 'en');
  const report = structuredClone(base.report);
  report.findings[0].candidates[0].category = 'item';
  report.findings[0].candidates.push({
    ...report.findings[0].candidates[0],
    candidate_id: 'e'.repeat(64),
    zh: ' 声骸 ',
    en: ' Echo ',
    category: ' item ',
  });
  report.findings[0].candidates.push({
    ...report.findings[0].candidates[0],
    candidate_id: 'f'.repeat(64),
    zh: '声骸',
    en: 'Echo',
    category: '<b>cat</b>',
  });
  const latest = { source: base.source, target: base.target, direction: 'en', report, resolutions: null };
  const zh = renderMarkdownReport(inputFor(latest), 'zh');
  assert.equal(zh.includes('类别：`物品`'), true);
  assert.equal(zh.includes('`  item  `'), true);
  assert.equal(zh.includes('<b>cat</b>'), true);
  assert.equal(markdownStructure(zh).htmlTags, 0);
  const en = renderMarkdownReport(inputFor(latest), 'en');
  assert.equal(en.includes('Category: `Item`'), true);
  assert.equal(en.includes('`  item  `'), true);
  assert.equal(en.includes('类别：'), false);
});
