import { test, expect } from '@playwright/test';
import { makeChoice, serializeWorkfile } from '../../lib/manuscript.js';
import { candidate, fixture } from '../fixtures/manuscript.mjs';

const BASE = process.env.WUWATERM_BROWSER_URL ?? '';
const POOL = {
  status: 'available', translation_enabled: true,
  terms: { used: 0, limit: 100, remaining: 100 },
  translations: { used: 0, limit: 100, remaining: 100 },
  characters: { used: 0, limit: 10000, remaining: 10000 },
  reviews: { used: 0, limit: 60, remaining: 60 },
  reset_at: '2099-01-01T00:00:00Z',
};

function manuscriptFile() {
  const draft = fixture('今汐。', 'Jinhsi.', 'en');
  const choice = makeChoice({
    source: draft.source, direction: draft.direction, alignments: null,
    report: draft.report, finding: draft.report.findings[0], candidate,
  });
  const history = [{
    source: draft.source, target: draft.target, direction: draft.direction,
    alignments: null, report: draft.report, resolutions: [],
  }];
  return Buffer.from(serializeWorkfile({
    source: draft.source, target: draft.target, direction: draft.direction,
    alignments: null, choices: [choice], history,
  }));
}

async function open(page, reviews) {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pool') return route.fulfill({ json: POOL });
    if (path === '/api/reviews' && reviews) return reviews(route);
    throw new Error(`Unexpected API request: ${path}`);
  });
  await page.goto(BASE + '/');
  await expect(page.locator('.review-quota')).toContainText('60', { timeout: 30_000 });
}

const state = page => page.locator('.manuscript-save-state');
const leaveDialog = page => page.getByRole('dialog', { name: '离开审校页？' });

async function download(page, name) {
  const waiting = page.waitForEvent('download');
  await page.getByRole('button', { name, exact: true }).click();
  return waiting;
}

test('blank workbench opens privacy without a leave dialog', async ({ page }) => {
  await open(page);
  await expect(state(page)).toContainText('还没有需要保存的审校内容');
  await expect(state(page)).not.toContainText('浏览器自己的确认框');
  await Promise.all([
    page.waitForURL(/\/privacy$/),
    page.getByRole('link', { name: '隐私说明' }).first().click(),
  ]);
  await expect(leaveDialog(page)).toHaveCount(0);
});

test('typed work stays when leaving is cancelled and goes when leaving is confirmed', async ({ page }) => {
  await open(page);
  await page.locator('#review-source').fill('只在此页。');
  await expect(state(page)).toContainText('目前只在此页');
  await expect(state(page)).not.toContainText('已保存本地稿件文件');
  await page.getByRole('link', { name: '隐私说明' }).first().click();
  await expect(leaveDialog(page)).toBeVisible();
  await page.getByRole('button', { name: '留在此页', exact: true }).click();
  await expect(leaveDialog(page)).toHaveCount(0);
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator('#review-source')).toHaveValue('只在此页。');
  await page.getByRole('button', { name: 'English', exact: true }).click();
  await expect(state(page)).toContainText('only on this page');
  await expect(page.locator('#review-source')).toHaveValue('只在此页。');
  await page.getByRole('button', { name: '中文', exact: true }).click();
  await page.locator('#review-target').fill('Jinhsi.');
  await page.getByRole('link', { name: '隐私说明' }).first().click();
  await expect(leaveDialog(page)).toBeVisible();
  const saving = page.waitForEvent('download');
  await page.getByRole('button', { name: '保存稿件并留下', exact: true }).click();
  await saving;
  await expect(page).toHaveURL(/\/$/);
  await expect(leaveDialog(page)).toBeVisible();
  await expect(state(page)).toContainText('不能确认文件已写入磁盘');
  await expect(page.locator('#review-source')).toHaveValue('只在此页。');
  await Promise.all([
    page.waitForURL(/\/privacy$/),
    page.getByRole('button', { name: '仍要离开', exact: true }).click(),
  ]);
});

test('a manuscript download is not a confirmed disk write and still asks before leaving', async ({ page }) => {
  await open(page);
  await page.locator('#review-source').fill('今汐。');
  await page.locator('#review-target').fill('Jinhsi.');
  await download(page, '保存稿件');
  await expect(state(page)).toContainText('不能确认文件已写入磁盘');
  await expect(page.locator('.notice-state')).toContainText('不能确认文件已写入磁盘');
  await expect(page.locator('.notice-state')).not.toContainText('已保存本地稿件文件');
  await page.getByRole('link', { name: '隐私说明' }).first().click();
  await expect(leaveDialog(page)).toBeVisible();
  await page.getByRole('button', { name: '留在此页', exact: true }).click();
  await expect(page.locator('#review-source')).toHaveValue('今汐。');
  await expect(page.locator('#review-target')).toHaveValue('Jinhsi.');
});

test('txt, result and markdown exports do not become the recoverable manuscript', async ({ page }) => {
  await open(page);
  await page.locator('#review-source').fill('今汐。');
  await page.locator('#review-target').fill('Jinhsi.');
  for (const name of ['导出译文', '导出当前结果', '导出 Markdown 报告']) {
    await download(page, name);
    await expect(state(page)).toContainText('目前只在此页');
    await expect(page.locator('.notice-state')).toContainText('不是可导入的稿件');
  }
});

test('import match follows the choice and returns after undo', async ({ page }) => {
  await open(page);
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'manuscript.json', mimeType: 'application/json', buffer: manuscriptFile(),
  });
  await expect(state(page)).toContainText('这次导入的稿件一致');
  await Promise.all([
    page.waitForURL(/\/limits$/),
    page.getByRole('link', { name: '使用与限额', exact: true }).click(),
  ]);
  await page.goto(BASE + '/');
  await expect(page.locator('.review-quota')).toContainText('60');
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'manuscript.json', mimeType: 'application/json', buffer: manuscriptFile(),
  });
  await expect(state(page)).toContainText('这次导入的稿件一致');
  await page.getByRole('button', { name: '移除选择', exact: true }).click();
  await expect(state(page)).toContainText('已经和最近一份可恢复稿件不同');
  await page.getByRole('link', { name: '隐私说明' }).first().click();
  await expect(leaveDialog(page)).toBeVisible();
  await page.getByRole('button', { name: '留在此页', exact: true }).click();
  await expect(page.locator('#review-source')).toHaveValue('今汐。');
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  await expect(state(page)).toContainText('这次导入的稿件一致');
  await expect(page.locator('.choice-records')).toContainText('今汐');
  await Promise.all([
    page.waitForURL(/\/limits$/),
    page.getByRole('link', { name: '使用与限额', exact: true }).click(),
  ]);
});

test('undo restores an imported manuscript after a source edit', async ({ page }) => {
  await open(page);
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'manuscript.json', mimeType: 'application/json', buffer: manuscriptFile(),
  });
  await page.locator('#review-source').fill('今汐。啊');
  await expect(state(page)).toContainText('已经和最近一份可恢复稿件不同');
  await page.getByRole('link', { name: '隐私说明' }).first().click();
  await expect(leaveDialog(page)).toBeVisible();
  await page.getByRole('button', { name: '留在此页', exact: true }).click();
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  await expect(page.locator('#review-source')).toHaveValue('今汐。');
  await expect(state(page)).toContainText('这次导入的稿件一致');
  await Promise.all([
    page.waitForURL(/\/limits$/),
    page.getByRole('link', { name: '使用与限额', exact: true }).click(),
  ]);
});

test('reload asks before discarding typed work and staying keeps it', async ({ page }) => {
  await open(page);
  await page.locator('#review-source').fill('刷新前的原文。');
  await page.locator('#review-source').click();
  const dialog = page.waitForEvent('dialog');
  // Dismissing the browser prompt cancels the reload, so this navigation does not settle.
  const reloading = page.reload({ timeout: 5_000 }).catch(error => error);
  const prompt = await dialog;
  expect(prompt.type()).toBe('beforeunload');
  await prompt.dismiss();
  await reloading;
  await expect(page.locator('#review-source')).toHaveValue('刷新前的原文。');
});

test('a running check is named in the leave dialog and is not released', async ({ page }) => {
  let reviews = 0;
  let released = false;
  await open(page, async () => {
    reviews += 1;
    await new Promise(() => {});
    released = true;
  });
  await page.locator('#review-source').fill('今汐。');
  await page.locator('#review-target').fill('Jinhsi.');
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect.poll(() => reviews).toBe(1);
  await page.getByRole('link', { name: '隐私说明' }).first().click();
  await expect(leaveDialog(page)).toContainText('尚未返回的结果');
  await page.getByRole('button', { name: '留在此页', exact: true }).click();
  await expect(page.locator('#review-source')).toHaveValue('今汐。');
  await expect(page.locator('#review-target')).toHaveValue('Jinhsi.');
  expect(released).toBe(false);
});

test.describe('mobile leave dialog', () => {
  test.use({ viewport: { width: 390, height: 844 } });
  test('staying keeps the draft inside a 390px viewport', async ({ page }) => {
    await open(page);
    await page.locator('#review-source').fill('窄屏原文。');
    await page.getByRole('link', { name: '隐私说明' }).first().click();
    const dialog = leaveDialog(page);
    await expect(dialog).toBeVisible();
    const box = await dialog.boundingBox();
    expect(box?.width ?? 0).toBeLessThanOrEqual(390);
    expect(box?.x ?? -1).toBeGreaterThanOrEqual(0);
    await page.getByRole('button', { name: '留在此页', exact: true }).click();
    await expect(page.locator('#review-source')).toHaveValue('窄屏原文。');
    await expect(state(page)).toContainText('目前只在此页');
  });
});

test('the in-page home link does not open the leave dialog', async ({ page }) => {
  await open(page);
  await page.locator('#review-source').fill('只在此页。');
  await page.getByRole('link', { name: 'WuwaTerm 首页' }).click();
  await expect(leaveDialog(page)).toHaveCount(0);
  await expect(page.locator('#review-source')).toHaveValue('只在此页。');
});
