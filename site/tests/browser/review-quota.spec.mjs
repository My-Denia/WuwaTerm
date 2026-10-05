import { test, expect } from '@playwright/test';
import { fixture } from '../fixtures/manuscript.mjs';

const BASE = process.env.WUWATERM_BROWSER_URL ?? '';
const SOURCE = '今汐。';
const TARGET = 'Jinhsi.';

const ZH_FULL = '今日共享审校还剩 60 / 60 次。这是全站快照，提交时重新核对，不是留给你个人的额度。';
const ZH_EMPTY = '今日共享审校还剩 0 / 60 次。这是全站快照，提交时重新核对，不是留给你个人的额度。';
const EN_FULL = 'Shared reviews left today: 60 / 60. This is a site-wide snapshot, rechecked when you submit, not a personal reserve.';
const EN_EMPTY = 'Shared reviews left today: 0 / 60. This is a site-wide snapshot, rechecked when you submit, not a personal reserve.';

function poolBody(reviewsRemaining) {
  return {
    status: 'available',
    translation_enabled: true,
    terms: { used: 0, limit: 100, remaining: 100 },
    translations: { used: 0, limit: 30, remaining: 30 },
    characters: { used: 0, limit: 12000, remaining: 12000 },
    reviews: { used: 60 - reviewsRemaining, limit: 60, remaining: reviewsRemaining },
    reset_at: '2099-01-01T00:00:00Z',
  };
}

async function cellStrongText(page, label) {
  const cells = page.locator('.pool-strip div');
  const count = await cells.count();
  for (let i = 0; i < count; i += 1) {
    const cell = cells.nth(i);
    const labelText = await cell.locator('span').first().textContent();
    if (labelText === label) return cell.locator('strong').first().evaluate(node => node.textContent);
  }
  return null;
}

async function exactText(locator) {
  return locator.evaluate(node => node.textContent);
}

async function switchTo(page, language) {
  const button = page.locator(`.lang-toggle button:text-is("${language}")`);
  await expect(button).toBeVisible();
  await expect(async () => {
    await button.click();
    await expect(button).toHaveAttribute('aria-pressed', 'true');
  }).toPass({ timeout: 10_000 });
}

async function fillPair(page) {
  await page.locator('#review-source').fill(SOURCE);
  await page.locator('#review-target').fill(TARGET);
}

test('first-run shows a full shared review allowance in Chinese and English', async ({ page }) => {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pool') return route.fulfill({ json: poolBody(60) });
    throw new Error(`Unexpected API request: ${path}`);
  });
  await page.goto(BASE + '/');
  await expect.poll(async () => cellStrongText(page, '今日审校'), { timeout: 30_000 }).toBe('60 / 60 次');
  expect(await cellStrongText(page, '今日术语查询')).toBe('100 / 100 次');
  await fillPair(page);
  await expect.poll(async () => exactText(page.locator('.review-quota p').first())).toBe(ZH_FULL);
  await expect(page.locator('body')).toContainText('显示为快照，提交时重新核对');
  await expect(page.getByRole('button', { name: '核对术语', exact: true })).toBeEnabled();

  await switchTo(page, 'English');
  expect(await cellStrongText(page, 'Reviews today')).toBe('60 / 60');
  expect(await exactText(page.locator('.review-quota p').first())).toBe(EN_FULL);
  await expect(page.locator('body')).toContainText('Shown as a snapshot; rechecked when you submit.');
  await expect(page.getByRole('button', { name: 'Check terms', exact: true })).toBeEnabled();
});

test('a full review quota disables the two admission buttons and leaves local actions usable', async ({ page }) => {
  const reviews = [];
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pool') return route.fulfill({ json: poolBody(0) });
    if (path === '/api/reviews') {
      reviews.push(route.request().postDataJSON());
      return route.fulfill({ json: fixture(SOURCE, TARGET).report });
    }
    throw new Error(`Unexpected API request: ${path}`);
  });
  await page.goto(BASE + '/');
  await expect.poll(async () => cellStrongText(page, '今日审校'), { timeout: 30_000 }).toBe('0 / 60 次');
  await fillPair(page);
  await expect.poll(async () => exactText(page.locator('.review-quota p').first())).toBe(ZH_EMPTY);
  await expect(page.locator('.review-quota')).toContainText('今日审校共享额度已用完');
  const check = page.getByRole('button', { name: '核对术语', exact: true });
  const fresh = page.getByRole('button', { name: '仅获取新依据', exact: true });
  await expect(check).toBeDisabled();
  await expect(fresh).toBeDisabled();
  const leaked = page.waitForRequest(request => new URL(request.url()).pathname === '/api/reviews', { timeout: 1_000 }).then(() => true, () => false);
  await check.click({ force: true });
  await fresh.click({ force: true });
  expect(await leaked).toBe(false);
  expect(reviews).toHaveLength(0);

  for (const name of ['保存稿件', '导入稿件', '导出译文', '导出当前结果', '导出 Markdown 报告']) {
    await expect(page.getByRole('button', { name, exact: true })).toBeEnabled();
  }
  await page.locator('#review-source').fill('今汐在。');
  await expect(page.getByRole('button', { name: '撤销修订', exact: true })).toBeEnabled();
  await page.locator('.alignment-panel > summary').click();
  await expect(page.getByRole('button', { name: '确认选中范围对应', exact: true })).toBeEnabled();

  await switchTo(page, 'English');
  expect(await cellStrongText(page, 'Reviews today')).toBe('0 / 60');
  expect(await exactText(page.locator('.review-quota p').first())).toBe(EN_EMPTY);
  await expect(page.getByRole('button', { name: 'Check terms', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Fetch fresh basis only', exact: true })).toBeDisabled();
});

test('adopt and not-term stay available after a check once the snapshot reaches zero', async ({ page }) => {
  let remaining = 60;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pool') return route.fulfill({ json: poolBody(remaining) });
    if (path === '/api/reviews') {
      const body = route.request().postDataJSON();
      return route.fulfill({ json: fixture(body.source, body.target, body.direction).report });
    }
    throw new Error(`Unexpected API request: ${path}`);
  });
  await page.goto(BASE + '/');
  await expect.poll(async () => cellStrongText(page, '今日审校'), { timeout: 30_000 }).toBe('60 / 60 次');
  await fillPair(page);
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page.getByRole('button', { name: '采用此官方词对', exact: true }).first()).toBeEnabled();
  remaining = 0;
  await page.getByRole('button', { name: '刷新额度', exact: true }).click();
  await expect.poll(async () => cellStrongText(page, '今日审校')).toBe('0 / 60 次');
  await expect.poll(async () => exactText(page.locator('.review-quota p').first())).toBe(ZH_EMPTY);
  await expect(page.getByRole('button', { name: '采用此官方词对', exact: true }).first()).toBeEnabled();
  await expect(page.getByRole('button', { name: '这里不是术语', exact: true }).first()).toBeEnabled();
});

test('a 200 pool response without reviews is an incomplete snapshot and does not disable review', async ({ page }) => {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pool') return route.fulfill({ json: {
      status: 'available',
      translation_enabled: true,
      terms: { used: 0, limit: 100, remaining: 100 },
      translations: { used: 0, limit: 60, remaining: 60 },
      characters: { used: 0, limit: 12000, remaining: 12000 },
      reset_at: '2099-01-01T00:00:00Z',
    } });
    throw new Error(`Unexpected API request: ${path}`);
  });
  await page.goto(BASE + '/');
  await expect(page.locator('.pool-strip')).toContainText('额度状态暂不可用', { timeout: 30_000 });
  await expect(page.locator('.review-quota')).toContainText('额度状态暂不可用');
  const labels = await page.locator('.pool-strip div span').evaluateAll(nodes => nodes.map(node => node.textContent));
  expect(labels.includes('今日审校')).toBe(false);
  const body = await page.locator('body').evaluate(node => node.textContent ?? '');
  expect(body.includes('60 / 60')).toBe(false);
  await fillPair(page);
  await expect(page.getByRole('button', { name: '核对术语', exact: true })).toBeEnabled();
});

test('limits explains the independent review quota and does not read the live pool', async ({ page }) => {
  let poolCalls = 0;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pool') poolCalls += 1;
    return route.fulfill({ status: 200, json: {} });
  });
  await page.goto(BASE + '/limits');
  await expect(page.locator('body')).toContainText('审校另有独立的每日次数', { timeout: 30_000 });
  await expect(page.locator('body')).toContainText('首页共享额度显示的是剩余快照');
  await expect(page.locator('body')).toContainText('审校用尽不会关闭术语查询或整句翻译');
  expect(poolCalls).toBe(0);
  await switchTo(page, 'English');
  await expect(page.locator('body')).toContainText('Review has its own daily count');
  await expect(page.locator('body')).toContainText('The home page shows that remaining amount as a snapshot');
  await expect(page.locator('body')).toContainText('Using up reviews does not close term lookup');
  expect(poolCalls).toBe(0);
});
