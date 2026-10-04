import { test, expect } from '@playwright/test';
import { fixture } from '../fixtures/manuscript.mjs';
import { serializeWorkfile } from '../../lib/manuscript.js';

const BASE = process.env.WUWATERM_BROWSER_URL ?? '';

async function open(page) {
  const calls = [];
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    calls.push(path);
    if (path !== '/api/pool') throw new Error(`Unexpected admission: ${path}`);
    await route.fulfill({ json: {
      status: 'available', translation_enabled: true,
      terms: { used: 0, limit: 100, remaining: 100 },
      translations: { used: 0, limit: 100, remaining: 100 },
      characters: { used: 0, limit: 10000, remaining: 10000 },
      reset_at: '2099-01-01T00:00:00Z',
    } });
  });
  await page.goto(BASE + '/');
  await expect(page.locator('.pool-strip')).toContainText('100', { timeout: 30_000 });
  return calls;
}

async function download(page, name) {
  const waiting = page.waitForEvent('download');
  await page.getByRole('button', { name, exact: true }).click();
  const parts = [];
  for await (const part of await (await waiting).createReadStream()) parts.push(part);
  return Buffer.concat(parts);
}

for (const [label, breaks] of [['CRLF', ['\r\n', '\r\n']], ['CR', ['\r', '\r']], ['mixed', ['\r\n', '\r']]]) {
  test(`imported ${label} locates source and translation after emoji and line breaks without changing bytes`, async ({ page }) => {
    const calls = await open(page);
    const source = `  😀前${breaks[0]}今汐。${breaks[1]}声骸。  `;
    const target = ` 😀prefix${breaks[0]}Jinhsi.${breaks[1]}Echo. `;
    const report = fixture(source, target, 'en').report;
    const content = serializeWorkfile({ source, target, direction: 'en', alignments: null, choices: [],
      history: [{ source, target, direction: 'en', alignments: null, report, resolutions: [] }] });
    await page.getByLabel('选择稿件文件').setInputFiles({ name: 'linebreaks.json', mimeType: 'application/json', buffer: Buffer.from(content) });
    await expect(page.locator('.review-summary')).toContainText('历史报告');
    const before = await download(page, '保存稿件');
    const findings = page.locator('.review-findings .term-result');
    for (let index = 0; index < 2; index += 1) {
      await findings.nth(index).getByRole('button', { name: '定位原文', exact: true }).click();
      expect(await page.locator('#review-source').evaluate(box => box.value.slice(box.selectionStart, box.selectionEnd))).toBe(report.findings[index].source_span.text);
      await findings.nth(index).getByRole('button', { name: '定位译文', exact: true }).click();
      expect(await page.locator('#review-target').evaluate(box => box.value.slice(box.selectionStart, box.selectionEnd))).toBe(report.findings[index].target_span.text);
    }
    const after = await download(page, '保存稿件');
    expect(after.equals(before)).toBe(true);
    const saved = JSON.parse(after.toString());
    expect(saved.source).toBe(source);
    expect(saved.target).toBe(target);
    expect(saved.history[0].report.source_revision).toBe(report.source_revision);
    expect(saved.history[0].report.target_revision).toBe(report.target_revision);
    expect(await download(page, '导出译文')).toEqual(Buffer.from(target));
    expect(calls.filter(path => path !== '/api/pool')).toEqual([]);
  });
}

test('manual alignment uses raw CRLF report coordinates for selected normalized text', async ({ page }) => {
  await open(page);
  const source = '😀前\r\n今汐。';
  const target = '😀prefix\r\nJinhsi.';
  const content = serializeWorkfile({ source, target, direction: 'en', alignments: null, choices: [], history: [] });
  await page.getByLabel('选择稿件文件').setInputFiles({ name: 'alignment.json', mimeType: 'application/json', buffer: Buffer.from(content) });
  await expect(page.locator('#review-source')).toHaveValue(source.replace(/\r\n/g, '\n'));
  for (const [selector, term] of [['#review-source', '今汐'], ['#review-target', 'Jinhsi']]) {
    await page.locator(selector).evaluate((box, term) => {
      const start = box.value.indexOf(term);
      box.setSelectionRange(start, start + term.length);
    }, term);
  }
  await page.locator('.alignment-panel > summary').click();
  await page.locator('.alignment-panel .actions button').first().click();
  await expect(page.locator('.alignment-row')).toHaveCount(1);
  const saved = JSON.parse((await download(page, '保存稿件')).toString());
  const expected = fixture(source, target).report.findings[0];
  expect(saved.alignments).toEqual([{ source: expected.source_span, target: expected.target_span }]);
  expect(saved.source).toBe(source);
  expect(saved.target).toBe(target);
  // A selected range spanning a normalized newline must retain raw CRLF text.
  await page.getByRole('button', { name: '清空对应', exact: true }).click();
  for (const selector of ['#review-source', '#review-target']) {
    await page.locator(selector).evaluate(box => box.setSelectionRange(0, box.value.length));
  }
  await page.getByRole('button', { name: '确认选中范围对应', exact: true }).click();
  const whole = JSON.parse((await download(page, '保存稿件')).toString());
  expect(whole.alignments).toEqual([{
    source: { start: 0, end: Array.from(source).length, text: source },
    target: { start: 0, end: Array.from(target).length, text: target },
  }]);
});

test('English header fits the available width of a 390px viewport with a classic scrollbar', async ({ page }) => {
  // 390px viewport minus a 15px classic scrollbar leaves 375 CSS pixels.
  await page.setViewportSize({ width: 375, height: 844 });
  await open(page);
  await page.getByRole('button', { name: 'English', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
  await page.getByRole('button', { name: '中文', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
});
