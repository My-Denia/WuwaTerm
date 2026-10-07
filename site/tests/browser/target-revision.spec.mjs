import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { fixture, freshApiReport } from '../fixtures/manuscript.mjs';
import { serializeWorkfile, makeChoice } from '../../lib/manuscript.js';

const BASE = process.env.WUWATERM_BROWSER_URL ?? '';
const engine = JSON.parse(readFileSync(new URL('../fixtures/target-revision-engine.json', import.meta.url), 'utf8'));
const openName = '指定译文片段并替换一次';
const confirmName = '只替换这一处';
const dialog = page => page.getByRole('dialog', { name: '只修订用户指定的一处译文' });
async function download(page, name) {
  const waiting = page.waitForEvent('download');
  await page.getByRole('button', { name, exact: true }).click();
  const parts = [];
  for await (const part of await (await waiting).createReadStream()) parts.push(part);
  return Buffer.concat(parts).toString();
}
async function open(page, data = engine.cases[0], responder) {
  const requests = [];
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pool') return route.fulfill({ json: {
      status: 'available', translation_enabled: true,
      terms: { used: 0, limit: 100, remaining: 100 }, translations: { used: 0, limit: 100, remaining: 100 },
      characters: { used: 0, limit: 10000, remaining: 10000 }, reviews: { used: 0, limit: 60, remaining: 60 }, reset_at: '2099-01-01T00:00:00Z',
    } });
    if (path !== '/api/reviews') throw new Error(`Unexpected API admission ${path}`);
    const body = route.request().postDataJSON(); requests.push(body);
    const report = responder ? responder(body, requests.length) : body.target === data.before_request.target ? data.before_report : data.after_report;
    return route.fulfill({ json: report });
  });
  await page.goto(BASE + '/');
  await expect(page.locator('.pool-strip')).toContainText('100', { timeout: 30_000 });
  await page.locator('#review-source').fill(data.before_request.source);
  await page.locator('#review-target').fill(data.before_request.target);
  if (data.before_request.direction === 'zh') await page.getByLabel('译文语言').selectOption('zh');
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page.locator('.review-summary')).toContainText('当前报告');
  return requests;
}
async function start(page, index = 0) {
  const button = page.locator('.review-findings .term-result').nth(index).getByRole('button', { name: openName, exact: true });
  await button.click();
  await expect(dialog(page).getByLabel('要替换的确切译文片段')).toBeFocused();
  await expect(dialog(page).getByLabel('要替换的确切译文片段')).toHaveValue('');
  await expect(dialog(page).getByRole('button', { name: confirmName })).toBeDisabled();
  return button;
}
async function select(page, text, index = 0) {
  await dialog(page).getByLabel('要替换的确切译文片段').fill(text);
  const radios = dialog(page).getByRole('radio');
  for (const radio of await radios.all()) await expect(radio).not.toBeChecked();
  await radios.nth(index).check();
}
for (const [caseIndex, fragment] of [[0, 'sound shell'], [1, 'Jinxi'], [2, '回音'], [3, 'sound shell']]) {
  test(`real-engine ${engine.cases[caseIndex].name}: explicit occurrence edit and explicit request/receipt`, async ({ page }, info) => {
    const data = engine.cases[caseIndex];
    const requests = await open(page, data);
    const findingIndex = caseIndex === 3 ? 1 : 0;
    const finding = page.locator('.review-findings .term-result').nth(findingIndex);
    expect(await finding.getAttribute('data-mention-id')).toBe(data.selected_finding_id);
    const before = JSON.parse(await download(page, '保存稿件'));
    await start(page, findingIndex);
    await select(page, fragment, caseIndex === 3 ? 1 : 0);
    await expect(dialog(page).locator('.revision-preview')).toContainText(fragment);
    await dialog(page).getByRole('button', { name: confirmName }).click();
    await expect(page.locator('#review-target')).toHaveValue(data.after_request.target);
    await expect(page.locator('#review-target')).toBeFocused();
    expect(requests.length).toBe(1);
    const edited = JSON.parse(await download(page, '保存稿件'));
    expect(edited.choices).toEqual(before.choices);
    expect(edited.history).toEqual(before.history);
    expect(edited).not.toHaveProperty('revision');
    expect(await download(page, '导出译文')).toBe(data.after_request.target);
    const result = JSON.parse(await download(page, '导出当前结果'));
    expect(result.status).toBe('requires_recheck'); expect(result.report_is_current).toBe(false);
    expect(result.report.target).toBe(data.before_request.target); expect(result.changes).toBeNull();
    const markdown = await download(page, '导出 Markdown 报告');
    expect(markdown).toContain('需要重新核对'); expect(markdown).toContain(data.after_request.target);
    await info.attach('saved-after-one-splice', { body: JSON.stringify(edited, null, 2), contentType: 'application/json' });
    await page.getByRole('button', { name: '撤销修订', exact: true }).click();
    await expect(page.locator('#review-target')).toHaveValue(data.before_request.target);
    await expect(page.locator('.review-summary')).not.toContainText('当前报告与文本');
    await start(page, findingIndex); await select(page, fragment, caseIndex === 3 ? 1 : 0);
    await dialog(page).getByRole('button', { name: confirmName }).click();
    await page.getByRole('button', { name: '核对术语', exact: true }).click();
    await expect(page.locator('.review-summary')).toContainText('当前报告');
    expect(requests.length).toBe(2); expect(requests[1]).toEqual(data.after_request);
    await info.attach('request-response-map-real-engine', { body: JSON.stringify({ requests, after_report: data.after_report }, null, 2), contentType: 'application/json' });
  });
}

test('cancel and Escape preserve canonical manuscript; changed fragment clears explicit singleton selection', async ({ page }) => {
  const requests = await open(page);
  const before = await download(page, '保存稿件');
  const button = await start(page);
  await dialog(page).getByLabel('要替换的确切译文片段').fill(' \n ');
  await expect(dialog(page).getByRole('radio')).toHaveCount(0);
  await dialog(page).getByLabel('要替换的确切译文片段').fill('SOUND SHELL');
  await expect(dialog(page)).toContainText('没有找到完全相同');
  await select(page, 'sound shell');
  await expect(dialog(page).getByRole('button', { name: confirmName })).toBeEnabled();
  await dialog(page).getByLabel('要替换的确切译文片段').fill('shell');
  await expect(dialog(page).getByRole('radio')).not.toBeChecked();
  await expect(dialog(page).getByRole('button', { name: confirmName })).toBeDisabled();
  await dialog(page).getByRole('button', { name: '取消修订' }).click();
  await expect(button).toBeFocused();
  expect(await download(page, '保存稿件')).toBe(before);
  await start(page); await select(page, 'sound shell'); await page.keyboard.press('Escape');
  expect(await download(page, '保存稿件')).toBe(before); expect(requests.length).toBe(1);
});

test('adoption is separate; unchanged-text staged choice can revise and explicit recheck submits ready choice', async ({ page }) => {
  const data = engine.cases[0], requests = await open(page, data);
  await page.locator('.review-findings .term-result').first().getByRole('button', { name: '采用此官方词对' }).click();
  await expect(page.locator('#review-target')).toHaveValue(data.before_request.target);
  const adopted = JSON.parse(await download(page, '保存稿件'));
  expect(adopted.choices).toHaveLength(1);
  await start(page); await select(page, 'sound shell'); await dialog(page).getByRole('button', { name: confirmName }).click();
  expect(JSON.parse(await download(page, '保存稿件')).choices).toEqual(adopted.choices); expect(requests.length).toBe(1);
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page.locator('.review-summary')).toContainText('当前报告');
  expect(requests[1]).toEqual(data.choice_recheck_request);
});

test('imported choices/provenance, explicit alignments and historical reports survive as records; one undo restores', async ({ page }) => {
  const data = engine.cases[0]; const requests = await open(page, data);
  const source = data.before_request.source, target = data.before_request.target;
  const alignments = [{ source: { start: 0, end: Array.from(source).length, text: source }, target: { start: 0, end: target.length, text: target } }];
  const choice = makeChoice({ source, direction: 'en', alignments, report: data.before_report, finding: data.before_report.findings[0], candidate: data.before_report.findings[0].candidates[0] });
  const content = serializeWorkfile({ source, target, direction: 'en', alignments, choices: [choice], history: [{ source, target, direction: 'en', alignments, report: data.before_report, resolutions: [] }] });
  await page.getByLabel('选择稿件文件').setInputFiles({ name: 'synthetic.json', mimeType: 'application/json', buffer: Buffer.from(content) });
  await expect(page.getByRole('button', { name: openName })).toBeDisabled();
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page.getByRole('button', { name: openName })).toBeEnabled();
  const before = JSON.parse(await download(page, '保存稿件'));
  await start(page); await select(page, 'sound shell'); await dialog(page).getByRole('button', { name: confirmName }).click();
  const edited = JSON.parse(await download(page, '保存稿件'));
  expect(edited.alignments).toEqual([]); expect(edited.choices).toEqual(before.choices); expect(edited.history).toEqual(before.history);
  const result = JSON.parse(await download(page, '导出当前结果'));
  expect(result.choices[0].status).not.toBe('applicable');
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  const restored = JSON.parse(await download(page, '保存稿件'));
  expect(restored).toEqual(before); expect(requests.length).toBe(2);
  const stale = JSON.parse(await download(page, '导出当前结果')); expect(stale.report_is_current).toBe(false);
  await page.getByLabel('选择稿件文件').setInputFiles({ name: 'revised.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(edited)) });
  await expect(page.locator('#review-target')).toHaveValue(data.after_request.target);
  expect(JSON.parse(await download(page, '保存稿件'))).toEqual(edited);
});

for (const mutation of ['source', 'target', 'direction', 'alignment', 'choice', 'undo', 'handoff', 'new-check', 'import']) {
  test(`pending revision cancels before ${mutation}; retained confirm cannot splice`, async ({ page }) => {
    const requests = await open(page); await start(page); await select(page, 'sound shell');
    const target = await page.locator('#review-target').inputValue();
    if (mutation === 'import') {
      const content = serializeWorkfile({ source: '今汐。', target: 'Jinxi.', direction: 'en', alignments: null, choices: [], history: [] });
      await page.getByLabel('选择稿件文件').setInputFiles({ name: 'new.json', mimeType: 'application/json', buffer: Buffer.from(content) });
    } else await page.evaluate(mutation => {
      const confirm = [...document.querySelectorAll('.revision-dialog button')].find(b => b.textContent === '只替换这一处');
      const clickText = text => [...document.querySelectorAll('button')].find(b => b.textContent === text).click();
      if (mutation === 'source' || mutation === 'target') {
        const box = document.querySelector('#review-' + mutation);
        const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
        setter.call(box, box.value + '!'); box.dispatchEvent(new Event('input', { bubbles: true }));
      } else if (mutation === 'direction') { const box = document.querySelector('.review-card select'); box.value = 'zh'; box.dispatchEvent(new Event('change', { bubbles: true })); }
      else if (mutation === 'alignment') clickText('确认全文为一个对应段');
      else if (mutation === 'choice') clickText('采用此官方词对');
      else if (mutation === 'undo') clickText('撤销修订');
      else if (mutation === 'handoff') window.dispatchEvent(new CustomEvent('wuwaterm-send-review', { detail: { source: '今汐。', target: 'Jinxi.', direction: 'en' } }));
      else if (mutation === 'new-check') document.querySelector('.review-card form').requestSubmit();
      confirm.click();
    }, mutation);
    await expect(dialog(page)).not.toBeVisible();
    const expected = mutation === 'target' ? target + '!' : mutation === 'import' ? 'Jinxi.' : mutation === 'undo' ? '' : target;
    await expect(page.locator('#review-target')).toHaveValue(expected);
    expect(requests.length).toBe(mutation === 'new-check' ? 2 : 1);
  });
}

test('raw CRLF edit selects second displayed multiline occurrence without changing outside bytes', async ({ page }) => {
  const source = '😀声骸。声骸。', target = ' 😀sound\r\nshell / sound\rshell ';
  const data = { before_request: { source, target, direction: 'en' } };
  const requests = await open(page, { before_request: { source, target: 'placeholder', direction: 'en' } }, body => freshApiReport(fixture(body.source, body.target).report));
  const content = serializeWorkfile({ source, target, direction: 'en', alignments: null, choices: [], history: [] });
  await page.getByLabel('选择稿件文件').setInputFiles({ name: 'raw.json', mimeType: 'application/json', buffer: Buffer.from(content) });
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page.getByRole('button', { name: openName }).first()).toBeEnabled();
  await start(page, 1); await select(page, 'sound\nshell', 1); await dialog(page).getByRole('button', { name: confirmName }).click();
  expect(await download(page, '导出译文')).toBe(' 😀sound\r\nshell / Echo ');
  expect(JSON.parse(await download(page, '保存稿件')).source).toBe(data.before_request.source); expect(requests.length).toBe(2);
});

test('keyboard selects an occurrence and confirms; language rerender retains fragment and selected intent', async ({ page }) => {
  const data = engine.cases[3]; await open(page, data);
  const button = page.locator('.review-findings .term-result').nth(1).getByRole('button', { name: openName });
  await button.focus(); await page.keyboard.press('Enter');
  await page.keyboard.type('sound shell'); await page.keyboard.press('Tab'); await page.keyboard.press('Tab');
  await expect(dialog(page).getByRole('radio').first()).toBeFocused();
  await page.keyboard.press('Space'); await page.keyboard.press('ArrowDown');
  await expect(dialog(page).getByRole('radio').nth(1)).toBeChecked();
  await page.evaluate(() => [...document.querySelectorAll('button')].find(b => b.textContent === 'English').click());
  const english = page.getByRole('dialog', { name: 'Revise one user-selected translation occurrence' });
  await expect(english.getByLabel('Exact translation fragment to replace')).toHaveValue('sound shell');
  await expect(english.getByRole('radio').nth(1)).toBeChecked();
  await page.keyboard.press('Tab'); await page.keyboard.press('Tab'); await page.keyboard.press('Enter');
  await expect(page.locator('#review-target')).toHaveValue(data.after_request.target);
});

test('null-span no-op and length excess are disabled without mutation', async ({ page }) => {
  const source = '声骸。', target = 'Echo';
  await open(page, { before_request: { source, target, direction: 'en' } }, body => {
    const report = freshApiReport(fixture(body.source, body.target).report); report.findings[0].target_span = null; return report;
  });
  const before = await download(page, '保存稿件'); await start(page); await select(page, 'Echo');
  await expect(dialog(page)).toContainText('替换前后完全相同'); await expect(dialog(page).getByRole('button', { name: confirmName })).toBeDisabled();
  await page.keyboard.press('Escape'); expect(await download(page, '保存稿件')).toBe(before);
  await page.locator('#review-target').fill('x'.repeat(1999) + 'a'); await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page.getByRole('button', { name: openName })).toBeEnabled(); await start(page); await select(page, 'a');
  await expect(dialog(page)).toContainText('超过 2,000'); await expect(dialog(page).getByRole('button', { name: confirmName })).toBeDisabled();
});

test.describe('touch input on emitted client', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  test('tap to open, select second occurrence, confirm; 375px English dialog wraps and Escape cancels', async ({ page }, info) => {
    const data = engine.cases[3]; const requests = await open(page, data);
    expect(await page.evaluate(() => navigator.maxTouchPoints)).toBeGreaterThan(0);
    await page.locator('.review-findings .term-result').nth(1).getByRole('button', { name: openName }).tap();
    await dialog(page).getByLabel('要替换的确切译文片段').fill('sound shell');
    await dialog(page).getByRole('radio').nth(1).tap();
    await info.attach('touch-selected', { body: await page.screenshot(), contentType: 'image/png' });
    await dialog(page).getByRole('button', { name: confirmName }).tap();
    await expect(page.locator('#review-target')).toHaveValue(data.after_request.target); expect(requests.length).toBe(1);
    await page.getByRole('button', { name: '撤销修订', exact: true }).tap();
    await page.setViewportSize({ width: 375, height: 844 }); await page.getByRole('button', { name: 'English', exact: true }).tap();
    await page.locator('.review-findings .term-result').nth(1).getByRole('button', { name: 'Specify a translation fragment to replace once' }).tap();
    const english = page.getByRole('dialog', { name: 'Revise one user-selected translation occurrence' });
    await english.getByLabel('Exact translation fragment to replace').fill('sound shell'); await english.getByRole('radio').nth(1).tap();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
    expect(await english.evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
    await english.getByRole('button', { name: 'Cancel revision' }).tap(); await expect(page.locator('#review-target')).toHaveValue(data.before_request.target);
  });
});

// Synthetic ambiguous candidate report exercises the pre-existing located shortcut.
test('trusted located shortcut replaces one official-form span and does not adopt a choice', async ({ page }) => {
  const source = '今汐。今汐。', target = 'Jinhsi. Jinhsi.';
  const { echo } = await import('../fixtures/manuscript.mjs');
  await open(page, { before_request: { source, target, direction: 'en' } }, body => {
    const report = freshApiReport(fixture(body.source, body.target).report);
    report.findings[1].target_span = { start: 8, end: 14, text: 'Jinhsi' };
    report.findings[1].candidates.push(echo);
    return report;
  });
  await page.locator('.review-findings .term-result').nth(1).getByRole('button', { name: '替换已定位词语', exact: true }).nth(1).click();
  await expect(page.locator('#review-target')).toHaveValue('Jinhsi. Echo.');
  expect(JSON.parse(await download(page, '保存稿件')).choices).toEqual([]);
});

test('identical and invalid handoffs keep pending intent; no automatic second modal', async ({ page }) => {
  const data = engine.cases[0]; await open(page); await start(page); await select(page, 'sound shell');
  await page.evaluate(data => {
    window.dispatchEvent(new CustomEvent('wuwaterm-send-review', { detail: data.before_request }));
    window.dispatchEvent(new CustomEvent('wuwaterm-send-review', { detail: { source: 123 } }));
  }, data);
  await expect(dialog(page).getByRole('radio')).toBeChecked();
  await expect(page.locator('dialog[open]')).toHaveCount(1);
  await dialog(page).getByRole('button', { name: confirmName }).click();
  await expect(page.locator('#review-target')).toHaveValue(data.after_request.target);
});
